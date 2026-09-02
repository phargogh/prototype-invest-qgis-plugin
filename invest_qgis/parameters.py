"""Build QGIS parameters from InVEST input plans, and read their values back.

Only ``Qgis.*``-scoped enums are used here.  The class-scoped spellings
(``QgsProcessingParameterFile.Folder`` and friends) still work in QGIS 3.x but
are monkey-patched compatibility shims that QGIS 4 removes.
"""

import os

from qgis.core import (
    Qgis,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFile,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterNumber,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterString,
    QgsProcessingParameterVectorLayer,
    QgsVectorFileWriter,
    QgsVectorLayer,
)

from . import paramspec

_SOURCE_TYPES = {
    "point": Qgis.ProcessingSourceType.VectorPoint,
    "line": Qgis.ProcessingSourceType.VectorLine,
    "polygon": Qgis.ProcessingSourceType.VectorPolygon,
    "any": Qgis.ProcessingSourceType.VectorAnyGeometry,
}

#: Formats InVEST can open directly, so a layer already stored in one of these
#: needs no conversion.
_OGR_COMPATIBLE = ["gpkg", "shp", "geojson"]

#: Containers that can hold more than one layer, where handing InVEST the bare
#: file path would be ambiguous.
_MULTILAYER_EXTENSIONS = {".gpkg", ".gdb", ".sqlite", ".kml", ".gml"}


def build(plan):
    """Return a configured ``QgsProcessingParameterDefinition`` for one plan."""
    kind = plan["kind"]
    name = plan["name"]
    description = plan["description"]
    optional = plan["optional"]

    if kind == "folder_destination":
        parameter = QgsProcessingParameterFolderDestination(name, description)
    elif kind == "raster":
        parameter = QgsProcessingParameterRasterLayer(
            name, description, optional=optional)
    elif kind == "vector":
        types = [_SOURCE_TYPES[token] for token in plan.get("geometries", ["any"])]
        parameter = QgsProcessingParameterVectorLayer(
            name, description, types=types, optional=optional)
    elif kind == "file":
        parameter = QgsProcessingParameterFile(
            name, description,
            behavior=Qgis.ProcessingFileParameterBehavior.File,
            optional=optional)
        # extension and fileFilter are mutually exclusive; the filter is
        # friendlier because it still lets a user pick an oddly-named file.
        if plan.get("file_filter"):
            parameter.setFileFilter(plan["file_filter"])
    elif kind == "folder":
        parameter = QgsProcessingParameterFile(
            name, description,
            behavior=Qgis.ProcessingFileParameterBehavior.Folder,
            optional=optional)
    elif kind == "boolean":
        parameter = QgsProcessingParameterBoolean(
            name, description, defaultValue=plan.get("default", False))
    elif kind == "enum":
        parameter = QgsProcessingParameterEnum(
            name, description, options=plan["options"], allowMultiple=False,
            defaultValue=plan.get("default"), optional=optional,
            usesStaticStrings=True)
    elif kind in ("number", "integer"):
        number_type = (Qgis.ProcessingNumberParameterType.Integer
                       if kind == "integer"
                       else Qgis.ProcessingNumberParameterType.Double)
        parameter = QgsProcessingParameterNumber(
            name, description, type=number_type, optional=optional)
        if plan.get("minimum") is not None:
            parameter.setMinimum(plan["minimum"])
        if plan.get("maximum") is not None:
            parameter.setMaximum(plan["maximum"])
    else:
        parameter = QgsProcessingParameterString(
            name, description, optional=optional)

    if plan.get("help"):
        parameter.setHelp(plan["help"])
    if plan.get("advanced"):
        parameter.setFlags(parameter.flags() | Qgis.ProcessingParameterFlag.Advanced)
    return parameter


def _is_unset(parameters, name):
    """Return True when the user left an optional parameter blank."""
    if name not in parameters:
        return True
    value = parameters[name]
    return value is None or (isinstance(value, str) and not value.strip())


def _raster_path(algorithm, parameters, name, context):
    """Return an on-disk GDAL path for a raster parameter."""
    layer = algorithm.parameterAsRasterLayer(parameters, name, context)
    if layer is None:
        raise QgsProcessingException(
            algorithm.invalidRasterError(parameters, name))
    if layer.providerType() != "gdal":
        # A WMS/WCS or in-memory raster has no path InVEST could open, and the
        # failure deep inside the model would be unrecognisable.
        raise QgsProcessingException(
            f"'{name}' must be a file-based (GDAL) raster, but the chosen "
            f"layer uses the '{layer.providerType()}' provider. Export it to "
            f"GeoTIFF first.")
    return layer.source()


