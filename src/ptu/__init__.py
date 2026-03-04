"""
FLIR PTU D100E Pan-Tilt Unit Control Package

Provides serial communication and control interface for the FLIR PTU D100E
pan-tilt unit used in the TerraCam instrument suite.

Usage:
    from ptu import PTUController, PTUConfig

    config = PTUConfig()  # auto-discovers serial port
    ptu = PTUController(config)
    ptu.connect()
    ptu.initialize()
    ptu.move_to_position(pan_steps=1000, tilt_steps=-500)

    # GPM geo-pointing (if hardware is available):
    if ptu.gpm is not None:
        from ptu.gpm import GeoTarget
        ptu.gpm.point_to_coordinate(GeoTarget(40.7128, -74.0060, 10.0))
"""

from .controller import PTUController, PTUConfig, PowerMode
from .discovery import PTUDeviceInfo, discover_ptu
from .gpm import (
    GPMController,
    GPSPosition,
    MountingAttitude,
    GeoTarget,
    Landmark,
    CalibrationQuality,
    GPMStatus,
)
from .logger import SessionLogger, OperationTimer

__all__ = [
    'PTUController',
    'PTUConfig',
    'PowerMode',
    'PTUDeviceInfo',
    'discover_ptu',
    'GPMController',
    'GPSPosition',
    'MountingAttitude',
    'GeoTarget',
    'Landmark',
    'CalibrationQuality',
    'GPMStatus',
    'SessionLogger',
    'OperationTimer',
]
