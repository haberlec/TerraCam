"""
Unit tests for fli.acquisition module

Tests the ImageAcquisition class without requiring hardware.
Uses mock camera objects to test validation and error handling logic.
"""

import pytest
import numpy as np
from unittest.mock import Mock, MagicMock
import logging

from fli.acquisition import ImageAcquisition


class TestImageValidation:
    """Tests for ImageAcquisition.validate_image()"""

    @pytest.fixture
    def acquisition(self):
        """Create ImageAcquisition with mock camera."""
        mock_camera = Mock()
        return ImageAcquisition(mock_camera)

    def test_validate_image_valid(self, acquisition):
        """Valid image with normal data should pass validation."""
        # Create a realistic image with noise
        image = np.random.randint(100, 50000, size=(1024, 1024), dtype=np.uint16)

        is_valid, message = acquisition.validate_image(image)

        assert is_valid is True
        assert "Valid" in message

    def test_validate_image_null(self, acquisition):
        """None image should fail validation."""
        is_valid, message = acquisition.validate_image(None)

        assert is_valid is False
        assert "None" in message

    def test_validate_image_empty(self, acquisition):
        """Empty image should fail validation."""
        image = np.array([], dtype=np.uint16)

        is_valid, message = acquisition.validate_image(image)

        assert is_valid is False
        assert "empty" in message.lower()

    def test_validate_image_zero_sum(self, acquisition):
        """All-zero image should fail validation (common USB failure mode)."""
        image = np.zeros((1024, 1024), dtype=np.uint16)

        is_valid, message = acquisition.validate_image(image)

        assert is_valid is False
        assert "zero" in message.lower()

    def test_validate_image_low_nonzero(self, acquisition):
        """Image with less than 10% non-zero pixels should fail."""
        # Create image with only 5% non-zero pixels
        image = np.zeros((1000, 1000), dtype=np.uint16)
        image[:50, :] = 1000  # Only 5% of pixels are non-zero

        is_valid, message = acquisition.validate_image(image)

        assert is_valid is False
        assert "non-zero" in message.lower() or "pixels" in message.lower()

    def test_validate_image_constant(self, acquisition):
        """Image with all same value should fail validation."""
        image = np.full((1024, 1024), 5000, dtype=np.uint16)

        is_valid, message = acquisition.validate_image(image)

        assert is_valid is False
        assert "same value" in message.lower()

    def test_validate_image_dark_frame(self, acquisition):
        """Dark frame with low but varying values should pass."""
        # Dark frame: low values with some noise
        image = np.random.randint(50, 200, size=(1024, 1024), dtype=np.uint16)

        is_valid, message = acquisition.validate_image(image)

        assert is_valid is True


class TestImageStatistics:
    """Tests for ImageAcquisition.get_image_statistics()"""

    @pytest.fixture
    def acquisition(self):
        """Create ImageAcquisition with mock camera."""
        mock_camera = Mock()
        return ImageAcquisition(mock_camera)

    def test_get_image_statistics_basic(self, acquisition):
        """Test statistics calculation on simple image."""
        image = np.array([[100, 200], [300, 400]], dtype=np.uint16)

        stats = acquisition.get_image_statistics(image)

        assert stats['shape'] == (2, 2)
        assert stats['min'] == 100
        assert stats['max'] == 400
        assert stats['mean'] == 250.0
        assert stats['non_zero_fraction'] == 1.0

    def test_get_image_statistics_with_zeros(self, acquisition):
        """Test statistics calculation with zero values."""
        image = np.array([[0, 100], [200, 0]], dtype=np.uint16)

        stats = acquisition.get_image_statistics(image)

        assert stats['min'] == 0
        assert stats['max'] == 200
        assert stats['non_zero_fraction'] == 0.5

    def test_get_image_statistics_large_image(self, acquisition):
        """Test statistics on larger image."""
        image = np.random.randint(0, 65535, size=(2048, 2048), dtype=np.uint16)

        stats = acquisition.get_image_statistics(image)

        assert stats['shape'] == (2048, 2048)
        assert 'uint16' in stats['dtype']
        assert 0 <= stats['min'] <= 65535
        assert 0 <= stats['max'] <= 65535


class TestUSBErrorDetection:
    """Tests for USB error pattern detection."""

    @pytest.fixture
    def acquisition(self):
        """Create ImageAcquisition with mock camera."""
        mock_camera = Mock()
        return ImageAcquisition(mock_camera)

    def test_is_usb_error_mac_pipe_read(self, acquisition):
        """Detect macOS USB pipe read error."""
        error_msg = "mac_usb_piperead: read error: e00002e8"

        assert acquisition._is_usb_error(error_msg) is True

    def test_is_usb_error_aborted(self, acquisition):
        """Detect USB transfer aborted error."""
        error_msg = "kIOReturnAborted: transfer was aborted"

        assert acquisition._is_usb_error(error_msg) is True

    def test_is_usb_error_pipe_stalled(self, acquisition):
        """Detect USB pipe stalled error."""
        error_msg = "kIOUSBPipeStalled: pipe stall detected"

        assert acquisition._is_usb_error(error_msg) is True

    def test_is_usb_error_not_usb(self, acquisition):
        """Non-USB error should not be detected as USB error."""
        error_msg = "ValueError: invalid exposure time"

        assert acquisition._is_usb_error(error_msg) is False

    def test_is_usb_error_empty(self, acquisition):
        """Empty error message should not be USB error."""
        assert acquisition._is_usb_error("") is False


