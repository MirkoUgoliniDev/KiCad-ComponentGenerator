"""@brief Collegamenti GUI dei servizi corretti il 25/09: annullo (D8) e costi della corsa (D7)."""
from pathlib import Path

import pytest

from kicadgen.gui import main_window as gui

from test_photo_inventory_gui import window  # noqa: F401 - fixture


def test_the_extraction_worker_hands_its_cancel_event_to_the_service(monkeypatch):
    seen = {}

    def fake_extract(doc_paths, *, cancel=None, **kwargs):
        seen["cancel"] = cancel
        return object()

    monkeypatch.setattr(gui, "extract_pin_header_params", fake_extract)
    worker = gui.ExtractWorker([Path("board.png")])
    worker.run()
    assert seen["cancel"] is worker.cancel_event and not worker.cancel_event.is_set()


def test_interrupting_a_run_raises_its_cancel_event(window, monkeypatch):  # noqa: F811
    """@brief Positivo: la corsa interrotta non fa altre chiamate. Negativo: un'altra corsa no."""
    worker, other = gui.ExtractWorker([Path("board.png")]), gui.ExtractWorker([Path("x.png")])
    window._abort_ai_run(worker, "prova")
    assert worker.cancel_event.is_set() and not other.cancel_event.is_set()
    window._abandoned_workers.clear()


def test_the_reconcile_checker_stops_only_after_the_interruption():
    from kicadgen.services.ai_diagnostics import CallCancelled
    worker = gui.ReconcileWorker(b"", [], 10, 10, "m", None)
    worker._raise_if_cancelled()  # negativo: nessuna interruzione, nessuna eccezione
    worker.cancel_event.set()
    with pytest.raises(CallCancelled):
        worker._raise_if_cancelled()


def test_a_run_charges_the_project_it_started_from(window, monkeypatch):  # noqa: F811
    calls = []
    monkeypatch.setattr(window.ai_account, "bind_run_project", lambda folder: calls.append(("bind", folder)))
    monkeypatch.setattr(window.ai_account, "release_run_project", lambda: calls.append(("release",)))
    window._begin_ai_run("Analisi AI")
    assert calls == [("bind", window._project_dir_or_none())]
    window._on_extract_finished()
    assert calls[-1] == ("release",)


def test_a_component_box_with_non_finite_values_is_not_usable():
    """@brief E6 lato generatore: NaN/inf non diventano coordinate inventate."""
    from kicadgen.generators.castellated_module import ComponentBox
    assert ComponentBox(x=1, y=2, length=3, width=2, height=1).is_usable()
    for field in ("x", "y", "length", "width", "height", "rotation"):
        for bad in (float("nan"), float("inf")):
            box = ComponentBox(x=1, y=2, length=3, width=2, height=1)
            setattr(box, field, bad)
            assert not box.is_usable(), (field, bad)
