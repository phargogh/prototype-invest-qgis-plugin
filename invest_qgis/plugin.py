"""Plugin entry point.

Mirrors the structure of the bundled GRASS provider plugin: ``initGui`` simply
delegates to ``initProcessing`` so the provider is registered whether QGIS
starts the plugin as a GUI plugin or, because ``hasProcessingProvider=yes``,
as a Processing-only plugin.
"""

from qgis.core import QgsApplication

from .provider import InvestProvider


class InvestPlugin:
    """Registers the InVEST Processing provider."""

    def __init__(self, iface=None):
        self.iface = iface
        self.provider = None
        self._refresh_action = None

    def initProcessing(self):
        if self.provider is None:
            self.provider = InvestProvider()
            QgsApplication.processingRegistry().addProvider(self.provider)

    def initGui(self):
        self.initProcessing()
        if self.iface is None:
            return

        from qgis.PyQt.QtWidgets import QAction

        self._refresh_action = QAction("Refresh InVEST models", self.iface.mainWindow())
        self._refresh_action.setToolTip(
            "Re-read the model list from the configured InVEST application.")
        self._refresh_action.triggered.connect(self._refresh)
        self.iface.addPluginToMenu("InVEST", self._refresh_action)

    def _refresh(self):
        started = self.provider.start_harvest(force=True)
        message = ("Reading InVEST models in the background…" if started
                   else "Could not start: check the InVEST application path in "
                        "Processing options.")
        self.iface.messageBar().pushInfo("InVEST", message)

    def unload(self):
        if self._refresh_action is not None and self.iface is not None:
            self.iface.removePluginMenu("InVEST", self._refresh_action)
            self._refresh_action = None
        if self.provider is not None:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None
