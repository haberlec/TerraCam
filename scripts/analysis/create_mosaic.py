#!/usr/bin/env python3
"""
Mosaic Assembly Script

Assembles individual grid survey position tiles into a single mosaic image.
Supports three modes:

  coordinate:  Place tiles based on PTU pan/tilt angles and lens FOV with
               linear blending in overlap regions. Fast and deterministic.

  opencv:      Column-first stitching with phase correlation refinement.
               Exploits nodal-point tilt axis (no tilt parallax).

  cylindrical: Gnomonic-to-cylindrical equirectangular reprojection using
               the pinhole camera model with SIFT-based global affine
               refinement. Correctly accounts for the nonlinear
               pixel-to-angle mapping of rectilinear lenses.

Usage:
    python -m scripts.analysis.create_mosaic \\
        --summary out/survey_summary.json \\
        --band 0

    python -m scripts.analysis.create_mosaic \\
        --summary out/survey_summary.json \\
        --band 0 --mode opencv

    python -m scripts.analysis.create_mosaic \\
        --summary out/survey_summary.json \\
        --band 5 --by-filter --mode cylindrical

    # Save projection model from reference band, reuse for other bands:
    python -m scripts.analysis.create_mosaic \\
        --summary out/survey_summary.json \\
        --band 5 --mode cylindrical --save-model model.json

    python -m scripts.analysis.create_mosaic \\
        --summary out/survey_summary.json \\
        --band 0 --mode cylindrical --load-model model.json
"""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import netCDF4 as nc
from PIL import Image
from scipy.ndimage import map_coordinates

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Boresight lever-arm parallax
# ---------------------------------------------------------------------------
# The camera entrance pupil is offset from the PTU pan/tilt axes
# intersection, so at finite scene range each tile's true pointing
# differs from the commanded pan/tilt (the pan-axis parallax that the
# opencv mode's phase correlation absorbs empirically). Configured once
# from main(); parallax_corrected_center() is a self-contained mirror of
# fli.geometry.boresight_azel — equivalence is pinned by
# tests/unit/test_geometry.py (analysis scripts do not import the fli
# package, which loads the camera C library on import).

_CAMERA_OFFSET_M = (0.0, 0.0, 0.0)
_SCENE_RANGE_M = None


def configure_parallax(
    camera_offset_m: Tuple[float, float, float],
    scene_range_m: float,
) -> None:
    """Set the mount lever arm and scene range for tile placement.

    Parameters
    ----------
    camera_offset_m : tuple of (float, float, float)
        Camera pupil offset from the PTU axes intersection in the
        tilt-stage frame: (forward, along tilt axis, up), meters.
    scene_range_m : float or None
        Assumed scene distance from the PTU origin in meters; None
        disables the correction (scene at infinity).
    """
    global _CAMERA_OFFSET_M, _SCENE_RANGE_M
    _CAMERA_OFFSET_M = tuple(camera_offset_m)
    _SCENE_RANGE_M = scene_range_m


def load_mount_offset() -> Tuple[float, float, float]:
    """Read camera_offset_m from ptu_specifications.json mount_geometry.

    Honors the TERRACAM_CONFIG environment variable; returns a zero
    offset (with a warning) if the config cannot be read.
    """
    import os
    env_dir = os.environ.get("TERRACAM_CONFIG")
    if env_dir:
        path = Path(env_dir) / "ptu_specifications.json"
    else:
        path = (Path(__file__).resolve().parents[2] / "config" /
                "ptu_specifications.json")
    try:
        with open(path) as f:
            mount = json.load(f).get("mount_geometry", {})
        offset = mount.get("camera_offset_m", {})
        return (
            float(offset.get("forward", 0.0)),
            float(offset.get("along_tilt_axis", 0.0)),
            float(offset.get("up", 0.0)),
        )
    except (OSError, json.JSONDecodeError, ValueError) as e:
        logger.warning(f"Could not read mount geometry ({e}); "
                       f"assuming zero camera offset")
        return (0.0, 0.0, 0.0)


def parallax_corrected_center(
    pan_deg: float, tilt_deg: float
) -> Tuple[float, float]:
    """Az/el of the boresight ray for a tile, in the PTU frame.

    With no configured scene range (infinity) this is the identity. At
    finite range, the boresight ray originates at the rotated camera
    offset and is intersected with the sphere of the configured radius
    centered on the PTU origin.

    Parameters
    ----------
    pan_deg, tilt_deg : float
        Commanded (or encoder-derived) PTU angles for the tile.

    Returns
    -------
    tuple of (float, float)
        Parallax-corrected (azimuth, elevation) in degrees.
    """
    if _SCENE_RANGE_M is None:
        return pan_deg, tilt_deg

    p = np.radians(pan_deg)
    t = np.radians(tilt_deg)
    cp, sp = np.cos(p), np.sin(p)
    ct, st = np.cos(t), np.sin(t)

    # Boresight direction and rotated camera offset in the PTU frame
    # (stage->PTU: tilt about stage Y, then pan about PTU Z)
    b = np.array([ct * cp, ct * sp, st])
    fwd, along, up = _CAMERA_OFFSET_M
    o = np.array([
        (fwd * ct - up * st) * cp - along * sp,
        (fwd * ct - up * st) * sp + along * cp,
        fwd * st + up * ct,
    ])

    r_sq = _SCENE_RANGE_M ** 2
    o_sq = float(o @ o)
    if r_sq <= o_sq:
        raise ValueError(
            f"scene range ({_SCENE_RANGE_M} m) must exceed the camera "
            f"offset length ({np.sqrt(o_sq):.3f} m)"
        )
    od = float(o @ b)
    t_ray = -od + np.sqrt(od ** 2 + r_sq - o_sq)
    v = o + t_ray * b

    az = float(np.degrees(np.arctan2(v[1], v[0])))
    el = float(np.degrees(np.arcsin(v[2] / np.linalg.norm(v))))
    return az, el


# ---------------------------------------------------------------------------
# Survey loading
# ---------------------------------------------------------------------------

def read_band_oriented(ds, band_index: int) -> np.ndarray:
    """Read one band and de-rotate it to scene orientation.

    Whether the raw frame needs a 180-degree rotation is read from the
    file's ``sensor_inverted`` attribute (1 = sensor mounted upside-down).
    Files written before that attribute existed (the original inverted
    mount) default to inverted, preserving the historical behavior where
    every tile was rotated 180 degrees. See fli.geometry.image_rotation_k.

    Parameters
    ----------
    ds : netCDF4.Dataset
        Open dataset for one position.
    band_index : int
        Band array index to read.

    Returns
    -------
    np.ndarray
        The band, rotated to scene orientation.
    """
    band = ds.variables["digital_number"][band_index, :, :]
    # Default True: legacy files (no attribute) came from the inverted mount
    inverted = bool(getattr(ds, "sensor_inverted", 1))
    k = 2 if inverted else 0
    return np.rot90(band, k) if k else np.asarray(band)


def load_survey(summary_path: str) -> Tuple[Dict, List[Dict]]:
    """Load survey summary and validate required fields.

    Parameters
    ----------
    summary_path : str
        Path to the grid survey summary JSON file.

    Returns
    -------
    tuple of (dict, list of dict)
        Grid geometry dict and list of position result dicts.
    """
    with open(summary_path) as f:
        summary = json.load(f)

    if "grid_geometry" not in summary:
        raise ValueError(
            "Summary JSON does not contain grid_geometry. "
            "Was this produced by a FOV-aware grid survey?"
        )

    geometry = summary["grid_geometry"]
    required_keys = [
        "fov_h_deg", "fov_v_deg", "overlap",
        "n_pan", "n_tilt",
    ]
    for key in required_keys:
        if key not in geometry:
            raise ValueError(f"grid_geometry missing required key: {key}")

    positions = summary.get("position_results", [])
    successful = [p for p in positions if p.get("success", False)]
    if not successful:
        raise ValueError("No successful positions found in summary")

    logger.info(
        f"Loaded survey: {len(successful)}/{len(positions)} successful "
        f"positions, {geometry['n_pan']}x{geometry['n_tilt']} grid"
    )

    return geometry, successful


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _resolve_nc_path(position: Dict, summary_dir: Path) -> Path:
    """Resolve the NetCDF file path for a position result.

    Parameters
    ----------
    position : dict
        Position result dict from the summary JSON.
    summary_dir : Path
        Directory containing the summary JSON.

    Returns
    -------
    Path
        Absolute path to the NetCDF file.
    """
    captures = position.get("captures", [])
    if not captures:
        raise ValueError(
            f"Position {position['position_id']} has no captures"
        )

    nc_rel = captures[0]["files"].get("netcdf")
    if nc_rel is None:
        raise ValueError(
            f"Position {position['position_id']} has no NetCDF output "
            f"(was the survey run with --output-format netcdf?)"
        )

    nc_path = summary_dir / nc_rel
    if not nc_path.exists():
        nc_path = Path(nc_rel)
    if not nc_path.exists():
        raise FileNotFoundError(
            f"NetCDF file not found: {nc_rel} "
            f"(searched {summary_dir / nc_rel} and {nc_rel})"
        )

    return nc_path


def find_band_index(nc_path: Path, filter_position: int) -> int:
    """Find the array index for a given filter position in a NetCDF file.

    Parameters
    ----------
    nc_path : Path
        Path to a NetCDF file from the survey.
    filter_position : int
        Filter wheel position number.

    Returns
    -------
    int
        Array index (0-based) for the requested filter position.
    """
    with nc.Dataset(nc_path, "r") as ds:
        filter_positions = ds.variables["filter_position"][:]
        matches = np.where(filter_positions == filter_position)[0]
        if len(matches) == 0:
            available = list(filter_positions)
            raise ValueError(
                f"Filter position {filter_position} not found. "
                f"Available: {available}"
            )
        return int(matches[0])


# ---------------------------------------------------------------------------
# Coordinate-based mosaic
# ---------------------------------------------------------------------------

def compute_canvas_geometry(
    geometry: Dict, positions: List[Dict], tile_shape: Tuple[int, int]
) -> Tuple[int, int, float, float, float, float]:
    """Compute the output canvas size and coordinate mapping.

    Determines the angular resolution from the tile dimensions and FOV,
    then computes the total canvas size from the extent of all tile
    positions plus their FOV footprint.

    Parameters
    ----------
    geometry : dict
        Grid geometry from survey summary.
    positions : list of dict
        Position results with target_position pan/tilt.
    tile_shape : tuple of (int, int)
        (height, width) of each tile in pixels.

    Returns
    -------
    tuple of (canvas_w, canvas_h, deg_per_px_h, deg_per_px_v,
              pan_min_edge, tilt_max_edge)
        Canvas dimensions in pixels, angular resolution, and the angular
        coordinates of the top-left canvas corner.
    """
    fov_h = geometry["fov_h_deg"]
    fov_v = geometry["fov_v_deg"]
    tile_h, tile_w = tile_shape

    deg_per_px_h = fov_h / tile_w
    deg_per_px_v = fov_v / tile_h

    pan_angles = [p["target_position"]["pan_deg"] for p in positions]
    tilt_angles = [p["target_position"]["tilt_deg"] for p in positions]

    pan_min_edge = min(pan_angles) - fov_h / 2
    pan_max_edge = max(pan_angles) + fov_h / 2
    tilt_min_edge = min(tilt_angles) - fov_v / 2
    tilt_max_edge = max(tilt_angles) + fov_v / 2

    canvas_w = int(np.ceil((pan_max_edge - pan_min_edge) / deg_per_px_h))
    canvas_h = int(np.ceil((tilt_max_edge - tilt_min_edge) / deg_per_px_v))

    logger.info(
        f"Canvas: {canvas_w}x{canvas_h} px, "
        f"angular extent: {pan_max_edge - pan_min_edge:.2f} x "
        f"{tilt_max_edge - tilt_min_edge:.2f} deg"
    )
    logger.info(
        f"Resolution: {deg_per_px_h:.6f} deg/px (h), "
        f"{deg_per_px_v:.6f} deg/px (v)"
    )

    return canvas_w, canvas_h, deg_per_px_h, deg_per_px_v, pan_min_edge, tilt_max_edge


def compute_weight_map(tile_h: int, tile_w: int, margin: int) -> np.ndarray:
    """Compute a linear distance-from-edge weight map for blending.

    Pixels within ``margin`` of the tile edge ramp linearly from 0 to 1.
    Interior pixels have weight 1.

    Parameters
    ----------
    tile_h : int
        Tile height in pixels.
    tile_w : int
        Tile width in pixels.
    margin : int
        Blending margin width in pixels.

    Returns
    -------
    np.ndarray
        Weight map of shape (tile_h, tile_w), dtype float32, values [0, 1].
    """
    if margin <= 0:
        return np.ones((tile_h, tile_w), dtype=np.float32)

    ramp_left = np.minimum(np.arange(tile_w, dtype=np.float32), margin)
    ramp_right = np.minimum(
        np.arange(tile_w - 1, -1, -1, dtype=np.float32), margin
    )
    horiz = np.minimum(ramp_left, ramp_right) / margin

    ramp_top = np.minimum(np.arange(tile_h, dtype=np.float32), margin)
    ramp_bottom = np.minimum(
        np.arange(tile_h - 1, -1, -1, dtype=np.float32), margin
    )
    vert = np.minimum(ramp_top, ramp_bottom) / margin

    return vert[:, np.newaxis] * horiz[np.newaxis, :]


