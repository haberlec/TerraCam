# Field Operations Guide

Planning and command reference for running TerraCam grid surveys in the
field. Assumes the camera, filter wheel, and PTU are connected and the
software is installed (see docs/windows_install.md for the field laptop).

All mission scripts must be run **as modules from the repo root**, not as
direct scripts:

    python -m scripts.mission.run_grid_survey ...     # correct
    python scripts/mission/run_grid_survey.py ...     # FAILS (import error)

---

## 0. Pre-flight checklist

Before a survey:

- [ ] Camera + filter wheel enumerate: `python -m tests.diagnostics.diagnose_camera_detection` (or any capture command starts cleanly).
- [ ] PTU auto-discovers: watch for "PTU found on ... D100E" at startup.
- [ ] Gimbal is physically clear to slew the full survey extent.
- [ ] Output location has disk space (~30 MB per position with all 16 filters).
- [ ] Decide whether to cool the CCD (see §4). For daytime reflectance,
      cooling reduces dark noise; for quick daylight scenes it is optional.
- [ ] Measure / estimate the **scene range** in meters (for the pointing
      backplane parallax correction, `--scene-range-m`).

Soft pointing limits used in these examples: **pan ±90°, tilt ±20°.**
Adjust to your mount's real clearances.

---

## 1. Plan the survey area interactively (aim_ptu)

Use the interactive aiming tool to frame the scene and derive the exact
grid parameters. It streams live frames and drives the PTU from the
keyboard.

    python -m scripts.mission.aim_ptu --lens 28mm

Controls (focus the video window):

| Key | Action |
|-----|--------|
| Arrow keys / W A S D | Nudge PTU (up/down = tilt, left/right = pan) |
| `[` / `]` | Decrease / increase step size (default 2.5°) |
| `-` / `=` | Decrease / increase preview exposure |
| `0`–`9` | Filter position (0 = clear) |
| `m` | **Mark** current pan/tilt as a survey corner |
| `c` | Clear marks |
| `p` | **Print** the run_grid_survey command from marked corners |
| `h` | Home to pan=0, tilt=0 |
| `q` / ESC | Quit |

**Workflow:** drive to one corner of the area you want to cover, press
`m`. Drive to the opposite corner, press `m`. Press `p` — the tool prints
the `--pan-center`, `--tilt-center`, `--pan-extent`, and `--tilt-extent`
that cover what you framed, as a ready-to-edit command. Mark more than two
corners if the area is irregular; it uses the bounding box of all marks.

The green crosshair is the frame center (the pointing direction). The
overlay shows current pan/tilt, step size, exposure, filter, and the FOV
for the selected lens.

Start it on a specific filter/exposure if the scene needs it:

    python -m scripts.mission.aim_ptu --lens 28mm --filter 6 --exposure 200

---

## 2. Run the grid survey

### FOV-aware mode (recommended)

Spacing is derived from the lens field of view and the requested overlap,
so you specify the area to cover and the tool computes the grid. This mode
also writes the az/el pointing backplanes (it knows the lens).

    python -m scripts.mission.run_grid_survey \
        --lens 28mm \
        --pan-center 2.0 --tilt-center 1.0 \
        --pan-extent 10.0 --tilt-extent 6.0 \
        --overlap 0.20 \
        --filters 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 \
        --auto-expose \
        --scene-range-m 500 \
        --settle-time 1.0 \
        --output ./out --name site_a

- `--pan-extent` / `--tilt-extent`: minimum area to cover in degrees
  (the grid rounds up to whole FOV steps).
- `--overlap 0.20`: 20% frame overlap for mosaicking.
- `--filters`: filter wheel positions to capture at each point. Position 0
  is clear (no bandpass); 1–16 are 400–1100nm ascending. Capturing in
  ascending order is efficient (the wheel does not backtrack).
- `--auto-expose`: measures optimal exposure per filter at the grid center
  once, then reuses it for the whole grid.
- `--scene-range-m`: assumed scene distance for backplane parallax; omit
  for scenes effectively at infinity.

