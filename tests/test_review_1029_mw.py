"""@brief Fixes of the 2026-10-29 review in main_window.py and component_state.py (scope MW).

Each finding has a positive test (the defect scenario now behaves) and a
negative twin (the case the fix must not change), told apart by structure,
not by a number that happens to fall on one side.
"""
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("pyvistaqt")

from PyQt6.QtCore import QEvent, QPoint, Qt                        # noqa: E402
from PyQt6.QtGui import QKeySequence                               # noqa: E402
from PyQt6.QtTest import QTest                                     # noqa: E402
from PyQt6.QtWidgets import (QApplication, QDialog, QGraphicsEllipseItem,  # noqa: E402
                             QMenu, QMessageBox)

from kicadgen.core.footprint import validate_footprint_drawings  # noqa: E402
from kicadgen.gui import main_window as gui                      # noqa: E402
from kicadgen.gui.component_state import normalised_side         # noqa: E402

from test_footprint_gesture_parity import (                     # noqa: E402,F401
    PX, _move, _press, _release, _vp, _with_drawing, fp,
)


def _drain(app=None):
    """Deliver posted events, deferred deletions included."""
    app = app or QApplication.instance()
    for _ in range(3):
        app.processEvents()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)


# ── A1: a damaged castellated side never escapes the project opening ──────

def _project(root, name, sides):
    folder = root / name
    (folder / "docs").mkdir(parents=True)
    state = {"project_kind": "board_module", "widgets": {}, "sides": sides}
    (folder / "project_config.json").write_text(json.dumps(
        {"format": "kicadgen-project", "version": 1, "state": state}, allow_nan=True),
        encoding="utf-8")
    return folder


@pytest.mark.parametrize("damage", [
    {"pitch_mm": float("nan")},     # NaN: read_project_config turns it into None
    {"count": None},                # null written by hand
    {"count": "quattro"},           # a word written by hand
    {"first_pin": True},            # a boolean is not a pin number
])
def test_a1_a_damaged_side_is_skipped_with_a_note_and_the_others_load(window, damage):
    good = {"edge": "right", "count": 6, "pitch_mm": 1.27, "first_pin": 7,
            "contact_style": "half_hole"}
    bad = dict({"edge": "left", "count": 4, "pitch_mm": 2.54}, **damage)
    logged = []
    window._append_log = lambda text, *a, **k: logged.append(str(text))
    window._open_project_at(_project(window._library_root(), "Danneggiato", [bad, good]))
    assert [(s["edge"], s["count"], s["pitch_mm"]) for s in window._sides] == [("right", 6, 1.27)]
    assert any("sides[0]" in line for line in logged), logged


def test_a1_a_valid_side_and_a_side_without_optional_keys_load_as_before(window):
    """Negative twin: an absent key still takes the table default; nothing is skipped."""
    sides = [{"edge": "left", "count": 3, "pitch_mm": 2.0, "first_pin": 1},
             {"edge": "right"}]
    window._open_project_at(_project(window._library_root(), "Sano", sides))
    assert [(s["edge"], s["count"], s["pitch_mm"], s["first_pin"]) for s in window._sides] == [
        ("left", 3, 2.0, 1), ("right", 2, 2.54, 1)]


def test_a1_normalised_side_refuses_non_finite_numbers_and_keeps_defaults_for_absent_keys():
    with pytest.raises(ValueError):
        normalised_side({"pitch_mm": float("nan")})
    with pytest.raises(ValueError):
        normalised_side({"offset_mm": "inf"})
    with pytest.raises(TypeError):
        normalised_side({"count": None})
    side = normalised_side({})
    assert (side["count"], side["pitch_mm"], side["offset_mm"], side["first_pin"]) == (2, 2.54, 0.0, 1)
    assert normalised_side({"count": "5", "pitch_mm": 20000})["count"] == 5      # numeric text, clamped


# ── A2: the key gate covers the session vision model ─────────────────────

def test_a2_a_deepseek_session_vision_model_without_its_key_stops_the_run(window, monkeypatch):
    from kicadgen.services import providers
    window._vlm_model = providers.DEEPSEEK_PREFIX + "deepseek-chat"
    asked = []

    def missing():
        asked.append(1)
        raise providers.ProviderKeyMissing("no deepseek key")
    monkeypatch.setattr(providers, "deepseek_api_key", missing)
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a))
    assert window._photo_detector_model() in window._run_models()
    assert window._ensure_api_key() is False
    assert asked and warned


