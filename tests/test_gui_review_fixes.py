"""@brief Regression tests of the GUI review fixes of 2026-10-13 (areas A, F, D2).

Each rule has a positive case and a negative case that differ structurally
(a worker is or is not running, the list did or did not change under a drag,
the value has or has not the kind of its default), not by a tuned number.
"""
import json
import math
import os
from pathlib import Path

import pytest
from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QDialog, QMessageBox

from kicadgen import crash_report as cr
from kicadgen import fsutil
from kicadgen.core.footprint import validate_footprint_drawings
from kicadgen.gui import app_config
from kicadgen.gui.project_dialogs import OpenProjectDialog
from kicadgen.gui.project_state import (project_config_path, read_project_config,
                                        write_project_config)
from tests.test_package_project_gui import acquire, extracted_package
from tests.test_photo_inventory_gui import gui


def record(kind="line", points=((0, 0), (5, 0))):
    return {"kind": kind, "layer": "F.SilkS", "width": 0.12,
            "points": [list(p) for p in points]}


def _turns(qapp, count=5):
    for _ in range(count):
        qapp.processEvents()


# ── A3: a footprint edit saved while a worker runs is not lost ────────────

@pytest.fixture
def busy_window(window, monkeypatch, qapp):
    """Acquired window whose generation start is recorded; `busy` drives the worker state."""
    acquire(window)
    _turns(qapp)
    started = []
    monkeypatch.setattr(gui.GenerateWorker, "start",
                        lambda worker: started.append(worker))
    busy = {"on": False}
    monkeypatch.setattr(window, "_background_work_running", lambda: busy["on"])
    # A project whose .kicad_mod exists: drawing and text edits rewrite it
    # (R3a: they never create one; see the dedicated test).
    monkeypatch.setattr(window, "_project_has_footprint", lambda: True)
    window._draw_preview()
    started.clear()
    return window, busy, started


def test_an_edit_made_while_busy_is_written_once_when_the_worker_finishes(busy_window, qapp):
    window, busy, started = busy_window
    busy["on"] = True
    first = validate_footprint_drawings([record()])
    window._apply_footprint_drawings(first)
    _turns(qapp)
    second = validate_footprint_drawings([record(), record("circle", ((0, 0), (1, 0)))])
    window._apply_footprint_drawings(second)
    _turns(qapp)
    assert started == [], "nothing can start while another worker runs"
    assert window._fp_rewrite_dirty, "the edit is remembered, not dropped"
    busy["on"] = False
    window._on_generation_finished()
    _turns(qapp)
    assert len(started) == 1, "two edits while busy give one rewrite"
    assert started[0].drawings == second, "the rewrite carries the latest data"
    assert not window._fp_rewrite_dirty
    # Another worker finishing later finds nothing to do.
    window._on_regen_3d_finished()
    _turns(qapp)
    assert len(started) == 1


def test_no_edit_while_busy_means_no_rewrite_at_the_finish(busy_window, qapp):
    window, busy, started = busy_window
    busy["on"] = True
    _turns(qapp)
    busy["on"] = False
    window._on_generation_finished()
    window._on_extract_finished()
    _turns(qapp)
    assert started == []
    assert not window._fp_rewrite_dirty


def test_a_text_move_and_a_pad_edit_while_busy_are_retried(busy_window, qapp, monkeypatch):
    from tests.test_package_pad_dialog import _drive, _tht_index
    window, busy, started = busy_window
    busy["on"] = True
    assert window._set_footprint_text_position("Reference", (1.0, 2.0))
    _drive(monkeypatch, drill=0.3)
    window._edit_package_pad(_tht_index(window))
    _turns(qapp)
    assert started == []
    busy["on"] = False
    window._on_terminal_detail_finished()
    _turns(qapp)
    assert len(started) == 1
    assert started[0].text_positions == {"Reference": (1.0, 2.0)}


def test_any_generation_start_covers_the_pending_rewrite(busy_window, qapp):
    """A generation started by another command writes the same .kicad_mod:
    the pending rewrite is not repeated after it; a refused start keeps it."""
    window, busy, started = busy_window
    busy["on"] = True
    window._apply_footprint_drawings(validate_footprint_drawings([record()]))
    _turns(qapp)
    assert window._generate(footprint_only=True) is False
    assert window._fp_rewrite_dirty, "a refused start keeps the edit pending"
    busy["on"] = False
    assert window._generate(footprint_only=True) is True     # e.g. «Applica metadati»
    assert not window._fp_rewrite_dirty
    window._on_generation_finished()
    _turns(qapp)
    assert len(started) == 1


def test_an_idle_window_rewrites_on_the_next_turn(busy_window, qapp):
    """Positive baseline: nothing running, the rewrite starts without any finish."""
    window, _busy, started = busy_window
    window._apply_footprint_drawings(validate_footprint_drawings([record()]))
    _turns(qapp)
    assert len(started) == 1 and not window._fp_rewrite_dirty


