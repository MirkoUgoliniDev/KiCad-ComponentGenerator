"""@brief Non-graphical parameters of the component being generated.

@details The Footprint 2D and Genera tabs were removed on 2026-09-26 together
with the «Utente avanzato» mode. Their editors were, at the same time, the
store that the automatic generation reads: matrix and castellated footprint
parameters, the 3D body, description and tags. This module keeps that data
without any widget.

Each value keeps a default, a range and a precision (numbers are rounded to
0.01 mm for lengths). A value outside the range is clamped, and
`MainWindow._validate_automatic_input` rejects extracted values outside it
before they are applied. Since 2026-09-26 the ranges are PHYSICAL limits
(what cannot exist, e.g. a length of zero or beyond 300 mm, the largest board
the program handles), not the arbitrary spin-box limits of the removed
editors: a value that is merely unusual is applied and reported by a warning
in the log (`MainWindow._plausibility_warnings`), never refused.

@note The read/write methods deliberately keep the names the editors had
    (`value`/`setValue`, `text`/`setText`, `isChecked`/`setChecked`): every
    caller of the generation reads parameters through them, so replacing the
    storage does not change a single computation.
"""
from __future__ import annotations

from typing import Callable

#: Largest length the program handles, in mm: the edge of the largest board
#: (and of the preview canvas). Same limit as the board and body fields.
MAX_LENGTH_MM = 300.0
#: Smallest height of a body or component, in mm (a thin film or a flat
#: shield; below it a solid cannot be modelled meaningfully).
MIN_HEIGHT_MM = 0.05
#: Smallest length of a body, in mm (same order as the smallest chip
#: packages, 01005 = 0.4 x 0.2 mm).
MIN_LENGTH_MM = 0.05
#: Height sources that are a reading, not a guess: typed by the user, taken
#: from a datasheet drawing, or measured.
MEASURED_HEIGHT_SOURCES = ("user", "datasheet", "measured")

#: Keys of a castellated side as stored in `MainWindow._sides`.
SIDE_KEYS = ("edge", "count", "pitch_mm", "offset_mm", "first_pin", "reverse", "contact_style",
             "half_hole_diameter_mm", "through_hole_drill_mm",
             "half_hole_diameter_declared", "through_hole_drill_declared",
             "pad_length_mm", "pad_width_mm", "pad_length_declared", "pad_width_declared")


class _Observable:
    """Hold change listeners; a failing listener never corrupts the value."""

    def __init__(self) -> None:
        """@brief Start with no listeners; `on_change` registers them.

        @note Subclasses call this before storing their first value, so a
            listener can never be notified about the initial assignment.
        """
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
        """@brief Return the stored value (int for counts, float otherwise).

        @return The current value, already rounded and clamped.
        """
        return self._value

    def setValue(self, value) -> None:
        """@brief Store `value` rounded and clamped; notify when it changed.

        @param value New value; converted with float() first.
        @throws ValueError When `value` is a string that float() cannot parse.
        @throws TypeError When `value` is neither a number nor a string
            (None included).
        """
        normalised = self._normalise(value)
        if normalised != self._value:
            self._value = normalised
            self._notify(normalised)

    def minimum(self):
        """@brief Return the lowest accepted value.

        @return The lower clamp bound given at construction.
        """
        return self._minimum

    def maximum(self):
        """@brief Return the highest accepted value.

        @return The upper clamp bound given at construction.
        """
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
        """@brief Return the stored text.

        @return The current text, "" when empty.
        """
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
        """@brief Return the stored state.

        @return True when the flag is on.
        """
        return self._checked

    def setChecked(self, checked) -> None:
        """@brief Store the state; notify when it changed.

        @param checked New state, converted with bool().
        """
        checked = bool(checked)
        if checked != self._checked:
            self._checked = checked
            self._notify(checked)


