"""
FLI Core Module

Low-level camera and filter wheel control interfaces.
This module provides direct access to FLI hardware through Python bindings.

Classes:
    USBCamera: Camera control (exposure, temperature, image acquisition)
    USBFilterWheel: Filter wheel positioning
    USBFocuser: Focuser control (stub)
    FLILibrary: Low-level C library interface
"""

from .camera import USBCamera
from .filter_wheel import USBFilterWheel
from .focuser import USBFocuser
from .device import USBDevice
from .lib import (
    FLILibrary,
    FLIError,
    FLIWarning,
    FLIDOMAIN_USB,
    FLIDEVICE_CAMERA,
    FLIDEVICE_FILTERWHEEL,
    FLIDEVICE_FOCUSER,
    FLI_FRAME_TYPE_NORMAL,
    FLI_FRAME_TYPE_DARK,
    FLI_FRAME_TYPE_FLOOD,
    FLI_FRAME_TYPE_RBI_FLUSH,
    flidomain_t,
)

__all__ = [
    # Device classes
    'USBCamera',
    'USBFilterWheel',
    'USBFocuser',
    'USBDevice',

    # Library interface
    'FLILibrary',
    'FLIError',
    'FLIWarning',

    # Constants
    'FLIDOMAIN_USB',
    'FLIDEVICE_CAMERA',
    'FLIDEVICE_FILTERWHEEL',
    'FLIDEVICE_FOCUSER',
    'FLI_FRAME_TYPE_NORMAL',
    'FLI_FRAME_TYPE_DARK',
    'FLI_FRAME_TYPE_FLOOD',
    'FLI_FRAME_TYPE_RBI_FLUSH',
    'flidomain_t',
]