def test_a2_without_a_session_vision_model_the_set_is_the_extraction_model(window):
    """Negative twin: the photo model then IS the extraction model, as before."""
    window._vlm_model = None
    models = window._run_models()
    assert models[:2] == (window._effective_model(), window._effective_model())


# ── A3: the review dialogs are released after their answer ───────────────

def _png():
    from PyQt6.QtCore import QBuffer, QIODevice
    from PyQt6.QtGui import QColor, QImage
    image = QImage(64, 32, QImage.Format.Format_RGB32)
    image.fill(QColor("green"))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def test_a3_the_photo_comparison_dialog_is_not_kept_after_closing(window, qapp, monkeypatch):
    from kicadgen.gui import photo_review_dialog as review_module
    window._photo_png = _png()
    window._board_size_known = True
    window._set_photo_components([{"name": "U1", "kind": "ic", "x_mm": 1.0, "y_mm": 1.0,
                                   "length_mm": 2.0, "width_mm": 2.0, "height_mm": 1.0,
                                   "_id": 1, "_box": [0.1, 0.1, 0.4, 0.4]}], replace_excluded=True)
    monkeypatch.setattr(review_module.PhotoReviewDialog, "exec",
                        lambda self: QDialog.DialogCode.Rejected)
    before = len(window.findChildren(review_module.PhotoReviewDialog))
    for _ in range(3):
        window._show_photo_comparison()
        _drain(qapp)
    assert len(window.findChildren(review_module.PhotoReviewDialog)) == before


def test_a3_the_reopened_detector_review_still_hands_over_its_kept_bodies(window, qapp, monkeypatch):
    """Negative twin: the answer is read before the deferred release."""
    from kicadgen.gui import photo_review_dialog as review_module
    from kicadgen.services import photo_inventory
    project = window._project_dir()
    project.mkdir(parents=True, exist_ok=True)
    body = {"_id": 1, "_box": [0.1, 0.1, 0.4, 0.4], "x_mm": 1, "y_mm": 1, "length_mm": 1,
            "width_mm": 1, "height_mm": 1, "name": "x", "kind": "generic"}
    photo_inventory.save_review_bundle(project, [body], _png(), 10.0, 10.0)
    window._photo_png = None
    window._refresh_reopen_review_button()
    monkeypatch.setattr(review_module.PhotoReviewDialog, "exec",
                        lambda self: QDialog.DialogCode.Accepted)
    written = []
    monkeypatch.setattr(window, "_write_review_bundle",
                        lambda kept, *a, **k: written.append(list(kept)) or None)
    before = len(window.findChildren(review_module.PhotoReviewDialog))
    window._reopen_detector_review()
    _drain(qapp)
    assert len(written) == 1 and len(written[0]) == 1
    assert len(window.findChildren(review_module.PhotoReviewDialog)) == before


# ── A4: typing in «Area di lavoro» is not a decision ─────────────────────

def _count_views(window, monkeypatch):
    calls = []
    original = window._update_project_path_label
    monkeypatch.setattr(window, "_update_project_path_label",
                        lambda: calls.append(1) or original())
    return calls


def test_a4_keystrokes_refresh_nothing_and_enter_refreshes_once(window, monkeypatch, tmp_path):
    calls = _count_views(window, monkeypatch)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    field = window.field_library_root
    field.setFocus()
    QTest.keyClicks(field, "abc")
    assert field.isModified() and calls == []
    other = tmp_path / "altro"
    other.mkdir()
    field.setText(str(other))
    calls.clear()
    field.setModified(True)                  # as if typed: the text waits for Enter
    window._on_library_root_edit_finished()
    assert calls, "Enter commits the folder and refreshes its views"
    assert window._library_root() == other
    assert Path(window.start_page.library_root_text()) == other \
        if hasattr(window.start_page, "library_root_text") else True


