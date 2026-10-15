"""@brief Regression tests of the residual symbol-editor defects S1-S7 (2026-10-14).

Each fix has a positive case (the defect is gone) and a negative case that
the same rule must tell apart for a structural reason, not a number tuned
on the reproduction.
"""
import os
import shutil
import subprocess
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QPointF

from kicadgen.generators import symbol_glyphs as g
from kicadgen.generators.symbol import (SYMBOL_GENERATOR_VERSION, SymbolPin, export_symbol,
                                        layout_symbol, serialize_symbol, symbol_inputs)
from kicadgen.gui import symbol_tab as symbol_tab_module
from kicadgen.gui.symbol_tab import GRID_MM, SCENE_UNITS_PER_MM, SymbolTab
from kicadgen.services.symbol_draft import load_draft


def _rows(count=4, **names):
    return [{"pin": str(i), "name": names.get(f"p{i}", f"P{i}")} for i in range(1, count + 1)]


def _ids(count=4, first=1):
    return [str(i) for i in range(first, count + 1)]


def _names(tab):
    return {row["pin"]: row["name"] for row in tab.draft_rows()}


def _flow(tab, path, rows, ids, name="Board", force=False):
    """What the main window does: `set_data`, then `set_recovery_path` of the project folder."""
    tab.set_data(name, rows, f"L:{name}", ids, force=force)
    tab.set_recovery_path(path)


def _on(step, values):
    return all(abs(v / step - round(v / step)) < 1e-6 for v in values)


def _offered(tab):
    return tab.recovery_button.defaultAction().isEnabled()


# ── S1: wires frozen only for the SAME project, by pin identity ─────────

def _wired_rows():
    """Five applied pins and a wire anchored to pin 1 (its stored fallback = pin 1's end)."""
    tab = SymbolTab()
    tab.resize(1200, 800)
    try:
        tab.set_data("Board", _rows(5), "L:Board", _ids(5))
        assert tab.place_glyph("relay_coil", 0, 0)
        tab.start_wire()
        assert tab.add_wire_point(-5.0, 2.6) and tab.add_wire_point(0.1, 5.0)
        assert tab.finish_wire(True)
        tab.apply()
        rows = tab.rows()
        wire = next(r for r in rows[0]["symbol_graphics"] if r["key"] == "wire")
        assert wire["primitives"][0]["points"][0]["anchor"] == {"kind": "pin", "number": "1"}
        return rows
    finally:
        tab.deleteLater()


def _moved_pin_one(rows):
    rows = [dict(r) for r in rows]
    rows[0].update(symbol_side="left", symbol_position=[-15.24, 10.16], symbol_angle=0)
    return rows


def _wire_start(tab):
    """(x, y) of the wire's first point and whether it is still anchored."""
    wire = next(r for r in tab.graphics() if r["key"] == "wire")
    point = wire["primitives"][0]["points"][0]
    if isinstance(point, dict):
        return (point["x"], point["y"]), "anchor" in point
    return tuple(point), False


def _end_of_pin_one(rows):
    return g.pin_inner_ends(layout_symbol(*symbol_inputs(rows)))["1"]


def test_s1_homonymous_project_never_freezes_wires_at_the_other_layout(qapp, tmp_path):
    pa = tmp_path / "rootA" / "Board" / ".symbol-draft.json"
    pb = tmp_path / "rootB" / "Board" / ".symbol-draft.json"
    b_rows = _wired_rows()
    a_rows = _moved_pin_one(b_rows)
    b_end, a_end = _end_of_pin_one(b_rows), _end_of_pin_one(a_rows)
    assert a_end != b_end
    tab = SymbolTab()
    tab.resize(1200, 800)
    try:
        _flow(tab, pa, a_rows, _ids(5))
        # Project B (same component name, another root) no longer has pin 1.
        _flow(tab, pb, b_rows[1:], _ids(5, first=2))
        point, _anchored = _wire_start(tab)
        assert point == pytest.approx(b_end), "B's wire keeps B's own stored position"
        assert point != pytest.approx(a_end), "never A's pin position"
        assert not tab.has_pending_edits(), "B opens unmodified"
    finally:
        tab.deleteLater()


