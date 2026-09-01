"""Normalise InVEST ``getspec`` JSON into a single stable shape.

InVEST's serialised model spec has changed across releases, most notably in how
outputs are described:

* InVEST 3.16 keys ``outputs`` by *relative path* and nests subdirectories in a
  ``contents`` list.
* InVEST 3.20 keys ``outputs`` by *logical id* and gives each entry an explicit
  ``path`` that already contains any subdirectory.

Everything downstream of this module consumes the normalised form only, so the
rest of the plugin never has to care which InVEST version produced a spec.
"""

import ast
import os
import posixpath

#: Outputs under these top-level directories are taskgraph bookkeeping, never
#: something a user wants on their map.
TASKGRAPH_DIRS = frozenset({"taskgraph_cache", "taskgraph_dir"})

#: Directories holding a model's working files rather than its results.
#: Everything InVEST calls "intermediate" plus its scratch directory; note that
#: "output", "outputs" and "visualization_outputs" are *results* directories
#: and must not be treated as intermediate, or models that write everything
#: into "output/" would appear to produce nothing at all.
_INTERMEDIATE_DIRS = frozenset({
    "intermediate", "intermediate_output", "intermediate_outputs",
    "intermediate_files", "tmp",
})


def is_intermediate_path(path):
    """Return True when a relative output path is a working file.

    Only the first path component is considered: InVEST groups its outputs one
    directory deep.
    """
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2:
        return False
    return parts[0].lower() in _INTERMEDIATE_DIRS


RASTER_EXTENSIONS = {".tif", ".tiff", ".img", ".vrt"}
VECTOR_EXTENSIONS = {".shp", ".gpkg", ".geojson", ".gml", ".kml"}
TABLE_EXTENSIONS = {".csv", ".tsv"}


class UnsupportedSpec(Exception):
    """Raised when a spec does not match any known InVEST layout."""


def parse_geometry_types(value):
    """Return a set of OGR geometry type names from a serialised spec value.

    InVEST serialises ``geometry_types`` as the ``repr`` of a Python set, e.g.
    ``"{'MULTIPOLYGON', 'POLYGON'}"``.  Older and newer variants may use a plain
    list, so both are accepted.
    """
    if not value:
        return set()
    if isinstance(value, (list, tuple, set)):
        return {str(item).upper() for item in value}
    if isinstance(value, str):
        try:
            parsed = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            # Fall back to treating it as a single bare name.
            return {value.strip().upper()}
        if isinstance(parsed, (list, tuple, set)):
            return {str(item).upper() for item in parsed}
        return {str(parsed).upper()}
    return set()


def parse_options(value):
    """Return a list of ``{key, display_name, about}`` dicts.

    ``getspec --json`` emits a list of objects, but the in-memory dataclass uses
    a ``{key: {display_name, description}}`` dict, so accept both.
    """
    if not value:
        return []
    options = []
    if isinstance(value, dict):
        for key, info in value.items():
            info = info if isinstance(info, dict) else {}
            options.append({
                "key": str(key),
                "display_name": info.get("display_name") or str(key),
                "about": info.get("about") or info.get("description") or "",
            })
        return options
    for item in value:
        if isinstance(item, dict):
            key = str(item.get("key", ""))
            options.append({
                "key": key,
                "display_name": item.get("display_name") or key,
                "about": item.get("about") or item.get("description") or "",
            })
        else:
            options.append({
                "key": str(item), "display_name": str(item), "about": ""})
    return [option for option in options if option["key"]]


def _classify(spec, path):
    """Return one of ``raster``, ``vector``, ``table`` or ``file``."""
    # Spec keys are authoritative when present; InVEST only attaches
    # geometry_types to vectors and columns/rows to tables.
    if spec.get("geometry_types"):
        return "vector"
    if spec.get("columns") or spec.get("rows"):
        return "table"
    if spec.get("data_type") or spec.get("bands"):
        return "raster"

    extension = os.path.splitext(path)[1].lower()
    if extension in RASTER_EXTENSIONS:
        return "raster"
    if extension in VECTOR_EXTENSIONS:
        return "vector"
    if extension in TABLE_EXTENSIONS:
        return "table"
    return "file"


def _normalise_output(spec, path):
    """Build a normalised output record for a leaf output."""
    # Paths are always stored posix-style so that suffix handling and the
    # intermediate check behave the same on every platform.
    path = path.replace(os.sep, "/").lstrip("/")
    return {
        "id": spec.get("id") or path,
        "path": path,
        "kind": _classify(spec, path),
        "about": spec.get("about") or "",
        "created_if": spec.get("created_if"),
        "is_intermediate": is_intermediate_path(path),
    }


