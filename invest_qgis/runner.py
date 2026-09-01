"""Execute and validate InVEST models through the frozen ``invest`` CLI."""

import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading

from qgis.core import QgsProcessingException

DATASTACK_FILENAME = "invest_args_from_qgis.json"

#: taskgraph reports "tasks complete: 5 (33.3%)" on its status line, and
#: pygeoprocessing reports "72.5% complete" for individual raster operations.
_TASKGRAPH_PROGRESS_RE = re.compile(r"tasks complete:\s*\d+\s*\(([\d.]+)%\)")

_ERROR_MARKERS = ("Traceback (most recent call last)", "Exception while executing")
_SUCCESS_MARKER = "Execution finished"

#: How often the run loop wakes up to notice a cancellation request.
_POLL_INTERVAL = 0.2

#: Grace period between asking the InVEST process tree to stop and killing it.
_TERMINATE_GRACE = 5

_IS_WINDOWS = sys.platform == "win32"


def write_datastack(workspace, model_id, invest_version, args):
    """Write an InVEST parameter set and return its path."""
    os.makedirs(workspace, exist_ok=True)
    path = os.path.join(workspace, DATASTACK_FILENAME)
    payload = {
        "model_id": model_id,
        "invest_version": invest_version,
        "args": args,
    }
    with open(path, "w", encoding="utf-8") as file_handle:
        json.dump(payload, file_handle, indent=4, sort_keys=True)
    return path


def _popen_isolated(command):
    """Start a process in its own process group.

    The group is what makes a clean cancellation possible: the frozen InVEST
    binary starts helper processes of its own, and killing only the process we
    launched would leave those running against the workspace.
    """
    keywords = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
        "bufsize": 1,
        "universal_newlines": True,
        "errors": "replace",
    }
    if _IS_WINDOWS:
        keywords["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        keywords["start_new_session"] = True
    return subprocess.Popen(command, **keywords)


def _terminate_tree(process):
    """Stop a process and everything it started."""
    if process.poll() is not None:
        return
    try:
        if _IS_WINDOWS:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)],
                           capture_output=True, timeout=30)
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        pass

    try:
        process.wait(timeout=_TERMINATE_GRACE)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        if not _IS_WINDOWS:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        else:
            process.kill()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        process.wait(timeout=_TERMINATE_GRACE)
    except subprocess.TimeoutExpired:
        pass


class _LogRouter:
    """Sends InVEST log lines to the right part of ``feedback``."""

    def __init__(self, feedback):
        self._feedback = feedback
        self.saw_error = False
        self.saw_success = False

    def __call__(self, line):
        line = line.rstrip("\r\n")
        if not line.strip():
            return

        match = _TASKGRAPH_PROGRESS_RE.search(line)
        if match:
            try:
                self._feedback.setProgress(float(match.group(1)))
            except (TypeError, ValueError):
                pass

        if _SUCCESS_MARKER in line:
            self.saw_success = True

        if any(marker in line for marker in _ERROR_MARKERS) or " ERROR " in line:
            self.saw_error = True
            self._feedback.reportError(line)
        elif " WARNING " in line:
            self._feedback.pushWarning(line)
        else:
            self._feedback.pushConsoleInfo(line)


class InvestRunner:
    """Runs InVEST models out of process."""

    def __init__(self, binary_path, invest_version=""):
        self.binary_path = binary_path
        self.invest_version = invest_version

    def _command(self, model_id, datastack_path, workspace):
        return [
            self.binary_path,
            # The CLI maps verbosity to a level of ERROR - 10*count, so two
            # -v flags are needed before InVEST's INFO progress messages
            # reach the console.
            "-vv",
            "--taskgraph-log-level", "INFO",
            "run", model_id,
            "-d", datastack_path,
            "-w", workspace,
        ]

    def run_model(self, model_id, args, workspace, feedback):
        """Run one model, streaming its log into ``feedback``.

        Raises:
            QgsProcessingException: if the model fails or is cancelled.
        """
        datastack_path = write_datastack(
            workspace, model_id, self.invest_version, args)
        command = self._command(model_id, datastack_path, workspace)

        feedback.pushInfo(f"InVEST: {self.binary_path}")
        feedback.pushCommandInfo(" ".join(command))
        # The frozen binary takes about a minute to start before it prints
        # anything, so say so rather than letting the dialog look hung.
        feedback.setProgressText(
            "Starting InVEST (the first output may take a minute)…")

        try:
            process = _popen_isolated(command)
        except OSError as error:
            raise QgsProcessingException(
                f"Could not start InVEST at {self.binary_path}: {error}") from error

        router = _LogRouter(feedback)
        lines = queue.Queue()

        def pump():
            try:
                for line in process.stdout:
                    lines.put(line)
            finally:
                lines.put(None)

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()

        cancelled = False
        while True:
            try:
                line = lines.get(timeout=_POLL_INTERVAL)
            except queue.Empty:
                # Reaching here regularly is what makes cancellation responsive
                # even while InVEST is silent during startup.
                if feedback.isCanceled():
                    cancelled = True
                    break
                continue
            if line is None:
                break
            router(line)

        if cancelled:
            feedback.pushInfo("Cancelling the InVEST process…")
            _terminate_tree(process)
            reader.join(timeout=_TERMINATE_GRACE)
            raise QgsProcessingException("InVEST run cancelled.")

        exit_code = process.wait()
        reader.join(timeout=_TERMINATE_GRACE)

        if feedback.isCanceled():
            _terminate_tree(process)
            raise QgsProcessingException("InVEST run cancelled.")

        if exit_code != 0:
            raise QgsProcessingException(
                f"InVEST exited with code {exit_code}. See the log above for "
                f"details.")

        if router.saw_error:
            raise QgsProcessingException(
                "InVEST reported an error during execution. See the log above.")

        return datastack_path
