"""
Unit tests for PayloadCoordinator mission hardening (Phase 3b).

Covers checkpoint/resume, the mission watchdog, and safe-state entry on
abort and on unhandled sequence exceptions. Hardware is mocked; PTU and
FLI interactions are asserted, not executed. See docs/hardening_plan.md.
"""

import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "mission"))

from coordinator import (  # noqa: E402
    PayloadCoordinator,
    PositionTarget,
    SequenceConfig,
    SequenceStatus,
)


def make_coordinator(tmp_path):
    """Coordinator with mocked PTU/FLI hardware and quiet logging."""
    coordinator = PayloadCoordinator.__new__(PayloadCoordinator)
    coordinator.ptu = Mock()
    coordinator.ptu.halt = Mock(return_value=True)
    coordinator.ptu.get_position = Mock(return_value=(100, -200))
    coordinator.fli = Mock()
    coordinator.output_dir = tmp_path
    coordinator.logger = Mock()
    coordinator.lens_id = None
    coordinator.status = SequenceStatus.IDLE
    coordinator.current_sequence = None
    coordinator.current_position_index = 0
    coordinator.sequence_results = []
    coordinator.on_position_start = None
    coordinator.on_position_complete = None
    coordinator.on_sequence_complete = None
    return coordinator


def make_sequence(n_positions=3, **overrides):
    positions = [
        PositionTarget(
            id=f"grid_{i:03d}", pan_degrees=float(i), tilt_degrees=0.0,
            pan_steps=i * 100, tilt_steps=0,
        )
        for i in range(n_positions)
    ]
    defaults = dict(
        sequence_name="test_seq",
        positions=positions,
        return_to_start=False,
    )
    defaults.update(overrides)
    return SequenceConfig(**defaults)


def position_success(position, *_args, **_kwargs):
    return {"position_id": position.id, "success": True, "captures": []}


class TestCheckpointResume:
    """Checkpoint written per position; resume skips completed ones."""

    def test_checkpoint_written_and_cleared(self, tmp_path):
        coordinator = make_coordinator(tmp_path)
        coordinator.execute_single_position = Mock(
            side_effect=position_success
        )
        sequence = make_sequence(3)

        checkpoint = coordinator._checkpoint_path("test_seq")
        summary = coordinator.execute_sequence(sequence)

        assert summary["status"] == "completed"
        assert summary["successful_positions"] == 3
        # Cleared after successful completion
        assert not checkpoint.exists()

    def test_checkpoint_survives_failure(self, tmp_path):
        coordinator = make_coordinator(tmp_path)

        def fail_second(position, *_args, **_kwargs):
            ok = position.id != "grid_001"
            return {"position_id": position.id, "success": ok,
                    "captures": []}

        coordinator.execute_single_position = Mock(side_effect=fail_second)
        sequence = make_sequence(3, continue_on_error=False)

        summary = coordinator.execute_sequence(sequence)

        assert summary["status"] == "error"
        completed = coordinator._load_checkpoint("test_seq")
        assert completed == ["grid_000"]

    def test_resume_skips_completed(self, tmp_path):
        coordinator = make_coordinator(tmp_path)
        coordinator._write_checkpoint("test_seq", ["grid_000", "grid_001"])
        coordinator.execute_single_position = Mock(
            side_effect=position_success
        )
        sequence = make_sequence(3)

        summary = coordinator.execute_sequence(sequence, resume=True)

        assert summary["status"] == "completed"
        executed_ids = [
            call.args[0].id
            for call in coordinator.execute_single_position.call_args_list
        ]
        assert executed_ids == ["grid_002"]

    def test_resume_ignores_foreign_checkpoint(self, tmp_path):
        coordinator = make_coordinator(tmp_path)
        coordinator._write_checkpoint("other_seq", ["grid_000"])
        # Rename the file so it collides with our sequence's path
        foreign = coordinator._checkpoint_path("other_seq")
        foreign.rename(coordinator._checkpoint_path("test_seq"))

        assert coordinator._load_checkpoint("test_seq") == []


class TestWatchdog:
    """Mission watchdog aborts into a safe state."""

    def test_watchdog_expires(self, tmp_path):
        coordinator = make_coordinator(tmp_path)
        coordinator.execute_single_position = Mock(
            side_effect=position_success
        )
        sequence = make_sequence(3, max_duration_s=0.0)

        summary = coordinator.execute_sequence(sequence)

        assert summary["status"] == "error"
        assert coordinator.execute_single_position.call_count == 0
        coordinator.ptu.halt.assert_called()

    def test_no_watchdog_by_default(self, tmp_path):
        coordinator = make_coordinator(tmp_path)
        coordinator.execute_single_position = Mock(
            side_effect=position_success
        )
        summary = coordinator.execute_sequence(make_sequence(2))
        assert summary["status"] == "completed"


class TestSafeState:
    """Faults and aborts bring the hardware to a safe state."""

    def test_exception_enters_safe_state(self, tmp_path):
        coordinator = make_coordinator(tmp_path)
        coordinator.execute_single_position = Mock(
            side_effect=RuntimeError("camera exploded")
        )
        summary = coordinator.execute_sequence(make_sequence(2))

        assert summary["status"] == "error"
        coordinator.ptu.halt.assert_called()
        coordinator.fli.camera.control_shutter.assert_called_with(
            open_shutter=False
        )

    def test_abort_enters_safe_state(self, tmp_path):
        coordinator = make_coordinator(tmp_path)
        coordinator.status = SequenceStatus.RUNNING

        coordinator.abort_sequence()

        assert coordinator.status == SequenceStatus.ABORTED
        coordinator.ptu.halt.assert_called()
        coordinator.fli.camera.control_shutter.assert_called_with(
            open_shutter=False
        )

    def test_safe_state_survives_halt_failure(self, tmp_path):
        coordinator = make_coordinator(tmp_path)
        coordinator.ptu.halt = Mock(side_effect=RuntimeError("serial dead"))
        coordinator.status = SequenceStatus.RUNNING

        coordinator.abort_sequence()  # must not raise

        # Shutter close still attempted despite halt failure
        coordinator.fli.camera.control_shutter.assert_called_with(
            open_shutter=False
        )