def test_a4_a_folder_set_by_the_program_refreshes_at_once(window, monkeypatch, tmp_path):
    """Negative twin: a text that is not being typed is a decision and refreshes immediately."""
    calls = _count_views(window, monkeypatch)
    refreshed = []
    monkeypatch.setattr(window.ai_account, "refresh", lambda: refreshed.append(1))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    other = tmp_path / "scelta"
    other.mkdir()
    window.field_library_root.setText(str(other))
    assert calls and refreshed


def test_a4_the_document_list_does_not_retranslate_the_guided_box_itself(window, monkeypatch):
    """The guided box translates what it rebuilt in its own `refresh`: once per refresh, not twice."""
    seen = []
    original = window.i18n.retranslate_widgets
    monkeypatch.setattr(window.i18n, "retranslate_widgets",
                        lambda widget, *a, **k: seen.append(widget) or original(widget, *a, **k))
    window._refresh_docs_list()
    assert seen.count(window.guided_docs) <= 1


# ── A5: a regeneration left pending by an abandoned run is retried ───────

@pytest.mark.parametrize("finished", ["_on_extract_finished", "_on_yolo_pretest_finished"])
def test_a5_the_late_end_of_an_abandoned_run_retries_a_pending_regeneration(window, qapp,
                                                                            monkeypatch, finished):
    runs = []
    monkeypatch.setattr(window, "_analysis_result_is_current", lambda: False)
    monkeypatch.setattr(window, "_run_pending_regen_3d", lambda: runs.append(1))
    window._regen_3d_pending = True
    getattr(window, finished)()
    qapp.processEvents()
    assert runs == [1]


def test_a5_nothing_is_scheduled_without_a_pending_regeneration(window, qapp, monkeypatch):
    runs = []
    monkeypatch.setattr(window, "_analysis_result_is_current", lambda: False)
    monkeypatch.setattr(window, "_run_pending_regen_3d", lambda: runs.append(1))
    window._regen_3d_pending = False
    window._on_extract_finished()
    qapp.processEvents()
    assert runs == []


# ── A6: the YOLO pretest shows a running text on the button ──────────────

class _FakePretest(gui.QThread):
    done = gui.pyqtSignal(object)
    error = gui.pyqtSignal(str)
    progress = gui.pyqtSignal(str)
    phase = gui.pyqtSignal(object)
    review_requested = gui.pyqtSignal(object)

    def __init__(self, *args, **kwargs):
        super().__init__()

    def start(self, *args):
        pass


def test_a6_the_pretest_button_says_the_pretest_is_running(window, monkeypatch, tmp_path):
    monkeypatch.setattr(gui, "YoloPretestWorker", _FakePretest)
    monkeypatch.setattr(window, "_body_size", lambda: (10.0, 10.0))
    monkeypatch.setattr(window, "_pretest_top_views", lambda *a: [tmp_path / "top.png"])
    window._set_extract_button_text("Preparazione…")
    window._start_yolo_pretest([tmp_path / "top.png"], None, None)
    assert window.btn_extract_ai.property("i18n_source") == "Pretest YOLO in corso"
    window._extract_worker = None
    window._extraction_busy = False


def test_a6_a_paid_run_keeps_its_own_running_text(window):
    """Negative twin: the paid analysis still shows «Analisi in corso...»."""
    window._begin_ai_run("Prova")
    assert window.btn_extract_ai.property("i18n_source") == "Analisi in corso..."
    window._extraction_busy = False


# ── A7: the dead rename helper is gone ───────────────────────────────────

def test_a7_the_unused_folder_move_helper_was_removed():
    assert not hasattr(gui.MainWindow, "_move_analysis_folders_to")
    # Negative twin: the diary move used by the rename is still there.
    assert hasattr(gui.MainWindow, "_move_log_to")


# ── A8: opening a project refreshes the symbol tab once ──────────────────

def test_a8_opening_with_the_symbol_tab_in_front_refreshes_it_once(window, monkeypatch):
    folder = _project(window._library_root(), "Simbolo", [])
    window.tabs.setCurrentIndex(gui.TAB_SYMBOL)
    forced = []
    original = window._refresh_symbol_tab
    monkeypatch.setattr(window, "_refresh_symbol_tab",
                        lambda force=False: forced.append(force) or original(force=force))
    window._open_project_at(folder)
    assert forced.count(True) == 1


