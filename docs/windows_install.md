# Windows Installation (Field Command & Control)

Setup guide for running the **full TerraCam acquisition stack** (FLI
camera + filter wheel + PTU) on a Windows 11 field computer — e.g. the
Panasonic Toughbook C2 machine. This box drives hardware, so both the
camera C-library/driver path and the serial/PTU path must work.

Reference hardware this guide was written against: Windows 11 Pro,
Intel Core i5-1345U, 16 GB RAM.

---

## Overview of the moving parts

| Layer | Windows requirement |
|-------|---------------------|
| FLI camera | `libfli.dll` (64-bit) + FLI USB kernel driver |
| FLI filter wheel / focuser | same DLL + driver (same USB stack) |
| PTU D100E | pyserial over a COM port (USB-serial adapter) — no special driver beyond the adapter's |
| Python packages | numpy, Pillow, pyserial, scipy, scikit-learn, netCDF4, spiceypy, opencv — all have Windows wheels |

The camera side is the only part with a real Windows-specific
dependency. The PTU side is portable Python.

---

## 1. Python

Install **Python 3.10 or 3.11** (64-bit) from python.org. Confirm the
architecture matches the DLL — this must report `64bit`:

```
python -c "import platform; print(platform.architecture()[0])"
```

A 32-bit Python cannot load the 64-bit `libfli.dll`, and vice versa.
This is the single most common silent failure — verify it first.

Create and activate a virtual environment in the repo root:

```
python -m venv .venv
.venv\Scripts\activate
```

## 2. FLI camera library (DLL)

The FLI Windows SDK provides a prebuilt DLL — **no compilation needed**
(skip the Visual Studio project under `src/libfli/windows/` entirely).

From the FLI SDK package, take the **64-bit** DLL (the one under an
`x64/` folder; it is `PE32+ x86-64`, ~250 KB). Ignore any 32-bit DLL in
the package root.

Copy it into the package directory. The loader tries both `libfli64.dll`
and `libfli.dll` there, so either name works:

```
copy <FLI_SDK>\x64\libfli.dll  src\fli\core\libfli64.dll
```

> Note: `src/fli/core/libfli.so` already in the repo is the macOS/Linux
> build and is ignored on Windows — leave it.

If the DLL is missing or the wrong architecture, `FLILibrary.getDll()`
now raises a clear error listing the paths it tried and pointing back
here, rather than a cryptic `OSError`.

## 3. FLI USB driver

`libfli.dll` talks to a kernel-mode USB driver via `DeviceIoControl`;
the SDK DLL alone is not enough. The driver is installed by **FLI's
Windows software (FLIGrab and the FLI camera utilities)** — if that is
already installed on this machine, the driver is almost certainly
present.

To confirm after plugging in the camera:

1. Open **Device Manager**.
2. The camera should appear as an **FLI device** (often under a
   "libusb" / "FLI" / "Imaging devices" category) — **not** as an
   "Unknown device" or with a yellow warning triangle.
3. If it shows as unknown: reinstall the FLI driver (reinstall FLIGrab,
   or install FLI's standalone driver package), then replug.

### Windows 11 driver-signature note

The bundled SDK DLL is dated 2021, so its paired driver is a similar
vintage. Windows 11 with Secure Boot enforces driver signatures; a
current FLI-signed driver loads fine, but if Device Manager reports a
signature/Code 52 error, obtain the latest FLI Windows driver package.
Do **not** disable driver-signature enforcement on a field machine —
get a properly signed driver instead.

### Sanity check the camera before Python

Launch **FLIGrab** and confirm it enumerates and captures from the
camera. If FLIGrab cannot see the camera, Python will not either — fix
it at the driver level first. This isolates driver problems from
Python/install problems.

## 4. PTU serial (COM port)

The PTU D100E connects over RS-232, typically through a USB-to-serial
adapter. Install the adapter's driver (FTDI/Prolific/etc. — usually
automatic on Windows 11). The port then appears as `COMx`.

