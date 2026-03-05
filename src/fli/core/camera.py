"""
 FLI.camera.py 
 
 Object-oriented interface for handling FLI USB cameras
 
 author:       Craig Wm. Versek, Yankee Environmental Systems 
 author_email: cwv@yesinc.com
"""

__author__ = 'Craig Wm. Versek'
__date__ = '2012-08-08'

import sys, time, warnings, traceback

try:
    from collections import OrderedDict
except ImportError:
    from odict import OrderedDict

from ctypes import pointer, POINTER, byref, sizeof, Structure, c_char,\
                   c_char_p, c_long, c_ubyte, c_uint8, c_uint16, c_double, \
                   create_string_buffer, c_size_t, c_void_p, cast

import numpy

from .lib import FLILibrary, FLIError, FLIWarning, flidomain_t, flidev_t,\
                fliframe_t, flibitdepth_t, flishutter_t, FLIDOMAIN_USB, FLIDEVICE_CAMERA,\
                FLI_FRAME_TYPE_NORMAL, FLI_FRAME_TYPE_DARK,\
                FLI_FRAME_TYPE_RBI_FLUSH, FLI_MODE_8BIT, FLI_MODE_16BIT,\
                FLI_TEMPERATURE_CCD, FLI_TEMPERATURE_BASE,\
                FLI_CAMERA_STATUS_IDLE, FLI_CAMERA_STATUS_EXPOSING,\
                FLI_CAMERA_STATUS_READING_CCD, FLI_CAMERA_STATUS_WAITING_FOR_TRIGGER,\
                FLI_CAMERA_STATUS_MASK,\
                FLI_SHUTTER_CLOSE, FLI_SHUTTER_OPEN

