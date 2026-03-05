#!/usr/bin/env python3
"""
Reflectance Calibration

Converts a raw multispectral image stack into an approximate reflectance
hypercube using Dark Object Subtraction (DOS) with an in-scene Spectralon
panel as the white reference and shadow regions as the dark reference.

For each band:
  1. Normalize by exposure time: DN_per_ms = DN / exposure_ms
  2. Compute shadow mean (path radiance estimate) and panel mean
  3. reflectance = panel_reflectance * (DN_per_ms - shadow_mean) /
                                       (panel_mean - shadow_mean)

Outputs:
  - reflectance_cube.npy — float32, shape (rows, cols, 15)
  - reflectance_cube_metadata.json — per-band calibration provenance
  - reflectance_bands/ — individual bands as uint16 TIFF (scaled x10000)

Usage:

  python scripts/analysis/calibrate_reflectance.py \\
      --input-dir ./out/raw --output-dir ./out/derived

  With custom panel reflectance (from calibration certificate):

    python scripts/analysis/calibrate_reflectance.py \\
        --input-dir ./out/raw --output-dir ./out/derived \\
        --panel-reflectance 0.99
"""

import argparse
import json
import logging
from pathlib import Path
from typing import List, Tuple

import numpy as np
from PIL import Image
from scipy import ndimage

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Filter positions 1-15 map to these center wavelengths (nm).
# Position 0 is clear/open (no bandpass) and is excluded.
FILTER_WAVELENGTHS_NM = [
    450, 500, 550, 600, 650, 700, 750, 800, 850, 900, 950, 975, 1000, 1050, 1100
]
NUM_BANDS = len(FILTER_WAVELENGTHS_NM)

# Reflectance TIFF scaling factor (remote sensing convention)
REFLECTANCE_SCALE_FACTOR = 10000

# Panel segmentation defaults
DEFAULT_PANEL_BAND = 2       # 550nm — good SNR, avoids chromatic issues
DEFAULT_PANEL_PERCENTILE = 95
DEFAULT_PANEL_EROSION = 20   # pixels to erode inward from panel edges

# Shadow segmentation defaults
DEFAULT_SHADOW_PERCENTILE = 15
DEFAULT_SHADOW_MIN_BANDS = 12
DEFAULT_SHADOW_CLOSING = 5
DEFAULT_SHADOW_EROSION = 2
DEFAULT_SHADOW_DILATION = 2


# ---------------------------------------------------------------------------
# Raw image loading
# ---------------------------------------------------------------------------

def load_raw_images(input_dir: Path) -> Tuple[np.ndarray, List[dict]]:
    """Load raw TIFF images and metadata for filter positions 1-15.

    Parameters
    ----------
    input_dir : Path
        Directory containing raw capture files (TIFF + JSON metadata).

    Returns
    -------
    cube : np.ndarray
        Raw DN image stack, shape (rows, cols, 15), float64.
    band_metadata : list of dict
        Per-band metadata including exposure_ms, filter_position, wavelength.
    """
    tiff_files = sorted(input_dir.glob("*.tiff")) + sorted(input_dir.glob("*.tif"))
    if not tiff_files:
        raise FileNotFoundError(f"No TIFF files found in {input_dir}")

    band_images = []

    for tiff_path in tiff_files:
        # Look for corresponding metadata JSON
        meta_path = tiff_path.with_suffix(".json")
        if not meta_path.exists():
            stem = tiff_path.stem
            meta_path = tiff_path.parent / f"{stem}_metadata.json"
        if not meta_path.exists():
            logger.warning("No metadata JSON for %s, skipping", tiff_path.name)
            continue

        with open(meta_path) as f:
            meta = json.load(f)

        filter_pos = meta.get("filter_position", meta.get("filter", {}).get("position"))
        if filter_pos is None:
            logger.warning("No filter_position in %s, skipping", meta_path.name)
            continue

        if filter_pos == 0:
            logger.info("Skipping filter position 0 (clear/open): %s", tiff_path.name)
            continue

        exposure_ms = meta.get("exposure_ms", meta.get("exposure", {}).get("duration_ms"))
        if exposure_ms is None:
            logger.warning("No exposure_ms in %s, skipping", meta_path.name)
            continue

        img = np.array(Image.open(tiff_path), dtype=np.float64)
        band_images.append((filter_pos, img, exposure_ms, meta))

    band_images.sort(key=lambda x: x[0])

    positions = [b[0] for b in band_images]
    expected = list(range(1, 16))
    if positions != expected:
        logger.warning("Expected filter positions %s, got %s", expected, positions)

    rows, cols = band_images[0][1].shape[:2]
    cube = np.zeros((rows, cols, len(band_images)), dtype=np.float64)
    metadata_list = []

    for i, (pos, img, exp_ms, meta) in enumerate(band_images):
        cube[:, :, i] = img
        metadata_list.append({
            "filter_position": pos,
            "wavelength_nm": FILTER_WAVELENGTHS_NM[i] if i < NUM_BANDS else None,
            "exposure_ms": exp_ms,
            "source_file": meta.get("source_file", ""),
        })

    logger.info("Loaded %d bands, image size %d x %d", len(band_images), rows, cols)
    return cube, metadata_list


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------

