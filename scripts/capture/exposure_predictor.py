#!/usr/bin/env python3
"""
Exposure Time Predictor for Multi-Filter Imaging

Uses characterized filter ratios to predict optimal exposure times across
all spectral channels from a single auto-exposure measurement.

This enables fast multi-filter imaging by:
1. Running auto-exposure on ONE reference filter
2. Predicting exposures for all other filters using pre-characterized ratios
3. Optionally validating/refining predictions during capture

Theory:
-------
For a linear CCD under stable illumination:
    Signal_i / Signal_ref = Throughput_i / Throughput_ref

Since we target equal P95 levels:
    Exposure_i = Exposure_ref × (Throughput_ref / Throughput_i)
    Exposure_i = Exposure_ref × ExposureRatio_i

Usage:
------
    from scripts.capture.exposure_predictor import ExposurePredictor

    # Load characterized ratios
    predictor = ExposurePredictor("calibration_data/filter_ratios.json")

    # Get reference exposure (from auto_expose or manual)
    ref_exposure = 500  # ms at filter 0

    # Predict all filter exposures
    predictions = predictor.predict_all(ref_exposure, reference_filter=0)
    for pos, exp_ms in predictions.items():
        print(f"Filter {pos}: {exp_ms}ms")

    # Or predict single filter
    exp_filter_5 = predictor.predict(ref_exposure, target_filter=5, reference_filter=0)
"""

import json
import logging
from pathlib import Path
from typing import Dict, Optional, Tuple, List
from dataclasses import dataclass


@dataclass
class FilterRatio:
    """Pre-characterized filter ratio data."""
    filter_position: int
    filter_name: str
    exposure_ratio: float  # Multiply reference exposure by this
    signal_ratio: float    # This filter's signal / reference signal
    confidence: float      # Measurement confidence (0-1)


