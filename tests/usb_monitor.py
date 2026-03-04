#!/usr/bin/env python3
"""
USB Transfer Monitor for FLI Camera

This module provides real-time monitoring of USB transfers
to debug the new chunked transfer implementation.

Author: Generated for FLI Camera USB Debug Analysis
"""

import ctypes
import time
import sys
import threading
import queue
import re
from datetime import datetime

from fli.core.lib import FLILibrary, FLIDOMAIN_USB, FLIDEVICE_CAMERA, FLIDEBUG_INFO, FLIDEBUG_WARN, FLIDEBUG_FAIL, FLIDEBUG_IO, FLI_SHUTTER_OPEN, FLI_SHUTTER_CLOSE, flidomain_t
from fli.core.camera import USBCamera

class USBTransferMonitor:
    """Monitor USB transfers through debug output analysis"""
    
    def __init__(self):
        self.transfer_log = []
        self.monitoring = False
        self.debug_queue = queue.Queue()
        self.transfer_patterns = {
            'pipe_read': re.compile(r'mac_usb_piperead.*?(\d+)\s+bytes'),
            'chunk_size': re.compile(r'chunk_size:\s*(\d+)'),
            'actual_read': re.compile(r'actual:\s*(\d+)'),
            'error': re.compile(r'error.*?0x([0-9a-fA-F]+)'),
            'stall': re.compile(r'pipe stalled'),
            'alignment': re.compile(r'512-byte.*?(\d+)'),
        }
        
    def start_monitoring(self):
        """Start monitoring USB debug output"""
        # Enable comprehensive debug logging
        dll = FLILibrary.getDll(debug=True)
        debug_level = (FLIDEBUG_INFO | FLIDEBUG_WARN |
                      FLIDEBUG_FAIL | FLIDEBUG_IO)
        dll.FLISetDebugLevel(None, debug_level)
        
        self.monitoring = True
        print("USB Transfer Monitor started - capturing debug output...")
        
    def stop_monitoring(self):
        """Stop monitoring and return collected data"""
        self.monitoring = False
        return self.transfer_log
        
    def analyze_transfer_log(self):
        """Analyze collected transfer data"""
        if not self.transfer_log:
            return {"error": "No transfer data collected"}
            
        analysis = {
            'total_transfers': 0,
            'total_bytes': 0,
            'chunk_sizes': [],
            'errors': [],
            'stalls': 0,
            'alignment_issues': 0,
            'transfer_rates': [],
        }
        
        for entry in self.transfer_log:
            # Parse transfer information
            for pattern_name, pattern in self.transfer_patterns.items():
                matches = pattern.findall(entry['message'])
                if matches:
                    if pattern_name == 'pipe_read':
                        analysis['total_transfers'] += 1
                        bytes_transferred = int(matches[0])
                        analysis['total_bytes'] += bytes_transferred
                        
                        if 'duration' in entry:
                            rate = bytes_transferred / entry['duration']
                            analysis['transfer_rates'].append(rate)
                            
                    elif pattern_name == 'chunk_size':
                        chunk_size = int(matches[0])
                        analysis['chunk_sizes'].append(chunk_size)
                        
                        # Check for proper 512-byte alignment
                        if chunk_size % 512 != 0 and chunk_size > 512:
                            analysis['alignment_issues'] += 1
                            
                    elif pattern_name == 'error':
                        error_code = matches[0]
                        analysis['errors'].append(error_code)
                        
                    elif pattern_name == 'stall':
                        analysis['stalls'] += 1
                        
        # Calculate statistics
        if analysis['chunk_sizes']:
            analysis['avg_chunk_size'] = sum(analysis['chunk_sizes']) / len(analysis['chunk_sizes'])
            analysis['max_chunk_size'] = max(analysis['chunk_sizes'])
            analysis['min_chunk_size'] = min(analysis['chunk_sizes'])
            
        if analysis['transfer_rates']:
            analysis['avg_transfer_rate'] = sum(analysis['transfer_rates']) / len(analysis['transfer_rates'])
            analysis['max_transfer_rate'] = max(analysis['transfer_rates'])
            
        return analysis
        
    def log_transfer(self, message, duration=None):
        """Log a transfer event with timestamp"""
        entry = {
            'timestamp': datetime.now(),
            'message': message,
            'duration': duration
        }
        self.transfer_log.append(entry)
        
    def create_transfer_report(self, analysis):
        """Create a detailed transfer report"""
        report = []
        report.append("USB Transfer Analysis Report")
        report.append("=" * 40)
        report.append(f"Analysis Date: {datetime.now()}")
        report.append("")
        
        report.append("Transfer Statistics:")
        report.append(f"  Total transfers: {analysis.get('total_transfers', 0)}")
        report.append(f"  Total bytes: {analysis.get('total_bytes', 0):,}")
        report.append(f"  Errors: {len(analysis.get('errors', []))}")
        report.append(f"  Pipe stalls: {analysis.get('stalls', 0)}")
        report.append(f"  Alignment issues: {analysis.get('alignment_issues', 0)}")
        report.append("")
        
        if 'avg_chunk_size' in analysis:
            report.append("Chunk Size Analysis:")
            report.append(f"  Average chunk size: {analysis['avg_chunk_size']:.0f} bytes")
            report.append(f"  Max chunk size: {analysis['max_chunk_size']} bytes")
            report.append(f"  Min chunk size: {analysis['min_chunk_size']} bytes")
            report.append("")
            
        if 'avg_transfer_rate' in analysis:
            report.append("Transfer Rate Analysis:")
            report.append(f"  Average rate: {analysis['avg_transfer_rate']/1024:.1f} KB/s")
            report.append(f"  Peak rate: {analysis['max_transfer_rate']/1024:.1f} KB/s")
            report.append("")
            
        if analysis.get('errors'):
            report.append("Error Details:")
            for error in analysis['errors']:
                report.append(f"  Error code: 0x{error}")
            report.append("")
            
        # Assessment
        report.append("Assessment:")
        issues = []
        
        if analysis.get('errors'):
            issues.append(f"{len(analysis['errors'])} USB errors detected")
            
        if analysis.get('stalls', 0) > 0:
            issues.append(f"{analysis['stalls']} pipe stalls occurred")
            
        if analysis.get('alignment_issues', 0) > 0:
            issues.append(f"{analysis['alignment_issues']} alignment issues")
            
        if not issues:
            report.append("  ✓ No significant USB issues detected")
            report.append("  ✓ Transfer patterns appear normal")
        else:
            report.append("  ⚠ Issues detected:")
            for issue in issues:
                report.append(f"    - {issue}")
                
        return "\n".join(report)

