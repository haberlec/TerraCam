"""
SPICE Ephemeris and Coordinate Transformation Module

Provides celestial body position computation via NASA SPICE toolkit
(SpiceyPy) and coordinate transformations from celestial coordinates
to PTU pan/tilt angles, accounting for observer location (GPS),
mounting attitude (roll/pitch/yaw), and atmospheric refraction.

Coordinate Systems:
    ITRF93  International Terrestrial Reference Frame (Earth-fixed)
    J2000   Earth Mean Equator and Equinox of J2000 (inertial)
    ENU     East-North-Up local topocentric frame at observer
    PTU     Pan-Tilt unit frame (related to ENU by mounting attitude)

Coordinate Flow:
    SPICE body -> ITRF93 position -> ENU relative vector -> (az, el)
    -> refraction correction -> mounting attitude inverse -> (pan, tilt)
"""

import math
import json
import logging
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any
from enum import Enum

import numpy as np

try:
    import spiceypy
    SPICEYPY_AVAILABLE = True
except ImportError:
    SPICEYPY_AVAILABLE = False


# ============================================================================
# Data Structures
# ============================================================================

class TargetType(Enum):
    """Type of celestial target."""
    SPICE_BODY = "spice_body"
    RA_DEC = "ra_dec"


@dataclass
class ObserverLocation:
    """Observer position on Earth's surface.

    Parameters
    ----------
    latitude_deg : float
        Geodetic latitude in degrees (positive = North).
    longitude_deg : float
        Geodetic longitude in degrees (positive = East).
    altitude_m : float
        Altitude above WGS84 ellipsoid in meters.
    """
    latitude_deg: float
    longitude_deg: float
    altitude_m: float

    @classmethod
    def from_gps_position(cls, gps_position) -> "ObserverLocation":
        """Create from a GPSPosition dataclass (from GPMController).

        Parameters
        ----------
        gps_position : ptu.gpm.GPSPosition
            GPS position from the GPM module.
        """
        return cls(
            latitude_deg=gps_position.latitude,
            longitude_deg=gps_position.longitude,
            altitude_m=gps_position.altitude,
        )

    def to_dict(self) -> Dict[str, float]:
        return asdict(self)


@dataclass
class CelestialTarget:
    """A celestial target — either a SPICE body or RA/Dec coordinates.

    Parameters
    ----------
    name : str
        Human-readable target name.
    target_type : TargetType
        Whether this is a SPICE body or RA/Dec coordinate.
    spice_name : str, optional
        SPICE body name (e.g., "MOON", "SUN", "MARS BARYCENTER").
    ra_deg : float, optional
        Right ascension in degrees (0-360).
    dec_deg : float, optional
        Declination in degrees (-90 to +90).
    default_interval_s : int
        Default repointing interval for continuous tracking.
    """
    name: str
    target_type: TargetType
    spice_name: Optional[str] = None
    ra_deg: Optional[float] = None
    dec_deg: Optional[float] = None
    default_interval_s: int = 60

    @classmethod
    def from_spice_body(
        cls, body_name: str, config_path: Optional[str] = None
    ) -> "CelestialTarget":
        """Create from a SPICE body name, optionally loading presets.

        Checks celestial_specifications.json for a matching preset
        (with SPICE name mapping and default interval). Falls back to
        using body_name directly as the SPICE name.

        Parameters
        ----------
        body_name : str
            Body name (e.g., "MOON", "MARS", "SUN").
        config_path : str, optional
            Path to celestial_specifications.json.
        """
        spice_name = body_name.upper()
        default_interval = 60

        try:
            if config_path is None:
                project_root = Path(__file__).resolve().parent.parent.parent
                config_path = (
                    project_root / "config" / "celestial_specifications.json"
                )
            else:
                config_path = Path(config_path)

            with open(config_path) as f:
                config = json.load(f)

            presets = config["celestial_tracking"]["target_presets"]
            key = body_name.upper()
            if key in presets:
                spice_name = presets[key]["spice_name"]
                default_interval = presets[key].get("default_interval_s", 60)
        except (FileNotFoundError, KeyError, json.JSONDecodeError):
            pass

        return cls(
            name=body_name.upper(),
            target_type=TargetType.SPICE_BODY,
            spice_name=spice_name,
            default_interval_s=default_interval,
        )

    @classmethod
    def from_ra_dec(
        cls, ra_deg: float, dec_deg: float, name: Optional[str] = None
    ) -> "CelestialTarget":
        """Create from Right Ascension and Declination.

        Parameters
        ----------
        ra_deg : float
            Right ascension in degrees (0-360).
        dec_deg : float
            Declination in degrees (-90 to +90).
        name : str, optional
            Human-readable name. Auto-generated if not provided.
        """
        if name is None:
            ra_h = ra_deg / 15.0
            name = f"RA{ra_h:.2f}h_DEC{dec_deg:+.1f}"

        return cls(
            name=name,
            target_type=TargetType.RA_DEC,
            ra_deg=ra_deg,
            dec_deg=dec_deg,
            default_interval_s=300,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "target_type": self.target_type.value,
            "spice_name": self.spice_name,
            "ra_deg": self.ra_deg,
            "dec_deg": self.dec_deg,
        }


