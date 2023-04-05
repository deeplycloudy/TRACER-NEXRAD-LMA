#!/usr/bin/env python
# coding: utf-8

import argparse

parse_desc = """Find features, track, and plot all nexrad data in a given destination folder

The path is a string destination
Files in path must have a postfix of 'grid.nc'. 
threshold is the tracking threshold in dbz
speed is the tracking speed in tobac units. 
Site is a string NEXRAD location


Example
=======
python knb_tobac_tracking.py --path="/archive/TRACER_processing/JUNE/20220602/" --threshold=15 --speed=1.0 --site=KHGX --type="
NEXRAD" 



"""


def create_parser():
    parser = argparse.ArgumentParser(description=parse_desc)
    parser.add_argument('--path',metavar='path', required=True,dest='path',
                        action = 'store',help='path in which the data is located')
    parser.add_argument('-o', '--output_path',
                        metavar='filename template including path',
                        required=False, dest='outdir', action='store',
                        default='.', help='path in which the data is located')
    parser.add_argument('--site', metavar='site', required=True,
                        dest='site', action='store',
                        help='NEXRAD site code, e.g., khgx')
    parser.add_argument('--threshold', metavar='dbz', required=True,type=float,
                        dest='track_threshold', action='store',
                        help='Tracking/Feature threshold in dbz, e.g., 15')
    parser.add_argument('--speed', metavar='value', required=True,type=float,
                        dest='track_speed', action='store',
                        help='Tracking speed, e.g., 1.0')
    parser.add_argument('--type', metavar='data type', required=True,
                        dest='data_type', action='store',
                        help='Datat name type, e.g., NEXRAD, POLARRIS, NUWRF')
    return parser

# End parsing #

# Import libraries:
import xarray
import xarray as xr
import numpy as np
import pandas as pd
import os
from six.moves import urllib
from glob import glob
import matplotlib.pyplot as plt
import matplotlib as mpl
import pickle
import pyart
from datetime import datetime
import math
from pandas.core.common import flatten
from scipy import ndimage
from scipy.spatial import KDTree

# get_ipython().run_line_magic("matplotlib", "inline")
# %matplotlib widget
import tobac
from tobac.merge_split import merge_split_MEST
from tobac.utils import standardize_track_dataset
from tobac.utils import compress_all

# Disable a couple of warnings:
import warnings

warnings.filterwarnings("ignore", category=UserWarning, append=True)
warnings.filterwarnings("ignore", category=RuntimeWarning, append=True)
warnings.filterwarnings("ignore", category=FutureWarning, append=True)
warnings.filterwarnings("ignore", category=pd.io.pytables.PerformanceWarning)



try:
    import pyproj

    _PYPROJ_AVAILABLE = True
except ImportError:
    _PYPROJ_AVAILABLE = False


