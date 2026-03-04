#!/usr/bin/env python3
"""
Debug Device ID Detection

This tool tries to identify exactly what device ID is being detected
and where the failure occurs in the camera open process.

Author: Generated for FLI Camera Debug
"""

import sys
import ctypes

from fli.core.lib import FLILibrary, FLIDOMAIN_USB, FLIDEVICE_CAMERA, FLIDEBUG_INFO, FLIDEBUG_WARN, FLIDEBUG_FAIL, FLIDEBUG_IO, flidomain_t, flidev_t, FLIError

def debug_device_id():
    """Debug device ID detection"""
    print("FLI Camera Device ID Debug")
    print("=" * 40)
    
    try:
        # Enable maximum debug output to see internal operations
        dll = FLILibrary.getDll(debug=True)
        debug_level = (FLIDEBUG_INFO | FLIDEBUG_WARN |
                      FLIDEBUG_FAIL | FLIDEBUG_IO)
        dll.FLISetDebugLevel(None, debug_level)
        
        print("Debug logging enabled - watch for USB/camera debug messages")
        
        # List devices
        dom = flidomain_t(FLIDOMAIN_USB | FLIDEVICE_CAMERA)
        names_ptr = ctypes.POINTER(ctypes.c_char_p)()
        
        result = dll.FLIList(dom, ctypes.byref(names_ptr))
        if result != 0:
            print(f"ERROR: FLIList failed: {result}")
            return False
            
        device_count = 0
        if names_ptr:
            while names_ptr[device_count]:
                device_name = names_ptr[device_count].decode('utf-8')
                if "MicroLine" in device_name:  # Focus on the camera
                    print(f"\nFound camera: {device_name}")
                    
                    # Split device info properly - use only the device name part!
                    if ';' in device_name:
                        device_id, model = device_name.split(';', 1)
                        print(f"  Device ID: {device_id}")
                        print(f"  Model: {model}")
                        use_name = device_id
                    else:
                        use_name = device_name
                    
                    # Try the low-level open to see what happens
                    print(f"Attempting FLIOpen with device ID: {use_name}")
                    print("Look for these debug messages:")
                    print("  - 'mac_usb_connect: connecting to usb device'")
                    print("  - Device ID detection")
                    print("  - Camera open process")
                    print("-" * 50)
                    
                    dev = flidev_t()
                    try:
                        result = dll.FLIOpen(ctypes.byref(dev), use_name.encode('utf-8'), dom)
                        print(f"FLIOpen result: {result}")
                        if result == 0:
                            print("SUCCESS: Camera opened!")
                            dll.FLIClose(dev)
                        else:
                            print(f"FAILED: Error code {result}")
                            
                    except Exception as e:
                        print(f"EXCEPTION: {e}")
                        
                    print("-" * 50)
                    break
                    
                device_count += 1
                
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        
    print("\nAnalysis:")
    print("Look at the debug output above to see:")
    print("1. Whether USB connection succeeds")
    print("2. What device ID gets detected") 
    print("3. Where exactly the failure occurs")
    print("4. Whether it's in USB, connection, or camera-specific code")

if __name__ == "__main__":
    debug_device_id()