def assemble_mosaic(
    geometry: Dict,
    positions: List[Dict],
    band_index: int,
    summary_dir: Path,
) -> np.ndarray:
    """Assemble a single-band mosaic from grid survey tiles.

    Parameters
    ----------
    geometry : dict
        Grid geometry from survey summary.
    positions : list of dict
        Successful position results.
    band_index : int
        Band array index (0-based) to extract from each NetCDF file.
    summary_dir : Path
        Directory containing the summary JSON (used to resolve relative
        NetCDF file paths).

    Returns
    -------
    np.ndarray
        Mosaic image as uint16 array.
    """
    first_nc_path = _resolve_nc_path(positions[0], summary_dir)
    with nc.Dataset(first_nc_path, "r") as ds:
        tile_shape = ds.variables["digital_number"].shape[1:]
        n_bands = ds.variables["digital_number"].shape[0]

    if band_index < 0 or band_index >= n_bands:
        raise ValueError(
            f"Band index {band_index} out of range [0, {n_bands - 1}]"
        )

    tile_h, tile_w = tile_shape
    logger.info(f"Tile dimensions: {tile_w}x{tile_h} px, {n_bands} bands")

    canvas_w, canvas_h, deg_per_px_h, deg_per_px_v, pan_min_edge, tilt_max_edge = (
        compute_canvas_geometry(geometry, positions, tile_shape)
    )

    canvas_sum = np.zeros((canvas_h, canvas_w), dtype=np.float64)
    canvas_weight = np.zeros((canvas_h, canvas_w), dtype=np.float64)

    overlap = geometry["overlap"]
    margin_h = int(tile_w * overlap / 2)
    margin_v = int(tile_h * overlap / 2)
    margin = min(margin_h, margin_v)
    weight_map = compute_weight_map(tile_h, tile_w, margin)

    logger.info(f"Blend margin: {margin} px (from {overlap:.0%} overlap)")

    for i, pos in enumerate(positions):
        pan_deg, tilt_deg = parallax_corrected_center(
            pos["target_position"]["pan_deg"],
            pos["target_position"]["tilt_deg"],
        )

        tile_pan_left = pan_deg - geometry["fov_h_deg"] / 2
        tile_tilt_top = tilt_deg + geometry["fov_v_deg"] / 2

        col_start = int(round((tile_pan_left - pan_min_edge) / deg_per_px_h))
        row_start = int(round((tilt_max_edge - tile_tilt_top) / deg_per_px_v))

        nc_path = _resolve_nc_path(pos, summary_dir)
        with nc.Dataset(nc_path, "r") as ds:
            tile_data = read_band_oriented(ds, band_index).astype(np.float64)

        r_end = min(row_start + tile_h, canvas_h)
        c_end = min(col_start + tile_w, canvas_w)
        r_start = max(row_start, 0)
        c_start = max(col_start, 0)

        tr_start = r_start - row_start
        tr_end = tr_start + (r_end - r_start)
        tc_start = c_start - col_start
        tc_end = tc_start + (c_end - c_start)

        tile_region = tile_data[tr_start:tr_end, tc_start:tc_end]
        weight_region = weight_map[tr_start:tr_end, tc_start:tc_end]

        canvas_sum[r_start:r_end, c_start:c_end] += tile_region * weight_region
        canvas_weight[r_start:r_end, c_start:c_end] += weight_region

        logger.info(
            f"  Placed {pos['position_id']} at canvas "
            f"({c_start}, {r_start})-({c_end}, {r_end}), "
            f"pan={pan_deg:.2f}, tilt={tilt_deg:.2f}"
        )

    mask = canvas_weight > 0
    mosaic = np.zeros((canvas_h, canvas_w), dtype=np.float64)
    mosaic[mask] = canvas_sum[mask] / canvas_weight[mask]

    mosaic = np.clip(mosaic, 0, 65535).astype(np.uint16)

    n_uncovered = np.sum(~mask)
    if n_uncovered > 0:
        total = canvas_h * canvas_w
        logger.warning(
            f"{n_uncovered}/{total} pixels ({n_uncovered / total:.1%}) "
            f"have no tile coverage"
        )

    return mosaic


# ---------------------------------------------------------------------------
# OpenCV column-first mosaic
# ---------------------------------------------------------------------------

def _load_tile_8bit(
    position: Dict, band_index: int, summary_dir: Path
) -> np.ndarray:
    """Load a tile as 8-bit image for OpenCV feature detection.

    Parameters
    ----------
    position : dict
        Position result dict.
    band_index : int
        Band array index.
    summary_dir : Path
        Directory containing the summary JSON.

    Returns
    -------
    np.ndarray
        8-bit single-channel image (rotated 180 degrees).
    """
    nc_path = _resolve_nc_path(position, summary_dir)
    with nc.Dataset(nc_path, "r") as ds:
        tile_16 = read_band_oriented(ds, band_index)

    p_low, p_high = np.percentile(tile_16, [1, 99])
    if p_high <= p_low:
        p_high = p_low + 1
    stretched = np.clip((tile_16.astype(np.float32) - p_low) /
                        (p_high - p_low) * 255, 0, 255).astype(np.uint8)
    return stretched


def _refine_offset_phase_correlation(
    tile_a: np.ndarray, tile_b: np.ndarray,
    expected_dx: int, expected_dy: int,
    overlap_px_h: int, overlap_px_v: int,
) -> Tuple[int, int]:
    """Refine the translational offset between two overlapping tiles using
    phase correlation on the expected overlap region.

    Parameters
    ----------
    tile_a : np.ndarray
        Reference tile (8-bit or 16-bit, single channel).
    tile_b : np.ndarray
        Neighboring tile.
    expected_dx : int
        Expected column offset of tile_b relative to tile_a.
    expected_dy : int
        Expected row offset of tile_b relative to tile_a.
    overlap_px_h : int
        Expected horizontal overlap in pixels.
    overlap_px_v : int
        Expected vertical overlap in pixels.

    Returns
    -------
    tuple of (int, int)
        Refined (dx, dy) offset of tile_b relative to tile_a.
    """
    h, w = tile_a.shape[:2]

    if expected_dx > 0 and overlap_px_h > 0:
        roi_a = tile_a[:, (w - overlap_px_h):].astype(np.float32)
        roi_b = tile_b[:, :overlap_px_h].astype(np.float32)
    elif expected_dx < 0 and overlap_px_h > 0:
        roi_a = tile_a[:, :overlap_px_h].astype(np.float32)
        roi_b = tile_b[:, (w - overlap_px_h):].astype(np.float32)
    elif expected_dy > 0 and overlap_px_v > 0:
        roi_a = tile_a[(h - overlap_px_v):, :].astype(np.float32)
        roi_b = tile_b[:overlap_px_v, :].astype(np.float32)
    elif expected_dy < 0 and overlap_px_v > 0:
        roi_a = tile_a[:overlap_px_v, :].astype(np.float32)
        roi_b = tile_b[(h - overlap_px_v):, :].astype(np.float32)
    else:
        return expected_dx, expected_dy

    min_h = min(roi_a.shape[0], roi_b.shape[0])
    min_w = min(roi_a.shape[1], roi_b.shape[1])
    roi_a = roi_a[:min_h, :min_w]
    roi_b = roi_b[:min_h, :min_w]

    f_a = np.fft.fft2(roi_a)
    f_b = np.fft.fft2(roi_b)
    cross_power = (f_a * np.conj(f_b))
    magnitude = np.abs(cross_power)
    magnitude[magnitude == 0] = 1
    cross_power /= magnitude
    correlation = np.abs(np.fft.ifft2(cross_power))

    peak = np.unravel_index(np.argmax(correlation), correlation.shape)
    shift_y, shift_x = peak

    if shift_y > min_h // 2:
        shift_y -= min_h
    if shift_x > min_w // 2:
        shift_x -= min_w

    refined_dx = expected_dx + shift_x
    refined_dy = expected_dy + shift_y

    return int(refined_dx), int(refined_dy)


def _stitch_column(
    tiles: List[np.ndarray],
    overlap_v_px: int,
) -> np.ndarray:
    """Stitch a vertical column of tiles using coordinate-based placement.

    Parameters
    ----------
    tiles : list of np.ndarray
        Tiles ordered top-to-bottom in image space (highest tilt first).
    overlap_v_px : int
        Vertical overlap in pixels between adjacent tiles.

    Returns
    -------
    tuple of (strip_sum, strip_weight, strip_h)
    """
    tile_h, tile_w = tiles[0].shape
    step_v = tile_h - overlap_v_px
    strip_h = tile_h + step_v * (len(tiles) - 1)

    strip_sum = np.zeros((strip_h, tile_w), dtype=np.float64)
    strip_weight = np.zeros((strip_h, tile_w), dtype=np.float64)

    margin = overlap_v_px // 2
    weight_map = compute_weight_map(tile_h, tile_w, margin)

    for i, tile in enumerate(tiles):
        r_start = i * step_v
        r_end = r_start + tile_h

        strip_sum[r_start:r_end, :] += tile * weight_map
        strip_weight[r_start:r_end, :] += weight_map

    return strip_sum, strip_weight, strip_h


