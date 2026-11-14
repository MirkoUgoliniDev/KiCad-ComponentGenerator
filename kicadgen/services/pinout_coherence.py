"""@brief Coherence between the pad numbering of a side-based module and the document pinout.

A castellated (side-based) module carries two descriptions of its contacts
that must be ONE numbering: each side numbers its pads (`first_pin`, `count`,
`reverse`) and lists the silkscreen label of every contact in the order met
along the edge (`pin_names`), while the document pinout names pin numbers.

Rules (docs/pinout-coherence-plan.md, narrowed by the generality audits of
2026-11-13):
- a SHIFTED numbering is structural: pads carrying numbers the pinout lacks
  while some entries without a declared reason have no pad (`contradicted`);
- a pinout entry that is no contact of any side counts as such only when the
  answer DECLARES it (`off_side` on the entry: an exposed or centre pad);
  an undeclared entry without a pad is a doubt the user settles, never a
  silent classification;
- label names are compared by a declared heuristic (`label_relation`); a
  name difference alone never blocks anything, it is reported.

This module is pure (no Qt, no AI, no I/O): it compares, it never decides.
The extraction (`datasheet_extractor`) turns a doubt into a question; the
component check reports what is left.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Iterable, Optional, Sequence

#: Relation of a side label to a pinout name: they name the same signal.
SAME = "same"
#: They name two different signals (both classified, classes or values differ).
DIFFERENT = "different"
#: The rule cannot tell (a free text such as «RST» against «EN», two different
#: index prefixes); `unknown` never triggers anything.
UNKNOWN = "unknown"

#: Kind of a document finding: a pinout entry whose number is no pad, in a
#: shifted numbering.
FINDING_PINOUT_WITHOUT_PAD = "pinout_without_pad"
#: Kind of a document finding: a pinout entry the answer declares off every
#: side (`off_side`, a numbered centre pad).
FINDING_OFF_SIDE_ENTRY = "off_side_entry"
#: Kind of a document finding: a pinout entry without a pad, not declared
#: off the sides and not part of a shift (the user is asked about it).
FINDING_UNEXPLAINED_ENTRY = "unexplained_entry"
#: Kind of a document finding: a pad whose number the pinout does not name.
FINDING_PAD_WITHOUT_PINOUT = "pad_without_pinout"
#: Kind of a document finding: a side label that names another signal than
#: the pinout entry of the same pad number (heuristic: a «verifica» warning,
#: never a block).
FINDING_LABEL_DIFFERS = "label_differs"
#: Kind of a document finding: on one side the labels place the pinout names
#: on other contacts than the analysis did, with strong support
#: (`strong_label_conflicts`): «da rivedere prima dell'uso», never a move.
FINDING_LABEL_PLACEMENT = "label_placement"

#: Least number of agreements on UNIQUE names (a name that occurs once in the
#: pinout, the label not a bare number) that makes a label placement strong
#: evidence against the analysis' mapping. Structural, not tuned: ONE
#: agreement can be a coincidence (audit H2/L3: one label must never move a
#: name), two independent unique names on the same window are not.
MIN_STRONG_AGREEMENTS = 2

# Ground spellings common on silkscreens: G, 0V, GND and VSS with any prefix
# (AGND, PGND) and an enumerating suffix (GND2, PGND1, GND_1, VSS1). «G»
# followed by digits is NOT a ground: some boards print GPIOs as G0, G1.
_GROUND = re.compile(r"^(?:G|0V|(?:[A-Z0-9]*GND|VSS)(?:_?\d+)?)$")
# Supply written with the V as the decimal point: 5V, 3V3, 5V0, +5V, V3V3.
_SUPPLY_V = re.compile(r"^\+?V?(\d+)V(\d*)$")
# Supply written with a decimal point or comma: 3.3V, 3,3V, +3.3V.
_SUPPLY_DECIMAL = re.compile(r"^\+?V?(\d+)[.,](\d+)V$")
# A supply rail named without a readable voltage: the bare rail (VDD, VCC,
# VIN, VBAT, VBUS, VSYS) or the rail followed by a voltage or a number (VDD33,
# VCC5, VDD3P3, VDD_3V3): a supply, voltage unknown. A rail name with a
# function after it (VINP, VINN, VBUS_DET, VCC_EN, VDDIO) is NOT a supply:
# an input, a detection or an enable pin (audit 2026-11-13, L2).
_SUPPLY_RAIL = re.compile(r"^\+?(?:VDD|VCC|VIN|VBAT|VBUS|VSYS)(?:_?\d+(?:[VP]\d+)?V?)?$")
# An index: a bare number (12) or letters followed by digits (GPIO12, D3, A0).
_INDEX = re.compile(r"^([A-Z]*)(\d+)$")


def primary_name(text) -> str:
    """@brief The primary name of a label or pinout name: text before the first «(», stripped, upper case.

    @param text Any value; None and non-text become "".
    @return E.g. "GPIO4" for "GPIO4 (ADC1_3, TOUCH4)", "TX" for "TX (GPIO43)".
    """
    if text is None:
        return ""
    return str(text).split("(", 1)[0].strip().upper()


def _label_class(name: str):
    """The class of a primary name: ("ground",), ("supply", volts or None), ("index", prefix, n) or None.

    Ground and supply spellings are tried BEFORE the index, so «GND2» or
    «VDD33» are never read as an index (audit 2026-11-13, item 1).
    """
    if _GROUND.match(name):
        return ("ground",)
    match = _SUPPLY_V.match(name)
    if match:
        return ("supply", float(f"{match.group(1)}.{match.group(2) or '0'}"))
    match = _SUPPLY_DECIMAL.match(name)
    if match:
        return ("supply", float(f"{match.group(1)}.{match.group(2)}"))
    if _SUPPLY_RAIL.match(name):
        return ("supply", None)
    match = _INDEX.match(name)
    if match:
        return ("index", match.group(1), int(match.group(2)))
    return None


def _supply_relation(a, b) -> str:
    """Relation of two supply voltages (None: not readable from the name)."""
    if a is None or b is None:
        return UNKNOWN
    if abs(a - b) < 1e-9:
        return SAME
    # «3V» for a 3.3 V rail is a common abbreviation: the same whole volts
    # with other decimals cannot be told apart.
    return UNKNOWN if int(a) == int(b) else DIFFERENT


def label_relation(side_label, pinout_name) -> str:
    """@brief Whether a silkscreen side label and a pinout name name the same signal.

    @param side_label The label printed beside one contact (`pin_names[i]`).
    @param pinout_name The name of the pinout entry with that contact's number.
    @return SAME, DIFFERENT or UNKNOWN, judged on the primary names only
        (`primary_name`):
        - equal primary names are SAME;
        - otherwise both are classified, in this order: ground (G, 0V, GND
          and VSS with prefixes and enumerating suffixes: AGND, GND2, PGND1,
          VSS1), supply (5V, 3V3, 5V0, 3.3V, V3V3, compared as a voltage;
          VDD/VCC/VIN/VBAT/VBUS/VSYS rails with any suffix, voltage unknown),
          prefixed index (letters followed by digits: GPIO12, D3, A0), bare
          number (12);
        - a BARE NUMBER is compared only with another bare number and is
          UNKNOWN against anything else: on a silkscreen it is either a
          signal index or the contact's physical position (audit H3);
        - two prefixed indices are compared by number when the prefixes are
          equal, UNKNOWN when they differ («IO4» against «GPIO4»);
        - two grounds are SAME; two supplies are SAME with equal voltages,
          UNKNOWN when a voltage is not readable or only the decimals differ
          («3V» against «3V3»), DIFFERENT otherwise; two different classes
          among ground, supply and prefixed index are DIFFERENT;
        - anything else (a free text such as «RST», an empty side) is UNKNOWN.
    @note DECLARED HEURISTIC (docs/pinout-coherence-plan.md, rule 2): common
        silkscreen abbreviations, not a standard, no part, brand or package
        name. What would make it unnecessary: the model returning, per pad,
        the index of its pinout entry. Its results never block anything: a
        DIFFERENT only excludes a placement and becomes a «verifica» warning;
        UNKNOWN never triggers anything.
    """
    a, b = primary_name(side_label), primary_name(pinout_name)
    if not a or not b:
        return UNKNOWN
    if a == b:
        return SAME
    ca, cb = _label_class(a), _label_class(b)
    if ca is None or cb is None:
        return UNKNOWN
    bare_a = ca[0] == "index" and not ca[1]
    bare_b = cb[0] == "index" and not cb[1]
    if bare_a or bare_b:
        if bare_a and bare_b:
            return SAME if ca[2] == cb[2] else DIFFERENT
        return UNKNOWN
    if ca[0] != cb[0]:
        return DIFFERENT
    if ca[0] == "ground":
        return SAME
    if ca[0] == "supply":
        return _supply_relation(ca[1], cb[1])
    if ca[1] != cb[1]:
        return UNKNOWN
    return SAME if ca[2] == cb[2] else DIFFERENT


#: Kind of an agreeing label (`agreement_kind`): a ground spelling.
KIND_GROUND = "ground"
#: Kind of an agreeing label: a supply voltage.
KIND_SUPPLY = "supply"
#: Kind of an agreeing label: an index (bare or prefixed number).
KIND_INDEX = "index"
#: Kind of an agreeing label: any other name (equal free text: «RST», «TX»).
KIND_NAME = "name"
#: Kinds of labels that usually repeat on a board (several grounds, two
#: 3V3). Informative only: weakness is judged by the REPETITION of the name
#: in the pinout (`side_placements`), not by the kind.
WEAK_KINDS = (KIND_GROUND, KIND_SUPPLY)


def agreement_kind(label) -> str:
    """@brief The kind of a side label, as listed in the support of a placement.

    @param label A silkscreen label.
    @return KIND_GROUND, KIND_SUPPLY, KIND_INDEX or KIND_NAME.
    """
    found = _label_class(primary_name(label))
    return found[0] if found else KIND_NAME


def pin_text(value) -> str:
    """@brief A pin number as the text every comparison uses.

    @param value A pin number (int, text or None).
    @return The stripped text; "" for None.
    """
    return str(value if value is not None else "").strip()


def pin_sort_key(text: str):
    """@brief Natural sort key of a pin number text.

    @param text A pin number as text (`pin_text`).
    @return A key that orders numbers by value first, then labels («A1»).
    """
    return (0, int(text), "") if text.isdigit() else (1, 0, text)


def _first_pin(side: dict) -> int:
    """The first pad number of a side (1 when missing or unusable, as the GUI did)."""
    try:
        value = side.get("first_pin", 1)
        return int(value) if value is not None and not isinstance(value, bool) else 1
    except (TypeError, ValueError):
        return 1


def _count(side: dict) -> int:
    """The number of contacts of a side (0 when missing or unusable)."""
    try:
        value = side.get("count")
        return max(int(value), 0) if value is not None and not isinstance(value, bool) else 0
    except (TypeError, ValueError):
        return 0


def side_pad_numbers(side: dict) -> list[int]:
    """@brief The pad number of each contact of a side, in the order met along the edge.

    @param side A castellated side: `first_pin` (default 1), `count`,
        `reverse` (True when the numbers decrease along the edge).
    @return `count` numbers; element i is the pad of contact i, the contact
        whose label is `pin_names[i]` -- the convention with which the
        generator numbers the pads.
    """
    first, count = _first_pin(side), _count(side)
    numbers = list(range(first, first + count))
    if side.get("reverse") is True:
        numbers.reverse()
    return numbers


def side_labels(side: dict) -> list[str]:
    """@brief The silkscreen labels of a side, one per contact.

    @param side A castellated side with `count` and `pin_names`.
    @return The labels as text; [] when there are none or their number is
        not the side's count (they cannot be paired with the pads).
    """
    names = side.get("pin_names") if isinstance(side, dict) else None
    if not isinstance(names, (list, tuple)) or len(names) != _count(side):
        return []
    return [str(name) for name in names]


def side_pad_labels(side: dict) -> list[dict]:
    """@brief Pin number -> silkscreen label, for one side of the module.

    @param side A castellated side (see `side_pad_numbers`).
    @return [{"pin": int, "name": label}] in the order met along the edge;
        empty when the side lists no labels or a number of labels other than
        its count (audit L3: a truncated pairing would put names on the wrong
        pads). Moved from `main_window._side_pinout` (2026-11-13) so the GUI,
        the extraction and the component check pair labels and numbers with
        ONE rule.
    """
    return [{"pin": number, "name": name}
            for number, name in zip(side_pad_numbers(side), side_labels(side))]


def document_pinout_entries(pinout) -> list[tuple[str, str]]:
    """@brief The usable entries of a document pinout as (pin text, name).

    @param pinout The `pinout` of an answer or of ExtractedParams: a list of
        {"pin": number or text, "name": text}; any other value is empty.
    @return One pair per distinct pin number with a non-empty pin text, in
        the source order; the first entry wins for a repeated number.
    """
    return [(number, name) for number, name, _entry in _entries(pinout)]


def _entries(pinout) -> list[tuple[str, str, dict]]:
    """(pin text, name, entry dict) of the usable entries (see `document_pinout_entries`)."""
    entries, seen = [], set()
    for entry in pinout if isinstance(pinout, (list, tuple)) else ():
        if not isinstance(entry, dict) or isinstance(entry.get("pin"), bool):
            continue
        number = pin_text(entry.get("pin"))
        if not number or number in seen:
            continue
        seen.add(number)
        name = entry.get("name")
        entries.append((number, name if isinstance(name, str) else "", entry))
    return entries


def declared_off_side(pinout) -> set:
    """@brief The pin numbers the answer DECLARES off every side (`off_side: true` on the entry).

    @param pinout The document pinout.
    @return Pin texts; an exposed, centre or bottom pad the document numbers.
    """
    return {number for number, _name, entry in _entries(pinout) if entry.get("off_side") is True}


def _repetition_key(name) -> str:
    """The group a pinout name repeats in: every ground is one group (GND1 and GND2 are
    two grounds, audit M4), a supply by its voltage, any other name by itself."""
    primary = primary_name(name)
    found = _label_class(primary) if primary else None
    if found and found[0] == "ground":
        return "ground"
    if found and found[0] == "supply" and found[1] is not None:
        return f"supply:{found[1]:g}"
    return primary


def _name_counts(entries: Sequence[tuple[str, str]]) -> dict:
    """{repetition group: how many pinout entries belong to it}; empty names are not counted."""
    counts: dict = {}
    for _number, name in entries:
        key = _repetition_key(name)
        if key:
            counts[key] = counts.get(key, 0) + 1
    return counts


def board_label_counts(sides) -> dict:
    """@brief How many contacts of the whole board carry each label (primary name).

    @param sides Castellated sides.
    @return {primary name: number of contacts}; empty labels are not counted.
    """
    counts: dict = {}
    for side in sides or ():
        for label in side_labels(side) if isinstance(side, dict) else ():
            key = primary_name(label)
            if key:
                counts[key] = counts.get(key, 0) + 1
    return counts


def _placement_relations(labels: Sequence[str], numbers: Sequence[int], names: dict,
                         counts: dict, board: Optional[dict] = None) -> dict:
    """Agreements of `labels[i]` with the pinout name of `numbers[i]`: counts by kind and by repetition."""
    found = {"same": 0, "different": 0, "unique": 0, "repeated": 0, "named": 0, "kinds": {}}
    for label, number in zip(labels, numbers):
        name = names.get(str(number), "")
        relation = label_relation(label, name)
        if relation == SAME:
            found["same"] += 1
            kind = agreement_kind(label)
            found["kinds"][kind] = found["kinds"].get(kind, 0) + 1
            # A name that occurs more than once in the pinout (GND, NC, 3V3)
            # fits several windows by itself: it is weak support.
            if counts.get(_repetition_key(name), 0) > 1:
                found["repeated"] += 1
            else:
                found["unique"] += 1
                # A bare number may be a position, not a name (audit H3), and
                # a label printed on more than one contact of the board (TX on
                # two UARTs) names no single contact (final audit M4): neither
                # counts as strong evidence.
                key = primary_name(label)
                if not key.isdigit() and (board or {}).get(key, 1) <= 1:
                    found["named"] += 1
        found["different"] += relation == DIFFERENT
    return found


def side_placements(side: dict, entries: Sequence[tuple[str, str]],
                    board: Optional[dict] = None) -> list[dict]:
    """@brief Every coherent placement of a side's labels on the document pinout.

    @param side A castellated side with `count` and `pin_names`.
    @param entries `document_pinout_entries` of the pinout.
    @param board `board_label_counts` of every side, or None (each label
        then counts as printed once).
    @return [{"first_pin", "reverse", "same", "labelled", "kinds", "unique",
        "repeated"}]: each window of `count` consecutive integer pin numbers
        that the pinout names entirely, read both ways along the edge, where
        no label is DIFFERENT from its pinout name and at least one is SAME.
        `unique` counts the agreements on a name that occurs ONCE in the
        pinout, `repeated` those on a repeated name (weak support, audit
        item 5), `named` the unique ones whose label is not a bare number
        and, given `board` (`board_label_counts`), occurs on one contact only;
        `kinds` the agreements by `agreement_kind`. Two readings that
        give every contact the same number are ONE placement (audit M2).
        Empty when the side has no labels.
    """
    labels = side_labels(side)
    count = len(labels)
    if not count:
        return []
    names = dict(entries)
    counts = _name_counts(entries)
    integers = {int(number) for number, _name in entries if number.isdigit()}
    labelled = sum(1 for label in labels if primary_name(label))
    found, seen = [], set()
    for first in sorted(integers):
        if any(first + step not in integers for step in range(count)):
            continue
        for reverse in (False, True):
            numbers = list(range(first, first + count))
            if reverse:
                numbers.reverse()
            if tuple(numbers) in seen:
                continue
            seen.add(tuple(numbers))
            relations = _placement_relations(labels, numbers, names, counts, board)
            if relations["same"] and not relations["different"]:
                found.append({"first_pin": first, "reverse": reverse, "same": relations["same"],
                              "labelled": labelled, "kinds": relations["kinds"],
                              "unique": relations["unique"], "repeated": relations["repeated"],
                              "named": relations["named"]})
    return found


def _edge(side: dict) -> str:
    """The edge name of a side, as the extraction keys it."""
    return str(side.get("edge") or "").strip().lower() if isinstance(side, dict) else ""


def pad_label_pairs(sides: Iterable[dict]) -> list[list[str]]:
    """@brief Every pad of the sides with its label, as sorted [pin text, label] pairs.

    @param sides Castellated sides.
    @return One pair per pad ("" as label when the side lists none). The
        pairing does not depend on the view the sides were read from (a
        bottom view reverses labels AND numbering direction), so the pairs
        identify one reading before and after the canonicalisation.
    """
    pairs = []
    for side in sides or ():
        if not isinstance(side, dict):
            continue
        labels = side_labels(side)
        for index, number in enumerate(side_pad_numbers(side)):
            pairs.append([str(number), labels[index] if labels else ""])
    return sorted(pairs, key=lambda pair: (pin_sort_key(pair[0]), pair[1]))


def renumbered_sides(sides: Sequence[dict], alignment: dict) -> list[dict]:
    """@brief Copies of the sides with `first_pin` and `reverse` taken from an alignment.

    @param sides Castellated sides.
    @param alignment {edge: {"first_pin", "reverse", ...}}; a side whose edge
        is missing is copied unchanged.
    @return New dicts; labels, count and every other key unchanged.
    """
    result = []
    for side in sides or ():
        side = dict(side)
        placement = alignment.get(_edge(side))
        if placement:
            side["first_pin"] = int(placement["first_pin"])
            side["reverse"] = bool(placement["reverse"])
        result.append(side)
    return result


def shifted_sides(sides: Sequence[dict], delta: int) -> list[dict]:
    """@brief Copies of the sides with every pad number moved by `delta` (direction unchanged).

    @param sides Castellated sides.
    @param delta Integer added to each side's `first_pin`.
    @return New dicts.
    """
    return [dict(side, first_pin=_first_pin(side) + int(delta)) for side in sides or ()]


def uniform_shifts(sides, pinout) -> list[int]:
    """@brief The shifts of the whole numbering that would put every pad on a pinout entry.

    @param sides Castellated sides.
    @param pinout The document pinout.
    @return Sorted non-zero integers d such that every pad number + d is an
        integer pinout number and every pinout entry without a pad that is not
        declared off the sides becomes a pad. Structural, no name rule: the
        option «the model shifted all the numbers by d». Numbers declared off
        the sides are never a target.
    """
    entries = document_pinout_entries(pinout)
    declared = declared_off_side(pinout)
    # A number declared off the sides (an exposed pad) is no contact's: a
    # shift never moves a side contact onto it (audit L5).
    integers = {int(number) for number, _name in entries
                if number.isdigit() and number not in declared}
    pads = [number for side in sides or () if isinstance(side, dict)
            for number in side_pad_numbers(side)]
    if not pads or not integers:
        return []
    pad_set = {str(number) for number in pads}
    wanted = {int(number) for number, _name in entries
              if number not in pad_set and number not in declared and number.isdigit()}
    found = []
    for delta in sorted({value - min(pads) for value in integers}):
        if delta == 0:
            continue
        moved = {number + delta for number in pads}
        if moved <= integers and wanted <= moved:
            found.append(delta)
    return found


def side_pinout_report(sides, pinout) -> dict:
    """@brief Compare the pad numbering of the sides with the document pinout.

    @param sides Castellated sides (with `first_pin` already assigned, e.g.
        `datasheet_extractor._castellated_sides`; a missing one counts as 1).
    @param pinout The document pinout (see `document_pinout_entries`;
        `off_side: true` on an entry declares it off every side).
    @return A dict:
        - "applicable": False when there is no side or no pinout entry
          (nothing else is then meaningful);
        - "dropped": pinout numbers that are no pad, natural order;
        - "unnamed_pads": pad numbers the pinout does not name;
        - "off_side_entries": the dropped entries DECLARED off the sides
          (reported, kept, never a question);
        - "contradicted": the numbering is SHIFTED -- undeclared dropped
          entries AND unnamed pads AND positive evidence of a shift: a uniform
          shift that puts every pad on an entry (`uniform_shifts`), or a side
          whose label placement differs from its current numbering (audit
          H1: an omitted NC pin plus an exposed pad is no shift). Only this
          earns the corrective retry;
        - "unexplained_entries": every undeclared dropped entry -- a doubt the
          user settles (an exposed pad, or part of the shift), never
          classified silently; the question always offers «off the sides»;
        - "needs_question": contradicted or unexplained entries;
        - "contradictions": [{"edge", "pin", "label", "name"}] where a label is
          DIFFERENT from the pinout entry of its own pad number (heuristic:
          reported as a «verifica» warning, never a block);
        - "placements": {edge: [placement]} of `side_placements`;
        - "alignment": {edge: placement} for the sides with exactly ONE
          coherent placement, or one that stands out ("contested_sides");
        - "ambiguous": edges with several and no single one standing out;
          "unplaced": edges with none;
        - "support": {edge: (same, labelled)} of the aligned placements;
        - "weak_sides": edges whose placement agrees only on names that
          repeat in the pinout (GND, NC, 3V3...);
        - "contested_sides": {edge: n} for the sides with several coherent
          placements of which exactly ONE agrees on a name that occurs once:
          that one is in `alignment`, `n` counts the others. A second,
          declared heuristic; never recommended;
        - "renumberable": every side has its placement and no two share a
          pin number;
        - "covers_pinout": the placements cover every pinout entry not
          declared off the sides;
        - "recommended": renumberable, covers_pinout, no weak and no
          contested side (no threshold, the support is shown);
        - "already_aligned": the current numbering IS that alignment;
        - "label_conflicts": `strong_label_conflicts` of the sides.
    """
    sides = [side for side in sides or () if isinstance(side, dict)]
    entries = document_pinout_entries(pinout)
    report = {"applicable": bool(sides and entries), "dropped": [], "unnamed_pads": [],
              "contradicted": False, "contradictions": [], "placements": {}, "alignment": {},
              "ambiguous": [], "unplaced": [], "support": {}, "renumberable": False,
              "recommended": False, "already_aligned": False, "off_side_entries": [],
              "unexplained_entries": [], "needs_question": False, "covers_pinout": False,
              "weak_sides": [], "contested_sides": {}, "label_conflicts": []}
    if not report["applicable"]:
        return report
    names = dict(entries)
    declared = declared_off_side(pinout)
    board = board_label_counts(sides)
    pads = {str(number) for side in sides for number in side_pad_numbers(side)}
    report["dropped"] = sorted((number for number, _name in entries if number not in pads),
                               key=pin_sort_key)
    report["unnamed_pads"] = sorted((number for number in pads if number not in names),
                                    key=pin_sort_key)
    report["off_side_entries"] = [number for number in report["dropped"] if number in declared]
    undeclared = [number for number in report["dropped"] if number not in declared]
    report["unexplained_entries"] = undeclared
    report["needs_question"] = bool(undeclared)
    for side in sides:
        labels = side_labels(side)
        for label, number in zip(labels, side_pad_numbers(side)):
            name = names.get(str(number))
            if name is not None and label_relation(label, name) == DIFFERENT:
                report["contradictions"].append({"edge": _edge(side), "pin": str(number),
                                                 "label": label, "name": name})
    taken: list[set] = []
    for side in sides:
        edge = _edge(side)
        found = side_placements(side, entries, board)
        report["placements"][edge] = found
        strong = [placement for placement in found if placement["unique"]]
        chosen = found[0] if len(found) == 1 else strong[0] if len(strong) == 1 else None
        if chosen is not None:
            report["alignment"][edge] = chosen
            report["support"][edge] = (chosen["same"], chosen["labelled"])
            if not chosen["unique"]:
                report["weak_sides"].append(edge)
            if len(found) > 1:
                report["contested_sides"][edge] = len(found) - 1
            taken.append(set(range(chosen["first_pin"], chosen["first_pin"] + _count(side))))
        elif found:
            report["ambiguous"].append(edge)
        else:
            report["unplaced"].append(edge)
    unique = len(report["alignment"]) == len(sides)
    disjoint = sum(len(numbers) for numbers in taken) == len(set().union(*taken)) if taken else False
    report["renumberable"] = unique and disjoint
    covered = {str(number) for numbers in taken for number in numbers}
    report["covers_pinout"] = report["renumberable"] and covered == set(names) - declared
    report["recommended"] = (report["covers_pinout"] and not report["weak_sides"]
                             and not report["contested_sides"])
    report["already_aligned"] = report["renumberable"] and all(
        _first_pin(side) == report["alignment"][_edge(side)]["first_pin"]
        and (side.get("reverse") is True) == report["alignment"][_edge(side)]["reverse"]
        for side in sides)
    # Positive evidence of a shift (audit H1): a uniform shift that fits, or
    # a side whose labels sit on another window than its current numbers.
    moved = any(placement["first_pin"] != _first_pin(side)
                or placement["reverse"] != (side.get("reverse") is True)
                for side in sides for placement in [report["alignment"].get(_edge(side))]
                if placement)
    report["contradicted"] = bool(undeclared and report["unnamed_pads"]
                                  and (moved or uniform_shifts(sides, pinout)))
    report["label_conflicts"] = strong_label_conflicts(sides, pinout)
    return report


def strong_label_conflicts(sides, pinout) -> list[dict]:
    """@brief The sides whose labels place the pinout names on other contacts than the analysis, strongly.

    @param sides Castellated sides, numbered as the analysis read them.
    @param pinout The analysis' pinout.
    @return [{"edge", "first_pin", "reverse", "named", "pins"}], one per side
        where exactly ONE placement of the labels has at least
        MIN_STRONG_AGREEMENTS agreements on unique names (`named`: the name
        once in the pinout, the label once on the board, not a bare number),
        no label contradicts it, its window takes no number another side
        holds with its own agreements, and it differs from the side's current
        numbering --
        the analysis' own name -> contact mapping. `pins` are the current pad
        numbers whose analysis name differs from the name that placement
        gives them.
    @note Evidence for ONE corrective retry and, if it persists, a finding;
        never a reason to move a name by itself (audit H2).
    """
    # Rows named from a label (`LABEL_NAME_KEY`) are no pinout names: a label
    # compared with itself would be evidence for nothing.
    entries = [(number, name) for number, name, entry in _entries(pinout)
               if entry.get(LABEL_NAME_KEY) is not True]
    names = dict(entries)
    counts = _name_counts(entries)
    board = board_label_counts(sides)
    sides = [side for side in sides or () if isinstance(side, dict)]
    # Numbers each side holds at its CURRENT numbering with its own strong
    # agreements: another side's placement over them is not evidence, it
    # would take a contact that already agrees (final audit M4).
    held = {}
    for side in sides:
        current = _placement_relations(side_labels(side), side_pad_numbers(side), names, counts,
                                       board)
        if current["named"]:
            held[id(side)] = {
                number for label, number in zip(side_labels(side), side_pad_numbers(side))
                if label_relation(label, names.get(str(number), "")) == SAME}
    conflicts = []
    for side in sides:
        if not side_labels(side):
            continue
        others = set().union(*(numbers for key, numbers in held.items() if key != id(side)))
        strong = [placement for placement in side_placements(side, entries, board)
                  if placement["named"] >= MIN_STRONG_AGREEMENTS
                  and not others & set(range(placement["first_pin"],
                                             placement["first_pin"] + _count(side)))]
        if len(strong) != 1:
            continue
        placement = strong[0]
        if (placement["first_pin"] == _first_pin(side)
                and placement["reverse"] == (side.get("reverse") is True)):
            continue
        current = side_pad_numbers(side)
        proposed = side_pad_numbers(dict(side, first_pin=placement["first_pin"],
                                         reverse=placement["reverse"]))
        pins = [str(old) for old, new in zip(current, proposed)
                if names.get(str(old)) != names.get(str(new))]
        if pins:
            conflicts.append({"edge": _edge(side), "first_pin": placement["first_pin"],
                              "reverse": placement["reverse"], "named": placement["named"],
                              "pins": pins})
    return conflicts


def translated_conflicts(conflicts, sides, numbers: dict) -> list[dict]:
    """@brief Strong label conflicts with their pins renumbered as `numbers` gives each contact.

    @param conflicts `strong_label_conflicts` computed on `sides` (the
        analysis' numbering).
    @param sides The same sides.
    @param numbers {edge: [new number per contact, listing order]}.
    @return Copies with "pins" in the new numbering (the contact keeps its
        identity, only its number changes).
    """
    new_of = {}
    for side in sides or ():
        if not isinstance(side, dict):
            continue
        for current, new in zip(side_pad_numbers(side), numbers.get(_edge(side)) or ()):
            new_of[str(current)] = str(new)
    return [dict(item, pins=[new_of.get(pin, pin) for pin in item["pins"]]) for item in conflicts]


def report_digest(sides, pinout) -> str:
    """@brief A short digest of a side/pinout situation: the pad-label pairs and the pinout entries.

    @param sides Castellated sides.
    @param pinout The document pinout.
    @return 12 hex characters; the same reading of the same documents gives
        the same digest (independent of the view, `pad_label_pairs`), so a
        saved answer applies again only to the situation it answered.
    """
    # Numbers AND names (audit L2), and the declared off-side flags: a
    # pinout read again differently is another situation.
    declared = declared_off_side(pinout)
    entries = sorted(([number, name, number in declared]
                      for number, name in document_pinout_entries(pinout)),
                     key=lambda entry: pin_sort_key(entry[0]))
    text = json.dumps({"pads": pad_label_pairs(sides), "pinout": entries},
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def pin_ranges_text(numbers: Iterable) -> str:
    """@brief Pin numbers compressed into ranges: «23-43, 45, A1».

    @param numbers Pin numbers (int or text).
    @return Consecutive integers joined as «a-b», the rest listed, comma
        separated, in natural order; "" when empty.
    """
    texts = sorted({pin_text(number) for number in numbers if pin_text(number)}, key=pin_sort_key)
    parts, run = [], []
    for text in texts:
        if text.isdigit() and run and int(text) == int(run[-1]) + 1:
            run.append(text)
            continue
        if run:
            parts.append(run[0] if len(run) == 1 else f"{run[0]}-{run[-1]}")
            run = []
        if text.isdigit():
            run = [text]
        else:
            parts.append(text)
    if run:
        parts.append(run[0] if len(run) == 1 else f"{run[0]}-{run[-1]}")
    return ", ".join(parts)


def kept_sides_pinout(sides, pinout) -> list[dict]:
    """@brief The pinout of the choice «keep the sides' numbering»: the side labels, pad by pad.

    @param sides Castellated sides.
    @param pinout The document pinout.
    @return [{"pin": str, "name": label}] for every pad of a side that lists
        labels; for a side without labels, the document entries whose number
        is one of its pads (the only names it has); and the entries DECLARED
        off every side as the document gives them -- a centre pad is never
        removed (audit H2). Natural pin order.
    """
    entries = dict(document_pinout_entries(pinout))
    pads = {str(number) for side in sides or () if isinstance(side, dict)
            for number in side_pad_numbers(side)}
    rows = [{"pin": number, "name": entries[number], "off_side": True}
            for number in sorted(declared_off_side(pinout) - pads, key=pin_sort_key)]
    for side in sides or ():
        if not isinstance(side, dict):
            continue
        labels = side_labels(side)
        for index, number in enumerate(side_pad_numbers(side)):
            if labels:
                rows.append({"pin": str(number), "name": labels[index]})
            elif str(number) in entries:
                rows.append({"pin": str(number), "name": entries[str(number)]})
    return sorted(rows, key=lambda row: pin_sort_key(row["pin"]))


def marked_off_side(pinout, numbers: Iterable) -> list[dict]:
    """@brief A copy of the pinout with the given entries declared off every side.

    @param pinout The document pinout.
    @param numbers Pin numbers the user (or the answer) puts off the sides.
    @return New entry dicts; `off_side: True` on the given numbers, the
        other entries unchanged. Nothing is removed or renumbered.
    """
    chosen = {pin_text(number) for number in numbers}
    result = []
    for entry in pinout if isinstance(pinout, (list, tuple)) else ():
        entry = dict(entry) if isinstance(entry, dict) else entry
        if isinstance(entry, dict) and pin_text(entry.get("pin")) in chosen:
            entry["off_side"] = True
        result.append(entry)
    return result


# --- the fixed numbering convention (documents that print no pin numbers) ----------------------

#: Edges in the order of the convention's walk: from the top-left corner,
#: counter-clockwise seen from above (the DIP rule), and whether the walk
#: follows each edge in its listing direction (top to bottom on the vertical
#: sides, left to right on the horizontal ones).
CONVENTION_WALK = (("left", True), ("bottom", True), ("right", False), ("top", False))


def convention_numbers(sides) -> dict:
    """@brief The pad numbers of the fixed convention: counter-clockwise from the top-left corner.

    @param sides Canonical castellated sides (view from above, labels in
        listing order: top to bottom on vertical edges, left to right on
        horizontal ones).
    @return {edge: [number of each contact, listing order]}: the walk starts
        at the top-left corner and goes down the left side, along the bottom
        side left to right, up the right side and along the top side right to
        left; the first contact met is pin 1, numbers are consecutive. With
        two horizontal rows pin 1 is the bottom-left contact. Works for any
        set of one to four sides.
    @note User decision 2026-11-13: on a module whose document prints no pin
        numbers «pin 1» has no electrical meaning; the numbers only link the
        symbol pins to the footprint pads, so one fixed convention replaces a
        question. It is not the manufacturer's numbering and the notes say so.
    """
    counts = {_edge(side): _count(side) for side in sides or () if isinstance(side, dict)}
    numbers, next_number = {}, 1
    for edge, forward in CONVENTION_WALK:
        count = counts.get(edge, 0)
        if count <= 0:
            continue
        run = list(range(next_number, next_number + count))
        numbers[edge] = run if forward else run[::-1]
        next_number += count
    return numbers


def sides_with_numbers(sides, numbers: dict) -> Optional[list[dict]]:
    """@brief Copies of the sides numbered as `numbers` ({edge: [number per contact, listing order]}).

    @param sides Canonical castellated sides.
    @param numbers One list per edge, as `convention_numbers` gives.
    @return New dicts with `first_pin` and `reverse`; None when a list does
        not fit its side (another count, numbers not consecutive).
    """
    result = []
    for side in sides or ():
        values = numbers.get(_edge(side))
        if values is None or len(values) != _count(side):
            return None
        ascending = sorted(values)
        if ascending != list(range(ascending[0], ascending[0] + len(values))):
            return None
        reverse = len(values) > 1 and values[0] > values[-1]
        result.append(dict(side, first_pin=ascending[0], reverse=reverse))
    return result


#: Key marking a pinout row whose name is the contact's side label because
#: the analysis gave that contact no pinout entry (`convention_pinout`).
LABEL_NAME_KEY = "label_name"


def convention_pinout(sides, pinout, numbers: dict) -> tuple:
    """@brief The pinout after a renumbering: every name stays on its physical contact.

    @param sides Canonical castellated sides, numbered as the analysis read
        them (before the renumbering).
    @param pinout The analysis' pinout.
    @param numbers The new {edge: [number per contact, listing order]}.
    @return (rows, unattached, clashes): rows = one entry per contact with its NEW
        number -- a copy of the pinout entry the ANALYSIS gave that contact
        (same current pad number; name, function... kept), else {"pin",
        "name": its side label, LABEL_NAME_KEY: True} -- followed by the entries no contact
        carries, numbered after the contacts in pinout order (the declared
        ones keep `off_side`); unattached = the CURRENT numbers of the
        undeclared entries among them, a repeated pin number included (a
        second entry with the same number is kept, never dropped silently);
        clashes = the numbers of entries DECLARED off the sides that equal a
        contact's number: kept off the sides, not attached to that contact
        (named in a note).
    @note The side labels are hints only: they never move a name onto
        another contact (audit H2, 2026-11-13). The mapping name -> contact
        is the analysis' own (its numbering of the contacts and its pinout
        numbers); only the numbers change.
    """
    # A declared off-side entry is no contact's even if its number is one
    # (final audit L6); the first entry of a repeated number names the
    # contact, the others are kept after the contacts (L5).
    raw = [entry for entry in pinout if isinstance(entry, dict)] if isinstance(
        pinout, (list, tuple)) else []
    by_number = {}
    for entry in raw:
        number = pin_text(entry.get("pin"))
        if number and entry.get("off_side") is not True and number not in by_number:
            by_number[number] = entry
    contact_numbers = {str(number) for side in sides or () if isinstance(side, dict)
                       for number in side_pad_numbers(side)}
    rows, used = [], set()
    total = 0
    for side in sides or ():
        if not isinstance(side, dict):
            continue
        edge, labels = _edge(side), side_labels(side)
        for index, (current, new) in enumerate(zip(side_pad_numbers(side), numbers.get(edge) or ())):
            entry = by_number.get(str(current))
            if entry is not None:
                used.add(id(entry))
            # A contact the analysis named nothing takes its label, marked as
            # such: a label is not a pinout name and never counts as evidence
            # for or against the labels (`strong_label_conflicts`).
            row = (dict(entry) if entry is not None else
                   {"name": labels[index] if labels else "", LABEL_NAME_KEY: True})
            row["pin"] = str(new)
            row.pop("off_side", None)
            rows.append(row)
            total = max(total, int(new))
    unattached, clashes = [], []
    for entry in raw:
        number = pin_text(entry.get("pin"))
        if not number or id(entry) in used:
            continue
        total += 1
        rows.append(dict(entry, pin=str(total)))
        if entry.get("off_side") is not True:
            unattached.append(number)
        elif number in contact_numbers:
            clashes.append(number)
    return sorted(rows, key=lambda row: pin_sort_key(row["pin"])), unattached, clashes


def document_findings(report: dict, *, kept_sides: bool = False) -> list[dict]:
    """@brief The findings of a side/pinout report, typed for the component check.

    @param report A `side_pinout_report`.
    @param kept_sides True when the user chose to keep the sides' numbering
        (question «side_numbering», option «keep_sides»): the findings are
        still listed, the check lowers their importance.
    @return [{"kind", "pins", ...}]: FINDING_PINOUT_WITHOUT_PAD with the
        dropped numbers of a shifted numbering, FINDING_UNEXPLAINED_ENTRY with
        the undeclared entries without a pad otherwise, FINDING_OFF_SIDE_ENTRY
        with the entries declared off the sides, FINDING_PAD_WITHOUT_PINOUT
        with the unnamed pads, one FINDING_LABEL_PLACEMENT per side whose
        labels strongly place the names elsewhere (`label_conflicts`, to
        review before use), one FINDING_LABEL_DIFFERS per heuristic name
        difference (with "edge", "label", "name"); each carries "kept_sides".
        Empty when not applicable.
    """
    if not report or not report.get("applicable"):
        return []
    findings = []
    declared = set(report.get("off_side_entries") or ())
    shifted = [pin for pin in report.get("dropped") or () if pin not in declared]
    if shifted and report.get("contradicted"):
        findings.append({"kind": FINDING_PINOUT_WITHOUT_PAD, "pins": shifted,
                         "kept_sides": kept_sides})
    if report.get("unexplained_entries") and not report.get("contradicted"):
        findings.append({"kind": FINDING_UNEXPLAINED_ENTRY,
                         "pins": list(report["unexplained_entries"]), "kept_sides": kept_sides})
    if report.get("off_side_entries"):
        findings.append({"kind": FINDING_OFF_SIDE_ENTRY,
                         "pins": list(report["off_side_entries"]), "kept_sides": kept_sides})
    if report.get("unnamed_pads"):
        findings.append({"kind": FINDING_PAD_WITHOUT_PINOUT, "pins": list(report["unnamed_pads"]),
                         "kept_sides": kept_sides})
    for item in report.get("label_conflicts") or ():
        findings.append({"kind": FINDING_LABEL_PLACEMENT, "pins": list(item["pins"]),
                         "edge": item["edge"], "kept_sides": kept_sides})
    for item in report.get("contradictions") or ():
        findings.append({"kind": FINDING_LABEL_DIFFERS, "pins": [item["pin"]],
                         "edge": item["edge"], "label": item["label"], "name": item["name"],
                         "kept_sides": kept_sides})
    return findings


#: Kind of a document finding: a pin name that reads like a remark of the
#: model (where it is printed, what it is labelled) rather than a signal name
#: as printed (`name_remarks`, a declared heuristic): «da rivedere», never
#: stripped.
FINDING_NAME_REMARK = "name_remark"

# Quotation marks: a signal name as printed carries none; a model quoting
# what it saw («labelled "1"») does.
_QUOTE_CHARACTERS = frozenset('"“”„«»')
# Tokens of a name: runs between blanks, underscores, slashes and list or
# group punctuation, so «top_left_field_label_G» splits into its words.
_NAME_TOKEN = re.compile(r"[^\s_/,;:()\[\]]+")
# A prose word: three or more letters, all lower case, no digit.
_PROSE_WORD = re.compile(r"[a-z]{3,}")
# One parenthesised group, innermost.
_GROUP = re.compile(r"\(([^()]*)\)")


def name_remarks(name) -> list[str]:
    """@brief The evidence that a pin name holds a remark instead of only a signal name.

    @param name A pinout or symbol pin name (any value; non-text is clean).
    @return The quote characters and lower-case prose words that make the
        name read as a remark, in written order; empty when the name looks
        like a signal name as printed.
    @note HEURISTIC, declared as such (user, 2026-11-13: the model wrote
        «GND (labelled G, between TX and the right hole)», «GND
        (top_left_field_label_G)», «GPIO1 / IO1 (labelled "1")» as pin
        names). Signal names are printed in upper case, with digits or as
        short mnemonics (GPIO4, ADC1_3, U0TXD, nRESET, 3V3, IO0/BOOT), and
        other functions in a group are identifiers too («GPIO4 (ADC1_3,
        TOUCH4)»). A name reads as a remark when ANY of:
        - it contains a quotation mark;
        - a parenthesised group holds a prose word (three or more lower-case
          letters, no digit: «labelled», «field», «between»);
        - the whole name holds two or more prose words (underscores count as
          blanks, so «top_left_field_label_G» has four).
        Not structural. Known false positives (flagged for review only,
        never altered and never a paid retry -- generality audit M2,
        2026-11-13): a document that prints a function in lower-case
        words, e.g. «not connected», «Chip enable (active high)», a value
        with a unit word in a group such as «VIN (5-12V, 1A max)», a
        lower-case silkscreen name of two words such as «led_red». Not
        flagged: a one-word name outside a group («Common», «Not
        connected» has one lower-case word). No rule on brands, parts or a
        list of words. What would make it unnecessary: a schema that returns the
        printed signal name and the model's remarks in separate fields.
    """
    if not isinstance(name, str) or not name.strip():
        return []
    quotes = [ch for ch in name if ch in _QUOTE_CHARACTERS]
    words = [token for token in _NAME_TOKEN.findall(name) if _PROSE_WORD.fullmatch(token)]
    grouped = [token for group in _GROUP.findall(name)
               for token in _NAME_TOKEN.findall(group) if _PROSE_WORD.fullmatch(token)]
    if quotes or grouped or len(words) >= 2:
        return quotes + words
    return []


def remark_named_pins(pinout) -> list[tuple[str, str]]:
    """@brief The (pin, name) entries of a pinout whose name reads as a remark (`name_remarks`).

    @param pinout A pinout (list of {"pin", "name"}) or symbol rows; any other
        value is empty.
    @return (pin text, name) in source order, one per distinct pin.
    """
    return [(number, name) for number, name, _entry in _entries(pinout) if name_remarks(name)]
