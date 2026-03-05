"""
Payload Coordinator for PTU and FLI Camera Operations

Orchestrates synchronized pan-tilt movement and multispectral image
acquisition. At each position the coordinator moves the PTU, waits for
settling, then cycles through the requested filter positions capturing
an image at each one.

Adapted from the SPECTRE PayloadControl coordinator to use the FLI
camera system (FLISystem) instead of a mock camera interface.
"""

import math
import os
import json
import time
import numpy as np
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple, Callable
from dataclasses import dataclass
from enum import Enum

from fli import FLISystem
from ptu import PTUController, PTUConfig
from ptu.logger import SessionLogger, OperationTimer
from scripts.capture.auto_expose import auto_expose, AutoExposeResult

try:
    from PIL import Image
except ImportError:
    Image = None


class SequenceStatus(Enum):
    """Status of an acquisition sequence."""
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    ERROR = "error"
    ABORTED = "aborted"


@dataclass
class PositionTarget:
    """Definition of a target position for data acquisition.

    Parameters
    ----------
    id : str
        Unique identifier for this position.
    pan_degrees : float
        Target pan angle in degrees.
    tilt_degrees : float
        Target tilt angle in degrees.
    pan_steps : int, optional
        Target pan position in encoder steps (computed if not provided).
    tilt_steps : int, optional
        Target tilt position in encoder steps (computed if not provided).
    settle_time_s : float
        Time to wait after movement before acquisition.
    metadata : dict, optional
        Arbitrary metadata to attach to this position.
    """
    id: str
    pan_degrees: float
    tilt_degrees: float
    pan_steps: Optional[int] = None
    tilt_steps: Optional[int] = None
    settle_time_s: float = 2.0
    metadata: Optional[Dict[str, Any]] = None


@dataclass
class GeoPositionTarget:
    """A geographic target position for GPM-based acquisition.

    Parameters
    ----------
    id : str
        Unique identifier for this target.
    latitude : float
        Target latitude in decimal degrees.
    longitude : float
        Target longitude in decimal degrees.
    altitude : float
        Target altitude in meters above MSL.
    settle_time_s : float
        Time to wait after movement before acquisition.
    metadata : dict, optional
        Arbitrary metadata to attach to this target.
    """
    id: str
    latitude: float
    longitude: float
    altitude: float
    settle_time_s: float = 2.0
    metadata: Optional[Dict[str, Any]] = None


@dataclass
class SequenceConfig:
    """Configuration for an acquisition sequence.

    Parameters
    ----------
    sequence_name : str
        Name identifier for this sequence.
    positions : list of PositionTarget
        Ordered list of positions to visit.
    filter_positions : list of int, optional
        Filter wheel positions to capture at each PTU position.
        If None, captures only the current filter position.
    exposure_ms : int
        Default exposure time in milliseconds for each capture.
    auto_expose_center : bool
        If True, run auto-exposure at the center grid position before
        starting the sequence. The optimal exposure for each filter is
        stored in per_filter_exposure_ms and used for all positions.
    per_filter_exposure_ms : dict of {int: int}, optional
        Per-filter exposure times in milliseconds. Populated automatically
        when auto_expose_center is True, or can be set manually. If a
        filter is not in this dict, exposure_ms is used as fallback.
    inter_position_delay_s : float
        Additional delay between positions beyond settle time.
    continue_on_error : bool
        Whether to continue the sequence if a single position fails.
    return_to_start : bool
        Whether to return to the first position after completing the sequence.
    """
    sequence_name: str
    positions: List[PositionTarget]
    filter_positions: Optional[List[int]] = None
    exposure_ms: int = 100
    auto_expose_center: bool = False
    per_filter_exposure_ms: Optional[Dict[int, int]] = None
    inter_position_delay_s: float = 0.0
    continue_on_error: bool = True
    return_to_start: bool = True


