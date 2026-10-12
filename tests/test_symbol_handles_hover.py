"""@brief Handles of the selected element show only near the pointer (user, 2026-10-12).

A handle square drawn over a small text hid its letters; now the handles
are small, translucent and hidden until the pointer enters the element's
area (with a margin), and always shown while a drag is in progress.
"""
from PyQt6.QtCore import QPointF

from kicadgen.gui import symbol_tab as st
from tests.test_symbol_palette_gui import _tab, SCENE_UNITS_PER_MM


def _handles(tab):
    return [item for item in tab.view.scene().items()
            if item.data(0) and item.data(0)[0] in ('shape_point', 'vertex')]


def test_handles_appear_inside_the_element_area_and_vanish_outside(qapp):
    tab = _tab()
    try:
        tab.start_shape('rectangle')
        tab.add_shape_point(0, 0)
        tab.add_shape_point(5.08, 2.54)
        tab._select_graphic(0)
        handles = _handles(tab)
        assert len(handles) == 2, 'the rectangle has its two corner handles'
        assert all(h.rect().width() == st.HANDLE_SIZE_PX for h in handles)
        assert all(h.brush().color().alpha() < 255 for h in handles), 'translucent fill'
        far = QPointF(40 * SCENE_UNITS_PER_MM, 40 * SCENE_UNITS_PER_MM)
        tab._update_readout(far)
        assert not any(h.isVisible() for h in handles), 'hidden while the pointer is far'
        inside = QPointF(2.5 * SCENE_UNITS_PER_MM, -1.2 * SCENE_UNITS_PER_MM)
        tab._update_readout(inside)
        assert all(h.isVisible() for h in handles), 'shown inside the element area'
        # Just beyond the corner but within the margin: still shown, so the
        # pointer finds the handle before touching it. The margin is screen
        # pixels: in scene units it is divided by the view scale.
        m = st.HANDLE_HOVER_MARGIN / tab.view.transform().m11()
        near = QPointF(5.08 * SCENE_UNITS_PER_MM + m - 1, 0)
        tab._update_readout(near)
        assert all(h.isVisible() for h in handles)
        beyond = QPointF(5.08 * SCENE_UNITS_PER_MM + m + 2, 0)
        tab._update_readout(beyond)
        assert not any(h.isVisible() for h in handles)
    finally:
        tab.deleteLater()


def test_handles_stay_visible_while_a_handle_is_dragged(qapp):
    tab = _tab()
    try:
        tab.start_shape('rectangle')
        tab.add_shape_point(0, 0)
        tab.add_shape_point(5.08, 2.54)
        tab._select_graphic(0)
        tab.view.gesture = ('shape_point', 0, 1)
        tab._begin_gesture(('shape_point', 0, 1), QPointF(5.08 * SCENE_UNITS_PER_MM, -2.54 * SCENE_UNITS_PER_MM))
        tab._move_gesture(('shape_point', 0, 1), QPointF(12 * SCENE_UNITS_PER_MM, -2.54 * SCENE_UNITS_PER_MM))
        tab._update_readout(QPointF(60 * SCENE_UNITS_PER_MM, 60 * SCENE_UNITS_PER_MM))
        assert all(h.isVisible() for h in _handles(tab)), 'a drag keeps the handles on screen'
        tab._end_gesture(True)
        tab.view.gesture = None
        assert tab.graphics()[0]['primitives'][0]['points'][1][0] > 5.08, 'the corner was dragged'
    finally:
        tab.deleteLater()


