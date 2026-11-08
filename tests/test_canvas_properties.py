"""@brief Shared live properties panel (plan footprint-drawing-kicad C1, contract 11)."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from kicadgen.gui.canvas_properties import MIXED_PLACEHOLDER, FieldSpec, PropertiesPanel

CHOICES = (("F.SilkS", "Serigrafia"), ("F.Fab", "Fabbricazione"))


def _specs(width=0.12, layer="F.SilkS", fill=False, text="A", mixed=()):
    return [
        FieldSpec("layer", "Layer", "choice", layer, mixed="layer" in mixed, choices=CHOICES),
        FieldSpec("width", "Spessore", "float", width, mixed="width" in mixed, minimum=0.0001,
                  maximum=10.0, step=0.01, decimals=4, suffix=" mm"),
        FieldSpec("count", "Lati", "int", 3, mixed="count" in mixed, minimum=3, maximum=64),
        FieldSpec("fill", "Riempimento", "bool", fill, mixed="fill" in mixed),
        FieldSpec("text", "Testo", "text", text, mixed="text" in mixed),
        FieldSpec("pad", "Piazzola", "readonly", 1.5, decimals=2, suffix=" mm"),
    ]


def _panel(**kwargs):
    panel = PropertiesPanel()
    seen = []
    panel.committed.connect(lambda key, value: seen.append((key, value)))
    panel.set_fields(_specs(**kwargs), title="Linea")
    return panel, seen


def test_set_fields_never_emits_even_when_values_change(qapp):
    panel, seen = _panel()
    panel.set_fields(_specs(width=0.5, layer="F.Fab", fill=True, text="B"))
    panel.set_fields(_specs(mixed=("width", "layer", "fill", "text", "count")))
    panel.set_empty()
    assert seen == []


def test_unchanged_signature_keeps_the_same_widgets_and_a_new_one_rebuilds(qapp):
    panel, _seen = _panel()
    before = {key: panel.field_widget(key) for key in panel.field_keys()}
    panel.set_fields(_specs(width=0.3, text="Z"))
    assert {key: panel.field_widget(key) for key in panel.field_keys()} == before
    assert panel.field_widget("width").value() == pytest.approx(0.3)
    assert panel.field_widget("text").text() == "Z"
    # Other choices: a different field set, so new widgets.
    other = _specs()
    other[0] = FieldSpec("layer", "Layer", "choice", "Cmts.User", choices=CHOICES + (("Cmts.User", "Commenti"),))
    panel.set_fields(other)
    assert panel.field_widget("layer") is not before["layer"]


def test_spin_commits_on_editing_finished_only_when_the_value_changed(qapp):
    panel, seen = _panel()
    box = panel.field_widget("width")
    QTest.keyClick(box, Qt.Key.Key_Return)
    assert seen == [], "Enter on an unchanged value is not a commit"
    box.setValue(0.25)
    assert seen == [], "a value change alone is not editingFinished"
    QTest.keyClick(box, Qt.Key.Key_Return)
    assert seen == [("width", pytest.approx(0.25))]
    QTest.keyClick(box, Qt.Key.Key_Return)
    assert len(seen) == 1, "the committed value is the new reference"


def test_spin_compares_with_the_shown_value_not_the_record(qapp):
    # 0.123456 is shown rounded to 4 decimals; Enter must not rewrite it.
    panel, seen = _panel(width=0.123456)
    QTest.keyClick(panel.field_widget("width"), Qt.Key.Key_Return)
    assert seen == []


def test_mixed_spin_is_empty_and_commits_only_when_typed(qapp):
    panel, seen = _panel(mixed=("width",))
    box = panel.field_widget("width")
    assert box.lineEdit().text() == "" and box.lineEdit().placeholderText() == MIXED_PLACEHOLDER
    QTest.keyClick(box, Qt.Key.Key_Return)
    assert seen == [] and panel.shown_value("width") is None
    QTest.keyClicks(box, box.locale().toString(0.5, "f", 1))
    QTest.keyClick(box, Qt.Key.Key_Return)
    assert seen == [("width", pytest.approx(0.5))]
    assert box.text().endswith(" mm"), "the suffix comes back after the typed value"


def test_leaving_the_mixed_state_on_refresh_shows_the_value(qapp):
    panel, _seen = _panel(mixed=("width",))
    panel.set_fields(_specs(width=0.2))
    box = panel.field_widget("width")
    assert box.text() == box.locale().toString(0.2, "f", 4) + " mm"


def test_mixed_text_choice_and_check_commit_only_on_user_input(qapp):
    panel, seen = _panel(mixed=("text", "layer", "fill"))
    edit, combo, check = (panel.field_widget(k) for k in ("text", "layer", "fill"))
    assert combo.currentIndex() == -1 and combo.placeholderText() == MIXED_PLACEHOLDER
    assert check.checkState() == Qt.CheckState.PartiallyChecked
    edit.editingFinished.emit()
    assert seen == []
    QTest.keyClicks(edit, "X")
    QTest.keyClick(edit, Qt.Key.Key_Return)
    combo.setCurrentIndex(1)
    combo.activated.emit(1)
    check.click()
    assert seen == [("text", "X"), ("layer", "F.Fab"), ("fill", True)]
    assert not check.isTristate()


def test_choice_and_check_commit_changes_but_not_programmatic_updates(qapp):
    panel, seen = _panel()
    combo, check = panel.field_widget("layer"), panel.field_widget("fill")
    combo.activated.emit(0)
    assert seen == [], "re-choosing the shown layer is not a change"
    combo.setCurrentIndex(1)
    combo.activated.emit(1)
    check.setChecked(True)
    assert seen == [("layer", "F.Fab")], "setChecked is not a user click"
    check.click()
    assert seen[-1] == ("fill", False)


def test_escape_restores_the_shown_value_without_committing(qapp):
    panel, seen = _panel(text="A")
    edit = panel.field_widget("text")
    QTest.keyClicks(edit, "BC")
    QTest.keyClick(edit, Qt.Key.Key_Escape)
    assert edit.text() == "A"
    QTest.keyClick(edit, Qt.Key.Key_Return)
    box = panel.field_widget("width")
    box.lineEdit().selectAll()
    QTest.keyClicks(box, "3")
    QTest.keyClick(box, Qt.Key.Key_Escape)
    QTest.keyClick(box, Qt.Key.Key_Return)
    assert seen == []
    assert box.value() == pytest.approx(0.12)


def test_escape_on_a_mixed_field_puts_back_the_placeholder(qapp):
    panel, seen = _panel(mixed=("width",))
    box = panel.field_widget("width")
    QTest.keyClicks(box, "1")
    QTest.keyClick(box, Qt.Key.Key_Escape)
    QTest.keyClick(box, Qt.Key.Key_Return)
    assert seen == [] and box.lineEdit().text() == ""


def test_readonly_disabled_and_empty_states(qapp):
    panel, _seen = _panel()
    assert panel.field_widget("pad").text() == "1.50 mm"
    specs = _specs()
    specs[1] = FieldSpec("width", "Spessore", "float", 0.1, enabled=False, tooltip="t")
    panel.set_fields(specs)
    assert not panel.field_widget("width").isEnabled()
    assert panel.field_widget("width").toolTip() == "t"
    panel.set_fields([])
    assert panel._placeholder.isVisibleTo(panel) and not panel._form_host.isVisibleTo(panel)
    assert panel._placeholder.text() == "Nessuna selezione"


def test_spin_boxes_do_not_take_wheel_focus(qapp):
    panel, _seen = _panel()
    assert panel.field_widget("width").focusPolicy() == Qt.FocusPolicy.StrongFocus
    assert panel.field_widget("layer").focusPolicy() == Qt.FocusPolicy.StrongFocus


def test_bad_specs_are_refused(qapp):
    panel = PropertiesPanel()
    with pytest.raises(ValueError):
        panel.set_fields([FieldSpec("a", "A", "colour")])
    with pytest.raises(ValueError):
        panel.set_fields([FieldSpec("a", "A", "int", 1), FieldSpec("a", "B", "int", 2)])


# ── Language switch: choice labels and the mixed placeholder (review M11) ──

EN_CHOICES = (("F.SilkS", "Silkscreen"), ("F.Fab", "Fabrication"))


def test_choice_labels_follow_the_specs_on_the_same_widget(qapp):
    panel, seen = _panel()
    combo = panel.field_widget("layer")
    specs = _specs(layer="F.Fab")
    specs[0] = FieldSpec("layer", "Layer", "choice", "F.Fab", choices=EN_CHOICES)
    panel.set_fields(specs)
    assert panel.field_widget("layer") is combo, "same data: relabelled, not rebuilt"
    assert [combo.itemText(i) for i in range(combo.count())] == ["Silkscreen", "Fabrication"]
    assert combo.currentData() == "F.Fab" and seen == []
    # Negative: unchanged labels leave the texts alone (and still never emit).
    panel.set_fields(specs)
    assert combo.itemText(0) == "Silkscreen" and seen == []


def test_mixed_placeholder_from_the_constructor_and_the_setter(qapp):
    panel = PropertiesPanel(mixed_placeholder="(various)")
    panel.set_fields(_specs(mixed=("width", "layer", "text")) + [
        FieldSpec("ro", "RO", "readonly", None, mixed=True)])
    assert panel.field_widget("width").lineEdit().placeholderText() == "(various)"
    assert panel.field_widget("layer").placeholderText() == "(various)"
    assert panel.field_widget("text").placeholderText() == "(various)"
    assert panel.field_widget("ro").text() == "(various)"
    panel.set_mixed_placeholder("(divers)")
    assert panel.mixed_placeholder() == "(divers)"
    assert panel.field_widget("width").lineEdit().placeholderText() == "(divers)"
    assert panel.field_widget("layer").placeholderText() == "(divers)"
    assert panel.field_widget("text").placeholderText() == "(divers)"
    assert panel.field_widget("ro").text() == "(divers)"
    # Negative: the default stays the Italian source text.
    assert PropertiesPanel().mixed_placeholder() == MIXED_PLACEHOLDER


# ── A refresh never wipes an uncommitted, focused edit (review M12) ──

def _focused(panel, key):
    """Show the panel and give the field's editor the keyboard focus."""
    panel.show()
    panel.activateWindow()
    widget = panel.field_widget(key)
    widget.setFocus()
    QApplication.processEvents()
    return widget


