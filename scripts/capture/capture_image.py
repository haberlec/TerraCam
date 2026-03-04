#!/usr/bin/env python3
"""
FLI Camera and Filter Wheel Image Capture Script for M2 Mac

This script:
1. Finds camera and filter wheel properly
2. Sets camera temperature to 0°C
3. Moves filter wheel to position 0
4. Sets camera exposure to 100ms for a "normal" frame
5. Takes a photo and displays it with matplotlib
"""
import sys
import os
import time
import json
import logging
import numpy as np
from datetime import datetime
from PIL import Image
from ctypes import POINTER, c_char_p, byref, c_void_p, c_size_t

# Use the new package imports
from fli.core.camera import USBCamera
from fli.core.filter_wheel import USBFilterWheel
from fli.core.lib import (
    FLILibrary, FLIDOMAIN_USB, FLIDEVICE_CAMERA, FLIDEVICE_FILTERWHEEL,
    flidomain_t, FLI_FRAME_TYPE_NORMAL
)

# Import the centralized auto-exposure module
try:
    from .auto_expose import auto_expose as ae_auto_expose, evaluate_exposure, AutoExposeResult
except ImportError:
    # Running as script - add parent to path
    script_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.dirname(os.path.dirname(script_dir)))
    from scripts.capture.auto_expose import auto_expose as ae_auto_expose, evaluate_exposure, AutoExposeResult

