"""
Session logging system for PTU and camera operations.

Provides structured logging with timestamps, operation tracking, and
JSON export for post-session analysis.
"""

import logging
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional
from dataclasses import dataclass, asdict


@dataclass
class LogEntry:
    """Structured log entry for a single operation.

    Parameters
    ----------
    timestamp : str
        ISO 8601 timestamp of the operation.
    operation : str
        Name of the operation (e.g., "move", "capture", "initialize").
    component : str
        System component (e.g., "PTU", "Camera", "System").
    details : dict
        Operation-specific parameters and context.
    success : bool
        Whether the operation completed successfully.
    duration_ms : float, optional
        Operation duration in milliseconds.
    error_message : str, optional
        Error description if the operation failed.
    """
    timestamp: str
    operation: str
    component: str
    details: Dict[str, Any]
    success: bool
    duration_ms: Optional[float] = None
    error_message: Optional[str] = None


class SessionLogger:
    """Session logger for PTU and camera operations.

    Creates structured logs with both human-readable text output and
    machine-parseable JSON export. Each session generates a `.log` file
    for text output and a `.json` file for structured data.

    Parameters
    ----------
    log_dir : str
        Directory for log file output.
    session_name : str, optional
        Session identifier. Auto-generated from timestamp if not provided.
    """

    def __init__(self, log_dir: str = "./logs",
                 session_name: Optional[str] = None):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)

        if session_name is None:
            session_name = f"ptu_session_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        self.session_name = session_name
        self.session_start = datetime.now()

        # Setup file paths
        self.log_file = self.log_dir / f"{session_name}.log"
        self.json_file = self.log_dir / f"{session_name}.json"

        # Setup logging
        self._setup_logging()

        # Session tracking
        self.session_entries: list[LogEntry] = []
        self.operation_count = 0

        self.logger.info(f"Session started: {session_name}")

    def _setup_logging(self):
        """Configure logging with both file and console output."""
        self.logger = logging.getLogger(f"ptu_session_{self.session_name}")
        self.logger.setLevel(logging.DEBUG)

        # Clear any existing handlers
        self.logger.handlers.clear()

        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )

        # File handler
        file_handler = logging.FileHandler(self.log_file)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)

        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(formatter)

        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)

    def log_operation(self, operation: str, component: str,
                      details: Dict[str, Any], success: bool,
                      duration_ms: Optional[float] = None,
                      error_message: Optional[str] = None):
        """Log a structured operation entry.

        Parameters
        ----------
        operation : str
            Operation name.
        component : str
            System component performing the operation.
        details : dict
            Operation parameters and context.
        success : bool
            Whether the operation succeeded.
        duration_ms : float, optional
            Operation duration in milliseconds.
        error_message : str, optional
            Error description if failed.
        """
        entry = LogEntry(
            timestamp=datetime.now().isoformat(),
            operation=operation,
            component=component,
            details=details,
            success=success,
            duration_ms=duration_ms,
            error_message=error_message
        )

        self.session_entries.append(entry)
        self.operation_count += 1

        # Log to standard logger
        log_level = logging.INFO if success else logging.ERROR
        message = f"{component} {operation}: {'SUCCESS' if success else 'FAILED'}"
        if duration_ms:
            message += f" ({duration_ms:.1f}ms)"
        if error_message:
            message += f" - {error_message}"

        self.logger.log(log_level, message)

        # Save to JSON file
        self._save_json_log()

    def _save_json_log(self):
        """Save session data to JSON file."""
        session_data = {
            'session_name': self.session_name,
            'session_start': self.session_start.isoformat(),
            'operation_count': self.operation_count,
            'entries': [asdict(entry) for entry in self.session_entries]
        }

        with open(self.json_file, 'w') as f:
            json.dump(session_data, f, indent=2)

    def log_ptu_initialization(self, config_dict: Dict[str, Any],
                               success: bool, duration_ms: float,
                               error_message: Optional[str] = None):
        """Log PTU initialization."""
        self.log_operation(
            operation="initialize",
            component="PTU",
            details={"config": config_dict},
            success=success,
            duration_ms=duration_ms,
            error_message=error_message
        )

    def log_ptu_movement(self, target_position: Dict[str, Any],
                         success: bool, duration_ms: float,
                         error_message: Optional[str] = None):
        """Log PTU movement operation."""
        self.log_operation(
            operation="move",
            component="PTU",
            details=target_position,
            success=success,
            duration_ms=duration_ms,
            error_message=error_message
        )

    def log_camera_operation(self, operation_type: str,
                             parameters: Dict[str, Any],
                             success: bool, duration_ms: float,
                             error_message: Optional[str] = None):
        """Log camera operation."""
        self.log_operation(
            operation=operation_type,
            component="Camera",
            details=parameters,
            success=success,
            duration_ms=duration_ms,
            error_message=error_message
        )

    def log_sequence_start(self, sequence_name: str, total_positions: int):
        """Log the start of a position sequence."""
        self.log_operation(
            operation="sequence_start",
            component="System",
            details={
                "sequence_name": sequence_name,
                "total_positions": total_positions
            },
            success=True
        )

    def log_sequence_complete(self, sequence_name: str,
                              completed_positions: int,
                              total_duration_ms: float):
        """Log completion of a position sequence."""
        avg_time = (total_duration_ms / completed_positions
                    if completed_positions > 0 else 0)
        self.log_operation(
            operation="sequence_complete",
            component="System",
            details={
                "sequence_name": sequence_name,
                "completed_positions": completed_positions,
                "average_time_per_position": avg_time
            },
            success=True,
            duration_ms=total_duration_ms
        )

    def get_session_summary(self) -> Dict[str, Any]:
        """Get summary statistics for the current session.

        Returns
        -------
        dict
            Session statistics including operation counts, success rates,
            and timing information.
        """
        if not self.session_entries:
            return {"message": "No operations logged yet"}

        total_operations = len(self.session_entries)
        successful_operations = sum(
            1 for entry in self.session_entries if entry.success
        )

        ptu_operations = [
            e for e in self.session_entries if e.component == "PTU"
        ]
        camera_operations = [
            e for e in self.session_entries if e.component == "Camera"
        ]

        durations = [
            e.duration_ms for e in self.session_entries
            if e.duration_ms is not None
        ]
        avg_duration = sum(durations) / len(durations) if durations else 0

        return {
            "session_name": self.session_name,
            "session_duration": (
                datetime.now() - self.session_start
            ).total_seconds(),
            "total_operations": total_operations,
            "successful_operations": successful_operations,
            "failed_operations": total_operations - successful_operations,
            "success_rate": (
                successful_operations / total_operations
                if total_operations > 0 else 0
            ),
            "ptu_operations": len(ptu_operations),
            "camera_operations": len(camera_operations),
            "average_operation_duration_ms": avg_duration,
            "log_files": {
                "text_log": str(self.log_file),
                "json_log": str(self.json_file)
            }
        }

    def close_session(self):
        """Close the logging session and save final summary."""
        summary = self.get_session_summary()

        self.logger.info("=" * 50)
        self.logger.info("SESSION SUMMARY")
        self.logger.info("=" * 50)
        self.logger.info(f"Total Operations: {summary['total_operations']}")
        self.logger.info(f"Success Rate: {summary['success_rate']:.1%}")
        self.logger.info(f"PTU Operations: {summary['ptu_operations']}")
        self.logger.info(f"Camera Operations: {summary['camera_operations']}")
        self.logger.info(
            f"Average Duration: "
            f"{summary['average_operation_duration_ms']:.1f}ms"
        )
        self.logger.info(f"Session Duration: {summary['session_duration']:.1f}s")
        self.logger.info("=" * 50)

        # Save final summary to JSON
        summary_file = self.log_dir / f"{self.session_name}_summary.json"
        with open(summary_file, 'w') as f:
            json.dump(summary, f, indent=2)


