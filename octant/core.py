# -*- coding: utf-8 -*-
"""Classes and functions for the analysis of cyclone tracking output."""
import operator
import warnings
from functools import partial
from pathlib import Path

import numpy as np

import pandas as pd

import xarray as xr

from .decor import ReprTrackRun, get_pbar
from .exceptions import (
    ArgumentError,
    ConcatenationError,
    GridError,
    InconsistencyWarning,
    LoadError,
    MissingConfWarning,
    NotCategorisedError,
    SelectError,
)
from .grid import cell_bounds, cell_centres, grid_cell_areas
from .io import ARCH_KEY, ARCH_KEY_CAT, PMCTRACKLoader
from .misc import _exclude_by_first_day, _exclude_by_last_day
from .params import EARTH_RADIUS, FILLVAL, HOUR, KM2M, MUX_NAMES
from .parts import OctantTrack, TrackSettings
from .utils import (
    distance_metric,
    great_circle,
    point_density_cell,
    point_density_rad,
    track_density_cell,
    track_density_rad,
)


class TrackRun:
    """
    Results of tracking experiment.

    Attributes
    ----------
    data: octant.core.OctantTrack
        DataFrame-like container of tracking locations, times, and other data
    filelist: list
        List of source "vortrack" files; see PMCTRACK docs for more info
    conf: octant.parts.TrackSettings
        Configuration used for tracking
    is_categorised: bool
        Flag if categorisation has been applied to the TrackRun
    cats: None or pandas.DataFrame
        DataFrame with the same index as data and the number of columns equal to
        the number of categories; None if `is_categorised` is False
    columns: sequence of str
        List of dataframe column names. Should contain 'time' to work on datetime objects.
    """

    _mux_names = MUX_NAMES

    def __init__(self, dirname=None, **load_kwargs):
        """
        Initialise octant.core.TrackRun.

        Parameters
        ----------
        dirname: pathlib.Path, optional
            Path to the directory with tracking output
            If present, load the data during on init
        load_kwargs: dict, optional
            Parameters passed to load_data()
        """
        self.dirname = dirname
        self.conf = None
        mux = pd.MultiIndex.from_arrays([[], []], names=self._mux_names)
        self.columns = []
        self.data = OctantTrack(index=mux, columns=self.columns)
        self.filelist = []
        self.sources = []
        self.cats = None
        self.is_categorised = False
        self.is_cat_inclusive = False
        self._cat_sep = "|"
        if isinstance(self.dirname, Path):
            # Read all files and store in self.all
            # as a list of `pandas.DataFrame`s
            self.load_data(self.dirname, **load_kwargs)
        elif self.dirname is not None:
            raise LoadError("To load data, `dirname` should be Path-like object")

        if not self.data.empty:
            # Define time step
            for (_, ot) in self.gb:
                if ot.shape[0] > 1:
                    self.tstep_h = ot.time.diff().values[-1] / HOUR
                    break

    def __len__(self):
        """Get the number of cyclone tracks within TrackRun."""
        return self.data.index.get_level_values(0).to_series().nunique()

    def __repr__(self):  # noqa
        rtr = ReprTrackRun(self)
        return rtr.str_repr(short=True)

    def __str__(self):  # noqa
        rtr = ReprTrackRun(self)
        return rtr.str_repr(short=False)

    def _repr_html_(self):
        rtr = ReprTrackRun(self)
        return rtr.html_repr()

    def __add__(self, other):
        """Combine two TrackRun objects together."""
        new = self.__class__()
        new.extend(self)
        new.extend(other)
        return new

    def __getitem__(self, subset):  # noqa
        if (subset in [slice(None), None, "all"]) or len(self) == 0:
            return self.data
        else:
            if self.is_categorised:
                if isinstance(subset, str):
                    subsets = [subset]
                else:
                    # if list of several subsets is given
                    subsets = subset
                selected = True
                for label in subsets:
                    if label not in self.cats.columns:
                        raise SelectError(
                            f"'{label}' is not among categories: {', '.join(self.cats.columns)}"
                        )
                    selected &= self.cats[label]
                idx = self.cats[selected].index
                return self.data.loc[idx, :]
            else:
                raise NotCategorisedError

    @property
    def cat_labels(self):
        """List of category labels."""
        try:
            return list(self.cats.columns)
        except AttributeError:
            return []

    @property
    def _pbar(self):
        """Get progress bar."""
        return get_pbar()

    @property
    def gb(self):
        """Group by track index."""
        return self.data.gb

    def size(self, subset=None):
        """Size of subset of tracks."""
        return self[subset].index.get_level_values(0).to_series().nunique()

    def rename_cats(self, **mapping):
        """
        Rename categories of the TrackRun.

        Parameters
        ----------
        mapping: dict
            How to rename categories, {old_key: new_key}
        """
        if self.is_categorised:
            self.cats = self.cats.rename(columns=mapping)
        else:
            raise NotCategorisedError

    def load_data(self, dirname, conf_file=None, loader_cls=PMCTRACKLoader):
        """
        Read tracking results from a directory into `TrackRun.data` attribute.

        Parameters
        ----------
        dirname: pathlib.Path
            Path to the directory with tracking output
        conf_file: pathlib.Path, optional
            Path to the configuration file. If omitted, an attempt is
            made to find a .conf file in the `dirname` directory
        loader_cls: type, optional
            Loader with methods to load files.
            By default, `octant.io.PMCTRACKLoader` is used.

        See Also
        --------
        octant.core.TrackRun.to_archive, octant.core.TrackRun.from_archive,
        octant.io.CSVLoader, octant.io.PMCTRACKLoader, octant.io.STARSLoader
        """
        self.sources.append(str(dirname))

        # Load configuration
        if conf_file is None:
            try:
                conf_file = list(dirname.glob("*.conf"))[0]
                self.conf = TrackSettings(conf_file)
            except (IndexError, AttributeError):
                msg = (
                    "Track settings file (.conf) in the `dirname` directory"
                    "is missing or could not be read"
                )
                warnings.warn(msg, MissingConfWarning)

        # Load the tracks
        loader_obj = loader_cls(dirname=dirname)
        self.data = loader_obj()
        self.columns = self.data.columns

    @classmethod
    def from_archive(cls, filename):
        """
        Construct TrackRun object from HDF5 file.

        Parameters
        ----------
        filename: str
            File path to HDF5 file

        Returns
        -------
        octant.core.TrackRun

        """
        with pd.HDFStore(filename, mode="r") as store:
            df = store[ARCH_KEY]
            metadata = store.get_storer(ARCH_KEY).attrs.metadata
            if metadata["is_categorised"]:
                try:
                    df_cat = store.get(ARCH_KEY_CAT)
                except KeyError:
                    msg = (
                        "TrackRun is saved as categorised, but the file does not have"
                        f" {ARCH_KEY_CAT} with category data;"
                        " replacing self.cats with an empty DataFrame."
                    )
                    warnings.warn(msg, InconsistencyWarning)
                    df_cat = pd.DataFrame(
                        index=df[cls._mux_names[0]].unique(), columns=[cls._mux_names[0]]
                    )
        out = cls()
        if df.shape[0] > 0:
            out.data = OctantTrack.from_mux_df(df.set_index(cls._mux_names))
        else:
            out.data = OctantTrack.from_mux_df(df)
        metadata["conf"] = TrackSettings.from_dict(metadata["conf"])
        out.__dict__.update(metadata)
        if out.is_categorised:
            out.cats = df_cat.set_index(cls._mux_names[0]).astype(bool)
        return out

    def to_archive(self, filename):
        """
        Save TrackRun and its metadata to HDF5 file.

        Parameters
        ----------
        filename: str
            File path to HDF5 file
        """
        with pd.HDFStore(filename, mode="w") as store:
            if self.size() > 0:
                df = pd.DataFrame.from_records(self.data.to_records(index=True))
            else:
                df = pd.DataFrame(columns=self.columns, index=self.data.index)
            store.put(ARCH_KEY, df)
            metadata = {
                k: v
                for k, v in self.__dict__.items()
                if k not in ["data", "filelist", "conf", "cats"]
            }
            metadata["conf"] = getattr(self.conf, "to_dict", lambda: {})()
            store.get_storer(ARCH_KEY).attrs.metadata = metadata
            # Store DataFrame with categorisation data
            if self.is_categorised:
                df_cat = pd.DataFrame.from_records(self.cats.astype("uint8").to_records(index=True))
                store.put(ARCH_KEY_CAT, df_cat)

    def extend(self, other, adapt_conf=True):
        """
        Extend the TrackRun by appending elements from another TrackRun.

        Parameters
        ---------
        other: octant.core.TrackRun
            Another TrackRun
        adapt_conf: bool
            Merge TrackSettings (.conf attribute) of each of the TrackRuns
            This is done by retaining matching values and setting other to None
        """
        # Check if category metadata match
        if (self.size() > 0) and (other.size() > 0):
            for attr in ["is_cat_inclusive", "is_categorised"]:
                a, b = getattr(self, attr), getattr(other, attr)
                if a != b:
                    raise ConcatenationError(
                        f"Categorisation metadata is different for '{attr}': {a} != {b}"
                    )
        elif other.size() > 0:
            for attr in ["is_cat_inclusive", "is_categorised"]:
                setattr(self, attr, getattr(other, attr))
        if getattr(self, "tstep_h", None) is None:
            self.tstep_h = getattr(other, "tstep_h", None)
        else:
            if getattr(other, "tstep_h", None) is not None:
                if self.tstep_h != other.tstep_h:
                    raise ConcatenationError(
                        "Extending by a TrackRun with different timestep is not allowed"
                    )
        if adapt_conf and other.conf is not None:
            if self.conf is None:
                self.conf = other.conf.copy()
            else:
                for field in self.conf._fields:
                    if getattr(self.conf, field) != getattr(other.conf, field):
                        setattr(self.conf, field, None)
        self.sources.extend(other.sources)

        new_data = pd.concat([self.data, other.data], sort=False)
        new_track_idx = new_data.index.get_level_values(0).to_series()
        new_track_idx = new_track_idx.ne(new_track_idx.shift()).cumsum() - 1

        mux = pd.MultiIndex.from_arrays(
            [new_track_idx, new_data.index.get_level_values(1)], names=new_data.index.names
        )
        self.data = new_data.set_index(mux)

        # Concatenate categories
        if (self.cats is not None) or (other.cats is not None):
            new_cats = pd.concat([self.cats, other.cats], sort=False).fillna(False)
            new_track_idx = new_cats.index.get_level_values(0).to_series()
            new_track_idx = new_track_idx.ne(new_track_idx.shift()).cumsum() - 1

            ix = pd.Index(new_track_idx, name=new_cats.index.name)
            self.cats = new_cats.set_index(ix)

    def time_slice(self, start=None, end=None):
        """
        Subset TrackRun by time using pandas boolean indexing.

        Parameters
        ----------
        start: str or datetime.datetime, optional
            Start of the slice (inclusive)
        stop: str or datetime.datetime, optional
            End of the slice (inclusive)

        Returns
        -------
        octant.core.TrackRun

        Examples
        --------
        >>> from octant.core import TrackRun
        >>> tr = TrackRun(path_to_directory_with_tracks)
        >>> sub_tr = tr.time_slice('2018-09-04', '2018-11-25')
        """
        if (start is None) and (end is None):
            return self
        else:
            crit = True
            if start is not None:
                crit &= self.data.time >= start
            if end is not None:
                crit &= self.data.time <= end
            # Create a copy of this TrackRun
            result = self.__class__()
            result.extend(self)
            # Replace data with TrackRun.data sliced by start or end
            result.data = result.data[crit]
            # Clear up sources to avoid confusion
            result.sources = []
            result.dirname = None
            result.filelist = []
            try:
                result.conf.dt_start = None
                result.conf.dt_end = None
            except AttributeError:
                pass
            return result

    def classify(self, conditions, inclusive=False, clear=True):
        """
        Categorise the loaded tracks.

        Parameters
        ----------
        conditions: list
            List of tuples. Each tuple is a (label, list) pair containing the category label and
            a list of functions each of which has OctantTrack as its only argument.
            The method assigns numbers to the labels in the same order
            that they are given, starting from number 1 (see examples).
        inclusive: bool, optional
            If true, next category in the list is a subset of lower category;
            otherwise categories are independent.
        clear: bool, optional
            If true, existing TrackRun categories are deleted.

        Examples
        --------
        A check using just OctantTrack properties

        >>> from octant.core import TrackRun
        >>> tr = TrackRun(path_to_directory_with_tracks)
        >>> def myfun(x):
                bool_flag = (x.vortex_type != 0).sum() / x.shape[0] < 0.2
                return bool_flag
        >>> conds = [
            ('category_a', [lambda ot: ot.lifetime_h >= 6]),  # only 1 function checking lifetime
            ('category_b', [myfun,
                            lambda ot: ot.gen_lys_dist_km > 300.0])  # 2 functions
        ]
        >>> tr.classify(conds)
        >>> tr.size('category_a'), tr.size('category_b')
        31, 10

        For more examples, see example notebooks.

        See Also
        --------
        octant.misc.check_by_mask
        """
        if clear:
            self.clear_categories()

        self.is_cat_inclusive = inclusive

        cond_with_new_labels = []
        for icond, (label, funcs) in enumerate(conditions):
            if label == "all":
                raise ArgumentError("'all' is not a permitted label")
            if self.is_cat_inclusive:
                lab = (
                    label
                    + self._cat_sep * min(icond, 1)
                    + self._cat_sep.join([j[0] for j in conditions[:icond][::-1]])
                )
            else:
                lab = label
            cond_with_new_labels.append((lab, funcs))

        self.cats = pd.concat(
            [
                self.cats,
                pd.DataFrame(
                    index=self.data.index.get_level_values(0).unique(),
                    columns=[cond[0] for cond in cond_with_new_labels],
                ).fillna(False),
            ],
            axis="columns",
        )

        for i, ot in self._pbar(self.gb):
            prev_flag = True
            for label, funcs in cond_with_new_labels:
                if self.is_cat_inclusive:
                    _flag = prev_flag
                else:
                    _flag = True

                for func in funcs:
                    _flag &= func(ot)
                if _flag:
                    self.cats.loc[i, label] = True
                if self.is_cat_inclusive:
                    prev_flag = _flag
        self.is_categorised = True

    def categorise(self, *args, **kwargs):
        """Alias for classify()."""
        return self.classify(*args, **kwargs)

    def categorise_by_percentile(self, by, subset=None, perc=95, oper="ge"):
        """
        Categorise by percentile.

        Parameters
        ----------
        by: str or tuple
            Property of OctantTrack to apply percentile to.
            If tuple is passed, it should be of form (label, function), where the function
            takes OctantTrack as the only parameter and returns one value for each track.
        subset: str, optional
            Subset of Trackrun to apply percentile to.
        perc: float, optional
            Percentile to define a category of cyclones.
            E.g. (perc=95, oper='ge') is the top 5% cyclones.
        oper: str, optional
            Math operator to select track above or below the percentile threshold.
            Can be one of (lt|le|gt|ge).

        Examples
        --------
        Existing property: `max_vort`

        >>> tr = TrackRun(path_to_directory_with_tracks)
        >>> tr.is_categorised
        False
        >>> tr.categorise_by_percentile("max_vort", perc=90, oper="gt")
        >>> tr.cat_labels
        ['max_vort__gt__90pc']
        >>> tr.size()
        71
        >>> tr.size("max_vort__gt__90pc")
        7

        Custom function

        >>> tr = TrackRun(path_to_directory_with_tracks)
        >>> tr.is_categorised
        False
        >>> tr.categorise_by_percentile(by=("slp_min", lambda x: np.nanmin(x.slp.values)),
                                        perc=20, oper="le")
        >>> tr.cat_labels
        ['slp_min__le__20pc']
        >>> tr.size("slp_min__le__10pc")
        14


        See Also
        --------
        octant.core.TrackRun.classify
        """
        allowed_ops = ["lt", "le", "gt", "ge"]
        if oper not in allowed_ops:
            raise ArgumentError(f"oper={oper} should be one of {allowed_ops}")
        op = getattr(operator, oper)
        if subset is None:
            label = ""
        else:
            label = f"{self._cat_sep}{subset}"
        if isinstance(by, str):
            by_label = by
            func = lambda x: getattr(x, by)  # noqa
        else:
            by_label, func = by
        label = f"{by_label}__{oper}__{perc}pc" + label

        v_per_track = self[subset].gb.apply(func)
        if len(v_per_track) > 0:
            # If this subset is not empty, create a new column in categories
            new_col = pd.DataFrame(
                index=self.data.index.get_level_values(0).unique(), columns=[label]
            ).fillna(False)
            # Find numerical threshold with the given percentage
            thresh = np.percentile(v_per_track, perc)
            # Find all tracks above it
            above_thresh = v_per_track[op(v_per_track, thresh)]
            # Punch the corresponding elements in cats
            new_col.loc[above_thresh.index, label] = True
            # Append the new category column to cats
            self.cats = pd.concat([self.cats, new_col], axis=1)
            self.is_categorised = True

    def clear_categories(self, subset=None, inclusive=None):
        """
        Clear TrackRun of its categories.

        If categories are inclusive, it destroys child categories

        Parameters
        ----------
        subset: str, optional
            If None (default), all categories are removed.
        inclusive: bool or None, optional
            If supplied, is used instead of is_cat_inclusive attribute.

        Examples
        --------
        Inclusive categories

        >>> tr = TrackRun(path_to_directory_with_tracks)
        >>> tr.is_cat_inclusive
        True
        >>> tr.cat_labels
        ['pmc', 'max_vort__ge__90pc|pmc']
        >>> tr.clear_categories(subset='pmc')
        >>> tr.cat_labels
        []

        Non-inclusive

        >>> tr = TrackRun(path_to_directory_with_tracks)
        >>> tr.cat_labels
        ['pmc', 'max_vort__ge__90pc|pmc']
        >>> tr.clear_categories(subset='pmc', inclusive=False)
        >>> tr.cat_labels
        ['max_vort__ge__90pc|pmc']
        """
        if inclusive is not None:
            inc = inclusive
        else:
            inc = self.is_cat_inclusive
        if subset is None:
            # clear all categories
            self.cats = None
        else:
            # Do not use self[subset].blah = 0 ! - SettingWithCopyWarning
            if inc:
                self.cats = self.cats.drop(
                    columns=[col for col in self.cats.columns.values if subset in col]
                )
            else:
                self.cats = self.cats.drop(columns=subset)
        if len(self.cat_labels) == 0:
            self.is_categorised = False
            self.is_cat_inclusive = False

    def match_tracks(
        self,
        others,
        subset=None,
        method="simple",
        interpolate_to="other",
        thresh_dist=250.0,
        time_frac=0.5,
        return_dist_matrix=False,
        beta=100.0,
        r_planet=EARTH_RADIUS,
    ):
        """
        Match tracked vortices to a list of vortices from another data source.

        Parameters
        ----------
        others: list or octant.core.TrackRun
            List of dataframes or a TrackRun instance
        subset: str, optional
            Subset (category) of TrackRun to match.
            If not given, the matching is done for all categories.
        method: str, optional
            Method of matching (intersection|simple|bs2000)
        interpolate_to: str, optional
            Interpolate `TrackRun` times to `other` times, or vice versa
        thresh_dist: float, optional
            Radius (km) threshold of distances between vortices.
            Used in 'intersection' and 'simple' methods
        time_frac: float, optional
            Fraction of a vortex lifetime used as a threshold in 'intersection'
            and 'simple' methods
        return_dist_matrix: bool, optional
            Used when method='bs2000'. If True, the method returns a tuple
            of matching pairs and distance matrix used to calculate them
        beta: float, optional
            Parameter used in 'bs2000' method
            E.g. beta=100 corresponds to 10 m/s average steering wind
        r_planet: float, optional
            Radius of the planet in metres
            Default: EARTH_RADIUS

        Returns
        -------
        match_pairs: list
            Index pairs of `other` vortices matching a vortex in `TrackRun`
            in a form (<index of `TrackRun` subset>, <index of `other`>)
        dist_matrix: numpy.ndarray
            2D array, returned if return_dist_matrix=True
        """
        # Recursive call for each of the available categies
        if subset is None:
            if self.is_categorised:
                result = {}
                for subset_key in self.cat_labels:
                    result[subset_key] = self.match_tracks(
                        others,
                        subset=subset_key,
                        method=method,
                        interpolate_to=interpolate_to,
                        thresh_dist=thresh_dist,
                        time_frac=time_frac,
                        return_dist_matrix=return_dist_matrix,
                        beta=beta,
                        r_planet=r_planet,
                    )
                return result
            else:
                subset = "all"

        # Select subset
        sub_gb = self[subset].gb
        if len(sub_gb) == 0 or len(others) == 0:
            return []
        if isinstance(others, list):
            # match against a list of DataFrames of tracks
            other_gb = pd.concat(
                [OctantTrack.from_df(df) for df in others],
                keys=range(len(others)),
                names=self._mux_names,
            ).gb
        elif isinstance(others, self.__class__):
            # match against another TrackRun
            other_gb = others[subset].gb
        else:
            raise ArgumentError('Argument "others" ' f"has a wrong type: {type(others)}")
        match_pairs = []
        if method == "intersection":
            for idx, ot in self._pbar(sub_gb):  # , desc="self tracks"):
                for other_idx, other_ot in self._pbar(other_gb, leave=False):
                    times = other_ot.time.values
                    time_match_thresh = time_frac * (times[-1] - times[0]) / HOUR

                    intersect = pd.merge(other_ot, ot, how="inner", left_on="time", right_on="time")
                    n_match_times = intersect.shape[0]
                    if n_match_times > 0:
                        _tstep_h = intersect.time.diff().values[-1] / HOUR
                        dist = intersect[["lon_x", "lon_y", "lat_x", "lat_y"]].apply(
                            lambda x: great_circle(*x.values, r_planet=r_planet), axis=1
                        )
                        prox_time = (dist < (thresh_dist * KM2M)).sum() * _tstep_h
                        if (
                            n_match_times * _tstep_h > time_match_thresh
                        ) and prox_time > time_match_thresh:
                            match_pairs.append((idx, other_idx))
                            break

        elif method == "simple":
            # TODO: explain
            ll = ["lon", "lat"]
            match_pairs = []
            for other_idx, other_ct in self._pbar(other_gb):  # , desc="other tracks"):
                candidates = []
                for idx, ct in self._pbar(sub_gb, leave=False):  # , desc="self tracks"):
                    if interpolate_to == "other":
                        df1, df2 = ct.copy(), other_ct
                    elif interpolate_to == "self":
                        df1, df2 = other_ct, ct.copy()
                    l_start = max(df1.time.values[0], df2.time.values[0])
                    e_end = min(df1.time.values[-1], df2.time.values[-1])
                    if (e_end - l_start) / HOUR > 0:
                        # df1 = df1.set_index('time')[ll]
                        # ts = pd.Series(index=df2.time)
                        # new_df1 = (pd.concat([df1, ts]).sort_index()
                        #            .interpolate(method='values')
                        #            .loc[ts.index])[ll]
                        tmp_df2 = pd.DataFrame(
                            data={"lon": np.nan, "lat": np.nan, "time": df2.time}, index=df2.index
                        )
                        new_df1 = (
                            pd.concat([df1[[*ll, "time"]], tmp_df2], ignore_index=True, keys="time")
                            .set_index("time")
                            .sort_index()
                            .interpolate(method="values")
                            .loc[tmp_df2.time]
                        )[ll]
                        new_df1 = new_df1[~new_df1.lon.isnull()]

                        # thr = (time_frac * 0.5
                        #       * (df2.time.values[-1] - df2.time.values[0]
                        #          + df1.time.values[-1] - df2.time.values[0]))
                        thr = time_frac * df2.shape[0]
                        dist_diff = np.full(new_df1.shape[0], FILLVAL)
                        for i, ((x1, y1), (x2, y2)) in enumerate(
                            zip(new_df1[ll].values, df2[ll].values)
                        ):
                            dist_diff[i] = great_circle(x1, x2, y1, y2, r_planet=r_planet)
                        within_r_idx = dist_diff < (thresh_dist * KM2M)
                        # if within_r_idx.any():
                        #     if (new_df1[within_r_idx].index[-1]
                        #        - new_df1[within_r_idx].index[0]) > thr:
                        #         candidates.append((idx, within_r_idx.sum()))
                        if within_r_idx.sum() > thr:
                            candidates.append((idx, within_r_idx.sum()))
                if len(candidates) > 0:
                    candidates = sorted(candidates, key=lambda x: x[1])
                    final_idx = candidates[-1][0]
                    match_pairs.append((final_idx, other_idx))

        elif method == "bs2000":
            # sub_list = [i[0] for i in list(sub_gb)]
            sub_indices = list(sub_gb.indices.keys())
            other_indices = list(other_gb.indices.keys())
            dist_matrix = np.full((len(sub_gb), len(other_gb)), FILLVAL)
            for i, (_, ct) in enumerate(self._pbar(sub_gb, leave=False)):  # , desc="self tracks"):
                x1, y1, t1 = ct.coord_view
                for j, (_, other_ct) in enumerate(self._pbar(other_gb, leave=False)):
                    x2, y2, t2 = other_ct.coord_view
                    dist_matrix[i, j] = distance_metric(
                        x1, y1, t1, x2, y2, t2, beta=float(beta), r_planet=r_planet
                    )
            for i, idx1 in enumerate(np.nanargmin(dist_matrix, axis=0)):
                for j, idx2 in enumerate(np.nanargmin(dist_matrix, axis=1)):
                    if i == idx2 and j == idx1:
                        match_pairs.append((sub_indices[idx1], other_indices[idx2]))
            if return_dist_matrix:
                return match_pairs, dist_matrix
        else:
            raise ArgumentError(f"Unknown method: {method}")

        return match_pairs

    def density(
        self,
        lon1d,
        lat1d,
        by="point",
        subset=None,
        method="cell",
        dist=222.0,
        exclude_first={"m": 10, "d": 1},
        exclude_last={"m": 4, "d": 30},
        grid_centres=True,
        weight_by_area=True,
        r_planet=EARTH_RADIUS,
    ):
        """
        Calculate different types of cyclone density for a given lon-lat grid.

        - `point`: all points of all tracks
        - `track`: each track only once for a given cell or circle
        - `genesis`: starting positions (excluding start date of tracking)
        - `lysis`: ending positions (excluding final date of tracking)

        Parameters
        ----------
        lon1d: numpy.ndarray
            Longitude points array of shape (M,)
        lat1d: numpy.ndarray
            Latitude points array of shape (N,)
        by: str, optional
            Type of cyclone density (point|track|genesis|lysis)
        subset: str, optional
            Subset (category) of TrackRun to calculate density from.
            If not given, the calculation is done for all categories.
        method: str, optional
            Method to calculate density (radius|cell)
        dist: float, optional
            Distance in km
            Used when method='radius'
            Default: ~2deg on Earth
        exclude_first: dict, optional
            Exclude start date (month, day)
        exclude_last: dict, optional
            Exclude end date (month, day)
        grid_centres: bool, optional
            If true, the function assumes that lon (M,) and lat (N,) arrays are grid centres
            and calculates boundaries, arrays of shape (M+1,) and (N+1,) so that the density
            values refer to centre points given.
            If false, the density is calculated between grid points.
        weight_by_area: bool, optional
            Weight result by area of grid cells.
        r_planet: float, optional
            Radius of the planet in metres
            Default: EARTH_RADIUS

        Returns
        -------
        dens: xarray.DataArray
            Array of track density of shape (M, N) with useful metadata in attrs
        """
        # Recursive call for each of the available categies
        if subset is None:
            if self.is_categorised:
                result = {}
                for subset_key in self.cat_labels:
                    result[subset_key] = self.density(
                        lon1d,
                        lat1d,
                        by=by,
                        subset=subset_key,
                        method=method,
                        dist=dist,
                        exclude_first=exclude_first,
                        exclude_last=exclude_last,
                        grid_centres=grid_centres,
                        weight_by_area=weight_by_area,
                        r_planet=r_planet,
                    )
                return result
            else:
                subset = "all"

        # Redefine grid if necessary
        if grid_centres:
            # Input arrays are centres of grid cells, so cell boundaries need to be calculated
            lon = cell_bounds(lon1d)
            lat = cell_bounds(lat1d)
            # Prepare coordinates for output
            xlon = xr.IndexVariable(dims="longitude", data=lon1d, attrs={"units": "degrees_east"})
            xlat = xr.IndexVariable(dims="latitude", data=lat1d, attrs={"units": "degrees_north"})
        else:
            # Input arrays are boundaries of grid cells, so cell centres need to be
            # calculated for the output
            lon, lat = lon1d, lat1d
            # Prepare coordinates for output
            xlon = xr.IndexVariable(
                dims="longitude", data=cell_centres(lon1d), attrs={"units": "degrees_east"}
            )
            xlat = xr.IndexVariable(
                dims="latitude", data=cell_centres(lat1d), attrs={"units": "degrees_north"}
            )

        # Create 2D mesh
        lon2d, lat2d = np.meshgrid(lon, lat)

        # Select subset
        sub_df = self[subset]
        # Prepare coordinates for cython
        lon2d_c = lon2d.astype("double", order="C")
        lat2d_c = lat2d.astype("double", order="C")

        # Select method
        if method == "radius":
            # Convert radius to metres
            dist_metres = dist * KM2M
            units = f"per {round(np.pi * dist**2)} km2"
            if by == "track":
                cy_func = partial(track_density_rad, dist=dist_metres, r_planet=r_planet)
            else:
                cy_func = partial(point_density_rad, dist=dist_metres, r_planet=r_planet)
        elif method == "cell":
            # TODO: make this check more flexible
            if (np.diff(lon2d[0, :]) < 0).any() or (np.diff(lat2d[:, 0]) < 0).any():
                raise GridError("Grid values must be in an ascending order")
            units = "1"
            if by == "track":
                cy_func = track_density_cell
            else:
                cy_func = point_density_cell

        # Convert dataframe columns to C-ordered arrays
        if by == "point":
            sub_data = sub_df.lonlat_c
        elif by == "track":
            sub_data = sub_df.tridlonlat_c
        elif by == "genesis":
            sub_data = (
                sub_df.gb.filter(_exclude_by_first_day, **exclude_first).xs(0, level="row_idx")
            ).lonlat_c
        elif by == "lysis":
            sub_data = (
                self[subset].gb.tail(1).gb.filter(_exclude_by_last_day, **exclude_last)
            ).lonlat_c
        else:
            raise ArgumentError("`by` should be one of point|track|genesis|lysis")

        data = cy_func(lon2d_c, lat2d_c, sub_data).base

        if weight_by_area:
            # calculate area in metres
            area = grid_cell_areas(xlon.values, xlat.values, r_planet=r_planet)
            data /= area
            data *= KM2M * KM2M  # convert to km^{-2}
            units = "km-2"

        dens = xr.DataArray(
            data,
            name=f"{by}_density",
            attrs={"units": units, "subset": subset, "method": method},
            dims=("latitude", "longitude"),
            coords={"longitude": xlon, "latitude": xlat},
        )
        return dens