def test_a8_opening_with_the_preview_in_front_still_draws_it(window, monkeypatch):
    """Negative twin: the tab in front other than the symbol is still redrawn."""
    folder = _project(window._library_root(), "Anteprima", [])
    window._set_acquisition_tabs_enabled(True)
    window.tabs.setCurrentIndex(gui.TAB_PREVIEW)
    drawn = []
    original = window._draw_preview
    monkeypatch.setattr(window, "_draw_preview", lambda: drawn.append(1) or original())
    window._open_project_at(folder)
    if window.tabs.currentIndex() == gui.TAB_PREVIEW:
        assert drawn


# ── B1: the 2D preview draws the fields, not the last generation ─────────

def _hole_diameters(window):
    out = []
    for item in window.preview_scene.items():
        if isinstance(item, QGraphicsEllipseItem) and item.zValue() == 1 and item.data(0x504144) is None:
            out.append(round(item.rect().width() / 20.0, 3))
    return sorted(set(out))


def test_b1_a_drill_typed_after_a_generation_is_drawn_and_checked(window, qapp):
    window.field_rows.setValue(1)
    window.field_cols.setValue(2)
    window.field_drill.setValue(1.0)
    window._last_params = None
    window._draw_preview()
    before = _hole_diameters(window)
    window._last_params = window._current_params()          # a generation ran
    window._set_single_tht_footprint_value("field_drill", 0.6)
    qapp.processEvents()
    assert _hole_diameters(window) != before
    assert window._edited_pad_reference()[1] == pytest.approx(0.6)


def test_b1_a_castellated_side_edited_after_a_generation_is_drawn(window, qapp):
    window.group_castellated.setChecked(True)
    window._last_params = None
    window._draw_preview()

    def pads():
        return [i for i in window.preview_scene.items()
                if i.zValue() == 1 and i.toolTip().startswith("Pin")]
    before = len(pads())
    window._last_params = window._current_castellated_params(photo_components=False)
    # One contact fewer always fits its edge (more could not fit the board).
    window._sides[0]["count"] = window._sides[0]["count"] - 1
    window._on_side_edited()
    qapp.processEvents()
    assert before and len(pads()) == before - 1


def test_b1_with_nothing_edited_the_preview_draws_what_was_generated(window):
    """Negative twin: right after a generation (or on reopening) the fields ARE the generated ones."""
    window.field_rows.setValue(1)
    window.field_cols.setValue(3)
    generated = window._current_params()
    window._last_params = generated
    drawn = window._preview_params()
    assert (drawn.rows, drawn.cols, drawn.pitch, drawn.drill, drawn.pad_diameter) == (
        generated.rows, generated.cols, generated.pitch, generated.drill, generated.pad_diameter)
    window._last_params = None
    assert window._preview_params() is not None


# ── B2: one output refresh evaluates the gate once ───────────────────────

def test_b2_one_refresh_evaluates_the_kicad_gate_and_the_family_once(window, monkeypatch):
    gates = []
    original = window._kicad_block_now
    monkeypatch.setattr(window, "_kicad_block_now", lambda: gates.append(1) or original())
    reads = []
    store_get = window.profile_store.get_type
    monkeypatch.setattr(window.profile_store, "get_type",
                        lambda key: reads.append(key) or store_get(key))
    window._refresh_output_actions()
    assert len(gates) <= 1
    assert len(reads) <= 1


def test_b2_outside_a_refresh_the_gate_is_read_again_every_time(window, monkeypatch):
    """Negative twin: the memo lives for one refresh only; a later change of state is seen."""
    window._refresh_output_actions()
    monkeypatch.setattr(window, "_kicad_work_busy", lambda: False)
    monkeypatch.setattr(window, "_kicad_files_alignment_block", lambda: None)
    assert window._kicad_block() is None
    monkeypatch.setattr(window, "_kicad_work_busy", lambda: True)
    assert window._kicad_block()[0] == "busy"
    assert window._output_refresh_memo is None


# ── B3: the 3D component menu is released ────────────────────────────────

def test_b3_the_component_menu_is_not_kept_after_a_right_click(window, qapp, monkeypatch):
    window._photo_rows = [{"name": "C1"}]
    monkeypatch.setattr(QMenu, "exec", lambda self, *a, **k: None)
    before = len(window.findChildren(QMenu))
    for _ in range(3):
        window._show_component_menu(0, QPoint(0, 0))
        _drain(qapp)
    assert len(window.findChildren(QMenu)) == before


