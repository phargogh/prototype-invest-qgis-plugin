"""The InVEST Processing provider."""

import os

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsMessageLog,
    QgsProcessingProvider,
    QgsTask,
)
from qgis.PyQt.QtCore import pyqtSignal

from . import harvest, normalize, server, settings, speccache
from .algorithm import InvestAlgorithm
from .locator import InvestNotFound, find_binary, quick_version

LOG_GROUP = "InVEST"
PROVIDER_NAME = "InVEST"


def _log(message, level=Qgis.MessageLevel.Info):
    QgsMessageLog.logMessage(message, LOG_GROUP, level)


def _needs_synchronous_harvest():
    """True where a background QgsTask would never get the chance to run.

    Under ``qgis_process`` or a plain PyQGIS script there is no event loop
    servicing the task manager, so the provider would stay empty forever.
    """
    return QgsApplication.platform() != "desktop"


class _HarvestTask(QgsTask):
    """Fetches every model spec in the background.

    Harvesting needs about a minute of InVEST server startup, so it must never
    run on the GUI thread.
    """

    #: Emitted with a human-readable description of the current step.
    stepChanged = pyqtSignal(str)

    def __init__(self, provider, binary_path, cache_dir):
        super().__init__("Reading InVEST model specifications",
                         QgsTask.Flag.CanCancel)
        self._provider = provider
        self._binary_path = binary_path
        self._cache_dir = cache_dir
        #: Readable by whoever is showing progress, once the task has finished.
        self.error_message = ""
        self.model_count = 0

    def _report(self, message, fraction=None):
        if fraction is not None:
            self.setProgress(fraction * 100)
        self.stepChanged.emit(message)

    def run(self):
        try:
            specs = harvest.harvest_specs(
                self._binary_path, progress=self._report,
                is_canceled=self.isCanceled)
        except harvest.HarvestError as error:
            self.error_message = str(error)
            return False

        if self.isCanceled():
            return False

        speccache.save(self._cache_dir, self._binary_path,
                       quick_version(self._binary_path), specs)
        self.model_count = len(specs)
        return True

    def finished(self, result):
        # Runs on the main thread, so it is safe to touch the registry here.
        if result:
            _log(f"Loaded {self.model_count} InVEST models.")
            self._provider.reload_from_cache()
        elif self.error_message:
            _log(f"Could not read InVEST models: {self.error_message}",
                 Qgis.MessageLevel.Critical)
            self._provider.harvest_failed(self.error_message)
        self._provider.harvest_finished()


