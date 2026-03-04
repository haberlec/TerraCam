#!/usr/bin/env python3
"""
Grid Survey Script

Executes a rectangular grid scan across a pan/tilt range, capturing
multispectral images at each position using the FLI camera and filter
wheel mounted on the FLIR PTU D100E gimbal.

Two modes of operation:

  FOV-aware mode (recommended):
    Specify the lens, desired angular coverage, and overlap fraction.
    The grid positions are computed automatically from the camera FOV.

    python run_grid_survey.py --port /dev/ttyUSB0 \\
        --lens 28mm --pan-extent 60 --tilt-extent 30 \\
        --overlap 0.20 --filters 0 1 2 3 --exposure 200

  Manual mode:
    Specify explicit pan/tilt ranges and step counts.

    python run_grid_survey.py --port /dev/ttyUSB0 \\
        --pan-range -10 10 --tilt-range -5 5 \\
        --pan-steps 5 --tilt-steps 3 \\
        --filters 0 1 2 3 --exposure 200
"""

import argparse
import sys
import json
from pathlib import Path

from fli import FLISystem
from ptu import PTUConfig, PowerMode
from ptu.logger import SessionLogger
from scripts.mission.coordinator import PayloadCoordinator


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Execute a grid survey with PTU and FLI camera",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "FOV-aware mode (requires --lens, --pan-extent, --tilt-extent):\n"
            "  Computes grid spacing from lens FOV and overlap fraction.\n\n"
            "Manual mode (requires --pan-range, --tilt-range, "
            "--pan-steps, --tilt-steps):\n"
            "  Uses explicit position ranges and step counts.\n"
        )
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

    # --- FOV-aware mode arguments ---
    fov_group = parser.add_argument_group(
        "FOV-aware mode",
        "Compute grid from lens FOV and desired coverage"
    )
    fov_group.add_argument(
        "--lens", type=str, choices=["28mm", "50mm"], default=None,
        help="Lens identifier (enables FOV-aware mode)"
    )
    fov_group.add_argument(
        "--pan-extent", type=float, default=None,
        help="Total pan coverage in degrees"
    )
    fov_group.add_argument(
        "--tilt-extent", type=float, default=None,
        help="Total tilt coverage in degrees"
    )
    fov_group.add_argument(
        "--pan-center", type=float, default=0.0,
        help="Center pan angle in degrees (default: 0.0)"
    )
    fov_group.add_argument(
        "--tilt-center", type=float, default=0.0,
        help="Center tilt angle in degrees (default: 0.0)"
    )
    fov_group.add_argument(
        "--overlap", type=float, default=0.20,
        help="Fractional overlap between adjacent frames (default: 0.20)"
    )

    # --- Manual mode arguments ---
    manual_group = parser.add_argument_group(
        "Manual mode",
        "Specify explicit grid positions"
    )
    manual_group.add_argument(
        "--pan-range", type=float, nargs=2, default=None,
        metavar=("MIN", "MAX"),
        help="Pan angle range in degrees (min max)"
    )
    manual_group.add_argument(
        "--tilt-range", type=float, nargs=2, default=None,
        metavar=("MIN", "MAX"),
        help="Tilt angle range in degrees (min max)"
    )
    manual_group.add_argument(
        "--pan-steps", type=int, default=None,
        help="Number of pan positions in grid"
    )
    manual_group.add_argument(
        "--tilt-steps", type=int, default=None,
        help="Number of tilt positions in grid"
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
        "--auto-expose", action="store_true",
        help="Auto-compute exposure at the center grid position for each "
             "filter, then use those exposures for all positions"
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
    parser.add_argument(
        "--inter-position-delay", type=float, default=0.0,
        help="Extra delay between positions in seconds (default: 0.0)"
    )

    # Output
    parser.add_argument(
        "--output", type=str, default="./out",
        help="Output directory (default: ./out)"
    )
    parser.add_argument(
        "--name", type=str, default="grid_survey",
        help="Sequence name (default: grid_survey)"
    )

    # Behavior
    parser.add_argument(
        "--no-return", action="store_true",
        help="Do not return to start position after sequence"
    )
    parser.add_argument(
        "--stop-on-error", action="store_true",
        help="Stop sequence on first error"
    )

    args = parser.parse_args()

    # Validate mode selection
    fov_mode = args.lens is not None
    manual_mode = args.pan_range is not None

    if fov_mode and manual_mode:
        parser.error(
            "Cannot use both FOV-aware mode (--lens) and manual mode "
            "(--pan-range) simultaneously"
        )

    if not fov_mode and not manual_mode:
        parser.error(
            "Must specify either FOV-aware mode (--lens --pan-extent "
            "--tilt-extent) or manual mode (--pan-range --tilt-range "
            "--pan-steps --tilt-steps)"
        )

    if fov_mode:
        if args.pan_extent is None or args.tilt_extent is None:
            parser.error(
                "FOV-aware mode requires --pan-extent and --tilt-extent"
            )
        if args.overlap < 0.0 or args.overlap >= 1.0:
            parser.error("--overlap must be >= 0.0 and < 1.0")

    if manual_mode:
        if (args.tilt_range is None or args.pan_steps is None or
                args.tilt_steps is None):
            parser.error(
                "Manual mode requires --pan-range, --tilt-range, "
                "--pan-steps, and --tilt-steps"
            )

    return args


def main():
    """Run grid survey."""
    args = parse_args()

    fov_mode = args.lens is not None

    # Create sequence based on mode
    if fov_mode:
        sequence, geometry = PayloadCoordinator.create_fov_grid_sequence(
            sequence_name=args.name,
            lens=args.lens,
            pan_center=args.pan_center,
            tilt_center=args.tilt_center,
            total_pan_deg=args.pan_extent,
            total_tilt_deg=args.tilt_extent,
            overlap=args.overlap,
            filter_positions=args.filters,
            exposure_ms=args.exposure,
            settle_time_s=args.settle_time,
        )

        total_positions = geometry["total_positions"]
        filters_per_pos = len(args.filters) if args.filters else 1
        total_captures = total_positions * filters_per_pos

        print(f"Grid Survey: {args.name} (FOV-aware mode)")
        print(f"  Lens: {geometry['lens_model']} ({args.lens})")
        print(f"  FOV: {geometry['fov_h_deg']} x {geometry['fov_v_deg']} deg")
        print(f"  Overlap: {geometry['overlap']:.0%}")
        print(f"  Step size: {geometry['step_pan_deg']} x "
              f"{geometry['step_tilt_deg']} deg")
        print(f"  Grid: {geometry['n_pan']} x {geometry['n_tilt']} = "
              f"{total_positions} positions")
        print(f"  Coverage: {geometry['actual_pan_coverage_deg']} x "
              f"{geometry['actual_tilt_coverage_deg']} deg "
              f"(requested: {geometry['requested_pan_deg']} x "
              f"{geometry['requested_tilt_deg']})")
        print(f"  Center: pan={geometry['center_pan_deg']}, "
              f"tilt={geometry['center_tilt_deg']}")
        pan_range = geometry['pan_range_deg']
        tilt_range = geometry['tilt_range_deg']
        print(f"  Pan range: {pan_range[0]} to {pan_range[1]} deg")
        print(f"  Tilt range: {tilt_range[0]} to {tilt_range[1]} deg")
        print(f"  Filters per position: {filters_per_pos}")
        print(f"  Total captures: {total_captures}")
        if args.auto_expose:
            print(f"  Exposure: AUTO (initial estimate {args.exposure} ms)")
        else:
            print(f"  Exposure: {args.exposure} ms")
        print(f"  Output: {args.output}")
        print()

    else:
        # Manual mode
        sequence = PayloadCoordinator.create_grid_sequence(
            sequence_name=args.name,
            pan_range=tuple(args.pan_range),
            tilt_range=tuple(args.tilt_range),
            pan_steps=args.pan_steps,
            tilt_steps=args.tilt_steps,
            filter_positions=args.filters,
            exposure_ms=args.exposure,
            settle_time_s=args.settle_time,
        )

        total_positions = args.pan_steps * args.tilt_steps
        filters_per_pos = len(args.filters) if args.filters else 1
        total_captures = total_positions * filters_per_pos

        print(f"Grid Survey: {args.name} (manual mode)")
        print(f"  Pan: {args.pan_range[0]} to {args.pan_range[1]} deg "
              f"({args.pan_steps} steps)")
        print(f"  Tilt: {args.tilt_range[0]} to {args.tilt_range[1]} deg "
              f"({args.tilt_steps} steps)")
        print(f"  Positions: {total_positions}")
        print(f"  Filters per position: {filters_per_pos}")
        print(f"  Total captures: {total_captures}")
        if args.auto_expose:
            print(f"  Exposure: AUTO (initial estimate {args.exposure} ms)")
        else:
            print(f"  Exposure: {args.exposure} ms")
        print(f"  Output: {args.output}")
        print()

    # Apply common sequence options
    sequence.return_to_start = not args.no_return
    sequence.continue_on_error = not args.stop_on_error
    sequence.inter_position_delay_s = args.inter_position_delay
    sequence.auto_expose_center = args.auto_expose

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

        # Execute sequence
        print(f"Starting grid survey with {total_positions} positions...")
        summary = coordinator.execute_sequence(sequence)

        # Print summary
        print()
        print("=" * 50)
        print("GRID SURVEY COMPLETE")
        print("=" * 50)
        print(f"Status: {summary['status']}")
        print(f"Positions: {summary['successful_positions']}/"
              f"{summary['total_positions']} successful")
        print(f"Success rate: {summary['success_rate']:.1%}")
        print(f"Total time: {summary['total_duration_s']:.1f}s")
        print(f"Avg time/position: "
              f"{summary['average_time_per_position_s']:.1f}s")

        # Save summary (include geometry info if FOV mode)
        if fov_mode:
            summary["grid_geometry"] = geometry
        if sequence.per_filter_exposure_ms:
            summary["auto_exposure"] = {
                str(k): v for k, v in
                sequence.per_filter_exposure_ms.items()
            }
        summary_path = Path(args.output) / f"{args.name}_summary.json"
        with open(summary_path, 'w') as f:
            json.dump(summary, f, indent=2)
        print(f"Summary saved: {summary_path}")

    except KeyboardInterrupt:
        print("\nSurvey interrupted by user")
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
