"""Curvilinear native-grid -> rectilinear regridding for Arctic products.

The Copernicus Marine ``originalGrid`` parts of the Arctic TOPAZ family are
delivered on the native curvilinear (y, x) grid with 2-D ``latitude`` /
``longitude`` coordinates.  The ``default`` part A previously ingested is a
service-side rectilinear reconstruction that contains scattered holes (native
data exists, but the reconstruction leaves NaN cells).  This module rebuilds
the rectilinear target grid directly from the native grid with a conservative
nearest-neighbour rule:

* candidates are native cells whose variable value is finite (and optionally a
  native water mask);
* distance is exact great-circle distance to the nearest candidate;
* cells farther than ``max_distance_km`` stay NaN (genuinely missing);
* target cells marked land by ``target_water_mask`` stay NaN (no interpolation
  across land);
* no extrapolation and no value invention.

Vector orientation is NOT handled here: native projected components (e.g.
``vxo``/``vyo`` with ``standard_name=sea_water_x/y_velocity``) are regridded as
components and later rotated by A's existing normalisation
(``normalization._normalize_vector_semantics``, polar-stereographic x/y ->
true east/north).
"""

from __future__ import annotations

import numpy as np
from scipy.spatial import cKDTree


def _haversine_km(
    lat1: np.ndarray,
    lon1: np.ndarray,
    lat2: np.ndarray,
    lon2: np.ndarray,
) -> np.ndarray:
    r_lat1 = np.radians(lat1)
    r_lat2 = np.radians(lat2)
    dlat = r_lat2 - r_lat1
    dlon = np.radians(lon2 - lon1)
    a = (
        np.sin(dlat / 2.0) ** 2
        + np.cos(r_lat1) * np.cos(r_lat2) * np.sin(dlon / 2.0) ** 2
    )
    return 2.0 * 6371.0088 * np.arcsin(np.sqrt(a))


def _local_xy(lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
    """Equirectangular-scaled coordinates (lon*cos(lat), lat) for KD-tree."""

    return np.stack((lon * np.cos(np.radians(lat)), lat), axis=-1)


def regrid_nearest_curvilinear(
    *,
    values: np.ndarray,
    native_lon: np.ndarray,
    native_lat: np.ndarray,
    target_lon: np.ndarray,
    target_lat: np.ndarray,
    max_distance_km: float,
    native_water_mask: np.ndarray | None = None,
    target_water_mask: np.ndarray | None = None,
) -> np.ndarray:
    """Regrid one native (ny, nx) field to a rectilinear target grid.

    Returns an array of shape ``(target_lat.size, target_lon.size)``.  Each
    target cell receives the value of the nearest *finite* native cell when it
    is within ``max_distance_km`` and the target is water (when
    ``target_water_mask`` is provided); otherwise NaN.
    """

    values = np.asarray(values, dtype=np.float64)
    native_lon = np.asarray(native_lon, dtype=np.float64)
    native_lat = np.asarray(native_lat, dtype=np.float64)
    if values.shape != native_lon.shape or native_lon.shape != native_lat.shape:
        raise ValueError("values/native_lon/native_lat must share shape (ny, nx)")
    target_lon = np.asarray(target_lon, dtype=np.float64)
    target_lat = np.asarray(target_lat, dtype=np.float64)
    if target_lon.ndim != 1 or target_lat.ndim != 1:
        raise ValueError("target_lon/target_lat must be 1-D")
    if not np.isfinite(max_distance_km) or max_distance_km <= 0:
        raise ValueError("max_distance_km must be finite and positive")

    finite = np.isfinite(values)
    if native_water_mask is not None:
        finite &= np.asarray(native_water_mask, dtype=bool)
    candidates = np.flatnonzero(finite)
    out = np.full((target_lat.size, target_lon.size), np.nan, dtype=np.float64)
    if candidates.size == 0:
        return out

    src_xy = _local_xy(native_lon.ravel()[candidates], native_lat.ravel()[candidates])
    tree = cKDTree(src_xy)

    tlon, tlat = np.meshgrid(target_lon, target_lat)
    flat_lon = tlon.ravel()
    flat_lat = tlat.ravel()
    tgt_xy = _local_xy(flat_lon, flat_lat)
    _, nearest = tree.query(tgt_xy, k=1)

    src_lon = native_lon.ravel()[candidates]
    src_lat = native_lat.ravel()[candidates]
    distance_km = _haversine_km(flat_lat, flat_lon, src_lat[nearest], src_lon[nearest])
    ok = distance_km <= max_distance_km
    if target_water_mask is not None:
        mask = np.asarray(target_water_mask, dtype=bool)
        if mask.shape != out.shape:
            raise ValueError("target_water_mask must match target grid shape")
        ok &= mask.ravel()
    out.ravel()[ok] = values.ravel()[candidates[nearest[ok]]]
    return out


def regrid_components(
    *,
    values_by_name: dict[str, np.ndarray],
    native_lon: np.ndarray,
    native_lat: np.ndarray,
    target_lon: np.ndarray,
    target_lat: np.ndarray,
    max_distance_km: float,
    native_water_mask: np.ndarray | None = None,
    target_water_mask: np.ndarray | None = None,
) -> dict[str, np.ndarray]:
    """Regrid several variables on the same native grid (components kept as-is)."""

    return {
        name: regrid_nearest_curvilinear(
            values=arr,
            native_lon=native_lon,
            native_lat=native_lat,
            target_lon=target_lon,
            target_lat=target_lat,
            max_distance_km=max_distance_km,
            native_water_mask=native_water_mask,
            target_water_mask=target_water_mask,
        )
        for name, arr in values_by_name.items()
    }
