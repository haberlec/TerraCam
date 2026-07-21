#!/usr/bin/env python3
"""
Derived Product Generator

Generates a suite of analysis products from a calibrated reflectance
hypercube. The reflectance cube is produced by calibrate_reflectance.py.

Products:
  - RGB composite (white-balanced, pan-sharpened)
  - False-color composites (NIR-R-G, deep NIR)
  - PCA composites (PC1-2-3, PC2-3-4)
  - Band ratio indices (iron oxide 650/500, NDVI-like)
  - Spectral angle map and spectral variability
  - Band depth at 975nm (continuum 900-1050nm and local)
  - Hydration slope 900-1000nm
  - Integrated band depth 975+1000nm (reflectance-weighted)
  - ROI diagnostic overlay (panel + shadow masks)

Usage:

  Basic (uses default crop and rotation):

    python scripts/analysis/generate_derived_products.py \\
        --cube ./out/derived/reflectance_cube.npy \\
        --output-dir ./out/derived

  With explicit crop and no rotation:

    python scripts/analysis/generate_derived_products.py \\
        --cube ./out/derived/reflectance_cube.npy \\
        --output-dir ./out/derived \\
        --crop 325 1899 380 2258 --no-rotate

  Adjust shadow mask parameters:

    python scripts/analysis/generate_derived_products.py \\
        --cube ./out/derived/reflectance_cube.npy \\
        --output-dir ./out/derived \\
        --shadow-percentile 10 --shadow-min-bands 10
"""

import argparse
import json
import logging
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
from PIL import Image
from scipy import ndimage
from sklearn.decomposition import PCA

from band_mapping import BandSet

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_CROP = (325, 1899, 380, 2258)

DEFAULT_SHADOW_PERCENTILE = 15
DEFAULT_SHADOW_MIN_BANDS = 12
DEFAULT_SHADOW_CLOSING = 5
DEFAULT_SHADOW_EROSION = 2
DEFAULT_SHADOW_DILATION = 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_band_set(metadata_path: Path, n_bands: int) -> BandSet:
    """Build a BandSet from the calibration metadata JSON.

    Parameters
    ----------
    metadata_path : Path
        Path to the ``*_metadata.json`` written by
        calibrate_reflectance.py alongside the cube.
    n_bands : int
        Number of bands in the loaded cube, for consistency validation.

    Returns
    -------
    tuple of (BandSet, dict)
        Wavelength-indexed band lookup and the full metadata dict (which
        carries ``sensor_inverted`` and other provenance).

    Raises
    ------
    FileNotFoundError
        If the metadata file does not exist.
    ValueError
        If the metadata band count does not match the cube.
    """
    if not metadata_path.exists():
        raise FileNotFoundError(
            f"Calibration metadata not found: {metadata_path}. "
            f"Band wavelengths are read from the metadata written by "
            f"calibrate_reflectance.py; pass --metadata explicitly if it "
            f"lives elsewhere."
        )

    with open(metadata_path) as f:
        metadata = json.load(f)

    wavelengths = metadata["wavelengths_nm"]
    if len(wavelengths) != n_bands:
        raise ValueError(
            f"Metadata lists {len(wavelengths)} bands but cube has "
            f"{n_bands}: {metadata_path} does not describe this cube"
        )

    return BandSet(wavelengths), metadata


def crop_and_rotate(
    array: np.ndarray,
    crop: Optional[Tuple[int, int, int, int]] = None,
    rotate_180: bool = True,
) -> np.ndarray:
    """Crop and optionally rotate 180 degrees.

    Parameters
    ----------
    array : np.ndarray
        2D or 3D array.
    crop : tuple of (row_min, row_max, col_min, col_max), optional
    rotate_180 : bool
    """
    if crop is not None:
        rmin, rmax, cmin, cmax = crop
        if array.ndim == 2:
            array = array[rmin:rmax, cmin:cmax]
        else:
            array = array[rmin:rmax, cmin:cmax, :]
    if rotate_180:
        array = np.rot90(array, 2)
    return array


