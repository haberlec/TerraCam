"""
Unit tests for fli.geometry — pixel to az/el backplane computation.

Pins the frame conventions analytically: center pixel maps to exactly
(pan, tilt); small-angle pixel offsets; tilt cross-coupling (azimuth
scale grows as 1/cos(el)); roll flips; boresight lever-arm parallax at
finite scene range vanishing at infinity.
"""

import math

import numpy as np
import pytest

from fli.geometry import (
    CameraModel,
    MountModel,
    boresight_azel,
    camera_model_from_configs,
    mount_model_from_config,
    pixel_to_azel,
)

# Simple test camera: f = 1000 px exactly (10mm / 10um), 201x101 sensor
CAM = CameraModel(
    focal_length_mm=10.0, pixel_pitch_um=10.0, width_px=201, height_px=101
)
MOUNT = MountModel()


class TestCenterPixel:
    """Boresight maps to exactly (pan, tilt) with a zero-offset mount."""

    @pytest.mark.parametrize("pan,tilt", [
        (0.0, 0.0), (45.0, 0.0), (-30.0, 20.0), (170.0, -45.0),
    ])
    def test_center_equals_pan_tilt(self, pan, tilt):
        az, el = boresight_azel(CAM, MOUNT, pan, tilt)
        assert az == pytest.approx(pan, abs=1e-9)
        assert el == pytest.approx(tilt, abs=1e-9)

    def test_offset_irrelevant_at_infinity(self):
        mount = MountModel(camera_offset_m=(0.2, 0.05, -0.1))
        az, el = boresight_azel(CAM, mount, 30.0, 15.0)
        assert az == pytest.approx(30.0, abs=1e-9)
        assert el == pytest.approx(15.0, abs=1e-9)


class TestPixelOffsets:
    """Sign conventions and small-angle behavior at tilt = 0."""

    def test_column_offset_decreases_azimuth(self):
        # One pixel right of center: tan(offset) = 1/f_px = 1/1000
        az, el = pixel_to_azel(
            CAM, MOUNT, 0.0, 0.0,
            rows=np.array([CAM.cy]), cols=np.array([CAM.cx + 1.0]),
        )
        expected = -math.degrees(math.atan(1.0 / 1000.0))
        assert az[0, 0] == pytest.approx(expected, rel=1e-9)
        assert el[0, 0] == pytest.approx(0.0, abs=1e-9)

    def test_row_offset_decreases_elevation(self):
        az, el = pixel_to_azel(
            CAM, MOUNT, 0.0, 0.0,
            rows=np.array([CAM.cy + 1.0]), cols=np.array([CAM.cx]),
        )
        expected = -math.degrees(math.atan(1.0 / 1000.0))
        assert el[0, 0] == pytest.approx(expected, rel=1e-9)
        assert az[0, 0] == pytest.approx(0.0, abs=1e-9)

    def test_roll_180_flips_both_signs(self):
        cam = CameraModel(
            focal_length_mm=10.0, pixel_pitch_um=10.0,
            width_px=201, height_px=101, roll_deg=180.0,
        )
        az, el = pixel_to_azel(
            cam, MOUNT, 0.0, 0.0,
            rows=np.array([cam.cy + 1.0]), cols=np.array([cam.cx + 1.0]),
        )
        assert az[0, 0] > 0
        assert el[0, 0] > 0


class TestTiltCrossCoupling:
    """At elevation, a column offset spans more azimuth (~1/cos el)."""

    def test_azimuth_scale_at_60_degrees(self):
        offset_px = 10.0
        az0, _ = pixel_to_azel(
            CAM, MOUNT, 0.0, 0.0,
            rows=np.array([CAM.cy]), cols=np.array([CAM.cx + offset_px]),
        )
        az60, _ = pixel_to_azel(
            CAM, MOUNT, 0.0, 60.0,
            rows=np.array([CAM.cy]), cols=np.array([CAM.cx + offset_px]),
        )
        # cos(60 deg) = 0.5 -> azimuth offset doubles (to first order)
        ratio = az60[0, 0] / az0[0, 0]
        assert ratio == pytest.approx(2.0, rel=1e-3)


class TestBoresightAngularOffsets:
    """Fixed angular misalignments shift the boresight."""

    def test_offsets_shift_center_at_zero_tilt(self):
        mount = MountModel(
            boresight_pan_offset_deg=0.5, boresight_tilt_offset_deg=-0.25
        )
        az, el = boresight_azel(CAM, mount, 10.0, 0.0)
        assert az == pytest.approx(10.5, abs=1e-9)
        assert el == pytest.approx(-0.25, abs=1e-9)


