"""
FLIR PTU D100E Controller

Provides high-level control interface for the FLIR PTU D100E pan-tilt unit
via RS-232 serial communication.

The PTU uses a text-based command protocol where commands are sent as ASCII
strings terminated by a space or newline delimiter. With command echo
enabled (the device default), each response line begins with the echoed
command, e.g. ``PP1000 *`` for a successful set, ``PP * Current Pan
position is 1000`` for a query, and ``PP3200 ! Maximum allowable Pan
position is 3090`` for an error.

Protocol robustness: this controller keeps echo enabled and requires every
response line to begin with the echoed command, giving positive
command/response correlation. Lines that do not correlate (stale responses
from a previous timeout) are discarded, so the protocol self-heals from
desynchronization instead of silently mis-parsing. Failures raise typed
exceptions (`PTUCommandError`, `PTUTimeoutError`, ...) rather than
returning unparsed strings.

Raw serial traffic can be logged for field diagnosis by enabling the
dedicated wire logger::

    logging.getLogger("ptu.serial").setLevel(logging.DEBUG)
"""

import serial
import time
import logging
from typing import Optional, Tuple, Dict, Any
from dataclasses import dataclass
from enum import Enum


class PTUError(Exception):
    """Base class for PTU communication and command errors."""


class PTUConnectionError(PTUError):
    """No serial connection is open, or the connection failed."""


class PTUCommandError(PTUError):
    """The PTU rejected a command (``!`` error response)."""


class PTUTimeoutError(PTUError):
    """No correlated response arrived within the command deadline."""


class PTUProtocolError(PTUError):
    """A response was received but could not be parsed."""


class PowerMode(Enum):
    """PTU power modes for hold and move operations."""
    OFF = "O"
    LOW = "L"
    REGULAR = "R"
    HIGH = "H"


# Await-completion poll counts: an axis is considered stopped after this
# many consecutive identical position reads, and stalled if it stops
# off-target for this many reads while a move is pending.
_STABLE_POLL_COUNT = 3
_STALL_POLL_COUNT = 8


@dataclass
class PTUConfig:
    """Configuration parameters for PTU initialization.

    Parameters
    ----------
    port : str
        Serial port device path, or "auto" to auto-discover.
    baudrate : int
        Serial communication baud rate.
    timeout : float
        Serial read timeout in seconds (per readline).
    command_timeout_s : float
        Deadline for receiving a correlated response to a command.
    poll_interval_s : float
        Position polling interval used while awaiting move completion.
    pan_min_user : int, optional
        User-defined minimum pan position in steps.
    pan_max_user : int, optional
        User-defined maximum pan position in steps.
    tilt_min_user : int, optional
        User-defined minimum tilt position in steps.
    tilt_max_user : int, optional
        User-defined maximum tilt position in steps.
    pan_speed : int, optional
        Pan axis speed in positions/second.
    tilt_speed : int, optional
        Tilt axis speed in positions/second.
    pan_acceleration : int, optional
        Pan axis acceleration in positions/second^2.
    tilt_acceleration : int, optional
        Tilt axis acceleration in positions/second^2.
    hold_power_mode : PowerMode
        Power mode when holding position.
    move_power_mode : PowerMode
        Power mode during movement.
    sequential_axes : bool
        If True, move pan and tilt axes one at a time (pan first, then
        tilt) to reduce peak current draw. Default is False (simultaneous).
    position_tolerance_steps : int
        Maximum allowable position error in steps after a move command.
        Movement is considered failed if either axis exceeds this tolerance.
    await_timeout_s : float
        Maximum wait time in seconds for movement completion.
    """
    port: str = "auto"
    baudrate: int = 9600
    timeout: float = 1.0
    command_timeout_s: float = 3.0
    poll_interval_s: float = 0.2
    pan_min_user: Optional[int] = None
    pan_max_user: Optional[int] = None
    tilt_min_user: Optional[int] = None
    tilt_max_user: Optional[int] = None
    pan_speed: Optional[int] = None
    tilt_speed: Optional[int] = None
    pan_acceleration: Optional[int] = None
    tilt_acceleration: Optional[int] = None
    hold_power_mode: PowerMode = PowerMode.LOW
    move_power_mode: PowerMode = PowerMode.LOW
    sequential_axes: bool = False
    position_tolerance_steps: int = 5
    await_timeout_s: float = 30.0