def test_s1_same_project_freezes_wires_at_its_applied_layout(qapp, tmp_path):
    """Negative: the same recovery file is the same project: the wire is frozen where pin 1 was."""
    path = tmp_path / "rootA" / "Board" / ".symbol-draft.json"
    rows = _moved_pin_one(_wired_rows())
    end = _end_of_pin_one(rows)
    tab = SymbolTab()
    tab.resize(1200, 800)
    try:
        _flow(tab, path, rows, _ids(5))
        _flow(tab, path, tab.rows()[1:], _ids(5, first=2))
        point, anchored = _wire_start(tab)
        assert point == pytest.approx(end) and not anchored, "frozen at the applied position of pin 1"
    finally:
        tab.deleteLater()


def test_s1_freezing_follows_pin_numbers_not_repeated_or_renamed_names(qapp, tmp_path):
    """Three pins named GND: only the wire of the pin NUMBER that disappeared is frozen."""
    path = tmp_path / "Board" / ".symbol-draft.json"
    rows = [dict(r, name="GND") for r in _wired_rows()]
    tab = SymbolTab()
    tab.resize(1200, 800)
    try:
        _flow(tab, path, rows, _ids(5))
        # Pin 2 (also GND) disappears and pin 1 is renamed: pin 1 keeps its wire.
        renamed = [dict(r) for r in tab.rows() if r["pin"] != "2"]
        renamed[0]["name"] = "VCC"
        _flow(tab, path, renamed, ["1", "3", "4", "5"])
        point, anchored = _wire_start(tab)
        assert anchored, "pin 1 still exists under another name: its wire stays attached"
        # Now pin 1 disappears while three other pins are still named GND.
        _flow(tab, path, [r for r in tab.rows() if r["pin"] != "1"], ["3", "4", "5"])
        point, anchored = _wire_start(tab)
        assert not anchored, "the disappeared number is frozen whatever the names of the others"
    finally:
        tab.deleteLater()


# ── S2/S3: an offered draft stays offered until recovered or discarded ──

def _restart(path, rows, ids):
    tab = SymbolTab()
    _flow(tab, path, rows, ids)
    return tab


def test_s2_editing_instead_of_recovering_keeps_the_offered_draft(qapp, tmp_path):
    path = tmp_path / ".symbol-draft.json"
    first = SymbolTab()
    _flow(first, path, _rows(), _ids())
    baseline = first.rows()
    first.table.item(0, 1).setText("DRAFT")
    first.deleteLater()
    tab = _restart(path, baseline, _ids())
    try:
        assert _offered(tab) and "Bozza disponibile" in tab.status.text()
        tab.table.item(1, 1).setText("NEW_WORK")
        assert load_draft(path, baseline)[1]["name"] == "NEW_WORK", "the edit is checkpointed"
        assert _offered(tab), "the offered draft is still offered after an edit"
        tab.recover_draft()
        assert _names(tab) == {"1": "DRAFT", "2": "P2", "3": "P3", "4": "P4"}
        assert _offered(tab), "the edits made meanwhile are recoverable in turn"
        tab.recover_draft()
        assert _names(tab) == {"1": "P1", "2": "NEW_WORK", "3": "P3", "4": "P4"}
    finally:
        tab.deleteLater()
    again = _restart(path, baseline, _ids())
    try:
        assert _offered(again), "after a restart the other draft is still there"
        again.recover_draft()
        again.recover_draft()
        assert {_names(again)["1"], _names(again)["2"]} & {"DRAFT", "NEW_WORK"}
    finally:
        again.deleteLater()


def test_s2_an_explicit_discard_retires_the_offer(qapp, tmp_path):
    """Negative: only an explicit discard removes an offered draft unrecovered."""
    path = tmp_path / ".symbol-draft.json"
    first = SymbolTab()
    _flow(first, path, _rows(), _ids())
    baseline = first.rows()
    first.table.item(0, 1).setText("DRAFT")
    first.deleteLater()
    tab = _restart(path, baseline, _ids())
    try:
        assert tab.discard_draft_action.isEnabled()
        assert tab.discard_draft()
        assert not _offered(tab) and not tab.discard_draft_action.isEnabled()
        assert not path.exists() and not SymbolTab.superseded_draft_paths(path)
        tab.table.item(1, 1).setText("NEW_WORK")
        assert not SymbolTab.superseded_draft_paths(path), "nothing offered: an edit writes no snapshot"
        assert not tab.discard_draft(), "nothing left to discard"
    finally:
        tab.deleteLater()
    again = _restart(path, baseline, _ids())
    try:
        again.recover_draft()
        assert _names(again)["1"] == "P1" and _names(again)["2"] == "NEW_WORK", "the discarded draft is gone"
    finally:
        again.deleteLater()


