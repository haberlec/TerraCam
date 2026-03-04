"""
 FLI.device.py 
 
 Object-oriented base interface for handling FLI USB devices
 
 author:       Craig Wm. Versek, Yankee Environmental Systems 
 author_email: cwv@yesinc.com
"""

__author__ = 'Craig Wm. Versek'
__date__   = '2012-08-16'

import sys, time

import ctypes
from ctypes import pointer, POINTER, byref, c_char, c_char_p, c_long, c_ubyte,\
                   c_double, c_size_t

from .lib import FLILibrary, FLIError, FLIWarning, flidomain_t, flidev_t,\
                FLIDOMAIN_USB
###############################################################################
DEBUG = True
BUFFER_SIZE = 64
###############################################################################
class USBDevice(object):
    """ base class for all FLI USB devices"""
    #load the DLL
    _libfli = FLILibrary.getDll(debug=DEBUG)
    _domain = flidomain_t(FLIDOMAIN_USB)

    def __init__(self, dev_name, model):
        self.dev_name = dev_name
        self.model  = model
        #open the device
        self._dev = flidev_t()
        self._closed = False  # Track device state to prevent double-close
        # Ensure dev_name is encoded to bytes for C library
        if isinstance(dev_name, str):
            dev_name_bytes = dev_name.encode('utf-8')
        else:
            dev_name_bytes = dev_name
        result = self._libfli.FLIOpen(byref(self._dev), dev_name_bytes, self._domain)
        if result != 0:
            raise FLIError(f"FLIOpen failed: {result}")

    def __del__(self):
        self.close()

    def close(self):
        """Safely close the device, preventing double-close"""
        if not self._closed and hasattr(self, '_dev'):
            try:
                self._libfli.FLIClose(self._dev)
            except:
                pass  # Ignore errors during cleanup
            finally:
                self._closed = True
        
    def get_serial_number(self):
        serial = ctypes.create_string_buffer(BUFFER_SIZE)
        self._libfli.FLIGetSerialString(self._dev,serial,c_size_t(BUFFER_SIZE))
        return serial.value
    
    @classmethod
    def find_devices(cls):
        """locates all FLI USB devices in the current domain and returns a 
           list of USBDevice objects"""

        tmplist = POINTER(c_char_p)()
        cls._libfli.FLIList(cls._domain, byref(tmplist))      #allocates memory
        devs = []
        i = 0
        #process list only if it is not NULL
        if tmplist:
            while tmplist[i]: #process members only if they are not NULLG
                dev_name, model = tmplist[i].split(b";")
                devs.append(cls(dev_name=dev_name,model=model))   #create device objects
                i += 1
            cls._libfli.FLIFreeList(tmplist)                      #frees memory
        #finished processing list
        return devs

    @classmethod
    def locate_device(cls, serial_number):
        """locates the FLI USB devices in the current domain that matches the
           'serial_number' string
            
           returns None if no match is found

           raises FLIError if more than one device matching the serial_number 
                  is found, i.e., there is a conflict
        """
        dev_match = None
        devs = cls.find_devices()
        for dev in devs:
            dev_sn = dev.get_serial_number()
            if dev_sn == serial_number:       #match found
                if dev_match is None:         #first match
                    dev_match = dev
                else:                         #conflict
                    msg = "Device Conflict: there are more than one devices matching the serial_number '%s'" % serial_number
                    raise FLIError(msg)
        return dev_match
###############################################################################
#  TEST CODE
###############################################################################
if __name__ == "__main__":
    devs = USBDevice.find_devices()
