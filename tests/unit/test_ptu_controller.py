"""
Unit tests for the PTU controller serial protocol layer.

Uses a scripted fake serial port (no hardware) to verify the hardened
command/response protocol: echo correlation, stale-line discard, typed
errors, timeout resync, position-polled move completion with stall
detection, and halt confirmation. See docs/hardening_plan.md, Phase 2.
"""

import pytest
from collections import deque

from ptu.controller import (
    PTUController,
    PTUConfig,
    PTUCommandError,
    PTUConnectionError,
    PTUProtocolError,
    PTUTimeoutError,
)
from ptu import discovery


class FakeSerial:
    """Scripted fake serial port speaking the PTU line protocol.

    Parameters
    ----------
    responder : callable
        ``responder(cmd) -> list of str`` returning raw response lines
        exactly as they would appear on the wire (echo included).
    """

    def __init__(self, responder):
        self.responder = responder
        self.rx = deque()
        self.tx_log = []
        self.is_open = True

    def write(self, data):
        text = data.decode()
        self.tx_log.append(text)
        cmd = text.strip()
        if not cmd:  # bare delimiter (resync)
            return len(data)
        for line in self.responder(cmd):
            self.rx.append(f"{line}\r\n".encode())
        return len(data)

    def readline(self):
        if self.rx:
            return self.rx.popleft()
        return b""

    def reset_input_buffer(self):
        self.rx.clear()

    def reset_output_buffer(self):
        pass

    def close(self):
        self.is_open = False


class FakePTU:
    """Stateful simulated D100E: axes advance toward target on each poll.

    Position advances by ``step`` per PP query, so awaits converge after
    a few polls. ``stalled`` freezes the axes mid-move; ``drift`` makes
    the pan axis creep regardless of target (to defeat halt confirmation).
    """

    def __init__(self, step=400):
        self.pan = 0
        self.tilt = 0
        self.pan_target = 0
        self.tilt_target = 0
        self.step = step
        self.stalled = False
        self.drift = False

    def __call__(self, cmd):
        if cmd.startswith("PP") and len(cmd) > 2:
            self.pan_target = int(cmd[2:])
            return [f"{cmd} *"]
        if cmd.startswith("TP") and len(cmd) > 2:
            self.tilt_target = int(cmd[2:])
            return [f"{cmd} *"]
        if cmd == "PP":
            self._advance()
            return [f"PP * Current Pan position is {self.pan}"]
        if cmd == "TP":
            return [f"TP * Current Tilt position is {self.tilt}"]
        if cmd == "H":
            self.pan_target = self.pan
            self.tilt_target = self.tilt
            return ["H *"]
        if cmd == "PR":
            return ["PR * 92.5714 seconds arc per position"]
        if cmd == "TR":
            return ["TR * 46.2857 seconds arc per position"]
        if cmd == "V":
            return ["V * PTU-D100E v3.10, (C)FLIR Systems"]
        if cmd == "GS":
            return ["GS ! GPM not installed"]
        return [f"{cmd} *"]

    def _advance(self):
        if self.drift:
            self.pan += self.step
            return
        if self.stalled:
            return
        for pos_attr, target_attr in (
            ("pan", "pan_target"), ("tilt", "tilt_target")
        ):
            current = getattr(self, pos_attr)
            delta = getattr(self, target_attr) - current
            move = max(-self.step, min(self.step, delta))
            setattr(self, pos_attr, current + move)


def make_controller(fake, **config_overrides):
    """Build a controller wired to a fake serial port (bypassing connect)."""
    defaults = dict(
        port="/dev/fake",
        command_timeout_s=0.25,
        poll_interval_s=0.01,
        await_timeout_s=1.0,
        position_tolerance_steps=5,
    )
    defaults.update(config_overrides)
    controller = PTUController(PTUConfig(**defaults))
    controller.serial_conn = fake
    return controller


# --- send_command protocol ---