def test_a_start_that_raises_is_attempted_once(window, monkeypatch, qapp):
    """R1: a failed start of the rewrite does not re-arm itself through its own finish."""
    acquire(window)
    _turns(qapp)
    attempts = []

    def boom(worker):
        attempts.append(worker)
        raise RuntimeError("cannot start thread")

    monkeypatch.setattr(gui.GenerateWorker, "start", boom)
    monkeypatch.setattr(window, "_project_has_footprint", lambda: True)
    errors = []
    monkeypatch.setattr(window, "_on_error", lambda message: errors.append(message))
    window._apply_footprint_drawings(validate_footprint_drawings([record()]))
    _turns(qapp, 50)
    assert len(attempts) == 1 and len(errors) == 1
    assert not window._fp_rewrite_dirty
    # Another worker finishing later does not bring it back either.
    window._on_regen_3d_finished()
    _turns(qapp, 10)
    assert len(attempts) == 1


def test_a_later_edit_after_a_failed_start_is_attempted_again(window, monkeypatch, qapp):
    """R1 positive: the attempt is consumed per edit, a new edit gets its own."""
    acquire(window)
    _turns(qapp)
    attempts = []

    def boom(worker):
        attempts.append(worker)
        raise RuntimeError("cannot start thread")

    monkeypatch.setattr(gui.GenerateWorker, "start", boom)
    monkeypatch.setattr(window, "_project_has_footprint", lambda: True)
    monkeypatch.setattr(window, "_on_error", lambda message: None)
    window._apply_footprint_drawings(validate_footprint_drawings([record()]))
    _turns(qapp, 10)
    window._apply_footprint_drawings(validate_footprint_drawings(
        [record(), record("circle", ((0, 0), (1, 0)))]))
    _turns(qapp, 10)
    assert len(attempts) == 2


def test_invalid_parameters_are_logged_once_and_drop_the_flag(busy_window, qapp, monkeypatch):
    """R3b: a validation failure of the rewrite clears the flag; later finishes log nothing."""
    window, _busy, started = busy_window
    logs = []
    original = window._append_log
    monkeypatch.setattr(window, "_append_log",
                        lambda message, *a, **k: (logs.append(str(message)),
                                                  original(message, *a, **k)))

    def refuse(name):
        raise ValueError("nome non valido")

    monkeypatch.setattr(window, "_validate_output_name", refuse)
    window._apply_footprint_drawings(validate_footprint_drawings([record()]))
    _turns(qapp)
    for finished in (window._on_regen_3d_finished, window._on_terminal_detail_finished,
                     window._on_generation_finished):
        finished()
        _turns(qapp)
    assert started == []
    assert len([line for line in logs if "nome non valido" in line]) == 1
    assert not window._fp_rewrite_dirty


@pytest.mark.parametrize("has_file", [False, True])
def test_a_drawing_rewrites_only_an_existing_footprint(busy_window, qapp, monkeypatch, has_file):
    """R3a: drawings and texts never create a .kicad_mod; they update one that exists."""
    window, _busy, started = busy_window
    monkeypatch.setattr(window, "_project_has_footprint", lambda: has_file)
    window._apply_footprint_drawings(validate_footprint_drawings([record()]))
    assert window._set_footprint_text_position("Reference", (1.0, 2.0))
    _turns(qapp)
    assert len(started) == (1 if has_file else 0)
    assert not window._fp_rewrite_dirty


def test_a_pad_edit_without_a_footprint_file_still_rewrites(busy_window, qapp, monkeypatch):
    """R3a: pad edits keep their behaviour (an acquired component is rewritten)."""
    from tests.test_package_pad_dialog import _drive, _tht_index
    window, _busy, started = busy_window
    monkeypatch.setattr(window, "_project_has_footprint", lambda: False)
    _drive(monkeypatch, drill=0.3)
    window._edit_package_pad(_tht_index(window))
    _turns(qapp)
    assert len(started) == 1


def _extraction_pause(window, monkeypatch, qapp, started, order, auto_params):
    """Drive `_on_extract_finished` as a current, finished extraction with a review pause."""
    monkeypatch.setattr(window, "_analysis_result_is_current", lambda: True)
    monkeypatch.setattr(window, "_show_ai_cost", lambda: None)
    window._applying_extraction = False
    window._automatic_generation_active = False

    def pause():
        # The review question is modal: a nested event loop runs here.
        _turns(qapp)
        order.append(("review", len(started)))
        return True

    monkeypatch.setattr(window, "_offer_inventory_review", pause)
    monkeypatch.setattr(window, "_start_automatic_generation",
                        lambda params: order.append(("auto", len(started))))
    window._auto_generation_params = auto_params
    window._on_extract_finished()
    _turns(qapp)


