"""@brief Footprint edits as a draft with Applica/Annulla, like the symbol (user, 2026-10-14).

Agreed behaviour (HANDOFF, 2026-10-14 sera): drawings, Reference/Value
positions and pad edits stay in a DRAFT that the preview shows while the
project file and the .kicad_mod keep the applied state; «Applica» saves and
rewrites once, «Annulla» goes back; Ctrl+Z/Y work inside the draft; leaving
the project asks (one question for symbol and footprint); the KiCad tab and
every generation use the applied state; a new run asks first; a crash leaves
a recovery file offered by «Recupera bozza». Each rule has a positive case
and a negative one that differ structurally (a draft exists or not, the save
works or not, the recovery file is bound to this applied state or not).
"""
import json
from copy import deepcopy

import pytest
from PyQt6.QtWidgets import QApplication, QMessageBox

from kicadgen.core.footprint import validate_footprint_drawings
from kicadgen.gui.main_window import TAB_PREVIEW, TAB_SYMBOL, MainWindow
from kicadgen.gui.project_state import PROJECT_CONFIG_NAME
from kicadgen.services import component_check
from kicadgen.services.footprint_draft import (FOOTPRINT_DRAFT_FILE, KEPT_DRAFTS_FILE,
                                               load_footprint_draft, save_footprint_draft)
from tests.test_package_pad_dialog import _drive, _tht_index
from tests.test_package_project_gui import acquire
from tests.test_photo_inventory_gui import gui

#: The real questions, captured at import (conftest answers them in every test).
_REAL_ASK_LEAVE = MainWindow._ask_pending_edits_choice
_REAL_ASK_START = MainWindow._ask_footprint_draft_before_start

KICAD_WARNING = ("ATTENZIONE: il footprint ha modifiche non applicate. Verifica, test, "
                 "esportazione e installazione usano il footprint applicato: usa Applica "
                 "nella scheda Footprint per includerle.")


def record(kind="line", points=((0, 0), (5, 0))):
    return {"kind": kind, "layer": "F.SilkS", "width": 0.12,
            "points": [list(p) for p in points]}


def _config(window):
    return json.loads((window._project_dir() / PROJECT_CONFIG_NAME).read_text(encoding="utf-8"))


def _recovery(window):
    return window._project_dir() / FOOTPRINT_DRAFT_FILE


def _kept(window):
    return window._project_dir() / KEPT_DRAFTS_FILE


@pytest.fixture
def project(window, monkeypatch):
    """Acquired package project, applied state saved; generations recorded instead of run."""
    acquire(window)
    QApplication.processEvents()
    assert window._save_project_config(force=True)
    generations = []
    monkeypatch.setattr(window, "_generate", lambda **kwargs: generations.append(kwargs) or True)
    monkeypatch.setattr(window, "_project_has_footprint", lambda: True)
    window._draw_preview()
    return window, generations


def _draft_everything(window, monkeypatch):
    """One drawing, one text position, one package pad drill: the three kinds of edit."""
    window._apply_footprint_drawings(validate_footprint_drawings([record()]))
    assert window._set_footprint_text_position("Reference", (1.0, 2.0))
    _drive(monkeypatch, drill=0.5)
    window._edit_package_pad(_tht_index(window))


# ── 1. Edits stay in the draft ──────────────────────────────────────────

def test_edits_stay_in_the_draft_and_the_file_keeps_the_applied_state(project, monkeypatch):
    window, generations = project
    before = _config(window)
    assert not window.has_pending_footprint_edits(), "negative: a fresh project has no draft"
    _draft_everything(window, monkeypatch)
    assert window.has_pending_footprint_edits()
    # The preview shows the draft...
    assert window.footprint_drawings == validate_footprint_drawings([record()])
    assert window.package_geometry.pins[_tht_index(window)].drill_mm == 0.5
    # ...the file and the .kicad_mod do not get it, whoever saves.
    QApplication.processEvents()
    assert generations == []
    window._save_project_config(force=True)        # autosave or any other save
    after = _config(window)
    assert after["state"]["footprint_drawings"] == before["state"]["footprint_drawings"] == []
    assert after["state"]["footprint_texts"] == {}
    assert after["state"]["package_geometry"]["pins"] == before["state"]["package_geometry"]["pins"]
    assert window.btn_fp_apply.isEnabledTo(window.btn_fp_apply.parentWidget())
    assert window.btn_fp_cancel.isEnabledTo(window.btn_fp_cancel.parentWidget())


# ── 2. Applica: one project write, one rewrite ──────────────────────────

