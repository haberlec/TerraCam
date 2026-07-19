"""
PTU Serial Port Auto-Discovery

Enumerates available serial ports and probes each one for a FLIR PTU
by sending the VM (Version Model) command. When a matching response is
found, the port is identified as a PTU and its model, serial number,
and firmware version are returned.

Usage:
    from ptu.discovery import discover_ptu

    device = discover_ptu()
    if device:
        print(f"Found PTU on {device.port}: {device.model}")
"""

import logging
import serial
import serial.tools.list_ports
from dataclasses import dataclass
from typing import Optional, List


@dataclass
class PTUDeviceInfo:
    """Information about a discovered PTU device.

    Parameters
    ----------
    port : str
        Serial port path (e.g. "/dev/ttyUSB0").
    model : str
        Model identification string from VM response.
    serial_number : str, optional
        Serial number from VS response.
    firmware_version : str, optional
        Firmware version from V response.
    """
    port: str
    model: str
    serial_number: Optional[str] = None
    firmware_version: Optional[str] = None


def list_serial_ports() -> List[str]:
    """List all available serial port device paths.

    Returns
    -------
    list of str
        Serial port device paths.
    """
    return [p.device for p in serial.tools.list_ports.comports()]


def _read_reply(
    conn: serial.Serial,
    command: str,
    max_lines: int = 4,
) -> Optional[str]:
    """Read lines until one correlates with ``command``.

    The PTU echoes commands by default, so a genuine response line either
    begins with the echoed command (``"VM * PTU-D100E..."``) or, with
    echo disabled, is a bare ``*``/``!`` line. Anything else is a stale
    response from an earlier exchange and is discarded, so the probe
    cannot desynchronize the command/response stream it leaves behind.

    Parameters
    ----------
    conn : serial.Serial
        Open serial connection.
    command : str
        Command string that was sent (without delimiter).
    max_lines : int
        Maximum lines to read before giving up.

    Returns
    -------
    str or None
        Response payload with the echo stripped, beginning with ``*`` or
        ``!``; None if no correlated response arrived.
    """
    for _ in range(max_lines):
        raw = conn.readline()
        if not raw:
            return None
        line = raw.decode(errors="replace").strip()
        if not line:
            continue
        if line.startswith(command):
            payload = line[len(command):].strip()
            if payload.startswith(("*", "!")):
                return payload
            continue
        if line.startswith(("*", "!")):
            return line
    return None


def probe_port(
    port: str,
    baudrate: int = 9600,
    timeout: float = 1.0,
    model_filter: str = "D100",
) -> Optional[PTUDeviceInfo]:
    """Probe a single serial port for a FLIR PTU.

    Opens a temporary connection, sends the VM (Version Model) command,
    and checks whether the response contains ``model_filter``. If a
    match is found, also queries VS (serial number) and V (firmware).
    Responses are read with echo-aware correlation so the probe works
    whether the PTU has command echo enabled (the default) or disabled,
    and leaves no unconsumed responses behind.

    Parameters
    ----------
    port : str
        Serial port path.
    baudrate : int
        Baud rate for the probe connection.
    timeout : float
        Read timeout in seconds.
    model_filter : str
        Substring to match in the VM response (default "D100").

    Returns
    -------
    PTUDeviceInfo or None
        Device info if a PTU was found, None otherwise.
    """
    conn = None
    try:
        conn = serial.Serial(
            port=port,
            baudrate=baudrate,
            timeout=timeout,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
        )

        # Flush stale data
        conn.reset_input_buffer()
        conn.reset_output_buffer()

        # Query model
        conn.write(b"VM ")
        vm_payload = _read_reply(conn, "VM")
        if vm_payload is None or model_filter not in vm_payload:
            return None

        model = vm_payload.lstrip("*").strip()

        # Query serial number
        serial_number = None
        conn.write(b"VS ")
        vs_payload = _read_reply(conn, "VS")
        if vs_payload is not None and vs_payload.startswith("*"):
            serial_number = vs_payload.lstrip("*").strip() or None

        # Query firmware version
        firmware_version = None
        conn.write(b"V ")
        v_payload = _read_reply(conn, "V")
        if v_payload is not None and v_payload.startswith("*"):
            firmware_version = v_payload.lstrip("*").strip() or None

        return PTUDeviceInfo(
            port=port,
            model=model,
            serial_number=serial_number,
            firmware_version=firmware_version,
        )

    except (serial.SerialException, OSError, UnicodeDecodeError) as e:
        log = logging.getLogger(__name__)
        log.debug(f"Probe of {port} failed: {e}")
        return None

    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def discover_ptu(
    baudrate: int = 9600,
    timeout: float = 1.0,
    model_filter: str = "D100",
    logger: Optional[logging.Logger] = None,
) -> Optional[PTUDeviceInfo]:
    """Auto-discover a FLIR PTU on available serial ports.

    Enumerates all serial ports, probes each one for a PTU by sending
    the VM command, and returns the first match.

    Parameters
    ----------
    baudrate : int
        Baud rate for probe connections (default 9600).
    timeout : float
        Per-port read timeout in seconds.
    model_filter : str
        Substring to match in VM response (default "D100").
    logger : logging.Logger, optional
        Logger for discovery progress messages.

    Returns
    -------
    PTUDeviceInfo or None
        Discovered PTU info, or None if no PTU found.
    """
    log = logger or logging.getLogger(__name__)

    ports = serial.tools.list_ports.comports()
    log.info(f"PTU discovery: scanning {len(ports)} serial port(s)...")

    for port_info in ports:
        port = port_info.device
        log.debug(f"  Probing {port} ({port_info.description})...")

        result = probe_port(port, baudrate, timeout, model_filter)
        if result is not None:
            log.info(
                f"  PTU found on {port}: {result.model}"
                f" (S/N: {result.serial_number or 'unknown'})"
            )
            return result

        log.debug(f"  {port}: not a PTU")

    log.warning("PTU discovery: no PTU found on any serial port")
    return None