def _materialise_single_layer(path, layer_name, context, feedback):
    """Write one layer of a multi-layer container to a standalone file.

    InVEST opens a vector by path and takes the first layer, so a specific
    layer inside a GeoPackage has to be extracted first.
    """
    source = QgsVectorLayer(f"{path}|layername={layer_name}", layer_name, "ogr")
    if not source.isValid():
        return path

    target = os.path.join(
        context.temporaryFolder() or os.path.dirname(path),
        f"{layer_name}.gpkg")
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    options.layerName = layer_name
    error = QgsVectorFileWriter.writeAsVectorFormatV3(
        source, target, context.transformContext(), options)
    # writeAsVectorFormatV3 returns (errorCode, message, newFilename, newLayer)
    if error[0] != QgsVectorFileWriter.WriterError.NoError:
        if feedback is not None:
            feedback.pushWarning(
                f"Could not isolate layer '{layer_name}' from {path}: {error[1]}")
        return path
    return target


def _vector_path(algorithm, parameters, name, context, feedback):
    """Return an on-disk OGR path for a vector parameter.

    Uses ``parameterAsCompatibleSourceLayerPathAndLayerName`` rather than
    ``layer.source()`` so that memory layers, a "selected features only"
    choice, and provider-level subset filters are all honoured instead of
    silently passing InVEST the full dataset.
    """
    path, layer_name = (
        algorithm.parameterAsCompatibleSourceLayerPathAndLayerName(
            parameters, name, context,
            compatibleFormats=_OGR_COMPATIBLE, preferredFormat="gpkg",
            feedback=feedback))
    if not path:
        raise QgsProcessingException(
            algorithm.invalidSourceError(parameters, name))

    extension = os.path.splitext(path)[1].lower()
    if layer_name and extension in _MULTILAYER_EXTENSIONS:
        stem = os.path.splitext(os.path.basename(path))[0]
        if layer_name != stem:
            return _materialise_single_layer(path, layer_name, context, feedback)
    return path


def _layer_source(algorithm, parameters, name, context):
    """Return a layer's path without converting or copying anything.

    Used for validation, where writing a temporary GeoPackage on every
    keystroke would be unacceptable and InVEST only needs to see a path.
    """
    layer = algorithm.parameterAsLayer(parameters, name, context)
    if layer is not None:
        return layer.source()
    return algorithm.parameterAsString(parameters, name, context)


def read_value(algorithm, plan, parameters, context, feedback, materialise=True):
    """Return the value to place in InVEST's ``args`` for one parameter.

    Unset optional parameters yield an empty string, which is what InVEST
    expects for "not provided".

    With ``materialise`` false, layer inputs are reported by their existing
    source rather than being exported to a format InVEST can open, and
    unusable layers are passed through instead of raising.  That makes the
    call cheap and side-effect free, which is what live validation needs.
    """
    kind = plan["kind"]
    name = plan["name"]

    if kind == "folder_destination":
        return algorithm.parameterAsString(parameters, name, context)

    if kind == "boolean":
        # Booleans are always present, and InVEST requires a real bool rather
        # than a string.
        return bool(algorithm.parameterAsBoolean(parameters, name, context))

    if _is_unset(parameters, name):
        return ""

    if kind == "raster":
        if not materialise:
            return _layer_source(algorithm, parameters, name, context)
        return _raster_path(algorithm, parameters, name, context)
    if kind == "vector":
        if not materialise:
            return _layer_source(algorithm, parameters, name, context)
        return _vector_path(algorithm, parameters, name, context, feedback)
    if kind in ("file", "folder"):
        return algorithm.parameterAsFile(parameters, name, context)
    if kind == "enum":
        return paramspec.enum_key_for_value(
            plan, algorithm.parameterAsEnumString(parameters, name, context))
    if kind == "integer":
        return algorithm.parameterAsInt(parameters, name, context)
    if kind == "number":
        return algorithm.parameterAsDouble(parameters, name, context)
    return algorithm.parameterAsString(parameters, name, context)


def build_args(algorithm, plans, parameters, context, feedback, workspace=None,
               materialise=True):
    """Return the complete InVEST ``args`` dict for a run.

    Every key the model declares is included, because InVEST's validation
    reports a missing key differently from an empty one.

    ``workspace`` must be the already-resolved workspace path.  Re-resolving it
    here would be wrong: for a destination parameter left as "temporary",
    every call to ``parameterAsString`` mints a *new* temporary directory, so
    the datastack would name a different folder from the one the model is
    told to write into.
    """
    args = {}
    for plan in plans:
        if workspace is not None and plan["kind"] == "folder_destination":
            args[plan["name"]] = workspace
            continue
        args[plan["name"]] = read_value(
            algorithm, plan, parameters, context, feedback, materialise)
    # Always run synchronously: spawning taskgraph worker processes from a
    # subprocess of QGIS is unreliable across platforms.
    args[paramspec.N_WORKERS] = paramspec.N_WORKERS_VALUE
    return args