def test_applica_writes_the_project_once_and_rewrites_the_footprint_once(project, monkeypatch):
    window, generations = project
    saves = []
    real_save = window._save_project_config
    monkeypatch.setattr(window, "_save_project_config",
                        lambda force=False: saves.append(force) or real_save(force=force))
    _draft_everything(window, monkeypatch)
    assert saves == []
    assert window.apply_footprint_draft()
    QApplication.processEvents()
    assert saves == [True], "one project write"
    assert generations == [{"footprint_only": True}], "one .kicad_mod rewrite"
    state = _config(window)["state"]
    assert state["footprint_drawings"] == validate_footprint_drawings([record()])
    # Marked as bottom-left anchors since 2026-10-16 (test_footprint_property_anchor).
    assert state["footprint_texts"] == {"Reference": [1.0, 2.0], "anchor": "left_bottom"}
    assert state["package_geometry"]["pins"][_tht_index(window)]["drill_mm"] == 0.5
    assert not window.has_pending_footprint_edits()
    assert not window.btn_fp_apply.isEnabledTo(window.btn_fp_apply.parentWidget())
    # Negative: nothing pending, Applica writes and rewrites nothing.
    assert window.apply_footprint_draft()
    QApplication.processEvents()
    assert saves == [True] and generations == [{"footprint_only": True}]


def test_a_failed_applica_keeps_the_draft(project, monkeypatch):
    window, generations = project
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args))
    monkeypatch.setattr(window, "_save_project_config", lambda force=False: False)
    window._apply_footprint_drawings(validate_footprint_drawings([record()]))
    assert not window.apply_footprint_draft()
    QApplication.processEvents()
    assert warnings and generations == []
    assert window.has_pending_footprint_edits(), "the draft waits for a later Applica"
    assert window.footprint_drawings == validate_footprint_drawings([record()])


def test_a_pad_change_is_rewritten_even_without_a_footprint_file(project, monkeypatch):
    """Pad edits keep their rule (they rewrite an acquired component); drawings do not."""
    window, generations = project
    monkeypatch.setattr(window, "_project_has_footprint", lambda: False)
    window._apply_footprint_drawings(validate_footprint_drawings([record()]))
    window.apply_footprint_draft()
    QApplication.processEvents()
    assert generations == [], "negative: a drawing never creates a .kicad_mod"
    window._edit_footprint_draft({"pad_overrides": {"1": {"shape": "rect", "size": 2.0}}})
    window.apply_footprint_draft()
    QApplication.processEvents()
    assert generations == [{"footprint_only": True}]


# ── 3. Annulla goes back to the applied state ───────────────────────────

def test_annulla_restores_the_applied_state_and_can_be_undone(project, monkeypatch):
    window, generations = project
    window._apply_footprint_drawings(validate_footprint_drawings([record()]))
    window.apply_footprint_draft()
    applied = list(window.footprint_drawings)
    window._apply_footprint_drawings([])
    assert window._set_footprint_text_position("Value", (0.0, 4.0))
    saves_before = _config(window)
    window.btn_fp_cancel.click()
    assert window.footprint_drawings == applied and window.footprint_text_positions == {}
    assert not window.has_pending_footprint_edits()
    assert _config(window) == saves_before, "Annulla writes nothing"
    # Ctrl+Z brings the discarded draft back, as a draft.
    window._undo_footprint_drawing()
    assert window.footprint_drawings == [] and window.footprint_text_positions == {"Value": (0.0, 4.0)}
    assert window.has_pending_footprint_edits()
    # Negative: with nothing pending Annulla records nothing.
    window.discard_footprint_draft()
    depth = len(window._fp_drawings_undo)
    window.discard_footprint_draft()
    assert len(window._fp_drawings_undo) == depth


# ── Undo/redo inside the draft ──────────────────────────────────────────

def test_undo_and_redo_cover_drawings_texts_and_pads_inside_the_draft(project, monkeypatch):
    window, generations = project
    window._apply_footprint_drawings(validate_footprint_drawings([record()]))
    assert window._set_footprint_text_position("Reference", (1.0, 2.0))
    _drive(monkeypatch, drill=0.5)
    window._edit_package_pad(_tht_index(window))
    index = _tht_index(window)
    applied_drill = _config(window)["state"]["package_geometry"]["pins"][index]["drill_mm"]
    window._undo_footprint_drawing()
    assert window.package_geometry.pins[index].drill_mm == applied_drill, "the pad edit is undone"
    window._undo_footprint_drawing()
    assert window.footprint_text_positions == {}
    window._undo_footprint_drawing()
    assert window.footprint_drawings == []
    assert not window.has_pending_footprint_edits(), "back to the applied state: nothing pending"
    window._redo_footprint_drawing()
    assert window.footprint_drawings == validate_footprint_drawings([record()])
    assert window.has_pending_footprint_edits()
    QApplication.processEvents()
    assert generations == [], "undo and redo write nothing"


def test_a_new_package_rebases_the_draft_and_drops_its_history(project, monkeypatch):
    """An analysis that replaces the package: its pads become the applied ones."""
    from copy import deepcopy
    window, _generations = project
    _drive(monkeypatch, drill=0.5)
    window._edit_package_pad(_tht_index(window))
    assert window.has_pending_footprint_edits()
    window.package_geometry = deepcopy(window.package_geometry)   # a new package object
    assert not window.has_pending_footprint_edits(), "the old package's draft does not apply"
    assert window._fp_drawings_undo == []
    # Negative: the same object keeps its draft and its history.
    window._edit_package_pad(_tht_index(window))
    window._apply_footprint_drawings(validate_footprint_drawings([record()]))
    assert window.has_pending_footprint_edits() and len(window._fp_drawings_undo) == 1


