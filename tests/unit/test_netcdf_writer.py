"""
Unit tests for fli.io.netcdf module

Tests the MultispectralNetCDF writer without requiring camera hardware.
Uses synthetic image data to verify NetCDF file structure, variable
attributes, incremental band writing, and RGB preview generation.
"""

import pytest
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path

netCDF4 = pytest.importorskip("netCDF4")

from fli.io.netcdf import MultispectralNetCDF


# --- Fixtures ---

@pytest.fixture
def output_dir(tmp_path):
    """Provide a temporary output directory."""
    return tmp_path / "netcdf_out"


@pytest.fixture
def nc_path(output_dir):
    """Provide a path for a test NetCDF file."""
    return output_dir / "test_position.nc"


@pytest.fixture
def synthetic_image():
    """Create a synthetic uint16 image with realistic structure."""
    rng = np.random.default_rng(42)
    # Simulate bias + signal with some gradient
    bias = 500
    y, x = np.mgrid[0:64, 0:128]
    signal = (y * 100 + x * 50).astype(np.uint16)
    noise = rng.integers(0, 200, size=(64, 128), dtype=np.uint16)
    return (bias + signal + noise).astype(np.uint16)


# Ground-truth mapping (mirrors config/filter_specifications.json):
# position 0 is clear (no filter); the 16 bandpass filters occupy
# positions 1-16 in ascending wavelength order.
FILTER_WAVELENGTHS_BY_POSITION = {
    1: 400, 2: 450, 3: 500, 4: 550, 5: 600, 6: 650, 7: 700, 8: 750,
    9: 800, 10: 850, 11: 900, 12: 950, 13: 975, 14: 1000, 15: 1050,
    16: 1100,
}


@pytest.fixture
def filter_config():
    """Minimal filter config matching the real structure."""
    filters = [{
        "position": 0,
        "center_wavelength_nm": None,
        "description": "Clear (no filter)",
        "peak_transmission_percent": 100,
        "transmission_profile": None,
    }]
    for pos, wl in FILTER_WAVELENGTHS_BY_POSITION.items():
        filters.append({
            "position": pos,
            "center_wavelength_nm": wl,
            "description": f"Filter {wl}nm",
            "peak_transmission_percent": 95.0,
            "transmission_profile": {
                "wavelength_nm": [wl - 25, wl, wl + 25],
                "transmission_percent": [0, 95, 0],
            },
        })
    return {
        "filter_specifications": {
            "total_filters": 16,
            "filter_positions_total": 17,
            "manufacturer": "Edmund Optics",
            "type": "Hard Coated Bandpass Interference Filters",
            "fwhm_nm": 25,
            "blocking_od": 4.0,
            "filters": filters,
        }
    }


@pytest.fixture
def camera_config():
    """Minimal camera config."""
    return {
        "model": "MLx695",
        "manufacturer": "Finger Lakes Instrumentation",
        "sensor": {
            "type": "Sony ICX695 Interline transfer CCD",
            "pixel_size": {"width_um": 4.54, "height_um": 4.54},
            "full_well_capacity_electrons": 17000,
        },
        "interface": {"data_bit_depth": 16},
    }


@pytest.fixture
def base_kwargs(nc_path, filter_config, camera_config):
    """Common kwargs for MultispectralNetCDF constructor."""
    return dict(
        filepath=nc_path,
        position_id="grid_001",
        pan_degrees=5.0,
        tilt_degrees=-10.0,
        pan_steps=333,
        tilt_steps=-667,
        sequence_name="test_survey",
        expected_bands=16,
        ccd_temperature_c=-20.0,
        filter_config=filter_config,
        camera_config=camera_config,
        lens_config=None,
    )


# --- Tests ---