def segment_shadow(
    cube: np.ndarray,
    percentile: int = DEFAULT_SHADOW_PERCENTILE,
    min_bands: int = DEFAULT_SHADOW_MIN_BANDS,
    closing_iter: int = DEFAULT_SHADOW_CLOSING,
    erosion_iter: int = DEFAULT_SHADOW_EROSION,
    dilation_iter: int = DEFAULT_SHADOW_DILATION,
) -> np.ndarray:
    """Detect shadow regions using multi-band percentile thresholding.

    A pixel is classified as shadow if its reflectance falls below the
    per-band percentile threshold in at least `min_bands` of the total bands.

    Parameters
    ----------
    cube : np.ndarray
        Reflectance cube, shape (rows, cols, bands).
    percentile : int
        Per-band percentile threshold.
    min_bands : int
        Minimum number of bands that must be below threshold.
    closing_iter, erosion_iter, dilation_iter : int
        Morphological operation iteration counts.

    Returns
    -------
    mask : np.ndarray
        Boolean mask, True = shadow.
    """
    nb = cube.shape[2]
    thresholds = np.percentile(cube, percentile, axis=(0, 1))
    below = cube < thresholds[np.newaxis, np.newaxis, :]
    n_below = below.sum(axis=2)
    mask = n_below >= min_bands

    if closing_iter > 0:
        mask = ndimage.binary_closing(mask, iterations=closing_iter)
    if erosion_iter > 0:
        mask = ndimage.binary_erosion(mask, iterations=erosion_iter)
    if dilation_iter > 0:
        mask = ndimage.binary_dilation(mask, iterations=dilation_iter)

    pct = 100 * mask.sum() / mask.size
    logger.info(
        "Shadow mask: %d pixels (%.1f%%), p%d, >=%d/%d bands",
        mask.sum(), pct, percentile, min_bands, nb,
    )
    return mask


def make_panel_mask(cube: np.ndarray, bands: BandSet) -> np.ndarray:
    """Panel mask from reflectance cube (R650 > 0.8)."""
    return cube[:, :, bands.index(650)] > 0.8


def percentile_stretch(
    data: np.ndarray,
    exclude_mask: np.ndarray,
    low_pct: float = 2.0,
    high_pct: float = 98.0,
) -> np.ndarray:
    """Stretch data to [0,1] using percentiles from non-excluded pixels.

    Parameters
    ----------
    data : np.ndarray
        2D or 3D input array.
    exclude_mask : np.ndarray
        Boolean mask of pixels to exclude from percentile computation.
    low_pct, high_pct : float
        Percentile bounds.
    """
    valid = ~exclude_mask
    if data.ndim == 3:
        result = np.zeros_like(data, dtype=np.float64)
        for i in range(data.shape[2]):
            ch = data[:, :, i]
            vmin = np.percentile(ch[valid], low_pct)
            vmax = np.percentile(ch[valid], high_pct)
            if vmax - vmin > 1e-10:
                result[:, :, i] = (ch - vmin) / (vmax - vmin)
        return np.clip(result, 0, 1)
    else:
        vmin = np.percentile(data[valid], low_pct)
        vmax = np.percentile(data[valid], high_pct)
        if vmax - vmin < 1e-10:
            return np.zeros_like(data, dtype=np.float64)
        return np.clip((data - vmin) / (vmax - vmin), 0, 1)


def save_jpg(array: np.ndarray, path: Path, quality: int = 92) -> None:
    """Save a float [0,1] or uint8 array as JPEG."""
    if array.dtype != np.uint8:
        array = (np.clip(array, 0, 1) * 255).astype(np.uint8)
    Image.fromarray(array).save(str(path), quality=quality)
    logger.info("Saved %s", path.name)