def assemble_mosaic_opencv(
    geometry: Dict,
    positions: List[Dict],
    band_index: int,
    summary_dir: Path,
) -> np.ndarray:
    """Assemble a mosaic using column-first stitching with phase correlation.

    Two-pass approach exploiting the PTU mounting geometry:

    Pass 1 — Vertical columns (coordinate-based):
        Tiles sharing the same pan angle are stacked vertically using
        nominal coordinate offsets. The tilt axis passes through the lens
        nodal point, so there is no parallax between these tiles.

    Pass 2 — Horizontal combination (phase-correlation refined):
        The stitched column strips are joined left-to-right. Phase
        correlation on the overlap regions refines the horizontal offsets
        to compensate for pan-axis parallax.

    Parameters
    ----------
    geometry : dict
        Grid geometry from survey summary.
    positions : list of dict
        Successful position results.
    band_index : int
        Band array index (0-based).
    summary_dir : Path
        Directory containing the summary JSON.

    Returns
    -------
    np.ndarray
        Mosaic image as uint16 array.
    """
    fov_h = geometry["fov_h_deg"]
    fov_v = geometry["fov_v_deg"]

    first_nc_path = _resolve_nc_path(positions[0], summary_dir)
    with nc.Dataset(first_nc_path, "r") as ds:
        tile_shape = ds.variables["digital_number"].shape[1:]

    tile_h, tile_w = tile_shape

    pan_vals = sorted(set(p["target_position"]["pan_deg"] for p in positions))
    tilt_vals = sorted(set(p["target_position"]["tilt_deg"] for p in positions))
    pan_to_idx = {v: i for i, v in enumerate(pan_vals)}
    tilt_to_idx = {v: i for i, v in enumerate(tilt_vals)}

    grid = {}
    for pos in positions:
        pi = pan_to_idx[pos["target_position"]["pan_deg"]]
        ti = tilt_to_idx[pos["target_position"]["tilt_deg"]]
        grid[(pi, ti)] = pos

    logger.info(f"Grid: {len(pan_vals)} pan x {len(tilt_vals)} tilt positions")

    tiles_16bit = {}
    for pos in positions:
        pi = pan_to_idx[pos["target_position"]["pan_deg"]]
        ti = tilt_to_idx[pos["target_position"]["tilt_deg"]]
        nc_path = _resolve_nc_path(pos, summary_dir)
        with nc.Dataset(nc_path, "r") as ds:
            tiles_16bit[(pi, ti)] = read_band_oriented(
                ds, band_index).astype(np.float64)

    overlap_h_px = int(tile_w * geometry["overlap"])
    overlap_v_px = int(tile_h * geometry["overlap"])
    step_px_h = tile_w - overlap_h_px

    # Pass 1: Stitch each column vertically
    column_strips = {}
    for pi in range(len(pan_vals)):
        column_tiles = []
        for ti in reversed(range(len(tilt_vals))):
            if (pi, ti) not in tiles_16bit:
                logger.warning(
                    f"Missing tile at pan_idx={pi}, tilt_idx={ti}, "
                    f"inserting zeros"
                )
                column_tiles.append(np.zeros((tile_h, tile_w), dtype=np.float64))
            else:
                column_tiles.append(tiles_16bit[(pi, ti)])

        col_sum, col_weight, col_h = _stitch_column(
            column_tiles, overlap_v_px
        )
        column_strips[pi] = (col_sum, col_weight, col_h)
        logger.info(
            f"  Column {pi} (pan={pan_vals[pi]:.2f} deg): "
            f"{len(column_tiles)} tiles -> {tile_w}x{col_h} strip"
        )

    # Pass 2: Join columns horizontally with phase correlation
    finalized_strips = {}
    for pi, (col_sum, col_weight, col_h) in column_strips.items():
        mask = col_weight > 0
        strip = np.zeros_like(col_sum)
        strip[mask] = col_sum[mask] / col_weight[mask]
        finalized_strips[pi] = strip

    strip_h = finalized_strips[0].shape[0]

    col_offsets = {0: (0, 0)}
    n_refined = 0

    for pi in range(1, len(pan_vals)):
        prev_strip = finalized_strips[pi - 1]
        curr_strip = finalized_strips[pi]

        dx, dy = _refine_offset_phase_correlation(
            prev_strip, curr_strip,
            expected_dx=step_px_h, expected_dy=0,
            overlap_px_h=overlap_h_px, overlap_px_v=0,
        )

        prev_off = col_offsets[pi - 1]
        col_offsets[pi] = (prev_off[0] + dx, prev_off[1] + dy)
        n_refined += 1
        logger.info(
            f"  Column {pi-1}->{pi}: dx={dx}, dy={dy}, "
            f"correction=({dx - step_px_h}, {dy})"
        )

    logger.info(f"Horizontal refinement: {n_refined} pairs")

    min_col = min(o[0] for o in col_offsets.values())
    min_row = min(o[1] for o in col_offsets.values())
    col_offsets = {
        k: (v[0] - min_col, v[1] - min_row) for k, v in col_offsets.items()
    }

    canvas_w = max(o[0] + tile_w for o in col_offsets.values())
    canvas_h = max(o[1] + strip_h for o in col_offsets.values())

    logger.info(f"Canvas: {canvas_w}x{canvas_h} px")

    canvas_sum = np.zeros((canvas_h, canvas_w), dtype=np.float64)
    canvas_weight = np.zeros((canvas_h, canvas_w), dtype=np.float64)

    h_margin = overlap_h_px // 2
    strip_weight_map = compute_weight_map(strip_h, tile_w, h_margin)

    for pi in range(len(pan_vals)):
        col_off, row_off = col_offsets[pi]
        col_sum, col_wt, col_h = column_strips[pi]

        r_end = min(row_off + strip_h, canvas_h)
        c_end = min(col_off + tile_w, canvas_w)
        th = r_end - row_off
        tw = c_end - col_off

        h_weight = strip_weight_map[:th, :tw]

        canvas_sum[row_off:r_end, col_off:c_end] += (
            col_sum[:th, :tw] * h_weight
        )
        canvas_weight[row_off:r_end, col_off:c_end] += (
            col_wt[:th, :tw] * h_weight
        )

    mask = canvas_weight > 0
    mosaic = np.zeros((canvas_h, canvas_w), dtype=np.float64)
    mosaic[mask] = canvas_sum[mask] / canvas_weight[mask]
    mosaic = np.clip(mosaic, 0, 65535).astype(np.uint16)

    return mosaic


# ---------------------------------------------------------------------------
# Cylindrical reprojection helpers
# ---------------------------------------------------------------------------

def _get_effective_focal_length(nc_path: Path) -> float:
    """Get the effective focal length for the lens used in a capture.

    Parameters
    ----------
    nc_path : Path
        Path to any NetCDF file from the survey.

    Returns
    -------
    float
        Effective focal length in mm.
    """
    spec_path = Path(__file__).resolve().parents[2] / "config" / "lens_specifications.json"
    with nc.Dataset(nc_path, "r") as ds:
        nominal_fl = float(ds.getncattr("focal_length_mm"))

    if spec_path.exists():
        with open(spec_path) as f:
            specs = json.load(f)
        for lens_id, lens_info in specs["lenses"].items():
            if lens_info["focal_length_mm"] == nominal_fl:
                f_eff = lens_info["f_eff_mm"]
                logger.info(
                    f"Lens: {lens_info['model']}, "
                    f"f_eff={f_eff} mm (nominal {nominal_fl} mm)"
                )
                return f_eff

    logger.warning(
        f"Could not find lens spec for {nominal_fl}mm, "
        f"using nominal focal length"
    )
    return nominal_fl


def _rotation_matrix_pan_tilt(pan_rad: float, tilt_rad: float) -> np.ndarray:
    """Compute the rotation matrix for a PTU pan/tilt pointing.

    Convention:
    - Camera looks along +Z in its local frame
    - Pan rotates about Y (positive pan = rotate right in world)
    - Tilt rotates about X (positive tilt = rotate up in world)

    Parameters
    ----------
    pan_rad : float
        Pan angle in radians.
    tilt_rad : float
        Tilt angle in radians.

    Returns
    -------
    np.ndarray
        3x3 rotation matrix (world-to-camera).
    """
    cp, sp = np.cos(pan_rad), np.sin(pan_rad)
    ct, st = np.cos(tilt_rad), np.sin(tilt_rad)

    r_pan = np.array([
        [ cp, 0, sp],
        [  0, 1,  0],
        [-sp, 0, cp],
    ])

    r_tilt = np.array([
        [1,   0,  0],
        [0,  ct, st],
        [0, -st, ct],
    ])

    return (r_pan @ r_tilt).T


def _rotation_matrix_pan_tilt_roll(
    pan_rad: float, tilt_rad: float, roll_rad: float = 0.0
) -> np.ndarray:
    """Compute rotation matrix with pan, tilt, and sensor roll.

    Parameters
    ----------
    pan_rad : float
        Pan angle in radians.
    tilt_rad : float
        Tilt angle in radians.
    roll_rad : float
        Roll angle in radians (rotation about optical axis).

    Returns
    -------
    np.ndarray
        3x3 rotation matrix (world-to-camera).
    """
    R_base = _rotation_matrix_pan_tilt(pan_rad, tilt_rad)
    if roll_rad == 0.0:
        return R_base
    cr, sr = np.cos(roll_rad), np.sin(roll_rad)
    R_roll = np.array([
        [cr, -sr, 0],
        [sr,  cr, 0],
        [ 0,   0, 1],
    ])
    return R_roll @ R_base


def _reproject_tile(
    tile_data: np.ndarray,
    pan_rad: float,
    tilt_rad: float,
    f_px: float,
    cx: float,
    cy: float,
    tile_w: int,
    tile_h: int,
    cos_el: np.ndarray,
    sin_el: np.ndarray,
    cos_az: np.ndarray,
    sin_az: np.ndarray,
    roll_rad: float = 0.0,
    k1: float = 0.0,
) -> Tuple[np.ndarray, np.ndarray]:
    """Reproject a single tile from gnomonic to cylindrical coordinates.

    Performs the inverse mapping from cylindrical (az, el) output pixels
    to gnomonic (u, v) source pixels using the pinhole camera model with
    optional radial distortion.

    Parameters
    ----------
    tile_data : np.ndarray
        Source tile image (already rotated 180 deg).
    pan_rad, tilt_rad : float
        Tile pointing angles in radians.
    f_px : float
        Focal length in pixels.
    cx, cy : float
        Principal point in pixel coordinates.
    tile_w, tile_h : int
        Source tile dimensions.
    cos_el, sin_el : np.ndarray
        Precomputed cos/sin of elevation for the output rows.
    cos_az, sin_az : np.ndarray
        Precomputed cos/sin of azimuth for the output columns.
    roll_rad : float
        Sensor roll angle in radians.
    k1 : float
        First-order radial distortion coefficient (Brown-Conrady).

    Returns
    -------
    sampled : np.ndarray
        Reprojected pixel values.
    valid : np.ndarray
        Boolean mask of valid (in-bounds) pixels.
    """
    rh = len(cos_el)
    rw = len(cos_az)

    R = _rotation_matrix_pan_tilt_roll(pan_rad, tilt_rad, roll_rad)

    world_x = cos_el[:, np.newaxis] * sin_az[np.newaxis, :]
    world_y = sin_el[:, np.newaxis] * np.ones(rw)[np.newaxis, :]
    world_z = cos_el[:, np.newaxis] * cos_az[np.newaxis, :]

    cam_x = R[0, 0] * world_x + R[0, 1] * world_y + R[0, 2] * world_z
    cam_y = R[1, 0] * world_x + R[1, 1] * world_y + R[1, 2] * world_z
    cam_z = R[2, 0] * world_x + R[2, 1] * world_y + R[2, 2] * world_z

    valid = cam_z > 0
    cam_y = -cam_y

    with np.errstate(divide='ignore', invalid='ignore'):
        u = f_px * (cam_x / cam_z) + cx
        v = f_px * (cam_y / cam_z) + cy

    if k1 != 0.0:
        dx = u - cx
        dy = v - cy
        r2 = dx * dx + dy * dy
        radial = 1.0 + k1 * r2
        u = cx + dx * radial
        v = cy + dy * radial

    valid &= (u >= 0) & (u <= tile_w - 1)
    valid &= (v >= 0) & (v <= tile_h - 1)

    u_flat = np.where(valid, u, 0).ravel()
    v_flat = np.where(valid, v, 0).ravel()

    sampled = map_coordinates(
        tile_data, [v_flat, u_flat], order=1, mode='constant', cval=0
    ).reshape(rh, rw).astype(np.float32)
    sampled[~valid] = np.nan

    return sampled, valid


# ---------------------------------------------------------------------------
# Overlap pair identification
# ---------------------------------------------------------------------------

def _identify_overlap_pairs(
    positions: List[Dict],
    pan_to_idx: Dict[float, int],
    tilt_to_idx: Dict[float, int],
    tile_half_fov_h: float,
    tile_half_fov_v: float,
) -> List[Tuple[Tuple[int, int], Tuple[int, int], Tuple[float, float], Tuple[float, float]]]:
    """Identify all pairs of adjacent tiles with angular overlap.

    Parameters
    ----------
    positions : list of dict
        Successful position results.
    pan_to_idx, tilt_to_idx : dict
        Mappings from angle value to grid index.
    tile_half_fov_h, tile_half_fov_v : float
        Half field-of-view in degrees (horizontal and vertical).

    Returns
    -------
    list of (key_a, key_b, az_range, el_range)
    """
    key_to_angles = {}
    for pos in positions:
        pan_deg = pos["target_position"]["pan_deg"]
        tilt_deg = pos["target_position"]["tilt_deg"]
        pi = pan_to_idx[pan_deg]
        ti = tilt_to_idx[tilt_deg]
        key_to_angles[(pi, ti)] = (pan_deg, tilt_deg)

    pairs = []
    for (pi, ti), (pan_a, tilt_a) in key_to_angles.items():
        # Horizontal neighbor
        key_b = (pi + 1, ti)
        if key_b in key_to_angles:
            pan_b, tilt_b = key_to_angles[key_b]
            az_lo = max(pan_a - tile_half_fov_h, pan_b - tile_half_fov_h)
            az_hi = min(pan_a + tile_half_fov_h, pan_b + tile_half_fov_h)
            el_lo = max(tilt_a - tile_half_fov_v, tilt_b - tile_half_fov_v)
            el_hi = min(tilt_a + tile_half_fov_v, tilt_b + tile_half_fov_v)
            if az_hi > az_lo and el_hi > el_lo:
                pairs.append(((pi, ti), key_b, (az_lo, az_hi), (el_lo, el_hi)))

        # Vertical neighbor
        key_b = (pi, ti + 1)
        if key_b in key_to_angles:
            pan_b, tilt_b = key_to_angles[key_b]
            az_lo = max(pan_a - tile_half_fov_h, pan_b - tile_half_fov_h)
            az_hi = min(pan_a + tile_half_fov_h, pan_b + tile_half_fov_h)
            el_lo = max(tilt_a - tile_half_fov_v, tilt_b - tile_half_fov_v)
            el_hi = min(tilt_a + tile_half_fov_v, tilt_b + tile_half_fov_v)
            if az_hi > az_lo and el_hi > el_lo:
                pairs.append(((pi, ti), key_b, (az_lo, az_hi), (el_lo, el_hi)))

    return pairs


# ---------------------------------------------------------------------------
# Projection optimization (camera intrinsics + per-tile corrections)
# ---------------------------------------------------------------------------

