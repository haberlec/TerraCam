"""
FLI System Module

Provides unified device discovery and management for FLI cameras and filter wheels.
This module centralizes device handling logic previously scattered across multiple scripts.

Usage:
    from fli import FLISystem

    system = FLISystem()
    system.discover_devices()
    system.initialize(target_temp=-20)
    image = system.capture_image(exposure_ms=100)
"""

import time
import logging
from typing import Optional, Tuple, Dict, Any
from ctypes import POINTER, c_char_p, byref

from .core.camera import USBCamera
from .core.filter_wheel import USBFilterWheel
from .core.lib import (
    FLILibrary, FLIDOMAIN_USB, FLIDEVICE_CAMERA, FLIDEVICE_FILTERWHEEL,
    flidomain_t, FLIDEBUG_INFO, FLIDEBUG_WARN, FLIDEBUG_FAIL
)
from .acquisition import ImageAcquisition


class FLISystem:
    """Unified FLI device management system.

    This class provides:
    - Device discovery for cameras and filter wheels
    - Temperature control and monitoring
    - Filter wheel positioning
    - Image acquisition through ImageAcquisition
    - Logging configuration

    Attributes:
        camera: USBCamera instance (None until discovered)
        filter_wheel: USBFilterWheel instance (None until discovered)
        acquisition: ImageAcquisition instance (None until camera discovered)
        logger: Logger for system messages
    """

    def __init__(self, logger: Optional[logging.Logger] = None):
        """Initialize FLI system.

        Args:
            logger: Optional logger instance. If None, creates a default logger.
        """
        self.camera: Optional[USBCamera] = None
        self.filter_wheel: Optional[USBFilterWheel] = None
        self.acquisition: Optional[ImageAcquisition] = None
        self.lib = FLILibrary.getDll()
        self.logger = logger or self._create_default_logger()

        # Suppress verbose C library debug output (WARN + FAIL only)
        self._set_fli_debug_level()

        # Device info cache
        self._camera_model: Optional[str] = None
        self._filter_wheel_model: Optional[str] = None

    def _create_default_logger(self) -> logging.Logger:
        """Create a default logger for the system."""
        logger = logging.getLogger('FLISystem')
        if not logger.handlers:
            logger.setLevel(logging.INFO)
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter('%(message)s'))
            logger.addHandler(handler)
        return logger

    def setup_logging(self, log_filename: str,
                      console_output: bool = True) -> logging.Logger:
        """Configure logging to file and optionally console.

        Args:
            log_filename: Path to log file
            console_output: Whether to also log to console (default: True)

        Returns:
            Configured logger instance
        """
        self.logger = logging.getLogger('FLISystem')
        self.logger.setLevel(logging.INFO)
        self.logger.handlers = []  # Clear existing handlers

        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

        # File handler
        file_handler = logging.FileHandler(log_filename)
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(formatter)
        self.logger.addHandler(file_handler)

        # Console handler
        if console_output:
            console_handler = logging.StreamHandler()
            console_handler.setLevel(logging.INFO)
            console_handler.setFormatter(logging.Formatter('%(message)s'))
            self.logger.addHandler(console_handler)

        return self.logger

    def _set_fli_debug_level(self, level: Optional[int] = None):
        """Set FLI library debug level to reduce verbose USB logging.

        Parameters
        ----------
        level : int, optional
            Bitmask of FLIDEBUG_* flags. Defaults to WARN | FAIL.
        """
        try:
            if level is None:
                level = FLIDEBUG_WARN | FLIDEBUG_FAIL
            self.lib.FLISetDebugLevel(None, level)
        except Exception:
            pass  # Non-critical

    def discover_devices(self, require_camera: bool = True,
                         require_filter_wheel: bool = True) -> bool:
        """Discover and connect to FLI camera and filter wheel.

        Args:
            require_camera: Raise error if no camera found (default: True)
            require_filter_wheel: Raise error if no filter wheel found (default: True)

        Returns:
            True if all required devices found

        Raises:
            RuntimeError: If required device not found
        """
        self.logger.info("FLI Device Discovery")

        # Find camera
        self._discover_camera()

        # Find filter wheel
        self._discover_filter_wheel()

        # Verify required devices
        if require_camera and not self.camera:
            raise RuntimeError("No camera found!")
        if require_filter_wheel and not self.filter_wheel:
            raise RuntimeError("No filter wheel found!")

        # Create acquisition instance if camera found
        if self.camera:
            self.acquisition = ImageAcquisition(
                self.camera,
                logger=self.logger,
                reconnect_callback=self._reconnect_devices
            )

        self.logger.info("Device discovery complete")
        return True

    def _discover_camera(self):
        """Discover and connect to camera."""
        self.logger.info("1. Searching for camera...")

        try:
            cam_domain = flidomain_t(FLIDOMAIN_USB | FLIDEVICE_CAMERA)
            tmplist = POINTER(c_char_p)()
            self.lib.FLIList(cam_domain, byref(tmplist))

            if tmplist:
                i = 0
                while tmplist[i]:
                    device_info = tmplist[i].decode('utf-8')
                    dev_name, model = device_info.split(';')
                    self.logger.info(f"   Found: {dev_name} - {model}")

                    # Connect to MicroLine camera
                    if 'MicroLine' in model and 'ML' in model:
                        self.logger.info("   -> Connecting to camera...")
                        self.camera = USBCamera(dev_name.encode(), model.encode())
                        self._camera_model = model
                        self.logger.info(f"   Camera connected: {model}")
                        break
                    i += 1
                # Note: Don't free tmplist - managed by FLI library

        except Exception as e:
            self.logger.error(f"   Camera discovery failed: {e}")
            raise

    def _discover_filter_wheel(self):
        """Discover and connect to filter wheel."""
        self.logger.info("2. Searching for filter wheel...")

        try:
            fw_domain = flidomain_t(FLIDOMAIN_USB | FLIDEVICE_FILTERWHEEL)
            tmplist = POINTER(c_char_p)()
            self.lib.FLIList(fw_domain, byref(tmplist))

            if tmplist:
                i = 0
                while tmplist[i]:
                    device_info = tmplist[i].decode('utf-8')
                    dev_name, model = device_info.split(';')
                    self.logger.info(f"   Found: {dev_name} - {model}")

                    # Connect to CenterLine filter wheel
                    if 'Filter Wheel' in model or 'CenterLine' in model:
                        self.logger.info("   -> Connecting to filter wheel...")
                        self.filter_wheel = USBFilterWheel(dev_name.encode(), model.encode())
                        self._filter_wheel_model = model
                        self.logger.info(f"   Filter wheel connected: {model}")
                        break
                    i += 1
                # Note: Don't free tmplist - managed by FLI library

        except Exception as e:
            self.logger.error(f"   Filter wheel discovery failed: {e}")
            raise

    def _reconnect_devices(self) -> bool:
        """Reconnect USB devices to recover from communication errors.

        Returns:
            True if reconnection successful
        """
        self.logger.info("Reconnecting USB devices...")

        # Store current models
        camera_model = self._camera_model
        fw_model = self._filter_wheel_model

        # Close existing connections
        if self.camera:
            try:
                self.camera.close()
            except:
                pass
            self.camera = None

        if self.filter_wheel:
            try:
                self.filter_wheel.close()
            except:
                pass
            self.filter_wheel = None

        # Brief pause for USB reset
        time.sleep(2)

        # Rediscover devices
        try:
            self.discover_devices()
            self.logger.info("Device reconnection completed")
            return True
        except Exception as e:
            self.logger.error(f"Device reconnection failed: {e}")
            return False

    def initialize(self, target_temp: float = -20.0,
                   wait_for_temp: bool = False,
                   temp_timeout_minutes: int = 10) -> bool:
        """Initialize system with temperature control.

        Args:
            target_temp: Target CCD temperature in Celsius (default: -20)
            wait_for_temp: Wait for temperature to stabilize (default: False)
            temp_timeout_minutes: Timeout for temperature stabilization

        Returns:
            True if initialization successful
        """
        if not self.camera:
            raise RuntimeError("No camera connected. Call discover_devices() first.")

        self.logger.info(f"Initializing system (target temp: {target_temp}C)")

        # Set temperature
        self.set_temperature(target_temp)

        # Wait for stabilization if requested
        if wait_for_temp:
            return self.wait_for_temperature(
                target_temp, timeout_minutes=temp_timeout_minutes
            )

        return True

    def set_temperature(self, target_temp: float):
        """Set camera CCD temperature.

        Args:
            target_temp: Target temperature in Celsius
        """
        if not self.camera:
            raise RuntimeError("No camera connected")

        self.logger.info(f"Setting camera temperature to {target_temp}C...")
        self.camera.set_temperature(target_temp)

        # Brief monitoring
        for i in range(3):
            current_temp = self.camera.get_temperature()
            self.logger.info(f"   Temperature reading: {current_temp:.1f}C")
            time.sleep(1)

    def get_temperature(self) -> float:
        """Get current CCD temperature.

        Returns:
            Temperature in Celsius
        """
        if not self.camera:
            raise RuntimeError("No camera connected")
        return self.camera.get_temperature()

    def wait_for_temperature(self, target_temp: float,
                              timeout_minutes: int = 10,
                              tolerance: float = 1.0,
                              required_stable_readings: int = 5) -> bool:
        """Wait for temperature to stabilize.

        Args:
            target_temp: Target temperature in Celsius
            timeout_minutes: Maximum wait time
            tolerance: Acceptable temperature deviation
            required_stable_readings: Number of consecutive readings within tolerance

        Returns:
            True if temperature stabilized, False if timeout
        """
        self.logger.info(f"Waiting for temperature to stabilize at {target_temp}C...")

        start_time = time.time()
        timeout_seconds = timeout_minutes * 60
        stable_readings = 0

        while time.time() - start_time < timeout_seconds:
            current_temp = self.camera.get_temperature()
            temp_diff = abs(current_temp - target_temp)

            self.logger.info(
                f"   Current: {current_temp:.1f}C "
                f"(target: {target_temp:.1f}C, diff: {temp_diff:.1f}C)"
            )

            if temp_diff <= tolerance:
                stable_readings += 1
                if stable_readings >= required_stable_readings:
                    self.logger.info(f"Temperature stabilized at {current_temp:.1f}C")
                    return True
            else:
                stable_readings = 0

            time.sleep(30)

        self.logger.warning(
            f"Temperature stabilization timeout after {timeout_minutes} minutes"
        )
        return False

    def move_filter(self, position: int, verify: bool = True) -> bool:
        """Move filter wheel to specified position.

        Args:
            position: Target filter position (0-indexed)
            verify: Verify final position matches request (default: True)

        Returns:
            True if movement successful

        Raises:
            ValueError: If position is invalid
            RuntimeError: If movement fails
        """
        if not self.filter_wheel:
            raise RuntimeError("No filter wheel connected")

        current_pos = self.filter_wheel.get_filter_pos()
        total_positions = self.filter_wheel.get_filter_count()

        self.logger.info(f"Moving filter wheel from {current_pos} to {position}")

        if position < 0 or position >= total_positions:
            raise ValueError(
                f"Invalid filter position {position} "
                f"(valid range: 0-{total_positions-1})"
            )

        if current_pos == position:
            self.logger.info(f"Filter already at position {position}")
            return True

        # Move to position
        self.filter_wheel.set_filter_pos(position)

        # Wait for movement
        self.logger.info("Waiting for filter wheel movement...")
        completed = self.filter_wheel.wait_for_movement_completion(timeout_seconds=30)

        if not completed:
            raise RuntimeError("Filter wheel movement timeout")

        # Verify position
        if verify:
            new_pos = self.filter_wheel.get_filter_pos()
            if new_pos != position:
                raise RuntimeError(
                    f"Filter wheel at position {new_pos} (requested {position})"
                )

        self.logger.info(f"Filter wheel moved to position {position}")
        return True

    def get_filter_position(self) -> int:
        """Get current filter wheel position.

        Returns:
            Current position (0-indexed)
        """
        if not self.filter_wheel:
            raise RuntimeError("No filter wheel connected")
        return self.filter_wheel.get_filter_pos()

    def get_filter_count(self) -> int:
        """Get total number of filter positions.

        Returns:
            Number of filter positions
        """
        if not self.filter_wheel:
            raise RuntimeError("No filter wheel connected")
        return self.filter_wheel.get_filter_count()

    def capture_image(self, exposure_ms: int = None,
                      frame_type: str = "normal",
                      **kwargs) -> 'np.ndarray':
        """Capture a single image using the robust acquisition system.

        Args:
            exposure_ms: Exposure time in milliseconds
            frame_type: "normal" or "dark"
            **kwargs: Additional arguments passed to ImageAcquisition.capture()

        Returns:
            numpy.ndarray: Image data
        """
        if not self.acquisition:
            raise RuntimeError("No camera connected. Call discover_devices() first.")

        return self.acquisition.capture(exposure_ms, frame_type, **kwargs)

    def capture_sequence(self, exposure_ms: int, num_frames: int,
                         frame_type: str = "normal", **kwargs):
        """Capture multiple frames.

        Args:
            exposure_ms: Exposure time in milliseconds
            num_frames: Number of frames to capture
            frame_type: "normal" or "dark"
            **kwargs: Additional arguments passed to ImageAcquisition.capture_sequence()

        Returns:
            List of numpy.ndarray images
        """
        if not self.acquisition:
            raise RuntimeError("No camera connected. Call discover_devices() first.")

        return self.acquisition.capture_sequence(
            exposure_ms, num_frames, frame_type, **kwargs
        )

    def get_camera_info(self) -> Dict[str, Any]:
        """Get camera information.

        Returns:
            Dictionary with camera specifications
        """
        if not self.camera:
            raise RuntimeError("No camera connected")
        return self.camera.get_info()

    def close(self):
        """Close all device connections."""
        if self.camera:
            try:
                self.camera.close()
            except:
                pass
            self.camera = None

        if self.filter_wheel:
            try:
                self.filter_wheel.close()
            except:
                pass
            self.filter_wheel = None

        self.acquisition = None
        self.logger.info("All devices closed")

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - ensures devices are closed."""
        self.close()
        return False
