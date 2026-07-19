"""
FLI SDK - Python Interface for Finger Lakes Instrumentation Cameras

This package provides a Python interface for FLI cameras and filter wheels,
with robust image acquisition and device management.

Quick Start:
    from fli import FLISystem

    # Create system and discover devices
    system = FLISystem()
    system.discover_devices()
    system.initialize(target_temp=-20)

    # Capture an image
    image = system.capture_image(exposure_ms=100)

    # Clean up
    system.close()

    # Or use as context manager:
    with FLISystem() as system:
        system.discover_devices()
        image = system.capture_image(exposure_ms=100)

Modules:
    fli.core: Low-level camera and filter wheel control
    fli.acquisition: Robust image acquisition with error recovery
    fli.system: Unified device management
    fli.auto_expose: Auto-exposure via binary search with quality scoring
    fli.config: Project config file resolution (TERRACAM_CONFIG override)
    fli.io: Output formats (multispectral NetCDF)
"""

from .system import FLISystem
from .acquisition import ImageAcquisition

# Re-export commonly used items from core
from .core import (
    USBCamera,
    USBFilterWheel,
    USBFocuser,
    FLILibrary,
    FLIError,
    FLIWarning,
)

# Optional I/O formats
try:
    from .io import MultispectralNetCDF
except ImportError:
    MultispectralNetCDF = None

__version__ = "1.0.0"

__all__ = [
    # High-level API
    'FLISystem',
    'ImageAcquisition',

    # I/O formats (optional)
    'MultispectralNetCDF',

    # Core device classes
    'USBCamera',
    'USBFilterWheel',
    'USBFocuser',

    # Library interface
    'FLILibrary',
    'FLIError',
    'FLIWarning',

    # Version
    '__version__',
]
