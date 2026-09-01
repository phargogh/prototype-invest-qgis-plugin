"""A single Processing algorithm class that adapts to any InVEST model spec."""

import os

from qgis.core import (
    Qgis,
    QgsProcessingAlgorithm,
    QgsProcessingContext,
    QgsProcessingException,
    QgsProcessingOutputFile,
    QgsProcessingOutputRasterLayer,
    QgsProcessingOutputVectorLayer,
    QgsProcessingParameterBoolean,
    QgsProcessingUtils,
)

from . import outputs as outputs_module
from . import paramspec, parameters, settings
from .locator import InvestNotFound, find_binary, quick_version
from .runner import InvestRunner

USERGUIDE_BASE = (
    "https://storage.googleapis.com/releases.naturalcapitalproject.org/"
    "invest-userguide/latest/en/")

#: InVEST models carry no category in their spec, so the toolbox grouping is
#: curated here.  Unlisted models fall back to "Other" so that a model added by
#: a future InVEST release still appears.
_GROUPS = {
    "annual_water_yield": "Freshwater",
    "ndr": "Freshwater",
    "sdr": "Freshwater",
    "seasonal_water_yield": "Freshwater",
    "coastal_blue_carbon": "Marine and Coastal",
    "coastal_blue_carbon_preprocessor": "Marine and Coastal",
    "coastal_vulnerability": "Marine and Coastal",
    "habitat_risk_assessment": "Marine and Coastal",
    "scenic_quality": "Marine and Coastal",
    "wave_energy": "Marine and Coastal",
    "wind_energy": "Marine and Coastal",
    "carbon": "Terrestrial",
    "crop_production_percentile": "Terrestrial",
    "crop_production_regression": "Terrestrial",
    "forest_carbon_edge_effect": "Terrestrial",
    "habitat_quality": "Terrestrial",
    "pollination": "Terrestrial",
    "recreation": "Terrestrial",
    "scenario_generator_proximity": "Terrestrial",
    "stormwater": "Urban",
    "urban_cooling_model": "Urban",
    "urban_flood_risk_mitigation": "Urban",
    "urban_mental_health": "Urban",
    "urban_nature_access": "Urban",
    "delineateit": "Support Tools",
    "routedem": "Support Tools",
}

_LAYER_HINTS = {
    "raster": QgsProcessingUtils.LayerHint.Raster,
    "vector": QgsProcessingUtils.LayerHint.Vector,
}