def _compute_overlap_cost(
    params: np.ndarray,
    stage: int,
    overlap_pairs: list,
    tiles: Dict[Tuple[int, int], Tuple[np.ndarray, float, float]],
    f_eff_mm_init: float,
    pixel_size_mm: float,
    cx_base: float,
    cy_base: float,
    tile_w: int,
    tile_h: int,
    subsample: int = 4,
) -> float:
    """Compute total SSD across all overlap regions for given parameters.

    Parameters
    ----------
    params : np.ndarray
        Parameter vector. For stage 1: [f_eff_mm, cx_offset, cy_offset,
        roll_rad, k1]. For stage 2: [delta_pan_0, delta_tilt_0, ...] for
        tiles 1..N-1.
    stage : int
        1 for global parameter optimization, 2 for per-tile corrections.
    overlap_pairs : list
        Output from ``_identify_overlap_pairs``.
    tiles : dict
        Mapping from (pi, ti) -> (tile_data, pan_deg, tilt_deg).
    f_eff_mm_init : float
        Initial effective focal length (used as-is in stage 2).
    pixel_size_mm : float
        Pixel size in mm.
    cx_base, cy_base : float
        Image center coordinates.
    tile_w, tile_h : int
        Tile dimensions.
    subsample : int
        Subsampling factor for speed.

    Returns
    -------
    float
        Total SSD cost.
    """
    if stage == 1:
        f_eff_mm = params[0]
        cx_off = params[1]
        cy_off = params[2]
        roll_rad = params[3]
        k1 = params[4]
        tile_keys_sorted = sorted(tiles.keys())
        deltas = {k: (0.0, 0.0) for k in tile_keys_sorted}
    else:
        f_eff_mm = f_eff_mm_init
        cx_off = 0.0
        cy_off = 0.0
        roll_rad = 0.0
        k1 = 0.0
        tile_keys_sorted = sorted(tiles.keys())
        deltas = {tile_keys_sorted[0]: (0.0, 0.0)}
        for i, key in enumerate(tile_keys_sorted[1:]):
            deltas[key] = (params[2 * i], params[2 * i + 1])

    f_px = f_eff_mm / pixel_size_mm
    cx = cx_base + cx_off
    cy = cy_base + cy_off

    total_ssd = 0.0
    total_count = 0

    for key_a, key_b, (az_lo, az_hi), (el_lo, el_hi) in overlap_pairs:
        if key_a not in tiles or key_b not in tiles:
            continue

        tile_a, pan_a, tilt_a = tiles[key_a]
        tile_b, pan_b, tilt_b = tiles[key_b]

        dp_a, dt_a = deltas[key_a]
        dp_b, dt_b = deltas[key_b]

        deg_per_px = np.degrees(pixel_size_mm / f_eff_mm)
        n_az = max(1, int((az_hi - az_lo) / deg_per_px / subsample))
        n_el = max(1, int((el_hi - el_lo) / deg_per_px / subsample))

        az_pts = np.linspace(np.radians(az_lo), np.radians(az_hi), n_az)
        el_pts = np.linspace(np.radians(el_lo), np.radians(el_hi), n_el)

        cos_el = np.cos(el_pts)
        sin_el = np.sin(el_pts)
        cos_az = np.cos(az_pts)
        sin_az = np.sin(az_pts)

        samp_a, valid_a = _reproject_tile(
            tile_a,
            np.radians(pan_a) + dp_a,
            np.radians(tilt_a) + dt_a,
            f_px, cx, cy, tile_w, tile_h,
            cos_el, sin_el, cos_az, sin_az,
            roll_rad=roll_rad, k1=k1,
        )
        samp_b, valid_b = _reproject_tile(
            tile_b,
            np.radians(pan_b) + dp_b,
            np.radians(tilt_b) + dt_b,
            f_px, cx, cy, tile_w, tile_h,
            cos_el, sin_el, cos_az, sin_az,
            roll_rad=roll_rad, k1=k1,
        )

        both_valid = valid_a & valid_b
        n_valid = np.sum(both_valid)
        if n_valid > 0:
            diff = samp_a[both_valid] - samp_b[both_valid]
            total_ssd += np.sum(diff ** 2)
            total_count += n_valid

    if total_count > 0:
        return total_ssd / total_count
    return 1e12


def _optimize_projection_params(
    tiles: Dict[Tuple[int, int], Tuple[np.ndarray, float, float]],
    overlap_pairs: list,
    f_eff_mm_init: float,
    pixel_size_mm: float,
    cx_base: float,
    cy_base: float,
    tile_w: int,
    tile_h: int,
    subsample: int = 4,
) -> Tuple[float, float, float, float, float, Dict[Tuple[int, int], Tuple[float, float]]]:
    """Two-stage optimization of projection parameters.

    Stage 1 optimizes 5 global parameters: focal length, principal point
    offsets, sensor roll, and radial distortion k1. Stage 2 optimizes
    per-tile pointing corrections.

    Parameters
    ----------
    tiles : dict
        Mapping from (pi, ti) -> (tile_data, pan_deg, tilt_deg).
    overlap_pairs : list
        Output from ``_identify_overlap_pairs``.
    f_eff_mm_init : float
        Nominal effective focal length.
    pixel_size_mm : float
        Pixel size in mm.
    cx_base, cy_base : float
        Nominal image center coordinates.
    tile_w, tile_h : int
        Tile dimensions.
    subsample : int
        Subsampling factor for cost evaluation.

    Returns
    -------
    f_eff_mm_opt : float
        Optimized focal length.
    cx_off_opt, cy_off_opt : float
        Optimized principal point offsets.
    roll_opt : float
        Optimized roll in radians.
    k1_opt : float
        Optimized first-order radial distortion coefficient.
    deltas : dict
        Per-tile (delta_pan_rad, delta_tilt_rad) corrections.
    """
    from scipy.optimize import minimize

    n_evals = [0]

    # --- Stage 1: Global parameters ---
    logger.info("  Stage 1: Optimizing global parameters "
                "(f_eff, cx_off, cy_off, roll, k1)...")

    x0_global = np.array([f_eff_mm_init, 0.0, 0.0, 0.0, 0.0])

    cost_init = _compute_overlap_cost(
        x0_global, 1, overlap_pairs, tiles,
        f_eff_mm_init, pixel_size_mm, cx_base, cy_base, tile_w, tile_h,
        subsample=subsample,
    )
    logger.info(f"    Initial cost (mean SSD): {cost_init:.2f}")

    def stage1_cost(params):
        n_evals[0] += 1
        return _compute_overlap_cost(
            params, 1, overlap_pairs, tiles,
            f_eff_mm_init, pixel_size_mm, cx_base, cy_base, tile_w, tile_h,
            subsample=subsample,
        )

    bounds_global = [
        (f_eff_mm_init - 2.0, f_eff_mm_init + 2.0),  # f_eff_mm
        (-50.0, 50.0),                                 # cx_offset
        (-50.0, 50.0),                                 # cy_offset
        (np.radians(-3.0), np.radians(3.0)),           # roll_rad
        (-1e-6, 1e-6),                                 # k1
    ]

    result1 = minimize(
        stage1_cost,
        x0_global,
        method='L-BFGS-B',
        bounds=bounds_global,
        options={'maxiter': 300, 'ftol': 1e-4},
    )

    f_eff_opt = result1.x[0]
    cx_off_opt = result1.x[1]
    cy_off_opt = result1.x[2]
    roll_opt = result1.x[3]
    k1_opt = result1.x[4]

    logger.info(
        f"    Stage 1 converged in {n_evals[0]} evaluations: "
        f"f_eff={f_eff_opt:.4f}mm, cx_off={cx_off_opt:.2f}px, "
        f"cy_off={cy_off_opt:.2f}px, roll={np.degrees(roll_opt):.4f}\u00b0, "
        f"k1={k1_opt:.2e}, cost={result1.fun:.2f}"
    )

    # --- Stage 2: Per-tile pointing corrections ---
    tile_keys_sorted = sorted(tiles.keys())
    n_tiles = len(tile_keys_sorted)

    if n_tiles <= 1:
        deltas = {tile_keys_sorted[0]: (0.0, 0.0)}
        return f_eff_opt, cx_off_opt, cy_off_opt, roll_opt, k1_opt, deltas

    logger.info(f"  Stage 2: Optimizing per-tile corrections "
                f"({(n_tiles - 1) * 2} parameters)...")

    def stage2_cost(params):
        f_px = f_eff_opt / pixel_size_mm
        cx_s2 = cx_base + cx_off_opt
        cy_s2 = cy_base + cy_off_opt

        delta_dict = {tile_keys_sorted[0]: (0.0, 0.0)}
        for i, key in enumerate(tile_keys_sorted[1:]):
            delta_dict[key] = (params[2 * i], params[2 * i + 1])

        total_ssd = 0.0
        total_count = 0

        for key_a, key_b, (az_lo, az_hi), (el_lo, el_hi) in overlap_pairs:
            if key_a not in tiles or key_b not in tiles:
                continue

            tile_a, pan_a, tilt_a = tiles[key_a]
            tile_b, pan_b, tilt_b = tiles[key_b]

            dp_a, dt_a = delta_dict.get(key_a, (0.0, 0.0))
            dp_b, dt_b = delta_dict.get(key_b, (0.0, 0.0))

            deg_per_px = np.degrees(pixel_size_mm / f_eff_opt)
            n_az = max(1, int((az_hi - az_lo) / deg_per_px / subsample))
            n_el = max(1, int((el_hi - el_lo) / deg_per_px / subsample))

            az_pts = np.linspace(np.radians(az_lo), np.radians(az_hi), n_az)
            el_pts = np.linspace(np.radians(el_lo), np.radians(el_hi), n_el)

            cos_el = np.cos(el_pts)
            sin_el = np.sin(el_pts)
            cos_az = np.cos(az_pts)
            sin_az = np.sin(az_pts)

            samp_a, valid_a = _reproject_tile(
                tile_a,
                np.radians(pan_a) + dp_a,
                np.radians(tilt_a) + dt_a,
                f_px, cx_s2, cy_s2, tile_w, tile_h,
                cos_el, sin_el, cos_az, sin_az,
                roll_rad=roll_opt, k1=k1_opt,
            )
            samp_b, valid_b = _reproject_tile(
                tile_b,
                np.radians(pan_b) + dp_b,
                np.radians(tilt_b) + dt_b,
                f_px, cx_s2, cy_s2, tile_w, tile_h,
                cos_el, sin_el, cos_az, sin_az,
                roll_rad=roll_opt, k1=k1_opt,
            )

            both_valid = valid_a & valid_b
            n_valid = np.sum(both_valid)
            if n_valid > 0:
                diff = samp_a[both_valid] - samp_b[both_valid]
                total_ssd += np.sum(diff ** 2)
                total_count += n_valid

        if total_count > 0:
            return total_ssd / total_count
        return 1e12

    x0_tiles = np.zeros((n_tiles - 1) * 2)
    n_evals[0] = 0

    max_delta_rad = np.radians(0.5)
    bounds_tiles = [(-max_delta_rad, max_delta_rad)] * len(x0_tiles)

    result2 = minimize(
        stage2_cost,
        x0_tiles,
        method='L-BFGS-B',
        bounds=bounds_tiles,
        options={'maxiter': 500, 'ftol': 1e-4},
    )

    deltas = {tile_keys_sorted[0]: (0.0, 0.0)}
    for i, key in enumerate(tile_keys_sorted[1:]):
        deltas[key] = (result2.x[2 * i], result2.x[2 * i + 1])

    logger.info(f"    Stage 2 cost: {result2.fun:.2f}")
    for key in tile_keys_sorted:
        dp, dt = deltas[key]
        if dp != 0.0 or dt != 0.0:
            logger.info(
                f"    Tile {key}: delta_pan={np.degrees(dp):.4f}\u00b0, "
                f"delta_tilt={np.degrees(dt):.4f}\u00b0"
            )

    return f_eff_opt, cx_off_opt, cy_off_opt, roll_opt, k1_opt, deltas


# ---------------------------------------------------------------------------
# Tone correction
# ---------------------------------------------------------------------------