### Manual mode

Explicit pan/tilt ranges and step counts. **Note:** manual mode does not
take a lens, so it does **not** write pointing backplanes — use FOV-aware
mode if you need them.

    python -m scripts.mission.run_grid_survey \
        --pan-range -10 10 --tilt-range -5 5 \
        --pan-steps 5 --tilt-steps 3 \
        --filters 1 6 11 \
        --exposure 200 \
        --output ./out --name site_b

### A quick single-band test survey

Smallest useful run to confirm the whole chain before committing to a long
survey:

    python -m scripts.mission.run_grid_survey \
        --lens 28mm --pan-center 0 --tilt-center 0 \
        --pan-extent 1 --tilt-extent 0 \
        --filters 6 --auto-expose \
        --output ./out --name test

---

## 3. Robustness options (field-critical)

These come from the sequence-engine hardening — use them on real missions.

    --max-duration-min 45     # watchdog: abort to a safe state after N min
    --resume                  # resume an interrupted survey of the same --name
    --stop-on-error           # stop on first failed position (default: continue)
    --no-return               # do not return to start when finished

**If a survey is interrupted** (Ctrl+C, power loss, crash): a checkpoint
file `<name>_checkpoint.json` records completed positions. Re-run the exact
same command with `--resume` added; it skips finished positions and
continues. The checkpoint is deleted automatically on successful
completion.

**On abort/fault**, the coordinator halts the PTU (confirmed stopped),
closes the shutter, and finalizes the in-progress NetCDF — no need to
manually safe the hardware, but visually confirm the gimbal stopped.

---

## 3b. Camera mounting orientation

The camera currently mounts with its labels upright, which puts the
**sensor upside-down** — so the raw frame is rotated 180° relative to the
scene. This is recorded as `sensor_inverted: true` in
`config/ptu_specifications.json` → `mount_geometry`, stamped into every
NetCDF, and read by the mosaic, derived-product, backplane, and aiming
code, which de-rotate automatically. You do not pass any flag for this.

The `aim_ptu` preview de-rotates too, so what you frame in the aiming
window matches the delivered data and mosaics.

**If you remount the camera the other way up** (sensor upright), set
`sensor_inverted: false` in the config — that single change keeps capture,
aiming, mosaicking, derived products, and backplanes all consistent. New
captures then carry `sensor_inverted: false` in their files; old captures
keep `true`, so both process correctly with no flags. See the remount
procedure in docs/hardware_checklist.md.

## 4. Cooling (optional)

    --target-temp -20         # set CCD target temperature in C

The camera does not wait for temperature by default. For dark-noise-
sensitive work, set the target early and give it a few minutes before
starting. For bright daylight scenes at short exposures, cooling has
little effect and can be skipped.

---

## 5. After the survey — build a mosaic

Each position is a NetCDF cube in `--output`. To stitch one band into a
mosaic (coordinate placement, with parallax correction matching capture):

    python scripts/analysis/create_mosaic.py \
        --summary ./out/site_a_summary.json \
        --by-filter --band 6 \
        --scene-range-m 500 \
        --mode coordinate

`--by-filter --band 6` selects the 650nm filter by position (resolved to
the cube band index). Use `--mode cylindrical` for the optimized
projection, or `--mode opencv` for feature-based stitching. `create_mosaic`
runs on any machine with the Python deps — it needs no camera/PTU driver,
so mosaic processing can happen off the field laptop.

---

## Quick reference: filter position → wavelength

| Pos | nm | Pos | nm | Pos | nm |
|-----|-----|-----|-----|-----|-----|
| 0 | clear | 6 | 650 | 12 | 950 |
| 1 | 400 | 7 | 700 | 13 | 975 |
| 2 | 450 | 8 | 750 | 14 | 1000 |
| 3 | 500 | 9 | 800 | 15 | 1050 |
| 4 | 550 | 10 | 850 | 16 | 1100 |
| 5 | 600 | 11 | 900 | | |

(Authoritative source: config/filter_specifications.json.)