# ── 4. Leaving the project asks, one question for both editors ─────────

def test_leaving_with_a_footprint_draft_asks_and_follows_the_answer(project, monkeypatch):
    window, generations = project
    asked = []

    def answer(choice):
        monkeypatch.setattr(window, "_ask_pending_edits_choice",
                            lambda editors, can_apply: asked.append((editors, can_apply)) or choice)

    assert window._can_leave_project() and asked == [], "negative: no draft, no question"
    window._apply_footprint_drawings(validate_footprint_drawings([record()]))
    answer(window.PENDING_EDITS_STAY)
    assert not window._can_leave_project()
    assert asked == [(("footprint",), True)]
    assert window.tabs.currentIndex() == TAB_PREVIEW
    assert window.has_pending_footprint_edits()
    answer(window.PENDING_EDITS_DISCARD)
    assert window._can_leave_project()
    assert window.footprint_drawings == [] and _config(window)["state"]["footprint_drawings"] == []
    window._apply_footprint_drawings(validate_footprint_drawings([record()]))
    answer(window.PENDING_EDITS_APPLY)
    assert window._can_leave_project()
    assert _config(window)["state"]["footprint_drawings"] == validate_footprint_drawings([record()])
    assert generations == [{"footprint_only": True}], "rewritten before leaving, not later"


def test_both_editors_pending_are_named_in_one_question(window, monkeypatch):
    # A board whose symbol rows match its pads (no acquired package here).
    from tests.test_symbol_integration import setup_board
    setup_board(window, monkeypatch)
    monkeypatch.setattr(window, "_refresh_output_actions", lambda: None)
    window.symbol_tab.place_pin('1', 50.8, 25.4)
    window._apply_footprint_drawings(validate_footprint_drawings([record()]))
    asked = []
    monkeypatch.setattr(window, "_ask_pending_edits_choice",
                        lambda editors, can_apply: asked.append(editors) or window.PENDING_EDITS_STAY)
    assert not window._can_leave_project()
    assert asked == [("symbol", "footprint")] and window.tabs.currentIndex() == TAB_SYMBOL
    monkeypatch.setattr(window, "_ask_pending_edits_choice",
                        lambda editors, can_apply: window.PENDING_EDITS_APPLY)
    assert window._can_leave_project()
    assert not window.symbol_tab.has_pending_edits() and not window.has_pending_footprint_edits()


def test_the_real_question_names_the_editors_concerned(window, monkeypatch):
    seen = {}

    def fake_exec(box):
        seen["text"] = box.text()
        seen["buttons"] = sorted(b.text() for b in box.buttons())
        seen["default"] = box.defaultButton().text()
        return 0
    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    monkeypatch.setattr(MainWindow, "_ask_pending_edits_choice", _REAL_ASK_LEAVE)
    tx = window.i18n.text
    assert window._ask_pending_edits_choice(("footprint",), True) == window.PENDING_EDITS_STAY
    assert seen["text"] == tx("Il footprint contiene modifiche non applicate.")
    assert window._ask_pending_edits_choice(("symbol", "footprint"), True) == window.PENDING_EDITS_STAY
    assert seen["text"] == tx("Il simbolo e il footprint contengono modifiche non applicate.")
    assert window._ask_pending_edits_choice(("symbol",), True) == window.PENDING_EDITS_STAY
    assert seen["text"] == tx("Il simbolo contiene modifiche non applicate.")
    assert seen["buttons"] == sorted(tx(t) for t in ("Applica", "Scarta", "Resta"))
    assert seen["default"] == tx("Resta")


# ── 5. KiCad tab and exports use the applied state ──────────────────────

def test_the_kicad_tab_warns_about_a_pending_draft(project):
    window, _generations = project
    warning = window.i18n.text(KICAD_WARNING)
    window._refresh_output_actions()
    assert warning not in window.label_kicad_status.text(), "negative: no draft, no warning"
    window._apply_footprint_drawings(validate_footprint_drawings([record()]))
    assert warning in window.label_kicad_status.text()
    window.btn_fp_apply.click()               # the toolbar's «Applica»
    assert not window.has_pending_footprint_edits()
    assert warning not in window.label_kicad_status.text()


def test_a_generation_started_with_a_draft_writes_the_applied_state(window, monkeypatch):
    """The deferred rewrite of an earlier apply never carries a newer draft."""
    acquire(window)
    started = []
    monkeypatch.setattr(gui.GenerateWorker, "start", lambda worker: started.append(worker))
    applied = validate_footprint_drawings([record()])
    window._apply_footprint_drawings(applied)
    window.apply_footprint_draft()
    draft = validate_footprint_drawings([record(), record("circle", ((0, 0), (1, 0)))])
    window._apply_footprint_drawings(draft)
    _drive(monkeypatch, drill=0.5)
    window._edit_package_pad(_tht_index(window))
    window._fp_rewrite_flushing = True        # what `_flush_footprint_rewrite` does
    try:
        assert window._generate(footprint_only=True)
    finally:
        window._fp_rewrite_flushing = False
    assert started[0].drawings == applied, "the file gets the applied drawings"
    assert started[0].params.pins[_tht_index(window)].drill_mm != 0.5, "and the applied pads"
    assert window.footprint_drawings == draft, "the preview keeps the draft"
    assert window.package_geometry.pins[_tht_index(window)].drill_mm == 0.5
    assert window._last_params is window.package_geometry, "the 2D preview draws the draft pads"
    assert window.has_pending_footprint_edits()