class TestFileStructure:
    """Tests for NetCDF file creation and structure."""

    def test_create_file(self, base_kwargs):
        """File is created on disk when writer is instantiated."""
        with MultispectralNetCDF(**base_kwargs) as writer:
            writer.add_band(
                np.zeros((64, 128), dtype=np.uint16),
                filter_position=0, exposure_ms=100,
                capture_time=datetime.now(),
            )
        assert base_kwargs["filepath"].exists()

    def test_creates_parent_directories(self, base_kwargs):
        """Writer creates parent directories if they don't exist."""
        base_kwargs["filepath"] = (
            base_kwargs["filepath"].parent / "sub" / "dir" / "test.nc"
        )
        with MultispectralNetCDF(**base_kwargs) as writer:
            writer.add_band(
                np.zeros((64, 128), dtype=np.uint16),
                filter_position=0, exposure_ms=100,
                capture_time=datetime.now(),
            )
        assert base_kwargs["filepath"].exists()

    def test_dimensions(self, base_kwargs, synthetic_image):
        """Verify dimension names and sizes."""
        with MultispectralNetCDF(**base_kwargs) as writer:
            writer.add_band(
                synthetic_image, filter_position=0,
                exposure_ms=100, capture_time=datetime.now(),
            )

        ds = netCDF4.Dataset(str(base_kwargs["filepath"]), 'r')
        try:
            assert "band" in ds.dimensions
            assert "y" in ds.dimensions
            assert "x" in ds.dimensions
            assert "rgb" in ds.dimensions
            assert ds.dimensions["band"].isunlimited()
            assert len(ds.dimensions["y"]) == synthetic_image.shape[0]
            assert len(ds.dimensions["x"]) == synthetic_image.shape[1]
            assert len(ds.dimensions["rgb"]) == 3
        finally:
            ds.close()

    def test_data_variables_exist(self, base_kwargs, synthetic_image):
        """Verify all expected variables are present."""
        with MultispectralNetCDF(**base_kwargs) as writer:
            writer.add_band(
                synthetic_image, filter_position=0,
                exposure_ms=100, capture_time=datetime.now(),
            )

        ds = netCDF4.Dataset(str(base_kwargs["filepath"]), 'r')
        try:
            expected_vars = [
                "digital_number", "rgb_preview",
                "wavelength", "bandwidth", "peak_transmission",
                "exposure_time", "capture_time", "filter_position",
                "image_min", "image_max", "image_mean", "image_std",
                "percentile_01", "percentile_05", "percentile_25",
                "percentile_50", "percentile_75", "percentile_95",
                "percentile_99",
            ]
            for var_name in expected_vars:
                assert var_name in ds.variables, (
                    f"Missing variable: {var_name}"
                )
        finally:
            ds.close()

    def test_cf_convention_attributes(self, base_kwargs, synthetic_image):
        """Verify CF Convention global attributes."""
        with MultispectralNetCDF(**base_kwargs) as writer:
            writer.add_band(
                synthetic_image, filter_position=0,
                exposure_ms=100, capture_time=datetime.now(),
            )

        ds = netCDF4.Dataset(str(base_kwargs["filepath"]), 'r')
        try:
            assert ds.Conventions == "CF-1.8"
            assert hasattr(ds, "title")
            assert hasattr(ds, "institution")
            assert hasattr(ds, "source")
            assert hasattr(ds, "history")
        finally:
            ds.close()