def segment_panel(
    cube: np.ndarray,
    band_index: int = DEFAULT_PANEL_BAND,
    percentile: int = DEFAULT_PANEL_PERCENTILE,
    erosion_pixels: int = DEFAULT_PANEL_EROSION,
) -> np.ndarray:
    """Segment the Spectralon panel as the largest bright connected component.

    Parameters
    ----------
    cube : np.ndarray
        Exposure-normalized image cube.
    band_index : int
        Band to use for thresholding (default 2 = 550nm).
    percentile : int
        Intensity percentile for bright threshold.
    erosion_pixels : int
        Morphological erosion to pull mask inward from edges.

    Returns
    -------
    mask : np.ndarray
        Boolean mask, True where panel is detected.
    """
    band = cube[:, :, band_index]
    threshold = np.percentile(band, percentile)
    bright = band > threshold

    labeled, n_features = ndimage.label(bright)
    if n_features == 0:
        raise ValueError("No bright regions found for panel segmentation")

    sizes = ndimage.sum(bright, labeled, range(1, n_features + 1))
    largest = np.argmax(sizes) + 1
    panel_mask = labeled == largest

    if erosion_pixels > 0:
        panel_mask = ndimage.binary_erosion(panel_mask, iterations=erosion_pixels)

    n_pixels = panel_mask.sum()
    logger.info(
        "Panel segmented: %d pixels (%.1f%% of image)",
        n_pixels, 100 * n_pixels / panel_mask.size,
    )
    return panel_mask


def segment_shadow(
    cube: np.ndarray,
    percentile: int = DEFAULT_SHADOW_PERCENTILE,
    min_bands: int = DEFAULT_SHADOW_MIN_BANDS,
    closing_iter: int = DEFAULT_SHADOW_CLOSING,
    erosion_iter: int = DEFAULT_SHADOW_EROSION,
    dilation_iter: int = DEFAULT_SHADOW_DILATION,
) -> np.ndarray:
    """Detect shadow regions using multi-band percentile thresholding.

    A pixel is classified as shadow if its value falls below the per-band
    percentile threshold in at least `min_bands` of the total bands.
    Morphological closing, erosion, and dilation clean up the mask.

    Parameters
    ----------
    cube : np.ndarray
        Exposure-normalized image cube.
    percentile : int
        Per-band percentile threshold.
    min_bands : int
        Minimum number of bands that must be below threshold.
    closing_iter, erosion_iter, dilation_iter : int
        Morphological operation iteration counts.

    Returns
    -------
    mask : np.ndarray
        Boolean mask, True where shadow is detected.
    """
    nb = cube.shape[2]
    thresholds = np.percentile(cube, percentile, axis=(0, 1))
    below = cube < thresholds[np.newaxis, np.newaxis, :]
    n_below = below.sum(axis=2)
    shadow_mask = n_below >= min_bands

    if closing_iter > 0:
        shadow_mask = ndimage.binary_closing(shadow_mask, iterations=closing_iter)
    if erosion_iter > 0:
        shadow_mask = ndimage.binary_erosion(shadow_mask, iterations=erosion_iter)
    if dilation_iter > 0:
        shadow_mask = ndimage.binary_dilation(shadow_mask, iterations=dilation_iter)

    pct = 100 * shadow_mask.sum() / shadow_mask.size
    logger.info(
        "Shadow mask: %d pixels (%.1f%%), percentile=%d, min_bands=%d/%d",
        shadow_mask.sum(), pct, percentile, min_bands, nb,
    )
    return shadow_mask


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

