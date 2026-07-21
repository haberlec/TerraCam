#!/usr/bin/env python3
"""
Interactive PTU Aiming + Live View

Streams live camera frames while driving the PTU from the keyboard in
small angular steps, for planning grid surveys in the field: point at the
scene corners, mark them, and the tool prints the ``run_grid_survey``
parameters (center + extent) that cover what you framed.

This is a planning/aiming aid, not an acquisition tool — it captures
preview frames only and writes no science data.

Usage (run as a module from the repo root so imports resolve):

    python -m scripts.mission.aim_ptu

    # explicit port, starting filter and exposure:
    python -m scripts.mission.aim_ptu --lens 28mm \\
        --port COM3 --filter 6 --exposure 200

Controls (focus the OpenCV window):

    Arrow keys / W A S D : nudge PTU (up/down = tilt, left/right = pan)
    [ / ]                : decrease / increase step size
    - / =                : decrease / increase exposure
    0-9                  : filter position (0=clear)
    m                    : mark current pan/tilt as a survey corner
    c                    : clear marked corners
    p                    : print survey parameters from marked corners
    h                    : re-home to pan=0, tilt=0
    q / ESC              : quit
"""

import argparse
import logging
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

try:
    import cv2
except ImportError:
    print("ERROR: OpenCV required. Install with: pip install -e '.[video]'")
    sys.exit(1)

from fli import FLISystem
from fli.config import load_config
from ptu import PTUController, PTUConfig, PTUError

logger = logging.getLogger("aim_ptu")


# --------------------------------------------------------------------------
# PTU angle helpers
# --------------------------------------------------------------------------

def deg_to_steps(pan_deg: float, tilt_deg: float,
                 ptu: PTUController) -> Tuple[int, int]:
    """Convert pan/tilt degrees to encoder steps using PTU resolution."""
    pan_steps = int(round(pan_deg * 3600.0 / ptu.pan_resolution))
    tilt_steps = int(round(tilt_deg * 3600.0 / ptu.tilt_resolution))
    return pan_steps, tilt_steps


def steps_to_deg(pan_steps: int, tilt_steps: int,
                 ptu: PTUController) -> Tuple[float, float]:
    """Convert encoder steps to pan/tilt degrees."""
    return (pan_steps * ptu.pan_resolution / 3600.0,
            tilt_steps * ptu.tilt_resolution / 3600.0)


# --------------------------------------------------------------------------
# Aiming tool
# --------------------------------------------------------------------------