class TestGlobalAttributes:
    """Tests for global metadata attributes."""

    def test_position_metadata(self, base_kwargs, synthetic_image):
        """Verify position-related global attributes."""
        with MultispectralNetCDF(**base_kwargs) as writer:
            writer.add_band(
                synthetic_image, filter_position=0,
                exposure_ms=100, capture_time=datetime.now(),
            )

        ds = netCDF4.Dataset(str(base_kwargs["filepath"]), 'r')
        try:
            assert ds.position_id == "grid_001"
            assert float(ds.pan_degrees) == pytest.approx(5.0)
            assert float(ds.tilt_degrees) == pytest.approx(-10.0)
            assert int(ds.pan_steps) == 333
            assert int(ds.tilt_steps) == -667
            assert float(ds.ccd_temperature_c) == pytest.approx(-20.0)
        finally:
            ds.close()

    def test_sensor_metadata(self, base_kwargs, synthetic_image):
        """Verify sensor-related global attributes."""
        with MultispectralNetCDF(**base_kwargs) as writer:
            writer.add_band(
                synthetic_image, filter_position=0,
                exposure_ms=100, capture_time=datetime.now(),
            )

        ds = netCDF4.Dataset(str(base_kwargs["filepath"]), 'r')
        try:
            assert ds.sensor_model == "MLx695"
            assert float(ds.pixel_size_um) == pytest.approx(4.54)
            assert int(ds.full_well_capacity) == 17000
            assert int(ds.data_bit_depth) == 16
        finally:
            ds.close()

    def test_geo_pointing_attributes(self, base_kwargs, synthetic_image):
        """Verify GPM geo-pointing attributes when provided."""
        base_kwargs["geo_pointing"] = {
            "gps_position": {
                "latitude": 35.0, "longitude": -120.0, "altitude": 100.0
            },
            "mounting_attitude": {
                "roll": 1.0, "pitch": 2.0, "yaw": 3.0
            },
            "calibration_quality": "excellent",
        }

        with MultispectralNetCDF(**base_kwargs) as writer:
            writer.add_band(
                synthetic_image, filter_position=0,
                exposure_ms=100, capture_time=datetime.now(),
            )

        ds = netCDF4.Dataset(str(base_kwargs["filepath"]), 'r')
        try:
            assert float(ds.gps_latitude) == pytest.approx(35.0)
            assert float(ds.gps_longitude) == pytest.approx(-120.0)
            assert float(ds.gps_altitude) == pytest.approx(100.0)
            assert float(ds.mounting_roll) == pytest.approx(1.0)
            assert ds.calibration_quality == "excellent"
        finally:
            ds.close()

    def test_no_geo_pointing(self, base_kwargs, synthetic_image):
        """File is valid without GPM geo-pointing data."""
        base_kwargs["geo_pointing"] = None

        with MultispectralNetCDF(**base_kwargs) as writer:
            writer.add_band(
                synthetic_image, filter_position=0,
                exposure_ms=100, capture_time=datetime.now(),
            )

        ds = netCDF4.Dataset(str(base_kwargs["filepath"]), 'r')
        try:
            assert not hasattr(ds, "gps_latitude")
        finally:
            ds.close()


class TestBandWriting:
    """Tests for incremental band addition."""

    def test_single_band(self, base_kwargs, synthetic_image):
        """Write a single band and verify data integrity."""
        now = datetime.now()
        with MultispectralNetCDF(**base_kwargs) as writer:
            writer.add_band(
                synthetic_image, filter_position=1,
                exposure_ms=100, capture_time=now,
            )

        ds = netCDF4.Dataset(str(base_kwargs["filepath"]), 'r')
        try:
            assert len(ds.dimensions["band"]) == 1
            data = ds.variables["digital_number"][0, :, :]
            np.testing.assert_array_equal(data, synthetic_image)
            assert float(ds.variables["wavelength"][0]) == pytest.approx(400.0)
            assert float(ds.variables["bandwidth"][0]) == pytest.approx(25.0)
            assert int(ds.variables["filter_position"][0]) == 1
            assert float(ds.variables["exposure_time"][0]) == pytest.approx(100.0)
        finally:
            ds.close()

    def test_multiple_bands(self, base_kwargs, synthetic_image):
        """Write multiple bands and verify the band dimension grows."""
        now = datetime.now()
        n_bands = 5
        with MultispectralNetCDF(**base_kwargs) as writer:
            for i in range(n_bands):
                writer.add_band(
                    synthetic_image + i * 100,
                    filter_position=i + 1,
                    exposure_ms=100 + i * 10,
                    capture_time=now + timedelta(seconds=i * 3),
                )

        ds = netCDF4.Dataset(str(base_kwargs["filepath"]), 'r')
        try:
            assert len(ds.dimensions["band"]) == n_bands
            # Verify each band's wavelength
            wavelengths = ds.variables["wavelength"][:]
            expected_wl = [400, 450, 500, 550, 600]
            for i, wl in enumerate(expected_wl):
                assert float(wavelengths[i]) == pytest.approx(wl)
            # Verify bands_captured updated
            assert int(ds.bands_captured) == n_bands
        finally:
            ds.close()

    def test_all_16_filters(self, base_kwargs, synthetic_image):
        """Write all 16 bandpass filters (positions 1-16)."""
        now = datetime.now()
        with MultispectralNetCDF(**base_kwargs) as writer:
            for i, pos in enumerate(FILTER_WAVELENGTHS_BY_POSITION):
                writer.add_band(
                    synthetic_image,
                    filter_position=pos,
                    exposure_ms=100,
                    capture_time=now + timedelta(seconds=i * 3),
                )

        ds = netCDF4.Dataset(str(base_kwargs["filepath"]), 'r')
        try:
            assert len(ds.dimensions["band"]) == 16
            assert int(ds.bands_captured) == 16
        finally:
            ds.close()

    def test_image_shape_mismatch_raises(self, base_kwargs):
        """Adding a band with different dimensions raises ValueError."""
        writer = MultispectralNetCDF(**base_kwargs)
        try:
            writer.add_band(
                np.zeros((64, 128), dtype=np.uint16),
                filter_position=0, exposure_ms=100,
                capture_time=datetime.now(),
            )
            with pytest.raises(ValueError, match="does not match"):
                writer.add_band(
                    np.zeros((32, 64), dtype=np.uint16),
                    filter_position=1, exposure_ms=100,
                    capture_time=datetime.now(),
                )
        finally:
            writer.close()