def test_b3_a_chosen_entry_still_runs(window, qapp, monkeypatch):
    """Negative twin: the entry chosen during the menu loop still acts."""
    window._photo_rows = [{"name": "C1"}]
    seen = []
    monkeypatch.setattr(window, "_set_component_visibility", lambda row, visible: seen.append((row, visible)))

    def choose(menu, *a, **k):
        entry = next(a for a in menu.actions() if a.isEnabled() and a.text() and not a.menu())
        entry.trigger()
    monkeypatch.setattr(QMenu, "exec", choose)
    window._show_component_menu(0, QPoint(0, 0))
    assert seen == [(0, False)]


# ── B4: the diary merge on rename is rollback-safe ───────────────────────

def _two_diaries(window, tmp_path):
    source = window._log_file()
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("vecchio\n", encoding="utf-8")
    destination = tmp_path / "Nuovo"
    destination.mkdir()
    target = destination / window.LOG_FILENAME
    target.write_text("nuovo\n", encoding="utf-8")
    return source, destination, target


def test_b4_a_source_that_cannot_be_removed_leaves_both_diaries_as_they_were(window, tmp_path,
                                                                             monkeypatch):
    source, destination, target = _two_diaries(window, tmp_path)
    real_unlink = Path.unlink

    def refuse(self, *a, **k):
        if self == source:
            raise PermissionError("locked")
        return real_unlink(self, *a, **k)
    monkeypatch.setattr(Path, "unlink", refuse)
    assert window._move_log_to(destination) is False
    assert target.read_text(encoding="utf-8") == "nuovo\n"
    # The source keeps its text; the failure line is then appended to it by the log.
    assert source.read_text(encoding="utf-8").startswith("vecchio\n")
    assert [p.name for p in destination.iterdir()] == [window.LOG_FILENAME], "no temporary left"


def test_b4_a_normal_merge_appends_and_removes_the_source(window, tmp_path):
    source, destination, target = _two_diaries(window, tmp_path)
    assert window._move_log_to(destination) is True
    assert target.read_text(encoding="utf-8") == "nuovo\nvecchio\n"
    assert not source.exists()


# ── B5: a refused footprint-only rewrite marks the file stale ────────────

@pytest.mark.parametrize("started, stale", [(False, True), (True, False)])
def test_b5_the_stale_mark_follows_the_rewrite_start(window, monkeypatch, started, stale):
    monkeypatch.setattr(window, "_project_has_footprint", lambda: True)
    monkeypatch.setattr(window, "_background_work_running", lambda: False)
    monkeypatch.setattr(window, "_pipeline_owns_output", lambda: False)
    monkeypatch.setattr(window, "_generate", lambda **k: started)
    window._fp_file_stale = False
    window._fp_rewrite_dirty = True
    window._fp_rewrite_pad_edit = True
    window._flush_footprint_rewrite()
    assert window._fp_file_stale is stale


# ── C3 / C8 / C10: footprint keyboard commands ───────────────────────────

def _count_gesture_ends(window, monkeypatch):
    ends = []
    original = window.gesture_end
    monkeypatch.setattr(window, "gesture_end",
                        lambda target, last, commit: ends.append(commit) or original(target, last, commit))
    return ends


@pytest.mark.parametrize("key, modifiers", [
    (Qt.Key.Key_Delete, Qt.KeyboardModifier.NoModifier),     # deletes the dragged drawing
    (Qt.Key.Key_D, Qt.KeyboardModifier.ControlModifier),     # duplicates it
])
def test_c3_an_edit_command_during_a_drag_cancels_it_exactly_once(fp, monkeypatch, key, modifiers):
    window = fp
    _with_drawing(window, "rectangle", ((0, 8), (4, 10)))
    window._fp_clipboard.clear()
    ends = _count_gesture_ends(window, monkeypatch)
    _move(window, _vp(window, 4 * PX, 9 * PX))
    _press(window, _vp(window, 4 * PX, 9 * PX))
    _move(window, _vp(window, 6 * PX, 9 * PX))
    QTest.keyClick(window.preview_view, key, modifiers)
    assert ends == [False], "one cancel, never a drop"
    undo = len(window._fp_drawings_undo)
    drawings = [dict(d) for d in window.footprint_drawings]
    _release(window, _vp(window, 6 * PX, 9 * PX))
    assert ends == [False] and len(window._fp_drawings_undo) == undo
    assert [dict(d) for d in window.footprint_drawings] == drawings, "the release drops nothing"