def test_refresh_keeps_the_text_typed_into_the_focused_spin(qapp):
    panel, seen = _panel(width=0.12)
    box = _focused(panel, "width")
    if not (box.hasFocus() or box.lineEdit().hasFocus()):
        pytest.skip("the platform gives no keyboard focus offscreen")
    box.lineEdit().selectAll()
    QTest.keyClicks(box, box.locale().toString(0.7, "f", 1))
    typed = box.lineEdit().text()
    panel.set_fields(_specs(width=0.12))                 # e.g. a toggle redrew the selection
    assert box.lineEdit().text() == typed, "the user's uncommitted text is kept"
    QTest.keyClick(box, Qt.Key.Key_Return)
    assert seen == [("width", pytest.approx(0.7))]
    panel.close()


def test_refresh_updates_a_focused_spin_the_user_did_not_touch(qapp):
    panel, seen = _panel(width=0.12)
    box = _focused(panel, "width")
    panel.set_fields(_specs(width=0.3))
    assert box.value() == pytest.approx(0.3), "focus alone does not hold a refresh back"
    assert seen == []
    panel.close()


def test_held_back_refresh_applies_when_the_edit_ends_without_commit(qapp):
    panel, seen = _panel(text="A")
    edit = _focused(panel, "text")
    if not edit.hasFocus():
        pytest.skip("the platform gives no keyboard focus offscreen")
    QTest.keyClicks(edit, "B")
    panel.set_fields(_specs(text="Z"))                   # another item's refresh while typing
    assert edit.text() == "AB"
    QTest.keyClick(edit, Qt.Key.Key_Escape)              # the user abandons the edit
    assert edit.text() == "Z", "the held-back refresh wins over the old value"
    assert seen == []
    panel.close()


