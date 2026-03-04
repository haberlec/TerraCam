#!/usr/bin/env python3
"""
FLI Camera Live Focus Script for M2 Mac

This script provides a live video feed from the FLI camera for manual focus adjustment.
Uses a two-stage approach:
1. Auto-exposure using still frames (auto_expose.py) to find optimal exposure
2. Video mode display (capture_video.py) with fixed exposure for focusing

Features:
- Real-time image display using OpenCV
- Auto-exposure with quantitative quality metrics
- Video mode support for faster acquisition (~5-10 FPS)
- Focus metrics overlay (sharpness, contrast)
- Filter wheel control
- On-demand recalibration (press 'a')

Controls:
- ESC or 'q': Quit
- 'a': Recalibrate exposure (stops video, runs auto-expose, restarts)
- 's': Save current frame
- '0'-'9': Move filter wheel to position (triggers recalibration)

Usage:
  python3 live_focus.py                    # Default (video mode, auto-exposure first)
  python3 live_focus.py --filter 2         # Start with filter wheel at position 2
  python3 live_focus.py --no-video         # Use still image mode instead of video mode
  python3 live_focus.py --no-auto          # Skip initial auto-exposure, use --exposure value
  python3 live_focus.py --exposure 500     # Start with 500ms exposure (or use as initial)
  python3 live_focus.py --help             # Show help
"""
import sys
import os
import argparse
import logging
import time

# Import the auto-exposure module
try:
    from .auto_expose import auto_expose, evaluate_exposure, AutoExposeResult
except ImportError:
    # Running as script - add parent to path
    script_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.dirname(os.path.dirname(script_dir)))
    from scripts.capture.auto_expose import auto_expose, evaluate_exposure, AutoExposeResult

# Import the video capture module
try:
    from .capture_video import VideoCapture
except ImportError:
    from scripts.capture.capture_video import VideoCapture


def create_recalibration_callback(args, logger):
    """
    Create a callback function for exposure recalibration.

    This callback is invoked when the user presses 'a' or changes the filter.
    It stops video mode, runs auto-exposure using still frames, and returns
    the new optimal exposure time.
    """
    def recalibrate(video_capture: VideoCapture) -> int:
        """
        Recalibrate exposure using still frame auto-exposure.

        Args:
            video_capture: The VideoCapture instance (provides camera access)

        Returns:
            New optimal exposure time in milliseconds
        """
        logger.info("=" * 50)
        logger.info("Starting exposure recalibration...")

        camera = video_capture.camera

        if camera is None:
            logger.error("No camera available for recalibration")
            return video_capture.exposure_ms

        try:
            # Run auto-exposure using still frames
            result: AutoExposeResult = auto_expose(
                camera=camera,
                target_p95=args.target,
                min_exposure_ms=1,
                max_exposure_ms=30000,
                initial_exposure_ms=video_capture.exposure_ms,
                max_iterations=8,
                quality_threshold=0.70,
                tolerance=0.10,
                binning=video_capture.binning,
                flushes=1,
                logger=logger
            )

            logger.info(f"Recalibration complete:")
            logger.info(f"  New exposure: {result.exposure_ms}ms")
            logger.info(f"  Quality: {result.final_metrics.quality_grade} "
                       f"({result.final_metrics.quality_score:.2f})")
            logger.info(f"  P95: {result.final_metrics.p95:.0f} ADU "
                       f"({result.final_metrics.p95_utilization*100:.1f}%)")

            if result.final_metrics.warnings:
                for warning in result.final_metrics.warnings:
                    logger.warning(f"  {warning}")

            logger.info("=" * 50)
            return result.exposure_ms

        except Exception as e:
            logger.error(f"Recalibration failed: {e}")
            import traceback
            traceback.print_exc()
            return video_capture.exposure_ms

    return recalibrate


