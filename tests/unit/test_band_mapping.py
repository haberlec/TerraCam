"""
Unit tests for scripts/analysis/band_mapping.py

Pins the filter position to wavelength mapping against the real project
config. Ground truth: position 0 is clear (no filter); the 16 bandpass
filters occupy positions 1-16 in ascending wavelength order (400nm at
position 1, 975nm at position 13, 1100nm at position 16).
"""

import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "analysis"))

from band_mapping import (
    BandSet,
    load_filter_wavelengths,
    nearest_band_index,
)


class TestLoadFilterWavelengths:
    """Tests against the real config/filter_specifications.json."""

    def test_clear_position_is_none(self):
        mapping = load_filter_wavelengths()
        assert mapping[0] is None

    def test_ground_truth_positions(self):
        """Position 1 = 400nm, 13 = 975nm, 16 = 1100nm."""
        mapping = load_filter_wavelengths()
        assert mapping[1] == pytest.approx(400.0)
        assert mapping[13] == pytest.approx(975.0)
        assert mapping[16] == pytest.approx(1100.0)

    def test_seventeen_positions_total(self):
        """Clear + 16 bandpass filters = 17 defined positions."""
        mapping = load_filter_wavelengths()
        assert sorted(mapping) == list(range(17))

    def test_bandpass_wavelengths_ascending(self):
        mapping = load_filter_wavelengths()
        wavelengths = [mapping[pos] for pos in range(1, 17)]
        assert wavelengths == sorted(wavelengths)
        assert wavelengths[0] == pytest.approx(400.0)
        assert wavelengths[-1] == pytest.approx(1100.0)

    def test_missing_config_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_filter_wavelengths(tmp_path / "nonexistent.json")

    def test_terracam_config_env_override(self, monkeypatch, tmp_path):
        import json
        spec = {"filter_specifications": {"filters": [
            {"position": 0, "center_wavelength_nm": None},
            {"position": 1, "center_wavelength_nm": 500},
        ]}}
        (tmp_path / "filter_specifications.json").write_text(json.dumps(spec))
        monkeypatch.setenv("TERRACAM_CONFIG", str(tmp_path))

        mapping = load_filter_wavelengths()
        assert mapping == {0: None, 1: 500.0}


class TestNearestBandIndex:
    """Tests for nearest-wavelength band lookup."""

    WAVELENGTHS = [400.0, 450.0, 500.0, 950.0, 975.0, 1000.0]

    def test_exact_match(self):
        assert nearest_band_index(self.WAVELENGTHS, 975.0) == 4

    def test_nearest_within_tolerance(self):
        assert nearest_band_index(self.WAVELENGTHS, 445.0) == 1

    def test_outside_tolerance_raises(self):
        with pytest.raises(ValueError, match="No band within"):
            nearest_band_index(self.WAVELENGTHS, 700.0)

    def test_none_entries_never_selected(self):
        """The clear position (None) is skipped even for a 0nm target."""
        wavelengths = [None, 400.0, 450.0]
        assert nearest_band_index(wavelengths, 400.0) == 1

    def test_nan_entries_never_selected(self):
        wavelengths = [np.nan, 400.0, 450.0]
        assert nearest_band_index(wavelengths, 400.0) == 1

    def test_all_none_raises(self):
        with pytest.raises(ValueError, match="No band with a finite"):
            nearest_band_index([None, None], 400.0)


class TestBandSet:
    """Tests for the BandSet wrapper."""

    def test_index_lookup(self):
        bands = BandSet([400.0, 450.0, 500.0])
        assert bands.index(450.0) == 1
        assert len(bands) == 3

    def test_tolerance_respected(self):
        bands = BandSet([400.0, 500.0], tolerance_nm=10.0)
        with pytest.raises(ValueError):
            bands.index(430.0)
