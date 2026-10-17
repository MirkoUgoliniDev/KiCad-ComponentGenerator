"""@brief AI-only bodies in the main window: out of the table and the 3D until kept (HANDOFF 141).

The inventory marks a body the vision model added with no detector proposal
``_ai_only_excluded`` True. Applied without the review dialog (pause off, or
declined), such a body must not enter the components table nor the 3D; one
log line says how many were left out and how to keep them. It must not be
lost either: reopening the photo review offers it again, and the saved
project carries it aside. Negative cases: the same body already kept by the
user (``_ai_only_excluded`` False) enters the table and the 3D as before.
"""
from copy import deepcopy
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
pytest.importorskip("PyQt6")
pytest.importorskip("pyvistaqt")
from PyQt6.QtWidgets import QDialog, QMessageBox

from kicadgen.gui import main_window as gui

from tests.test_pipeline_pauses import (  # noqa: F401 - shared helpers
    _answer_pause, _component, _extracted, _ready, _run_extraction)


def _ai_only(excluded=True):
    """An inductor the detector missed, added by the model alone, far from R1."""
    return _component("L1", x_mm=-6.0, y_mm=-3.0, length_mm=3.0, width_mm=3.0,
                      source_candidate_ids=[], _ai_only_excluded=excluded,
                      _review_reason="added by the model with no detector proposal")


def _log(window):
    return window.ai_progress_text.toPlainText()


def _run(window, monkeypatch, components):
    """An extraction applied with the pause switched off: no review is shown."""
    _ready(window, monkeypatch)
    monkeypatch.setattr(window, "_inventory_pause_enabled", lambda: False)
    _answer_pause(monkeypatch, None)
    _run_extraction(window, monkeypatch)
    window._on_extracted(_extracted([_component("R1", source_candidate_ids=[1])] + components))
    window._on_extract_finished()


def test_without_the_review_an_excluded_body_is_neither_in_the_table_nor_in_the_3d(
        window, monkeypatch):
    _run(window, monkeypatch, [_ai_only()])
    assert [c.name for c in window._components_from_table()] == ["R1"]
    assert [r["name"] for r in window._photo_components_from_table()] == ["R1"]
    worker = window._worker
    assert worker is not None, "the run goes on with the kept bodies"
    assert [(c.x, c.length) for c in worker.body.components] == [(1.0, 4.0)]
    assert [r["name"] for r in window._photo_excluded_rows] == ["L1"]
    log = _log(window)
    assert "1 corpi aggiunti dal solo modello" in log
    assert "«Revisione componenti…»" in log, "the log names the real button"


def test_a_kept_ai_only_body_enters_the_table_and_the_3d(window, monkeypatch):
    """Negative: same body, already kept by the user -> as any other body."""
    _run(window, monkeypatch, [_ai_only(excluded=False)])
    assert [c.name for c in window._components_from_table()] == ["R1", "L1"]
    assert len(window._worker.body.components) == 2
    assert window._photo_excluded_rows == []
    assert "corpi aggiunti dal solo modello" not in _log(window)


def _real_review(monkeypatch, action=None):
    """Replace exec() of the REAL dialog: optional action, then Apply.

    @return List of the component lists each opened dialog received.
    """
    opened = []

    class Review(gui.PhotoReviewDialog):
        def exec(self):
            opened.append(self.get_components())
            if action is not None:
                action(self)
            self.accept()
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(gui, "PhotoReviewDialog", Review)
    return opened


def test_reopening_the_review_offers_the_excluded_body_again(window, monkeypatch):
    _run(window, monkeypatch, [_ai_only()])
    opened = _real_review(monkeypatch)
    window._show_photo_comparison()
    assert [c["name"] for c in opened[0]] == ["R1", "L1"]
    assert opened[0][1]["_ai_only_excluded"] is True
    # Applied without keeping it: still aside, still offered next time.
    assert [c.name for c in window._components_from_table()] == ["R1"]
    assert [r["name"] for r in window._photo_excluded_rows] == ["L1"]
    window._show_photo_comparison()
    assert [c["name"] for c in opened[1]] == ["R1", "L1"]


def test_keeping_it_in_the_review_moves_it_into_the_table(window, monkeypatch):
    _run(window, monkeypatch, [_ai_only()])
    _real_review(monkeypatch, action=lambda dialog: dialog.toggle_keep(1))
    window._show_photo_comparison()
    assert [c.name for c in window._components_from_table()] == ["R1", "L1"]
    assert window._photo_excluded_rows == []
    assert window._photo_components_from_table()[1]["_ai_only_excluded"] is False


def test_the_hand_editor_does_not_drop_the_bodies_set_aside(window, monkeypatch):
    """An apply that is not the photo review (hand editor, format painter)
    changes the table and leaves the excluded bodies where they are."""
    _run(window, monkeypatch, [_ai_only()])
    window._apply_photo_review(window._photo_components_from_table())
    assert [r["name"] for r in window._photo_excluded_rows] == ["L1"]


def test_the_saved_project_carries_them_aside_and_a_new_inventory_replaces_them(
        window, monkeypatch):
    _run(window, monkeypatch, [_ai_only()])
    state = window._project_state()
    saved = [c for c in state["components"] if c.get("_ai_only_excluded") is True]
    assert [c["name"] for c in saved] == ["L1"]
    window._set_photo_components([], replace_excluded=True)
    assert window._photo_excluded_rows == []
    window._apply_project_state(deepcopy(state))
    assert [c.name for c in window._components_from_table()] == ["R1"]
    assert [r["name"] for r in window._photo_excluded_rows] == ["L1"]
    # A whole new inventory without AI-only bodies leaves nothing stale aside.
    window._set_photo_components([_component("R2")], replace_excluded=True)
    assert window._photo_excluded_rows == []


# The extraction path converts the inventory with datasheet_extractor.
# _components_3d(trusted=True): its key whitelist must carry
# `_ai_only_excluded`, otherwise a first extraction delivers the AI-only bodies
# as ordinary ones (the relaunch path and saved projects keep the flag anyway).
def test_the_flag_survives_the_extraction_conversion():
    from kicadgen.services.datasheet_extractor import _components_3d
    converted = _components_3d([_ai_only()], trusted=True)
    assert converted[0].get("_ai_only_excluded") is True
