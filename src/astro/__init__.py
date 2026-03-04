"""
TerraCam Astrometry Package

Provides celestial body tracking via NASA SPICE toolkit (SpiceyPy),
coordinate transformations from celestial to PTU frame, and
continuous tracking loop integration with PayloadCoordinator.

Usage:
    from astro import CelestialTracker, CelestialTarget, KernelManager

    tracker = CelestialTracker(coordinator)
    tracker.initialize()
    tracker.track(TrackingConfig(
        target=CelestialTarget.from_spice_body("MOON"),
    ))
"""

from .ephemeris import (
    KernelManager,
    ObserverLocation,
    CelestialTarget,
    AzElResult,
    PTUAngles,
    TargetType,
    compute_azimuth_elevation,
    az_el_to_ptu_angles,
)
from .tracker import CelestialTracker, TrackingConfig, TrackingResult

__all__ = [
    'KernelManager',
    'ObserverLocation',
    'CelestialTarget',
    'AzElResult',
    'PTUAngles',
    'TargetType',
    'compute_azimuth_elevation',
    'az_el_to_ptu_angles',
    'CelestialTracker',
    'TrackingConfig',
    'TrackingResult',
]