def test_an_extraction_does_not_rewrite_before_its_review(busy_window, qapp, monkeypatch):
    """R2: the pending edit is not written with fresh, unreviewed parameters."""
    window, busy, started = busy_window
    busy["on"] = True
    window._apply_footprint_drawings(validate_footprint_drawings([record()]))
    _turns(qapp)
    assert window._fp_rewrite_dirty and started == []
    busy["on"] = False
    order = []
    _extraction_pause(window, monkeypatch, qapp, started, order, auto_params=object())
    assert order == [("review", 0), ("auto", 0)]
    assert started == [], "no footprint-only worker: the pipeline owns the output"
    assert not window._fp_rewrite_dirty, "the next generation carries the edit"


def test_the_pipeline_window_holds_a_rewrite_until_it_ends(busy_window, qapp):
    """R2: an edit retried while the automatic generation is pending waits for it."""
    window, _busy, started = busy_window
    window._auto_generation_params = object()
    try:
        window._apply_footprint_drawings(validate_footprint_drawings([record()]))
        _turns(qapp)
        assert started == [] and window._fp_rewrite_dirty
    finally:
        window._auto_generation_params = None
    window._on_generation_finished()
    _turns(qapp)
    assert len(started) == 1, "outside the pipeline the same edit is written"


def test_every_generation_worker_carries_drawings_and_texts(busy_window, qapp):
    """R2 premise: the automatic generation writes the pending edit itself."""
    window, _busy, started = busy_window
    drawings = validate_footprint_drawings([record()])
    window.footprint_drawings = drawings
    window.footprint_text_positions = {"Reference": (1.0, 2.0)}
    assert window._generate() is True
    assert started[-1].drawings == drawings
    assert started[-1].text_positions == {"Reference": (1.0, 2.0)}


def test_an_abandoned_extraction_still_retries(busy_window, qapp, monkeypatch):
    """R2 negative: an abandoned run changed no parameter, the edit is written."""
    window, busy, started = busy_window
    busy["on"] = True
    window._apply_footprint_drawings(validate_footprint_drawings([record()]))
    _turns(qapp)
    busy["on"] = False
    monkeypatch.setattr(window, "_analysis_result_is_current", lambda: False)
    window._on_extract_finished()
    _turns(qapp)
    assert len(started) == 1


# ── A2, A4, A5: drags, undo/redo, hover against list changes ─────────────

@pytest.fixture
def preview(window, monkeypatch):
    gens = []
    monkeypatch.setattr(window, "_generate", lambda **kw: gens.append(kw) or True)
    monkeypatch.setattr(window, "_project_has_footprint", lambda: True)
    window._draw_preview()
    window.chk_grid.setChecked(False)
    return window


def _mouse(view, kind, sx, sy, button=Qt.MouseButton.NoButton, buttons=Qt.MouseButton.NoButton):
    pos = view.mapFromScene(QPointF(sx, sy))
    return QMouseEvent(kind, QPointF(pos), QPointF(view.viewport().mapToGlobal(pos)),
                       button, buttons, Qt.KeyboardModifier.NoModifier)


def _drag(view, start, end, between=None):
    """Press at `start`, move to `end`, run `between`, release at `end` (scene px)."""
    left = Qt.MouseButton.LeftButton
    view.mouseMoveEvent(_mouse(view, QEvent.Type.MouseMove, *start))
    view.mousePressEvent(_mouse(view, QEvent.Type.MouseButtonPress, *start, left, left))
    view.mouseMoveEvent(_mouse(view, QEvent.Type.MouseMove, *end, Qt.MouseButton.NoButton, left))
    if between is not None:
        between()
    view.mouseReleaseEvent(_mouse(view, QEvent.Type.MouseButtonRelease, *end, left,
                                  Qt.MouseButton.NoButton))


def _shown(window):
    view = window.preview_view
    view.resize(800, 600)
    window._fit_preview(force=True)
    return view


def test_a_plain_handle_drag_still_moves_the_point(preview):
    window = preview
    window._apply_footprint_drawings(validate_footprint_drawings(
        [record(points=((0, 0), (5, 0))), record(points=((0, 3), (5, 3)))]))
    view = _shown(window)
    _drag(view, (5 * 20, 0), (6 * 20, 0))
    assert window.footprint_drawings[0]["points"] == [[0.0, 0.0], [6.0, 0.0]]