def test_the_component_check_reports_a_footprint_draft(tmp_path):
    results = component_check.verify_component(tmp_path, "X", [], footprint_pending=True)
    expected = component_check.tx("Verificato il footprint applicato; la bozza del footprint non è inclusa.")
    assert any(r.status == "WARN" and r.detail == expected for r in results)
    results = component_check.verify_component(tmp_path, "X", [])
    assert not any(r.detail == expected for r in results)


def test_the_verification_passes_the_footprint_draft_state(project, monkeypatch):
    from kicadgen.gui import component_check_dialog
    window, _generations = project
    captured = []

    class Dialog:
        def __init__(self, args, parent, tx):
            captured.append(args)

        def exec(self):
            return 0
    monkeypatch.setattr(component_check_dialog, "ComponentCheckDialog", Dialog)
    window._verify_component()
    window._apply_footprint_drawings(validate_footprint_drawings([record()]))
    window._verify_component()
    assert [a["footprint_pending"] for a in captured] == [False, True]


# ── 6. A new run asks first ─────────────────────────────────────────────

def test_a_generation_start_with_a_draft_asks_first(window, monkeypatch):
    acquire(window)
    started = []
    monkeypatch.setattr(gui.GenerateWorker, "start", lambda worker: started.append(worker))
    asked = []

    def answer(choice):
        monkeypatch.setattr(window, "_ask_footprint_draft_before_start",
                            lambda: asked.append(choice) or choice)

    answer(window.PENDING_EDITS_STAY)
    assert window._generate(footprint_only=True) and asked == [], "negative: no draft, no question"
    window._on_generation_finished()
    started.clear()
    drawing = validate_footprint_drawings([record()])
    window._apply_footprint_drawings(drawing)
    assert window._generate(footprint_only=True) is False, "«Resta» cancels the start"
    assert started == [] and window.has_pending_footprint_edits()
    answer(window.PENDING_EDITS_DISCARD)
    assert window._generate(footprint_only=True)
    assert started[-1].drawings == [] and window.footprint_drawings == []
    window._on_generation_finished()
    window._apply_footprint_drawings(drawing)
    answer(window.PENDING_EDITS_APPLY)
    assert window._generate(footprint_only=True)
    assert started[-1].drawings == drawing and _config(window)["state"]["footprint_drawings"] == drawing


def test_an_analysis_start_with_a_draft_asks_first(window, monkeypatch):
    started = []
    monkeypatch.setattr(window, "_extract_with_ai_prepared", lambda: started.append(True))
    monkeypatch.setattr(window, "_ask_footprint_draft_before_start", lambda: window.PENDING_EDITS_STAY)
    window._extract_with_ai()
    assert started == [True], "negative: no draft, the analysis starts"
    window._apply_footprint_drawings(validate_footprint_drawings([record()]))
    window._extract_with_ai()
    assert started == [True], "«Resta»: the analysis does not start"
    monkeypatch.setattr(window, "_ask_footprint_draft_before_start", lambda: window.PENDING_EDITS_APPLY)
    window._extract_with_ai()
    assert started == [True, True] and not window.has_pending_footprint_edits()


def test_the_real_start_question_stays_by_default(window, monkeypatch):
    seen = {}

    def fake_exec(box):
        seen["default"] = box.defaultButton().text()
        seen["escape"] = box.escapeButton().text()
        return 0
    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    monkeypatch.setattr(MainWindow, "_ask_footprint_draft_before_start", _REAL_ASK_START)
    assert window._ask_footprint_draft_before_start() == window.PENDING_EDITS_STAY
    assert seen["default"] == seen["escape"] == window.i18n.text("Resta")


# ── 7. Recovery file and «Recupera bozza» ───────────────────────────────

def _crash_and_reopen(window):
    """The window state is lost without the leave question: reopen from the files."""
    window._open_project_at(window._project_dir(), save_current=False)


def test_a_crash_leaves_a_draft_offered_and_never_applied(project, monkeypatch):
    window, _generations = project
    drawing = validate_footprint_drawings([record()])
    window._apply_footprint_drawings(drawing)
    assert window._set_footprint_text_position("Value", (0.0, 4.0))
    assert _recovery(window).is_file(), "the draft is checkpointed in the project folder"
    _crash_and_reopen(window)
    assert window.footprint_drawings == [] and not window.has_pending_footprint_edits(), \
        "the project opens with its applied state"
    assert window.btn_fp_recover.isEnabledTo(window.btn_fp_recover.parentWidget())
    window.recover_footprint_draft()          # what the button's click runs
    assert window.footprint_drawings == drawing
    assert window.footprint_text_positions == {"Value": (0.0, 4.0)}
    assert window.has_pending_footprint_edits(), "recovered as a draft, not applied"
    assert _config(window)["state"]["footprint_drawings"] == []
    assert not window.btn_fp_recover.isEnabledTo(window.btn_fp_recover.parentWidget())
    assert _recovery(window).is_file(), "the recovered draft is the file's content now"