def _compute_tone_corrections(
    reprojected: Dict[
        Tuple[int, int],
        Tuple[np.ndarray, np.ndarray, int, int, float, float]
    ],
    overlap_pairs: list,
) -> Dict[Tuple[int, int], float]:
    """Compute per-tile gain corrections for tone balancing.

    Uses the overlap regions between adjacent tiles to solve a global
    least-squares system for multiplicative gain corrections that
    minimize brightness discontinuities at tile boundaries.

    Parameters
    ----------
    reprojected : dict
        Reprojected tile data, keyed by (pi, ti). Each value is
        (sampled, valid, row_min, col_min, pan_deg, tilt_deg).
    overlap_pairs : list
        Output from ``_identify_overlap_pairs``.

    Returns
    -------
    dict
        Mapping from (pi, ti) -> gain correction factor.
    """
    all_keys = sorted(reprojected.keys())
    key_to_idx = {k: i for i, k in enumerate(all_keys)}
    n_tiles = len(all_keys)

    if n_tiles <= 1:
        return {all_keys[0]: 1.0}

    rows = []
    rhs = []

    for key_a, key_b, _, _ in overlap_pairs:
        if key_a not in reprojected or key_b not in reprojected:
            continue

        img_a, valid_a, row_a, col_a, _, _ = reprojected[key_a]
        img_b, valid_b, row_b, col_b, _, _ = reprojected[key_b]

        ovl_row_start = max(row_a, row_b)
        ovl_row_end = min(
            row_a + img_a.shape[0], row_b + img_b.shape[0]
        )
        ovl_col_start = max(col_a, col_b)
        ovl_col_end = min(
            col_a + img_a.shape[1], col_b + img_b.shape[1]
        )

        if ovl_row_end <= ovl_row_start or ovl_col_end <= ovl_col_start:
            continue

        sa = img_a[
            (ovl_row_start - row_a):(ovl_row_end - row_a),
            (ovl_col_start - col_a):(ovl_col_end - col_a),
        ]
        va = valid_a[
            (ovl_row_start - row_a):(ovl_row_end - row_a),
            (ovl_col_start - col_a):(ovl_col_end - col_a),
        ]
        sb = img_b[
            (ovl_row_start - row_b):(ovl_row_end - row_b),
            (ovl_col_start - col_b):(ovl_col_end - col_b),
        ]
        vb = valid_b[
            (ovl_row_start - row_b):(ovl_row_end - row_b),
            (ovl_col_start - col_b):(ovl_col_end - col_b),
        ]

        both = va & vb
        n_both = np.sum(both)
        if n_both < 500:
            continue

        mean_a = np.mean(sa[both])
        mean_b = np.mean(sb[both])
        if mean_a <= 0 or mean_b <= 0:
            continue

        idx_a = key_to_idx[key_a]
        idx_b = key_to_idx[key_b]
        weight = np.sqrt(n_both)

        row = np.zeros(n_tiles)
        row[idx_a] = weight
        row[idx_b] = -weight
        rows.append(row)
        rhs.append(weight * np.log(mean_b / mean_a))

    anchor = np.zeros(n_tiles)
    anchor[key_to_idx[all_keys[0]]] = 1000.0
    rows.append(anchor)
    rhs.append(0.0)

    A = np.array(rows)
    b = np.array(rhs)
    log_gains, _, _, _ = np.linalg.lstsq(A, b, rcond=None)

    gains = np.exp(log_gains)
    return {key: gains[key_to_idx[key]] for key in all_keys}


def _apply_tone_corrections(
    reprojected: Dict[
        Tuple[int, int],
        Tuple[np.ndarray, np.ndarray, int, int, float, float]
    ],
    gains: Dict[Tuple[int, int], float],
) -> None:
    """Apply gain corrections to reprojected tiles in-place.

    Parameters
    ----------
    reprojected : dict
        Reprojected tile data (modified in-place).
    gains : dict
        Per-tile gain factors from ``_compute_tone_corrections``.
    """
    for key in list(reprojected.keys()):
        if key not in gains:
            continue
        g = gains[key]
        if abs(g - 1.0) < 1e-6:
            continue
        sampled, valid, row_min, col_min, epan, etilt = reprojected[key]
        reprojected[key] = (sampled * g, valid, row_min, col_min, epan, etilt)


# ---------------------------------------------------------------------------
# SIFT pairwise matching and global affine solve
# ---------------------------------------------------------------------------

def _to_8bit(img: np.ndarray) -> np.ndarray:
    """Stretch a float image to 8-bit for feature detection."""
    finite = img[np.isfinite(img)]
    if finite.size == 0:
        return np.zeros(img.shape, dtype=np.uint8)
    lo, hi = np.percentile(finite, [1, 99])
    stretched = np.where(
        np.isfinite(img),
        (img - lo) / max(1.0, hi - lo) * 255,
        0,
    )
    return np.clip(stretched, 0, 255).astype(np.uint8)


def _compute_pairwise_matches(
    reprojected: Dict[Tuple[int, int], Tuple],
) -> List[Tuple[Tuple[int, int], Tuple[int, int], np.ndarray, np.ndarray, int]]:
    """SIFT match all neighboring tile pairs, returning inlier points in
    each tile's local coordinate system.

    Parameters
    ----------
    reprojected : dict
        Mapping (pi, ti) -> (sampled, valid, row_min, col_min, eff_pan,
        eff_tilt) from reprojection.

    Returns
    -------
    list of (key_a, key_b, pts_a_local, pts_b_local, n_good)
    """
    import cv2

    sift = cv2.SIFT_create(nfeatures=10000)
    bf = cv2.BFMatcher(cv2.NORM_L2, crossCheck=False)

    neighbor_pairs = []
    for (pi, ti) in reprojected:
        if (pi + 1, ti) in reprojected:
            neighbor_pairs.append(((pi, ti), (pi + 1, ti)))
        if (pi, ti + 1) in reprojected:
            neighbor_pairs.append(((pi, ti), (pi, ti + 1)))

    results = []
    for key_a, key_b in neighbor_pairs:
        sam_a, val_a, rmin_a, cmin_a, _, _ = reprojected[key_a]
        sam_b, val_b, rmin_b, cmin_b, _, _ = reprojected[key_b]
        rh_a, rw_a = sam_a.shape
        rh_b, rw_b = sam_b.shape

        ovlp_r0 = max(rmin_a, rmin_b)
        ovlp_r1 = min(rmin_a + rh_a, rmin_b + rh_b)
        ovlp_c0 = max(cmin_a, cmin_b)
        ovlp_c1 = min(cmin_a + rw_a, cmin_b + rw_b)
        if ovlp_r1 <= ovlp_r0 or ovlp_c1 <= ovlp_c0:
            continue

        la_r0, la_c0 = ovlp_r0 - rmin_a, ovlp_c0 - cmin_a
        la_r1, la_c1 = ovlp_r1 - rmin_a, ovlp_c1 - cmin_a
        lb_r0, lb_c0 = ovlp_r0 - rmin_b, ovlp_c0 - cmin_b
        lb_r1, lb_c1 = ovlp_r1 - rmin_b, ovlp_c1 - cmin_b

        region_a = sam_a[la_r0:la_r1, la_c0:la_c1]
        valid_a = val_a[la_r0:la_r1, la_c0:la_c1]
        region_b = sam_b[lb_r0:lb_r1, lb_c0:lb_c1]
        valid_b = val_b[lb_r0:lb_r1, lb_c0:lb_c1]

        overlap_mask = valid_a & valid_b
        if np.sum(overlap_mask) < 1000:
            continue

        img_a = _to_8bit(region_a)
        img_b = _to_8bit(region_b)
        mask_u8 = overlap_mask.astype(np.uint8) * 255

        kp_a, des_a = sift.detectAndCompute(img_a, mask_u8)
        kp_b, des_b = sift.detectAndCompute(img_b, mask_u8)
        if des_a is None or des_b is None or \
                len(kp_a) < 10 or len(kp_b) < 10:
            continue

        matches = bf.knnMatch(des_a, des_b, k=2)
        good = [m for m, n in matches if m.distance < 0.75 * n.distance]
        if len(good) < 10:
            continue

        pts_a_ovlp = np.float32(
            [kp_a[m.queryIdx].pt for m in good]
        ).reshape(-1, 2)
        pts_b_ovlp = np.float32(
            [kp_b[m.trainIdx].pt for m in good]
        ).reshape(-1, 2)

        _, mask = cv2.estimateAffine2D(
            pts_a_ovlp.reshape(-1, 1, 2),
            pts_b_ovlp.reshape(-1, 1, 2),
            method=cv2.RANSAC,
            ransacReprojThreshold=5.0,
        )
        if mask is None:
            continue
        inlier_mask = mask.ravel().astype(bool)
        n_inliers = int(np.sum(inlier_mask))
        if n_inliers < 5:
            continue

        pts_a_local = pts_a_ovlp[inlier_mask].copy()
        pts_a_local[:, 0] += la_c0
        pts_a_local[:, 1] += la_r0

        pts_b_local = pts_b_ovlp[inlier_mask].copy()
        pts_b_local[:, 0] += lb_c0
        pts_b_local[:, 1] += lb_r0

        direction = "H" if key_b[0] != key_a[0] else "V"
        logger.info(
            f"  Pair {key_a}-{key_b} ({direction}): {len(good)} matches, "
            f"{n_inliers} inliers"
        )

        results.append((key_a, key_b, pts_a_local, pts_b_local, len(good)))

    return results


def _solve_global_affines(
    pair_matches: List[Tuple[Tuple[int, int], Tuple[int, int],
                             np.ndarray, np.ndarray, int]],
    reprojected: Dict[Tuple[int, int], Tuple],
    anchor_key: Tuple[int, int],
) -> Dict[Tuple[int, int], np.ndarray]:
    """Solve for per-tile full affine transforms that minimize the sum of
    squared distances between matched points across all neighbor pairs
    simultaneously, with Huber robust loss and identity regularization.

    Parameters
    ----------
    pair_matches : list
        Output of _compute_pairwise_matches.
    reprojected : dict
        Reprojected tile data (for col_min, row_min).
    anchor_key : tuple
        (pi, ti) of the anchor tile.

    Returns
    -------
    dict
        Mapping (pi, ti) -> 2x3 np.ndarray affine matrix.
    """
    from scipy.optimize import least_squares

    all_keys = sorted(reprojected.keys())
    free_keys = [k for k in all_keys if k != anchor_key]
    key_to_idx = {k: i for i, k in enumerate(free_keys)}
    n_free = len(free_keys)
    dof_per_tile = 6

    _, _, anc_rmin, anc_cmin, _, _ = reprojected[anchor_key]

    x0 = np.zeros(dof_per_tile * n_free, dtype=np.float64)
    for k in free_keys:
        i = key_to_idx[k]
        _, _, rmin, cmin, _, _ = reprojected[k]
        base = dof_per_tile * i
        x0[base + 0] = 1.0   # a
        x0[base + 1] = 0.0   # b
        x0[base + 2] = 0.0   # c
        x0[base + 3] = 1.0   # d
        x0[base + 4] = float(cmin)  # tx
        x0[base + 5] = float(rmin)  # ty

    def get_params(x, key):
        if key == anchor_key:
            return 1.0, 0.0, 0.0, 1.0, float(anc_cmin), float(anc_rmin)
        i = key_to_idx[key]
        base = dof_per_tile * i
        return (x[base], x[base + 1], x[base + 2],
                x[base + 3], x[base + 4], x[base + 5])

    inlier_counts = [n for _, _, _, _, n in pair_matches]
    n_data_residuals = sum(2 * len(pts_a) for _, _, pts_a, _, _ in pair_matches)
    n_reg_residuals = 4 * n_free
    # Scale regularization so its total energy is comparable to data energy.
    # Without this, the few regularization terms are overwhelmed by thousands
    # of data residuals, allowing degenerate affines (e.g. vertical collapse).
    lambda_base = float(np.sqrt(np.median(inlier_counts)))
    lambda_reg = lambda_base * float(np.sqrt(n_data_residuals / n_reg_residuals))

    def residuals(x):
        res = []
        for key_a, key_b, pts_a, pts_b, _ in pair_matches:
            a_a, b_a, c_a, d_a, tx_a, ty_a = get_params(x, key_a)
            a_b, b_b, c_b, d_b, tx_b, ty_b = get_params(x, key_b)

            col_a, row_a = pts_a[:, 0], pts_a[:, 1]
            col_b, row_b = pts_b[:, 0], pts_b[:, 1]

            res.append(
                a_a * col_a + b_a * row_a + tx_a
                - (a_b * col_b + b_b * row_b + tx_b)
            )
            res.append(
                c_a * col_a + d_a * row_a + ty_a
                - (c_b * col_b + d_b * row_b + ty_b)
            )

        reg = np.zeros(4 * n_free, dtype=np.float64)
        for k in free_keys:
            i = key_to_idx[k]
            a, b, c, d, _, _ = get_params(x, k)
            reg[4 * i + 0] = lambda_reg * (a - 1.0)
            reg[4 * i + 1] = lambda_reg * b
            reg[4 * i + 2] = lambda_reg * c
            reg[4 * i + 3] = lambda_reg * (d - 1.0)
        res.append(reg)

        return np.concatenate(res)

    logger.info(
        f"  Global affine solve: {n_free} tiles x {dof_per_tile} DOF = "
        f"{dof_per_tile * n_free} unknowns, "
        f"{n_data_residuals} data + {n_reg_residuals} reg residuals, "
        f"lambda_reg={lambda_reg:.1f}"
    )

    result = least_squares(
        residuals, x0, method='trf',
        loss='huber', f_scale=5.0,
        ftol=1e-12, xtol=1e-12, max_nfev=5000,
    )
    logger.info(
        f"  Solver: cost={result.cost:.1f}, nfev={result.nfev}, "
        f"status={result.status}"
    )

    x_opt = result.x

    affines = {}
    for k in all_keys:
        a, b, c, d, tx, ty = get_params(x_opt, k)
        affines[k] = np.array([
            [a, b, tx],
            [c, d, ty],
        ], dtype=np.float64)

        if k != anchor_key:
            _, _, rmin, cmin, _, _ = reprojected[k]
            theta = np.degrees(np.arctan2(b, a))
            sx_eff = np.sqrt(a**2 + c**2)
            sy_eff = np.sqrt(b**2 + d**2)
            logger.info(
                f"  Tile {k}: rot={theta:.3f}\u00b0, "
                f"sx={sx_eff:.6f}, sy={sy_eff:.6f}, "
                f"dtx={tx - cmin:.1f}, dty={ty - rmin:.1f} px"
            )

    return affines


