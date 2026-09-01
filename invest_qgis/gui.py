"""An algorithm dialog with a button for loading an InVEST datastack.

Subclassing Processing's own :class:`AlgorithmDialog` keeps the entire standard
parameter panel and only adds one button, so the dialog behaves exactly like
every other algorithm dialog in QGIS.
"""

from qgis.core import (Qgis, QgsApplication, QgsMessageLog, QgsSettings,
                       QgsTask)
from qgis.PyQt.QtWidgets import QDialogButtonBox, QFileDialog, QPushButton

from processing.gui.AlgorithmDialog import AlgorithmDialog
from processing.tools import dataobjects

from . import datastack, paramspec, server, settings
from .locator import InvestNotFound, find_binary

LOG_GROUP = "InVEST"
_LAST_DIR_KEY = "invest_qgis/lastDatastackDir"

FILE_FILTER = (
    "InVEST datastack (*.json *.invest.json *.invs.json);;All files (*)")


class _ValidateTask(QgsTask):
    """Runs InVEST validation off the GUI thread.

    Only needed the first time, while the InVEST server is still starting;
    once it is warm, validation is fast enough to run inline.
    """

    def __init__(self, algorithm, args, on_done):
        super().__init__("Validating InVEST inputs", QgsTask.Flag.CanCancel)
        self._algorithm = algorithm
        self._args = args
        self._on_done = on_done
        self._result = None

    def run(self):
        self._result = self._algorithm.validate_args(self._args, wait=True)
        return True

    def finished(self, ok):
        self._on_done(self._result if ok else None)


