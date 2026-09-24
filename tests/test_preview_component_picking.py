"""@brief Verify component-only actor picking without an OpenGL renderer."""

import json
import os
from collections import OrderedDict
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest

pytest.importorskip('pyvistaqt')
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtTest import QSignalSpy
from kicadgen.gui.preview_3d_vtk import Preview3DVtk, _component_index, _component_row


@pytest.mark.parametrize('name,index', [
    ('Cmp0_U1', 0), ('Cmp12_IC_body', 12), ('Cmp12_IC_pin_3', 12),
    ('Cmp12_IC_color4', 12), ('Cmp2_connector_shell', 2),
    ('Pin1', None), ('Body', None), ('PCB', None), ('Cmp1', None),
    ('Cmp1_', None), ('prefixCmp2_body', None), ('Cmp-1_body', None),
    ('Cmp1x_body', None), (None, None), ('Cmp2147483648_body', None),
])
def test_component_names_are_strict(name, index):
    assert _component_index(name) == index


@pytest.mark.parametrize('part,row', [
    ({'source_row': 9, 'component_index': 2, 'name': 'Cmp2_U1'}, 9),
    ({'source_row': 0, 'component_index': 2, 'name': 'Cmp2_U1'}, 0),
    ({'source_row': 7}, 7),
    ({'component_index': 5, 'name': 'Cmp2_U1'}, 5),
    ({'name': 'Cmp2_U1_color3'}, 2),
    ({'name': 'Pin1'}, None), ({'name': 'Body'}, None), ({}, None),
])
def test_component_row_metadata_precedence(part, row):
    assert _component_row(part) == row


@pytest.mark.parametrize('invalid', [None, -1, True, False, 1.0, '4', float('nan'),
                                     float('inf'), 2_147_483_648, [], {}])
def test_invalid_source_row_uses_valid_metadata_then_legacy_name(invalid):
    assert _component_row(dict(source_row=invalid, component_index=6, name='Cmp2_U1')) == 6
    assert _component_row(dict(source_row=invalid, component_index=invalid, name='Cmp2_U1')) == 2
    assert _component_row(dict(source_row=invalid, component_index=invalid, name='Pin1')) is None


def _actor(address):
    return SimpleNamespace(GetAddressAsString=lambda _: address)


def test_callback_emits_row_for_native_actor_identity_only():
    class Signals(QObject):
        component_requested = pyqtSignal(int)

    signals = Signals()
    spy = QSignalSpy(signals.component_requested)
    viewer = SimpleNamespace(_component_actors={'body': 0, 'subpart': 0, 'other': 3},
                             component_requested=signals.component_requested)
    for address in ('body', 'subpart', 'pin', 'other'):
        Preview3DVtk._component_picked(viewer, _actor(address))
    Preview3DVtk._component_picked(viewer, None)
    assert list(spy) == [[0], [0], [3]]
    viewer._component_actors.clear()
    Preview3DVtk._component_picked(viewer, _actor('body'))
    assert len(spy) == 3


def test_picking_configuration_preserves_left_rotation_and_existing_mesh():
    calls = []
    callback = object()
    viewer = SimpleNamespace(_component_picked=callback,
                             enable_mesh_picking=lambda **kw: calls.append(kw))
    Preview3DVtk._configure_component_picking(viewer)
    assert calls == [dict(callback=callback, use_actor=True, left_clicking=False,
                          show=False, show_message=False)]


def test_double_click_configuration_registers_component_callback():
    calls = []
    callback = object()
    viewer = SimpleNamespace(iren=SimpleNamespace(
        add_observer=lambda event, handler: calls.append((event, handler))),
        _component_double_clicked=callback)
    Preview3DVtk._configure_component_double_click(viewer)
    assert calls == [("LeftButtonDoubleClickEvent", callback)]


def test_clear_and_reset_invalidate_mapping(monkeypatch):
    from pyvistaqt import QtInteractor

    calls = []
    monkeypatch.setattr(QtInteractor, 'clear', lambda self: calls.append('clear'))
    viewer = Preview3DVtk.__new__(Preview3DVtk)
    viewer._initialized = False
    viewer._component_actors = {'old': 4}
    Preview3DVtk.clear(viewer)
    assert viewer._component_actors == {}
    assert calls == ['clear']
    dummy = SimpleNamespace(clear=lambda: calls.append('reset-clear'),
                            set_background=lambda color: None)
    Preview3DVtk._reset(dummy)
    assert calls[-1] == 'reset-clear'


@pytest.mark.parametrize('metadata,expected', [
    ({}, {'Cmp0_U1': 0, 'Cmp0_U1_color2': 0, 'Cmp2_USB_shell': 2}),
    ({'component_index': 4}, {'Cmp0_U1': 4, 'Cmp0_U1_color2': 4, 'Cmp2_USB_shell': 4}),
    ({'source_row': 11, 'component_index': 4},
     {'Cmp0_U1': 11, 'Cmp0_U1_color2': 11, 'Cmp2_USB_shell': 11}),
])
def test_render_maps_all_component_subparts_but_not_board_or_pins(tmp_path, metadata, expected):
    names = ['Cmp0_U1', 'Cmp0_U1_color2', 'Cmp2_USB_shell', 'Pin1', 'Body']
    path = tmp_path / 'mesh.json'
    path.write_text(json.dumps({'parts': [dict(name=name, vertices=[[0,0,0], [1,0,0], [0,1,0]],
                                              triangles=[[0,1,2]],
                                              **(metadata if name.startswith('Cmp') else {})) for name in names]}))
    actors = iter(_actor(name) for name in names)
    # render_mesh now also consults the file-identity cache and batches the
    # renders (note 10): the stand-in carries that state, the mapping
    # contract checked below is unchanged.
    viewer = SimpleNamespace(_component_actors={'stale': 9},
                             add_mesh=lambda *args, **kw: next(actors),
                             _add_pin_labels=lambda anchors: None, _finish=lambda: None,
                             _mesh_cache=OrderedDict(), _shown_mesh_key=None,
                             suppress_rendering=False, render=lambda: None)
    viewer._rendering_suppressed = lambda: Preview3DVtk._rendering_suppressed(viewer)
    viewer._reset = lambda: viewer._component_actors.clear()
    Preview3DVtk.render_mesh(viewer, path)
    assert viewer._component_actors == expected


