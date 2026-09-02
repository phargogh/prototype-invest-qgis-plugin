"""Plugin entry point.

Mirrors the structure of the bundled GRASS provider plugin: ``initGui`` simply
delegates to ``initProcessing`` so the provider is registered whether QGIS
starts the plugin as a GUI plugin or, because ``hasProcessingProvider=yes``,
as a Processing-only plugin.
"""

from qgis.core import QgsApplication

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
        self.provider.start_harvest()
        self.iface.messageBar().pushInfo(
            MENU, "InVEST location saved. Models will appear in the "
                  "Processing Toolbox once the model list has been read.")

    def _refresh(self):
        started = self.provider.start_harvest(force=True)
        if started:
            self.iface.messageBar().pushInfo(
                MENU, "Reading InVEST models in the background…")
        else:
            self.iface.messageBar().pushWarning(
                MENU, "No InVEST installation configured. Use "
                      "Plugins > InVEST > Configure InVEST.")

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
