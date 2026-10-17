"""@brief Regression tests of the symbol-editor review fixes B1-B10 (2026-10-13).

Each fix has a positive case (the defect is gone) and a negative case that
the same rule must tell apart for a structural reason, not a number tuned
on the reproduction.
"""
import ast
import os
import re
import shutil
import string
import subprocess
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QPointF

from kicadgen.generators import symbol as symbol_module
from kicadgen.generators import symbol_glyphs as g
from kicadgen.generators.symbol import (
    SYMBOL_NAME_ILLEGAL_CHARACTERS, SymbolError, SymbolPin, export_symbol, layout_symbol,
    serialize_symbol, symbol_inputs)
from kicadgen.gui.symbol_tab import GRID_MM, SCENE_UNITS_PER_MM, SymbolTab
from kicadgen.services import klc_checks as k
from kicadgen.services.i18n import DEFAULT_TRANSLATIONS, LANGUAGES
from kicadgen.services.symbol_draft import load_draft

ROOT = Path(__file__).resolve().parents[1]


def _kicad_cli():
    cli = shutil.which("kicad-cli")
    if cli:
        return cli
    for candidate in sorted(Path("C:/Program Files/KiCad").glob("*/bin/kicad-cli.exe"), reverse=True):
        return str(candidate)
    return None


def _rows(count=4, **names):
    return [{"pin": str(i), "name": names.get(f"p{i}", f"P{i}")} for i in range(1, count + 1)]


def _ids(count=4):
    return [str(i) for i in range(1, count + 1)]


def _names(tab):
    return {row["pin"]: row["name"] for row in tab.draft_rows()}


# ── B3: a pending draft is never lost when the source changes ────────────

def test_b3_same_pins_rebase_the_draft_onto_the_new_baseline(qapp, tmp_path):
    path = tmp_path / ".symbol-draft.json"
    tab = SymbolTab()
    try:
        tab.set_data("Board", _rows(), "L:Board", _ids())
        tab.set_recovery_path(path)
        tab.table.item(0, 1).setText("USER_EDIT")
        incoming = _rows(p4="OTHER")
        tab.set_data("Board", incoming, "L:Board", _ids())
        assert _names(tab) == {"1": "USER_EDIT", "2": "P2", "3": "P3", "4": "OTHER"}, \
            "the draft edit is kept and the new data arrives"
        assert tab.has_pending_edits() and "riportate" in tab.status.text()
        tab.set_recovery_path(path)
        tab._changed()
        assert path.exists(), "the rebased draft is checkpointed, never deleted"
        assert load_draft(path, tab.rows())[0]["name"] == "USER_EDIT"
    finally:
        tab.deleteLater()


def test_b3_different_pins_keep_the_old_draft_recoverable(qapp, tmp_path):
    path = tmp_path / ".symbol-draft.json"
    tab = SymbolTab()
    try:
        tab.set_data("Board", _rows(), "L:Board", _ids())
        tab.set_recovery_path(path)
        old_baseline = tab.rows()
        tab.table.item(0, 1).setText("USER_EDIT")
        draft = tab.draft_rows()
        tab.set_data("Board", _rows(3), "L:Board", _ids(3))
        tab.set_recovery_path(path)
        assert _names(tab) == {"1": "P1", "2": "P2", "3": "P3"}, "the new data is shown as it is"
        assert not tab.has_pending_edits()
        assert tab.recovery_button.defaultAction().isEnabled()
        assert "Recupera bozza" in tab.status.text()
        tab._changed()
        assert path.exists(), "the recovery file is not deleted while the draft is offered"
        [copy] = SymbolTab.superseded_draft_paths(path)
        assert load_draft(copy, old_baseline) == draft, "a copy bound to the old baseline stays loadable"
        tab.recover_draft()
        assert _names(tab) == {"1": "USER_EDIT", "2": "P2", "3": "P3"}
        assert tab.has_pending_edits()
        assert not SymbolTab.superseded_draft_paths(path), "a recovered snapshot is retired"
        assert load_draft(path, tab.rows())[0]["name"] == "USER_EDIT", "...once the recovery file holds it"
    finally:
        tab.deleteLater()


def test_b3_a_field_changed_on_both_sides_is_not_overwritten_silently(qapp, tmp_path):
    tab = SymbolTab()
    try:
        tab.set_data("Board", _rows(), "L:Board", _ids())
        tab.table.item(0, 1).setText("MINE")
        tab.set_data("Board", _rows(p1="THEIRS"), "L:Board", _ids())
        assert _names(tab)["1"] == "THEIRS" and not tab.has_pending_edits()
        assert tab.recovery_button.defaultAction().isEnabled()
        tab.recover_draft()
        assert _names(tab)["1"] == "MINE"
    finally:
        tab.deleteLater()