class InvestProvider(QgsProcessingProvider):
    """Publishes one Processing algorithm per InVEST model."""

    #: Emitted with the task whenever a harvest begins, including the ones
    #: started automatically, so the plugin can show progress for those too.
    harvest_started = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self._specs = []
        self._warning = ""
        self._version = ""
        self._harvesting = False

    # -- identity -----------------------------------------------------------

    def id(self):
        return "invest"

    def name(self):
        return PROVIDER_NAME

    def longName(self):
        return f"InVEST {self._version}" if self._version else "InVEST"

    def icon(self):
        icon_path = os.path.join(os.path.dirname(__file__), "icons", "invest.svg")
        if os.path.exists(icon_path):
            from qgis.PyQt.QtGui import QIcon

            return QIcon(icon_path)
        return QgsApplication.getThemeIcon("/processingAlgorithm.svg")

    def supportsNonFileBasedOutput(self):
        # InVEST writes plain files, so Processing must always hand us real
        # filesystem paths rather than memory layers.
        return False

    def warningMessage(self):
        return self._warning

    # -- lifecycle ----------------------------------------------------------

    def load(self):
        settings.register(self.name())
        self._load_cached_specs()
        self.refreshAlgorithms()
        return True

    def unload(self):
        # Leaving the InVEST server running would outlive the plugin.
        server.shutdown()

        from processing.core.ProcessingConfig import ProcessingConfig

        for key in (settings.APP_PATH, settings.VALIDATE_BEFORE_RUN,
                    settings.LOAD_INTERMEDIATE_DEFAULT):
            ProcessingConfig.removeSetting(key)

    def loadAlgorithms(self):
        # Fresh instances every time: addAlgorithm takes ownership of them.
        for spec in self._specs:
            self.addAlgorithm(InvestAlgorithm(spec))

    # -- spec management ----------------------------------------------------

    def _binary_path(self):
        """Return the configured binary path, or None (recording why)."""
        try:
            return find_binary(settings.app_path())
        except InvestNotFound as error:
            self._warning = str(error)
            return None

    def _load_cached_specs(self, auto_harvest=True):
        """Populate specs from the cache.

        ``auto_harvest`` is disabled when called from a just-finished harvest,
        so that a cache which still fails to validate cannot start an endless
        chain of harvests.
        """
        self._specs = []
        binary_path = self._binary_path()
        if binary_path is None:
            return

        self._version = quick_version(binary_path)
        payload = speccache.load(settings.cache_dir(), binary_path)
        if payload is None:
            if not auto_harvest:
                self._warning = (
                    "The InVEST model list has not been read yet. Use "
                    "Plugins > InVEST > Refresh InVEST Models. If it was "
                    "already tried, see the InVEST log messages panel.")
                return
            if _needs_synchronous_harvest():
                # Nothing will run a background task here, so this either
                # worked or it did not; queuing one would only strand the
                # provider in a permanent "harvesting" state.
                if not self._harvest_now(binary_path):
                    self._warning = (
                        "Could not read the InVEST models. See the InVEST "
                        "log messages panel for details.")
                    return
                payload = speccache.load(settings.cache_dir(), binary_path)
            else:
                self._warning = (
                    "Reading the InVEST model list. This takes about a minute "
                    "the first time; the models will appear when it finishes.")
                self.start_harvest(binary_path)
                return
        if payload is None:
            self._warning = "The InVEST model list could not be read."
            return

        self._specs = self._normalise_all(payload["specs"])
        self._version = payload.get("invest_version") or self._version
        self._warning = "" if self._specs else "No usable InVEST models found."

    def _normalise_all(self, raw_specs):
        """Normalise cached specs, skipping any the plugin cannot interpret."""
        normalised = []
        for model_id, raw_spec in sorted(raw_specs.items()):
            try:
                normalised.append(normalize.normalise(raw_spec))
            except normalize.UnsupportedSpec as error:
                _log(f"Skipping model {model_id}: {error}",
                     Qgis.MessageLevel.Warning)
        return normalised

    def _harvest_now(self, binary_path):
        """Harvest right now, returning whether it succeeded."""
        _log("Reading InVEST model specifications (this takes about a minute)…")
        try:
            # progress is called with (message, fraction); the log takes a
            # level as its second argument, so it cannot be passed directly.
            specs = harvest.harvest_specs(
                binary_path,
                progress=lambda message, fraction=None: _log(message))
        except harvest.HarvestError as error:
            _log(f"Could not read InVEST models: {error}",
                 Qgis.MessageLevel.Critical)
            return False
        speccache.save(settings.cache_dir(), binary_path,
                       quick_version(binary_path), specs)
        _log(f"Loaded {len(specs)} InVEST models.")
        return True

    def start_harvest(self, binary_path=None, force=False):
        """Kick off a background spec harvest.

        Returns:
            The running task, so a caller with a GUI can show its progress, or
            None if no harvest could be started.
        """
        if self._harvesting:
            return None
        if binary_path is None:
            binary_path = self._binary_path()
        if binary_path is None:
            return None
        if force:
            speccache.clear(settings.cache_dir())

        self._harvesting = True
        task = _HarvestTask(self, binary_path, settings.cache_dir())
        self.harvest_started.emit(task)
        QgsApplication.taskManager().addTask(task)
        return task

    def harvest_finished(self):
        self._harvesting = False

    def harvest_failed(self, message):
        """Leave the reason in the toolbox, not just in a transient message.

        Otherwise the provider keeps advertising that the model list is being
        read long after the attempt gave up.
        """
        self._warning = f"Could not read the InVEST models: {message}"

    def reload_from_cache(self):
        """Re-read the cache and republish the algorithms."""
        self._load_cached_specs(auto_harvest=False)
        self.refreshAlgorithms()
