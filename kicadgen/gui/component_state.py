"""@brief Non-graphical parameters of the component being generated.

@details The Footprint 2D and Genera tabs were removed on 2026-09-26 together
with the «Utente avanzato» mode. Their editors were, at the same time, the
store that the automatic generation reads: matrix and castellated footprint
parameters, the 3D body, description and tags. This module keeps that data
without any widget.

Each value keeps the contract of the editor it replaces, because the
generation relies on it: the same default, the same range (a value outside
it is clamped, and `MainWindow._validate_automatic_input` rejects extracted
values outside it before they are applied) and the same precision (numbers
are rounded to the editor's decimals, 0.01 mm for lengths).

@note The read/write methods deliberately keep the names the editors had
    (`value`/`setValue`, `text`/`setText`, `isChecked`/`setChecked`): every
    caller of the generation reads parameters through them, so replacing the
    storage does not change a single computation.
"""
from __future__ import annotations

from typing import Callable

#: Keys of a castellated side as stored in `MainWindow._sides`.
SIDE_KEYS = ("edge", "count", "pitch_mm", "offset_mm", "first_pin", "reverse", "contact_style",
             "half_hole_diameter_mm", "through_hole_drill_mm")


class _Observable:
    """Hold change listeners; a failing listener never corrupts the value."""

    def __init__(self) -> None:
        self._listeners: list[Callable] = []

    def on_change(self, callback: Callable) -> None:
        """@brief Call `callback(new_value)` after every actual change.

        @param callback Listener; called only when the stored value changes,
            like Qt's valueChanged/toggled/textChanged.
        """
        self._listeners.append(callback)

    def _notify(self, value) -> None:
        for callback in list(self._listeners):
            callback(value)


class BoundedValue(_Observable):
    """@brief A number with the range and precision of the editor it replaces.

    @note Same order as QDoubleSpinBox: round to the declared decimals, then
        clamp into [minimum, maximum]. Integers (`decimals=0, integer=True`)
        truncate like QSpinBox after rounding.
    """

    def __init__(self, value: float, minimum: float, maximum: float, *,
                 decimals: int = 2, integer: bool = False) -> None:
        """@brief Create the value already normalised.

        @param value Default value.
        @param minimum Lowest accepted value.
        @param maximum Highest accepted value.
        @param decimals Digits kept after the decimal point.
        @param integer True for counts: the value is an int.
        """
        super().__init__()
        if minimum > maximum:
            raise ValueError("minimum must not exceed maximum")
        self._minimum, self._maximum = minimum, maximum
        self._decimals, self._integer = decimals, integer
        self._value = self._normalise(value)

    def _normalise(self, value):
        number = round(float(value), 0 if self._integer else self._decimals)
        number = min(max(number, self._minimum), self._maximum)
        return int(number) if self._integer else number

    def value(self):
        """@brief Return the stored value (int for counts, float otherwise)."""
        return self._value

    def setValue(self, value) -> None:
        """@brief Store `value` rounded and clamped; notify when it changed.

        @param value New value; converted with float() first.
        @throws ValueError When `value` is not a number.
        """
        normalised = self._normalise(value)
        if normalised != self._value:
            self._value = normalised
            self._notify(normalised)

    def minimum(self):
        """@brief Return the lowest accepted value."""
        return self._minimum

    def maximum(self):
        """@brief Return the highest accepted value."""
        return self._maximum


class TextValue(_Observable):
    """@brief A free text parameter (description, tags, colors, pin lists)."""

    def __init__(self, text: str = "") -> None:
        """@brief Create the value.

        @param text Default text.
        """
        super().__init__()
        self._text = str(text)

    def text(self) -> str:
        """@brief Return the stored text."""
        return self._text

    def setText(self, text) -> None:
        """@brief Store `text`; notify when it changed.

        @param text New text; None counts as empty.
        """
        text = "" if text is None else str(text)
        if text != self._text:
            self._text = text
            self._notify(text)

    def clear(self) -> None:
        """@brief Empty the text."""
        self.setText("")


class FlagValue(_Observable):
    """@brief An on/off parameter (castellated layout, 3D model requested)."""

    def __init__(self, checked: bool = False) -> None:
        """@brief Create the flag.

        @param checked Default state.
        """
        super().__init__()
        self._checked = bool(checked)

    def isChecked(self) -> bool:
        """@brief Return the stored state."""
        return self._checked

    def setChecked(self, checked) -> None:
        """@brief Store the state; notify when it changed.

        @param checked New state, converted with bool().
        """
        checked = bool(checked)
        if checked != self._checked:
            self._checked = checked
            self._notify(checked)


def normalised_side(side: dict | None, default_style: str) -> dict:
    """@brief One castellated side with the ranges of the former sides table.

    @param side Extracted or serialised side; missing keys take the table's
        defaults (left edge, 2 contacts, 2.54 mm pitch, first pin 1). The
        per-side hole sizes (`half_hole_diameter_mm`, `through_hole_drill_mm`)
        stay None when the side does not declare them (2026-09-26).
    @param default_style Contact style used when the side declares none.
    @return A new dict with exactly the keys in SIDE_KEYS.
    @note Ranges: count 1..100, pitch 0..200 mm, offset -200..200 mm,
        first pin 1..999; lengths rounded to 0.01 mm.
    """
    side = side or {}
    count = BoundedValue(int(side.get("count", 2)), 1, 100, decimals=0, integer=True)
    pitch = BoundedValue(float(side.get("pitch_mm", 2.54)), 0.0, 200.0)
    offset = BoundedValue(float(side.get("offset_mm", 0.0)), -200.0, 200.0)
    first = BoundedValue(int(side.get("first_pin", 1)), 1, 999, decimals=0, integer=True)
    return dict(edge=side.get("edge", "left"), count=count.value(), pitch_mm=pitch.value(),
                offset_mm=offset.value(), first_pin=first.value(),
                reverse=bool(side.get("reverse")),
                contact_style=side.get("contact_style") or default_style,
                half_hole_diameter_mm=_optional_size(side.get("half_hole_diameter_mm")),
                through_hole_drill_mm=_optional_size(side.get("through_hole_drill_mm")))


def _optional_size(value):
    """A declared hole size in mm (0.01 mm steps), or None when not declared or invalid."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = round(float(value), 2)
    return number if 0 < number < 200 else None