def test_an_offered_draft_goes_only_when_discarded_explicitly(project):
    window, _generations = project
    window._apply_footprint_drawings(validate_footprint_drawings([record()]))
    _crash_and_reopen(window)
    assert window._fp_recoverable is not None
    # Editing does not retire the offered draft: it moves to the kept file
    # (review E, 2026-10-14) and stays offered.
    window._apply_footprint_drawings(validate_footprint_drawings([record("circle", ((0, 0), (2, 0)))]))
    window.discard_footprint_draft()
    assert window._fp_recoverable is not None and _kept(window).is_file()
    assert not _recovery(window).is_file(), "nothing pending: no current-draft file"
    assert window.discard_footprint_recovery()
    assert window._fp_recoverable is None and not _kept(window).is_file()
    assert not window.action_fp_discard_recovery.isEnabled()
    assert not window.discard_footprint_recovery(), "negative: nothing offered, nothing discarded"


def test_applica_and_annulla_remove_the_recovery_file(project):
    window, _generations = project
    window._apply_footprint_drawings(validate_footprint_drawings([record()]))
    assert _recovery(window).is_file()
    window.apply_footprint_draft()
    assert not _recovery(window).is_file()
    window._apply_footprint_drawings([])
    assert _recovery(window).is_file()
    window.discard_footprint_draft()
    assert not _recovery(window).is_file()


def test_a_draft_bound_to_another_applied_state_is_not_offered(project):
    window, _generations = project
    state = window._footprint_state_json(window._applied_footprint_state())
    other = dict(state, drawings=validate_footprint_drawings([record("circle", ((0, 0), (3, 0)))]))
    draft = dict(state, drawings=validate_footprint_drawings([record()]))
    save_footprint_draft(_recovery(window), other, draft)
    _crash_and_reopen(window)
    assert window._fp_recoverable is None, "made on another applied state: not offered"
    save_footprint_draft(_recovery(window), state, draft)
    _crash_and_reopen(window)
    assert window._fp_recoverable == draft, "positive: bound to this applied state"


def test_the_recovery_file_never_creates_a_folder(tmp_path):
    target = tmp_path / "missing" / FOOTPRINT_DRAFT_FILE
    state = {"drawings": [], "texts": {}, "pad_overrides": {}, "package_pins": None}
    assert not save_footprint_draft(target, state, dict(state, texts={"Value": [0, 1]}))
    assert not target.parent.exists()
    target = tmp_path / FOOTPRINT_DRAFT_FILE
    assert save_footprint_draft(target, state, dict(state, texts={"Value": [0, 1]}))
    assert load_footprint_draft(target, state) == dict(state, texts={"Value": [0, 1]})
    save_footprint_draft(target, state, state)
    assert load_footprint_draft(target, state) is None, "nothing beyond the applied state"
    target.write_text("{not json", encoding="utf-8")
    assert load_footprint_draft(target, state) is None


# ── Existing projects: the file is the applied state ────────────────────

def test_an_existing_project_opens_as_applied_without_a_draft(project):
    window, _generations = project
    folder = window._project_dir()
    config = json.loads((folder / PROJECT_CONFIG_NAME).read_text(encoding="utf-8"))
    config["state"]["footprint_drawings"] = validate_footprint_drawings([record()])
    config["state"]["footprint_texts"] = {"Reference": [1.0, -3.0]}
    (folder / PROJECT_CONFIG_NAME).write_text(json.dumps(config), encoding="utf-8")
    window._open_project_at(folder, save_current=False)
    assert window.footprint_drawings == validate_footprint_drawings([record()])
    # CONTRACT CHANGED 2026-10-16: an unmarked (pre-anchor) position is the
    # text centre, converted once to its bottom-left corner (KiCad's text
    # geometry: KiCad keeps drawing it there); still no draft.
    from kicadgen.gui.main_window import _property_text_corner_offset
    dx, dy = _property_text_corner_offset("REF**")
    assert window.footprint_text_positions["Reference"] == pytest.approx((1.0 + dx, -3.0 + dy), abs=1e-4)
    assert not window.has_pending_footprint_edits()
    assert not window.btn_fp_apply.isEnabledTo(window.btn_fp_apply.parentWidget())
    assert not window.btn_fp_recover.isEnabledTo(window.btn_fp_recover.parentWidget())
    assert window._project_state()["footprint_drawings"] == window.footprint_drawings


# ── Review fixes (adversarial review, 2026-10-14) ───────────────────────

def _header_pad_widths(window, number):
    from PyQt6.QtWidgets import QGraphicsEllipseItem, QGraphicsRectItem
    return [round(item.rect().width(), 6) for item in window.preview_scene.items()
            if item.data(0x504144) == f"header:{number}"
            and isinstance(item, (QGraphicsRectItem, QGraphicsEllipseItem))]