def apply_colormap(
    data: np.ndarray,
    shadow_mask: np.ndarray,
    panel_mask: np.ndarray,
    exclude_mask: np.ndarray,
    cmap_name: str,
    output_path: Path,
    low_pct: float = 2.0,
    high_pct: float = 98.0,
) -> None:
    """Apply percentile stretch + colormap, mask shadow/panel, save JPEG.

    Parameters
    ----------
    data : np.ndarray
        2D scalar field.
    shadow_mask, panel_mask : np.ndarray
        Region masks.
    exclude_mask : np.ndarray
        Pixels to exclude from percentile computation.
    cmap_name : str
        Matplotlib colormap name.
    output_path : Path
        JPEG output path.
    low_pct, high_pct : float
        Percentile stretch bounds.
    """
    from matplotlib import cm

    data = data.copy()
    data[shadow_mask] = 0
    data[panel_mask] = 0
    normed = percentile_stretch(data, exclude_mask, low_pct, high_pct)
    import matplotlib
    cmap = matplotlib.colormaps[cmap_name]
    colored = (cmap(normed)[:, :, :3] * 255).astype(np.uint8)
    colored[shadow_mask] = 30
    colored[panel_mask] = 200
    save_jpg(colored, output_path)


# ---------------------------------------------------------------------------
# Product generators
# ---------------------------------------------------------------------------

def generate_rgb_composite(
    cube: np.ndarray,
    bands: BandSet,
    shadow_mask: np.ndarray,
    panel_mask: np.ndarray,
    output_dir: Path,
) -> None:
    """White-balanced, pan-sharpened approximate RGB composite.

    Bands: 650nm (R), 550nm (G), 450nm (B). White balance divides each
    channel by the panel mean. Pan-sharpening uses Brovey transform with
    650nm as the luminance source (typically sharpest due to chromatic
    aberration).
    """
    red = cube[:, :, bands.index(650)].copy()
    grn = cube[:, :, bands.index(550)].copy()
    blu = cube[:, :, bands.index(450)].copy()

    for ch in [red, grn, blu]:
        panel_mean = ch[panel_mask].mean()
        if panel_mean > 0:
            ch /= panel_mean

    rgb = np.stack([red, grn, blu], axis=2)
    rgb_sum = np.maximum(rgb.sum(axis=2, keepdims=True), 1e-10)
    ratios = rgb / rgb_sum
    sharpened = ratios * red[:, :, np.newaxis] * 3.0

    exclude = shadow_mask | panel_mask
    stretched = percentile_stretch(sharpened, exclude, 1, 99)
    stretched[shadow_mask] = 0

    save_jpg(stretched, output_dir / "rgb_wb_sharp.jpg")


def generate_false_color_composites(
    cube: np.ndarray,
    bands: BandSet,
    panel_mask: np.ndarray,
    output_dir: Path,
) -> None:
    """False-color composites (unmasked, shadows visible).

    NIR-R-G: 850nm / 650nm / 550nm
    Deep NIR: 1100nm / 900nm / 750nm
    """
    exclude = panel_mask

    nir_r_g = np.stack([
        cube[:, :, bands.index(850)],
        cube[:, :, bands.index(650)],
        cube[:, :, bands.index(550)],
    ], axis=2)
    save_jpg(percentile_stretch(nir_r_g, exclude, 2, 98),
             output_dir / "false_color_NIR_R_G.jpg")

    deep = np.stack([
        cube[:, :, bands.index(1100)],
        cube[:, :, bands.index(900)],
        cube[:, :, bands.index(750)],
    ], axis=2)
    save_jpg(percentile_stretch(deep, exclude, 2, 98),
             output_dir / "false_color_deepNIR.jpg")


