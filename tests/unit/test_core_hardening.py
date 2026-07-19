"""
Unit tests for FLI core-layer hardening (Phase 3a).

Covers the error-path fixes: chk_err / FLIError / FLIWarning constructor
regressions (error paths previously crashed with TypeError), fault-tolerant
camera idle waits, and temperature stabilization that survives transient
USB faults but abandons an unreachable camera. See docs/hardening_plan.md.
"""

import warnings

import pytest
from unittest.mock import Mock, patch

from fli.core.lib import FLIError, FLIWarning, chk_err
from fli.core.camera import USBCamera
from fli.core.lib import FLI_CAMERA_STATUS_IDLE, FLI_CAMERA_STATUS_EXPOSING
from fli.system import FLISystem


class TestChkErr:
    """Error-checking wrapper applied to every FLI C call."""

    def test_success_returns_zero(self):
        assert chk_err(0) == 0

    def test_negative_raises_fli_error(self):
        with pytest.raises(FLIError) as exc_info:
            chk_err(-5)
        assert exc_info.value.errors == -5

    def test_positive_warns_and_returns(self):
        """Positive returns are undocumented: warn, do not crash."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            assert chk_err(3) == 3
        assert len(caught) == 1
        assert issubclass(caught[0].category, FLIWarning)

    def test_fli_error_single_arg_constructible(self):
        """Regression: call sites raise FLIError with one argument."""
        err = FLIError("something failed")
        assert err.errors is None

    def test_fli_warning_single_arg_constructible(self):
        warn = FLIWarning("something odd")
        assert warn.errors is None


class TestWaitForIdle:
    """Camera idle wait: transient faults retried, never silent-crash."""

    def _bare_camera(self):
        """Camera instance without opening hardware."""
        camera = USBCamera.__new__(USBCamera)
        return camera

    @patch("fli.core.camera.time.sleep")
    def test_idle_immediately(self, _sleep):
        camera = self._bare_camera()
        camera.get_camera_status = Mock(return_value=FLI_CAMERA_STATUS_IDLE)
        assert camera.wait_for_idle(timeout_seconds=1) is True

    @patch("fli.core.camera.time.sleep")
    def test_transient_status_failures_recovered(self, _sleep):
        camera = self._bare_camera()
        camera.get_camera_status = Mock(side_effect=[
            FLIError("USB pipe read failed"),
            FLIError("USB pipe read failed"),
            FLI_CAMERA_STATUS_IDLE,
        ])
        assert camera.wait_for_idle(timeout_seconds=5) is True

    @patch("fli.core.camera.time.sleep")
    def test_never_idle_times_out(self, _sleep):
        camera = self._bare_camera()
        camera.get_camera_status = Mock(
            return_value=FLI_CAMERA_STATUS_EXPOSING
        )
        assert camera.wait_for_idle(timeout_seconds=0.2) is False


class TestWaitForTemperature:
    """Stabilization survives transient faults, abandons a dead camera."""

    def _system(self):
        with patch("fli.system.FLILibrary"):
            system = FLISystem(logger=Mock())
        system.camera = Mock()
        return system

    @patch("fli.system.time.sleep")
    def test_stabilizes(self, _sleep):
        system = self._system()
        system.camera.get_temperature = Mock(return_value=-20.1)
        assert system.wait_for_temperature(
            -20.0, timeout_minutes=1, required_stable_readings=3
        ) is True

    @patch("fli.system.time.sleep")
    def test_transient_read_failures_recovered(self, _sleep):
        system = self._system()
        system.camera.get_temperature = Mock(side_effect=[
            FLIError("USB fault"),
            FLIError("USB fault"),
            -20.0, -20.0, -20.0,
        ])
        assert system.wait_for_temperature(
            -20.0, timeout_minutes=1, required_stable_readings=3
        ) is True

    @patch("fli.system.time.sleep")
    def test_persistent_read_failures_abandon(self, _sleep):
        system = self._system()
        system.camera.get_temperature = Mock(
            side_effect=FLIError("USB fault")
        )
        assert system.wait_for_temperature(-20.0, timeout_minutes=1) is False
        # Abandoned after the failure cap, not the full timeout
        assert system.camera.get_temperature.call_count == 5

    def test_timeout_returns_false(self):
        system = self._system()
        system.camera.get_temperature = Mock(return_value=0.0)
        assert system.wait_for_temperature(-20.0, timeout_minutes=0) is False