def test_b3_without_pending_edits_nothing_is_offered_or_copied(qapp, tmp_path):
    """Negative: a plain reload must not invent a draft nor write a copy."""
    path = tmp_path / ".symbol-draft.json"
    tab = SymbolTab()
    try:
        tab.set_data("Board", _rows(), "L:Board", _ids())
        tab.set_recovery_path(path)
        tab.set_data("Board", _rows(3), "L:Board", _ids(3))
        tab.set_recovery_path(path)
        assert not tab.recovery_button.defaultAction().isEnabled()
        assert not SymbolTab.superseded_draft_paths(path)
        assert not tab.has_pending_edits()
    finally:
        tab.deleteLater()


def test_b3_another_project_draft_is_not_offered_but_kept_on_disk(qapp, tmp_path):
    path = tmp_path / "a" / ".symbol-draft.json"
    # CONTRACT CHANGED (audit L3, 2026-10-15): drafts never create the project
    # folder; a real project has one from «Nuovo progetto» on.
    path.parent.mkdir()
    (tmp_path / "b").mkdir()
    tab = SymbolTab()
    try:
        tab.set_data("Alpha", _rows(), "L:Alpha", _ids())
        tab.set_recovery_path(path)
        alpha = tab.rows()
        tab.table.item(0, 1).setText("USER_EDIT")
        tab.set_data("Beta", _rows(), "L:Beta", _ids())
        tab.set_recovery_path(tmp_path / "b" / ".symbol-draft.json")
        assert _names(tab)["1"] == "P1" and not tab.recovery_button.defaultAction().isEnabled()
        assert load_draft(path, alpha)[0]["name"] == "USER_EDIT", \
            "Alpha's own recovery file, still bound to Alpha's baseline, keeps the draft"
    finally:
        tab.deleteLater()


# ── B6: wires are frozen where the APPLIED layout had the dropped pin ────

def test_b6_frozen_wire_point_uses_the_applied_layout_not_the_draft(qapp):
    tab = SymbolTab()
    tab.resize(1200, 800)
    try:
        tab.set_data("Board", _rows(5), pad_ids=_ids(5))
        assert tab.place_glyph("relay_coil", 0, 0)
        tab.start_wire()
        assert tab.add_wire_point(-5.0, 2.6) and tab.add_wire_point(0.1, 5.0)
        assert tab.finish_wire(True)
        tab.apply()
        applied_end = g.pin_inner_ends(layout_symbol(*symbol_inputs(tab.rows())))["1"]
        wire = next(r for r in tab.rows()[0]["symbol_graphics"] if r["key"] == "wire")
        assert wire["primitives"][0]["points"][0]["anchor"]["kind"] == "pin", "anchored to pin 1"
        assert tab.place_pin("1", -15.24, 10.16), "unapplied move of pin 1"
        draft_end = g.pin_inner_ends(tab._symbol)["1"]
        assert draft_end != applied_end
        tab.set_data("Board", [r for r in tab.rows() if r["pin"] != "1"], pad_ids=_ids(5)[1:])
        wire = next(r for r in tab.graphics() if r["key"] == "wire")
        assert tuple(wire["primitives"][0]["points"][0]) == pytest.approx(applied_end)
    finally:
        tab.deleteLater()


# ── B4/B5: group drag on the pin grid, shapes keep the drawing grid ──────

def _shape_and_glyph_tab():
    tab = SymbolTab()
    tab.resize(1200, 800)
    tab.set_data("Board", _rows(5), pad_ids=_ids(5))
    assert tab.place_glyph(tab.glyph_combo.itemData(0), 0, 0)
    # An odd-sized rectangle: its origin (centre) is NOT on the drawing grid.
    rect = g.shape_instance("rectangle", [(5.08, 5.08), (5.08 + 3 * 0.635, 5.08 + 0.635)])
    rows = tab._with_graphics(tab.draft_rows(), tab.graphics() + [rect])
    tab.set_data("Board", rows, pad_ids=_ids(5), force=True)
    tab.set_drawing_grid(0.635)
    return tab


def _corners(record):
    points = g.placed_primitives(record)[0].points
    return [coordinate for point in points for coordinate in point]


