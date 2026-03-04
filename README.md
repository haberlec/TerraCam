# TerraCam - Multispectral Imaging Command & Control

Python and C software for controlling a Finger Lakes Instrumentation (FLI) MicroLine MLx695 CCD camera with a 16-position VNIR bandpass filter wheel (400-1100nm), mounted on a FLIR PTU D100E biaxial gimbal with Geo Pointing Module (GPM).

## Project Structure

```
├── src/
│   ├── fli/               # FLI camera and filter wheel control
│   │   ├── core/          # Low-level hardware interface (ctypes bindings)
│   │   ├── system.py      # High-level device orchestration
│   │   └── acquisition.py # Robust capture with USB error recovery
│   ├── ptu/               # FLIR PTU D100E gimbal control
│   │   ├── controller.py  # Serial controller (connect, move, halt, status)
│   │   ├── discovery.py   # Auto-discovery of PTU on serial ports
│   │   ├── gpm.py         # Geo Pointing Module (GPS, attitude, geo-aiming)
│   │   └── logger.py      # Structured session logging
│   ├── astro/             # Celestial body tracking (SpiceyPy / NASA SPICE)
│   │   ├── ephemeris.py   # SPICE ephemeris and coordinate transforms
│   │   └── tracker.py     # Single-shot and continuous tracking logic
│   └── libfli/            # FLI C library (modified for macOS compatibility)
├── scripts/
│   ├── capture/           # Image capture and auto-exposure scripts
│   └── mission/           # Mission scripts (grid survey, waypoints, celestial)
├── config/                # Hardware specifications (JSON)
├── data/
│   └── spice/             # Bundled NASA SPICE kernels for celestial tracking
├── tests/                 # Unit, integration, and hardware tests
├── tools/                 # USB permission diagnostics
└── docs/                  # SDK documentation and command references
```

## Getting Started

### Install the Python package

```bash
pip install -e .
```

For celestial tracking support:
```bash
pip install -e ".[astro]"
```

For live video support:
```bash
pip install -e ".[video]"
```

### Build the C library

```bash
cd src/libfli
make
```

### Run tests

```bash
pytest tests/unit/
```

## Key Components

### FLI Camera API

```python
from fli import FLISystem

with FLISystem() as system:
    system.set_temperature(-20)
    system.wait_for_temperature()
    system.move_filter(3)
    image = system.capture_image(exposure_ms=1000)
```

### PTU Gimbal Control

The PTU auto-discovers its serial port (no `--port` needed):

```python
from ptu import PTUController, PTUConfig

config = PTUConfig()  # auto-discovers serial port
ptu = PTUController(config)
ptu.connect()
ptu.initialize()
ptu.move_to_position(pan_steps=1000, tilt_steps=-500)

# GPM geo-pointing (if hardware available):
if ptu.gpm is not None:
    from ptu.gpm import GeoTarget
    ptu.gpm.point_to_coordinate(GeoTarget(40.7128, -74.0060, 10.0))
```

### Mission Scripts

**Grid Survey** — rectangular grid scan with FOV-aware or manual positioning:
```bash
python scripts/mission/run_grid_survey.py \
    --lens 28mm --pan-extent 60 --tilt-extent 30 \
    --overlap 0.20 --filters 0 1 2 3 --exposure 200
```

**Waypoint Mission** — capture at predefined positions from a JSON file:
```bash
python scripts/mission/run_waypoint_mission.py \
    --waypoints waypoints.json --filters 0 1 2 --exposure 200
```

**Celestial Tracking** — point at and track celestial bodies using NASA SPICE:
```bash
# Single-shot: point at the Moon and capture
python scripts/mission/run_celestial_track.py --target MOON --filters 0 1 2

# Continuous: track the Moon for 1 hour, repointing every 60s
python scripts/mission/run_celestial_track.py --target MOON \
    --duration 3600 --interval 60 --filters 0 1 2

# RA/Dec target (e.g., Orion Nebula)
python scripts/mission/run_celestial_track.py \
    --ra 83.82 --dec -5.39 --name "M42"
```

### Capture Scripts

- **capture_image.py** — Full capture workflow with auto-exposure and metadata
- **capture_video.py** — Real-time video display via OpenCV
- **live_focus.py** — Interactive focusing with live metrics
- **auto_expose.py** — Auto-exposure engine with quality scoring
- **exposure_predictor.py** — Multi-filter exposure prediction

## Requirements

- Python >= 3.8
- numpy, Pillow, pyserial
- FLI camera hardware (USB) and/or FLIR PTU D100E (RS-232)
- macOS or Linux
- Optional: spiceypy (celestial tracking), opencv-python (live video)

## License

Please refer to the FLI SDK license terms.