class TestCaptureWithMock:
    """Tests for capture functionality with mocked camera."""

    @pytest.fixture
    def mock_camera(self):
        """Create a mock camera that returns valid images."""
        camera = Mock()
        camera.wait_for_idle = Mock(return_value=True)
        camera.set_exposure = Mock()
        camera.take_photo = Mock(
            return_value=np.random.randint(100, 50000, size=(1024, 1024), dtype=np.uint16)
        )
        return camera

    def test_capture_success(self, mock_camera):
        """Test successful capture returns image."""
        acquisition = ImageAcquisition(mock_camera)

        image = acquisition.capture(exposure_ms=100, frame_type="normal")

        assert image is not None
        assert image.shape == (1024, 1024)
        mock_camera.set_exposure.assert_called_once_with(100, frametype="normal")
        mock_camera.take_photo.assert_called_once()

    def test_capture_sets_exposure(self, mock_camera):
        """Test capture sets exposure on camera."""
        acquisition = ImageAcquisition(mock_camera)

        acquisition.capture(exposure_ms=500, frame_type="dark")

        mock_camera.set_exposure.assert_called_with(500, frametype="dark")

    def test_capture_retry_on_zero_image(self, mock_camera):
        """Test capture retries when receiving zero image."""
        # First call returns zero image, second returns valid
        mock_camera.take_photo = Mock(side_effect=[
            np.zeros((1024, 1024), dtype=np.uint16),
            np.random.randint(100, 50000, size=(1024, 1024), dtype=np.uint16)
        ])

        acquisition = ImageAcquisition(mock_camera)
        acquisition.retry_delay_seconds = 0.01  # Speed up test

        image = acquisition.capture(exposure_ms=100, max_retries=3)

        assert image is not None
        assert np.sum(image) > 0
        assert mock_camera.take_photo.call_count == 2

    def test_capture_fails_after_max_retries(self, mock_camera):
        """Test capture raises error after exhausting retries."""
        # Always return zero image
        mock_camera.take_photo = Mock(
            return_value=np.zeros((1024, 1024), dtype=np.uint16)
        )

        acquisition = ImageAcquisition(mock_camera)
        acquisition.retry_delay_seconds = 0.01

        with pytest.raises(RuntimeError, match="validation failed"):
            acquisition.capture(exposure_ms=100, max_retries=2)


class TestCaptureSequence:
    """Tests for capture_sequence functionality."""

    @pytest.fixture
    def mock_camera(self):
        """Create a mock camera for sequence capture."""
        camera = Mock()
        camera.wait_for_idle = Mock(return_value=True)
        camera.set_exposure = Mock()
        camera.take_photo = Mock(
            return_value=np.random.randint(100, 50000, size=(512, 512), dtype=np.uint16)
        )
        return camera

    def test_capture_sequence_returns_list(self, mock_camera):
        """Test capture_sequence returns list of images."""
        acquisition = ImageAcquisition(mock_camera)
        acquisition.retry_delay_seconds = 0.01

        frames = acquisition.capture_sequence(
            exposure_ms=100, num_frames=3, inter_frame_delay=0.01
        )

        assert len(frames) == 3
        for frame in frames:
            assert frame.shape == (512, 512)

    def test_capture_dark_frames(self, mock_camera):
        """Test capture_dark_frames convenience method."""
        acquisition = ImageAcquisition(mock_camera)
        acquisition.retry_delay_seconds = 0.01

        frames = acquisition.capture_dark_frames(
            exposure_ms=100, num_frames=2, inter_frame_delay=0.01
        )

        assert len(frames) == 2
        mock_camera.set_exposure.assert_called_with(100, frametype="dark")

    def test_capture_light_frames(self, mock_camera):
        """Test capture_light_frames convenience method."""
        acquisition = ImageAcquisition(mock_camera)
        acquisition.retry_delay_seconds = 0.01

        frames = acquisition.capture_light_frames(
            exposure_ms=100, num_frames=2, inter_frame_delay=0.01
        )

        assert len(frames) == 2
        mock_camera.set_exposure.assert_called_with(100, frametype="normal")

    def test_capture_sequence_with_callback(self, mock_camera):
        """Test frame callback is called for each frame."""
        acquisition = ImageAcquisition(mock_camera)
        acquisition.retry_delay_seconds = 0.01

        callback_calls = []

        def callback(frame_num, image):
            callback_calls.append((frame_num, image.shape))

        acquisition.capture_sequence(
            exposure_ms=100, num_frames=3,
            frame_callback=callback, inter_frame_delay=0.01
        )

        assert len(callback_calls) == 3
        assert callback_calls[0][0] == 0
        assert callback_calls[1][0] == 1
        assert callback_calls[2][0] == 2
