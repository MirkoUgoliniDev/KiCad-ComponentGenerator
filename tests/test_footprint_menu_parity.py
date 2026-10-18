"""@brief Right-click menu of a footprint drawing (plan docs/canvas-menus-plan.md, phase C).

The menu is opened through the real `_footprint_drawing_context_menu` with
`QMenu.exec` replaced: the replacement records the menu as built (texts,
enabled state, separators, submenus with their check state) and returns the
action a test chooses, so the dispatch of every entry is exercised without a
modal loop. The same harness works whatever builds the menu, the window
itself or the shared `canvas_menus` module, because both use `QMenu`.
"""
from copy import deepcopy

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QInputDialog, QMenu  # noqa: E402

from kicadgen.core.footprint import USER_TEXT_ANCHOR, validate_footprint_drawings  # noqa: E402


def record(kind="line", points=((0, 0), (5, 5)), layer="F.SilkS", **extra):
    """A drawing record before validation."""
    base = {"kind": kind, "layer": layer, "width": 0.12, "points": [list(p) for p in points]}
    base.update(extra)
    return base


@pytest.fixture
def preview(window, monkeypatch):
    """The window with its preview drawn; no file is written."""
    monkeypatch.setattr(window, "_generate", lambda **kwargs: True)
    monkeypatch.setattr(window, "_project_has_footprint", lambda: True)
    window.chk_grid.setChecked(False)
    window._draw_preview()
    return window


def _describe(menu):
    """The menu as a list of rows: ("-",) for a separator, (text, enabled, submenu rows | None, checked)."""
    rows = []
    for action in menu.actions():
        if action.isSeparator():
            rows.append(("-",))
            continue
        sub = action.menu()
        rows.append((action.text(), action.isEnabled(),
                     _describe(sub) if sub is not None else None,
                     action.isChecked() if action.isCheckable() else None))
    return rows


def _find(menu, text):
    """The enabled action whose text is `text`, searching submenus too; None when absent."""
    for action in menu.actions():
        sub = action.menu()
        if sub is not None:
            found = _find(sub, text)
            if found is not None:
                return found
        elif action.text() == text and not action.isSeparator():
            return action
    return None


def open_menu(window, monkeypatch, index, choose=None):
    """Open the drawing menu of `index`, choose the entry `choose` (None dismisses), return its rows."""
    seen = []

    def fake_exec(menu, *_args):
        seen.append(_describe(menu))
        if choose is None:
            return None
        action = _find(menu, choose)
        assert action is not None, f"no entry {choose!r} in {seen[-1]}"
        return action

    monkeypatch.setattr(QMenu, "exec", fake_exec)
    window._footprint_drawing_context_menu(index)
    assert len(seen) == 1, "exactly one menu is shown"
    return seen[0]


def _texts(rows):
    """Top-level texts of the menu rows, separators as "-"."""
    return [row[0] for row in rows]


#: Entries shared with the symbol editor's element menu, in its order (plan §2).
SHARED = ["Ruota di 90 gradi (antiorario)", "Ruota di 90 gradi (orario)", "Specchia", "Elimina disegno",
          "Copia (Ctrl+C)", "Duplica (Ctrl+D)", "Layer", "Proprietà elemento…"]


# ── Menu structure ─────────────────────────────────────────────────────

def test_shape_menu_entries(preview, monkeypatch):
    """flipped: was ["Duplica (Ctrl+D)", "Layer", "Spessore del tratto…", "-", "Elimina disegno"]
    (no title, no turn/mirror/copy/properties, delete last); phase C aligns it
    with the symbol editor's element menu, built by `canvas_menus`."""
    window = preview
    window.footprint_drawings = validate_footprint_drawings([record()])
    rows = open_menu(window, monkeypatch, 0)
    assert _texts(rows) == ["Linea", "-"] + SHARED + ["-", "Spessore del tratto…"]
    assert rows[0][1] is False, "the title line is disabled"
    assert next(r for r in rows if r[0] == "Specchia")[1] is True, "a shape can be mirrored"


def test_text_menu_entries(preview, monkeypatch):
    """flipped: was ["Duplica (Ctrl+D)", "Layer", "-", "Modifica testo…", "-", "Elimina disegno"]."""
    window = preview
    window.footprint_drawings = validate_footprint_drawings(
        [record("text", ((1, 1),), text="HI", size=1.0, anchor=USER_TEXT_ANCHOR)])
    rows = open_menu(window, monkeypatch, 0)
    assert _texts(rows) == ["Testo", "-"] + SHARED + ["-", "Modifica testo…"]
    # Decision C-M1: a mirror never changes a text, so the entry is greyed out.
    assert next(r for r in rows if r[0] == "Specchia")[1] is False


