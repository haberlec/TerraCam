# CLAUDE.md

## Philosophy

This document defines the working relationship between the developer and Claude. Think of the developer as the conductor of an orchestra — Claude operates autonomously on small, focused, well-defined tasks (instrument sections), while the developer directs the overall composition and flow.

**Cardinal Rule:** Never take shortcuts. When complexity increases, break the problem down methodically. Do not simplify implementations to manage token usage or reduce scope. Shortcuts create technical debt that compounds into larger problems. If a task is too large, propose how to decompose it — do not silently reduce fidelity.

## Session Workflow

Each working session follows this structure:

1. **Developer states goals** — The developer defines the objectives for the session
2. **Claude reviews and researches** — Claude examines the existing codebase, documentation, and performs any necessary research relevant to the stated goals
3. **Claude presents a plan** — Claude proposes an implementation approach to accomplish the goals
4. **Developer approves or iterates** — The developer approves the plan or requests revisions; implementation begins only after plan approval

This cycle may repeat within a session as work progresses through multiple focused tasks.

## Code Style and Practices

### Language and Dependencies

- **Primary language:** Python 3.x with NumPy and SciPy as foundational libraries
- **Acceptable dependencies:** scikit-learn, OpenCV, and other well-established scientific Python packages
- **Performance extensions:** When computationally justified, numba, multithreading, or C/C++/FORTRAN bindings are acceptable
- **Baseline assumption:** NumPy broadcasting and vectorization should be the default approach for array operations. Avoid Python loops over array elements unless necessary.

### Documentation

- **Docstrings:** NumPy style, comprehensive
- **Type hints:** Required for all function signatures
- **Inline comments:** Use sparingly to explain *intent*, not mechanics
- **Port projects:** When porting from another language, include inline comments with provenance: original module, function name, and line numbers

### Error Handling

- Use explicit exception handling with informative error messages
- Fail fast with clear diagnostics
- Error messages should help the developer locate and understand the problem

### Logging

- Use Python's `logging` module when appropriate
- Log messages must be focused and informative, not verbose
- No celebratory language or decorative emojis in logs
- Acceptable indicators: functional symbols (checkmark, x) for success/failure status

## Development Workflow

### Testing Framework

**Framework:** pytest

**Philosophy:** Validate implementation against developer expectations before codifying tests. The workflow is:
1. Implement the feature
2. Validate behavior interactively or against reference implementations
3. Write tests to codify the validated behavior

**Rationale:** In scientific software, correctness is validated against physical reality, reference implementations, or analytical solutions — not against assumptions made at development time. Writing tests after validation ensures the tests encode *verified* behavior rather than encoding potentially incorrect assumptions.

**Test Types:**

- **Unit tests:** Test individual functions and components in isolation
- **Validation tests:** End-to-end or subsection tests that execute the code on representative inputs, produce outputs, and compare against expected results

**Validation expectations** may be derived from:
- Scientifically derived models (theoretical calculations, analytical solutions)
- Gold test data (actual data with known-correct outputs from validated implementations)

### Git Operations

Claude may propose opportune times for commits and draft commit messages, but the developer controls all Git operations (add, commit, push, pull). Do not execute Git commands autonomously.

**Commit messages:** Keep them simple and informative. A brief narrative or a short list of key changes is fine.

### Proactive Review

- Flag potential issues proactively: edge cases, performance concerns, maintainability
- If an approach appears problematic, ask for clarification rather than silently proceeding
- Prioritize readability and maintainability over cleverness
- Avoid excessive abstraction; use clear, descriptive naming

### When Uncertain or Stuck

**Ask rather than guess.** If uncertain about requirements, intent, or implementation approach, ask for clarification rather than making assumptions that may waste time.

If genuinely stuck on methodology or algorithmic implementation, suggest additional research to refine the approach.

**Accessing scientific literature:** If Claude cannot access papers due to paywalls or access restrictions, inform the developer. Team members typically have university or NASA affiliations with journal access and can retrieve papers and place them in the project's `/docs/` directory.

### Refactoring

When refactoring existing code, prioritize preserving the correctness and validity of outputs. Behavior preservation is the primary constraint; structural improvements must not alter results.

**No dead code:** Do not preserve unused code paths for backward compatibility — this creates bloat and harms readability. If code is no longer needed, remove it. Version control preserves history if retrieval is ever necessary.

## What to Avoid

- **Shortcuts due to complexity or token limits** — decompose instead
- **Verbose or celebratory log messages**
- **Writing debug outputs to project root**
- **Autonomous Git operations**
- **Excessive abstraction or poor naming**
- **Python loops where NumPy broadcasting applies**
- **Preserving dead code for backward compatibility** — remove unused code; Git preserves history

