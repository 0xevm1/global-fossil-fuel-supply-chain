import logging, sys, time

#from ffsc.pipeline.nodes.intersections import find_intersecting_points
from ffsc.pipeline.nodes.utils import (
    preprocess_geodata,
    unique_nodes_from_segments,
    convert_segments_to_lines,
    calculate_havesine_distance,
)

import numpy as np
import pandas as pd
import geopandas as gpd
gpd.options.use_pygeos=False
from shapely import geometry
from typing import List, AnyStr
from shapely import ops

logging.basicConfig(stream=sys.stdout, level=logging.INFO)

import multiprocessing as mp
NPARTITIONS=mp.cpu_count()//4

### I think you do need this...
from dask.distributed import Client
client = Client(n_workers=NPARTITIONS)
client.cluster

import dask.dataframe as dd



def preprocess_railway_data_int(data):
    df = preprocess_geodata(data, "railway_id")
    df = df.rename({"type_x": "rail_type", "type_y": "geometry_type"}, axis=1)
    # The country for all rows with missing region was either one or several of following:
    # United States of America, Canada, and Mexico
    # or missing.
    df.loc[df.md_country.notnull(), "md_region"] = df.loc[
        df.md_country.notnull(), "md_region"
    ].fillna("N. and C. America")
    return df[
        [
            "railway_id",
            "md_country",
            "md_region",
            "rail_type",
            "geometry_type",
            "coordinates",
        ]
    ]