def test_s3_snapshot_is_offered_after_the_recovery_file_draft(qapp, tmp_path):
    path = tmp_path / ".symbol-draft.json"
    tab = SymbolTab()
    try:
        _flow(tab, path, _rows(), _ids())
        tab.table.item(0, 1).setText("V1")
        _flow(tab, path, _rows(3), _ids(3))          # V1 kept as a superseded snapshot
        tab.table.item(1, 1).setText("V2")           # the recovery file now holds V2
        baseline = tab.rows()
        assert len(SymbolTab.superseded_draft_paths(path)) == 1
    finally:
        tab.deleteLater()
    fresh = _restart(path, baseline, _ids(3))
    try:
        assert _offered(fresh) and "Bozza disponibile" in fresh.status.text(), "the recovery file first"
        fresh.recover_draft()
        assert _names(fresh) == {"1": "P1", "2": "V2", "3": "P3"}
        assert _offered(fresh), "the superseded snapshot is offered too"
        fresh.recover_draft()
        assert _names(fresh)["1"] == "V1", "the snapshot carried onto the current data"
    finally:
        fresh.deleteLater()


def test_s3_nothing_is_offered_when_every_draft_is_already_shown(qapp, tmp_path):
    """Negative: a snapshot equal to the editor content or to the baseline is not offered."""
    path = tmp_path / ".symbol-draft.json"
    tab = SymbolTab()
    try:
        _flow(tab, path, _rows(), _ids())
        tab.table.item(0, 1).setText("V1")
        _flow(tab, path, _rows(3), _ids(3))
        tab.recover_draft()
        assert _names(tab)["1"] == "V1"
        assert not _offered(tab), "the only draft is the one shown"
        tab.table.item(0, 1).setText("P1")           # back to the applied data by hand
        assert not _offered(tab)
    finally:
        tab.deleteLater()


# ── S4: a moved project folder is the same project ─────────────────────

def test_s4_a_moved_project_keeps_its_draft_in_the_editor(qapp, tmp_path):
    pa = tmp_path / "rootA" / "Board" / ".symbol-draft.json"
    pc = tmp_path / "rootC" / "Board" / ".symbol-draft.json"
    # CONTRACT CHANGED (audit L3, 2026-10-15): drafts never create the project
    # folder; a real project has one from «Nuovo progetto» on.
    pa.parent.mkdir(parents=True)
    tab = SymbolTab()
    try:
        _flow(tab, pa, _rows(), _ids())
        baseline = tab.rows()
        tab.table.item(0, 1).setText("MOVED_EDIT")
        pc.parent.parent.mkdir(parents=True)
        shutil.move(str(pa.parent), str(pc.parent))
        _flow(tab, pc, _rows(), _ids())
        assert _names(tab)["1"] == "MOVED_EDIT" and tab.has_pending_edits()
        tab.table.item(1, 1).setText("MORE")
        assert load_draft(pc, baseline)[1]["name"] == "MORE", "checkpointed in the new folder"
        # A pinout change right after a move carries the draft as in the same folder.
        pd = tmp_path / "rootD" / "Board" / ".symbol-draft.json"
        pd.parent.parent.mkdir(parents=True)
        shutil.move(str(pc.parent), str(pd.parent))
        _flow(tab, pd, _rows(p4="NEW"), _ids())
        assert _names(tab) == {"1": "MOVED_EDIT", "2": "MORE", "3": "P3", "4": "NEW"}
        assert load_draft(pd, tab.rows())[0]["name"] == "MOVED_EDIT"
    finally:
        tab.deleteLater()