def test_with_monitoring():
    """Test camera operation with USB monitoring"""
    monitor = USBTransferMonitor()
    
    try:
        # Start monitoring
        monitor.start_monitoring()
        
        # Import and run camera test
        # USBCamera already imported at module level
        
        print("Finding cameras...")
        
        # Use proper device ID parsing
        dll = FLILibrary.getDll(debug=True)
        dom = flidomain_t(FLIDOMAIN_USB | FLIDEVICE_CAMERA)
        names_ptr = ctypes.POINTER(ctypes.c_char_p)()
        
        result = dll.FLIList(dom, ctypes.byref(names_ptr))
        if result != 0:
            print("Failed to enumerate cameras!")
            return
            
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
            print("No MicroLine cameras found!")
            return
            
        print(f"Testing with camera: {cam.get_info()}")
        
        # Test different scenarios (exposure times in milliseconds)
        test_scenarios = [
            {"binning": (1, 1), "exposure": 10, "name": "1x1 binning, 10ms exposure"},
            {"binning": (2, 2), "exposure": 100, "name": "2x2 binning, 100ms exposure"},
            {"binning": (1, 1), "exposure": 1000, "name": "1x1 binning, 1s exposure"},
        ]
        
        for scenario in test_scenarios:
            print(f"\nTesting: {scenario['name']}")
            
            # Configure camera
            cam.set_image_binning(*scenario['binning'])
            cam.set_exposure(scenario['exposure'])
            
            # Take photo with timing
            start_time = time.time()
            
            cam._libfli.FLIControlShutter(cam._dev, FLI_SHUTTER_OPEN)
            try:
                frame = cam.take_photo()
                duration = time.time() - start_time
                
                print(f"  Capture completed in {duration:.3f}s")
                print(f"  Frame shape: {frame.shape if hasattr(frame, 'shape') else 'N/A'}")
                
                # Log this transfer
                monitor.log_transfer(f"Frame capture: {scenario['name']}", duration)
                
            finally:
                cam._libfli.FLIControlShutter(cam._dev, FLI_SHUTTER_CLOSE)
                
    except Exception as e:
        print(f"Error during monitoring test: {e}")
        import traceback
        traceback.print_exc()
        
    finally:
        # Stop monitoring and analyze
        print("\nAnalyzing USB transfer data...")
        transfer_data = monitor.stop_monitoring()
        analysis = monitor.analyze_transfer_log()
        
        # Generate report
        report = monitor.create_transfer_report(analysis)
        print("\n" + report)
        
        # Save report to file
        with open("usb_transfer_report.txt", "w") as f:
            f.write(report)
        print(f"\nDetailed report saved to: usb_transfer_report.txt")

if __name__ == "__main__":
    test_with_monitoring()