def cartesian_to_geographic_aeqd(x, y, lon_0, lat_0, R=6370997.0):
    """
    Azimuthal equidistant Cartesian to geographic coordinate transform.

    Transform a set of Cartesian/Cartographic coordinates (x, y) to
    geographic coordinate system (lat, lon) using a azimuthal equidistant
    map projection [1]_.

    .. math::

        lat = \\arcsin(\\cos(c) * \\sin(lat_0) +
                       (y * \\sin(c) * \\cos(lat_0) / \\rho))

        lon = lon_0 + \\arctan2(
            x * \\sin(c),
            \\rho * \\cos(lat_0) * \\cos(c) - y * \\sin(lat_0) * \\sin(c))

        \\rho = \\sqrt(x^2 + y^2)

        c = \\rho / R

    Where x, y are the Cartesian position from the center of projection;
    lat, lon the corresponding latitude and longitude; lat_0, lon_0 are the
    latitude and longitude of the center of the projection; R is the radius of
    the earth (defaults to ~6371 km). lon is adjusted to be between -180 and
    180.

    Parameters
    ----------
    x, y : array-like
        Cartesian coordinates in the same units as R, typically meters.
    lon_0, lat_0 : float
        Longitude and latitude, in degrees, of the center of the projection.
    R : float, optional
        Earth radius in the same units as x and y. The default value is in
        units of meters.

    Returns
    -------
    lon, lat : array
        Longitude and latitude of Cartesian coordinates in degrees.

    References
    ----------
    .. [1] Snyder, J. P. Map Projections--A Working Manual. U. S. Geological
        Survey Professional Paper 1395, 1987, pp. 191-202.

    """
    x = np.atleast_1d(np.asarray(x))
    y = np.atleast_1d(np.asarray(y))

    lat_0_rad = np.deg2rad(lat_0)
    lon_0_rad = np.deg2rad(lon_0)

    rho = np.sqrt(x * x + y * y)
    c = rho / R

    with warnings.catch_warnings():
        # division by zero may occur here but is properly addressed below so
        # the warnings can be ignored
        warnings.simplefilter("ignore", RuntimeWarning)
        lat_rad = np.arcsin(
            np.cos(c) * np.sin(lat_0_rad) + y * np.sin(c) * np.cos(lat_0_rad) / rho
        )
    lat_deg = np.rad2deg(lat_rad)
    # fix cases where the distance from the center of the projection is zero
    lat_deg[rho == 0] = lat_0

    x1 = x * np.sin(c)
    x2 = rho * np.cos(lat_0_rad) * np.cos(c) - y * np.sin(lat_0_rad) * np.sin(c)
    lon_rad = lon_0_rad + np.arctan2(x1, x2)
    lon_deg = np.rad2deg(lon_rad)
    # Longitudes should be from -180 to 180 degrees
    lon_deg[lon_deg > 180] -= 360.0
    lon_deg[lon_deg < -180] += 360.0

    return lon_deg, lat_deg


def cartesian_to_geographic(grid_ds):
    """
    Cartesian to Geographic coordinate transform.

    Transform a set of Cartesian/Cartographic coordinates (x, y) to a
    geographic coordinate system (lat, lon) using pyproj or a build in
    Azimuthal equidistant projection.

    Parameters
    ----------
    grid_ds: xarray DataSet
        Cartesian coordinates in meters unless R is defined in different units
        in the projparams parameter.

    Returns
    -------
    lon, lat : array
        Longitude and latitude of the Cartesian coordinates in degrees.

    """
    projparams = grid_ds.ProjectionCoordinateSystem
    x = grid_ds.x.values
    y = grid_ds.y.values
    z = grid_ds.z.values
    z, y, x = np.meshgrid(z, y, x, indexing="ij")
    if projparams.attrs["grid_mapping_name"] == "azimuthal_equidistant":
        # Use Py-ART's Azimuthal equidistance projection
        lat_0 = projparams.attrs["latitude_of_projection_origin"]
        lon_0 = projparams.attrs["longitude_of_projection_origin"]
        if "semi_major_axis" in projparams:
            R = projparams.attrs["semi_major_axis"]
            lon, lat = cartesian_to_geographic_aeqd(x, y, lon_0, lat_0, R)
        else:
            lon, lat = cartesian_to_geographic_aeqd(x, y, lon_0, lat_0)
    else:
        # Use pyproj for the projection
        # check that pyproj is available
        if not _PYPROJ_AVAILABLE:
            raise MissingOptionalDependency(
                "PyProj is required to use cartesian_to_geographic "
                "with a projection other than pyart_aeqd but it is not "
                "installed"
            )
        proj = pyproj.Proj(projparams)
        lon, lat = proj(x, y, inverse=True)
    return lon, lat


def add_lat_lon_grid(grid_ds):
    lon, lat = cartesian_to_geographic(grid_ds)
    grid_ds["point_latitude"] = xr.DataArray(lat, dims=["z", "y", "x"])
    grid_ds["point_latitude"].attrs["long_name"] = "Latitude"
    grid_ds["point_latitude"].attrs["units"] = "degrees"
    grid_ds["point_longitude"] = xr.DataArray(lon, dims=["z", "y", "x"])
    grid_ds["point_longitude"].attrs["long_name"] = "Latitude"
    grid_ds["point_longitude"].attrs["units"] = "degrees"
    return grid_ds


def parse_grid_datetime(my_ds):
    year = my_ds["time"].dt.year
    month = my_ds["time"].dt.month
    day = my_ds["time"].dt.day
    hour = my_ds["time"].dt.hour
    minute = my_ds["time"].dt.minute
    second = my_ds["time"].dt.second
    return datetime(
        year=year, month=month, day=day, hour=hour, minute=minute, second=second
    )


