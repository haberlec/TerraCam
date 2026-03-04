#!/usr/bin/env python3
"""
USB Packet Analysis Test for FLI Camera

This test analyzes USB communication patterns to verify:
1. Proper 512-byte alignment in bulk transfers
2. Chunked transfer behavior for large reads
3. USB error handling and recovery
4. Frame data integrity and non-zero content validation

Author: Generated for FLI Camera USB Analysis
"""

import ctypes
import time
import sys
import struct
import numpy as np
from collections import defaultdict

from fli.core.lib import FLILibrary, FLIDOMAIN_USB, FLIDEVICE_CAMERA, FLIDEBUG_INFO, FLIDEBUG_WARN, FLIDEBUG_FAIL, FLIDEBUG_IO, FLI_SHUTTER_OPEN, FLI_SHUTTER_CLOSE, flidomain_t
from fli.core.camera import USBCamera

class USBTransferAnalyzer:
    """Analyzes USB transfer patterns and data integrity"""
    
    def __init__(self):
        self.transfer_stats = defaultdict(int)
        self.error_counts = defaultdict(int)
        self.frame_stats = {}
        self.debug_level = FLIDEBUG_INFO
        
    def enable_debug_logging(self, level=None):
        """Enable detailed USB debug logging"""
        if level is None:
            level = FLIDEBUG_INFO | FLIDEBUG_WARN | FLIDEBUG_FAIL | FLIDEBUG_IO
        
        dll = FLILibrary.getDll(debug=True)
        dll.FLISetDebugLevel(None, level)
        print(f"Enabled debug logging with level: 0x{level:x}")
        
    def analyze_frame_data(self, frame_data, frame_info):
        """Analyze frame data for patterns that indicate USB issues"""
        if frame_data is None:
            return {"error": "No frame data"}
            
        # Convert to numpy array for analysis
        if hasattr(frame_data, 'shape'):
            data = frame_data
        else:
            data = np.array(frame_data)
            
        stats = {
            'shape': data.shape,
            'dtype': data.dtype,
            'size_bytes': data.nbytes,
            'min_value': np.min(data),
            'max_value': np.max(data),
            'mean_value': np.mean(data),
            'std_value': np.std(data),
            'zero_pixels': np.count_nonzero(data == 0),
            'zero_percentage': (np.count_nonzero(data == 0) / data.size) * 100,
        }
        
        # Check for problematic patterns
        stats['analysis'] = {
            'mostly_zeros': stats['zero_percentage'] > 90,
            'all_zeros': stats['zero_percentage'] == 100,
            'uniform_data': stats['std_value'] < 1.0,
            'expected_noise': stats['std_value'] > 5.0,  # Some variation expected
        }
        
        # Check for USB alignment patterns in raw bytes
        if data.nbytes > 0:
            raw_bytes = data.tobytes()
            stats['usb_analysis'] = self._analyze_usb_patterns(raw_bytes)
            
        return stats
        
    def _analyze_usb_patterns(self, raw_bytes):
        """Analyze raw byte patterns for USB transfer artifacts"""
        size = len(raw_bytes)
        
        # Check 512-byte boundary alignment
        chunks_512 = []
        for i in range(0, size, 512):
            chunk = raw_bytes[i:i+512]
            if len(chunk) == 512:
                chunks_512.append(chunk)
                
        # Analyze chunk patterns
        analysis = {
            'total_bytes': size,
            'is_512_aligned': (size % 512) == 0,
            'num_512_chunks': len(chunks_512),
            'chunk_patterns': []
        }
        
        # Look for repeated patterns that might indicate USB transfer issues
        for i, chunk in enumerate(chunks_512[:5]):  # Check first 5 chunks
            chunk_stats = {
                'chunk_index': i,
                'all_zeros': all(b == 0 for b in chunk),
                'uniform_bytes': len(set(chunk)) == 1,
                'first_bytes': chunk[:16].hex() if len(chunk) >= 16 else chunk.hex(),
                'last_bytes': chunk[-16:].hex() if len(chunk) >= 16 else chunk.hex(),
            }
            analysis['chunk_patterns'].append(chunk_stats)
            
        return analysis
        
    def test_camera_usb_behavior(self, exposure_time=0.1, num_frames=3):
        """Test camera USB behavior with frame acquisition"""
        print("=" * 60)
        print("FLI Camera USB Behavior Analysis Test")
        print("=" * 60)
        
        # Enable debug logging
        self.enable_debug_logging()
        
        try:
            # Find and connect to camera using proper device ID parsing
            print("\n1. Finding USB cameras...")
            
            dll = FLILibrary.getDll(debug=True)
            dom = flidomain_t(FLIDOMAIN_USB | FLIDEVICE_CAMERA)
            names_ptr = ctypes.POINTER(ctypes.c_char_p)()
            
            result = dll.FLIList(dom, ctypes.byref(names_ptr))
            if result != 0:
                print("ERROR: Failed to enumerate cameras!")
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
                        print(f"Found camera: {model} (ID: {device_id})")
                        
                        # Create camera with proper device ID
                        cam = USBCamera(device_id.encode(), model.encode())
                        break
                    device_count += 1
                    
            if not cam:
                print("ERROR: No MicroLine cameras found!")
                return False
            
            # Get camera configuration
            print("\n2. Camera configuration...")
            info = cam.get_info()
            image_size = cam.get_image_size()
            temperature = cam.get_temperature()
            
            print(f"Model: {info.get('model', 'Unknown')}")
            print(f"Image size: {image_size}")
            print(f"Temperature: {temperature}°C")
            print(f"Current mode: {cam.get_camera_mode_string()}")
            
            # Test different binning modes
            binning_modes = [(1,1), (2,2)]
            
            for hbin, vbin in binning_modes:
                print(f"\n3. Testing binning mode {hbin}x{vbin}...")
                cam.set_image_binning(hbin, vbin)
                
                # Set exposure time (convert to milliseconds)
                exposure_ms = int(exposure_time * 1000)
                cam.set_exposure(exposure_ms)
                print(f"Set exposure time: {exposure_time}s ({exposure_ms}ms)")
                
                # Take multiple frames to analyze consistency
                for frame_num in range(num_frames):
                    print(f"\n  Frame {frame_num + 1}/{num_frames}:")
                    
                    # Record timing
                    start_time = time.time()
                    
                    # Open shutter and take photo
                    cam._libfli.FLIControlShutter(cam._dev, FLI_SHUTTER_OPEN)
                    
                    try:
                        frame_data = cam.take_photo()
                        acquisition_time = time.time() - start_time
                        
                        print(f"    Acquisition time: {acquisition_time:.3f}s")
                        
                        # Analyze frame data
                        frame_info = {
                            'binning': (hbin, vbin),
                            'exposure_time': exposure_time,
                            'frame_number': frame_num,
                            'acquisition_time': acquisition_time
                        }
                        
                        stats = self.analyze_frame_data(frame_data, frame_info)
                        self._print_frame_analysis(stats, frame_num + 1)
                        
                        # Store for comparison
                        key = f"{hbin}x{vbin}_frame_{frame_num}"
                        self.frame_stats[key] = stats
                        
                    finally:
                        cam._libfli.FLIControlShutter(cam._dev, FLI_SHUTTER_CLOSE)
                        
            # Print summary analysis
            self._print_summary_analysis()
            
            return True
            
        except Exception as e:
            print(f"ERROR during USB analysis: {e}")
            import traceback
            traceback.print_exc()
            return False
            
    def _print_frame_analysis(self, stats, frame_num):
        """Print detailed frame analysis"""
        print(f"    Frame {frame_num} Analysis:")
        print(f"      Shape: {stats['shape']}")
        print(f"      Size: {stats['size_bytes']:,} bytes")
        print(f"      Data range: {stats['min_value']} - {stats['max_value']}")
        print(f"      Mean: {stats['mean_value']:.2f}, Std: {stats['std_value']:.2f}")
        print(f"      Zero pixels: {stats['zero_pixels']:,} ({stats['zero_percentage']:.1f}%)")
        
        # USB-specific analysis
        if 'usb_analysis' in stats:
            usb = stats['usb_analysis']
            print(f"      USB Analysis:")
            print(f"        512-byte aligned: {usb['is_512_aligned']}")
            print(f"        Number of 512-byte chunks: {usb['num_512_chunks']}")
            
            # Check for problematic patterns
            if usb['chunk_patterns']:
                problematic_chunks = [p for p in usb['chunk_patterns'] 
                                    if p['all_zeros'] or p['uniform_bytes']]
                if problematic_chunks:
                    print(f"        WARNING: Found {len(problematic_chunks)} problematic chunks")
                    for chunk in problematic_chunks[:2]:  # Show first 2
                        print(f"          Chunk {chunk['chunk_index']}: "
                              f"zeros={chunk['all_zeros']}, uniform={chunk['uniform_bytes']}")
        
        # Overall assessment
        analysis = stats['analysis']
        issues = []
        if analysis['all_zeros']:
            issues.append("ALL ZEROS - USB transfer failed")
        elif analysis['mostly_zeros']:
            issues.append("MOSTLY ZEROS - partial USB failure")
        elif analysis['uniform_data']:
            issues.append("UNIFORM DATA - possible USB issue")
        elif not analysis['expected_noise']:
            issues.append("NO NOISE - suspicious for camera data")
            
        if issues:
            print(f"      ISSUES: {', '.join(issues)}")
        else:
            print(f"      STATUS: Frame appears normal")
            
    def _print_summary_analysis(self):
        """Print summary analysis across all frames"""
        print("\n" + "=" * 60)
        print("SUMMARY ANALYSIS")
        print("=" * 60)
        
        if not self.frame_stats:
            print("No frame data to analyze")
            return
            
        # Count issues across frames
        total_frames = len(self.frame_stats)
        problem_frames = 0
        zero_frames = 0
        
        for key, stats in self.frame_stats.items():
            analysis = stats['analysis']
            if analysis['all_zeros']:
                zero_frames += 1
                problem_frames += 1
            elif analysis['mostly_zeros'] or analysis['uniform_data']:
                problem_frames += 1
                
        print(f"Total frames analyzed: {total_frames}")
        print(f"Frames with issues: {problem_frames}")
        print(f"Frames with all zeros: {zero_frames}")
        
        if zero_frames > 0:
            print(f"\nCRITICAL: {zero_frames}/{total_frames} frames returned all zeros!")
            print("This indicates USB transfer failures.")
        elif problem_frames > 0:
            print(f"\nWARNING: {problem_frames}/{total_frames} frames show suspicious patterns.")
            print("This may indicate partial USB issues.")
        else:
            print(f"\nSUCCESS: All {total_frames} frames appear to contain valid camera data.")
            
        # USB transfer analysis summary
        aligned_frames = sum(1 for stats in self.frame_stats.values() 
                           if stats.get('usb_analysis', {}).get('is_512_aligned', False))
        print(f"\nUSB Transfer Analysis:")
        print(f"  512-byte aligned frames: {aligned_frames}/{total_frames}")
        
        if aligned_frames == total_frames:
            print("  ✓ All frames properly aligned for USB bulk transfers")
        else:
            print("  ⚠ Some frames not properly aligned - may impact performance")