@pytest.mark.parametrize("kind, points, title", [
    ("rectangle", ((0, 0), (2, 1)), "Rettangolo"),
    ("circle", ((0, 0), (1, 0)), "Cerchio"),
    ("arc", ((0, 0), (1, 1), (2, 0)), "Arco"),
])
def test_title_is_the_drawing_kind(preview, monkeypatch, kind, points, title):
    """Decision C-M4: the title line names the kind (palette words)."""
    window = preview
    window.footprint_drawings = validate_footprint_drawings([record(kind, points)])
    rows = open_menu(window, monkeypatch, 0)
    assert rows[0][0] == title and rows[1] == ("-",)


def test_layer_submenu_lists_the_three_layers_with_the_current_checked(preview, monkeypatch):
    window = preview
    window.footprint_drawings = validate_footprint_drawings([record(layer="F.Fab")])
    rows = open_menu(window, monkeypatch, 0)
    layer_row = next(row for row in rows if row[0] == "Layer")
    sub = layer_row[2]
    assert [r[0] for r in sub] == ["Serigrafia (stampata)", "Fabbricazione (documentazione)",
                                   "Commenti (solo progetto)"]
    assert [r[3] for r in sub] == [False, True, False]


def test_choosing_a_layer_moves_the_drawing(preview, monkeypatch):
    window = preview
    window.footprint_drawings = validate_footprint_drawings([record()])
    open_menu(window, monkeypatch, 0, "Commenti (solo progetto)")
    assert window.footprint_drawings[0]["layer"] == "Cmts.User"


def test_choosing_delete_removes_the_drawing(preview, monkeypatch):
    window = preview
    window.footprint_drawings = validate_footprint_drawings([record(), record("circle", ((0, 0), (1, 0)))])
    open_menu(window, monkeypatch, 0, "Elimina disegno")
    assert [r["kind"] for r in window.footprint_drawings] == ["circle"]


def test_choosing_duplicate_adds_a_shifted_copy(preview, monkeypatch):
    window = preview
    window.footprint_drawings = validate_footprint_drawings([record()])
    step = (window.preview_view.grid_step() or 20.0) / 20.0
    open_menu(window, monkeypatch, 0, "Duplica (Ctrl+D)")
    assert len(window.footprint_drawings) == 2
    assert window.footprint_drawings[1]["points"] == [[round(step, 4), round(step, 4)],
                                                      [round(5 + step, 4), round(5 + step, 4)]]


def test_choosing_stroke_width_asks_and_sets_it(preview, monkeypatch):
    window = preview
    window.footprint_drawings = validate_footprint_drawings([record()])
    monkeypatch.setattr(QInputDialog, "getDouble", staticmethod(lambda *a, **k: (0.25, True)))
    open_menu(window, monkeypatch, 0, "Spessore del tratto…")
    assert window.footprint_drawings[0]["width"] == 0.25


def test_choosing_edit_text_asks_and_sets_it(preview, monkeypatch):
    window = preview
    window.footprint_drawings = validate_footprint_drawings(
        [record("text", ((1, 1),), text="HI", size=1.0, anchor=USER_TEXT_ANCHOR)])
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("NEW", True)))
    open_menu(window, monkeypatch, 0, "Modifica testo…")
    assert window.footprint_drawings[0]["text"] == "NEW"


def test_dismissing_the_menu_changes_nothing(preview, monkeypatch):
    window = preview
    window.footprint_drawings = validate_footprint_drawings([record()])
    before = deepcopy(window.footprint_drawings)
    depth = len(window._fp_drawings_undo)
    open_menu(window, monkeypatch, 0, None)
    assert window.footprint_drawings == before and len(window._fp_drawings_undo) == depth


# ── New entries: turn, mirror, copy/paste, properties ─────────────────