# ---------------------------------------------------------------------------
# Cylindrical mosaic: shared helpers
# ---------------------------------------------------------------------------

def _load_tiles(
    positions: List[Dict],
    summary_dir: Path,
    band_index: int,
    pan_to_idx: Dict[float, int],
    tilt_to_idx: Dict[float, int],
) -> Dict[Tuple[int, int], Tuple[np.ndarray, float, float]]:
    """Load all tiles from NetCDF files into a grid-indexed dict.

    Parameters
    ----------
    positions : list of dict
        Successful position results.
    summary_dir : Path
        Directory containing the summary JSON.
    band_index : int
        Band array index (0-based).
    pan_to_idx, tilt_to_idx : dict
        Mappings from angle value to grid index.

    Returns
    -------
    dict
        Mapping from (pi, ti) -> (tile_data, pan_deg, tilt_deg).
    """
    tiles = {}
    for pos in positions:
        pan_deg = pos["target_position"]["pan_deg"]
        tilt_deg = pos["target_position"]["tilt_deg"]
        pi = pan_to_idx[pan_deg]
        ti = tilt_to_idx[tilt_deg]

        nc_path = _resolve_nc_path(pos, summary_dir)
        with nc.Dataset(nc_path, "r") as ds:
            tile_data = read_band_oriented(ds, band_index).astype(np.float64)

        # Grid indexing uses the nominal angles; projection uses the
        # parallax-corrected pointing (identity when no scene range set)
        eff_pan, eff_tilt = parallax_corrected_center(pan_deg, tilt_deg)
        tiles[(pi, ti)] = (tile_data, eff_pan, eff_tilt)
        logger.info(
            f"  Loaded {pos['position_id']}: pan={eff_pan:.2f}, "
            f"tilt={eff_tilt:.2f}"
        )

    return tiles


def _compute_overlap_rms_diagnostic(
    reprojected: Dict[Tuple[int, int], Tuple],
    label: str,
) -> None:
    """Log overlap RMS diff diagnostics for all neighbor pairs.

    Parameters
    ----------
    reprojected : dict
        Reprojected tile data.
    label : str
        Label for log messages.
    """
    logger.info(f"  Overlap RMS diff ({label}):")
    for direction_label, pair_filter in [("Vertical", "V"), ("Horizontal", "H")]:
        rms_list = []
        for (pi, ti) in sorted(reprojected.keys()):
            for key_b in [(pi + 1, ti), (pi, ti + 1)]:
                if key_b not in reprojected:
                    continue
                d = "H" if key_b[0] != pi else "V"
                if d != pair_filter:
                    continue
                key_a = (pi, ti)
                sam_a, val_a, rmin_a, cmin_a, _, _ = reprojected[key_a]
                sam_b, val_b, rmin_b, cmin_b, _, _ = reprojected[key_b]
                rh_a, rw_a = sam_a.shape
                rh_b, rw_b = sam_b.shape

                ovlp_r0 = max(rmin_a, rmin_b)
                ovlp_r1 = min(rmin_a + rh_a, rmin_b + rh_b)
                ovlp_c0 = max(cmin_a, cmin_b)
                ovlp_c1 = min(cmin_a + rw_a, cmin_b + rw_b)
                if ovlp_r1 <= ovlp_r0 or ovlp_c1 <= ovlp_c0:
                    continue

                ra = sam_a[
                    ovlp_r0 - rmin_a : ovlp_r1 - rmin_a,
                    ovlp_c0 - cmin_a : ovlp_c1 - cmin_a,
                ]
                va = val_a[
                    ovlp_r0 - rmin_a : ovlp_r1 - rmin_a,
                    ovlp_c0 - cmin_a : ovlp_c1 - cmin_a,
                ]
                rb = sam_b[
                    ovlp_r0 - rmin_b : ovlp_r1 - rmin_b,
                    ovlp_c0 - cmin_b : ovlp_c1 - cmin_b,
                ]
                vb = val_b[
                    ovlp_r0 - rmin_b : ovlp_r1 - rmin_b,
                    ovlp_c0 - cmin_b : ovlp_c1 - cmin_b,
                ]

                both_valid = va & vb
                n_valid = int(np.sum(both_valid))
                if n_valid < 100:
                    continue

                diff = ra[both_valid].astype(np.float64) - rb[both_valid].astype(np.float64)
                rms = float(np.sqrt(np.mean(diff ** 2)))
                mean_signal = float(np.mean(
                    (ra[both_valid] + rb[both_valid]) / 2.0
                ))
                nrms = rms / max(1.0, mean_signal) * 100

                logger.info(
                    f"    {key_a}-{key_b} ({d}): "
                    f"n={n_valid}, RMS={rms:.0f}, nRMS={nrms:.1f}%"
                )
                rms_list.append(nrms)

        if rms_list:
            logger.info(
                f"    {direction_label} mean nRMS: {np.mean(rms_list):.1f}%"
            )


def _compute_post_warp_rms_diagnostic(
    warped_tiles: Dict[Tuple[int, int], np.ndarray],
    warped_valids: Dict[Tuple[int, int], np.ndarray],
    reprojected: Dict[Tuple[int, int], Tuple],
    label: str,
) -> None:
    """Log pixel-level overlap RMS after affine warp.

    Parameters
    ----------
    warped_tiles : dict
        Warped tile data.
    warped_valids : dict
        Warped tile valid masks.
    reprojected : dict
        Reprojected tile data (for key enumeration).
    label : str
        Label for log messages.
    """
    logger.info(f"  Pixel-level overlap nRMS ({label}):")
    for direction_label, pair_filter in [("Vertical", "V"), ("Horizontal", "H")]:
        rms_list = []
        for (pi, ti) in sorted(reprojected.keys()):
            for key_b in [(pi + 1, ti), (pi, ti + 1)]:
                if key_b not in reprojected:
                    continue
                d = "H" if key_b[0] != pi else "V"
                if d != pair_filter:
                    continue
                key_a = (pi, ti)

                if key_a not in warped_valids or key_b not in warped_valids:
                    continue

                both = warped_valids[key_a] & warped_valids[key_b]
                n_valid = int(np.sum(both))
                if n_valid < 100:
                    continue

                diff = (warped_tiles[key_a][both].astype(np.float64)
                        - warped_tiles[key_b][both].astype(np.float64))
                rms = float(np.sqrt(np.mean(diff ** 2)))
                mean_sig = float(np.mean(
                    (warped_tiles[key_a][both] + warped_tiles[key_b][both])
                    / 2.0
                ))
                nrms = rms / max(1.0, mean_sig) * 100

                logger.info(
                    f"    {key_a}-{key_b} ({d}): n={n_valid}, "
                    f"RMS={rms:.0f}, nRMS={nrms:.1f}%"
                )
                rms_list.append(nrms)

        if rms_list:
            logger.info(
                f"    {direction_label} mean nRMS: {np.mean(rms_list):.1f}%"
            )


def _warp_and_composite(
    reprojected: Dict[Tuple[int, int], Tuple],
    affines: Dict[Tuple[int, int], np.ndarray],
    canvas_w: int,
    canvas_h: int,
    draw_order: List[Tuple[int, int]],
) -> Tuple[np.ndarray, Dict[Tuple[int, int], np.ndarray], Dict[Tuple[int, int], np.ndarray]]:
    """Warp reprojected tiles using affine transforms and composite.

    Parameters
    ----------
    reprojected : dict
        Reprojected tile data.
    affines : dict
        Per-tile 2x3 affine matrices.
    canvas_w, canvas_h : int
        Output canvas dimensions.
    draw_order : list of (pi, ti)
        Tile placement order.

    Returns
    -------
    mosaic : np.ndarray
        Composited mosaic (float64).
    warped_tiles : dict
        Warped tile data (for RMS diagnostics).
    warped_valids : dict
        Warped valid masks.
    """
    import cv2

    mosaic = np.full((canvas_h, canvas_w), np.nan, dtype=np.float32)
    has_data = np.zeros((canvas_h, canvas_w), dtype=bool)
    warped_tiles = {}
    warped_valids = {}

    for key in draw_order:
        sampled, valid, row_min, col_min, eff_pan, eff_tilt = reprojected[key]
        A = affines[key]

        warped = cv2.warpAffine(
            sampled.astype(np.float32), A, (canvas_w, canvas_h),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=float('nan'),
        )
        warped_v = np.isfinite(warped)

        warped_tiles[key] = warped
        warped_valids[key] = warped_v

        mosaic[warped_v] = warped[warped_v]
        has_data |= warped_v

        a, b, tx = A[0, 0], A[0, 1], A[0, 2]
        c, d, ty = A[1, 0], A[1, 1], A[1, 2]
        theta = np.degrees(np.arctan2(b, a))
        sx_eff = np.sqrt(a**2 + c**2)
        sy_eff = np.sqrt(b**2 + d**2)
        logger.info(
            f"  Placed tile {key}: rot={theta:.3f}\u00b0, "
            f"sx={sx_eff:.6f}, sy={sy_eff:.6f}, "
            f"dtx={tx - col_min:.1f}, dty={ty - row_min:.1f}, "
            f"{int(np.sum(warped_v))} px"
        )

    n_uncovered = np.sum(~has_data)
    if n_uncovered > 0:
        total = canvas_h * canvas_w
        logger.warning(
            f"{n_uncovered}/{total} pixels ({n_uncovered / total:.1%}) "
            f"have no tile coverage"
        )

    return mosaic, warped_tiles, warped_valids


# ---------------------------------------------------------------------------
# Mosaic model save/load
# ---------------------------------------------------------------------------

def _save_mosaic_model(
    path: Path,
    reference_band_index: int,
    camera_params: Dict,
    canvas_params: Dict,
    deltas: Dict[Tuple[int, int], Tuple[float, float]],
    affines: Dict[Tuple[int, int], np.ndarray],
    draw_order: List[Tuple[int, int]],
) -> None:
    """Save projection model to JSON for reuse across bands.

    Parameters
    ----------
    path : Path
        Output JSON file path.
    reference_band_index : int
        Band index used to compute the model.
    camera_params : dict
        Optimized camera intrinsics.
    canvas_params : dict
        Canvas geometry.
    deltas : dict
        Per-tile pointing corrections.
    affines : dict
        Per-tile 2x3 affine matrices.
    draw_order : list of (pi, ti)
        Tile draw order.
    """
    model = {
        "version": 1,
        "reference_band_index": reference_band_index,
        "camera_params": camera_params,
        "canvas": canvas_params,
        "per_tile_deltas_rad": {
            str(k): list(v) for k, v in deltas.items()
        },
        "per_tile_affines": {
            str(k): v.tolist() for k, v in affines.items()
        },
        "draw_order": [list(k) for k in draw_order],
    }

    with open(path, "w") as f:
        json.dump(model, f, indent=2)

    logger.info(f"Mosaic model saved: {path}")


def _load_mosaic_model(path: Path) -> Dict:
    """Load a previously saved mosaic model.

    Parameters
    ----------
    path : Path
        Path to the mosaic model JSON file.

    Returns
    -------
    dict
        Model dict with parsed keys and numpy arrays.
    """
    with open(path) as f:
        raw = json.load(f)

    if raw.get("version", 0) != 1:
        raise ValueError(
            f"Unsupported mosaic model version: {raw.get('version')}"
        )

    # Parse string tuple keys back to actual tuples
    deltas = {}
    for k, v in raw["per_tile_deltas_rad"].items():
        key = tuple(int(x) for x in k.strip("()").split(","))
        deltas[key] = tuple(v)

    affines = {}
    for k, v in raw["per_tile_affines"].items():
        key = tuple(int(x) for x in k.strip("()").split(","))
        affines[key] = np.array(v, dtype=np.float64)

    draw_order = [tuple(k) for k in raw["draw_order"]]

    model = {
        "version": raw["version"],
        "reference_band_index": raw["reference_band_index"],
        "camera_params": raw["camera_params"],
        "canvas": raw["canvas"],
        "per_tile_deltas_rad": deltas,
        "per_tile_affines": affines,
        "draw_order": draw_order,
    }

    logger.info(
        f"Loaded mosaic model from {path} "
        f"(reference band {model['reference_band_index']}, "
        f"{len(affines)} tiles)"
    )

    return model


