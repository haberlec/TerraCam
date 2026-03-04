#!/usr/bin/env python3
"""
Quick USB Test for FLI Camera

Simple test to quickly verify USB functionality and detect zero-frame issues.

Author: Generated for FLI Camera USB Testing
"""

import sys
import time
import ctypes
import numpy as np

from fli.core.lib import FLILibrary, FLIDOMAIN_USB, FLIDEVICE_CAMERA, FLIDEBUG_INFO, FLIDEBUG_WARN, FLIDEBUG_FAIL, FLI_SHUTTER_OPEN, FLI_SHUTTER_CLOSE, flidomain_t
from fli.core.camera import USBCamera

def quick_usb_test():
    """Quick test to verify USB camera functionality"""
    print("FLI Camera Quick USB Test")
    print("=" * 40)
    
    try:
        # Enable debug output
        dll = FLILibrary.getDll(debug=True)
        dll.FLISetDebugLevel(None, FLIDEBUG_INFO | FLIDEBUG_WARN | FLIDEBUG_FAIL)
        
        # Find cameras using the working method from stable_camera.py
        print("1. Finding USB cameras...")
        dom = flidomain_t(FLIDOMAIN_USB | FLIDEVICE_CAMERA)
        names_ptr = ctypes.POINTER(ctypes.c_char_p)()
        
        result = dll.FLIList(dom, ctypes.byref(names_ptr))
        if result != 0:
            print("   ✗ Failed to enumerate cameras!")
            return False
            
        # Find MicroLine camera
        cam = None
        device_count = 0
        if names_ptr:
            while names_ptr[device_count]:
                device_info = names_ptr[device_count].decode('utf-8')
                if "MicroLine" in device_info and "ML" in device_info:
                    # Split device info properly
                    device_id, model = device_info.split(';', 1)
                    print(f"   ✓ Found camera: {model} (ID: {device_id})")
                    
                    # Create camera with proper device ID
                    cam = USBCamera(device_id.encode(), model.encode())
                    break
                device_count += 1
                
        if not cam:
            print("   ✗ No MicroLine cameras found!")
            return False
        
        # Get basic info
        print("\n2. Camera information...")
        try:
            image_size = cam.get_image_size()
            temperature = cam.get_temperature()
            mode = cam.get_camera_mode_string()
            
            print(f"   Image size: {image_size}")
            print(f"   Temperature: {temperature}°C")
            print(f"   Mode: {mode}")
        except Exception as e:
            print(f"   ⚠ Warning getting camera info: {e}")
            
        # Test frame acquisition
        print("\n3. Testing frame acquisition...")
        cam.set_image_binning(2, 2)  # Use 2x2 binning for faster test
        cam.set_exposure(100)  # 100ms exposure (milliseconds)
        
        test_results = []
        
        for i in range(3):
            print(f"   Frame {i+1}/3: ", end="")
            
            start_time = time.time()
            
            try:
                cam._libfli.FLIControlShutter(cam._dev, FLI_SHUTTER_OPEN)
                frame = cam.take_photo()
                cam._libfli.FLIControlShutter(cam._dev, FLI_SHUTTER_CLOSE)
                
                duration = time.time() - start_time
                
                if frame is None:
                    print("✗ No frame data returned")
                    test_results.append(False)
                    continue
                    
                # Convert to numpy for analysis
                if hasattr(frame, 'shape'):
                    data = frame
                else:
                    data = np.array(frame)
                    
                # Quick analysis
                zero_count = np.count_nonzero(data == 0)
                zero_percent = (zero_count / data.size) * 100
                
                result = {
                    'duration': duration,
                    'shape': data.shape,
                    'min': np.min(data),
                    'max': np.max(data),
                    'mean': np.mean(data),
                    'zero_percent': zero_percent
                }
                
                if zero_percent > 90:
                    print(f"✗ MOSTLY ZEROS ({zero_percent:.1f}% zeros) - USB ISSUE!")
                    test_results.append(False)
                elif zero_percent > 50:
                    print(f"⚠ Many zeros ({zero_percent:.1f}% zeros) - Possible issue")
                    test_results.append(False)
                else:
                    print(f"✓ OK ({duration:.2f}s, {zero_percent:.1f}% zeros, range: {result['min']}-{result['max']})")
                    test_results.append(True)
                    
            except Exception as e:
                print(f"✗ ERROR: {e}")
                test_results.append(False)
                
        # Summary
        print(f"\n4. Test Summary:")
        success_count = sum(test_results)
        total_tests = len(test_results)
        
        print(f"   Successful frames: {success_count}/{total_tests}")
        
        if success_count == total_tests:
            print("   ✓ All tests passed - USB appears to be working correctly!")
            return True
        elif success_count == 0:
            print("   ✗ All tests failed - USB transfers are not working!")
            print("   This indicates the zero-frame issue is still present.")
            return False
        else:
            print(f"   ⚠ Partial success - {total_tests - success_count} frames failed")
            print("   This indicates intermittent USB issues.")
            return False
            
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        return False

def main():
    """Main function"""
    print("Starting quick USB test...\n")
    
    success = quick_usb_test()
    
    print(f"\n{'='*40}")
    if success:
        print("QUICK TEST RESULT: ✓ PASSED")
        print("The USB implementation appears to be working correctly.")
        print("\nTo run more comprehensive tests:")
        print("  python usb_analysis_test.py")
        print("  python usb_monitor.py")
    else:
        print("QUICK TEST RESULT: ✗ FAILED")
        print("USB issues detected. The zero-frame problem may still exist.")
        print("\nFor detailed analysis:")
        print("  python usb_analysis_test.py")
        print("  python usb_monitor.py")
        
    return 0 if success else 1

if __name__ == "__main__":
    sys.exit(main())