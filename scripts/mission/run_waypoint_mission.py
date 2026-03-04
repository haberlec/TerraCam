#!/usr/bin/env python3
"""
Waypoint Mission Script

Executes an acquisition sequence at a list of predefined waypoint positions,
capturing multispectral images at each one.

Waypoints are defined in a JSON file:
    {
        "waypoints": [
            {"pan_deg": 0.0, "tilt_deg": 0.0},
            {"pan_deg": 10.0, "tilt_deg": -5.0},
            {"pan_deg": 20.0, "tilt_deg": 0.0}
        ]
    }

Usage:
    python run_waypoint_mission.py --port /dev/ttyUSB0 \\
        --waypoints waypoints.json \\
        --filters 0 1 2 --exposure 200 \\
        --output ./out/waypoint_mission
"""

import argparse
import json
import sys
from pathlib import Path

from fli import FLISystem
from ptu import PTUConfig, PowerMode
from ptu.logger import SessionLogger
from scripts.mission.coordinator import PayloadCoordinator


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Execute a waypoint mission with PTU and FLI camera"
    )

    # PTU configuration
    parser.add_argument(
        "--port", type=str, default="auto",
        help="Serial port for PTU (default: auto-discover)"
    )
    parser.add_argument(
        "--baudrate", type=int, default=9600,
        help="PTU serial baud rate (default: 9600)"
    )

    # Waypoints
    parser.add_argument(
        "--waypoints", type=str, required=True,
        help="Path to waypoints JSON file"
    )

    # Camera settings
    parser.add_argument(
        "--filters", type=int, nargs="+", default=None,
        help="Filter positions to capture (default: current position only)"
    )
    parser.add_argument(
        "--exposure", type=int, default=100,
        help="Exposure time in ms (default: 100)"
    )
    parser.add_argument(
        "--target-temp", type=float, default=-20.0,
        help="CCD target temperature in C (default: -20)"
    )

    # Timing
    parser.add_argument(
        "--settle-time", type=float, default=2.0,
        help="Settle time after PTU movement in seconds (default: 2.0)"
    )

    # Output
    parser.add_argument(
        "--output", type=str, default="./out",
        help="Output directory (default: ./out)"
    )
    parser.add_argument(
        "--name", type=str, default="waypoint_mission",
        help="Mission name (default: waypoint_mission)"
    )

    # Behavior
    parser.add_argument(
        "--no-return", action="store_true",
        help="Do not return to start position after mission"
    )
    parser.add_argument(
        "--stop-on-error", action="store_true",
        help="Stop mission on first error"
    )

    return parser.parse_args()


def load_waypoints(filepath: str) -> list:
    """Load waypoints from JSON file.

    Parameters
    ----------
    filepath : str
        Path to JSON waypoints file.

    Returns
    -------
    list of (float, float)
        List of (pan_degrees, tilt_degrees) tuples.
    """
    with open(filepath) as f:
        data = json.load(f)

    waypoints = []
    for wp in data["waypoints"]:
        pan = wp["pan_deg"]
        tilt = wp["tilt_deg"]
        waypoints.append((pan, tilt))

    return waypoints


def main():
    """Run waypoint mission."""
    args = parse_args()

    # Load waypoints
    waypoints_path = Path(args.waypoints)
    if not waypoints_path.exists():
        print(f"ERROR: Waypoints file not found: {args.waypoints}")
        sys.exit(1)

    waypoints = load_waypoints(args.waypoints)
    filters_per_pos = len(args.filters) if args.filters else 1
    total_captures = len(waypoints) * filters_per_pos

    print(f"Waypoint Mission: {args.name}")
    print(f"  Waypoints: {len(waypoints)}")
    print(f"  Filters per position: {filters_per_pos}")
    print(f"  Total captures: {total_captures}")
    print(f"  Exposure: {args.exposure} ms")
    print(f"  Output: {args.output}")
    print()

    for i, (pan, tilt) in enumerate(waypoints):
        print(f"  WP{i:03d}: pan={pan:.1f} tilt={tilt:.1f}")
    print()

    # Setup logging
    session_logger = SessionLogger(
        log_dir=str(Path(args.output) / "logs"),
        session_name=args.name,
    )

    fli_system = None
    coordinator = None

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

        # Create waypoint sequence
        sequence = PayloadCoordinator.create_waypoint_sequence(
            sequence_name=args.name,
            waypoints=waypoints,
            filter_positions=args.filters,
            exposure_ms=args.exposure,
            settle_time_s=args.settle_time,
        )
        sequence.return_to_start = not args.no_return
        sequence.continue_on_error = not args.stop_on_error

        # Execute sequence
        print(f"Starting waypoint mission with {len(waypoints)} positions...")
        summary = coordinator.execute_sequence(sequence)

        # Print summary
        print()
        print("=" * 50)
        print("WAYPOINT MISSION COMPLETE")
        print("=" * 50)
        print(f"Status: {summary['status']}")
        print(f"Positions: {summary['successful_positions']}/"
              f"{summary['total_positions']} successful")
        print(f"Success rate: {summary['success_rate']:.1%}")
        print(f"Total time: {summary['total_duration_s']:.1f}s")
        print(f"Avg time/position: "
              f"{summary['average_time_per_position_s']:.1f}s")

        # Save summary
        summary_path = Path(args.output) / f"{args.name}_summary.json"
        with open(summary_path, 'w') as f:
            json.dump(summary, f, indent=2)
        print(f"Summary saved: {summary_path}")

    except KeyboardInterrupt:
        print("\nMission interrupted by user")
        if coordinator:
            coordinator.abort_sequence()

    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    finally:
        if coordinator:
            coordinator.shutdown()
        if fli_system:
            fli_system.close()


if __name__ == "__main__":
    main()
