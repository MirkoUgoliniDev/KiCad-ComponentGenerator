"""Grid controls must not change the scale or pan of the footprint preview."""
import pytest
from test_photo_inventory_gui import gui, QApplication
from kicadgen.core.package_geometry import PackageGeometry, PinSpec


@pytest.mark.parametrize('family', ['matrix', 'castellated', 'package'])
@pytest.mark.parametrize('initial_grid', [False, True])
@pytest.mark.parametrize('zoom', [1., 1.4])
def test_repeated_grid_changes_preserve_view(window, family, initial_grid, zoom):
    window.chk_grid.setChecked(initial_grid)
    choices = dict(castellated=gui.CastellatedModuleParams(
        name='GridTest', body_length=42.8, body_width=15.2,
        sides=[gui.CastellatedSide(edge='left', count=3, pitch=5.08, first_pin=1),
               gui.CastellatedSide(edge='right', count=4, pitch=2.54, first_pin=4)]),
        matrix=gui.PinHeaderParams(name='GridTest', rows=2, cols=4),
        package=PackageGeometry(family='soic', body_length_mm=5, body_width_mm=4,
            pins=[PinSpec('1', -2, -1, 1.2, .6), PinSpec('2', 2, -1, 1.2, .6)]))
    window._last_params = choices[family]
    # Stato che lascia un'estrazione vera (2026-09-25): le schede di anteprima
    # ora seguono i dati a ogni cambio di scheda, non la sola chiamata interna.
    window._acquisition_completed = True
    window._set_acquisition_tabs_enabled(True)
    window.tabs.setCurrentIndex(gui.TAB_PREVIEW)
    window.show()
    QApplication.processEvents()
    window._draw_preview()
    QApplication.processEvents()
    view = window.preview_view
    view.scale(zoom, zoom)
    QApplication.processEvents()
    transform = view.transform()
    center = view.mapToScene(view.viewport().rect().center())
    size = window.size()
    scene_rect = window.preview_scene.sceneRect()
    component_items = set(window.preview_scene.items())
    scales = []
    for i in range(12):
        window.chk_grid.setChecked(not window.chk_grid.isChecked())
        window.combo_grid_unit.setCurrentIndex(i % 2)
        QApplication.processEvents()
        scales.append(view.transform().m11())
        assert view.transform().m11() == pytest.approx(transform.m11(), abs=1e-9), scales
        assert view.transform().m22() == pytest.approx(transform.m22(), abs=1e-9)
        assert window.size() == size
        assert window.preview_scene.sceneRect() == scene_rect
        # CONTRACT CHANGED 2026-11-02 (user: «una basta ed avanza»): the grid
        # is only the view's background, so the controls add, remove or hide
        # no scene item; the view's grid follows the checkbox and the unit.
        assert set(window.preview_scene.items()) == component_items
        unit = 2.54 if window.combo_grid_unit.currentData() == 'inch' else 1.0
        if window.chk_grid.isChecked():
            assert view.grid_step() == pytest.approx(unit * window._preview_grid_spec['mm_to_px'])
        else:
            assert view.grid_step() is None
        assert abs(view.mapFromScene(center).x() - view.viewport().rect().center().x()) <= 1
        assert abs(view.mapFromScene(center).y() - view.viewport().rect().center().y()) <= 1


def test_grid_controls_work_before_any_footprint_exists(window):
    window.chk_grid.setChecked(False)
    window.combo_grid_unit.setCurrentIndex(1)
    window.chk_grid.setChecked(True)
    assert window.preview_scene.items() == []

