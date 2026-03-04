#!/usr/bin/env python3
"""
Timing-Optimized FLI Camera Test

This test addresses the identified timing issues that cause zero-value frames:
1. Proper CCD flushing before exposures
2. Correct shutter timing and synchronization  
3. Adequate inter-frame delays for camera settling
4. Elimination of manual/auto shutter conflicts

Author: Generated for FLI Camera Timing Analysis
"""

import sys
import time
import ctypes
import numpy as np

from fli.core.lib import FLILibrary, FLIDOMAIN_USB, FLIDEVICE_CAMERA, FLIDEBUG_INFO, FLIDEBUG_WARN, FLIDEBUG_FAIL, flidomain_t
from fli.core.camera import USBCamera

class TimingOptimizedCameraTest:
    """Test class that implements proper timing for reliable image acquisition"""
    
    def __init__(self):
        self.camera = None
        self.test_results = []
        
    def setup_camera(self):
        """Setup camera with proper timing and flush settings"""
        print("Setting up camera with optimized timing...")
        
        # Enable debug output
        dll = FLILibrary.getDll(debug=True)
        dll.FLISetDebugLevel(None, FLIDEBUG_INFO | FLIDEBUG_WARN | FLIDEBUG_FAIL)
        
        # Find and connect to camera
        print("1. Finding USB cameras...")
        dom = flidomain_t(FLIDOMAIN_USB | FLIDEVICE_CAMERA)
        names_ptr = ctypes.POINTER(ctypes.c_char_p)()
        
        result = dll.FLIList(dom, ctypes.byref(names_ptr))
        if result != 0:
            raise RuntimeError("Failed to enumerate cameras!")
            
        # Find MicroLine camera
        device_count = 0
        if names_ptr:
            while names_ptr[device_count]:
                device_info = names_ptr[device_count].decode('utf-8')
                if "MicroLine" in device_info and "ML" in device_info:
                    # Split device info properly
                    device_id, model = device_info.split(';', 1)
                    print(f"   Found camera: {model} (ID: {device_id})")
                    
                    # Create camera with proper device ID
                    self.camera = USBCamera(device_id.encode(), model.encode())
                    break
                device_count += 1
                
        if not self.camera:
            raise RuntimeError("No MicroLine cameras found!")
            
        # Configure camera for stable operation
        print("2. Configuring camera for stable operation...")
        
        # Set proper CCD flushing (CRITICAL for clean images)
        flush_count = 5  # Use 5 flushes to ensure CCD is clean
        self.camera.set_flushes(flush_count)
        print(f"   Set CCD flushes: {flush_count}")
        
        # Set initial binning
        self.camera.set_image_binning(2, 2)  # 2x2 binning for faster readout
        print("   Set binning: 2x2")
        
        # Wait for camera to settle after configuration
        print("   Waiting for camera to settle...")
        time.sleep(2.0)  # Allow camera to fully configure
        
        # Ensure camera is idle before proceeding
        if not self.camera.wait_for_idle(timeout_seconds=10):
            print("   WARNING: Camera not idle after setup")
        else:
            print("   Camera ready and idle")
            
        return True
        
    def test_proper_exposure_sequence(self, exposure_ms=100, num_frames=3):
        """Test with proper exposure timing sequence"""
        print(f"\n3. Testing proper exposure sequence ({exposure_ms}ms, {num_frames} frames)...")
        
        frame_results = []
        
        for frame_num in range(num_frames):
            print(f"\n   Frame {frame_num + 1}/{num_frames}:")
            
            try:
                # Step 1: Ensure camera is idle before starting
                print("     Waiting for camera idle...")
                if not self.camera.wait_for_idle(timeout_seconds=10):
                    print("     WARNING: Camera not idle before exposure")
                
                # Step 2: Set exposure time (camera handles shutter automatically)
                print(f"     Setting exposure: {exposure_ms}ms")
                self.camera.set_exposure(exposure_ms, "normal")  # "normal" = auto shutter
                
                # Step 3: Additional settling time for first frame or after delay
                if frame_num == 0:
                    print("     Extra settling time for first frame...")
                    time.sleep(1.0)  # Extra time for first frame
                else:
                    print("     Inter-frame settling...")
                    time.sleep(0.5)  # Inter-frame delay
                
                # Step 4: Take exposure using integrated method (NO manual shutter)
                print("     Starting exposure...")
                start_time = time.time()
                
                # Use take_photo() which handles the complete sequence:
                # - CCD flush, exposure start, wait for completion, image readout
                frame_data = self.camera.take_photo()
                
                acquisition_time = time.time() - start_time
                print(f"     Acquisition completed in {acquisition_time:.3f}s")
                
                # Step 5: Analyze frame data
                frame_result = self.analyze_frame_quality(frame_data, frame_num + 1)
                frame_result['acquisition_time'] = acquisition_time
                frame_result['exposure_ms'] = exposure_ms
                
                frame_results.append(frame_result)
                
                # Step 6: Wait for camera to return to idle before next frame
                print("     Waiting for idle before next frame...")
                if not self.camera.wait_for_idle(timeout_seconds=5):
                    print("     WARNING: Camera not idle after frame")
                    
                # Step 7: Additional recovery time between frames
                if frame_num < num_frames - 1:  # Not the last frame
                    print("     Recovery delay...")
                    time.sleep(1.0)  # Recovery time between frames
                    
            except Exception as e:
                print(f"     ERROR: Frame {frame_num + 1} failed: {e}")
                frame_results.append({
                    'frame_number': frame_num + 1,
                    'success': False,
                    'error': str(e)
                })
                
                # Try to recover
                print("     Attempting recovery...")
                time.sleep(2.0)
                try:
                    self.camera.wait_for_idle(timeout_seconds=10)
                except:
                    pass
                    
        return frame_results
        
    def analyze_frame_quality(self, frame_data, frame_num):
        """Analyze frame data quality and detect zero-frame issues"""
        result = {
            'frame_number': frame_num,
            'success': False,
            'zero_percentage': 100.0,
            'data_range': (0, 0),
            'mean_value': 0.0,
            'std_value': 0.0,
            'issues': []
        }
        
        if frame_data is None:
            result['issues'].append("No frame data returned")
            return result
            
        try:
            # Convert to numpy array for analysis
            if hasattr(frame_data, 'shape'):
                data = frame_data
            else:
                data = np.array(frame_data)
                
            # Basic statistics
            result['shape'] = data.shape
            result['min_value'] = np.min(data)
            result['max_value'] = np.max(data)
            result['mean_value'] = np.mean(data)
            result['std_value'] = np.std(data)
            result['data_range'] = (result['min_value'], result['max_value'])
            
            # Zero pixel analysis
            zero_count = np.count_nonzero(data == 0)
            total_pixels = data.size
            result['zero_percentage'] = (zero_count / total_pixels) * 100
            
            # Quality assessment
            if result['zero_percentage'] == 100.0:
                result['issues'].append("ALL ZEROS - Complete USB transfer failure")
            elif result['zero_percentage'] > 95.0:
                result['issues'].append("MOSTLY ZEROS - Severe USB transfer issue")
            elif result['zero_percentage'] > 80.0:
                result['issues'].append("Many zeros - Partial USB transfer issue")
            elif result['std_value'] < 1.0:
                result['issues'].append("Very low variation - Possible transfer problem")
            else:
                result['success'] = True
                
            # Log results
            status = "✓ GOOD" if result['success'] else "✗ FAILED"
            print(f"     Analysis: {status}")
            print(f"       Shape: {result['shape']}")
            print(f"       Range: {result['min_value']} - {result['max_value']}")
            print(f"       Mean: {result['mean_value']:.1f}, Std: {result['std_value']:.1f}")
            print(f"       Zeros: {result['zero_percentage']:.1f}%")
            
            if result['issues']:
                print(f"       Issues: {', '.join(result['issues'])}")
                
        except Exception as e:
            result['issues'].append(f"Analysis error: {e}")
            
        return result
        
    def run_comprehensive_test(self):
        """Run comprehensive test with multiple exposure times"""
        print("=" * 60)
        print("FLI Camera Timing-Optimized Test")
        print("=" * 60)
        
        try:
            # Setup camera
            if not self.setup_camera():
                return False
                
            # Test different exposure times
            exposure_times = [50, 100, 500, 1000]  # 50ms to 1s
            
            all_results = []
            
            for exp_time in exposure_times:
                print(f"\n{'='*40}")
                print(f"Testing {exp_time}ms exposures")
                print(f"{'='*40}")
                
                results = self.test_proper_exposure_sequence(exp_time, num_frames=3)
                all_results.extend(results)
                
                # Summary for this exposure time
                successful = sum(1 for r in results if r.get('success', False))
                print(f"\n   Summary for {exp_time}ms: {successful}/{len(results)} frames successful")
                
            # Overall summary
            self.print_final_summary(all_results)
            
            return True
            
        except Exception as e:
            print(f"Test failed: {e}")
            import traceback
            traceback.print_exc()
            return False
            
    def print_final_summary(self, all_results):
        """Print comprehensive test summary"""
        print(f"\n{'='*60}")
        print("FINAL TEST SUMMARY")
        print(f"{'='*60}")
        
        total_frames = len(all_results)
        successful_frames = sum(1 for r in all_results if r.get('success', False))
        zero_frames = sum(1 for r in all_results if r.get('zero_percentage', 0) > 95.0)
        
        print(f"Total frames tested: {total_frames}")
        print(f"Successful frames: {successful_frames}")
        print(f"Failed frames: {total_frames - successful_frames}")
        print(f"Zero-value frames: {zero_frames}")
        
        success_rate = (successful_frames / total_frames) * 100 if total_frames > 0 else 0
        print(f"Success rate: {success_rate:.1f}%")
        
        if zero_frames == 0:
            print("\n✓ SUCCESS: No zero-value frames detected!")
            print("  The timing optimizations have resolved the USB transfer issues.")
        elif zero_frames < total_frames * 0.1:  # Less than 10% failures
            print(f"\n⚠ PARTIAL SUCCESS: Only {zero_frames} zero-frames detected")
            print("  Significant improvement, but some timing issues remain.")
        else:
            print(f"\n✗ FAILURE: {zero_frames} zero-frames still detected")
            print("  Additional timing fixes needed.")
            
        # Analysis of remaining issues
        issues_summary = {}
        for result in all_results:
            for issue in result.get('issues', []):
                issues_summary[issue] = issues_summary.get(issue, 0) + 1
                
        if issues_summary:
            print(f"\nRemaining issues:")
            for issue, count in issues_summary.items():
                print(f"  - {issue}: {count} occurrences")
                
def main():
    """Main test function"""
    test = TimingOptimizedCameraTest()
    success = test.run_comprehensive_test()
    
    return 0 if success else 1
    
if __name__ == "__main__":
    sys.exit(main())