@pytest.mark.parametrize("edit", ["delete", "undo"])
def test_a_list_change_during_a_drag_cancels_its_drop(preview, edit):
    window = preview
    first = validate_footprint_drawings([record(points=((0, 0), (5, 0)))])
    both = validate_footprint_drawings([record(points=((0, 0), (5, 0))),
                                        record(points=((0, 3), (5, 3)))])
    window._apply_footprint_drawings(first)
    window._apply_footprint_drawings(both)
    view = _shown(window)
    after = {}

    def change():
        if edit == "delete":
            window._fp_hovered_drawing = 0
            window._delete_hovered_drawing()
        else:
            window._undo_footprint_drawing()
        after["list"] = [dict(r, points=[list(p) for p in r["points"]])
                         for r in window.footprint_drawings]
        after["redo"] = len(window._fp_drawings_redo)
    # Grab the end point of drawing 0 and drop it elsewhere.
    _drag(view, (5 * 20, 0), (7 * 20, 2 * 20), between=change)
    assert window.footprint_drawings == after["list"], "the stale drop edited nothing"
    assert len(window._fp_drawings_redo) == after["redo"], "redo stack untouched by the drop"
    assert view._handle_drag is None and not window._fp_handle_active


def test_a_failed_save_keeps_the_undo_and_redo_steps(preview, monkeypatch):
    window = preview
    window._apply_footprint_drawings(validate_footprint_drawings([record()]))
    window._apply_footprint_drawings([])
    assert len(window._fp_drawings_undo) == 2
    monkeypatch.setattr(gui.QMessageBox, "warning", lambda *a, **k: None)
    real_save = window._save_project_config
    monkeypatch.setattr(window, "_save_project_config", lambda force=False: False)
    window._undo_footprint_drawing()
    assert len(window._fp_drawings_undo) == 2 and window.footprint_drawings == []
    # Positive: once saving works, the same undo applies and pops one step.
    monkeypatch.setattr(window, "_save_project_config", real_save)
    window._undo_footprint_drawing()
    assert len(window._fp_drawings_undo) == 1 and len(window.footprint_drawings) == 1
    assert len(window._fp_drawings_redo) == 1
    monkeypatch.setattr(window, "_save_project_config", lambda force=False: False)
    window._redo_footprint_drawing()
    assert len(window._fp_drawings_redo) == 1, "a failed redo keeps its step"


def test_the_handles_come_back_on_a_moved_drawing(preview):
    window = preview
    window._apply_footprint_drawings(validate_footprint_drawings(
        [record("rectangle", ((0, 0), (4, 3)))]))
    view = _shown(window)
    _drag(view, (80, 16), (100, 16))           # right edge, 4 mm -> 5 mm
    assert window.footprint_drawings[0]["points"][0] == [1.0, 0.0]
    assert view.hovered_drawing() == -1, "the view forgot the old hover"
    view.mouseMoveEvent(_mouse(view, QEvent.Type.MouseMove, 100, 22))
    assert window._fp_hovered_drawing == 0 and view.hovered_drawing() == 0
    # Negative: away from any drawing, nothing is hovered.
    view.mouseMoveEvent(_mouse(view, QEvent.Type.MouseMove, 400, 400))
    assert window._fp_hovered_drawing == -1


# ── A6: texts snap on the same grid as the drawings ──────────────────────

def test_a_dropped_text_lands_on_the_drawing_grid_node(window, monkeypatch):
    acquire(window)
    monkeypatch.setattr(window, "_generate", lambda **kw: True)
    window.chk_grid.setChecked(True)
    window.combo_grid_unit.setCurrentIndex(window.combo_grid_unit.findData("inch"))
    window._draw_preview()
    view = window.preview_view
    x, y = 0.37 * 20, 3.11 * 20
    sx, sy = view.snap_to_grid(x, y, 2.0)
    drawing_node = (round(sx / 20, 4), round(sy / 20, 4))
    window._on_footprint_text_moved("Reference", x, y)
    assert window.footprint_text_positions["Reference"] == drawing_node
    # The case discriminates: the grid does not pass through the scene
    # origin, so a snap from the origin would give another node.
    step = view.fine_step() / 20
    from_origin = tuple(round(round(v / 20 / step) * step, 4) for v in (x, y))
    assert from_origin != drawing_node


# ── F1: list-valued keys of a hand-edited project file ───────────────────

