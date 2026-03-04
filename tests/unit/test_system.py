"""
Unit tests for fli.system module

Tests the FLISystem class without requiring hardware.
Uses mock objects to test device discovery, logging, and lifecycle management.
"""

import pytest
import logging
import tempfile
import os
from unittest.mock import Mock, MagicMock, patch
import numpy as np

from fli.system import FLISystem


class TestLoggerCreation:
    """Tests for FLISystem logger functionality."""

    def test_create_default_logger(self):
        """FLISystem creates a default logger when none provided."""
        with patch.object(FLISystem, '_create_default_logger') as mock_create:
            mock_create.return_value = logging.getLogger('test')
            # Patch FLILibrary to avoid actual library loading
            with patch('fli.system.FLILibrary'):
                system = FLISystem()
                mock_create.assert_called_once()

    def test_custom_logger_used(self):
        """FLISystem uses provided logger when given."""
        custom_logger = logging.getLogger('custom_test_logger')

        with patch('fli.system.FLILibrary'):
            system = FLISystem(logger=custom_logger)
            assert system.logger is custom_logger

    def test_default_logger_has_handler(self):
        """Default logger should have at least one handler."""
        with patch('fli.system.FLILibrary'):
            system = FLISystem()
            assert len(system.logger.handlers) > 0

    def test_default_logger_level(self):
        """Default logger should be set to INFO level."""
        with patch('fli.system.FLILibrary'):
            system = FLISystem()
            assert system.logger.level == logging.INFO


class TestSetupLogging:
    """Tests for FLISystem.setup_logging()."""

    def test_setup_logging_creates_file_handler(self):
        """setup_logging should create a file handler."""
        with patch('fli.system.FLILibrary'):
            system = FLISystem()

            with tempfile.NamedTemporaryFile(suffix='.log', delete=False) as f:
                log_file = f.name

            try:
                system.setup_logging(log_file, console_output=False)

                file_handlers = [h for h in system.logger.handlers
                                 if isinstance(h, logging.FileHandler)]
                assert len(file_handlers) == 1
            finally:
                os.unlink(log_file)

    def test_setup_logging_with_console(self):
        """setup_logging should add console handler when requested."""
        with patch('fli.system.FLILibrary'):
            system = FLISystem()

            with tempfile.NamedTemporaryFile(suffix='.log', delete=False) as f:
                log_file = f.name

            try:
                system.setup_logging(log_file, console_output=True)

                stream_handlers = [h for h in system.logger.handlers
                                   if isinstance(h, logging.StreamHandler)
                                   and not isinstance(h, logging.FileHandler)]
                assert len(stream_handlers) == 1
            finally:
                os.unlink(log_file)

    def test_setup_logging_without_console(self):
        """setup_logging should not add console handler when disabled."""
        with patch('fli.system.FLILibrary'):
            system = FLISystem()

            with tempfile.NamedTemporaryFile(suffix='.log', delete=False) as f:
                log_file = f.name

            try:
                system.setup_logging(log_file, console_output=False)

                stream_handlers = [h for h in system.logger.handlers
                                   if isinstance(h, logging.StreamHandler)
                                   and not isinstance(h, logging.FileHandler)]
                assert len(stream_handlers) == 0
            finally:
                os.unlink(log_file)

    def test_setup_logging_clears_existing_handlers(self):
        """setup_logging should clear existing handlers."""
        with patch('fli.system.FLILibrary'):
            system = FLISystem()

            # Add some extra handlers
            system.logger.addHandler(logging.StreamHandler())
            system.logger.addHandler(logging.StreamHandler())
            initial_count = len(system.logger.handlers)

            with tempfile.NamedTemporaryFile(suffix='.log', delete=False) as f:
                log_file = f.name

            try:
                system.setup_logging(log_file, console_output=True)

                # Should have exactly 2 handlers: file + console
                assert len(system.logger.handlers) == 2
            finally:
                os.unlink(log_file)


class TestContextManager:
    """Tests for FLISystem context manager functionality."""

    def test_context_manager_enter(self):
        """Context manager should return self on enter."""
        with patch('fli.system.FLILibrary'):
            system = FLISystem()

            result = system.__enter__()

            assert result is system

    def test_context_manager_exit_calls_close(self):
        """Context manager should call close on exit."""
        with patch('fli.system.FLILibrary'):
            system = FLISystem()
            system.close = Mock()

            system.__exit__(None, None, None)

            system.close.assert_called_once()

    def test_context_manager_exit_on_exception(self):
        """Context manager should close even when exception occurs."""
        with patch('fli.system.FLILibrary'):
            system = FLISystem()
            system.close = Mock()

            # Simulate exception context
            result = system.__exit__(ValueError, ValueError("test"), None)

            system.close.assert_called_once()
            assert result is False  # Don't suppress exception

    def test_with_statement_closes_system(self):
        """Using 'with' statement should close system on exit."""
        with patch('fli.system.FLILibrary'):
            with FLISystem() as system:
                system.close = Mock()

            system.close.assert_called_once()


