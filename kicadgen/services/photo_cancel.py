"""@brief The cancellation channel of the local photo phases («Interrompi» / Stop).

The photo inventory runs long local phases before any paid call: board
search and calibration, edge measurements, the YOLO views (a 48 MP photo:
about 22 s of calibration and 165 s over 66 views, review 2026-11-16 R8-2).
They never looked at the user's «Interrompi», so the analysis ran to the end
and even opened the review window. Every phase now checks the same channel
the local SMD network already checked, and stops with the documented
cancellation type `CallCancelled`, which the extractor turns into
`ExtractionCancelled`.
"""
from __future__ import annotations

try:
    from .ai_diagnostics import CallCancelled
    from .messages import tx
except ImportError:  # loaded as a standalone file (import isolation tests)
    from kicadgen.services.ai_diagnostics import CallCancelled
    from kicadgen.services.messages import tx


def cancel_requested(cancel) -> bool:
    """@brief Whether a cancellation channel is set.

    @param cancel A threading.Event-like object (`is_set`), a predicate
        returning a truth value, or None.
    @return True only when the channel says so; None, or a channel that
        raises, is not a request (a broken channel must not stop the work).
    """
    if cancel is None:
        return False
    try:
        return bool(cancel.is_set() if hasattr(cancel, "is_set") else cancel())
    except Exception:  # noqa: BLE001 - a broken channel is not a cancellation request
        return False


def raise_if_cancelled(cancel) -> None:
    """@brief Stop the local photo work when the user asked to.

    @param cancel Cancellation channel, see `cancel_requested`.
    @throws CallCancelled When the channel is set.
    """
    if cancel_requested(cancel):
        raise CallCancelled(tx("Analisi locale della foto annullata."))
