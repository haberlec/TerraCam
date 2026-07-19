"""
Unit tests for the reflectance calibration band loading.

Validates that calibrate_reflectance.load_raw_images() assigns wavelengths
by filter position through the config mapping (position 1 = 400nm ...
position 16 = 1100nm), skips the clear position, and fails fast on
inconsistent inputs. This pins the fix for the historical +50nm-red band
mislabeling (see docs/hardening_plan.md, finding F1).
"""

import json
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "analysis"))

from band_mapping import load_filter_wavelengths
from calibrate_reflectance import load_raw_images

GROUND_TRUTH_NM = [
    400, 450, 500, 550, 600, 650, 700, 750,
    800, 850, 900, 950, 975, 1000, 1050, 1100,
]


def write_capture(directory: Path, name: str, filter_pos: int,
                  pixel_value: int, exposure_ms: int = 100) -> None:
    """Write a synthetic TIFF + metadata JSON capture pair."""
    image = np.full((8, 8), pixel_value, dtype=np.uint16)
    Image.fromarray(image).save(str(directory / f"{name}.tiff"))
    with open(directory / f"{name}.json", "w") as f:
        json.dump(
            {"filter_position": filter_pos, "exposure_ms": exposure_ms}, f
        )


@pytest.fixture
def filter_wavelengths():
    """Real position-to-wavelength mapping from the project config."""
    return load_filter_wavelengths()


class TestLoadRawImages:
    """Tests for wavelength assignment and band ordering."""

    def test_wavelengths_assigned_by_position(
        self, tmp_path, filter_wavelengths
    ):
        """Position N maps to the config wavelength, not the band index.

        Filenames are written in descending position order to verify that
        band order comes from filter position, not file name.
        """
        for pos in range(1, 17):
            write_capture(
                tmp_path, f"capture_{17 - pos:02d}", pos, pixel_value=pos
            )

        cube, band_metadata = load_raw_images(tmp_path, filter_wavelengths)

        assert cube.shape == (8, 8, 16)
        assert [b["filter_position"] for b in band_metadata] == (
            list(range(1, 17))
        )
        assert [b["wavelength_nm"] for b in band_metadata] == GROUND_TRUTH_NM
        # Cube band axis follows position order: band i holds position i+1
        for i in range(16):
            assert cube[0, 0, i] == i + 1

    def test_clear_position_skipped(self, tmp_path, filter_wavelengths):
        write_capture(tmp_path, "capture_clear", 0, pixel_value=999)
        write_capture(tmp_path, "capture_400", 1, pixel_value=1)

        cube, band_metadata = load_raw_images(tmp_path, filter_wavelengths)

        assert cube.shape[2] == 1
        assert band_metadata[0]["filter_position"] == 1
        assert band_metadata[0]["wavelength_nm"] == pytest.approx(400.0)

    def test_unknown_position_raises(self, tmp_path, filter_wavelengths):
        write_capture(tmp_path, "capture_bad", 17, pixel_value=1)

        with pytest.raises(ValueError, match="not.*defined"):
            load_raw_images(tmp_path, filter_wavelengths)

    def test_duplicate_position_raises(self, tmp_path, filter_wavelengths):
        write_capture(tmp_path, "capture_a", 5, pixel_value=1)
        write_capture(tmp_path, "capture_b", 5, pixel_value=2)

        with pytest.raises(ValueError, match="Duplicate"):
            load_raw_images(tmp_path, filter_wavelengths)

    def test_no_usable_captures_raises(self, tmp_path, filter_wavelengths):
        write_capture(tmp_path, "capture_clear", 0, pixel_value=1)

        with pytest.raises(ValueError, match="No usable"):
            load_raw_images(tmp_path, filter_wavelengths)

    def test_nested_acquisition_settings_format(
        self, tmp_path, filter_wavelengths
    ):
        """Coordinator missions nest fields under acquisition_settings."""
        image = np.full((8, 8), 42, dtype=np.uint16)
        Image.fromarray(image).save(str(tmp_path / "capture.tiff"))
        with open(tmp_path / "capture_metadata.json", "w") as f:
            json.dump({
                "image_info": {"filename": "capture"},
                "acquisition_settings": {
                    "exposure_time_ms": 76,
                    "filter_position": 13,
                    "ccd_temperature_c": -20.0,
                },
            }, f)

        cube, band_metadata = load_raw_images(tmp_path, filter_wavelengths)

        assert cube.shape == (8, 8, 1)
        assert band_metadata[0]["filter_position"] == 13
        assert band_metadata[0]["wavelength_nm"] == pytest.approx(975.0)
        assert band_metadata[0]["exposure_ms"] == 76
        assert band_metadata[0]["source_file"] == "capture"