# Count neighbors
def count_track_neighbors(track_ds, distance_thresholds = (5.0, 10.0, 15.0, 20.0), grid_spacing = 0.5):
    from scipy.spatial import KDTree

    feature_neighbor_variable_names = []

    # First find the trees corresponding to all features at each time.
    time_groups = track_ds.groupby('feature_time_index')
    time_groups.groups.keys()
    trees_each_time_index = {}
    for time_idx, group in time_groups:
        hdim1 = group['feature_hdim1_coordinate'].values*grid_spacing
        hdim2 = group['feature_hdim2_coordinate'].values*grid_spacing
        #note hdim1,2 are in km
        pts = np.vstack((hdim2, hdim1)).T
        tree = KDTree(pts)
        trees_each_time_index[time_idx] = tree

    # Now we'll look at each feature in turn, and its neighbors at that time.
    hdim1 = track_ds['feature_hdim1_coordinate'].values*grid_spacing
    hdim2 = track_ds['feature_hdim2_coordinate'].values*grid_spacing
    pts = np.vstack((hdim2, hdim1)).T
    #note hdim1,2 are in km
    for distance_threshold in distance_thresholds:
        num_obj = np.zeros(len(track_ds["feature"].values), dtype=int)
        for i, ind in enumerate(track_ds["feature"].values):
            time_idx = track_ds.feature_time_index.values[i]
            tree = trees_each_time_index[time_idx]
            # Need to subtract one, since the feature itself is always near (at) the test location
            num_obj[i]=len(tree.query_ball_point(pts[i],r=distance_threshold)) - 1 
        this_nearby_var_name = 'feature_nearby_count_{0}km'.format(int(distance_threshold))
        feature_neighbor_variable_names.append(this_nearby_var_name)
        track_ds = track_ds.assign(**{this_nearby_var_name:(['feature'], num_obj)})
    return track_ds



""" X-Array based TINT I/O module. """

import xarray as xr
import random
import numpy as np
import pyproj


from datetime import datetime


if __name__ == '__main__':
    parser = create_parser()
    args = parser.parse_args()

    

    #NEXRAD
    if args.data_type == 'NEXRAD':
    
        data = xarray.open_mfdataset(args.path+"*.nc", engine="netcdf4")
        data['time'].encoding['units']="seconds since 2000-01-01 00:00:00"
        bad_rhv = data["cross_correlation_ratio"] < 0.9
        bad_refl = data["reflectivity"] < 10
        bad=bad_rhv & bad_refl
        maxrefl = data["reflectivity"].where(~bad, np.nan).max(axis=1)
        ts = pd.to_datetime(data['time'][0].values)
        date = ts.strftime('%Y%m%d')

        date = args.path[-9:-1]
        
        
        # Set up directory to save output and plots:
        savedir = args.data_type + "_tobac_Save_"+date
        if not os.path.exists(savedir):
            os.makedirs(savedir)
        plot_dir = savedir+"/tobac_Plot/"
        if not os.path.exists(plot_dir):
            os.makedirs(plot_dir)

        