@dataclass
class AzElResult:
    """Result of an azimuth/elevation computation.

    Parameters
    ----------
    azimuth_deg : float
        Azimuth in degrees from North, increasing eastward (0-360).
    elevation_deg : float
        Elevation above horizon in degrees (-90 to +90).
    distance_km : float
        Distance to target in kilometers.
    azimuth_raw_deg : float
        Azimuth before refraction correction.
    elevation_raw_deg : float
        Elevation before refraction correction.
    refraction_applied : bool
        Whether atmospheric refraction was applied.
    utc_time : str
        UTC time of computation (ISO 8601).
    target_name : str
        Name of the target.
    is_above_horizon : bool
        Whether the target is above the horizon.
    """
    azimuth_deg: float
    elevation_deg: float
    distance_km: float
    azimuth_raw_deg: float
    elevation_raw_deg: float
    refraction_applied: bool
    utc_time: str
    target_name: str
    is_above_horizon: bool

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PTUAngles:
    """PTU pan/tilt angles after mounting attitude correction.

    Parameters
    ----------
    pan_deg : float
        Pan angle in degrees.
    tilt_deg : float
        Tilt angle in degrees.
    azimuth_deg : float
        Source azimuth used for computation.
    elevation_deg : float
        Source elevation used for computation.
    mounting_attitude : dict
        The mounting attitude used (roll, pitch, yaw).
    """
    pan_deg: float
    tilt_deg: float
    azimuth_deg: float
    elevation_deg: float
    mounting_attitude: Dict[str, float]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ============================================================================
# Kernel Manager
# ============================================================================

class KernelManager:
    """Manages SPICE kernel loading and unloading.

    Loads the metakernel (which references LSK, SPK, PCK, BPC)
    and provides kernel state management with reference counting.

    Parameters
    ----------
    metakernel_path : str, optional
        Path to the metakernel file. If None, searches for
        data/spice/terracam.tm relative to the project root.
    logger : logging.Logger, optional
        Logger instance.
    """

    def __init__(
        self,
        metakernel_path: Optional[str] = None,
        logger: Optional[logging.Logger] = None,
    ):
        if not SPICEYPY_AVAILABLE:
            raise ImportError(
                "spiceypy is required for celestial tracking. "
                "Install it with: pip install spiceypy"
            )

        self.logger = logger or logging.getLogger(__name__)
        self._loaded = False
        self._load_count = 0

        if metakernel_path is None:
            project_root = Path(__file__).resolve().parent.parent.parent
            self.metakernel_path = (
                project_root / "data" / "spice" / "terracam.tm"
            )
        else:
            self.metakernel_path = Path(metakernel_path)

    def load(self) -> None:
        """Load SPICE kernels from the metakernel.

        Safe to call multiple times; uses reference counting.

        Raises
        ------
        FileNotFoundError
            If the metakernel does not exist.
        RuntimeError
            If kernel loading fails.
        """
        self._load_count += 1
        if self._loaded:
            return

        if not self.metakernel_path.exists():
            raise FileNotFoundError(
                f"SPICE metakernel not found: {self.metakernel_path}\n"
                "Ensure SPICE kernels are installed in data/spice/"
            )

        try:
            # SPICE resolves relative paths in metakernels relative to
            # the current working directory. Temporarily chdir to the
            # metakernel's directory so $KERNELS/... paths resolve.
            import os
            prev_cwd = os.getcwd()
            os.chdir(self.metakernel_path.parent)
            try:
                spiceypy.furnsh(str(self.metakernel_path.name))
            finally:
                os.chdir(prev_cwd)
            self._loaded = True
            self.logger.info(
                f"SPICE kernels loaded from {self.metakernel_path}"
            )
        except Exception as e:
            self._load_count -= 1
            raise RuntimeError(f"Failed to load SPICE kernels: {e}") from e

    def unload(self) -> None:
        """Unload SPICE kernels.

        Only actually unloads when the reference count reaches zero.
        """
        if self._load_count > 0:
            self._load_count -= 1

        if self._load_count == 0 and self._loaded:
            spiceypy.kclear()
            self._loaded = False
            self.logger.info("SPICE kernels unloaded")

    @property
    def is_loaded(self) -> bool:
        """Whether kernels are currently loaded."""
        return self._loaded

    def __enter__(self):
        self.load()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.unload()
        return False


