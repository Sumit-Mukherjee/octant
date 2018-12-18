# -*- coding: utf-8 -*-
"""Miscellanea."""
import numpy as np

import xarray as xr

from .decor import pbar
from .params import CATS


SUBSETS = [i for i in CATS.keys() if i != "unknown"]
DENSITY_TYPES = ["point", "track", "genesis", "lysis"]


def _exclude_by_first_day(df, m, d):
    """Check if OctantTrack starts on certain day and month."""
    return not ((df.time.dt.month[0] == m).any() and (df.time.dt.day[0] == d).any())


def _exclude_by_last_day(df, m, d):
    """Check if OctantTrack ends on certain day and month."""
    return not ((df.time.dt.month[-1] == m).any() and (df.time.dt.day[-1] == d).any())


def calc_all_dens(tr_obj, lon2d, lat2d, subsets=SUBSETS, **kwargs):
    """
    Calculate all types of cyclone density for all SUBSETS of TrackRun.

    Parameters
    ----------
    lon2d: numpy.ndarray
        2D array of longitudes
    lat2d: numpy.ndarray
        2D array of latitudes
    subsets: list
        Subsets of `TrackRun` to process
    **kwargs: dict
        Keyword arguments passed to `octant.core.TrackRun.density()`.
        Should not include `subset` and `by` keywords, because they are passed separately.

    Returns
    -------
    da: xarray.DataArray
       4d array with dimensions (subset, dens_type, latitude, longitude)

    """
    subset_dim = xr.DataArray(name="subset", dims=("subset"), data=SUBSETS)
    dens_dim = xr.DataArray(name="dens_type", dims=("dens_type"), data=DENSITY_TYPES)
    list1 = []
    for subset in pbar(subsets, leave=False, desc="subsets"):
        list2 = []
        for by in pbar(DENSITY_TYPES, leave=False, desc="density_types"):
            list2.append(tr_obj.density(lon2d, lat2d, by=by, subset=subset, **kwargs))
        list1.append(xr.concat(list2, dim=dens_dim))
    da = xr.concat(list1, dim=subset_dim)
    return da.rename("density")


def bin_count_tracks(tr_obj, start_year, n_winters, by="M"):
    """
    Take `octant.TrackRun` and count cyclone tracks by month or by winter.

    Parameters
    ----------
    tr_obj: octant.core.TrackRun
        TrackRun object
    start_year: int
        Start year
    n_winters: int
        Number of years

    Returns
    -------
    counter: numpy.ndarray
        Binned counts of shape (N,)

    """
    if by.upper() == "M":
        counter = np.zeros(12, dtype=int)
        for _, df in pbar(tr_obj.groupby("track_idx"), leave=False, desc="tracks"):
            track_months = df.time.dt.month.unique()
            for m in track_months:
                counter[m - 1] += 1
    if by.upper() == "W":
        # winter
        counter = np.zeros(n_winters, dtype=int)
        for _, df in pbar(tr_obj.groupby("track_idx"), leave=False, desc="tracks"):
            track_months = df.time.dt.month.unique()
            track_years = df.time.dt.year.unique()

            for i in range(n_winters):
                if track_months[-1] <= 6:
                    if track_years[0] == i + start_year + 1:
                        counter[i] += 1
                else:
                    if track_years[-1] == i + start_year:
                        counter[i] += 1
    return counter