def test_body_handles_show_only_in_a_ring_around_the_outline(qapp):
    """The body covers the symbol: over its middle the handles stay hidden."""
    tab = _tab()
    try:
        body = [item for item in tab.view.scene().items() if item.data(0) and item.data(0)[0] == 'resize']
        assert len(body) == 8
        width = tab._symbol.width * SCENE_UNITS_PER_MM
        m = st.HANDLE_HOVER_MARGIN / tab.view.transform().m11()  # screen pixels -> scene units
        tab._update_readout(QPointF(0, 0))
        assert not any(h.isVisible() for h in body), 'the middle of the body does not reveal them'
        tab._update_readout(QPointF(width / 2 - m / 2, 0))
        assert all(h.isVisible() for h in body), 'near the outline, inside'
        tab._update_readout(QPointF(width / 2 + m - 1, 0))
        assert all(h.isVisible() for h in body), 'near the outline, outside'
        tab._update_readout(QPointF(width / 2 + m + 2, 0))
        assert not any(h.isVisible() for h in body), 'beyond the margin'
    finally:
        tab.deleteLater()


def test_a_press_without_a_prior_move_still_grabs_a_hidden_handle(qapp):
    """Touch and automation press without hovering: the press reveals first."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication
    tab = st.SymbolTab()
    try:
        tab.resize(1200, 750)
        tab.set_data('Board', [{'pin': str(n), 'name': f'P{n}'} for n in range(1, 5)])
        tab.show()
        QApplication.processEvents()
        view = tab.view
        w, h = tab._symbol.width, tab._symbol.height
        corner = view.mapFromScene(QPointF(w * 5, h * 5))
        assert not any(i.isVisible() for i in view.scene().items() if i.data(0) and i.data(0)[0] == 'resize')
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=corner)
        assert view.gesture is not None and view.gesture[0] == 'resize'
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=corner)
        tab.close()
    finally:
        tab.deleteLater()


def test_leaving_the_view_hides_the_handles_and_a_gesture_reveals_only_its_owner(qapp):
    tab = _tab()
    try:
        tab.start_shape('rectangle')
        tab.add_shape_point(0, 0)
        tab.add_shape_point(5.08, 2.54)
        tab._select_graphic(0)
        shape = _handles(tab)
        body = [i for i in tab.view.scene().items() if i.data(0) and i.data(0)[0] == 'resize']
        tab._update_readout(QPointF(2.5 * SCENE_UNITS_PER_MM, -1.2 * SCENE_UNITS_PER_MM))
        assert all(h.isVisible() for h in shape)
        tab.view.pointer_left.emit()
        assert not any(h.isVisible() for h in shape), 'the pointer left the drawing'
        # A pin drag owns no handle group: nothing is revealed by it.
        tab.view.gesture = ('pin', '1')
        tab._update_readout(QPointF(60 * SCENE_UNITS_PER_MM, 60 * SCENE_UNITS_PER_MM))
        assert not any(h.isVisible() for h in shape + body)
        # A body resize reveals the body handles only.
        tab.view.gesture = ('resize', 1, 1)
        tab._update_readout(QPointF(60 * SCENE_UNITS_PER_MM, 60 * SCENE_UNITS_PER_MM))
        assert all(h.isVisible() for h in body) and not any(h.isVisible() for h in shape)
        tab.view.gesture = None
    finally:
        tab.deleteLater()


def test_the_reveal_margin_and_the_squares_are_screen_pixels_at_any_zoom(qapp):
    tab = _tab()
    try:
        tab.start_shape('rectangle')
        tab.add_shape_point(0, 0)
        tab.add_shape_point(5.08, 2.54)
        tab._select_graphic(0)
        handles = _handles(tab)
        from PyQt6.QtWidgets import QGraphicsItem
        assert all(h.flags() & QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations for h in handles)
        tab.view.auto_fit = False
        tab.view.resetTransform()
        tab.view.scale(0.25, 0.25)
        # 12 screen pixels at scale 0.25 are 48 scene units: a pointer 40
        # units past the corner is inside the margin, 60 units is not.
        tab._update_readout(QPointF(5.08 * SCENE_UNITS_PER_MM + 40, 0))
        assert all(h.isVisible() for h in handles)
        tab._update_readout(QPointF(5.08 * SCENE_UNITS_PER_MM + 60, 0))
        assert not any(h.isVisible() for h in handles)
    finally:
        tab.deleteLater()
