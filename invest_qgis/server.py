"""A long-lived ``invest serve`` process, shared by the whole plugin.

The frozen InVEST binary needs about a minute to start, which makes one-shot
CLI calls useless for anything interactive.  The same binary can instead run a
small local HTTP server that answers validation and specification requests in
milliseconds, so the startup cost is paid once per QGIS session.

The server is started on demand, kept warm, restarted if it dies, and stopped
when the plugin unloads.  Nothing here imports QGIS, so it can be exercised in
isolation.
"""

import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

#: How long to wait for the Flask app to answer after launching it.
START_TIMEOUT = 300
_POLL_INTERVAL = 1.0

#: Requests are answered in milliseconds once warm; this only guards against a
#: wedged server.
REQUEST_TIMEOUT = 60


class ServerError(Exception):
    """Raised when the InVEST server cannot be started or queried."""


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class InvestServer:
    """Owns one ``invest serve`` subprocess."""

    def __init__(self, binary_path):
        self.binary_path = binary_path
        self._process = None
        self._port = None
        self._log_path = None
        self._lock = threading.RLock()
        self._starting = threading.Event()
        self._last_error = ""

    # -- state --------------------------------------------------------------

    @property
    def last_error(self):
        return self._last_error

    def is_alive(self):
        return self._process is not None and self._process.poll() is None

    def is_ready(self):
        """True when a request can be answered right now, without waiting."""
        if not self.is_alive() or self._port is None:
            return False
        try:
            with urllib.request.urlopen(f"{self._base}/ready", timeout=3):
                return True
        except (urllib.error.URLError, OSError, ValueError):
            return False

    @property
    def _base(self):
        return f"http://127.0.0.1:{self._port}/api"

    # -- lifecycle ----------------------------------------------------------

    def start_in_background(self):
        """Begin warming the server without blocking the caller.

        Called when a dialog opens so the server is usually ready by the time
        the user has finished filling in a form.
        """
        if self.is_alive() or self._starting.is_set():
            return
        self._starting.set()

        def warm():
            try:
                self.ensure_running()
            except ServerError:
                pass
            finally:
                self._starting.clear()

        threading.Thread(target=warm, daemon=True, name="invest-server").start()

    def ensure_running(self, timeout=START_TIMEOUT, on_wait=None):
        """Start the server if needed and block until it answers.

        Args:
            on_wait: optional callable receiving the seconds waited so far, so
                a caller can show progress during the long startup.

        Raises:
            ServerError: if the server cannot be started.
        """
        with self._lock:
            if self.is_alive() and self.is_ready():
                return
            if self._process is not None and not self.is_alive():
                # It died; clear it out so a fresh one is launched.
                self._reset()
            if self._process is None:
                self._launch()
            self._wait_until_ready(timeout, on_wait)

    def _launch(self):
        self._port = _free_port()
        handle, self._log_path = tempfile.mkstemp(
            prefix="invest-server-", suffix=".log")
        try:
            self._process = subprocess.Popen(
                [self.binary_path, "serve", "--port", str(self._port)],
                stdout=handle, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                # Its own process group, so stopping it takes any helper
                # processes with it.
                **({"start_new_session": True} if os.name != "nt" else {}))
        except OSError as error:
            os.close(handle)
            self._reset()
            self._last_error = f"Could not start {self.binary_path}: {error}"
            raise ServerError(self._last_error) from error
        finally:
            try:
                os.close(handle)
            except OSError:
                pass

    def _wait_until_ready(self, timeout, on_wait=None):
        started = time.time()
        deadline = started + timeout
        while time.time() < deadline:
            if on_wait is not None:
                on_wait(time.time() - started)
            if self._process.poll() is not None:
                detail = self._read_log()
                self._reset()
                self._last_error = (
                    f"The InVEST server stopped while starting. {detail}")
                raise ServerError(self._last_error)
            try:
                with urllib.request.urlopen(f"{self._base}/ready", timeout=5):
                    self._last_error = ""
                    return
            except (urllib.error.URLError, OSError, ValueError):
                time.sleep(_POLL_INTERVAL)
        self.stop()
        self._last_error = (
            f"The InVEST server did not start within {timeout} seconds.")
        raise ServerError(self._last_error)

    def _read_log(self):
        if not self._log_path:
            return ""
        try:
            with open(self._log_path, encoding="utf-8", errors="replace") as handle:
                return handle.read().strip()[-400:]
        except OSError:
            return ""

    def _reset(self):
        self._process = None
        self._port = None
        if self._log_path:
            try:
                os.remove(self._log_path)
            except OSError:
                pass
            self._log_path = None

    def stop(self):
        """Terminate the server if it is running."""
        with self._lock:
            process = self._process
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        pass
            self._reset()

    # -- requests -----------------------------------------------------------

    def _get(self, endpoint):
        self.ensure_running()
        try:
            with urllib.request.urlopen(
                    f"{self._base}/{endpoint}", timeout=REQUEST_TIMEOUT) as response:
                return json.load(response)
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise ServerError(f"InVEST server request failed: {error}") from error

    def _post(self, endpoint, payload):
        self.ensure_running()
        request = urllib.request.Request(
            f"{self._base}/{endpoint}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                return json.load(response)
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise ServerError(f"InVEST server request failed: {error}") from error

    def models(self):
        return self._get("models")

    def getspec(self, model_id):
        return self._post("getspec", model_id)

    def validate(self, model_id, args, limit_to=None):
        """Return InVEST's validation warnings as ``[[keys], message]`` pairs."""
        payload = {"model_id": model_id, "args": json.dumps(args)}
        if limit_to is not None:
            payload["limit_to"] = limit_to
        return self._post("validate", payload)

    def args_enabled(self, model_id, args):
        """Return ``{arg_id: bool}`` for which inputs currently apply."""
        return self._post(
            "args_enabled", {"model_id": model_id, "args": json.dumps(args)})


_INSTANCE = None
_INSTANCE_LOCK = threading.Lock()


def get(binary_path):
    """Return the shared server for ``binary_path``, creating it if needed."""
    global _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is not None and _INSTANCE.binary_path != binary_path:
            # The user pointed at a different InVEST installation.
            _INSTANCE.stop()
            _INSTANCE = None
        if _INSTANCE is None:
            _INSTANCE = InvestServer(binary_path)
        return _INSTANCE


def shutdown():
    """Stop the shared server, if any."""
    global _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is not None:
            _INSTANCE.stop()
            _INSTANCE = None