def test_s4_a_copied_project_is_another_project(qapp, tmp_path):
    """Negative: when the old folder still holds the same id it was copied, not moved."""
    pa = tmp_path / "rootA" / "Board" / ".symbol-draft.json"
    pc = tmp_path / "rootC" / "Board" / ".symbol-draft.json"
    # CONTRACT CHANGED (audit L3, 2026-10-15): drafts never create the project
    # folder; a real project has one from «Nuovo progetto» on.
    pa.parent.mkdir(parents=True)
    tab = SymbolTab()
    try:
        _flow(tab, pa, _rows(), _ids())
        baseline = tab.rows()
        tab.table.item(0, 1).setText("ORIGINAL_EDIT")
        shutil.copytree(pa.parent, pc.parent)
        _flow(tab, pc, _rows(p4="NEW"), _ids())
        assert _names(tab)["1"] == "P1" and _names(tab)["4"] == "NEW"
        assert not tab.has_pending_edits()
        assert load_draft(pa, baseline)[0]["name"] == "ORIGINAL_EDIT", "the original keeps its draft"
        assert not SymbolTab.superseded_draft_paths(pc), "nothing carried into the copy"
    finally:
        tab.deleteLater()


# ── S5/S7: every member of a group lands on its own grid ───────────────

def _drag(tab, index, dx_mm, dy_mm=0.0):
    tab._begin_gesture(("graphic", index), QPointF(0, 0))
    tab._move_gesture(("graphic", index), QPointF(dx_mm * SCENE_UNITS_PER_MM, -dy_mm * SCENE_UNITS_PER_MM))
    tab._end_gesture(True)


def _corners(record):
    return [c for point in g.placed_primitives(record)[0].points for c in point]


def _group_tab(glyph_x):
    tab = SymbolTab()
    tab.resize(1200, 800)
    tab.set_data("Board", _rows(5), pad_ids=_ids(5))
    assert tab.place_glyph("relay_coil", 0, 0)
    glyph = dict(tab.graphics()[0], x=glyph_x)
    rect = g.shape_instance("rectangle", [(5.08, 5.08), (5.08 + 3 * 0.635, 5.08 + 0.635)])
    tab.set_data("Board", tab._with_graphics(tab.draft_rows(), [glyph, rect]), pad_ids=_ids(5), force=True)
    tab.set_drawing_grid(0.635)
    tab._select_graphic(0)
    tab._select_graphic(1, additive=True)
    return tab


@pytest.mark.parametrize("leader", [0, 1])
def test_s5_an_off_grid_glyph_does_not_drag_shapes_off_their_grid(qapp, leader):
    tab = _group_tab(0.3)
    try:
        _drag(tab, leader, 2.6, 0.0)
        glyph, rect = tab.graphics()
        assert _on(GRID_MM, [glyph["x"], glyph["y"]]), "the glyph lands on the pin grid"
        assert _on(0.635, _corners(rect)), "the shape keeps its corners on the drawing grid"
    finally:
        tab.deleteLater()


def test_s5_an_on_grid_group_moves_rigidly(qapp):
    """Negative: with every member on its grid the group keeps its shape."""
    tab = _group_tab(0.0)
    try:
        before = tab.graphics()
        _drag(tab, 1, 2.6, 2.4)
        after = tab.graphics()
        delta_glyph = (after[0]["x"] - before[0]["x"], after[0]["y"] - before[0]["y"])
        delta_rect = (after[1]["x"] - before[1]["x"], after[1]["y"] - before[1]["y"])
        assert delta_glyph == pytest.approx((2.54, 2.54))
        assert delta_rect == pytest.approx(delta_glyph)
    finally:
        tab.deleteLater()


