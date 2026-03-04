"""
FLIR PTU D100E Geo Pointing Module (GPM) Controller

Provides high-level access to the GPM command set on E Series PTUs.
The GPM enables GPS-based geo-pointing: given the PTU's own position
and mounting attitude, it can compute the pan/tilt angles required to
aim at arbitrary geographic coordinates.

This module does NOT own the serial connection. It delegates all
communication through a ``send_command`` callable provided by the
parent PTUController.

Usage:
    from ptu.gpm import GPMController, GeoTarget

    gpm = GPMController(send_command=ptu.send_command)
    if gpm.detect():
        pos = gpm.get_gps_position()
        print(f"PTU at {pos.latitude}, {pos.longitude}")

        target = GeoTarget(latitude=40.7128, longitude=-74.006, altitude=10)
        gpm.point_to_coordinate(target)
"""

import logging
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any, Tuple, Callable, List
from enum import Enum


# ============================================================================
# Data Structures
# ============================================================================

@dataclass
class GPSPosition:
    """GPS position from the PTU's Geo Pointing Module.

    Parameters
    ----------
    latitude : float
        Latitude in decimal degrees (positive = North).
    longitude : float
        Longitude in decimal degrees (positive = East).
    altitude : float
        Altitude in meters above mean sea level.
    """
    latitude: float
    longitude: float
    altitude: float

    def to_dict(self) -> Dict[str, float]:
        return asdict(self)


@dataclass
class MountingAttitude:
    """Mounting attitude of the PTU base.

    Parameters
    ----------
    roll : float
        Roll angle in degrees.
    pitch : float
        Pitch angle in degrees.
    yaw : float
        Yaw / heading angle in degrees.
    """
    roll: float
    pitch: float
    yaw: float

    def to_dict(self) -> Dict[str, float]:
        return asdict(self)


@dataclass
class GeoTarget:
    """A geographic target for geo-pointing.

    Parameters
    ----------
    latitude : float
        Target latitude in decimal degrees.
    longitude : float
        Target longitude in decimal degrees.
    altitude : float
        Target altitude in meters above MSL.
    name : str, optional
        Human-readable name for this target.
    """
    latitude: float
    longitude: float
    altitude: float
    name: Optional[str] = None


@dataclass
class Landmark:
    """A stored landmark in the PTU's GPM memory.

    Parameters
    ----------
    index : int
        Landmark storage index in the GPM.
    latitude : float
        Landmark latitude.
    longitude : float
        Landmark longitude.
    altitude : float
        Landmark altitude.
    name : str, optional
        User-assigned name.
    """
    index: int
    latitude: float
    longitude: float
    altitude: float
    name: Optional[str] = None


class CalibrationQuality(Enum):
    """GPM calibration quality levels."""
    UNKNOWN = "unknown"
    POOR = "poor"
    FAIR = "fair"
    GOOD = "good"
    EXCELLENT = "excellent"


@dataclass
class GPMStatus:
    """Comprehensive GPM status snapshot.

    Parameters
    ----------
    available : bool
        Whether GPM hardware is detected and responding.
    gps_position : GPSPosition, optional
        Current GPS fix, or None if no fix.
    mounting_attitude : MountingAttitude, optional
        Current base attitude, or None if unavailable.
    calibration_quality : CalibrationQuality
        Current calibration quality.
    landmark_count : int
        Number of stored landmarks.
    """
    available: bool
    gps_position: Optional[GPSPosition] = None
    mounting_attitude: Optional[MountingAttitude] = None
    calibration_quality: CalibrationQuality = CalibrationQuality.UNKNOWN
    landmark_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "available": self.available,
            "gps_position": (
                self.gps_position.to_dict() if self.gps_position else None
            ),
            "mounting_attitude": (
                self.mounting_attitude.to_dict()
                if self.mounting_attitude else None
            ),
            "calibration_quality": self.calibration_quality.value,
            "landmark_count": self.landmark_count,
        }


# ============================================================================
# GPM Controller
# ============================================================================

