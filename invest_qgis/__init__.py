"""InVEST Processing provider for QGIS."""


def classFactory(iface):  # noqa: N802 (QGIS-mandated name)
    """Entry point called by QGIS to instantiate the plugin."""
    from .plugin import InvestPlugin

    return InvestPlugin(iface)
