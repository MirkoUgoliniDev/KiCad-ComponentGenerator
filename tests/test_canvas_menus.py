"""@brief Shared element menu and clipboard (`kicadgen.gui.canvas_menus`)."""
import pytest
from PyQt6.QtCore import QPoint, Qt, QTimer
from PyQt6.QtTest import QTest
from PyQt6.QtGui import QIcon

from kicadgen.gui import canvas_menus as cm

TX = lambda text: f"<{text}>"   # noqa: E731  marks every label as translated


def _standard():
    return (cm.rotate_entries(TX) + [cm.mirror_entry(TX), cm.delete_entry(TX, "Elimina disegno"),
                                     cm.copy_entry(TX), cm.duplicate_entry(TX), cm.properties_entry(TX)])


def test_standard_entries_have_the_shared_keys_words_and_icons():
    entries = _standard()
    assert [e.key for e in entries] == [cm.ROTATE_CCW, cm.ROTATE_CW, cm.MIRROR, cm.DELETE,
                                        cm.COPY, cm.DUPLICATE, cm.PROPERTIES]
    assert entries[0].label == "<Ruota di 90 gradi (antiorario)>"
    assert entries[3].label == "<Elimina disegno>"
    for entry in entries:
        if entry.icon:
            assert (cm.ASSETS / entry.icon).is_file(), entry.icon


def test_menu_has_a_disabled_title_then_the_entries_in_order(qapp):
    menu, keys = cm.build_element_menu(None, "Testo", _standard())
    actions = menu.actions()
    assert actions[0].text() == "Testo" and not actions[0].isEnabled()
    assert actions[1].isSeparator()
    assert [keys[a] for a in actions[2:]] == [e.key for e in _standard()]
    assert all(a.isIconVisibleInMenu() for a in actions[2:6])
    menu.deleteLater()


def test_no_title_means_no_heading_line(qapp):
    menu, keys = cm.build_element_menu(None, "", [cm.copy_entry(TX)])
    assert len(menu.actions()) == 1 and keys[menu.actions()[0]] == cm.COPY
    menu.deleteLater()


def test_disabled_entry_separator_and_checkable_submenu(qapp):
    swatch = QIcon()
    entries = [cm.mirror_entry(TX, enabled=False),
               cm.MenuEntry("", "Layer", separator_before=True, children=(
                   cm.MenuEntry("layer:F.SilkS", "F.SilkS", checkable=True, checked=True, icon_object=swatch),
                   cm.MenuEntry("layer:F.Fab", "F.Fab", checkable=True)))]
    menu, keys = cm.build_element_menu(None, "", entries)
    actions = menu.actions()
    assert not actions[0].isEnabled()
    assert actions[1].isSeparator()
    sub = actions[2].menu()
    assert sub is not None and [a.isChecked() for a in sub.actions()] == [True, False]
    assert sorted(keys.values()) == ["layer:F.Fab", "layer:F.SilkS", cm.MIRROR]
    menu.deleteLater()


def test_exec_returns_the_chosen_key_or_none(qapp):
    menu, keys = cm.build_element_menu(None, "T", [cm.copy_entry(TX), cm.duplicate_entry(TX)])
    target = menu.actions()[3]
    QTimer.singleShot(0, lambda: (menu.setActiveAction(target), QTest.keyClick(menu, Qt.Key.Key_Return)))
    assert cm.exec_element_menu(menu, keys, QPoint(10, 10)) == cm.DUPLICATE
    menu2, keys2 = cm.build_element_menu(None, "T", [cm.copy_entry(TX)])
    QTimer.singleShot(0, menu2.close)
    assert cm.exec_element_menu(menu2, keys2, QPoint(10, 10)) is None


def _move(record, dx, dy):
    record["x"] += dx
    record["y"] += dy
    return record


def test_clipboard_pastes_one_step_further_each_time_only_after_a_commit():
    clip = cm.ElementClipboard(_move)
    assert not clip and clip.copy([]) == 0
    assert clip.copy([{"x": 0.0, "y": 0.0}]) == 1 and len(clip) == 1
    step = (1.0, 2.0)
    assert clip.paste_batch(step) == [{"x": 1.0, "y": 2.0}]
    # Not committed: the next batch lands at the same place.
    assert clip.paste_batch(step) == [{"x": 1.0, "y": 2.0}]
    clip.advance(step)
    assert clip.paste_batch(step) == [{"x": 2.0, "y": 4.0}]


