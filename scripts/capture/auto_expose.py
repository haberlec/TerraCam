#!/usr/bin/env python3
"""
FLI Camera Auto-Exposure Module

Standalone auto-exposure with quantitative evaluation metrics.
Uses still frame captures with binary search optimization.

This module provides:
1. evaluate_exposure() - Quantitative scoring of any image's exposure quality
2. auto_expose() - Binary search algorithm to find optimal exposure time

Evaluation criteria based on scientific imaging best practices:
- Target P95 at 70-85% of full well (saturation headroom)
- Saturation fraction < 0.1% (avoid clipping)
- Quantitative exposure quality score (0.0-1.0)

For 16-bit CCD (65,535 max ADU):
- Excellent: P95 45,000-55,000 ADU, saturation < 0.01%
- Good: P95 35,000-60,000 ADU, saturation < 0.1%
- Acceptable: P95 20,000-62,000 ADU, saturation < 1.0%

Usage:
    from scripts.capture.auto_expose import auto_expose, evaluate_exposure

    # Evaluate any image
    metrics = evaluate_exposure(image)
    print(f"Quality: {metrics['quality_grade']} ({metrics['quality_score']:.2f})")

    # Find optimal exposure (requires connected camera)
    result = auto_expose(camera, target_p95=0.75)
    print(f"Optimal exposure: {result['exposure_ms']}ms")

References:
- ESO Signal-to-Noise documentation
- Hamamatsu CCD linearity and saturation guides
- HST ACS CCD operations documentation
"""
import time
import logging
import numpy as np
from dataclasses import dataclass, field
from typing import Optional, Dict, Tuple, List, Callable, Any


# =============================================================================
# Constants for 16-bit CCD
# =============================================================================

MAX_ADU = 65535  # 16-bit maximum

# Saturation thresholds (based on research)
SATURATION_HARD_LIMIT = 0.95 * MAX_ADU      # 62,258 - pixels above this are saturated
SATURATION_SOFT_LIMIT = 0.90 * MAX_ADU      # 58,982 - linearity starts degrading
LINEARITY_LIMIT = 0.85 * MAX_ADU            # 55,704 - guaranteed linear response

# Target ranges for P95 (primary metric)
P95_EXCELLENT_MIN = 0.69 * MAX_ADU          # 45,239
P95_EXCELLENT_MAX = 0.84 * MAX_ADU          # 55,049
P95_GOOD_MIN = 0.53 * MAX_ADU               # 34,733
P95_GOOD_MAX = 0.92 * MAX_ADU               # 60,292
P95_ACCEPTABLE_MIN = 0.30 * MAX_ADU         # 19,660

# Default target
DEFAULT_TARGET_P95 = 0.75  # 75% of max ADU = 49,151


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class ExposureMetrics:
    """Quantitative metrics for evaluating image exposure quality."""

    # Raw measurements
    p01: float
    p05: float
    p25: float
    p50: float
    p75: float
    p95: float
    p99: float
    min_value: int
    max_value: int
    mean_value: float
    std_value: float

    # Derived metrics (0.0 to 1.0 scale)
    p95_utilization: float          # P95 / MAX_ADU
    p99_utilization: float          # P99 / MAX_ADU
    dynamic_range: float            # (P99 - P01) / MAX_ADU
    saturation_fraction: float      # Fraction of pixels >= SATURATION_HARD_LIMIT
    histogram_entropy: float        # Normalized Shannon entropy

    # Overall assessment
    quality_score: float            # Weighted composite score (0.0 - 1.0)
    quality_grade: str              # "Excellent", "Good", "Acceptable", "Poor"
    warnings: List[str] = field(default_factory=list)


@dataclass
class AutoExposeResult:
    """Result from auto_expose() function."""

    exposure_ms: int                # Optimal exposure time found
    converged: bool                 # Whether algorithm converged
    iterations: int                 # Number of iterations used
    final_metrics: ExposureMetrics  # Metrics of final image
    history: List[Tuple[int, float, float]]  # (exposure_ms, p95_util, quality_score)

    # Scene analysis
    scene_type: str                 # "Very Dark", "Dark", "Normal", "Bright"
    initial_exposure_ms: int        # Starting exposure
    target_p95: float               # Target P95 utilization


