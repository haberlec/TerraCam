#!/usr/bin/env python3
"""
Celestial Tracking Script

Points the PTU at a celestial body (or RA/Dec coordinate) and captures
multispectral imagery. Supports single-shot and continuous tracking modes.

Single-shot examples:
    python run_celestial_track.py --target MOON
    python run_celestial_track.py --target MOON --filters 0 1 2 3
    python run_celestial_track.py --ra 83.63 --dec 22.01 --name "M42"

Continuous tracking examples:
    python run_celestial_track.py --target MOON --duration 3600 --interval 60
    python run_celestial_track.py --target SUN --duration 7200 --interval 120 \\
        --filters 0 1 2 --auto-expose

Manual observer location (no GPM):
    python run_celestial_track.py --target MOON \\
        --observer-lat 40.7128 --observer-lon -74.0060 --observer-alt 10
"""

import argparse
import json
import sys
import signal
from datetime import datetime
from pathlib import Path

from fli import FLISystem
from ptu import PTUConfig, PowerMode
from ptu.logger import SessionLogger
from scripts.mission.coordinator import PayloadCoordinator
from astro import (
    CelestialTracker,
    CelestialTarget,
    KernelManager,
    ObserverLocation,
)
from astro.tracker import TrackingConfig


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Track a celestial target with PTU and FLI camera",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Target specification:\n"
            "  --target NAME    SPICE body: MOON, SUN, MARS, JUPITER, etc.\n"
            "  --ra/--dec       Right Ascension / Declination in degrees\n\n"
            "Tracking modes:\n"
            "  Single-shot:     --target MOON (default)\n"
            "  Continuous:      --target MOON --duration 3600 --interval 60\n"
        )
    )

    # Target specification
    target_group = parser.add_argument_group("Target")
    target_group.add_argument(
        "--target", type=str, default=None,
        help="SPICE body name (e.g., MOON, SUN, MARS, JUPITER, SATURN)"
    )
    target_group.add_argument(
        "--ra", type=float, default=None,
        help="Right Ascension in degrees (0-360)"
    )
    target_group.add_argument(
        "--dec", type=float, default=None,
        help="Declination in degrees (-90 to +90)"
    )
    target_group.add_argument(
        "--name", type=str, default=None,
        help="Target name for RA/Dec targets (default: auto-generated)"
    )

    # Tracking mode
    tracking_group = parser.add_argument_group("Tracking Mode")
    tracking_group.add_argument(
        "--duration", type=float, default=None,
        help="Continuous tracking duration in seconds (omit for single-shot)"
    )
    tracking_group.add_argument(
        "--interval", type=float, default=None,
        help="Repointing interval in seconds (default: target-dependent)"
    )
    tracking_group.add_argument(
        "--min-elevation", type=float, default=5.0,
        help="Minimum elevation in degrees (default: 5.0)"
    )
    tracking_group.add_argument(
        "--no-refraction", action="store_true",
        help="Disable atmospheric refraction correction"
    )

    # PTU configuration
    ptu_group = parser.add_argument_group("PTU")
    ptu_group.add_argument(
        "--port", type=str, default="auto",
        help="Serial port for PTU (default: auto-discover)"
    )
    ptu_group.add_argument(
        "--baudrate", type=int, default=9600,
        help="PTU serial baud rate (default: 9600)"
    )
    ptu_group.add_argument(
        "--settle-time", type=float, default=3.0,
        help="Settle time after PTU movement in seconds (default: 3.0)"
    )

    # Camera settings
    camera_group = parser.add_argument_group("Camera")
    camera_group.add_argument(
        "--filters", type=int, nargs="+", default=None,
        help="Filter positions to capture (default: current position only)"
    )
    camera_group.add_argument(
        "--exposure", type=int, default=100,
        help="Exposure time in ms (default: 100)"
    )
    camera_group.add_argument(
        "--auto-expose", action="store_true",
        help="Auto-compute exposure at first pointing"
    )
    camera_group.add_argument(
        "--target-temp", type=float, default=-20.0,
        help="CCD target temperature in C (default: -20)"
    )

    # Observer location override
    observer_group = parser.add_argument_group(
        "Observer Location (override GPM)"
    )
    observer_group.add_argument(
        "--observer-lat", type=float, default=None,
        help="Observer latitude in degrees"
    )
    observer_group.add_argument(
        "--observer-lon", type=float, default=None,
        help="Observer longitude in degrees"
    )
    observer_group.add_argument(
        "--observer-alt", type=float, default=None,
        help="Observer altitude in meters"
    )

    # SPICE
    parser.add_argument(
        "--metakernel", type=str, default=None,
        help="Path to SPICE metakernel (default: data/spice/terracam.tm)"
    )

    # Output
    parser.add_argument(
        "--output", type=str, default="./out",
        help="Output directory (default: ./out)"
    )
    parser.add_argument(
        "--session-name", type=str, default=None,
        help="Session name (default: auto-generated)"
    )

    args = parser.parse_args()

    # Validate target specification
    has_body = args.target is not None
    has_radec = args.ra is not None or args.dec is not None

    if not has_body and not has_radec:
        parser.error("Must specify --target or both --ra and --dec")

    if has_body and has_radec:
        parser.error("Cannot specify both --target and --ra/--dec")

    if has_radec and (args.ra is None or args.dec is None):
        parser.error("Both --ra and --dec are required for RA/Dec targets")

    # Validate observer override (all or none)
    obs_parts = [args.observer_lat, args.observer_lon, args.observer_alt]
    if any(p is not None for p in obs_parts):
        if not all(p is not None for p in obs_parts):
            parser.error(
                "All of --observer-lat, --observer-lon, --observer-alt "
                "are required when overriding observer location"
            )

    return args