class TestDeviceState:
    """Tests for FLISystem device state management."""

    def test_initial_state_no_devices(self):
        """Initially, no devices should be connected."""
        with patch('fli.system.FLILibrary'):
            system = FLISystem()

            assert system.camera is None
            assert system.filter_wheel is None
            assert system.acquisition is None

    def test_close_clears_devices(self):
        """close() should clear all device references."""
        with patch('fli.system.FLILibrary'):
            system = FLISystem()

            # Set up mock devices
            system.camera = Mock()
            system.filter_wheel = Mock()
            system.acquisition = Mock()

            system.close()

            assert system.camera is None
            assert system.filter_wheel is None
            assert system.acquisition is None

    def test_close_calls_device_close(self):
        """close() should call close on connected devices."""
        with patch('fli.system.FLILibrary'):
            system = FLISystem()

            mock_camera = Mock()
            mock_filter_wheel = Mock()
            system.camera = mock_camera
            system.filter_wheel = mock_filter_wheel

            system.close()

            mock_camera.close.assert_called_once()
            mock_filter_wheel.close.assert_called_once()

    def test_close_handles_device_close_errors(self):
        """close() should handle errors when closing devices."""
        with patch('fli.system.FLILibrary'):
            system = FLISystem()

            mock_camera = Mock()
            mock_camera.close.side_effect = RuntimeError("Close failed")
            system.camera = mock_camera

            # Should not raise
            system.close()

            assert system.camera is None


class TestMethodsWithoutDevices:
    """Tests for FLISystem methods when devices aren't connected."""

    def test_set_temperature_no_camera_raises(self):
        """set_temperature should raise when no camera connected."""
        with patch('fli.system.FLILibrary'):
            system = FLISystem()

            with pytest.raises(RuntimeError, match="No camera connected"):
                system.set_temperature(-20)

    def test_get_temperature_no_camera_raises(self):
        """get_temperature should raise when no camera connected."""
        with patch('fli.system.FLILibrary'):
            system = FLISystem()

            with pytest.raises(RuntimeError, match="No camera connected"):
                system.get_temperature()

    def test_capture_image_no_camera_raises(self):
        """capture_image should raise when no camera connected."""
        with patch('fli.system.FLILibrary'):
            system = FLISystem()

            with pytest.raises(RuntimeError, match="No camera connected"):
                system.capture_image(exposure_ms=100)

    def test_capture_sequence_no_camera_raises(self):
        """capture_sequence should raise when no camera connected."""
        with patch('fli.system.FLILibrary'):
            system = FLISystem()

            with pytest.raises(RuntimeError, match="No camera connected"):
                system.capture_sequence(exposure_ms=100, num_frames=3)

    def test_get_camera_info_no_camera_raises(self):
        """get_camera_info should raise when no camera connected."""
        with patch('fli.system.FLILibrary'):
            system = FLISystem()

            with pytest.raises(RuntimeError, match="No camera connected"):
                system.get_camera_info()

    def test_move_filter_no_wheel_raises(self):
        """move_filter should raise when no filter wheel connected."""
        with patch('fli.system.FLILibrary'):
            system = FLISystem()

            with pytest.raises(RuntimeError, match="No filter wheel connected"):
                system.move_filter(0)

    def test_get_filter_position_no_wheel_raises(self):
        """get_filter_position should raise when no filter wheel connected."""
        with patch('fli.system.FLILibrary'):
            system = FLISystem()

            with pytest.raises(RuntimeError, match="No filter wheel connected"):
                system.get_filter_position()

    def test_get_filter_count_no_wheel_raises(self):
        """get_filter_count should raise when no filter wheel connected."""
        with patch('fli.system.FLILibrary'):
            system = FLISystem()

            with pytest.raises(RuntimeError, match="No filter wheel connected"):
                system.get_filter_count()

    def test_initialize_no_camera_raises(self):
        """initialize should raise when no camera connected."""
        with patch('fli.system.FLILibrary'):
            system = FLISystem()

            with pytest.raises(RuntimeError, match="No camera connected"):
                system.initialize()


class TestTemperatureControl:
    """Tests for FLISystem temperature control with mock camera."""

    @pytest.fixture
    def system_with_camera(self):
        """Create FLISystem with mock camera."""
        with patch('fli.system.FLILibrary'):
            system = FLISystem()
            mock_camera = Mock()
            mock_camera.get_temperature = Mock(return_value=-15.0)
            mock_camera.set_temperature = Mock()
            system.camera = mock_camera
            return system

    def test_set_temperature_calls_camera(self, system_with_camera):
        """set_temperature should call camera.set_temperature."""
        system_with_camera.set_temperature(-25)

        system_with_camera.camera.set_temperature.assert_called_with(-25)

    def test_get_temperature_returns_value(self, system_with_camera):
        """get_temperature should return camera temperature."""
        result = system_with_camera.get_temperature()

        assert result == -15.0


