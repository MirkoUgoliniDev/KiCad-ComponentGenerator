"""Independent review regressions for preview/export agreement."""

import pytest
from PyQt6.QtWidgets import QGraphicsEllipseItem, QGraphicsSimpleTextItem, QGraphicsLineItem, QGraphicsRectItem

from kicadgen.generators.host_mounting import header_footprint_params
from kicadgen.core.package_geometry import PackageGeometry, PinSpec
from kicadgen.generators.pin_header import PinHeaderParams, pin_positions, build_pin_header
from kicadgen.core.footprint import FpLine, FpRect
from kicadgen.gui.theme import BASE, TEXT
from tests.test_contact_style import dev_kit


def _is_property_text(item):
    """True for the draggable Reference/Value groups and their children.

    They carry tooltips since b99f835 but are footprint properties, not pin
    labels or copper: the pad/label contracts of this module skip them.
    """
    from kicadgen.gui.footprint_view import TEXT_KEY_ROLE
    while item is not None:
        if item.data(TEXT_KEY_ROLE) is not None:
            return True
        item = item.parentItem()
    return False


def test_grid_header_preview_shows_exported_drills(window, monkeypatch):
    """The grid-header renderer must show holes just like castellated hosts."""
    params = header_footprint_params(PinHeaderParams(rows=2, cols=3), 1.8, .9)
    monkeypatch.setattr(window, '_preview_params', lambda: params)
    window._draw_preview()
    holes = [item for item in window.preview_scene.items()
             if isinstance(item, QGraphicsEllipseItem)
             and item.brush().color().name() == BASE]
    labels = [item for item in window.preview_scene.items()
              if isinstance(item, QGraphicsSimpleTextItem) and item.toolTip()
              and not _is_property_text(item)]
    assert len(holes) == 6
    for _, x, y in pin_positions(params):
        hole = next(item for item in holes
                    if item.rect().center().x() == pytest.approx(x * 20)
                    and item.rect().center().y() == pytest.approx(y * 20))
        assert hole.rect().width() == pytest.approx(params.drill * 20)
        assert not any(label.sceneBoundingRect().intersects(hole.sceneBoundingRect())
                       for label in labels)


def test_host_preview_total_excludes_display_margin(window, monkeypatch):
    """Whitespace around a preview is not two extra millimetres of the board."""
    params = header_footprint_params(dev_kit(), 1.7, 1)
    horizontal, vertical = [], []
    monkeypatch.setattr(window, '_add_dim_h', lambda x1, x2, y, text: horizontal.append((x1, x2, text)))
    monkeypatch.setattr(window, '_add_dim_v', lambda y1, y2, x, text: vertical.append((y1, y2, text)))
    window._draw_preview_package(params)
    assert horizontal[-1][1] - horizontal[-1][0] == pytest.approx(params.body_length_mm * 20)
    assert vertical[-1][1] - vertical[-1][0] == pytest.approx(params.body_width_mm * 20)


@pytest.mark.parametrize('shape, angle, expected_width', [
    ('circle', 37, 2), ('oval', 90, 1), ('roundrect', 90, 1), ('rect', 90, 1),
])
def test_package_total_includes_rotated_pad_extent_without_body(window, monkeypatch, shape, angle, expected_width):
    pad = PinSpec('1', 5, -3, 2 if shape == 'circle' else 4,
                  2 if shape == 'circle' else 1, shape=shape, rotation_deg=angle)
    widths = []
    monkeypatch.setattr(window, '_add_dim_h', lambda x1, x2, y, text: widths.append((x2 - x1) / 20))
    window._draw_preview_package(PackageGeometry(pins=[pad]))
    assert widths[-1] == pytest.approx(expected_width)