# =============================================================================
# Core Evaluation Function
# =============================================================================

def evaluate_exposure(
    image: np.ndarray,
    max_adu: int = MAX_ADU,
    saturation_threshold: float = SATURATION_HARD_LIMIT
) -> ExposureMetrics:
    """
    Compute quantitative exposure quality metrics for an image.

    This function can evaluate ANY image - it doesn't require camera access.
    Use it to score existing images or compare different exposures.

    Args:
        image: 2D numpy array (grayscale image data)
        max_adu: Maximum ADU value for the camera (default: 65535 for 16-bit)
        saturation_threshold: ADU value above which pixels are considered saturated

    Returns:
        ExposureMetrics dataclass with all computed metrics

    Example:
        >>> metrics = evaluate_exposure(image)
        >>> print(f"Quality: {metrics.quality_grade} ({metrics.quality_score:.2f})")
        >>> if metrics.warnings:
        >>>     print(f"Warnings: {metrics.warnings}")
    """
    warnings = []

    # Flatten for histogram calculations
    flat = image.flatten().astype(np.float64)

    # Calculate percentiles
    p01, p05, p25, p50, p75, p95, p99 = np.percentile(
        flat, [1, 5, 25, 50, 75, 95, 99]
    )

    # Basic statistics
    min_val = int(np.min(image))
    max_val = int(np.max(image))
    mean_val = float(np.mean(flat))
    std_val = float(np.std(flat))

    # Derived metrics (normalized to 0-1)
    p95_util = p95 / max_adu
    p99_util = p99 / max_adu
    dynamic_range = (p99 - p01) / max_adu

    # Saturation fraction
    saturated_pixels = np.sum(image >= saturation_threshold)
    sat_fraction = saturated_pixels / image.size

    # Histogram entropy (normalized)
    # Use 256 bins for efficiency, normalize to max possible entropy
    hist, _ = np.histogram(flat, bins=256, range=(0, max_adu))
    hist = hist / hist.sum()  # Normalize to probabilities
    # Filter zeros to avoid log(0)
    hist_nonzero = hist[hist > 0]
    entropy = -np.sum(hist_nonzero * np.log2(hist_nonzero))
    max_entropy = np.log2(256)  # Maximum possible entropy for 256 bins
    normalized_entropy = entropy / max_entropy

    # Generate warnings
    if sat_fraction > 0.01:
        warnings.append(f"High saturation: {sat_fraction*100:.1f}% of pixels saturated")
    elif sat_fraction > 0.001:
        warnings.append(f"Minor saturation: {sat_fraction*100:.2f}% of pixels saturated")

    if p95_util < 0.30:
        warnings.append(f"Underexposed: P95 at {p95_util*100:.0f}% of max")

    if p99_util > 0.95:
        warnings.append(f"Near saturation: P99 at {p99_util*100:.0f}% of max")

    if dynamic_range < 0.20:
        warnings.append(f"Low dynamic range: {dynamic_range*100:.0f}%")

    if max_val == max_adu:
        warnings.append("Clipping detected: pixels at maximum ADU")

    # Calculate quality score
    quality_score = _calculate_quality_score(
        p95_util=p95_util,
        p99_util=p99_util,
        sat_fraction=sat_fraction,
        dynamic_range=dynamic_range,
        normalized_entropy=normalized_entropy
    )

    # Determine quality grade
    quality_grade = _determine_grade(quality_score, sat_fraction, p95_util)

    return ExposureMetrics(
        p01=p01, p05=p05, p25=p25, p50=p50, p75=p75, p95=p95, p99=p99,
        min_value=min_val, max_value=max_val,
        mean_value=mean_val, std_value=std_val,
        p95_utilization=p95_util,
        p99_utilization=p99_util,
        dynamic_range=dynamic_range,
        saturation_fraction=sat_fraction,
        histogram_entropy=normalized_entropy,
        quality_score=quality_score,
        quality_grade=quality_grade,
        warnings=warnings
    )


