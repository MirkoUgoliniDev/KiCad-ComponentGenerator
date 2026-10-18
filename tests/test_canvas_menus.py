"""@brief Shared element menu and clipboard (`kicadgen.gui.canvas_menus`)."""
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
