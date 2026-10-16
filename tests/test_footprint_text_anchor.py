"""@brief Bottom-left anchor of the texts drawn on the footprint preview (user, 2026-10-14).

A user text grows right and up from the clicked node: its handle square,
its grid snap and the ``fp_text`` written to the .kicad_mod (``(justify left
bottom)``) all sit on the bottom-left corner. Texts saved before (centred on
their point) are migrated once on load so they stay where the user saw them.
REF**/Value, the ``${REFERENCE}`` fabrication text and the other drawing
kinds are not touched.
"""
import re
import subprocess
from copy import deepcopy

import pytest

from kicadgen.core.footprint import (
    FpText, Footprint, USER_TEXT_ANCHOR, drawing_graphics, fab_reference_text,
    migrate_centred_text, validate_footprint_drawings,
)
from kicadgen.services.system_dependencies import find_kicad_cli


def record(kind="text", points=((1, 1),), layer="F.SilkS", **extra):
    """@brief A raw drawing record; a text gets "HH" at 1 mm unless told otherwise."""
    base = {"kind": kind, "layer": layer, "width": 0.12,
            "points": [list(p) for p in points]}
    if kind == "text":
        base.update(text="HH", size=1.0)
    base.update(extra)
    return base


# ── Core: record, validation and file output ─────────────────────────

def test_an_anchored_text_is_kept_and_written_left_bottom_at_its_point():
    clean = validate_footprint_drawings([record(points=((2.5, -1.5),), anchor=USER_TEXT_ANCHOR)])
    assert clean[0]["anchor"] == USER_TEXT_ANCHOR
    (text,) = drawing_graphics(clean)
    # The writer pretty-prints: compare with the whitespace collapsed.
    rendered = re.sub(r"\s+", " ", str(text.to_sexpr())).replace("( ", "(").replace(" )", ")")
    assert "(at 2.5 -1.5)" in rendered
    assert ("(effects (font (size 1 1) (thickness 0.15)) (justify left bottom))"
            in rendered), rendered


def test_a_legacy_text_stays_centred_and_an_unknown_anchor_is_dropped():
    """Negative: no anchor is a legacy centred record (no justify); a foreign value is untrusted."""
    (legacy,) = validate_footprint_drawings([record()])
    assert "anchor" not in legacy
    assert "justify" not in str(drawing_graphics([legacy])[0].to_sexpr())
    assert validate_footprint_drawings([record(anchor="centre")]) == []
    assert validate_footprint_drawings([record(anchor=1)]) == []


def test_reference_value_fab_text_and_other_kinds_write_no_justify():
    """Negative: only the user text drawings change; every other text keeps KiCad's centring."""
    fp = Footprint(name="X")
    fp.add_graphic(fab_reference_text((0.0, 0.0), (3.0, 2.0)))
    for item in drawing_graphics(validate_footprint_drawings([
            record("line", ((0, 0), (1, 1))), record("rectangle", ((0, 0), (2, 1))),
            record("circle", ((0, 0), (1, 0))), record("arc", ((0, 0), (1, 1), (2, 0)))])):
        fp.add_graphic(item)
    assert "justify" not in fp.render()
    default = FpText(kind="user", text="T", at=(0, 0), layer="F.Fab")
    assert default.justify is None and "justify" not in str(default.to_sexpr())


def test_migration_moves_only_legacy_texts_and_is_idempotent():
    legacy = validate_footprint_drawings([record(points=((1.0, 1.0),))])[0]
    migrated = migrate_centred_text(legacy, (-0.75, 0.6))
    assert migrated["points"] == [[0.25, 1.6]] and migrated["anchor"] == USER_TEXT_ANCHOR
    assert migrate_centred_text(migrated, (-0.75, 0.6)) == migrated, "migrated once"
    assert validate_footprint_drawings([migrated]) == [migrated], "the validator keeps it"
    line = validate_footprint_drawings([record("line", ((0, 0), (1, 1)))])[0]
    assert migrate_centred_text(line, (-1, 1)) == line, "other kinds are not touched"
    # A corner beyond the coordinate bound keeps the legacy (still valid) record.
    edge = validate_footprint_drawings([record(points=((-499.9, 0),))])[0]
    assert migrate_centred_text(edge, (-0.5, 0.5)) == edge