class GPMController:
    """Controller for the PTU D100E Geo Pointing Module.

    Wraps the GPM command set (G-prefixed commands) and provides
    high-level methods for GPS queries, attitude, geo-pointing,
    and landmark management.

    This class does not own the serial connection. It delegates
    command sending to a provided ``send_command`` callable (typically
    ``PTUController.send_command``).

    Parameters
    ----------
    send_command : callable
        Function that sends a command string to the PTU and returns
        the response string. Signature: ``(str) -> str``.
    logger : logging.Logger, optional
        Logger instance.
    """

    def __init__(
        self,
        send_command: Callable[[str], str],
        logger: Optional[logging.Logger] = None,
    ):
        self._send = send_command
        self.logger = logger or logging.getLogger(__name__)
        self._available: Optional[bool] = None

    def _require_available(self):
        """Raise if GPM is not available."""
        if not self.available:
            raise RuntimeError("GPM is not available on this PTU unit")

    def _parse_float(self, response: str, command: str) -> float:
        """Parse a single float value from a PTU response.

        Parameters
        ----------
        response : str
            Raw response string from the PTU.
        command : str
            Command that was sent (for error messages).

        Returns
        -------
        float
        """
        if not response.startswith("*"):
            raise RuntimeError(f"GPM command {command} failed: {response}")
        parts = response.split()
        if len(parts) < 2:
            raise RuntimeError(
                f"GPM command {command}: unexpected response format: "
                f"{response}"
            )
        return float(parts[-1])

    # ----------------------------------------------------------------
    # Detection
    # ----------------------------------------------------------------

    def detect(self) -> bool:
        """Detect whether the GPM is available on this PTU.

        Sends GS (GPM status) and checks for a valid response.
        Caches the result.

        Returns
        -------
        bool
            True if GPM is available.
        """
        try:
            response = self._send("GS")
            self._available = response.startswith("*")
            return self._available
        except Exception:
            self._available = False
            return False

    @property
    def available(self) -> bool:
        """Whether GPM is available (calls detect() if not yet checked)."""
        if self._available is None:
            self.detect()
        return self._available

    # ----------------------------------------------------------------
    # GPS Position
    # ----------------------------------------------------------------

    def get_gps_position(self) -> GPSPosition:
        """Query current GPS position (GLLA command).

        Returns
        -------
        GPSPosition
            Current latitude, longitude, altitude.
        """
        self._require_available()
        response = self._send("GLLA")
        if not response.startswith("*"):
            raise RuntimeError(f"GPM GPS query failed: {response}")

        # Response: "* <lat>,<lon>,<alt>" or "* <lat> <lon> <alt>"
        payload = response.lstrip("* ").strip()
        # Handle both comma and space separation
        if "," in payload:
            parts = payload.split(",")
        else:
            parts = payload.split()

        if len(parts) < 3:
            raise RuntimeError(
                f"GPM GLLA unexpected response format: {response}"
            )

        return GPSPosition(
            latitude=float(parts[0]),
            longitude=float(parts[1]),
            altitude=float(parts[2]),
        )

    def set_gps_position(self, position: GPSPosition) -> bool:
        """Set the PTU's GPS position (GLLA command).

        Parameters
        ----------
        position : GPSPosition
            Position to set.

        Returns
        -------
        bool
            True if accepted.
        """
        self._require_available()
        response = self._send(
            f"GLLA{position.latitude},{position.longitude},{position.altitude}"
        )
        return response.startswith("*")

    def get_latitude(self) -> float:
        """Query latitude only (GL command)."""
        self._require_available()
        return self._parse_float(self._send("GL"), "GL")

    def get_longitude(self) -> float:
        """Query longitude only (GO command)."""
        self._require_available()
        return self._parse_float(self._send("GO"), "GO")

    def get_altitude(self) -> float:
        """Query altitude only (GA command)."""
        self._require_available()
        return self._parse_float(self._send("GA"), "GA")

    # ----------------------------------------------------------------
    # Mounting Attitude
    # ----------------------------------------------------------------

    def get_mounting_attitude(self) -> MountingAttitude:
        """Query mounting attitude (GRPY command).

        Returns
        -------
        MountingAttitude
            Roll, pitch, yaw of the PTU base.
        """
        self._require_available()
        response = self._send("GRPY")
        if not response.startswith("*"):
            raise RuntimeError(
                f"GPM attitude query failed: {response}"
            )

        payload = response.lstrip("* ").strip()
        if "," in payload:
            parts = payload.split(",")
        else:
            parts = payload.split()

        if len(parts) < 3:
            raise RuntimeError(
                f"GPM GRPY unexpected response format: {response}"
            )

        return MountingAttitude(
            roll=float(parts[0]),
            pitch=float(parts[1]),
            yaw=float(parts[2]),
        )

    def set_mounting_attitude(self, attitude: MountingAttitude) -> bool:
        """Set the PTU's mounting attitude (GRPY command).

        Parameters
        ----------
        attitude : MountingAttitude
            Roll, pitch, yaw to set.

        Returns
        -------
        bool
            True if accepted.
        """
        self._require_available()
        response = self._send(
            f"GRPY{attitude.roll},{attitude.pitch},{attitude.yaw}"
        )
        return response.startswith("*")

    def get_roll(self) -> float:
        """Query roll only (GR command)."""
        self._require_available()
        return self._parse_float(self._send("GR"), "GR")

    def get_pitch(self) -> float:
        """Query pitch only (GP command)."""
        self._require_available()
        return self._parse_float(self._send("GP"), "GP")

    def get_yaw(self) -> float:
        """Query yaw only (GY command)."""
        self._require_available()
        return self._parse_float(self._send("GY"), "GY")

    # ----------------------------------------------------------------
    # Geo-Pointing
    # ----------------------------------------------------------------

    def point_to_coordinate(
        self, target: GeoTarget, wait: bool = True
    ) -> bool:
        """Aim the PTU at a geographic coordinate (GG command).

        The GPM computes the required pan/tilt internally based on the
        PTU's GPS position, mounting attitude, and the target coordinate.

        Parameters
        ----------
        target : GeoTarget
            Target latitude, longitude, altitude.
        wait : bool
            If True, block until movement completes (sends A command).

        Returns
        -------
        bool
            True if the command was accepted.
        """
        self._require_available()
        response = self._send(
            f"GG{target.latitude},{target.longitude},{target.altitude}"
        )
        if not response.startswith("*"):
            self.logger.error(f"Geo-point command failed: {response}")
            return False

        if wait:
            self._send("A")

        return True

    def point_to_landmark(self, index: int, wait: bool = True) -> bool:
        """Aim PTU at a stored landmark (GG<index> command).

        Parameters
        ----------
        index : int
            Landmark index in GPM memory.
        wait : bool
            If True, block until movement completes.

        Returns
        -------
        bool
            True if the command was accepted.
        """
        self._require_available()
        response = self._send(f"GG{index}")
        if not response.startswith("*"):
            self.logger.error(
                f"Geo-point to landmark {index} failed: {response}"
            )
            return False

        if wait:
            self._send("A")

        return True

    def get_distance_to(
        self, target: Optional[GeoTarget] = None
    ) -> float:
        """Get distance to aim point or specified coordinate (GGD command).

        Parameters
        ----------
        target : GeoTarget, optional
            If provided, computes distance to this coordinate.
            If None, returns distance to current aim point.

        Returns
        -------
        float
            Distance in meters.
        """
        self._require_available()
        if target is not None:
            cmd = (
                f"GGD{target.latitude},{target.longitude},{target.altitude}"
            )
        else:
            cmd = "GGD"
        return self._parse_float(self._send(cmd), "GGD")

    # ----------------------------------------------------------------
    # Landmark Management
    # ----------------------------------------------------------------

    def add_landmark(self, target: GeoTarget) -> bool:
        """Add a landmark to GPM memory (GMA command).

        Parameters
        ----------
        target : GeoTarget
            Geographic coordinate and optional name.

        Returns
        -------
        bool
            True if the landmark was added successfully.
        """
        self._require_available()
        name = target.name or "landmark"
        response = self._send(
            f"GMA{name},{target.latitude},{target.longitude},{target.altitude}"
        )
        return response.startswith("*")

    def get_landmark(self, index: int) -> Landmark:
        """Query a stored landmark by index (GM<index> command).

        Parameters
        ----------
        index : int
            Landmark index.

        Returns
        -------
        Landmark
            Stored landmark data.
        """
        self._require_available()
        response = self._send(f"GM{index}")
        if not response.startswith("*"):
            raise RuntimeError(
                f"GPM landmark query failed for index {index}: {response}"
            )

        payload = response.lstrip("* ").strip()
        if "," in payload:
            parts = payload.split(",")
        else:
            parts = payload.split()

        # Response may include name as first field
        # Try to detect: if first part is not a number, treat as name
        name = None
        float_parts: List[str] = []
        for part in parts:
            try:
                float(part)
                float_parts.append(part)
            except ValueError:
                if name is None:
                    name = part

        if len(float_parts) < 3:
            raise RuntimeError(
                f"GPM landmark {index} unexpected format: {response}"
            )

        return Landmark(
            index=index,
            latitude=float(float_parts[0]),
            longitude=float(float_parts[1]),
            altitude=float(float_parts[2]),
            name=name,
        )

    def get_landmark_count(self) -> int:
        """Get number of stored landmarks (GMN command).

        Returns
        -------
        int
            Number of landmarks.
        """
        self._require_available()
        response = self._send("GMN")
        if not response.startswith("*"):
            raise RuntimeError(
                f"GPM landmark count query failed: {response}"
            )
        return int(response.split()[-1])

    def delete_landmark(self, index: int) -> bool:
        """Delete a landmark by index (GMD<index> command).

        Parameters
        ----------
        index : int
            Landmark index to delete.

        Returns
        -------
        bool
            True if deletion successful.
        """
        self._require_available()
        response = self._send(f"GMD{index}")
        return response.startswith("*")

    def clear_all_landmarks(self) -> bool:
        """Delete all landmarks (GMC command).

        Returns
        -------
        bool
            True if successful.
        """
        self._require_available()
        response = self._send("GMC")
        return response.startswith("*")

    # ----------------------------------------------------------------
    # Calibration
    # ----------------------------------------------------------------

    def calibrate(self) -> bool:
        """Start GPM calibration (GC command).

        Returns
        -------
        bool
            True if calibration command accepted.
        """
        self._require_available()
        response = self._send("GC")
        return response.startswith("*")

    def get_calibration_quality(self) -> CalibrationQuality:
        """Query calibration quality (GCQ command).

        Returns
        -------
        CalibrationQuality
            Current calibration quality level.
        """
        self._require_available()
        response = self._send("GCQ")
        if not response.startswith("*"):
            return CalibrationQuality.UNKNOWN

        payload = response.lstrip("* ").strip().lower()

        for quality in CalibrationQuality:
            if quality.value in payload:
                return quality

        return CalibrationQuality.UNKNOWN

    # ----------------------------------------------------------------
    # Camera Offset
    # ----------------------------------------------------------------

    def set_camera_offset(self, offset: float) -> bool:
        """Set camera pointing offset from PTU axis (GCP command).

        Parameters
        ----------
        offset : float
            Camera offset in degrees.

        Returns
        -------
        bool
            True if accepted.
        """
        self._require_available()
        response = self._send(f"GCP{offset}")
        return response.startswith("*")

    def get_camera_offset(self) -> float:
        """Query camera pointing offset (GCP command).

        Returns
        -------
        float
            Camera offset in degrees.
        """
        self._require_available()
        return self._parse_float(self._send("GCP"), "GCP")

    # ----------------------------------------------------------------
    # Point Type
    # ----------------------------------------------------------------

    def get_point_type(self) -> str:
        """Query the current GPM point type (GPT command).

        Returns
        -------
        str
            Point type identifier string.
        """
        self._require_available()
        response = self._send("GPT")
        if not response.startswith("*"):
            raise RuntimeError(f"GPM point type query failed: {response}")
        return response.lstrip("* ").strip()

    def set_point_type(self, point_type: str) -> bool:
        """Set the GPM point type (GPT command).

        Parameters
        ----------
        point_type : str
            Point type identifier.

        Returns
        -------
        bool
            True if accepted.
        """
        self._require_available()
        response = self._send(f"GPT{point_type}")
        return response.startswith("*")

    # ----------------------------------------------------------------
    # Settings Persistence
    # ----------------------------------------------------------------

    def save_settings(self) -> bool:
        """Save GPM settings to non-volatile memory (GDS command).

        Returns
        -------
        bool
            True if successful.
        """
        self._require_available()
        response = self._send("GDS")
        return response.startswith("*")

    def restore_settings(self) -> bool:
        """Restore GPM settings from non-volatile memory (GDR command).

        Returns
        -------
        bool
            True if successful.
        """
        self._require_available()
        response = self._send("GDR")
        return response.startswith("*")

    def factory_reset(self) -> bool:
        """Reset GPM to factory defaults (GDF command).

        Returns
        -------
        bool
            True if successful.
        """
        self._require_available()
        response = self._send("GDF")
        return response.startswith("*")

    # ----------------------------------------------------------------
    # Status
    # ----------------------------------------------------------------

    def get_status(self) -> GPMStatus:
        """Get comprehensive GPM status.

        Queries GPS position, mounting attitude, calibration quality,
        and landmark count, assembling them into a GPMStatus snapshot.

        Returns
        -------
        GPMStatus
            Current GPM status.
        """
        if not self.available:
            return GPMStatus(available=False)

        gps_position = None
        mounting_attitude = None
        calibration = CalibrationQuality.UNKNOWN
        landmark_count = 0

        try:
            gps_position = self.get_gps_position()
        except Exception:
            pass

        try:
            mounting_attitude = self.get_mounting_attitude()
        except Exception:
            pass

        try:
            calibration = self.get_calibration_quality()
        except Exception:
            pass

        try:
            landmark_count = self.get_landmark_count()
        except Exception:
            pass

        return GPMStatus(
            available=True,
            gps_position=gps_position,
            mounting_attitude=mounting_attitude,
            calibration_quality=calibration,
            landmark_count=landmark_count,
        )

    def get_metadata_snapshot(self) -> Dict[str, Any]:
        """Get a metadata dictionary for embedding in capture metadata.

        Returns a dict with gps_position, mounting_attitude, and
        calibration_quality. Returns an empty dict if GPM is not
        available.

        Returns
        -------
        dict
            GPM metadata snapshot, or empty dict.
        """
        if not self.available:
            return {}

        result: Dict[str, Any] = {}

        try:
            pos = self.get_gps_position()
            result["gps_position"] = pos.to_dict()
        except Exception:
            pass

        try:
            att = self.get_mounting_attitude()
            result["mounting_attitude"] = att.to_dict()
        except Exception:
            pass

        try:
            cal = self.get_calibration_quality()
            result["calibration_quality"] = cal.value
        except Exception:
            pass

        return result
