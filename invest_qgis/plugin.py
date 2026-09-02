"""Plugin entry point.

Mirrors the structure of the bundled GRASS provider plugin: ``initGui`` simply
delegates to ``initProcessing`` so the provider is registered whether QGIS
starts the plugin as a GUI plugin or, because ``hasProcessingProvider=yes``,
as a Processing-only plugin.
"""

from qgis.core import Qgis, QgsApplication

from .provider import InvestProvider

MENU = "InVEST"


class InvestPlugin:
    """Registers the InVEST Processing provider."""

    def __init__(self, iface=None):
        self.iface = iface
        self.provider = None
        self._actions = []

    def initProcessing(self):
        if self.provider is None:
            self.provider = InvestProvider()
            QgsApplication.processingRegistry().addProvider(self.provider)

    def initGui(self):
        self.initProcessing()
        if self.iface is None:
            return

        from qgis.PyQt.QtWidgets import QAction

        configure = QAction("Configure InVEST…", self.iface.mainWindow())
        configure.setToolTip("Choose which InVEST installation to run models with.")
        configure.triggered.connect(self.configure)

        refresh = QAction("Refresh InVEST Models", self.iface.mainWindow())
        refresh.setToolTip(
            "Re-read the model list from the configured InVEST installation.")
        refresh.triggered.connect(self._refresh)

        for action in (configure, refresh):
            self.iface.addPluginToMenu(MENU, action)
            self._actions.append(action)

        # Harvests can also start on their own -- when the provider loads with
        # a stale cache, for instance -- and those need the same progress
        # reporting as the ones the user asks for.
        self.provider.harvest_started.connect(self._show_harvest_progress)
        self._offer_setup()

    # -- actions ------------------------------------------------------------

    def configure(self):
        """Open the configuration dialog and apply the result."""
        from .configdialog import ConfigDialog

        dialog = ConfigDialog(self.iface.mainWindow())
        if not dialog.exec():
            return
        dialog.save()
        # Picking a different installation invalidates the cached model list;
        # reload_from_cache notices and harvests again if needed.
        self.provider.reload_from_cache()
        if not self.provider.start_harvest() and self.provider.algorithms():
            self.iface.messageBar().pushSuccess(
                MENU, f"Using InVEST at {dialog.selected_path()}.")

    def _refresh(self):
        if self.provider.start_harvest(force=True) is None:
            self.iface.messageBar().pushWarning(
                MENU, "Could not read the InVEST models. Check the "
                      "installation in Plugins > InVEST > Configure InVEST.")

    # -- progress reporting -------------------------------------------------

    def _show_harvest_progress(self, task):
        """Show a progress bar in the message bar while models are read.

        Reading the model list takes about a minute, so without this the
        toolbox simply stays empty with no sign that anything is happening.
        """
        from qgis.gui import QgsMessageBarItem
        from qgis.PyQt.QtWidgets import QProgressBar

        bar = self.iface.messageBar()
        progress = QProgressBar()
        progress.setRange(0, 100)
        progress.setTextVisible(False)
        progress.setMaximumWidth(220)

        # duration 0 keeps it up until the harvest finishes.
        item = QgsMessageBarItem(
            MENU, "Reading the InVEST model list…", progress,
            Qgis.MessageLevel.Info, 0, self.iface.mainWindow())
        bar.pushItem(item)

        state = {"item": item, "step": ""}

        def remove():
            if state["item"] is not None:
                bar.popWidget(state["item"])
                state["item"] = None

        def on_progress(value):
            progress.setValue(int(value))

        def on_step(message):
            state["step"] = message
            item.setText(f"InVEST: {message}")

        def on_done():
            remove()
            bar.pushSuccess(
                MENU, f"{task.model_count} InVEST models are ready in the "
                      f"Processing Toolbox.")

        def on_failed():
            remove()
            detail = task.error_message or state["step"] or "No details available."
            bar.pushMessage(
                MENU, "Could not read the InVEST models.", detail,
                Qgis.MessageLevel.Critical, 0)

        task.progressChanged.connect(on_progress)
        task.stepChanged.connect(on_step)
        task.taskCompleted.connect(on_done)
        task.taskTerminated.connect(on_failed)

    def _offer_setup(self):
        """Nudge the user once if the plugin has nothing to run models with."""
        from .locator import InvestNotFound, detect_installations, find_binary
        from . import settings

        try:
            find_binary(settings.app_path())
            return  # already configured
        except InvestNotFound:
            pass

        found = len(detect_installations())
        if found:
            message = (f"Found {found} InVEST installation(s). Use "
                       f"Plugins > InVEST > Configure InVEST to choose one.")
        else:
            message = ("No InVEST installation found. Install the InVEST "
                       "Workbench, then use Plugins > InVEST > Configure InVEST.")
        self.iface.messageBar().pushInfo(MENU, message)

    # -- teardown -----------------------------------------------------------

    def unload(self):
        if self.iface is not None:
            for action in self._actions:
                self.iface.removePluginMenu(MENU, action)
        self._actions = []
        if self.provider is not None:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None