class InvestAlgorithmDialog(AlgorithmDialog):
    """The standard algorithm dialog plus 'Load parameters from datastack'."""

    def __init__(self, algorithm, parent=None):
        super().__init__(algorithm, False, parent)
        self._load_button = QPushButton(self.tr("Load Parameters…"))
        self._load_button.setToolTip(
            self.tr("Populate these inputs from an InVEST datastack "
                    "(.invest.json) file"))
        self._load_button.clicked.connect(self.load_datastack)
        self.buttonBox().addButton(
            self._load_button, QDialogButtonBox.ButtonRole.ActionRole)

        self._validate_button = QPushButton(self.tr("Validate"))
        self._validate_button.setToolTip(
            self.tr("Check these inputs with InVEST's own validation"))
        self._validate_button.clicked.connect(self.validate_now)
        self.buttonBox().addButton(
            self._validate_button, QDialogButtonBox.ButtonRole.ActionRole)

        # Begin warming the InVEST server now, so that validation is instant by
        # the time the user has finished filling in the form.
        self._warm_server()

    def _warm_server(self):
        try:
            binary_path = find_binary(settings.app_path())
        except InvestNotFound:
            return
        server.get(binary_path).start_in_background()

    # -- helpers ------------------------------------------------------------

    def _wrappers(self):
        """Return the parameter widget wrappers, keyed by parameter name."""
        panel = self.mainWidget()
        return getattr(panel, "wrappers", None) or {}

    def _plans(self):
        return getattr(self.algorithm(), "parameter_plans", lambda: [])()

    def _notify(self, text, level=Qgis.MessageLevel.Info, detail=None):
        self.messageBar().pushMessage("InVEST", text, level=level, duration=8)
        QgsMessageLog.logMessage(detail or text, LOG_GROUP, level)

    # -- the button ---------------------------------------------------------

    def load_datastack(self):
        settings = QgsSettings()
        start_dir = settings.value(_LAST_DIR_KEY, "")
        path, _ = QFileDialog.getOpenFileName(
            self, self.tr("Load InVEST datastack"), start_dir, FILE_FILTER)
        if not path:
            return
        settings.setValue(_LAST_DIR_KEY, str(path).rsplit("/", 1)[0])
        self.apply_datastack(path)

    def apply_datastack(self, path):
        """Read ``path`` and push its values into the parameter widgets."""
        plans = self._plans()
        try:
            result = datastack.load_for_plans(path, plans)
        except datastack.DatastackError as error:
            self._notify(str(error), Qgis.MessageLevel.Critical)
            return

        wrappers = self._wrappers()
        if not wrappers:
            self._notify(
                self.tr("Could not reach the parameter widgets to populate."),
                Qgis.MessageLevel.Critical)
            return

        context = dataobjects.createContext()
        applied, failed = [], list(result["problems"])
        for name, value in result["values"].items():
            wrapper = wrappers.get(name)
            if wrapper is None:
                continue
            try:
                wrapper.setWidgetValue(value, context)
                applied.append(name)
            except Exception as error:  # noqa: BLE001 - widget errors vary
                failed.append(f"{name}: {error}")

        self._report(path, result, applied, failed)

    def _report(self, path, result, applied, failed):
        """Tell the user exactly what was and was not populated."""
        expected_id = self.algorithm().name()
        found_id = result.get("model_id")
        details = [f"Loaded {path}"]

        if found_id and found_id != expected_id:
            details.append(
                f"The datastack names model '{found_id}' but this is "
                f"'{expected_id}'.")
            self._notify(
                self.tr("This datastack is for '{0}', not '{1}'. Only matching "
                        "inputs were filled in.").format(found_id, expected_id),
                Qgis.MessageLevel.Warning)

        if result.get("skipped"):
            details.append(
                "Not restored by design (choose it yourself): "
                + ", ".join(result["skipped"]))
        if result["unknown"]:
            details.append("Not applicable to this model: "
                           + ", ".join(result["unknown"]))
        if result["empty"]:
            details.append("Left blank in the datastack: "
                           + ", ".join(result["empty"]))
        if failed:
            details.append("Could not be set: " + "; ".join(failed))

        summary = self.tr("Filled in {0} of {1} inputs.").format(
            len(applied), len(self._plans()))
        if result.get("skipped"):
            summary += " " + self.tr("The workspace folder was not changed.")
        if result["unknown"] or failed:
            # Name the first few so the user knows what to check by hand,
            # with the full list in the log.
            leftover = result["unknown"] + [item.split(":")[0] for item in failed]
            summary += " " + self.tr("Not set: {0}.").format(
                ", ".join(leftover[:4]) + ("…" if len(leftover) > 4 else ""))
            level = Qgis.MessageLevel.Warning
        else:
            level = Qgis.MessageLevel.Success

        self._notify(summary, level, detail="\n".join(details))

    # -- validation ---------------------------------------------------------

    def _current_args(self):
        """Return the InVEST args for what is currently in the dialog.

        Returns ``None`` and reports the reason if the inputs cannot be read.
        """
        panel = self.mainWidget()
        if panel is None:
            return None
        try:
            parameters = panel.createProcessingParameters()
        except Exception as error:  # noqa: BLE001 - panel raises many types
            self._notify(self.tr("Some inputs are incomplete: {0}").format(error),
                         Qgis.MessageLevel.Warning)
            return None
        try:
            return self.algorithm().invest_args(
                parameters, dataobjects.createContext())
        except Exception as error:  # noqa: BLE001 - layer resolution can fail
            self._notify(str(error), Qgis.MessageLevel.Critical)
            return None

    def validate_now(self):
        """Validate the current inputs, warming the server first if needed."""
        args = self._current_args()
        if args is None:
            return

        binary_ready = False
        try:
            binary_ready = server.get(
                find_binary(settings.app_path())).is_ready()
        except InvestNotFound as error:
            self._notify(str(error), Qgis.MessageLevel.Critical)
            return

        if binary_ready:
            self._show_validation(self.algorithm().validate_args(args))
            return

        # Cold start: do it in the background rather than freezing QGIS.
        self._validate_button.setEnabled(False)
        self._notify(
            self.tr("Starting InVEST to validate; this takes about a minute "
                    "the first time…"))

        def done(result):
            self._validate_button.setEnabled(True)
            self._show_validation(result)

        task = _ValidateTask(self.algorithm(), args, done)
        QgsApplication.taskManager().addTask(task)
        self._validate_task = task  # keep a reference alive

    def _show_validation(self, warnings):
        if warnings is None:
            self._notify(
                self.tr("Could not reach InVEST to validate these inputs."),
                Qgis.MessageLevel.Warning)
            return
        if not warnings:
            self._notify(self.tr("InVEST validated these inputs successfully."),
                         Qgis.MessageLevel.Success)
            return

        text = paramspec.format_validation_warnings(
            warnings, self.algorithm().parameter_plans())
        self._notify(
            self.tr("InVEST found {0} problem(s) with these inputs.").format(
                len(warnings)),
            Qgis.MessageLevel.Warning, detail=text)
        # Also write it into the dialog's own Log tab, where there is room.
        self.setInfo(text.replace("\n", "<br>"), isWarning=True, escapeHtml=False)