def _terminal_points(record):
    return [(t.x, t.y) for t in g.placed_terminals(record)]


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_s7_half_pitch_terminals_land_on_the_pin_grid(qapp, rotation):
    tab = SymbolTab()
    tab.resize(1200, 800)
    try:
        tab.set_data("Board", _rows(5), pad_ids=_ids(5))
        assert tab.place_glyph("resistor", 0.3, 0.2, rotation=rotation)
        resistor = tab.graphics()[0]
        assert g.placed_terminals(dict(resistor, x=0, y=0))[0].y in (3.81, -3.81) or rotation % 180, \
            "the glyph geometry is unchanged (terminals at 3.81 mm from the origin)"
        points = _terminal_points(resistor)
        assert _on(GRID_MM, [c for p in points for c in p]), points
        _drag(tab, 0, 5.3, -2.2)
        points = _terminal_points(tab.graphics()[0])
        assert _on(GRID_MM, [c for p in points for c in p]), "still on the grid after a drag"
        tab._select_graphic(0)
        assert tab._edit_selected_graphics("rotate", 90)
        points = _terminal_points(tab.graphics()[0])
        assert _on(GRID_MM, [c for p in points for c in p]), "and after a quarter turn"
        tx, ty = points[0]
        assert tab.place_pin("1", tx, ty)
        pin = next(p for p in tab._symbol.pins if p.pin.number == "1")
        assert (pin.x, pin.y) == pytest.approx((tx, ty)), "a pin dropped on the terminal sits on it"
    finally:
        tab.deleteLater()


def test_s7_on_grid_terminals_keep_the_origin_on_the_grid(qapp):
    """Negative: terminals a whole pitch from the origin introduce no offset."""
    tab = SymbolTab()
    tab.resize(1200, 800)
    try:
        tab.set_data("Board", _rows(5), pad_ids=_ids(5))
        assert tab.place_glyph("relay_coil", 0.3, 0.2)
        coil = tab.graphics()[0]
        assert (coil["x"], coil["y"]) == (0.0, 0.0)
        no_terminals = {"key": "relay_link", "x": 0.0, "y": 0.0, "rotation": 0, "mirror": False,
                        "primitives": [], "terminals": []}
        assert symbol_tab_module.snap_glyph_origin(no_terminals, 1.4, -1.4) == (2.54, -2.54)
        imported = {"key": "imported", "x": 0.0, "y": 0.0, "rotation": 90, "mirror": False, "primitives": [],
                    "terminals": [{"x": 1.27, "y": 0.0, "angle": 180, "length": 2.54}]}
        x, y = symbol_tab_module.snap_glyph_origin(imported, 0.0, 0.0)
        terminal = g.placed_terminals(dict(imported, x=x, y=y))[0]
        assert _on(GRID_MM, [terminal.x, terminal.y]), "an imported glyph uses its own terminal"
    finally:
        tab.deleteLater()


def test_s7_an_old_project_glyph_is_not_moved_on_load(qapp):
    """Data compatibility: a resistor saved with its origin on the grid stays where it was."""
    tab = SymbolTab()
    tab.resize(1200, 800)
    try:
        tab.set_data("Board", _rows(5), pad_ids=_ids(5))
        assert tab.place_glyph("resistor", 0, 0)
        old = dict(tab.graphics()[0], x=0.0, y=0.0)
        tab.set_data("Board", tab._with_graphics(tab.draft_rows(), [old]), pad_ids=_ids(5), force=True)
        assert (tab.graphics()[0]["x"], tab.graphics()[0]["y"]) == (0.0, 0.0)
        assert not tab.has_pending_edits()
    finally:
        tab.deleteLater()


# ── S6: text angle units decided by the file header ────────────────────

def _library(generator, version, angle):
    return (f'(kicad_symbol_lib (version 20251024) (generator "{generator}") (generator_version "{version}")\n'
            f'  (symbol "T" (symbol "T_0_1" (text "X" (at 0 0 {angle}) (effects (font (size 1.27 1.27)))))))\n')


def _text_angle(tmp_path, text):
    library = tmp_path / "t.kicad_sym"
    library.write_text(text, encoding="utf-8")
    g._library_text.cache_clear()
    glyph = g.load_library_glyph(library, "T")
    return next(p for p in glyph.primitives if p.kind == "text").angle


def test_s6_a_kicad_text_at_9_degrees_is_read_as_9(tmp_path):
    assert _text_angle(tmp_path, _library("kicad_symbol_editor", "10.0", 90)) == 9
    assert _text_angle(tmp_path, _library("kicadgen", SYMBOL_GENERATOR_VERSION, 900)) == 90
    assert g.text_angle_from_file(90) == pytest.approx(9)


