"""@brief Shared live «Proprietà» panel of the symbol editor and the footprint preview.

@details Plan docs/footprint-drawing-kicad-plan.md, C1 and contract 11. The
host describes the selection as a list of `FieldSpec` and calls
`PropertiesPanel.set_fields`; the panel shows one compact row per field and
reports a user edit as `committed(key, value)`. The host turns each commit
into one undo step through the same record builder its dialog uses, then
refreshes the panel with the new values.

Contract:

* a programmatic refresh (`set_fields`, `set_empty`) never emits;
* widgets are rebuilt only when the field set changes (keys, kinds, choices);
  otherwise the same widgets get the new values with their signals blocked,
  so a refresh after a commit does not steal the focus or flicker;
* a commit happens on editingFinished (spin boxes, line edit; Enter or focus
  loss), on a user click (check box) or a user choice (drop-down), and only
  when the value differs from the one the widget itself showed: a spin box
  rounds and clamps, so the comparison is never with the host's record;
* a mixed field (several selected items disagree) shows an empty field with
  the placeholder «(vari)» (the host passes it translated, `set_mixed_placeholder`)
  and is committed only once the user types, steps or chooses a value;
* Escape restores the shown value (and the mixed state) without committing;
* a refresh never overwrites the editor that has the keyboard focus while
  the user has typed into it without confirming: the typed text stays, and
  the refreshed value is applied when the edit ends (Enter, focus loss) with
  no commit, or by the next refresh once the field has lost the focus;
* the labels of a drop-down follow the specs: a language switch that only
  renames the choices relabels the same widget, silently.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from PyQt6.QtCore import QEvent, QObject, Qt, pyqtSignal
from PyQt6.QtGui import QFont, QValidator
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QLabel, QLineEdit,
                             QSizePolicy, QSpinBox, QVBoxLayout, QWidget)

from .theme import SUBTEXT

#: Field kinds a `FieldSpec` may declare.
FIELD_KINDS = ("float", "int", "choice", "bool", "text", "readonly")

#: Placeholder of a mixed value (Italian source text, translated by the host's pass).
MIXED_PLACEHOLDER = "(vari)"

#: Default placeholder of an empty panel (Italian source text).
EMPTY_TEXT = "Nessuna selezione"

#: Sentinel of `PropertiesPanel.set_fields(subject=)`: "subject not given".
_NO_SUBJECT = object()

#: Largest length Qt allows in a QLineEdit, used when a field declares no limit.
_QT_MAX_LENGTH = 32767

#: Range used when a numeric field declares no bound: wide enough that no
#: shown value is clamped (the host's validator refuses with its own reason).
_FLOAT_LIMIT = 1.0e6
_INT_LIMIT = 2 ** 31 - 1


@dataclass(frozen=True)
class FieldSpec:
    """@brief Description of one row of the properties panel.

    @note `value` is ignored when `mixed` is True; `choices` is a tuple of
        (data, label) pairs for kind "choice"; `minimum`/`maximum` None mean
        a range wide enough not to clamp; `step` None means 0.1 for floats and
        1 for integers. Units belong in `suffix` (e.g. " mm"). `max_length`
        limits a "text" field's characters as the host's dialog does (0 =
        unlimited, i.e. Qt's own 32767).
    """

    key: str
    label: str
    kind: str
    value: Any = None
    mixed: bool = False
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    step: Optional[float] = None
    decimals: int = 2
    suffix: str = ""
    choices: tuple = ()
    enabled: bool = True
    tooltip: str = ""
    max_length: int = 0


def _signature(specs) -> tuple:
    """The structure that decides a rebuild: keys, kinds and choice data in order."""
    return tuple((s.key, s.kind, tuple(data for data, _label in s.choices)) for s in specs)


class _MixedSpinMixin:
    """Mixed-value behaviour shared by the float and integer spin boxes.

    While mixed the box shows an empty text (its placeholder «(vari)») and no
    suffix; typing or stepping ends the mixed state. The suffix comes back at
    editingFinished, not while typing: restoring it mid-typing would make Qt
    redisplay the value and wipe what the user typed.
    """

    def _init_mixed(self, placeholder: str = MIXED_PLACEHOLDER) -> None:
        """Set up the mixed flag and watch user typing."""
        self._mixed = False
        self._suffix_text = ""
        self.lineEdit().setPlaceholderText(placeholder)
        self.lineEdit().textEdited.connect(self._typed)

    def set_suffix_text(self, suffix: str) -> None:
        """Remember the unit suffix and show it unless mixed."""
        self._suffix_text = suffix
        self.setSuffix("" if self._mixed else suffix)

    def set_mixed(self, mixed: bool) -> None:
        """Enter or leave the mixed state (the caller blocks signals)."""
        self._mixed = bool(mixed)
        self.setSuffix("" if self._mixed else self._suffix_text)
        if self._mixed:
            self.lineEdit().clear()

    def is_mixed(self) -> bool:
        """True while the box still shows «(vari)» untouched."""
        return self._mixed

    def restore_suffix(self) -> None:
        """Put the suffix back after the user ended the mixed state."""
        if not self._mixed and self.suffix() != self._suffix_text:
            self.setSuffix(self._suffix_text)

    def _typed(self, _text: str) -> None:
        """A user keystroke ends the mixed state; the suffix waits for editingFinished."""
        self._mixed = False

    def _mixed_text(self, value, base):
        """Empty while mixed, else the base class text."""
        return "" if self._mixed else base(value)

    def _mixed_validate(self, text, pos, base):
        """An empty text is acceptable while mixed."""
        if self._mixed and text == "":
            return QValidator.State.Acceptable, text, pos
        return base(text, pos)

    def _mixed_value(self, text, base):
        """An empty text keeps the current value while mixed."""
        if self._mixed and text == "":
            return self.value()
        return base(text)

    def _mixed_step(self, steps, base) -> None:
        """Stepping ends the mixed state and steps from the stored value."""
        if self._mixed:
            self._mixed = False
            self.setSuffix(self._suffix_text)
        base(steps)


class _DoubleSpin(_MixedSpinMixin, QDoubleSpinBox):
    """Float field of the panel, with the mixed state."""

    def __init__(self, parent=None, placeholder: str = MIXED_PLACEHOLDER) -> None:
        """@brief Create the box and its mixed-state wiring.

        @param parent Parent widget.
        @param placeholder Text shown while the value is mixed.
        """
        super().__init__(parent)
        self._init_mixed(placeholder)

    def textFromValue(self, value):  # noqa: N802 - Qt override
        """@brief Empty while mixed, else Qt's text.

        @param value Value to show.
        @return Text without prefix and suffix.
        """
        return self._mixed_text(value, super().textFromValue)

    def validate(self, text, pos):
        """@brief Accept the empty text while mixed.

        @param text Text being validated.
        @param pos Cursor position.
        @return (state, text, pos).
        """
        return self._mixed_validate(text, pos, super().validate)

    def valueFromText(self, text):  # noqa: N802 - Qt override
        """@brief Keep the stored value for the empty mixed text.

        @param text Text to interpret.
        @return The value.
        """
        return self._mixed_value(text, super().valueFromText)

    def stepBy(self, steps):  # noqa: N802 - Qt override
        """@brief Step, ending the mixed state first.

        @param steps Number of steps.
        """
        self._mixed_step(steps, super().stepBy)


class _IntSpin(_MixedSpinMixin, QSpinBox):
    """Integer field of the panel, with the mixed state."""

    def __init__(self, parent=None, placeholder: str = MIXED_PLACEHOLDER) -> None:
        """@brief Create the box and its mixed-state wiring.

        @param parent Parent widget.
        @param placeholder Text shown while the value is mixed.
        """
        super().__init__(parent)
        self._init_mixed(placeholder)

    def textFromValue(self, value):  # noqa: N802 - Qt override
        """@brief Empty while mixed, else Qt's text.

        @param value Value to show.
        @return Text without prefix and suffix.
        """
        return self._mixed_text(value, super().textFromValue)

    def validate(self, text, pos):
        """@brief Accept the empty text while mixed.

        @param text Text being validated.
        @param pos Cursor position.
        @return (state, text, pos).
        """
        return self._mixed_validate(text, pos, super().validate)

    def valueFromText(self, text):  # noqa: N802 - Qt override
        """@brief Keep the stored value for the empty mixed text.

        @param text Text to interpret.
        @return The value.
        """
        return self._mixed_value(text, super().valueFromText)

    def stepBy(self, steps):  # noqa: N802 - Qt override
        """@brief Step, ending the mixed state first.

        @param steps Number of steps.
        """
        self._mixed_step(steps, super().stepBy)


class PropertiesPanel(QWidget):
    """@brief Compact form of editable properties for the current selection.

    @note Sized for a 250 px side column: labels and fields shrink, long
        rows wrap. Spin boxes and drop-downs follow the application wheel
        guard (`wheel_guard.install`): the wheel never changes an unfocused
        value.
    """

    #: The user changed a field: its key and the new value (float, int,
    #: the choice's data, bool or str by kind).
    committed = pyqtSignal(str, object)

    def __init__(self, parent: Optional[QWidget] = None, mixed_placeholder: str = MIXED_PLACEHOLDER) -> None:
        """@brief Create an empty panel showing the «Nessuna selezione» placeholder.

        @param parent Parent widget.
        @param mixed_placeholder Placeholder of a mixed field, already
            translated by the host (default the Italian source «(vari)»).
        """
        super().__init__(parent)
        self._mixed_placeholder = str(mixed_placeholder)
        from .wheel_guard import install as install_wheel_guard
        install_wheel_guard()
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        self._title = QLabel("")
        title_font = QFont(self._title.font())
        title_font.setBold(True)
        self._title.setFont(title_font)
        self._title.setWordWrap(True)
        self._title.setVisible(False)
        column.addWidget(self._title)
        self._placeholder = QLabel(EMPTY_TEXT)
        self._placeholder.setStyleSheet(f"color: {SUBTEXT};")
        self._placeholder.setWordWrap(True)
        column.addWidget(self._placeholder)
        self._form_host = QWidget()
        self._form = QFormLayout(self._form_host)
        self._form.setContentsMargins(0, 0, 0, 0)
        self._form.setHorizontalSpacing(6)
        self._form.setVerticalSpacing(2)
        self._form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self._form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self._form_host.setVisible(False)
        column.addWidget(self._form_host)
        self._signature: tuple = ()
        self._specs: dict[str, FieldSpec] = {}
        self._widgets: dict[str, QWidget] = {}
        self._labels: dict[str, QLabel] = {}
        # What each widget showed after the last refresh or commit: the
        # reference of "changed" (see the module contract).
        self._shown: dict[str, Any] = {}
        # True while the user has typed into a spin box or line edit since the
        # last refresh, restore or commit: an uncommitted edit (contract M12).
        self._text_edited: dict[str, bool] = {}
        # Specs whose refresh was held back because their editor had the focus
        # with an uncommitted edit; applied when that edit ends.
        self._pending: dict[str, FieldSpec] = {}
        # The host's token of what the fields describe (`set_fields(subject=)`).
        self._subject: Any = _NO_SUBJECT
        self._key_filter = _EscapeFilter(self)

    # ── Host API ──────────────────────────────────────────────────────────

    def set_empty(self, text: str = EMPTY_TEXT) -> None:
        """@brief Show only a grey placeholder (no selection, or nothing editable).

        @param text Placeholder text (already translated by the host, or the Italian source).
        """
        self._placeholder.setText(text)
        self._placeholder.setVisible(True)
        self._title.setVisible(False)
        self._form_host.setVisible(False)

    def set_mixed_placeholder(self, text: str) -> None:
        """@brief Change the placeholder of mixed fields (the host passes the translated «(vari)»).

        @param text New placeholder; existing widgets and read-only mixed
            fields are updated at once, silently.
        """
        self._mixed_placeholder = str(text)
        for key, widget in self._widgets.items():
            if isinstance(widget, (QDoubleSpinBox, QSpinBox)):
                widget.lineEdit().setPlaceholderText(self._mixed_placeholder)
            elif isinstance(widget, (QComboBox, QLineEdit)):
                widget.setPlaceholderText(self._mixed_placeholder)
            elif isinstance(widget, QLabel):
                spec = self._specs.get(key)
                if spec is not None and spec.kind == "readonly":
                    widget.setText(self._readonly_text(spec))

    def mixed_placeholder(self) -> str:
        """@brief The placeholder of mixed fields.

        @return The text last set (constructor or `set_mixed_placeholder`).
        """
        return self._mixed_placeholder

    def set_fields(self, specs, title: str = "", subject: Any = _NO_SUBJECT) -> None:
        """@brief Show the given fields; never emits `committed`.

        @param specs Sequence of FieldSpec, in display order; empty shows the placeholder.
        @param title Optional bold heading (e.g. the selected element's kind).
        @param subject Optional hashable token of WHAT the fields describe (the
            host's selection). When it differs from the previous call's, an
            uncommitted typed edit is DISCARDED and the new values are shown:
            text typed for one element must never be committed to another
            (review N1, 2026-11-08). Omitted = same subject as before.
        @throws ValueError When a spec has an unknown kind or two specs share a key.
        @note A field whose editor has the focus with an uncommitted typed
            edit keeps the user's text while the subject stays the same; its
            new spec is applied when the edit ends without a commit (see the
            module contract).
        """
        if subject is not _NO_SUBJECT and subject != self._subject:
            # The selection moved under a pending edit: drop it rather than
            # let Enter write it into the newly selected element.
            self._subject = subject
            self._text_edited.clear()
            self._pending.clear()
        specs = list(specs)
        keys = [s.key for s in specs]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate field keys")
        for spec in specs:
            if spec.kind not in FIELD_KINDS:
                raise ValueError(f"unknown field kind: {spec.kind!r}")
        if not specs:
            self.set_empty(self._placeholder.text())
            return
        signature = _signature(specs)
        if signature != self._signature:
            self._rebuild(specs)
            self._signature = signature
        for spec in specs:
            self._show(spec)
        self._title.setText(title)
        self._title.setVisible(bool(title))
        self._placeholder.setVisible(False)
        self._form_host.setVisible(True)

    def field_widget(self, key: str) -> Optional[QWidget]:
        """@brief The editor widget of a field (for tests and focus handling).

        @param key Field key.
        @return The widget, or None when the field is not shown.
        """
        return self._widgets.get(key)

    def field_keys(self) -> list:
        """@brief Keys of the fields currently built, in display order.

        @return A new list.
        """
        return list(self._widgets)

    def shown_value(self, key: str) -> Any:
        """@brief The value a field showed after the last refresh or commit (None when mixed).

        @param key Field key.
        @return The value, or None.
        """
        return self._shown.get(key)

    # ── Building ──────────────────────────────────────────────────────────

    def _rebuild(self, specs) -> None:
        """Replace every row with new widgets for the new field set."""
        while self._form.rowCount():
            self._form.removeRow(0)
        self._widgets.clear()
        self._labels.clear()
        self._shown.clear()
        self._text_edited.clear()
        self._pending.clear()
        for spec in specs:
            widget = self._make_widget(spec)
            widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            widget.setMinimumWidth(0)
            if spec.kind == "bool":
                # A check box carries its own label, as in the drawing dialog.
                self._form.addRow(widget)
            else:
                label = QLabel(spec.label)
                label.setWordWrap(True)
                self._labels[spec.key] = label
                self._form.addRow(label, widget)
            self._widgets[spec.key] = widget

    def _make_widget(self, spec: FieldSpec) -> QWidget:
        """One editor for a spec, wired to the commit handlers."""
        key = spec.key
        if spec.kind in ("float", "int"):
            box = (_DoubleSpin if spec.kind == "float" else _IntSpin)(placeholder=self._mixed_placeholder)
            box.setKeyboardTracking(False)
            box.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            box.lineEdit().textEdited.connect(lambda _text, k=key: self._text_edited.__setitem__(k, True))
            box.editingFinished.connect(lambda k=key: self._spin_finished(k))
            # Keys reach the inner line edit first (the box's focus proxy).
            box.installEventFilter(self._key_filter)
            box.lineEdit().installEventFilter(self._key_filter)
            return box
        if spec.kind == "choice":
            combo = QComboBox()
            combo.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            combo.setPlaceholderText(self._mixed_placeholder)
            for data, label in spec.choices:
                combo.addItem(label, data)
            combo.activated.connect(lambda _index, k=key: self._choice_activated(k))
            return combo
        if spec.kind == "bool":
            check = QCheckBox(spec.label)
            # `clicked` is user-only: a programmatic setChecked never commits.
            check.clicked.connect(lambda _checked=False, k=key: self._check_clicked(k))
            return check
        if spec.kind == "text":
            edit = QLineEdit()
            edit.setPlaceholderText(self._mixed_placeholder)
            edit.textEdited.connect(lambda _text, k=key: self._text_edited.__setitem__(k, True))
            edit.editingFinished.connect(lambda k=key: self._text_finished(k))
            edit.installEventFilter(self._key_filter)
            return edit
        label = QLabel()
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label

    # ── Showing values (signals blocked) ──────────────────────────────────

    def _editing(self, key: str, widget: QWidget) -> bool:
        """True when the field's editor has the focus and holds a typed, uncommitted edit."""
        if not self._text_edited.get(key):
            return False
        inner = widget.lineEdit() if isinstance(widget, (QDoubleSpinBox, QSpinBox)) else None
        return widget.hasFocus() or (inner is not None and inner.hasFocus())

    def _show(self, spec: FieldSpec) -> None:
        """Put a spec's value, bounds, state and labels on its existing widget, silently.

        A field being edited (focus + uncommitted typing) keeps the user's
        text: only its label is refreshed and the spec waits in `_pending`.
        """
        widget = self._widgets[spec.key]
        label = self._labels.get(spec.key)
        if label is not None:
            label.setText(spec.label)
            label.setToolTip(spec.tooltip)
            label.setEnabled(spec.enabled)
        if self._editing(spec.key, widget):
            self._pending[spec.key] = spec
            return
        self._pending.pop(spec.key, None)
        self._specs[spec.key] = spec
        widget.setToolTip(spec.tooltip)
        widget.setEnabled(spec.enabled)
        blocked = widget.blockSignals(True)
        try:
            self._shown[spec.key] = self._put_value(spec, widget)
        finally:
            widget.blockSignals(blocked)
        self._text_edited[spec.key] = False

    def _put_value(self, spec: FieldSpec, widget: QWidget) -> Any:
        """Write the value on the widget; return what it shows (None when mixed)."""
        if spec.kind in ("float", "int"):
            if spec.kind == "float":
                widget.setDecimals(int(spec.decimals))
                low = -_FLOAT_LIMIT if spec.minimum is None else float(spec.minimum)
                high = _FLOAT_LIMIT if spec.maximum is None else float(spec.maximum)
                widget.setSingleStep(0.1 if spec.step is None else float(spec.step))
            else:
                low = -_INT_LIMIT if spec.minimum is None else int(spec.minimum)
                high = _INT_LIMIT if spec.maximum is None else int(spec.maximum)
                widget.setSingleStep(1 if spec.step is None else int(spec.step))
            widget.setRange(low, high)
            # Mixed first, so the suffix and the empty text are decided before the value.
            widget.set_mixed(spec.mixed)
            widget.set_suffix_text(spec.suffix)
            if spec.value is not None:
                widget.setValue(float(spec.value) if spec.kind == "float" else int(spec.value))
            if spec.mixed:
                widget.lineEdit().clear()
                return None
            # The widget's own value: rounded to its decimals and clamped to its range.
            return widget.value()
        if spec.kind == "choice":
            # Same data (the signature), maybe other labels: a language switch relabels in place.
            for row, (_data, text) in enumerate(spec.choices):
                if widget.itemText(row) != text:
                    widget.setItemText(row, text)
            widget.setPlaceholderText(self._mixed_placeholder)
            index = -1 if spec.mixed else widget.findData(spec.value)
            widget.setCurrentIndex(index)
            return None if index < 0 else widget.itemData(index)
        if spec.kind == "bool":
            widget.setText(spec.label)
            if spec.mixed:
                widget.setTristate(True)
                widget.setCheckState(Qt.CheckState.PartiallyChecked)
                return None
            widget.setTristate(False)
            widget.setChecked(bool(spec.value))
            return widget.isChecked()
        if spec.kind == "text":
            limit = int(spec.max_length) if spec.max_length and spec.max_length > 0 else _QT_MAX_LENGTH
            widget.setMaxLength(min(limit, _QT_MAX_LENGTH))
            text = "" if spec.mixed or spec.value is None else str(spec.value)
            widget.setText(text)
            # What the widget really shows (a longer value is cut at max_length).
            return None if spec.mixed else widget.text()
        widget.setText(self._readonly_text(spec))
        return None if spec.mixed else spec.value

    def _readonly_text(self, spec: FieldSpec) -> str:
        """Display text of a read-only field: floats with their decimals, then the suffix."""
        if spec.mixed:
            return self._mixed_placeholder
        if spec.value is None:
            return ""
        if isinstance(spec.value, float):
            return f"{spec.value:.{int(spec.decimals)}f}{spec.suffix}"
        return f"{spec.value}{spec.suffix}"

    def restore(self, key: str) -> None:
        """@brief Put back the last shown value of a field (Escape), without committing.

        @param key Field key; unknown keys are ignored.
        """
        spec = self._specs.get(key)
        if spec is None or key not in self._widgets:
            return
        # Escape abandons the typed edit: a refresh held back meanwhile wins over the old value.
        self._text_edited[key] = False
        pending = self._pending.pop(key, None)
        if pending is not None:
            self._show(pending)
            return
        if spec.kind in ("float", "int") and not spec.mixed:
            # The value shown after the last commit may differ from the spec's.
            spec = _replace_value(spec, self._shown.get(key, spec.value))
        elif spec.kind == "text" and not spec.mixed:
            spec = _replace_value(spec, self._shown.get(key, spec.value))
        self._show(spec)

    # ── Commits ───────────────────────────────────────────────────────────

    def _commit(self, key: str, value: Any) -> None:
        """Record the new shown value and emit it (the host's refresh then applies to this field)."""
        self._shown[key] = value
        self._text_edited[key] = False
        self._pending.pop(key, None)
        spec = self._specs.get(key)
        if spec is not None and spec.mixed:
            # The field now holds one value for every selected item.
            self._specs[key] = _replace_value(spec, value, mixed=False)
        self.committed.emit(key, value)

    def _spin_finished(self, key: str) -> None:
        """editingFinished of a spin box: commit only a real change, never an untouched mixed field."""
        box = self._widgets.get(key)
        if box is None:
            return
        if not box.is_mixed():
            box.restore_suffix()
            value = box.value()
            if self._shown.get(key) is None or value != self._shown[key]:
                self._commit(key, value)
        self._edit_ended(key)

    def _edit_ended(self, key: str) -> None:
        """The typed edit of a field ended (Enter or focus loss): apply a refresh held back meanwhile."""
        self._text_edited[key] = False
        pending = self._pending.pop(key, None)
        if pending is not None and key in self._widgets:
            self._show(pending)

    def _choice_activated(self, key: str) -> None:
        """A user choice in a drop-down: commit when it differs from the shown one (or the field was mixed)."""
        combo = self._widgets[key]
        value = combo.currentData()
        if combo.currentIndex() < 0 or (self._shown.get(key) is not None and value == self._shown[key]):
            return
        self._commit(key, value)

    def _check_clicked(self, key: str) -> None:
        """A user click on a check box: a mixed box becomes two-state and commits its new state."""
        check = self._widgets[key]
        if check.isTristate():
            blocked = check.blockSignals(True)
            check.setTristate(False)
            check.setChecked(True)
            check.blockSignals(blocked)
        self._commit(key, check.isChecked())

    def _text_finished(self, key: str) -> None:
        """editingFinished of a line edit: commit a typed change; an untouched mixed field stays."""
        edit = self._widgets[key]
        spec = self._specs.get(key)
        if not (spec is not None and spec.mixed and not self._text_edited.get(key)):
            text = edit.text()
            if self._shown.get(key) is None or text != self._shown[key]:
                self._commit(key, text)
        self._edit_ended(key)


def _replace_value(spec: FieldSpec, value: Any, mixed: Optional[bool] = None) -> FieldSpec:
    """A copy of a frozen spec with another value (and optionally another mixed flag)."""
    from dataclasses import replace
    return replace(spec, value=value, mixed=spec.mixed if mixed is None else mixed)


class _EscapeFilter(QObject):
    """Escape on a panel editor restores its shown value instead of reaching the canvas."""

    def __init__(self, panel: PropertiesPanel) -> None:
        """@brief Keep the panel whose fields this filter restores.

        @param panel The owning panel (also the QObject parent).
        """
        super().__init__(panel)
        self._panel = panel

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt override
        """@brief Swallow Escape on a field editor and restore the field.

        @param watched The editor receiving the event.
        @param event The event.
        @return True when Escape was handled.
        """
        if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
            for key, widget in self._panel._widgets.items():
                inner = widget.lineEdit() if isinstance(widget, (QDoubleSpinBox, QSpinBox)) else None
                if widget is watched or inner is watched:
                    self._panel.restore(key)
                    return True
        return False
