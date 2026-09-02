"""Decide which QGIS parameter each InVEST input needs.

This module deliberately has no QGIS imports so the mapping rules can be tested
without a QGIS runtime.  :mod:`parameters` turns the plans produced here into
real ``QgsProcessingParameter`` objects.
"""

import re

#: InVEST's taskgraph worker count.  The plugin always runs models
#: synchronously, so this is never exposed as a parameter.
N_WORKERS = "n_workers"
N_WORKERS_VALUE = -1

WORKSPACE = "workspace_dir"
RESULTS_SUFFIX = "results_suffix"

#: Extra parameter the plugin adds itself, not part of any InVEST spec.
LOAD_INTERMEDIATE = "load_intermediate_outputs"

_RANGE_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*(<=|<)\s*value\s*(<=|<)\s*(-?\d+(?:\.\d+)?)")
_MIN_RE = re.compile(r"value\s*(>=|>)\s*(-?\d+(?:\.\d+)?)")
_MAX_RE = re.compile(r"value\s*(<=|<)\s*(-?\d+(?:\.\d+)?)")

_GEOMETRY_TOKENS = {
    "POINT": "point", "MULTIPOINT": "point",
    "LINESTRING": "line", "MULTILINESTRING": "line",
    "POLYGON": "polygon", "MULTIPOLYGON": "polygon",
}


def parse_bounds(expression, input_type):
    """Return ``(minimum, maximum, force_integer)`` for a numeric input.

    Bounds come from the InVEST type first (a ratio is 0-1, a percent 0-100)
    and are then refined by the spec's validation ``expression``.

    Strict comparisons are treated as inclusive: QGIS spin boxes only support
    inclusive bounds, and InVEST re-validates the exact rule at run time, so a
    boundary value is rejected there rather than silently accepted.
    """
    minimum = maximum = None
    if input_type == "ratio":
        minimum, maximum = 0.0, 1.0
    elif input_type == "percent":
        minimum, maximum = 0.0, 100.0

    force_integer = False
    if expression:
        # ``float(value).is_integer()`` is how InVEST marks a whole-number
        # field (e.g. carbon's LULC year) that is not typed as an integer.
        force_integer = "is_integer" in expression

        match = _RANGE_RE.search(expression)
        if match:
            minimum = float(match.group(1))
            maximum = float(match.group(4))
        else:
            match = _MIN_RE.search(expression)
            if match:
                minimum = float(match.group(2))
            match = _MAX_RE.search(expression)
            if match:
                maximum = float(match.group(2))

    return minimum, maximum, force_integer


def geometry_tokens(geometry_types):
    """Return the distinct QGIS source-type tokens for a vector input."""
    tokens = {_GEOMETRY_TOKENS[name] for name in geometry_types
              if name in _GEOMETRY_TOKENS}
    # An input accepting every geometry, or one whose types we do not
    # recognise, should not filter the layer list at all.
    if not tokens or len(tokens) == 3:
        return ["any"]
    return sorted(tokens)


def build_description(record):
    """Return the label shown next to the widget.

    InVEST names are lowercase ("baseline LULC"), so the first character is
    capitalised.  Conditional requirements cannot be enforced by the Processing
    dialog, so the condition is surfaced in the label instead.
    """
    name = record.get("name") or record.get("id") or ""
    description = name[:1].upper() + name[1:] if name else record.get("id", "")

    units = record.get("units")
    if units and units not in ("unitless", "none", ""):
        description = f"{description} [{units}]"

    required = record.get("required", True)
    if isinstance(required, str):
        description = f"{description} (required if: {required})"
    return description


def build_help(record):
    """Return the tooltip / help text for a parameter."""
    parts = []
    if record.get("about"):
        parts.append(record["about"])

    allowed = record.get("allowed", True)
    if isinstance(allowed, str):
        parts.append(f"Only used when: {allowed}")

    options = record.get("options") or []
    described = [f"{option['key']}: {option['about']}"
                 for option in options if option.get("about")]
    if described:
        parts.append("Options — " + "; ".join(described))

    if record.get("dynamic_options"):
        parts.append(
            "Valid values depend on another input, so they cannot be listed "
            "here. Enter the value (typically a field name) directly.")
    return "\n\n".join(parts)