No code change is needed: auto-discovery probes all COM ports with the
`VM` command (`PTUConfig(port="auto")`), or pass the port explicitly:

```
--port COM3
```

Find the port in Device Manager under **Ports (COM & LPT)**, or:

```
python -c "import serial.tools.list_ports as p; [print(x.device, x.description) for x in p.comports()]"
```

## 5. Install the Python package

From the repo root, with the venv active:

```
pip install -e ".[netcdf,astro,video]"
```

This pulls numpy, Pillow, pyserial (core) plus netCDF4 (NetCDF output +
az/el backplanes), spiceypy (celestial tracking), and opencv (video/
focus tools). All have Windows wheels — no build tools required for the
Python side.

If you do **not** need celestial tracking or live video on this box,
`pip install -e ".[netcdf]"` is enough for grid/waypoint surveys.

## 6. Verify

Run the unit tests (no hardware needed — mocks only):

```
pytest tests\unit
```

Then, with hardware connected, the camera-detection diagnostic:

```
python tests\diagnostics\diagnose_camera_detection.py
```

A successful run means the DLL loaded, the driver responded, and
`FLIList` enumerated the camera and filter wheel.

---

## Field-C2 checklist (do before you rely on it)

This machine drives hardware unattended in the field, so verify these
beyond a bench "it connects":

- [ ] **Reboot persistence.** Confirm the FLI driver survives a reboot
      and the camera re-enumerates without reinstalling anything.
- [ ] **USB port consistency.** Note which physical USB ports the camera
      and PTU adapter use; COM port numbers can change if you move the
      adapter to a different port. Prefer always using the same ports,
      or pass `--port COMx` explicitly rather than relying on auto-detect
      if enumeration is slow.
- [ ] **Power management.** In Device Manager, disable "Allow the
      computer to turn off this device to save power" for the USB hubs
      and the serial adapter — Windows USB selective-suspend can drop a
      camera or PTU mid-survey. Also set the Windows power plan to High
      Performance / never sleep while on mission.
- [ ] **`TERRACAM_CONFIG` (optional).** If the repo is not at a fixed
      path, set this environment variable to the `config\` directory so
      config resolution is unambiguous regardless of working directory.
- [ ] **Output path.** Surveys write to `.\out\` relative to where you
      run; on a field laptop point `--output` at a known location with
      space (NetCDF cubes are ~tens of MB per position).
- [ ] **PTU serial timeout budget.** The hardened PTU layer logs raw
      TX/RX on the `ptu.serial` logger; enable it for the first field
      runs to confirm clean command/response correlation over the actual
      USB-serial adapter (some cheap adapters add latency). See
      docs/hardware_checklist.md.
- [ ] **Run the full hardware checklist.** docs/hardware_checklist.md
      covers the camera/PTU/sequence-engine validation that applies
      regardless of OS.

---

## Troubleshooting

**`RuntimeError: Could not load the libfli shared library`**
The DLL is missing, misnamed, or the wrong architecture. The error lists
the paths tried — put a 64-bit `libfli64.dll` (or `libfli.dll`) in
`src\fli\core\`. Re-check Python is 64-bit (step 1).

**DLL loads but no camera found / `FLIList` returns nothing**
The DLL loaded but the USB driver is not talking to the camera. Verify
in Device Manager (step 3) and that FLIGrab sees the camera (step 3
sanity check).

**`ImportError: DLL load failed while importing ...`** (numpy, netCDF4,
cv2)
A wheel/architecture mismatch in the Python environment — reinstall in a
clean 64-bit venv.

**Camera drops out mid-survey**
Almost always USB selective-suspend or a power-plan sleep — see the
power-management item in the field checklist.

**PTU "unresponsive" / garbled responses**
The hardened protocol layer self-heals from desync, but a high-latency
USB-serial adapter can still cause timeouts. Enable
`logging.getLogger("ptu.serial").setLevel(logging.DEBUG)` and check
whether responses arrive uncorrelated (adapter latency → raise
`command_timeout_s`) or not at all (wiring/power). See the PTU section
of docs/hardware_checklist.md.