from .device import USBDevice
###############################################################################
DEBUG = False
DEFAULT_BITDEPTH = '16bit'
###############################################################################
class USBCamera(USBDevice):
    #load the DLL
    _libfli = FLILibrary.getDll(debug=DEBUG)
    _domain = flidomain_t(FLIDOMAIN_USB | FLIDEVICE_CAMERA)
    
    def __init__(self, dev_name, model, bitdepth = DEFAULT_BITDEPTH):
        USBDevice.__init__(self, dev_name = dev_name, model = model)
        self.hbin  = 1
        self.vbin  = 1
        self.bitdepth = bitdepth
        self.current_exposure_ms = 0  # Track current exposure time

    def get_info(self):
        info = OrderedDict()
        tmp1, tmp2, tmp3, tmp4   = (c_long(),c_long(),c_long(),c_long())
        d1, d2                   = (c_double(),c_double())        
        info['serial_number'] = self.get_serial_number()
        self._libfli.FLIGetHWRevision(self._dev, byref(tmp1))
        info['hardware_rev'] = tmp1.value
        self._libfli.FLIGetFWRevision(self._dev, byref(tmp1))
        info['firmware_rev'] = tmp1.value
        self._libfli.FLIGetPixelSize(self._dev, byref(d1), byref(d2))
        info['pixel_size'] = (d1.value,d2.value)
        self._libfli.FLIGetArrayArea(self._dev, byref(tmp1), byref(tmp2), byref(tmp3), byref(tmp4))
        info['array_area'] = (tmp1.value,tmp2.value,tmp3.value,tmp4.value)
        self._libfli.FLIGetVisibleArea(self._dev, byref(tmp1), byref(tmp2), byref(tmp3), byref(tmp4))
        info['visible_area'] = (tmp1.value,tmp2.value,tmp3.value,tmp4.value)        
        return info
        
    def get_camera_mode_string(self):
        #("FLIGetCameraModeString", [flidev_t, flimode_t, c_char_p, c_size_t]),
        #(flidev_t dev, flimode_t mode_index, char *mode_string, size_t siz);
        buff_size = 32
        mode_string = create_string_buffer(b"",buff_size)
        mode_index = self.get_camera_mode()
        self._libfli.FLIGetCameraModeString(self._dev, mode_index, mode_string, c_size_t(buff_size))
        return mode_string.value

    def get_camera_mode(self):
        #LIBFLIAPI FLIGetCameraMode(flidev_t dev, flimode_t *mode_index);
        mode_index = c_long(-1)
        self._libfli.FLIGetCameraMode(self._dev, byref(mode_index))
        return mode_index

    def set_camera_mode(self, mode_index):
        #LIBFLIAPI FLIGetCameraMode(flidev_t dev, flimode_t *mode_index);
        index = c_long(mode_index)
        self._libfli.FLISetCameraMode(self._dev, index)

    def get_image_size(self):
        "returns (row_width, img_rows, img_size)"
        left, top, right, bottom   = (c_long(),c_long(),c_long(),c_long())        
        result = self._libfli.FLIGetVisibleArea(self._dev, byref(left), byref(top), byref(right), byref(bottom))
        if result != 0:
            raise FLIError(f"FLIGetVisibleArea failed: {result}")
        row_width = (right.value - left.value)//self.hbin
        img_rows  = (bottom.value - top.value)//self.vbin
        img_size = img_rows * row_width * sizeof(c_uint16)
        return (row_width, img_rows, img_size)

    def set_image_area(self, ul_x, ul_y, lr_x, lr_y):
        #FIXME does this API call actually do anything?
        left, top, right, bottom   = (c_long(ul_x),c_long(ul_y),c_long(lr_x),c_long(lr_y))
        row_width = (right.value - left.value)/self.hbin
        img_rows  = (bottom.value - top.value)/self.vbin
        self._libfli.FLISetImageArea(self._dev, left, top, c_long(left.value + row_width), c_long(top.value + img_rows))

    def set_image_binning(self, hbin = 1, vbin = 1):
        left, top, right, bottom   = (c_long(),c_long(),c_long(),c_long())        
        result = self._libfli.FLIGetVisibleArea(self._dev, byref(left), byref(top), byref(right), byref(bottom))
        if result != 0:
            raise FLIError(f"FLIGetVisibleArea failed: {result}")
        row_width = (right.value - left.value)//hbin
        img_rows  = (bottom.value - top.value)//vbin
        result = self._libfli.FLISetImageArea(self._dev, left, top, c_long(left.value + row_width), c_long(top.value + img_rows))
        if result != 0:
            raise FLIError(f"FLISetImageArea failed: {result}")
        result = self._libfli.FLISetHBin(self._dev, c_long(hbin))
        if result != 0:
            raise FLIError(f"FLISetHBin failed: {result}")
        result = self._libfli.FLISetVBin(self._dev, c_long(vbin))
        if result != 0:
            raise FLIError(f"FLISetVBin failed: {result}")
        self.hbin = hbin
        self.vbin = vbin
    
    def set_flushes(self, num):
        """set the number of flushes to the CCD before taking exposure
           
           must have 0 <= num <= 16, else raises ValueError
        """
        if not(0 <= num <= 16):
            raise ValueError("must have 0 <= num <= 16")
        result = self._libfli.FLISetNFlushes(self._dev, c_long(num))
        if result != 0:
            raise FLIError(f"FLISetNFlushes failed: {result}")

    def set_temperature(self, T):
        "set the camera's temperature target in degrees Celcius"
        self._libfli.FLISetTemperature(self._dev, c_double(T))
                
    def get_temperature(self):
        "gets the camera's temperature in degrees Celcius"
        T = c_double()         
        self._libfli.FLIGetTemperature(self._dev, byref(T))
        return T.value
        
    def read_CCD_temperature(self):
        "gets the CCD's temperature in degrees Celcius"
        T = c_double()         
        self._libfli.FLIReadTemperature(self._dev, FLI_TEMPERATURE_CCD, byref(T))
        return T.value
        
    def read_base_temperature(self):
        "gets the cooler's hot side in degrees Celcius"
        T = c_double()         
        self._libfli.FLIReadTemperature(self._dev, FLI_TEMPERATURE_BASE, byref(T))
        return T.value
        
    def get_cooler_power(self):
        "gets the cooler's power in watts (undocumented API function)"
        P = c_double()
        self._libfli.FLIGetCoolerPower(self._dev, byref(P))
        return P.value

    def control_shutter(self, open_shutter):
        """Manually control the mechanical shutter.

        Args:
            open_shutter: True to open shutter, False to close it.

        Returns:
            int: 0 on success, error code on failure.

        Note:
            This bypasses the firmware's automatic shutter control which has
            significant timing overhead (~20-30ms). For accurate short exposures,
            pre-open the shutter before starting the exposure and use DARK frame
            type to prevent firmware from cycling the shutter.
        """
        shutter_cmd = flishutter_t(FLI_SHUTTER_OPEN if open_shutter else FLI_SHUTTER_CLOSE)
        result = self._libfli.FLIControlShutter(self._dev, shutter_cmd)
        if result != 0:
            raise FLIError(f"FLIControlShutter failed: {result}", result)
        return result

    def wait_for_idle(self, timeout_seconds=10):
        """Wait for camera to return to idle status before next operation.
        
        Args:
            timeout_seconds: Maximum time to wait for idle status
            
        Returns:
            bool: True if camera reached idle status, False if timeout
        """
        start_time = time.time()
        
        while time.time() - start_time < timeout_seconds:
            try:
                status = self.get_camera_status()
                if status == FLI_CAMERA_STATUS_IDLE:
                    return True
                # Sleep briefly before checking again
                time.sleep(0.1)
            except Exception:
                # If status check fails, wait a bit more and continue
                time.sleep(0.2)
                continue
        
        # Timeout reached
        if DEBUG:
            print(f"⚠️  Camera did not return to idle within {timeout_seconds}s")
        return False

    def set_exposure(self, exptime, frametype = "normal"):
        """setup the exposure type:
               exptime   - exposure time in milliseconds 
               frametype -  'normal'     - open shutter exposure
                            'dark'       - exposure with shutter closed
                            'rbi_flush'  - flood CCD with internal light, with shutter closed
        """
        # Wait for camera to be idle before changing exposure settings
        if not self.wait_for_idle(timeout_seconds=15):
            if DEBUG:
                print(f"⚠️  Warning: Camera not idle when setting exposure, continuing anyway")
        
        self.current_exposure_ms = exptime  # Store the exposure time
        exptime = c_long(exptime)        
        if frametype == "normal":
            frametype = fliframe_t(FLI_FRAME_TYPE_NORMAL)
        elif frametype == "dark":
            frametype = fliframe_t(FLI_FRAME_TYPE_DARK)
        elif frametype == "rbi_flush":
            #FIXME note: FLI_FRAME_TYPE_RBI_FLUSH = FLI_FRAME_TYPE_FLOOD | FLI_FRAME_TYPE_DARK
            # is this always the correct mode?
            frametype = fliframe_t(FLI_FRAME_TYPE_RBI_FLUSH)
        else:
            raise ValueError("'frametype' must be either 'normal','dark' or 'rbi_flush'")
        result = self._libfli.FLISetExposureTime(self._dev, exptime)
        if result != 0:
            raise FLIError(f"FLISetExposureTime failed: {result}")
        result = self._libfli.FLISetFrameType(self._dev, frametype)
        if result != 0:
            raise FLIError(f"FLISetFrameType failed: {result}")

    def set_bitdepth(self, bitdepth):
        """Set the camera bit depth for image capture.
        
        Args:
            bitdepth: Either '8bit' or '16bit'
            
        Note:
            Many USB cameras do not support changing bit depth and will
            raise a warning if the operation is not supported.
        """
        if bitdepth == '8bit':
            bitdepth_val = FLI_MODE_8BIT
        elif bitdepth == '16bit':
            bitdepth_val = FLI_MODE_16BIT
        else:
            raise ValueError("'bitdepth' must be either '8bit' or '16bit'")
        
        try:
            self._libfli.FLISetBitDepth(self._dev, bitdepth_val)
            self.bitdepth = bitdepth
            print(f"✅ Bit depth set to {bitdepth}")
        except FLIError as e:
            msg = f"Camera does not support changing bit depth: {e}"
            warnings.warn(FLIWarning(msg, e))
            # Keep the current bit depth setting
            print(f"⚠️  Bit depth change failed, keeping current setting: {self.bitdepth}")

    def take_photo(self):
        """Expose the frame, wait for completion, and fetch the image data.

        Takes full manual control of the mechanical shutter to eliminate
        firmware shutter timing overhead. The firmware's automatic shutter
        control adds ~20-30ms of transit time to every exposure, which
        causes partial occlusion — especially visible at short exposures
        but present at all durations.

        By using DARK frame type, the firmware leaves the shutter alone
        and all shutter timing is controlled explicitly here.

        The sequence is:
        1. Open shutter manually
        2. Wait 25ms for shutter to fully open
        3. Start DARK exposure (firmware does not cycle shutter)
        4. Wait for exposure and readout
        5. Fetch image data
        6. Close shutter manually
        """
        SHUTTER_PREOPEN_DELAY_MS = 25  # Time for shutter to fully open

        # Ensure camera is idle before starting exposure
        if not self.wait_for_idle(timeout_seconds=15):
            if DEBUG:
                print(f"⚠️  Warning: Camera not idle when starting exposure, continuing anyway")

        # Override frame type to DARK so firmware does not touch the shutter
        result = self._libfli.FLISetFrameType(
            self._dev, fliframe_t(FLI_FRAME_TYPE_DARK)
        )
        if result != 0:
            raise FLIError(f"FLISetFrameType failed: {result}")

        # Manual shutter control for accurate exposure timing
        self.control_shutter(open_shutter=True)
        time.sleep(SHUTTER_PREOPEN_DELAY_MS / 1000.0)

        try:
            self.start_exposure()
            # Wait for complete exposure and readout cycle using state polling
            self.wait_for_exposure_and_readout_completion()
            # Grab the image
            image = self.fetch_image()
        finally:
            # Always close shutter after exposure, even on error
            self.control_shutter(open_shutter=False)

        # Ensure camera returns to idle after image fetch
        if not self.wait_for_idle(timeout_seconds=5):
            if DEBUG:
                print(f"⚠️  Warning: Camera not idle after image fetch")

        return image

    def take_dark(self):
        """Take a dark frame with the shutter closed.

        Uses the standard firmware-controlled DARK frame type without manual
        shutter control. The firmware keeps the shutter closed throughout
        the exposure to capture bias + dark current without any light signal.

        Returns:
            numpy.ndarray: The dark frame image data.
        """
        # Ensure camera is idle before starting exposure
        if not self.wait_for_idle(timeout_seconds=15):
            if DEBUG:
                print(f"⚠️  Warning: Camera not idle when starting dark exposure, continuing anyway")

        # Set DARK frame type - firmware handles shutter (keeps it closed)
        result = self._libfli.FLISetFrameType(self._dev, fliframe_t(FLI_FRAME_TYPE_DARK))
        if result != 0:
            raise FLIError(f"FLISetFrameType failed: {result}", result)

        self.start_exposure()
        # Wait for complete exposure and readout cycle using state polling
        self.wait_for_exposure_and_readout_completion()
        # Grab the image
        image = self.fetch_image()

        # Ensure camera returns to idle after image fetch
        if not self.wait_for_idle(timeout_seconds=5):
            if DEBUG:
                print(f"⚠️  Warning: Camera not idle after dark image fetch")

        return image

    def start_exposure(self):
        """ Begin the exposure and return immediately.
            Use the method  'get_timeleft' to check the exposure progress 
            until it returns 0, then use method 'fetch_image' to fetch the image
            data as a numpy array.
        """
        result = self._libfli.FLIExposeFrame(self._dev)
        if result != 0:
            raise FLIError(f"FLIExposeFrame failed: {result}")
        
    def get_exposure_timeleft(self):
        """ Returns the time left on the exposure in milliseconds.
        """
        timeleft = c_long()
        result = self._libfli.FLIGetExposureStatus(self._dev,byref(timeleft))
        if result != 0:
            raise FLIError(f"FLIGetExposureStatus failed: {result}")
        return timeleft.value
    
    def get_camera_status(self):
        """ Returns the current camera status.
        """
        status = c_long()
        result = self._libfli.FLIGetDeviceStatus(self._dev, byref(status))
        if result != 0:
            raise FLIError(f"FLIGetDeviceStatus failed: {result}")
        return status.value & FLI_CAMERA_STATUS_MASK
    
    def wait_for_exposure_and_readout_completion(self):
        """ Wait for complete exposure and CCD readout cycle to finish.
            Uses conservative timing based on measured readout performance and proper state polling.
        """
        start_time = time.time()
        
        # Conservative timing based on measurements: readout takes ~350ms with 150ms safety buffer
        CONSERVATIVE_READOUT_TIME_MS = 350  # Conservative estimate from timing tests
        READOUT_SAFETY_BUFFER_MS = 150      # Safety margin for USB variations
        
        wait_time = 0.9 * (self.current_exposure_ms + CONSERVATIVE_READOUT_TIME_MS + READOUT_SAFETY_BUFFER_MS) / 1000.0
        time.sleep(wait_time)

        max_wait_time = wait_time+30

        # Phase 2: Smart polling based on camera state
        poll_count = 0
        last_status = None
        readout_start_time = None
        
        while True:
            elapsed = time.time() - start_time
            if elapsed > max_wait_time:
                raise FLIError(f"Exposure timeout: exceeded {max_wait_time:.1f}s waiting for completion")
            
            status = self.get_camera_status()
            
            # Log status changes for debugging
            if status != last_status:
                status_names = {
                    FLI_CAMERA_STATUS_IDLE: "IDLE",
                    FLI_CAMERA_STATUS_WAITING_FOR_TRIGGER: "WAITING_FOR_TRIGGER", 
                    FLI_CAMERA_STATUS_EXPOSING: "EXPOSING",
                    FLI_CAMERA_STATUS_READING_CCD: "READING_CCD"
                }
                if DEBUG:
                    print(f"   Camera status: {status_names.get(status, f'UNKNOWN({status})')}")
                
                # Track when readout phase begins
                if status == FLI_CAMERA_STATUS_READING_CCD and readout_start_time is None:
                    readout_start_time = time.time()
                
                last_status = status
            
            if status == FLI_CAMERA_STATUS_IDLE:
                # Camera has completed exposure and readout
                break
            elif status == FLI_CAMERA_STATUS_EXPOSING:
                # Still exposing - wait longer between polls
                time.sleep(0.2)
                poll_count += 1
            elif status == FLI_CAMERA_STATUS_READING_CCD:
                # Reading CCD - use conservative readout timing 
                if readout_start_time:
                    readout_elapsed = (time.time() - readout_start_time) * 1000  # Convert to ms
                    if readout_elapsed < CONSERVATIVE_READOUT_TIME_MS * 0.8:  # Still early in readout
                        time.sleep(0.1)  # Wait during most of readout
                    else:
                        time.sleep(0.05)  # More frequent polling near completion
                else:
                    time.sleep(0.1)
                poll_count += 1
            elif status == FLI_CAMERA_STATUS_WAITING_FOR_TRIGGER:
                # Waiting for trigger - check frequently
                time.sleep(0.05)
                poll_count += 1
            else:
                # Unknown status - wait briefly and continue
                time.sleep(0.05)
                poll_count += 1
            
            # Safety check to prevent infinite polling
            if poll_count > 2000:  # Increased limit for more robust operation
                raise FLIError(f"Excessive polling: camera stuck in status {status}")
    
    def fetch_image(self):
        """ Fetch the image data for the last exposure.
            Returns a numpy.ndarray object.
            Fixed to match working ctypes implementation.
        """
        row_width, img_rows, _  = self.get_image_size()
        
        # Ensure dimensions are integers (explicit conversion like working ctypes)
        row_width = int(row_width)
        img_rows = int(img_rows)
        
        #use bit depth to determine array data type
        img_array_dtype = None
        img_ptr_ctype   = None
        if self.bitdepth == '8bit':
            img_array_dtype = numpy.uint8
            img_ptr_ctype = c_uint8
        elif self.bitdepth == '16bit':
            img_array_dtype = numpy.uint16
            img_ptr_ctype = c_uint16
        else:
            raise FLIError("'bitdepth' must be either '8bit' or '16bit'")
        #allocate numpy array to store image
        img_array = numpy.zeros((img_rows, row_width), dtype=img_array_dtype)
        #get pointer to array's data space
        img_ptr   = img_array.ctypes.data_as(POINTER(img_ptr_ctype))
        
        #grab image buff row by row with proper error checking and types
        for row in range(img_rows):
            offset = row * row_width * sizeof(img_ptr_ctype)
            
            result = self._libfli.FLIGrabRow(self._dev, byref(img_ptr.contents, offset), c_size_t(row_width))
            if result != 0:
                raise FLIError(f"FLIGrabRow failed on row {row}: error {result}")
        
        # Post-acquisition buffer wait and idle verification
        self.wait_post_acquisition_buffer()
        
        return img_array
    
    def wait_post_acquisition_buffer(self):
        """ Wait for the readout buffer time after image acquisition, then verify idle state.
            This prevents stepping on unfinished frames in rapid acquisition scenarios.
        """
        READOUT_SAFETY_BUFFER_MS = 150  # Conservative buffer from timing measurements

        if DEBUG:
            print(f"   Waiting {READOUT_SAFETY_BUFFER_MS}ms post-acquisition buffer...")

        # Wait the buffer time
        time.sleep(READOUT_SAFETY_BUFFER_MS / 1000.0)

        # Verify camera is truly idle after buffer wait
        max_idle_wait = 2.0  # Max 2 seconds to reach idle after buffer
        if not self.wait_for_idle(timeout_seconds=max_idle_wait):
            if DEBUG:
                print(f"⚠️  Warning: Camera not idle after {READOUT_SAFETY_BUFFER_MS}ms buffer + {max_idle_wait}s wait")
        else:
            if DEBUG:
                print(f"   ✅ Camera confirmed idle after buffer wait")

    # =========================================================================
    # Video Mode Methods
    # =========================================================================

    def start_video_mode(self):
        """Start continuous video streaming mode.

        Video mode enables faster frame acquisition by keeping the camera
        in a continuous readout state. Use grab_video_frame() to retrieve
        frames, and stop_video_mode() when finished.

        Note:
            Exposure time should be set with set_exposure() before starting
            video mode. The camera will continuously expose and read frames
            at the set exposure time.
        """
        result = self._libfli.FLIStartVideoMode(self._dev)
        if result != 0:
            raise FLIError(f"FLIStartVideoMode failed: {result}")
        if DEBUG:
            print("✅ Video mode started")

    def stop_video_mode(self):
        """Stop video streaming mode and return to normal operation.

        Should be called when finished capturing video frames to return
        the camera to its normal state for still image capture.
        """
        result = self._libfli.FLIStopVideoMode(self._dev)
        if result != 0:
            raise FLIError(f"FLIStopVideoMode failed: {result}")
        if DEBUG:
            print("✅ Video mode stopped")

    def grab_video_frame(self):
        """Grab a single frame from the video stream.

        Must be called after start_video_mode(). Returns a numpy array
        containing the image data.

        Returns:
            numpy.ndarray: The captured frame as a 2D array.

        Raises:
            FLIError: If frame grab fails.
        """
        row_width, img_rows, img_size = self.get_image_size()

        # Ensure dimensions are integers
        row_width = int(row_width)
        img_rows = int(img_rows)
        img_size = int(img_size)

        # Use bit depth to determine array data type
        if self.bitdepth == '8bit':
            img_array_dtype = numpy.uint8
            img_ptr_ctype = c_uint8
        elif self.bitdepth == '16bit':
            img_array_dtype = numpy.uint16
            img_ptr_ctype = c_uint16
        else:
            raise FLIError("'bitdepth' must be either '8bit' or '16bit'")

        # Allocate numpy array to store image
        img_array = numpy.zeros((img_rows, row_width), dtype=img_array_dtype)

        # Get pointer to array's data space as void pointer
        img_ptr = img_array.ctypes.data_as(POINTER(img_ptr_ctype))

        # Grab entire frame at once (video mode grabs full frame, not row-by-row)
        void_ptr = cast(img_ptr, c_void_p)

        result = self._libfli.FLIGrabVideoFrame(self._dev, void_ptr, c_size_t(img_size))
        if result != 0:
            raise FLIError(f"FLIGrabVideoFrame failed: {result}")

        return img_array

###############################################################################
#  TEST CODE
###############################################################################
if __name__ == "__main__":
    cams = USBCamera.find_devices()
    cam0 = cams[0]
    print("info:", cam0.get_info())
    print("image size:", cam0.get_image_size())
    print("temperature:", cam0.get_temperature())
    print("mode:", cam0.get_camera_mode_string())
    cam0.set_image_binning(2,2)
    # cam0.set_bitdepth("16bit") #this should generate a warning for any USB camera in libfli-1.104
    cam0.set_exposure(5)
    img = cam0.take_photo()
    print(img)