@pytest.mark.parametrize("written, degrees", [
    (90, 90), (180, 180), (270, 270),          # bc01ca2-shaped file: degrees, quarter turns only
    (900, 90), (1800, 180), (2700, 270),       # af3a92a-shaped file: tenths under the same header
    (0, 0), (540, 54)])                        # 0 is 0 for both; anything else is tenths
def test_s6_both_writers_of_the_ambiguous_header_read_right(tmp_path, written, degrees):
    assert _text_angle(tmp_path, _library("kicadgen", "10.0", written)) == degrees


def test_s6_af3a92a_vertical_text_is_vertical(tmp_path):
    """The writer of af3a92a (tenths, header 10.0) serialised a vertical text as 900."""
    rec = g.shape_instance("text", [(0.0, 0.0)], text="VERT")
    rec["rotation"] = 90
    text = serialize_symbol("T", [SymbolPin("1", "A")], graphics=[rec]).replace(
        f'(generator_version "{SYMBOL_GENERATOR_VERSION}")', '(generator_version "10.0")')
    assert "(at 0 0 900)" in text
    library = tmp_path / "t.kicad_sym"
    library.write_text(text, encoding="utf-8")
    g._library_text.cache_clear()
    glyph = g.load_library_glyph(library, "T")
    assert next(p for p in glyph.primitives if p.kind == "text").angle == 90


def test_s6_a_legacy_kicadgen_file_is_read_in_degrees(tmp_path):
    """Negative: only the legacy header opens the quarter-turn fallback."""
    assert _text_angle(tmp_path, _library("kicadgen", "10.0", 90)) == 90
    assert _text_angle(tmp_path, _library("kicadgen", SYMBOL_GENERATOR_VERSION, 90)) == 9
    assert _text_angle(tmp_path, _library("kicad_symbol_editor", "10.0", 1800)) == 180
    assert g.text_angle_from_file(90, legacy_degrees=True) == 90
    assert g.text_angles_in_degrees(_library("kicadgen", "10.0", 0))
    assert not g.text_angles_in_degrees(_library("kicadgen", SYMBOL_GENERATOR_VERSION, 0))
    assert not g.text_angles_in_degrees(_library("kicad_symbol_editor", "10.0", 0))


def test_s6_the_writer_never_emits_the_legacy_marker():
    assert SYMBOL_GENERATOR_VERSION != g.LEGACY_DEGREES_GENERATOR[1]
    text = serialize_symbol("M", [SymbolPin("1", "A")])
    assert not g.text_angles_in_degrees(text)
    assert f'(generator_version "{SYMBOL_GENERATOR_VERSION}")' in text