class PTUAimer:
    """Live-view + keyboard PTU control for survey planning.

    Parameters
    ----------
    fli : FLISystem
        Initialized FLI system (devices discovered).
    ptu : PTUController
        Connected and initialized PTU controller.
    lens_id : str
        Lens key for FOV overlay (e.g. "50mm").
    exposure_ms : int
        Initial preview exposure time.
    step_deg : float
        Initial PTU nudge step in degrees.
    pan_limit_deg, tilt_limit_deg : float
        Soft limits; nudges beyond these are refused.
    """

    WINDOW = "PTU Aiming — arrows/WASD move, m mark, p print, q quit"

    def __init__(self, fli: FLISystem, ptu: PTUController, lens_id: str = "28mm",
                 exposure_ms: int = 100, step_deg: float = 2.5,
                 pan_limit_deg: float = 90.0, tilt_limit_deg: float = 20.0):
        self.fli = fli
        self.ptu = ptu
        self.lens_id = lens_id
        self.exposure_ms = exposure_ms
        self.step_deg = step_deg
        self.pan_limit = pan_limit_deg
        self.tilt_limit = tilt_limit_deg

        self.fov_h, self.fov_v = self._compute_fov(lens_id)

        self.current_filter = 0
        self.pan_deg = 0.0
        self.tilt_deg = 0.0
        self.marks: List[Tuple[float, float]] = []

        self._frame: Optional[np.ndarray] = None
        self._frame_lock = threading.Lock()
        self._running = False
        self._capture_thread: Optional[threading.Thread] = None

    @staticmethod
    def _compute_fov(lens_id: str) -> Tuple[float, float]:
        """Horizontal/vertical FOV in degrees from lens + sensor config."""
        import math
        lens_cfg = load_config("lens_specifications.json")
        if not lens_cfg or lens_id not in lens_cfg.get("lenses", {}):
            logger.warning("Lens '%s' not in config; FOV overlay disabled",
                           lens_id)
            return (0.0, 0.0)
        sensor = lens_cfg["sensor"]
        f = lens_cfg["lenses"][lens_id].get(
            "f_eff_mm", lens_cfg["lenses"][lens_id]["focal_length_mm"]
        )
        fov_h = 2 * math.degrees(math.atan(sensor["width_mm"] / (2 * f)))
        fov_v = 2 * math.degrees(math.atan(sensor["height_mm"] / (2 * f)))
        return (fov_h, fov_v)

    # -- capture thread --------------------------------------------------

    def _capture_loop(self):
        """Continuously grab preview frames into the shared buffer."""
        while self._running:
            try:
                img = self.fli.capture_image(exposure_ms=self.exposure_ms)
                with self._frame_lock:
                    self._frame = img
            except Exception as e:
                logger.warning("Preview capture failed: %s", e)
                time.sleep(0.5)

    # -- display ---------------------------------------------------------

    def _scale_for_display(self, image: np.ndarray) -> np.ndarray:
        """16-bit -> 8-bit percentile stretch, downscaled for the window."""
        lo, hi = np.percentile(image, [1, 99])
        if hi <= lo:
            hi = lo + 1
        scaled = np.clip((image.astype(np.float32) - lo) / (hi - lo), 0, 1)
        disp = (scaled * 255).astype(np.uint8)
        h, w = disp.shape
        target_w = 1024
        if w > target_w:
            scale = target_w / w
            disp = cv2.resize(disp, (target_w, int(h * scale)))
        return cv2.cvtColor(disp, cv2.COLOR_GRAY2BGR)

    def _draw_overlay(self, disp: np.ndarray) -> np.ndarray:
        """Draw pointing, step, exposure, filter, marks, and a crosshair."""
        h, w = disp.shape[:2]
        # Center crosshair
        cv2.drawMarker(disp, (w // 2, h // 2), (0, 255, 0),
                       cv2.MARKER_CROSS, 30, 1)

        lines = [
            f"pan={self.pan_deg:+.1f}  tilt={self.tilt_deg:+.1f} deg",
            f"step={self.step_deg:.1f} deg   exp={self.exposure_ms} ms",
            f"filter={self.current_filter}   FOV={self.fov_h:.1f}x{self.fov_v:.1f}",
            f"marks={len(self.marks)}  (m=mark p=print c=clear)",
        ]
        y = 22
        for ln in lines:
            cv2.putText(disp, ln, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                        (0, 0, 0), 3, cv2.LINE_AA)
            cv2.putText(disp, ln, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                        (0, 255, 255), 1, cv2.LINE_AA)
            y += 24
        return disp

    # -- PTU movement ----------------------------------------------------

    def _nudge(self, d_pan: float, d_tilt: float):
        """Move the PTU by a relative step, respecting soft limits."""
        new_pan = self.pan_deg + d_pan
        new_tilt = self.tilt_deg + d_tilt
        if abs(new_pan) > self.pan_limit or abs(new_tilt) > self.tilt_limit:
            print(f"  refused: ({new_pan:+.1f}, {new_tilt:+.1f}) exceeds "
                  f"limits (pan +/-{self.pan_limit}, tilt +/-{self.tilt_limit})")
            return
        try:
            pan_steps, tilt_steps = deg_to_steps(new_pan, new_tilt, self.ptu)
            if self.ptu.move_to_position(pan_steps, tilt_steps, wait=True):
                actual_pan, actual_tilt = self.ptu.get_position()
                self.pan_deg, self.tilt_deg = steps_to_deg(
                    actual_pan, actual_tilt, self.ptu)
            else:
                print("  move did not complete")
        except PTUError as e:
            print(f"  PTU error: {e}")

    def _home(self):
        """Return to pan=0, tilt=0."""
        try:
            if self.ptu.move_to_position(0, 0, wait=True):
                self.pan_deg, self.tilt_deg = 0.0, 0.0
                print("  homed to (0, 0)")
        except PTUError as e:
            print(f"  PTU error: {e}")

    def _move_filter(self, pos: int):
        try:
            self.fli.move_filter(pos)
            self.current_filter = self.fli.get_filter_position()
            print(f"  filter -> {self.current_filter}")
        except Exception as e:
            print(f"  filter error: {e}")

    # -- survey planning -------------------------------------------------

    def _print_survey(self):
        """Compute and print run_grid_survey parameters from marks."""
        if len(self.marks) < 2:
            print("  need at least 2 marks (opposite corners of the scene)")
            return
        pans = [m[0] for m in self.marks]
        tilts = [m[1] for m in self.marks]
        pan_min, pan_max = min(pans), max(pans)
        tilt_min, tilt_max = min(tilts), max(tilts)
        pan_center = (pan_min + pan_max) / 2
        tilt_center = (tilt_min + tilt_max) / 2
        pan_extent = pan_max - pan_min
        tilt_extent = tilt_max - tilt_min

        print("\n" + "=" * 60)
        print("SURVEY PARAMETERS from marked corners")
        print("=" * 60)
        print(f"  marked corners: {[(round(p,1), round(t,1)) for p,t in self.marks]}")
        print(f"  pan:  {pan_min:+.1f} to {pan_max:+.1f}  "
              f"(center {pan_center:+.1f}, extent {pan_extent:.1f} deg)")
        print(f"  tilt: {tilt_min:+.1f} to {tilt_max:+.1f}  "
              f"(center {tilt_center:+.1f}, extent {tilt_extent:.1f} deg)")
        print("\n  FOV-aware grid survey command:\n")
        print(f"  python -m scripts.mission.run_grid_survey \\")
        print(f"      --lens {self.lens_id} \\")
        print(f"      --pan-center {pan_center:.1f} --tilt-center {tilt_center:.1f} \\")
        print(f"      --pan-extent {max(pan_extent, 0.1):.1f} "
              f"--tilt-extent {max(tilt_extent, 0.0):.1f} \\")
        print(f"      --filters 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 \\")
        print(f"      --auto-expose --overlap 0.20 \\")
        print(f"      --scene-range-m <meters> \\")
        print(f"      --output ./out --name <survey_name>")
        print("=" * 60 + "\n")

    # -- main loop -------------------------------------------------------

    def run(self):
        """Open the window and run the interactive loop."""
        self._running = True
        self._capture_thread = threading.Thread(
            target=self._capture_loop, daemon=True)
        self._capture_thread.start()

        cv2.namedWindow(self.WINDOW, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(self.WINDOW, 1024, 820)
        self._print_controls()

        try:
            while self._running:
                with self._frame_lock:
                    frame = None if self._frame is None else self._frame.copy()

                if frame is not None:
                    disp = self._draw_overlay(self._scale_for_display(frame))
                    cv2.imshow(self.WINDOW, disp)

                key = cv2.waitKey(30) & 0xFF
                if key == 255:
                    if cv2.getWindowProperty(
                            self.WINDOW, cv2.WND_PROP_VISIBLE) < 1:
                        break
                    continue
                if not self._handle_key(key):
                    break
        finally:
            self._running = False
            time.sleep(0.3)
            cv2.destroyAllWindows()

    def _handle_key(self, key: int) -> bool:
        """Dispatch a keypress. Returns False to quit."""
        s = self.step_deg
        # Arrow keys (OpenCV codes vary by platform; accept common ones)
        if key in (ord('w'), 82, 0):          # up -> tilt +
            self._nudge(0, +s)
        elif key in (ord('s'), 84, 1):        # down -> tilt -
            self._nudge(0, -s)
        elif key in (ord('a'), 81, 2):        # left -> pan -
            self._nudge(-s, 0)
        elif key in (ord('d'), 83, 3):        # right -> pan +
            self._nudge(+s, 0)
        elif key == ord('['):
            self.step_deg = max(0.1, round(self.step_deg - 0.5, 1))
            print(f"  step = {self.step_deg} deg")
        elif key == ord(']'):
            self.step_deg = round(self.step_deg + 0.5, 1)
            print(f"  step = {self.step_deg} deg")
        elif key == ord('-'):
            self.exposure_ms = max(1, int(self.exposure_ms * 0.7))
            print(f"  exposure = {self.exposure_ms} ms")
        elif key in (ord('='), ord('+')):
            self.exposure_ms = int(self.exposure_ms * 1.4) + 1
            print(f"  exposure = {self.exposure_ms} ms")
        elif ord('0') <= key <= ord('9'):
            self._move_filter(key - ord('0'))
        elif key == ord('m'):
            self.marks.append((self.pan_deg, self.tilt_deg))
            print(f"  marked ({self.pan_deg:+.1f}, {self.tilt_deg:+.1f}) "
                  f"[{len(self.marks)} total]")
        elif key == ord('c'):
            self.marks.clear()
            print("  marks cleared")
        elif key == ord('p'):
            self._print_survey()
        elif key == ord('h'):
            self._home()
        elif key in (ord('q'), 27):
            return False
        return True

    def _print_controls(self):
        print("\n" + "=" * 60)
        print("PTU AIMING — controls (focus the video window):")
        print("  arrows / WASD : move PTU (up/down tilt, left/right pan)")
        print("  [ ]          : step size down / up")
        print("  - =          : exposure down / up")
        print("  0-9          : filter position")
        print("  m            : mark corner    c: clear    p: print survey")
        print("  h            : home (0,0)     q/ESC: quit")
        print("=" * 60 + "\n")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Interactive PTU aiming with live view for survey "
                    "planning.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--lens", default="28mm", choices=["28mm", "50mm"],
                   help="Lens id for FOV overlay (default: 28mm, the wide lens)")
    p.add_argument("--port", default="auto",
                   help="PTU serial port (default: auto-discover)")
    p.add_argument("--baudrate", type=int, default=9600)
    p.add_argument("--filter", type=int, default=0,
                   help="Starting filter position (default: 0 = clear)")
    p.add_argument("--exposure", type=int, default=100,
                   help="Initial preview exposure in ms (default: 100)")
    p.add_argument("--step", type=float, default=2.5,
                   help="Initial PTU step in degrees (default: 2.5)")
    p.add_argument("--pan-limit", type=float, default=90.0,
                   help="Soft pan limit +/- deg (default: 90)")
    p.add_argument("--tilt-limit", type=float, default=20.0,
                   help="Soft tilt limit +/- deg (default: 20)")
    return p.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(message)s")
    args = parse_args()

    fli = FLISystem()
    ptu = PTUController(PTUConfig(port=args.port, baudrate=args.baudrate))

    try:
        print("Initializing camera + filter wheel...")
        fli.discover_devices()
        print("Initializing PTU...")
        if not ptu.connect() or not ptu.initialize():
            print("ERROR: PTU init failed")
            return 1

        # Start from a known pointing
        ptu.move_to_position(0, 0, wait=True)
        if args.filter:
            fli.move_filter(args.filter)

        aimer = PTUAimer(
            fli, ptu, lens_id=args.lens,
            exposure_ms=args.exposure, step_deg=args.step,
            pan_limit_deg=args.pan_limit, tilt_limit_deg=args.tilt_limit,
        )
        aimer.current_filter = fli.get_filter_position()
        aimer.run()
        return 0

    except KeyboardInterrupt:
        print("\nInterrupted")
        return 0
    finally:
        try:
            ptu.halt()
            ptu.disconnect()
        except Exception:
            pass
        fli.close()


if __name__ == "__main__":
    sys.exit(main())
