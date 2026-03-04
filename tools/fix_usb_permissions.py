#!/usr/bin/env python3
"""
Script to help fix USB permissions for FLI devices on macOS
"""
import os
import sys
import subprocess

def check_current_user():
    """Check if running as root or regular user"""
    if os.geteuid() == 0:
        print("✅ Running as root - USB access should work")
        return True
    else:
        print(f"ℹ️  Running as user: {os.getlogin()} (UID: {os.geteuid()})")
        return False

def find_fli_usb_devices():
    """Find FLI USB devices using system tools"""
    try:
        # Use ioreg to find FLI devices
        result = subprocess.run(['ioreg', '-p', 'IOUSB', '-w', '0'], 
                              capture_output=True, text=True)
        
        fli_devices = []
        if result.returncode == 0:
            lines = result.stdout.split('\n')
            for i, line in enumerate(lines):
                if 'Finger Lakes' in line or '"idVendor" = 3864' in line:
                    # Found FLI device, get more details
                    device_info = []
                    # Look ahead for device details
                    for j in range(max(0, i-5), min(len(lines), i+15)):
                        if any(key in lines[j] for key in ['"USB Address"', '"locationID"', '"idProduct"', '"USB Product Name"']):
                            device_info.append(lines[j].strip())
                    fli_devices.append('\n'.join(device_info))
        
        return fli_devices
    except Exception as e:
        print(f"Error finding USB devices: {e}")
        return []

def suggest_solutions():
    """Suggest solutions for USB access issues"""
    print("\n" + "=" * 60)
    print("POTENTIAL SOLUTIONS")
    print("=" * 60)
    
    print("\n1. **Run Python script with sudo** (temporary fix):")
    print("   sudo python3 your_script.py")
    
    print("\n2. **Add your user to dialout group** (if it exists):")
    print("   sudo dseditgroup -o edit -a $(whoami) -t user _dialout")
    
    print("\n3. **Check for FLI driver installation**:")
    print("   - FLI devices may need official drivers from Finger Lakes Instrumentation")
    print("   - Check: https://www.flicamera.com/downloads/")
    
    print("\n4. **Create a launch daemon for USB access** (advanced):")
    print("   - This requires creating a plist file for System permissions")
    
    print("\n5. **Use USB device reset** (sometimes helps):")
    print("   - Unplug and replug USB devices")
    print("   - Try different USB ports")
    
    print("\n6. **Check System Preferences > Security & Privacy**:")
    print("   - Allow Python/Terminal to access USB devices if prompted")

def test_sudo_access():
    """Test if we can access devices with current permissions"""
    print("\n" + "=" * 60)
    print("TESTING USB ACCESS")
    print("=" * 60)
    
    try:
        from fli.core.filter_wheel import USBFilterWheel
        from fli.core.camera import USBCamera
        
        print("Testing filter wheel access...")
        try:
            fws = USBFilterWheel.find_devices()
            print(f"✅ SUCCESS: Found {len(fws)} filter wheels")
            for i, fw in enumerate(fws):
                print(f"   {i+1}. {fw.dev_name.decode()} - {fw.model.decode()}")
                try:
                    count = fw.get_filter_count()
                    print(f"      Filter positions: {count}")
                    pos = fw.get_filter_pos()
                    print(f"      Current position: {pos}")
                except Exception as e:
                    print(f"      Command error: {e}")
        except Exception as e:
            print(f"❌ Filter wheel access failed: {e}")
            
        print("\nTesting camera access...")
        try:
            cams = USBCamera.find_devices()
            print(f"✅ SUCCESS: Found {len(cams)} cameras")
            for i, cam in enumerate(cams):
                print(f"   {i+1}. {cam.dev_name.decode()} - {cam.model.decode()}")
                try:
                    serial = cam.get_serial_number()
                    print(f"      Serial: {serial.decode()}")
                except Exception as e:
                    print(f"      Command error: {e}")
        except Exception as e:
            print(f"❌ Camera access failed: {e}")
            
    except Exception as e:
        print(f"❌ Testing failed: {e}")

def main():
    print("FLI USB Permissions Fix Tool")
    print("="*60)
    
    # Change to script directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)
    
    # Check user permissions
    is_root = check_current_user()
    
    # Find FLI devices
    devices = find_fli_usb_devices()
    if devices:
        print(f"\n✅ Found {len(devices)} FLI USB device(s) in system")
        for i, device in enumerate(devices):
            print(f"\nDevice {i+1}:")
            print(device)
    else:
        print("\n⚠️  No FLI USB devices found in system")
    
    # Test current access
    test_sudo_access()
    
    # Suggest solutions
    suggest_solutions()
    
    if not is_root:
        print("\n" + "="*60)
        print("NEXT STEPS")
        print("="*60)
        print("Try running this command to test with elevated privileges:")
        print(f"sudo python3 {__file__}")

if __name__ == "__main__":
    main()