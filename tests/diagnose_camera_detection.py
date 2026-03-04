#!/usr/bin/env python3
"""
Camera Detection Diagnostic Tool

This tool diagnoses camera detection issues by examining the
device information that's being read during connection.

Author: Generated for FLI Camera USB Debug
"""

import sys
import ctypes

from fli.core.lib import FLILibrary, FLIDOMAIN_USB, FLIDEVICE_CAMERA, FLIDEBUG_INFO, FLIDEBUG_WARN, FLIDEBUG_FAIL, FLIDEBUG_IO, flidomain_t, flidev_t, FLIError

def diagnose_camera_detection():
    """Diagnose camera detection and device information"""
    print("FLI Camera Detection Diagnostic")
    print("=" * 50)
    
    try:
        # Enable maximum debug output
        dll = FLILibrary.getDll(debug=True)
        debug_level = (FLIDEBUG_INFO | FLIDEBUG_WARN |
                      FLIDEBUG_FAIL | FLIDEBUG_IO)
        dll.FLISetDebugLevel(None, debug_level)
        
        print("1. Enumerating USB devices...")
        
        # Enumerate devices using low-level call
        dom = flidomain_t(FLIDOMAIN_USB | FLIDEVICE_CAMERA)
        names_ptr = ctypes.POINTER(ctypes.c_char_p)()
        
        result = dll.FLIList(dom, ctypes.byref(names_ptr))
        if result != 0:
            print(f"   ERROR: FLIList failed with code {result}")
            return False
            
        # Count devices
        device_count = 0
        if names_ptr:
            while names_ptr[device_count]:
                device_name = names_ptr[device_count].decode('utf-8')
                print(f"   Found device: {device_name}")
                device_count += 1
                
        if device_count == 0:
            print("   No USB cameras found")
            return False
            
        print(f"   Total devices found: {device_count}")
        
        # Try to connect to each device to find the camera
        camera_found = False
        for i in range(device_count):
            device_name = names_ptr[i].decode('utf-8')
            print(f"\n2.{i+1} Attempting connection to: {device_name}")
            
            # Skip if this looks like a filter wheel
            if "Filter Wheel" in device_name:
                print(f"   SKIP: This appears to be a filter wheel, not a camera")
                continue
                
            dev = flidev_t()
            result = dll.FLIOpen(ctypes.byref(dev), device_name.encode('utf-8'), dom)
            
            if result != 0:
                error_msg = FLIError.get_error_string(result)
                print(f"   ERROR: FLIOpen failed with code {result}: {error_msg}")
                
                # Check if it's the ENODEV error we're seeing
                if result == -19:
                    print(f"   This is ENODEV - likely camera type not in knowndev table")
                    print(f"   or firmware version compatibility issue")
                continue
            else:
                print("   SUCCESS: Camera opened successfully!")
                camera_found = True
                
                # Get device information
                print(f"\n3. Reading device information...")
                
                # Get hardware info
                hwrev = ctypes.c_long()
                result = dll.FLIGetHWRev(dev, ctypes.byref(hwrev))
                if result == 0:
                    print(f"   Hardware revision: 0x{hwrev.value:04x}")
                else:
                    print(f"   Hardware revision: ERROR {result}")
                    
                # Get firmware info  
                fwrev = ctypes.c_long()
                result = dll.FLIGetFWRev(dev, ctypes.byref(fwrev))
                if result == 0:
                    print(f"   Firmware revision: 0x{fwrev.value:04x}")
                    
                    # Check if this is the firmware compatibility issue
                    if fwrev.value < 0x0201:
                        print(f"   → OLD firmware (< 0x0201) - needs knowndev table lookup")
                    else:
                        print(f"   → NEW firmware (>= 0x0201) - should work without knowndev")
                else:
                    print(f"   Firmware revision: ERROR {result}")
                    
                # Get model info
                model_buf = ctypes.create_string_buffer(256)
                result = dll.FLIGetModel(dev, model_buf, 256)
                if result == 0:
                    print(f"   Model: {model_buf.value.decode('utf-8')}")
                else:
                    print(f"   Model: ERROR {result}")
                    
                # Get serial number
                serial = ctypes.c_long()
                result = dll.FLIGetSerialString(dev, model_buf, 256)
                if result == 0:
                    print(f"   Serial: {model_buf.value.decode('utf-8')}")
                else:
                    print(f"   Serial: ERROR {result}")
                    
                # Close device
                dll.FLIClose(dev)
                break
                
        if not camera_found:
            print(f"\n3. No cameras could be opened")
            print(f"   Found {device_count} USB devices but none could be opened as cameras")
            return False
            
        return True
            
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False

def main():
    """Main function"""
    success = diagnose_camera_detection()
    
    print(f"\n{'=' * 50}")
    if success:
        print("DIAGNOSIS: Camera detection working correctly")
        print("The USB implementation and device recognition are functional.")
    else:
        print("DIAGNOSIS: Camera detection failed")
        print("Check the debug output above for specific error details.")
        print("\nCommon issues:")
        print("- Camera firmware too old, device type not in knowndev table")
        print("- USB connection issues")
        print("- Device permissions")
        
    return 0 if success else 1

if __name__ == "__main__":
    sys.exit(main())