class InvestAlgorithm(QgsProcessingAlgorithm):
    """Exposes one InVEST model, built entirely from its normalised spec."""

    def __init__(self, spec):
        super().__init__()
        self._spec = spec
        self._plans = paramspec.plan_model(spec["inputs"])
        # Output ids and parameter names share the results namespace, so any
        # collision is resolved in favour of the parameter.
        parameter_names = {plan["name"] for plan in self._plans}
        self._declared_outputs = [
            record for record in spec["outputs"]
            if not record["is_intermediate"] and record["id"] not in parameter_names]

    # -- identity -----------------------------------------------------------

    def createInstance(self):
        return InvestAlgorithm(self._spec)

    def name(self):
        return self._spec["model_id"]

    def displayName(self):
        return self._spec["model_title"]

    def group(self):
        return _GROUPS.get(self._spec["model_id"], "Other")

    def groupId(self):
        return self.group().lower().replace(" ", "_")

    def tags(self):
        return ["invest", "ecosystem services"] + list(self._spec["aliases"])

    def shortHelpString(self):
        about = self._spec["about"].strip()
        return (f"{about}\n\nResults are written to the workspace folder. "
                f"Top-level outputs are added to the map automatically.")

    def helpUrl(self):
        userguide = self._spec.get("userguide")
        if not userguide:
            return None
        if userguide.startswith("http"):
            return userguide
        return USERGUIDE_BASE + userguide

    def flags(self):
        return super().flags() | Qgis.ProcessingAlgorithmFlag.CanCancel

    # -- definition ---------------------------------------------------------

    def initAlgorithm(self, configuration=None):
        for plan in self._plans:
            self.addParameter(parameters.build(plan))

        load_intermediate = QgsProcessingParameterBoolean(
            paramspec.LOAD_INTERMEDIATE,
            "Also load intermediate outputs onto the map",
            defaultValue=self._intermediate_default())
        load_intermediate.setFlags(
            load_intermediate.flags() | Qgis.ProcessingParameterFlag.Advanced)
        load_intermediate.setHelp(
            "InVEST writes many working files to the model's intermediate "
            "directory. They are produced either way; this only controls "
            "whether they are added to the map.")
        self.addParameter(load_intermediate)

        for record in self._declared_outputs:
            self.addOutput(self._output_definition(record))

    def _intermediate_default(self):
        try:
            return settings.load_intermediate_default()
        except Exception:
            # Settings are unavailable when the algorithm is introspected
            # outside a configured Processing session.
            return False

    def _output_definition(self, record):
        name, about = record["id"], record["about"]
        if record["kind"] == "raster":
            return QgsProcessingOutputRasterLayer(name, about)
        if record["kind"] == "vector":
            return QgsProcessingOutputVectorLayer(name, about)
        return QgsProcessingOutputFile(name, about)

    # -- execution ----------------------------------------------------------

    def _runner(self):
        """Return a configured runner, or raise a helpful error."""
        try:
            binary_path = find_binary(settings.app_path())
        except InvestNotFound as error:
            raise QgsProcessingException(str(error)) from error
        return InvestRunner(binary_path, quick_version(binary_path))

    def processAlgorithm(self, parameters_, context, feedback):
        runner = self._runner()

        workspace = self.parameterAsString(
            parameters_, paramspec.WORKSPACE, context)
        if not workspace:
            raise QgsProcessingException("A workspace folder is required.")
        os.makedirs(workspace, exist_ok=True)

        args = parameters.build_args(
            self, self._plans, parameters_, context, feedback)

        if settings.validate_before_run():
            self._validate(runner, args, workspace, feedback)

        if feedback.isCanceled():
            return {}

        runner.run_model(self._spec["model_id"], args, workspace, feedback)

        return self._collect_results(parameters_, context, feedback,
                                     workspace, args)

    def _validate(self, runner, args, workspace, feedback):
        feedback.pushInfo("Validating inputs with InVEST…")
        warnings = runner.validate(self._spec["model_id"], args, workspace)
        if warnings is None:
            feedback.pushWarning(
                "Could not run InVEST validation; continuing anyway.")
            return
        if warnings:
            for keys, message in warnings:
                feedback.reportError(f"{', '.join(keys)}: {message}")
            raise QgsProcessingException(
                "InVEST validation failed. See the errors above.")
        feedback.pushInfo("Inputs validated.")

    def _collect_results(self, parameters_, context, feedback, workspace, args):
        include_intermediate = self.parameterAsBoolean(
            parameters_, paramspec.LOAD_INTERMEDIATE, context)
        suffix = args.get(paramspec.RESULTS_SUFFIX, "")

        found = outputs_module.collect(
            self._spec["outputs"], workspace, suffix, include_intermediate)

        declared_ids = {record["id"] for record in self._declared_outputs}
        results = {paramspec.WORKSPACE: workspace}
        loaded = 0

        for record in found:
            full_path = record["full_path"]
            if record["id"] in declared_ids:
                results[record["id"]] = full_path

            hint = _LAYER_HINTS.get(record["kind"])
            if hint is None:
                # Tables, HTML reports and logs stay on disk.
                continue

            details = QgsProcessingContext.LayerDetails(
                outputs_module.layer_name(record), context.project(),
                record["id"] if record["id"] in declared_ids else "", hint)
            details.groupName = self._spec["model_title"]
            context.addLayerToLoadOnCompletion(full_path, details)
            loaded += 1

        if loaded:
            feedback.pushInfo(
                f"Adding {loaded} output layer(s) to the map in group "
                f"'{self._spec['model_title']}'.")
        else:
            feedback.pushWarning(
                "No spatial outputs were found to add to the map. Check the "
                "workspace folder.")
        return results