def test_rotate_counter_clockwise_sends_a_point_right_of_centre_up(preview, monkeypatch):
    """Screen sense, Y down: «antiorario» moves the right end of a horizontal line to the top."""
    window = preview
    window.footprint_drawings = validate_footprint_drawings([record(points=((0, 0), (4, 0)))])
    open_menu(window, monkeypatch, 0, "Ruota di 90 gradi (antiorario)")
    assert window.footprint_drawings[0]["points"] == [[2, 2], [2, -2]], "the right end (4, 0) went UP to y = -2"
    open_menu(window, monkeypatch, 0, "Ruota di 90 gradi (orario)")
    assert window.footprint_drawings[0]["points"] == [[0, 0], [4, 0]], "clockwise undoes it"


def test_four_turns_give_the_drawing_back_and_each_is_one_undo_step(preview, monkeypatch):
    window = preview
    window.footprint_drawings = validate_footprint_drawings([record("arc", ((0, 0), (1, 1), (2, 0)))])
    before = deepcopy(window.footprint_drawings)
    depth = len(window._fp_drawings_undo)
    for _ in range(4):
        open_menu(window, monkeypatch, 0, "Ruota di 90 gradi (orario)")
    assert window.footprint_drawings == before
    assert len(window._fp_drawings_undo) == depth + 4
    window._undo_footprint_drawing()
    assert window.footprint_drawings != before, "one undo takes back one quarter turn"


def test_mirror_flips_a_shape_about_its_bounds_centre(preview, monkeypatch):
    window = preview
    window.footprint_drawings = validate_footprint_drawings([record(points=((0, 0), (4, 2)))])
    open_menu(window, monkeypatch, 0, "Specchia")
    assert window.footprint_drawings[0]["points"] == [[4, 0], [0, 2]]


def test_rotating_a_text_toggles_its_angle_and_keeps_its_anchor(preview, monkeypatch):
    window = preview
    window.footprint_drawings = validate_footprint_drawings(
        [record("text", ((1, 1),), text="HI", size=1.0, anchor=USER_TEXT_ANCHOR)])
    open_menu(window, monkeypatch, 0, "Ruota di 90 gradi (antiorario)")
    assert window.footprint_drawings[0]["angle"] == 90 and window.footprint_drawings[0]["points"] == [[1, 1]]
    open_menu(window, monkeypatch, 0, "Ruota di 90 gradi (orario)")
    assert "angle" not in window.footprint_drawings[0], "back to horizontal: the record is as never turned"


def test_copy_then_paste_lands_one_more_step_away_each_time(preview, monkeypatch):
    window = preview
    window.footprint_drawings = validate_footprint_drawings([record()])
    step = window._footprint_paste_step()[0]
    open_menu(window, monkeypatch, 0, "Copia (Ctrl+C)")
    assert len(window.footprint_drawings) == 1, "copying adds nothing"
    assert window._paste_footprint_drawings()
    assert window._paste_footprint_drawings()
    firsts = [r["points"][0] for r in window.footprint_drawings]
    assert firsts == [[0, 0], [round(step, 4), round(step, 4)], [round(2 * step, 4), round(2 * step, 4)]]


def test_paste_with_an_empty_clipboard_changes_nothing(preview):
    window = preview
    window.footprint_drawings = validate_footprint_drawings([record()])
    depth = len(window._fp_drawings_undo)
    assert not window._paste_footprint_drawings()
    assert len(window.footprint_drawings) == 1 and len(window._fp_drawings_undo) == depth


def test_a_refused_paste_does_not_move_the_next_one(preview):
    window = preview
    step = window._footprint_paste_step()[0]
    edge = round(500 - step / 2, 4)
    window.footprint_drawings = validate_footprint_drawings([record(points=((edge, 0), (edge, 1)))])
    window._copy_drawings([0])
    assert not window._paste_footprint_drawings(), "beyond the 500 mm bound: refused"
    assert len(window.footprint_drawings) == 1
    window.footprint_drawings = validate_footprint_drawings([record()])
    window._copy_drawings([0])
    assert window._paste_footprint_drawings()


def test_duplicate_keeps_the_clipboard(preview, monkeypatch):
    window = preview
    window.footprint_drawings = validate_footprint_drawings([record(), record("circle", ((0, 0), (1, 0)))])
    window._copy_drawings([1])
    open_menu(window, monkeypatch, 0, "Duplica (Ctrl+D)")
    assert [r["kind"] for r in window.footprint_drawings] == ["line", "circle", "line"]
    assert window._paste_footprint_drawings()
    assert window.footprint_drawings[-1]["kind"] == "circle", "the clipboard still holds the copied circle"