class PayloadCoordinator:
    """Coordinates PTU and FLI camera operations for automated data acquisition.

    Manages the lifecycle of both the pan-tilt unit and the FLI camera/filter
    wheel system, executing synchronized scanning sequences.

    Parameters
    ----------
    ptu_config : PTUConfig
        Configuration for the PTU serial connection.
    fli_system : FLISystem
        Initialized FLI camera system (must have devices discovered).
    output_dir : str
        Directory for saving captured images and metadata.
    session_logger : SessionLogger, optional
        Logger for structured operation tracking.
    """

    def __init__(self, ptu_config: PTUConfig, fli_system: FLISystem,
                 output_dir: str = "./out",
                 session_logger: Optional[SessionLogger] = None):
        self.ptu = PTUController(ptu_config)
        self.fli = fli_system
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.logger = session_logger or SessionLogger()

        self.status = SequenceStatus.IDLE
        self.current_sequence: Optional[SequenceConfig] = None
        self.current_position_index = 0
        self.sequence_results: List[Dict[str, Any]] = []

        # Callbacks for sequence events
        self.on_position_start: Optional[Callable[[PositionTarget], None]] = None
        self.on_position_complete: Optional[
            Callable[[PositionTarget, Dict[str, Any]], None]
        ] = None
        self.on_sequence_complete: Optional[
            Callable[[SequenceConfig, List[Dict[str, Any]]], None]
        ] = None

    def initialize(self) -> bool:
        """Initialize PTU (assumes FLI system is already initialized).

        Returns
        -------
        bool
            True if PTU initialization successful.
        """
        try:
            with OperationTimer(
                self.logger, "initialize", "System",
                {"components": ["PTU"]}
            ) as timer:
                if not self.ptu.connect():
                    raise RuntimeError("Failed to connect to PTU")

                if not self.ptu.initialize():
                    raise RuntimeError("Failed to initialize PTU")

                timer.mark_success()

            self.logger.logger.info(
                "Payload coordinator initialization completed"
            )
            return True

        except Exception as e:
            self.logger.logger.error(f"Coordinator initialization failed: {e}")
            return False

    def shutdown(self):
        """Shutdown PTU and close logging session.

        Note: The FLI system lifecycle is managed externally by the caller.
        """
        if self.status == SequenceStatus.RUNNING:
            self.abort_sequence()

        self.ptu.disconnect()
        self.logger.close_session()

    def execute_single_position(
        self, position: PositionTarget,
        filter_positions: Optional[List[int]] = None,
        exposure_ms: int = 100
    ) -> Dict[str, Any]:
        """Execute data acquisition at a single PTU position.

        Moves the PTU, waits for settling, then captures an image at each
        requested filter position.

        Parameters
        ----------
        position : PositionTarget
            Target PTU position.
        filter_positions : list of int, optional
            Filter wheel positions to capture. If None, captures at the
            current filter position only.
        exposure_ms : int
            Exposure time in milliseconds.

        Returns
        -------
        dict
            Results including PTU movement status, captured images info,
            and timing data.
        """
        start_time = time.time()
        position_result = {
            "position_id": position.id,
            "target_position": {
                "pan_deg": position.pan_degrees,
                "tilt_deg": position.tilt_degrees,
            },
            "success": False,
            "ptu_movement": None,
            "captures": [],
            "total_time_s": 0,
            "error_message": None
        }

        try:
            # Convert degrees to steps if not provided
            if position.pan_steps is None or position.tilt_steps is None:
                if (self.ptu.pan_resolution is None or
                        self.ptu.tilt_resolution is None):
                    raise RuntimeError(
                        "PTU resolution not available for degree conversion"
                    )
                # Resolution is in arcsec/step; convert degrees to steps
                position.pan_steps = int(
                    position.pan_degrees * 3600.0 / self.ptu.pan_resolution
                )
                position.tilt_steps = int(
                    position.tilt_degrees * 3600.0 / self.ptu.tilt_resolution
                )

            # Notify position start callback
            if self.on_position_start:
                self.on_position_start(position)

            # --- Move PTU ---
            move_start = time.time()
            with OperationTimer(
                self.logger, "move_to_position", "PTU",
                {"pan_steps": position.pan_steps,
                 "tilt_steps": position.tilt_steps,
                 "pan_deg": position.pan_degrees,
                 "tilt_deg": position.tilt_degrees}
            ) as timer:
                if not self.ptu.move_to_position(
                    position.pan_steps, position.tilt_steps, wait=True
                ):
                    raise RuntimeError("PTU movement failed")
                timer.mark_success()

            move_time = time.time() - move_start

            # Verify position
            actual_pan, actual_tilt = self.ptu.get_position()
            position_error_pan = abs(actual_pan - position.pan_steps)
            position_error_tilt = abs(actual_tilt - position.tilt_steps)

            if position_error_pan > 5 or position_error_tilt > 5:
                self.logger.logger.warning(
                    f"PTU position error: Pan={position_error_pan}, "
                    f"Tilt={position_error_tilt} steps"
                )

            position_result["ptu_movement"] = {
                "success": True,
                "duration_s": move_time,
                "actual_position_steps": {
                    "pan": actual_pan, "tilt": actual_tilt
                },
                "position_error_steps": {
                    "pan": position_error_pan, "tilt": position_error_tilt
                },
            }

            # Wait for settle
            if position.settle_time_s > 0:
                self.logger.logger.debug(
                    f"Waiting {position.settle_time_s}s for PTU to settle"
                )
                time.sleep(position.settle_time_s)

            # --- Capture images at each filter position ---
            if filter_positions is None:
                filter_positions = [self.fli.get_filter_position()]

            for filt_pos in filter_positions:
                if self.status == SequenceStatus.ABORTED:
                    break

                # Use per-filter exposure if available
                filt_exposure = exposure_ms
                if (self.current_sequence and
                        self.current_sequence.per_filter_exposure_ms and
                        filt_pos in self.current_sequence.per_filter_exposure_ms):
                    filt_exposure = (
                        self.current_sequence.per_filter_exposure_ms[filt_pos]
                    )

                capture_result = self._capture_at_filter(
                    position, filt_pos, filt_exposure,
                    actual_pan, actual_tilt
                )
                position_result["captures"].append(capture_result)

            # Check if all captures succeeded
            all_ok = all(
                c["success"] for c in position_result["captures"]
            )
            position_result["success"] = all_ok
            position_result["total_time_s"] = time.time() - start_time

            # Notify position complete callback
            if self.on_position_complete:
                self.on_position_complete(position, position_result)

        except Exception as e:
            position_result["error_message"] = str(e)
            position_result["total_time_s"] = time.time() - start_time
            self.logger.logger.error(f"Position {position.id} failed: {e}")

        return position_result

    def _capture_at_filter(
        self, position: PositionTarget, filter_pos: int,
        exposure_ms: int, pan_steps: int, tilt_steps: int
    ) -> Dict[str, Any]:
        """Capture a single image at the specified filter position.

        Parameters
        ----------
        position : PositionTarget
            Current PTU position (for metadata).
        filter_pos : int
            Filter wheel position to move to.
        exposure_ms : int
            Exposure time in milliseconds.
        pan_steps : int
            Actual pan position in steps.
        tilt_steps : int
            Actual tilt position in steps.

        Returns
        -------
        dict
            Capture results including file paths and image statistics.
        """
        capture_result = {
            "filter_position": filter_pos,
            "exposure_ms": exposure_ms,
            "success": False,
            "files": {},
            "error_message": None,
        }

        try:
            # Move filter wheel
            with OperationTimer(
                self.logger, "move_filter", "Camera",
                {"filter_position": filter_pos}
            ) as timer:
                self.fli.move_filter(filter_pos)
                timer.mark_success()

            # Capture image
            capture_start = time.time()
            with OperationTimer(
                self.logger, "capture", "Camera",
                {"position_id": position.id,
                 "filter_position": filter_pos,
                 "exposure_ms": exposure_ms}
            ) as timer:
                image = self.fli.capture_image(exposure_ms=exposure_ms)
                timer.mark_success()

            capture_time = time.time() - capture_start

            # Save image and metadata
            sequence_name = (
                self.current_sequence.sequence_name
                if self.current_sequence else "single"
            )
            timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
            base_name = (
                f"{sequence_name}_{position.id}_F{filter_pos:02d}_{timestamp}"
            )

            saved_files = self._save_image(image, base_name)
            metadata_file = self._save_metadata(
                image, base_name, position, filter_pos,
                exposure_ms, pan_steps, tilt_steps, capture_time
            )

            capture_result["success"] = True
            capture_result["files"] = {
                **saved_files,
                "metadata": str(metadata_file),
            }
            capture_result["capture_time_s"] = capture_time
            capture_result["image_stats"] = {
                "shape": list(image.shape),
                "min": int(np.min(image)),
                "max": int(np.max(image)),
                "mean": float(np.mean(image)),
            }

        except Exception as e:
            capture_result["error_message"] = str(e)
            self.logger.logger.error(
                f"Capture failed at filter {filter_pos}: {e}"
            )

        return capture_result

    def _save_image(self, image: np.ndarray,
                    base_name: str) -> Dict[str, str]:
        """Save image in 16-bit TIFF and 8-bit JPEG formats.

        Parameters
        ----------
        image : numpy.ndarray
            Image data (uint16).
        base_name : str
            Base filename without extension.

        Returns
        -------
        dict
            Paths to saved files keyed by format.
        """
        saved = {}

        if Image is None:
            self.logger.logger.warning(
                "Pillow not available, skipping image save"
            )
            return saved

        tiff_path = self.output_dir / f"{base_name}.tiff"
        tiff_img = Image.fromarray(image.astype(np.uint16), mode='I;16')
        tiff_img.save(str(tiff_path), format='TIFF')
        saved["tiff"] = str(tiff_path)

        # 8-bit JPEG for quick preview
        jpeg_path = self.output_dir / f"{base_name}.jpg"
        min_val = np.percentile(image, 0.5)
        max_val = np.percentile(image, 99.5)
        if max_val > min_val:
            scaled = (
                (image.astype(np.float32) - min_val) /
                (max_val - min_val) * 255
            )
            jpeg_arr = np.clip(scaled, 0, 255).astype(np.uint8)
        else:
            jpeg_arr = np.zeros_like(image, dtype=np.uint8)

        jpeg_img = Image.fromarray(jpeg_arr, mode='L')
        jpeg_img.save(str(jpeg_path), format='JPEG', quality=95)
        saved["jpeg"] = str(jpeg_path)

        return saved

    def _save_metadata(
        self, image: np.ndarray, base_name: str,
        position: PositionTarget, filter_pos: int,
        exposure_ms: int, pan_steps: int, tilt_steps: int,
        capture_time_s: float
    ) -> Path:
        """Save capture metadata as JSON.

        Parameters
        ----------
        image : numpy.ndarray
            Image data for computing statistics.
        base_name : str
            Base filename without extension.
        position : PositionTarget
            PTU position information.
        filter_pos : int
            Filter wheel position.
        exposure_ms : int
            Exposure time in milliseconds.
        pan_steps : int
            Actual pan position in steps.
        tilt_steps : int
            Actual tilt position in steps.
        capture_time_s : float
            Total capture duration in seconds.

        Returns
        -------
        Path
            Path to saved metadata file.
        """
        percentiles = np.percentile(
            image, [1, 5, 25, 50, 75, 95, 99]
        )

        ccd_temp = None
        try:
            ccd_temp = self.fli.get_temperature()
        except Exception:
            pass

        metadata = {
            "image_info": {
                "filename": base_name,
                "capture_time": datetime.now().isoformat(),
                "shape": list(image.shape),
                "dtype": str(image.dtype),
                "min_value": int(np.min(image)),
                "max_value": int(np.max(image)),
                "mean_value": float(np.mean(image)),
                "std_value": float(np.std(image)),
            },
            "acquisition_settings": {
                "exposure_time_ms": exposure_ms,
                "filter_position": filter_pos,
                "ccd_temperature_c": ccd_temp,
                "frame_type": "normal",
                "capture_duration_s": capture_time_s,
            },
            "ptu_position": {
                "pan_degrees": position.pan_degrees,
                "tilt_degrees": position.tilt_degrees,
                "pan_steps": pan_steps,
                "tilt_steps": tilt_steps,
                "position_id": position.id,
            },
            "image_statistics": {
                "percentile_01": float(percentiles[0]),
                "percentile_05": float(percentiles[1]),
                "percentile_25": float(percentiles[2]),
                "percentile_50": float(percentiles[3]),
                "percentile_75": float(percentiles[4]),
                "percentile_95": float(percentiles[5]),
                "percentile_99": float(percentiles[6]),
            },
        }

        if position.metadata:
            metadata["position_metadata"] = position.metadata

        # Include GPM geo-pointing metadata if available
        if self.ptu.gpm is not None:
            try:
                gpm_data = self.ptu.gpm.get_metadata_snapshot()
                if gpm_data:
                    metadata["geo_pointing"] = gpm_data
            except Exception:
                pass

        metadata_path = self.output_dir / f"{base_name}_metadata.json"
        with open(metadata_path, 'w') as f:
            json.dump(metadata, f, indent=2)

        return metadata_path

    def _auto_expose_at_center(
        self, sequence_config: SequenceConfig
    ) -> Dict[int, AutoExposeResult]:
        """Run auto-exposure at the center grid position for each filter.

        Moves the PTU to the center position (middle of the position list),
        then runs auto_expose() for each requested filter. The optimal
        exposure times are stored in sequence_config.per_filter_exposure_ms.

        Parameters
        ----------
        sequence_config : SequenceConfig
            Sequence to configure. per_filter_exposure_ms is populated
            in-place with the computed exposures.

        Returns
        -------
        dict of {int: AutoExposeResult}
            Auto-exposure results keyed by filter position.
        """
        positions = sequence_config.positions
        center_idx = len(positions) // 2
        center_pos = positions[center_idx]

        filters = sequence_config.filter_positions or [
            self.fli.get_filter_position()
        ]

        self.logger.logger.info(
            f"Auto-expose: moving to center position {center_pos.id} "
            f"(pan={center_pos.pan_degrees}, tilt={center_pos.tilt_degrees})"
        )

        # Convert degrees to steps if needed
        if center_pos.pan_steps is None or center_pos.tilt_steps is None:
            if (self.ptu.pan_resolution is None or
                    self.ptu.tilt_resolution is None):
                raise RuntimeError(
                    "PTU resolution not available for degree conversion"
                )
            # Resolution is in arcsec/step; convert degrees to steps
            center_pos.pan_steps = int(
                center_pos.pan_degrees * 3600.0 / self.ptu.pan_resolution
            )
            center_pos.tilt_steps = int(
                center_pos.tilt_degrees * 3600.0 / self.ptu.tilt_resolution
            )

        # Move to center
        if not self.ptu.move_to_position(
            center_pos.pan_steps, center_pos.tilt_steps, wait=True
        ):
            raise RuntimeError("Failed to move to center position for auto-exposure")

        if center_pos.settle_time_s > 0:
            time.sleep(center_pos.settle_time_s)

        # Run auto-exposure for each filter
        results: Dict[int, AutoExposeResult] = {}
        per_filter_exposure: Dict[int, int] = {}

        for filt_pos in filters:
            self.logger.logger.info(
                f"Auto-expose: filter {filt_pos}..."
            )

            with OperationTimer(
                self.logger, "auto_expose", "Camera",
                {"filter_position": filt_pos}
            ) as timer:
                # Move filter
                self.fli.move_filter(filt_pos)

                # Build capture function using FLISystem
                def capture_func(exp_ms: int) -> np.ndarray:
                    return self.fli.capture_image(exposure_ms=exp_ms)

                result = auto_expose(
                    camera=self.fli.camera,
                    capture_func=capture_func,
                    initial_exposure_ms=sequence_config.exposure_ms,
                    logger=self.logger.logger,
                )
                timer.mark_success()

            results[filt_pos] = result
            per_filter_exposure[filt_pos] = result.exposure_ms

            self.logger.logger.info(
                f"Auto-expose: filter {filt_pos} -> {result.exposure_ms}ms "
                f"({result.final_metrics.quality_grade}, "
                f"converged={result.converged})"
            )

        sequence_config.per_filter_exposure_ms = per_filter_exposure

        self.logger.logger.info(
            f"Auto-expose complete: {per_filter_exposure}"
        )

        return results

    def execute_sequence(self, sequence_config: SequenceConfig) -> Dict[str, Any]:
        """Execute a complete acquisition sequence.

        Iterates through all positions in the sequence, capturing images
        at each one. Supports pause/resume and abort operations via
        the status flag.

        Parameters
        ----------
        sequence_config : SequenceConfig
            Sequence definition including positions and capture parameters.

        Returns
        -------
        dict
            Sequence summary including per-position results and timing.

        Raises
        ------
        RuntimeError
            If a sequence is already running.
        """
        if self.status != SequenceStatus.IDLE:
            raise RuntimeError(
                f"Cannot start sequence, current status: {self.status.value}"
            )

        self.current_sequence = sequence_config
        self.current_position_index = 0
        self.sequence_results = []
        self.status = SequenceStatus.RUNNING

        sequence_start_time = time.time()

        self.logger.log_sequence_start(
            sequence_config.sequence_name,
            len(sequence_config.positions)
        )

        try:
            # Auto-expose at center position if requested
            if sequence_config.auto_expose_center:
                self._auto_expose_at_center(sequence_config)

            for i, position in enumerate(sequence_config.positions):
                if self.status != SequenceStatus.RUNNING:
                    break

                self.current_position_index = i

                position_result = self.execute_single_position(
                    position,
                    sequence_config.filter_positions,
                    sequence_config.exposure_ms,
                )

                self.sequence_results.append(position_result)

                if (not position_result["success"] and
                        not sequence_config.continue_on_error):
                    self.status = SequenceStatus.ERROR
                    break

                # Inter-position delay
                if (sequence_config.inter_position_delay_s > 0 and
                        i < len(sequence_config.positions) - 1):
                    time.sleep(sequence_config.inter_position_delay_s)

            # Return to start position if requested
            if (sequence_config.return_to_start and
                    len(sequence_config.positions) > 0 and
                    self.status == SequenceStatus.RUNNING):
                start_pos = sequence_config.positions[0]
                self.logger.logger.info("Returning to start position")
                if start_pos.pan_steps is not None:
                    self.ptu.move_to_position(
                        start_pos.pan_steps, start_pos.tilt_steps, wait=True
                    )

            if self.status == SequenceStatus.RUNNING:
                self.status = SequenceStatus.COMPLETED

        except Exception as e:
            self.status = SequenceStatus.ERROR
            self.logger.logger.error(f"Sequence execution failed: {e}")

        finally:
            sequence_duration = time.time() - sequence_start_time
            successful = sum(
                1 for r in self.sequence_results if r["success"]
            )
            total = len(self.sequence_results)

            sequence_summary = {
                "sequence_name": sequence_config.sequence_name,
                "status": self.status.value,
                "total_positions": len(sequence_config.positions),
                "completed_positions": total,
                "successful_positions": successful,
                "success_rate": successful / total if total > 0 else 0,
                "total_duration_s": sequence_duration,
                "average_time_per_position_s": (
                    sequence_duration / total if total > 0 else 0
                ),
                "position_results": self.sequence_results,
            }

            self.logger.log_sequence_complete(
                sequence_config.sequence_name,
                successful,
                sequence_duration * 1000
            )

            if self.on_sequence_complete:
                self.on_sequence_complete(
                    sequence_config, self.sequence_results
                )

            self.status = SequenceStatus.IDLE
            return sequence_summary

    def pause_sequence(self):
        """Pause the current sequence after the current position completes."""
        if self.status == SequenceStatus.RUNNING:
            self.status = SequenceStatus.PAUSED
            self.ptu.halt()

    def resume_sequence(self):
        """Resume a paused sequence."""
        if self.status == SequenceStatus.PAUSED:
            self.status = SequenceStatus.RUNNING

    def abort_sequence(self):
        """Abort the current sequence immediately."""
        if self.status in (SequenceStatus.RUNNING, SequenceStatus.PAUSED):
            self.status = SequenceStatus.ABORTED
            self.ptu.halt()

    def get_status(self) -> Dict[str, Any]:
        """Get comprehensive coordinator status.

        Returns
        -------
        dict
            Status of the coordinator, PTU, and current sequence progress.
        """
        return {
            "coordinator_status": self.status.value,
            "current_sequence": (
                self.current_sequence.sequence_name
                if self.current_sequence else None
            ),
            "current_position_index": self.current_position_index,
            "completed_positions": len(self.sequence_results),
            "ptu_status": (
                self.ptu.get_status()
                if self.ptu._is_initialized else "not_initialized"
            ),
        }

    @staticmethod
    def create_grid_sequence(
        sequence_name: str,
        pan_range: Tuple[float, float],
        tilt_range: Tuple[float, float],
        pan_steps: int,
        tilt_steps: int,
        filter_positions: Optional[List[int]] = None,
        exposure_ms: int = 100,
        settle_time_s: float = 2.0
    ) -> SequenceConfig:
        """Create a grid-based acquisition sequence.

        Generates a rectangular grid of positions spanning the specified
        pan and tilt ranges.

        Parameters
        ----------
        sequence_name : str
            Name for this sequence.
        pan_range : tuple of (float, float)
            (min, max) pan angle in degrees.
        tilt_range : tuple of (float, float)
            (min, max) tilt angle in degrees.
        pan_steps : int
            Number of pan positions in the grid.
        tilt_steps : int
            Number of tilt positions in the grid.
        filter_positions : list of int, optional
            Filter positions to capture at each grid point.
        exposure_ms : int
            Exposure time in milliseconds.
        settle_time_s : float
            Settle time after each PTU movement.

        Returns
        -------
        SequenceConfig
            Configured sequence ready for execution.
        """
        positions = []

        pan_min, pan_max = pan_range
        tilt_min, tilt_max = tilt_range

        pan_increment = (
            (pan_max - pan_min) / (pan_steps - 1) if pan_steps > 1 else 0
        )
        tilt_increment = (
            (tilt_max - tilt_min) / (tilt_steps - 1) if tilt_steps > 1 else 0
        )

        position_id = 0
        for i in range(pan_steps):
            for j in range(tilt_steps):
                pan_deg = pan_min + i * pan_increment
                tilt_deg = tilt_min + j * tilt_increment

                position = PositionTarget(
                    id=f"grid_{position_id:03d}",
                    pan_degrees=pan_deg,
                    tilt_degrees=tilt_deg,
                    settle_time_s=settle_time_s,
                    metadata={
                        "grid_position": {"pan_index": i, "tilt_index": j}
                    }
                )
                positions.append(position)
                position_id += 1

        return SequenceConfig(
            sequence_name=sequence_name,
            positions=positions,
            filter_positions=filter_positions,
            exposure_ms=exposure_ms,
        )

    @staticmethod
    def create_fov_grid_sequence(
        sequence_name: str,
        lens: str,
        pan_center: float,
        tilt_center: float,
        total_pan_deg: float,
        total_tilt_deg: float,
        overlap: float = 0.20,
        filter_positions: Optional[List[int]] = None,
        exposure_ms: int = 100,
        settle_time_s: float = 2.0,
        config_path: Optional[str] = None,
    ) -> Tuple["SequenceConfig", Dict[str, Any]]:
        """Create a grid sequence with spacing derived from lens FOV and overlap.

        Computes the per-frame field of view from the sensor geometry and lens
        focal length, then determines the grid step size to achieve the
        requested overlap fraction. The grid is centered on
        (pan_center, tilt_center) and sized to cover at least the requested
        angular extent.

        Parameters
        ----------
        sequence_name : str
            Name for this sequence.
        lens : str
            Lens identifier key in lens_specifications.json (e.g. "28mm", "50mm").
        pan_center : float
            Center of the survey in pan degrees.
        tilt_center : float
            Center of the survey in tilt degrees.
        total_pan_deg : float
            Minimum total pan coverage in degrees.
        total_tilt_deg : float
            Minimum total tilt coverage in degrees.
        overlap : float
            Fractional overlap between adjacent frames (0.0 to <1.0).
        filter_positions : list of int, optional
            Filter positions to capture at each grid point.
        exposure_ms : int
            Exposure time in milliseconds.
        settle_time_s : float
            Settle time after each PTU movement.
        config_path : str, optional
            Path to lens_specifications.json. If None, searches relative to
            the project config/ directory.

        Returns
        -------
        tuple of (SequenceConfig, dict)
            The configured sequence and a geometry summary dict containing
            FOV, step size, grid dimensions, and actual coverage.
        """
        # Load lens specifications
        if config_path is None:
            # Walk up from this file to find config/
            project_root = Path(__file__).resolve().parent.parent.parent
            config_path = project_root / "config" / "lens_specifications.json"
        else:
            config_path = Path(config_path)

        with open(config_path) as f:
            specs = json.load(f)

        if lens not in specs["lenses"]:
            available = ", ".join(specs["lenses"].keys())
            raise ValueError(
                f"Unknown lens '{lens}'. Available: {available}"
            )

        sensor = specs["sensor"]
        lens_spec = specs["lenses"][lens]
        focal_length = lens_spec["focal_length_mm"]

        # Compute field of view (degrees) using 2 * atan(sensor_dim / (2 * f))
        fov_h_deg = 2 * math.degrees(
            math.atan(sensor["width_mm"] / (2 * focal_length))
        )
        fov_v_deg = 2 * math.degrees(
            math.atan(sensor["height_mm"] / (2 * focal_length))
        )

        # Step size = FOV * (1 - overlap)
        step_pan = fov_h_deg * (1.0 - overlap)
        step_tilt = fov_v_deg * (1.0 - overlap)

        # Number of positions needed to cover the requested extent
        # At minimum we need ceil(extent / step) + 1 positions to span the
        # extent, but at least 1 position per axis
        n_pan = max(1, math.ceil(total_pan_deg / step_pan) + 1)
        n_tilt = max(1, math.ceil(total_tilt_deg / step_tilt) + 1)

        # Actual coverage
        actual_pan_coverage = (n_pan - 1) * step_pan + fov_h_deg
        actual_tilt_coverage = (n_tilt - 1) * step_tilt + fov_v_deg

        # Grid start positions (centered on pan_center, tilt_center)
        pan_start = pan_center - (n_pan - 1) * step_pan / 2
        tilt_start = tilt_center - (n_tilt - 1) * step_tilt / 2

        pan_end = pan_start + (n_pan - 1) * step_pan
        tilt_end = tilt_start + (n_tilt - 1) * step_tilt

        # Generate positions
        positions = []
        position_id = 0
        for i in range(n_pan):
            for j in range(n_tilt):
                pan_deg = pan_start + i * step_pan
                tilt_deg = tilt_start + j * step_tilt

                position = PositionTarget(
                    id=f"grid_{position_id:03d}",
                    pan_degrees=round(pan_deg, 4),
                    tilt_degrees=round(tilt_deg, 4),
                    settle_time_s=settle_time_s,
                    metadata={
                        "grid_position": {
                            "pan_index": i,
                            "tilt_index": j,
                        },
                        "lens": lens,
                        "overlap": overlap,
                    }
                )
                positions.append(position)
                position_id += 1

        sequence = SequenceConfig(
            sequence_name=sequence_name,
            positions=positions,
            filter_positions=filter_positions,
            exposure_ms=exposure_ms,
        )

        geometry = {
            "lens": lens,
            "lens_model": lens_spec["model"],
            "focal_length_mm": focal_length,
            "sensor_width_mm": sensor["width_mm"],
            "sensor_height_mm": sensor["height_mm"],
            "fov_h_deg": round(fov_h_deg, 2),
            "fov_v_deg": round(fov_v_deg, 2),
            "overlap": overlap,
            "step_pan_deg": round(step_pan, 2),
            "step_tilt_deg": round(step_tilt, 2),
            "n_pan": n_pan,
            "n_tilt": n_tilt,
            "total_positions": n_pan * n_tilt,
            "requested_pan_deg": total_pan_deg,
            "requested_tilt_deg": total_tilt_deg,
            "actual_pan_coverage_deg": round(actual_pan_coverage, 2),
            "actual_tilt_coverage_deg": round(actual_tilt_coverage, 2),
            "pan_range_deg": (round(pan_start, 2), round(pan_end, 2)),
            "tilt_range_deg": (round(tilt_start, 2), round(tilt_end, 2)),
            "center_pan_deg": pan_center,
            "center_tilt_deg": tilt_center,
        }

        return sequence, geometry

    @staticmethod
    def create_waypoint_sequence(
        sequence_name: str,
        waypoints: List[Tuple[float, float]],
        filter_positions: Optional[List[int]] = None,
        exposure_ms: int = 100,
        settle_time_s: float = 2.0
    ) -> SequenceConfig:
        """Create a sequence from a list of waypoint coordinates.

        Parameters
        ----------
        sequence_name : str
            Name for this sequence.
        waypoints : list of (float, float)
            List of (pan_degrees, tilt_degrees) tuples.
        filter_positions : list of int, optional
            Filter positions to capture at each waypoint.
        exposure_ms : int
            Exposure time in milliseconds.
        settle_time_s : float
            Settle time after each PTU movement.

        Returns
        -------
        SequenceConfig
            Configured sequence ready for execution.
        """
        positions = []

        for i, (pan_deg, tilt_deg) in enumerate(waypoints):
            position = PositionTarget(
                id=f"waypoint_{i:03d}",
                pan_degrees=pan_deg,
                tilt_degrees=tilt_deg,
                settle_time_s=settle_time_s,
                metadata={"waypoint_index": i}
            )
            positions.append(position)

        return SequenceConfig(
            sequence_name=sequence_name,
            positions=positions,
            filter_positions=filter_positions,
            exposure_ms=exposure_ms,
        )

    # ------------------------------------------------------------------
    # Geo-pointing methods (require GPM hardware)
    # ------------------------------------------------------------------

    def execute_single_geo_position(
        self,
        geo_target: GeoPositionTarget,
        filter_positions: Optional[List[int]] = None,
        exposure_ms: int = 100,
    ) -> Dict[str, Any]:
        """Execute acquisition at a single geographic coordinate.

        Uses the GPM to aim the PTU at the given latitude/longitude/altitude,
        then captures images through the requested filters.

        Parameters
        ----------
        geo_target : GeoPositionTarget
            Geographic target to aim at.
        filter_positions : list of int, optional
            Filter positions to capture. None = current filter only.
        exposure_ms : int
            Exposure time in milliseconds.

        Returns
        -------
        dict
            Result dictionary with status, captures, and timing info.

        Raises
        ------
        RuntimeError
            If GPM is not available on this PTU.
        """
        if self.ptu.gpm is None:
            raise RuntimeError(
                "GPM not available — geo-pointing requires a PTU with "
                "Geo Pointing Module hardware"
            )

        from ptu.gpm import GeoTarget

        result = {
            "target_id": geo_target.id,
            "latitude": geo_target.latitude,
            "longitude": geo_target.longitude,
            "altitude": geo_target.altitude,
            "status": "error",
            "captures": [],
            "start_time": datetime.now().isoformat(),
        }

        try:
            # Aim PTU at geographic coordinate
            target = GeoTarget(
                latitude=geo_target.latitude,
                longitude=geo_target.longitude,
                altitude=geo_target.altitude,
            )
            self.logger.info(
                f"Geo-pointing to {geo_target.id}: "
                f"lat={geo_target.latitude}, lon={geo_target.longitude}, "
                f"alt={geo_target.altitude}"
            )

            if not self.ptu.gpm.point_to_coordinate(target, wait=True):
                self.logger.error(
                    f"Geo-pointing failed for {geo_target.id}"
                )
                result["error"] = "geo-pointing command failed"
                return result

            # Settle
            if geo_target.settle_time_s > 0:
                time.sleep(geo_target.settle_time_s)

            # Read resulting pan/tilt position
            pan_deg, tilt_deg = self.ptu.get_position_degrees()

            # Create a temporary PositionTarget for metadata and capture
            temp_position = PositionTarget(
                id=geo_target.id,
                pan_degrees=pan_deg,
                tilt_degrees=tilt_deg,
                settle_time_s=0.0,  # already settled
                metadata=geo_target.metadata or {},
            )

            # Capture at each filter
            filters = filter_positions or [None]
            for filter_pos in filters:
                capture_result = self._capture_at_filter(
                    position=temp_position,
                    filter_position=filter_pos,
                    exposure_ms=exposure_ms,
                )
                result["captures"].append(capture_result)

            result["status"] = "success"
            result["pan_degrees"] = pan_deg
            result["tilt_degrees"] = tilt_deg

        except Exception as e:
            self.logger.error(
                f"Error at geo target {geo_target.id}: {e}"
            )
            result["error"] = str(e)

        result["end_time"] = datetime.now().isoformat()
        return result

    def execute_geo_sequence(
        self,
        sequence_name: str,
        geo_targets: List[GeoPositionTarget],
        filter_positions: Optional[List[int]] = None,
        exposure_ms: int = 100,
        continue_on_error: bool = True,
    ) -> Dict[str, Any]:
        """Execute an acquisition sequence over geographic targets.

        Iterates through a list of geographic coordinates, aiming the PTU
        at each one via the GPM and capturing images.

        Parameters
        ----------
        sequence_name : str
            Name for this geo-pointing sequence.
        geo_targets : list of GeoPositionTarget
            Geographic targets to visit.
        filter_positions : list of int, optional
            Filter positions to capture at each target.
        exposure_ms : int
            Exposure time in milliseconds.
        continue_on_error : bool
            If True, continue to next target on error.

        Returns
        -------
        dict
            Summary dictionary with overall status and per-target results.

        Raises
        ------
        RuntimeError
            If GPM is not available on this PTU.
        """
        if self.ptu.gpm is None:
            raise RuntimeError(
                "GPM not available — geo-pointing requires a PTU with "
                "Geo Pointing Module hardware"
            )

        self.logger.info(
            f"Starting geo-sequence '{sequence_name}' with "
            f"{len(geo_targets)} targets"
        )

        start_time = time.time()
        results = []
        successful = 0

        for i, geo_target in enumerate(geo_targets):
            self.logger.info(
                f"Geo target {i + 1}/{len(geo_targets)}: {geo_target.id}"
            )

            target_result = self.execute_single_geo_position(
                geo_target=geo_target,
                filter_positions=filter_positions,
                exposure_ms=exposure_ms,
            )
            results.append(target_result)

            if target_result["status"] == "success":
                successful += 1
            elif not continue_on_error:
                self.logger.error(
                    f"Stopping geo-sequence on error at {geo_target.id}"
                )
                break

        total_time = time.time() - start_time
        total = len(geo_targets)

        summary = {
            "sequence_name": sequence_name,
            "status": "completed" if successful == total else "partial",
            "total_targets": total,
            "successful_targets": successful,
            "success_rate": successful / total if total > 0 else 0.0,
            "total_duration_s": round(total_time, 2),
            "average_time_per_target_s": round(
                total_time / total if total > 0 else 0.0, 2
            ),
            "results": results,
        }

        self.logger.info(
            f"Geo-sequence '{sequence_name}' complete: "
            f"{successful}/{total} targets successful in {total_time:.1f}s"
        )

        return summary

    @staticmethod
    def create_geo_waypoint_sequence(
        sequence_name: str,
        geo_waypoints: List[Tuple[float, float, float]],
        settle_time_s: float = 2.0,
    ) -> List[GeoPositionTarget]:
        """Create a list of GeoPositionTargets from coordinate tuples.

        Parameters
        ----------
        sequence_name : str
            Base name for target IDs.
        geo_waypoints : list of (float, float, float)
            List of (latitude, longitude, altitude) tuples.
        settle_time_s : float
            Settle time after each movement.

        Returns
        -------
        list of GeoPositionTarget
            Targets ready for ``execute_geo_sequence()``.
        """
        targets = []
        for i, (lat, lon, alt) in enumerate(geo_waypoints):
            target = GeoPositionTarget(
                id=f"{sequence_name}_geo_{i:03d}",
                latitude=lat,
                longitude=lon,
                altitude=alt,
                settle_time_s=settle_time_s,
                metadata={"waypoint_index": i},
            )
            targets.append(target)
        return targets
