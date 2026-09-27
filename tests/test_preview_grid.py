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
    component_items = {item for item in window.preview_scene.items() if not item.data(gui._GRID_ITEM)}
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
        assert component_items == {item for item in window.preview_scene.items() if not item.data(gui._GRID_ITEM)}
        grid_items = [item for item in window.preview_scene.items() if item.data(gui._GRID_ITEM)]
        assert grid_items
        assert all(item.isVisible() == window.chk_grid.isChecked() for item in grid_items)
        assert abs(view.mapFromScene(center).x() - view.viewport().rect().center().x()) <= 1
        assert abs(view.mapFromScene(center).y() - view.viewport().rect().center().y()) <= 1


def test_grid_controls_work_before_any_footprint_exists(window):
    window.chk_grid.setChecked(False)
    window.combo_grid_unit.setCurrentIndex(1)
    window.chk_grid.setChecked(True)
    assert window.preview_scene.items() == []