# ============================================================================
# Internal Helper Functions
# ============================================================================

def _utc_now() -> str:
    """Get current UTC time as ISO 8601 string for SPICE."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")


def _datetime_to_et(utc_time: Optional[str] = None) -> float:
    """Convert UTC time string to SPICE Ephemeris Time (ET).

    Parameters
    ----------
    utc_time : str, optional
        UTC time in ISO 8601 format. If None, uses current time.

    Returns
    -------
    float
        SPICE ET (seconds past J2000 epoch).
    """
    if utc_time is None:
        utc_time = _utc_now()
    return spiceypy.str2et(utc_time)


def _observer_itrf93_position(observer: ObserverLocation) -> np.ndarray:
    """Compute observer position in ITRF93 from geodetic coordinates.

    Uses SPICE's pgrrec() to convert planetographic (geodetic)
    coordinates to rectangular ITRF93 coordinates.

    Parameters
    ----------
    observer : ObserverLocation
        Observer geodetic position.

    Returns
    -------
    numpy.ndarray
        3-element array [x, y, z] in km, ITRF93 frame.
    """
    radii = spiceypy.bodvrd("EARTH", "RADII", 3)[1]
    re = radii[0]       # Equatorial radius in km
    rp = radii[2]       # Polar radius in km
    f = (re - rp) / re  # Flattening

    lon_rad = math.radians(observer.longitude_deg)
    lat_rad = math.radians(observer.latitude_deg)
    alt_km = observer.altitude_m / 1000.0

    obs_pos = spiceypy.pgrrec(
        "EARTH", lon_rad, lat_rad, alt_km, re, f
    )
    return np.array(obs_pos)


def _build_enu_rotation_matrix(
    lat_rad: float, lon_rad: float
) -> np.ndarray:
    """Build rotation matrix from ITRF93 (ECEF) to local ENU frame.

    The ENU (East-North-Up) frame at a given latitude/longitude:
        East  = [-sin(lon),           cos(lon),          0         ]
        North = [-sin(lat)*cos(lon), -sin(lat)*sin(lon), cos(lat)  ]
        Up    = [ cos(lat)*cos(lon),  cos(lat)*sin(lon), sin(lat)  ]

    Parameters
    ----------
    lat_rad : float
        Observer geodetic latitude in radians.
    lon_rad : float
        Observer geodetic longitude in radians.

    Returns
    -------
    numpy.ndarray
        3x3 rotation matrix (rows: E, N, U).
    """
    sin_lat = math.sin(lat_rad)
    cos_lat = math.cos(lat_rad)
    sin_lon = math.sin(lon_rad)
    cos_lon = math.cos(lon_rad)

    R = np.array([
        [-sin_lon,            cos_lon,           0.0     ],  # East
        [-sin_lat * cos_lon, -sin_lat * sin_lon, cos_lat ],  # North
        [ cos_lat * cos_lon,  cos_lat * sin_lon, sin_lat ],  # Up
    ])
    return R


def _atmospheric_refraction(elevation_deg: float) -> float:
    """Compute atmospheric refraction correction using Bennett's formula.

    Approximates the apparent elevation increase due to atmospheric
    refraction. Most significant near the horizon.

    Bennett (1982): R = 1/tan(el + 7.31/(el + 4.4)) in arcminutes

    Parameters
    ----------
    elevation_deg : float
        True (geometric) elevation in degrees.

    Returns
    -------
    float
        Refraction correction in degrees (always positive).
        Add to geometric elevation to get apparent elevation.
    """
    if elevation_deg < -1.0:
        return 0.0

    el = max(elevation_deg, -1.0)

    r_arcmin = 1.0 / math.tan(
        math.radians(el + 7.31 / (el + 4.4))
    )

    return max(0.0, r_arcmin / 60.0)


# ============================================================================
# Core Computation Functions
# ============================================================================

def compute_azimuth_elevation(
    target: CelestialTarget,
    observer: ObserverLocation,
    utc_time: Optional[str] = None,
    apply_refraction: bool = True,
    aberration_correction: str = "LT+S",
) -> AzElResult:
    """Compute azimuth and elevation of a celestial target.

    For SPICE bodies: uses spkpos() to get position in ITRF93,
    computes relative vector to observer, transforms to ENU,
    and derives azimuth/elevation.

    For RA/Dec targets: converts RA/Dec to unit vector in J2000,
    transforms to ITRF93 via pxform(), then to ENU.

    Parameters
    ----------
    target : CelestialTarget
        The celestial target.
    observer : ObserverLocation
        Observer position on Earth.
    utc_time : str, optional
        UTC time (ISO 8601). If None, uses current time.
    apply_refraction : bool
        Whether to apply atmospheric refraction correction.
    aberration_correction : str
        SPICE aberration correction string.

    Returns
    -------
    AzElResult
        Azimuth, elevation, and related metadata.

    Raises
    ------
    ValueError
        If target type is invalid.
    """
    if utc_time is None:
        utc_time = _utc_now()

    et = _datetime_to_et(utc_time)
    obs_pos_itrf93 = _observer_itrf93_position(observer)

    lat_rad = math.radians(observer.latitude_deg)
    lon_rad = math.radians(observer.longitude_deg)
    R_enu = _build_enu_rotation_matrix(lat_rad, lon_rad)

    if target.target_type == TargetType.SPICE_BODY:
        target_pos_itrf93, light_time = spiceypy.spkpos(
            target.spice_name, et, "ITRF93",
            aberration_correction, "EARTH"
        )
        target_pos_itrf93 = np.array(target_pos_itrf93)

        rel_itrf93 = target_pos_itrf93 - obs_pos_itrf93
        rel_enu = R_enu @ rel_itrf93
        distance_km = float(np.linalg.norm(rel_enu))

    elif target.target_type == TargetType.RA_DEC:
        ra_rad = math.radians(target.ra_deg)
        dec_rad = math.radians(target.dec_deg)

        dir_j2000 = np.array([
            math.cos(dec_rad) * math.cos(ra_rad),
            math.cos(dec_rad) * math.sin(ra_rad),
            math.sin(dec_rad),
        ])

        rot_j2000_to_itrf93 = np.array(
            spiceypy.pxform("J2000", "ITRF93", et)
        )
        dir_itrf93 = rot_j2000_to_itrf93 @ dir_j2000

        rel_enu = R_enu @ dir_itrf93
        distance_km = float('inf')

    else:
        raise ValueError(f"Unknown target type: {target.target_type}")

    # Compute azimuth and elevation from ENU components
    east, north, up = rel_enu[0], rel_enu[1], rel_enu[2]

    # Azimuth: from North, increasing eastward
    azimuth_rad = math.atan2(east, north)
    azimuth_deg = math.degrees(azimuth_rad) % 360.0

    # Elevation: angle above horizontal plane
    horizontal_dist = math.sqrt(east**2 + north**2)
    elevation_rad = math.atan2(up, horizontal_dist)
    elevation_deg = math.degrees(elevation_rad)

    az_raw = azimuth_deg
    el_raw = elevation_deg

    refraction_applied = False
    if apply_refraction and elevation_deg > -1.0:
        refraction = _atmospheric_refraction(elevation_deg)
        elevation_deg += refraction
        refraction_applied = True

    return AzElResult(
        azimuth_deg=azimuth_deg,
        elevation_deg=elevation_deg,
        distance_km=distance_km,
        azimuth_raw_deg=az_raw,
        elevation_raw_deg=el_raw,
        refraction_applied=refraction_applied,
        utc_time=utc_time,
        target_name=target.name,
        is_above_horizon=elevation_deg > 0.0,
    )


def az_el_to_ptu_angles(
    azimuth_deg: float,
    elevation_deg: float,
    mounting_attitude,
) -> PTUAngles:
    """Convert azimuth/elevation to PTU pan/tilt angles.

    Applies the inverse of the PTU's mounting attitude to transform
    from the geographic (ENU-derived) az/el to the PTU's own pan/tilt
    frame.

    For a level mount (roll~0, pitch~0):
        pan = azimuth - yaw
        tilt = elevation

    For a tilted mount, a full ZYX inverse rotation is applied.

    Parameters
    ----------
    azimuth_deg : float
        Target azimuth in degrees from North (0-360).
    elevation_deg : float
        Target elevation in degrees above horizon.
    mounting_attitude : MountingAttitude
        PTU mounting attitude (roll, pitch, yaw in degrees).

    Returns
    -------
    PTUAngles
        Pan and tilt angles in degrees for the PTU.
    """
    yaw_rad = math.radians(mounting_attitude.yaw)
    pitch_rad = math.radians(mounting_attitude.pitch)
    roll_rad = math.radians(mounting_attitude.roll)

    attitude_dict = mounting_attitude.to_dict()

    # Simple case: level mount (pitch and roll near zero)
    if abs(mounting_attitude.pitch) < 0.5 and abs(mounting_attitude.roll) < 0.5:
        pan_deg = (azimuth_deg - mounting_attitude.yaw) % 360.0
        if pan_deg > 180.0:
            pan_deg -= 360.0
        tilt_deg = elevation_deg

        return PTUAngles(
            pan_deg=pan_deg,
            tilt_deg=tilt_deg,
            azimuth_deg=azimuth_deg,
            elevation_deg=elevation_deg,
            mounting_attitude=attitude_dict,
        )

    # Full rotation matrix for tilted mounts
    az_rad = math.radians(azimuth_deg)
    el_rad = math.radians(elevation_deg)

    # Direction in geographic frame (x=North, y=East, z=Up)
    dx = math.cos(el_rad) * math.cos(az_rad)
    dy = math.cos(el_rad) * math.sin(az_rad)
    dz = math.sin(el_rad)
    dir_geo = np.array([dx, dy, dz])

    # Mounting rotation matrix (ZYX intrinsic: yaw-pitch-roll)
    # Rotates from PTU frame to geographic frame
    cy, sy = math.cos(yaw_rad), math.sin(yaw_rad)
    cp, sp = math.cos(pitch_rad), math.sin(pitch_rad)
    cr, sr = math.cos(roll_rad), math.sin(roll_rad)

    R_mount = np.array([
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp,     cp * sr,                cp * cr               ],
    ])

    # Inverse rotation: geographic -> PTU frame
    dir_ptu = R_mount.T @ dir_geo

    # Extract pan/tilt from PTU-frame direction vector
    pan_rad = math.atan2(dir_ptu[1], dir_ptu[0])
    horiz = math.sqrt(dir_ptu[0] ** 2 + dir_ptu[1] ** 2)
    tilt_rad = math.atan2(dir_ptu[2], horiz)

    return PTUAngles(
        pan_deg=math.degrees(pan_rad),
        tilt_deg=math.degrees(tilt_rad),
        azimuth_deg=azimuth_deg,
        elevation_deg=elevation_deg,
        mounting_attitude=attitude_dict,
    )
