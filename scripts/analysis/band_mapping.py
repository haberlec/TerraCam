"""
Filter Band Mapping

Shared utilities for mapping filter wheel positions and band wavelengths
in the analysis scripts. The single source of truth for the
position-to-wavelength mapping is ``config/filter_specifications.json``:
position 0 is the clear (unfiltered) path, and the 16 bandpass filters
occupy positions 1-16 in ascending wavelength order (400-1100nm).

Analysis code must never hardcode a wavelength list; it either loads the
config (when only filter positions are available, e.g. raw capture
metadata) or reads per-band wavelengths from file metadata written at
capture/calibration time.
"""

import json
import os
from pathlib import Path
from typing import Dict, Optional, Sequence, Union

import numpy as np


def _default_config_path() -> Path:
    """Resolve the filter specifications path.

    Honors the ``TERRACAM_CONFIG`` environment variable; otherwise uses
    the repo-relative ``config/`` directory. (Kept self-contained — the
    analysis scripts deliberately do not depend on the fli package.)
    """
    env_dir = os.environ.get("TERRACAM_CONFIG")
    if env_dir:
        return Path(env_dir) / "filter_specifications.json"
    return (
        Path(__file__).resolve().parents[2] / "config" /
        "filter_specifications.json"
    )


def load_filter_wavelengths(
    config_path: Optional[Union[str, Path]] = None,
) -> Dict[int, Optional[float]]:
    """Load the filter position to center wavelength mapping.

    Parameters
    ----------
    config_path : str or Path, optional
        Path to ``filter_specifications.json``. Defaults to the project
        config directory relative to this module.

    Returns
    -------
    dict
        Mapping of filter wheel position (int) to center wavelength in nm
        (float), or None for the clear (unfiltered) position.

    Raises
    ------
    FileNotFoundError
        If the config file does not exist.
    KeyError
        If the config file lacks the expected structure.
    """
    path = (
        Path(config_path) if config_path is not None
        else _default_config_path()
    )
    if not path.exists():
        raise FileNotFoundError(
            f"Filter specifications config not found: {path}"
        )

    with open(path) as f:
        config = json.load(f)

    filters = config["filter_specifications"]["filters"]
    mapping: Dict[int, Optional[float]] = {}
    for filt in filters:
        wl = filt["center_wavelength_nm"]
        mapping[int(filt["position"])] = float(wl) if wl is not None else None
    return mapping


def nearest_band_index(
    wavelengths_nm: Sequence[Optional[float]],
    target_nm: float,
    tolerance_nm: float = 15.0,
) -> int:
    """Return the band index whose wavelength is nearest to a target.

    Entries that are None or NaN (e.g. the clear position) are never
    selected.

    Parameters
    ----------
    wavelengths_nm : sequence of float or None
        Per-band center wavelengths, ordered along the cube band axis.
    target_nm : float
        Requested wavelength in nm.
    tolerance_nm : float
        Maximum allowed |band wavelength - target| in nm. The default of
        15nm is below half the minimum filter spacing (25nm between the
        950/975/1000nm filters), so a match is always unambiguous.

    Returns
    -------
    int
        Index into the band axis.

    Raises
    ------
    ValueError
        If no band lies within ``tolerance_nm`` of the target.
    """
    arr = np.array(
        [np.nan if wl is None else wl for wl in wavelengths_nm],
        dtype=np.float64,
    )
    finite = np.isfinite(arr)
    if not finite.any():
        raise ValueError(
            f"No band with a finite wavelength available "
            f"(requested {target_nm}nm)"
        )

    distances = np.where(finite, np.abs(arr - target_nm), np.inf)
    idx = int(np.argmin(distances))
    if distances[idx] > tolerance_nm:
        available = ", ".join(f"{wl:.0f}" for wl in arr[finite])
        raise ValueError(
            f"No band within {tolerance_nm}nm of {target_nm}nm "
            f"(nearest: {arr[idx]:.0f}nm; available: {available})"
        )
    return idx


class BandSet:
    """Wavelength-indexed view of a reflectance cube's band axis.

    Parameters
    ----------
    wavelengths_nm : sequence of float or None
        Per-band center wavelengths, ordered along the cube band axis.
        None/NaN entries (clear position) are allowed but never matched.
    tolerance_nm : float
        Maximum wavelength mismatch accepted by :meth:`index`.
    """

    def __init__(
        self,
        wavelengths_nm: Sequence[Optional[float]],
        tolerance_nm: float = 15.0,
    ):
        self.wavelengths_nm = list(wavelengths_nm)
        self.tolerance_nm = tolerance_nm

    def __len__(self) -> int:
        return len(self.wavelengths_nm)

    def index(self, target_nm: float) -> int:
        """Return the band index nearest to ``target_nm``.

        Raises
        ------
        ValueError
            If no band lies within the tolerance of the target.
        """
        return nearest_band_index(
            self.wavelengths_nm, target_nm, self.tolerance_nm
        )