def test_keyboard_copy_and_paste_act_on_the_hovered_drawing(preview):
    window = preview
    window.footprint_drawings = validate_footprint_drawings([record(), record("circle", ((0, 0), (1, 0)))])
    window._fp_hovered_drawing = 1
    window._copy_hovered_drawing()
    window._fp_hovered_drawing = -1
    window._copy_hovered_drawing()          # nothing hovered: the clipboard keeps the circle
    assert window._paste_footprint_drawings()
    assert window.footprint_drawings[-1]["kind"] == "circle"


def test_properties_unchanged_change_nothing(preview):
    window = preview
    window.footprint_drawings = validate_footprint_drawings([record("arc", ((0, 0), (1, 1), (2, 0)))])
    before = deepcopy(window.footprint_drawings)
    depth = len(window._fp_drawings_undo)
    values = window._drawing_properties_values(window.footprint_drawings[0])
    assert window.set_drawing_properties(0, dict(values))
    assert window.footprint_drawings == before and len(window._fp_drawings_undo) == depth


def test_properties_move_and_resize_a_rectangle_in_one_undo_step(preview):
    window = preview
    window.footprint_drawings = validate_footprint_drawings([record("rectangle", ((0, 0), (4, 2)))])
    depth = len(window._fp_drawings_undo)
    values = window._drawing_properties_values(window.footprint_drawings[0])
    assert (values["x"], values["y"], values["rect_width"], values["rect_height"]) == (2, 1, 4, 2)
    values.update(x=10.0, rect_width=6.0, layer="F.Fab", width=0.2)
    assert window.set_drawing_properties(0, values)
    drawn = window.footprint_drawings[0]
    assert drawn["points"] == [[7, 0], [13, 2]] and drawn["layer"] == "F.Fab" and drawn["width"] == 0.2
    assert len(window._fp_drawings_undo) == depth + 1


def test_properties_resize_a_circle_and_refuse_a_zero_radius(preview):
    window = preview
    window.footprint_drawings = validate_footprint_drawings([record("circle", ((1, 1), (1, 3)))])
    values = window._drawing_properties_values(window.footprint_drawings[0])
    assert values["radius"] == 2
    assert window.set_drawing_properties(0, dict(values, radius=0.5))
    assert window.footprint_drawings[0]["points"] == [[1, 1], [1, 1.5]], "rim keeps its direction"
    before = deepcopy(window.footprint_drawings)
    assert not window.set_drawing_properties(0, dict(values, radius=0.0))
    assert window.footprint_drawings == before


def test_properties_edit_a_text_anchor_content_size_and_angle(preview):
    window = preview
    window.footprint_drawings = validate_footprint_drawings(
        [record("text", ((1, 1),), text="HI", size=1.0, anchor=USER_TEXT_ANCHOR)])
    values = window._drawing_properties_values(window.footprint_drawings[0])
    assert (values["x"], values["y"], values["text"], values["angle"]) == (1, 1, "HI", 0)
    values.update(x=2.5, text="NEW", size=1.5, angle=90)
    assert window.set_drawing_properties(0, values)
    drawn = window.footprint_drawings[0]
    assert drawn == {"kind": "text", "layer": "F.SilkS", "width": 0.12, "points": [[2.5, 1]],
                     "text": "NEW", "size": 1.5, "anchor": USER_TEXT_ANCHOR, "angle": 90}
    assert not window.set_drawing_properties(0, dict(values, text="  ")), "a blank text is refused"


def test_a_vertical_text_is_drawn_turned_about_its_anchor(preview):
    """The preview item of an angle-90 text reads bottom-to-top and grows UP from its anchor."""
    from kicadgen.gui.footprint_view import DRAWING_KEY_ROLE
    window = preview
    window.footprint_drawings = validate_footprint_drawings(
        [record("text", ((1, 1),), text="HHHH", size=1.0, anchor=USER_TEXT_ANCHOR, angle=90)])
    window._draw_preview()
    item = next(i for i in window.preview_scene.items() if i.data(DRAWING_KEY_ROLE) == 0)
    box = item.sceneBoundingRect()
    assert item.rotation() == -90
    assert box.height() > box.width(), "a vertical text is taller than wide"
    assert box.bottom() <= 20 + 2 and box.top() < 20 - 40, "it grows up from the anchor at y = 20 px"
    assert box.right() <= 20 + 2, "the glyph tops face -x: the box stays left of the anchor"
    # Negative twin: the same text without angle is wider than tall, right of the anchor.
    window.footprint_drawings = validate_footprint_drawings(
        [record("text", ((1, 1),), text="HHHH", size=1.0, anchor=USER_TEXT_ANCHOR)])
    window._draw_preview()
    flat = next(i for i in window.preview_scene.items() if i.data(DRAWING_KEY_ROLE) == 0)
    assert flat.rotation() == 0 and flat.sceneBoundingRect().width() > flat.sceneBoundingRect().height()


