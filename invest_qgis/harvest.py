"""Harvest every InVEST model spec in one pass.

The frozen ``invest`` binary takes roughly a minute to start, so asking it for
26 model specs one CLI call at a time would take close to half an hour.  The
shared :mod:`server` pays that startup cost once and then answers spec requests
in milliseconds.
"""

from . import server

#: Kept for callers that catch harvest failures specifically.
HarvestError = server.ServerError

#: Roughly how long the frozen binary takes to start.  Only used to turn a
#: wait with no natural progress into a moving bar; the fraction is clamped so
#: a slow machine never appears to finish early.
_EXPECTED_START_SECONDS = 75.0

#: Share of the overall progress given to server startup.  Reading the specs
#: themselves takes well under a second, so startup really is almost all of it.
_START_SHARE = 0.9


def harvest_specs(binary_path, progress=None, is_canceled=None):
    """Return ``{model_id: raw_spec}`` for every model the binary provides.

    Args:
        binary_path: path to the frozen ``invest`` executable.
        progress: optional callable ``(message, fraction)`` where fraction is
            0..1, or None when it cannot be estimated.
        is_canceled: optional callable returning True to abort early.

    Raises:
        HarvestError: if the server cannot be started or queried.
    """
    progress = progress or (lambda message, fraction=None: None)
    is_canceled = is_canceled or (lambda: False)

    invest = server.get(binary_path)

    def on_wait(elapsed):
        fraction = min(elapsed / _EXPECTED_START_SECONDS, 1.0) * _START_SHARE
        progress("Starting InVEST (about a minute)…", fraction)

    if not invest.is_ready():
        progress("Starting InVEST (about a minute)…", 0.0)
    invest.ensure_running(on_wait=on_wait)

    if is_canceled():
        raise HarvestError("Cancelled.")

    progress("Reading the model list…", _START_SHARE)
    model_ids = sorted(invest.models())

    specs = {}
    for index, model_id in enumerate(model_ids, start=1):
        if is_canceled():
            raise HarvestError("Cancelled.")
        progress(f"Reading {model_id} ({index}/{len(model_ids)})",
                 _START_SHARE + (1 - _START_SHARE) * index / len(model_ids))
        try:
            specs[model_id] = invest.getspec(model_id)
        except server.ServerError as error:
            # One unreadable model should not lose the other 25.
            progress(f"Skipping {model_id}: {error}", None)

    if not specs:
        raise HarvestError("The InVEST server returned no model specs.")
    return specs
