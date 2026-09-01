"""Locate the ``invest`` executable inside a user-supplied InVEST installation.

The InVEST Workbench ships a PyInstaller-frozen ``invest`` binary inside its
application bundle.  The user points the plugin at the application they
installed and we derive the executable from it.
"""

import glob
import os
import platform
import re
import subprocess

#: Locations of the frozen binary relative to the application the user picks.
#: macOS is the only layout verified against a real installation; the Windows
#: and Linux candidates are best-effort and are simply skipped if absent.
_RELATIVE_CANDIDATES = {
    "Darwin": [
        os.path.join("Contents", "Resources", "invest", "invest"),
    ],
    "Windows": [
        os.path.join("resources", "invest", "invest.exe"),
        os.path.join("invest", "invest.exe"),
        "invest.exe",
    ],
    "Linux": [
        os.path.join("resources", "invest", "invest"),
        os.path.join("invest", "invest"),
        "invest",
    ],
}

#: How long to wait for ``invest --version``.  The frozen binary is slow to
#: start (~60s measured on macOS), so this is generous on purpose.
VERSION_TIMEOUT = 180


class InvestNotFound(Exception):
    """Raised when no usable ``invest`` executable can be derived."""


def _candidates(app_path):
    """Yield plausible executable paths for a user-supplied application path."""
    # The user may have pointed straight at the binary.
    yield app_path
    for relative in _RELATIVE_CANDIDATES.get(platform.system(), []):
        yield os.path.join(app_path, relative)
    # Be forgiving across platforms: a user on an unrecognised system, or one
    # who copied a bundle between machines, still gets a working lookup.
    for candidates in _RELATIVE_CANDIDATES.values():
        for relative in candidates:
            yield os.path.join(app_path, relative)


def find_binary(app_path):
    """Return the path to the ``invest`` executable inside ``app_path``.

    Raises:
        InvestNotFound: if ``app_path`` is empty, missing, or contains no
            executable at any known location.
    """
    if not app_path:
        raise InvestNotFound(
            "No InVEST application configured. Set it in "
            "Processing > Options > Providers > InVEST.")

    app_path = os.path.expanduser(app_path.strip())
    if not os.path.exists(app_path):
        raise InvestNotFound(f"InVEST application does not exist: {app_path}")

    seen = set()
    for candidate in _candidates(app_path):
        if candidate in seen:
            continue
        seen.add(candidate)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    raise InvestNotFound(
        f"No InVEST executable found inside {app_path}. Expected something "
        f"like <App>.app/Contents/Resources/invest/invest on macOS.")


def binary_version(binary_path):
    """Return the version string reported by ``invest --version``.

    Raises:
        InvestNotFound: if the executable cannot be run or does not respond.
    """
    try:
        completed = subprocess.run(
            [binary_path, "--version"],
            capture_output=True, text=True, timeout=VERSION_TIMEOUT,
            stdin=subprocess.DEVNULL)
    except OSError as error:
        raise InvestNotFound(f"Could not run {binary_path}: {error}") from error
    except subprocess.TimeoutExpired as error:
        raise InvestNotFound(
            f"{binary_path} did not respond within {VERSION_TIMEOUT}s") from error

    if completed.returncode != 0:
        raise InvestNotFound(
            f"{binary_path} --version failed with code {completed.returncode}: "
            f"{completed.stderr.strip()}")

    # The frozen binary prints warnings (e.g. a Shapely FutureWarning, or a
    # matplotlib font-cache notice) before the version, so take the last
    # non-empty line rather than the whole of stdout.
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise InvestNotFound(f"{binary_path} --version produced no output")
    return lines[-1]


def resolve(app_path):
    """Return ``(binary_path, version)`` for a user-supplied application path."""
    binary_path = find_binary(app_path)
    return binary_path, binary_version(binary_path)


def quick_version(binary_path):
    """Return the InVEST version without running the binary.

    PyInstaller leaves a ``natcap_invest-<version>.dist-info`` directory beside
    the frozen executable, so the version can be read from the filesystem.
    Running ``invest --version`` instead would cost roughly a minute.

    Returns an empty string if the marker cannot be found.
    """
    pattern = os.path.join(
        os.path.dirname(binary_path), "_internal", "natcap_invest-*.dist-info")
    for candidate in sorted(glob.glob(pattern)):
        match = re.search(r"natcap_invest-(.+?)\.dist-info$", candidate)
        if match:
            return match.group(1)
    return ""
