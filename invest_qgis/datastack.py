"""Read InVEST parameter-set (datastack) files and map them onto parameters.

An InVEST datastack is JSON of the form::

    {"model_id": "sdr", "invest_version": "3.20.0", "args": {...}}

Older files use ``model_name`` holding a Python module path instead of
``model_id``.  Values in the wild are messy: paths are usually relative to the
datastack file, numbers are frequently quoted, and argument names drift between
InVEST releases, so nothing here assumes a well-formed modern file.
"""

import json
import os

#: Argument keys the plugin manages itself and never loads from a datastack.
_IGNORED_KEYS = {"n_workers"}

#: The workspace is deliberately not restored.  A datastack records wherever it
#: was authored, which is usually a path from another machine or a previous
#: run, so applying it risks quietly overwriting earlier results.  It is
#: reported separately rather than dropped silently.
_SKIPPED_KEYS = {"workspace_dir"}

#: Parameter kinds whose values are filesystem paths.
_PATH_KINDS = {"raster", "vector", "file", "folder", "folder_destination"}

_TRUE_STRINGS = {"true", "1", "yes", "y", "t"}
_FALSE_STRINGS = {"false", "0", "no", "n", "f"}


class DatastackError(Exception):
    """Raised when a file cannot be read as an InVEST parameter set."""


def read(path):
    """Return ``(model_id, args, invest_version)`` from a datastack file.

    Raises:
        DatastackError: if the file is missing, is not JSON, or has no
            recognisable arguments.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as error:
        raise DatastackError(f"Could not read {path}: {error}") from error
    except ValueError as error:
        raise DatastackError(
            f"{os.path.basename(path)} is not valid JSON: {error}") from error

    if not isinstance(payload, dict):
        raise DatastackError(
            f"{os.path.basename(path)} does not contain a JSON object.")

    args = payload.get("args")
    if args is None and all(
            key not in payload for key in ("model_id", "model_name", "invest_version")):
        # Tolerate a bare mapping of argument names to values.
        args = payload
    if not isinstance(args, dict):
        raise DatastackError(
            f"{os.path.basename(path)} has no 'args' section, so it does not "
            f"look like an InVEST datastack.")

    model_id = payload.get("model_id") or _model_id_from_pyname(
        payload.get("model_name"))
    return model_id, args, payload.get("invest_version") or ""


def _model_id_from_pyname(pyname):
    """Best-effort model id from an old-style ``natcap.invest.x.y`` name.

    Only used to warn about a mismatch, never to reject a file, because the
    mapping is not reliable (``natcap.invest.hra`` is ``habitat_risk_assessment``).
    """
    if not pyname or not isinstance(pyname, str):
        return ""
    return pyname.rsplit(".", 1)[-1]


def _enum_label(plan, key):
    """Return the dropdown label for an InVEST option key."""
    for candidate, label in zip(plan.get("option_keys") or [],
                                plan.get("options") or []):
        if candidate == key:
            return label
    return key


def _coerce_bool(value):
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE_STRINGS:
        return True
    if text in _FALSE_STRINGS:
        return False
    return None


def _resolve_path(value, base_dir):
    """Make a datastack path absolute, the way InVEST itself does."""
    expanded = os.path.expanduser(os.path.expandvars(value))
    if os.path.isabs(expanded):
        return os.path.normpath(expanded)
    candidate = os.path.normpath(os.path.join(base_dir, expanded))
    # Only rewrite when the relative path actually resolves; otherwise keep
    # the original text so the user sees what the file asked for.
    return candidate if os.path.exists(candidate) else expanded


def to_parameter_values(args, plans, base_dir):
    """Translate datastack arguments into QGIS parameter values.

    Args:
        args: the ``args`` mapping from a datastack.
        plans: parameter plans from :mod:`paramspec` for the target model.
        base_dir: directory of the datastack, for resolving relative paths.

    Returns:
        A dict with ``values`` (parameter name to value), ``empty`` (keys the
        datastack left blank), ``unknown`` (keys this model does not have),
        ``skipped`` (keys deliberately not restored) and ``problems``
        (human-readable conversion failures).
    """
    by_name = {plan["name"]: plan for plan in plans}
    values, empty, unknown, problems, skipped = {}, [], [], [], []

    for key, raw in args.items():
        if key in _IGNORED_KEYS:
            continue
        if key in _SKIPPED_KEYS:
            skipped.append(key)
            continue
        plan = by_name.get(key)
        if plan is None:
            unknown.append(key)
            continue
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            # A blank entry means "not provided"; leave the widget alone
            # rather than actively clearing it.
            empty.append(key)
            continue

        kind = plan["kind"]
        if kind == "boolean":
            coerced = _coerce_bool(raw)
            if coerced is None:
                problems.append(f"{key}: {raw!r} is not a true/false value")
                continue
            values[key] = coerced
        elif kind == "integer":
            try:
                values[key] = int(float(str(raw).strip()))
            except (TypeError, ValueError):
                problems.append(f"{key}: {raw!r} is not a whole number")
        elif kind == "number":
            try:
                values[key] = float(str(raw).strip())
            except (TypeError, ValueError):
                problems.append(f"{key}: {raw!r} is not a number")
        elif kind == "enum":
            text = str(raw).strip()
            keys = plan.get("option_keys") or []
            match = next((k for k in keys if k.lower() == text.lower()), None)
            if match is None:
                problems.append(
                    f"{key}: {text!r} is not one of {', '.join(keys)}")
                continue
            # The widget's options are labels, so hand it the label.
            values[key] = _enum_label(plan, match)
        elif kind in _PATH_KINDS:
            values[key] = _resolve_path(str(raw), base_dir)
        else:
            values[key] = str(raw)

    return {"values": values, "empty": sorted(empty),
            "unknown": sorted(unknown), "problems": problems,
            "skipped": sorted(skipped)}


def load_for_plans(path, plans):
    """Read a datastack and translate it for a model in one step."""
    model_id, args, invest_version = read(path)
    result = to_parameter_values(args, plans, os.path.dirname(os.path.abspath(path)))
    result["model_id"] = model_id
    result["invest_version"] = invest_version
    return result