def _open(window, monkeypatch, name):
    monkeypatch.setattr(OpenProjectDialog, "selected_name", lambda self: name)
    monkeypatch.setattr(OpenProjectDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    window._open_project()


@pytest.mark.parametrize("state, label", [
    ({"sides": 5}, "sides"),
    ({"components": 5}, "components"),
    ({"annotations": {"fillets": 5}}, "annotations.fillets"),
    ({"annotations": {"texts": "x"}}, "annotations.texts"),
    ({"pinout": 7}, "pinout"),
])
def test_a_non_list_value_is_skipped_and_logged(window, monkeypatch, state, label):
    monkeypatch.setattr(gui, "save_config", lambda c: None)
    root = window._library_root()
    write_project_config(root / "P", dict(state, project_kind="board"))
    (root / "P" / "docs").mkdir()
    _open(window, monkeypatch, "P")
    assert window._component_name() == "P"
    assert any(label in line for line in window._log_lines), window._log_lines[-5:]


def test_a_list_value_is_still_applied(window, monkeypatch):
    monkeypatch.setattr(gui, "save_config", lambda c: None)
    root = window._library_root()
    pinout = [{"pin": "1", "name": "VCC"}]
    write_project_config(root / "P", {"project_kind": "board", "pinout": pinout})
    (root / "P" / "docs").mkdir()
    _open(window, monkeypatch, "P")
    assert window.pinout == pinout
    assert not any("pinout (" in line for line in window._log_lines)


def test_non_finite_numbers_are_dropped_and_the_state_stays_serialisable(window, monkeypatch):
    monkeypatch.setattr(gui, "save_config", lambda c: None)
    root = window._library_root()
    (root / "P" / "docs").mkdir(parents=True)
    project_config_path(root / "P").write_text(
        '{"format": "kicadgen-project", "version": 1, "state": {"project_kind": "board", '
        '"zzz_unknown": NaN, "widgets": {"spin_pitch": Infinity}}}', encoding="utf-8")
    dropped = []
    state = read_project_config(root / "P", dropped)
    assert sorted(dropped) == ["widgets.spin_pitch", "zzz_unknown"]
    assert state["zzz_unknown"] is None
    _open(window, monkeypatch, "P")
    assert window._component_name() == "P"
    assert window._project_state_text() is not None
    assert any("zzz_unknown" in line for line in window._log_lines)


def test_finite_numbers_are_read_unchanged():
    import tempfile
    with tempfile.TemporaryDirectory() as folder:
        write_project_config(folder, {"widgets": {"spin_pitch": 2.54}})
        dropped = []
        assert read_project_config(folder, dropped)["widgets"]["spin_pitch"] == 2.54
        assert dropped == []


# ── F8: an unserialisable state does not trap the user ───────────────────

def _open_plain(window, monkeypatch):
    monkeypatch.setattr(gui, "save_config", lambda c: None)
    root = window._library_root()
    write_project_config(root / "P", {"project_kind": "board"})
    (root / "P" / "docs").mkdir()
    _open(window, monkeypatch, "P")


@pytest.mark.parametrize("answer, leaves", [(QMessageBox.StandardButton.Yes, True),
                                            (QMessageBox.StandardButton.No, False)])
def test_an_unserialisable_state_offers_to_abandon(window, monkeypatch, answer, leaves):
    _open_plain(window, monkeypatch)
    window._declared_row_spacing = float("nan")
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or answer)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: pytest.fail("warning shown"))
    try:
        assert window._can_leave_project() is leaves
        assert len(asked) == 1
    finally:
        window._declared_row_spacing = None


def test_a_saved_state_leaves_without_asking(window, monkeypatch):
    _open_plain(window, monkeypatch)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: pytest.fail("asked"))
    assert window._can_leave_project() is True


# ── F4/R8: the working folder field commits on Enter only ───────────────

def _open_typed(window, monkeypatch):
    _open_plain(window, monkeypatch)
    field = window.field_library_root
    field.setFocus()
    field.end(False)
    return field


def test_typing_in_the_working_folder_does_not_close_the_project(window, monkeypatch):
    field = _open_typed(window, monkeypatch)
    QTest.keyClick(field, Qt.Key.Key_Backspace)
    QTest.keyClick(field, Qt.Key.Key_X)
    assert window._component_name() == "P", "a keystroke is not a decision"


def test_confirming_another_folder_closes_the_project(window, monkeypatch, tmp_path):
    field = _open_typed(window, monkeypatch)
    QTest.keyClicks(field, "-altrove")
    assert window._component_name() == "P"
    QTest.keyClick(field, Qt.Key.Key_Return)
    assert window._component_name() == ""


def test_confirming_the_same_folder_keeps_the_project(window, monkeypatch):
    field = _open_typed(window, monkeypatch)
    QTest.keyClicks(field, os.sep)          # same folder, written with a trailing separator
    QTest.keyClick(field, Qt.Key.Key_Return)
    assert window._component_name() == "P"


def test_leaving_the_field_without_enter_restores_the_folder(window, monkeypatch):
    """R8 negative: focus out (Alt+Tab, a click on «Sfoglia») is not a confirmation."""
    field = _open_typed(window, monkeypatch)
    committed = field.text()
    QTest.keyClicks(field, "-mezzo")
    assert field.isModified()
    field.editingFinished.emit()            # what Qt emits on focus out
    assert window._component_name() == "P", "a half-typed path does not close the project"
    assert field.text() == committed and not field.isModified()
    # Enter afterwards confirms nothing new: the folder is the same one.
    QTest.keyClick(field, Qt.Key.Key_Return)
    assert window._component_name() == "P"


def test_leaving_an_untouched_field_changes_nothing(window, monkeypatch):
    """R8 positive baseline: no typed text, focus out keeps the text as it is."""
    field = _open_typed(window, monkeypatch)
    before = field.text()
    field.editingFinished.emit()
    assert field.text() == before and window._component_name() == "P"


