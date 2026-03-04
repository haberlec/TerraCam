"""
FLIR PTU D100E Controller

Provides high-level control interface for the FLIR PTU D100E pan-tilt unit
via RS-232 serial communication.

The PTU uses a text-based command protocol where commands are sent as ASCII
strings terminated by a space or newline delimiter. Successful commands return
"*", successful queries return "* <value>", and errors return "! <message>".
"""

import serial
import time
import logging
from typing import Optional, Tuple, Dict, Any
from dataclasses import dataclass
from enum import Enum


class PowerMode(Enum):
    """PTU power modes for hold and move operations."""
    OFF = "O"
    LOW = "L"
    REGULAR = "R"
    HIGH = "H"


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
        Serial read timeout in seconds.
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
    """
    port: str = "auto"
    baudrate: int = 9600
    timeout: float = 1.0
    pan_min_user: Optional[int] = None
    pan_max_user: Optional[int] = None
    tilt_min_user: Optional[int] = None
    tilt_max_user: Optional[int] = None
    pan_speed: Optional[int] = None
    tilt_speed: Optional[int] = None
    pan_acceleration: Optional[int] = None
    tilt_acceleration: Optional[int] = None
    hold_power_mode: PowerMode = PowerMode.REGULAR
    move_power_mode: PowerMode = PowerMode.REGULAR


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
        Pan axis resolution in steps/degree (set during initialize()).
    tilt_resolution : float or None
        Tilt axis resolution in steps/degree (set during initialize()).
    """

    def __init__(self, config: PTUConfig):
        self.config = config
        self.serial_conn: Optional[serial.Serial] = None
        self.logger = logging.getLogger(__name__)
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

    def send_command(self, command: str) -> str:
        """Send command to PTU and return response.

        Parameters
        ----------
        command : str
            PTU command string (delimiter added automatically if absent).

        Returns
        -------
        str
            Response string from the PTU.

        Raises
        ------
        RuntimeError
            If no serial connection is open.
        """
        if not self.serial_conn or not self.serial_conn.is_open:
            raise RuntimeError("PTU not connected")

        # Add delimiter if not present
        if not command.endswith(' ') and not command.endswith('\n'):
            command += ' '

        self.logger.debug(f"Sending command: {command.strip()}")
        self.serial_conn.write(command.encode())

        # Read response
        response = self.serial_conn.readline().decode().strip()
        self.logger.debug(f"Response: {response}")

        return response

    def initialize(self) -> bool:
        """Initialize PTU with configuration parameters.

        Performs a full initialization sequence: firmware check, reset,
        resolution query, limit configuration, speed/acceleration setup,
        and power mode configuration.

        Returns
        -------
        bool
            True if initialization successful.
        """
        try:
            # Check firmware version
            version_resp = self.send_command("V")
            self.logger.info(f"PTU Firmware: {version_resp}")

            # Reset PTU to known state
            self.send_command("R")
            time.sleep(2)  # Wait for reset to complete

            # Set feedback mode to verbose
            self.send_command("FV")

            # Get resolution values
            pan_res_resp = self.send_command("PR")
            tilt_res_resp = self.send_command("TR")

            # Parse resolution (response format: "* <value>")
            if pan_res_resp.startswith("*"):
                self.pan_resolution = float(pan_res_resp.split()[1])
            if tilt_res_resp.startswith("*"):
                self.tilt_resolution = float(tilt_res_resp.split()[1])

            self.logger.info(f"Pan resolution: {self.pan_resolution} steps/degree")
            self.logger.info(f"Tilt resolution: {self.tilt_resolution} steps/degree")

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

            self._is_initialized = True

            # Detect Geo Pointing Module (optional hardware)
            self._detect_gpm()

            self.logger.info("PTU initialization completed successfully")
            return True

        except Exception as e:
            self.logger.error(f"PTU initialization failed: {e}")
            return False

    def _set_power_modes(self):
        """Set hold and move power modes for both axes."""
        # Set hold power modes
        self.send_command(f"PH{self.config.hold_power_mode.value}")
        self.send_command(f"TH{self.config.hold_power_mode.value}")

        # Set move power modes
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
            True if movement command accepted (and completed, if wait=True).
        """
        if not self._is_initialized:
            raise RuntimeError("PTU not initialized")

        try:
            pan_resp = self.send_command(f"PP{pan_steps}")
            tilt_resp = self.send_command(f"TP{tilt_steps}")

            if pan_resp.startswith("!") or tilt_resp.startswith("!"):
                self.logger.error(
                    f"Position command failed: Pan={pan_resp}, Tilt={tilt_resp}"
                )
                return False

            if wait:
                self.await_completion()

            self.logger.info(f"Moved to position: Pan={pan_steps}, Tilt={tilt_steps}")
            return True

        except Exception as e:
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

            pan_steps_delta = int(pan_degrees * self.pan_resolution)
            tilt_steps_delta = int(tilt_degrees * self.tilt_resolution)

            new_pan = current_pan + pan_steps_delta
            new_tilt = current_tilt + tilt_steps_delta

            return self.move_to_position(new_pan, new_tilt, wait)

        except Exception as e:
            self.logger.error(f"Relative move failed: {e}")
            return False

    def get_position(self) -> Tuple[int, int]:
        """Get current pan and tilt positions in encoder steps.

        Returns
        -------
        tuple of (int, int)
            Current (pan_steps, tilt_steps) position.
        """
        pan_resp = self.send_command("PP")
        tilt_resp = self.send_command("TP")

        # Parse responses (format: "* Current Pan position is <value>")
        pan_steps = int(pan_resp.split()[-1])
        tilt_steps = int(tilt_resp.split()[-1])

        return pan_steps, tilt_steps

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
        pan_degrees = pan_steps / self.pan_resolution
        tilt_degrees = tilt_steps / self.tilt_resolution

        return pan_degrees, tilt_degrees

    def await_completion(self, timeout: float = 30.0) -> bool:
        """Wait for all movement to complete.

        Parameters
        ----------
        timeout : float
            Maximum wait time in seconds.

        Returns
        -------
        bool
            True if movement completed within timeout.
        """
        start_time = time.time()

        while time.time() - start_time < timeout:
            response = self.send_command("A")
            if response.strip() == "*":
                return True
            time.sleep(0.1)

        self.logger.warning("Movement completion timeout")
        return False

    def halt(self):
        """Emergency halt of all movement."""
        self.send_command("H")
        self.logger.info("PTU movement halted")

    def save_settings(self):
        """Save current settings as power-on defaults."""
        self.send_command("DS")
        self.logger.info("PTU settings saved")

    def get_status(self) -> Dict[str, Any]:
        """Get comprehensive PTU status.

        Returns
        -------
        dict
            Status information including position, limits, and
            temperature/voltage readings.
        """
        status = {}

        # Position
        pan_steps, tilt_steps = self.get_position()
        status['position_steps'] = {'pan': pan_steps, 'tilt': tilt_steps}

        if self.pan_resolution and self.tilt_resolution:
            pan_deg, tilt_deg = self.get_position_degrees()
            status['position_degrees'] = {'pan': pan_deg, 'tilt': tilt_deg}

        # Limits
        limits_resp = self.send_command("L")
        status['limits'] = limits_resp

        # Temperature and voltage
        temp_resp = self.send_command("O")
        status['temperature_voltage'] = temp_resp

        # GPM status
        if self.gpm is not None:
            try:
                status['gpm'] = self.gpm.get_status().to_dict()
            except Exception:
                status['gpm'] = {"available": True, "error": "status query failed"}
        else:
            status['gpm'] = {"available": False}

        return status
