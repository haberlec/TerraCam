# Hardware Test Checklist

Running list of tests and decisions that require physical access to the
camera, filter wheel, and PTU. Maintained alongside docs/hardening_plan.md;
items are added as code changes land and checked off with results noted
inline. Bring: serial cable, multimeter (PTU supply measurement), Spectralon
panel for any calibration captures.

## Filter wheel (Phase 1 follow-ups)

- [ ] **Confirm hardware filter count.** Run
      `FLISystem.get_filter_count()` (wraps `FLIGetFilterCount`) and record
      the value. The config defines 17 logical positions (0-16). If the
      hardware reports fewer, `system.py` position validation will refuse
      position 16 (the 1100nm filter) and we need a mapping fix at the
      device layer.
- [ ] **Command position 16 and verify.** `move_filter(16)`, then
      `get_filter_position()` — confirm it lands and reports 16. The
      pre-fix missions only ever commanded 0-15, so position 16 is
      untested in the field.
- [ ] **Full 17-position stack.** Capture positions 0-16 to NetCDF and
      verify: `filter_position` runs 0-16, `wavelength` runs
      NaN, 400 ... 1100, RGB preview looks sane (clear band excluded).
- [ ] **One physical sanity check of the corrected labels** (quick): a
      color-target or clear-sky capture; confirm blue/green/red render
      correctly with no +1 band shift, and the water-vapor dip sits in the
      950nm band (see memory note: band core ~940nm, 975 filter on the
      shoulder).

## PTU serial protocol (Phase 2 validation)

Enable wire logging for every PTU session below:
`logging.getLogger("ptu.serial").setLevel(logging.DEBUG)` — attach a
`FileHandler` so the raw TX/RX log is preserved.

- [ ] **Protocol smoke test.** connect → initialize → small move →
      `get_position` → `halt` → disconnect. Confirm in the wire log that
      every RX line begins with the echoed command (EE took effect) and no
      `discarded stale` lines appear in steady state.
- [ ] **Desync-vs-brownout discriminator.** Run a long simultaneous
      pan+tilt slew at the configured power modes and watch the wire log:
      uncorrelated/garbled RX lines → protocol issue (should now
      self-heal); RX silence or an unsolicited boot banner mid-move →
      power brownout. This is the definitive verdict on the field trial
      failure.
- [ ] **Supply voltage measurement.** Multimeter (or scope) on the PTU
      supply at the connector during: (a) simultaneous move at LOW/LOW,
      (b) simultaneous move at HIGH move power, (c) sequential-axes move.
      Record the sag. Decide whether the LOW/LOW + `sequential_axes`
      mitigation is needed or the supply should be upsized.
- [ ] **Hold-power sag test.** With the full camera payload mounted and
      hold power LOW, park at tilt extremes and log `get_position()` every
      10 s for ~5 min. Any drift means LOW hold power cannot hold the
      payload — expect the new stall/position-verification checks to flag
      exactly this, so decide hold power policy before the next mission.
- [ ] **Stall detection sanity.** Command a move and confirm normal moves
      never trip the stall detector (positions frozen off-target for 8
      polls ≈ 1.6 s). If accel ramps are slow enough to trip it, raise
      `_STALL_POLL_COUNT` or the poll interval.
- [ ] **Halt-mid-move responsiveness.** Start a long slew, call `halt()`
      ~1 s in: confirm it returns True promptly and the axes are
      stationary well short of the original target (this validates
      dropping the `A` await command).
- [ ] **Auto-discovery on the real port.** `discover_ptu()` with the PTU
      on and, separately, with other serial devices attached — confirm
      the echo-aware probe identifies the PTU and skips imposters, and
      that a subsequent connect+initialize works first try (the original
      failure mode).
- [ ] **GPM detection through the new protocol** (if GPM mounted):
      confirm `ptu.gpm` is detected and a GPS/attitude query parses.
- [ ] **2-second post-reset behavior.** We never send `R` (reset) in
      normal operation; if a manual reset is issued, confirm the
      controller re-initializes cleanly afterward.

## Sequence engine (Phase 3 validation)