# CONTRACT CHANGED 2026-10-29 (coordinator, parity with the symbol editor's
# `_end_drag_for_command`): a command with nothing to act on -- nothing to
# undo, an empty clipboard -- leaves the drag alone, as it did in the
# footprint before the review; only an acting command cancels it.
@pytest.mark.parametrize("key, modifiers", [
    (Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier),     # nothing to undo
    (Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier),     # empty clipboard
])
def test_c3_a_command_with_nothing_to_act_on_keeps_the_drag(fp, monkeypatch, key, modifiers):
    """Negative twin: the drag goes on and the drop commits, as in the symbol editor."""
    window = fp
    _with_drawing(window, "rectangle", ((0, 8), (4, 10)))
    window._fp_clipboard.clear()
    window._fp_drawings_undo.clear()
    ends = _count_gesture_ends(window, monkeypatch)
    _move(window, _vp(window, 4 * PX, 9 * PX))
    _press(window, _vp(window, 4 * PX, 9 * PX))
    _move(window, _vp(window, 6 * PX, 9 * PX))
    QTest.keyClick(window.preview_view, key, modifiers)
    assert ends == []
    _release(window, _vp(window, 6 * PX, 9 * PX))
    assert ends == [True] and window.footprint_drawings[0]["points"] == [[2.0, 8.0], [6.0, 10.0]]