def run_initial_auto_expose(args, logger):
    """
    Run initial auto-exposure before starting video mode.

    Uses a temporary camera connection to find optimal exposure.

    Returns:
        Optimal exposure time in milliseconds
    """
    from ctypes import POINTER, c_char_p, byref
    from fli.core.camera import USBCamera
    from fli.core.filter_wheel import USBFilterWheel
    from fli.core.lib import (
        FLILibrary, FLIDOMAIN_USB, FLIDEVICE_CAMERA, FLIDEVICE_FILTERWHEEL,
        flidomain_t
    )

    logger.info("Running initial auto-exposure...")
    logger.info("=" * 50)

    lib = FLILibrary.getDll()
    camera = None
    filter_wheel = None

    try:
        # Find camera
        cam_domain = flidomain_t(FLIDOMAIN_USB | FLIDEVICE_CAMERA)
        tmplist = POINTER(c_char_p)()
        lib.FLIList(cam_domain, byref(tmplist))

        if tmplist:
            i = 0
            while tmplist[i]:
                device_info = tmplist[i].decode('utf-8')
                dev_name, model = device_info.split(';')

                if 'MicroLine' in model and 'ML' in model:
                    camera = USBCamera(dev_name.encode(), model.encode())
                    logger.info(f"Camera connected: {model}")
                    break
                i += 1
            lib.FLIFreeList(tmplist)

        if not camera:
            raise RuntimeError("No MicroLine camera found!")

        # Find filter wheel (optional)
        fw_domain = flidomain_t(FLIDOMAIN_USB | FLIDEVICE_FILTERWHEEL)
        tmplist = POINTER(c_char_p)()
        lib.FLIList(fw_domain, byref(tmplist))

        if tmplist:
            i = 0
            while tmplist[i]:
                device_info = tmplist[i].decode('utf-8')
                dev_name, model = device_info.split(';')

                if 'Filter Wheel' in model or 'CenterLine' in model:
                    filter_wheel = USBFilterWheel(dev_name.encode(), model.encode())
                    logger.info(f"Filter wheel connected: {model}")
                    break
                i += 1
            lib.FLIFreeList(tmplist)

        # Move to initial filter position if specified
        if args.filter is not None and filter_wheel:
            logger.info(f"Moving filter wheel to position {args.filter}...")
            filter_wheel.set_filter_pos(args.filter)
            filter_wheel.wait_for_movement_completion(timeout_seconds=30)
            logger.info(f"Filter wheel at position {filter_wheel.get_filter_pos()}")

        # Set binning
        camera.set_image_binning(args.binning, args.binning)

        # Run auto-exposure
        result: AutoExposeResult = auto_expose(
            camera=camera,
            target_p95=args.target,
            min_exposure_ms=1,
            max_exposure_ms=30000,
            initial_exposure_ms=args.exposure,
            max_iterations=8,
            quality_threshold=0.70,
            tolerance=0.10,
            binning=(args.binning, args.binning),
            flushes=1,
            logger=logger
        )

        logger.info("=" * 50)
        logger.info(f"Initial auto-exposure complete:")
        logger.info(f"  Optimal exposure: {result.exposure_ms}ms")
        logger.info(f"  Quality: {result.final_metrics.quality_grade} "
                   f"({result.final_metrics.quality_score:.2f})")
        logger.info(f"  Scene: {result.scene_type}")
        logger.info("=" * 50)

        return result.exposure_ms

    finally:
        # Clean up temporary connections
        # Note: We close these so VideoCapture can open fresh connections
        if camera:
            try:
                lib.FLIClose(camera._dev)
            except:
                pass

        if filter_wheel:
            try:
                lib.FLIClose(filter_wheel._dev)
            except:
                pass

        # Brief delay before VideoCapture opens devices
        time.sleep(0.5)