def test_the_browse_button_commits_the_chosen_folder(window, monkeypatch, tmp_path):
    _open_plain(window, monkeypatch)
    monkeypatch.setattr(gui.QFileDialog, "getExistingDirectory",
                        lambda *a, **k: str(tmp_path / "scelta"))
    window._browse_library_root()
    assert window._component_name() == ""


# ── F5: the full-paths toggle goes through the catalogue ─────────────────

def test_the_full_paths_toggle_is_translated(window, monkeypatch):
    monkeypatch.setattr(window.i18n, "text", lambda source, *a, **k: f"<{source}>")
    window._toggle_kicad_file_paths(True)
    assert window.file_paths_toggle.text() == "<Nascondi percorsi completi>"
    window._toggle_kicad_file_paths(False)
    assert window.file_paths_toggle.text() == "<Mostra percorsi completi>"


# ── F2, F3: config.json values and corrupt files ─────────────────────────

@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setenv("KICADGEN_CONFIG_PATH", str(path))
    return path


def test_a_value_of_the_wrong_kind_falls_back_to_its_default(config_file):
    config_file.write_text(json.dumps({
        "language": ["it"], "show_grid": "no", "include_nc_pins": 1,
        "grid_unit": "inch", "draw_grid_divisor": 5, "recent_projects": ["A"]}), encoding="utf-8")
    report = []
    config = app_config.load_config(report)
    defaults = app_config.DEFAULTS
    assert config["language"] == defaults["language"]
    assert config["show_grid"] is defaults["show_grid"]
    assert config["include_nc_pins"] is defaults["include_nc_pins"], "1 is not a boolean"
    # Negative: values of the right kind, and keys without a default, are kept.
    assert config["grid_unit"] == "inch"
    assert config["draw_grid_divisor"] == 5 and config["recent_projects"] == ["A"]
    assert sorted(values["key"] for _template, values in report) == [
        "include_nc_pins", "language", "show_grid"]
    assert app_config.take_load_problems() == report
    assert app_config.take_load_problems() == [], "taken once"


def test_a_config_with_a_bom_is_read(config_file):
    config_file.write_bytes(b"\xef\xbb\xbf" + json.dumps({"language": "fr"}).encode())
    report = []
    assert app_config.load_config(report)["language"] == "fr"
    assert report == []
    assert [p.name for p in config_file.parent.iterdir()] == ["config.json"]


def test_a_corrupt_config_is_kept_aside_before_the_next_save(config_file):
    original = b'{"language": "de", "show_grid": false,}\n'
    config_file.write_bytes(original)
    report = []
    config = app_config.load_config(report)
    assert config["language"] == app_config.DEFAULTS["language"], "defaults, as intended"
    copies = sorted(config_file.parent.glob("config.json.corrupt-*"))
    assert len(copies) == 1 and copies[0].read_bytes() == original
    assert report and str(copies[0]) in str(report[0][1]["backup"])
    app_config.load_config()                       # read again: no second copy
    assert len(list(config_file.parent.glob("config.json.corrupt-*"))) == 1
    assert app_config.save_config(config) is True
    assert copies[0].read_bytes() == original, "the save does not touch the copy"


def test_a_valid_config_makes_no_copy(config_file):
    config_file.write_text(json.dumps({"language": "en"}), encoding="utf-8")
    assert app_config.load_config()["language"] == "en"
    assert [p.name for p in config_file.parent.iterdir()] == ["config.json"]


def test_the_window_logs_the_config_problems(qapp, monkeypatch, tmp_path, config_file):
    pytest.importorskip("pyvistaqt")
    import qt_fixtures
    config_file.write_text(json.dumps({"show_grid": "no"}), encoding="utf-8")
    monkeypatch.setattr(gui, "Preview3DVtk", qt_fixtures.PreviewStub)
    monkeypatch.setattr(gui, "DEFAULT_LIBRARY_ROOT", tmp_path)
    window = gui.MainWindow()
    try:
        assert any("show_grid" in line for line in window._log_lines)
        assert window.chk_grid.isChecked() is app_config.DEFAULTS["show_grid"]
    finally:
        qt_fixtures.shutdown_main_window(window, qapp)


# ── F7: crash report scrubbing ───────────────────────────────────────────

@pytest.mark.parametrize("text, kept, gone", [
    ("open C:\\Users\\Mario Rossi\\Desktop\\x.pdf failed",
     "C:\\Users\\<user>\\Desktop\\x.pdf failed", "Rossi"),
    ("'C:\\\\Users\\\\Mario Rossi\\\\AppData'", "C:\\\\Users\\\\<user>\\\\AppData", "Rossi"),
    ("at /home/jane doe/src", "/home/<user>/src", "jane"),
    ("\\\\fileserver\\homes\\Mario Rossi\\docs\\a.txt",
     "\\\\fileserver\\homes\\<user>\\docs\\a.txt", "Rossi"),
    ("'\\\\\\\\fileserver\\\\homes\\\\mario\\\\x'",
     "\\\\\\\\fileserver\\\\homes\\\\<user>\\\\x", "mario"),
])
def test_user_segments_are_masked_whole(text, kept, gone):
    scrubbed = cr.scrub(text)
    assert kept in scrubbed and gone not in scrubbed