def test_refresh_without_focus_overwrites_even_a_typed_field(qapp):
    panel, _seen = _panel(text="A")
    edit = panel.field_widget("text")
    edit.setText("typed")
    edit.textEdited.emit("typed")                        # typed, but the editor has no focus
    panel.set_fields(_specs(text="Z"))
    assert edit.text() == "Z"


# ── subject token (review N1): a typed edit never moves to another element ──

def test_a_typed_edit_is_dropped_when_the_subject_changes(qapp):
    """Positive: the selection moves (same field set) under a focused typed
    edit; Enter must NOT write the text into the new subject."""
    panel, seen = _panel(width=0.12)
    panel.set_fields(_specs(width=0.12), subject=("drawing", 0))
    box = _focused(panel, "width")
    if not (box.hasFocus() or box.lineEdit().hasFocus()):
        pytest.skip("the platform gives no keyboard focus offscreen")
    box.lineEdit().selectAll()
    QTest.keyClicks(box, box.locale().toString(0.33, "f", 2))
    panel.set_fields(_specs(width=0.25), subject=("drawing", 1))   # another line selected
    assert box.value() == pytest.approx(0.25), "the new subject's value is shown"
    QTest.keyClick(box, Qt.Key.Key_Return)
    assert seen == [], "the text typed for drawing 0 is not committed to drawing 1"
    panel.close()


def test_a_typed_edit_survives_a_refresh_of_the_same_subject(qapp):
    """Negative: the same subject redrawn (e.g. a toggle) keeps the typed text
    and Enter commits it."""
    panel, seen = _panel(width=0.12)
    panel.set_fields(_specs(width=0.12), subject=("drawing", 0))
    box = _focused(panel, "width")
    if not (box.hasFocus() or box.lineEdit().hasFocus()):
        pytest.skip("the platform gives no keyboard focus offscreen")
    box.lineEdit().selectAll()
    QTest.keyClicks(box, box.locale().toString(0.33, "f", 2))
    panel.set_fields(_specs(width=0.12), subject=("drawing", 0))
    QTest.keyClick(box, Qt.Key.Key_Return)
    assert seen == [("width", pytest.approx(0.33))]
    panel.close()


# ── FieldSpec.max_length (review B9) ──

def test_text_max_length_is_applied_and_zero_means_unlimited(qapp):
    panel = PropertiesPanel()
    panel.set_fields([FieldSpec("t", "Testo", "text", "abcdef", max_length=4)])
    edit = panel.field_widget("t")
    assert edit.maxLength() == 4 and edit.text() == "abcd"
    assert panel.shown_value("t") == "abcd", "the shown reference is what the widget holds"
    panel.set_fields([FieldSpec("t", "Testo", "text", "abcdef")])
    assert edit.maxLength() == 32767 and edit.text() == "abcdef"