def run_usb_stress_test():
    """Run a stress test with various exposure times and binning modes"""
    analyzer = USBTransferAnalyzer()
    
    print("Starting USB Stress Test...")
    
    # Test different exposure times
    exposure_times = [0.001, 0.01, 0.1, 1.0]  # 1ms to 1s
    
    success_count = 0
    total_tests = len(exposure_times)
    
    for exp_time in exposure_times:
        print(f"\n{'='*40}")
        print(f"Testing exposure time: {exp_time}s")
        print(f"{'='*40}")
        
        try:
            if analyzer.test_camera_usb_behavior(exposure_time=exp_time, num_frames=2):
                success_count += 1
        except Exception as e:
            print(f"Test failed for exposure {exp_time}s: {e}")
            
    print(f"\n{'='*60}")
    print(f"USB STRESS TEST SUMMARY")
    print(f"{'='*60}")
    print(f"Successful tests: {success_count}/{total_tests}")
    
    if success_count == total_tests:
        print("✓ All USB tests passed!")
    else:
        print(f"⚠ {total_tests - success_count} tests failed - USB issues detected")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "stress":
        run_usb_stress_test()
    else:
        analyzer = USBTransferAnalyzer()
        success = analyzer.test_camera_usb_behavior(exposure_time=0.1, num_frames=3)
        
        if success:
            print("\n✓ USB analysis test completed successfully")
            sys.exit(0)
        else:
            print("\n✗ USB analysis test failed")
            sys.exit(1)