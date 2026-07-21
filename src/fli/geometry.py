"""
Camera-on-Gimbal Pointing Geometry

Maps image pixels to azimuth/elevation directions in the PTU's reference
frame, including the boresight lever arm: the camera entrance pupil is
offset from the intersection of the pan and tilt axes, so at finite scene
range each pixel's apparent direction from the PTU origin differs from
the pure-rotation model (parallax ~ offset/range radians).

Frames and conventions (pinned by unit tests; signs to be confirmed
against hardware — see docs/hardware_checklist.md):

- PTU frame: right-handed; +Z up along the pan axis; +X is the boresight
  direction at pan=0, tilt=0. Azimuth is measured positive in the same
  sense as positive pan, elevation positive up, so by construction the
  center pixel maps to exactly (az, el) = (pan, tilt) when all offsets
  are zero.
- Tilt-stage frame: rigidly attached to the tilted platform. The camera
  offset vector is expressed here as (forward, along-tilt-axis, up) in
  meters; at pan=0, tilt=0 it coincides with the PTU frame axes.
- Camera frame: +z out of the lens along the boresight, +x along
  increasing image column, +y along increasing image row. A positive
  column offset decreases azimuth and a positive row offset decreases
  elevation under these conventions; the mount's 180-degree flip (or any
  clocking) is expressed via ``CameraModel.roll_deg``.
- Radial distortion follows r_d = r_u * (1 + k1 * r_u**2) with radii in
  pixels from the principal point (the convention fitted by
  create_mosaic.py's cylindrical model).

The camera intrinsics (focal length, principal point, roll, k1) can be
taken from the nominal lens specification or, better, from a mosaic
model JSON fitted by create_mosaic.py — the parameters are deliberately
the same.
"""

import math
from dataclasses import dataclass
from typing import Optional, Tuple, Union

import numpy as np


@dataclass
class CameraModel:
    """Pinhole camera intrinsics with weak radial distortion.

    Parameters
    ----------
    focal_length_mm : float
        Effective focal length in millimeters.
    pixel_pitch_um : float
        Pixel pitch in micrometers.
    width_px : int
        Sensor width in pixels (columns).
    height_px : int
        Sensor height in pixels (rows).
    cx_offset_px : float
        Principal point column offset from the sensor center, in pixels.
    cy_offset_px : float
        Principal point row offset from the sensor center, in pixels.
    roll_deg : float
        Camera clocking rotation about the boresight, in degrees. Use
        180.0 for an inverted mount.
    k1 : float
        First-order radial distortion coefficient, pixel-radius
        convention (see module docstring). 0 disables distortion.
    """
    focal_length_mm: float
    pixel_pitch_um: float
    width_px: int
    height_px: int
    cx_offset_px: float = 0.0
    cy_offset_px: float = 0.0
    roll_deg: float = 0.0
    k1: float = 0.0

    @property
    def cx(self) -> float:
        """Principal point column coordinate in pixels."""
        return (self.width_px - 1) / 2.0 + self.cx_offset_px

    @property
    def cy(self) -> float:
        """Principal point row coordinate in pixels."""
        return (self.height_px - 1) / 2.0 + self.cy_offset_px

    @property
    def focal_length_px(self) -> float:
        """Focal length expressed in pixels."""
        return self.focal_length_mm * 1000.0 / self.pixel_pitch_um


@dataclass
class MountModel:
    """Rigid mounting of the camera on the PTU tilt stage.

    Parameters
    ----------
    camera_offset_m : tuple of (float, float, float)
        Position of the camera entrance pupil relative to the
        intersection of the pan and tilt axes, in the tilt-stage frame:
        (forward along boresight, along tilt axis, up), meters. This is
        the boresight lever arm producing parallax at finite range.
    boresight_pan_offset_deg : float
        Fixed angular misalignment of the boresight about the stage
        vertical axis (calibratable; adds to azimuth at tilt=0).
    boresight_tilt_offset_deg : float
        Fixed angular misalignment about the tilt axis (adds to
        elevation).
    sensor_inverted : bool
        True if the camera is mounted such that the sensor is upside-down
        (raw image rotated 180 deg relative to the scene). Drives the
        image-plane roll used both for backplane geometry and for
        de-rotating captured frames.
    """
    camera_offset_m: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    boresight_pan_offset_deg: float = 0.0
    boresight_tilt_offset_deg: float = 0.0
    sensor_inverted: bool = False


def image_rotation_k(sensor_inverted: bool) -> int:
    """Return the ``numpy.rot90`` k-value that de-rotates a captured frame.

    A single source of truth for scene orientation: every consumer that
    displays or mosaics raw frames (create_mosaic, generate_derived_
    products, aim_ptu) uses this so they cannot disagree. ``k=2`` is a
    180-degree rotation for an inverted sensor; ``k=0`` (no rotation) when
    the sensor is upright.

    Parameters
    ----------
    sensor_inverted : bool
        Whether the sensor was mounted upside-down (from the capture
        metadata or the mount config).
    """
    return 2 if sensor_inverted else 0


