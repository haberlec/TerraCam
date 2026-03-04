#!/usr/bin/env python3
"""
FLI Camera Video Capture Module

Pure video display module for FLI cameras using OpenCV.
Exposure should be set BEFORE starting video mode - this module
does not perform auto-exposure (use auto_expose.py for that).

Features:
- OpenCV-based display for fast frame rates
- True video mode support (FLIGrabVideoFrame)
- Focus metrics overlay (sharpness, contrast)
- Filter wheel control
- Frame saving

Usage:
    from scripts.capture.capture_video import VideoCapture

    # Basic usage with fixed exposure
    with VideoCapture(exposure_ms=200) as vc:
        vc.start_live_view()

    # With auto-exposure (use auto_expose first)
    from scripts.capture.auto_expose import auto_expose
    result = auto_expose(camera)
    with VideoCapture(exposure_ms=result.exposure_ms) as vc:
        vc.start_live_view()

Controls (during live view):
    q, ESC  : Quit
    s       : Save current frame
    0-9     : Filter wheel position (triggers recalibration callback)

Note: Exposure adjustment during video requires stopping/restarting video mode.
Use the 'a' key in live_focus.py to trigger recalibration cycle.
"""
import sys
import os
import time
import threading
import queue
import logging
import numpy as np
from datetime import datetime
from typing import Optional, Tuple, Dict, Any, Callable
from ctypes import POINTER, c_char_p, byref

try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False
    print("Warning: OpenCV not available. Install with: pip install opencv-python")

from PIL import Image

# Use the new package imports
from fli.core.camera import USBCamera
from fli.core.filter_wheel import USBFilterWheel
from fli.core.lib import (
    FLILibrary, FLIDOMAIN_USB, FLIDEVICE_CAMERA, FLIDEVICE_FILTERWHEEL,
    flidomain_t
)