class TestFilterMapping:
    """Pin the filter position to wavelength mapping.

    Ground truth: position 0 is clear (no filter); bandpass filters occupy
    positions 1-16 in ascending wavelength order (400nm at position 1,
    975nm at position 13, 1100nm at position 16). A regression here means
    band wavelength labels are wrong in every downstream product.
    """

    @pytest.mark.parametrize("position,expected_nm", [
        (1, 400.0), (13, 975.0), (16, 1100.0),
    ])
    def test_position_wavelengths(
        self, base_kwargs, synthetic_image, position, expected_nm
    ):
        """Filter positions map to their ground-truth wavelengths."""
        with MultispectralNetCDF(**base_kwargs) as writer:
            writer.add_band(
                synthetic_image, filter_position=position,
                exposure_ms=100, capture_time=datetime.now(),
            )

        ds = netCDF4.Dataset(str(base_kwargs["filepath"]), 'r')
        try:
            assert float(ds.variables["wavelength"][0]) == pytest.approx(
                expected_nm
            )
            assert int(ds.variables["filter_position"][0]) == position
        finally:
            ds.close()

    def test_clear_position_writes_nan(self, base_kwargs, synthetic_image):
        """Clear position 0 gets NaN wavelength and bandwidth, not 0.0."""
        with MultispectralNetCDF(**base_kwargs) as writer:
            writer.add_band(
                synthetic_image, filter_position=0,
                exposure_ms=100, capture_time=datetime.now(),
            )

        ds = netCDF4.Dataset(str(base_kwargs["filepath"]), 'r')
        try:
            assert np.isnan(float(ds.variables["wavelength"][0]))
            assert np.isnan(float(ds.variables["bandwidth"][0]))
            assert float(
                ds.variables["peak_transmission"][0]
            ) == pytest.approx(1.0)
            assert int(ds.variables["filter_position"][0]) == 0
        finally:
            ds.close()

    def test_unknown_position_raises(self, base_kwargs, synthetic_image):
        """A position absent from the config is rejected, not mislabeled."""
        with pytest.raises(ValueError, match="not defined"):
            with MultispectralNetCDF(**base_kwargs) as writer:
                writer.add_band(
                    synthetic_image, filter_position=17,
                    exposure_ms=100, capture_time=datetime.now(),
                )

    def test_filter_position_valid_range(self, base_kwargs, synthetic_image):
        """filter_position valid_range covers positions 0-16."""
        with MultispectralNetCDF(**base_kwargs) as writer:
            writer.add_band(
                synthetic_image, filter_position=16,
                exposure_ms=100, capture_time=datetime.now(),
            )

        ds = netCDF4.Dataset(str(base_kwargs["filepath"]), 'r')
        try:
            valid_range = ds.variables["filter_position"].valid_range
            assert list(valid_range) == [0, 16]
        finally:
            ds.close()

    def test_clear_band_never_selected_for_rgb_preview(
        self, base_kwargs, synthetic_image
    ):
        """RGB preview ignores the NaN-wavelength clear band."""
        now = datetime.now()
        with MultispectralNetCDF(**base_kwargs) as writer:
            # Clear first, then 400-650nm (positions 1-6)
            writer.add_band(
                synthetic_image, filter_position=0,
                exposure_ms=100, capture_time=now,
            )
            for i in range(6):
                writer.add_band(
                    synthetic_image + i * 500,
                    filter_position=i + 1,
                    exposure_ms=100,
                    capture_time=now + timedelta(seconds=i + 1),
                )

        ds = netCDF4.Dataset(str(base_kwargs["filepath"]), 'r')
        try:
            rgb = np.asarray(ds.variables["rgb_preview"][:])
            assert rgb.shape == (3, 64, 128)
            assert np.sum(rgb) > 0
        finally:
            ds.close()