class OperationTimer:
    """Context manager for timing and logging operations.

    Automatically measures operation duration and logs the result
    via the provided SessionLogger.

    Parameters
    ----------
    logger : SessionLogger
        Logger instance for recording the operation.
    operation : str
        Operation name.
    component : str
        System component performing the operation.
    details : dict
        Operation parameters and context.

    Examples
    --------
    >>> with OperationTimer(logger, "move", "PTU", {"target": 1000}) as timer:
    ...     ptu.move_to_position(1000, 0)
    ...     timer.mark_success()
    """

    def __init__(self, logger: SessionLogger, operation: str,
                 component: str, details: Dict[str, Any]):
        self.logger = logger
        self.operation = operation
        self.component = component
        self.details = details
        self.start_time: Optional[float] = None
        self.success = False
        self.error_message: Optional[str] = None

    def __enter__(self):
        self.start_time = time.time()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        duration_ms = (time.time() - self.start_time) * 1000

        if exc_type is not None:
            self.success = False
            self.error_message = str(exc_val)

        self.logger.log_operation(
            operation=self.operation,
            component=self.component,
            details=self.details,
            success=self.success,
            duration_ms=duration_ms,
            error_message=self.error_message
        )

    def mark_success(self):
        """Mark the operation as successful."""
        self.success = True
