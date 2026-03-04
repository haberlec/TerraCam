#!/usr/bin/env python3
"""
Saturation Test for FLI Camera

This test investigates whether long exposures are causing CCD saturation
leading to zero-value images, while short exposures work correctly.

Author: Generated for FLI Camera Saturation Analysis
"""

import sys
import time
import ctypes
import numpy as np

from fli.core.lib import FLILibrary, FLIDOMAIN_USB, FLIDEVICE_CAMERA, FLIDEBUG_INFO, FLIDEBUG_WARN, FLIDEBUG_FAIL, flidomain_t
from fli.core.camera import USBCamera

def test_saturation_hypothesis():
    """Test if zero-frame issue is due to CCD saturation from over-exposure"""
    print("=" * 60)
    print("FLI Camera Saturation Test")
    print("=" * 60)
    
    try:
        # Setup camera
        print("1. Connecting to camera...")
        dll = FLILibrary.getDll(debug=True)
        dll.FLISetDebugLevel(None, FLIDEBUG_INFO | FLIDEBUG_WARN | FLIDEBUG_FAIL)
        
        # Find camera
        dom = flidomain_t(FLIDOMAIN_USB | FLIDEVICE_CAMERA)
        names_ptr = ctypes.POINTER(ctypes.c_char_p)()
        
        result = dll.FLIList(dom, ctypes.byref(names_ptr))
        if result != 0:
            raise RuntimeError("Failed to enumerate cameras!")
            
        # Find MicroLine camera
        cam = None
        device_count = 0
        if names_ptr:
            while names_ptr[device_count]:
                device_info = names_ptr[device_count].decode('utf-8')
                if "MicroLine" in device_info and "ML" in device_info:
                    device_id, model = device_info.split(';', 1)
                    print(f"   Found camera: {model} (ID: {device_id})")
                    cam = USBCamera(device_id.encode(), model.encode())
                    break
                device_count += 1
                
        if not cam:
            raise RuntimeError("No MicroLine cameras found!")
            
        # Configure camera
        print("2. Configuring camera...")
        cam.set_flushes(5)  # Proper CCD flushing
        cam.set_image_binning(2, 2)  # 2x2 binning
        time.sleep(1.0)  # Allow settling
        
        # Test exposure range: 30ms to 2000ms in ~10 steps
        exposure_times = [30, 50, 75, 100, 150, 250, 400, 600, 1000, 2000]  # ms
        
        print("3. Testing exposure range for saturation patterns...")
        results = []
        
        for exp_time in exposure_times:
            print(f"\n   Testing {exp_time}ms exposure:")
            
            try:
                # Set exposure - NO manual shutter control
                cam.set_exposure(exp_time, "normal")  # Auto shutter
                time.sleep(0.5)  # Settling
                
                # Take image using proper sequence
                start_time = time.time()
                frame = cam.take_photo()  # Handles complete sequence
                acquisition_time = time.time() - start_time
                
                # Analyze frame
                result = analyze_saturation_pattern(frame, exp_time, acquisition_time)
                results.append(result)
                
                # Print immediate result
                status = "✓ GOOD" if result['success'] else "✗ FAILED"
                print(f"     {status}: Range {result['min_val']}-{result['max_val']}, "
                      f"Mean {result['mean_val']:.1f}, Zeros {result['zero_percent']:.1f}%")
                
                if result['saturated']:
                    print(f"     WARNING: Saturation detected! Max pixel value: {result['max_val']}")
                    
            except Exception as e:
                print(f"     ERROR: {e}")
                results.append({
                    'exposure_ms': exp_time,
                    'success': False,
                    'error': str(e)
                })
                
            # Brief recovery time
            time.sleep(0.5)
            
        # Analyze results for saturation patterns
        print_saturation_analysis(results)
        
        return True
        
    except Exception as e:
        print(f"Test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def analyze_saturation_pattern(frame, exposure_ms, acquisition_time):
    """Analyze frame for saturation indicators"""
    result = {
        'exposure_ms': exposure_ms,
        'acquisition_time': acquisition_time,
        'success': False,
        'saturated': False,
        'zero_percent': 100.0,
        'min_val': 0,
        'max_val': 0,
        'mean_val': 0.0,
        'std_val': 0.0,
        'issues': []
    }
    
    if frame is None:
        result['issues'].append("No frame data")
        return result
        
    try:
        # Convert to numpy for analysis
        if hasattr(frame, 'shape'):
            data = frame
        else:
            data = np.array(frame)
            
        result['shape'] = data.shape
        result['min_val'] = np.min(data)
        result['max_val'] = np.max(data)
        result['mean_val'] = np.mean(data)
        result['std_val'] = np.std(data)
        
        # Zero analysis
        zero_count = np.count_nonzero(data == 0)
        result['zero_percent'] = (zero_count / data.size) * 100
        
        # Saturation analysis (16-bit camera)
        MAX_16BIT = 65535
        saturated_pixels = np.count_nonzero(data >= MAX_16BIT * 0.95)  # 95% of max
        saturation_percent = (saturated_pixels / data.size) * 100
        
        if saturation_percent > 1.0:  # More than 1% saturated
            result['saturated'] = True
            result['issues'].append(f"Saturated ({saturation_percent:.1f}% pixels)")
        
        # Quality assessment
        if result['zero_percent'] == 100.0:
            result['issues'].append("ALL ZEROS")
        elif result['zero_percent'] > 95.0:
            result['issues'].append("MOSTLY ZEROS")
        elif result['zero_percent'] > 50.0:
            result['issues'].append("Many zeros")
        elif result['std_val'] < 1.0:
            result['issues'].append("No variation")
        else:
            result['success'] = True
            
    except Exception as e:
        result['issues'].append(f"Analysis error: {e}")
        
    return result

def print_saturation_analysis(results):
    """Print comprehensive saturation analysis"""
    print(f"\n{'='*60}")
    print("SATURATION ANALYSIS RESULTS")
    print(f"{'='*60}")
    
    # Find transition points
    successful_exposures = [r['exposure_ms'] for r in results if r.get('success', False)]
    failed_exposures = [r['exposure_ms'] for r in results if not r.get('success', False)]
    saturated_exposures = [r['exposure_ms'] for r in results if r.get('saturated', False)]
    
    print(f"Total exposures tested: {len(results)}")
    print(f"Successful exposures: {len(successful_exposures)}")
    print(f"Failed exposures: {len(failed_exposures)}")
    print(f"Saturated exposures: {len(saturated_exposures)}")
    
    if successful_exposures:
        print(f"Successful range: {min(successful_exposures)}ms - {max(successful_exposures)}ms")
    if failed_exposures:
        print(f"Failed range: {min(failed_exposures)}ms - {max(failed_exposures)}ms")
    if saturated_exposures:
        print(f"Saturated range: {min(saturated_exposures)}ms - {max(saturated_exposures)}ms")
        
    # Look for patterns
    print(f"\nPattern Analysis:")
    
    # Check if short exposures succeed and long fail (saturation pattern)
    short_success = any(r.get('success', False) for r in results if r['exposure_ms'] <= 100)
    long_failure = any(not r.get('success', False) for r in results if r['exposure_ms'] >= 1000)
    
    if short_success and long_failure:
        print("✓ SATURATION PATTERN DETECTED:")
        print("  - Short exposures succeed")
        print("  - Long exposures fail") 
        print("  - This suggests over-exposure/saturation issues")
    elif not short_success and not long_failure:
        print("⚠ UNDER-EXPOSURE PATTERN:")
        print("  - Short exposures fail")
        print("  - Long exposures succeed")
        print("  - This suggests insufficient illumination")
    else:
        print("? MIXED PATTERN - needs further investigation")
        
    # Detailed breakdown
    print(f"\nDetailed Results:")
    print(f"{'Exp(ms)':<8} {'Status':<8} {'Min':<6} {'Max':<6} {'Mean':<8} {'Zeros%':<8} {'Issues'}")
    print("-" * 70)
    
    for r in results:
        if r.get('success') is not None:
            status = "PASS" if r['success'] else "FAIL"
            zeros = r.get('zero_percent', 0)
            issues = ", ".join(r.get('issues', [])[:2])  # First 2 issues
            print(f"{r['exposure_ms']:<8} {status:<8} {r.get('min_val', 0):<6} "
                  f"{r.get('max_val', 0):<6} {r.get('mean_val', 0):<8.1f} "
                  f"{zeros:<8.1f} {issues}")

def main():
    """Main test function"""
    success = test_saturation_hypothesis()
    return 0 if success else 1

if __name__ == "__main__":
    sys.exit(main())