@pytest.mark.parametrize("text", [
    "C:\\Program Files\\KiCad\\bin",
    "'C:\\\\Windows\\\\System32\\\\drivers'",
    "see https://example.org/share/page and D:\\Lib\\Parts",
])
def test_ordinary_paths_are_untouched(text):
    assert cr.scrub(text) == text


B = "\\"


@pytest.mark.parametrize("text, expected", [
    # R6a: a spaced name is taken only when a separator follows it.
    ("Saved to /home/bob and then opened the viewer",
     "Saved to /home/<user> and then opened the viewer"),
    ("cwd=C:" + B + "Users" + B + "bob (portable) mode=on",
     "cwd=C:" + B + "Users" + B + "<user> (portable) mode=on"),
    ("/Users/alice is missing; retry in 5 s", "/Users/<user> is missing; retry in 5 s"),
    ("Saved to /home/bob and then opened /tmp/x", "Saved to /home/<user> and then opened /tmp/x"),
    ("File \"C:" + B + "Users" + B + "Mario Rossi" + B + "app" + B + "x.py\", line 3",
     "File \"C:" + B + "Users" + B + "<user>" + B + "app" + B + "x.py\", line 3"),
])
def test_a_user_name_with_spaces_needs_a_following_separator(text, expected):
    assert cr.scrub(text) == expected


@pytest.mark.parametrize("text", [
    # R6b: doubled backslashes of source lines are not UNC paths.
    '    pattern = re.compile(r"' + B * 2 + "d+" + B * 2 + "s+" + B * 2 + 'w+")',
    '    text.split("' + B * 2 + "n" + B * 2 + "t" + B * 2 + 'x")',
    "regex (?<=x)" + B * 2 + "b" + B * 2 + "(a b)" + B * 2 + "c done",
    # R6c: UNC paths that do not name a person.
    B * 2 + "fileserver" + B + "Projects" + B + "KiCad" + B + "board.kicad_pcb",
    B * 2 + "." + B + "pipe" + B + "kicad-ipc",
    B * 2 + "?" + B + "C:" + B + "Windows" + B + "x.dll",
    # A drive path as repr() writes it (every separator doubled). The mixed
    # 2/1 form is a UNC shape and is masked since 2026-10-14 (see
    # tests/test_gui_residual_fixes.py).
    "x=C:" + B * 2 + "srv" + B * 2 + "home" + B * 2 + "mario",
])
def test_non_user_unc_and_escapes_are_untouched(text):
    assert cr.scrub(text) == text


@pytest.mark.parametrize("text, expected", [
    (B * 2 + "srv" + B + "profiles" + B + "mario" + B + "x",
     B * 2 + "srv" + B + "profiles" + B + "<user>" + B + "x"),
    ("open(" + B * 2 + "srv" + B + "Users" + B + "anna",
     "open(" + B * 2 + "srv" + B + "Users" + B + "<user>"),
    ("path=" + B * 2 + "nas.local" + B + "home" + B + "Mario Rossi" + B + "f.txt",
     "path=" + B * 2 + "nas.local" + B + "home" + B + "<user>" + B + "f.txt"),
])
def test_user_unc_shares_are_masked(text, expected):
    assert cr.scrub(text) == expected


# ── F9a: durable writes ──────────────────────────────────────────────────

def test_atomic_write_fsyncs_before_replacing(tmp_path, monkeypatch):
    calls = []
    real_fsync, real_replace = os.fsync, fsutil.replace_file
    monkeypatch.setattr(os, "fsync", lambda fd: calls.append("fsync") or real_fsync(fd))
    monkeypatch.setattr(fsutil, "replace_file",
                        lambda s, t, **k: calls.append("replace") or real_replace(s, t))
    target = tmp_path / "f.json"
    fsutil.atomic_write_bytes(target, b"{}")
    assert target.read_bytes() == b"{}" and calls == ["fsync", "replace"]
    calls.clear()
    write_project_config(tmp_path / "P", {"a": 1})
    assert calls == ["fsync", "replace"], "the project file is fsynced too"


def test_a_refused_replace_keeps_the_old_file_and_no_temporary(tmp_path, monkeypatch):
    target = tmp_path / "f.json"
    target.write_bytes(b"old")

    def refuse(*_a, **_k):
        raise PermissionError("locked")
    monkeypatch.setattr(fsutil, "replace_file", refuse)
    with pytest.raises(PermissionError):
        fsutil.atomic_write_bytes(target, b"new")
    assert target.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["f.json"]


