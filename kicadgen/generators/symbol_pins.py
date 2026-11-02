"""@brief Symbol pins derived from the footprint pads; the pinout table only names them.

The pads of the footprint are the electrical identities of the component: a
symbol pin without a pad, or a pad without a pin, is a broken component in
KiCad. A pinout table read by the analysis may number its entries with its
own convention (a relay drawn with pads 11, 12, 14, A1, A2 and a table
numbered 1..5), so its numbers are never trusted to create pins: they only
choose which pad receives a name.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Iterable

from .symbol import LocalizedText, _message

#: Separators of the name tokens compared with the pad numbers: every run of
#: characters that is neither a letter nor a digit («Coil_A1» -> Coil, A1).
_TOKEN_SEPARATOR = re.compile(r'[^0-9A-Za-z]+')


def _key(value) -> str:
    """Pin or pad number as compared everywhere: text, stripped, None as empty."""
    return str(value if value is not None else '').strip()


def _name(entry: dict) -> str:
    """Pin name of a pinout entry, KiCad's «~» read as no name."""
    text = _key(entry.get('name'))
    return '' if text == '~' else text


def name_tokens(name: str) -> tuple[str, ...]:
    """@brief Split a pin name into its whole tokens.

    @param name Pin name, e.g. «Common_11» or «Coil A1».
    @return The non-empty runs of letters and digits, in order
        («Common_11» -> («Common», «11»)). A token is never split further:
        «IN1» stays «IN1» and never yields the pad «1».
    """
    return tuple(token for token in _TOKEN_SEPARATOR.split(name or '') if token)


@dataclass
class PinoutOnPads:
    """@brief The symbol rows built from the footprint pads and what the pinout could not place.

    @note `rows` holds exactly one row per distinct electrical pad number, in
        pad order. `unmatched` lists the pinout entries that named no pad;
        `by_name` the (entry number, entry name, pad) triples placed by the
        name-token heuristic; `notes` the messages the user must read.
    """
    rows: list[dict] = field(default_factory=list)
    unmatched: list[dict] = field(default_factory=list)
    by_name: list[tuple[str, str, str]] = field(default_factory=list)
    notes: list[LocalizedText] = field(default_factory=list)


def _entry_label(entry: dict) -> str:
    """«number name» of a pinout entry for a note, the part that is missing left out."""
    return ' '.join(part for part in (_key(entry.get('pin')), _name(entry)) if part)


def pinout_on_pads(pinout: Iterable[dict] | None, pad_numbers: Iterable) -> PinoutOnPads:
    """@brief Build the symbol rows as exactly the footprint pads, named by the pinout table.

    @param pinout Pinout entries (dicts with `pin`, `name` and optionally
        `electrical_type` or other row keys); None or non-dict items count
        as no entry.
    @param pad_numbers Electrical pad numbers of the footprint, in pad order;
        a number shared by several pads (a ground tab) gives one pin.
    @return A `PinoutOnPads`: one row per distinct pad number. A matched
        entry is copied whole (name, electrical type, layout keys) with its
        `pin` set to the pad number; a pad no entry names gets
        `{"pin": pad, "name": ""}`, which the symbol writer emits as KiCad's
        «~». Entries naming no pad never become pins: they are listed in
        `unmatched` and in a note.
    @note Matching order: (1) an entry whose number IS a pad names that pad;
        a second entry for the same pad with a different name is unmatched,
        one with the same name is the same pin listed twice. (2) An entry
        whose number is not a pad names the pad whose number appears as a
        whole token of the entry's name («Coil_A1» -> A1, «Common_11» -> 11),
        compared exactly (case included) and only when exactly one pad
        number is among the tokens, that pad is still unnamed and no other
        entry claims it the same way. Never fuzzy: «IN1» does not name pad 1.
    @note Step (2) is a HEURISTIC, a fallback for tables whose numbering
        follows another convention than the pads (seen on a relay drawing
        whose pads are 11, 12, 14, A1, A2 while the analysis numbered its
        table 1..5). It relies on the name repeating the pad number, which
        is common on drawings of relays and connectors but not guaranteed;
        it would be unnecessary if the analysis returned the pad numbers of
        the drawing. Every pin placed this way is reported in a note so the
        user can check it.
    @note An entry matched by number whose name holds, as a token, another
        pad number only is reported as a contradiction: its name is kept
        (the number is the stronger evidence) but the user is told.
    @note With no pad number at all (footprint not known) the pinout entries
        are returned unchanged and nothing is reported: there is nothing to
        check them against.
    """
    entries = [dict(entry) for entry in pinout or () if isinstance(entry, dict)]
    pads = list(dict.fromkeys(key for key in (_key(number) for number in pad_numbers or ()) if key))
    result = PinoutOnPads()
    if not pads:
        result.rows = entries
        return result
    pad_set = set(pads)
    named: dict[str, dict] = {}
    pending = []
    for entry in entries:
        number = _key(entry.get('pin'))
        if number not in pad_set:
            pending.append(entry)
            continue
        if number not in named:
            named[number] = entry
            # Contradiction check: the name points at one other pad only.
            others = {token for token in name_tokens(_name(entry)) if token in pad_set} - {number}
            if len(others) == 1 and number not in name_tokens(_name(entry)):
                result.notes.append(_message(
                    'Pin {number}: il nome «{name}» richiama il pad {other}; tenuto sul pad {number} '
                    'come dice il numero. Verifica la piedinatura.',
                    'Pin {number}: the name «{name}» refers to pad {other}; kept on pad {number} as '
                    'its number says. Check the pinout.',
                    number=number, name=_name(entry), other=next(iter(others))))
        elif _name(named[number]) != _name(entry):
            result.unmatched.append(entry)
    # Step (2): whole-token heuristic for entries whose number is not a pad.
    claims: dict[str, list[dict]] = {}
    for entry in pending:
        candidates = {token for token in name_tokens(_name(entry)) if token in pad_set}
        if len(candidates) == 1:
            claims.setdefault(next(iter(candidates)), []).append(entry)
    placed = set()
    for pad, claimants in claims.items():
        if pad in named or len(claimants) != 1:
            continue
        entry = claimants[0]
        named[pad] = entry
        placed.add(id(entry))
        result.by_name.append((_key(entry.get('pin')), _name(entry), pad))
    result.unmatched.extend(entry for entry in pending if id(entry) not in placed)
    for pad in pads:
        entry = named.get(pad)
        row = dict(entry) if entry is not None else {'name': ''}
        row['pin'] = pad
        if entry is not None:
            row['name'] = _name(entry)
        result.rows.append(row)
    if result.by_name:
        result.notes.append(_message(
            'Pin associati ai pad per nome, perché i numeri della piedinatura non sono pad del '
            'footprint (da verificare): {pairs}.',
            'Pins matched to pads by name, because the pinout numbers are not footprint pads '
            '(to be checked): {pairs}.',
            pairs=', '.join(f'{name} -> {pad}' for _number, name, pad in result.by_name)))
    if result.unmatched:
        result.notes.append(_message(
            'Voci della piedinatura senza un pad del footprint, non diventano pin: {entries}.',
            'Pinout entries without a footprint pad, not turned into pins: {entries}.',
            entries=', '.join(_entry_label(entry) for entry in result.unmatched)))
    return result