class ExposurePredictor:
    """
    Predict exposure times across filters using characterized ratios.

    The predictor uses pre-measured filter throughput ratios to estimate
    optimal exposure times without running auto-exposure on each filter.
    """

    def __init__(
        self,
        ratios_file: Optional[str] = None,
        logger: Optional[logging.Logger] = None
    ):
        """
        Initialize predictor.

        Args:
            ratios_file: Path to filter ratios JSON file from characterization.
                        If None, uses default ratios based on typical VNIR filters.
            logger: Optional logger instance
        """
        self.logger = logger or logging.getLogger(__name__)
        self.ratios: Dict[int, FilterRatio] = {}
        self.reference_filter: int = 0
        self.calibration_timestamp: Optional[str] = None
        self.scene_type: Optional[str] = None

        if ratios_file:
            self.load_ratios(ratios_file)
        else:
            self._use_default_ratios()

    def load_ratios(self, ratios_file: str):
        """Load filter ratios from characterization JSON file."""
        path = Path(ratios_file)
        if not path.exists():
            self.logger.warning(f"Ratios file not found: {ratios_file}")
            self.logger.warning("Using default ratios")
            self._use_default_ratios()
            return

        with open(path, 'r') as f:
            data = json.load(f)

        self.reference_filter = data.get('reference_filter', 0)
        self.calibration_timestamp = data.get('timestamp')
        self.scene_type = data.get('scene_type')
        self.source = data.get('source', 'unknown')
        self.reference_wavelength_nm = data.get('reference_wavelength_nm')

        for pos_str, ratio_data in data.get('filter_ratios', {}).items():
            pos = int(pos_str)
            self.ratios[pos] = FilterRatio(
                filter_position=ratio_data['filter_position'],
                filter_name=ratio_data.get('filter_name', f'Filter_{pos}'),
                exposure_ratio=ratio_data['exposure_ratio'],
                signal_ratio=ratio_data['signal_ratio'],
                confidence=ratio_data.get('confidence', 0.5)
            )

        self.logger.info(f"Loaded {len(self.ratios)} filter ratios from {ratios_file}")
        self.logger.info(f"Reference filter: {self.reference_filter} ({self.reference_wavelength_nm}nm)")
        self.logger.info(f"Source: {self.source}")

    def _use_default_ratios(self):
        """
        Use default ratios based on typical VNIR filter response.

        These are approximate and should be replaced with actual
        characterization data for accurate predictions.

        Default assumes:
        - 8-position filter wheel with VNIR filters
        - Typical solar spectrum × atmospheric transmission × CCD QE
        """
        self.logger.warning("Using DEFAULT filter ratios - characterize for accuracy!")

        # Approximate ratios for typical VNIR filters (400-1000nm)
        # These assume filter 0 (typically 550nm green) as reference
        # Ratios derived from solar spectrum × typical QE × atm transmission
        default_ratios = {
            0: {'name': 'Green_550nm', 'exp_ratio': 1.00, 'sig_ratio': 1.00},
            1: {'name': 'Blue_450nm', 'exp_ratio': 1.30, 'sig_ratio': 0.77},
            2: {'name': 'Red_650nm', 'exp_ratio': 0.90, 'sig_ratio': 1.11},
            3: {'name': 'NIR_750nm', 'exp_ratio': 1.20, 'sig_ratio': 0.83},
            4: {'name': 'NIR_850nm', 'exp_ratio': 1.50, 'sig_ratio': 0.67},
            5: {'name': 'NIR_900nm', 'exp_ratio': 1.80, 'sig_ratio': 0.56},
            6: {'name': 'NIR_950nm', 'exp_ratio': 2.20, 'sig_ratio': 0.45},
            7: {'name': 'NIR_1000nm', 'exp_ratio': 3.00, 'sig_ratio': 0.33},
        }

        for pos, data in default_ratios.items():
            self.ratios[pos] = FilterRatio(
                filter_position=pos,
                filter_name=data['name'],
                exposure_ratio=data['exp_ratio'],
                signal_ratio=data['sig_ratio'],
                confidence=0.3  # Low confidence for defaults
            )

        self.reference_filter = 0
        self.scene_type = "Default (uncharacterized)"

    def predict(
        self,
        reference_exposure_ms: int,
        target_filter: int,
        reference_filter: Optional[int] = None,
        clamp: bool = True
    ) -> int:
        """
        Predict optimal exposure for a target filter.

        Args:
            reference_exposure_ms: Known exposure time at reference filter
            target_filter: Filter position to predict exposure for
            reference_filter: Reference filter position (default: calibration reference)
            clamp: If True, clamp result to 1-30000ms range

        Returns:
            Predicted exposure time in milliseconds
        """
        if reference_filter is None:
            reference_filter = self.reference_filter

        if target_filter not in self.ratios:
            self.logger.warning(f"No ratio data for filter {target_filter}, "
                              f"returning reference exposure")
            return reference_exposure_ms

        if reference_filter not in self.ratios:
            self.logger.warning(f"No ratio data for reference filter {reference_filter}")
            return reference_exposure_ms

        # Get ratios
        target_ratio = self.ratios[target_filter].exposure_ratio
        ref_ratio = self.ratios[reference_filter].exposure_ratio

        # Calculate relative ratio if reference isn't the calibration reference
        if reference_filter != self.reference_filter:
            # target_exp / ref_exp = target_ratio / ref_ratio
            relative_ratio = target_ratio / ref_ratio
        else:
            relative_ratio = target_ratio

        # Predict exposure
        predicted = int(reference_exposure_ms * relative_ratio)

        if clamp:
            predicted = max(1, min(30000, predicted))

        return predicted

    def predict_all(
        self,
        reference_exposure_ms: int,
        reference_filter: Optional[int] = None,
        filter_positions: Optional[List[int]] = None,
        clamp: bool = True
    ) -> Dict[int, int]:
        """
        Predict exposure times for all (or specified) filters.

        Args:
            reference_exposure_ms: Known exposure time at reference filter
            reference_filter: Reference filter position (default: calibration reference)
            filter_positions: List of filters to predict (default: all characterized)
            clamp: If True, clamp results to 1-30000ms range

        Returns:
            Dict mapping filter position to predicted exposure (ms)
        """
        if filter_positions is None:
            filter_positions = list(self.ratios.keys())

        predictions = {}
        for pos in filter_positions:
            predictions[pos] = self.predict(
                reference_exposure_ms,
                target_filter=pos,
                reference_filter=reference_filter,
                clamp=clamp
            )

        return predictions

    def get_confidence(self, filter_position: int) -> float:
        """Get prediction confidence for a filter (0-1)."""
        if filter_position in self.ratios:
            return self.ratios[filter_position].confidence
        return 0.0

    def get_low_confidence_filters(self, threshold: float = 0.7) -> List[int]:
        """Get list of filters with confidence below threshold."""
        return [pos for pos, ratio in self.ratios.items()
                if ratio.confidence < threshold]

    def print_predictions(
        self,
        reference_exposure_ms: int,
        reference_filter: Optional[int] = None
    ):
        """Print formatted prediction table."""
        predictions = self.predict_all(reference_exposure_ms, reference_filter)

        print("\n" + "=" * 60)
        print("EXPOSURE PREDICTIONS")
        print("=" * 60)
        print(f"Reference Exposure: {reference_exposure_ms}ms "
              f"(Filter {reference_filter or self.reference_filter})")
        print("-" * 60)
        print(f"{'Filter':>6} | {'Name':>15} | {'Exposure (ms)':>12} | {'Confidence':>10}")
        print("-" * 60)

        for pos in sorted(predictions.keys()):
            if pos in self.ratios:
                ratio = self.ratios[pos]
                conf_str = f"{ratio.confidence:.0%}"
                if ratio.confidence < 0.5:
                    conf_str += " ⚠️"
                print(f"{pos:>6} | {ratio.filter_name:>15} | {predictions[pos]:>12} | {conf_str:>10}")

        print("-" * 60)

        low_conf = self.get_low_confidence_filters(0.5)
        if low_conf:
            print(f"\n⚠️  Low confidence predictions for filters: {low_conf}")
            print("   Consider re-characterizing or using auto-expose for these.")