def generate_pca(
    cube: np.ndarray,
    shadow_mask: np.ndarray,
    panel_mask: np.ndarray,
    output_dir: Path,
) -> None:
    """PCA composites fitted on scene pixels only (no shadow, no panel).

    Per-channel percentile stretch, no gamma (PCA scores are linear).
    """
    h, w, nb = cube.shape
    exclude = shadow_mask | panel_mask

    scene_pixels = cube[~exclude].reshape(-1, nb)
    pca = PCA(n_components=min(6, nb))
    pca.fit(scene_pixels)

    scores = pca.transform(cube.reshape(-1, nb)).reshape(h, w, -1)

    logger.info(
        "PCA variance: %s",
        ", ".join(f"PC{i+1}={v:.3f}" for i, v in enumerate(pca.explained_variance_ratio_[:6])),
    )

    save_jpg(percentile_stretch(scores[:, :, :3], exclude, 2, 98),
             output_dir / "pca_PC123.jpg")

    if scores.shape[2] >= 4:
        save_jpg(percentile_stretch(scores[:, :, 1:4], exclude, 2, 98),
                 output_dir / "pca_PC234.jpg")


def generate_band_ratios(
    cube: np.ndarray,
    bands: BandSet,
    shadow_mask: np.ndarray,
    panel_mask: np.ndarray,
    output_dir: Path,
) -> None:
    """Band ratio indices.

    Iron oxide: R650 / R500 — ferric iron produces a red slope.
    NDVI-like: (R850 - R650) / (R850 + R650) — vegetation / red-edge index.
    """
    exclude = shadow_mask | panel_mask
    r650 = cube[:, :, bands.index(650)]
    r500 = cube[:, :, bands.index(500)]
    r850 = cube[:, :, bands.index(850)]

    iron = np.where(r500 > 0.01, r650 / r500, 0.0)
    apply_colormap(iron, shadow_mask, panel_mask, exclude, "RdYlBu_r",
                   output_dir / "index_iron_oxide.jpg")

    ndvi = np.where((r850 + r650) > 0.01, (r850 - r650) / (r850 + r650), 0.0)
    apply_colormap(ndvi, shadow_mask, panel_mask, exclude, "RdYlGn",
                   output_dir / "index_NDVI_like.jpg")


def generate_spectral_maps(
    cube: np.ndarray,
    bands: BandSet,
    shadow_mask: np.ndarray,
    panel_mask: np.ndarray,
    output_dir: Path,
) -> None:
    """Spectral Angle Mapper (SAM) and spectral variability.

    SAM reference: mean spectrum of moderate-reflectance (0.15-0.30 at
    650nm) scene pixels, approximating a spectrally neutral surface.
    """
    exclude = shadow_mask | panel_mask
    r650 = cube[:, :, bands.index(650)]

    neutral = ~shadow_mask & ~panel_mask & (r650 > 0.15) & (r650 < 0.30)
    if neutral.sum() < 100:
        neutral = ~shadow_mask & ~panel_mask & (r650 > 0.05)
    ref = cube[neutral].mean(axis=0)
    ref_norm = np.linalg.norm(ref)

    pixel_norms = np.linalg.norm(cube, axis=2)
    dot = (cube * ref[np.newaxis, np.newaxis, :]).sum(axis=2)
    cos_angle = np.clip(
        np.where(pixel_norms * ref_norm > 1e-10, dot / (pixel_norms * ref_norm), 1.0),
        -1, 1,
    )
    sam = np.arccos(cos_angle)
    apply_colormap(sam, shadow_mask, panel_mask, exclude, "viridis",
                   output_dir / "spectral_angle_map.jpg")

    spec_var = cube.std(axis=2)
    apply_colormap(spec_var, shadow_mask, panel_mask, exclude, "inferno",
                   output_dir / "spectral_variability.jpg")