class TestStatistics:
    """Tests for per-band image statistics."""

    def test_statistics_values(self, base_kwargs):
        """Verify computed statistics are correct."""
        image = np.array([[100, 200], [300, 400]], dtype=np.uint16)
        with MultispectralNetCDF(**base_kwargs) as writer:
            writer.add_band(
                image, filter_position=0,
                exposure_ms=100, capture_time=datetime.now(),
            )

        ds = netCDF4.Dataset(str(base_kwargs["filepath"]), 'r')
        try:
            assert float(ds.variables["image_min"][0]) == pytest.approx(100.0)
            assert float(ds.variables["image_max"][0]) == pytest.approx(400.0)
            assert float(ds.variables["image_mean"][0]) == pytest.approx(250.0)
        finally:
            ds.close()


class TestCompression:
    """Tests for zlib compression."""

    def test_compression_enabled(self, base_kwargs, synthetic_image):
        """Verify digital_number uses zlib compression."""
        with MultispectralNetCDF(**base_kwargs) as writer:
            writer.add_band(
                synthetic_image, filter_position=0,
                exposure_ms=100, capture_time=datetime.now(),
            )

        ds = netCDF4.Dataset(str(base_kwargs["filepath"]), 'r')
        try:
            dn = ds.variables["digital_number"]
            filters = dn.filters()
            assert filters["zlib"] is True
            assert filters["complevel"] == 4
        finally:
            ds.close()

    def test_custom_compression_level(self, base_kwargs, synthetic_image):
        """Verify custom compression level is applied."""
        base_kwargs["compression_level"] = 9
        with MultispectralNetCDF(**base_kwargs) as writer:
            writer.add_band(
                synthetic_image, filter_position=0,
                exposure_ms=100, capture_time=datetime.now(),
            )

        ds = netCDF4.Dataset(str(base_kwargs["filepath"]), 'r')
        try:
            filters = ds.variables["digital_number"].filters()
            assert filters["complevel"] == 9
        finally:
            ds.close()


class TestRGBPreview:
    """Tests for the false-color RGB composite."""

    def test_rgb_preview_created(self, base_kwargs, synthetic_image):
        """Verify RGB preview is generated on finalize."""
        # Write bands that include R(650), G(550), B(450)
        now = datetime.now()
        with MultispectralNetCDF(**base_kwargs) as writer:
            for i in range(6):  # Positions 1-6: 400..650nm
                writer.add_band(
                    synthetic_image + i * 500,
                    filter_position=i + 1,
                    exposure_ms=100,
                    capture_time=now + timedelta(seconds=i),
                )

        ds = netCDF4.Dataset(str(base_kwargs["filepath"]), 'r')
        try:
            rgb = np.asarray(ds.variables["rgb_preview"][:])
            assert rgb.shape == (3, 64, 128)
            assert rgb.dtype == np.uint8
            # Should not be all zeros (synthetic_image has variation)
            assert np.sum(rgb) > 0
        finally:
            ds.close()

    def test_rgb_preview_values_in_range(self, base_kwargs, synthetic_image):
        """Verify RGB preview pixel values are in [0, 255]."""
        now = datetime.now()
        with MultispectralNetCDF(**base_kwargs) as writer:
            for i in range(6):
                writer.add_band(
                    synthetic_image + i * 500,
                    filter_position=i + 1,
                    exposure_ms=100,
                    capture_time=now + timedelta(seconds=i),
                )

        ds = netCDF4.Dataset(str(base_kwargs["filepath"]), 'r')
        try:
            rgb = ds.variables["rgb_preview"][:]
            assert np.min(rgb) >= 0
            assert np.max(rgb) <= 255
        finally:
            ds.close()