class TestSendCommand:
    """Echo correlation, stale-line discard, and typed errors."""

    def test_set_command_returns_payload(self):
        fake = FakeSerial(lambda cmd: [f"{cmd} *"])
        ptu = make_controller(fake)
        assert ptu.send_command("PP1000") == "*"

    def test_query_returns_verbose_payload(self):
        fake = FakeSerial(lambda cmd: ["PP * Current Pan position is 500"])
        ptu = make_controller(fake)
        assert ptu.send_command("PP") == "* Current Pan position is 500"

    def test_echo_disabled_device_accepted(self):
        fake = FakeSerial(lambda cmd: ["* Current Pan position is 500"])
        ptu = make_controller(fake)
        assert ptu.send_command("PP") == "* Current Pan position is 500"

    def test_error_response_raises(self):
        fake = FakeSerial(
            lambda cmd: [f"{cmd} ! Maximum allowable Pan position is 3090"]
        )
        ptu = make_controller(fake)
        with pytest.raises(PTUCommandError, match="Maximum allowable"):
            ptu.send_command("PP9999")

    def test_stale_lines_discarded(self):
        """Uncorrelated leftovers from an earlier fault are skipped."""
        fake = FakeSerial(lambda cmd: [
            "A *",                                  # stale await response
            "TP * Current Tilt position is -80",    # stale query response
            "PP * Current Pan position is 500",     # ours
        ])
        ptu = make_controller(fake)
        assert ptu.send_command("PP") == "* Current Pan position is 500"

    def test_prefix_collision_stale_discarded(self):
        """A stale 'PP1000 *' must not satisfy a 'PP' query."""
        fake = FakeSerial(lambda cmd: [
            "PP1000 *",                             # stale set-command echo
            "PP * Current Pan position is 500",
        ])
        ptu = make_controller(fake)
        assert ptu.send_command("PP") == "* Current Pan position is 500"

    def test_silence_raises_timeout_and_resyncs(self):
        fake = FakeSerial(lambda cmd: [])
        ptu = make_controller(fake)
        with pytest.raises(PTUTimeoutError):
            ptu.send_command("PP")
        # Resync wrote a bare delimiter after the timeout
        assert " " in fake.tx_log

    def test_not_connected_raises(self):
        ptu = PTUController(PTUConfig(port="/dev/fake"))
        with pytest.raises(PTUConnectionError):
            ptu.send_command("PP")


# --- position queries ---

class TestGetPosition:
    """Position parsing and retry-then-raise behavior."""

    def test_parses_positions(self):
        ptu = make_controller(FakeSerial(FakePTU()))
        assert ptu.get_position() == (0, 0)

    def test_unparseable_retries_then_raises(self):
        fake = FakeSerial(lambda cmd: [f"{cmd} * no numbers here"])
        ptu = make_controller(fake)
        with pytest.raises(PTUProtocolError, match="after 2 attempts"):
            ptu.get_position(retries=2)


# --- initialization ---

class TestInitialize:
    """Initialization sequence over the fake device."""

    def test_initialize_success(self):
        device = FakePTU()
        ptu = make_controller(FakeSerial(device))
        assert ptu.initialize() is True
        assert ptu._is_initialized
        assert ptu.pan_resolution == pytest.approx(92.5714)
        assert ptu.tilt_resolution == pytest.approx(46.2857)
        assert ptu.gpm is None  # GS probe returned '!'

    def test_initialize_command_error_returns_false(self):
        device = FakePTU()

        def responder(cmd):
            if cmd == "LU":
                return ["LU ! command failed"]
            return device(cmd)

        ptu = make_controller(FakeSerial(responder))
        assert ptu.initialize() is False
        assert not ptu._is_initialized


# --- movement ---