def normalised_side(side: dict | None, default_style: str | None = None) -> dict:
    """@brief One castellated side, with its values brought into physical ranges.

    @param side Extracted or serialised side; missing keys take the table's
        defaults (left edge, 2 contacts, 2.54 mm pitch, first pin 1). The
        per-side hole sizes (`half_hole_diameter_mm`, `through_hole_drill_mm`)
        stay None when the side does not declare them (2026-09-26), and so do
        the per-side pad sizes (`pad_length_mm`, `pad_width_mm`, 2026-09-28):
        a side without them uses the module-wide pad fields, as before.
    @param default_style Contact style used when the side declares none.
        None (the default) keeps the style UNKNOWN: since 2026-09-26 a style
        that nobody observed is asked to the user, never filled in.
    @return A new dict with exactly the keys in SIDE_KEYS.
    @note Ranges: count 1..10000, pitch 0..MAX_LENGTH_MM, offset
        -MAX_LENGTH_MM..MAX_LENGTH_MM, first pin 1..100000; lengths rounded to
        0.01 mm. They only keep the numbers finite and storable: whether the
        contacts fit their edge is checked by the generator
        (`CastellatedModuleParams.validate`), from the declared board size.
    """
    side = side or {}
    count = BoundedValue(int(side.get("count", 2)), 1, 10000, decimals=0, integer=True)
    pitch = BoundedValue(float(side.get("pitch_mm", 2.54)), 0.0, MAX_LENGTH_MM)
    offset = BoundedValue(float(side.get("offset_mm", 0.0)), -MAX_LENGTH_MM, MAX_LENGTH_MM)
    first = BoundedValue(int(side.get("first_pin", 1)), 1, 100000, decimals=0, integer=True)
    return dict(edge=side.get("edge", "left"), count=count.value(), pitch_mm=pitch.value(),
                offset_mm=offset.value(), first_pin=first.value(),
                reverse=bool(side.get("reverse")),
                contact_style=side.get("contact_style") or default_style,
                half_hole_diameter_mm=_optional_size(side.get("half_hole_diameter_mm")),
                through_hole_drill_mm=_optional_size(side.get("through_hole_drill_mm")),
                # True ONLY if the analysis says it read or derived the
                # diameter from the documents: an estimate, a value without
                # provenance or a project saved before this key stay
                # estimates, which the generator may reduce, declaring it (26/09).
                half_hole_diameter_declared=side.get("half_hole_diameter_declared") is True,
                through_hole_drill_declared=side.get("through_hole_drill_declared") is True,
                # Pads of the side (28/09): same rule as the holes. Declared
                # only if read or derived from the documents (or typed by the
                # user); the generator reduces an estimate if it makes two
                # contacts touch, and writes so in the notes.
                pad_length_mm=_optional_size(side.get("pad_length_mm")),
                pad_width_mm=_optional_size(side.get("pad_width_mm")),
                pad_length_declared=side.get("pad_length_declared") is True,
                pad_width_declared=side.get("pad_width_declared") is True)


def _optional_size(value):
    """A per-side hole or pad size in mm (0.01 mm steps), or None when absent or invalid."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = round(float(value), 2)
    return number if 0 < number < 200 else None


def height_is_estimated(component: dict) -> bool:
    """@brief Whether the height of a photo component is an estimate, not a reading.

    @param component Photo component dict.
    @return The declared `_height_estimated` flag when it is a bool (the
        pipeline's attribute, which wins); otherwise True when a
        `_height_source` is declared and is not one of
        MEASURED_HEIGHT_SOURCES, and True when the dict has no `height_mm` at
        all (the editors then show a placeholder). A height with no declared
        provenance (older projects) is not flagged: nothing says it is a guess.
    """
    flag = component.get("_height_estimated")
    if isinstance(flag, bool):
        return flag
    source = component.get("_height_source")
    if isinstance(source, str) and source:
        return source not in MEASURED_HEIGHT_SOURCES
    return "height_mm" not in component


def mark_height_measured(component: dict) -> None:
    """@brief Record that the user typed the height of `component` by hand.

    @param component Photo component dict, changed in place.
    @note From here on the height is the user's reading: the «stimata» mark
        disappears from the review and the component editor.
    """
    component["_height_source"] = "user"
    component["_height_estimated"] = False


def mark_height_placeholder(component: dict) -> None:
    """@brief Record that the height of a body added by hand is a placeholder.

    @param component Photo component dict, changed in place.
    """
    component["_height_source"] = "placeholder_not_measured"
    component["_height_estimated"] = True