class TestPartialCapture:
    """Tests for robustness to partial captures."""

    def test_partial_capture_valid(self, base_kwargs, synthetic_image):
        """File with fewer bands than expected is still valid."""
        base_kwargs["expected_bands"] = 16
        now = datetime.now()
        with MultispectralNetCDF(**base_kwargs) as writer:
            for i in range(3):  # Only 3 of 16
                writer.add_band(
                    synthetic_image,
                    filter_position=i + 1,
                    exposure_ms=100,
                    capture_time=now + timedelta(seconds=i),
                )

        ds = netCDF4.Dataset(str(base_kwargs["filepath"]), 'r')
        try:
            assert len(ds.dimensions["band"]) == 3
            assert int(ds.bands_captured) == 3
        finally:
            ds.close()

    def test_zero_bands_on_error(self, base_kwargs):
        """File created but no bands written (error path) is valid."""
        writer = MultispectralNetCDF(**base_kwargs)
        writer.close()  # Close without adding any bands

        # File should exist but have no band dimension created
        assert base_kwargs["filepath"].exists()


class TestContextManager:
    """Tests for context manager behavior."""

    def test_context_manager_finalizes(self, base_kwargs, synthetic_image):
        """Context manager calls finalize on clean exit."""
        with MultispectralNetCDF(**base_kwargs) as writer:
            writer.add_band(
                synthetic_image, filter_position=0,
                exposure_ms=100, capture_time=datetime.now(),
            )

        # File should be readable after context exit
        ds = netCDF4.Dataset(str(base_kwargs["filepath"]), 'r')
        try:
            assert int(ds.bands_captured) == 1
        finally:
            ds.close()

    def test_context_manager_closes_on_error(self, base_kwargs, synthetic_image):
        """Context manager closes (not finalizes) on exception."""
        with pytest.raises(RuntimeError):
            with MultispectralNetCDF(**base_kwargs) as writer:
                writer.add_band(
                    synthetic_image, filter_position=0,
                    exposure_ms=100, capture_time=datetime.now(),
                )
                raise RuntimeError("Simulated capture failure")

        # File should still exist and be readable
        assert base_kwargs["filepath"].exists()

    def test_add_band_after_close_raises(self, base_kwargs, synthetic_image):
        """Adding a band after close raises RuntimeError."""
        writer = MultispectralNetCDF(**base_kwargs)
        writer.close()

        with pytest.raises(RuntimeError, match="closed"):
            writer.add_band(
                synthetic_image, filter_position=0,
                exposure_ms=100, capture_time=datetime.now(),
            )


