"""Plugin settings, stored alongside the other Processing provider settings."""

import os

from qgis.core import QgsApplication

from .locator import InvestNotFound, find_binary

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


def _validate_app_path(value):
    """Reject a path that is not a usable InVEST installation.

    Replaces the default validator for Setting.FILE, which requires write
    access to the chosen path.  An InVEST installation under /Applications is
    read-only for the user, so the stock check rejects every correct answer.

    Raises:
        ValueError: as the Processing options dialog expects.
    """
    if not value:
        return  # clearing the setting is allowed
    try:
        find_binary(value)
    except InvestNotFound as error:
        raise ValueError(str(error)) from error


def register(provider_name):
    """Register this provider's settings with the Processing framework."""
    from processing.core.ProcessingConfig import ProcessingConfig, Setting

    # Setting.FILE, not FOLDER: FOLDER browses with
    # QFileDialog.getExistingDirectory(ShowDirsOnly), and macOS treats an .app
    # bundle as a package, so the Workbench cannot be selected that way at all.
    # FILE browses with getOpenFileName, which can select a bundle, and
    # locator.find_binary() accepts either a bundle or a bare executable.
    ProcessingConfig.addSetting(Setting(
        provider_name, APP_PATH,
        "InVEST application (use Plugins > InVEST > Configure InVEST)",
        "", valuetype=Setting.FILE, validator=_validate_app_path,
        placeholder="e.g. /Applications/InVEST 3.20.0 Workbench.app"))
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