def _drag(tab, index, dx_mm, dy_mm=0.0):
    tab._begin_gesture(("graphic", index), QPointF(0, 0))
    tab._move_gesture(("graphic", index), QPointF(dx_mm * SCENE_UNITS_PER_MM, -dy_mm * SCENE_UNITS_PER_MM))
    tab._end_gesture(True)


def _on(step, values):
    return all(abs(v / step - round(v / step)) < 1e-6 for v in values)


def test_b4_a_group_led_by_a_shape_moves_on_the_pin_grid(qapp):
    tab = _shape_and_glyph_tab()
    try:
        tab._select_graphic(0)
        tab._select_graphic(1, additive=True)
        _drag(tab, 1, 1.5, 0.7)
        glyph, rect = tab.graphics()
        assert (glyph["x"], glyph["y"]) == (2.54, 0.0), "the glyph stays on the 2.54 mm grid"
        assert _on(GRID_MM, [glyph["x"], glyph["y"]])
        assert _on(0.635, _corners(rect)), "the shape moved by the same whole pin steps"
    finally:
        tab.deleteLater()


def test_b5_shapes_alone_move_by_drawing_grid_steps_and_keep_their_corners(qapp):
    """Negative of B4: without a glyph or wire the finer drawing grid applies."""
    tab = _shape_and_glyph_tab()
    try:
        before = _corners(tab.graphics()[1])
        tab._select_graphic(1)
        _drag(tab, 1, 0.7, 0.0)
        after = _corners(tab.graphics()[1])
        assert after[0] - before[0] == pytest.approx(0.635), "one drawing step, not a pin step"
        assert _on(0.635, after), "an odd-sized shape keeps its corners on the drawing grid"
        assert tab.graphics()[0]["x"] == 0.0, "an unselected glyph does not move"
    finally:
        tab.deleteLater()


# ── B8: pin numbers are normalised once at the entry point ──────────────

def test_b8_a_padded_pin_number_is_the_same_pin(qapp):
    tab = SymbolTab()
    tab.resize(1200, 800)
    try:
        tab.set_data("B", [{"pin": "1 ", "name": "A"}, {"pin": "2", "name": "B"}], "L:B", ["1", " 2"])
        assert [r["pin"] for r in tab.draft_rows()] == ["1", "2"]
        assert tab._symbol is not None and not tab._error
        assert tab.place_pin("1", -10.16, 0.0)
    finally:
        tab.deleteLater()


def test_b8_padded_and_plain_numbers_together_are_a_duplicate(qapp):
    """Negative: stripping must merge identities, so the pair is now reported."""
    tab = SymbolTab()
    try:
        tab.set_data("B", [{"pin": "1 ", "name": "A"}, {"pin": "1", "name": "B"}])
        assert "duplicati" in tab.status.text()
    finally:
        tab.deleteLater()


# ── B9: generator errors reach the interface translated ─────────────────