def _rot_z(angle_rad: float) -> np.ndarray:
    """Rotation matrix about +Z by ``angle_rad``."""
    c, s = math.cos(angle_rad), math.sin(angle_rad)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def _rot_y(angle_rad: float) -> np.ndarray:
    """Rotation matrix about +Y by ``angle_rad``."""
    c, s = math.cos(angle_rad), math.sin(angle_rad)
    return np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]])


def stage_to_ptu_matrix(pan_deg: float, tilt_deg: float) -> np.ndarray:
    """Rotation taking tilt-stage frame vectors into the PTU frame.

    Composition: tilt about the stage Y axis (positive tilt raises the
    boresight), then pan about the PTU Z axis.
    """
    return _rot_z(math.radians(pan_deg)) @ _rot_y(-math.radians(tilt_deg))


def _undistort_radii(r_d: np.ndarray, k1: float) -> np.ndarray:
    """Invert r_d = r_u (1 + k1 r_u^2) for the undistorted radius.

    Two fixed-point iterations, ample for |k1 r^2| << 1 (fitted values
    are ~1e-2 at the sensor corner).
    """
    r_u = r_d
    for _ in range(2):
        r_u = r_d / (1.0 + k1 * r_u ** 2)
    return r_u


def pixel_directions_camera(
    camera: CameraModel,
    rows: np.ndarray,
    cols: np.ndarray,
) -> np.ndarray:
    """Unit direction vectors in the camera frame for a pixel grid.

    Parameters
    ----------
    camera : CameraModel
        Camera intrinsics.
    rows, cols : np.ndarray
        1-D arrays of pixel row and column indices. The output grid is
        their outer product.

    Returns
    -------
    np.ndarray
        Array of shape (len(rows), len(cols), 3): unit vectors
        (x, y, z) in the camera frame.
    """
    rows = np.asarray(rows, dtype=np.float64)
    cols = np.asarray(cols, dtype=np.float64)

    x_px = cols[np.newaxis, :] - camera.cx
    y_px = rows[:, np.newaxis] - camera.cy
    x_px, y_px = np.broadcast_arrays(x_px, y_px)

    if camera.k1 != 0.0:
        r_d = np.hypot(x_px, y_px)
        r_u = _undistort_radii(r_d, camera.k1)
        with np.errstate(invalid="ignore", divide="ignore"):
            scale = np.where(r_d > 0, r_u / r_d, 1.0)
        x_px = x_px * scale
        y_px = y_px * scale

    if camera.roll_deg != 0.0:
        roll = math.radians(camera.roll_deg)
        c, s = math.cos(roll), math.sin(roll)
        x_px, y_px = c * x_px - s * y_px, s * x_px + c * y_px

    f_px = camera.focal_length_px
    d = np.stack([x_px, y_px, np.full_like(x_px, f_px)], axis=-1)
    return d / np.linalg.norm(d, axis=-1, keepdims=True)