def test_s6_kicad_loads_the_new_header(tmp_path):
    cli = shutil.which("kicad-cli") or next(
        (str(p) for p in sorted(Path("C:/Program Files/KiCad").glob("*/bin/kicad-cli.exe"), reverse=True)), None)
    if not cli:
        pytest.skip("KiCad CLI not installed")
    path = export_symbol(tmp_path / "t.kicad_sym", "T", [SymbolPin("1", "A")])
    result = subprocess.run([cli, "sym", "export", "svg", "-o", str(tmp_path / "svg"), str(path)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_s4_binding_a_project_without_a_folder_creates_nothing(qapp, tmp_path):
    """The project id is written with the first draft, never by binding alone.

    CONTRACT CHANGED (audit L3, 2026-10-15): the first draft does not create
    the folder either (a renamed or deleted project came back as a ghost
    folder); once the folder exists, the id is written beside the draft.
    """
    path = tmp_path / "root" / "Board" / ".symbol-draft.json"
    tab = SymbolTab()
    try:
        _flow(tab, path, _rows(), _ids())
        assert not path.parent.exists(), "binding does not make a folder appear in the workspace"
        tab.table.item(0, 1).setText("EDIT")
        assert not path.parent.exists(), "nor does a draft"
        path.parent.mkdir(parents=True)
        tab.table.item(1, 1).setText("EDIT2")
        assert path.is_file()
        assert SymbolTab.project_id_path(path).is_file(), "written beside the first draft"
    finally:
        tab.deleteLater()


def test_s4_binding_an_existing_folder_without_a_draft_writes_nothing(qapp, tmp_path):
    """Opening a project must not touch its folder (recent-project order uses its mtimes)."""
    folder = tmp_path / "root" / "Board"
    folder.mkdir(parents=True)
    (folder / "project.json").write_text("{}", encoding="utf-8")
    before = sorted(p.name for p in folder.iterdir())
    path = folder / ".symbol-draft.json"
    tab = SymbolTab()
    try:
        _flow(tab, path, _rows(), _ids())
        tab._changed()
        assert sorted(p.name for p in folder.iterdir()) == before, "no id, no draft written by binding"
        tab.table.item(0, 1).setText("EDIT")
        assert SymbolTab.project_id_path(path).is_file(), "the id comes with the first draft"
    finally:
        tab.deleteLater()


# ── Review of 2026-10-14: dialog confirmed unchanged, best reference terminal ──

def _old_resistor_tab():
    tab = SymbolTab()
    tab.resize(1200, 800)
    tab.set_data("Board", _rows(5), pad_ids=_ids(5))
    assert tab.place_glyph("resistor", 0, 0)
    old = dict(tab.graphics()[0], x=0.0, y=0.0)
    tab.set_data("Board", tab._with_graphics(tab.draft_rows(), [old]), pad_ids=_ids(5), force=True)
    assert not tab.has_pending_edits()
    return tab


def test_s7_properties_confirmed_unchanged_do_not_move_an_old_glyph(qapp):
    tab = _old_resistor_tab()
    try:
        assert tab.set_element_geometry(0, 0.0, 0.0, rotation=0, mirror=False)
        assert tab.set_element_geometry(0, 0.0, 0.0)
        assert (tab.graphics()[0]["x"], tab.graphics()[0]["y"]) == (0.0, 0.0)
        assert not tab.has_pending_edits()
    finally:
        tab.deleteLater()


def test_s7_properties_with_a_new_position_snap_the_terminals(qapp):
    """Negative: a real edit in the dialog is a placement and snaps by the terminals."""
    tab = _old_resistor_tab()
    try:
        assert tab.set_element_geometry(0, 2.54, 0.0)
        points = _terminal_points(tab.graphics()[0])
        assert _on(GRID_MM, [c for p in points for c in p]) and tab.has_pending_edits()
    finally:
        tab.deleteLater()


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_s7_potentiometer_puts_the_most_terminals_on_the_grid(qapp, rotation):
    record = g.instance_from_glyph(next(x for x in g.catalogue() if x.key == "potentiometer"), 0, 0, rotation)
    x, y = symbol_tab_module.snap_glyph_origin(record, 0.4, -0.3)
    points = _terminal_points(dict(record, x=x, y=y))
    on_grid = [p for p in points if _on(GRID_MM, p)]
    assert len(on_grid) == 2 and len(points) == 3, "both ends on the grid, the wiper cannot be"


@pytest.mark.parametrize("key", ["resistor", "capacitor", "inductor", "diode", "led", "crystal"])
@pytest.mark.parametrize("rotation", [0, 90])
def test_s7_two_terminal_glyphs_keep_every_terminal_on_the_grid(key, rotation):
    record = g.instance_from_glyph(next(x for x in g.catalogue() if x.key == key), 0, 0, rotation)
    x, y = symbol_tab_module.snap_glyph_origin(record, 1.1, 0.7)
    assert _on(GRID_MM, [c for p in _terminal_points(dict(record, x=x, y=y)) for c in p])


def test_s7_reference_is_not_simply_the_first_terminal():
    """An imported glyph listing its odd terminal first: the majority group still lands on the grid."""
    record = {"key": "imported", "x": 0.0, "y": 0.0, "rotation": 0, "mirror": False, "primitives": [],
              "terminals": [{"x": 3.81, "y": 0.0, "angle": 180, "length": 2.54},
                            {"x": 0.0, "y": 3.81, "angle": 270, "length": 2.54},
                            {"x": 0.0, "y": -3.81, "angle": 90, "length": 2.54}]}
    x, y = symbol_tab_module.snap_glyph_origin(record, 0.0, 0.0)
    points = _terminal_points(dict(record, x=x, y=y))
    assert [_on(GRID_MM, p) for p in points] == [False, True, True]
