# FLI Camera USB Test Suite

This test suite analyzes USB packet behavior to verify the new 512-byte aligned chunked transfer implementation and detect zero-frame issues.

## Test Scripts

### 1. `quick_test.py` - Quick Functionality Test
**Purpose**: Fast verification that USB transfers are working correctly.

```bash
cd /Users/chaberle/Documents/projects/SPECTRE/TerraCam/FLI_sdk/tests
python quick_test.py
```

**What it tests**:
- Camera discovery and connection
- Basic frame acquisition (3 frames)
- Zero-frame detection
- Quick statistical analysis

**Expected output**: 
- ✓ PASSED: All frames contain valid camera data
- ✗ FAILED: Frames contain mostly zeros (USB issue)

### 2. `usb_analysis_test.py` - Comprehensive Analysis
**Purpose**: Detailed analysis of USB transfer patterns and frame data integrity.

```bash
# Standard test
python usb_analysis_test.py

# Stress test with multiple exposure times
python usb_analysis_test.py stress
```

**What it analyzes**:
- 512-byte alignment of USB transfers
- Chunked transfer behavior
- Frame data patterns and statistics
- USB error handling
- Different binning modes and exposure times

**Key metrics**:
- Zero pixel percentage (should be < 10%)
- USB chunk alignment (should be 512-byte aligned)
- Data variation (should show camera noise, not uniform data)
- Transfer timing and consistency

### 3. `usb_monitor.py` - Real-time USB Monitoring
**Purpose**: Monitor USB debug output in real-time to analyze transfer patterns.

```bash
python usb_monitor.py
```

**What it captures**:
- USB pipe read operations
- Chunk sizes and alignment
- Error codes and pipe stalls
- Transfer rates and timing
- Detailed debug logging analysis

**Output**: Creates `usb_transfer_report.txt` with detailed analysis.

## Understanding the Results

### Successful USB Operation
```
✓ All tests passed - USB appears to be working correctly!
Frame Analysis:
  Zero pixels: 1,234 (2.3%)        # Low percentage is normal
  Data range: 45 - 4095            # Should show variation
  512-byte aligned: True           # Confirms proper alignment
  STATUS: Frame appears normal
```

### Zero-Frame Issue (USB Problem)
```
✗ MOSTLY ZEROS (95.2% zeros) - USB ISSUE!
Frame Analysis:
  Zero pixels: 512,045 (95.2%)     # High percentage indicates failure
  Data range: 0 - 5                # Very limited range
  STATUS: ALL ZEROS - USB transfer failed
```

### Partial USB Issues
```
⚠ Many zeros (67.3% zeros) - Possible issue
Frame Analysis:
  Zero pixels: 362,144 (67.3%)     # Moderate percentage
  Data range: 0 - 234             # Some data but suspect
  USB Analysis:
    WARNING: Found 3 problematic chunks
```

## USB Implementation Details

The new USB implementation features:

### 1. Chunked Transfers (`mac_usb_piperead`)
- Breaks large transfers into optimal chunks
- Maximum chunk size: 65,536 bytes (`USB_READ_SIZ_MAX`)
- Automatic 512-byte boundary alignment
- Enhanced error handling with retry logic

### 2. Page-Aligned Memory Allocation
- Uses `posix_memalign()` for page-aligned buffers
- Ensures both page alignment and 512-byte alignment
- Optimal for macOS USB bulk transfers

### 3. Error Recovery
- Automatic pipe stall detection and recovery
- Graceful handling of short reads
- Detailed debug logging for troubleshooting

## Troubleshooting

### If Tests Fail

1. **Check USB connection**:
   - Ensure camera is properly connected
   - Try different USB ports/cables
   - Check for USB 3.0 vs 2.0 compatibility

2. **Enable debug logging**:
   ```bash
   # In any test script, debug logging is automatically enabled
   # Look for messages like:
   # "mac_usb_piperead: successfully read 65536 bytes"
   # "chunk_size: 512" 
   ```

3. **Check for compilation issues**:
   ```bash
   cd /Users/chaberle/Documents/projects/SPECTRE/TerraCam/FLI_sdk/src/libfli
   make clean && make
   # Should compile without errors
   ```

4. **Verify library deployment**:
   ```bash
   ls -la /Users/chaberle/Documents/projects/SPECTRE/TerraCam/FLI_sdk/src/python/libfli.so
   # Should show recent timestamp matching rebuild
   ```

### Common Issues

- **"No USB cameras found"**: Check physical connections and camera power
- **"USB transfer failed"**: May indicate alignment or timing issues
- **"Pipe stalled"**: USB communication error, usually recovers automatically
- **"All zeros"**: Classic symptom of the original USB buffer issue

## Expected Behavior After Fix

With the new USB implementation, you should see:

1. **Consistent frame data**: No frames with >90% zeros
2. **Proper alignment**: All transfers 512-byte aligned
3. **Error recovery**: Automatic handling of USB pipe stalls
4. **Performance**: Efficient chunked transfers for large frames
5. **Debug output**: Clear logging of transfer operations

The tests verify that the fundamental USB buffer alignment issue has been resolved and that camera frames now contain valid image data instead of zeros.