"""
 FLI.filter_wheel.py 
 
 Object-oriented interface for handling FLI (Finger Lakes Instrumentation)
 USB filter wheels
 
 author:       Craig Wm. Versek, Yankee Environmental Systems 
 author_email: cwv@yesinc.com
"""

__author__ = 'Craig Wm. Versek'
__date__ = '2012-08-16'

import sys, time

from ctypes import byref, c_char, c_char_p, c_long, c_ubyte, c_double

from .lib import FLILibrary, FLIError, FLIWarning, flidomain_t, flidev_t,\
                fliframe_t, FLIDOMAIN_USB, FLIDEVICE_FILTERWHEEL,\
                FLI_FILTER_STATUS_MOVING_CCW, FLI_FILTER_STATUS_MOVING_CW,\
                FLI_FILTER_STATUS_HOMING, FLI_FILTER_STATUS_HOME,\
                FLI_FILTER_STATUS_HOME_LEFT, FLI_FILTER_STATUS_HOME_RIGHT,\
                FLI_FILTER_STATUS_HOME_SUCCEEDED

from .device import USBDevice
###############################################################################
DEBUG = False

###############################################################################
class USBFilterWheel(USBDevice):
    #load the DLL
    _libfli = FLILibrary.getDll(debug=DEBUG)
    _domain = flidomain_t(FLIDOMAIN_USB | FLIDEVICE_FILTERWHEEL)
    
    def __init__(self, dev_name, model):
        USBDevice.__init__(self, dev_name = dev_name, model = model)

    def set_filter_pos(self, pos):      
        self._libfli.FLISetFilterPos(self._dev, c_long(pos))

    def get_filter_pos(self):
        pos = c_long()      
        self._libfli.FLIGetFilterPos(self._dev, byref(pos))
        return pos.value

    def get_filter_count(self):
        count = c_long()      
        self._libfli.FLIGetFilterCount(self._dev, byref(count))
        return count.value
    
    def get_status(self):
        """Get filter wheel status
        
        Returns:
            int: Raw status value from the device
        """
        status = c_long()
        self._libfli.FLIGetDeviceStatus(self._dev, byref(status))
        return status.value
    
    def get_status_string(self):
        """Get human-readable filter wheel status
        
        Returns:
            str: Human-readable status description
        """
        status = self.get_status()
        status_parts = []
        
        # Check movement status
        if status & FLI_FILTER_STATUS_MOVING_CCW:
            status_parts.append("Moving Counter-Clockwise")
        elif status & FLI_FILTER_STATUS_MOVING_CW:
            status_parts.append("Moving Clockwise")
        
        # Check homing status
        if status & FLI_FILTER_STATUS_HOMING:
            status_parts.append("Homing")
        
        # Check home position status
        if status & FLI_FILTER_STATUS_HOME:
            status_parts.append("At Home")
        if status & FLI_FILTER_STATUS_HOME_LEFT:
            status_parts.append("Home Left")
        if status & FLI_FILTER_STATUS_HOME_RIGHT:
            status_parts.append("Home Right")
        if status & FLI_FILTER_STATUS_HOME_SUCCEEDED:
            status_parts.append("Home Succeeded")
        
        # If no status flags are set, assume idle
        if not status_parts:
            status_parts.append("Idle")
        
        return ", ".join(status_parts)
    
    def is_moving(self):
        """Check if filter wheel is currently moving
        
        Returns:
            bool: True if moving, False if stationary
        """
        status = self.get_status()
        return bool(status & (FLI_FILTER_STATUS_MOVING_CCW | FLI_FILTER_STATUS_MOVING_CW))
    
    def is_homing(self):
        """Check if filter wheel is performing homing operation
        
        Returns:
            bool: True if homing, False otherwise
        """
        status = self.get_status()
        return bool(status & FLI_FILTER_STATUS_HOMING)
    
    def wait_for_movement_completion(self, timeout_seconds=30):
        """Wait for filter wheel movement to complete
        
        Args:
            timeout_seconds: Maximum time to wait for completion
            
        Returns:
            bool: True if movement completed, False if timed out
        """
        start_time = time.time()
        
        while time.time() - start_time < timeout_seconds:
            if not self.is_moving() and not self.is_homing():
                return True
            time.sleep(0.1)  # Poll every 100ms
        
        return False  # Timed out
    
   
        
###############################################################################
#  TEST CODE
###############################################################################
if __name__ == "__main__":
    fws = USBFilterWheel.find_devices()
    fw0 = fws[0]