- [ ] **Mid-mission abort drill.** Start a small grid survey, Ctrl+C
      during a capture: confirm the PTU halts (log line "Safe state: PTU
      halted at Pan=..., Tilt=..."), shutter closes, the in-progress
      NetCDF is finalized/readable, and the checkpoint file
      (`<name>_checkpoint.json` in the output dir) lists the completed
      positions.
- [ ] **Checkpoint resume drill.** After the abort drill, rerun the same
      survey command with `--resume`: confirm it skips the completed
      positions (log: "Resuming: N positions already completed") and the
      checkpoint file is deleted when the survey finishes.
- [ ] **Power-pull recovery.** Kill power to the PTU mid-survey, restore,
      rerun with `--resume`: confirm the mission continues from the last
      completed position and the PTU re-initializes cleanly.
- [ ] **Watchdog sanity.** Run a short survey with `--max-duration-min`
      set below the expected runtime: confirm it aborts into safe state
      with the "Mission watchdog expired" log line rather than running on.
- [ ] **Camera fault visibility.** During a survey, watch the session log
      for the newly-visible camera warnings ("Camera not idle...",
      "Camera status query failed...") — previously these were silent.
      Any that appear routinely indicate a timing constant worth tuning.
- [ ] **GPM geo-pointing smoke test** (if GPM mounted): run a one-target
      geo sequence — this path had latent crashes (wrong call signature,
      wrong logger calls) fixed 2026-07-10 and has plausibly never run
      end-to-end; verify captures land and metadata includes geo-pointing.

## Pointing backplanes (feature validation)

- [ ] **Measure the camera offset vector.** Distance from the pan/tilt
      axes intersection to the lens entrance pupil, decomposed as
      (forward along boresight, along tilt axis, up) at tilt = 0. Tape
      measure / CAD is fine to ~5 mm. Enter into
      `config/ptu_specifications.json` → `mount_geometry.camera_offset_m`.
- [ ] **Sign-convention capture.** One frame of a scene with known
      left/right and up/down (or slew the PTU +pan and watch which way
      the image moves): confirm the backplane azimuth increases in the
      correct image direction, and set `CameraModel.roll_deg` (180 for
      the inverted mount) accordingly.
- [ ] **Overlap self-consistency.** Run a small grid survey with
      backplanes at the estimated scene range; for a feature visible in
      two adjacent tiles, compare its interpolated backplane az/el
      between the tiles. Disagreement > a few hundredths of a degree
      indicates offset/boresight/roll error. Repeat with
      `--scene-range-m` off to see the lever-arm term directly.
- [ ] **Celestial cross-check.** Point at the Moon (or a bright star),
      centroid it, and compare the centroid pixel's backplane az/el
      (converted to world frame via mounting attitude) against the SPICE
      prediction from the astro package. Calibrates the boresight
      angular offsets absolutely.
- [ ] **Boresight calibration via mosaic fit.** Run a cylindrical-mode
      mosaic with `--save-model`; the fitted f_eff/cx/cy/roll/k1 are in
      the same convention as `fli.geometry.CameraModel` — feed them back
      as the calibrated intrinsics and re-check overlap consistency.
- [ ] **Scene-range sensitivity.** For a typical target, compare
      mosaics at the estimated range vs infinity; the residual tile
      misregistration difference should match offset/range prediction.

## Reorganization (Phase 4 validation)

- [ ] **Capture-script smoke test.** Run `capture_image.py` and
      `live_focus.py` once with hardware attached — their auto-exposure
      import moved to the installed `fli.auto_expose` module (imports
      verified without hardware; capture flow itself unchanged).

## Camera mounting orientation

The mount currently puts the sensor upside-down (labels upright), so
`config/ptu_specifications.json` → `mount_geometry.sensor_inverted` is
`true`. Orientation is metadata-driven end to end: stamped into each
NetCDF and each capture's metadata, read by mosaic / derived-products /
backplane / aim_ptu, all of which de-rotate automatically. Old (inverted)
and future (upright) captures each carry their own flag, so both process
correctly with no analysis-time flags.

- [ ] **Confirm the de-rotation direction on hardware.** In `aim_ptu`,
      point at a scene with obvious up/down and left/right; confirm the
      (de-rotated) preview shows it the right way up and that PTU
      up/left/right nudges move the scene the expected way. If the
      preview is still upside-down, the mount is *not* actually inverted
      — set `sensor_inverted: false`.
- [ ] **Backplane sign check.** With a de-rotated capture, confirm the
      backplane azimuth increases toward increasing image column (the
      camera roll_deg is tied to the same flag).

### Remount procedure (when flipping the camera upright)

When you physically remount the camera so the sensor is upright:

1. Edit `config/ptu_specifications.json` → `mount_geometry.sensor_inverted`
   to `false`. This is the **only** change required.
2. Re-run the `aim_ptu` orientation check above to confirm the preview is
   now upright with no rotation.
3. New captures automatically stamp `sensor_inverted: false`; existing
   data keeps `true`. No reprocessing of old data is needed, and no
   analysis flags change.
4. If you have raw-TIFF (legacy) captures without the field, they default
   to inverted — correct for everything captured on the old mount.

## Notes / results

(Record dates, log file paths, and measurements here as items are run.)