def test_clipboard_holds_copies_and_snapshot_restores():
    source = [{"x": 0.0, "y": 0.0}]
    clip = cm.ElementClipboard(_move)
    clip.copy(source)
    source[0]["x"] = 99.0
    assert clip.paste_batch((0, 0)) == [{"x": 0.0, "y": 0.0}]
    state = clip.snapshot()
    clip.copy([{"x": 5.0, "y": 5.0}])
    clip.restore(state)
    assert clip.paste_batch((0, 0)) == [{"x": 0.0, "y": 0.0}]


def test_vertex_entries_follow_the_pick():
    """@brief Segment pick enables «Aggiungi vertice», vertex pick «Rimuovi vertice», no pick neither."""
    from kicadgen.core.vertex_edit import SEGMENT, VERTEX, Pick
    on_segment = cm.vertex_entries(TX, Pick(SEGMENT, 0, (1.0, 0.0), 0.0))
    on_vertex = cm.vertex_entries(TX, Pick(VERTEX, 1, (2.0, 0.0), 0.0))
    off = cm.vertex_entries(TX, None)
    assert [e.key for e in on_segment] == [cm.ADD_VERTEX, cm.REMOVE_VERTEX]
    assert [e.label for e in on_segment] == ["<Aggiungi vertice>", "<Rimuovi vertice>"]
    assert [e.enabled for e in on_segment] == [True, False]
    assert [e.enabled for e in on_vertex] == [False, True]
    assert [e.enabled for e in off] == [False, False]
    assert on_segment[0].separator_before and not on_segment[1].separator_before
    assert cm.VERTEX_PICK_PX > 0


def test_the_vertex_reach_is_the_handle_half_diagonal():
    """@brief The reach holds the whole drawn handle square, corners included, and nothing is a new number."""
    import math
    from kicadgen.gui.canvas_gestures import HANDLE_SIZE_PX
    half = HANDLE_SIZE_PX / 2.0
    assert cm.VERTEX_PICK_PX == pytest.approx(math.hypot(half, half))
    assert cm.VERTEX_PICK_PX < HANDLE_SIZE_PX, 'smaller than the former full side'


def test_pick_reach_mm_converts_screen_pixels_at_the_zoom():
    """@brief px / (|scale| * units per mm); a degenerate or non-finite scale picks nothing."""
    assert cm.pick_reach_mm(2.0, 10.0) == pytest.approx(cm.VERTEX_PICK_PX / 20.0)
    assert cm.pick_reach_mm(-2.0, 10.0, px=4) == pytest.approx(0.2), 'a mirrored view has the same reach'
    for bad in (0.0, float('nan'), float('inf'), None):
        assert cm.pick_reach_mm(bad, 10.0) == 0.0
    assert cm.pick_reach_mm(1.0, 0.0) == 0.0


def test_every_refusal_reason_has_its_own_wording():
    """@brief Each user-facing reason of `vertex_edit` is worded; BAD_INDEX (a program error) uses the general one."""
    from kicadgen.core import vertex_edit as ve
    reasons = {ve.TOO_FEW_POINTS, ve.NO_AREA, ve.NO_LENGTH, ve.COINCIDENT, ve.DUPLICATE}
    assert set(cm.VERTEX_REFUSALS) == reasons
    assert len(set(cm.VERTEX_REFUSALS.values())) == len(reasons)
    assert cm.vertex_refusal_text(ve.BAD_INDEX) == cm.VERTEX_REFUSED
    assert cm.vertex_refusal_text(ve.DUPLICATE) == cm.VERTEX_REFUSALS[ve.DUPLICATE]


def test_the_refusal_words_live_only_in_canvas_menus():
    """@brief Both tabs read the public `canvas_menus` refusal words; the symbol tab keeps no private copy."""
    from kicadgen.gui import symbol_tab
    assert not hasattr(symbol_tab, '_VERTEX_REFUSALS') and not hasattr(symbol_tab, '_VERTEX_REFUSED')
    assert symbol_tab.canvas_menus.VERTEX_REFUSALS is cm.VERTEX_REFUSALS
    assert symbol_tab.canvas_menus.vertex_refusal_text is cm.vertex_refusal_text


def test_the_context_hit_margin_is_the_footprint_one():
    """@brief Parity D1: both tabs open a menu within the same screen margin of a stroke or handle."""
    from kicadgen.gui.footprint_view import HIT_MARGIN_PX
    assert cm.CONTEXT_HIT_MARGIN_PX == HIT_MARGIN_PX