def preprocess_railway_data_prm(int_railways, parameters):
    logger = logging.getLogger(__name__)
    tic = time.time()
    
    
    def find_intersecting_points(
        network_df,
        parameters,
        object_column="pipeline_object",
        entity_ids=["pipeline_id", "pipeline_segment_id"],
    ):
        """
        This function finds the intersecting LineString objects, finds the intersecting points, and then snap the
        intersecing points to the LineString objects.

        :param network_df: The dataframe with the LineString objects
        :param parameters: Dictionary with pipeline parameters.
        :param object_column: The column in the network_df containing the LineString objects.
        :param entity_ids: The unique keys of the dataframe.
        :return: Returns the dataframe with the LineString objects modified to include the intersecting points.
        """
        # Set up the GeoPandas dataframe
        network_gdf = gpd.GeoDataFrame(network_df, geometry=network_df[object_column])
        network_gdf.sindex

        # Find the intersecting LineStrings
        intersected_gdf = gpd.sjoin(
            network_gdf[entity_ids + ["geometry"]],
            network_gdf[entity_ids + ["geometry"]],
            op="intersects",
        )

        # Bring in the LineString object of the other intersecting object:
        intersected_gdf = (
            intersected_gdf.merge(
                network_gdf[["geometry"]], left_on="index_right", right_index=True
            )
            .reset_index(drop=True)
            .rename(columns={"geometry_x": "geometry_left", "geometry_y": "geometry_right"})
        )

        # Find the intersection of the intersecting LineStrings:
        intersected_gdf["intersection"] = intersected_gdf.apply(
            lambda row: row["geometry_left"].intersection(row["geometry_right"]), axis=1
        )

        # We only focus on the intersection where the type is either Point or MultiPoint.
        # This removes the intersection of LineStrings with themselves.
        # orig_index is the index for intersection.
        intersected_gdf['geomtype'] = intersected_gdf.intersection.apply(lambda x: x.type)

        
        points_df = intersected_gdf.loc[
                intersected_gdf.intersection.apply(lambda x: "Point" in x.type) # get only the points
            ].intersection.apply(
                lambda x: list(x)
                if x.type == "MultiPoint"
                else [x]
            ).explode().reset_index().rename(columns={'index':'orig_index','intersection':'intersection_point'})
        

        # Remove the duplicated intersections (points appear twice per intersection.)
        points_gdf = gpd.GeoDataFrame(
            points_df["orig_index"], geometry=points_df["intersection_point"]
        ).drop_duplicates()

        # Bring in the entity_ids and geometries of LinesStrings corresponding for each intersection.
        intersection_df = (
            points_gdf.merge(
                intersected_gdf[
                    [
                        entity_id + "_" + direction
                        for direction in ["left", "right"]
                        for entity_id in entity_ids + ["geometry"]
                    ]
                ],
                left_on="orig_index",
                right_index=True,
            )
            .rename(columns={"geometry": "intersection_point"})
            .drop(columns=["orig_index"])
        ).reset_index(drop=True)

        # Put the left and right LineStrings on top of each other.
        intersection_df = (
            (
                pd.concat(
                    [
                        intersection_df[
                            ["intersection_point"]
                            + [
                                entity_id + "_left"
                                for entity_id in ["geometry"] + entity_ids
                            ]
                        ].rename(
                            columns={
                                entity_id + "_left": entity_id
                                for entity_id in ["geometry"] + entity_ids
                            }
                        ),
                        intersection_df[
                            ["intersection_point"]
                            + [
                                entity_id + "_right"
                                for entity_id in ["geometry"] + entity_ids
                            ]
                        ].rename(
                            columns={
                                entity_id + "_right": entity_id
                                for entity_id in ["geometry"] + entity_ids
                            }
                        ),
                    ],
                    ignore_index=True,
                )
            )
            .reset_index(drop=True)
            .drop_duplicates()
        )

        # For each LineString, combine all intersecting points to a single MultiPoint object.

        intersecting_points = (
            intersection_df.groupby(entity_ids)
            .intersection_point.apply(lambda x: gpd.GeoDataFrame(geometry=x).unary_union)
            .reset_index()
        )

        for entity_id in entity_ids:
            if entity_id not in intersecting_points.columns:
                intersecting_points[entity_id]=None
        
        # Bring in entity ids and geometries of intersecting LineStrings to the objects above.
        intersecting_points = intersecting_points.merge(
            intersection_df[entity_ids + ["geometry"]].drop_duplicates(), on=entity_ids
        )


        if not intersecting_points.empty:
            
            # Snap the intersecting points to LineString objects.
            intersecting_points["snapped_geometry"] = intersecting_points.apply(
                lambda row: ops.snap(
                    row["geometry"], row["intersection_point"], parameters["snapping_threshold"]
                ),
                axis=1,
            )
        else:
            intersecting_points["snapped_geometry"] = None

        return intersecting_points[entity_ids + ['snapped_geometry']]
    
    def _nest_coords(row):
        if row['geometry_type']=='MultiLineString':
            return row['coordinates']
        else:
            return [list(row['coordinates'])]
        
    # nest any LineStrings to [[[],[]]]
    logger.info(f'nesting coordinates {time.time()-tic}')
    int_railways['coordinates'] = int_railways.apply(lambda row: _nest_coords(row), axis=1)
    logger.info(f'nesting coordinates {time.time()-tic}')
    
    
    #explode along coordinates
    logger.info(f'exploding df {time.time()-tic}')
    railway_df = int_railways.explode('coordinates')
    railway_df['railway_segment_id'] = railway_df.index
    logger.info(f'exploded df {time.time()-tic}')
    
    
    # get the missing region
    logger.info(f'get missing regions {time.time()-tic}')
    railway_missing_region_df = railway_df.loc[railway_df.md_region.isna()].copy()
    logger.info(f'get missing regions {time.time()-tic}')
    
    # cast to dask dataframe
    logger.info(f'cast to dask {time.time()-tic}')
    ddf = dd.from_pandas(railway_missing_region_df, npartitions=NPARTITIONS*4)
    logger.info(f'cast to dask {time.time()-tic}')
    
    # load NE
    logger.info(f'get world gdf {time.time()-tic}')
    world = gpd.read_file(gpd.datasets.get_path("naturalearth_lowres"))
    world.geometry = world.geometry.geometry.buffer(10)
    logger.info(f'get world gdf {time.time()-tic}')
    
    ### parallelise sjoin in Dask
    
    # define meta dataframe
    logger.info(f'get meta {time.time()-tic}')
    meta = pd.DataFrame(columns=['railway_id','name','continent'])
    meta.railway_id = meta.railway_id.astype(int)
    meta.name = meta.name.astype('str')
    meta.continent = meta.continent.astype('str')
    logger.info(f'get meta {time.time()-tic}')
    
    def dask_sjoin(df, obj_col, world):

        df[obj_col] = df["coordinates"].apply(geometry.LineString)
        gdf = gpd.GeoDataFrame(df, geometry=df[obj_col], crs='epsg:4326')
        gdf = gpd.sjoin(gdf, world[['name','continent','geometry']], how='left',op='intersects')

        return pd.DataFrame(gdf[['railway_id','name','continent']])
    
    logger.info(f'map regions on dask {time.time()-tic}')
    retrieved_region_df = ddf.map_partitions(dask_sjoin,'railway_object',world, meta=meta).compute()
    logger.info(f'map regions on dask {time.time()-tic}')
    #client.restart()
    
    logger.info(f'regions reset {time.time()-tic}')
    region_dict = {
        "Oceania": "Australia and Oceania",
        "Africa": "Africa",
        "North America": "N. and C. America",
        "Asia": "Asia",
        "South America": "South America",
        "Europe": "Europe",
    }

    retrieved_region_df["retrieved_region"] = retrieved_region_df.continent.map(
        region_dict
    )

    retrieved_region_df.loc[
        retrieved_region_df.retrieved_region == "Asia", "retrieved_region"
    ] = retrieved_region_df.loc[
        retrieved_region_df.retrieved_region == "Asia", "name"
    ].apply(
        lambda x: "Middle East"
        if x
        in [
            "Iraq",
            "Turkey",
            "Armenia",
            "Azerbaijan",
            "Iran",
            "Kuwait",
            "Israel",
            "Jordan",
            "Syria",
            "Saudi Arabia",
            "Lebanon",
        ]
        else "Asia"
    )

    retrieved_region_df.dropna(subset=["retrieved_region"], inplace=True)

    retrieved_region_df = (
        retrieved_region_df.drop_duplicates(subset=["railway_id", "retrieved_region"])
        .sort_values(["railway_id", "retrieved_region"])
        .groupby("railway_id")
        .retrieved_region.apply(lambda x: "; ".join(x.tolist()))
        .reset_index()
    )
    logger.info(f'regions reset {time.time()-tic}')

    
    logger.info(f'merge back to main df {time.time()-tic}')
    railway_df = railway_df.merge(retrieved_region_df, how="left", on="railway_id")

    railway_df.loc[:, "md_region"] = railway_df.loc[:, "md_region"].fillna(
        railway_df.loc[:, "retrieved_region"]
    )

    railway_df.drop(columns=["retrieved_region"], inplace=True)
    logger.info(f'merge back to main df {time.time()-tic}')
    
    # drop the regionless orphans
    railway_df.dropna(subset=['md_region'], inplace=True)

    logger.info(f'making geometry column {time.time()-tic}')
    railway_df['railway_object'] = railway_df.coordinates.apply(geometry.LineString)
    logger.info(f'making geometry column {time.time()-tic}')

    logger.info(f'making new ddf {time.time()-tic}')
    ddf = dd.from_pandas(railway_df, npartitions=len(railway_df.md_region.unique())).set_index('md_region') # This needs to be one for the way that the load gets spread out

    logger.info(f'making new ddf {time.time()-tic}')
    meta= pd.DataFrame({'railway_id': [1], 'railway_segment_id': [2], 'snapped_geometry':['str']})
    


    logger.info(f'calling groupby md_region{time.time()-tic}')
    prm_railways_data = ddf.map_partitions(find_intersecting_points, 
                                    parameters, 
                                    'railway_object',
                                    ['railway_id','railway_segment_id'], 
                                    meta=meta) \
                            .compute()

    prm_railways_data = prm_railways_data[
        ["railway_segment_id", "railway_id", "snapped_geometry"]
    ].copy()

    # Amend the pipelines that did not have any intersections.
    prm_railways_data = pd.concat(
        [
            railway_df.loc[
                ~railway_df.railway_segment_id.isin(
                    prm_railways_data.railway_segment_id.unique()
                ),
                ["railway_segment_id", "railway_id", "railway_object"],
            ].rename(columns={"railway_object": "snapped_geometry"}),
            prm_railways_data,
        ],
        ignore_index=True,
    ).reset_index(drop=True)

    return prm_railways_data