def _svg_text_extent(svg: str, text: str, ref_x: float, ref_y: float):
    """@brief Stroke extent of `text` relative to the reference cross at (ref_x, ref_y) mm.

    The exported SVG is cropped (shifted): the cross drawn on the same layer
    at a known footprint point gives the offset back.
    """
    start = svg.index(f"<desc>{text}</desc>")
    group = svg[start:svg.index("</g>", start)]
    rest = svg.replace(group, "")
    numbers = [(float(a), float(b)) for a, b in re.findall(r"[ML]([-\d.]+) ([-\d.]+)", rest)]
    xs = sorted({p[0] for p in numbers})
    ys = sorted({p[1] for p in numbers})
    # The cross: a horizontal and a vertical 1 mm segment meeting at the reference.
    off_x = next(x for x in xs if sum(1 for p in numbers if p[0] == x) >= 2) - ref_x
    off_y = next(y for y in ys if sum(1 for p in numbers if p[1] == y) >= 2) - ref_y
    points = [(float(a) - off_x, float(b) - off_y)
              for a, b in re.findall(r"[ML]([-\d.]+) ([-\d.]+)", group)]
    return (min(p[0] for p in points), max(p[0] for p in points),
            min(p[1] for p in points), max(p[1] for p in points))


def test_kicad_draws_the_anchored_text_right_of_and_above_its_point(tmp_path):
    """kicad-cli read-only export of a copy: the strokes start at the anchor and sit above it."""
    cli = find_kicad_cli()
    if not cli:
        pytest.skip("KiCad CLI not installed")
    library = tmp_path / "anchor.pretty"
    library.mkdir()
    fp = Footprint(name="ANCHOR")
    records = validate_footprint_drawings([
        record(points=((5, 5),), layer="Cmts.User", anchor=USER_TEXT_ANCHOR, size=1.5),
        # Reference cross 3 mm left of the anchor, on the same layer.
        record("line", ((1.5, 5), (2.5, 5)), layer="Cmts.User"),
        record("line", ((2, 4.5), (2, 5.5)), layer="Cmts.User")])
    for item in drawing_graphics(records):
        fp.add_graphic(item)
    (library / "ANCHOR.kicad_mod").write_text(fp.render(), encoding="utf-8")
    out = tmp_path / "svg"
    out.mkdir()
    subprocess.run([cli, "fp", "export", "svg", "--fp", "ANCHOR", "--layers", "Cmts.User",
                    "-o", str(out), str(library)], capture_output=True, timeout=120,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    svgs = list(out.glob("*.svg"))
    assert svgs, "kicad-cli exported the footprint"
    left, right, top, bottom = _svg_text_extent(svgs[0].read_text(encoding="utf-8"), "HH", 2, 5)
    assert 5.0 <= left < 5.0 + 1.5, "the text starts at the anchor, within one character"
    assert right > left + 1.5, "it grows to the right"
    assert bottom <= 5.0 and top < 5.0 - 1.0, "it sits above the anchor (cap height 1.5 mm)"


# ── Window: drawing, handle, snap, migration, edits ──────────────────

pytest.importorskip("PyQt6")


@pytest.fixture
def preview(window, monkeypatch):
    """The window with its preview drawn; saves kept in memory, regenerations recorded."""
    generations = []
    monkeypatch.setattr(window, "_generate", lambda **kwargs: generations.append(kwargs) or True)
    monkeypatch.setattr(window, "_project_has_footprint", lambda: True)
    monkeypatch.setattr(window, "_schedule_footprint_rewrite",
                        lambda: window._generate(footprint_only=True))
    window._draw_preview()
    return window


def _text_item(window, index=0):
    from PyQt6.QtWidgets import QGraphicsSimpleTextItem
    from kicadgen.gui.footprint_view import DRAWING_KEY_ROLE
    return next(i for i in window.preview_scene.items()
                if isinstance(i, QGraphicsSimpleTextItem) and i.data(DRAWING_KEY_ROLE) == index)


def _scene_baseline(item):
    """Scene y (px) of a drawn text item's baseline (its layout ascent, scaled with the item)."""
    from kicadgen.gui.main_window import _drawing_text_baseline
    return item.pos().y() + _drawing_text_baseline(item) * item.scale()


def _cells(text, size=1.0):
    """(along, up) scene px from a bottom-left anchor to where KiCad lays the text's cells and baseline.

    CONTRACT CHANGED 2026-10-16 (user: KiCad's width and box,
    gui/kicad_text_item.py): the preview draws a drawn text as KiCad does,
    cells half the pen margin right of the anchor and the baseline
    0.125 x size above it, no longer the layout edge and baseline on it.
    """
    from kicadgen.core.kicad_text import kicad_text_cells
    from kicadgen.gui.main_window import _drawn_text_thickness
    along, up = kicad_text_cells(text, size, _drawn_text_thickness())
    return along * 20, up * 20


def _handles(window):
    from kicadgen.gui.footprint_view import HANDLE_KEY_ROLE
    return [i for i in window.preview_scene.items() if i.data(HANDLE_KEY_ROLE) is not None]


def test_a_new_text_is_anchored_on_the_snapped_click_at_its_bottom_left(preview, monkeypatch):
    from kicadgen.gui import main_window as gui
    window = preview
    monkeypatch.setattr(gui.QInputDialog, "getText", staticmethod(lambda *a, **k: ("HH", True)))
    window.chk_grid.setChecked(True)
    window.combo_grid_unit.setCurrentIndex(window.combo_grid_unit.findData("mm"))
    window.preview_view.set_grid(20.0, (0.0, 0.0), True)
    window._toggle_footprint_draw_tool("text")
    window._on_footprint_draw_clicked(2.03 * 20, 3.06 * 20)
    (stored,) = window.footprint_drawings
    assert stored["points"] == [[2.0, 3.1]], "the anchor is the snapped node"
    assert stored["anchor"] == USER_TEXT_ANCHOR
    from kicadgen.gui.main_window import _drawing_text_baseline
    item = _text_item(window)
    box = item.sceneBoundingRect()
    along, up = _cells("HH")
    assert box.left() == pytest.approx(2.0 * 20 + along, abs=0.01)
    baseline = _scene_baseline(item)
    assert baseline == pytest.approx(3.1 * 20 - up, abs=0.01), "the baseline sits where KiCad's does"
    assert box.right() > box.left() and box.top() < 3.1 * 20, "grows right and up"
    window._on_footprint_drawing_hovered(0)
    (square,) = _handles(window)
    assert (square.pos().x(), square.pos().y()) == pytest.approx((2.0 * 20, 3.1 * 20))


def test_descenders_go_below_the_anchor_like_kicad_and_the_symbol_editor(preview):
    """Positive/negative pair: "gjpq" reaches below the anchor, the box of "HH" ends near it."""
    window = preview
    window.footprint_drawings = validate_footprint_drawings([
        record(points=((1, 5),), text="gjpq", anchor=USER_TEXT_ANCHOR),
        record(points=((1, 9),), text="HH", anchor=USER_TEXT_ANCHOR)])
    window._draw_preview()
    from PyQt6.QtGui import QFontMetricsF
    from kicadgen.gui.main_window import _drawing_text_baseline
    item = _text_item(window, 0)
    descending = item.sceneBoundingRect()
    along, up = _cells("gjpq")
    assert descending.left() == pytest.approx(20 + along, abs=0.01)
    assert _scene_baseline(item) == pytest.approx(5 * 20 - up, abs=0.01)
    # The box reaches below the baseline by the font's descent (descenders),
    # above it by the ascent. The test platform's stub font may have no
    # descent: the identity holds either way, and is > 0 with real fonts.
    descent = QFontMetricsF(item.font()).descent() * item.scale()
    assert descending.bottom() - _scene_baseline(item) == pytest.approx(descent, abs=0.5)
    assert descending.top() < 5 * 20
    capitals = _text_item(window, 1)
    assert capitals.sceneBoundingRect().top() < 9 * 20
    assert _scene_baseline(capitals) == pytest.approx(9 * 20 - _cells("HH")[1], abs=0.01)


def test_a_drawn_capital_is_as_tall_as_the_text_size_like_kicad(preview):
    """KiCad's text size is the cap height: an 'H' of 1.5 mm is 1.5 mm tall in the preview."""
    from kicadgen.gui.main_window import _cap_height_px
    window = preview
    for size in (0.8, 1.0, 1.5, 3.0):
        window.footprint_drawings = validate_footprint_drawings([
            record(points=((1, 5),), text="H", size=size, anchor=USER_TEXT_ANCHOR)])
        window._draw_preview()
        item = _text_item(window)
        # The outline of «H» in the item, scaled to the scene.
        assert _cap_height_px(item.font()) * item.scale() == pytest.approx(size * 20, rel=0.03)
        # CONTRACT CHANGED 2026-10-16 (user): stretched to KiCad's width by
        # the item's transform, not by the font (whose stretch stays plain).
        from kicadgen.core.kicad_text import kicad_advance_width
        assert item.font().stretch() in (0, 100)
        assert item.sceneBoundingRect().width() == pytest.approx(kicad_advance_width("H", size) * 20, abs=0.01)


def test_a_legacy_text_keeps_its_drawn_corner_through_the_migration_on_load(preview):
    """The corner the user saw (left edge, baseline of the OLD em-based font) is kept."""
    from kicadgen.gui.main_window import _drawing_text_baseline, _legacy_drawing_text_item
    window = preview
    legacy = validate_footprint_drawings([record(points=((3.0, 2.0),), text="2q3qe")])
    old = _legacy_drawing_text_item(legacy[0], 20.0)
    old_rect = old.boundingRect()
    old_left = 3.0 * 20 - old_rect.width() / 2 + old_rect.left()
    old_baseline = 2.0 * 20 - old_rect.height() / 2 + _drawing_text_baseline(old)
    state = deepcopy(window._project_state())
    state["footprint_drawings"] = deepcopy(legacy) + [record("line", ((0, 0), (1, 1)))]
    window._apply_project_state(state)
    migrated = window.footprint_drawings[0]
    assert migrated["anchor"] == USER_TEXT_ANCHOR
    assert window.footprint_drawings[1] == validate_footprint_drawings(
        [record("line", ((0, 0), (1, 1)))])[0], "other kinds are loaded unchanged"
    assert migrated["points"][0] == pytest.approx([old_left / 20, old_baseline / 20], abs=0.001)
    window._draw_preview()
    item = _text_item(window)
    # The migration keeps the ANCHOR on the corner the user saw; the text
    # is now drawn from it as KiCad draws (see `_cells`).
    along, up = _cells("2q3qe")
    assert abs(item.sceneBoundingRect().left() - along - old_left) < 1.0, "within a pixel"
    assert abs(_scene_baseline(item) + up - old_baseline) < 1.0
    # Loading again what the window now saves changes nothing (migrated once).
    window._apply_project_state(deepcopy(window._project_state()))
    assert window.footprint_drawings[0] == migrated


def test_drag_duplicate_edit_and_undo_keep_the_bottom_left_anchor(preview):
    window = preview
    window.chk_grid.setChecked(False)
    window.footprint_drawings = validate_footprint_drawings(
        [record(points=((1, 1),), anchor=USER_TEXT_ANCHOR)])
    window._draw_preview()
    # Whole drag: grab the text body, drop 2 mm right.
    window._on_footprint_handle_pressed(0, -1, 1.5 * 20, 0.8 * 20)
    window._on_footprint_handle_dropped(0, -1, 3.5 * 20, 0.8 * 20)
    assert window.footprint_drawings[0]["points"] == [[3.0, 1.0]]
    assert window.footprint_drawings[0]["anchor"] == USER_TEXT_ANCHOR
    # Duplicate keeps the anchor mark.
    window._duplicate_drawing(0)
    assert window.footprint_drawings[1]["anchor"] == USER_TEXT_ANCHOR
    # Editing the text keeps the corner where it is: the text grows to the right.
    window._set_drawing_field(0, "text", "LONGER TEXT")
    assert window.footprint_drawings[0]["points"] == [[3.0, 1.0]]
    window._draw_preview()
    assert _text_item(window).sceneBoundingRect().left() == pytest.approx(60 + _cells("LONGER TEXT")[0], abs=0.01)
    # Undo walks back through the same anchored records.
    window._undo_footprint_drawing()
    assert window.footprint_drawings[0]["text"] == "HH"
    assert window.footprint_drawings[0]["anchor"] == USER_TEXT_ANCHOR