def generate_hydration_maps(
    cube: np.ndarray,
    bands: BandSet,
    shadow_mask: np.ndarray,
    panel_mask: np.ndarray,
    output_dir: Path,
) -> None:
    """Band depth and absorption maps targeting the 975nm hydration feature.

    BD975 (continuum 900-1050nm):
      Continuum interpolated linearly from R900 to R1050.
      BD = 1 - R975 / R_continuum

    BD975 local (continuum from shoulders):
      R_continuum = (R950 + R1000) / 2
      BD = 1 - R975 / R_continuum

    Hydration slope (Pancam-style, Rice et al. 2010):
      slope = (R1000 - R900) / 100  [per nm]

    Integrated band depth (reflectance-weighted):
      Continuum from 950-1050nm. Band depths computed at 975nm and 1000nm.
      Both must be positive (absorption present at both wavelengths).
      Trapezoidal integration of the depth profile:
        area = 25 * BD975 + 37.5 * BD1000
      Weighted by mean reflectance of the four bands (950, 975, 1000, 1050)
      to suppress noisy detections in dark, low-SNR materials.
    """
    from matplotlib import cm

    exclude = shadow_mask | panel_mask

    R900 = cube[:, :, bands.index(900)]
    R950 = cube[:, :, bands.index(950)]
    R975 = cube[:, :, bands.index(975)]
    R1000 = cube[:, :, bands.index(1000)]
    R1050 = cube[:, :, bands.index(1050)]

    # --- BD975: continuum 900-1050nm ---
    frac = (975.0 - 900.0) / (1050.0 - 900.0)
    R_cont = R900 + frac * (R1050 - R900)
    bd975 = np.where(R_cont > 0.01, 1.0 - R975 / R_cont, 0.0)
    apply_colormap(bd975, shadow_mask, panel_mask, exclude, "RdYlBu_r",
                   output_dir / "band_depth_975nm.jpg")

    # --- BD975 local ---
    R_cont_local = (R950 + R1000) / 2.0
    bd975_local = np.where(R_cont_local > 0.01, 1.0 - R975 / R_cont_local, 0.0)
    apply_colormap(bd975_local, shadow_mask, panel_mask, exclude, "RdYlBu_r",
                   output_dir / "band_depth_975nm_local.jpg")

    # --- Hydration slope ---
    slope = (R1000 - R900) / 100.0
    apply_colormap(slope, shadow_mask, panel_mask, exclude, "RdYlBu_r",
                   output_dir / "hydration_slope_900_1000.jpg")

    # --- Integrated BD: reflectance-weighted ---
    frac_975 = (975.0 - 950.0) / (1050.0 - 950.0)
    frac_1000 = (1000.0 - 950.0) / (1050.0 - 950.0)
    Rcont_975 = R950 + frac_975 * (R1050 - R950)
    Rcont_1000 = R950 + frac_1000 * (R1050 - R950)

    BD975_int = np.where(Rcont_975 > 0.01, 1.0 - R975 / Rcont_975, 0.0)
    BD1000_int = np.where(Rcont_1000 > 0.01, 1.0 - R1000 / Rcont_1000, 0.0)

    both_positive = (BD975_int > 0) & (BD1000_int > 0)
    area = np.where(
        both_positive,
        25.0 * np.clip(BD975_int, 0, None) + 37.5 * np.clip(BD1000_int, 0, None),
        0.0,
    )

    R_mean = (R950 + R975 + R1000 + R1050) / 4.0
    area_weighted = area * R_mean

    valid = ~shadow_mask & (R950 > 0.01)
    area_weighted = np.where(valid, area_weighted, 0.0)
    area_weighted[panel_mask] = 0

    pos_scene = area_weighted[(~exclude) & (area_weighted > 0)]
    vmax = np.percentile(pos_scene, 98) if len(pos_scene) > 100 else 5.0
    area_norm = np.clip(area_weighted / vmax, 0, 1)
    area_norm[shadow_mask] = 0
    area_norm[panel_mask] = 0

    colored = (cm.magma(area_norm)[:, :, :3] * 255).astype(np.uint8)
    colored[shadow_mask] = 30
    colored[panel_mask] = 200
    save_jpg(colored, output_dir / "integrated_bd_975_1000.jpg")