def calibrate_reflectance(
    cube: np.ndarray,
    band_metadata: List[dict],
    panel_mask: np.ndarray,
    shadow_mask: np.ndarray,
    panel_reflectance: float = 1.0,
) -> Tuple[np.ndarray, dict]:
    """Convert raw DN cube to approximate reflectance via DOS.

    For each band:
      1. Normalize by exposure time: DN_per_ms = DN / exposure_ms
      2. Compute shadow mean (dark reference) and panel mean (bright reference)
      3. reflectance = panel_reflectance * (DN_per_ms - shadow_mean) /
                                           (panel_mean - shadow_mean)

    Parameters
    ----------
    cube : np.ndarray
        Raw DN cube, shape (rows, cols, bands).
    band_metadata : list of dict
        Per-band metadata with 'exposure_ms' key.
    panel_mask : np.ndarray
        Boolean mask for Spectralon panel pixels.
    shadow_mask : np.ndarray
        Boolean mask for shadow pixels.
    panel_reflectance : float
        Assumed reflectance of the Spectralon panel (default 1.0 = 100%).

    Returns
    -------
    refl_cube : np.ndarray
        Reflectance cube, float32, shape (rows, cols, bands).
    calibration_metadata : dict
        Per-band calibration parameters for provenance.
    """
    rows, cols, nb = cube.shape
    refl_cube = np.zeros((rows, cols, nb), dtype=np.float32)
    cal_bands = []

    for i in range(nb):
        exp_ms = band_metadata[i]["exposure_ms"]
        dn_per_ms = cube[:, :, i] / exp_ms

        panel_mean = dn_per_ms[panel_mask].mean()
        shadow_mean = dn_per_ms[shadow_mask].mean()
        denom = panel_mean - shadow_mean

        if abs(denom) < 1e-10:
            logger.warning(
                "Band %d (%dnm): panel-shadow difference near zero, skipping",
                i, band_metadata[i].get("wavelength_nm", 0),
            )
            continue

        refl_cube[:, :, i] = panel_reflectance * (dn_per_ms - shadow_mean) / denom

        cal_bands.append({
            "filter_position": band_metadata[i]["filter_position"],
            "wavelength_nm": band_metadata[i]["wavelength_nm"],
            "exposure_ms": exp_ms,
            "panel_dn_per_ms": float(panel_mean),
            "shadow_dn_per_ms": float(shadow_mean),
            "panel_reflectance": float(panel_reflectance),
            "shadow_reflectance": float(
                panel_reflectance * (shadow_mean - shadow_mean) / denom
            ),
        })

        logger.info(
            "Band %d (%dnm): panel=%.1f, shadow=%.1f DN/ms, range=[%.3f, %.3f]",
            i, band_metadata[i]["wavelength_nm"],
            panel_mean, shadow_mean,
            refl_cube[:, :, i].min(), refl_cube[:, :, i].max(),
        )

    calibration_metadata = {
        "bands": cal_bands,
        "wavelengths_nm": FILTER_WAVELENGTHS_NM,
        "shape": list(refl_cube.shape),
        "description": (
            f"Approximate reflectance hypercube, {nb} bands "
            f"({FILTER_WAVELENGTHS_NM[0]}-{FILTER_WAVELENGTHS_NM[-1]}nm)"
        ),
        "calibration": (
            f"shadow-subtracted, Spectralon-normalized "
            f"(assumed {panel_reflectance * 100:.0f}%)"
        ),
        "axes": ["rows", "cols", "bands"],
    }

    return refl_cube, calibration_metadata