class TestBackplanes:
    """Az/el pointing backplanes written on finalize."""

    LENS_CONFIG = {
        "sensor": {"pixel_size_um": 10.0},
        "lenses": {"test": {"focal_length_mm": 10.0}},
    }
    MOUNT_CONFIG = {
        "camera_offset_m": {"forward": 0.0, "along_tilt_axis": 0.1,
                            "up": 0.0},
    }

    def _write(self, base_kwargs, synthetic_image, **extra):
        base_kwargs.update(
            lens_config=self.LENS_CONFIG, lens_id="test",
            pan_degrees=20.0, tilt_degrees=-15.0,
        )
        base_kwargs.update(extra)
        with MultispectralNetCDF(**base_kwargs) as writer:
            writer.add_band(
                synthetic_image, filter_position=1,
                exposure_ms=100, capture_time=datetime.now(),
            )
        return netCDF4.Dataset(str(base_kwargs["filepath"]), 'r')

    def test_subsampled_backplanes_match_geometry(
        self, base_kwargs, synthetic_image
    ):
        """Written values equal fli.geometry at the stored node grid."""
        from fli.geometry import (
            CameraModel, MountModel, pixel_to_azel
        )
        ds = self._write(base_kwargs, synthetic_image)
        try:
            rows = np.asarray(ds.variables["backplane_y"][:])
            cols = np.asarray(ds.variables["backplane_x"][:])
            # Grid covers the image edges
            assert rows[0] == 0 and rows[-1] == 63
            assert cols[0] == 0 and cols[-1] == 127

            camera = CameraModel(
                focal_length_mm=10.0, pixel_pitch_um=10.0,
                width_px=128, height_px=64,
            )
            az_expected, el_expected = pixel_to_azel(
                camera, MountModel(), 20.0, -15.0, rows=rows, cols=cols
            )
            np.testing.assert_allclose(
                np.asarray(ds.variables["backplane_azimuth"][:]),
                az_expected, atol=1e-4,
            )
            np.testing.assert_allclose(
                np.asarray(ds.variables["backplane_elevation"][:]),
                el_expected, atol=1e-4,
            )
        finally:
            ds.close()

    def test_none_mode_omits_backplanes(self, base_kwargs, synthetic_image):
        ds = self._write(base_kwargs, synthetic_image, backplanes="none")
        try:
            assert "backplane_azimuth" not in ds.variables
        finally:
            ds.close()

    def test_missing_lens_config_skips(self, base_kwargs, synthetic_image):
        base_kwargs["pan_degrees"] = 20.0
        with MultispectralNetCDF(**base_kwargs) as writer:  # lens_config=None
            writer.add_band(
                synthetic_image, filter_position=1,
                exposure_ms=100, capture_time=datetime.now(),
            )
        ds = netCDF4.Dataset(str(base_kwargs["filepath"]), 'r')
        try:
            assert "backplane_azimuth" not in ds.variables
        finally:
            ds.close()

    def test_actual_angles_preferred(self, base_kwargs, synthetic_image):
        """Encoder-derived pointing wins over the commanded angles."""
        ds = self._write(
            base_kwargs, synthetic_image,
            actual_pan_degrees=20.5, actual_tilt_degrees=-15.2,
        )
        try:
            az_var = ds.variables["backplane_azimuth"]
            assert float(az_var.pointing_pan_degrees) == pytest.approx(20.5)
            assert float(ds.actual_pan_degrees) == pytest.approx(20.5)
        finally:
            ds.close()

    def test_finite_range_parallax_applied(
        self, base_kwargs, synthetic_image
    ):
        """A lateral lever arm at finite range shifts the azimuth."""
        ds = self._write(
            base_kwargs, synthetic_image,
            mount_config=self.MOUNT_CONFIG, scene_range_m=10.0,
        )
        try:
            az_var = ds.variables["backplane_azimuth"]
            assert float(az_var.scene_range_m) == pytest.approx(10.0)
            az = np.asarray(az_var[:])
            # 0.1 m lateral offset at 10 m: ~0.57 deg azimuth parallax
            center = az[len(az) // 2, az.shape[1] // 2]
            assert abs(center - 20.0) == pytest.approx(0.573, abs=0.05)
        finally:
            ds.close()


class TestCaptureTime:
    """Tests for capture time tracking."""

    def test_total_capture_time(self, base_kwargs, synthetic_image):
        """Verify total_capture_time_s is computed correctly."""
        t0 = datetime(2026, 3, 11, 12, 0, 0)
        with MultispectralNetCDF(**base_kwargs) as writer:
            writer.add_band(
                synthetic_image, filter_position=0,
                exposure_ms=100, capture_time=t0,
            )
            writer.add_band(
                synthetic_image, filter_position=1,
                exposure_ms=100,
                capture_time=t0 + timedelta(seconds=10),
            )

        ds = netCDF4.Dataset(str(base_kwargs["filepath"]), 'r')
        try:
            assert float(ds.total_capture_time_s) == pytest.approx(10.0)
        finally:
            ds.close()