def _drive_header_pad(monkeypatch, shape, size):
    """Accept «Modifica pad N» of a header with the given shape and size."""
    from PyQt6.QtWidgets import QComboBox, QDialog, QDoubleSpinBox

    def exec_(dialog):
        combo = dialog.findChild(QComboBox)
        combo.setCurrentIndex(combo.findData(shape))
        dialog.findChild(QDoubleSpinBox).setValue(size)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(QDialog, "exec", exec_)


def test_a_header_pad_draft_is_drawn_after_a_generation(window, monkeypatch):
    """Review A: the last generation froze the applied overrides in `_last_params`;
    the preview must draw the draft, its undo and its Annulla."""
    from kicadgen.gui.main_window import PinHeaderParams
    assert window._save_project_config(force=True)
    window._last_params = window._current_params()      # what a generation leaves behind
    assert isinstance(window._last_params, PinHeaderParams)
    window._draw_preview()
    before = _header_pad_widths(window, 2)
    assert before and 50.0 not in before
    _drive_header_pad(monkeypatch, "rect", 2.5)          # the real dialog path
    window._edit_header_pad("2")
    assert window.has_pending_footprint_edits()
    assert 50.0 in _header_pad_widths(window, 2), "the draft land (2.5 mm = 50 px) is drawn"
    window._undo_footprint_drawing()
    assert _header_pad_widths(window, 2) == before, "the undo is visible"
    window._redo_footprint_drawing()
    assert 50.0 in _header_pad_widths(window, 2)
    window.discard_footprint_draft()
    assert _header_pad_widths(window, 2) == before, "Annulla is visible"
    # Negative: the parameters of the last generation are not changed.
    assert not window._last_params.pad_overrides


def test_applica_on_a_working_folder_change_writes_only_the_open_project(window, monkeypatch, tmp_path):
    """Review B: the leave question after a change of «Area di lavoro»."""
    acquire(window)
    QApplication.processEvents()
    assert window._save_project_config(force=True)
    original = window._project_dir()
    started = []
    monkeypatch.setattr(gui.GenerateWorker, "start", lambda worker: started.append(worker))
    monkeypatch.setattr(window, "_project_has_footprint", lambda: True)
    window._apply_footprint_drawings(validate_footprint_drawings([record()]))
    monkeypatch.setattr(window, "_ask_pending_edits_choice",
                        lambda editors, can_apply: window.PENDING_EDITS_APPLY)
    other = tmp_path / "otherroot"
    other.mkdir()
    window.field_library_root.setText(str(other))
    window.field_library_root.setModified(True)
    window._on_library_root_edit_finished()
    QApplication.processEvents()
    saved = json.loads((original / PROJECT_CONFIG_NAME).read_text(encoding="utf-8"))["state"]
    assert saved["footprint_drawings"] == validate_footprint_drawings([record()])
    assert saved.get("footprint_file_stale") is True, "the .kicad_mod is known to be behind"
    assert started == [], "no rewrite with the working folder already changed"
    assert list(other.iterdir()) == [], "nothing is written under the new working folder"
    # The project opened again says so in the KiCad tab.
    window.field_library_root.setText(str(original.parent))
    window._on_library_root_edit_finished()
    window._open_project_at(original, save_current=False)
    assert window._fp_file_stale
    window._refresh_output_actions()
    assert "kicad_mod non contiene" in window.label_kicad_status.text()
    window._on_generated(str(original / "x.kicad_mod"))
    assert not window._fp_file_stale, "a written footprint clears the mark"


def _symbol_draft_then_change_working_folder(window, monkeypatch, tmp_path):
    """A symbol draft, «Applica» answered, then a new «Area di lavoro».

    Returns (open project folder, new working folder, rows applied before, draft rows)."""
    from tests.test_symbol_integration import setup_board
    setup_board(window, monkeypatch)
    monkeypatch.setattr(window, "_refresh_output_actions", lambda: None)
    assert window._save_project_config(force=True)
    original = window._project_dir()
    applied_before = deepcopy(window.pinout)
    window.symbol_tab.place_pin('1', 50.8, 25.4)
    assert window.symbol_tab.has_pending_edits()
    draft_rows = window.symbol_tab.draft_rows()
    monkeypatch.setattr(window, "_ask_pending_edits_choice",
                        lambda editors, can_apply: window.PENDING_EDITS_APPLY)
    other = tmp_path / "otherroot"
    other.mkdir()
    window.field_library_root.setText(str(other))
    window.field_library_root.setModified(True)
    window._on_library_root_edit_finished()
    QApplication.processEvents()
    return original, other, applied_before, draft_rows