class TestMoveToPosition:
    """Target-verified moves, stall detection, and error paths."""

    def _initialized(self, device, **overrides):
        ptu = make_controller(FakeSerial(device), **overrides)
        assert ptu.initialize()
        return ptu

    def test_move_reaches_target(self):
        device = FakePTU(step=400)
        ptu = self._initialized(device)
        assert ptu.move_to_position(2000, -1000) is True
        assert device.pan == 2000
        assert device.tilt == -1000

    def test_move_rejected_by_device(self):
        device = FakePTU()

        def responder(cmd):
            if cmd == "PP9999":
                return ["PP9999 ! Maximum allowable Pan position is 3090"]
            return device(cmd)

        ptu = make_controller(FakeSerial(responder))
        assert ptu.initialize()
        assert ptu.move_to_position(9999, 0) is False

    def test_stall_detected(self):
        device = FakePTU(step=400)
        ptu = self._initialized(device, await_timeout_s=5.0)
        device.stalled = True
        assert ptu.move_to_position(2000, 0) is False

    def test_timeout_when_target_unreachable_in_time(self):
        device = FakePTU(step=1)  # crawls: cannot cover 2000 steps in time
        ptu = self._initialized(device, await_timeout_s=0.1)
        assert ptu.move_to_position(2000, 0) is False

    def test_sequential_axes_orders_commands(self):
        device = FakePTU(step=400)
        fake = FakeSerial(device)
        ptu = make_controller(fake, sequential_axes=True)
        assert ptu.initialize()
        assert ptu.move_to_position(2000, -1000) is True
        assert device.pan == 2000
        assert device.tilt == -1000
        commands = [t.strip() for t in fake.tx_log if t.strip()]
        # Tilt command must come after the pan move completed (pan polls
        # in between)
        pan_cmd = commands.index("PP2000")
        tilt_cmd = commands.index("TP-1000")
        assert tilt_cmd > pan_cmd
        assert "PP" in commands[pan_cmd + 1:tilt_cmd]

    def test_uninitialized_raises(self):
        ptu = make_controller(FakeSerial(FakePTU()))
        with pytest.raises(RuntimeError, match="not initialized"):
            ptu.move_to_position(100, 100)


# --- await without target ---

class TestAwaitCompletion:
    """Stability-based completion when no target is known."""

    def test_stable_position_completes(self):
        device = FakePTU()
        ptu = make_controller(FakeSerial(device))
        assert ptu.await_completion(timeout=2.0) is True

    def test_timeout_returns_false(self):
        device = FakePTU()
        device.drift = True  # position never settles
        ptu = make_controller(FakeSerial(device))
        assert ptu.await_completion(timeout=0.2) is False


# --- halt ---

class TestHalt:
    """Halt must be confirmed by stationary axes."""

    def test_halt_confirmed(self):
        device = FakePTU(step=400)
        ptu = make_controller(FakeSerial(device))
        assert ptu.initialize()
        ptu.move_to_position(5000, 0, wait=False)
        assert ptu.halt() is True
        assert device.pan_target == device.pan

    def test_halt_unconfirmed_when_still_moving(self):
        device = FakePTU(step=400)
        device.drift = True
        ptu = make_controller(FakeSerial(device))
        assert ptu.halt(timeout=0.2) is False


# --- discovery ---

class TestDiscoveryProbe:
    """Echo-aware port probing."""

    MODEL_LINE = "PTU-D100E E Series"

    def _fake_device(self, echo):
        def responder(cmd):
            replies = {
                "VM": f"* {self.MODEL_LINE}",
                "VS": "* 12345678",
                "V": "* PTU-D100E v3.10",
            }
            if cmd not in replies:
                return []
            if echo:
                return [f"{cmd} {replies[cmd]}"]
            return [replies[cmd]]
        return FakeSerial(responder)

    @pytest.mark.parametrize("echo", [True, False])
    def test_probe_identifies_ptu(self, monkeypatch, echo):
        fake = self._fake_device(echo)
        monkeypatch.setattr(
            discovery.serial, "Serial", lambda **kwargs: fake
        )
        info = discovery.probe_port("/dev/fake")
        assert info is not None
        assert "D100" in info.model
        assert info.serial_number == "12345678"
        assert info.firmware_version == "PTU-D100E v3.10"
        assert not fake.is_open  # port closed after probe

    def test_probe_rejects_non_ptu(self, monkeypatch):
        fake = FakeSerial(lambda cmd: ["ERROR unknown command"])
        monkeypatch.setattr(
            discovery.serial, "Serial", lambda **kwargs: fake
        )
        assert discovery.probe_port("/dev/fake") is None

    def test_probe_handles_silent_port(self, monkeypatch):
        fake = FakeSerial(lambda cmd: [])
        monkeypatch.setattr(
            discovery.serial, "Serial", lambda **kwargs: fake
        )
        assert discovery.probe_port("/dev/fake") is None
