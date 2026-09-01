"""Plugin settings, stored alongside the other Processing provider settings."""

import os

from qgis.core import QgsApplication

#: Path to the InVEST Workbench application the user installed.
APP_PATH = "INVEST_APP_PATH"

#: Check inputs with InVEST's own validation before running.  Answered by a
#: warm InVEST server in milliseconds, so this is on by default.
VALIDATE_BEFORE_RUN = "INVEST_VALIDATE_BEFORE_RUN"

#: Also load outputs from the model's intermediate directory.
LOAD_INTERMEDIATE_DEFAULT = "INVEST_LOAD_INTERMEDIATE_DEFAULT"


def cache_dir():
    """Return the directory holding the harvested spec cache."""
    return os.path.join(QgsApplication.qgisSettingsDirPath(), "invest_qgis")


def _config():
    from processing.core.ProcessingConfig import ProcessingConfig

    return ProcessingConfig


def app_path():
    """Return the configured InVEST application path, or an empty string."""
    value = _config().getSetting(APP_PATH)
    return (value or "").strip()


def validate_before_run():
    value = _config().getSetting(VALIDATE_BEFORE_RUN)
    # An unregistered setting reads back as None; default to validating.
    return True if value is None else bool(value)


def load_intermediate_default():
    return bool(_config().getSetting(LOAD_INTERMEDIATE_DEFAULT))


def register(provider_name):
    """Register this provider's settings with the Processing framework."""
    from processing.core.ProcessingConfig import ProcessingConfig, Setting

    ProcessingConfig.addSetting(Setting(
        provider_name, APP_PATH,
        "InVEST application (e.g. InVEST 3.20.0 Workbench.app)",
        "", valuetype=Setting.FOLDER))
    ProcessingConfig.addSetting(Setting(
        provider_name, VALIDATE_BEFORE_RUN,
        "Check inputs with InVEST before running",
        True))
    ProcessingConfig.addSetting(Setting(
        provider_name, LOAD_INTERMEDIATE_DEFAULT,
        "Load intermediate outputs onto the map by default",
        False))

    # addSetting only registers a setting; without this the stored value is
    # never loaded and every setting reads back as its default.  Processing
    # calls readSettings() during its own startup, which happens before a
    # provider gets the chance to register anything.
    ProcessingConfig.readSettings()
