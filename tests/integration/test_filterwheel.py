#!/usr/bin/env python3
"""
Filter Wheel Position Test Script

This script steps through each filter wheel position numerically,
displaying the current position and waiting for user input to continue.
"""
import sys
import os
import time
from ctypes import POINTER, c_char_p, byref

# Use the new package imports
from fli.core.filter_wheel import USBFilterWheel
from fli.core.lib import FLILibrary, FLIDOMAIN_USB, FLIDEVICE_FILTERWHEEL, flidomain_t

class FilterWheelTester:
    def __init__(self):
        self.filter_wheel = None
        self.lib = FLILibrary.getDll()
        
    def discover_filter_wheel(self):
        """Find and connect to the filter wheel"""
        print("=== Filter Wheel Discovery ===")
        
        try:
            fw_domain = flidomain_t(FLIDOMAIN_USB | FLIDEVICE_FILTERWHEEL)
            tmplist = POINTER(c_char_p)()
            self.lib.FLIList(fw_domain, byref(tmplist))
            
            if tmplist:
                i = 0
                while tmplist[i]:
                    device_info = tmplist[i].decode('utf-8')
                    dev_name, model = device_info.split(';')
                    print(f"Found: {dev_name} - {model}")
                    
                    # Connect to filter wheel
                    if 'Filter Wheel' in model or 'CenterLine' in model:
                        print(f"Connecting to filter wheel...")
                        self.filter_wheel = USBFilterWheel(dev_name.encode(), model.encode())
                        print(f"Filter wheel connected: {model}")
                        break
                    i += 1
                self.lib.FLIFreeList(tmplist)
                
        except Exception as e:
            print(f"Filter wheel discovery failed: {e}")
            raise
            
        if not self.filter_wheel:
            raise RuntimeError("No filter wheel found!")
            
        return True
    
    def get_filter_info(self):
        """Get filter wheel information"""
        current_pos = self.filter_wheel.get_filter_pos()
        total_positions = self.filter_wheel.get_filter_count()
        current_status = self.filter_wheel.get_status_string()
        
        print(f"Filter wheel information:")
        print(f"  Current position: {current_pos}")
        print(f"  Total positions: {total_positions}")
        print(f"  Available positions: 0 to {total_positions - 1}")
        print(f"  Current status: {current_status}")
        
        return current_pos, total_positions
    
    def move_to_position(self, position):
        """Move filter wheel to specified position"""
        print(f"Moving to position {position}...")
        
        try:
            # Get initial status
            initial_status = self.filter_wheel.get_status_string()
            print(f"  Initial status: {initial_status}")
            
            self.filter_wheel.set_filter_pos(position)
            
            # Wait for movement to complete using status polling
            print(f"  Waiting for movement to complete...")
            movement_completed = self.filter_wheel.wait_for_movement_completion(timeout_seconds=30)
            
            if movement_completed:
                final_status = self.filter_wheel.get_status_string()
                print(f"  Final status: {final_status}")
                
                # Verify position
                new_pos = self.filter_wheel.get_filter_pos()
                if new_pos == position:
                    print(f"Successfully moved to position {new_pos}")
                    return True
                else:
                    print(f"Warning: Filter wheel at position {new_pos} (requested {position})")
                    return False
            else:
                print(f"  Timeout: Movement did not complete within 30 seconds")
                current_status = self.filter_wheel.get_status_string()
                print(f"  Current status: {current_status}")
                return False
                
        except Exception as e:
            print(f"Movement failed: {e}")
            return False
    
    def step_through_positions(self, interactive=True):
        """Step through all filter wheel positions"""
        current_pos, total_positions = self.get_filter_info()
        
        print(f"\n=== Stepping Through Filter Positions ===")
        print(f"Will test positions 0 through {total_positions - 1}")
        
        if interactive:
            print("Press Enter to move to next position, or 'q' to quit")
        
        for position in range(total_positions):
            print(f"\n--- Position {position} ---")
            
            if interactive:
                user_input = input(f"Move to position {position}? (Enter to continue, 'q' to quit): ").strip().lower()
                if user_input == 'q':
                    print("Stopping at user request")
                    break
            
            success = self.move_to_position(position)
            
            if not success:
                print(f"Failed to move to position {position}")
                if interactive:
                    user_input = input("Continue anyway? (Enter to continue, 'q' to quit): ").strip().lower()
                    if user_input == 'q':
                        break
            
            # Small delay between moves in automatic mode
            if not interactive:
                time.sleep(0.5)  # Brief pause for readability
        
        print(f"\nFilter wheel position test complete!")
    
    def cleanup(self):
        """Clean up filter wheel connection"""
        print("\n=== Cleaning Up ===")
        
        try:
            if self.filter_wheel:
                del self.filter_wheel
                print("Filter wheel disconnected")
                
        except Exception as e:
            print(f"Cleanup warning: {e}")

def main():
    """Main function"""
    import sys
    
    # Check for command line arguments
    interactive = True
    if len(sys.argv) > 1:
        if sys.argv[1] == "--auto":
            interactive = False
            print("Running in automatic mode (no user prompts)")
        elif sys.argv[1] == "--help":
            print("Filter Wheel Position Test Script")
            print("Usage:")
            print("  python3 test_filterwheel.py           # Interactive mode (default)")
            print("  python3 test_filterwheel.py --auto    # Automatic mode (no prompts)")
            print("  python3 test_filterwheel.py --help    # Show this help")
            return
    else:
        print("Running in interactive mode")
    
    print("Filter Wheel Position Test Script")
    print("=" * 40)
    
    tester = None
    
    try:
        # Initialize tester
        tester = FilterWheelTester()
        
        # Find filter wheel
        tester.discover_filter_wheel()
        
        # Step through positions
        tester.step_through_positions(interactive=interactive)
        
        print("\nSUCCESS: Filter wheel test complete!")
        
    except Exception as e:
        print(f"\nFAILED: {e}")
        import traceback
        traceback.print_exc()
        
    finally:
        # Always clean up
        if tester:
            tester.cleanup()

if __name__ == "__main__":
    main()