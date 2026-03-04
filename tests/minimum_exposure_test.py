#!/usr/bin/env python3
"""
Minimum Exposure Time Test

Two focused tests:
1. Linearity test: Single sweep of exposures to verify signal scales linearly
2. Pre-open delay test: Find minimum effective shutter pre-open delay at 1ms exposure

Author: Claude Code
"""

import sys
import time
import numpy as np
from datetime import datetime
from pathlib import Path
from ctypes import POINTER, byref, c_char_p

from fli.core.camera import USBCamera
from fli.core.filter_wheel import USBFilterWheel
from fli.core.lib import (
    FLILibrary, FLIDOMAIN_USB, FLIDEVICE_CAMERA, FLIDEVICE_FILTERWHEEL,
    flidomain_t, flishutter_t,
    FLI_SHUTTER_CLOSE, FLI_SHUTTER_OPEN
)

sdk_root = Path(__file__).parent.parent


def control_shutter(cam, open_shutter):
    """Manually control the shutter."""
    dll = FLILibrary.getDll()
    shutter_cmd = flishutter_t(FLI_SHUTTER_OPEN if open_shutter else FLI_SHUTTER_CLOSE)
    return dll.FLIControlShutter(cam._dev, shutter_cmd)


def find_camera():
    """Find and connect to the FLI camera."""
    dll = FLILibrary.getDll(debug=False)
    dom = flidomain_t(FLIDOMAIN_USB | FLIDEVICE_CAMERA)
    names_ptr = POINTER(c_char_p)()

    if dll.FLIList(dom, byref(names_ptr)) != 0:
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

    if dll.FLIList(dom, byref(names_ptr)) != 0:
        raise RuntimeError("Failed to enumerate filter wheels")

    if not names_ptr or not names_ptr[0]:
        raise RuntimeError("No filter wheels found")

    device_info = names_ptr[0].decode('utf-8')
    device_id, model = device_info.split(';', 1)
    print(f"Found filter wheel: {model}")

    fw = USBFilterWheel(device_id.encode(), model.encode())
    dll.FLIFreeList(names_ptr)
    return fw


def capture_with_manual_shutter(cam, exposure_ms, pre_open_delay_ms):
    """Capture frame with manual shutter control."""
    control_shutter(cam, open_shutter=True)
    time.sleep(pre_open_delay_ms / 1000.0)
    cam.set_exposure(exposure_ms, "dark")  # DARK won't cycle shutter
    time.sleep(0.05)
    frame = cam.take_photo()
    control_shutter(cam, open_shutter=False)
    return frame


def capture_with_auto_shutter(cam, exposure_ms):
    """Capture frame with automatic shutter control."""
    cam.set_exposure(exposure_ms, "normal")
    time.sleep(0.05)
    return cam.take_photo()


def get_frame_mean(frame):
    """Get mean of center region."""
    if frame is None:
        return None
    data = np.array(frame, dtype=np.float64)
    h, w = data.shape
    margin = 100
    return np.mean(data[margin:h-margin, margin:w-margin])


def test_linearity(cam, shutter_threshold_ms):
    """
    Test 1: Linearity across exposure times.

    Uses manual shutter for exposures < threshold, auto shutter otherwise.
    """
    print("\n" + "=" * 70)
    print(f"TEST 1: LINEARITY (shutter threshold = {shutter_threshold_ms}ms)")
    print("=" * 70)

    exposure_times = [100, 75, 50, 40, 30, 25, 20, 15, 10, 5, 2, 1]
    pre_open_delay = 25  # ms

    print(f"\n{'Exp(ms)':<8} {'Mean DN':<12} {'Delta':<10} {'Rate':<12} {'Mode'}")
    print("-" * 60)

    results = []
    prev_mean = None

    for exp_ms in exposure_times:
        use_manual = exp_ms < shutter_threshold_ms

        if use_manual:
            frame = capture_with_manual_shutter(cam, exp_ms, pre_open_delay)
            mode = "manual"
        else:
            frame = capture_with_auto_shutter(cam, exp_ms)
            mode = "auto"

        mean = get_frame_mean(frame)
        delta = mean - prev_mean if prev_mean else 0
        rate = (mean - 1027) / exp_ms if exp_ms > 0 else 0  # Subtract bias (~1027)

        results.append({'exp_ms': exp_ms, 'mean': mean, 'mode': mode})
        print(f"{exp_ms:<8} {mean:<12.1f} {delta:<+10.1f} {rate:<12.2f} {mode}")

        prev_mean = mean
        time.sleep(0.1)

    # Check linearity by computing signal rate (excluding bias)
    bias = results[-1]['mean']  # 1ms exposure approximates bias
    rates = []
    for r in results[:-1]:  # Exclude 1ms
        signal_above_bias = r['mean'] - bias
        rate = signal_above_bias / r['exp_ms']
        rates.append(rate)

    mean_rate = np.mean(rates)
    std_rate = np.std(rates)

    print(f"\nSignal rate: {mean_rate:.2f} ± {std_rate:.2f} DN/ms (excluding bias)")
    print(f"Linearity: {'GOOD' if std_rate/mean_rate < 0.1 else 'POOR'} (CV = {100*std_rate/mean_rate:.1f}%)")

    return results


def test_preopen_delay(cam, exposure_ms=1):
    """
    Test 2: Find minimum effective pre-open delay.

    At a fixed short exposure (1ms), vary the pre-open delay and measure signal.
    """
    print("\n" + "=" * 70)
    print(f"TEST 2: PRE-OPEN DELAY (exposure = {exposure_ms}ms)")
    print("=" * 70)

    delays = [50, 40, 30, 25, 20, 15, 10, 5, 2, 1, 0]

    print(f"\n{'Delay(ms)':<12} {'Mean DN':<12} {'Delta from max'}")
    print("-" * 45)

    results = []
    max_mean = None

    for delay_ms in delays:
        frame = capture_with_manual_shutter(cam, exposure_ms, delay_ms)
        mean = get_frame_mean(frame)
        results.append({'delay_ms': delay_ms, 'mean': mean})

        if max_mean is None:
            max_mean = mean

        delta = mean - max_mean
        print(f"{delay_ms:<12} {mean:<12.1f} {delta:<+.1f}")
        time.sleep(0.1)

    # Find knee point where signal drops significantly
    threshold = 0.95 * max_mean
    min_delay = delays[0]
    for r in results:
        if r['mean'] >= threshold:
            min_delay = r['delay_ms']

    print(f"\nMaximum signal: {max_mean:.1f} DN (at {delays[0]}ms delay)")
    print(f"Minimum effective delay: {min_delay}ms (95% of max signal)")

    return results


def main():
    print("=" * 70)
    print("FLI CAMERA MINIMUM EXPOSURE TEST")
    print("=" * 70)
    print(f"Timestamp: {datetime.now().isoformat()}")

    try:
        # Setup
        print("\nConnecting to devices...")
        cam = find_camera()
        cam.set_flushes(2)
        cam.set_image_binning(1, 1)

        fw = find_filter_wheel()
        if fw.get_filter_pos() != 0:
            fw.set_filter_pos(0)
            time.sleep(2.0)
        print(f"Filter position: {fw.get_filter_pos()}")

        print("\nWaiting for camera to settle...")
        time.sleep(2.0)

        # Run tests
        linearity_results = test_linearity(cam, shutter_threshold_ms=50)
        delay_results = test_preopen_delay(cam, exposure_ms=1)

        print("\n" + "=" * 70)
        print("TESTS COMPLETE")
        print("=" * 70)

        return 0

    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