class FLISystem:
    def __init__(self):
        self.camera = None
        self.filter_wheel = None
        self.lib = FLILibrary.getDll()
        self.logger = None
        
    def setup_logging(self, log_filename):
        """Setup logging configuration to write to file and console
        
        Args:
            log_filename: Full path to the log file
        """
        # Create logger
        self.logger = logging.getLogger('FLI_Camera_System')
        self.logger.setLevel(logging.INFO)
        
        # Clear any existing handlers
        self.logger.handlers = []
        
        # Create formatters
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        
        # File handler
        file_handler = logging.FileHandler(log_filename)
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(formatter)
        self.logger.addHandler(file_handler)
        
        # Console handler (so we still see output in terminal)
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(logging.Formatter('%(message)s'))  # Simpler format for console
        self.logger.addHandler(console_handler)
        
        return self.logger
        
    def discover_devices(self):
        """Discover and connect to camera and filter wheel properly"""
        self.logger.info("FLI Device Discovery")
        
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
                    
                    # Connect to MicroLine camera
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
                    
                    # Connect to CenterLine filter wheel
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
            
        # Verify both devices connected
        if not self.camera:
            raise RuntimeError("No camera found!")
        if not self.filter_wheel:
            raise RuntimeError("No filter wheel found!")
            
        self.logger.info("Both devices connected successfully")
        
    def set_camera_temperature(self, target_temp=10.0):
        """Set camera temperature to specified value (default 10°C)"""
        self.logger.info("Setting Camera Temperature")
        
        try:
            self.logger.info(f"Setting camera temperature to {target_temp}°C...")
            self.camera.set_temperature(target_temp)
            
            # Monitor temperature for a bit
            self.logger.info("Monitoring temperature...")
            for i in range(5):
                current_temp = self.camera.get_temperature()
                self.logger.info(f"   Temperature reading {i+1}: {current_temp:.1f}°C")
                time.sleep(1)
                
            self.logger.info(f"Camera temperature set to {target_temp}°C")
            
        except Exception as e:
            self.logger.error(f"Temperature setting failed: {e}")
            # Continue anyway - temperature control is not critical for this demo
            
    def move_filter_wheel(self, position=0):
        """Move filter wheel to specified position (default position 0)"""
        self.logger.info("Moving Filter Wheel")
        
        try:
            current_pos = self.filter_wheel.get_filter_pos()
            total_positions = self.filter_wheel.get_filter_count()
            initial_status = self.filter_wheel.get_status_string()
            
            self.logger.info(f"Current position: {current_pos}")
            self.logger.info(f"Total positions: {total_positions}")
            self.logger.info(f"Initial status: {initial_status}")
            self.logger.info(f"Moving to position {position}...")
            
            if position < 0 or position >= total_positions:
                self.logger.warning(f"Warning: Position {position} may be invalid (max: {total_positions-1})")
                
            self.filter_wheel.set_filter_pos(position)
            
            # Wait for movement to complete using real-time status polling
            self.logger.info("Waiting for filter wheel movement to complete...")
            movement_completed = self.filter_wheel.wait_for_movement_completion(timeout_seconds=30)
            
            if movement_completed:
                final_status = self.filter_wheel.get_status_string()
                self.logger.info(f"Movement completed. Final status: {final_status}")
                
                # Verify final position
                new_pos = self.filter_wheel.get_filter_pos()
                if new_pos == position:
                    self.logger.info(f"Filter wheel moved to position {new_pos}")
                else:
                    self.logger.warning(f"Filter wheel at position {new_pos} (requested {position})")
            else:
                current_status = self.filter_wheel.get_status_string()
                self.logger.error(f"Timeout: Filter wheel movement did not complete within 30 seconds")
                self.logger.error(f"Current status: {current_status}")
                raise RuntimeError(f"Filter wheel movement timeout - status: {current_status}")
                
        except Exception as e:
            self.logger.error(f"Filter wheel movement failed: {e}")
            raise

    def auto_expose(self, min_exp=10, max_exp=30000, target_p95=0.75):
        """Auto-exposure using the centralized auto_expose module.

        Uses still frame captures with binary search and quantitative
        exposure quality metrics for evaluation.

        Args:
            min_exp: Minimum exposure time (ms)
            max_exp: Maximum exposure time (ms)
            target_p95: Target P95 as fraction of max ADU (default: 0.75)

        Returns:
            Tuple of (optimal_exposure_ms, analysis_data)
        """
        self.logger.info("Auto Exposure System")
        self.logger.info("Using centralized auto_expose module with quality metrics")

        # Create capture function for the auto_expose module
        def capture_func(exp_ms: int) -> np.ndarray:
            self.camera.set_exposure(exp_ms, frametype="normal")
            self.camera.set_flushes(1)
            time.sleep(0.05)
            return self.camera.take_photo()

        # Call the centralized auto_expose function
        result: AutoExposeResult = ae_auto_expose(
            camera=self.camera,
            capture_func=capture_func,
            target_p95=target_p95,
            min_exposure_ms=min_exp,
            max_exposure_ms=max_exp,
            initial_exposure_ms=100,
            max_iterations=8,
            quality_threshold=0.70,
            tolerance=0.10,
            binning=(1, 1),
            flushes=1,
            logger=self.logger
        )

        # Convert AutoExposeResult to the legacy analysis_data format for compatibility
        metrics = result.final_metrics
        analysis_data = {
            'scene_type': result.scene_type,
            'converged': result.converged,
            'iterations': result.iterations,
            'target_p95': result.target_p95,
            'final_exposure_ms': result.exposure_ms,
            'quality_score': metrics.quality_score,
            'quality_grade': metrics.quality_grade,
            'p95_utilization': metrics.p95_utilization,
            'p99_utilization': metrics.p99_utilization,
            'saturation_fraction': metrics.saturation_fraction,
            'dynamic_range': metrics.dynamic_range,
            'histogram_entropy': metrics.histogram_entropy,
            'warnings': metrics.warnings,
            'history': [(h[0], h[1], h[2]) for h in result.history]  # (exp_ms, p95_util, score)
        }

        self.logger.info(f"Auto-exposure complete: {result.exposure_ms}ms")
        self.logger.info(f"  Quality: {metrics.quality_grade} ({metrics.quality_score:.2f})")
        self.logger.info(f"  P95: {metrics.p95:.0f} ADU ({metrics.p95_utilization*100:.1f}%)")
        self.logger.info(f"  Scene: {result.scene_type}")

        if metrics.warnings:
            for warning in metrics.warnings:
                self.logger.warning(f"  {warning}")

        return result.exposure_ms, analysis_data
    
    def setup_camera_exposure(self, exposure_ms=None, auto_expose=True):
        """Set up camera for image capture
        
        Args:
            exposure_ms: Manual exposure time in ms (if None and auto_expose=False, uses 100ms)
            auto_expose: Whether to automatically determine optimal exposure
            
        Returns:
            Tuple of (final_exposure, analysis_data) where analysis_data is None for manual exposure
        """
        self.logger.info("Setting Up Camera Exposure")
        
        try:
            # Get and display camera info first
            info = self.camera.get_info()
            self.logger.info(f"Camera specifications:")
            self.logger.info(f"   Serial: {info['serial_number']}")
            self.logger.info(f"   Array area: {info['array_area']}")
            self.logger.info(f"   Visible area: {info['visible_area']}")
            self.logger.info(f"   Pixel size: {info['pixel_size'][0]:.2e} x {info['pixel_size'][1]:.2e} meters")
            
            # Set binning to 1x1 for full resolution (this also sets the image area)
            self.camera.set_image_binning(hbin=1, vbin=1)
            
            # Determine exposure time
            analysis_data = None
            if auto_expose:
                optimal_exposure, analysis_data = self.auto_expose()
                final_exposure = optimal_exposure
            else:
                final_exposure = exposure_ms if exposure_ms is not None else 100
            
            # Set final exposure
            self.logger.info(f"Setting final exposure time to {final_exposure} ms with normal frame type...")
            self.camera.set_exposure(final_exposure, frametype="normal")
            
            self.logger.info(f"Camera configured for {final_exposure}ms exposure")
            return final_exposure, analysis_data
            
        except Exception as e:
            self.logger.error(f"Camera setup failed: {e}")
            raise
            
    def capture_image(self, flushes=2, max_retries=3, verbose=True):
        """Capture an image and return as numpy array with robust error handling
        
        Args:
            flushes: Number of CCD flushes to perform before exposure (default: 2)
            max_retries: Maximum number of retry attempts for failed captures
            verbose: Whether to print detailed capture information
            
        Returns:
            numpy.ndarray: Image data
            
        Raises:
            RuntimeError: If image capture fails after all retries
        """
        if verbose:
            self.logger.info("Capturing Image")
        
        for attempt in range(max_retries):
            try:
                if verbose:
                    # Get image dimensions
                    row_width, img_rows, img_size = self.camera.get_image_size()
                    self.logger.info(f"Image dimensions: {row_width} x {img_rows} pixels ({img_size} bytes)")
                
                # Set CCD flushes for clean imaging
                if verbose:
                    self.logger.info(f"Setting CCD flushes: {flushes}")
                self.camera.set_flushes(flushes)
                time.sleep(1)
                
                if verbose:
                    self.logger.info("Taking photo (this may take a moment)...")
                
                # Use the simple take_photo method which handles everything
                image_array = self.camera.take_photo()
                
                # Validate the captured image
                if image_array is None:
                    raise ValueError("Camera returned None image")
                
                if image_array.size == 0:
                    raise ValueError("Camera returned empty image")
                
                # Check for zero data (indicates capture failure)
                if np.all(image_array == 0):
                    raise ValueError("Camera returned all-zero image")
                
                # Check for reasonable data range
                if np.max(image_array) < 10:
                    raise ValueError(f"Camera returned suspiciously low maximum value: {np.max(image_array)}")
                
                if verbose:
                    self.logger.info(f"Image captured: {image_array.shape} pixels")
                    self.logger.info(f"   Data type: {image_array.dtype}")
                    self.logger.info(f"   Value range: {image_array.min()} - {image_array.max()}")
                
                return image_array
                
            except Exception as e:
                error_msg = f"Image capture attempt {attempt + 1}/{max_retries} failed: {e}"
                if verbose:
                    self.logger.error(f"{error_msg}")
                else:
                    self.logger.warning(f"      {error_msg}")
                
                if attempt < max_retries - 1:
                    if verbose:
                        self.logger.info(f"   Retrying in 1 second...")
                    else:
                        self.logger.info(f"         Retrying in 1 second...")
                    time.sleep(1)  # Wait before retry
                else:
                    raise RuntimeError(f"Image capture failed after {max_retries} attempts: {e}")
            
    def save_images(self, image_array, base_filename):
        """Save image in both 16-bit TIFF and 8-bit JPEG formats
        
        Args:
            image_array: numpy array containing image data
            base_filename: base name for the files (without extension)
        
        Returns:
            tuple: (tiff_path, jpeg_path) of saved files
        """
        self.logger.info("Saving Images")
        
        try:
            # Create output directory if it doesn't exist
            out_dir = "out"
            if not os.path.exists(out_dir):
                os.makedirs(out_dir)
                self.logger.info(f"Created output directory: {out_dir}")
            
            # File paths (base_filename already contains timestamp)
            tiff_path = os.path.join(out_dir, f"{base_filename}.tiff")
            jpeg_path = os.path.join(out_dir, f"{base_filename}.jpg")
            
            # Save 16-bit TIFF (preserve full dynamic range)
            self.logger.info(f"Saving 16-bit TIFF: {tiff_path}")
            # PIL expects uint16 in 0-65535 range
            tiff_image = Image.fromarray(image_array.astype(np.uint16), mode='I;16')
            tiff_image.save(tiff_path, format='TIFF')
            
            # Save 8-bit JPEG (scaled for display)
            self.logger.info(f"Saving 8-bit JPEG: {jpeg_path}")
            # Scale to 8-bit range with good contrast
            # Use 99.5th percentile to avoid hot pixels affecting scaling
            min_val = np.percentile(image_array, 0.5)
            max_val = np.percentile(image_array, 99.5)
            
            # Scale to 0-255 range
            if max_val > min_val:
                jpeg_array = ((image_array.astype(np.float32) - min_val) / (max_val - min_val) * 255)
                jpeg_array = np.clip(jpeg_array, 0, 255).astype(np.uint8)
            else:
                jpeg_array = np.zeros_like(image_array, dtype=np.uint8)
            
            jpeg_image = Image.fromarray(jpeg_array, mode='L')
            jpeg_image.save(jpeg_path, format='JPEG', quality=95)
            
            self.logger.info(f"Images saved successfully")
            self.logger.info(f"   16-bit TIFF: {tiff_path}")
            self.logger.info(f"   8-bit JPEG: {jpeg_path}")
            
            return tiff_path, jpeg_path
            
        except Exception as e:
            self.logger.error(f"Image saving failed: {e}")
            raise
    
    def create_metadata_file(self, image_array, base_filename, capture_time, exposure_ms, 
                            filter_position, ccd_temperature, analysis_data=None):
        """Create comprehensive metadata file for the captured image
        
        Args:
            image_array: numpy array containing image data
            base_filename: base name for the metadata file
            capture_time: datetime object of when the image was captured
            exposure_ms: exposure time in milliseconds
            filter_position: current filter wheel position
            ccd_temperature: CCD temperature at time of capture
            analysis_data: auto-exposure analysis data (None for manual exposures)
        
        Returns:
            Path to the saved metadata file
        """
        self.logger.info("Creating Metadata File")
        
        try:
            # Create output directory if it doesn't exist
            out_dir = "out"
            if not os.path.exists(out_dir):
                os.makedirs(out_dir)
            
            # Generate histogram (8-bit for storage efficiency)
            hist, bin_edges = np.histogram(image_array.flatten(), bins=256, range=(0, 65535))
            
            # Calculate image statistics
            percentiles = np.percentile(image_array, [1, 5, 10, 25, 50, 75, 90, 95, 99])
            
            # Build metadata dictionary
            metadata = {
                'image_info': {
                    'filename': base_filename,
                    'capture_time': capture_time.isoformat(),
                    'shape': list(image_array.shape),
                    'dtype': str(image_array.dtype),
                    'min_value': int(image_array.min()),
                    'max_value': int(image_array.max()),
                    'mean_value': float(np.mean(image_array)),
                    'std_value': float(np.std(image_array))
                },
                'acquisition_settings': {
                    'exposure_time_ms': exposure_ms,
                    'filter_position': filter_position,
                    'ccd_temperature_c': ccd_temperature,
                    'binning': {'horizontal': 1, 'vertical': 1},
                    'frame_type': 'normal'
                },
                'image_statistics': {
                    'percentile_01': float(percentiles[0]),
                    'percentile_05': float(percentiles[1]),
                    'percentile_10': float(percentiles[2]),
                    'percentile_25': float(percentiles[3]),
                    'percentile_50': float(percentiles[4]),
                    'percentile_75': float(percentiles[5]),
                    'percentile_90': float(percentiles[6]),
                    'percentile_95': float(percentiles[7]),
                    'percentile_99': float(percentiles[8])
                },
                'histogram': {
                    'bins': 256,
                    'range': [0, 65535],
                    'values': hist.tolist(),  # Convert to list for JSON serialization
                    'bin_edges': bin_edges.tolist()
                }
            }
            
            # Add auto-exposure analysis data if available
            if analysis_data is not None:
                metadata['auto_exposure_analysis'] = analysis_data
            
            # Save metadata file
            metadata_path = os.path.join(out_dir, f"{base_filename}_metadata.json")
            with open(metadata_path, 'w') as f:
                json.dump(metadata, f, indent=2)
            
            self.logger.info(f"Metadata file created: {metadata_path}")
            return metadata_path
            
        except Exception as e:
            self.logger.error(f"Metadata file creation failed: {e}")
            raise
            
    def cleanup(self):
        """Clean up device connections"""
        self.logger.info("Cleaning Up")
        
        try:
            if self.camera:
                # Turn off TEC cooler by setting temperature to ambient (25°C)
                self.logger.info("Turning off TEC cooler...")
                try:
                    self.camera.set_temperature(25.0)
                    self.logger.info("TEC cooler set to ambient temperature")
                except Exception as e:
                    self.logger.warning(f"TEC cooler shutdown warning: {e}")
                
                del self.camera
                self.logger.info("Camera disconnected")
                
            if self.filter_wheel:
                del self.filter_wheel
                self.logger.info("Filter wheel disconnected")
                
        except Exception as e:
            self.logger.warning(f"Cleanup warning: {e}")