def _symbol_templates():
    """Every Italian template given to `_message`/`_label` in generators/symbol.py."""
    found = set()
    tree = ast.parse((ROOT / "kicadgen" / "generators" / "symbol.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and getattr(node.func, "id", None) in ("_message", "_label")
                and node.args and isinstance(node.args[0], ast.Constant)):
            found.add(node.args[0].value)
    return found


def _fields(text):
    return sorted(name for _, name, _, _ in string.Formatter().parse(text) if name)


def test_b9_every_symbol_error_template_has_four_languages():
    templates = _symbol_templates()
    assert len(templates) > 25, "the scan no longer sees the generator's messages"
    problems = []
    for template in sorted(templates):
        entry = DEFAULT_TRANSLATIONS.get("ui:" + template)
        if not entry:
            problems.append(f"missing {template!r}")
            continue
        for language in LANGUAGES:
            if not entry.get(language) or _fields(entry[language]) != _fields(template):
                problems.append(f"{language}: {template!r}")
    assert not problems, "\n".join(problems)


def _german(source):
    return DEFAULT_TRANSLATIONS.get("ui:" + source, {}).get("de", source)


def test_b9_a_symbol_error_is_english_in_logs_and_translated_on_demand():
    with pytest.raises(SymbolError) as caught:
        layout_symbol([SymbolPin("7", "A", side="middle")])
    exc = caught.value
    assert isinstance(exc, ValueError) and str(exc) == "Pin 7: invalid side"
    assert exc.localized() == "Pin 7: lato non valido.", "Italian source without a translator"
    assert exc.localized(_german) == "Pin 7: ungültige Seite."
    with pytest.raises(SymbolError) as nested:
        symbol_module.normalize_pins([SymbolPin("3", "A\x01")])
    assert nested.value.localized(_german) == "Name von Pin 3: Steuerzeichen sind nicht erlaubt."
    assert str(nested.value) == "Pin 3 name: control characters are not allowed"


def test_b9_the_status_line_shows_the_translated_error(qapp):
    tab = SymbolTab(tx=_german)
    try:
        rows = [dict(r, symbol_body=[2.54, 2.54]) for r in _rows(2)]
        tab.set_data("B", rows, "L:B", _ids(2))
        assert tab.status.text() == ("Die Abmessungen des Symbolkörpers müssen zwischen "
                                     "10,16 und 1000 mm liegen.")
        assert tab.error_text(ValueError("plain")) == "plain", "other errors keep their text"
    finally:
        tab.deleteLater()


# ── B1: symbol text angles in tenths of a degree ────────────────────────

def test_b1_text_angle_is_written_in_tenths():
    rec = g.shape_instance("text", [(0.0, 0.0)], text="VERT")
    rec["rotation"] = 90
    line = next(l for l in serialize_symbol("T", [SymbolPin("1", "A")], graphics=[rec]).splitlines()
                if '(text "VERT"' in l)
    assert "(at 0 0 900)" in line
    assert [g.text_angle_to_file(a) for a in (0, 90, 180, 270)] == [0, 900, 1800, 2700]


@pytest.mark.parametrize("value, degrees", [
    (0, 0), (900, 90), (1800, 180), (2700, 270), (450, 45),   # KiCad: tenths
    (90, 9), (180, 18), (270, 27),                           # KiCad tenths, no value heuristic
    (9, 0.9), (300, 30)])                                   # not a legacy quarter turn: tenths
def test_b1_text_angle_reading(value, degrees):
    assert g.text_angle_from_file(value) == pytest.approx(degrees)


def test_b1_kicad_draws_a_900_text_vertical_and_a_0_text_horizontal(tmp_path):
    cli = _kicad_cli()
    if not cli:
        pytest.skip("KiCad CLI not installed")
    vertical = g.shape_instance("text", [(0.0, 2.54)], text="VERTX")
    vertical["rotation"] = 90
    horizontal = g.shape_instance("text", [(0.0, -2.54)], text="HORZX")
    path = export_symbol(tmp_path / "t.kicad_sym", "T", [SymbolPin("1", "A")],
                         graphics=[vertical, horizontal])
    out = tmp_path / "svg"
    result = subprocess.run([cli, "sym", "export", "svg", "-o", str(out), str(path)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    svg = next(out.glob("*.svg")).read_text(encoding="utf-8")

    def rotation(label):
        match = re.search(r'(?:<g transform="rotate\(([-0-9.]+)[^"]*">\s*)?<text[^>]*>' + label + "</text>", svg)
        assert match, label
        return float(match.group(1) or 0)

    assert abs(rotation("VERTX")) == pytest.approx(90)
    assert rotation("HORZX") == pytest.approx(0)


# ── B2: one illegal-character set per item kind ─────────────────────────

@pytest.mark.parametrize("character", list(SYMBOL_NAME_ILLEGAL_CHARACTERS) + ["\x07"])
def test_b2_the_generator_refuses_what_kicad_cannot_load(character):
    with pytest.raises(SymbolError):
        serialize_symbol(f"N{character}x", [SymbolPin("1", "A")])


@pytest.mark.parametrize("name", ["N/x", "N|x", "N*x", "N?x", "Nä€", "N x"])
def test_b2_names_kicad_accepts_are_exported(name):
    assert serialize_symbol(name, [SymbolPin("1", "A")])


def test_b2_klc_uses_the_same_set_per_kind():
    assert k.ILLEGAL_NAME_CHARACTERS_BY_KIND["symbol"] is SYMBOL_NAME_ILLEGAL_CHARACTERS
    good = serialize_symbol("N/x", [SymbolPin("1", "A")])
    assert not [f for f in k.check_symbol_text(good) if f.code.startswith("symbol_name")], \
        "a slash is fine in a symbol name"
    bad = good.replace('"N/x', '"N<x')
    assert [f.code for f in k.check_symbol_text(bad) if f.code.startswith("symbol_name")] == ["symbol_name_illegal"]
    assert "footprint_name_illegal" in [f.code for f in k.name_findings("a/b", code="footprint_name", label="F")], \
        "a slash is still illegal in a footprint name (file name)"


def test_b2_kicad_loads_a_slash_and_refuses_a_less_than(tmp_path):
    cli = _kicad_cli()
    if not cli:
        pytest.skip("KiCad CLI not installed")
    good = export_symbol(tmp_path / "good.kicad_sym", "N/x", [SymbolPin("1", "A")])
    bad = tmp_path / "bad.kicad_sym"
    bad.write_text(good.read_text(encoding="utf-8").replace('"N/x', '"N<x'), encoding="utf-8")
    runs = {p.name: subprocess.run([cli, "sym", "export", "svg", "-o", str(tmp_path / p.stem), str(p)],
                                   capture_output=True, text=True, timeout=60).returncode
            for p in (good, bad)}
    assert runs["good.kicad_sym"] == 0, "KiCad loads a symbol name with /"
    assert runs["bad.kicad_sym"] != 0, "KiCad refuses a symbol name with <"


# ── B10: Qt overrides carry Doxygen docstrings ──────────────────────────

_QT_OVERRIDES = {"mousePressEvent", "mouseMoveEvent", "mouseReleaseEvent", "mouseDoubleClickEvent",
                 "keyPressEvent", "wheelEvent", "resizeEvent", "showEvent", "hideEvent",
                 "contextMenuEvent", "leaveEvent", "enterEvent", "drawBackground", "drawForeground",
                 "paintEvent", "eventFilter", "focusOutEvent", "closeEvent", "changeEvent"}


def _overrides():
    tree = ast.parse((ROOT / "kicadgen" / "gui" / "symbol_tab.py").read_text(encoding="utf-8"))
    for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
        for node in cls.body:
            if isinstance(node, ast.FunctionDef) and node.name in _QT_OVERRIDES:
                yield cls.name, node


def test_b10_every_qt_override_of_the_symbol_tab_has_a_brief():
    found = list(_overrides())
    assert len([1 for cls, _ in found if cls == "_SymbolView"]) >= 11, "the scan lost the view's overrides"
    missing = [f"{cls}.{node.name}" for cls, node in found
               if not (ast.get_docstring(node) or "").startswith("@brief")]
    assert not missing, missing


# ── Cycle 3 (F1-F7): the carried draft is bound to its project and durable ──

def _flow(tab, path, rows, count, name="Board", force=False):
    """What the main window does: `set_data`, then `set_recovery_path` of the project folder."""
    tab.set_data(name, rows, f"L:{name}", _ids(count), force=force)
    tab.set_recovery_path(path)


def _restart(path, rows, count, name="Board"):
    """A fresh editor on the same project (application restart)."""
    tab = SymbolTab()
    _flow(tab, path, rows, count, name)
    return tab


def test_f1_same_name_in_another_library_root_never_receives_the_draft(qapp, tmp_path):
    pa = tmp_path / "rootA" / "Board" / ".symbol-draft.json"
    pb = tmp_path / "rootB" / "Board" / ".symbol-draft.json"
    # CONTRACT CHANGED (audit L3, 2026-10-15): drafts never create the project
    # folder; a real project has one from «Nuovo progetto» on.
    pa.parent.mkdir(parents=True)
    pb.parent.mkdir(parents=True)
    other = SymbolTab()
    _flow(other, pb, _rows(), 4)
    b_baseline = other.rows()
    other.table.item(3, 1).setText("B_OWN")
    other.deleteLater()
    tab = SymbolTab()
    try:
        _flow(tab, pa, _rows(), 4)
        a_baseline = tab.rows()
        tab.table.item(0, 1).setText("A_EDIT")
        # Same name, same pins, identical inputs: `set_data` returns early.
        _flow(tab, pb, _rows(), 4)
        assert _names(tab)["1"] == "P1" and not tab.has_pending_edits(), "A's edit is not shown in B"
        assert tab.recovery_button.defaultAction().isEnabled(), "B's own draft is offered"
        tab.table.item(2, 1).setText("typing in B")
        assert "A_EDIT" not in pb.read_text(encoding="utf-8")
        assert load_draft(pa, a_baseline)[0]["name"] == "A_EDIT", "A's draft stays in A's file"
        # A forced reload with another pinout: the draft must not be rebased into B either.
        _flow(tab, pa, _rows(), 4, force=True)
        tab.table.item(0, 1).setText("A_AGAIN")
        _flow(tab, pb, _rows(p4="NEW"), 4, force=True)
        assert _names(tab)["1"] == "P1" and not tab.has_pending_edits()
        # Each project's own offered draft may be kept as a snapshot in its own
        # folder (S2, 2026-10-14): what matters is that nothing crosses over.
        for snapshot in SymbolTab.superseded_draft_paths(pb):
            assert "A_" not in snapshot.read_text(encoding="utf-8")
        for snapshot in SymbolTab.superseded_draft_paths(pa):
            text = snapshot.read_text(encoding="utf-8")
            assert "B_OWN" not in text and "typing in B" not in text
        assert "A_AGAIN" not in pb.read_text(encoding="utf-8")
        tab.undo()
        assert _names(tab)["1"] == "P1", "undo cannot reach the other project's draft"
    finally:
        tab.deleteLater()
    assert b_baseline == a_baseline


def test_f1_same_project_path_still_rebases(qapp, tmp_path):
    """Negative: the same recovery path is the same project, the draft is carried."""
    path = tmp_path / "rootA" / "Board" / ".symbol-draft.json"
    # CONTRACT CHANGED (audit L3, 2026-10-15): drafts never create the project
    # folder; a real project has one from «Nuovo progetto» on.
    path.parent.mkdir(parents=True)
    tab = SymbolTab()
    try:
        _flow(tab, path, _rows(), 4)
        tab.table.item(0, 1).setText("A_EDIT")
        _flow(tab, path, _rows(p4="NEW"), 4)
        assert _names(tab) == {"1": "A_EDIT", "2": "P2", "3": "P3", "4": "NEW"}
        assert load_draft(path, tab.rows())[0]["name"] == "A_EDIT"
    finally:
        tab.deleteLater()


def _placed(number, y):
    return {"pin": number, "name": "P" + number, "symbol_side": "left",
            "symbol_position": [-12.7, y], "symbol_angle": 0}


def _three_pins():
    return [_placed("1", 2.54), _placed("2", 0.0), _placed("3", -2.54)]


def test_f2_a_merge_that_puts_two_pins_on_one_point_is_a_conflict(qapp, tmp_path):
    path = tmp_path / ".symbol-draft.json"
    tab = SymbolTab()
    try:
        _flow(tab, path, _three_pins(), 3)
        draft = tab.draft_rows()
        draft[0]["symbol_position"] = [-12.7, -5.08]
        tab._populate(draft)
        assert tab._symbol is not None and tab.has_pending_edits(), "the draft alone is valid"
        incoming = _three_pins()
        incoming[1]["symbol_position"] = [-12.7, -5.08]
        _flow(tab, path, incoming, 3)
        assert tab._symbol is not None and not tab.has_pending_edits(), "the valid new data is shown"
        assert "disposizione non valida" in tab.status.text()
        assert tab.recovery_button.defaultAction().isEnabled()
        tab.recover_draft()
        assert tab._symbol is None, "the recovered merge shows its layout error to fix"
    finally:
        tab.deleteLater()


def test_f2_a_merge_on_distinct_points_is_rebased(qapp, tmp_path):
    """Negative: both sides move a pin, to different free points: a valid merge."""
    path = tmp_path / ".symbol-draft.json"
    tab = SymbolTab()
    try:
        _flow(tab, path, _three_pins(), 3)
        draft = tab.draft_rows()
        draft[0]["symbol_position"] = [-12.7, -5.08]
        tab._populate(draft)
        incoming = _three_pins()
        incoming[1]["symbol_position"] = [-12.7, -7.62]
        _flow(tab, path, incoming, 3)
        assert tab._symbol is not None and tab.has_pending_edits()
        assert "riportate" in tab.status.text()
        assert [r["symbol_position"] for r in tab.draft_rows()] == [[-12.7, -5.08], [-12.7, -7.62], [-12.7, -2.54]]
    finally:
        tab.deleteLater()


def test_f3_a_rebased_draft_survives_a_restart(qapp, tmp_path):
    path = tmp_path / ".symbol-draft.json"
    tab = SymbolTab()
    try:
        _flow(tab, path, _rows(), 4)
        tab.table.item(0, 1).setText("USER_EDIT")
        _flow(tab, path, _rows(p4="OTHER"), 4)
        baseline = tab.rows()
        assert load_draft(path, baseline) is not None, "bound to the NEW baseline at once"
    finally:
        tab.deleteLater()
    fresh = _restart(path, baseline, 4)
    try:
        assert fresh.recovery_button.defaultAction().isEnabled()
        fresh._changed()
        assert path.exists(), "the first redraw does not delete the offered draft"
        fresh.recover_draft()
        assert _names(fresh) == {"1": "USER_EDIT", "2": "P2", "3": "P3", "4": "OTHER"}
    finally:
        fresh.deleteLater()


def test_f3_a_kept_draft_survives_a_restart(qapp, tmp_path):
    path = tmp_path / ".symbol-draft.json"
    tab = SymbolTab()
    try:
        _flow(tab, path, _rows(), 4)
        tab.table.item(0, 1).setText("EDIT_A")
        _flow(tab, path, _rows(3), 3)
        baseline = tab.rows()
    finally:
        tab.deleteLater()
    fresh = _restart(path, baseline, 3)
    try:
        assert fresh.recovery_button.defaultAction().isEnabled()
        assert "Bozza superata" in fresh.status.text()
        fresh._changed()
        assert SymbolTab.superseded_draft_paths(path), "never deleted before recovery"
        fresh.recover_draft()
        assert _names(fresh) == {"1": "EDIT_A", "2": "P2", "3": "P3"}
        assert not SymbolTab.superseded_draft_paths(path)
    finally:
        fresh.deleteLater()
    again = _restart(path, baseline, 3)
    try:
        assert again.recovery_button.defaultAction().isEnabled(), "now the recovery file holds it"
        assert "Bozza disponibile" in again.status.text()
    finally:
        again.deleteLater()


def test_f3_without_a_draft_a_restart_offers_nothing(qapp, tmp_path):
    """Negative: pinout changes with no unapplied edit leave nothing to offer."""
    path = tmp_path / ".symbol-draft.json"
    tab = SymbolTab()
    try:
        _flow(tab, path, _rows(), 4)
        _flow(tab, path, _rows(3), 3)
        baseline = tab.rows()
    finally:
        tab.deleteLater()
    fresh = _restart(path, baseline, 3)
    try:
        assert not fresh.recovery_button.defaultAction().isEnabled()
        assert not SymbolTab.superseded_draft_paths(path) and not path.exists()
    finally:
        fresh.deleteLater()


def test_f4_superseded_drafts_are_never_overwritten_and_are_bounded(qapp, tmp_path):
    from kicadgen.gui import symbol_tab as module
    path = tmp_path / ".symbol-draft.json"
    tab = SymbolTab()
    try:
        _flow(tab, path, _rows(), 4)
        tab.table.item(0, 1).setText("VALUABLE")
        _flow(tab, path, _rows(3), 3)
        tab.table.item(1, 1).setText("OTHER_EDIT")
        _flow(tab, path, _rows(2), 2)
        files = SymbolTab.superseded_draft_paths(path)
        texts = [f.read_text(encoding="utf-8") for f in files]
        assert len(files) == 2 and "OTHER_EDIT" in texts[0] and "VALUABLE" in texts[1], \
            "a different superseded draft gets its own file, newest first"
        for count in range(module.MAX_SUPERSEDED_DRAFTS + 2):
            tab.table.item(0, 1).setText(f"E{count}")
            _flow(tab, path, _rows(2 + (count % 2)), 2 + (count % 2))
        files = SymbolTab.superseded_draft_paths(path)
        assert len(files) == module.MAX_SUPERSEDED_DRAFTS, "the oldest beyond the bound are removed"
        assert f"E{module.MAX_SUPERSEDED_DRAFTS + 1}" in files[0].read_text(encoding="utf-8")
    finally:
        tab.deleteLater()


def test_f4_an_identical_reload_writes_no_snapshot(qapp, tmp_path):
    """Negative: a forced reload with the same data carries the draft, writes nothing new."""
    path = tmp_path / ".symbol-draft.json"
    tab = SymbolTab()
    try:
        _flow(tab, path, _rows(), 4)
        tab.table.item(0, 1).setText("V1")
        _flow(tab, path, _rows(3), 3)
        [first] = SymbolTab.superseded_draft_paths(path)
        tab.table.item(1, 1).setText("V2")
        _flow(tab, path, tab.rows(), 3, force=True)
        assert SymbolTab.superseded_draft_paths(path) == [first] and "V1" in first.read_text(encoding="utf-8")
        assert _names(tab)["2"] == "V2" and tab.has_pending_edits()
    finally:
        tab.deleteLater()


def _glyph_tab(x):
    tab = SymbolTab()
    tab.resize(1200, 800)
    tab.set_data("Board", _rows(), "L:Board", _ids())
    assert tab.place_glyph(tab.glyph_combo.itemData(0), 0, 0)
    rows = tab.draft_rows()
    for row in rows:
        row["symbol_graphics"][0]["x"] = x
    tab.set_data("Board", rows, "L:Board", _ids(), force=True)
    return tab


def test_f5_a_glyph_off_the_pin_grid_lands_back_on_it(qapp):
    tab = _glyph_tab(1.27)
    try:
        tab._select_graphic(0)
        _drag(tab, 0, 2.6)
        assert _on(GRID_MM, [tab.graphics()[0]["x"], tab.graphics()[0]["y"]])
        # Led by a shape, the glyph of the group is the one snapped.
        rect = g.shape_instance("rectangle", [(5.08, 5.08), (5.08 + 3 * 0.635, 5.08 + 0.635)])
        rows = tab._with_graphics(tab.draft_rows(), [dict(tab.graphics()[0], x=1.27), rect])
        tab.set_data("Board", rows, "L:Board", _ids(), force=True)
        tab._select_graphic(0)
        tab._select_graphic(1, additive=True)
        _drag(tab, 1, 1.5)
        assert _on(GRID_MM, [tab.graphics()[0]["x"]])
    finally:
        tab.deleteLater()


def test_f5_a_shape_alone_off_the_pin_grid_keeps_its_drawing_steps(qapp):
    """Negative: without a glyph or wire nothing is pulled onto the 2.54 mm grid."""
    tab = _shape_and_glyph_tab()
    try:
        before = tab.graphics()[1]["x"]
        tab._select_graphic(1)
        _drag(tab, 1, 2.6)
        after = tab.graphics()[1]["x"]
        assert after - before == pytest.approx(4 * 0.635)
        assert not _on(GRID_MM, [after]), "the shape is not pulled onto the pin grid"
    finally:
        tab.deleteLater()


def _legacy_tab():
    tab = SymbolTab()
    tab.resize(1200, 800)
    tab.set_data("Board", _rows(), "L:Board", _ids())
    tab.place_glyph(tab.glyph_combo.itemData(0), 0, 0)
    legacy = tab.draft_rows()
    for row in legacy:
        row["symbol_graphics"] = [{k: v for k, v in x.items() if k != "id"} for x in row["symbol_graphics"]]
    tab.set_data("Board", legacy, "L:Board", _ids(), force=True)
    return tab, legacy


def test_f6_an_upstream_rename_is_not_a_graphics_conflict_in_a_legacy_project(qapp):
    tab, legacy = _legacy_tab()
    try:
        first_ids = [x["id"] for x in tab.graphics()]
        assert not tab.has_pending_edits()
        tab.place_glyph(tab.glyph_combo.itemData(0), 5.08, 5.08)
        upstream = [dict(r) for r in legacy]
        upstream[1]["name"] = "RENAMED"
        tab.set_data("Board", upstream, "L:Board", _ids())
        assert tab.has_pending_edits() and not tab.recovery_button.defaultAction().isEnabled()
        assert len(tab.graphics()) == 2 and _names(tab)["2"] == "RENAMED"
        assert [x["id"] for x in tab.graphics()][:1] == first_ids, "the legacy id is the same at every load"
    finally:
        tab.deleteLater()


def test_f6_a_real_upstream_graphics_change_is_still_a_conflict(qapp):
    """Negative: the legacy glyph itself moved upstream while the draft edited graphics."""
    tab, legacy = _legacy_tab()
    try:
        tab.place_glyph(tab.glyph_combo.itemData(0), 5.08, 5.08)
        upstream = [dict(r) for r in legacy]
        for row in upstream:
            row["symbol_graphics"] = [dict(x, x=2.54) for x in row["symbol_graphics"]]
        tab.set_data("Board", upstream, "L:Board", _ids())
        assert not tab.has_pending_edits() and tab.recovery_button.defaultAction().isEnabled()
    finally:
        tab.deleteLater()


def test_f7_kicad_tenths_and_the_legacy_files_told_by_their_header():
    assert g.text_angle_from_file(540) == pytest.approx(54), "a KiCad angle in tenths is exact"
    assert g.text_angle_from_file(180) == pytest.approx(18), "KiCad's 18 degrees, no value heuristic"
    assert g.text_angle_from_file(180, legacy_degrees=True) == pytest.approx(180), \
        "a file whose header marks kicadgen's degree writer keeps 180 degrees"
    doc = g.text_angle_from_file.__doc__
    assert doc.lstrip().startswith("@brief") and "SW_Coded_SH-7010" in doc and "header" in doc