class TestParallax:
    """Lever-arm parallax at finite scene range."""

    def test_lateral_offset_parallax(self):
        # Camera 10 cm along the tilt axis (+Y at pan=0), scene at 10 m:
        # boresight ray from (0, 0.1, 0) along +X hits the 10 m sphere at
        # x = sqrt(100 - 0.01); az = atan2(0.1, x)
        mount = MountModel(camera_offset_m=(0.0, 0.1, 0.0))
        az, el = boresight_azel(CAM, mount, 0.0, 0.0, scene_range_m=10.0)
        expected = math.degrees(math.atan2(0.1, math.sqrt(100.0 - 0.01)))
        assert az == pytest.approx(expected, rel=1e-9)
        assert el == pytest.approx(0.0, abs=1e-9)

    def test_forward_offset_no_boresight_shift(self):
        # Pupil forward along the boresight: the center ray stays on
        # axis regardless of range...
        mount = MountModel(camera_offset_m=(0.15, 0.0, 0.0))
        az, el = boresight_azel(CAM, mount, 25.0, 10.0, scene_range_m=50.0)
        assert az == pytest.approx(25.0, abs=1e-9)
        assert el == pytest.approx(10.0, abs=1e-9)

    def test_forward_offset_shifts_field_edge(self):
        # ...but off-axis pixels do move: this is the pan-axis parallax
        # the mosaic phase-correlation currently absorbs empirically.
        mount = MountModel(camera_offset_m=(0.15, 0.0, 0.0))
        col_edge = np.array([CAM.cx + 100.0])
        row_c = np.array([CAM.cy])
        az_inf, _ = pixel_to_azel(
            CAM, mount, 0.0, 0.0, rows=row_c, cols=col_edge
        )
        az_near, _ = pixel_to_azel(
            CAM, mount, 0.0, 0.0, rows=row_c, cols=col_edge,
            scene_range_m=20.0,
        )
        # Angular field offset ~5.7 deg; lever arm 0.15 m at 20 m range
        # shifts it by roughly offset*sin(theta)/range ~ 0.0428 deg
        shift = abs(az_near[0, 0] - az_inf[0, 0])
        assert shift == pytest.approx(0.0428, rel=0.05)

    def test_parallax_vanishes_with_range(self):
        mount = MountModel(camera_offset_m=(0.1, 0.1, 0.05))
        az_far, el_far = boresight_azel(
            CAM, mount, 12.0, 8.0, scene_range_m=1e7
        )
        assert az_far == pytest.approx(12.0, abs=1e-5)
        assert el_far == pytest.approx(8.0, abs=1e-5)

    def test_range_shorter_than_offset_raises(self):
        mount = MountModel(camera_offset_m=(0.5, 0.0, 0.0))
        with pytest.raises(ValueError, match="must exceed"):
            boresight_azel(CAM, mount, 0.0, 0.0, scene_range_m=0.3)


class TestGridComputation:
    """Full and subsampled grids are consistent."""

    def test_subgrid_matches_full(self):
        az_full, el_full = pixel_to_azel(CAM, MOUNT, 15.0, -10.0)
        rows = np.arange(0, CAM.height_px, 25)
        cols = np.arange(0, CAM.width_px, 25)
        az_sub, el_sub = pixel_to_azel(
            CAM, MOUNT, 15.0, -10.0, rows=rows, cols=cols
        )
        np.testing.assert_allclose(
            az_sub, az_full[np.ix_(rows, cols)], atol=1e-12
        )
        np.testing.assert_allclose(
            el_sub, el_full[np.ix_(rows, cols)], atol=1e-12
        )

    def test_full_grid_shape(self):
        az, el = pixel_to_azel(CAM, MOUNT, 0.0, 0.0)
        assert az.shape == (CAM.height_px, CAM.width_px)
        assert el.shape == az.shape


class TestMosaicParallaxEquivalence:
    """create_mosaic.py carries a self-contained copy of the boresight
    parallax formula (analysis scripts do not import the fli package);
    this test pins it to the library implementation so they cannot
    drift apart.
    """

    @pytest.fixture
    def create_mosaic(self):
        import sys
        from pathlib import Path
        repo_root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(repo_root / "scripts" / "analysis"))
        module = pytest.importorskip("create_mosaic")
        yield module
        module.configure_parallax((0.0, 0.0, 0.0), None)

    def test_matches_boresight_azel(self, create_mosaic):
        offset = (0.12, 0.08, -0.05)
        scene_range = 25.0
        create_mosaic.configure_parallax(offset, scene_range)
        mount = MountModel(camera_offset_m=offset)

        for pan, tilt in [(0.0, 0.0), (35.0, -20.0), (-120.0, 45.0),
                          (170.0, 5.0)]:
            az_m, el_m = create_mosaic.parallax_corrected_center(pan, tilt)
            az_g, el_g = boresight_azel(
                CAM, mount, pan, tilt, scene_range_m=scene_range
            )
            assert az_m == pytest.approx(az_g, abs=1e-9)
            assert el_m == pytest.approx(el_g, abs=1e-9)

    def test_identity_without_scene_range(self, create_mosaic):
        create_mosaic.configure_parallax((0.2, 0.0, 0.0), None)
        assert create_mosaic.parallax_corrected_center(12.5, -7.25) == (
            12.5, -7.25
        )


class TestConfigBuilders:
    """Model construction from the real project config files."""

    def test_camera_model_from_real_lens_config(self):
        import json
        with open("config/lens_specifications.json") as f:
            lens_config = json.load(f)
        cam = camera_model_from_configs(lens_config, "28mm", 2758, 2208)
        assert cam.focal_length_mm == pytest.approx(28.65)  # f_eff
        assert cam.pixel_pitch_um == pytest.approx(4.54)
        assert cam.width_px == 2758

    def test_mount_model_from_config_section(self):
        mount = mount_model_from_config({
            "camera_offset_m": {"forward": 0.12, "up": -0.03},
            "boresight_pan_offset_deg": 0.1,
        })
        assert mount.camera_offset_m == (0.12, 0.0, -0.03)
        assert mount.boresight_pan_offset_deg == 0.1

    def test_none_config_gives_zero_mount(self):
        mount = mount_model_from_config(None)
        assert mount.camera_offset_m == (0.0, 0.0, 0.0)