def main():
    """Main function with command line argument support."""
    parser = argparse.ArgumentParser(
        description="FLI Live Focus Tool - Real-time camera view for manual focusing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Controls (in the display window):
  ESC or 'q'      : Quit
  'a'             : Recalibrate exposure (runs auto-expose)
  's'             : Save current frame
  '0' through '9' : Move filter wheel to position 0-9

Examples:
  python3 live_focus.py                    # Default (video mode, auto-exposure)
  python3 live_focus.py --filter 2         # Start with filter at position 2
  python3 live_focus.py -f 5               # Start with filter at position 5
  python3 live_focus.py --no-video         # Use still image mode
  python3 live_focus.py --no-auto          # Skip initial auto-exposure
  python3 live_focus.py --exposure 500     # Start with 500ms exposure
  python3 live_focus.py --target 0.7       # Set auto-exposure target to 70%
        """
    )

    parser.add_argument(
        '-f', '--filter',
        type=int,
        metavar='POS',
        help='Initial filter wheel position (0-15, depends on your filter wheel)'
    )

    parser.add_argument(
        '--no-video',
        action='store_true',
        help='Disable video mode and use still image capture (slower but more compatible)'
    )

    parser.add_argument(
        '--no-auto',
        action='store_true',
        help='Skip initial auto-exposure (use --exposure value directly)'
    )

    parser.add_argument(
        '--exposure', '-e',
        type=int,
        default=100,
        metavar='MS',
        help='Initial exposure time in milliseconds (default: 100)'
    )

    parser.add_argument(
        '--target', '-t',
        type=float,
        default=0.75,
        metavar='BRIGHTNESS',
        help='Auto-exposure target P95 as fraction 0.0-1.0 (default: 0.75)'
    )

    parser.add_argument(
        '--binning', '-b',
        type=int,
        default=2,
        choices=[1, 2, 3, 4],
        help='Pixel binning factor (default: 2 for 2x2 binning)'
    )

    args = parser.parse_args()

    # Validate arguments
    if args.filter is not None:
        if args.filter < 0 or args.filter > 15:
            print(f"Error: Filter position must be between 0-15, got {args.filter}")
            return 1

    if args.target < 0.0 or args.target > 1.0:
        print(f"Error: Target brightness must be between 0.0-1.0, got {args.target}")
        return 1

    if args.exposure < 1 or args.exposure > 30000:
        print(f"Error: Exposure must be between 1-30000ms, got {args.exposure}")
        return 1

    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    logger = logging.getLogger(__name__)

    print("FLI Live Focus Tool")
    print("=" * 50)

    # Display configuration
    mode_str = "Still Mode" if args.no_video else "Video Mode"
    auto_str = "Disabled" if args.no_auto else f"Target P95 {args.target:.0%}"
    print(f"Mode: {mode_str}")
    print(f"Auto-Exposure: {auto_str}")
    print(f"Initial Exposure: {args.exposure}ms")
    print(f"Binning: {args.binning}x{args.binning}")
    if args.filter is not None:
        print(f"Initial Filter Position: {args.filter}")
    print()

    # Determine starting exposure
    if args.no_auto:
        # Use provided exposure directly
        exposure_ms = args.exposure
        logger.info(f"Using manual exposure: {exposure_ms}ms")
    else:
        # Run initial auto-exposure
        try:
            exposure_ms = run_initial_auto_expose(args, logger)
        except Exception as e:
            logger.error(f"Initial auto-exposure failed: {e}")
            logger.info(f"Falling back to default exposure: {args.exposure}ms")
            exposure_ms = args.exposure

    # Create recalibration callback
    recalibrate_callback = create_recalibration_callback(args, logger)

    try:
        # Create and start video capture with the determined exposure
        with VideoCapture(
            exposure_ms=exposure_ms,
            use_video_mode=not args.no_video,
            binning=(args.binning, args.binning),
            window_name="FLI Live Focus",
            recalibrate_callback=recalibrate_callback
        ) as vc:
            # Move to initial filter position if specified
            # (VideoCapture opens fresh connections, so we need to move again)
            if args.filter is not None and vc.filter_wheel:
                logger.info(f"Moving filter wheel to position {args.filter}...")
                vc.filter_wheel.set_filter_pos(args.filter)
                vc.filter_wheel.wait_for_movement_completion(timeout_seconds=30)
                vc.current_filter_pos = vc.filter_wheel.get_filter_pos()

            # Start live view
            vc.start_live_view()

    except ImportError as e:
        print(f"Error: Missing required library - {e}")
        print("Install OpenCV with: pip install opencv-python")
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted by user")
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return 1

    print("Live focus session ended")
    return 0


if __name__ == "__main__":
    sys.exit(main())