def plan_parameter(record):
    """Return a plan dict describing the QGIS parameter for one InVEST input.

    Returns ``None`` for inputs the plugin handles itself rather than exposing.
    """
    input_id = record["id"]
    if input_id == N_WORKERS:
        return None

    input_type = record.get("type") or "string"
    required = record.get("required", True)
    # A conditional requirement is an expression over other inputs, which the
    # dialog cannot evaluate, so the widget must be optional and the real rule
    # left to InVEST's own validation.
    optional = required is not True

    plan = {
        "name": input_id,
        "description": build_description(record),
        "help": build_help(record),
        "optional": optional,
        "advanced": bool(record.get("hidden")) or input_id == RESULTS_SUFFIX,
        "invest_type": input_type,
    }

    if input_id == WORKSPACE or input_type == "workspace":
        plan.update(kind="folder_destination", optional=False, advanced=False)
    elif input_type == "raster":
        plan["kind"] = "raster"
    elif input_type == "vector":
        plan["kind"] = "vector"
        plan["geometries"] = geometry_tokens(record.get("geometry_types") or set())
    elif input_type == "csv":
        plan.update(kind="file", extension="csv",
                    file_filter="CSV files (*.csv *.CSV);;All files (*.*)")
    elif input_type == "file":
        plan["kind"] = "file"
    elif input_type == "directory":
        plan["kind"] = "folder"
    elif input_type == "boolean":
        # A Processing boolean is always present, so it can never be "unset".
        plan.update(kind="boolean", optional=False, default=False)
    elif input_type == "option_string":
        options = record.get("options") or []
        if options:
            keys = [option["key"] for option in options]
            labels = [option["display_name"] for option in options]
            plan.update(
                kind="enum",
                options=labels,
                option_keys=keys,
                # The default has to be one of the option strings, which are
                # the labels; a key would match nothing when they differ.
                default=None if optional else labels[0])
        else:
            # A runtime-computed dropdown; fall back to free text.
            plan["kind"] = "string"
    elif input_type in ("number", "integer", "ratio", "percent"):
        minimum, maximum, force_integer = parse_bounds(
            record.get("expression"), input_type)
        plan.update(
            kind="integer" if (input_type == "integer" or force_integer) else "number",
            minimum=minimum, maximum=maximum)
    else:
        plan["kind"] = "string"

    return plan


def plan_model(inputs):
    """Return parameter plans for every exposed input of a model."""
    plans = []
    for record in inputs:
        plan = plan_parameter(record)
        if plan is not None:
            plans.append(plan)
    return plans


def label_for(plans, key):
    """Return the dialog label for an InVEST argument key.

    Validation messages name raw argument ids; the user is looking at
    descriptions, so translate before showing them.
    """
    for plan in plans:
        if plan["name"] == key:
            # Strip the conditional annotation, which is noise in an error.
            return plan["description"].split(" (required if:")[0]
    return key


def format_validation_warnings(warnings, plans):
    """Render InVEST validation warnings as readable text.

    Args:
        warnings: ``[[keys], message]`` pairs as returned by InVEST.
        plans: parameter plans, used to name inputs the way the dialog does.
    """
    lines = []
    for entry in warnings:
        try:
            keys, message = entry
        except (TypeError, ValueError):
            lines.append(str(entry))
            continue
        labels = ", ".join(label_for(plans, key) for key in keys)
        lines.append(f"\u2022 {labels}: {message}")
    return "InVEST found problems with these inputs:\n\n" + "\n".join(lines)


def enum_key_for_value(plan, value):
    """Return the InVEST option key for a value coming out of the widget.

    A QgsProcessingParameterEnum with usesStaticStrings stores the option
    strings themselves, and those are the human-readable labels, so the label
    has to be translated back to the key InVEST expects.  A key is accepted
    unchanged, so a value typed in the modeler or passed to qgis_process still
    works.
    """
    keys = plan.get("option_keys") or []
    if value in keys:
        return value
    for label, key in zip(plan.get("options") or [], keys):
        if label == value:
            return key
    return value


def enum_value_for_key(plan, key):
    """Return the widget value for an InVEST option key."""
    for candidate, label in zip(plan.get("option_keys") or [],
                                plan.get("options") or []):
        if candidate == key:
            return label
    return key