def test_symbol_applica_on_a_working_folder_change_writes_only_the_open_project(
        window, monkeypatch, tmp_path):
    """Before: `_save_project_config` refused (`_project_dir()` already named
    the new working folder), and showing the symbol tab for the question
    rebound the editor's recovery file there, dropping the draft from the
    editor: «Applica» was greyed out and the user was kept."""
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: pytest.fail("no error expected"))
    original, other, applied_before, draft_rows = _symbol_draft_then_change_working_folder(
        window, monkeypatch, tmp_path)
    saved = json.loads((original / PROJECT_CONFIG_NAME).read_text(encoding="utf-8"))["state"]
    assert saved["pinout"] != applied_before
    assert saved["pinout"] == draft_rows, "the applied rows are in the open project"
    assert (original / f"{original.name}.kicad_sym").is_file(), "the .kicad_sym is in the open project"
    assert list(other.iterdir()) == [], "nothing is written under the new working folder"
    assert window._open_project_folder != original, "the project was left"


def test_symbol_applica_refused_on_a_working_folder_change_keeps_the_draft(
        window, monkeypatch, tmp_path):
    """Negative: the open project's file cannot be written -> nothing applied, the user stays."""
    monkeypatch.setattr(window, "_write_project_config_at", lambda folder: False)
    original, other, _, _ = _symbol_draft_then_change_working_folder(window, monkeypatch, tmp_path)
    assert window.symbol_tab.has_pending_edits(), "the draft is kept"
    assert window._open_project_folder == original, "the project stays open"
    assert window.field_library_root.text() == str(original.parent), "the working folder goes back"
    assert not (original / f"{original.name}.kicad_sym").exists()
    assert list(other.iterdir()) == []


def test_leaving_while_the_pipeline_owns_the_output_marks_the_footprint_stale(project):
    """Review LOW (verified): an Applica made while a pipeline owns the output
    keeps its rewrite pending; leaving would drop it without a word."""
    window, generations = project
    window._auto_generation_params = object()
    try:
        window._apply_footprint_drawings(validate_footprint_drawings([record()]))
        window.apply_footprint_draft()
        QApplication.processEvents()
        assert generations == [] and window._fp_rewrite_dirty
        assert window._can_leave_project()
    finally:
        window._auto_generation_params = None
    assert _config(window)["state"].get("footprint_file_stale") is True
    # Negative: an idle window writes the footprint before leaving, no mark.
    window._fp_file_stale = False
    window._apply_footprint_drawings([])
    window.apply_footprint_draft()
    assert window._can_leave_project()
    assert "footprint_file_stale" not in _config(window)["state"]


def test_a_deeply_nested_recovery_file_never_blocks_the_project(window, tmp_path):
    """Review C: RecursionError from the JSON parser is an unreadable file."""
    from kicadgen.services.symbol_draft import load_draft
    nested = "[" * 200000 + "]" * 200000
    target = tmp_path / FOOTPRINT_DRAFT_FILE
    target.write_text(nested, encoding="utf-8")
    state = {"drawings": [], "texts": {}, "pad_overrides": {}, "package_pins": None}
    assert load_footprint_draft(target, state) is None
    symbol_file = tmp_path / ".symbol-draft.json"
    symbol_file.write_text(nested, encoding="utf-8")
    assert load_draft(symbol_file, []) is None
    acquire(window)
    QApplication.processEvents()
    assert window._save_project_config(force=True)
    folder = window._project_dir()
    (folder / FOOTPRINT_DRAFT_FILE).write_text(nested, encoding="utf-8")
    (folder / KEPT_DRAFTS_FILE).write_text(nested, encoding="utf-8")
    window._open_project_at(folder, save_current=False)
    assert window._open_project_folder == folder and window._fp_recoverable is None


def test_malformed_recovered_pins_keep_the_open_package_pads(project):
    """Recovery values are re-validated: bad pins never reach the package."""
    window, _generations = project
    state = window._footprint_state_json(window._applied_footprint_state())
    pins = list(window.package_geometry.pins)
    for bad in ([1, 2, 3], [None], [{"number": "1"}], ["x"] * len(state["package_pins"])):
        draft = dict(state, package_pins=bad, drawings=validate_footprint_drawings([record()]))
        save_footprint_draft(_recovery(window), state, draft)
        _crash_and_reopen(window)
        assert window._fp_recoverable is not None
        window.recover_footprint_draft()
        assert list(window.package_geometry.pins) == pins
        assert window.footprint_drawings == validate_footprint_drawings([record()])
        window.discard_footprint_draft()


def test_a_second_crash_recovers_the_newest_edits_and_keeps_the_older_draft(project):
    """Review E: edits made while a crash draft is offered are checkpointed."""
    window, _generations = project
    old = validate_footprint_drawings([record()])
    new = validate_footprint_drawings([record("circle", ((0, 0), (2, 0)))])
    window._apply_footprint_drawings(old)
    _crash_and_reopen(window)                                  # first crash
    assert window._fp_recoverable["drawings"] == old
    window._apply_footprint_drawings(new)                      # edits, offer untouched
    _crash_and_reopen(window)                                  # second crash
    assert window._fp_recoverable["drawings"] == new, "the newest edits come first"
    window.recover_footprint_draft()
    assert window.footprint_drawings == new
    assert window._fp_recoverable is not None and window._fp_recoverable["drawings"] == old, \
        "the older draft stays offered"
    window.recover_footprint_draft()
    assert window.footprint_drawings == old
    assert window._fp_recoverable["drawings"] == new, "the edits it replaced stay recoverable"
    assert window.discard_footprint_recovery()
    assert window._fp_recoverable is None and not _kept(window).is_file()


