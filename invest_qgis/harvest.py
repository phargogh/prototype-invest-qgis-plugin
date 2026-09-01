"""Harvest every InVEST model spec in one pass.

The frozen ``invest`` binary takes roughly a minute to start, so asking it for
26 model specs one CLI call at a time would take close to half an hour.
``invest serve`` pays that startup cost exactly once and then answers spec
requests over HTTP in milliseconds, which is why harvesting is built around it.
"""

import json
import socket
import subprocess
import time
import urllib.error
import urllib.request

#: How long to wait for the Flask app to come up.  Measured startup on macOS is
#: about 60 seconds; this leaves generous headroom for slower machines.
READY_TIMEOUT = 300
READY_POLL_INTERVAL = 2.0
REQUEST_TIMEOUT = 120


class HarvestError(Exception):
    """Raised when model specs could not be retrieved."""


def _free_port():
    """Return a port number that is free right now."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _get_json(url):
    with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT) as response:
        return json.load(response)


def _post_json(url, payload):
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return json.load(response)


def _wait_until_ready(base_url, process, progress):
    """Block until the InVEST server responds, or raise."""
    deadline = time.time() + READY_TIMEOUT
    while time.time() < deadline:
        if process.poll() is not None:
            stderr = (process.stderr.read() or b"").decode("utf-8", "replace")
            raise HarvestError(
                f"InVEST server exited early with code {process.returncode}. "
                f"{stderr.strip()[-500:]}")
        try:
            with urllib.request.urlopen(f"{base_url}/ready", timeout=5):
                return
        except (urllib.error.URLError, OSError, ValueError):
            remaining = int(deadline - time.time())
            progress(f"Waiting for InVEST to start ({remaining}s left)…")
            time.sleep(READY_POLL_INTERVAL)
    raise HarvestError(
        f"InVEST server did not become ready within {READY_TIMEOUT}s.")


def harvest_specs(binary_path, progress=None, is_canceled=None):
    """Return ``{model_id: raw_spec}`` for every model the binary provides.

    Args:
        binary_path: path to the frozen ``invest`` executable.
        progress: optional callable receiving human-readable status strings.
        is_canceled: optional callable returning True to abort early.

    Raises:
        HarvestError: if the server cannot be started or queried.
    """
    progress = progress or (lambda message: None)
    is_canceled = is_canceled or (lambda: False)

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}/api"
    progress("Starting the InVEST server (this takes about a minute)…")

    try:
        process = subprocess.Popen(
            [binary_path, "serve", "--port", str(port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL)
    except OSError as error:
        raise HarvestError(f"Could not start {binary_path}: {error}") from error

    try:
        _wait_until_ready(base_url, process, progress)
        if is_canceled():
            raise HarvestError("Cancelled.")

        progress("Fetching the model list…")
        try:
            models = _get_json(f"{base_url}/models")
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise HarvestError(f"Could not list InVEST models: {error}") from error

        model_ids = sorted(models)
        specs = {}
        for index, model_id in enumerate(model_ids, start=1):
            if is_canceled():
                raise HarvestError("Cancelled.")
            progress(f"Reading spec {index}/{len(model_ids)}: {model_id}")
            try:
                specs[model_id] = _post_json(f"{base_url}/getspec", model_id)
            except (urllib.error.URLError, OSError, ValueError) as error:
                # One unreadable model should not lose the other 25.
                progress(f"Skipping {model_id}: {error}")

        if not specs:
            raise HarvestError("The InVEST server returned no model specs.")
        return specs
    finally:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