# ── menu contestuale: individuare la riga sotto il cursore ───────────

def _viewer_at(hit, *, height=400, ratio=1.0, mapping=None):
    picked = []
    return picked, SimpleNamespace(
        _component_actors=mapping if mapping is not None else {'body': 7},
        height=lambda: height,
        devicePixelRatioF=lambda: ratio,
        _component_actor_at=lambda x, y: (picked.append((x, y)) or hit))


def test_the_context_menu_looks_up_the_row_under_the_cursor():
    picked, viewer = _viewer_at(_actor('body'))
    assert Preview3DVtk._component_row_at_widget_point(viewer, 120, 50) == 7
    # Qt conta dall'alto, VTK dal basso: senza il ribaltamento si prenderebbe
    # il componente speculare rispetto al centro della vista.
    assert picked == [(120, 350)]


def test_a_high_density_screen_scales_the_pick_point():
    picked, viewer = _viewer_at(_actor('body'), ratio=2.0)
    Preview3DVtk._component_row_at_widget_point(viewer, 120, 50)
    assert picked == [(240, 700)]


def test_an_unusable_scale_factor_falls_back_to_one_to_one():
    picked, viewer = _viewer_at(_actor('body'))
    viewer.devicePixelRatioF = lambda: None
    assert Preview3DVtk._component_row_at_widget_point(viewer, 120, 50) == 7
    assert picked == [(120, 350)]


def test_empty_space_and_unknown_actors_open_no_component_menu():
    """Il negativo: fuori dai componenti il tasto destro resta quello di VTK."""
    _, empty = _viewer_at(None)
    assert Preview3DVtk._component_row_at_widget_point(empty, 1, 1) is None
    _, foreign = _viewer_at(_actor('pcb'))
    assert Preview3DVtk._component_row_at_widget_point(foreign, 1, 1) is None


# ── tasto destro: click contro zoom a trascinamento ──────────────────

@pytest.fixture
def right_click_view():
    """Widget vero: l'evento del tasto destro va provato sul suo percorso Qt."""
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    view = Preview3DVtk()
    view.resize(400, 300)
    view.show()
    app.processEvents()
    view._component_actors = {'fake': 3}
    view._component_actor_at = lambda x, y: _actor('fake')
    rows = []
    view.component_menu_requested.connect(lambda row, _pos: rows.append(row))
    yield view, rows, app
    view.close()
    app.processEvents()


def _press_release(app, view, start, end, button=None):
    from PyQt6.QtCore import QPointF, Qt
    from PyQt6.QtGui import QMouseEvent
    button = button or Qt.MouseButton.RightButton
    for kind, point in ((QMouseEvent.Type.MouseButtonPress, start),
                        (QMouseEvent.Type.MouseButtonRelease, end)):
        app.sendEvent(view, QMouseEvent(kind, QPointF(point), QPointF(view.mapToGlobal(point)),
                                        button, button, Qt.KeyboardModifier.NoModifier))


def test_a_steady_right_click_asks_for_the_component_menu(right_click_view):
    from PyQt6.QtCore import QPoint
    view, rows, app = right_click_view
    _press_release(app, view, QPoint(50, 40), QPoint(50, 40))
    assert rows == [3]


def test_a_right_drag_is_a_zoom_and_opens_no_menu(right_click_view):
    """Il negativo: il tasto destro di VTK e' anche lo zoom a trascinamento."""
    from PyQt6.QtCore import QPoint
    view, rows, app = right_click_view
    _press_release(app, view, QPoint(50, 40), QPoint(90, 40))
    assert rows == []


def test_a_left_click_never_opens_the_component_menu(right_click_view):
    from PyQt6.QtCore import QPoint, Qt
    view, rows, app = right_click_view
    _press_release(app, view, QPoint(50, 40), QPoint(50, 40), Qt.MouseButton.LeftButton)
    assert rows == []


def test_the_same_gesture_never_opens_two_menus(right_click_view):
    """Su Windows il rilascio e contextMenuEvent arrivano entrambi."""
    from PyQt6.QtCore import QPoint
    from PyQt6.QtGui import QContextMenuEvent
    view, rows, app = right_click_view
    _press_release(app, view, QPoint(50, 40), QPoint(50, 40))
    app.sendEvent(view, QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(50, 40),
                                          view.mapToGlobal(QPoint(50, 40))))
    assert rows == [3]


def test_the_keyboard_menu_key_still_works_on_its_own(right_click_view):
    from PyQt6.QtCore import QPoint
    from PyQt6.QtGui import QContextMenuEvent
    view, rows, app = right_click_view
    app.sendEvent(view, QContextMenuEvent(QContextMenuEvent.Reason.Keyboard, QPoint(50, 40),
                                          view.mapToGlobal(QPoint(50, 40))))
    assert rows == [3]