@pytest.mark.parametrize('spacing, aligned', [(5.08, True), (5.0, False)])
def test_pad_reference_grid_preserves_incompatible_geometry(window, spacing, aligned):
    package = PackageGeometry(family='soic', body_length_mm=15, body_width_mm=10,
        pins=[PinSpec('1', -3.17, -2.11, 1.2, .6),
              PinSpec('2', -3.17 + spacing, -2.11, 1.2, .6)])
    original = [(p.x_mm, p.y_mm) for p in package.pins]
    window.combo_grid_unit.setCurrentIndex(1)
    window._draw_preview_package(package)
    # The grid is the view's (no grid lines in the scene): a line passes
    # through the first pad, and through the second only when the spacing is
    # a whole number of steps.
    window.chk_grid.setChecked(True)
    view = window.preview_view
    step, (ox, oy) = view.grid_step() / 20, (c / 20 for c in view._grid[1])
    assert step == pytest.approx(2.54)
    assert (ox, oy) == pytest.approx((-3.17, -2.11))
    lines_from_origin = (-3.17 + spacing - ox) / step
    assert (abs(lines_from_origin - round(lines_from_origin)) < 1e-8) == aligned
    assert [(p.x_mm, p.y_mm) for p in package.pins] == original
    window.combo_grid_unit.setCurrentIndex(0)
    assert window._preview_grid_spec['origin'] == (-3.17, -2.11)


def test_one_grid_only_and_pin_functions_survive_the_grid_switch(window):
    """@brief Positive: switching the grid off hides the view's grid; negative: it hides no footprint item.

    @note Regression 2026-11-02: the old scene-item grid (drawn on top of the
        view's own grid, «una griglia sopra l'altra») was marked with item data
        key 0, the same key the pin-function labels carry («pin-function»), so
        unticking «Griglia» hid those labels too. Now the scene holds no grid
        line at all and the checkbox only drives the view's background.
    """
    from PyQt6.QtGui import QColor
    window.pinout = [{'pin': '1', 'name': 'GND'}]
    window.chk_grid.setChecked(True)
    window._draw_preview_package(PackageGeometry(family='soic', body_length_mm=10, body_width_mm=6,
        pins=[PinSpec('1', -3, -2, 1.2, .6, side='left'), PinSpec('2', 3, -2, 1.2, .6, side='right')]))
    grid_colours = {QColor(gui.SURFACE1).name(), QColor(gui.SURFACE2).name()}
    assert not [item for item in window.preview_scene.items()
                if hasattr(item, 'line') and item.pen().color().name() in grid_colours]
    labels = [item for item in window.preview_scene.items() if item.data(0) == 'pin-function']
    assert labels and all(item.isVisible() for item in labels)
    assert window.preview_view.grid_step() is not None
    window.chk_grid.setChecked(False)
    assert window.preview_view.grid_step() is None
    assert all(item.isVisible() for item in labels)


def test_footprint_zoom_anchor_pan_and_fit(window):
    from PyQt6.QtCore import QPoint, Qt
    from PyQt6.QtTest import QTest
    window.show()
    window._acquisition_completed = True
    window._set_acquisition_tabs_enabled(True)
    window.tabs.setCurrentIndex(gui.TAB_PREVIEW)
    window._draw_preview_package(PackageGeometry(family='soic', body_length_mm=15,
        body_width_mm=10, pins=[PinSpec('1', -3, -2, 1.2, .6)]))
    QApplication.processEvents()
    view = window.preview_view
    initial = view.transform().m11()
    point = view.viewport().rect().center() + QPoint(80, 30)
    anchored = view.mapToScene(point)
    view.zoom(2, point)
    assert view.transform().m11() == pytest.approx(initial * 2)
    assert (view.mapFromScene(anchored) - point).manhattanLength() <= 2
    before = view.mapToScene(view.viewport().rect().center())
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseMove(view.viewport(), point + QPoint(100, 40))
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=point + QPoint(100, 40))
    after = view.mapToScene(view.viewport().rect().center())
    assert after.x() < before.x()
    assert not view.camera.pan_active
    # A redraw after the user's zoom/pan keeps the camera (user, 2026-10-13);
    # «Adatta alla vista» forces the fit.
    window._fit_preview()
    assert view.transform().m11() == pytest.approx(initial * 2)
    window._fit_preview(force=True)
    assert view.transform().m11() == pytest.approx(initial)