# #HORIZONTAL GRID RESOLUTION, AND TIME RESOLUTION
        datetimes = data["time"]
        timedeltas = [(datetimes[i - 1] - datetimes[i]).astype("timedelta64[m]")for i in range(1, len(datetimes))]
        average_timedelta = sum(timedeltas) / len(timedeltas)
        dt = np.abs(np.array(average_timedelta)).astype("timedelta64[m]").astype(int)
        deltax = [data["x"][i - 1] - data["x"][i] for i in range(1, len(data["x"]))]
        dxy = np.abs(np.mean(deltax).astype(int)) / 1000.


        
    #NUWRF
    if args.data_type == 'NUWRF':
    
    #FOR NUWRF NOT POLARRIS  
    
        files = sorted(glob(args.path+"wrfout*"))
        data1 = xarray.open_dataset(files[0])
        drop_list = list(np.sort(list(data1.variables)))
        drop_list = [e for e in drop_list if e not in ('COMDBZ', 'Times','XLAT','XLONG','XTIME')]



        import xarray as xr
        import xwrf
        data = xr.open_mfdataset(files, engine="netcdf4",parallel=True,
            concat_dim="Time", combine="nested", chunks={"Time": 1},decode_times=False,
            drop_variables=drop_list,).xwrf.postprocess()


        #MAKE THE TIME DIMENSION AND COORDINATES PLAY NICE
        data = data.rename_dims({'Time': 'time'})
        data['time'] = data['Time']
        maxrefl = data['COMDBZ']
        maxrefl = maxrefl.drop('XTIME')
        maxrefl = maxrefl.drop('Time')

        # #HORIZONTAL GRID RESOLUTION, AND TIME RESOLUTION
        dxy = data1.DX/1000.
        dt = data1.DT
        print(dxy)
        print(dt)
        ts = pd.to_datetime(data['time'][0].values)
        date = ts.strftime('%Y%m%d')
        print(date)
        
        savedir = args.data_type + "_tobac_Save_"+date
        if not os.path.exists(savedir):
            os.makedirs(savedir)
        plot_dir = savedir+"/tobac_Plot/"
        if not os.path.exists(plot_dir):
            os.makedirs(plot_dir)

    

        #POLARRIS
        
    if args.data_type == 'POLARRIS':
    
        data = xr.open_mfdataset(args.path+'*.nc', engine = 'netcdf4',combine = 'nested' ,concat_dim='time')
        data['time'].encoding['units']="seconds since 2000-01-01 00:00:00"
        files = sorted(glob(args.path+'*.nc'))
        arr = []
        for i in files:
            arr.append(pd.to_datetime(i[-19:-3], format = '%Y_%m%d_%H%M%S'))
        arr = pd.DatetimeIndex(arr)
        data = data.assign_coords(time=arr)


        bad_rhv = data["RH"] < 0.9
        bad_refl = data["CZ"] < 10
        bad=bad_rhv & bad_refl
        maxrefl = data["CZ"].where(~bad, np.nan).max(axis=1)

    

        #Dt, DXY
        datetimes = data['time']
        timedeltas = [(datetimes[i-1]-datetimes[i]).astype('timedelta64[m]') for i in range(1, len(datetimes))]
        average_timedelta = sum(timedeltas) / len(timedeltas)
        dt = np.abs(np.array(average_timedelta)).astype('timedelta64[m]').astype(int)
        deltax = [data['x'][i-1]-data['x'][i] for i in range(1, len(data['x']))]
        dxy = np.abs(np.mean(deltax).astype(int))/1000


        ts = pd.to_datetime(data['time'][0].values)
        date = ts.strftime('%Y%m%d')
        print(date)


        savedir = args.data_type + "_tobac_Save_"+date
        if not os.path.exists(savedir):
            os.makedirs(savedir)
        plot_dir = savedir+"/tobac_Plot/"
        if not os.path.exists(plot_dir):
            os.makedirs(plot_dir)




    # Dictionary containing keyword options (could also be directly given to the function)
    parameters_features = {}
    parameters_features["position_threshold"] = "weighted_diff"
    parameters_features["sigma_threshold"] = 1.0  # 0.5 is the default
    parameters_features["threshold"] = args.track_threshold

    # Dictionary containing keyword arguments for segmentation step:
    parameters_segmentation = {}
    parameters_segmentation["method"] = "watershed"
    parameters_segmentation["threshold"] = args.track_threshold  # mm/h mixing ratio
        
        

