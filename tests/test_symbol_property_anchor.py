"""@brief Reference and Value of the symbol are anchored at their bottom-left corner (user, 2026-10-16).

Same contract as the drawn texts of 2026-10-14: the anchor is the
bottom-left corner of the text, the editor draws the handle square there,
the text grows right and up, a drag snaps that corner to the grid and the
file writes `(justify left bottom)` at it. Centres (default positions and
positions stored before) are converted with KiCad's own text geometry, so
KiCad draws the text where it drew it centred; converted positions are
marked and never converted twice. Hidden properties are not touched.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import re
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
from PyQt6.QtCore import QPointF
from PyQt6.QtWidgets import QGraphicsSimpleTextItem

from kicadgen.generators import symbol_glyphs as g
from kicadgen.generators.symbol import (
    PROPERTIES_KEY, PROPERTY_ANCHOR_BOTTOM_LEFT, PROPERTY_ANCHOR_CENTRE, PROPERTY_ANCHOR_FIELD, TEXT_SIZE,
    PropertyPositions, SymbolPin, default_property_positions, export_symbol, kicad_text_width, layout_symbol,
    property_anchor_from_centre, property_anchors, property_texts, serialize_symbol, symbol_inputs,
    symbol_property_positions,
)

PINS = [SymbolPin('1', 'A', 'passive', 'left'), SymbolPin('2', 'B', 'passive', 'right')]
ROWS = [{'pin': '1', 'name': 'A'}, {'pin': '2', 'name': 'B'}]


def _kicad_cli():
    cli = shutil.which('kicad-cli')
    if cli:
        return cli
    for candidate in sorted(Path('C:/Program Files/KiCad').glob('*/bin/kicad-cli.exe'), reverse=True):
        return str(candidate)
    return None


def _property_line(text, key):
    return next(line for line in text.splitlines() if line.strip().startswith(f'(property "{key}"'))


def _num(value):
    return f'{value:.4f}'.rstrip('0').rstrip('.') or '0'


def _centred_file(text, centres):
    """The same library with Reference/Value written centred at `centres` (the file before 2026-10-16)."""
    for key, (x, y) in centres.items():
        line = _property_line(text, key)
        old = re.sub(r'\(at [-\d.]+ [-\d.]+ (\d+)\)', lambda m: f'(at {_num(x)} {_num(y)} {m.group(1)})', line)
        old = old.replace(' (justify left bottom)', '')
        text = text.replace(line, old)
    return text


def _kicad_strokes(cli, path, out):
    """{label: (min x, max x, min y, max y)} of the stroked texts KiCad draws, mm, symbol coordinates (y up)."""
    result = subprocess.run([cli, 'sym', 'export', 'svg', '-o', str(out), str(path)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    svg = next(Path(out).glob('*.svg')).read_text(encoding='utf-8')
    source = Path(path).read_text(encoding='utf-8')
    body = re.search(r'\(rectangle \(start ([-\d.]+) ([-\d.]+)\)', source)
    rect = re.search(r'<rect x="([-\d.]+)" y="([-\d.]+)"', svg)
    ox, oy = float(rect.group(1)) - float(body.group(1)), float(rect.group(2)) + float(body.group(2))
    boxes = {}
    for match in re.finditer(r'<g class="stroked-text"><desc>([^<]*)</desc>(.*?)</g>', svg, re.S):
        numbers = [float(v) for v in re.findall(r'[-\d.]+', ' '.join(re.findall(r'd="([^"]*)"', match.group(2))))]
        xs = [x - ox for x in numbers[0::2]]
        ys = [oy - y for y in numbers[1::2]]
        boxes[match.group(1)] = (min(xs), max(xs), min(ys), max(ys))
    return boxes


# ── Generator: geometry and file ──────────────────────────────────────

def test_the_kicad_width_follows_the_stroke_font_and_not_the_character_count():
    # Measured with kicad-cli 10.0 (2026-10-16): RS485 6.3084, U? 2.6194,
    # MMMM 6.006 and iiii 2.6196 mm at 1.27 mm.
    assert kicad_text_width('RS485', 1.27) == pytest.approx(6.3084, abs=0.002)
    assert kicad_text_width('U?', 1.27) == pytest.approx(2.6194, abs=0.002)
    assert kicad_text_width('MMMM', 1.27) == pytest.approx(6.006, abs=0.002)
    assert kicad_text_width('iiii', 1.27) == pytest.approx(2.6196, abs=0.002)
    # Negative: same length, different widths; and the width scales with the size.
    assert kicad_text_width('MMMM', 1.27) > 2 * kicad_text_width('iiii', 1.27)
    assert kicad_text_width('RS485', 2.54) == pytest.approx(12.4164, abs=0.002)


def test_a_centre_becomes_the_bottom_left_corner_along_the_text_frame():
    w = kicad_text_width('RS485', 1.27)
    assert property_anchor_from_centre('RS485', 1.27, 10.0, 20.0) == pytest.approx((10 - w / 2, 20 - 0.585 * 1.27), abs=1e-4)
    # Turned 90 degrees: it reads along +Y and its glyphs point to -X, so
    # the corner is back along Y and towards +X (as KiCad turns a text).
    assert property_anchor_from_centre('RS485', 1.27, 10.0, 20.0, 90) == pytest.approx((10 + 0.585 * 1.27, 20 - w / 2), abs=1e-4)


def test_the_file_writes_both_visible_properties_bottom_left_and_leaves_the_hidden_ones():
    text = serialize_symbol('Board', PINS)
    anchors = property_anchors(layout_symbol(PINS), property_texts('U', 'Board'))
    for key in ('Reference', 'Value'):
        x, y = anchors[key]
        line = _property_line(text, key)
        assert f'(at {_num(x)} {_num(y)} 0)' in line
        assert line.endswith('(effects (font (size 1.27 1.27)) (justify left bottom)))')
    for key in ('Footprint', 'Datasheet', 'Description'):
        line = _property_line(text, key)
        assert '(at 0 0 0)' in line and '(hide yes)' in line and 'justify' not in line


def test_a_new_project_default_is_the_converted_centre_and_follows_the_layout():
    layout = layout_symbol(PINS)
    centres = default_property_positions(layout)
    anchors = property_anchors(layout, property_texts('U', 'Board'))
    for key, text in property_texts('U', 'Board').items():
        assert anchors[key] == property_anchor_from_centre(text, TEXT_SIZE, *centres[key])
    # The default keeps following the rule: a top pin lifts the centre and the anchor alike.
    top = [SymbolPin('1', 'A', 'passive', 'top'), SymbolPin('2', 'B', 'passive', 'right')]
    lifted = property_anchors(layout_symbol(top), property_texts('U', 'Board'))
    assert lifted['Value'][1] - anchors['Value'][1] == pytest.approx(
        default_property_positions(layout_symbol(top))['Value'][1] - centres["Value"][1], abs=2e-4)
    # A longer name moves the default corner left: the centred place is kept.
    assert property_anchors(layout, property_texts('U', 'LongerBoard'))['Value'][0] < anchors['Value'][0]


def test_stored_positions_are_converted_only_when_they_are_centres():
    layout = layout_symbol(PINS)
    texts = property_texts('U', 'Board')
    legacy = [dict(row, **{PROPERTIES_KEY: {'Value': [7.62, -10.16]}}) for row in ROWS]
    marked = [dict(row, **{PROPERTIES_KEY: {'Value': [7.62, -10.16], PROPERTY_ANCHOR_FIELD: PROPERTY_ANCHOR_BOTTOM_LEFT}})
              for row in ROWS]
    old, new = symbol_property_positions(legacy), symbol_property_positions(marked)
    assert old.anchor == PROPERTY_ANCHOR_CENTRE and new.anchor == PROPERTY_ANCHOR_BOTTOM_LEFT
    assert old == new == {'Value': (7.62, -10.16)}
    assert property_anchors(layout, texts, new)['Value'] == (7.62, -10.16), 'an anchor is never converted'
    assert property_anchors(layout, texts, old)['Value'] == property_anchor_from_centre('Board', TEXT_SIZE, 7.62, -10.16)
    # A plain mapping keeps the meaning `properties` always had: centres.
    plain = serialize_symbol('Board', PINS, properties={'Value': (7.62, -10.16)})
    assert plain == serialize_symbol('Board', PINS, properties=old)
    assert '(at 7.62 -10.16 0)' in _property_line(serialize_symbol('Board', PINS, properties=new), 'Value')


def test_an_unknown_anchor_mark_is_refused():
    rows = [dict(row, **{PROPERTIES_KEY: {'Value': [0, 1], PROPERTY_ANCHOR_FIELD: 'middle'}}) for row in ROWS]
    with pytest.raises(ValueError):
        symbol_property_positions(rows)


def test_kicad_draws_the_property_where_it_drew_it_centred(tmp_path):
    """kicad-cli: default positions and a legacy stored centre land on the same strokes as before."""
    cli = _kicad_cli()
    if not cli:
        pytest.skip('KiCad CLI not installed')
    legacy = [dict(row, **{PROPERTIES_KEY: {'Reference': [-7.62, -12.7]}}) for row in ROWS]
    pins, body = symbol_inputs(legacy)
    layout = layout_symbol(pins, body)
    chosen = symbol_property_positions(legacy)
    new = export_symbol(tmp_path / 'new' / 'RS485.kicad_sym', 'RS485', pins, body=body, properties=chosen)
    centres = dict(default_property_positions(layout), Reference=(-7.62, -12.7))
    old = tmp_path / 'old' / 'RS485.kicad_sym'
    old.parent.mkdir()
    old.write_text(_centred_file(new.read_text(encoding='utf-8'), centres), encoding='utf-8')
    assert 'justify' not in _property_line(old.read_text(encoding='utf-8'), 'Value')
    before = _kicad_strokes(cli, old, tmp_path / 'svg-old')
    after = _kicad_strokes(cli, new, tmp_path / 'svg-new')
    for label in ('U?', 'RS485'):
        assert after[label] == pytest.approx(before[label], abs=0.01), label
    # The new strokes start at the anchor (stroke bearing only), as for the drawn texts.
    ax, ay = property_anchors(layout, property_texts('U', 'RS485'), chosen)['Value']
    xs0, _xs1, ys0, _ys1 = after['RS485']
    assert 0 <= xs0 - ax < 1.0 and 0 <= ys0 - ay < 0.6


def test_kicad_draws_a_turned_property_where_it_drew_it_centred(tmp_path):
    """kicad-cli at 90 degrees: the conversion follows the text frame like the drawn texts."""
    cli = _kicad_cli()
    if not cli:
        pytest.skip('KiCad CLI not installed')
    text = serialize_symbol('RS485', PINS)
    line = _property_line(text, 'Value')
    turned = line.replace(' 0) (show_name', ' 90) (show_name')
    x, y = property_anchor_from_centre('RS485', TEXT_SIZE, 20.0, 0.0, 90)
    anchored = re.sub(r'\(at [-\d.]+ [-\d.]+ 90\)', f'(at {_num(x)} {_num(y)} 90)', turned)
    centred = re.sub(r'\(at [-\d.]+ [-\d.]+ 90\)', '(at 20 0 90)', turned).replace(' (justify left bottom)', '')
    boxes = []
    for name, value_line in (('a', anchored), ('c', centred)):
        path = tmp_path / name / 'T.kicad_sym'
        path.parent.mkdir()
        path.write_text(text.replace(line, value_line), encoding='utf-8')
        boxes.append(_kicad_strokes(cli, path, tmp_path / name / 'svg')['RS485'])
    assert boxes[0] == pytest.approx(boxes[1], abs=0.01)


# ── Editor ───────────────────────────────────────────────────────────

@pytest.fixture
def tab(qapp):
    from kicadgen.gui.symbol_tab import SymbolTab
    tab = SymbolTab()
    tab.resize(1200, 800)
    tab.set_data('Board', deepcopy(ROWS), 'KiCadGen:Board', ['1', '2'])
    yield tab
    tab.deleteLater()


def _property_item(tab, key):
    return next(item for item in tab.view.scene().items()
                if isinstance(item, QGraphicsSimpleTextItem) and item.data(0) == ('property', key))


def _anchor_square(tab, key):
    squares = [item for item in tab.view.scene().items()
               if item.data(0) == ('property', key) and not isinstance(item, QGraphicsSimpleTextItem)]
    assert len(squares) == 1
    return squares[0]


def _drawn_corner(tab, item):
    """The bottom-left anchor, scene units, KiCad would draw this text item from.

    CONTRACT CHANGED 2026-10-16 (user: KiCad's width and box,
    gui/kicad_text_item.py): the drawing no longer puts the layout's left
    edge and baseline on the anchor but KiCad's cells and baseline, half the
    pen margin to the right and 0.125 x size above it; this undoes exactly
    that offset (texts at `TEXT_SIZE`, the size of the properties and of
    the drawn texts compared here).
    """
    from kicadgen.core.kicad_text import kicad_text_cells
    from kicadgen.gui.symbol_tab import SCENE_UNITS_PER_MM, SymbolTab
    left, up = kicad_text_cells(item.text(), TEXT_SIZE)
    baseline = item.mapToScene(0, SymbolTab._text_baseline(item.text(), item.font())).y()
    return item.sceneBoundingRect().left() - left * SCENE_UNITS_PER_MM, baseline + up * SCENE_UNITS_PER_MM


def test_the_editor_draws_each_property_from_its_anchor_with_the_square_there(tab):
    from kicadgen.gui.symbol_tab import SCENE_UNITS_PER_MM as S
    anchors = property_anchors(tab._symbol, property_texts('U', 'Board'))
    assert tab._property_positions(tab.draft_rows(), tab._symbol) == anchors
    for key in ('Reference', 'Value'):
        x, y = anchors[key]
        item = _property_item(tab, key)
        assert _drawn_corner(tab, item) == pytest.approx((x * S, -y * S), abs=0.5)
        rect = item.sceneBoundingRect()
        assert rect.right() > x * S and rect.top() < -y * S, 'it grows right and up'
        square = _anchor_square(tab, key)
        assert (square.pos().x(), square.pos().y()) == pytest.approx((x * S, -y * S))
    assert not any(isinstance(i, QGraphicsSimpleTextItem) and i.text() == 'KiCadGen:Board'
                   for i in tab.view.scene().items()), 'hidden properties are not drawn'


def test_a_new_project_property_stays_in_its_place_on_screen(tab):
    # Before 2026-10-16 the editor centred the text on the default centre.
    # CONTRACT CHANGED 2026-10-16 (user: KiCad's width, gui/kicad_text_item.py):
    # the drawn layout is now KiCad's own box, so its middle IS the default
    # centre KiCad centred the text on (it was within 15 % of the width
    # while the editor's font kept its own width), and the baseline lies
    # where KiCad's lies for a text centred there: (0.585 - 0.125) x size
    # below the centre.
    from kicadgen.core.kicad_text import KICAD_BOTTOM_TO_BASELINE, KICAD_CENTRE_TO_BOTTOM, kicad_text_margin
    from kicadgen.gui.symbol_tab import SCENE_UNITS_PER_MM as S, SymbolTab
    centres = default_property_positions(tab._symbol)
    for key, text in property_texts('U', 'Board').items():
        item = _property_item(tab, key)
        box = item.sceneBoundingRect()
        assert box.width() / S + kicad_text_margin(TEXT_SIZE) == pytest.approx(kicad_text_width(text, TEXT_SIZE),
                                                                               abs=0.002), key
        assert box.center().x() / S == pytest.approx(centres[key][0], abs=0.002), key
        baseline = item.mapToScene(0, SymbolTab._text_baseline(item.text(), item.font())).y()
        below = (KICAD_CENTRE_TO_BOTTOM - KICAD_BOTTOM_TO_BASELINE) * TEXT_SIZE
        assert -baseline / S == pytest.approx(centres[key][1] - below, abs=0.002), key


def test_the_anchor_square_shows_near_the_text_and_while_dragged(tab):
    from kicadgen.gui.symbol_tab import SCENE_UNITS_PER_MM as S
    square = _anchor_square(tab, 'Value')
    tab._pointer_inside = True
    tab._update_handle_visibility(_property_item(tab, 'Value').sceneBoundingRect().center())
    assert square.isVisible(), 'shown with the pointer on the text, like a drawn text handle'
    far = QPointF(60 * S, 60 * S)
    tab._update_handle_visibility(far)
    assert not square.isVisible(), 'hidden with the pointer far'
    tab.view.gesture = ('property', 'Value')
    tab._update_handle_visibility(far)
    assert square.isVisible(), 'kept on screen while its property is dragged'
    tab.view.gesture = None


def test_a_drag_snaps_the_corner_to_the_grid_and_marks_the_mapping(tab):
    from kicadgen.gui.symbol_tab import GRID_MM, SCENE_UNITS_PER_MM as S
    before = tab._property_positions(tab.draft_rows(), tab._symbol)['Value']
    tab._begin_gesture(('property', 'Value'), QPointF(0, 0))
    tab._move_gesture(('property', 'Value'), QPointF(7.9 * S, 10.3 * S))
    tab._end_gesture(True)
    expected = tuple(round(round(v / GRID_MM) * GRID_MM, 4) for v in (before[0] + 7.9, before[1] - 10.3))
    for row in tab.draft_rows():
        assert row[PROPERTIES_KEY] == {'Value': list(expected), PROPERTY_ANCHOR_FIELD: PROPERTY_ANCHOR_BOTTOM_LEFT}
    assert _drawn_corner(tab, _property_item(tab, 'Value')) == pytest.approx((expected[0] * S, -expected[1] * S),
                                                                              abs=0.5)
    pins, body = symbol_inputs(tab.draft_rows())
    text = serialize_symbol('Board', pins, body=body, properties=symbol_property_positions(tab.draft_rows()))
    assert f'(at {_num(expected[0])} {_num(expected[1])} 0)' in _property_line(text, 'Value')


def test_a_drag_back_into_the_grid_cell_of_the_default_writes_nothing(tab):
    from kicadgen.gui.symbol_tab import GRID_MM, SCENE_UNITS_PER_MM as S
    before = tab.draft_rows()
    x, y = tab._property_positions(before, tab._symbol)['Value']
    # Any target that snaps to the grid point of the default anchor.
    target = (round(x / GRID_MM) * GRID_MM + 0.2, round(y / GRID_MM) * GRID_MM - 0.2)
    tab._begin_gesture(('property', 'Value'), QPointF(0, 0))
    tab._move_gesture(('property', 'Value'), QPointF((target[0] - x) * S, -(target[1] - y) * S))
    tab._end_gesture(True)
    assert tab.draft_rows() == before
    # Negative: one grid step away is a real move.
    tab._begin_gesture(('property', 'Value'), QPointF(0, 0))
    tab._move_gesture(('property', 'Value'), QPointF(GRID_MM * S, 0))
    tab._end_gesture(True)
    assert PROPERTIES_KEY in tab.draft_rows()[0]


def test_a_legacy_centre_is_converted_once_and_not_twice(qapp):
    from kicadgen.gui.symbol_tab import SCENE_UNITS_PER_MM, SymbolTab
    legacy = {'Reference': [0.0, 10.16], 'Value': [5.08, 7.62]}
    rows = [dict(row, **{PROPERTIES_KEY: deepcopy(legacy)}) for row in ROWS]
    tab = SymbolTab()
    try:
        tab.set_data('Board', deepcopy(rows), 'KiCadGen:Board', ['1', '2'])
        assert not tab.has_pending_edits(), 'loading an old project does not modify it'
        assert tab.rows()[0][PROPERTIES_KEY] == legacy, 'stored as it was until the user moves a property'
        converted = property_anchor_from_centre('Board', TEXT_SIZE, 5.08, 7.62)
        assert tab._property_positions(tab.draft_rows(), tab._symbol)['Value'] == converted
        # Moving the Reference rewrites the mapping with anchors: the Value
        # centre becomes its anchor, once.
        tab._begin_gesture(('property', 'Reference'), QPointF(0, 0))
        tab._move_gesture(('property', 'Reference'), QPointF(-5.08 * SCENE_UNITS_PER_MM, 0))
        tab._end_gesture(True)
        stored = tab.draft_rows()[0][PROPERTIES_KEY]
        assert stored[PROPERTY_ANCHOR_FIELD] == PROPERTY_ANCHOR_BOTTOM_LEFT
        assert tuple(stored['Value']) == converted
        assert tab._property_positions(tab.draft_rows(), tab._symbol)['Value'] == converted, 'not converted again'
        # Reloading the marked rows (save and reopen) keeps the same anchor.
        tab.apply()
        tab.set_data('Board', tab.rows(), 'KiCadGen:Board', ['1', '2'], force=True)
        assert tab._property_positions(tab.draft_rows(), tab._symbol)['Value'] == converted
        # And the file draws it where the legacy file did (same anchor written).
        pins, body = symbol_inputs(rows)
        old_text = serialize_symbol('Board', pins, body=body, properties=symbol_property_positions(rows))
        pins, body = symbol_inputs(tab.rows())
        new_text = serialize_symbol('Board', pins, body=body, properties=symbol_property_positions(tab.rows()))
        assert _property_line(old_text, 'Value') == _property_line(new_text, 'Value')
    finally:
        tab.deleteLater()


def test_a_drawn_text_and_a_property_share_the_anchor_contract(tab):
    """Same corner, same growth, same square, same file form."""
    from kicadgen.gui.symbol_tab import SCENE_UNITS_PER_MM as S
    tab._text_prompt = lambda *args: ('Board', TEXT_SIZE)
    assert tab.start_shape('text')
    tab.add_shape_point(-10.16, -12.7)
    (record,) = tab.graphics()
    tab._begin_gesture(('property', 'Value'), QPointF(0, 0))
    x, y = tab._property_positions(tab.draft_rows(), tab._symbol)['Value']
    tab._move_gesture(('property', 'Value'), QPointF((10.16 - x) * S, -(-12.7 - y) * S))
    tab._end_gesture(True)
    assert symbol_property_positions(tab.draft_rows())['Value'] == (10.16, -12.7)
    tab._select_graphic(0)
    drawn = next(i for i in tab.view.scene().items()
                 if isinstance(i, QGraphicsSimpleTextItem) and i.data(0) == ('graphic', 0))
    prop = _property_item(tab, 'Value')
    dl, db = _drawn_corner(tab, drawn)
    pl, pb = _drawn_corner(tab, prop)
    assert (dl - record['x'] * S, db + record['y'] * S) == pytest.approx((pl - 10.16 * S, pb - 12.7 * S), abs=0.5)
    assert drawn.sceneBoundingRect().size() == prop.sceneBoundingRect().size()
    text_square = next(i for i in tab.view.scene().items() if i.data(0) == ('shape_point', 0, 0))
    assert text_square.rect() == _anchor_square(tab, 'Value').rect(), 'the same handle square'
    pins, body = symbol_inputs(tab.draft_rows())
    out = serialize_symbol('Board', pins, body=body, graphics=tab.graphics(),
                           properties=symbol_property_positions(tab.draft_rows()))
    text_line = next(line for line in out.splitlines() if line.strip().startswith('(text "Board"'))
    assert '(at -10.16 -12.7 0)' in text_line and text_line.endswith('(justify left bottom)))')
    assert '(at 10.16 -12.7 0)' in _property_line(out, 'Value')
    assert _property_line(out, 'Value').endswith('(justify left bottom)))')


def test_the_reference_corner_follows_the_designator_text(tab):
    tab.set_reference('RV')
    anchors = tab._property_positions(tab.draft_rows(), tab._symbol)
    centre = default_property_positions(tab._symbol)['Reference']
    assert anchors['Reference'] == property_anchor_from_centre('RV?', TEXT_SIZE, *centre)
    assert anchors['Reference'][0] < property_anchor_from_centre('U?', TEXT_SIZE, *centre)[0]