class PTUController:
    """Controller for the FLIR PTU D100E pan-tilt unit.

    Provides methods for connecting, initializing, and commanding the PTU
    over a serial interface. Supports absolute and relative positioning in
    both steps and degrees.

    Parameters
    ----------
    config : PTUConfig
        Configuration parameters for the PTU connection and behavior.

    Attributes
    ----------
    pan_resolution : float or None
        Pan axis resolution in arcsec/step (set during initialize()).
    tilt_resolution : float or None
        Tilt axis resolution in arcsec/step (set during initialize()).
    """

    def __init__(self, config: PTUConfig):
        self.config = config
        self.serial_conn: Optional[serial.Serial] = None
        self.logger = logging.getLogger(__name__)
        self._serial_log = logging.getLogger("ptu.serial")
        self.pan_resolution: Optional[float] = None
        self.tilt_resolution: Optional[float] = None
        self._is_initialized = False
        self._device_info: Optional[Any] = None
        self.gpm: Optional[Any] = None

    def connect(self) -> bool:
        """Establish serial connection to PTU.

        If ``config.port`` is ``"auto"``, performs auto-discovery to
        find the PTU on available serial ports before connecting.

        Returns
        -------
        bool
            True if connection successful.
        """
        try:
            # Auto-discover if port is "auto"
            if self.config.port == "auto":
                from .discovery import discover_ptu

                self.logger.info("Auto-discovering PTU serial port...")
                device_info = discover_ptu(
                    baudrate=self.config.baudrate,
                    timeout=self.config.timeout,
                    logger=self.logger,
                )
                if device_info is None:
                    self.logger.error(
                        "PTU auto-discovery failed: no PTU found. "
                        "Specify port explicitly with PTUConfig(port=...)"
                    )
                    return False
                self.config.port = device_info.port
                self._device_info = device_info
                self.logger.info(
                    f"Auto-discovered PTU on {device_info.port}: "
                    f"{device_info.model}"
                    f" (S/N: {device_info.serial_number or 'N/A'})"
                )

            self.serial_conn = serial.Serial(
                port=self.config.port,
                baudrate=self.config.baudrate,
                timeout=self.config.timeout,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE
            )
            # Start from a clean slate: any bytes left over from a prior
            # session (e.g. an aborted run) would desynchronize the first
            # command/response exchange.
            self.serial_conn.reset_input_buffer()
            self.serial_conn.reset_output_buffer()
            self.logger.info(f"Connected to PTU on {self.config.port}")
            return True
        except Exception as e:
            self.logger.error(f"Failed to connect to PTU: {e}")
            return False

    def disconnect(self):
        """Close serial connection."""
        if self.serial_conn and self.serial_conn.is_open:
            self.serial_conn.close()
            self.logger.info("Disconnected from PTU")

    def _drain_input(self):
        """Read and discard buffered lines until the port goes quiet.

        Bounded: stops at the first empty read (serial timeout) or after
        20 lines, whichever comes first.
        """
        for _ in range(20):
            raw = self.serial_conn.readline()
            if not raw:
                return
            self._serial_log.debug("RX %r (discarded stale)", raw)

    def _resync(self):
        """Restore command/response alignment after a protocol fault.

        Sends a bare delimiter to terminate any partial command in the
        PTU's input parser, waits briefly for in-flight responses to
        arrive, then discards everything in the input buffer. Best-effort:
        never raises, so it is safe to call from error paths.
        """
        if not (self.serial_conn and self.serial_conn.is_open):
            return
        try:
            self._serial_log.debug("TX %r (resync delimiter)", b" ")
            self.serial_conn.write(b" ")
            time.sleep(0.2)
            self.serial_conn.reset_input_buffer()
            self._drain_input()
        except Exception as e:
            self.logger.debug(f"Resync failed: {e}")

    @staticmethod
    def _parse_numeric_response(response: str) -> Optional[float]:
        """Extract the first numeric value from a PTU response payload.

        Handles formats like ``"* 108.000000 seconds arc per position"``
        or ``"* Current Pan position is 1000"``.
        """
        for token in response.split():
            try:
                return float(token)
            except ValueError:
                continue
        return None

    def send_command(self, command: str) -> str:
        """Send a command and return its correlated response payload.

        Flushes stale serial input, writes the command, then reads lines
        until one correlates with this command: either it begins with the
        command echo (echo enabled, the device default) or it is a bare
        ``*``/``!`` line (echo disabled). Non-correlating lines are stale
        responses from earlier faults and are discarded.

        Parameters
        ----------
        command : str
            PTU command string (delimiter added automatically).

        Returns
        -------
        str
            Response payload with the command echo stripped, beginning
            with ``*`` (e.g. ``"* Current Pan position is 1000"``).

        Raises
        ------
        PTUConnectionError
            If no serial connection is open.
        PTUCommandError
            If the PTU rejected the command (``!`` response).
        PTUTimeoutError
            If no correlated response arrived within
            ``config.command_timeout_s``. The input buffer is resynced
            before raising.
        """
        if not self.serial_conn or not self.serial_conn.is_open:
            raise PTUConnectionError("PTU not connected")

        cmd = command.strip()
        data = (cmd + " ").encode("ascii")

        # Flush stale input before sending
        self.serial_conn.reset_input_buffer()

        self._serial_log.debug("TX %r", data)
        self.serial_conn.write(data)

        deadline = time.time() + self.config.command_timeout_s
        while time.time() < deadline:
            raw = self.serial_conn.readline()
            if not raw:
                continue
            self._serial_log.debug("RX %r", raw)
            line = raw.decode("ascii", errors="replace").strip()
            if not line:
                continue

            if line.startswith(cmd):
                payload = line[len(cmd):].strip()
                if not (payload.startswith("*") or payload.startswith("!")):
                    # Prefix collision with a stale line (e.g. leftover
                    # "PP1000 *" while sending "PP"): not our response.
                    self._serial_log.debug("RX discarded (no */!): %r", raw)
                    continue
            elif line.startswith(("*", "!")):
                # Bare response: device has echo disabled
                payload = line
            else:
                self._serial_log.debug("RX discarded (uncorrelated): %r", raw)
                continue

            if payload.startswith("!"):
                raise PTUCommandError(
                    f"PTU command '{cmd}' failed: {payload}"
                )
            return payload

        self._resync()
        raise PTUTimeoutError(
            f"No response to '{cmd}' within "
            f"{self.config.command_timeout_s:.1f}s"
        )

    def _query_numeric(self, command: str) -> float:
        """Send a query command and parse a numeric value from the payload.

        Raises
        ------
        PTUProtocolError
            If the response contains no numeric value.
        """
        payload = self.send_command(command)
        value = self._parse_numeric_response(payload)
        if value is None:
            raise PTUProtocolError(
                f"No numeric value in response to '{command}': {payload!r}"
            )
        return value

    def initialize(self) -> bool:
        """Initialize PTU with configuration parameters.

        Performs a full initialization sequence: firmware check, echo and
        feedback mode setup, halt, resolution query, limit configuration,
        speed/acceleration setup, and power mode configuration.

        Returns
        -------
        bool
            True if initialization successful.
        """
        try:
            # Enable command echo so every response is positively
            # correlated with its command (see module docstring), and
            # verbose feedback for self-describing query responses.
            self.send_command("EE")
            self.send_command("FV")

            # Check firmware version
            version_resp = self.send_command("V")
            self.logger.info(f"PTU Firmware: {version_resp}")

            # Halt any in-progress movement
            self.send_command("H")

            # Get resolution values
            # Response format: "* 108.000000 seconds arc per position"
            self.pan_resolution = self._query_numeric("PR")
            self.tilt_resolution = self._query_numeric("TR")

            self.logger.info(f"Pan resolution: {self.pan_resolution} arcsec/step")
            self.logger.info(f"Tilt resolution: {self.tilt_resolution} arcsec/step")

            # Configure user-defined limits if specified
            if self.config.pan_min_user is not None:
                self.send_command(f"PNU{self.config.pan_min_user}")
            if self.config.pan_max_user is not None:
                self.send_command(f"PXU{self.config.pan_max_user}")
            if self.config.tilt_min_user is not None:
                self.send_command(f"TNU{self.config.tilt_min_user}")
            if self.config.tilt_max_user is not None:
                self.send_command(f"TXU{self.config.tilt_max_user}")

            # Enable user limits
            self.send_command("LU")

            # Set speeds if specified
            if self.config.pan_speed is not None:
                self.send_command(f"PS{self.config.pan_speed}")
            if self.config.tilt_speed is not None:
                self.send_command(f"TS{self.config.tilt_speed}")

            # Set acceleration if specified
            if self.config.pan_acceleration is not None:
                self.send_command(f"PA{self.config.pan_acceleration}")
            if self.config.tilt_acceleration is not None:
                self.send_command(f"TA{self.config.tilt_acceleration}")

            # Set power modes
            self._set_power_modes()

            # Set position control mode
            self.send_command("CI")

            # Set Immediate execution mode so commands execute on receipt
            # (Slaved mode buffers commands and may never execute them)
            self.send_command("I")

            self._is_initialized = True

            # Detect Geo Pointing Module (optional hardware)
            self._detect_gpm()

            self.logger.info("PTU initialization completed successfully")
            return True

        except PTUError as e:
            self.logger.error(f"PTU initialization failed: {e}")
            return False

    def _set_power_modes(self):
        """Set hold and move power modes for both axes."""
        self.send_command(f"PH{self.config.hold_power_mode.value}")
        self.send_command(f"TH{self.config.hold_power_mode.value}")
        self.send_command(f"PM{self.config.move_power_mode.value}")
        self.send_command(f"TM{self.config.move_power_mode.value}")

    def _detect_gpm(self):
        """Detect the Geo Pointing Module if available.

        Creates a GPMController and probes the GPM with the GS command.
        If the GPM responds, ``self.gpm`` is set; otherwise it remains None.
        """
        try:
            from .gpm import GPMController

            gpm = GPMController(
                send_command=self.send_command,
                logger=self.logger,
            )
            if gpm.detect():
                self.gpm = gpm
                self.logger.info("GPM detected and available")
            else:
                self.gpm = None
                self.logger.info(
                    "GPM not detected (geo-pointing unavailable)"
                )
        except ImportError:
            self.gpm = None
        except Exception as e:
            self.gpm = None
            self.logger.debug(f"GPM detection failed: {e}")

    def move_to_position(self, pan_steps: int, tilt_steps: int,
                         wait: bool = True) -> bool:
        """Move PTU to absolute position in encoder steps.

        When ``wait`` is True, completion is confirmed by polling the
        actual position until both axes are within
        ``config.position_tolerance_steps`` of the target, so a True
        return means the PTU verifiably reached the commanded position.

        Parameters
        ----------
        pan_steps : int
            Target pan position in steps.
        tilt_steps : int
            Target tilt position in steps.
        wait : bool
            If True, block until movement completes.

        Returns
        -------
        bool
            True if movement command accepted (and the target position
            was verifiably reached, if wait=True).
        """
        if not self._is_initialized:
            raise RuntimeError("PTU not initialized")

        try:
            if self.config.sequential_axes:
                # Move pan first, wait for completion, then move tilt.
                # This reduces peak current draw by avoiding simultaneous
                # motor acceleration on both axes.
                self.send_command(f"PP{pan_steps}")
                if not self.await_completion(pan_target=pan_steps):
                    self.logger.error(
                        "Pan movement failed during sequential move"
                    )
                    return False
                self.send_command(f"TP{tilt_steps}")
            else:
                # Simultaneous movement (default)
                self.send_command(f"PP{pan_steps}")
                self.send_command(f"TP{tilt_steps}")

            if wait:
                if not self.await_completion(
                    pan_target=pan_steps, tilt_target=tilt_steps
                ):
                    self.logger.error(
                        f"Move did not reach target Pan={pan_steps}, "
                        f"Tilt={tilt_steps} within "
                        f"{self.config.await_timeout_s:.1f}s"
                    )
                    return False
                self.logger.info(
                    f"Moved to position: Pan={pan_steps}, Tilt={tilt_steps}"
                )

            return True

        except PTUError as e:
            self.logger.error(f"Move to position failed: {e}")
            return False

    def move_relative_degrees(self, pan_degrees: float = 0.0,
                              tilt_degrees: float = 0.0,
                              wait: bool = True) -> bool:
        """Move PTU relative to current position in degrees.

        Parameters
        ----------
        pan_degrees : float
            Relative pan movement in degrees.
        tilt_degrees : float
            Relative tilt movement in degrees.
        wait : bool
            If True, block until movement completes.

        Returns
        -------
        bool
            True if movement successful.
        """
        if not self._is_initialized:
            raise RuntimeError("PTU not initialized")

        if self.pan_resolution is None or self.tilt_resolution is None:
            raise RuntimeError("Pan/tilt resolution not available")

        try:
            current_pan, current_tilt = self.get_position()

            pan_steps_delta = int(pan_degrees * 3600.0 / self.pan_resolution)
            tilt_steps_delta = int(tilt_degrees * 3600.0 / self.tilt_resolution)

            new_pan = current_pan + pan_steps_delta
            new_tilt = current_tilt + tilt_steps_delta

            return self.move_to_position(new_pan, new_tilt, wait)

        except PTUError as e:
            self.logger.error(f"Relative move failed: {e}")
            return False

    def get_position(self, retries: int = 3) -> Tuple[int, int]:
        """Get current pan and tilt positions in encoder steps.

        Parameters
        ----------
        retries : int
            Number of attempts before giving up. Transient protocol
            faults (timeouts, unparseable responses) are retried.

        Returns
        -------
        tuple of (int, int)
            Current (pan_steps, tilt_steps) position.

        Raises
        ------
        PTUProtocolError
            If the position could not be read after all retries.
        """
        last_error: Optional[PTUError] = None
        for attempt in range(retries):
            try:
                pan_val = self._query_numeric("PP")
                tilt_val = self._query_numeric("TP")
                return int(pan_val), int(tilt_val)
            except PTUError as e:
                last_error = e
                self.logger.warning(
                    f"Position query failed "
                    f"(attempt {attempt + 1}/{retries}): {e}"
                )
                time.sleep(0.15)

        raise PTUProtocolError(
            f"Could not read PTU position after {retries} attempts"
        ) from last_error

    def get_position_degrees(self) -> Tuple[float, float]:
        """Get current position in degrees.

        Returns
        -------
        tuple of (float, float)
            Current (pan_degrees, tilt_degrees) position.

        Raises
        ------
        RuntimeError
            If resolution values are not available.
        """
        if self.pan_resolution is None or self.tilt_resolution is None:
            raise RuntimeError("Pan/tilt resolution not available")

        pan_steps, tilt_steps = self.get_position()
        pan_degrees = pan_steps * self.pan_resolution / 3600.0
        tilt_degrees = tilt_steps * self.tilt_resolution / 3600.0

        return pan_degrees, tilt_degrees

    def await_completion(self, timeout: Optional[float] = None,
                         pan_target: Optional[int] = None,
                         tilt_target: Optional[int] = None) -> bool:
        """Wait for movement to complete by polling actual position.

        Position polling is used instead of the PTU ``A`` (await) command:
        ``A`` delays the command interpreter until motion completes, which
        would make ``halt()`` unresponsive mid-move and leaves an
        unconsumed response in the buffer if the wait is abandoned.

        When a target is given for an axis, completion means that axis is
        within ``config.position_tolerance_steps`` of the target; if the
        position stops changing while still off-target, the move is
        declared stalled and False is returned early. Without targets,
        completion means the position has been stable for several polls.

        Parameters
        ----------
        timeout : float, optional
            Maximum wait time in seconds. If None, uses
            ``config.await_timeout_s``.
        pan_target : int, optional
            Commanded pan position in steps.
        tilt_target : int, optional
            Commanded tilt position in steps.

        Returns
        -------
        bool
            True if movement completed (at the target, when targets are
            given) within the timeout.
        """
        if timeout is None:
            timeout = self.config.await_timeout_s

        tolerance = self.config.position_tolerance_steps
        has_target = pan_target is not None or tilt_target is not None

        start_time = time.time()
        deadline = start_time + timeout
        last_position: Optional[Tuple[int, int]] = None
        stable_polls = 0

        while time.time() < deadline:
            try:
                pan, tilt = self.get_position(retries=1)
            except PTUError as e:
                self.logger.warning(f"Position poll failed during await: {e}")
                time.sleep(self.config.poll_interval_s)
                continue

            if has_target:
                pan_ok = (pan_target is None or
                          abs(pan - pan_target) <= tolerance)
                tilt_ok = (tilt_target is None or
                           abs(tilt - tilt_target) <= tolerance)
                if pan_ok and tilt_ok:
                    return True

            if (pan, tilt) == last_position:
                stable_polls += 1
            else:
                stable_polls = 0
            last_position = (pan, tilt)

            if has_target and stable_polls >= _STALL_POLL_COUNT:
                self.logger.error(
                    f"PTU stalled at Pan={pan}, Tilt={tilt} "
                    f"(target Pan={pan_target}, Tilt={tilt_target})"
                )
                return False

            # Without a target: stationary for several polls = complete.
            # The grace period avoids declaring completion before the
            # axes have started accelerating.
            if (not has_target and stable_polls >= _STABLE_POLL_COUNT
                    and time.time() - start_time > 0.5):
                return True

            time.sleep(self.config.poll_interval_s)

        self.logger.warning(
            f"Movement completion timeout after {timeout:.1f}s"
        )
        return False

    def halt(self, timeout: float = 5.0) -> bool:
        """Halt all movement and confirm the axes have stopped.

        Parameters
        ----------
        timeout : float
            Maximum time in seconds to wait for motion to stop after the
            halt command is accepted.

        Returns
        -------
        bool
            True if the halt was accepted and both axes are confirmed
            stationary.
        """
        try:
            self.send_command("H")
        except PTUError as e:
            # A failed halt matters: resync the link and try once more.
            self.logger.error(f"Halt command failed: {e}; retrying")
            self._resync()
            try:
                self.send_command("H")
            except PTUError as retry_error:
                self.logger.error(f"Halt retry failed: {retry_error}")
                return False

        deadline = time.time() + timeout
        last_position: Optional[Tuple[int, int]] = None
        stable_polls = 0

        while time.time() < deadline:
            try:
                position = self.get_position(retries=1)
            except PTUError as e:
                self.logger.warning(f"Position poll failed during halt: {e}")
                time.sleep(self.config.poll_interval_s)
                continue

            if position == last_position:
                stable_polls += 1
                if stable_polls >= _STABLE_POLL_COUNT:
                    self.logger.info(
                        f"PTU halted at Pan={position[0]}, "
                        f"Tilt={position[1]}"
                    )
                    return True
            else:
                stable_polls = 0
            last_position = position

            time.sleep(self.config.poll_interval_s)

        self.logger.error(f"PTU halt not confirmed within {timeout:.1f}s")
        return False

    def save_settings(self):
        """Save current settings as power-on defaults.

        Raises
        ------
        PTUError
            If the save command fails.
        """
        self.send_command("DS")
        self.logger.info("PTU settings saved")

    def get_status(self) -> Dict[str, Any]:
        """Get comprehensive PTU status.

        Returns
        -------
        dict
            Status information including position, limits, and
            temperature/voltage readings. Individual query failures are
            recorded in the corresponding entry rather than raised.
        """
        status = {}

        # Position
        pan_steps, tilt_steps = self.get_position()
        status['position_steps'] = {'pan': pan_steps, 'tilt': tilt_steps}

        if self.pan_resolution and self.tilt_resolution:
            pan_deg, tilt_deg = self.get_position_degrees()
            status['position_degrees'] = {'pan': pan_deg, 'tilt': tilt_deg}

        # Limits, temperature and voltage
        for key, cmd in (("limits", "L"), ("temperature_voltage", "O")):
            try:
                status[key] = self.send_command(cmd)
            except PTUError as e:
                status[key] = f"query failed: {e}"

        # GPM status
        if self.gpm is not None:
            try:
                status['gpm'] = self.gpm.get_status().to_dict()
            except Exception:
                status['gpm'] = {"available": True, "error": "status query failed"}
        else:
            status['gpm'] = {"available": False}

        return status
