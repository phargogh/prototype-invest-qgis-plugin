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

#: Where InVEST installs itself, by platform.  Only the macOS layout is
#: verified against a real installation; the others are best-effort, which is
#: why the configuration dialog always offers a Browse button as well.
_SEARCH_GLOBS = {
    "Darwin": [
        "/Applications/InVEST*.app",
        "~/Applications/InVEST*.app",
    ],
    "Windows": [
        "C:/Program Files/InVEST*",
        "C:/Program Files (x86)/InVEST*",
        "~/AppData/Local/Programs/InVEST*",
    ],
    "Linux": [
        "/opt/InVEST*",
        "/usr/local/InVEST*",
        "~/InVEST*",
        "~/.local/opt/InVEST*",
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
            "No InVEST installation configured. Use "
            "Plugins > InVEST > Configure InVEST.")

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


def _version_key(version):
    """Sort key placing the newest release first.

    Version strings are mostly ``3.20.0`` but development builds look like
    ``3.13.0a3.dev3543+geb8201a89``.  Only the leading numbers are compared,
    with a pre-release sorting below the matching release.
    """
    # Only the leading dotted numbers count.  Splitting on every non-digit
    # would fold a pre-release suffix into the version itself, making
    # "3.20.0a1" -> (3, 20, 0, 1) sort above "3.20.0" -> (3, 20, 0).
    match = re.match(r"(\d+(?:\.\d+)*)", version or "")
    numbers = tuple(int(part) for part in match.group(1).split(".")) if match else ()
    is_release = re.search(r"(a\d|b\d|rc|dev)", version or "") is None
    return (numbers, is_release)


def detect_installations(search_globs=None):
    """Find InVEST installations without running anything.

    Versions are read from the ``natcap_invest-*.dist-info`` directory beside
    the frozen binary, so scanning is a filesystem operation rather than a
    minute of process startup per candidate.

    Args:
        search_globs: patterns to search *instead of* the platform defaults.
            Used by tests so that a real installation on the machine running
            them cannot affect the result.

    Returns:
        A list of ``(app_path, binary_path, version)`` tuples, newest first.
        Candidates without a usable executable are skipped.
    """
    patterns = (list(search_globs) if search_globs is not None
                else list(_SEARCH_GLOBS.get(platform.system(), [])))

    found = {}
    for pattern in patterns:
        for candidate in glob.glob(os.path.expanduser(pattern)):
            candidate = os.path.normpath(candidate)
            if candidate in found:
                continue
            try:
                binary_path = find_binary(candidate)
            except InvestNotFound:
                continue
            found[candidate] = (candidate, binary_path, quick_version(binary_path))

    return sorted(found.values(), key=lambda item: _version_key(item[2]),
                  reverse=True)