# ── D2: malformed extracted fillets cost only themselves ─────────────────

def test_malformed_fillets_are_skipped_and_the_valid_one_applied(window):
    window._freecad_available = True
    params = extracted_package()
    params.edge_fillets = [{"edge": "top-front", "radius_mm": 0.4}, "junk",
                           {"edge": "nowhere", "radius_mm": 1}, {"radius_mm": 1},
                           {"edge": "top-back", "radius_mm": "abc"},
                           {"edge": "top-left", "radius_mm": math.nan}]
    window._on_extracted(params)
    assert [(a.edge, a.radius_mm) for a in window.fillet_annotations] == [("top-front", 0.4)]
    assert window.package_geometry is not None, "the rest of the extraction was applied"


def test_well_formed_fillets_are_all_applied(window):
    window._freecad_available = True
    params = extracted_package()
    params.edge_fillets = [{"edge": "top-front", "radius_mm": 0.4},
                           {"edge": "top-back", "radius_mm": 0.2}]
    window._on_extracted(params)
    assert [a.edge for a in window.fillet_annotations] == ["top-front", "top-back"]


# ── R5: the refused-arc toast names the real reason ─────────────────────

def test_an_aligned_arc_gets_its_own_explanation(window, monkeypatch):
    """Positive: three distinct, collinear points are explained as an aligned arc."""
    monkeypatch.setattr(window.i18n, "text", lambda source, *a, **k: source)
    aligned = {"kind": "arc", "points": [[0, 0], [1, 0], [2, 0]]}
    assert window._degenerate_drawing_text(aligned).startswith("Arco senza centro")


@pytest.mark.parametrize("record_", [
    {"kind": "arc", "points": [[0, 0], [0, 0], [2, 0]]},       # repeated point
    {"kind": "rectangle", "points": [[0, 0], [0, 2]]},         # zero side
])
def test_other_degenerate_shapes_keep_the_generic_text(window, monkeypatch, record_):
    """Negative: not an aligned arc, the zero-radius/zero-side text stays."""
    monkeypatch.setattr(window.i18n, "text", lambda source, *a, **k: source)
    assert window._degenerate_drawing_text(record_).startswith("La forma è degenere")


# ── Final review (cycle 4, 2026-10-14) ───────────────────────────────

def test_a_half_typed_working_folder_is_not_where_output_goes(window, monkeypatch, qapp):
    """Positive: while «Area di lavoro» holds typed, unconfirmed text, the
    project folder stays the open one, so a rewrite retried by a finishing
    worker writes into the open project, not into a homonymous project of
    the typed folder. Negative: after Enter the typed folder is the folder."""
    _open_plain(window, monkeypatch)
    acquire(window)
    _turns(qapp)
    opened = window._open_project_folder
    started = []
    monkeypatch.setattr(gui.GenerateWorker, "start", lambda w: started.append(w))
    monkeypatch.setattr(window, "_project_has_footprint", lambda: True)
    busy = {"on": True}
    monkeypatch.setattr(window, "_background_work_running", lambda: busy["on"])
    window._apply_footprint_drawings(validate_footprint_drawings([record()]))
    _turns(qapp)
    field = window.field_library_root
    field.setFocus()
    field.end(False)
    QTest.keyClicks(field, "-ALTROVE")          # typed, not confirmed
    assert field.isModified()
    assert window._project_dir() == opened, "typed text is not the folder in use"
    busy["on"] = False
    window._on_regen_3d_finished()              # a worker finishes meanwhile
    _turns(qapp)
    assert started and all(Path(w.project_dir) == opened for w in started), \
        [str(w.project_dir) for w in started]
    # Negative: once confirmed with Enter, the typed folder is the working folder.
    monkeypatch.setattr(window, "_save_project_config", lambda force=False: True)
    QTest.keyClick(field, Qt.Key.Key_Return)
    assert not field.isModified()
    assert window._library_root() == Path(field.text().strip())
    assert str(window._library_root()).endswith("-ALTROVE")


def test_the_symbol_error_path_still_binds_the_recovery_file(window, monkeypatch):
    """Positive: when the footprint pads cannot be read, the symbol tab
    still gets its recovery file, so a carried draft stays durable and
    offerable. Negative: the normal path binds the same file."""
    _open_plain(window, monkeypatch)
    expected = window._project_dir() / ".symbol-draft.json"

    def broken():
        raise ValueError("pads unreadable")
    monkeypatch.setattr(window, "_symbol_pad_ids", broken)
    window.symbol_tab._recovery_path = None
    window._refresh_symbol_tab(force=True)
    assert Path(window.symbol_tab._recovery_path) == expected
    monkeypatch.setattr(window, "_symbol_pad_ids", lambda: [])
    window.symbol_tab._recovery_path = None
    window._refresh_symbol_tab(force=True)
    assert Path(window.symbol_tab._recovery_path) == expected
