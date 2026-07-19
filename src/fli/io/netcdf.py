"""
NetCDF4 Writer for Multispectral Image Cubes

Packages all bands captured at a single grid position into a single
self-describing NetCDF4 file with CF Convention metadata. Supports
incremental band addition matching the coordinator's filter-by-filter
capture workflow.

Usage:
    from fli.io.netcdf import MultispectralNetCDF

    with MultispectralNetCDF(
        filepath="position_001.nc",
        position_id="grid_001",
        pan_degrees=5.0,
        tilt_degrees=-10.0,
        pan_steps=333,
        tilt_steps=-667,
        sequence_name="survey_01",
    ) as writer:
        for filt_pos in [0, 1, 2, ...]:
            image = camera.capture(...)
            writer.add_band(image, filt_pos, exposure_ms, datetime.now())
"""

import logging
import numpy as np
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, Union

import netCDF4

from ..config import load_config
from ..geometry import (
    camera_model_from_configs,
    mount_model_from_config,
    pixel_to_azel,
)


logger = logging.getLogger(__name__)


class MultispectralNetCDF:
    """Writer for multispectral image cubes in NetCDF4 format.

    Creates a single NetCDF4 file per grid position containing all
    captured bands with full metadata. Supports incremental band
    addition (one band at a time) with ``nc.sync()`` after each write
    for crash safety.

    Parameters
    ----------
    filepath : str or Path
        Output file path.
    position_id : str
        Position identifier (e.g., ``"grid_001"``).
    pan_degrees : float
        Pan angle in degrees.
    tilt_degrees : float
        Tilt angle in degrees.
    pan_steps : int
        Pan position in encoder steps.
    tilt_steps : int
        Tilt position in encoder steps.
    sequence_name : str
        Name of the acquisition sequence.
    expected_bands : int
        Expected number of bands (informational, not enforced).
    ccd_temperature_c : float, optional
        CCD temperature at time of capture.
    geo_pointing : dict, optional
        GPM metadata snapshot with ``gps_position``, ``mounting_attitude``,
        and ``calibration_quality`` keys.
    filter_config : dict, optional
        Pre-loaded ``filter_specifications.json`` content. Loaded from
        default path if not provided.
    camera_config : dict, optional
        Pre-loaded ``camera_specifications.json`` content. Loaded from
        default path if not provided.
    lens_config : dict, optional
        Pre-loaded ``lens_specifications.json`` content. Loaded from
        default path if not provided.
    lens_id : str, optional
        Lens identifier key (e.g., ``"28mm"``).
    compression_level : int
        zlib compression level 1-9 (default: 4).
    backplanes : str
        Az/el backplane generation: ``"subsampled"`` (default) writes
        the backplane on a decimated pixel grid (interpolation error is
        negligible for the smooth az/el field), ``"full"`` writes every
        pixel, ``"none"`` disables. Requires ``lens_config`` and
        ``lens_id``.
    scene_range_m : float, optional
        Assumed scene distance for the backplane parallax correction
        (camera lever arm off the PTU axes); None means infinity.
    mount_config : dict, optional
        The ``mount_geometry`` section of ptu_specifications.json
        (camera offset and boresight misalignment). None means an
        ideal centered mount.
    actual_pan_degrees : float, optional
        Encoder-derived pan angle; used for the backplane in preference
        to the commanded ``pan_degrees``.
    actual_tilt_degrees : float, optional
        Encoder-derived tilt angle (see ``actual_pan_degrees``).
    """

    # Target wavelengths for false-color RGB composite (nm)
    _RGB_WAVELENGTHS = (650.0, 550.0, 450.0)  # R, G, B

    # Decimation factor for "subsampled" backplanes
    _BACKPLANE_SUBSAMPLE = 16

    def __init__(
        self,
        filepath: Union[str, Path],
        position_id: str,
        pan_degrees: float,
        tilt_degrees: float,
        pan_steps: int,
        tilt_steps: int,
        sequence_name: str,
        expected_bands: int = 16,
        ccd_temperature_c: Optional[float] = None,
        geo_pointing: Optional[Dict[str, Any]] = None,
        filter_config: Optional[Dict[str, Any]] = None,
        camera_config: Optional[Dict[str, Any]] = None,
        lens_config: Optional[Dict[str, Any]] = None,
        lens_id: Optional[str] = None,
        compression_level: int = 4,
        backplanes: str = "subsampled",
        scene_range_m: Optional[float] = None,
        mount_config: Optional[Dict[str, Any]] = None,
        actual_pan_degrees: Optional[float] = None,
        actual_tilt_degrees: Optional[float] = None,
    ):
        if backplanes not in ("subsampled", "full", "none"):
            raise ValueError(
                f"backplanes must be 'subsampled', 'full', or 'none', "
                f"got {backplanes!r}"
            )
        self.backplanes = backplanes
        self.scene_range_m = scene_range_m
        self.mount_config = mount_config
        self.actual_pan_degrees = actual_pan_degrees
        self.actual_tilt_degrees = actual_tilt_degrees
        self.filepath = Path(filepath)
        self.position_id = position_id
        self.pan_degrees = pan_degrees
        self.tilt_degrees = tilt_degrees
        self.pan_steps = pan_steps
        self.tilt_steps = tilt_steps
        self.sequence_name = sequence_name
        self.expected_bands = expected_bands
        self.ccd_temperature_c = ccd_temperature_c
        self.geo_pointing = geo_pointing
        self.lens_id = lens_id
        self.compression_level = compression_level

        # Load configs (resolution honors TERRACAM_CONFIG; see fli.config)
        self._filter_config = filter_config or load_config(
            "filter_specifications.json"
        )
        self._camera_config = camera_config or load_config(
            "camera_specifications.json"
        )
        self._lens_config = lens_config or load_config(
            "lens_specifications.json"
        )

        # Build filter lookup: position -> filter spec dict
        self._filter_lookup = self._build_filter_lookup()

        # State
        self._dataset: Optional[netCDF4.Dataset] = None
        self._dimensions_created = False
        self._band_count = 0
        self._first_capture_time: Optional[datetime] = None
        self._closed = False

        # Open the dataset immediately
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        self._dataset = netCDF4.Dataset(
            str(self.filepath), mode='w', format='NETCDF4'
        )
        self._write_global_attributes()

    def _build_filter_lookup(self) -> Dict[int, Dict[str, Any]]:
        """Build a lookup dict from filter position to filter spec.

        Returns
        -------
        dict
            Mapping of filter position (int) to filter specification dict.
        """
        lookup = {}
        if self._filter_config and "filter_specifications" in self._filter_config:
            specs = self._filter_config["filter_specifications"]
            for filt in specs.get("filters", []):
                lookup[filt["position"]] = filt
        return lookup

    def _write_global_attributes(self) -> None:
        """Write global attributes to the NetCDF dataset."""
        ds = self._dataset
        now = datetime.now()

        # CF Convention attributes
        ds.Conventions = "CF-1.8"
        ds.title = f"{self.sequence_name} position {self.position_id}"
        ds.institution = "TerraCam"
        ds.source = "FLI MLx695 CCD with 16-position VNIR filter wheel"
        ds.history = (
            f"Created {now.isoformat()} by TerraCam MultispectralNetCDF writer"
        )

        # Position metadata
        ds.sequence_name = self.sequence_name
        ds.position_id = self.position_id
        ds.pan_degrees = np.float64(self.pan_degrees)
        ds.tilt_degrees = np.float64(self.tilt_degrees)
        ds.pan_steps = np.int32(self.pan_steps)
        ds.tilt_steps = np.int32(self.tilt_steps)

        if self.ccd_temperature_c is not None:
            ds.ccd_temperature_c = np.float64(self.ccd_temperature_c)

        # Encoder-derived pointing (commanded angles are in
        # pan_degrees/tilt_degrees; these are what the PTU reported)
        if self.actual_pan_degrees is not None:
            ds.actual_pan_degrees = np.float64(self.actual_pan_degrees)
        if self.actual_tilt_degrees is not None:
            ds.actual_tilt_degrees = np.float64(self.actual_tilt_degrees)

        # Sensor metadata
        if self._camera_config:
            cam = self._camera_config
            sensor = cam.get("sensor", {})
            ds.sensor_model = cam.get("model", "MLx695")
            ds.sensor_manufacturer = cam.get(
                "manufacturer", "Finger Lakes Instrumentation"
            )
            ds.sensor_type = sensor.get(
                "type", "Sony ICX695 Interline transfer CCD"
            )
            pixel = sensor.get("pixel_size", {})
            ds.pixel_size_um = np.float64(pixel.get("width_um", 4.54))
            ds.full_well_capacity = np.int32(
                sensor.get("full_well_capacity_electrons", 17000)
            )
            iface = cam.get("interface", {})
            ds.data_bit_depth = np.int32(iface.get("data_bit_depth", 16))

        # Lens metadata
        if self._lens_config and self.lens_id:
            lenses = self._lens_config.get("lenses", {})
            lens = lenses.get(self.lens_id, {})
            if lens:
                ds.lens_model = lens.get("model", self.lens_id)
                ds.focal_length_mm = np.float64(
                    lens.get("focal_length_mm", 0)
                )
                ds.f_number_range = lens.get("f_number_range", "")

        # Filter wheel metadata
        if self._filter_config and "filter_specifications" in self._filter_config:
            specs = self._filter_config["filter_specifications"]
            ds.filter_manufacturer = specs.get("manufacturer", "Edmund Optics")
            ds.filter_type = specs.get(
                "type", "Hard Coated Bandpass Interference Filters"
            )
            ds.blocking_od = np.float64(specs.get("blocking_od", 4.0))

        # Geo-pointing metadata
        if self.geo_pointing:
            gps = self.geo_pointing.get("gps_position", {})
            if gps:
                ds.gps_latitude = np.float64(gps.get("latitude", 0.0))
                ds.gps_longitude = np.float64(gps.get("longitude", 0.0))
                ds.gps_altitude = np.float64(gps.get("altitude", 0.0))
            attitude = self.geo_pointing.get("mounting_attitude", {})
            if attitude:
                ds.mounting_roll = np.float64(attitude.get("roll", 0.0))
                ds.mounting_pitch = np.float64(attitude.get("pitch", 0.0))
                ds.mounting_yaw = np.float64(attitude.get("yaw", 0.0))
            cal = self.geo_pointing.get("calibration_quality")
            if cal:
                ds.calibration_quality = str(cal)

        # Placeholders updated on finalize
        ds.bands_captured = np.int32(0)
        ds.total_capture_time_s = np.float64(0.0)

    def _create_dimensions_and_variables(
        self, image_height: int, image_width: int
    ) -> None:
        """Create dimensions and variables on first band write.

        Parameters
        ----------
        image_height : int
            Image height in pixels.
        image_width : int
            Image width in pixels.
        """
        ds = self._dataset

        # Dimensions
        ds.createDimension("band", None)  # UNLIMITED
        ds.createDimension("y", image_height)
        ds.createDimension("x", image_width)
        ds.createDimension("rgb", 3)

        # --- Primary data variable ---
        dn = ds.createVariable(
            "digital_number", np.uint16, ("band", "y", "x"),
            zlib=True, complevel=self.compression_level,
            chunksizes=(1, image_height, image_width),
            fill_value=np.uint16(0),
        )
        dn.long_name = "Raw digital number"
        dn.units = "count"
        dn.valid_range = np.array([0, 65535], dtype=np.uint16)
        dn.comment = (
            "Uncalibrated sensor output. Apply radiometric calibration "
            "to convert to physical units."
        )

        # --- RGB preview ---
        rgb = ds.createVariable(
            "rgb_preview", np.uint8, ("rgb", "y", "x"),
            zlib=True, complevel=self.compression_level,
            fill_value=np.uint8(0),
        )
        rgb.long_name = "False-color RGB preview"
        rgb.comment = (
            "8-bit false-color composite: R~650nm, G~550nm, B~450nm. "
            "Percentile-stretched for visualization."
        )

        # --- Coordinate variables ---
        wl = ds.createVariable("wavelength", np.float32, ("band",))
        wl.standard_name = "radiation_wavelength"
        wl.long_name = "Center wavelength of bandpass filter"
        wl.units = "nm"
        wl.comment = (
            "NaN for the clear (unfiltered) position 0, which has no "
            "bandpass."
        )

        bw = ds.createVariable("bandwidth", np.float32, ("band",))
        bw.long_name = "Full width at half maximum"
        bw.units = "nm"

        pt = ds.createVariable("peak_transmission", np.float32, ("band",))
        pt.long_name = "Peak filter transmission"
        pt.units = "1"
        pt.comment = "Fraction, 0 to 1"

        et = ds.createVariable("exposure_time", np.float32, ("band",))
        et.long_name = "Exposure time"
        et.units = "ms"

        ct = ds.createVariable("capture_time", np.float64, ("band",))
        ct.long_name = "Capture time"
        ct.units = "seconds since 1970-01-01T00:00:00Z"
        ct.calendar = "standard"

        fp = ds.createVariable("filter_position", np.int32, ("band",))
        fp.long_name = "Filter wheel position"
        fp.units = "1"
        fp.comment = "0 = clear (no filter); 1-16 = bandpass filters, 400-1100nm ascending"
        fp.valid_range = np.array([0, 16], dtype=np.int32)

        # --- Per-band statistics ---
        for stat_name, long_name in [
            ("image_min", "Minimum pixel value"),
            ("image_max", "Maximum pixel value"),
            ("image_mean", "Mean pixel value"),
            ("image_std", "Standard deviation of pixel values"),
            ("percentile_01", "1st percentile"),
            ("percentile_05", "5th percentile"),
            ("percentile_25", "25th percentile"),
            ("percentile_50", "Median (50th percentile)"),
            ("percentile_75", "75th percentile"),
            ("percentile_95", "95th percentile"),
            ("percentile_99", "99th percentile"),
        ]:
            var = ds.createVariable(stat_name, np.float32, ("band",))
            var.long_name = long_name
            var.units = "count"

        self._dimensions_created = True

    def add_band(
        self,
        image: np.ndarray,
        filter_position: int,
        exposure_ms: int,
        capture_time: datetime,
    ) -> None:
        """Write one band of image data with metadata.

        Parameters
        ----------
        image : numpy.ndarray
            2D image array (uint16 expected, shape ``(y, x)``).
        filter_position : int
            Filter wheel position (0 = clear, 1-16 = bandpass filters).
        exposure_ms : int
            Exposure time in milliseconds.
        capture_time : datetime
            Timestamp of capture.

        Raises
        ------
        RuntimeError
            If the writer has been closed or finalized.
        ValueError
            If image dimensions don't match previous bands, or if
            ``filter_position`` is not defined in the filter
            specifications config.
        """
        if self._closed:
            raise RuntimeError("Cannot add band: writer is closed")

        if self._dataset is None:
            raise RuntimeError("Cannot add band: no dataset open")

        # Create dimensions on first band
        if not self._dimensions_created:
            self._create_dimensions_and_variables(
                image.shape[0], image.shape[1]
            )

        # Validate shape consistency
        ds = self._dataset
        expected_y = len(ds.dimensions["y"])
        expected_x = len(ds.dimensions["x"])
        if image.shape[0] != expected_y or image.shape[1] != expected_x:
            raise ValueError(
                f"Image shape {image.shape} does not match expected "
                f"({expected_y}, {expected_x})"
            )

        idx = self._band_count

        # Track first capture time for total duration calculation
        if self._first_capture_time is None:
            self._first_capture_time = capture_time

        # Write image data
        ds.variables["digital_number"][idx, :, :] = image.astype(np.uint16)

        # Write coordinate variables. Refuse positions absent from the
        # config: writing a band with unknown provenance silently would
        # mislabel the science data.
        if self._filter_lookup and filter_position not in self._filter_lookup:
            raise ValueError(
                f"Filter position {filter_position} is not defined in "
                f"filter_specifications.json (defined positions: "
                f"{sorted(self._filter_lookup)})"
            )
        filt_spec = self._filter_lookup.get(filter_position, {})

        # Clear position (and missing config) carries no bandpass:
        # wavelength and bandwidth are NaN, per the variable attributes.
        center_nm = filt_spec.get("center_wavelength_nm")
        ds.variables["wavelength"][idx] = np.float32(
            center_nm if center_nm is not None else np.nan
        )

        fwhm = np.nan
        if (center_nm is not None and self._filter_config and
                "filter_specifications" in self._filter_config):
            fwhm = self._filter_config["filter_specifications"].get(
                "fwhm_nm", 25.0
            )
        ds.variables["bandwidth"][idx] = np.float32(fwhm)

        peak_pct = filt_spec.get("peak_transmission_percent")
        ds.variables["peak_transmission"][idx] = np.float32(
            peak_pct / 100.0 if peak_pct is not None else np.nan
        )
        ds.variables["exposure_time"][idx] = np.float32(exposure_ms)
        ds.variables["capture_time"][idx] = np.float64(
            capture_time.timestamp()
        )
        ds.variables["filter_position"][idx] = np.int32(filter_position)

        # Write per-band statistics
        percentiles = np.percentile(image, [1, 5, 25, 50, 75, 95, 99])
        ds.variables["image_min"][idx] = np.float32(np.min(image))
        ds.variables["image_max"][idx] = np.float32(np.max(image))
        ds.variables["image_mean"][idx] = np.float32(np.mean(image))
        ds.variables["image_std"][idx] = np.float32(np.std(image))
        ds.variables["percentile_01"][idx] = np.float32(percentiles[0])
        ds.variables["percentile_05"][idx] = np.float32(percentiles[1])
        ds.variables["percentile_25"][idx] = np.float32(percentiles[2])
        ds.variables["percentile_50"][idx] = np.float32(percentiles[3])
        ds.variables["percentile_75"][idx] = np.float32(percentiles[4])
        ds.variables["percentile_95"][idx] = np.float32(percentiles[5])
        ds.variables["percentile_99"][idx] = np.float32(percentiles[6])

        self._band_count += 1

        # Flush to disk for crash safety
        ds.sync()

        logger.debug(
            "Band %d written: filter=%d, wavelength=%snm, exposure=%dms",
            idx, filter_position,
            "clear" if center_nm is None else center_nm, exposure_ms,
        )

    def _scale_to_uint8(self, image: np.ndarray) -> np.ndarray:
        """Scale a uint16 image to uint8 using percentile stretch.

        Uses P0.5 to P99.5 stretch, matching the existing JPEG preview
        scaling in ``coordinator._save_image()``.

        Parameters
        ----------
        image : numpy.ndarray
            Input image (uint16).

        Returns
        -------
        numpy.ndarray
            Scaled uint8 image.
        """
        # Convert masked arrays (from NetCDF reads) to regular arrays
        if hasattr(image, 'data') and hasattr(image, 'mask'):
            image = np.asarray(image)
        min_val = np.percentile(image, 0.5)
        max_val = np.percentile(image, 99.5)
        if max_val > min_val:
            scaled = (image.astype(np.float32) - min_val) / (max_val - min_val) * 255
            return np.clip(scaled, 0, 255).astype(np.uint8)
        return np.zeros(image.shape, dtype=np.uint8)

    def _build_rgb_preview(self) -> None:
        """Build a false-color RGB composite from the closest bands.

        Selects bands nearest to 650nm (R), 550nm (G), and 450nm (B)
        from the bands already written.
        """
        ds = self._dataset
        if self._band_count == 0:
            return

        wavelengths = np.asarray(
            ds.variables["wavelength"][:self._band_count], dtype=np.float64
        )

        # Find closest band index for each RGB target wavelength.
        # Bands with NaN wavelength (clear position) are never selected;
        # if no band has a finite wavelength, fall back to band 0.
        finite = np.isfinite(wavelengths)
        rgb_indices = []
        for target_nm in self._RGB_WAVELENGTHS:
            if finite.any():
                distances = np.where(
                    finite, np.abs(wavelengths - target_nm), np.inf
                )
                rgb_indices.append(int(np.argmin(distances)))
            else:
                rgb_indices.append(0)

        # Build the composite
        height = len(ds.dimensions["y"])
        width = len(ds.dimensions["x"])
        composite = np.zeros((3, height, width), dtype=np.uint8)

        for channel, band_idx in enumerate(rgb_indices):
            band_data = ds.variables["digital_number"][band_idx, :, :]
            composite[channel, :, :] = self._scale_to_uint8(band_data)

        ds.variables["rgb_preview"][:, :, :] = composite

    def _write_backplanes(self) -> None:
        """Write az/el backplane variables in the PTU reference frame.

        Uses the encoder-derived pointing when available, the lens
        intrinsics from config, and the mount geometry (camera lever
        arm off the PTU axes) with the configured scene range for the
        parallax correction. Best-effort: missing configuration is
        logged and skips the backplanes rather than failing the file.
        """
        if self.backplanes == "none":
            return
        if self._lens_config is None or self.lens_id is None:
            logger.warning(
                "Backplanes skipped: lens_config and lens_id required"
            )
            return

        ds = self._dataset
        height = len(ds.dimensions["y"])
        width = len(ds.dimensions["x"])

        try:
            camera = camera_model_from_configs(
                self._lens_config, self.lens_id, width, height
            )
        except KeyError as e:
            logger.warning("Backplanes skipped: lens config missing %s", e)
            return
        mount = mount_model_from_config(self.mount_config)

        pan = (self.actual_pan_degrees
               if self.actual_pan_degrees is not None
               else self.pan_degrees)
        tilt = (self.actual_tilt_degrees
                if self.actual_tilt_degrees is not None
                else self.tilt_degrees)

        if self.backplanes == "full":
            rows = np.arange(height)
            cols = np.arange(width)
        else:
            step = self._BACKPLANE_SUBSAMPLE
            rows = np.unique(np.append(np.arange(0, height, step),
                                       height - 1))
            cols = np.unique(np.append(np.arange(0, width, step),
                                       width - 1))

        az, el = pixel_to_azel(
            camera, mount, pan, tilt,
            rows=rows, cols=cols, scene_range_m=self.scene_range_m,
        )

        ds.createDimension("backplane_y", len(rows))
        ds.createDimension("backplane_x", len(cols))

        by = ds.createVariable("backplane_y", np.int32, ("backplane_y",))
        by.long_name = "Image row index of each backplane node"
        by[:] = rows.astype(np.int32)
        bx = ds.createVariable("backplane_x", np.int32, ("backplane_x",))
        bx.long_name = "Image column index of each backplane node"
        bx[:] = cols.astype(np.int32)

        comment = (
            "Direction from the PTU pan/tilt axes intersection to the "
            "scene point seen by this pixel, in the PTU reference frame. "
            "Pinhole model with camera lever-arm parallax at the "
            "assumed scene range; interpolate bilinearly between nodes. "
            "See fli.geometry."
        )
        for name, data, long_name in (
            ("backplane_azimuth", az, "Azimuth in the PTU frame"),
            ("backplane_elevation", el, "Elevation in the PTU frame"),
        ):
            var = ds.createVariable(
                name, np.float32, ("backplane_y", "backplane_x"),
                zlib=True, complevel=self.compression_level,
            )
            var.units = "degrees"
            var.long_name = long_name
            var.comment = comment
            var.pointing_pan_degrees = np.float64(pan)
            var.pointing_tilt_degrees = np.float64(tilt)
            var.focal_length_mm = np.float64(camera.focal_length_mm)
            var.camera_offset_m = np.array(
                mount.camera_offset_m, dtype=np.float64
            )
            if self.scene_range_m is not None:
                var.scene_range_m = np.float64(self.scene_range_m)
            else:
                var.scene_range = "infinity"
            var[:, :] = data.astype(np.float32)

        logger.debug(
            "Backplanes written: %dx%d nodes (%s)",
            len(rows), len(cols), self.backplanes,
        )

    def finalize(self) -> None:
        """Finalize the NetCDF file.

        Builds the RGB preview composite, updates summary attributes,
        and closes the dataset. Safe to call multiple times (subsequent
        calls are no-ops).
        """
        if self._closed:
            return

        if self._dataset is None:
            self._closed = True
            return

        try:
            # Update summary attributes
            self._dataset.bands_captured = np.int32(self._band_count)

            if self._first_capture_time is not None and self._band_count > 0:
                last_time = self._dataset.variables["capture_time"][
                    self._band_count - 1
                ]
                first_time = self._dataset.variables["capture_time"][0]
                self._dataset.total_capture_time_s = np.float64(
                    float(last_time) - float(first_time)
                )

            # Build RGB preview and pointing backplanes
            if self._dimensions_created:
                self._build_rgb_preview()
                self._write_backplanes()

            self._dataset.sync()
        finally:
            self._dataset.close()
            self._dataset = None
            self._closed = True

        logger.info(
            "NetCDF finalized: %s (%d bands)", self.filepath, self._band_count
        )

    def close(self) -> None:
        """Close the dataset without building the RGB preview.

        Use ``finalize()`` for normal completion. This method is a safety
        net for error paths.
        """
        if self._closed:
            return

        if self._dataset is not None:
            try:
                self._dataset.bands_captured = np.int32(self._band_count)
                self._dataset.sync()
            except Exception:
                pass
            try:
                self._dataset.close()
            except Exception:
                pass
            self._dataset = None

        self._closed = True

    def __enter__(self) -> "MultispectralNetCDF":
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        """Context manager exit — finalizes on clean exit, closes on error."""
        if exc_type is None:
            self.finalize()
        else:
            self.close()
        return False

    def __del__(self):
        """Safety net: close dataset if not already closed."""
        if not self._closed:
            self.close()
