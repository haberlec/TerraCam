#!/usr/bin/env python3
"""
Test script to replicate auto-exposure behavior and diagnose image issues.

This script:
1. Connects to FLI camera and filter wheel
2. Moves to a specific filter position
3. Performs the same auto-exposure routine as radiometric calibration
4. Saves all test images as 8-bit JPEG with 2% histogram stretch for visual inspection
5. Logs detailed statistics to help diagnose the mean=4 DN vs saturation issue
"""

import sys
import os
import time
import numpy as np
from datetime import datetime
from pathlib import Path
from PIL import Image
import logging
from ctypes import POINTER, c_char_p, byref

# Use the new package imports
from fli.core.camera import USBCamera
from fli.core.filter_wheel import USBFilterWheel
from fli.core.lib import FLILibrary, FLIDOMAIN_USB, FLIDEVICE_CAMERA, FLIDEVICE_FILTERWHEEL, flidomain_t

class AutoExposureTest:
    def __init__(self):
        self.camera = None
        self.filter_wheel = None
        self.lib = FLILibrary.getDll()
        self.logger = None
        self.test_dir = None
        
    def setup_logging_and_output(self):
        """Setup logging and test output directory"""
        # Create test output directory
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.test_dir = Path(f"auto_exposure_test_{timestamp}")
        self.test_dir.mkdir(exist_ok=True)
        
        # Setup logging
        log_file = self.test_dir / "test_log.txt"
        self.logger = logging.getLogger('AutoExposureTest')
        self.logger.setLevel(logging.INFO)
        self.logger.handlers = []
        
        # File handler
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.INFO)
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(formatter)
        self.logger.addHandler(file_handler)
        
        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(logging.Formatter('%(message)s'))
        self.logger.addHandler(console_handler)
        
        self.logger.info(f"Auto-exposure test started")
        self.logger.info(f"Test output directory: {self.test_dir}")
        
    def discover_devices(self):
        """Discover and connect to camera and filter wheel"""
        self.logger.info("=== Device Discovery ===")
        
        # Find camera
        self.logger.info("1. Searching for camera...")
        try:
            cam_domain = flidomain_t(FLIDOMAIN_USB | FLIDEVICE_CAMERA)
            tmplist = POINTER(c_char_p)()
            self.lib.FLIList(cam_domain, byref(tmplist))
            
            if tmplist:
                i = 0
                while tmplist[i]:
                    device_info = tmplist[i].decode('utf-8')
                    dev_name, model = device_info.split(';')
                    self.logger.info(f"   Found: {dev_name} - {model}")
                    
                    if 'MicroLine' in model and 'ML' in model:
                        self.logger.info(f"   -> Connecting to camera...")
                        self.camera = USBCamera(dev_name.encode(), model.encode())
                        self.logger.info(f"   Camera connected: {model}")
                        break
                    i += 1
                self.lib.FLIFreeList(tmplist)
                
        except Exception as e:
            self.logger.error(f"   Camera discovery failed: {e}")
            raise
            
        # Find filter wheel
        self.logger.info("2. Searching for filter wheel...")
        try:
            fw_domain = flidomain_t(FLIDOMAIN_USB | FLIDEVICE_FILTERWHEEL)
            tmplist = POINTER(c_char_p)()
            self.lib.FLIList(fw_domain, byref(tmplist))
            
            if tmplist:
                i = 0
                while tmplist[i]:
                    device_info = tmplist[i].decode('utf-8')
                    dev_name, model = device_info.split(';')
                    self.logger.info(f"   Found: {dev_name} - {model}")
                    
                    if 'Filter Wheel' in model or 'CenterLine' in model:
                        self.logger.info(f"   -> Connecting to filter wheel...")
                        self.filter_wheel = USBFilterWheel(dev_name.encode(), model.encode())
                        self.logger.info(f"   Filter wheel connected: {model}")
                        break
                    i += 1
                self.lib.FLIFreeList(tmplist)
                
        except Exception as e:
            self.logger.error(f"   Filter wheel discovery failed: {e}")
            raise
            
        if not self.camera or not self.filter_wheel:
            raise RuntimeError("Failed to connect to required devices")
            
        self.logger.info("✅ Both devices connected successfully")
    
    def move_to_filter(self, filter_pos):
        """Move filter wheel to specified position"""
        self.logger.info(f"Moving to filter position {filter_pos}")
        
        current_pos = self.filter_wheel.get_filter_pos()
        if current_pos == filter_pos:
            self.logger.info(f"Already at filter position {filter_pos}")
            return True
            
        self.filter_wheel.set_filter_pos(filter_pos)
        success = self.filter_wheel.wait_for_movement_completion(timeout_seconds=30)
        
        if success:
            final_pos = self.filter_wheel.get_filter_pos()
            if final_pos == filter_pos:
                self.logger.info(f"✅ Moved to filter position {filter_pos}")
                return True
            else:
                self.logger.error(f"❌ Filter at position {final_pos}, requested {filter_pos}")
                return False
        else:
            self.logger.error(f"❌ Filter movement timeout")
            return False
    
    def histogram_stretch_to_8bit(self, image_16bit, low_pct=2, high_pct=98):
        """Convert 16-bit image to 8-bit with histogram stretch"""
        # Calculate percentiles for stretch
        low_val = np.percentile(image_16bit, low_pct)
        high_val = np.percentile(image_16bit, high_pct)
        
        # Avoid division by zero
        if high_val <= low_val:
            high_val = low_val + 1
        
        # Stretch and clip
        stretched = (image_16bit.astype(np.float32) - low_val) / (high_val - low_val) * 255.0
        stretched = np.clip(stretched, 0, 255)
        
        return stretched.astype(np.uint8)
    
    def save_diagnostic_image(self, image, filename_prefix, exposure_ms, iteration, mean_dn, max_dn, sat_pct):
        """Save image as 8-bit JPEG with diagnostic info"""
        # Generate filename with stats
        filename = f"{filename_prefix}_exp{exposure_ms}ms_iter{iteration}_mean{mean_dn:.0f}_max{max_dn}_sat{sat_pct:.1f}pct.jpg"
        filepath = self.test_dir / filename
        
        # Convert to 8-bit with histogram stretch
        image_8bit = self.histogram_stretch_to_8bit(image)
        
        # Save as JPEG
        pil_image = Image.fromarray(image_8bit, mode='L')
        pil_image.save(filepath, format='JPEG', quality=95)
        
        self.logger.info(f"   💾 Saved diagnostic image: {filename}")
        return filepath
    
    def detailed_image_analysis(self, image, exposure_ms, iteration):
        """Perform detailed analysis of image statistics"""
        if image is None:
            return None, "NULL_IMAGE"
            
        if image.size == 0:
            return None, "EMPTY_IMAGE"
        
        # Basic statistics
        mean_dn = np.mean(image)
        median_dn = np.median(image)
        std_dn = np.std(image)
        min_dn = np.min(image)
        max_dn = np.max(image)
        sum_dn = np.sum(image)
        
        # Percentiles
        p01 = np.percentile(image, 1)
        p05 = np.percentile(image, 5)
        p25 = np.percentile(image, 25)
        p75 = np.percentile(image, 75)
        p95 = np.percentile(image, 95)
        p99 = np.percentile(image, 99)
        
        # Saturation analysis
        sat_60k = np.sum(image >= 60000)
        sat_65k = np.sum(image >= 65000)
        sat_pct_60k = (sat_60k / image.size) * 100
        sat_pct_65k = (sat_65k / image.size) * 100
        
        # Zero pixel analysis
        zero_pixels = np.sum(image == 0)
        zero_pct = (zero_pixels / image.size) * 100
        
        # Image shape and data type
        shape = image.shape
        dtype = image.dtype
        
        stats = {
            'mean': mean_dn,
            'median': median_dn,
            'std': std_dn,
            'min': min_dn,
            'max': max_dn,
            'sum': sum_dn,
            'p01': p01,
            'p05': p05,
            'p25': p25,
            'p75': p75,
            'p95': p95,
            'p99': p99,
            'sat_60k_pixels': sat_60k,
            'sat_65k_pixels': sat_65k,
            'sat_60k_pct': sat_pct_60k,
            'sat_65k_pct': sat_pct_65k,
            'zero_pixels': zero_pixels,
            'zero_pct': zero_pct,
            'shape': shape,
            'dtype': str(dtype),
            'exposure_ms': exposure_ms,
            'iteration': iteration
        }
        
        # Log detailed stats
        self.logger.info(f"   📊 Image Analysis - Exposure: {exposure_ms}ms, Iteration: {iteration}")
        self.logger.info(f"      Shape: {shape}, Type: {dtype}")
        self.logger.info(f"      Mean: {mean_dn:.1f}, Median: {median_dn:.1f}, Std: {std_dn:.1f}")
        self.logger.info(f"      Min: {min_dn}, Max: {max_dn}, Sum: {sum_dn}")
        self.logger.info(f"      Percentiles - P1: {p01:.0f}, P5: {p05:.0f}, P25: {p25:.0f}, P75: {p75:.0f}, P95: {p95:.0f}, P99: {p99:.0f}")
        self.logger.info(f"      Saturation - 60k: {sat_pct_60k:.1f}% ({sat_60k} px), 65k: {sat_pct_65k:.1f}% ({sat_65k} px)")
        self.logger.info(f"      Zero pixels: {zero_pct:.1f}% ({zero_pixels} px)")
        
        # Save diagnostic image
        status = "VALID"
        if sum_dn == 0:
            status = "ZERO_SUM"
        elif mean_dn < 10:
            status = "LOW_SIGNAL"
        elif sat_pct_60k > 50:
            status = "HIGH_SATURATION"
        
        self.save_diagnostic_image(image, f"test_image", exposure_ms, iteration, mean_dn, max_dn, sat_pct_60k)
        
        return stats, status
    
    def test_auto_exposure_for_target(self, filter_pos, target_dn, min_exp=30, max_exp=5000):
        """Test the auto-exposure routine that's causing issues"""
        self.logger.info(f"\n=== AUTO-EXPOSURE TEST ===")
        self.logger.info(f"Filter: {filter_pos}, Target: {target_dn} DN")
        
        # Move to filter
        if not self.move_to_filter(filter_pos):
            raise RuntimeError(f"Failed to move to filter {filter_pos}")
        
        # Initial exposure guess
        if target_dn <= 10000:
            initial_exposure = 100
        elif target_dn <= 30000:
            initial_exposure = 300
        else:
            initial_exposure = 800
            
        initial_exposure = max(min_exp, min(max_exp, initial_exposure))
        
        # Test parameters
        tolerance = 0.15
        max_saturation = 3.0
        max_iterations = 8  # Increased for thorough testing
        
        current_exposure = initial_exposure
        all_stats = []
        
        for iteration in range(max_iterations):
            self.logger.info(f"\n--- Test Iteration {iteration+1}/{max_iterations} ---")
            self.logger.info(f"Testing exposure: {current_exposure}ms")
            
            # Ensure camera is idle
            if hasattr(self.camera, 'wait_for_idle'):
                idle_success = self.camera.wait_for_idle(timeout_seconds=10)
                self.logger.info(f"Camera idle check: {'✅ Success' if idle_success else '⚠️ Timeout'}")
            
            # Add delay between iterations
            if iteration > 0:
                self.logger.info("Waiting 3 seconds before next test...")
                time.sleep(3.0)
            
            try:
                # Set exposure
                self.camera.set_exposure(current_exposure, frametype="normal")
                self.logger.info(f"Exposure set to {current_exposure}ms (normal frame)")
                
                # Wait before capture
                time.sleep(1.0)
                
                # Capture image
                self.logger.info("Capturing test image...")
                test_image = self.camera.take_photo()
                
                # Detailed analysis
                stats, status = self.detailed_image_analysis(test_image, current_exposure, iteration)
                
                if stats:
                    all_stats.append(stats)
                    
                    # Check if target achieved
                    mean_dn = stats['mean']
                    sat_pct = stats['sat_60k_pct']
                    
                    self.logger.info(f"   🎯 Target Analysis: {mean_dn:.0f}/{target_dn} DN ({mean_dn/target_dn:.2f}x), Status: {status}")
                    
                    if mean_dn > 0:  # Valid image
                        error_ratio = mean_dn / target_dn
                        if (1.0 - tolerance <= error_ratio <= 1.0 + tolerance and 
                            sat_pct <= max_saturation):
                            self.logger.info(f"   ✅ TARGET ACHIEVED: {current_exposure}ms → {mean_dn:.0f} DN")
                            break
                        
                        # Calculate next exposure
                        if sat_pct > max_saturation:
                            scale_factor = 0.7
                            self.logger.info(f"   📉 High saturation, reducing exposure")
                        else:
                            scale_factor = target_dn / mean_dn
                            scale_factor = max(0.5, min(2.0, scale_factor))
                            direction = "increasing" if scale_factor > 1 else "reducing"
                            self.logger.info(f"   📊 Signal ratio {error_ratio:.2f}, {direction} exposure")
                        
                        new_exposure = int(current_exposure * scale_factor)
                        new_exposure = max(min_exp, min(max_exp, new_exposure))
                        
                        if abs(new_exposure - current_exposure) < 10:
                            self.logger.info(f"   🔄 Converged at {current_exposure}ms")
                            break
                            
                        current_exposure = new_exposure
                    else:
                        self.logger.warning(f"   ⚠️ Zero mean image, increasing exposure significantly")
                        current_exposure = min(max_exp, current_exposure * 3)
                else:
                    self.logger.error(f"   ❌ Failed to analyze image: {status}")
                    current_exposure = min(max_exp, current_exposure * 2)
                    
            except Exception as e:
                self.logger.error(f"   ❌ Capture failed: {e}")
                if iteration < max_iterations - 1:
                    self.logger.info(f"   🔄 Retrying with 5 second delay...")
                    time.sleep(5.0)
                    continue
                else:
                    raise
        
        # Save summary statistics
        summary_file = self.test_dir / "test_summary.txt"
        with open(summary_file, 'w') as f:
            f.write(f"Auto-Exposure Test Summary\n")
            f.write(f"Filter: {filter_pos}, Target: {target_dn} DN\n")
            f.write(f"Total iterations: {len(all_stats)}\n\n")
            
            for i, stats in enumerate(all_stats):
                f.write(f"Iteration {i+1}:\n")
                f.write(f"  Exposure: {stats['exposure_ms']}ms\n")
                f.write(f"  Mean: {stats['mean']:.1f} DN\n")
                f.write(f"  Min/Max: {stats['min']}/{stats['max']} DN\n")
                f.write(f"  Saturation: {stats['sat_60k_pct']:.1f}%\n")
                f.write(f"  Zero pixels: {stats['zero_pct']:.1f}%\n\n")
        
        self.logger.info(f"\n✅ Test completed. Results saved to {self.test_dir}")
        return current_exposure, all_stats
    
    def cleanup(self):
        """Clean up devices"""
        self.logger.info("Cleaning up...")
        try:
            if self.camera:
                del self.camera
            if self.filter_wheel:
                del self.filter_wheel
        except Exception as e:
            self.logger.warning(f"Cleanup warning: {e}")

def main():
    test = AutoExposureTest()
    
    try:
        # Setup
        test.setup_logging_and_output()
        test.discover_devices()
        
        # Test parameters - modify these as needed
        filter_position = 5  # Change to test different filters
        target_dn = 32000   # Target 50% full well
        
        # Run the test
        final_exposure, stats = test.test_auto_exposure_for_target(filter_position, target_dn)
        
        print(f"\n✅ Test completed successfully!")
        print(f"Final exposure: {final_exposure}ms")
        print(f"Test output: {test.test_dir}")
        
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        return 1
        
    finally:
        test.cleanup()
    
    return 0

if __name__ == "__main__":
    sys.exit(main())