# ---------------------------------------------------------------------------
# Cylindrical mosaic assembler
# ---------------------------------------------------------------------------

def _reproject_all_tiles(
    tiles: Dict[Tuple[int, int], Tuple[np.ndarray, float, float]],
    deltas: Dict[Tuple[int, int], Tuple[float, float]],
    f_px: float,
    cx: float,
    cy: float,
    tile_w: int,
    tile_h: int,
    roll_rad: float,
    k1: float,
    half_fov_h: float,
    half_fov_v: float,
    az_min: float,
    el_max: float,
    deg_per_px: float,
    canvas_w: int,
    canvas_h: int,
) -> Dict[Tuple[int, int], Tuple[np.ndarray, np.ndarray, int, int, float, float]]:
    """Reproject all tiles to cylindrical coordinates.

    Parameters
    ----------
    tiles : dict
        Mapping from (pi, ti) -> (tile_data, pan_deg, tilt_deg).
    deltas : dict
        Per-tile pointing corrections (radians).
    f_px : float
        Focal length in pixels.
    cx, cy : float
        Principal point.
    tile_w, tile_h : int
        Tile dimensions.
    roll_rad : float
        Sensor roll.
    k1 : float
        Radial distortion coefficient.
    half_fov_h, half_fov_v : float
        Tile half field-of-view in degrees.
    az_min, el_max : float
        Canvas angular origin in degrees.
    deg_per_px : float
        Angular resolution.
    canvas_w, canvas_h : int
        Canvas dimensions.

    Returns
    -------
    dict
        Mapping (pi, ti) -> (sampled, valid, row_lo, col_lo, eff_pan, eff_tilt).
    """
    az_arr = np.radians(az_min + (np.arange(canvas_w) + 0.5) * deg_per_px)
    el_arr = np.radians(el_max - (np.arange(canvas_h) + 0.5) * deg_per_px)
    cos_el_all = np.cos(el_arr)
    sin_el_all = np.sin(el_arr)
    cos_az_all = np.cos(az_arr)
    sin_az_all = np.sin(az_arr)

    reprojected = {}
    for key, (tile_data, pan_deg, tilt_deg) in tiles.items():
        dp, dt = deltas.get(key, (0.0, 0.0))
        eff_pan = pan_deg + np.degrees(dp)
        eff_tilt = tilt_deg + np.degrees(dt)

        col_lo = max(
            0,
            int((eff_pan - half_fov_h - az_min) / deg_per_px) - 2,
        )
        col_hi = min(
            canvas_w,
            int((eff_pan + half_fov_h - az_min) / deg_per_px) + 3,
        )
        row_lo = max(
            0,
            int((el_max - eff_tilt - half_fov_v) / deg_per_px) - 2,
        )
        row_hi = min(
            canvas_h,
            int((el_max - eff_tilt + half_fov_v) / deg_per_px) + 3,
        )

        if col_hi <= col_lo or row_hi <= row_lo:
            continue

        sampled, valid = _reproject_tile(
            tile_data,
            np.radians(pan_deg) + dp,
            np.radians(tilt_deg) + dt,
            f_px, cx, cy, tile_w, tile_h,
            cos_el_all[row_lo:row_hi], sin_el_all[row_lo:row_hi],
            cos_az_all[col_lo:col_hi], sin_az_all[col_lo:col_hi],
            roll_rad=roll_rad, k1=k1,
        )

        reprojected[key] = (sampled, valid, row_lo, col_lo, eff_pan, eff_tilt)
        n_valid = np.sum(valid)
        logger.info(
            f"  Reprojected {key}: pan={eff_pan:.2f}, tilt={eff_tilt:.2f}, "
            f"{sampled.shape[1]}x{sampled.shape[0]} px, {n_valid} valid"
        )

    return reprojected


def assemble_mosaic_cylindrical(
    geometry: Dict,
    positions: List[Dict],
    band_index: int,
    summary_dir: Path,
    tone_mode: str = "gain",
    save_model_path: Path = None,
    load_model_path: Path = None,
) -> np.ndarray:
    """Assemble a mosaic using cylindrical equirectangular reprojection
    with SIFT-based global affine refinement.

    When ``save_model_path`` is provided, the optimized projection
    parameters and affine transforms are written to a JSON file for
    reuse. When ``load_model_path`` is provided, the expensive
    optimization and SIFT registration are skipped, and the saved
    parameters are applied directly. This enables fast mosaicking of
    additional bands or derived products that share the same tile
    geometry.

    Parameters
    ----------
    geometry : dict
        Grid geometry from survey summary.
    positions : list of dict
        Successful position results.
    band_index : int
        Band array index (0-based).
    summary_dir : Path
        Directory containing the summary JSON.
    tone_mode : str
        Tone balancing mode: "gain" or "none".
    save_model_path : Path, optional
        If provided, save projection model to this path.
    load_model_path : Path, optional
        If provided, load projection model from this path.

    Returns
    -------
    np.ndarray
        Mosaic image as uint16 array.
    """
    # --- Camera parameters ---
    first_nc_path = _resolve_nc_path(positions[0], summary_dir)
    with nc.Dataset(first_nc_path, "r") as ds:
        tile_shape = ds.variables["digital_number"].shape[1:]
        pixel_size_um = float(ds.getncattr("pixel_size_um"))

    tile_h, tile_w = tile_shape
    pixel_size_mm = pixel_size_um / 1000.0
    f_eff_mm = _get_effective_focal_length(first_nc_path)
    f_px = f_eff_mm / pixel_size_mm

    cx = (tile_w - 1) / 2.0
    cy = (tile_h - 1) / 2.0

    logger.info(
        f"Camera: f_eff={f_eff_mm:.2f}mm, f_px={f_px:.1f}px, "
        f"tile={tile_w}x{tile_h}, center=({cx:.1f}, {cy:.1f})"
    )

    # --- Grid indexing ---
    pan_vals = sorted(set(p["target_position"]["pan_deg"] for p in positions))
    tilt_vals = sorted(set(p["target_position"]["tilt_deg"] for p in positions))
    pan_to_idx = {v: i for i, v in enumerate(pan_vals)}
    tilt_to_idx = {v: i for i, v in enumerate(tilt_vals)}
    n_pan = len(pan_vals)
    n_tilt = len(tilt_vals)

    # --- Load model or compute from scratch ---
    if load_model_path is not None:
        model = _load_mosaic_model(load_model_path)
        cp = model["camera_params"]
        f_eff_opt = cp["f_eff_mm"]
        cx_off_opt = cp["cx_offset_px"]
        cy_off_opt = cp["cy_offset_px"]
        roll_opt = cp["roll_rad"]
        k1_opt = cp["k1"]
        deltas = model["per_tile_deltas_rad"]
        affines = model["per_tile_affines"]
        draw_order = model["draw_order"]

        canvas_p = model["canvas"]
        az_min_opt = canvas_p["az_min_deg"]
        el_max_opt = canvas_p["el_max_deg"]
        deg_per_px_opt = canvas_p["deg_per_px"]
        canvas_w_opt = canvas_p["width_px"]
        canvas_h_opt = canvas_p["height_px"]

        f_px_opt = f_eff_opt / pixel_size_mm
        cx_opt = cx + cx_off_opt
        cy_opt = cy + cy_off_opt

        half_w = (tile_w - 1) / 2.0
        half_h = (tile_h - 1) / 2.0
        half_fov_h_opt = np.degrees(np.arctan(half_w / f_px_opt))
        half_fov_v_opt = np.degrees(np.arctan(half_h / f_px_opt))

        # Load tiles for the requested band
        logger.info("Phase 1: Loading tiles...")
        tiles = _load_tiles(
            positions, summary_dir, band_index, pan_to_idx, tilt_to_idx
        )

        # Reproject with saved parameters
        logger.info("Phase 3: Reprojecting with saved model parameters...")
        reprojected = _reproject_all_tiles(
            tiles, deltas,
            f_px_opt, cx_opt, cy_opt, tile_w, tile_h,
            roll_opt, k1_opt,
            half_fov_h_opt, half_fov_v_opt,
            az_min_opt, el_max_opt, deg_per_px_opt,
            canvas_w_opt, canvas_h_opt,
        )

        # Recompute tone corrections for this band
        all_pairs = _identify_overlap_pairs(
            positions, pan_to_idx, tilt_to_idx,
            half_fov_h_opt, half_fov_v_opt,
        )
        if tone_mode == "gain":
            logger.info("Phase 4: Tone balancing (recomputed for this band)...")
            gains = _compute_tone_corrections(reprojected, all_pairs)
            for key in sorted(gains.keys()):
                logger.info(f"  Tile {key}: gain = {gains[key]:.4f}")
            _apply_tone_corrections(reprojected, gains)

        # Warp with saved affines
        logger.info("Phase 5: Warping with saved affine transforms...")
        mosaic, warped_tiles, warped_valids = _warp_and_composite(
            reprojected, affines, canvas_w_opt, canvas_h_opt, draw_order,
        )

        _compute_post_warp_rms_diagnostic(
            warped_tiles, warped_valids, reprojected,
            "after affine warp",
        )
        del warped_tiles, warped_valids

        return mosaic.astype(np.float32)

    # --- Full computation path ---

    # Output canvas geometry
    rad_per_px = pixel_size_mm / f_eff_mm
    deg_per_px = np.degrees(rad_per_px)

    half_w = (tile_w - 1) / 2.0
    half_h = (tile_h - 1) / 2.0
    tile_half_fov_h = np.degrees(np.arctan(half_w / f_px))
    tile_half_fov_v = np.degrees(np.arctan(half_h / f_px))

    logger.info(
        f"Tile true FOV: {2*tile_half_fov_h:.3f} x {2*tile_half_fov_v:.3f} deg "
        f"(geometry says {geometry['fov_h_deg']:.2f} x {geometry['fov_v_deg']:.2f})"
    )
    logger.info(f"Output resolution: {deg_per_px:.6f} deg/px")

    pan_angles = [p["target_position"]["pan_deg"] for p in positions]
    tilt_angles = [p["target_position"]["tilt_deg"] for p in positions]

    az_min = min(pan_angles) - tile_half_fov_h
    az_max = max(pan_angles) + tile_half_fov_h
    el_min = min(tilt_angles) - tile_half_fov_v
    el_max = max(tilt_angles) + tile_half_fov_v

    canvas_w = int(np.ceil((az_max - az_min) / deg_per_px))
    canvas_h = int(np.ceil((el_max - el_min) / deg_per_px))

    logger.info(
        f"Canvas: {canvas_w}x{canvas_h} px, "
        f"az=[{az_min:.2f}, {az_max:.2f}], el=[{el_min:.2f}, {el_max:.2f}] deg"
    )

    # === Phase 1: Load all tiles ===
    logger.info("Phase 1: Loading tiles...")
    tiles = _load_tiles(
        positions, summary_dir, band_index, pan_to_idx, tilt_to_idx
    )

    # === Phase 2: Optimize camera params (vertical pairs only) ===
    logger.info("Phase 2: Optimizing camera parameters (vertical pairs only)...")

    all_pairs = _identify_overlap_pairs(
        positions, pan_to_idx, tilt_to_idx,
        tile_half_fov_h, tile_half_fov_v,
    )
    vertical_pairs = [
        (ka, kb, az_range, el_range)
        for ka, kb, az_range, el_range in all_pairs
        if ka[0] == kb[0]
    ]
    logger.info(
        f"  Found {len(all_pairs)} total pairs, "
        f"using {len(vertical_pairs)} vertical pairs"
    )

    (f_eff_opt, cx_off_opt, cy_off_opt, roll_opt, k1_opt,
     deltas) = _optimize_projection_params(
        tiles, vertical_pairs,
        f_eff_mm, pixel_size_mm, cx, cy, tile_w, tile_h,
        subsample=4,
    )

    f_px_opt = f_eff_opt / pixel_size_mm
    cx_opt = cx + cx_off_opt
    cy_opt = cy + cy_off_opt

    # === Phase 3: Reproject all tiles ===
    logger.info("Phase 3: Reprojecting with optimized parameters...")

    rad_per_px_opt = pixel_size_mm / f_eff_opt
    deg_per_px_opt = np.degrees(rad_per_px_opt)

    half_fov_h_opt = np.degrees(np.arctan(half_w / f_px_opt))
    half_fov_v_opt = np.degrees(np.arctan(half_h / f_px_opt))

    eff_centers = {}
    for key, (_, pan_deg, tilt_deg) in tiles.items():
        dp, dt = deltas.get(key, (0.0, 0.0))
        eff_centers[key] = (
            pan_deg + np.degrees(dp),
            tilt_deg + np.degrees(dt),
        )

    az_min_opt = min(p - half_fov_h_opt for p, _ in eff_centers.values())
    az_max_opt = max(p + half_fov_h_opt for p, _ in eff_centers.values())
    el_min_opt = min(t - half_fov_v_opt for _, t in eff_centers.values())
    el_max_opt = max(t + half_fov_v_opt for _, t in eff_centers.values())

    canvas_w_opt = int(np.ceil((az_max_opt - az_min_opt) / deg_per_px_opt))
    canvas_h_opt = int(np.ceil((el_max_opt - el_min_opt) / deg_per_px_opt))

    logger.info(
        f"  Optimized canvas: {canvas_w_opt}x{canvas_h_opt} px, "
        f"az=[{az_min_opt:.2f}, {az_max_opt:.2f}], "
        f"el=[{el_min_opt:.2f}, {el_max_opt:.2f}] deg"
    )

    reprojected = _reproject_all_tiles(
        tiles, deltas,
        f_px_opt, cx_opt, cy_opt, tile_w, tile_h,
        roll_opt, k1_opt,
        half_fov_h_opt, half_fov_v_opt,
        az_min_opt, el_max_opt, deg_per_px_opt,
        canvas_w_opt, canvas_h_opt,
    )

    # === Phase 4: Tone balancing ===
    if tone_mode == "gain":
        logger.info("Phase 4: Tone balancing (gain correction, all pairs)...")
        gains = _compute_tone_corrections(reprojected, all_pairs)
        for key in sorted(gains.keys()):
            logger.info(f"  Tile {key}: gain = {gains[key]:.4f}")
        _apply_tone_corrections(reprojected, gains)
        logger.info("  Tone corrections applied")
    else:
        logger.info("Phase 4: Tone balancing skipped (mode=none)")

    # === Phase 5: Global pairwise registration ===
    logger.info("Phase 5: Global pairwise registration...")

    _compute_overlap_rms_diagnostic(reprojected, "pure cylindrical")

    # 5a: SIFT match all neighbor pairs
    pair_matches = _compute_pairwise_matches(reprojected)
    logger.info(f"  Matched {len(pair_matches)} pairs")

    # 5b: Solve for per-tile full affine
    anchor_key = (n_pan - 1, n_tilt - 1)
    affines = _solve_global_affines(pair_matches, reprojected, anchor_key)

    # 5b2: Measure overlap RMS AFTER registration
    logger.info("  Overlap RMS diff AFTER global affine:")
    for direction_label, pair_filter in [("Vertical", "V"), ("Horizontal", "H")]:
        rms_list = []
        for key_a, key_b, pts_a, pts_b, _ in pair_matches:
            d = "H" if key_b[0] != key_a[0] else "V"
            if d != pair_filter:
                continue

            A_a = affines[key_a]
            A_b = affines[key_b]

            ca_col = A_a[0, 0] * pts_a[:, 0] + A_a[0, 2]
            ca_row = A_a[1, 1] * pts_a[:, 1] + A_a[1, 2]
            cb_col = A_b[0, 0] * pts_b[:, 0] + A_b[0, 2]
            cb_row = A_b[1, 1] * pts_b[:, 1] + A_b[1, 2]

            err = np.sqrt((ca_col - cb_col)**2 + (ca_row - cb_row)**2)
            rms_px = float(np.sqrt(np.mean(err**2)))

            logger.info(
                f"    {key_a}-{key_b} ({d}): {len(pts_a)} pts, "
                f"RMS={rms_px:.1f} px"
            )
            rms_list.append(rms_px)

        if rms_list:
            logger.info(
                f"    {direction_label} mean RMS: {np.mean(rms_list):.1f} px"
            )

    # 5c: Warp and place tiles
    col_order = [3, 0, 1, 2]
    draw_order = []
    for pi in col_order:
        for ti in range(n_tilt):
            if (pi, ti) in reprojected:
                draw_order.append((pi, ti))

    logger.info(
        f"  Draw order ({len(draw_order)} tiles): "
        + " -> ".join(str(k) for k in draw_order)
    )

    mosaic, warped_tiles, warped_valids = _warp_and_composite(
        reprojected, affines, canvas_w_opt, canvas_h_opt, draw_order,
    )

    # 5d: Post-warp diagnostics
    _compute_post_warp_rms_diagnostic(
        warped_tiles, warped_valids, reprojected,
        "after affine warp",
    )
    del warped_tiles, warped_valids

    # Save model if requested
    if save_model_path is not None:
        _save_mosaic_model(
            save_model_path,
            reference_band_index=band_index,
            camera_params={
                "f_eff_mm": f_eff_opt,
                "cx_offset_px": cx_off_opt,
                "cy_offset_px": cy_off_opt,
                "roll_rad": roll_opt,
                "k1": k1_opt,
            },
            canvas_params={
                "az_min_deg": az_min_opt,
                "el_max_deg": el_max_opt,
                "deg_per_px": deg_per_px_opt,
                "width_px": canvas_w_opt,
                "height_px": canvas_h_opt,
            },
            deltas=deltas,
            affines=affines,
            draw_order=draw_order,
        )

    return mosaic.astype(np.float32)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Assemble grid survey tiles into a mosaic",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m scripts.analysis.create_mosaic \\\n"
            "      --summary out/survey_summary.json --band 0\n\n"
            "  python -m scripts.analysis.create_mosaic \\\n"
            "      --summary out/survey_summary.json \\\n"
            "      --band 5 --by-filter --mode cylindrical\n\n"
            "  # Save model, then reuse for other bands:\n"
            "  python -m scripts.analysis.create_mosaic \\\n"
            "      --summary out/survey_summary.json \\\n"
            "      --band 5 --mode cylindrical --save-model model.json\n"
            "  python -m scripts.analysis.create_mosaic \\\n"
            "      --summary out/survey_summary.json \\\n"
            "      --band 0 --mode cylindrical --load-model model.json\n"
        ),
    )
    parser.add_argument(
        "--summary", type=str, required=True,
        help="Path to grid survey summary JSON file",
    )
    parser.add_argument(
        "--band", type=int, default=0,
        help="Band index (0-based) or filter position (default: 0)",
    )
    parser.add_argument(
        "--by-filter", action="store_true",
        help="Interpret --band as filter wheel position instead of array index",
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Output TIFF file path (default: auto-generated in out/)",
    )
    parser.add_argument(
        "--mode", type=str, default="coordinate",
        choices=["coordinate", "opencv", "cylindrical"],
        help="Mosaic mode: coordinate (flat tile placement), "
             "opencv (column-first with phase correlation), or "
             "cylindrical (gnomonic-to-cylindrical with SIFT refinement) "
             "(default: coordinate)",
    )
    parser.add_argument(
        "--tone-mode", type=str, default="gain",
        choices=["none", "gain"],
        help="Tone balancing mode (default: gain)",
    )
    parser.add_argument(
        "--save-model", type=str, default=None,
        help="Save cylindrical projection model to JSON for reuse",
    )
    parser.add_argument(
        "--load-model", type=str, default=None,
        help="Load cylindrical projection model from JSON (skip optimization)",
    )
    parser.add_argument(
        "--max-position-error", type=int, default=None,
        metavar="STEPS",
        help="Exclude grid positions where pan or tilt position error "
             "exceeds this threshold (in encoder steps)",
    )
    parser.add_argument(
        "--scene-range-m", type=float, default=None,
        help="Assumed scene distance in meters for boresight lever-arm "
             "parallax correction of tile placement (camera offset read "
             "from ptu_specifications.json mount_geometry); default: "
             "infinity (no correction)",
    )
    return parser.parse_args()


