"""
Configuration File Resolution

Locates the project ``config/`` directory and loads JSON specification
files. Resolution order:

1. The ``TERRACAM_CONFIG`` environment variable, if set, must point to a
   directory containing the specification JSON files.
2. Otherwise, walk upward from this module's location looking for a
   ``config/filter_specifications.json`` (works for the editable-install
   repo layout).
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_SENTINEL_FILE = "filter_specifications.json"


def find_config_dir() -> Path:
    """Locate the project config/ directory.

    Returns
    -------
    Path
        Path to the ``config/`` directory.

    Raises
    ------
    FileNotFoundError
        If no config directory can be located, or ``TERRACAM_CONFIG``
        points somewhere that lacks the specification files.
    """
    env_dir = os.environ.get("TERRACAM_CONFIG")
    if env_dir:
        path = Path(env_dir)
        if not (path / _SENTINEL_FILE).exists():
            raise FileNotFoundError(
                f"TERRACAM_CONFIG={env_dir} does not contain "
                f"{_SENTINEL_FILE}"
            )
        return path

    current = Path(__file__).resolve().parent
    for _ in range(6):
        candidate = current / "config" / _SENTINEL_FILE
        if candidate.exists():
            return current / "config"
        current = current.parent

    raise FileNotFoundError(
        "Cannot locate the config/ directory; set the TERRACAM_CONFIG "
        "environment variable to point at it"
    )


def load_config(filename: str) -> Optional[Dict[str, Any]]:
    """Load a JSON config file from the project config directory.

    Parameters
    ----------
    filename : str
        Config filename (e.g., ``"filter_specifications.json"``).

    Returns
    -------
    dict or None
        Parsed config, or None (with a logged warning) if the file could
        not be located or parsed.
    """
    try:
        path = find_config_dir() / filename
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        logger.warning("Could not load config %s: %s", filename, e)
        return None