---

# Project-Specific Configuration

## Project Description

TerraCam is a command & control system for a Finger Lakes Instrumentation (FLI) MicroLine MLx695 CCD camera with a 16-position VNIR bandpass filter wheel (400-1100nm), mounted on a FLIR PTU D100E biaxial gimbal with Geo Pointing Module (GPM). The codebase has four layers: a C library (`libfli` v1.2.104) providing low-level camera hardware access, a Python package (`fli`) providing high-level camera control, a Python package (`ptu`) providing gimbal serial control with auto-discovery and GPM geo-pointing, and an astrometry package (`astro`) providing celestial body tracking via NASA SPICE. Mission scripts in `scripts/mission/` orchestrate synchronized PTU movement and multispectral image acquisition for grid surveys, waypoint missions, and celestial tracking.

## Build System

### C Library (src/libfli/)
- **Build**: `cd src/libfli && make` - Builds `libfli.a` and `libfli.so`
- **Clean**: `make clean` - Removes object files and compiled libraries
- The Makefile auto-detects platform (`uname -s`) and configures flags accordingly
- macOS builds link against IOKit and CoreFoundation frameworks
- The C library has been modified from the FLI original with macOS-specific improvements (IOKit API updates, page-aligned USB buffers, chunked transfer support)

### Python Package
- **Install**: `pip install -e .` (editable install from project root)
- **Install with video**: `pip install -e ".[video]"` (adds OpenCV)
- **Test**: `pytest tests/unit/` (unit tests with mocks, no hardware needed)
- **Dependencies**: numpy, Pillow, pyserial (core); opencv-python (optional for video)

## Architecture

### Python Package (src/fli/)
- **core/lib.py**: ctypes bindings to the C library — all FLI API functions
- **core/device.py**: Base USB device class with discovery and connection management
- **core/camera.py**: Camera control (exposure, temperature, shutter, readout, video mode)
- **core/filter_wheel.py**: Filter wheel positioning and status monitoring
- **core/focuser.py**: Focuser stepper motor control
- **system.py**: High-level orchestration — device discovery, temperature control, capture sequences
- **acquisition.py**: Robust image capture with USB error recovery, retry logic, image validation

### PTU Package (src/ptu/)
- **controller.py**: FLIR PTU D100E serial controller — connect, initialize, move, halt, status; auto-discovers serial port when `PTUConfig(port="auto")`
- **discovery.py**: Serial port auto-discovery — probes ports with `VM` command, returns `PTUDeviceInfo`
- **gpm.py**: Geo Pointing Module — GPS position, mounting attitude (roll/pitch/yaw), geo-pointing at lat/lon coordinates, landmark management, calibration
- **logger.py**: Structured session logging with JSON export and OperationTimer context manager

### Astrometry Package (src/astro/)
- **ephemeris.py**: NASA SPICE wrapper via SpiceyPy — `KernelManager` for kernel lifecycle, `compute_azimuth_elevation()` for celestial body az/el from observer GPS, `az_el_to_ptu_angles()` with mounting attitude correction (supports level and tilted mounts via ZYX rotation matrix)
- **tracker.py**: `CelestialTracker` — single-shot and continuous tracking modes, integrates with PayloadCoordinator for captures; supports any SPICE body (Moon, Sun, planets) and arbitrary RA/Dec coordinates

### Capture Scripts (scripts/capture/)
- **capture_image.py**: Main capture workflow — device setup, auto-exposure, save TIFF+JPEG+metadata
- **capture_video.py**: Real-time video display via OpenCV with threading
- **live_focus.py**: Interactive focusing tool combining auto-exposure + live video
- **auto_expose.py**: Auto-exposure engine — binary search, quality scoring, scene classification
- **exposure_predictor.py**: Multi-filter exposure prediction from a single reference measurement

### Mission Scripts (scripts/mission/)
- **coordinator.py**: PayloadCoordinator — orchestrates PTU movement + multi-filter FLI camera acquisition at each position; supports auto-exposure at grid center, GPM metadata injection, and geo-pointing sequences
- **run_grid_survey.py**: CLI for rectangular grid scans (FOV-aware or manual mode, auto-exposure)
- **run_waypoint_mission.py**: CLI for waypoint-based missions (reads JSON waypoint files)
- **run_celestial_track.py**: CLI for celestial body tracking (single-shot or continuous, SPICE bodies or RA/Dec)