def test_regenerating_the_3d_model_never_uses_draft_pads(project, monkeypatch):
    """Review F: the STEP/WRL is built from the package pads."""
    window, _generations = project
    window.group_3d.setChecked(True)
    index = _tht_index(window)
    applied = window.package_geometry.pins[index].drill_mm
    _drive(monkeypatch, drill=0.5)
    window._edit_package_pad(index)
    started = []
    monkeypatch.setattr(gui.Cad3DWorker, "start", lambda worker: started.append(worker))
    try:
        monkeypatch.setattr(window, "_ask_footprint_draft_before_start", lambda: window.PENDING_EDITS_STAY)
        window._regenerate_3d()
        assert started == [], "«Resta»: nothing starts"
        # The automatic retry does not ask and uses the applied pads.
        window._regen_3d_pending = True
        window._last_mesh_path = None
        window._run_pending_regen_3d()
        assert started and started[-1].params.pins[index].drill_mm == applied
        assert window.package_geometry.pins[index].drill_mm == 0.5, "the draft stays on screen"
        window._cad3d_worker = None
        window.tabs.setEnabled(True)
        monkeypatch.setattr(window, "_ask_footprint_draft_before_start",
                            lambda: window.PENDING_EDITS_DISCARD)
        window._regenerate_3d()
        assert started[-1].params.pins[index].drill_mm == applied
        assert not window.has_pending_footprint_edits()
    finally:
        window.tabs.setEnabled(True)
        window._cad3d_worker = None


def test_a_worker_never_sees_a_later_pad_edit(window, monkeypatch):
    """Review F: the package is rebound, never mutated, so a running worker keeps its pads."""
    acquire(window)
    started = []
    monkeypatch.setattr(gui.GenerateWorker, "start", lambda worker: started.append(worker))
    index = _tht_index(window)
    assert window._generate(footprint_only=True)
    held = started[0].params
    drill = held.pins[index].drill_mm
    _drive(monkeypatch, drill=0.5)
    window._edit_package_pad(index)
    assert held.pins[index].drill_mm == drill, "the worker's package is unchanged"
    assert window.package_geometry.pins[index].drill_mm == 0.5
    assert window.package_geometry is not held
    assert window.has_pending_footprint_edits() and len(window._fp_drawings_undo) == 1, \
        "a rebound package is not a package replaced by an analysis"


def test_applica_for_both_editors_applies_the_footprint_first(window, monkeypatch):
    """Review H: a footprint that cannot be saved applies nothing; a symbol
    that refuses after the footprint is said."""
    from tests.test_symbol_integration import setup_board
    setup_board(window, monkeypatch)
    monkeypatch.setattr(window, "_refresh_output_actions", lambda: None)
    window.symbol_tab.place_pin('1', 50.8, 25.4)
    window._apply_footprint_drawings(validate_footprint_drawings([record()]))
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a))
    monkeypatch.setattr(window, "_ask_pending_edits_choice",
                        lambda editors, can_apply: window.PENDING_EDITS_APPLY)
    real_apply = window.apply_footprint_draft
    monkeypatch.setattr(window, "apply_footprint_draft", lambda folder=None: False)
    assert not window._can_leave_project()
    assert window.symbol_tab.has_pending_edits(), "the footprint failed: the symbol is not applied"
    monkeypatch.setattr(window, "apply_footprint_draft", real_apply)
    monkeypatch.setattr(window.symbol_tab, "apply", lambda: None)      # the symbol refuses
    assert not window._can_leave_project()
    assert not window.has_pending_footprint_edits(), "the footprint was applied"
    assert any(window.i18n.text("Il footprint è stato applicato; il simbolo no e resta in bozza.")
               in a for a in warnings)


def test_an_analysis_that_replaces_the_package_says_the_pad_draft_is_dropped(project, monkeypatch):
    """Review LOW: no silent loss when a non-asking path replaces the package."""
    from copy import deepcopy
    window, _generations = project
    logs = []
    original = window._append_log
    monkeypatch.setattr(window, "_append_log", lambda m, *a, **k: (logs.append(str(m)), original(m, *a, **k)))
    _drive(monkeypatch, drill=0.5)
    window._edit_package_pad(_tht_index(window))
    window._refresh_output_actions()
    assert _recovery(window).is_file() and window._fp_draft_was_pending
    window.package_geometry = deepcopy(window.package_geometry)
    window.has_pending_footprint_edits()
    QApplication.processEvents()
    assert any("sostituito il package" in line for line in logs)
    assert not _recovery(window).is_file() and not window._fp_draft_was_pending
    # Negative: replacing the package without a pad draft says nothing.
    logs.clear()
    window.package_geometry = deepcopy(window.package_geometry)
    window.has_pending_footprint_edits()
    QApplication.processEvents()
    assert not any("sostituito il package" in line for line in logs)