# # #Feature detection:


    maxrefl_iris = maxrefl.to_iris()
    print("starting feature detection based on multiple thresholds")
    Features_iris = tobac.feature_detection_multithreshold(maxrefl_iris, dxy, **parameters_features)
    Features = Features_iris.to_xarray()
    print("feature detection done")
    Features.to_netcdf(os.path.join(savedir, "Features.nc"))
    print("features saved")



    # Dictionary containing keyword arguments for the linking step:
    parameters_linking = {}
    parameters_linking["stubs"] = 5
    parameters_linking["method_linking"] = "predict"
    parameters_linking["adaptive_stop"] = 0.2
    parameters_linking["adaptive_step"] = 0.95
    parameters_linking["order"] = 2  # Order of polynomial for extrapolating
    parameters_linking["subnetwork_size"] = 100  # 50 #100
    parameters_linking["memory"] = 3  # 4
    # parameters_linking['time_cell_min']=1
    parameters_linking["v_max"] =  args.track_speed  
    parameters_linking["d_min"] = None  # 5    

    Features_df = Features.to_dataframe()

    # Perform Segmentation and save resulting mask to NetCDF file:
    print("Starting segmentation based on reflectivity")
    Mask_iris, Features_Precip = tobac.segmentation.segmentation(Features_df, maxrefl_iris, dxy, **parameters_segmentation)
    # Mask,Features_Precip=tobac.themes.tobac_v1.segmentation(Features,maxrefl,dxy,**parameters_segmentation)
    Mask = xarray.DataArray.from_iris(Mask_iris)
    Mask = Mask.to_dataset()

    # Mask,Features_Precip=segmentation(Features,maxrefl,dxy,**parameters_segmentation)
    print("segmentation based on reflectivity performed, start saving results to files")
    Mask.to_netcdf(os.path.join(savedir, "Mask_Segmentation_refl.nc"))
    print("segmentation reflectivity performed and saved")


    areas = np.zeros([(len(Features["index"]) + 1)])
    maxfeature_refl = np.zeros([(len(Features["index"]) + 1)])
    # Mask = Mask.to_dataset()
    frame_features = Features.groupby("frame")

    for frame_i, features_i in frame_features:
        mask_i = Mask["segmentation_mask"][frame_i, :, :].values
        subrefl = maxrefl[frame_i, :, :].values
        for i in np.unique(mask_i):
            feature_area_i = np.where(mask_i == i)
            areas[i] = len(feature_area_i[0])
            maxfeature_refl[i] = np.nanmax(subrefl[feature_area_i])


    var = Features["feature"].copy(data=areas[1:])
    var = var.rename("areas")
    var_max = Features["feature"].copy(data=maxfeature_refl[1:])
    var_max = var_max.rename("max_reflectivity")
    Features = xarray.merge([Features, var], compat="override")
    Features = xarray.merge([Features, var_max], compat="override")
    Features.to_netcdf(os.path.join(savedir, "Features.nc"))
    Mask = Mask.to_array()
    Features_df = Features.to_dataframe()
    print("features saved")


    # Perform trajectory linking using trackpy and save the resulting DataFrame:

    Features_df = Features.to_dataframe()
    Track = tobac.linking_trackpy(Features_df, Mask_iris, dt=dt, dxy=dxy, **parameters_linking)
    #(type(Track))
    Track = Track.to_xarray()
    Track.to_netcdf(os.path.join(savedir, "Track.nc"))

    Track = xarray.open_dataset(savedir + "/Track.nc")
    Track = Track.to_dataframe()
    Features = xarray.open_dataset(savedir + "/Features.nc")
    refl_mask = xarray.open_dataset(savedir + "/Mask_Segmentation_refl.nc")
#Track=tobac.themes.tobac_v1.linking_trackpy(Features,Mask,dt=dt,dxy=dxy,**parameters_linking)


    print("starting merge_split")

    d = merge_split_MEST(Track,dxy*1000., distance=15000.0)  # , dxy = dxy)
    Track = xarray.open_dataset(savedir + "/Track.nc")
    if args.data_type =='NUWRF':
        Track = Track.rename_vars({'XLAT':'wrf_XLAT', 'XLONG':'wrf_XLONG'})
    ds = standardize_track_dataset(Track, refl_mask)
    both_ds = xarray.merge([ds, d], compat="override")
    
    both_ds = count_track_neighbors(both_ds, grid_spacing=dt)
    
#     hdim1 = both_ds['feature_hdim1_coordinate'].values*0.5
#     hdim2 = both_ds['feature_hdim2_coordinate'].values*0.5
#     pts = np.vstack((hdim2, hdim1)).T
#     tree = KDTree(pts)
#     #note hdim is in km on the grid
#     num_obj = np.zeros(len(both_ds["feature"].values))
#     for i,ind in enumerate(both_ds["feature"].values):
#         num_obj[i]=len(tree.query_ball_point(pts[i],r=5))
#     num_obj = num_obj.astype(int)
#     both_ds = both_ds.assign(feature_nearby_count=(['feature'], num_obj))
    
    both_ds = compress_all(both_ds)
    both_ds.to_netcdf(os.path.join(savedir, "Track_features_merges.nc"))
 
    print("tobac completed")

