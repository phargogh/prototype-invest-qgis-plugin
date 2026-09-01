"""Harvest every InVEST model spec in one pass.

The frozen ``invest`` binary takes roughly a minute to start, so asking it for
26 model specs one CLI call at a time would take close to half an hour.  The
shared :mod:`server` pays that startup cost once and then answers spec requests
in milliseconds.
"""

from . import server

#: Kept for callers that catch harvest failures specifically.
HarvestError = server.ServerError


def harvest_specs(binary_path, progress=None, is_canceled=None):
    """Return ``{model_id: raw_spec}`` for every model the binary provides.

    Args:
        binary_path: path to the frozen ``invest`` executable.
        progress: optional callable receiving human-readable status strings.
        is_canceled: optional callable returning True to abort early.

    Raises:
        HarvestError: if the server cannot be started or queried.
    """
    progress = progress or (lambda message: None)
    is_canceled = is_canceled or (lambda: False)

    invest = server.get(binary_path)
    if not invest.is_ready():
        progress("Starting the InVEST server (this takes about a minute)…")
    invest.ensure_running()

    if is_canceled():
        raise HarvestError("Cancelled.")

    progress("Fetching the model list…")
    model_ids = sorted(invest.models())

    specs = {}
    for index, model_id in enumerate(model_ids, start=1):
        if is_canceled():
            raise HarvestError("Cancelled.")
        progress(f"Reading spec {index}/{len(model_ids)}: {model_id}")
        try:
            specs[model_id] = invest.getspec(model_id)
        except server.ServerError as error:
            # One unreadable model should not lose the other 25.
            progress(f"Skipping {model_id}: {error}")

    if not specs:
        raise HarvestError("The InVEST server returned no model specs.")
    return specs