def test_c3_copy_during_a_drag_does_not_cancel_it(fp, monkeypatch):
    """Negative twin: Ctrl+C edits nothing, so the drag goes on and the drop commits."""
    window = fp
    _with_drawing(window, "rectangle", ((0, 8), (4, 10)))
    ends = _count_gesture_ends(window, monkeypatch)
    _move(window, _vp(window, 4 * PX, 9 * PX))
    _press(window, _vp(window, 4 * PX, 9 * PX))
    _move(window, _vp(window, 6 * PX, 9 * PX))
    QTest.keyClick(window.preview_view, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert ends == []
    _release(window, _vp(window, 6 * PX, 9 * PX))
    assert ends == [True] and window.footprint_drawings[0]["points"] == [[2.0, 8.0], [6.0, 10.0]]


def test_c8_arrows_nudge_and_r_turns_the_selection_one_undo_step_each(fp):
    window = fp
    _with_drawing(window, "line", ((0, 8), (4, 8)))
    window._select_footprint_drawing(0)
    view = window.preview_view
    view.setFocus()
    QApplication.processEvents()
    step = window._footprint_paste_step()[0]
    QTest.keyClick(view, Qt.Key.Key_Right)
    assert window.footprint_drawings[0]["points"] == [[step, 8.0], [4 + step, 8.0]]
    QTest.keyClick(view, Qt.Key.Key_Up)
    assert window.footprint_drawings[0]["points"] == [[step, 8 - step], [4 + step, 8 - step]], "Y down"
    assert len(window._fp_drawings_undo) == 2
    QTest.keyClick(view, Qt.Key.Key_R)
    turned = window.footprint_drawings[0]["points"]
    xs = {round(p[0], 4) for p in turned}
    assert len(xs) == 1, "a horizontal line turned a quarter is vertical"
    assert len(window._fp_drawings_undo) == 3
    QTest.keyClick(view, Qt.Key.Key_R, Qt.KeyboardModifier.ShiftModifier)
    assert len(window._fp_drawings_undo) == 4
    assert window.footprint_drawings[0]["points"] == [[step, 8 - step], [4 + step, 8 - step]], \
        "Shift+R undoes the turn of R"


def test_c8_without_a_target_arrows_and_r_change_nothing(fp):
    """Negative twin: no selection and no hovered drawing: no edit, no undo step."""
    window = fp
    _with_drawing(window, "line", ((0, 8), (4, 8)))
    window._clear_footprint_selection()
    window._fp_hovered_drawing = -1
    view = window.preview_view
    view.setFocus()
    QApplication.processEvents()
    for key in (Qt.Key.Key_Left, Qt.Key.Key_Down, Qt.Key.Key_R):
        QTest.keyClick(view, key)
    assert window.footprint_drawings[0]["points"] == [[0, 8], [4, 8]]
    assert window._fp_drawings_undo == []
    keys = {s.key().toString() for s in window._fp_shortcuts}
    assert {"Left", "Right", "Up", "Down", "R", "Shift+R"} <= keys


def test_c10_pressing_the_same_selection_does_not_restyle(fp, monkeypatch):
    window = fp
    window.footprint_drawings = validate_footprint_drawings([
        {"kind": "line", "layer": "F.SilkS", "width": 0.12, "points": [[0, 8], [4, 8]]},
        {"kind": "line", "layer": "F.SilkS", "width": 0.12, "points": [[0, 12], [4, 12]]}])
    window._draw_preview()
    window._select_footprint_drawing(0)
    restyled = []
    original = window._refresh_drawing_selection
    monkeypatch.setattr(window, "_refresh_drawing_selection",
                        lambda: restyled.append(1) or original())
    window._select_footprint_drawing(0)
    assert restyled == []
    # Negative twin: another drawing, or the same one added to a bigger
    # selection, restyles.
    window._select_footprint_drawing(1)
    assert restyled == [1]
    window._select_footprint_drawing(0, additive=True)
    assert restyled == [1, 1]


# ── L2-GUI: a disabled «KiCadGen» entry is a confirmable conflict ────────

def _install_with(window, tmp_path, monkeypatch, code):
    from kicadgen.services.kicad_library import KiCadLibraryError
    from test_photo_inventory_gui import _write_complete_kicad_output
    _write_complete_kicad_output(window)
    project = tmp_path / "DestinationProject"
    project.mkdir()
    (project / "DestinationProject.kicad_pro").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(gui.QFileDialog, "getExistingDirectory", lambda *args: str(project))
    calls = []

    def install(*args, **kwargs):
        calls.append(kwargs.get("replace_library_entry"))
        if not kwargs.get("replace_library_entry"):
            raise KiCadLibraryError("entry", code=code, library="KiCadGen", path="fp-lib-table",
                                    old_uri="C:/Old/KiCadGen.pretty", new_uri="${KIPRJMOD}/KiCadGen.pretty",
                                    old_type="KiCad")
        return SimpleNamespace(identifier="KiCadGen:X", footprint_path=project, table_path=project,
                               replaced=(), removed=())
    monkeypatch.setattr(gui, "install_component_library", install)
    monkeypatch.setattr(gui, "record_kicad_export", lambda **k: True)
    keys = []
    original_tr = window.i18n.tr
    monkeypatch.setattr(window.i18n, "tr", lambda key, *a, **k: keys.append(key) or original_tr(key, *a, **k))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    window._install_project_library()
    return calls, keys


def test_l2_a_disabled_entry_asks_to_enable_it_and_retries_with_replace(window, tmp_path, monkeypatch):
    calls, keys = _install_with(window, tmp_path, monkeypatch, "library_entry_disabled")
    assert calls == [False, True]
    assert "kicad.confirm_enable_library_entry" in keys
    assert "kicad.confirm_replace_library_entry" not in keys


def test_l2_a_conflicting_entry_still_asks_the_replace_question(window, tmp_path, monkeypatch):
    calls, keys = _install_with(window, tmp_path, monkeypatch, "library_entry_conflict")
    assert calls == [False, True]
    assert "kicad.confirm_replace_library_entry" in keys
    assert "kicad.confirm_enable_library_entry" not in keys


# ── G7 (generators) seen from the window: stale host pad overrides ───────

def _headers_window(window, monkeypatch, overrides, drill=1.0):
    """Header mounting on a two-pin through-hole board, with `overrides` as the pad draft."""
    from kicadgen.core.package_geometry import PackageGeometry, PinSpec
    pins = [PinSpec(number=str(n), x_mm=x, y_mm=0.0, pad_length_mm=1.8, pad_width_mm=1.8,
                    side="left", name="", pad_type="thru_hole", shape="circle", drill_mm=1.0)
            for n, x in ((1, -2.54), (2, 2.54))]
    window.package_geometry = PackageGeometry(name="BOARD", family="module", body_length_mm=10.0,
                                              body_width_mm=6.0, confidence="high",
                                              needs_review=False, pins=pins)
    window.combo_mounting.setCurrentIndex(window.combo_mounting.findData("headers"))
    monkeypatch.setattr(window, "_photo_inventory_family", lambda: True)
    window.spin_header_pad.setValue(1.7)
    window.spin_header_drill.setValue(drill)
    window.header_pad_overrides = overrides
    logged = []
    monkeypatch.setattr(window, "_append_log", lambda text, *a, **k: logged.append(str(text)))
    return logged


def _host_overrides(host):
    """{pin: size} of the host lands that differ from the shared 1.7 mm diameter."""
    return {pin.number: pin.pad_length_mm for pin in host.pins if pin.pad_length_mm != 1.7}


def test_g7_stale_pad_overrides_are_left_out_reported_once_and_kept(window, monkeypatch):
    """A pin gone after a re-analysis, and a pad no longer larger than a widened drill."""
    overrides = {"1": {"shape": "rect", "size": 1.9}, "9": {"shape": "rect", "size": 1.9},
                 "2": {"shape": "circle", "size": 1.1}}
    logged = _headers_window(window, monkeypatch, overrides, drill=1.2)
    host = window._preview_params()                                      # no ValueError, drawn
    assert host is not None and _host_overrides(host) == {"1": 1.9}
    assert len(logged) == 1 and "2, 9" in logged[0]
    window._host_footprint_params(window.package_geometry)              # same set: no new line
    assert len(logged) == 1
    assert set(window.header_pad_overrides) == {"1", "2", "9"}          # the draft is not rewritten
    assert window._symbol_pad_ids() == ["1", "2"]


def test_g7_applicable_pad_overrides_pass_unchanged_without_a_log_line(window, monkeypatch):
    """Negative twin: overrides of existing pins larger than the drill reach the generator as they are."""
    overrides = {"1": {"shape": "rect", "size": 1.9}, "2": {"shape": "circle", "size": 1.5}}
    logged = _headers_window(window, monkeypatch, overrides, drill=1.0)
    host = window._preview_params()
    assert host is not None and _host_overrides(host) == {"1": 1.9, "2": 1.5}
    assert logged == []


# ── Direct pin header: stale pad edits (user decision 2026-10-29) ─────────

def _direct_header_window(window, monkeypatch, overrides, drill=1.0):
    """A 1 x 4 pin header drawn directly (no package, no host mounting), `overrides` as the pad draft."""
    window.package_geometry = None
    window.combo_mounting.setCurrentIndex(0)
    window.field_rows.setValue(1)
    window.field_cols.setValue(4)
    window.field_pad.setValue(1.7)
    window.field_drill.setValue(drill)
    window.header_pad_overrides = overrides
    logged = []
    monkeypatch.setattr(window, "_append_log", lambda text, *a, **k: logged.append(str(text)))
    return logged


def test_direct_header_stale_pad_edits_are_left_out_reported_once_and_kept(window, monkeypatch):
    """@brief Positive: a pin gone (9) and a pad not larger than the drill (3) no longer hide the preview."""
    overrides = {"2": {"shape": "rect", "size": 1.9}, "9": {"shape": "rect", "size": 1.9},
                 "3": {"shape": "circle", "size": 0.9}}
    logged = _direct_header_window(window, monkeypatch, overrides, drill=1.0)
    params = window._preview_params()
    assert isinstance(params, gui.PinHeaderParams)
    assert params.pad_overrides == {"2": {"shape": "rect", "size": 1.9}}
    assert len(logged) == 1 and "3, 9" in logged[0]
    window._current_params()                                            # same set: no new line
    assert len(logged) == 1
    assert set(window.header_pad_overrides) == {"2", "3", "9"}          # the draft is not rewritten


def test_direct_header_valid_pad_edits_pass_unchanged_without_a_log_line(window, monkeypatch):
    """@brief Negative: edits of existing pins larger than the drill reach the generator as they are."""
    overrides = {"1": {"shape": "rect", "size": 1.9}, "4": {"shape": "circle", "size": 1.5}}
    logged = _direct_header_window(window, monkeypatch, overrides, drill=1.0)
    params = window._preview_params()
    assert params is not None and params.pad_overrides == overrides
    assert logged == []
