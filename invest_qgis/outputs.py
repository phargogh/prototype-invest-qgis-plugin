"""Resolve declared InVEST outputs to real files on disk.

InVEST declares its outputs as relative paths in the model spec.  Turning one
into an actual path means applying the results-suffix rule, and an output may
legitimately be absent because its ``created_if`` condition was not met, so
every candidate is existence-checked before being offered to QGIS.
"""

import os


def suffix_string(results_suffix):
    """Return the suffix InVEST will splice into output filenames.

    Mirrors ``natcap.invest.utils.make_suffix_string``: empty for no suffix,
    otherwise prefixed with an underscore unless the user supplied one.
    """
    if not results_suffix:
        return ""
    suffix = str(results_suffix).strip()
    if not suffix:
        return ""
    return suffix if suffix.startswith("_") else f"_{suffix}"


def resolve_path(workspace, relative_path, suffix):
    """Return the absolute path of a declared output.

    The suffix is inserted before the extension of the *filename only*.
    Directory components are never suffixed.
    """
    parts = [part for part in relative_path.split("/") if part]
    if not parts:
        return workspace
    directories, filename = parts[:-1], parts[-1]
    if suffix:
        stem, extension = os.path.splitext(filename)
        filename = f"{stem}{suffix}{extension}"
    return os.path.join(workspace, *directories, filename)


def collect(output_records, workspace, results_suffix, include_intermediate):
    """Return the outputs that exist on disk and should be reported.

    Args:
        output_records: normalised output records from :mod:`normalize`.
        workspace: the model's workspace directory.
        results_suffix: the user's results suffix, if any.
        include_intermediate: whether outputs in subdirectories are included.

    Returns:
        A list of records with an added absolute ``full_path``.
    """
    suffix = suffix_string(results_suffix)
    collected = []
    for record in output_records:
        if record["is_intermediate"] and not include_intermediate:
            continue
        full_path = resolve_path(workspace, record["path"], suffix)
        if not os.path.exists(full_path):
            # Either a conditional output that was not produced, or a file the
            # model names differently at run time; neither is an error.
            continue
        resolved = dict(record)
        resolved["full_path"] = full_path
        collected.append(resolved)
    return collected


def layer_name(record):
    """Return a human-readable layer name for an output."""
    stem = os.path.splitext(os.path.basename(record["path"]))[0]
    return stem