class AdaptiveExposurePredictor(ExposurePredictor):
    """
    Exposure predictor with runtime adaptation.

    Can update predictions based on actual measurements during capture,
    improving accuracy over time.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.runtime_corrections: Dict[int, float] = {}
        self.measurement_history: Dict[int, List[Tuple[int, int]]] = {}

    def record_measurement(
        self,
        filter_position: int,
        predicted_exposure_ms: int,
        actual_optimal_ms: int
    ):
        """
        Record actual vs predicted exposure for runtime adaptation.

        Call this after capturing and evaluating an image to improve
        future predictions for this filter.
        """
        if filter_position not in self.measurement_history:
            self.measurement_history[filter_position] = []

        self.measurement_history[filter_position].append(
            (predicted_exposure_ms, actual_optimal_ms)
        )

        # Calculate correction factor
        if predicted_exposure_ms > 0:
            correction = actual_optimal_ms / predicted_exposure_ms
            self.runtime_corrections[filter_position] = correction
            self.logger.info(f"Filter {filter_position}: correction factor = {correction:.3f}")

    def predict(self, *args, **kwargs) -> int:
        """Predict with runtime correction applied."""
        base_prediction = super().predict(*args, **kwargs)

        target_filter = kwargs.get('target_filter', args[1] if len(args) > 1 else None)
        if target_filter in self.runtime_corrections:
            corrected = int(base_prediction * self.runtime_corrections[target_filter])
            return max(1, min(30000, corrected))

        return base_prediction


# Convenience function for quick usage
def predict_exposures(
    reference_exposure_ms: int,
    reference_filter: int = 0,
    ratios_file: Optional[str] = None
) -> Dict[int, int]:
    """
    Quick function to predict exposures for all filters.

    Args:
        reference_exposure_ms: Exposure time at reference filter
        reference_filter: Which filter the reference exposure is for
        ratios_file: Path to filter ratios JSON (uses defaults if None)

    Returns:
        Dict mapping filter position to predicted exposure (ms)
    """
    predictor = ExposurePredictor(ratios_file)
    return predictor.predict_all(reference_exposure_ms, reference_filter)


if __name__ == "__main__":
    # Demo usage
    import sys

    print("Exposure Predictor Demo")
    print("=" * 50)

    # Check for ratios file argument
    ratios_file = sys.argv[1] if len(sys.argv) > 1 else None

    predictor = ExposurePredictor(ratios_file)

    # Example: reference exposure of 500ms at filter 0
    ref_exp = 500
    ref_filter = 0

    predictor.print_predictions(ref_exp, ref_filter)

    # Show how to use in code
    print("\n\nCode example:")
    print("-" * 50)
    print(f"reference_exposure = {ref_exp}  # ms at filter {ref_filter}")
    print("predictions = predictor.predict_all(reference_exposure)")
    print("\nfor filter_pos in [0, 3, 5, 7]:")
    print("    exp = predictions[filter_pos]")
    print("    capture_image(filter_pos, exp)")
