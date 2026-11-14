"""@brief The «hole or terminal» question in the window (extractor G2, 2026-11-14).

@details A printed diameter that, read as the hole, does not let the
terminal read for the same pads through: the extractor asks whether it is the
hole or the terminal. The question rides on the «pad_layout» field with a
payload of kind `HOLE_OR_TERMINAL_KIND`, so the window treats it as a
footprint question: open, it blocks every footprint write; «Rigenera
footprint» asks it, the REAL `apply_answers` applies the chosen payload, the
answer is remembered and the package rebuilt. Its values («…=hole»,
«…=terminal») are never read as «Lascia bloccato». Offline, no call is made.
Positive and negative cases.
"""

from copy import deepcopy
from dataclasses import asdict

from kicadgen.services import datasheet_extractor as ext
from tests.test_footprint_draft import project  # noqa: F401 - pytest fixture
from tests.test_package_pad_dialog import _tht_index

KIND = ext.HOLE_OR_TERMINAL_KIND


def _question(window):
    """(question, extraction) for the window's first holed pad: hole Ø0.80 or terminal (hole 1.10, land 1.80)."""
    pin = window.package_geometry.pins[_tht_index(window)]
    number = str(pin.number)
    hole, terminal = f"{KIND}:{number}@0.8=hole", f"{KIND}:{number}@0.8=terminal"
    key = f"pad_layout:{KIND}:{number}"
    question = {"key": key, "field": "pad_layout", "text": "Foro o terminale?", "evidence": None,
                "options": [{"label": "Il foro", "value": hole},
                            {"label": "Il terminale", "value": terminal}]}
    changes = {number: {"drill_mm": 1.1, "pad_length_mm": 1.8, "pad_width_mm": 1.8,
                        "shape": "circle", "sources": {"drill_mm": "estimated"}}}
    candidates = {key: {hole: {"kind": KIND, "pads": [number], "choice": "hole"},
                        terminal: {"kind": KIND, "pads": [number], "choice": "terminal",
                                   "changes": changes}}}
    params = ext.ExtractedParams(name="", description="", tags="", confidence="low", notes="",
                                 layout="package", package_geometry=asdict(window.package_geometry),
                                 questions=[deepcopy(question)], candidates=candidates)
    return question, params, number, hole, terminal


def _hold(window, monkeypatch, params, question, answer):
    """The window holding the open question on `params`, answering `answer`."""
    window._extraction_questions = window._valid_extraction_questions([question])
    window._extraction_answers = {}
    window._pads_kept_blocked = False
    window._last_extracted = params
    monkeypatch.setattr(window, "_settled_extraction", lambda: window._last_extracted)
    monkeypatch.setattr(window, "_ask_extraction_question", lambda q: answer)


def test_the_question_blocks_the_footprint_until_answered(project, monkeypatch):  # noqa: F811
    """@brief Open -> unattended writes refused with the pad-layout text; negative: answered -> no block."""
    window, _generations = project
    question, params, _number, hole, _terminal = _question(window)
    _hold(window, monkeypatch, params, question, hole)
    assert window._pad_layout_question_block()
    assert window._resolve_stored_pad_layout_question() is True
    assert window._pad_layout_question_block() == ""


def test_terminal_answer_rebuilds_the_pad_from_the_terminal(project, monkeypatch):  # noqa: F811
    """@brief Positive: «terminal» -> the pad gets the derived hole, the answer is remembered, no block."""
    window, _generations = project
    question, params, number, _hole, terminal = _question(window)
    _hold(window, monkeypatch, params, question, terminal)
    assert window._resolve_stored_pad_layout_question() is True
    pin = next(p for p in window.package_geometry.pins if str(p.number) == number)
    assert pin.drill_mm == 1.1
    assert window._extraction_answers == {question["key"]: terminal}
    assert window._extraction_questions == [] and window._pads_kept_blocked is False


def test_hole_answer_keeps_the_hole_and_is_never_a_kept_block(project, monkeypatch):  # noqa: F811
    """@brief Negative: «hole» -> the printed hole stays; neither value reads as «Lascia bloccato»."""
    window, _generations = project
    question, params, number, hole, terminal = _question(window)
    before = next(p for p in window.package_geometry.pins if str(p.number) == number).drill_mm
    _hold(window, monkeypatch, params, question, hole)
    assert window._resolve_stored_pad_layout_question() is True
    pin = next(p for p in window.package_geometry.pins if str(p.number) == number)
    assert pin.drill_mm == before and window._pads_kept_blocked is False
    assert not window._is_layout_block_answer(hole) and not window._is_layout_block_answer(terminal)
    assert window._is_layout_block_answer(f"{ext.LAYOUT_BLOCK_KIND}:blocked")