def coord_to_rail_key(coord: List) -> AnyStr:
    return "railway_node_" + "".join([str(item) for item in coord])


def create_railway_graph_components(prm_railways_data):
    unique_nodes = unique_nodes_from_segments(prm_railways_data.snapped_geometry)

    node_df = pd.DataFrame()
    node_df["coordinates"] = unique_nodes
    node_df["RailwayNodeID:ID(RailwayNode)"] = [
        coord_to_rail_key(coord) for coord in unique_nodes
    ]
    node_df["lat"] = [coord[0] for coord in unique_nodes]
    node_df["long"] = [coord[1] for coord in unique_nodes]
    node_df[":LABEL"] = "RailwayNode"

    edges = convert_segments_to_lines(
        [list(line.coords) for line in prm_railways_data.snapped_geometry]
    )

    edge_df = pd.DataFrame()
    edge_df["StartNodeId:START_ID(RailwayNode)"] = [
        coord_to_rail_key(edge[0]) for edge in edges
    ]
    edge_df["EndNodeId:END_ID(RailwayNode)"] = [
        coord_to_rail_key(edge[1]) for edge in edges
    ]
    edge_df[":TYPE"] = "RAILWAY_CONNECTION"

    edge_df["distance"] = calculate_havesine_distance(
        np.array([edge[0] for edge in edges]), np.array([edge[1] for edge in edges])
    )
    edge_df["impedance"] = edge_df["distance"] ** 2

    return node_df, edge_df