def _calculate_quality_score(
    p95_util: float,
    p99_util: float,
    sat_fraction: float,
    dynamic_range: float,
    normalized_entropy: float
) -> float:
    """
    Calculate weighted quality score from individual metrics.

    Weights based on scientific imaging priorities:
    - No saturation is critical (30%)
    - Good exposure level is primary goal (35%)
    - Safety margin from clipping (15%)
    - Dynamic range utilization (10%)
    - Information content (10%)
    """
    # 1. Saturation penalty (30% weight) - Critical
    # Any saturation is bad, >1% is very bad
    if sat_fraction < 0.0001:
        sat_score = 1.0
    elif sat_fraction < 0.001:
        sat_score = 0.9
    elif sat_fraction < 0.01:
        sat_score = 0.6
    elif sat_fraction < 0.05:
        sat_score = 0.3
    else:
        sat_score = 0.0

    # 2. P95 utilization score (35% weight) - Primary
    # Target: 0.70-0.85 is optimal
    if 0.69 <= p95_util <= 0.85:
        p95_score = 1.0
    elif 0.53 <= p95_util < 0.69:
        # Below optimal but acceptable
        p95_score = 0.7 + 0.3 * (p95_util - 0.53) / (0.69 - 0.53)
    elif 0.85 < p95_util <= 0.92:
        # Above optimal but acceptable
        p95_score = 1.0 - 0.3 * (p95_util - 0.85) / (0.92 - 0.85)
    elif 0.30 <= p95_util < 0.53:
        # Underexposed
        p95_score = 0.3 + 0.4 * (p95_util - 0.30) / (0.53 - 0.30)
    elif p95_util > 0.92:
        # Too close to saturation
        p95_score = 0.7 - 0.7 * min(1.0, (p95_util - 0.92) / 0.08)
    else:
        # Severely underexposed
        p95_score = p95_util / 0.30 * 0.3

    # 3. P99 safety margin (15% weight)
    # P99 should stay below 90% for safety
    if p99_util < 0.85:
        p99_score = 1.0
    elif p99_util < 0.90:
        p99_score = 0.8
    elif p99_util < 0.95:
        p99_score = 0.5
    else:
        p99_score = 0.2

    # 4. Dynamic range (10% weight)
    # Want at least 40% of range used
    dr_score = min(1.0, dynamic_range / 0.50)

    # 5. Entropy (10% weight)
    # Higher entropy = more information content
    entropy_score = normalized_entropy

    # Weighted combination
    quality_score = (
        0.30 * sat_score +
        0.35 * p95_score +
        0.15 * p99_score +
        0.10 * dr_score +
        0.10 * entropy_score
    )

    return quality_score


def _determine_grade(quality_score: float, sat_fraction: float, p95_util: float) -> str:
    """Determine quality grade from score and critical metrics."""
    # Saturation is an automatic downgrade
    if sat_fraction > 0.01:
        return "Poor"
    if sat_fraction > 0.001:
        if quality_score >= 0.70:
            return "Acceptable"
        return "Poor"

    # Score-based grading
    if quality_score >= 0.85:
        return "Excellent"
    elif quality_score >= 0.70:
        return "Good"
    elif quality_score >= 0.50:
        return "Acceptable"
    else:
        return "Poor"


# =============================================================================
# Auto-Exposure Algorithm
# =============================================================================