# ── Review findings M1, L1-L3, L5 ─────────────────────────────────────

#: A long arc whose drawn box reaches far left of its stored points (centre x about -617 mm).
LONG_ARC = ((-485.0933, 96.4181), (-493.934, 106.066), (-485.0933, -96.4181))


def run_properties(window, monkeypatch, index, edit=None):
    """Open «Proprietà elemento…» on `index` with real widgets, let `edit(dialog)` change them, press OK.

    @return {"labels": form row captions, "message": text of the refusal line, "accepted": bool}.
    """
    from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout, QLabel
    seen = {}

    def fake_exec(dialog):
        if edit is not None:
            edit(dialog)
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()
        form = dialog.findChild(QFormLayout)
        labels = []
        for row in range(form.rowCount()):
            item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
            if item is not None and isinstance(item.widget(), QLabel):
                labels.append(item.widget().text())
        seen["labels"] = labels
        seen["message"] = " ".join(label.text() for label in dialog.findChildren(QLabel) if label.wordWrap())
        seen["accepted"] = dialog.result() == QDialog.DialogCode.Accepted
        return dialog.result()

    monkeypatch.setattr(QDialog, "exec", fake_exec)
    window._edit_drawing_properties(index)
    return seen


def _spin(dialog, caption):
    """The spin box of the form row whose caption is `caption`."""
    from PyQt6.QtWidgets import QFormLayout
    form = dialog.findChild(QFormLayout)
    for row in range(form.rowCount()):
        label = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
        if label is not None and label.widget().text() == caption:
            return form.itemAt(row, QFormLayout.ItemRole.FieldRole).widget()
    raise AssertionError(caption)


@pytest.mark.parametrize("drawing", [
    record(width=7.0),                                       # beyond the old 5 mm spin maximum
    record(width=0.005),                                     # below the old 0.01 mm spin minimum
    record("circle", ((-400, -400), (400, 400))),            # radius ~1131 mm, beyond the old 500
    record("arc", LONG_ARC),                                 # box centre x ~-617, beyond the old +-500
], ids=["width7", "width0.005", "big-circle", "long-arc"])
def test_m1_properties_accepted_unchanged_change_nothing(preview, monkeypatch, drawing):
    """M1: a widget that would round or clamp the shown value never rewrites the drawing."""
    window = preview
    window.footprint_drawings = validate_footprint_drawings([drawing])
    assert window.footprint_drawings, "the repro is a valid record"
    before = deepcopy(window.footprint_drawings)
    depth = len(window._fp_drawings_undo)
    seen = run_properties(window, monkeypatch, 0)
    assert seen["accepted"]
    assert window.footprint_drawings == before and len(window._fp_drawings_undo) == depth


@pytest.mark.parametrize("drawing", [record(width=7.0), record("circle", ((-400, -400), (400, 400))),
                                     record("arc", LONG_ARC)], ids=["width7", "big-circle", "long-arc"])
def test_m1_a_real_edit_in_the_same_dialog_still_commits(preview, monkeypatch, drawing):
    """Negative twin of M1: changing one field commits it, and only it, as one undo step."""
    window = preview
    window.footprint_drawings = validate_footprint_drawings([drawing])
    before = deepcopy(window.footprint_drawings[0])
    depth = len(window._fp_drawings_undo)
    run_properties(window, monkeypatch, 0,
                   lambda d: _spin(d, "Y (mm)").setValue(_spin(d, "Y (mm)").value() + 1.0))
    after = window.footprint_drawings[0]
    assert len(window._fp_drawings_undo) == depth + 1
    assert after["width"] == before["width"] and after["layer"] == before["layer"]
    assert after["points"] == [[x, round(y + 1.0, 4)] for x, y in before["points"]]


