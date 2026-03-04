"""
Celestial Tracking Module

Provides single-shot pointing and continuous tracking of celestial
targets using the PTU, FLI camera system, and SPICE ephemeris
computations.

Integrates with PayloadCoordinator for image capture and with
the GPM module for observer location and mounting attitude.
"""

import time
import json
import logging
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any, Callable

from .ephemeris import (
    KernelManager,
    ObserverLocation,
    CelestialTarget,
    AzElResult,
    PTUAngles,
    compute_azimuth_elevation,
    az_el_to_ptu_angles,
)


# ============================================================================
# Data Structures
# ============================================================================

@dataclass
class TrackingConfig:
    """Configuration for a tracking session.

    Parameters
    ----------
    target : CelestialTarget
        The celestial target to track.
    duration_s : float, optional
        Total tracking duration in seconds. If None, single-shot mode.
    interval_s : float, optional
        Repointing interval in seconds. If None, uses target's
        default_interval_s.
    filter_positions : list of int, optional
        Filter positions to capture at each pointing.
    exposure_ms : int
        Exposure time in milliseconds.
    auto_expose : bool
        Whether to run auto-exposure at first pointing.
    settle_time_s : float
        Time to wait after PTU movement before capturing.
    min_elevation_deg : float
        Minimum elevation to accept.
    apply_refraction : bool
        Whether to apply atmospheric refraction correction.
    output_dir : str
        Output directory for captured images and metadata.
    session_name : str, optional
        Session name for logging.
    observer_override : ObserverLocation, optional
        Manual observer location override. If None, reads from GPM.
    """
    target: CelestialTarget
    duration_s: Optional[float] = None
    interval_s: Optional[float] = None
    filter_positions: Optional[List[int]] = None
    exposure_ms: int = 100
    auto_expose: bool = False
    settle_time_s: float = 3.0
    min_elevation_deg: float = 5.0
    apply_refraction: bool = True
    output_dir: str = "./out"
    session_name: Optional[str] = None
    observer_override: Optional[ObserverLocation] = None

    @property
    def is_continuous(self) -> bool:
        """Whether this is a continuous tracking session."""
        return self.duration_s is not None and self.duration_s > 0

    @property
    def effective_interval_s(self) -> float:
        """The actual repointing interval to use."""
        if self.interval_s is not None:
            return self.interval_s
        return float(self.target.default_interval_s)


@dataclass
class TrackingPointResult:
    """Result of a single tracking point (repoint + captures).

    Parameters
    ----------
    timestamp : str
        ISO 8601 timestamp.
    az_el : AzElResult
        Computed azimuth/elevation.
    ptu_angles : PTUAngles
        PTU pan/tilt after mounting correction.
    ptu_moved : bool
        Whether the PTU was actually commanded to move.
    captures : list of dict
        Capture results for each filter.
    error_message : str, optional
        Error description if this point failed.
    """
    timestamp: str
    az_el: AzElResult
    ptu_angles: PTUAngles
    ptu_moved: bool
    captures: List[Dict[str, Any]] = field(default_factory=list)
    error_message: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.error_message is None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "az_el": self.az_el.to_dict(),
            "ptu_angles": self.ptu_angles.to_dict(),
            "ptu_moved": self.ptu_moved,
            "captures": self.captures,
            "success": self.success,
            "error_message": self.error_message,
        }


@dataclass
class TrackingResult:
    """Summary result of a complete tracking session.

    Parameters
    ----------
    config : TrackingConfig
        The tracking configuration used.
    points : list of TrackingPointResult
        Results for each repoint.
    total_duration_s : float
        Total session duration.
    successful_points : int
        Number of successful repoints.
    total_captures : int
        Total images captured.
    target_below_horizon : bool
        Whether tracking ended because target went below horizon.
    """
    config: TrackingConfig
    points: List[TrackingPointResult] = field(default_factory=list)
    total_duration_s: float = 0.0
    successful_points: int = 0
    total_captures: int = 0
    target_below_horizon: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target": self.config.target.to_dict(),
            "mode": "continuous" if self.config.is_continuous else "single_shot",
            "duration_s": self.config.duration_s,
            "interval_s": self.config.effective_interval_s,
            "total_duration_s": self.total_duration_s,
            "successful_points": self.successful_points,
            "total_points": len(self.points),
            "total_captures": self.total_captures,
            "target_below_horizon": self.target_below_horizon,
            "points": [p.to_dict() for p in self.points],
        }


# ============================================================================
# Celestial Tracker
# ============================================================================