def auto_expose(
    camera,
    capture_func: Optional[Callable[[int], np.ndarray]] = None,
    target_p95: float = DEFAULT_TARGET_P95,
    min_exposure_ms: int = 1,
    max_exposure_ms: int = 30000,
    initial_exposure_ms: int = 100,
    max_iterations: int = 8,
    quality_threshold: float = 0.70,
    tolerance: float = 0.10,
    binning: Tuple[int, int] = (1, 1),
    flushes: int = 1,
    logger: Optional[logging.Logger] = None
) -> AutoExposeResult:
    """
    Find optimal exposure time using binary search with quality evaluation.

    Uses still frame captures (not video mode) for accurate measurement.
    The algorithm:
    1. Takes an initial analysis frame to classify the scene
    2. Estimates starting exposure using linear CCD response
    3. Refines using binary search until convergence or max iterations
    4. Returns when quality score exceeds threshold or best found

    Args:
        camera: Connected USBCamera instance with take_photo() method
        capture_func: Optional custom capture function(exposure_ms) -> image
                     If None, uses camera.take_photo() after set_exposure()
        target_p95: Target P95 as fraction of max ADU (default: 0.75)
        min_exposure_ms: Minimum exposure time in milliseconds
        max_exposure_ms: Maximum exposure time in milliseconds
        initial_exposure_ms: Starting exposure for analysis frame
        max_iterations: Maximum refinement iterations
        quality_threshold: Minimum quality score to accept (default: 0.70)
        tolerance: Acceptable deviation from target (default: 0.10 = 10%)
        binning: Pixel binning (hbin, vbin) - applied before capture
        flushes: Number of CCD flushes before each capture
        logger: Optional logger for status messages

    Returns:
        AutoExposeResult with optimal exposure and metrics

    Example:
        >>> from fli.core.camera import USBCamera
        >>> camera = USBCamera(...)
        >>> result = auto_expose(camera, target_p95=0.75)
        >>> print(f"Exposure: {result.exposure_ms}ms")
        >>> print(f"Quality: {result.final_metrics.quality_grade}")
    """
    log = logger or logging.getLogger(__name__)

    # Setup capture function
    if capture_func is None:
        def capture_func(exp_ms: int) -> np.ndarray:
            camera.set_exposure(exp_ms, frametype="normal")
            camera.set_flushes(flushes)
            time.sleep(0.05)  # Brief delay for settings to take effect
            return camera.take_photo()

    # Apply binning if camera supports it
    try:
        hbin, vbin = binning
        camera.set_image_binning(hbin, vbin)
    except Exception:
        pass  # Binning may already be set or not supported

    log.info(f"Auto-exposure starting (target P95: {target_p95*100:.0f}%)")

    # Track history
    history: List[Tuple[int, float, float]] = []

    # =========================================================================
    # Phase 1: Scene Analysis
    # =========================================================================
    log.info("Phase 1: Scene analysis...")

    try:
        analysis_image = capture_func(initial_exposure_ms)
        analysis_metrics = evaluate_exposure(analysis_image)

        log.info(f"  Analysis frame: P95={analysis_metrics.p95:.0f} ADU "
                f"({analysis_metrics.p95_utilization*100:.1f}%), "
                f"P99={analysis_metrics.p99:.0f} ADU")

        history.append((
            initial_exposure_ms,
            analysis_metrics.p95_utilization,
            analysis_metrics.quality_score
        ))

    except Exception as e:
        log.error(f"  Analysis capture failed: {e}")
        # Return with initial exposure if analysis fails
        return AutoExposeResult(
            exposure_ms=initial_exposure_ms,
            converged=False,
            iterations=0,
            final_metrics=ExposureMetrics(
                p01=0, p05=0, p25=0, p50=0, p75=0, p95=0, p99=0,
                min_value=0, max_value=0, mean_value=0, std_value=0,
                p95_utilization=0, p99_utilization=0, dynamic_range=0,
                saturation_fraction=0, histogram_entropy=0,
                quality_score=0, quality_grade="Unknown",
                warnings=["Analysis capture failed"]
            ),
            history=history,
            scene_type="Unknown",
            initial_exposure_ms=initial_exposure_ms,
            target_p95=target_p95
        )

    # Classify scene based on analysis
    scene_type = _classify_scene(analysis_metrics)
    log.info(f"  Scene type: {scene_type}")

    # =========================================================================
    # Phase 2: Initial Estimate
    # =========================================================================
    log.info("Phase 2: Estimating optimal exposure...")

    target_p95_adu = target_p95 * MAX_ADU
    current_p95 = analysis_metrics.p95

    # Linear estimate (CCD response is linear)
    if current_p95 > 100:
        estimated_exposure = int(initial_exposure_ms * (target_p95_adu / current_p95))
    elif analysis_metrics.saturation_fraction > 0.001:
        # Saturated - reduce significantly
        estimated_exposure = int(initial_exposure_ms * 0.3)
    else:
        # Very dark - increase significantly
        estimated_exposure = int(initial_exposure_ms * 5)

    # Clamp to valid range
    estimated_exposure = max(min_exposure_ms, min(max_exposure_ms, estimated_exposure))
    log.info(f"  Initial estimate: {estimated_exposure}ms")

    # =========================================================================
    # Phase 3: Binary Search Refinement
    # =========================================================================
    log.info("Phase 3: Refining exposure...")

    low_exp = min_exposure_ms
    high_exp = max_exposure_ms
    current_exp = estimated_exposure

    best_exp = current_exp
    best_metrics = analysis_metrics
    best_score = 0.0

    for iteration in range(max_iterations):
        log.info(f"  Iteration {iteration + 1}/{max_iterations}: testing {current_exp}ms")

        try:
            test_image = capture_func(current_exp)
            metrics = evaluate_exposure(test_image)

            p95_util = metrics.p95_utilization
            sat_frac = metrics.saturation_fraction
            score = metrics.quality_score

            log.info(f"    P95: {metrics.p95:.0f} ADU ({p95_util*100:.1f}%), "
                    f"sat: {sat_frac*100:.3f}%, score: {score:.2f}")

            history.append((current_exp, p95_util, score))

            # Track best result
            if score > best_score and sat_frac < 0.01:
                best_score = score
                best_exp = current_exp
                best_metrics = metrics

            # Check convergence criteria
            error = abs(p95_util - target_p95) / target_p95

            if (error < tolerance and
                sat_frac < 0.001 and
                score >= quality_threshold):
                log.info(f"  Converged! Exposure: {current_exp}ms, "
                        f"quality: {metrics.quality_grade}")

                return AutoExposeResult(
                    exposure_ms=current_exp,
                    converged=True,
                    iterations=iteration + 1,
                    final_metrics=metrics,
                    history=history,
                    scene_type=scene_type,
                    initial_exposure_ms=initial_exposure_ms,
                    target_p95=target_p95
                )

            # Binary search adjustment
            if sat_frac > 0.001 or p95_util > target_p95:
                # Too bright or saturated - reduce exposure
                high_exp = current_exp
                current_exp = (low_exp + current_exp) // 2
            else:
                # Too dark - increase exposure
                low_exp = current_exp
                current_exp = (current_exp + high_exp) // 2

            # Prevent tiny adjustments
            if abs(current_exp - best_exp) < max(1, best_exp * 0.02):
                log.info(f"  Converged (minimal change)")
                break

        except Exception as e:
            log.warning(f"    Capture failed: {e}")
            # Try middle of remaining range
            current_exp = (low_exp + high_exp) // 2
            continue

    # Use best found
    log.info(f"  Final exposure: {best_exp}ms ({best_metrics.quality_grade})")

    return AutoExposeResult(
        exposure_ms=best_exp,
        converged=best_score >= quality_threshold,
        iterations=len(history),
        final_metrics=best_metrics,
        history=history,
        scene_type=scene_type,
        initial_exposure_ms=initial_exposure_ms,
        target_p95=target_p95
    )