def test_l1_a_text_has_no_stroke_width_row(preview, monkeypatch):
    window = preview
    window.footprint_drawings = validate_footprint_drawings(
        [record("text", ((1, 1),), text="HI", size=1.0, anchor=USER_TEXT_ANCHOR)])
    assert "Spessore del tratto in mm" not in run_properties(window, monkeypatch, 0)["labels"]
    # Negative twin: a shape keeps the row.
    window.footprint_drawings = validate_footprint_drawings([record()])
    assert "Spessore del tratto in mm" in run_properties(window, monkeypatch, 0)["labels"]


def _toasts(window, monkeypatch):
    shown = []
    monkeypatch.setattr(window, "_draw_feedback_toast", lambda title, body: shown.append((title, body)))
    return shown


def test_l2_a_turn_leaving_the_limit_is_refused_with_a_toast(preview, monkeypatch):
    window = preview
    # Arc 300..499 mm wide, 800 mm tall: a quarter turn about its box centre
    # (399.5, 0) sends the end (300, 400) to x = 799.5, beyond +-500 mm.
    window.footprint_drawings = validate_footprint_drawings([record("arc", ((300, -400), (499, 0), (300, 400)))])
    assert window.footprint_drawings
    before = deepcopy(window.footprint_drawings)
    shown = _toasts(window, monkeypatch)
    open_menu(window, monkeypatch, 0, "Ruota di 90 gradi (antiorario)")
    assert window.footprint_drawings == before
    assert len(shown) == 1 and shown[0][0] == "Modifica scartata." and "area consentita" in shown[0][1]
    # Negative twin: a mirror of the same arc stays inside, no toast.
    open_menu(window, monkeypatch, 0, "Specchia")
    assert window.footprint_drawings != before and len(shown) == 1


def test_l2_a_refused_paste_says_why(preview, monkeypatch):
    window = preview
    step = window._footprint_paste_step()[0]
    edge = round(500 - step / 2, 4)
    window.footprint_drawings = validate_footprint_drawings([record(points=((edge, 0), (edge, 1)))])
    shown = _toasts(window, monkeypatch)
    window._copy_drawings([0])
    assert not window._paste_footprint_drawings()
    assert len(shown) == 1 and "area consentita" in shown[0][1]


def test_l3_properties_refusal_names_the_reason(preview, monkeypatch):
    from PyQt6.QtWidgets import QLineEdit
    window = preview
    window.footprint_drawings = validate_footprint_drawings(
        [record("text", ((1, 1),), text="HI", size=1.0, anchor=USER_TEXT_ANCHOR)])
    seen = run_properties(window, monkeypatch, 0, lambda d: d.findChild(QLineEdit).setText("   "))
    assert not seen["accepted"] and seen["message"].startswith("Testo non valido")
    window.footprint_drawings = validate_footprint_drawings([record()])
    seen = run_properties(window, monkeypatch, 0, lambda d: _spin(d, "X (mm)").setValue(600.0))
    assert not seen["accepted"] and "area consentita" in seen["message"]
    assert "rotazione a passi" not in seen["message"], "no longer the generic symbol message"


def test_l5_copy_and_paste_report_their_count_in_the_status_bar(preview):
    window = preview
    window.footprint_drawings = validate_footprint_drawings([record()])
    window.statusBar().clearMessage()
    window._fp_hovered_drawing = -1
    window._copy_hovered_drawing()
    assert window.statusBar().currentMessage() == "", "nothing hovered: nothing copied, nothing said"
    window._fp_hovered_drawing = 0
    window._copy_hovered_drawing()
    assert window.statusBar().currentMessage() == "1 elementi copiati."
    assert window._paste_footprint_drawings()
    assert window.statusBar().currentMessage() == "1 elementi incollati."


def test_angle_survives_the_project_file(window):
    state = deepcopy(window._project_state())
    state["footprint_drawings"] = [record("text", ((1, 1),), text="HI", size=1.0,
                                          anchor=USER_TEXT_ANCHOR, angle=90)]
    window._apply_project_state(state)
    assert window.footprint_drawings[0]["angle"] == 90
    assert window._project_state()["footprint_drawings"][0]["angle"] == 90


def test_angle_survives_the_recovery_draft(window):
    window.footprint_drawings = validate_footprint_drawings(
        [record("text", ((1, 1),), text="HI", size=1.0, anchor=USER_TEXT_ANCHOR, angle=90)])
    data = window._footprint_state_json(window._footprint_edit_state())
    assert window._footprint_state_from_json(data)["drawings"][0]["angle"] == 90