@pytest.mark.parametrize('params', [
    PinHeaderParams(rows=2, cols=3, body_length=20, body_width=12,
                    body_offset_x=-4, body_offset_y=-2, margin=.4),
    PinHeaderParams(rows=2, cols=3, body_length=3, body_width=2,
                    body_offset_x=1, body_offset_y=1, margin=2),
    PinHeaderParams(rows=2, cols=3, margin=.2),
    PinHeaderParams(rows=2, cols=3, margin=2),
])
def test_grid_outline_and_dimensions_match_export(window, monkeypatch, params):
    horizontal, vertical = [], []
    monkeypatch.setattr(window, '_preview_params', lambda: params)
    monkeypatch.setattr(window, '_add_dim_h', lambda a, b, y, text: horizontal.append((a / 20, b / 20)))
    monkeypatch.setattr(window, '_add_dim_v', lambda a, b, x, text: vertical.append((a / 20, b / 20)))
    window._draw_preview()
    export = build_pin_header(params)
    courtyard = next(g for g in export.graphics if g.layer == 'F.CrtYd')
    assert horizontal[-1] == pytest.approx((courtyard.start[0] + params.margin,
                                          courtyard.end[0] - params.margin))
    assert vertical[-1] == pytest.approx((courtyard.start[1] + params.margin,
                                        courtyard.end[1] - params.margin))
    actual = []
    for item in window.preview_scene.items():
        if not isinstance(item, (QGraphicsLineItem, QGraphicsRectItem)) or item.pen().color().name() != TEXT:
            continue
        if isinstance(item, QGraphicsLineItem):
            line = item.line()
            actual.append(('line', line.x1() / 20, line.y1() / 20, line.x2() / 20, line.y2() / 20))
        else:
            rect = item.rect()
            actual.append(('rect', rect.left() / 20, rect.top() / 20, rect.right() / 20, rect.bottom() / 20))
    expected = [('line' if isinstance(g, FpLine) else 'rect', *g.start, *g.end)
                for g in export.graphics if g.layer == 'F.SilkS' and isinstance(g, (FpLine, FpRect))]
    assert len(actual) == len(expected)
    for observed, exported in zip(sorted(actual), sorted(expected)):
        assert observed[0] == exported[0]
        assert observed[1:] == pytest.approx(exported[1:])


def test_preview_builder_failure_does_not_escape_qt_slot(window, monkeypatch):
    from kicadgen.gui import main_window as gui
    monkeypatch.setattr(window, '_preview_params', lambda: PinHeaderParams())
    def fail(params):
        raise ValueError('invalid exported extent')
    monkeypatch.setattr(gui, 'build_pin_header', fail)
    window._draw_preview()
    assert not window.preview_scene.items()
    assert window.label_preview_mounting_error.text() == 'invalid exported extent'


def test_grid_dimensions_do_not_cover_pin_labels(window, monkeypatch, qapp):
    from PyQt6.QtGui import QFont, QFontDatabase
    from pathlib import Path
    font_path = Path('C:/Windows/Fonts/segoeui.ttf')
    if not font_path.exists():
        pytest.skip('Windows visual regression requires Segoe UI')
    original = qapp.font()
    QFontDatabase.addApplicationFont(str(font_path))
    try:
        qapp.setFont(QFont('Segoe UI', 10))
        monkeypatch.setattr(window, '_preview_params', lambda: PinHeaderParams(rows=2, cols=3, margin=.2))
        window._draw_preview()
        texts = [item for item in window.preview_scene.items()
                 if isinstance(item, QGraphicsSimpleTextItem) and not _is_property_text(item)]
        pins = [item for item in texts if item.toolTip()]
        dimensions = [item for item in texts if not item.toolTip()]
        assert not any(pin.sceneBoundingRect().intersects(dim.sceneBoundingRect())
                       for pin in pins for dim in dimensions)
    finally:
        qapp.setFont(original)


@pytest.mark.parametrize('language', ['de', 'en', 'fr', 'it'])
def test_preview_icon_and_mounting_controls_translate_together(window, language):
    window.i18n.set_language(language)
    window.i18n.retranslate_widgets(window)
    button = window.btn_update_host_footprint_preview
    expected = window.i18n.text('Aggiorna solo il footprint')
    assert button.text() == ''
    assert not button.icon().isNull()
    assert button.toolTip() == expected
    assert button.accessibleName() == expected
    for index in range(window.combo_mounting.count()):
        assert window.combo_preview_mounting.itemData(index) == window.combo_mounting.itemData(index)
        assert window.combo_preview_mounting.itemText(index) == window.combo_mounting.itemText(index)
