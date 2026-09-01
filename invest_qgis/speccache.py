"""Persist harvested InVEST model specs between QGIS sessions.

Raw specs are cached rather than normalised ones, so that improvements to
:mod:`normalize` take effect immediately without forcing a re-harvest.
"""

import json
import os
import time

CACHE_FILENAME = "invest_specs.json"
CACHE_FORMAT = 1


def cache_path(cache_dir):
    return os.path.join(cache_dir, CACHE_FILENAME)


def _binary_fingerprint(binary_path):
    """Return a value that changes when the user points at a different InVEST."""
    try:
        return os.path.getmtime(binary_path)
    except OSError:
        return None


def save(cache_dir, binary_path, invest_version, specs):
    """Write harvested specs to disk."""
    os.makedirs(cache_dir, exist_ok=True)
    payload = {
        "format": CACHE_FORMAT,
        "invest_version": invest_version,
        "binary_path": binary_path,
        "binary_mtime": _binary_fingerprint(binary_path),
        "harvested_at": time.time(),
        "specs": specs,
    }
    target = cache_path(cache_dir)
    # Write via a temporary file so an interrupted save cannot leave a
    # truncated cache that would fail to parse on next startup.
    temporary = f"{target}.tmp"
    with open(temporary, "w", encoding="utf-8") as file_handle:
        json.dump(payload, file_handle)
    os.replace(temporary, target)
    return target


def load(cache_dir, binary_path=None):
    """Return the cached payload, or ``None`` if unusable.

    A cache built from a different InVEST installation is ignored, so switching
    to another Workbench version transparently triggers a fresh harvest.
    """
    target = cache_path(cache_dir)
    try:
        with open(target, encoding="utf-8") as file_handle:
            payload = json.load(file_handle)
    except (OSError, ValueError):
        return None

    if payload.get("format") != CACHE_FORMAT or not payload.get("specs"):
        return None

    if binary_path:
        if payload.get("binary_path") != binary_path:
            return None
        fingerprint = _binary_fingerprint(binary_path)
        if fingerprint is not None and payload.get("binary_mtime") != fingerprint:
            return None

    return payload


def clear(cache_dir):
    """Remove the cache file if present."""
    try:
        os.remove(cache_path(cache_dir))
    except OSError:
        pass