def _walk_nested_outputs(outputs, prefix, collected):
    """Recurse the InVEST 3.16 nested ``contents`` layout."""
    # 3.16 keys the mapping by relative path; a list is possible inside
    # ``contents``, where the path lives on the ``id``.
    items = outputs.items() if isinstance(outputs, dict) else (
        (entry.get("id", ""), entry) for entry in outputs)

    for key, spec in items:
        if not isinstance(spec, dict):
            continue
        name = spec.get("id") or key
        path = posixpath.join(prefix, name) if prefix else name
        if "contents" in spec and spec["contents"]:
            _walk_nested_outputs(spec["contents"], path, collected)
        else:
            collected.append(_normalise_output(spec, path))


def normalise_outputs(raw_outputs):
    """Return a list of normalised output records from either spec layout."""
    if not raw_outputs:
        return []

    values = list(raw_outputs.values()) if isinstance(raw_outputs, dict) else list(raw_outputs)
    # InVEST 3.20+ gives every output an explicit ``path``; 3.16 does not.
    is_flat = any(isinstance(value, dict) and "path" in value for value in values)

    collected = []
    if is_flat:
        for spec in values:
            if not isinstance(spec, dict):
                continue
            path = spec.get("path") or spec.get("id") or ""
            if path:
                collected.append(_normalise_output(spec, path))
    else:
        _walk_nested_outputs(raw_outputs, "", collected)

    # Taskgraph's cache is declared in the spec but is never user-facing.
    return [output for output in collected
            if output["path"].split("/")[0] not in TASKGRAPH_DIRS]


def _normalise_input(spec):
    """Build a normalised input record."""
    input_type = spec.get("type") or "string"
    record = {
        "id": spec.get("id") or "",
        "name": spec.get("name") or spec.get("id") or "",
        "about": spec.get("about") or "",
        "type": input_type,
        # ``required`` and ``allowed`` are either a bool or a Python expression
        # string evaluated against the other argument values.
        "required": spec.get("required", True),
        "allowed": spec.get("allowed", True),
        "hidden": bool(spec.get("hidden", False)),
        "units": spec.get("units"),
        "expression": spec.get("expression"),
    }

    if input_type == "vector":
        record["geometry_types"] = parse_geometry_types(spec.get("geometry_types"))
    elif input_type == "option_string":
        record["options"] = parse_options(spec.get("options"))
        # A dropdown_function means the choices are computed at runtime from
        # another argument's value, and cannot be enumerated ahead of time.
        record["dynamic_options"] = bool(
            spec.get("dropdown_function")) and not record["options"]
    elif input_type == "string":
        record["regexp"] = spec.get("regexp")
    elif input_type == "raster":
        record["data_type"] = spec.get("data_type")

    return record


def normalise_inputs(raw_args, input_field_order):
    """Return normalised inputs in the order the InVEST UI would show them.

    Inputs named in ``input_field_order`` come first, in that order.  Anything
    absent from it (``n_workers``, and recreation's ``hostname``/``port``) is
    appended and marked hidden, matching how the InVEST Workbench treats them.
    """
    if not raw_args:
        return []

    ordered_ids = []
    for group in input_field_order or []:
        # A group may be null in some specs to signal a visual separator.
        for arg_id in (group or []):
            if arg_id in raw_args and arg_id not in ordered_ids:
                ordered_ids.append(arg_id)

    trailing = [arg_id for arg_id in raw_args if arg_id not in ordered_ids]

    inputs = []
    for arg_id in ordered_ids + trailing:
        spec = raw_args[arg_id]
        if not isinstance(spec, dict):
            continue
        spec = dict(spec)
        spec.setdefault("id", arg_id)
        record = _normalise_input(spec)
        if arg_id in trailing:
            record["hidden"] = True
        inputs.append(record)
    return inputs


def normalise(raw_spec):
    """Normalise one raw ``getspec`` payload.

    Raises:
        UnsupportedSpec: if the payload has no recognisable arguments section.
    """
    if not isinstance(raw_spec, dict):
        raise UnsupportedSpec("Model spec is not a JSON object")

    # ``args`` is what ModelSpec.to_json emits; ``inputs`` is the dataclass
    # attribute name, tolerated in case a future release stops renaming it.
    raw_args = raw_spec.get("args")
    if raw_args is None:
        raw_args = raw_spec.get("inputs")
    if not isinstance(raw_args, dict):
        raise UnsupportedSpec(
            f"Model spec for {raw_spec.get('model_id', '<unknown>')!r} has no "
            f"recognisable 'args' mapping; this InVEST version may not be "
            f"supported by the plugin.")

    return {
        "model_id": raw_spec.get("model_id") or "",
        "model_title": raw_spec.get("model_title") or raw_spec.get("model_id") or "",
        "about": raw_spec.get("about") or "",
        "userguide": raw_spec.get("userguide") or "",
        "aliases": list(raw_spec.get("aliases") or []),
        "inputs": normalise_inputs(raw_args, raw_spec.get("input_field_order")),
        "outputs": normalise_outputs(raw_spec.get("outputs")),
    }