def generate_olivine_maps(
    cube: np.ndarray,
    bands: BandSet,
    shadow_mask: np.ndarray,
    panel_mask: np.ndarray,
    output_dir: Path,
) -> None:
    """Olivine (forsterite) band depth map targeting the Fe2+ 1-micron absorption.

    Olivine produces a broad, asymmetric absorption centered near 1000nm
    from three overlapping Fe2+ crystal field bands in M1 and M2 octahedral
    sites (Sunshine & Pieters 1998). The reflectance peaks at 600-650nm,
    then decreases into a trough at 1000-1050nm.

    OBD1000 (Olivine Band Depth at 1000nm):
      Zero-slope continuum anchored at R650 (the olivine reflectance peak).
      TerraCam's 1100nm cutoff captures the absorption minimum but not
      the true reflectance recovery, so a sloped continuum would
      underestimate band depth. A flat continuum from the peak is the
      physically appropriate choice for an incompletely resolved feature.

      R_cont = R650
      OBD = 1 - R1000 / R650

    Reflectance-weighted by mean(R650, R850, R1000, R1100) to suppress
    noisy detections in dark, low-SNR materials.
    """
    from matplotlib import cm

    exclude = shadow_mask | panel_mask

    R650 = cube[:, :, bands.index(650)]
    R850 = cube[:, :, bands.index(850)]
    R1000 = cube[:, :, bands.index(1000)]
    R1100 = cube[:, :, bands.index(1100)]

    # Zero-slope continuum from R650 (olivine reflectance peak)
    obd = np.where(R650 > 0.01, 1.0 - R1000 / R650, 0.0)

    # Require positive absorption only
    obd = np.clip(obd, 0, None)

    # Reflectance-weight to favor light-toned, high-SNR materials
    R_mean = (R650 + R850 + R1000 + R1100) / 4.0
    obd_weighted = obd * R_mean

    valid = ~shadow_mask & (R650 > 0.01)
    obd_weighted = np.where(valid, obd_weighted, 0.0)
    obd_weighted[panel_mask] = 0

    pos_scene = obd_weighted[(~exclude) & (obd_weighted > 0)]
    vmax = np.percentile(pos_scene, 98) if len(pos_scene) > 100 else 1.0
    obd_norm = np.clip(obd_weighted / vmax, 0, 1)
    obd_norm[shadow_mask] = 0
    obd_norm[panel_mask] = 0

    colored = (cm.magma(obd_norm)[:, :, :3] * 255).astype(np.uint8)
    colored[shadow_mask] = 30
    colored[panel_mask] = 200
    save_jpg(colored, output_dir / "olivine_bd_1000nm.jpg")