def main():
    """Run celestial tracking."""
    args = parse_args()

    # Build target
    if args.target:
        target = CelestialTarget.from_spice_body(args.target)
    else:
        target = CelestialTarget.from_ra_dec(
            ra_deg=args.ra, dec_deg=args.dec, name=args.name
        )

    # Build observer override
    observer_override = None
    if args.observer_lat is not None:
        observer_override = ObserverLocation(
            latitude_deg=args.observer_lat,
            longitude_deg=args.observer_lon,
            altitude_m=args.observer_alt,
        )

    # Build tracking config
    session_name = args.session_name or (
        f"celestial_{target.name}_"
        f"{datetime.now().strftime('%Y%m%dT%H%M%S')}"
    )

    config = TrackingConfig(
        target=target,
        duration_s=args.duration,
        interval_s=args.interval,
        filter_positions=args.filters,
        exposure_ms=args.exposure,
        auto_expose=args.auto_expose,
        settle_time_s=args.settle_time,
        min_elevation_deg=args.min_elevation,
        apply_refraction=not args.no_refraction,
        output_dir=args.output,
        session_name=session_name,
        observer_override=observer_override,
    )

    # Print configuration
    mode = "Continuous" if config.is_continuous else "Single-shot"
    print(f"Celestial Tracking: {target.name} ({mode})")
    print(f"  Target type: {target.target_type.value}")
    if target.spice_name:
        print(f"  SPICE body: {target.spice_name}")
    if target.ra_deg is not None:
        print(f"  RA: {target.ra_deg:.4f} deg, "
              f"Dec: {target.dec_deg:.4f} deg")
    if config.is_continuous:
        print(f"  Duration: {config.duration_s}s")
        print(f"  Interval: {config.effective_interval_s}s")
    print(f"  Min elevation: {config.min_elevation_deg} deg")
    print(f"  Refraction correction: "
          f"{'ON' if config.apply_refraction else 'OFF'}")
    if args.filters:
        print(f"  Filters: {args.filters}")
    if args.auto_expose:
        print(f"  Exposure: AUTO (initial {args.exposure} ms)")
    else:
        print(f"  Exposure: {args.exposure} ms")
    print(f"  Output: {args.output}")
    if observer_override:
        print(
            f"  Observer: {observer_override.latitude_deg:.6f}, "
            f"{observer_override.longitude_deg:.6f}, "
            f"{observer_override.altitude_m:.1f}m (manual)"
        )
    else:
        print("  Observer: from GPM GPS")
    print()

    # Setup logging
    session_logger = SessionLogger(
        log_dir=str(Path(args.output) / "logs"),
        session_name=session_name,
    )

    fli_system = None
    coordinator = None
    tracker = None

    # Signal handler for graceful abort
    def signal_handler(signum, frame):
        print("\nAbort requested (finishing current cycle)...")
        if tracker:
            tracker.abort()

    signal.signal(signal.SIGINT, signal_handler)

    try:
        # Initialize FLI camera system
        print("Initializing FLI camera system...")
        fli_system = FLISystem()
        fli_system.discover_devices()
        fli_system.initialize(target_temp=args.target_temp)

        # Configure PTU
        ptu_config = PTUConfig(
            port=args.port,
            baudrate=args.baudrate,
            pan_speed=1000,
            tilt_speed=1000,
            hold_power_mode=PowerMode.REGULAR,
            move_power_mode=PowerMode.HIGH,
        )

        # Create coordinator
        coordinator = PayloadCoordinator(
            ptu_config=ptu_config,
            fli_system=fli_system,
            output_dir=args.output,
            session_logger=session_logger,
        )

        # Initialize PTU
        print("Initializing PTU...")
        if not coordinator.initialize():
            print("ERROR: PTU initialization failed")
            sys.exit(1)

        # Create tracker
        kernel_manager = KernelManager(
            metakernel_path=args.metakernel
        )
        tracker = CelestialTracker(
            coordinator=coordinator,
            kernel_manager=kernel_manager,
            logger=session_logger.logger,
        )

        # Load SPICE kernels
        print("Loading SPICE kernels...")
        tracker.initialize()

        # Progress callback for continuous mode
        def on_progress(index, point):
            status = "OK" if point.success else "FAIL"
            print(
                f"  Point {index}: {status} "
                f"az={point.az_el.azimuth_deg:.2f} "
                f"el={point.az_el.elevation_deg:.2f} "
                f"captures={len(point.captures)}"
            )

        # Execute tracking
        print(f"Starting {mode.lower()} tracking...")
        result = tracker.track(config, progress_callback=on_progress)

        # Print summary
        print()
        print("=" * 50)
        print("TRACKING COMPLETE")
        print("=" * 50)
        print(f"Target: {target.name}")
        print(f"Mode: {mode}")
        print(
            f"Points: {result.successful_points}/"
            f"{len(result.points)}"
        )
        print(f"Total captures: {result.total_captures}")
        print(f"Duration: {result.total_duration_s:.1f}s")
        if result.target_below_horizon:
            print("NOTE: Target dropped below horizon during tracking")

        # Save summary
        summary_path = Path(args.output) / f"{session_name}_summary.json"
        with open(summary_path, 'w') as f:
            json.dump(result.to_dict(), f, indent=2, default=str)
        print(f"Summary saved: {summary_path}")

    except KeyboardInterrupt:
        print("\nTracking interrupted by user")
        if tracker:
            tracker.abort()

    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    finally:
        if tracker:
            tracker.shutdown()
        if coordinator:
            coordinator.shutdown()
        if fli_system:
            fli_system.close()


if __name__ == "__main__":
    main()
