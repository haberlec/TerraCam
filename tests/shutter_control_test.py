#!/usr/bin/env python3
"""
Shutter Control Test

Tests manual shutter control to determine:
1. Does opening shutter then commanding NORMAL frame reset/cycle the shutter?
2. Does opening shutter then commanding DARK frame preserve shutter state?
3. Does opening shutter then commanding FLOOD frame preserve shutter state?

This helps determine if we can bypass the mechanical shutter timing limits
by manually controlling the shutter and using electronic integration.

Author: Claude Code
"""

import sys
import time
import numpy as np
from datetime import datetime
from pathlib import Path
from ctypes import POINTER, byref, c_char_p

# Use the installed fli package
from fli.core.camera import USBCamera
from fli.core.filter_wheel import USBFilterWheel
from fli.core.lib import (
    FLILibrary, FLIDOMAIN_USB, FLIDEVICE_CAMERA, FLIDEVICE_FILTERWHEEL,
    flidomain_t, flishutter_t,
    FLI_SHUTTER_CLOSE, FLI_SHUTTER_OPEN
)

sdk_root = Path(__file__).parent.parent


def find_camera():
    """Find and connect to the FLI camera."""
    dll = FLILibrary.getDll(debug=False)
    dom = flidomain_t(FLIDOMAIN_USB | FLIDEVICE_CAMERA)
    names_ptr = POINTER(c_char_p)()

    result = dll.FLIList(dom, byref(names_ptr))
    if result != 0:
        raise RuntimeError("Failed to enumerate cameras")

    idx = 0
    cam = None
    while names_ptr[idx]:
        device_info = names_ptr[idx].decode('utf-8')
        device_id, model = device_info.split(';', 1)
        if "MicroLine" in model or "ML" in model:
            print(f"Found camera: {model}")
            cam = USBCamera(device_id.encode(), model.encode())
            break
        idx += 1

    dll.FLIFreeList(names_ptr)
    if cam is None:
        raise RuntimeError("No MicroLine camera found")
    return cam


def find_filter_wheel():
    """Find and connect to the FLI filter wheel."""
    dll = FLILibrary.getDll(debug=False)
    dom = flidomain_t(FLIDOMAIN_USB | FLIDEVICE_FILTERWHEEL)
    names_ptr = POINTER(c_char_p)()

    result = dll.FLIList(dom, byref(names_ptr))
    if result != 0:
        raise RuntimeError("Failed to enumerate filter wheels")

    if not names_ptr or not names_ptr[0]:
        raise RuntimeError("No filter wheels found")

    device_info = names_ptr[0].decode('utf-8')
    device_id, model = device_info.split(';', 1)
    print(f"Found filter wheel: {model}")

    fw = USBFilterWheel(device_id.encode(), model.encode())
    dll.FLIFreeList(names_ptr)
    return fw


def control_shutter(cam, open_shutter):
    """Manually control the shutter."""
    dll = FLILibrary.getDll()
    shutter_cmd = flishutter_t(FLI_SHUTTER_OPEN if open_shutter else FLI_SHUTTER_CLOSE)
    result = dll.FLIControlShutter(cam._dev, shutter_cmd)
    if result != 0:
        print(f"  Warning: FLIControlShutter returned {result}")
    return result


def analyze_frame(frame, label):
    """Analyze and print frame statistics."""
    if frame is None:
        print(f"  {label}: No data")
        return None

    data = np.array(frame, dtype=np.float64)
    h, w = data.shape
    margin = 100
    center = data[margin:h-margin, margin:w-margin]

    stats = {
        'mean': np.mean(center),
        'std': np.std(center),
        'min': np.min(center),
        'max': np.max(center),
    }

    print(f"  {label}: mean={stats['mean']:.1f}, std={stats['std']:.1f}, "
          f"min={stats['min']:.0f}, max={stats['max']:.0f}")
    return stats