def generate_roi_diagnostic(
    cube: np.ndarray,
    bands: BandSet,
    shadow_mask: np.ndarray,
    panel_mask: np.ndarray,
    output_dir: Path,
) -> None:
    """Diagnostic overlay: panel in green, shadow in blue."""
    rgb = np.stack([
        cube[:, :, bands.index(650)],
        cube[:, :, bands.index(550)],
        cube[:, :, bands.index(450)],
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

    save_jpg(rgb_8, output_dir / "roi_diagnostic.jpg")


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run_pipeline(args: argparse.Namespace) -> None:
    """Execute the derived product generation pipeline."""
    output_dir = Path(args.output_dir)
    crop = tuple(args.crop) if args.crop else DEFAULT_CROP

    logger.info("Loading reflectance cube: %s", args.cube)
    refl_cube = np.load(args.cube)
    logger.info("Full cube shape: %s", refl_cube.shape)

    cube_path = Path(args.cube)
    metadata_path = (
        Path(args.metadata) if args.metadata
        else cube_path.with_name(f"{cube_path.stem}_metadata.json")
    )
    bands, cube_meta = load_band_set(metadata_path, refl_cube.shape[2])
    logger.info(
        "Band wavelengths (nm): %s",
        ", ".join("clear" if wl is None else f"{wl:.0f}"
                  for wl in bands.wavelengths_nm),
    )

    # Resolve 180-degree de-rotation. --no-rotate (deprecated) and
    # --rotate on/off force it; otherwise 'auto' reads the cube's
    # sensor_inverted metadata (default True for legacy cubes without it).
    if args.no_rotate or args.rotate == "off":
        rotate_180 = False
    elif args.rotate == "on":
        rotate_180 = True
    else:  # auto
        rotate_180 = bool(cube_meta.get("sensor_inverted", True))

    logger.info("Crop %s, rotate_180=%s (source: %s)", crop, rotate_180,
                "flag" if (args.no_rotate or args.rotate != "auto")
                else "cube metadata")
    cube = crop_and_rotate(refl_cube, crop, rotate_180)
    h, w, nb = cube.shape
    logger.info("Working cube: %d x %d x %d bands", h, w, nb)

    panel_mask = make_panel_mask(cube, bands)
    shadow_mask = segment_shadow(
        cube,
        percentile=args.shadow_percentile,
        min_bands=args.shadow_min_bands,
    )

    products_dir = output_dir / "cropped"
    products_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Generating RGB composite")
    generate_rgb_composite(cube, bands, shadow_mask, panel_mask, products_dir)

    logger.info("Generating false-color composites")
    generate_false_color_composites(cube, bands, panel_mask, products_dir)

    logger.info("Generating PCA composites")
    generate_pca(cube, shadow_mask, panel_mask, products_dir)

    logger.info("Generating band ratio indices")
    generate_band_ratios(cube, bands, shadow_mask, panel_mask, products_dir)

    logger.info("Generating spectral maps")
    generate_spectral_maps(cube, bands, shadow_mask, panel_mask, products_dir)

    logger.info("Generating hydration / band-depth maps")
    generate_hydration_maps(cube, bands, shadow_mask, panel_mask, products_dir)

    logger.info("Generating olivine band-depth map")
    generate_olivine_maps(cube, bands, shadow_mask, panel_mask, products_dir)

    logger.info("Generating ROI diagnostic")
    generate_roi_diagnostic(cube, bands, shadow_mask, panel_mask, products_dir)

    logger.info("All products saved to %s", products_dir)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Generate derived analysis products from a calibrated "
                    "reflectance hypercube.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--cube", type=str, required=True,
        help="Path to reflectance cube (.npy), output of calibrate_reflectance.py.",
    )
    parser.add_argument(
        "--metadata", type=str, default=None,
        help="Path to the calibration metadata JSON with per-band "
             "wavelengths (default: <cube>_metadata.json next to the cube).",
    )
    parser.add_argument(
        "--output-dir", type=str, default="./out/derived",
        help="Output directory (default: ./out/derived).",
    )

    geom = parser.add_argument_group("Geometry")
    geom.add_argument(
        "--crop", type=int, nargs=4,
        metavar=("RMIN", "RMAX", "CMIN", "CMAX"),
        default=None,
        help="Crop bounds (default: %(default)s).",
    )
    geom.add_argument(
        "--rotate", choices=["auto", "on", "off"], default="auto",
        help="180-degree scene de-rotation. 'auto' (default) uses the "
             "cube's sensor_inverted metadata; 'on'/'off' force it.",
    )
    geom.add_argument(
        "--no-rotate", action="store_true",
        help="Deprecated alias for --rotate off.",
    )

    mask = parser.add_argument_group("Shadow mask")
    mask.add_argument(
        "--shadow-percentile", type=int,
        default=DEFAULT_SHADOW_PERCENTILE,
        help=f"Per-band percentile threshold (default: {DEFAULT_SHADOW_PERCENTILE}).",
    )
    mask.add_argument(
        "--shadow-min-bands", type=int,
        default=DEFAULT_SHADOW_MIN_BANDS,
        help=f"Minimum bands below threshold (default: {DEFAULT_SHADOW_MIN_BANDS}).",
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
    run_pipeline(args)


if __name__ == "__main__":
    main()