class VideoCapture:
    """
    Real-time video capture and display for FLI cameras.

    This is a PURE video display class - it does not perform auto-exposure.
    Set exposure_ms before creating the VideoCapture, or pass a
    recalibrate_callback to handle exposure changes.

    The video mode uses FLIGrabVideoFrame for faster frame acquisition
    compared to repeated take_photo() calls.
    """

    def __init__(
        self,
        exposure_ms: int = 100,
        use_video_mode: bool = True,
        binning: Tuple[int, int] = (2, 2),
        window_name: str = "FLI Camera - Live View",
        recalibrate_callback: Optional[Callable[['VideoCapture'], int]] = None
    ):
        """
        Initialize video capture.

        Args:
            exposure_ms: Fixed exposure time in milliseconds
            use_video_mode: If True, use FLI video mode (faster)
            binning: Pixel binning (hbin, vbin)
            window_name: OpenCV window title
            recalibrate_callback: Optional function called when 'a' is pressed.
                                 Should return new exposure_ms.
                                 Signature: callback(video_capture) -> int
        """
        if not CV2_AVAILABLE:
            raise ImportError("OpenCV required. Install: pip install opencv-python")

        self.exposure_ms = exposure_ms
        self.use_video_mode = use_video_mode
        self.binning = binning
        self.window_name = window_name
        self.recalibrate_callback = recalibrate_callback

        # Device handles
        self.camera: Optional[USBCamera] = None
        self.filter_wheel: Optional[USBFilterWheel] = None
        self.lib = FLILibrary.getDll()

        # State
        self.running = False
        self.video_mode_active = False
        self.current_image: Optional[np.ndarray] = None
        self.current_metrics: Dict[str, Any] = {}

        # Threading
        self.capture_thread: Optional[threading.Thread] = None
        self.frame_queue: queue.Queue = queue.Queue(maxsize=3)

        # Statistics
        self.frame_count = 0
        self.fps = 0.0
        self.last_fps_time = time.time()
        self.fps_frame_count = 0

        # Filter wheel state
        self.current_filter_pos: Optional[int] = None
        self.filter_count: int = 0

        # Recalibration request flag
        self._recalibrate_requested = False

        # Logging
        self.logger = logging.getLogger(__name__)

    def __enter__(self):
        """Context manager entry."""
        self.discover_devices()
        self.setup_camera()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.stop()
        self.cleanup()
        return False

    def discover_devices(self):
        """Find and connect to FLI camera and filter wheel."""
        self.logger.info("Discovering FLI devices...")

        # Find camera
        cam_domain = flidomain_t(FLIDOMAIN_USB | FLIDEVICE_CAMERA)
        tmplist = POINTER(c_char_p)()
        self.lib.FLIList(cam_domain, byref(tmplist))

        if tmplist:
            i = 0
            while tmplist[i]:
                device_info = tmplist[i].decode('utf-8')
                dev_name, model = device_info.split(';')

                if 'MicroLine' in model and 'ML' in model:
                    self.camera = USBCamera(dev_name.encode(), model.encode())
                    self.logger.info(f"Camera connected: {model}")
                    break
                i += 1
            self.lib.FLIFreeList(tmplist)

        if not self.camera:
            raise RuntimeError("No MicroLine camera found!")

        # Find filter wheel (optional)
        fw_domain = flidomain_t(FLIDOMAIN_USB | FLIDEVICE_FILTERWHEEL)
        tmplist = POINTER(c_char_p)()
        self.lib.FLIList(fw_domain, byref(tmplist))

        if tmplist:
            i = 0
            while tmplist[i]:
                device_info = tmplist[i].decode('utf-8')
                dev_name, model = device_info.split(';')

                if 'Filter Wheel' in model or 'CenterLine' in model:
                    self.filter_wheel = USBFilterWheel(dev_name.encode(), model.encode())
                    self.filter_count = self.filter_wheel.get_filter_count()
                    self.current_filter_pos = self.filter_wheel.get_filter_pos()
                    self.logger.info(f"Filter wheel connected: {model}")
                    break
                i += 1
            self.lib.FLIFreeList(tmplist)

    def setup_camera(self):
        """Configure camera for live viewing."""
        self.logger.info("Setting up camera...")

        # Set binning
        hbin, vbin = self.binning
        self.camera.set_image_binning(hbin, vbin)
        self.logger.info(f"Binning: {hbin}x{vbin}")

        # Set flushes
        self.camera.set_flushes(2)

        # Get image size
        row_width, img_rows, _ = self.camera.get_image_size()
        self.logger.info(f"Image size: {row_width}x{img_rows}")

        # Set exposure
        self.camera.set_exposure(self.exposure_ms, frametype="normal")
        self.logger.info(f"Exposure: {self.exposure_ms}ms")

        # Start video mode if requested
        if self.use_video_mode:
            try:
                self.camera.start_video_mode()
                self.video_mode_active = True
                self.logger.info("Video mode started")
            except Exception as e:
                self.logger.warning(f"Video mode failed, using still capture: {e}")
                self.video_mode_active = False

    def capture_frame(self) -> Optional[np.ndarray]:
        """Capture a single frame."""
        try:
            if self.video_mode_active:
                return self.camera.grab_video_frame()
            else:
                return self.camera.take_photo()
        except Exception as e:
            self.logger.error(f"Capture error: {e}")
            return None

    def _capture_loop(self):
        """Background thread for continuous capture."""
        consecutive_errors = 0
        max_errors = 5

        while self.running:
            # Check for recalibration request
            if self._recalibrate_requested:
                self._recalibrate_requested = False
                self._handle_recalibration()
                continue

            try:
                image = self.capture_frame()

                if image is None or image.size == 0:
                    time.sleep(0.1)
                    continue

                # Skip zero frames (exposure not ready in video mode)
                if np.sum(image) == 0:
                    time.sleep(0.05)
                    continue

                consecutive_errors = 0
                self.frame_count += 1
                self.fps_frame_count += 1

                # Calculate FPS
                now = time.time()
                elapsed = now - self.last_fps_time
                if elapsed >= 1.0:
                    self.fps = self.fps_frame_count / elapsed
                    self.fps_frame_count = 0
                    self.last_fps_time = now

                # Calculate display metrics
                self.current_metrics = self._calculate_metrics(image)

                # Queue frame for display
                try:
                    self.frame_queue.put_nowait((image, self.current_metrics))
                except queue.Full:
                    try:
                        self.frame_queue.get_nowait()
                        self.frame_queue.put_nowait((image, self.current_metrics))
                    except queue.Empty:
                        pass

                # Frame pacing
                if self.video_mode_active:
                    # Video mode - pace based on exposure
                    frame_time = (self.exposure_ms + 50) / 1000.0
                    time.sleep(max(0.01, frame_time))
                else:
                    # Still mode - slower
                    time.sleep(0.5)

            except Exception as e:
                consecutive_errors += 1
                self.logger.error(f"Error {consecutive_errors}/{max_errors}: {e}")

                if consecutive_errors >= max_errors:
                    if self.video_mode_active:
                        self.logger.warning("Falling back to still mode")
                        try:
                            self.camera.stop_video_mode()
                        except:
                            pass
                        self.video_mode_active = False
                    consecutive_errors = 0
                time.sleep(0.5)

    def _handle_recalibration(self):
        """Handle recalibration request (runs in capture thread)."""
        if self.recalibrate_callback is None:
            self.logger.warning("No recalibration callback set")
            return

        self.logger.info("Recalibrating exposure...")

        # Stop video mode for calibration
        was_video_mode = self.video_mode_active
        if self.video_mode_active:
            try:
                self.camera.stop_video_mode()
                self.video_mode_active = False
            except:
                pass

        # Call the recalibration callback
        try:
            new_exposure_ms = self.recalibrate_callback(self)
            if new_exposure_ms and new_exposure_ms != self.exposure_ms:
                self.exposure_ms = new_exposure_ms
                self.camera.set_exposure(self.exposure_ms, frametype="normal")
                self.logger.info(f"New exposure: {self.exposure_ms}ms")
        except Exception as e:
            self.logger.error(f"Recalibration failed: {e}")

        # Restart video mode
        if was_video_mode and self.use_video_mode:
            try:
                self.camera.start_video_mode()
                self.video_mode_active = True
            except Exception as e:
                self.logger.warning(f"Failed to restart video mode: {e}")

    def _calculate_metrics(self, image: np.ndarray) -> Dict[str, Any]:
        """Calculate focus and exposure metrics."""
        try:
            img_float = image.astype(np.float32)

            # Sharpness via gradient
            grad_x = np.gradient(img_float, axis=1)
            grad_y = np.gradient(img_float, axis=0)
            sharpness = np.mean(np.sqrt(grad_x**2 + grad_y**2))

            # Percentiles
            p25, p50, p75, p95 = np.percentile(image, [25, 50, 75, 95])

            return {
                'sharpness': sharpness,
                'contrast': np.std(img_float),
                'mean': np.mean(image),
                'min': int(np.min(image)),
                'max': int(np.max(image)),
                'p25': p25,
                'p50': p50,
                'p75': p75,
                'p95': p95
            }
        except Exception:
            return {}

    def _scale_for_display(self, image: np.ndarray) -> np.ndarray:
        """Scale 16-bit image to 8-bit for display."""
        p1, p99 = np.percentile(image, [1, 99])
        if p99 > p1:
            scaled = (image.astype(np.float32) - p1) / (p99 - p1) * 255
            scaled = np.clip(scaled, 0, 255).astype(np.uint8)
        else:
            scaled = np.zeros_like(image, dtype=np.uint8)
        return scaled

    def _draw_overlay(self, display_image: np.ndarray) -> np.ndarray:
        """Draw status overlay on display image."""
        if len(display_image.shape) == 2:
            display_image = cv2.cvtColor(display_image, cv2.COLOR_GRAY2BGR)

        green = (0, 255, 0)
        white = (255, 255, 255)

        font = cv2.FONT_HERSHEY_SIMPLEX
        font_scale = 0.5
        thickness = 1
        line_height = 20

        y = 25
        x = 10

        # Mode and exposure
        mode = "Video" if self.video_mode_active else "Still"
        cv2.putText(display_image, f"Mode: {mode} | Exp: {self.exposure_ms}ms | FPS: {self.fps:.1f}",
                    (x, y), font, font_scale, green, thickness)
        y += line_height

        # Filter wheel
        if self.filter_wheel:
            cv2.putText(display_image, f"Filter: {self.current_filter_pos}/{self.filter_count-1}",
                        (x, y), font, font_scale, white, thickness)
            y += line_height

        # Image metrics
        if self.current_metrics:
            m = self.current_metrics
            cv2.putText(display_image,
                        f"Focus: {m.get('sharpness', 0):.1f} | Contrast: {m.get('contrast', 0):.1f}",
                        (x, y), font, font_scale, white, thickness)
            y += line_height
            cv2.putText(display_image,
                        f"P95: {m.get('p95', 0):.0f} | Range: {m.get('min', 0)}-{m.get('max', 0)}",
                        (x, y), font, font_scale, white, thickness)

        # Controls help at bottom
        h = display_image.shape[0]
        cv2.putText(display_image, "q:Quit  a:Recalibrate  s:Save  0-9:Filter",
                    (x, h - 10), font, font_scale * 0.9, (128, 128, 128), thickness)

        return display_image

    def start_live_view(self):
        """Start the live view display loop."""
        self.logger.info("Starting live view...")
        self.running = True

        # Start capture thread
        self.capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self.capture_thread.start()

        # Create window
        cv2.namedWindow(self.window_name, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(self.window_name, 1024, 768)

        print("\nLive view started. Controls:")
        print("  q/ESC  : Quit")
        print("  a      : Recalibrate exposure")
        print("  s      : Save frame")
        print("  0-9    : Filter position")
        print()

        try:
            while self.running:
                # Get frame
                try:
                    image, metrics = self.frame_queue.get(timeout=0.5)
                    self.current_image = image
                    self.current_metrics = metrics
                except queue.Empty:
                    key = cv2.waitKey(1) & 0xFF
                    if key == ord('q') or key == 27:
                        break
                    continue

                # Display
                display = self._scale_for_display(image)
                display = self._draw_overlay(display)
                cv2.imshow(self.window_name, display)

                # Handle input
                key = cv2.waitKey(1) & 0xFF
                self._handle_key(key)

                if key == ord('q') or key == 27:
                    break

                if cv2.getWindowProperty(self.window_name, cv2.WND_PROP_VISIBLE) < 1:
                    break

        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def _handle_key(self, key: int):
        """Handle keyboard input."""
        if key == ord('a'):
            if self.recalibrate_callback:
                self._recalibrate_requested = True
                print("Recalibration requested...")
            else:
                print("No recalibration callback set")
        elif key == ord('s'):
            self._save_frame()
        elif ord('0') <= key <= ord('9'):
            self._move_filter(key - ord('0'))

    def _move_filter(self, position: int):
        """Move filter wheel."""
        if not self.filter_wheel:
            print("No filter wheel")
            return

        if position >= self.filter_count:
            print(f"Invalid position {position}")
            return

        try:
            print(f"Moving to filter {position}...")
            self.filter_wheel.set_filter_pos(position)
            time.sleep(2.0)
            self.current_filter_pos = self.filter_wheel.get_filter_pos()
            print(f"Filter: {self.current_filter_pos}")

            # Trigger recalibration after filter change
            if self.recalibrate_callback:
                self._recalibrate_requested = True
        except Exception as e:
            print(f"Filter error: {e}")

    def _save_frame(self):
        """Save current frame."""
        if self.current_image is None:
            print("No image")
            return

        try:
            os.makedirs("focus_frames", exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"focus_frames/frame_{timestamp}_{self.exposure_ms}ms.tiff"

            tiff = Image.fromarray(self.current_image.astype(np.uint16), mode='I;16')
            tiff.save(filename, format='TIFF')
            print(f"Saved: {filename}")
        except Exception as e:
            print(f"Save error: {e}")

    def stop(self):
        """Stop live view."""
        self.running = False

        if self.capture_thread and self.capture_thread.is_alive():
            self.capture_thread.join(timeout=2.0)

        if self.camera and self.video_mode_active:
            try:
                self.camera.stop_video_mode()
            except:
                pass
            self.video_mode_active = False

        cv2.destroyAllWindows()

    def cleanup(self):
        """Clean up devices."""
        if self.camera:
            try:
                self.lib.FLIClose(self.camera._dev)
            except:
                pass
            self.camera = None

        if self.filter_wheel:
            try:
                self.lib.FLIClose(self.filter_wheel._dev)
            except:
                pass
            self.filter_wheel = None


def main():
    """Main entry point for testing."""
    import argparse

    parser = argparse.ArgumentParser(
        description="FLI Camera Live Video",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Controls:
  q, ESC  : Quit
  a       : Recalibrate (requires callback)
  s       : Save frame
  0-9     : Filter position
        """
    )

    parser.add_argument('--no-video', action='store_true',
                        help='Use still capture mode')
    parser.add_argument('--exposure', type=int, default=100,
                        help='Exposure time in ms (default: 100)')
    parser.add_argument('--binning', type=int, default=2,
                        help='Binning factor (default: 2)')

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s - %(levelname)s - %(message)s')

    print("FLI Camera Video Capture")
    print("=" * 50)
    print(f"Exposure: {args.exposure}ms")
    print(f"Mode: {'Still' if args.no_video else 'Video'}")
    print()

    try:
        with VideoCapture(
            exposure_ms=args.exposure,
            use_video_mode=not args.no_video,
            binning=(args.binning, args.binning)
        ) as vc:
            vc.start_live_view()
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