def main() -> int:
    """Run mosaic assembly."""
    args = parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    summary_path = Path(args.summary)
    if not summary_path.exists():
        logger.error(f"Summary file not found: {summary_path}")
        return 1

    summary_dir = summary_path.parent

    # Boresight lever-arm parallax correction for tile placement
    if args.scene_range_m is not None:
        camera_offset = load_mount_offset()
        configure_parallax(camera_offset, args.scene_range_m)
        logger.info(
            f"Parallax correction enabled: camera offset "
            f"{camera_offset} m, scene range {args.scene_range_m} m"
        )

    geometry, positions = load_survey(str(summary_path))

    # Filter positions by position error
    if args.max_position_error is not None:
        tol = args.max_position_error
        n_before = len(positions)
        positions = [
            p for p in positions
            if abs(p.get("ptu_movement", {}).get("position_error_steps", {}).get("pan", 0)) <= tol
            and abs(p.get("ptu_movement", {}).get("position_error_steps", {}).get("tilt", 0)) <= tol
        ]
        n_dropped = n_before - len(positions)
        if n_dropped > 0:
            logger.info(
                f"Excluded {n_dropped}/{n_before} positions with "
                f"position error > {tol} steps"
            )

    # Resolve band index
    band_index = args.band
    if args.by_filter:
        first_nc = _resolve_nc_path(positions[0], summary_dir)
        band_index = find_band_index(first_nc, args.band)
        logger.info(
            f"Filter position {args.band} -> band index {band_index}"
        )

    # Get band info for output naming
    first_nc = _resolve_nc_path(positions[0], summary_dir)
    with nc.Dataset(first_nc, "r") as ds:
        wavelength = float(ds.variables["wavelength"][band_index])
        filter_pos = int(ds.variables["filter_position"][band_index])
    logger.info(f"Mosaicking band {band_index}: {wavelength:.0f} nm (filter {filter_pos})")

    # Validate model args
    if args.save_model and args.mode != "cylindrical":
        logger.warning("--save-model only applies to cylindrical mode, ignoring")
        args.save_model = None
    if args.load_model and args.mode != "cylindrical":
        logger.warning("--load-model only applies to cylindrical mode, ignoring")
        args.load_model = None

    # Assemble mosaic
    logger.info(f"Mode: {args.mode}")
    if args.mode == "cylindrical":
        mosaic = assemble_mosaic_cylindrical(
            geometry, positions, band_index, summary_dir,
            tone_mode=args.tone_mode,
            save_model_path=Path(args.save_model) if args.save_model else None,
            load_model_path=Path(args.load_model) if args.load_model else None,
        )
    elif args.mode == "opencv":
        mosaic = assemble_mosaic_opencv(
            geometry, positions, band_index, summary_dir
        )
    else:
        mosaic = assemble_mosaic(geometry, positions, band_index, summary_dir)

    # Trim empty border rows/columns
    has_data = np.isfinite(mosaic)
    row_mask = np.any(has_data, axis=1)
    col_mask = np.any(has_data, axis=0)
    if np.any(row_mask) and np.any(col_mask):
        r0, r1 = np.argmax(row_mask), len(row_mask) - np.argmax(row_mask[::-1])
        c0, c1 = np.argmax(col_mask), len(col_mask) - np.argmax(col_mask[::-1])
        trimmed = mosaic[r0:r1, c0:c1]
        logger.info(
            f"Trimmed canvas: {mosaic.shape[1]}x{mosaic.shape[0]} -> "
            f"{trimmed.shape[1]}x{trimmed.shape[0]} px "
            f"(removed {r0}+{mosaic.shape[0]-r1} rows, "
            f"{c0}+{mosaic.shape[1]-c1} cols)"
        )
        mosaic = trimmed

    # Determine output path
    if args.output:
        output_path = Path(args.output)
    else:
        with open(summary_path) as f:
            seq_name = json.load(f)["sequence_name"]
        mode_suffix = f"_{args.mode}" if args.mode != "coordinate" else ""
        output_path = summary_dir / (
            f"mosaic_{seq_name}_{wavelength:.0f}nm{mode_suffix}.tiff"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Save as TIFF (float32 with NaN nodata for cylindrical, uint16 otherwise)
    is_float = mosaic.dtype in (np.float32, np.float64)
    if is_float:
        img = Image.fromarray(mosaic.astype(np.float32))
    else:
        img = Image.fromarray(mosaic)
    img.save(str(output_path))
    logger.info(
        f"Mosaic saved: {output_path} ({mosaic.shape[1]}x{mosaic.shape[0]} px, "
        f"{'float32 NaN-nodata' if is_float else mosaic.dtype})"
    )

    # Save compressed JPEG preview
    jpeg_path = output_path.with_suffix(".jpg")
    finite_vals = mosaic[np.isfinite(mosaic)]
    if finite_vals.size > 0:
        p_lo, p_hi = np.percentile(finite_vals, [1, 99])
    else:
        p_lo, p_hi = 0, 1
    stretched = np.where(
        np.isfinite(mosaic),
        (mosaic.astype(np.float64) - p_lo) / max(1, p_hi - p_lo) * 255,
        0,
    )
    img_jpg = Image.fromarray(np.clip(stretched, 0, 255).astype(np.uint8))
    img_jpg.save(str(jpeg_path), quality=85, optimize=True)
    jpeg_kb = jpeg_path.stat().st_size / 1024
    logger.info(f"JPEG preview saved: {jpeg_path} ({jpeg_kb:.0f} KB)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
