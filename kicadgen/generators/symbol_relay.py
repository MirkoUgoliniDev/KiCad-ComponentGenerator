"""@brief Compose a relay's electrical symbol -- coil, dashed link, contacts -- from DECLARED pin functions.

The single-part analysis reads, for every pin of the pinout, a closed
`function` the document shows (a table or a drawn circuit symbol):
``coil``, ``common``, ``nc``, ``no`` or ``other``. From the COUNT of those
functions, never from a part name or from the free-text pin names, this
module lays out the symbol KiCad's own `Relay_SPDT` family draws: the coil
on the left, a dashed link, then one contact per common pin (changeover,
normally open or normally closed), all inside the body rectangle, with the
pin names hidden as KiCad hides them (user, 2026-11-05: «un simbolo più
adeguato, mettendo la bobina e i contatti»).

Every piece is a glyph of `symbol_glyphs.relay_part_glyphs` (the palette
the user composes relays with by hand), so the generated draft is editable
exactly like a hand-made one: the rows carry `symbol_position`,
`symbol_angle`, `symbol_body`, `symbol_graphics` and
`symbol_pin_names_hidden`, nothing the editor does not already know.

Declared limits:
- a relay with more than one coil (two windings, four coil pins), a pin
  whose function is ``other`` or missing, or two pins of the same role in
  one contact group is NOT composed: the plain box stays (`relay_plan`
  returns None with the reason). The program never guesses a function it
  was not given.
- the contact a NC/NO pin belongs to is a DECLARED attribute too: with ONE
  common pin every NC/NO pin belongs to it (no label needed); with several
  common pins each common, NC and NO pin must carry the `contact` label the
  document prints (1, 2, A, B...) and the pins are grouped by that label,
  whatever their order in the table. A multi-contact relay without labels
  is refused (`contact_group_missing`): the list order is not a datum
  (generality audit 2026-11-05, HIGH-1: a table sorted by pin number would
  have paired pole 1 with pole 2's contacts in silence).
- the rows of a composition carry `symbol_composed` (COMPOSED_KEY): a
  derived layout the program may replace, unlike one the user made.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Optional

from .symbol import PIN_GRID_MM, PIN_NAMES_HIDDEN_KEY

#: The closed pin functions the pinout schema declares (`datasheet_extractor`).
RELAY_PIN_FUNCTIONS = ("coil", "common", "nc", "no", "other")
#: Horizontal pitch between the coil and each contact, mm: KiCad's Relay_SPDT
#: places the coil at x = -5.08 and the changeover at +5.08 in a 20.32 mm body.
RELAY_PART_PITCH_MM = 4 * PIN_GRID_MM
#: Body height, mm: the pins' inner ends (2.54 mm long, outer end at y = ±7.62)
#: touch the body edge at y = ±5.08, as in KiCad's relay symbols.
RELAY_BODY_HEIGHT_MM = 4 * PIN_GRID_MM
#: Shared row key (like `symbol_body`) marking a layout the program composed:
#: derived data, replaced by the next composition and dropped by a new
#: analysis, never mistaken for a layout the user drew.
COMPOSED_KEY = "symbol_composed"


@dataclass(frozen=True)
class ContactPlan:
    """@brief One contact of the plan: its common pin and the pins it switches to."""
    common: str
    nc: Optional[str] = None
    no: Optional[str] = None

    @property
    def glyph_key(self) -> str:
        """@brief The palette glyph that draws this contact.

        @return "relay_contact_spdt" with both NC and NO, else the NO-only or NC-only contact.
        """
        if self.nc and self.no:
            return "relay_contact_spdt"
        return "relay_contact_no" if self.no else "relay_contact_nc"


@dataclass(frozen=True)
class RelayPlan:
    """@brief Which pin plays which role in the composed symbol."""
    coil: tuple[str, str]
    contacts: tuple[ContactPlan, ...] = field(default_factory=tuple)


def pin_function(row) -> Optional[str]:
    """@brief The declared function of a pinout row.

    @param row A pinout row (dict); anything else has no function.
    @return The function, lower-case, when it is one of RELAY_PIN_FUNCTIONS; None otherwise.
    """
    value = row.get("function") if isinstance(row, dict) else None
    if isinstance(value, str) and value.strip().lower() in RELAY_PIN_FUNCTIONS:
        return value.strip().lower()
    return None


def contact_label(row) -> Optional[str]:
    """@brief The declared contact label of a pinout row (`contact`), as printed, or None.

    @param row A pinout row (dict).
    @return The label stripped of blanks, case kept; an integral number
        becomes its text; None when absent, blank or of another type.
    """
    value = row.get("contact") if isinstance(row, dict) else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value == int(value):
        return str(int(value))
    return None


#: Reason codes of `relay_plan` (the GUI translates them): code -> the
#: placeholders its message takes.
RELAY_REASONS = {
    "no_pin_number": (), "no_function": ("pin",), "role_twice": ("function", "pin"),
    "coil_pins": ("count",), "no_common": (), "common_without_contact": ("pin",), "duplicate_pin": (),
    "contact_group_missing": ("pin",), "contact_group_unknown": ("pin", "label"),
    "pads_without_function": ("pads",),
}


def relay_plan(rows) -> tuple[Optional[RelayPlan], tuple]:
    """@brief Group the pins of `rows` into one coil and its contacts from their declared functions.

    @param rows Pinout rows ({"pin", "name", "function", ...}) in the
        document's order.
    @return (plan, reason): the plan with an empty reason, or None with the
        structural reason as (code, {placeholder: value}), codes in
        `RELAY_REASONS`, so the GUI can say it in the user's language.
    @note The rules are the declared limits of the module docstring: exactly
        two coil pins, every pin with a function other than ``other``, one
        pin per role in a contact, at least one contact; with several
        common pins every contact pin grouped by its declared `contact`
        label. The contacts are ordered by label as the document prints
        them (numeric labels numerically, then text), never by list order.
    """
    coil: list[str] = []
    commons: list[tuple[str, Optional[str]]] = []
    switched: list[tuple[str, str, Optional[str]]] = []
    for row in rows:
        number = str(row.get("pin", "")).strip() if isinstance(row, dict) else ""
        function = pin_function(row)
        if not number:
            return None, ("no_pin_number", {})
        if function is None or function == "other":
            return None, ("no_function", {"pin": number})
        if function == "coil":
            coil.append(number)
        elif function == "common":
            commons.append((number, contact_label(row)))
        else:
            switched.append((number, function, contact_label(row)))
    if len(coil) != 2:
        return None, ("coil_pins", {"count": len(coil)})
    if not commons:
        return None, ("no_common", {})
    groups: dict = {}
    if len(commons) == 1:
        # One common pin: every NC/NO pin is its contact, no label needed.
        only = commons[0][1] or ""
        groups[only] = {"common": commons[0][0]}
        switched = [(number, function, only) for number, function, _label in switched]
    else:
        for number, label in commons:
            if label is None:
                return None, ("contact_group_missing", {"pin": number})
            if label in groups:
                return None, ("role_twice", {"function": "COMMON", "pin": groups[label]["common"]})
            groups[label] = {"common": number}
    for number, function, label in switched:
        if label is None:
            return None, ("contact_group_missing", {"pin": number})
        if label not in groups:
            return None, ("contact_group_unknown", {"pin": number, "label": label})
        if function in groups[label]:
            return None, ("role_twice", {"function": function.upper(), "pin": groups[label]["common"]})
        groups[label][function] = number
    for group in groups.values():
        if "nc" not in group and "no" not in group:
            return None, ("common_without_contact", {"pin": group["common"]})

    def label_order(label: str):
        return (0, int(label), "") if label.isdigit() else (1, 0, label)

    ordered = [groups[label] for label in sorted(groups, key=label_order)]
    return RelayPlan((coil[0], coil[1]),
                     tuple(ContactPlan(g["common"], g.get("nc"), g.get("no")) for g in ordered)), ()


def relay_symbol_rows(rows) -> tuple[Optional[list[dict]], tuple]:
    """@brief The symbol rows of the composed relay, or None with the reason of `relay_plan`.

    @param rows Pinout rows with their declared `function`.
    @return (rows, reason): copies of `rows` carrying the composed layout --
        `symbol_position`/`symbol_angle`/`symbol_pin_length` on the glyph
        terminals, the shared `symbol_body`, the shared `symbol_graphics`
        (the placed coil, links and contacts) and `symbol_pin_names_hidden`
        -- or (None, (code, values)) when `relay_plan` refuses. Every other
        `symbol_*` key of the input rows is dropped: the layout is the
        composed one.
    @note Geometry: parts at RELAY_PART_PITCH_MM from each other, the whole
        row centred on the origin (the body is centred by `layout_symbol`),
        a dashed link between neighbours; the terminals of the placed glyphs
        give the pin positions, so a glyph change moves the pins with it.
    """
    from .symbol_glyphs import instance_from_glyph, placed_terminals, relay_part_glyphs
    plan, reason = relay_plan(rows)
    if plan is None:
        return None, reason
    glyphs = {glyph.key: glyph for glyph in relay_part_glyphs()}
    parts = ["relay_coil"] + [contact.glyph_key for contact in plan.contacts]
    first_x = -(len(parts) - 1) * RELAY_PART_PITCH_MM / 2
    graphics, positions = [], {}
    # Terminal name of each glyph -> the pin number that takes its place.
    owners = [{"A1": plan.coil[0], "A2": plan.coil[1]}]
    owners += [{"COM": c.common, "NC": c.nc, "NO": c.no} for c in plan.contacts]
    for index, (key, owner) in enumerate(zip(parts, owners)):
        x = round(first_x + index * RELAY_PART_PITCH_MM, 4)
        if index:
            # The dashed link sits half way between this part and the previous one.
            graphics.append(instance_from_glyph(glyphs["relay_link"], round(x - RELAY_PART_PITCH_MM / 2, 4), 0.0))
        instance = instance_from_glyph(glyphs[key], x, 0.0)
        graphics.append(instance)
        for terminal in placed_terminals(instance):
            # The coil glyph names its terminals by number (A1/A2), the
            # contacts by name (COM/NC/NO): the role is whichever is set.
            number = owner.get(terminal.name) or owner.get(terminal.number)
            if number is not None:
                positions[number] = (terminal.x, terminal.y, int(terminal.angle), float(terminal.length))
    body = [round(len(parts) * RELAY_PART_PITCH_MM, 4), RELAY_BODY_HEIGHT_MM]
    # A pin the plan does not place cannot happen (every row was classified
    # by relay_plan), but a duplicate pin number would: refuse, never guess.
    if len({str(row.get("pin", "")).strip() for row in rows}) != len(rows):
        return None, ("duplicate_pin", {})
    composed = []
    for row in rows:
        clean = {k: deepcopy(v) for k, v in row.items() if not str(k).startswith("symbol_")}
        x, y, angle, length = positions[str(row.get("pin", "")).strip()]
        clean.update(symbol_position=[round(x, 4), round(y, 4)], symbol_angle=angle,
                     symbol_pin_length=length, symbol_body=list(body),
                     symbol_graphics=graphics)
        clean[PIN_NAMES_HIDDEN_KEY] = True
        clean[COMPOSED_KEY] = True
        composed.append(clean)
    return composed, ()


def is_composed_layout(rows) -> bool:
    """@brief Whether `rows` carry the layout the program composed (COMPOSED_KEY on any row).

    @param rows Project pin rows.
    @return True when at least one row carries the marker set.
    """
    return any(bool(row.get(COMPOSED_KEY)) for row in rows if isinstance(row, dict))


def relay_parts_summary(plan: RelayPlan) -> str:
    """@brief One short language-neutral summary of a plan for the log.

    @param plan The plan to describe.
    @return 'coil 1-5; COM 4 NC 2 NO 3': pin numbers by role, one piece per part.
    """
    parts = [f"coil {plan.coil[0]}-{plan.coil[1]}"]
    for contact in plan.contacts:
        piece = f"COM {contact.common}"
        if contact.nc:
            piece += f" NC {contact.nc}"
        if contact.no:
            piece += f" NO {contact.no}"
        parts.append(piece)
    return "; ".join(parts)