def main():
    """Main function to run the complete imaging sequence"""
    import sys
    
    # Check for command line arguments
    use_auto_expose = True
    manual_exposure = None
    
    if len(sys.argv) > 1:
        if sys.argv[1] == "--manual" and len(sys.argv) > 2:
            use_auto_expose = False
            try:
                manual_exposure = int(sys.argv[2])
                print(f"Using manual exposure: {manual_exposure} ms")
            except ValueError:
                print("Invalid exposure time. Using auto-exposure.")
                use_auto_expose = True
        elif sys.argv[1] == "--help":
            print("FLI Camera Image Capture System")
            print("Usage:")
            print("  python3 capture_image.py                # Auto-exposure (adaptive algorithm)")
            print("  python3 capture_image.py --manual <ms>  # Manual exposure time")
            print("  python3 capture_image.py --help         # Show this help")
            return
    
    fli_system = None
    
    try:
        # Initialize system
        fli_system = FLISystem()
        
        # Generate timestamp and filename early for logging setup
        capture_start_time = datetime.now()
        timestamp = capture_start_time.strftime("%Y%m%dT%H%M%S")
        
        # We'll update the filter position in the filename after we connect to devices
        # For now, use a placeholder
        temp_base_filename = f"VNIR_POS00_{timestamp}"
        
        # Setup logging with the base filename
        out_dir = "out"
        if not os.path.exists(out_dir):
            os.makedirs(out_dir)
        log_file = os.path.join(out_dir, f"{temp_base_filename}.log")
        fli_system.setup_logging(log_file)
        
        # Log the startup information
        fli_system.logger.info("FLI Camera Image Capture System")
        fli_system.logger.info("=" * 50)
        if use_auto_expose:
            fli_system.logger.info("Mode: Auto-exposure (adaptive algorithm)")
        else:
            fli_system.logger.info(f"Mode: Manual exposure ({manual_exposure} ms)")
        
        # Step 1: Find devices
        fli_system.discover_devices()
        
        # Step 2: Set camera temperature
        fli_system.set_camera_temperature(10.0)
        
        # Step 3: Move filter wheel
        fli_system.move_filter_wheel(1)
        
        # Get current filter position and update filename/logging
        current_filter_pos = fli_system.filter_wheel.get_filter_pos()
        base_filename = f"VNIR_POS{current_filter_pos:02d}_{timestamp}"
        
        # Update log file to use correct filename
        new_log_file = os.path.join(out_dir, f"{base_filename}.log")
        if new_log_file != log_file:
            # Close current log and rename file
            for handler in fli_system.logger.handlers[:]:
                if isinstance(handler, logging.FileHandler):
                    handler.close()
                    fli_system.logger.removeHandler(handler)
            os.rename(log_file, new_log_file)
            
            # Add new file handler with correct name
            formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
            file_handler = logging.FileHandler(new_log_file)
            file_handler.setLevel(logging.INFO)
            file_handler.setFormatter(formatter)
            fli_system.logger.addHandler(file_handler)
        
        # Step 4: Setup camera exposure
        final_exposure, analysis_data = fli_system.setup_camera_exposure(
            exposure_ms=manual_exposure, 
            auto_expose=use_auto_expose
        )
        
        # Step 5: Capture and save image
        image = fli_system.capture_image(flushes=2, verbose=True)
        
        # Get CCD temperature for metadata
        current_ccd_temp = fli_system.camera.get_temperature()
        
        # Save image files
        saved_files = fli_system.save_images(image, base_filename)
        
        # Create metadata file
        metadata_file = fli_system.create_metadata_file(
            image, base_filename, capture_start_time, final_exposure,
            current_filter_pos, current_ccd_temp, analysis_data
        )
        
        if saved_files:
            fli_system.logger.info("Files saved:")
            fli_system.logger.info(f"   16-bit TIFF: {saved_files[0]}")
            fli_system.logger.info(f"   8-bit JPEG: {saved_files[1]}")
            fli_system.logger.info(f"   Metadata JSON: {metadata_file}")
            fli_system.logger.info(f"   Log file: {new_log_file}")
        
        fli_system.logger.info("SUCCESS: Image capture complete!")
        fli_system.logger.info("Camera and filter wheel working perfectly with enhanced metadata")
        
    except Exception as e:
        if fli_system and fli_system.logger:
            fli_system.logger.error(f"FAILED: {e}")
            import traceback
            fli_system.logger.error(traceback.format_exc())
        else:
            print(f"\nFAILED: {e}")
            import traceback
            traceback.print_exc()
        
    finally:
        # Always clean up
        if fli_system:
            fli_system.cleanup()

if __name__ == "__main__":
    main()