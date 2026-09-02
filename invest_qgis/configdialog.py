"""A dialog for choosing which InVEST installation the plugin should use.

The path is stored in the same Processing setting the provider reads, so this
dialog is purely a friendlier way to set it; ``qgis_process`` and any existing
configuration keep working untouched.
"""

from qgis.core import Qgis, QgsApplication, QgsTask
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from . import server, settings, speccache
from .locator import InvestNotFound, detect_installations, find_binary, quick_version

#: A file dialog, not a directory dialog: macOS treats an .app bundle as a
#: package, so only an open-file dialog can select the Workbench.
_FILE_FILTER = "InVEST application or executable (*)"


class _TestTask(QgsTask):
    """Starts the InVEST server so the user can confirm the path really works."""

    def __init__(self, binary_path, on_done):
        super().__init__("Testing the InVEST installation", QgsTask.Flag.CanCancel)
        self._binary_path = binary_path
        self._on_done = on_done
        self._error = ""

    def run(self):
        try:
            server.get(self._binary_path).ensure_running()
        except server.ServerError as error:
            self._error = str(error)
            return False
        return True

    def finished(self, ok):
        self._on_done(ok, self._error)


class ConfigDialog(QDialog):
    """Pick an InVEST installation, by detection or by browsing."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Configure InVEST"))
        self.setMinimumWidth(620)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(self.tr(
            "The plugin runs models using a separate InVEST installation. "
            "Choose which one to use:")))

        self._list = QListWidget(self)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.setMaximumHeight(150)
        self._list.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self._list)

        row = QHBoxLayout()
        row.addWidget(QLabel(self.tr("Path:")))
        self._path = QLineEdit(self)
        self._path.setPlaceholderText(
            self.tr("e.g. /Applications/InVEST 3.20.0 Workbench.app"))
        self._path.textChanged.connect(self._refresh_status)
        row.addWidget(self._path, 1)
        browse = QPushButton(self.tr("Browse…"), self)
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        layout.addLayout(row)

        self._status = QLabel(self)
        self._status.setWordWrap(True)
        self._status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self._status)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self)
        self._test_button = self._buttons.addButton(
            self.tr("Test"), QDialogButtonBox.ButtonRole.ActionRole)
        self._test_button.setToolTip(self.tr(
            "Start this InVEST installation to confirm it works. "
            "Takes about a minute."))
        self._test_button.clicked.connect(self._test)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

        self._populate()

    # -- setup --------------------------------------------------------------

    def _populate(self):
        """List detected installations and preselect the configured one."""
        current = settings.app_path()
        installations = detect_installations()

        if not installations:
            item = QListWidgetItem(self.tr(
                "No InVEST installation found automatically — use Browse."))
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self._list.addItem(item)
        else:
            for app_path, _binary, version in installations:
                label = f"{version or '?'}    {app_path}"
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, app_path)
                self._list.addItem(item)
                if app_path == current:
                    self._list.setCurrentItem(item)

        # Setting the text last means the status line reflects the real value
        # even when the configured path is not one of the detected ones.
        self._path.setText(current)
        # textChanged does not fire when the text is already empty.
        self._refresh_status()

    # -- interaction --------------------------------------------------------

    def _selection_changed(self):
        item = self._list.currentItem()
        if item is None:
            return
        app_path = item.data(Qt.ItemDataRole.UserRole)
        if app_path:
            self._path.setText(app_path)

    def _sync_list_to_path(self):
        """Keep the highlighted row honest about what will actually be saved.

        The path field is the single source of truth, so a hand-typed path must
        clear the highlight rather than leave a different install looking
        selected.
        """
        path = self.selected_path()
        self._list.blockSignals(True)
        try:
            for index in range(self._list.count()):
                item = self._list.item(index)
                if item.data(Qt.ItemDataRole.UserRole) == path:
                    self._list.setCurrentItem(item)
                    return
            self._list.setCurrentItem(None)
            self._list.clearSelection()
        finally:
            self._list.blockSignals(False)

    def _browse(self):
        start_dir = self._path.text().strip() or "/Applications"
        chosen, _ = QFileDialog.getOpenFileName(
            self, self.tr("Select the InVEST application"), start_dir,
            _FILE_FILTER)
        if chosen:
            self._path.setText(chosen)

    def _test(self):
        try:
            binary_path = find_binary(self.selected_path())
        except InvestNotFound as error:
            self._status.setText(str(error))
            return
        self._test_button.setEnabled(False)
        self._status.setText(self.tr(
            "Starting InVEST to test it; this takes about a minute…"))

        def done(ok, error):
            self._test_button.setEnabled(True)
            if ok:
                self._status.setText(self.tr("InVEST started successfully."))
            else:
                self._status.setText(
                    self.tr("InVEST could not be started. {0}").format(error))

        self._task = _TestTask(binary_path, done)
        QgsApplication.taskManager().addTask(self._task)

    # -- status -------------------------------------------------------------

    def _set_valid(self, is_valid):
        ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok is not None:
            ok.setEnabled(is_valid)
        self._test_button.setEnabled(is_valid)

    def _refresh_status(self):
        self._sync_list_to_path()
        path = self.selected_path()
        if not path:
            self._status.setText(self.tr("No InVEST installation selected."))
            self._set_valid(False)
            return
        try:
            binary_path = find_binary(path)
        except InvestNotFound as error:
            self._status.setText(str(error))
            self._set_valid(False)
            return
        self._set_valid(True)

        version = quick_version(binary_path) or self.tr("unknown version")
        cached = speccache.load(settings.cache_dir(), binary_path)
        if cached:
            detail = self.tr("{0} models already loaded.").format(
                len(cached.get("specs") or {}))
        else:
            detail = self.tr(
                "The model list will be read the first time it is needed, "
                "which takes about a minute.")
        self._status.setText(f"InVEST {version} — {detail}")

    # -- result -------------------------------------------------------------

    def selected_path(self):
        return self._path.text().strip()

    def save(self):
        """Store the chosen path in the Processing setting."""
        from processing.core.ProcessingConfig import ProcessingConfig

        ProcessingConfig.setSettingValue(settings.APP_PATH, self.selected_path())