def test_shutter_behavior(cam, exposure_ms=100, interactive=False):
    """Test shutter behavior with different frame types."""

    print("\n" + "=" * 70)
    print("SHUTTER CONTROL TEST")
    print("=" * 70)
    print(f"Exposure time: {exposure_ms}ms")
    if interactive:
        print("\nListening for shutter sounds during each test...")

    results = {}

    def wait_for_user(msg):
        if interactive:
            input(msg)
        else:
            print(msg.replace("Press Enter to start...", "Starting..."))
            time.sleep(1.0)

    # Ensure shutter starts closed
    print("  Ensuring shutter is closed...")
    control_shutter(cam, open_shutter=False)
    time.sleep(0.5)

    # Baseline: Normal dark frame (shutter should stay closed)
    print("\n--- Test 0: Baseline DARK frame (shutter closed) ---")
    print("  Expected: No shutter sound, bias-level signal")
    wait_for_user("  Press Enter to start...")
    cam.set_exposure(exposure_ms, "dark")
    time.sleep(0.5)
    frame = cam.take_photo()
    results['baseline_dark'] = analyze_frame(frame, "Dark frame")
    control_shutter(cam, open_shutter=False)  # Ensure closed
    time.sleep(0.3)

    # Baseline: Normal frame (shutter cycles automatically)
    print("\n--- Test 1: Baseline NORMAL frame (auto shutter) ---")
    print("  Expected: Shutter open/close sounds, light signal")
    wait_for_user("  Press Enter to start...")
    cam.set_exposure(exposure_ms, "normal")
    time.sleep(0.5)
    frame = cam.take_photo()
    results['baseline_normal'] = analyze_frame(frame, "Normal frame")
    control_shutter(cam, open_shutter=False)  # Ensure closed
    time.sleep(0.3)

    # Test 2: Open shutter, then NORMAL frame
    print("\n--- Test 2: Manual OPEN shutter, then NORMAL frame ---")
    print("  Question: Does firmware reset/cycle the shutter?")
    wait_for_user("  Press Enter to start...")
    print("  Opening shutter manually...")
    control_shutter(cam, open_shutter=True)
    time.sleep(0.5)  # Let shutter fully open
    print("  Starting NORMAL exposure...")
    cam.set_exposure(exposure_ms, "normal")
    time.sleep(0.1)
    frame = cam.take_photo()
    results['open_then_normal'] = analyze_frame(frame, "Open+Normal")
    control_shutter(cam, open_shutter=False)  # Ensure closed
    time.sleep(0.3)

    # Test 3: Open shutter, then DARK frame
    print("\n--- Test 3: Manual OPEN shutter, then DARK frame ---")
    print("  Question: Does DARK frame keep shutter open or close it?")
    wait_for_user("  Press Enter to start...")
    print("  Opening shutter manually...")
    control_shutter(cam, open_shutter=True)
    time.sleep(0.5)  # Let shutter fully open
    print("  Starting DARK exposure (firmware should NOT touch shutter)...")
    cam.set_exposure(exposure_ms, "dark")
    time.sleep(0.1)
    frame = cam.take_photo()
    results['open_then_dark'] = analyze_frame(frame, "Open+Dark")
    control_shutter(cam, open_shutter=False)  # Ensure closed
    time.sleep(0.3)

    # Test 4: Open shutter, then RBI_FLUSH frame (flood + dark)
    print("\n--- Test 4: Manual OPEN shutter, then RBI_FLUSH frame ---")
    print("  Question: What does flood frame do to shutter?")
    wait_for_user("  Press Enter to start...")
    print("  Opening shutter manually...")
    control_shutter(cam, open_shutter=True)
    time.sleep(0.5)
    print("  Starting RBI_FLUSH exposure...")
    cam.set_exposure(exposure_ms, "rbi_flush")
    time.sleep(0.1)
    frame = cam.take_photo()
    results['open_then_flood'] = analyze_frame(frame, "Open+Flood")
    control_shutter(cam, open_shutter=False)  # Ensure closed
    time.sleep(0.3)

    # Analysis
    print("\n" + "=" * 70)
    print("ANALYSIS")
    print("=" * 70)

    dark_level = results['baseline_dark']['mean'] if results['baseline_dark'] else 0
    normal_level = results['baseline_normal']['mean'] if results['baseline_normal'] else 0

    print(f"\nBaseline dark level: {dark_level:.1f} DN")
    print(f"Baseline normal level: {normal_level:.1f} DN")
    print(f"Light signal: {normal_level - dark_level:.1f} DN")

    for test_name, stats in results.items():
        if stats and test_name not in ['baseline_dark', 'baseline_normal']:
            signal_above_dark = stats['mean'] - dark_level
            print(f"\n{test_name}:")
            print(f"  Signal above dark: {signal_above_dark:.1f} DN")
            if signal_above_dark > (normal_level - dark_level) * 0.5:
                print(f"  -> Shutter was OPEN during exposure")
            else:
                print(f"  -> Shutter was CLOSED during exposure")

    return results


def main():
    print("=" * 70)
    print("FLI CAMERA SHUTTER CONTROL TEST")
    print("=" * 70)
    print(f"Timestamp: {datetime.now().isoformat()}")

    try:
        # Connect to camera
        print("\n1. Connecting to camera...")
        cam = find_camera()

        # Configure camera
        print("\n2. Configuring camera...")
        cam.set_flushes(2)
        cam.set_image_binning(1, 1)

        # Set filter wheel to position 0
        print("\n3. Setting filter wheel to position 0...")
        fw = find_filter_wheel()
        if fw.get_filter_pos() != 0:
            fw.set_filter_pos(0)
            time.sleep(2.0)
        print(f"   Filter position: {fw.get_filter_pos()}")

        # Wait for settling
        print("\n4. Waiting for camera to settle...")
        time.sleep(2.0)

        # Run shutter tests
        results = test_shutter_behavior(cam, exposure_ms=25)

        print("\n\nTest complete!")
        return 0

    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