def _classify_scene(metrics: ExposureMetrics) -> str:
    """Classify scene type based on initial analysis metrics."""
    p50_util = metrics.p50 / MAX_ADU
    dr = metrics.dynamic_range

    # Brightness classification
    if p50_util < 0.02:
        brightness = "Very Dark"
    elif p50_util < 0.10:
        brightness = "Dark"
    elif p50_util > 0.50:
        brightness = "Bright"
    else:
        brightness = "Normal"

    # Contrast classification
    if dr < 0.10:
        contrast = " (Low Contrast)"
    elif dr > 0.50:
        contrast = " (High Contrast)"
    else:
        contrast = ""

    return brightness + contrast


# =============================================================================
# Convenience Functions
# =============================================================================

def is_well_exposed(image: np.ndarray, strict: bool = False) -> bool:
    """
    Quick check if an image is acceptably exposed.

    Args:
        image: Image to evaluate
        strict: If True, requires "Excellent" grade; if False, accepts "Good"

    Returns:
        True if exposure quality is acceptable
    """
    metrics = evaluate_exposure(image)

    if strict:
        return metrics.quality_grade == "Excellent"
    else:
        return metrics.quality_grade in ("Excellent", "Good")


def suggest_exposure_adjustment(
    image: np.ndarray,
    current_exposure_ms: int,
    target_p95: float = DEFAULT_TARGET_P95
) -> Tuple[int, str]:
    """
    Suggest exposure adjustment based on current image.

    Does NOT capture new images - just analyzes the provided image
    and suggests what the next exposure should be.

    Args:
        image: Current image to analyze
        current_exposure_ms: Exposure time used for this image
        target_p95: Target P95 utilization (default: 0.75)

    Returns:
        Tuple of (suggested_exposure_ms, reason)

    Example:
        >>> new_exp, reason = suggest_exposure_adjustment(image, 100)
        >>> print(f"Suggested: {new_exp}ms ({reason})")
    """
    metrics = evaluate_exposure(image)

    # Check saturation first
    if metrics.saturation_fraction > 0.001:
        # Saturated - reduce based on severity
        factor = 0.5 if metrics.saturation_fraction > 0.01 else 0.7
        new_exp = int(current_exposure_ms * factor)
        return max(1, new_exp), "reducing due to saturation"

    # Calculate ratio needed
    target_adu = target_p95 * MAX_ADU
    current_p95 = metrics.p95

    if current_p95 < 100:
        # Very dark - can't reliably estimate
        new_exp = current_exposure_ms * 3
        return min(30000, new_exp), "increasing (very dark scene)"

    ratio = target_adu / current_p95

    # Apply damping to prevent overshoot
    damped_ratio = 1.0 + 0.7 * (ratio - 1.0)
    new_exp = int(current_exposure_ms * damped_ratio)

    # Clamp
    new_exp = max(1, min(30000, new_exp))

    if new_exp > current_exposure_ms:
        reason = f"increasing (P95 at {metrics.p95_utilization*100:.0f}%)"
    elif new_exp < current_exposure_ms:
        reason = f"decreasing (P95 at {metrics.p95_utilization*100:.0f}%)"
    else:
        reason = "no change needed"

    return new_exp, reason