def save_reflectance_bands(cube: np.ndarray, output_dir: Path) -> None:
    """Save individual reflectance bands as uint16 TIFFs scaled by 10000."""
    bands_dir = output_dir / "reflectance_bands"
    bands_dir.mkdir(parents=True, exist_ok=True)

    for i, wl in enumerate(FILTER_WAVELENGTHS_NM):
        band = cube[:, :, i]
        scaled = np.clip(band * REFLECTANCE_SCALE_FACTOR, 0, 65535).astype(np.uint16)
        img = Image.fromarray(scaled)
        path = bands_dir / f"refl_{wl:04d}nm.tiff"
        img.save(str(path))

    logger.info("Saved %d reflectance band TIFFs to %s", NUM_BANDS, bands_dir)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run_calibration(args: argparse.Namespace) -> None:
    """Execute the reflectance calibration pipeline."""
    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Loading raw images from %s", input_dir)
    raw_cube, band_metadata = load_raw_images(input_dir)

    # Build exposure-normalized cube for segmentation
    exp_times = np.array([b["exposure_ms"] for b in band_metadata])
    cube_norm = raw_cube / exp_times[np.newaxis, np.newaxis, :]

    logger.info("Segmenting Spectralon panel")
    panel_mask = segment_panel(cube_norm)

    logger.info("Segmenting shadow regions")
    shadow_mask = segment_shadow(
        cube_norm,
        percentile=args.shadow_percentile,
        min_bands=args.shadow_min_bands,
    )

    logger.info("Calibrating reflectance (DOS)")
    refl_cube, cal_metadata = calibrate_reflectance(
        raw_cube, band_metadata, panel_mask, shadow_mask,
        panel_reflectance=args.panel_reflectance,
    )

    # Save reflectance cube
    cube_path = output_dir / "reflectance_cube.npy"
    np.save(str(cube_path), refl_cube)
    logger.info(
        "Saved reflectance cube: %s (%.0f MB)",
        cube_path, cube_path.stat().st_size / 1e6,
    )

    # Save calibration metadata
    meta_path = output_dir / "reflectance_cube_metadata.json"
    with open(meta_path, "w") as f:
        json.dump(cal_metadata, f, indent=2)
    logger.info("Saved calibration metadata: %s", meta_path)

    # Save per-band TIFFs
    save_reflectance_bands(refl_cube, output_dir)

    # Save diagnostic overlay
    idx_r, idx_g, idx_b = 4, 2, 0  # 650, 550, 450nm
    rgb = np.stack([
        refl_cube[:, :, idx_r],
        refl_cube[:, :, idx_g],
        refl_cube[:, :, idx_b],
    ], axis=2)
    rgb = np.clip(rgb * 3.0, 0, 1)
    rgb_8 = (rgb * 255).astype(np.uint8).copy()
    alpha = 0.4
    rgb_8[panel_mask] = (
        rgb_8[panel_mask] * (1 - alpha) + np.array([0, 255, 0]) * alpha
    ).astype(np.uint8)
    rgb_8[shadow_mask] = (
        rgb_8[shadow_mask] * (1 - alpha) + np.array([0, 0, 255]) * alpha
    ).astype(np.uint8)
    Image.fromarray(rgb_8).save(
        str(output_dir / "reflectance_roi_diagnostic.jpg"), quality=92
    )
    logger.info("Saved ROI diagnostic overlay")

    logger.info("Calibration complete")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Calibrate raw multispectral images to approximate "
                    "reflectance using Dark Object Subtraction (DOS).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--input-dir", type=str, required=True,
        help="Directory containing raw TIFF + metadata JSON files.",
    )
    parser.add_argument(
        "--output-dir", type=str, default="./out/derived",
        help="Output directory (default: ./out/derived).",
    )
    parser.add_argument(
        "--panel-reflectance", type=float, default=1.0,
        help="Assumed Spectralon panel reflectance, 0-1 (default: 1.0).",
    )
    parser.add_argument(
        "--shadow-percentile", type=int,
        default=DEFAULT_SHADOW_PERCENTILE,
        help=f"Per-band shadow percentile threshold (default: {DEFAULT_SHADOW_PERCENTILE}).",
    )
    parser.add_argument(
        "--shadow-min-bands", type=int,
        default=DEFAULT_SHADOW_MIN_BANDS,
        help=f"Minimum bands below threshold for shadow (default: {DEFAULT_SHADOW_MIN_BANDS}/{NUM_BANDS}).",
    )

    return parser.parse_args()


def main() -> None:
    """Entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    args = parse_args()
    run_calibration(args)


if __name__ == "__main__":
    main()