def pixel_to_azel(
    camera: CameraModel,
    mount: MountModel,
    pan_deg: float,
    tilt_deg: float,
    rows: Union[np.ndarray, int, None] = None,
    cols: Union[np.ndarray, int, None] = None,
    scene_range_m: Optional[float] = None,
) -> Tuple[np.ndarray, np.ndarray]:
    """Compute az/el backplanes in the PTU frame for a pixel grid.

    With ``scene_range_m`` None (infinity), directions are pure
    rotations and the camera offset has no effect. With a finite range,
    each pixel ray (originating at the offset camera position) is
    intersected with the sphere of radius ``scene_range_m`` centered on
    the PTU origin, and the az/el of that scene point is returned —
    this is the parallax-corrected pointing a mosaic should register
    against.

    Parameters
    ----------
    camera : CameraModel
        Camera intrinsics.
    mount : MountModel
        Camera mounting geometry (lever arm and angular offsets).
    pan_deg, tilt_deg : float
        PTU angles for this frame (use encoder-derived actual angles).
    rows, cols : np.ndarray or None
        1-D pixel row/column indices; None means every row/column.
    scene_range_m : float, optional
        Assumed scene distance from the PTU origin in meters; None
        means infinity.

    Returns
    -------
    tuple of (np.ndarray, np.ndarray)
        Azimuth and elevation in degrees, each of shape
        (len(rows), len(cols)).

    Raises
    ------
    ValueError
        If ``scene_range_m`` is not greater than the camera offset
        length.
    """
    if rows is None:
        rows = np.arange(camera.height_px)
    if cols is None:
        cols = np.arange(camera.width_px)
    rows = np.atleast_1d(np.asarray(rows))
    cols = np.atleast_1d(np.asarray(cols))

    # Camera-frame rays mapped onto the tilt stage:
    # stage_x (forward) = cam_z, stage_y = -cam_x, stage_z = -cam_y
    d_cam = pixel_directions_camera(camera, rows, cols)
    d_stage = np.stack(
        [d_cam[..., 2], -d_cam[..., 0], -d_cam[..., 1]], axis=-1
    )

    # Fixed angular boresight misalignment (stage frame)
    if (mount.boresight_pan_offset_deg != 0.0 or
            mount.boresight_tilt_offset_deg != 0.0):
        r_bore = (
            _rot_z(math.radians(mount.boresight_pan_offset_deg)) @
            _rot_y(-math.radians(mount.boresight_tilt_offset_deg))
        )
        d_stage = d_stage @ r_bore.T

    # Stage -> PTU rotation
    r_sp = stage_to_ptu_matrix(pan_deg, tilt_deg)
    d_ptu = d_stage @ r_sp.T

    if scene_range_m is None or math.isinf(scene_range_m):
        v = d_ptu
    else:
        origin = r_sp @ np.asarray(mount.camera_offset_m, dtype=np.float64)
        origin_norm_sq = float(origin @ origin)
        if scene_range_m ** 2 <= origin_norm_sq:
            raise ValueError(
                f"scene_range_m ({scene_range_m}) must exceed the camera "
                f"offset length ({math.sqrt(origin_norm_sq):.3f} m)"
            )
        # Ray-sphere intersection: |origin + t*d| = scene_range
        od = d_ptu @ origin
        t = -od + np.sqrt(od ** 2 + scene_range_m ** 2 - origin_norm_sq)
        v = origin + t[..., np.newaxis] * d_ptu

    az = np.degrees(np.arctan2(v[..., 1], v[..., 0]))
    el = np.degrees(
        np.arcsin(np.clip(v[..., 2] / np.linalg.norm(v, axis=-1), -1, 1))
    )
    return az, el


def boresight_azel(
    camera: CameraModel,
    mount: MountModel,
    pan_deg: float,
    tilt_deg: float,
    scene_range_m: Optional[float] = None,
) -> Tuple[float, float]:
    """Az/el of the principal-point ray — the parallax-corrected frame
    center used for mosaic tile placement.

    Returns
    -------
    tuple of (float, float)
        (azimuth, elevation) in degrees.
    """
    az, el = pixel_to_azel(
        camera, mount, pan_deg, tilt_deg,
        rows=np.array([camera.cy]), cols=np.array([camera.cx]),
        scene_range_m=scene_range_m,
    )
    return float(az[0, 0]), float(el[0, 0])


def camera_model_from_configs(
    lens_config: dict,
    lens_id: str,
    width_px: int,
    height_px: int,
    mount_inverted: bool = False,
) -> CameraModel:
    """Build a nominal CameraModel from the project lens config.

    Prefers the calibrated effective focal length (``f_eff_mm``) over
    the nominal value when present. Image dimensions are taken from the
    actual capture (the config's sensor pixel counts are nominal and
    differ slightly from the delivered frame size).

    Parameters
    ----------
    lens_config : dict
        Parsed lens_specifications.json (sensor geometry + lenses).
    lens_id : str
        Lens identifier key (e.g. "28mm", "50mm").
    width_px, height_px : int
        Actual captured image dimensions in pixels.
    mount_inverted : bool
        If True the sensor is mounted upside-down; the camera model's
        image-plane roll is set to 180 deg so the backplane az/el matches
        the de-rotated frame.

    Raises
    ------
    KeyError
        If the lens id or required sensor fields are missing.
    """
    sensor = lens_config["sensor"]
    lens = lens_config["lenses"][lens_id]
    focal = float(lens.get("f_eff_mm", lens["focal_length_mm"]))
    return CameraModel(
        focal_length_mm=focal,
        pixel_pitch_um=float(sensor["pixel_size_um"]),
        width_px=int(width_px),
        height_px=int(height_px),
        roll_deg=180.0 if mount_inverted else 0.0,
    )


def mount_model_from_config(mount_config: Optional[dict]) -> MountModel:
    """Build a MountModel from the ptu_specifications.json section.

    Parameters
    ----------
    mount_config : dict or None
        The ``mount_geometry`` section; None yields a zero-offset mount.
    """
    if not mount_config:
        return MountModel()
    offset = mount_config.get("camera_offset_m", {})
    return MountModel(
        camera_offset_m=(
            float(offset.get("forward", 0.0)),
            float(offset.get("along_tilt_axis", 0.0)),
            float(offset.get("up", 0.0)),
        ),
        boresight_pan_offset_deg=float(
            mount_config.get("boresight_pan_offset_deg", 0.0)
        ),
        boresight_tilt_offset_deg=float(
            mount_config.get("boresight_tilt_offset_deg", 0.0)
        ),
        sensor_inverted=bool(mount_config.get("sensor_inverted", False)),
    )