### C Library (src/libfli/)
- **libfli.c/libfli.h**: Main API and command dispatcher
- **libfli-camera*.c/.h**: CCD camera control (exposure, readout, temperature, shutter)
- **libfli-filter-focuser*.c/.h**: Filter wheel and focuser control
- **unix/osx/**: macOS USB implementation via IOKit (substantially modified from original)
- **unix/linux/**: Linux USB and parallel port implementations
- **windows/**: Windows implementations and Visual Studio project files

### Key API Patterns
- All C functions return `long` (0 = success, negative = error)
- Device handles use `flidev_t` (opaque long type)
- Domain = interface | device type (e.g., `FLIDOMAIN_USB | FLIDEVICE_CAMERA`)
- Python classes: `USBCamera`, `USBFilterWheel`, `USBFocuser` inherit from `USBDevice`
- High-level: `FLISystem` provides context-managed device lifecycle

### Import Conventions
```python
# High-level API
from fli import FLISystem, ImageAcquisition

# Direct hardware access
from fli.core.camera import USBCamera
from fli.core.filter_wheel import USBFilterWheel
from fli.core.lib import FLILibrary, FLIDOMAIN_USB, FLIDEVICE_CAMERA

# PTU control
from ptu import PTUController, PTUConfig, PowerMode
from ptu import discover_ptu, PTUDeviceInfo
from ptu.logger import SessionLogger, OperationTimer

# GPM geo-pointing
from ptu.gpm import GPMController, GPSPosition, MountingAttitude, GeoTarget

# Celestial tracking
from astro import CelestialTracker, CelestialTarget, KernelManager, ObserverLocation
from astro.ephemeris import compute_azimuth_elevation, az_el_to_ptu_angles
from astro.tracker import TrackingConfig, TrackingResult
```

### Testing
- **Unit tests** (`tests/unit/`): Use mocks, no hardware required
- **Integration tests** (`tests/integration/`): Require connected FLI hardware
- **Standalone test scripts** (`tests/*.py`): Hardware diagnostic tools for USB, timing, saturation
- Use `FLISetDebugLevel()` with `FLIDEBUG_INFO`, `FLIDEBUG_WARN`, `FLIDEBUG_FAIL`, `FLIDEBUG_IO`

### Configuration
- `config/camera_specifications.json`: MLx695 sensor specs and QE curves
- `config/filter_specifications.json`: 16 Edmund Optics bandpass filter transmission profiles
- `config/lens_specifications.json`: Sensor geometry and lens FOV data (28mm, 50mm Schneider EMERALD)
- `config/ptu_specifications.json`: FLIR PTU D100E defaults (serial, speeds, limits, GPM commands, discovery)
- `config/celestial_specifications.json`: Celestial tracking target presets, SPICE settings, refraction model

### SPICE Data
- `data/spice/terracam.tm`: Metakernel referencing bundled SPICE kernels
- `data/spice/`: LSK (naif0012.tls), SPK (de440s.bsp), PCK (pck00011.tpc), Earth BPC

## Key Concepts and Data Structures

<!-- Domain-specific terminology, core data structures, important algorithms -->

## External Dependencies

- **numpy**: Array operations and image data handling
- **Pillow**: Image I/O (TIFF, JPEG)
- **pyserial**: RS-232 serial communication with PTU D100E
- **spiceypy** (optional `[astro]`): NASA SPICE toolkit for celestial ephemeris and coordinate transforms
- **opencv-python** (optional `[video]`): Live video display and focus tools

## Reference Materials

- `docs/FLI_SDK_Documentation.pdf`: FLI SDK API reference
- `docs/MLx695.pdf`: MicroLine MLx695 camera datasheet
- `docs/ptu_command_index.md`: FLIR E Series PTU command protocol reference (includes GPM commands in Section 17)

## Known Constraints and Quirks

- macOS USB transfers require page-aligned buffers and chunked reads (see modified `unix/osx/libfli-usb-sys.c`)
- Camera firmware has 20-30ms shutter timing overhead; `camera.py` uses manual shutter pre-opening for accurate short exposures
- Zero-value frames are a known USB failure mode; `acquisition.py` includes retry logic and image validation to handle this
- Conservative readout timing: 350ms + 150ms safety buffer after exposure completion
- PTU D100E communicates at 9600 baud via RS-232; commands are space/newline delimited ASCII text
- PTU requires a 2-second pause after reset (`R` command) before accepting further commands
- PTU serial port auto-discovered by probing with `VM` command; explicit `--port` still supported
- GPM (Geo Pointing Module) is auto-detected during PTU initialization; `ptu.gpm` is `None` if not present
- SPICE Earth orientation kernel (`earth_latest_high_prec.bpc`) has a limited prediction window and should be periodically updated from NASA NAIF

## Validation Tolerances

<!-- Acceptable tolerances for validation tests -->

## Project-Specific Conventions

- Debug/test outputs go to `tests/` or `/tmp`, never project root
- Output images go to `out/` (gitignored)