# =============================================================================
# Main (for testing)
# =============================================================================

if __name__ == "__main__":
    # Test with synthetic image
    print("Auto-Exposure Module Test")
    print("=" * 50)

    # Create synthetic test image (simulated 16-bit CCD data)
    np.random.seed(42)

    # Normal exposure simulation
    test_image = np.random.normal(30000, 8000, (1024, 1024))
    test_image = np.clip(test_image, 0, 65535).astype(np.uint16)

    print("\nTest 1: Normal exposure")
    metrics = evaluate_exposure(test_image)
    print(f"  P95: {metrics.p95:.0f} ADU ({metrics.p95_utilization*100:.1f}%)")
    print(f"  Quality: {metrics.quality_grade} ({metrics.quality_score:.2f})")
    print(f"  Warnings: {metrics.warnings or 'None'}")

    # Underexposed simulation
    test_image_dark = np.random.normal(5000, 2000, (1024, 1024))
    test_image_dark = np.clip(test_image_dark, 0, 65535).astype(np.uint16)

    print("\nTest 2: Underexposed")
    metrics = evaluate_exposure(test_image_dark)
    print(f"  P95: {metrics.p95:.0f} ADU ({metrics.p95_utilization*100:.1f}%)")
    print(f"  Quality: {metrics.quality_grade} ({metrics.quality_score:.2f})")
    print(f"  Warnings: {metrics.warnings or 'None'}")

    # Saturated simulation
    test_image_sat = np.random.normal(55000, 10000, (1024, 1024))
    test_image_sat = np.clip(test_image_sat, 0, 65535).astype(np.uint16)

    print("\nTest 3: Near saturation")
    metrics = evaluate_exposure(test_image_sat)
    print(f"  P95: {metrics.p95:.0f} ADU ({metrics.p95_utilization*100:.1f}%)")
    print(f"  Saturation: {metrics.saturation_fraction*100:.2f}%")
    print(f"  Quality: {metrics.quality_grade} ({metrics.quality_score:.2f})")
    print(f"  Warnings: {metrics.warnings or 'None'}")

    # Test suggestion function
    print("\nTest 4: Exposure adjustment suggestion")
    suggested, reason = suggest_exposure_adjustment(test_image_dark, 100)
    print(f"  Current: 100ms")
    print(f"  Suggested: {suggested}ms ({reason})")

    print("\n" + "=" * 50)
    print("Tests complete")
