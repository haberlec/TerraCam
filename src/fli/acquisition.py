"""
FLI Image Acquisition Module

Provides robust image acquisition with USB error recovery, retry logic,
and image validation. This module centralizes the battle-tested acquisition
logic from the radiometric calibration workflow.

Usage:
    from fli.acquisition import ImageAcquisition
    from fli.core import USBCamera

    camera = USBCamera(...)
    acq = ImageAcquisition(camera)
    image = acq.capture(exposure_ms=100)
"""

import time
import logging
import numpy as np
from typing import Optional, List, Tuple, Callable


class ImageAcquisition:
    """Robust image acquisition with USB error recovery and validation.

    This class provides centralized image acquisition logic with:
    - USB error detection and retry (especially for macOS)
    - Zero-sum image detection
    - Progressive retry with delays
    - Image validation
    - Device reconnection support

    Attributes:
        camera: USBCamera instance
        logger: Logger for acquisition messages
        reconnect_callback: Optional callback for device reconnection
    """

    # USB error patterns (macOS specific)
    USB_ERROR_PATTERNS = [
        ("mac_usb_piperead", "e00002e8"),  # macOS USB pipe read error
        ("kIOReturnAborted", ""),           # USB transfer aborted
        ("kIOUSBPipeStalled", ""),          # USB pipe stalled
    ]

    def __init__(self, camera, logger: Optional[logging.Logger] = None,
                 reconnect_callback: Optional[Callable] = None):
        """Initialize acquisition with camera instance.

        Args:
            camera: USBCamera instance to use for acquisition
            logger: Optional logger (creates default if None)
            reconnect_callback: Optional function to call for device reconnection.
                               Should take no arguments and return True on success.
        """
        self.camera = camera
        self.logger = logger or logging.getLogger(__name__)
        self.reconnect_callback = reconnect_callback

        # Acquisition settings
        self.default_max_retries = 3
        self.retry_delay_seconds = 3.0
        self.usb_error_retry_delay = 5.0
        self.post_reconnect_delay = 2.0

    def capture(self, exposure_ms: int = None, frame_type: str = "normal",
                max_retries: int = None) -> np.ndarray:
        """Capture a single image with retry logic and validation.

        Args:
            exposure_ms: Exposure time in milliseconds. If None, uses camera's
                        current exposure setting.
            frame_type: "normal" or "dark" frame type
            max_retries: Maximum retry attempts (default: 3)

        Returns:
            numpy.ndarray: Image data as 2D array

        Raises:
            RuntimeError: If capture fails after all retries
        """
        max_retries = max_retries or self.default_max_retries

        # Set exposure if specified
        if exposure_ms is not None:
            self.camera.set_exposure(exposure_ms, frametype=frame_type)

        for attempt in range(max_retries):
            try:
                # Ensure camera is idle before capture
                if hasattr(self.camera, 'wait_for_idle'):
                    if not self.camera.wait_for_idle(timeout_seconds=10):
                        self.logger.warning(
                            f"Camera not idle before {frame_type} capture"
                        )

                # Acquire frame
                image_array = self.camera.take_photo()

                # Validate the image
                validation_result, validation_msg = self.validate_image(image_array)

                if validation_result:
                    return image_array
                else:
                    self.logger.warning(
                        f"Image validation failed on attempt {attempt + 1}: {validation_msg}"
                    )
                    if attempt < max_retries - 1:
                        self.logger.info(f"Waiting {self.retry_delay_seconds}s before retry...")
                        time.sleep(self.retry_delay_seconds)
                        continue
                    else:
                        raise RuntimeError(
                            f"Image validation failed after {max_retries} attempts: {validation_msg}"
                        )

            except Exception as e:
                error_msg = str(e)

                # Check for USB errors
                if self._is_usb_error(error_msg):
                    self.logger.warning(
                        f"USB communication error on {frame_type} frame, "
                        f"attempt {attempt + 1}/{max_retries}"
                    )
                    self.logger.warning(f"Error: {error_msg}")

                    if attempt < max_retries - 1:
                        self.logger.info(
                            f"Retrying in {self.usb_error_retry_delay}s..."
                        )
                        time.sleep(self.usb_error_retry_delay)

                        # Attempt device reconnection if callback provided
                        if self.reconnect_callback:
                            try:
                                self.logger.info("Attempting device reconnection...")
                                if self.reconnect_callback():
                                    # Re-set exposure parameters after reconnection
                                    if exposure_ms is not None:
                                        self.camera.set_exposure(
                                            exposure_ms, frametype=frame_type
                                        )
                                    time.sleep(self.post_reconnect_delay)
                            except Exception as reconnect_error:
                                self.logger.warning(
                                    f"Device reconnection failed: {reconnect_error}"
                                )
                        continue
                    else:
                        raise RuntimeError(
                            f"USB communication failed after {max_retries} attempts: {error_msg}"
                        )
                else:
                    # Non-USB error
                    self.logger.warning(
                        f"Capture error on {frame_type} frame, "
                        f"attempt {attempt + 1}/{max_retries}: {error_msg}"
                    )

                    if attempt < max_retries - 1:
                        self.logger.info(
                            f"Retrying in {self.retry_delay_seconds}s..."
                        )
                        time.sleep(self.retry_delay_seconds)
                        continue
                    else:
                        raise RuntimeError(
                            f"Capture failed after {max_retries} attempts: {error_msg}"
                        )

        # Should not reach here
        raise RuntimeError("Capture failed - unexpected code path")

    def capture_sequence(self, exposure_ms: int, num_frames: int,
                         frame_type: str = "normal",
                         frame_callback: Optional[Callable] = None,
                         inter_frame_delay: float = 0.5) -> List[np.ndarray]:
        """Capture multiple frames with consistent settings.

        Args:
            exposure_ms: Exposure time in milliseconds
            num_frames: Number of frames to capture
            frame_type: "normal" or "dark" frame type
            frame_callback: Optional callback(frame_num, image) called after each frame
            inter_frame_delay: Delay between frames in seconds (default: 0.5)

        Returns:
            List of numpy.ndarray images

        Raises:
            RuntimeError: If any frame capture fails
        """
        self.logger.info(
            f"Capturing {num_frames} {frame_type} frames at {exposure_ms}ms exposure"
        )

        # Set exposure once for the sequence
        self.camera.set_exposure(exposure_ms, frametype=frame_type)

        frames = []
        for i in range(num_frames):
            self.logger.info(f"Frame {i+1}/{num_frames}")

            # Capture with retry logic (exposure already set)
            image_array = self.capture(frame_type=frame_type)

            if image_array is None or image_array.size == 0:
                raise RuntimeError(f"Failed to acquire {frame_type} frame {i+1}")

            frames.append(image_array)

            # Call frame callback if provided
            if frame_callback:
                frame_callback(i, image_array)

            # Inter-frame delay (skip after last frame)
            if i < num_frames - 1:
                time.sleep(inter_frame_delay)

        self.logger.info(f"Captured {len(frames)} {frame_type} frames")
        return frames

    def capture_dark_frames(self, exposure_ms: int, num_frames: int,
                           **kwargs) -> List[np.ndarray]:
        """Convenience method for dark frame acquisition.

        Args:
            exposure_ms: Exposure time in milliseconds
            num_frames: Number of dark frames to capture
            **kwargs: Additional arguments passed to capture_sequence

        Returns:
            List of dark frame numpy.ndarray images
        """
        return self.capture_sequence(
            exposure_ms, num_frames, frame_type="dark", **kwargs
        )

    def capture_light_frames(self, exposure_ms: int, num_frames: int,
                            **kwargs) -> List[np.ndarray]:
        """Convenience method for light frame acquisition.

        Args:
            exposure_ms: Exposure time in milliseconds
            num_frames: Number of light frames to capture
            **kwargs: Additional arguments passed to capture_sequence

        Returns:
            List of light frame numpy.ndarray images
        """
        return self.capture_sequence(
            exposure_ms, num_frames, frame_type="normal", **kwargs
        )

    def validate_image(self, image_array: np.ndarray) -> Tuple[bool, str]:
        """Validate that image contains reasonable data.

        Checks for:
        - Null or empty images
        - All-zero images (common USB failure mode)
        - Very low non-zero pixel fraction
        - Constant pixel values (no variation)

        Args:
            image_array: Image data to validate

        Returns:
            Tuple of (is_valid, message)
        """
        # Check for null/empty
        if image_array is None:
            return False, "Image is None"

        if image_array.size == 0:
            return False, "Image is empty"

        # Check for all-zero images (common failure mode)
        image_sum = np.sum(image_array)
        if image_sum == 0:
            return False, "All pixels are zero (zero-sum image)"

        # Check for reasonable pixel value distribution
        non_zero_pixels = np.count_nonzero(image_array)
        total_pixels = image_array.size
        non_zero_fraction = non_zero_pixels / total_pixels

        if non_zero_fraction < 0.1:  # Less than 10% non-zero pixels
            return False, f"Only {non_zero_fraction:.1%} pixels are non-zero"

        # Check for reasonable value range
        min_val = np.min(image_array)
        max_val = np.max(image_array)

        if max_val == min_val:
            return False, f"All pixels have same value ({min_val})"

        # Check for very low variation (may indicate partial transfer)
        std_val = np.std(image_array)
        if std_val < 1.0:
            # Warning but not failure - might be valid for very uniform scenes
            self.logger.debug(f"Very low pixel variation (std={std_val:.2f})")

        return True, f"Valid: shape={image_array.shape}, range={min_val}-{max_val}"

    def get_image_statistics(self, image_array: np.ndarray) -> dict:
        """Calculate statistics for an image.

        Args:
            image_array: Image data

        Returns:
            Dictionary with image statistics
        """
        return {
            'shape': image_array.shape,
            'dtype': str(image_array.dtype),
            'min': int(np.min(image_array)),
            'max': int(np.max(image_array)),
            'mean': float(np.mean(image_array)),
            'std': float(np.std(image_array)),
            'median': float(np.median(image_array)),
            'sum': float(np.sum(image_array)),
            'non_zero_fraction': float(np.count_nonzero(image_array) / image_array.size),
        }

    def _is_usb_error(self, error_msg: str) -> bool:
        """Check if error message indicates a USB communication error.

        Args:
            error_msg: Error message string

        Returns:
            True if error appears to be USB-related
        """
        error_lower = error_msg.lower()

        for pattern, code in self.USB_ERROR_PATTERNS:
            if pattern.lower() in error_lower:
                if not code or code.lower() in error_lower:
                    return True

        return False