class TestFilterWheelControl:
    """Tests for FLISystem filter wheel control with mock."""

    @pytest.fixture
    def system_with_filter_wheel(self):
        """Create FLISystem with mock filter wheel."""
        with patch('fli.system.FLILibrary'):
            system = FLISystem()
            mock_fw = Mock()
            mock_fw.get_filter_pos = Mock(return_value=0)
            mock_fw.get_filter_count = Mock(return_value=5)
            mock_fw.set_filter_pos = Mock()
            mock_fw.wait_for_movement_completion = Mock(return_value=True)
            system.filter_wheel = mock_fw
            return system

    def test_move_filter_success(self, system_with_filter_wheel):
        """move_filter should move to requested position."""
        # Position after move
        system_with_filter_wheel.filter_wheel.get_filter_pos = Mock(
            side_effect=[0, 2]  # First call returns current, second returns new
        )

        result = system_with_filter_wheel.move_filter(2)

        assert result is True
        system_with_filter_wheel.filter_wheel.set_filter_pos.assert_called_with(2)

    def test_move_filter_already_at_position(self, system_with_filter_wheel):
        """move_filter should skip if already at position."""
        system_with_filter_wheel.filter_wheel.get_filter_pos = Mock(return_value=3)

        result = system_with_filter_wheel.move_filter(3)

        assert result is True
        system_with_filter_wheel.filter_wheel.set_filter_pos.assert_not_called()

    def test_move_filter_invalid_position_raises(self, system_with_filter_wheel):
        """move_filter should raise for invalid position."""
        with pytest.raises(ValueError, match="Invalid filter position"):
            system_with_filter_wheel.move_filter(10)

    def test_move_filter_negative_position_raises(self, system_with_filter_wheel):
        """move_filter should raise for negative position."""
        with pytest.raises(ValueError, match="Invalid filter position"):
            system_with_filter_wheel.move_filter(-1)

    def test_move_filter_timeout_raises(self, system_with_filter_wheel):
        """move_filter should raise on movement timeout."""
        system_with_filter_wheel.filter_wheel.wait_for_movement_completion = Mock(
            return_value=False
        )

        with pytest.raises(RuntimeError, match="timeout"):
            system_with_filter_wheel.move_filter(2)

    def test_get_filter_position(self, system_with_filter_wheel):
        """get_filter_position should return current position."""
        system_with_filter_wheel.filter_wheel.get_filter_pos = Mock(return_value=3)

        result = system_with_filter_wheel.get_filter_position()

        assert result == 3

    def test_get_filter_count(self, system_with_filter_wheel):
        """get_filter_count should return total positions."""
        result = system_with_filter_wheel.get_filter_count()

        assert result == 5


class TestImageCapture:
    """Tests for FLISystem image capture with mock acquisition."""

    @pytest.fixture
    def system_with_acquisition(self):
        """Create FLISystem with mock acquisition."""
        with patch('fli.system.FLILibrary'):
            system = FLISystem()
            mock_acq = Mock()
            mock_acq.capture = Mock(
                return_value=np.random.randint(100, 50000, size=(512, 512), dtype=np.uint16)
            )
            mock_acq.capture_sequence = Mock(
                return_value=[
                    np.random.randint(100, 50000, size=(512, 512), dtype=np.uint16)
                    for _ in range(3)
                ]
            )
            system.acquisition = mock_acq
            return system

    def test_capture_image_calls_acquisition(self, system_with_acquisition):
        """capture_image should delegate to acquisition.capture."""
        image = system_with_acquisition.capture_image(exposure_ms=200, frame_type="dark")

        system_with_acquisition.acquisition.capture.assert_called_once_with(
            200, "dark"
        )
        assert image is not None

    def test_capture_image_passes_kwargs(self, system_with_acquisition):
        """capture_image should pass additional kwargs."""
        system_with_acquisition.capture_image(
            exposure_ms=100,
            frame_type="normal",
            max_retries=5
        )

        system_with_acquisition.acquisition.capture.assert_called_with(
            100, "normal", max_retries=5
        )

    def test_capture_sequence_calls_acquisition(self, system_with_acquisition):
        """capture_sequence should delegate to acquisition.capture_sequence."""
        frames = system_with_acquisition.capture_sequence(
            exposure_ms=100, num_frames=3, frame_type="dark"
        )

        system_with_acquisition.acquisition.capture_sequence.assert_called_once()
        assert len(frames) == 3


class TestInitialize:
    """Tests for FLISystem.initialize()."""

    @pytest.fixture
    def system_with_camera(self):
        """Create FLISystem with mock camera for initialization tests."""
        with patch('fli.system.FLILibrary'):
            system = FLISystem()
            mock_camera = Mock()
            mock_camera.get_temperature = Mock(return_value=-20.0)
            mock_camera.set_temperature = Mock()
            system.camera = mock_camera
            return system

    def test_initialize_sets_temperature(self, system_with_camera):
        """initialize should set target temperature."""
        system_with_camera.initialize(target_temp=-25)

        system_with_camera.camera.set_temperature.assert_called_with(-25)

    def test_initialize_default_temperature(self, system_with_camera):
        """initialize should use default temperature of -20."""
        system_with_camera.initialize()

        system_with_camera.camera.set_temperature.assert_called_with(-20.0)

    def test_initialize_returns_true(self, system_with_camera):
        """initialize should return True on success."""
        result = system_with_camera.initialize()

        assert result is True