class CelestialTracker:
    """Orchestrates celestial body tracking with the PTU and FLI camera.

    Combines SPICE ephemeris computations with PTU control and camera
    capture to provide single-shot and continuous tracking modes.

    Uses PayloadCoordinator's execute_single_position() for capture
    operations and GPMController for observer location/attitude.

    Parameters
    ----------
    coordinator : PayloadCoordinator
        Initialized coordinator (PTU + FLI system).
    kernel_manager : KernelManager, optional
        SPICE kernel manager. Created automatically if not provided.
    logger : logging.Logger, optional
        Logger instance.
    """

    def __init__(
        self,
        coordinator,  # PayloadCoordinator — avoid circular import
        kernel_manager: Optional[KernelManager] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.coordinator = coordinator
        self.ptu = coordinator.ptu
        self.fli = coordinator.fli
        self.kernel_manager = kernel_manager or KernelManager()
        self.logger = logger or logging.getLogger(__name__)
        self._abort_requested = False

    def initialize(self) -> None:
        """Load SPICE kernels and verify GPM availability.

        Raises
        ------
        RuntimeError
            If SPICE kernel loading fails.
        """
        self.kernel_manager.load()
        self.logger.info("CelestialTracker initialized, SPICE kernels loaded")

        if self.ptu.gpm is not None:
            gps = self.ptu.gpm.get_gps_position()
            self.logger.info(
                f"GPM GPS position: {gps.latitude:.6f}, "
                f"{gps.longitude:.6f}, {gps.altitude:.1f}m"
            )
        else:
            self.logger.warning(
                "GPM not available. Observer location must be provided "
                "manually via TrackingConfig.observer_override."
            )

    def shutdown(self) -> None:
        """Unload SPICE kernels."""
        self.kernel_manager.unload()
        self.logger.info("CelestialTracker shut down")

    def _get_observer(
        self, config: TrackingConfig
    ) -> ObserverLocation:
        """Get observer location from config override or GPM.

        Raises
        ------
        RuntimeError
            If neither override nor GPM is available.
        """
        if config.observer_override is not None:
            return config.observer_override

        if self.ptu.gpm is None:
            raise RuntimeError(
                "No observer location available. Either provide "
                "observer_override in TrackingConfig or ensure GPM "
                "hardware is connected."
            )

        gps = self.ptu.gpm.get_gps_position()
        return ObserverLocation.from_gps_position(gps)

    def _get_mounting_attitude(self):
        """Get mounting attitude from GPM.

        Raises
        ------
        RuntimeError
            If GPM is not available.
        """
        if self.ptu.gpm is None:
            raise RuntimeError(
                "GPM not available; cannot read mounting attitude. "
                "For celestial tracking without GPM, the mounting "
                "attitude must be known."
            )
        return self.ptu.gpm.get_mounting_attitude()

    def compute_and_point(
        self, config: TrackingConfig, utc_time: Optional[str] = None
    ) -> TrackingPointResult:
        """Compute target position and point the PTU.

        Steps:
            1. Get observer location (GPM or override)
            2. Get mounting attitude (GPM)
            3. Compute azimuth/elevation via SPICE
            4. Apply mounting attitude correction -> PTU pan/tilt
            5. Convert degrees to steps and command PTU
            6. Wait for settle

        Parameters
        ----------
        config : TrackingConfig
            Tracking configuration.
        utc_time : str, optional
            UTC time for computation. If None, uses current time.

        Returns
        -------
        TrackingPointResult
        """
        timestamp = datetime.now(timezone.utc).isoformat()

        try:
            observer = self._get_observer(config)
            attitude = self._get_mounting_attitude()

            az_el = compute_azimuth_elevation(
                target=config.target,
                observer=observer,
                utc_time=utc_time,
                apply_refraction=config.apply_refraction,
            )

            self.logger.info(
                f"Target {config.target.name}: "
                f"az={az_el.azimuth_deg:.4f}, "
                f"el={az_el.elevation_deg:.4f}, "
                f"dist={az_el.distance_km:.1f}km"
            )

            # Check minimum elevation
            if not az_el.is_above_horizon:
                return TrackingPointResult(
                    timestamp=timestamp,
                    az_el=az_el,
                    ptu_angles=PTUAngles(
                        pan_deg=0, tilt_deg=0,
                        azimuth_deg=az_el.azimuth_deg,
                        elevation_deg=az_el.elevation_deg,
                        mounting_attitude=attitude.to_dict(),
                    ),
                    ptu_moved=False,
                    error_message=(
                        f"Target below horizon "
                        f"(el={az_el.elevation_deg:.2f})"
                    ),
                )

            if az_el.elevation_deg < config.min_elevation_deg:
                return TrackingPointResult(
                    timestamp=timestamp,
                    az_el=az_el,
                    ptu_angles=PTUAngles(
                        pan_deg=0, tilt_deg=0,
                        azimuth_deg=az_el.azimuth_deg,
                        elevation_deg=az_el.elevation_deg,
                        mounting_attitude=attitude.to_dict(),
                    ),
                    ptu_moved=False,
                    error_message=(
                        f"Target below minimum elevation "
                        f"({az_el.elevation_deg:.2f} < "
                        f"{config.min_elevation_deg})"
                    ),
                )

            ptu_angles = az_el_to_ptu_angles(
                az_el.azimuth_deg,
                az_el.elevation_deg,
                attitude,
            )

            self.logger.info(
                f"PTU angles: pan={ptu_angles.pan_deg:.4f}, "
                f"tilt={ptu_angles.tilt_deg:.4f}"
            )

            if (self.ptu.pan_resolution is None or
                    self.ptu.tilt_resolution is None):
                raise RuntimeError("PTU resolution not available")

            pan_steps = int(ptu_angles.pan_deg * self.ptu.pan_resolution)
            tilt_steps = int(ptu_angles.tilt_deg * self.ptu.tilt_resolution)

            move_ok = self.ptu.move_to_position(
                pan_steps, tilt_steps, wait=True
            )

            if not move_ok:
                return TrackingPointResult(
                    timestamp=timestamp,
                    az_el=az_el,
                    ptu_angles=ptu_angles,
                    ptu_moved=False,
                    error_message="PTU movement command failed",
                )

            if config.settle_time_s > 0:
                time.sleep(config.settle_time_s)

            return TrackingPointResult(
                timestamp=timestamp,
                az_el=az_el,
                ptu_angles=ptu_angles,
                ptu_moved=True,
            )

        except Exception as e:
            self.logger.error(f"compute_and_point failed: {e}")
            return TrackingPointResult(
                timestamp=timestamp,
                az_el=AzElResult(
                    azimuth_deg=0, elevation_deg=0, distance_km=0,
                    azimuth_raw_deg=0, elevation_raw_deg=0,
                    refraction_applied=False, utc_time=timestamp,
                    target_name=config.target.name,
                    is_above_horizon=False,
                ),
                ptu_angles=PTUAngles(
                    pan_deg=0, tilt_deg=0,
                    azimuth_deg=0, elevation_deg=0,
                    mounting_attitude={},
                ),
                ptu_moved=False,
                error_message=str(e),
            )

    def _capture_at_current_position(
        self, config: TrackingConfig, ptu_angles: PTUAngles
    ) -> List[Dict[str, Any]]:
        """Capture images through all requested filters at current position.

        Creates a PositionTarget compatible with PayloadCoordinator
        and delegates to execute_single_position().

        Parameters
        ----------
        config : TrackingConfig
            Tracking configuration.
        ptu_angles : PTUAngles
            Current PTU angles (for metadata).

        Returns
        -------
        list of dict
            Capture results for each filter.
        """
        from scripts.mission.coordinator import PositionTarget

        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        position_id = f"track_{config.target.name}_{timestamp}"

        target_pos = PositionTarget(
            id=position_id,
            pan_degrees=ptu_angles.pan_deg,
            tilt_degrees=ptu_angles.tilt_deg,
            pan_steps=int(ptu_angles.pan_deg * self.ptu.pan_resolution),
            tilt_steps=int(ptu_angles.tilt_deg * self.ptu.tilt_resolution),
            settle_time_s=0,  # Already settled during compute_and_point
            metadata={
                "celestial_target": config.target.to_dict(),
                "azimuth_deg": ptu_angles.azimuth_deg,
                "elevation_deg": ptu_angles.elevation_deg,
                "tracking_mode": (
                    "continuous" if config.is_continuous else "single_shot"
                ),
            },
        )

        result = self.coordinator.execute_single_position(
            position=target_pos,
            filter_positions=config.filter_positions,
            exposure_ms=config.exposure_ms,
        )

        return result.get("captures", [])

    def execute_single_shot(
        self, config: TrackingConfig
    ) -> TrackingResult:
        """Execute a single-shot tracking operation.

        Points the PTU at the target and captures images once.

        Parameters
        ----------
        config : TrackingConfig
            Tracking configuration.

        Returns
        -------
        TrackingResult
        """
        start_time = time.time()
        result = TrackingResult(config=config)

        self.logger.info(
            f"Single-shot tracking: {config.target.name}"
        )

        point_result = self.compute_and_point(config)

        if point_result.success and point_result.ptu_moved:
            captures = self._capture_at_current_position(
                config, point_result.ptu_angles
            )
            point_result.captures = captures

        result.points.append(point_result)
        result.total_duration_s = time.time() - start_time
        result.successful_points = 1 if point_result.success else 0
        result.total_captures = sum(
            1 for c in point_result.captures if c.get("success")
        )
        result.target_below_horizon = not point_result.az_el.is_above_horizon

        self.logger.info(
            f"Single-shot complete: "
            f"{'SUCCESS' if point_result.success else 'FAILED'}, "
            f"{result.total_captures} captures"
        )

        return result

    def execute_continuous(
        self, config: TrackingConfig,
        progress_callback: Optional[
            Callable[[int, TrackingPointResult], None]
        ] = None,
    ) -> TrackingResult:
        """Execute continuous tracking over a time window.

        Repeatedly computes the target position, re-points the PTU,
        and captures images at the configured interval.

        Parameters
        ----------
        config : TrackingConfig
            Tracking configuration (duration_s must be > 0).
        progress_callback : callable, optional
            Called after each repoint with (point_index, point_result).

        Returns
        -------
        TrackingResult
        """
        if not config.is_continuous:
            raise ValueError(
                "duration_s must be > 0 for continuous tracking"
            )

        start_time = time.time()
        end_time = start_time + config.duration_s
        interval = config.effective_interval_s

        result = TrackingResult(config=config)
        point_index = 0
        self._abort_requested = False

        self.logger.info(
            f"Continuous tracking: {config.target.name} for "
            f"{config.duration_s}s, interval={interval}s"
        )

        while time.time() < end_time and not self._abort_requested:
            loop_start = time.time()

            self.logger.info(
                f"Tracking point {point_index}: "
                f"{time.time() - start_time:.1f}s / "
                f"{config.duration_s}s elapsed"
            )

            point_result = self.compute_and_point(config)

            # Check if target went below horizon
            if (not point_result.az_el.is_above_horizon or
                    point_result.az_el.elevation_deg <
                    config.min_elevation_deg):
                self.logger.warning(
                    f"Target {config.target.name} below horizon/minimum "
                    f"elevation (el={point_result.az_el.elevation_deg:.2f})"
                )
                result.points.append(point_result)
                result.target_below_horizon = True
                break

            # Capture if pointing succeeded
            if point_result.success and point_result.ptu_moved:
                captures = self._capture_at_current_position(
                    config, point_result.ptu_angles
                )
                point_result.captures = captures
                result.total_captures += sum(
                    1 for c in captures if c.get("success")
                )

            result.points.append(point_result)
            if point_result.success:
                result.successful_points += 1

            if progress_callback:
                progress_callback(point_index, point_result)

            point_index += 1

            # Wait for next interval (sleep in 1s increments for abort)
            elapsed_this_loop = time.time() - loop_start
            wait_time = max(0, interval - elapsed_this_loop)
            if wait_time > 0 and time.time() + wait_time < end_time:
                self.logger.debug(
                    f"Waiting {wait_time:.1f}s until next repoint"
                )
                sleep_end = time.time() + wait_time
                while (time.time() < sleep_end
                       and not self._abort_requested):
                    time.sleep(min(1.0, sleep_end - time.time()))

        result.total_duration_s = time.time() - start_time

        self.logger.info(
            f"Continuous tracking complete: "
            f"{result.successful_points}/{len(result.points)} points, "
            f"{result.total_captures} captures, "
            f"{result.total_duration_s:.1f}s"
        )

        return result

    def track(
        self, config: TrackingConfig,
        progress_callback: Optional[
            Callable[[int, TrackingPointResult], None]
        ] = None,
    ) -> TrackingResult:
        """Main entry point: dispatch to single-shot or continuous.

        Parameters
        ----------
        config : TrackingConfig
            Tracking configuration.
        progress_callback : callable, optional
            Progress callback for continuous mode.

        Returns
        -------
        TrackingResult
        """
        if config.is_continuous:
            return self.execute_continuous(config, progress_callback)
        else:
            return self.execute_single_shot(config)

    def abort(self) -> None:
        """Request abort of continuous tracking.

        The current repoint/capture cycle will complete before
        the loop exits.
        """
        self._abort_requested = True
        self.logger.info("Tracking abort requested")

    def __enter__(self):
        self.initialize()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.shutdown()
        return False
