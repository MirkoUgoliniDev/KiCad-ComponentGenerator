"""@brief Read-only checks on disk artifacts and the current applied symbol."""
from dataclasses import dataclass
from pathlib import Path
from .messages import tx
import json
import math
import os
import re
import subprocess
import tempfile
from types import SimpleNamespace

from .system_dependencies import check_kicad_version, find_kicad_cli
from .kicad_library import (ComponentArtifacts, KiCadLibraryError, PREVIEW_FOOTPRINT_ORIGIN,
                            _atomic_write_text,
                            UTF8_BOM, create_preview_project, neutral_working_dir,
                            detach_3d_model, name_footprint, step_file_is_complete,
                            _decode_utf8, _direct_child_ranges, _node_arguments)
from ..generators.castellated_module import EDGE_LABELS
from ..generators.symbol import symbol_inputs, serialize_symbol, symbol_property_positions


#: Categories of a check: the document coherence (pinout against pads and
#: names), the technical file checks (KiCad reads, STEP, alignment), the
#: final reviews (electrical types, names left to the user) and the manual
#: checks the program cannot run (ERC, DRC, visual 3D).
CATEGORIES = ('document', 'technical', 'final', 'manual')

#: Importance of a check: 1 blocks the use of the component, 2 must be fixed
#: before production (a user decision or a difference left on purpose),
#: 3 belongs to the final review. The dialog's verdict is derived from
#: status, category and importance, never from a translated title.
IMPORTANCE_BLOCKS_USE = 1
IMPORTANCE_BEFORE_PRODUCTION = 2
IMPORTANCE_FINAL_REVIEW = 3

#: Stable ids of the automatic fixes a check can offer (`Check.action`). The
#: creator of the dialog maps them to callables; the check only names them.
ACTION_REEXPORT_SYMBOL = 'reexport_symbol'
#: Ask again the open «contact_nc» question (which switch terminal is
#: normally closed) and recompose the symbol from the answer.
ACTION_ANSWER_CONTACT_NC = 'answer_contact_nc'
#: Open the Pinout table to correct the NC/NO functions by hand.
ACTION_EDIT_PINOUT = 'edit_pinout'
#: Open the preview board and schematic of the check in KiCad's editors, to
#: see where a DRC/ERC violation is. A view: nothing changes, no re-check.
ACTION_SHOW_IN_KICAD = 'show_in_kicad'
#: Hand-offs: the dialog closes and the window shows the KiCad tab (an
#: installed library to refresh) or the Symbol tab (electrical types).
ACTION_OPEN_KICAD_TAB = 'open_kicad_tab'
ACTION_OPEN_SYMBOL_TAB = 'open_symbol_tab'

#: Codes of the checks that need kicad-cli: without it they are SKIP and the
#: dialog folds them into one «KiCad non trovato» row (`kicad_cli` is the
#: code of the row that says kicad-cli is missing).
CLI_CHECK_CODES = frozenset({'kicad_footprint_read', 'kicad_symbol_read', 'kicad_symbol_file_read',
                             'kicad_3d_import', 'drc', 'erc'})

#: DRC violation types that come from the PREVIEW board, not from the
#: footprint: no net (unconnected items) and the preview's own footprint
#: library (lib_footprint_*). Never shown. The other entries CANNOT OCCUR on
#: the preview -- one footprint (courtyards_overlap, duplicate_footprints),
#: no schematic parity run (schematic_parity, extra_footprint,
#: missing_footprint, footprint_symbol_mismatch), no copper zone
#: (isolated_copper, starved_thermal) -- and are listed so that a future
#: kicad-cli default never turns them into rows about the preview.
DRC_PREVIEW_ARTEFACTS = frozenset({
    'unconnected_items', 'lib_footprint_issues', 'lib_footprint_mismatch',
    'schematic_parity', 'courtyards_overlap', 'duplicate_footprints', 'extra_footprint',
    'missing_footprint', 'footprint_symbol_mismatch', 'isolated_copper', 'starved_thermal'})
#: Findings about libraries and the drawing grid: no manufacturing rule, so
#: their rows carry no «KiCad's default rules, not your manufacturer's» note.
LIBRARY_TYPES = frozenset({'lib_symbol_issues', 'lib_symbol_mismatch', 'footprint_link_issues',
                           'endpoint_off_grid', 'lib_footprint_issues', 'lib_footprint_mismatch'})
#: Edge types: an artefact only when the violation names the preview's OWN
#: outline (its uuid); an Edge.Cuts drawing of the footprint stays visible.
DRC_EDGE_TYPES = frozenset({'copper_edge_clearance', 'silk_edge_clearance', 'invalid_outline'})
#: DRC types between two pads that `footprint_pads_overlap` already reports
#: when it named the same pad pair: shown once, by the program's own rule.
DRC_OVERLAP_TYPES = frozenset({'clearance', 'shorting_items', 'solder_mask_bridge'})
#: ERC types of a symbol placed alone on the preview sheet: its pins are not
#: connected (counted in the OK row) and its power inputs have no driver
#: (said in the manual row: the user's schematic must drive them).
ERC_PREVIEW_ARTEFACTS = frozenset({'pin_not_connected', 'power_pin_not_driven'})

#: Italian sentence of the violation types the user meets on a footprint or a
#: symbol. Any other type is shown with KiCad's own description, which is in
#: KiCad's interface language.
VIOLATION_TEXTS = {
    'clearance': 'Rame troppo vicino ad altro rame',
    'shorting_items': 'Elementi di rame in cortocircuito',
    'solder_mask_bridge': 'Ponte di solder mask fra pad',
    'annular_width': 'Anello di rame del foro troppo sottile',
    'drill_out_of_range': 'Foro fuori dai limiti di KiCad',
    'hole_to_hole': 'Fori troppo vicini fra loro',
    'hole_clearance': 'Foro troppo vicino al rame',
    'malformed_courtyard': 'Courtyard non chiuso o malformato',
    'missing_courtyard': 'Courtyard mancante',
    'text_height': 'Testo troppo basso',
    'text_thickness': 'Tratto del testo troppo sottile',
    'silk_over_copper': 'Serigrafia sopra il rame',
    'silk_overlap': 'Serigrafie sovrapposte',
    'padstack': 'Pad non valido',
    'pin_to_pin': 'Tipi elettrici dei pin in conflitto',
    'duplicate_pins': 'Numeri di pin duplicati',
    'endpoint_off_grid': 'Pin fuori dalla griglia',
    'lib_symbol_issues': 'Simbolo diverso da quello della libreria',
    'lib_symbol_mismatch': 'Simbolo diverso da quello della libreria',
    'footprint_link_issues': 'Footprint del simbolo non trovato',
}


@dataclass(frozen=True)
class Check:
    """@brief Carry one verification outcome with status, title and diagnostic detail.

    @param status `OK`, `ERROR`, `WARN` or `SKIP`.
    @param title Translated title.
    @param detail Translated detail.
    @param category One of `CATEGORIES`; `technical` by default.
    @param importance 1, 2 or 3 (see `IMPORTANCE_*`); 3 by default. An
        ERROR always blocks the use whatever this value says.
    @param remedy Translated remedy (what to do and where), '' when the
        title and detail already say it.
    @param action Stable id of an automatic fix (`ACTION_*`), or None.
    @param code Stable id of the kind of check (e.g. `electrical_type`),
        so that the dialog groups and recognises checks without comparing
        translated texts; '' for an unnamed kind.
    @param pins Pin or pad numbers the check concerns, as text; the dialog
        compresses them into ranges.
    @param needs_cli True for a check skipped only because kicad-cli is missing.
    """
    status: str
    title: str
    detail: str
    category: str = 'technical'
    importance: int = IMPORTANCE_FINAL_REVIEW
    remedy: str = ''
    action: str | None = None
    code: str = ''
    pins: tuple = ()
    #: True for a SKIP only because kicad-cli is missing: the dialog folds
    #: these into one «KiCad non trovato» row (structural, never the text).
    needs_cli: bool = False


def pin_ranges(values):
    """@brief Pin numbers as compact ranges («1–4, 7, A1»).

    @param values Pin numbers as text.
    @return The canonical ASCII decimals joined into ranges, then every
        other value as written, sorted, comma separated.
    @note Only a canonical ASCII decimal joins a range: «01» is another pin
        than «1» (KiCad compares pad numbers as text) and must not collapse
        into it, and a Unicode digit such as «²» passes `str.isdigit` but
        not `int`.
    """
    values = [str(value) for value in values]

    def canonical(value):
        return value.isascii() and value.isdigit() and str(int(value)) == value
    numeric = sorted({int(value) for value in values if canonical(value)})
    other = sorted({value for value in values if not canonical(value)})
    parts = []
    if numeric:
        start = previous = numeric[0]
        for value in numeric[1:]:
            if value == previous + 1:
                previous = value
                continue
            parts.append(str(start) if start == previous else f'{start}–{previous}')
            start = previous = value
        parts.append(str(start) if start == previous else f'{start}–{previous}')
    return ', '.join(parts + other)


def _label_relation():
    """The shared side-label rule, `pinout_coherence.label_relation`.

    Imported lazily: the check module stays importable on its own, and the
    rule lives in ONE place (docs/pinout-coherence-plan.md, rule 2).
    """
    from .pinout_coherence import label_relation
    return label_relation


def document_checks(document, rows, pad_ids):
    """@brief Checks of the symbol and footprint against the document pinout.

    @param document Dict with `pinout` (list of {pin, name}), `side_labels`
        (list of (pad, label) read on the board's sides), `user_names` (set
        of pin numbers whose name the user typed), `decision`
        (`renumber`, `keep_sides` or None: the answer to the side numbering
        question), `open_question` (True while that question is open),
        `layout` ("sides" for a module whose pads are numbered along its
        sides, anything else for another layout; missing reads as "sides")
        `off_side_pins` (pinout entries DECLARED on no side, e.g. a numbered
        exposed pad; may be missing), `unexplained_pins` (entries without a
        pad that nobody declared off the sides while the numbering is not
        shifted; may be missing), `shifted` (True when the side numbering
        is shifted against the pinout with positive evidence:
        `pinout_coherence.side_pinout_report` «contradicted») and
        `numbers_printed` (False when the documents print no pin numbers;
        missing reads as printed) and `label_conflicts` ([{"edge", "pins"}]:
        sides whose labels strongly put the pinout names on other contacts,
        `pinout_coherence.FINDING_LABEL_PLACEMENT`; may be missing).
    @param rows Applied symbol rows ({pin, name, ...}).
    @param pad_ids Electrical pad numbers of the footprint on disk, or None
        when it was not readable (the symbol's pin numbers stand in: the
        symbol pins are the footprint pads).
    @return A list of `Check`, category `document`.
    @note Only STRUCTURAL findings block use (ERROR importance 1): a
        pinout entry without a pad (the numbering is shifted) and a symbol
        pin left unnamed where the document names it; after the user chose
        `keep_sides` they are WARN importance 2 naming the choice. A side
        label `different` from the pinout name is a HEURISTIC
        (`pinout_coherence.label_relation`, common abbreviations): at most a
        WARN importance 2 asking to verify, never a block (generality audit
        2026-11-13, H3). A name the user typed that differs from the
        document is a WARN of importance 2. `unknown` relations never yield
        a finding.
    @note Without printed pin numbers (`numbers_printed` False) the
        extractor applied a fixed conventional numbering: the caller passes
        the CONVENTION rows (same frame as the sides), the normal checks run,
        a final-review note says the numbering is conventional, and no claim
        about the manufacturer's numbers is made (no shifted numbering, no
        dropped entry: an entry without a pad is unexplained, with a remedy
        that does not mention the side-numbering question).
    @note Side-based only (audit M1): the rules and remedies speak of the
        sides' numbering, so another layout gets no document check here. A
        pinout entry on no side (`off_side_pins`, e.g. an exposed pad) is not
        a dropped pin: without a pad in the footprint it is to be fixed
        before production (WARN importance 2, a missing electrical pad, L6);
        present, a final-review note (importance 3); never an error.
    """
    if not document or document.get('layout', 'sides') != 'sides':
        return []
    # Without printed pin numbers the extractor numbered the contacts by a
    # fixed convention (user decision 2026-11-13): the document's rows and the
    # sides are both in that frame (audit HIGH 1), so the normal checks run;
    # only claims about the MANUFACTURER's numbers are suppressed (no shifted
    # numbering, no dropped entry), and a note says the numbering is
    # conventional.
    convention = document.get('numbers_printed') is False
    relation = _label_relation()
    results = []
    if convention:
        results.append(Check(
            'WARN', tx('Numerazione dei pin'),
            tx("Il documento non stampa i numeri dei pin: numerazione convenzionale (primo "
               "contatto incontrato girando in senso antiorario dall'angolo in alto a sinistra), "
               "non quella del costruttore."),
            'document', IMPORTANCE_BEFORE_PRODUCTION,
            tx('Prima della produzione confronta la posizione del pin 1 e il verso della '
               'numerazione con la scheda reale o con lo schema del produttore.'),
            ACTION_EDIT_PINOUT, 'document_numbering_convention'))
    pinout = {}
    for entry in document.get('pinout') or ():
        pin = str((entry or {}).get('pin', '')).strip()
        if pin:
            pinout.setdefault(pin, str(entry.get('name') or '').strip())
    names = {str(row.get('pin', '')): str(row.get('name') or '').strip() for row in rows}
    pads = {str(p) for p in pad_ids} if pad_ids is not None else set(names)
    user_names = {str(pin) for pin in (document.get('user_names') or ())}
    kept = document.get('decision') == 'keep_sides'
    choice = tx('Scelta dell’utente: numerazione dei lati mantenuta.') + ' ' if kept else ''

    def contradiction(title, detail, remedy, code, pins):
        if kept:
            results.append(Check('WARN', title, choice + detail, 'document',
                                 IMPORTANCE_BEFORE_PRODUCTION,
                                 tx('Hai scelto di mantenere la numerazione dei lati: confronta il '
                                    'componente con il documento prima della produzione.'),
                                 None, code, tuple(pins)))
        else:
            results.append(Check('ERROR', title, detail, 'document', IMPORTANCE_BLOCKS_USE,
                                 remedy, None, code, tuple(pins)))

    renumber = tx('Rispondi alla domanda sulla numerazione dei lati, oppure correggi i numeri dei pin '
                  'nella scheda «Simbolo elettrico» confrontandoli con il pinout del documento.')
    off_side = {str(pin) for pin in (document.get('off_side_pins') or ())}
    # A shifted numbering (positive evidence) drops its entries: structural, it
    # blocks. Without that evidence an entry without a pad is unexplained; by
    # convention numbers nothing is ever «shifted» (no manufacturer numbers).
    if convention:
        unexplained = {pin for pin in pinout if pin not in pads and pin not in off_side}
    else:
        unexplained = (set() if document.get('shifted') else
                       {str(pin) for pin in (document.get('unexplained_pins') or ())})
    # Only a SHIFTED numbering drops entries (structural, blocks use); entries
    # nobody explained are a review item of their own below.
    dropped = [pin for pin in pinout
               if pin not in pads and pin not in off_side and pin not in unexplained]
    if dropped:
        contradiction(tx('Pinout del documento'),
                      tx('Voci del pinout senza pad nel footprint: {pins}.', pins=pin_ranges(dropped)),
                      renumber, 'document_dropped', dropped)
    # FINDING_LABEL_PLACEMENT: a side's labels strongly place the names on
    # other contacts -- to review before use, listed once with its pins; the
    # plain label differences below skip the pins it already names.
    placed = set()
    for conflict in document.get('label_conflicts') or ():
        pins = [str(pin) for pin in conflict.get('pins') or ()]
        if not pins:
            continue
        placed.update(pins)
        edge = str(conflict.get('edge') or '')
        results.append(Check(
            'WARN', tx('Etichette dei lati'),
            choice + tx("Etichette del lato {label} che mettono i nomi del pinout su altri contatti "
                        "(pin {pins}): nomi lasciati come li ha dati l'analisi, da rivedere prima "
                        "dell'uso.", label=tx(EDGE_LABELS.get(edge, edge)), pins=pin_ranges(pins)),
            'document', IMPORTANCE_BEFORE_PRODUCTION,
            tx('Verifica sul documento i pad indicati: etichetta del lato e nome del pinout sono '
               'confrontati con una regola approssimata, che può sbagliare sulle abbreviazioni.'),
            None, 'document_label_placement', tuple(pins)))
    differing = []
    for pad, label in document.get('side_labels') or ():
        pad = str(pad)
        if pad in pinout and pad not in placed and relation(label, pinout[pad]) == 'different':
            differing.append((pad, str(label), pinout[pad]))
    if differing:
        # A heuristic comparison (H3): never a block, always «verify».
        pairs = '; '.join(f'{pad} «{label}» / «{name}»' for pad, label, name in differing)
        results.append(Check(
            'WARN', tx('Etichette dei lati'),
            choice + tx('Etichetta sul lato diversa dal nome del pinout per lo stesso pad '
                        '(pad «lato» / «pinout»): {pairs}.', pairs=pairs),
            'document', IMPORTANCE_BEFORE_PRODUCTION,
            tx('Verifica sul documento i pad indicati: etichetta del lato e nome del pinout sono '
               'confrontati con una regola approssimata, che può sbagliare sulle abbreviazioni.'),
            None, 'document_side_label', tuple(pad for pad, _l, _n in differing)))
    lost = [pin for pin in pinout if pin in unexplained and pin not in pads]
    if lost:
        # FINDING_UNEXPLAINED_ENTRY: no pad and no declaration -- maybe a pad
        # off the sides, maybe a missing pad; not a proof, so not a block.
        results.append(Check(
            'WARN', tx('Pinout del documento'),
            choice + tx('Voci del pinout senza pad e non dichiarate fuori dai lati: {pins}.',
                        pins=pin_ranges(lost)),
            'document', IMPORTANCE_BEFORE_PRODUCTION,
            # In convention mode no side-numbering question exists (audit MEDIUM 2).
            tx("Verifica sul documento se è un pad (per esempio esposto) e aggiungilo, oppure "
               "togli la voce.") if convention else
            tx('Verifica sul documento se queste voci sono pad fuori dai lati (per esempio un pad '
               'esposto) o pad mancanti nel footprint, poi rispondi alla domanda sulla numerazione '
               'dei lati.'),
            None, 'document_unexplained', tuple(lost)))
    missing_off_side = [pin for pin in pinout if pin in off_side and pin not in pads]
    if missing_off_side:
        # L6: a numbered pad declared off the sides that the footprint lacks is
        # a missing electrical pad: to fix before production.
        results.append(Check(
            'WARN', tx('Pinout del documento'),
            tx('Voci del pinout fuori dai lati (per esempio un pad esposto) senza pad nel '
               'footprint: {pins}.', pins=pin_ranges(missing_off_side)),
            'document', IMPORTANCE_BEFORE_PRODUCTION,
            tx('Verifica sul documento se queste voci sono pad del componente da aggiungere al '
               'footprint.'),
            None, 'document_off_side', tuple(missing_off_side)))
    present_off_side = [pin for pin in pinout if pin in off_side and pin in pads]
    if present_off_side:
        # The same entries present as pads: only a note for the final review.
        results.append(Check(
            'WARN', tx('Pinout del documento'),
            tx('Voci del pinout fuori dai lati presenti come pad nel footprint: {pins}.',
               pins=pin_ranges(present_off_side)),
            'document', IMPORTANCE_FINAL_REVIEW,
            tx('Verifica sul documento la posizione di questi pad fuori dai lati.'),
            None, 'document_off_side_present', tuple(present_off_side)))
    unnamed = [pin for pin, name in names.items() if not name and pinout.get(pin)]
    if unnamed:
        contradiction(tx('Nomi dei pin'),
                      tx('Pin del simbolo senza nome che il documento nomina: {pins}.',
                         pins=pin_ranges(unnamed)),
                      tx('Nella scheda «Simbolo elettrico» dai ai pin indicati il nome del pinout del '
                         'documento, poi Applica.'),
                      'document_unnamed', unnamed)
    # A name that reads as a remark of the model instead of the printed
    # signal (pinout_coherence.FINDING_NAME_REMARK, a declared heuristic):
    # left as it is and flagged before production (user, 2026-11-13). Read on
    # the APPLIED names, so editing them in the symbol tab clears it.
    from .pinout_coherence import name_remarks
    # Names the user typed are theirs (audit L2): never flagged as remarks.
    remarked = [(pin, name) for pin, name in names.items()
                if pin not in user_names and name_remarks(name)]
    if remarked:
        pairs = '; '.join(f'{pin} «{name}»' for pin, name in remarked)
        results.append(Check(
            'WARN', tx('Nomi dei pin'),
            tx('Nomi dei pin che sembrano commenti e non il solo nome del segnale stampato: '
               '{pairs}.', pairs=pairs),
            'document', IMPORTANCE_BEFORE_PRODUCTION,
            tx('Nella scheda «Simbolo elettrico» lascia nel nome solo il segnale come è stampato '
               'sul documento (le note vanno altrove), poi Applica. La regola che li riconosce è '
               'approssimata.'),
            None, 'document_name_remark', tuple(pin for pin, _n in remarked)))
    typed = [(pin, name, pinout[pin]) for pin, name in names.items()
             if pin in user_names and name and pinout.get(pin)
             and relation(name, pinout[pin]) == 'different']
    if typed:
        pairs = '; '.join(f'{pin} «{name}» / «{doc}»' for pin, name, doc in typed)
        results.append(Check(
            'WARN', tx('Nomi dei pin'),
            tx('Nomi scritti a mano diversi dal documento (pin «simbolo» / «documento»): {pairs}.',
               pairs=pairs),
            'document', IMPORTANCE_BEFORE_PRODUCTION,
            tx('Confronta i nomi indicati con il pinout del documento nella scheda «Simbolo '
               'elettrico»: li hai scritti a mano e restano come li hai scritti.'),
            None, 'document_user_name', tuple(pin for pin, _n, _d in typed)))
    if document.get('open_question'):
        results.append(Check(
            'WARN', tx('Domanda aperta'),
            tx('La domanda sulla numerazione dei lati non ha ancora una risposta.'),
            'document', IMPORTANCE_BEFORE_PRODUCTION,
            tx('Rispondi alla domanda sulla numerazione dei lati e ripeti la verifica.'),
            None, 'document_open_question'))
    if (not results or (convention and len(results) == 1)) and pinout:
        results.append(Check('OK', tx('Pinout del documento'),
                             tx('{count} voci del pinout coerenti con pad e nomi.', count=len(pinout)),
                             'document', code='document_coherent'))
    return results


def _children(text, name):
    return [text[a:b] for a, b in _direct_child_ranges(text, name)]


def _single_footprint(text):
    """Reject extra root expressions even when a usable pad precedes them."""
    text = text.strip()
    if not re.match(r'\(footprint\s', text):
        raise ValueError(tx('File footprint non valido.'))
    # The shared parser checks balance/quotes; additionally require one root.
    _direct_child_ranges(text, 'pad')
    depth, quoted, escaped = 0, False, False
    for index, character in enumerate(text):
        if quoted:
            if escaped:
                escaped = False
            elif character == '\\':
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character == '(':
            depth += 1
        elif character == ')':
            depth -= 1
            if depth == 0 and index != len(text) - 1:
                raise ValueError(tx('Contenuto estraneo dopo il footprint.'))


def _pad_geometry(pad):
    """Validate basic physical dimensions independently of CLI availability."""
    args = _node_arguments(pad)
    if len(args) < 3:
        raise ValueError(tx('Pad senza numero, tipo o forma.'))
    if args[1] not in ('smd', 'thru_hole', 'np_thru_hole', 'connect'):
        raise ValueError(tx('Pad {pin}: tipo non valido.', pin=args[0]))
    layers = _children(pad, 'layers')
    if len(layers) != 1 or not _node_arguments(layers[0]):
        raise ValueError(tx('Pad {pin}: layer mancanti.', pin=args[0]))
    for field in ('at', 'size'):
        nodes = _children(pad, field)
        if len(nodes) != 1:
            raise ValueError(tx('Pad {pin}: {field} mancante o duplicato.', pin=args[0], field=field))
        values = _node_arguments(nodes[0])
        if len(values) < 2:
            raise ValueError(tx('Pad {pin}: {field} incompleto.', pin=args[0], field=field))
        numbers = [float(v) for v in values[:3 if field == 'at' else 2]]
        if not all(math.isfinite(v) for v in numbers):
            raise ValueError(tx('Pad {pin}: coordinate/dimensioni non finite.', pin=args[0]))
        if field == 'size':
            if min(numbers) <= 0:
                raise ValueError(tx('Pad {pin}: dimensioni non positive.', pin=args[0]))
            size = numbers
    if args[1] in ('thru_hole', 'np_thru_hole'):
        drills = _children(pad, 'drill')
        if len(drills) != 1:
            raise ValueError(tx('Pad {pin}: foro mancante o duplicato.', pin=args[0]))
        drill = _node_arguments(drills[0])
        oval = drill[:1] == ['oval']
        dimensions = drill[1:3] if oval else drill[:1]
        if len(dimensions) != (2 if oval else 1):
            raise ValueError(tx('Pad {pin}: foro incompleto.', pin=args[0]))
        dimensions = [float(v) for v in dimensions]
        if not all(math.isfinite(v) and v > 0 for v in dimensions):
            raise ValueError(tx('Pad {pin}: diametro foro non valido.', pin=args[0]))
        if not oval:
            dimensions *= 2
        if any(d > s + 1e-9 for d, s in zip(dimensions, size)):
            raise ValueError(tx('Pad {pin}: foro maggiore del pad.', pin=args[0]))
    return args


def _file_copper_pads(text):
    """The pads of a .kicad_mod text as the objects the copper rules read (`footprint_checks`).

    @param text The footprint file, already validated pad by pad (`_pad_geometry`).
    @return One namespace per pad with the attributes `footprint_checks.pad_copper`
        and `check_pads_overlap` read: number, pad_type, shape, at, rotation,
        size, layers, rect_delta, roundrect_rratio and drill_offset (the
        `(drill ... (offset dx dy))` that moves a castellated pad's copper),
        plus `hole`: (side along the pad length, side across) mm, None
        without a drill.
    @note A «custom» pad is checked through its anchor only (`pad_copper`
        reads any unknown shape as a rectangle of its size): its primitives
        are not read, a declared limit -- an overlap of a primitive is missed,
        never invented.
    """
    pads = []
    for node in _children(text, 'pad'):
        args = _node_arguments(node)
        at = [float(v) for v in _node_arguments(_children(node, 'at')[0])]
        size = [float(v) for v in _node_arguments(_children(node, 'size')[0])[:2]]
        layers = _node_arguments(_children(node, 'layers')[0])
        delta = _children(node, 'rect_delta')
        delta = tuple(float(v) for v in _node_arguments(delta[0])[:2]) if delta else None
        offset, hole = (0.0, 0.0), None
        for drill in _children(node, 'drill'):
            offsets = _children(drill, 'offset')
            if offsets:
                offset = tuple(float(v) for v in _node_arguments(offsets[0])[:2])
            values = [v for v in _node_arguments(drill) if v != 'oval']
            try:
                sides = [float(v) for v in values[:2]]
            except ValueError:
                sides = []
            if sides:
                # (side along the pad length, side across): round holes repeat it.
                hole = (sides[0], sides[1] if len(sides) > 1 else sides[0])
        pads.append(SimpleNamespace(number=args[0], pad_type=args[1], shape=args[2],
                                    at=(at[0], at[1]), rotation=at[2] if len(at) > 2 else 0.0,
                                    size=tuple(size), layers=layers, rect_delta=delta,
                                    roundrect_rratio=None, drill_offset=offset, hole=hole))
    return pads


def pin_role_checks(rows):
    """@brief Pins named after one switch role whose declared function is another.

    @param rows Applied symbol rows ({"pin", "name", "function", ...}).
    @return One ERROR (importance 1) per contradicting pin, with the action
        that opens the Pinout table; [] when no pin contradicts.
    @note Real night run 2026-11-14: a pin named «NC» declared normally
        open drew the contact the wrong way in the relay symbol. The role
        words are the ONE vocabulary of `core.contact_roles` (NC/NO/COM,
        NA/NF, the long forms, a contact number), shared with the
        extraction. Only rows whose function is a switch role are judged
        (`role_conflicts`), so an IC pin named «NC» (not connected) never is.
    """
    from ..core.contact_roles import role_conflicts
    from ..generators.symbol_relay import pin_function
    # The declared function normalised as the relay symbol reads it.
    entries = [dict(row, function=pin_function(row)) for row in rows or () if isinstance(row, dict)]
    results = []
    for conflict in role_conflicts(entries):
        pin, function = conflict['pin'], conflict['function']
        results.append(Check('ERROR', tx('Funzioni NC/NO'), tx(
            'Nome del pin {pin} ({name}) in contrasto con la funzione ({function}).',
            pin=pin, name=str(conflict['name']),
            function={'nc': 'NC', 'no': 'NO', 'common': 'COM'}[function]),
            'technical', IMPORTANCE_BLOCKS_USE,
            tx('Correggi il nome (o la funzione) del pin nella tabella Pinout.'),
            ACTION_EDIT_PINOUT, 'pin_role_contradiction', (pin,)))
    return results


def _file_holes(text):
    """{pad number: (side along the pad length, side along its width)} of the holed pads of a .kicad_mod."""
    holes = {}
    for node in _children(text, 'pad'):
        args = _node_arguments(node)
        drills = _children(node, 'drill')
        if len(args) < 2 or not drills:
            continue
        values = _node_arguments(drills[0])
        try:
            sides = ((float(values[1]), float(values[2])) if values[:1] == ['oval']
                     else (float(values[0]), float(values[0])))
        except (IndexError, ValueError):
            continue
        holes.setdefault(str(args[0]), sides)
    return holes


def _lead_entry(entry):
    """(lead, fit, source) of one `pad_leads` value: a lead mapping, or {"lead", "fit", "source"}."""
    if isinstance(entry, dict) and "lead" in entry:
        return entry.get("lead"), entry.get("fit"), entry.get("source")
    return entry, None, None


def lead_hole_checks(text, pad_leads):
    """@brief Pads whose hole IN THE FILE does not let the declared terminal pass.

    @param text The .kicad_mod text on disk.
    @param pad_leads {pad number: entry} of the applied package, where an
        entry is {"lead": lead mapping, "fit": `PinSpec.lead_fit`, "source":
        the pad's provenance of the lead} (a bare lead mapping is read as
        {"lead": it}); {} or None when no terminal is declared.
    @return [] when no declared terminal meets a hole in the file;
        otherwise, in this order: one ERROR (importance 1) naming the pads
        whose terminal does not pass; one WARN (importance 2) naming the pads
        whose terminal does not pass but whose lead reading the user's chosen
        hole contradicts (provenance «contradicted»: the terminal is the
        doubtful value, not the hole); one OK naming how many pads were
        checked when none fails -- so the absence of an error is visible.
    @note The rule is `tht_estimates.lead_passes` (a round hole larger than
        the lead's circumscribed diameter, a slot the lead fits in):
        structural, no threshold. A press-fit pad (`lead_fit` «press_fit»)
        is meant to be tighter than its terminal and is not judged.
    @note Real run 2026-11-14 (SRD relay): every declared terminal fitted,
        and the check said nothing at all, so the user could not tell it had
        run; the OK row fixes that.
    """
    from ..core.tht_estimates import lead_passes
    if not pad_leads:
        return []
    holes = _file_holes(text)
    blocked, contradicted, checked = [], [], 0
    for number, entry in pad_leads.items():
        lead, fit, source = _lead_entry(entry)
        hole = holes.get(str(number))
        if not lead or hole is None or fit == "press_fit":
            continue
        try:
            passes = lead_passes(lead, hole[0], hole[1])
        except ValueError:
            continue
        checked += 1
        if passes:
            continue
        (contradicted if source == "contradicted" else blocked).append(str(number))
    results = []
    if blocked:
        results.append(Check('ERROR', tx('Geometria del footprint'), tx(
            'I terminali dei pad {pads} non entrano nei fori.', pads=', '.join(blocked)),
            'technical', IMPORTANCE_BLOCKS_USE,
            tx('Correggi il foro dei pad in «Piazzole del componente THT» (o nel dialog del pad), '
               'poi applica e ripeti la verifica.'), None, 'lead_hole', tuple(blocked)))
    if contradicted:
        results.append(Check('WARN', tx('Geometria del footprint'), tx(
            'Pad {pads}: terminale letto contraddetto dal foro scelto dall\'utente.',
            pads=', '.join(contradicted)),
            'technical', IMPORTANCE_BEFORE_PRODUCTION,
            tx('Controlla sul disegno la sezione del terminale: il foro e\' quello scelto; se il '
               'terminale letto e\' giusto, correggi il foro in «Piazzole del componente THT».'),
            None, 'lead_hole_contradicted', tuple(contradicted)))
    if checked and not results:
        results.append(Check('OK', tx('Geometria del footprint'), tx(
            'Terminali dichiarati nei fori: {count} pad controllati, tutti passano.', count=checked),
            'technical', code='lead_hole'))
    return results


def footprint_geometry_checks(text):
    """@brief The geometric rules of the footprint tab run on a footprint FILE: copper overlaps.

    @param text The .kicad_mod text (validated by `_pad_geometry`).
    @return One ERROR `Check` (importance 1, «NON USARE») per pair of pads
        with different numbers whose copper overlaps or touches, with the
        rule's remedy; one OK `Check` when no pair does.
    @note Real run 2026-11-14 (an RS485 module): 16 mm pads with copper
        overlapping between two pads got «UTILIZZABILE CON RISERVE» because
        the check ran no footprint geometry. The rule is the footprint tab's
        own (`footprint_checks.check_pads_overlap`: pads sharing one number
        are one pin and may overlap), so the preview and the check agree.
    """
    from .footprint_checks import check_pads_overlap
    pads = _file_copper_pads(text)
    results = []
    for finding in check_pads_overlap(SimpleNamespace(pads=pads)):
        results.append(Check('ERROR', tx('Geometria del footprint'), finding.message, 'technical',
                             IMPORTANCE_BLOCKS_USE, finding.hint, None, 'footprint_pads_overlap',
                             tuple(str(pads[index].number) for index in finding.pads)))
    if not results:
        results.append(Check('OK', tx('Geometria del footprint'),
                             tx('Nessun pad con il rame sovrapposto a quello di un pad di un altro '
                                'numero.'), 'technical', code='footprint_geometry'))
    return results


def contact_function_checks(witness, rows=()):
    """@brief The check of the NC/NO functions of a switch contact, from the extraction's witness.

    @param witness `ExtractedParams.contact_function_witness` as saved with
        the project: «two_models», «user», «same_model», «single», «manual»,
        «asked», or None.
    @param rows Applied symbol rows: with no witness, pins declaring a switch
        function (nc, no, common) are read as «single» -- a project analysed
        before the witness existed has NC/NO nobody verified (practical test
        2026-11-14); without such pins no row is made.
    @return [] for None or an unknown value; otherwise one `Check`:
        «two_models» and «user» OK; «same_model» a final review (importance
        3: two readings of one model are not independent); «single»,
        «manual» and «asked» a WARN to fix before production (importance 2):
        «manual» (the user answered «Nessuna delle due») with the action
        that opens the Pinout table, «asked» with the action that asks the
        open question again.
    @note A wrong NC/NO swaps the contact in the circuit with no error from
        KiCad: only the drawing of the contact settles it, so a reading the
        program could not confirm is never reported as OK.
    """
    title = tx('Funzioni NC/NO')
    verify = tx('Confronta NC e NO dei pin del contatto con il disegno del contatto nel '
                'datasheet; se sono scambiati correggi i nomi nella scheda Simbolo.')
    if witness is None:
        from ..generators.symbol_relay import pin_function
        if any(pin_function(row) in ('nc', 'no', 'common') for row in rows or ()):
            witness = 'single'
    # The rows that ask to compare with the drawing open the Pinout table
    # (names and functions); the dialog adds «Rivedi nella scheda Simbolo».
    if witness == 'two_models':
        return [Check('OK', title, tx('Funzioni NC/NO concordi in due letture di modelli diversi.'),
                      code='contact_functions')]
    if witness == 'user':
        return [Check('OK', title, tx('Funzioni NC/NO scelte dall\'utente.'),
                      code='contact_functions')]
    if witness == 'same_model':
        return [Check('WARN', title, tx('Funzioni NC/NO uguali in due letture dello stesso modello: '
                                        'non sono letture indipendenti, da controllare sul disegno.'),
                      'final', IMPORTANCE_FINAL_REVIEW, verify, ACTION_EDIT_PINOUT, 'contact_functions')]
    if witness == 'single':
        return [Check('WARN', title, tx('Funzioni NC/NO lette una sola volta, non verificate: '
                                        'controllarle sul disegno del contatto.'),
                      'technical', IMPORTANCE_BEFORE_PRODUCTION, verify, ACTION_EDIT_PINOUT,
                      'contact_functions')]
    if witness == 'manual':
        return [Check('WARN', title, tx('Funzioni NC/NO lasciate alla correzione a mano nella '
                                        'tabella Pinout.'),
                      'technical', IMPORTANCE_BEFORE_PRODUCTION,
                      tx('Correggi NC e NO a mano nella tabella Pinout (pulsante «Apri la tabella '
                         'Pinout» qui sotto) confrontandoli con il disegno del contatto, poi ripeti '
                         'la verifica.'), ACTION_EDIT_PINOUT, 'contact_functions')]
    if witness == 'asked':
        return [Check('WARN', title, tx('Funzioni NC/NO: le letture non concordano e la domanda su '
                                        'quale contatto e\' normalmente chiuso e\' ancora aperta; il '
                                        'simbolo usa intanto le funzioni della risposta principale.'),
                      'technical', IMPORTANCE_BEFORE_PRODUCTION,
                      tx('Rispondi alla domanda con «Rispondi alla domanda» qui sotto: il simbolo '
                         'viene ricomposto dalla risposta.'), ACTION_ANSWER_CONTACT_NC,
                      'contact_functions')]
    return []


#: Distance, mm, within which a report position names a pad centre: KiCad
#: writes positions with 4 decimals, the pads are far coarser.
_POSITION_TOLERANCE_MM = 1e-3
#: Distance, mm, at or below which two shapes count as touching: float noise only.
_TOUCH_EPS_MM = 1e-6


def _rotated(x, y, angle_deg):
    """(x, y) turned by `angle_deg` the KiCad way (counter-clockwise on a y-down board)."""
    angle = math.radians(angle_deg or 0.0)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    return x * cos_a + y * sin_a, -x * sin_a + y * cos_a


def preview_board_pads(board_text):
    """@brief The pads of the footprint on a preview board, with what the DRC rows need.

    @param board_text The .kicad_pcb text of `create_preview_project`.
    @return (pads, outline_uuids): `pads` are the namespaces of
        `_file_copper_pads` (footprint coordinates) plus `index` (order in
        the footprint, the same as in the .kicad_mod), `uuid` (None when the
        file gives the pad none) and `board_xy` (pad centre on the board, mm);
        `outline_uuids` are the uuids of the board's own Edge.Cuts drawings
        (the preview outline), never the footprint's.
    """
    pads, outline = [], set()
    for footprint in _children(board_text, 'footprint'):
        at = _children(footprint, 'at')
        values = [float(v) for v in _node_arguments(at[0])] if at else list(PREVIEW_FOOTPRINT_ORIGIN)
        fx, fy, angle = values[0], values[1], values[2] if len(values) > 2 else 0.0
        nodes = _children(footprint, 'pad')
        for index, (node, pad) in enumerate(zip(nodes, _file_copper_pads(footprint))):
            uuids = _children(node, 'uuid')
            values = _node_arguments(uuids[0]) if uuids else []
            pad.index = index
            pad.uuid = str(values[0]) if values else None
            dx, dy = _rotated(float(pad.at[0]), float(pad.at[1]), angle)
            pad.board_xy = (fx + dx, fy + dy)
            pads.append(pad)
    for kind in ('gr_rect', 'gr_line', 'gr_poly', 'gr_arc', 'gr_circle'):
        for node in _children(board_text, kind):
            layers = [_node_arguments(layer) for layer in _children(node, 'layer')]
            uuids = _children(node, 'uuid')
            if uuids and any(args and args[0] == 'Edge.Cuts' for args in layers):
                values = _node_arguments(uuids[0])
                if values:
                    outline.add(str(values[0]))
    return pads, outline


def _item_key(item):
    """The identity of one report item: its uuid, else its position."""
    uuid = str(item.get('uuid') or '')
    if uuid:
        return uuid
    pos = item.get('pos') if isinstance(item.get('pos'), dict) else {}
    try:
        return ('pos', round(float(pos.get('x')), 4), round(float(pos.get('y')), 4))
    except (TypeError, ValueError):
        return ('item', str(item.get('description') or ''))


def _item_pad(item, pads):
    """The pad a report item is, or None.

    By uuid first. A position is matched ONLY with a pad written without a
    uuid (KiCad gives it one at load, unknown to the program): an item with
    an unknown uuid that is not such a pad -- the footprint itself, a text,
    a drawing -- names no pad, even when it sits on a pad centre (an exposed
    pad at the footprint origin, review 2026-11-14).
    """
    uuid = str(item.get('uuid') or '')
    by_uuid = next((pad for pad in pads if pad.uuid and pad.uuid == uuid), None)
    if by_uuid is not None:
        return by_uuid
    pos = item.get('pos') if isinstance(item.get('pos'), dict) else {}
    try:
        x, y = float(pos.get('x')), float(pos.get('y'))
    except (TypeError, ValueError):
        return None
    return next((pad for pad in pads if pad.uuid is None
                 and abs(pad.board_xy[0] - x) <= _POSITION_TOLERANCE_MM
                 and abs(pad.board_xy[1] - y) <= _POSITION_TOLERANCE_MM), None)


_SCHEMATIC_PIN = re.compile(r'\(pin\s+"((?:[^"\\]|\\.)*)"\s+\(uuid\s+"([^"]+)"\s*\)')


def _schematic_pin_uuids(schematic_text):
    """{uuid: pin number} of the pins of the symbol instances on a schematic text."""
    return {uuid: number for number, uuid in _SCHEMATIC_PIN.findall(schematic_text)}


#: Italian source of each reason the preview schematic was not written
#: (`kicad_library.PREVIEW_SCHEMATIC_SKIP_REASONS`), for the ERC SKIP row.
SCHEMATIC_SKIP_TEXTS = {
    "symbol_missing": "Schema non creato: manca la libreria simboli del componente.",
    "symbol_unreadable": "Schema non creato: la libreria simboli non è leggibile.",
    "symbol_not_found": "Schema non creato: la libreria non contiene il simbolo del componente.",
    "symbol_derived": "Schema non creato: il simbolo deriva da un altro simbolo.",
    "sym_lib_table_foreign": ("Schema non creato: la cartella contiene già un'altra tabella "
                              "dei simboli, lasciata intatta."),
    "fp_lib_table_foreign": ("Schema non creato: la cartella contiene già un'altra tabella "
                             "delle impronte, lasciata intatta."),
    "symbol_copy_foreign": ("Schema non creato: la cartella contiene già un'altra libreria "
                            "simboli con lo stesso nome, lasciata intatta."),
    "footprint_copy_foreign": ("Schema non creato: la cartella contiene già un'altra libreria "
                               "di impronte KiCadGen_Preview, lasciata intatta."),
}


def courtyard_checks(text):
    """@brief KLC F5.3 on the footprint FILE: the courtyard encloses pads and fabrication drawings.

    @param text The .kicad_mod text.
    @return One WARN (importance 2) per finding of
        `footprint_checks.check_courtyard_enclosure`, with its remedy; [] when
        the file draws no courtyard or encloses everything.
    @note The preview's edge rules are filtered (`DRC_EDGE_TYPES`), so the
        program's own enclosure rule runs on the file: a body drawn outside
        its courtyard is a real defect that no DRC row would show.
    """
    from ..core.footprint import FpArc, FpCircle, FpLine, FpPoly, FpRect
    from .footprint_checks import check_courtyard_enclosure

    def point(node, key):
        nodes = _children(node, key)
        return tuple(float(v) for v in _node_arguments(nodes[0])[:2]) if nodes else None

    def layer(node):
        nodes = _children(node, 'layer')
        args = _node_arguments(nodes[0]) if nodes else []
        return args[0] if args else ''

    graphics = []
    for node in _children(text, 'fp_line'):
        start, end = point(node, 'start'), point(node, 'end')
        if start and end:
            graphics.append(FpLine(start, end, layer(node)))
    for node in _children(text, 'fp_rect'):
        start, end = point(node, 'start'), point(node, 'end')
        if start and end:
            graphics.append(FpRect(start, end, layer(node)))
    for node in _children(text, 'fp_circle'):
        centre, end = point(node, 'center'), point(node, 'end')
        if centre and end:
            graphics.append(FpCircle(centre, math.dist(centre, end), layer(node)))
    for node in _children(text, 'fp_arc'):
        start, mid, end = point(node, 'start'), point(node, 'mid'), point(node, 'end')
        if start and mid and end:
            graphics.append(FpArc(start, mid, end, layer(node)))
    for node in _children(text, 'fp_poly'):
        pts = _children(node, 'pts')
        xys = [tuple(float(v) for v in _node_arguments(xy)[:2]) for xy in _children(pts[0], 'xy')] if pts else []
        if len(xys) >= 3:
            graphics.append(FpPoly(xys, layer(node)))
    footprint = SimpleNamespace(pads=_file_copper_pads(text), graphics=graphics)
    try:
        findings = check_courtyard_enclosure(footprint)
    except (TypeError, ValueError, AttributeError):
        return []
    return [Check('WARN', tx('Geometria del footprint'), finding.message, 'technical',
                  IMPORTANCE_BEFORE_PRODUCTION, finding.hint, None, 'footprint_courtyard')
            for finding in findings]


def _hole_capsule(pad):
    """(segment start, segment end, radius), mm, footprint frame, of a pad's hole; None without one.

    A round hole is a point with its radius; a slot a segment along its
    longer side with half the shorter side as radius, turned with the pad.
    `at` is the hole centre (a drill offset moves the copper, not the hole).
    """
    sides = getattr(pad, 'hole', None)
    if not sides:
        return None
    along, across = float(sides[0]), float(sides[1])
    cx, cy = float(pad.at[0]), float(pad.at[1])
    half = abs(along - across) / 2
    ux, uy = (1.0, 0.0) if along >= across else (0.0, 1.0)
    dx, dy = _rotated(ux * half, uy * half, getattr(pad, 'rotation', 0.0))
    return (cx - dx, cy - dy), (cx + dx, cy + dy), min(along, across) / 2


def _zero_distance(kind, pads):
    """True for a two-pad violation whose shapes really meet: the part cannot be built.

    @note Computed from the board's own geometry, never from KiCad's
        description (it is in KiCad's interface language): a short is always
        a contact; a clearance at 0 mm between two coppers; two drills that
        intersect; a drill that cuts another pad's copper. Two pads with the
        same non-empty number are one pin and never a contact. Any other
        shortfall is a distance below KiCad's default rule.
    """
    from ..core.package_geometry import overlapping_pad_pairs
    from .footprint_checks import _segment_segment_distance, pad_copper, segment_copper_distance
    if len(pads) == 2 and str(pads[0].number) and str(pads[0].number) == str(pads[1].number):
        # One electrical pin in two pieces (a split pad, a thermal pad with
        # its vias): touching is allowed, KiCad's severity ranks the row --
        # unless the two are COPIES of one pad read twice (GA9), a real defect.
        from ..core.package_geometry import _copies
        from .footprint_checks import copper_box
        return _copies(copper_box(pads[0]), copper_box(pads[1]))
    if kind == 'shorting_items':
        return True
    if len(pads) != 2:
        return False
    first, second = pads
    if kind == 'clearance':
        # 0 mm between the coppers: they touch.
        outlines = [(SimpleNamespace(number=f'#{index}'), pad_copper(pad))
                    for index, pad in enumerate(pads)]
        return bool(overlapping_pad_pairs(outlines))
    holes = [_hole_capsule(first), _hole_capsule(second)]
    if kind == 'hole_to_hole' and all(holes):
        (a0, a1, ra), (b0, b1, rb) = holes
        return _segment_segment_distance(a0, a1, b0, b1) - ra - rb <= _TOUCH_EPS_MM
    if kind == 'hole_clearance':
        for hole, other in ((holes[0], second), (holes[1], first)):
            if hole is None or other.pad_type == 'np_thru_hole':
                continue
            start, end, radius = hole
            if segment_copper_distance(start, end, pad_copper(other)) - radius <= _TOUCH_EPS_MM:
                return True
    return False


def _unnumbered_labels():
    """The labels of unnumbered pads: a mechanical hole, any other pad."""
    return (tx('foro di montaggio'), tx('pad senza numero'))


def _pad_label(pad):
    """The pad's number; «foro di montaggio» for an unnumbered np_thru_hole, «pad senza numero» otherwise."""
    if str(pad.number):
        return str(pad.number)
    return tx('foro di montaggio') if pad.pad_type == 'np_thru_hole' else tx('pad senza numero')


def _pins_text(labels):
    """Pad/pin labels as ranges, the unnumbered pads named after the numbers."""
    special = _unnumbered_labels()
    numbers = [label for label in labels if label not in special]
    parts = [pin_ranges(numbers)] if numbers else []
    parts.extend(label for label in special if label in labels)
    return ', '.join(parts)


def _violation_groups(violations, resolve, ignore, zero=None):
    """Group KiCad violations by (type, class), deduplicated by (type, set of items).

    @param violations The JSON `violations` list of a DRC or ERC report.
    @param resolve item -> (label, instance key) of the pad/pin it is, or None.
    @param ignore (type, items) -> True for a violation never shown (a preview
        artefact, or one already reported by the program's own rule).
    @param zero (type, resolved items) -> True for a contact (`_zero_distance`), or None.
    @return [(type, class, [labels], [KiCad descriptions])], class «zero»
        (a contact: blocks the use), «error» or «warning», first-seen order.
    @note Only `type`, `severity`, `items[].uuid` and `items[].pos` are read:
        the descriptions are in KiCad's interface language and only shown.
        The duplicate key is the set of item uuids, or of positions for
        items that carry no uuid.
    """
    groups, seen = {}, set()
    for violation in violations or ():
        if not isinstance(violation, dict):
            continue
        kind, severity = str(violation.get('type') or ''), str(violation.get('severity') or '')
        if not kind or severity not in ('error', 'warning'):
            continue
        items = [item for item in violation.get('items') or () if isinstance(item, dict)]
        if ignore(kind, items):
            continue
        key = (kind, frozenset(_item_key(item) for item in items))
        if key in seen:
            continue
        seen.add(key)
        resolved = [found for found in (resolve(item) for item in items) if found is not None]
        unique = list({instance: (label, instance) for label, instance in resolved}.values())
        cls = 'zero' if zero is not None and zero(kind, unique) else severity
        group = groups.setdefault((kind, cls), ([], []))
        for label, _instance in unique:
            if label not in group[0]:
                group[0].append(label)
        description = str(violation.get('description') or '').strip()
        if description and description not in group[1]:
            group[1].append(description)
    return [(kind, cls, sorted(labels, key=_pin_sort), descriptions)
            for (kind, cls), (labels, descriptions) in groups.items()]


def _canonical_number(text):
    """True for a canonical ASCII decimal («12», not «012» nor «²»), as `pin_ranges` reads it."""
    return text.isascii() and text.isdigit() and str(int(text)) == text


def _pin_sort(value):
    """Sort key of pad/pin numbers: canonical decimals by value, then the rest as text.

    `str.isdigit` accepts «²», which `int` refuses: only the canonical test of
    `pin_ranges` decides a numeric key (review 2026-11-14).
    """
    text = str(value)
    return (0, int(text), '') if _canonical_number(text) else (1, 0, text)


def _violation_checks(groups, title, pins_template, remedy, code_prefix):
    """One row per violation group.

    A contact («zero» class) is an ERROR of importance 1: the part cannot be
    built whatever the rules. A KiCad error is a WARN of importance 2, a
    warning of importance 3, both with the note that the rules are KiCad's
    defaults, not the manufacturer's. Every row opens the check's preview
    («Mostra in KiCad»): a re-export cannot change the copies the check
    made, so a symbol-library mismatch gets the same view.
    """
    results = []
    for kind, cls, labels, descriptions in groups:
        what = tx(VIOLATION_TEXTS[kind]) if kind in VIOLATION_TEXTS else (
            descriptions[0] if descriptions else kind)
        detail = (tx(pins_template, what=what, pins=_pins_text(labels)) if labels
                  else tx('{what}.', what=what))
        if descriptions:
            detail += '\n' + tx('KiCad: {description}', description='; '.join(descriptions[:3]))
        if cls == 'zero':
            detail += '\n' + tx('Contatto reale fra le forme: il componente non si può realizzare così.')
        elif kind not in LIBRARY_TYPES:
            # A library or grid finding is not a manufacturing rule.
            detail += '\n' + tx('Regole predefinite di KiCad, non quelle del tuo produttore.')
        status, importance = (('ERROR', IMPORTANCE_BLOCKS_USE) if cls == 'zero' else
                              ('WARN', IMPORTANCE_BEFORE_PRODUCTION) if cls == 'error' else
                              ('WARN', IMPORTANCE_FINAL_REVIEW))
        pins = tuple(label for label in labels if label not in _unnumbered_labels())
        results.append(Check(status, title, detail, 'technical', importance, remedy,
                             ACTION_SHOW_IN_KICAD, f'{code_prefix}_{kind}', pins))
    return results


def drc_checks(data, pads, overlap_pairs=(), outline_uuids=()):
    """@brief The rows of a `kicad-cli pcb drc` JSON report of the preview board.

    @param data The parsed JSON (its `violations` list).
    @param pads The pads of the same run's preview board (`preview_board_pads`).
    @param overlap_pairs Pad INDEX pairs (frozensets) the program's
        `footprint_pads_overlap` reported: a clearance, short or mask bridge
        between exactly those two pads is not shown twice.
    @param outline_uuids Uuids of the preview's own board outline: an edge
        violation is a preview artefact only when it names that outline; an
        Edge.Cuts drawing of the footprint itself stays visible.
    @return One row per (type, class) after the preview artefacts
        (`DRC_PREVIEW_ARTEFACTS`, `DRC_EDGE_TYPES`) and the duplicates are
        dropped, or one OK when nothing is left.
    """
    pairs = {frozenset(pair) for pair in overlap_pairs}
    outline = set(outline_uuids)

    def resolve(item):
        pad = _item_pad(item, pads)
        return None if pad is None else (_pad_label(pad), pad.index)

    def ignore(kind, items):
        if kind in DRC_PREVIEW_ARTEFACTS:
            return True
        if kind in DRC_EDGE_TYPES:
            return any(str(item.get('uuid') or '') in outline for item in items)
        if kind in DRC_OVERLAP_TYPES:
            found = frozenset(pad.index for pad in (_item_pad(item, pads) for item in items) if pad)
            return len(found) == 2 and found in pairs
        return False

    by_index = {pad.index: pad for pad in pads}
    groups = _violation_groups(
        data.get('violations'), resolve, ignore,
        lambda kind, found: _zero_distance(kind, [by_index[index] for _label, index in found]))
    title = tx('DRC del footprint in KiCad')
    results = _violation_checks(
        groups, title, '{what}: pad {pins}.',
        tx('Guarda il punto con «Mostra in KiCad», poi correggi il footprint nella scheda '
           'Anteprima footprint e ripeti la verifica.'), 'drc')
    return results or [Check('OK', title, tx('Nessuna violazione del footprint con le regole '
                                              'predefinite di KiCad.'), code='drc')]


def erc_checks(data, pin_uuids):
    """@brief The rows of a `kicad-cli sch erc` JSON report of the preview schematic.

    @param data The parsed JSON (its `sheets[].violations`).
    @param pin_uuids {uuid: pin number} of the preview schematic of the same run.
    @return (checks, power_pins): one row per (type, severity) after the
        artefacts of a symbol alone on its sheet (`ERC_PREVIEW_ARTEFACTS`),
        or one OK naming how many DISTINCT pins are unconnected; and the
        pins whose power input has no driver, for the manual row.
    """
    violations = [violation for sheet in data.get('sheets') or () if isinstance(sheet, dict)
                  for violation in sheet.get('violations') or () if isinstance(violation, dict)]

    def items_of(kind):
        return [item for violation in violations if violation.get('type') == kind
                for item in violation.get('items') or () if isinstance(item, dict)]

    # Distinct pins, an item the schematic does not name counted by its identity.
    unconnected = {pin_uuids.get(str(item.get('uuid') or ''), _item_key(item))
                   for item in items_of('pin_not_connected')}
    power = sorted({pin_uuids[str(item.get('uuid'))] for item in items_of('power_pin_not_driven')
                    if str(item.get('uuid')) in pin_uuids}, key=_pin_sort)

    def resolve(item):
        pin = pin_uuids.get(str(item.get('uuid') or ''))
        return None if pin is None else (pin, pin)

    groups = _violation_groups(violations, resolve, lambda kind, items: kind in ERC_PREVIEW_ARTEFACTS)
    title = tx('ERC del simbolo in KiCad')
    results = _violation_checks(
        groups, title, '{what}: pin {pins}.',
        tx('Guarda il punto con «Mostra in KiCad», poi correggi il simbolo nella scheda Simbolo e '
           'ripeti la verifica.'), 'erc')
    return (results or [Check('OK', title, tx(
        'Nessuna violazione del simbolo; {count} pin non collegati, normale per un simbolo da solo '
        'su un foglio.', count=len(unconnected)), code='erc')]), power


_SEXPR_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[()]|[^\s()"]+')


def sexpr_tokens(text):
    """@brief The tokens of an S-expression: parentheses, quoted strings kept whole, atoms.

    @param text S-expression text.
    @return The token list: layout whitespace is gone, the content of a
        quoted string is untouched («Relay (5V)  coil» stays different from
        «Relay(5V) coil»).
    """
    return _SEXPR_TOKEN.findall(text)


def _file_digest(path):
    """(size, sha256 hex) of a file, or None when it cannot be read."""
    import hashlib
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    return len(data), hashlib.sha256(data).hexdigest()


def library_checks(name, footprint_text, installs, step_path=None, model_offset=(0.0, 0.0, 0.0)):
    """@brief Whether the KiCad libraries the component was installed in hold what an install writes NOW.

    @param name Component name (the installed file is `<name>.kicad_mod`).
    @param footprint_text The .kicad_mod text of the component folder.
    @param installs None (no check: the caller records no installation), or
        a list of {"scope": "project"|"global", "pretty_dir": absolute folder}.
    @param step_path The component's STEP (None or missing: delivered without model).
    @param model_offset The model offset the install would write
        (`kicad_library.model_offset_for_anchor`).
    @return [] for None; one WARN (importance 3) when the list is empty (never
        installed); a SKIP «non confrontabile» for an installation that
        cannot be compared (and when none can); otherwise one row per installation: OK when everything an
        install would write now is there, WARN importance 2 naming WHAT
        differs otherwise -- the footprint, its model link (presence,
        reference, offset) or the STEP file (size and SHA-256) -- with the
        action that opens the KiCad tab.
    @note The comparison is on S-expression tokens (`sexpr_tokens`): layout
        whitespace is ignored, quoted strings are compared exactly. The
        installation writes no symbol library, so no symbol is compared.
    """
    if installs is None:
        return []
    title = tx('Libreria KiCad')
    if not installs:
        return [Check('WARN', title, tx('Componente non ancora installato in una libreria KiCad.'),
                      'final', IMPORTANCE_FINAL_REVIEW,
                      tx('Installalo dalla scheda KiCad quando è pronto.'), ACTION_OPEN_KICAD_TAB,
                      'kicad_library')]
    from .kicad_library import attach_3d_model

    def not_comparable(reason):
        return [Check('SKIP', title, tx('Libreria KiCad non confrontabile: {reason}', reason=reason),
                      code='kicad_library')]
    try:
        named = name_footprint(footprint_text, name)
    except (RuntimeError, ValueError) as exc:
        return not_comparable(str(exc))
    with_model = step_path is not None and Path(step_path).is_file()
    results = []
    for install in installs:
        if not isinstance(install, dict) or not install.get('pretty_dir'):
            continue
        project = install.get('scope') == 'project'
        scope = tx('del progetto') if project else tx('globale')
        pretty = Path(install['pretty_dir'])
        installed = pretty / f'{name}.kicad_mod'
        shapes = pretty.parent / f'{pretty.stem}.3dshapes'
        installed_step = shapes / f'{name}.step'
        reference = (f'${{KIPRJMOD}}/{pretty.stem}.3dshapes/{name}.step' if project
                     else installed_step.as_posix())
        try:
            expected = (attach_3d_model(named, reference, tuple(model_offset)) if with_model
                        else detach_3d_model(named))
        except (RuntimeError, ValueError) as exc:
            results.extend(not_comparable(str(exc)))
            continue
        differs = []
        try:
            text = installed.read_text(encoding='utf-8-sig')
        except (OSError, ValueError):
            text = None
        if text is None:
            differs.append(tx('footprint mancante'))
        else:
            try:
                if sexpr_tokens(detach_3d_model(text)) != sexpr_tokens(detach_3d_model(expected)):
                    differs.append(tx('footprint'))
                if ([sexpr_tokens(node) for node in _children(text, 'model')]
                        != [sexpr_tokens(node) for node in _children(expected, 'model')]):
                    differs.append(tx('collegamento al modello STEP'))
            except (RuntimeError, ValueError):
                differs.append(tx('footprint'))
        if with_model and _file_digest(installed_step) != _file_digest(step_path):
            differs.append(tx('modello STEP'))
        if not differs:
            results.append(Check('OK', title, tx('Libreria {scope} aggiornata: {path}',
                                                 scope=scope, path=installed), code='kicad_library'))
        else:
            results.append(Check('WARN', title, tx(
                'Libreria {scope} non aggiornata ({what}): {path}', scope=scope,
                what=', '.join(differs), path=installed),
                'technical', IMPORTANCE_BEFORE_PRODUCTION,
                tx('Reinstallala dalla scheda KiCad: la reinstallazione sovrascrive anche le modifiche '
                   'fatte nell\'editor di KiCad.'), ACTION_OPEN_KICAD_TAB, 'kicad_library'))
    # Recorded installations none of which could be compared: said, never silent.
    return results or not_comparable(tx('nessuna installazione registrata è leggibile'))


class _NoStep(Exception):
    """The component has no STEP model: the 3D checks are skipped (already reported)."""


#: Symbol properties that belong to the metadata (written by «Applica ai file»,
#: checked by the KLC gate), left out when the exported file is compared with
#: the applied symbol -- the same set the window leaves out
#: (`main_window.SYMBOL_METADATA_PROPERTIES`).
SYMBOL_METADATA_PROPERTIES = ("Reference", "Description", "ki_keywords", "Datasheet", "ki_fp_filters")


def symbol_text_without_metadata(text: str) -> str:
    """@brief A .kicad_sym text with its metadata property lines removed and whitespace normalised.

    @param text The library text (file or rendered).
    @return The lines that describe the symbol itself (name, Footprint link,
        pins, graphics, property positions), stripped, joined by newlines.
    @note The one comparison of «exported file» against «applied symbol»
        (real runs 2026-11-13: comparing the full texts flagged every fresh
        export as stale, because the applied text was rebuilt without the
        metadata and the relay graphics the export writes).
    """
    kept = []
    for line in str(text).replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if any(stripped.startswith(f'(property "{key}"') for key in SYMBOL_METADATA_PROPERTIES):
            continue
        kept.append(stripped)
    return "\n".join(kept)


def verify_component(directory, name, rows, *, pending=False, issues=(), emit=lambda result: None,
                     footprint_pending=False, model_offset=(0.0, 0.0, 0.0), document=None,
                     applied_symbol=None, footprint_missing=None, contact_witness=None,
                     pad_leads=None, installs=None, describe_error=None):
    """@brief Use scratch copies only; failed checks do not suppress independent checks.

    @param directory Component folder.
    @param name Component name.
    @param rows Applied symbol rows.
    @param pending True when the symbol editor holds unapplied edits.
    @param issues Review messages of the symbol editor: (kind, text[, pins])
        tuples (`SymbolTab.review_issue_items`; the kinds reported per row are
        dropped, the text overlaps carry their pins) or bare texts (dropped
        when equal to a per-row message).
    @param emit Callback receiving each Check as it is produced.
    @param footprint_pending True when the footprint preview holds an
        unapplied draft: the footprint checked is the applied one on disk,
        and a warning says the draft is not included.
    @param model_offset Model offset, mm, KiCad 3D axes (Y up), of the
        footprint on disk (`kicad_library.model_offset_for_anchor` of the
        anchor it was written with): the 3D import test places the model as
        the preview and the installs will, (0, 0, 0) for an unmoved anchor.
    @pre `model_offset` must be the offset of the anchor the .kicad_mod ON
        DISK was written with. The file does not record its anchor, so this
        function cannot detect a stale file (written with another anchor, an
        «Applica» not yet rewritten to disk): the import test would then
        place the model off the pads and still report OK. The caller must
        run it only when the file on disk matches the applied footprint
        (the KiCad tab's alignment gate), or report the mismatch instead.
    @param document The document's pinout and the user's choices about it
        (see `document_checks`), or None when there is none: then no
        document check is made.
    @param applied_symbol The applied symbol as the window exports it
        (graphics and metadata included), or None to rebuild it from `rows`
        (no graphics, no metadata). The exported file is compared with it
        through `symbol_text_without_metadata`.
    @param footprint_missing Translated reason the footprint does not exist
        yet (e.g. an open pad-layout question), or None: shown instead of the
        operating system's «file not found».
    @param contact_witness How the NC/NO functions of a switch contact were
        confirmed (`contact_function_checks`), None without a switch contact.
    @param pad_leads {pad number: declared terminal} of the applied package
        (`lead_hole_checks`), None or {} when no terminal is declared.
    @param installs KiCad libraries the component was installed in
        (`library_checks`), None for no library row.
    @param describe_error Callable translating a `KiCadLibraryError` for the
        SKIP rows (the window's `_kicad_error_text`), None for its own text.
    @note DRC and ERC run AUTOMATICALLY (user decision 2026-11-14) on a
        preview project written in the scratch folder: the APPLIED symbol and
        the footprint on disk, through `create_preview_project`, created once
        and shared with the 3D import test. A failure of kicad-cli, a timeout
        or an unreadable report is a SKIP, never an ERROR; the preview's own
        artefacts are filtered (`drc_checks`, `erc_checks`).
    @return The list of Check results.
    @note No exception escapes for a footprint without electrical pads or an
        incomplete STEP file: the ValueError raised internally is caught and
        reported as an ERROR check.
    @note A kicad-cli older than `kicad_library.MINIMUM_KICAD_VERSION` is an
        ERROR «Versione KiCad» before the exports, unless a supported KiCad
        is installed too: that one then runs the exports, with a WARN naming
        both. An unknown version gives no verdict.
    """
    results = []
    def add(status, title, detail, *, category='technical', importance=None, remedy='',
            action=None, code='', pins=()):
        # A generator error (SymbolError) carries its own translatable text.
        # No lookup argument: `tx` also formats, so the error renders itself
        # through the catalogue (passing `tx` would fall back to Italian).
        localized = getattr(detail, 'localized', None)
        text = None
        if callable(localized):
            try:
                text = str(localized())
            except Exception:  # noqa: BLE001 - fall back to the plain message
                text = None
        if importance is None:
            # An error blocks the use; everything else is a final review
            # unless the caller ranks it.
            importance = IMPORTANCE_BLOCKS_USE if status == 'ERROR' else IMPORTANCE_FINAL_REVIEW
        result = Check(status, title, text if text is not None else str(detail), category,
                       importance, remedy, action, code, tuple(pins))
        publish(result)

    def publish(result):
        results.append(result)
        emit(result)

    root = Path(directory)
    if not isinstance(name, str) or not name.strip() or name in ('.', '..') or any(
            c in name for c in '/\\:\x00'):
        add('ERROR', tx('Componente'), tx('Nome componente non valido.'))
        return results
    fp = root / f'{name}.pretty' / f'{name}.kicad_mod'
    step = root / f'{name}.3dshapes' / f'{name}.step'
    cli = find_kicad_cli()
    if not cli:
        publish(Check('SKIP', tx('KiCad'), tx('KiCad CLI non disponibile: apertura reale non verificata.'),
                      code='kicad_cli', needs_cli=True))
    else:
        add('OK', tx('KiCad CLI'), cli)
        # The exports below only prove something when this KiCad reads the
        # file format written (review 2026-11-03, D2); unknown -> no verdict.
        verdict = check_kicad_version(cli)
        if verdict.supported is False and verdict.newer_cli:
            add('WARN', tx('Versione KiCad'), tx(
                'KiCad {version} ({path}) è più vecchio del minimo {minimum}: '
                'la verifica usa KiCad {newer} ({newer_path}).',
                version=verdict.version, path=cli, minimum=verdict.minimum,
                newer=verdict.newer_version, newer_path=verdict.newer_cli))
            cli = verdict.newer_cli
        elif verdict.supported is False:
            add('ERROR', tx('Versione KiCad'), tx(
                'KiCad {version} ({path}) è più vecchio del minimo {minimum}: '
                'non legge i file generati. Installare KiCad {minimum} o successivo.',
                version=verdict.version, path=cli, minimum=verdict.minimum))
    if pending:
        add('WARN', tx('Modifiche non applicate'), tx('Verificato il simbolo applicato; la bozza non è inclusa.'))
    if footprint_pending:
        add('WARN', tx('Modifiche non applicate'),
            tx('Verificato il footprint applicato; la bozza del footprint non è inclusa.'))
    # Inspect the applied snapshot even when the editor has an unrelated draft.
    # UI review messages describe the draft and must not be its only evidence.
    # The per-pin checks carry their code and pin, so the dialog groups them
    # without parsing translated texts; an editor message that repeats one of
    # them word for word is not emitted twice.
    row_checks = []
    for row in rows:
        number = str(row.get('pin', ''))
        if not str(row.get('name') or '').strip():
            row_checks.append((tx('Nome mancante: {number}', number=number), 'missing_name', number))
        if row.get('electrical_type_reviewed') is False or (
                row.get('electrical_type', 'passive') in ('passive', 'unspecified')
                and not row.get('electrical_type_reviewed')):
            row_checks.append((tx('Tipo elettrico da verificare: {number}', number=number),
                               'electrical_type', number))
    row_messages = {message for message, _code, _pin in row_checks}
    row_kinds = {code for _message, code, _pin in row_checks}
    for issue in issues:
        # (kind, text) from `SymbolTab.review_issue_items`: a kind the per-row
        # checks below already report is skipped whatever the language of its
        # text (the editor words «Tipo elettrico da verificare: 1» its own
        # way); a bare text is compared as before.
        if isinstance(issue, tuple) and len(issue) in (2, 3):
            kind, text, pins = (issue + ((),))[:3] if len(issue) == 2 else issue
        else:
            kind, text, pins = None, issue, ()
        if (kind is not None and kind in row_kinds) or str(text) in row_messages:
            continue
        if kind == 'text_overlap':
            # Folded by the dialog into ONE row with every pin concerned.
            add('WARN', tx('Revisione simbolo'), text, category='final', code='text_overlap',
                pins=tuple(str(pin) for pin in pins))
            continue
        add('WARN', tx('Revisione simbolo'), text, category='final')
    for message, code, number in row_checks:
        # No per-pin remedy: the dialog merges the group into one row and
        # words its remedy once, with the pins as ranges, in that row's
        # tooltip (`component_check_dialog.PIN_GROUPS`).
        add('WARN', tx('Revisione simbolo'), message, category='final', code=code, pins=(number,))

    # A scratch file held by another program (a virus scanner, a lingering
    # kicad-cli) must not turn the end of the check into «Verifica interrotta».
    with tempfile.TemporaryDirectory(prefix='kicadgen-check-', ignore_cleanup_errors=True) as scratch:
        scratch = Path(scratch)
        def run(title, args, output, pattern=None, code=''):
            if not cli:
                publish(Check('SKIP', title, tx('Richiede KiCad CLI.'), code=code, needs_cli=True))
                return False
            try:
                if pattern:
                    output.mkdir(parents=True, exist_ok=True)
                proc = subprocess.run([cli, *map(str, args)], capture_output=True,
                    text=True, errors='replace', timeout=60, cwd=str(neutral_working_dir(cli)),
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                log = (proc.stdout + '\n' + proc.stderr).strip()
                files = list(output.rglob(pattern)) if pattern else [output]
                valid = any(p.is_file() and p.stat().st_size for p in files)
                if proc.returncode or not valid:
                    add('ERROR', title, log.replace(str(scratch), '…') or tx('Nessun risultato prodotto da KiCad.'),
                        code=code)
                    return False
                # A successful CLI exit can still contain model import warnings.
                if re.search(r'^[\s*]*(?:error|errore|fehler|erreur|fallita|fallito|impossibile|échec|echec)\b|\b(?:failed|unable)\s+to\b|\bcannot\s+(?:read|open|load|import)\b', log, re.I | re.M):
                    add('ERROR', title, log.replace(str(scratch), '…'), code=code)
                    return False
                warning = bool(re.search(r'^[\s*]*(?:warning|avviso|attenzione|warnung|avertissement)\b', log, re.I | re.M))
                # A passed read says so in the interface language; kicad-cli's
                # own lines (in KiCad's language, naming a scratch file deleted
                # at the end of the check) are kept only for a warning, with
                # the scratch folder shortened.
                add('WARN' if warning else 'OK', title,
                    log.replace(str(scratch), '…') if warning and log else tx('File letto da KiCad.'),
                    code=code)
                return True
            except (OSError, subprocess.TimeoutExpired) as exc:
                add('ERROR', title, exc, code=code)
                return False

        pad_ids = None
        footprint_text = None
        overlap_pairs = []
        symbol = None
        try:
            data = fp.read_bytes()
            if data.startswith(UTF8_BOM):
                # A BOM made the root check fail with a generic «File footprint
                # non valido» («invalid footprint file») although the file is
                # readable; kicad-cli refuses it, the install strips it
                # (review 2026-10-29, L4). Named here, the checks go on.
                add('WARN', tx('Footprint su disco'),
                    tx('Il file {path} inizia con un BOM UTF-8 (byte EF BB BF): KiCad non legge questa copia nella cartella del componente. La copia installata nella libreria viene scritta senza BOM.', path=fp))
            # Same decoding as the install (BOM dropped, universal newlines),
            # so the check reads the footprint the library will receive.
            text = _decode_utf8(data, fp)
            _single_footprint(text)
            pads = [_pad_geometry(pad) for pad in _children(text, 'pad')]
            pad_ids = {p[0] for p in pads if len(p) > 2 and p[1] != 'np_thru_hole' and p[0]}
            if not pad_ids:
                raise ValueError(tx('Nessun pad elettrico trovato.'))
            add('OK', tx('Footprint su disco'), str(fp))
            # The copper rules of the footprint tab, on the file the library
            # receives (real run 2026-11-14: overlapping pads were not seen).
            footprint_text = text
            for result in footprint_geometry_checks(text):
                publish(result)
            # The overlapping pad pairs by INSTANCE (index), for the DRC
            # dedupe: two pads may share a number.
            from .footprint_checks import check_pads_overlap
            overlap_pairs.extend(frozenset(finding.pads) for finding in
                                 check_pads_overlap(SimpleNamespace(pads=_file_copper_pads(text))))
            for result in courtyard_checks(text):
                publish(result)
            # The declared terminals against the holes the library receives.
            for result in lead_hole_checks(text, pad_leads):
                publish(result)
            if any(not p[0] and p[1] != 'np_thru_hole' for p in pads):
                add('WARN', tx('Pad senza numero'), tx('Pad di rame senza numero esclusi dalla corrispondenza elettrica.'))
            models = _children(text, 'model')
            if not models and step.is_file() and installs is None:
                # By design the component-folder .kicad_mod carries no model
                # node: the installation and the preview add it (coordinator,
                # 2026-11-13). An informational final review with NO automatic
                # fix (KiCad workflow review): a written ${KIPRJMOD} link would
                # only turn this into an inconclusive SKIP, and a regeneration
                # drops it anyway. With the «Libreria KiCad» row (`installs`
                # given) it is not made: that row already says to install and
                # compares the installed model link (practical test 2026-11-14:
                # two rows, one thing to do).
                add('WARN', tx('Collegamento 3D originale'), tx('Il footprint su disco non contiene un riferimento 3D; il test di importazione lo aggiunge soltanto nella copia temporanea.'),
                    remedy=tx('Installa la libreria dalla scheda KiCad e verifica il modello STEP nella libreria installata.'),
                    code='model_link_missing', importance=IMPORTANCE_FINAL_REVIEW)
            for model in models:
                # The isolated import normalizes transforms; inspect the original
                # values separately so an invalid scale is not hidden by that copy.
                changed_transform = False
                for field, default in (('offset', (0., 0., 0.)), ('rotate', (0., 0., 0.)), ('scale', (1., 1., 1.))):
                    nodes = _children(model, field)
                    if not nodes:
                        continue
                    xyz = _children(nodes[0], 'xyz')
                    try:
                        values = [float(v) for v in _node_arguments(xyz[0])] if len(xyz) == 1 else []
                        valid = len(nodes) == 1 and len(values) == 3 and all(math.isfinite(v) for v in values)
                        if field == 'scale':
                            valid = valid and all(v != 0 for v in values)
                    except ValueError:
                        valid = False
                    if not valid:
                        add('ERROR', tx('Collegamento 3D originale'), tx('Trasformazione {field} non valida.', field=field))
                    elif tuple(values) != default:
                        changed_transform = True
                if changed_transform:
                    add('WARN', tx('Collegamento 3D originale'), tx('Trasformazione 3D originale non verificata: la prova temporanea usa scala e orientamento normalizzati.'))
                args = _node_arguments(model)
                reference = args[0] if args else ''
                expanded = os.path.expandvars(reference)
                if not reference:
                    add('ERROR', tx('Collegamento 3D originale'), tx('Riferimento modello vuoto.'))
                elif '${' in expanded or re.search(r'%[^%]+%', expanded):
                    add('SKIP', tx('Collegamento 3D originale'), tx('Variabile KiCad da risolvere nel progetto di destinazione: {reference}', reference=reference))
                elif not Path(expanded).is_absolute():
                    add('SKIP', tx('Collegamento 3D originale'), tx('Percorso relativo da verificare nel progetto di destinazione: {reference}', reference=reference))
                elif not Path(expanded).is_file():
                    add('ERROR', tx('Collegamento 3D originale'), tx('Modello non trovato: {reference}', reference=reference))
                else:
                    add('OK', tx('Collegamento 3D originale'), reference)
            run(tx('Lettura footprint in KiCad'), ['fp', 'export', 'svg', '--footprint', name, '-o', scratch / 'fp', fp.parent], scratch / 'fp', '*.svg',
                code='kicad_footprint_read')
        except FileNotFoundError:
            # No footprint yet: said in words, with its cause when the caller
            # knows it (an open pad-layout question), never as an errno.
            add('ERROR', tx('Footprint su disco'),
                footprint_missing or tx('Nessun footprint nella cartella del componente.'),
                remedy=tx('Rispondi alla domanda con «Rigenera footprint»: il footprint viene '
                          'scritto dopo la risposta.') if footprint_missing else
                tx('Genera il footprint, poi ripeti la verifica.'),
                code='footprint_missing', importance=IMPORTANCE_BLOCKS_USE)
        except (OSError, ValueError, RuntimeError) as exc:
            add('ERROR', tx('Footprint su disco'), exc)

        try:
            pins, body = symbol_inputs(rows)
            from ..generators.symbol import symbol_pin_names_hidden
            symbol = applied_symbol if isinstance(applied_symbol, str) and applied_symbol else \
                serialize_symbol(name, pins, footprint=f'KiCadGen:{name}', body=body,
                                 properties=symbol_property_positions(rows),
                                 pin_names_hidden=symbol_pin_names_hidden(rows))
            ids = {str(p.number) for p in pins}
            if pad_ids is None:
                add('SKIP', tx('Corrispondenza pin-pad'), tx('Footprint non leggibile.'))
            elif ids != pad_ids:
                add('ERROR', tx('Corrispondenza pin-pad'),
                    tx('Solo simbolo: {symbol_only}; solo footprint: {footprint_only}', symbol_only=sorted(ids - pad_ids), footprint_only=sorted(pad_ids - ids)),
                    code='pin_pad')
            else:
                add('OK', tx('Corrispondenza pin-pad'), tx('{count} numeri elettrici corrispondenti.', count=len(ids)),
                    code='pin_pad')
            candidate = scratch / f'{name}.kicad_sym'
            candidate.write_text(symbol, encoding='utf-8')
            run(tx('Simbolo applicato: lettura in KiCad'), ['sym', 'export', 'svg', '-o', scratch / 'sym', candidate], scratch / 'sym', '*.svg',
                code='kicad_symbol_read')
            saved = root / f'{name}.kicad_sym'
            # A stale or missing exported symbol is to be fixed before
            # production (user decision 2026-11-13): importance 2, «DA RIVEDERE
            # PRIMA DELL’USO» until fixed. Other technical warnings, the
            # missing model link included, stay final reviews (importance 3).
            export_remedy = tx('Esporta il simbolo .kicad_sym nella cartella del componente dal tab KiCad.')
            if saved.is_file():
                if (symbol_text_without_metadata(saved.read_text(encoding='utf-8'))
                        != symbol_text_without_metadata(symbol)):
                    add('WARN', tx('Simbolo esportato'), tx('Il file nella cartella componente differisce dal simbolo applicato: riesportarlo.'),
                        remedy=export_remedy, action=ACTION_REEXPORT_SYMBOL, code='symbol_export_stale',
                    importance=IMPORTANCE_BEFORE_PRODUCTION)
                run(tx('Simbolo su disco: lettura in KiCad'), ['sym', 'export', 'svg', '-o', scratch / 'saved', saved], scratch / 'saved', '*.svg',
                    code='kicad_symbol_file_read')
            else:
                # The same export writes a missing file: the same fix applies.
                add('WARN', tx('Simbolo esportato'), tx('Nessun .kicad_sym nella cartella componente; verificata la versione applicata in memoria.'),
                    remedy=export_remedy, action=ACTION_REEXPORT_SYMBOL, code='symbol_export_missing',
                    importance=IMPORTANCE_BEFORE_PRODUCTION)
        except (OSError, ValueError, TypeError) as exc:
            add('ERROR', tx('Simbolo'), exc)

        # The document pinout against the pads on disk and the applied names.
        for result in document_checks(document, rows, pad_ids):
            publish(result)
        # The NC/NO functions of a switch contact, as the analysis confirmed them,
        # and pin names that contradict them.
        for result in contact_function_checks(contact_witness, rows):
            publish(result)
        for result in pin_role_checks(rows):
            publish(result)

        # ONE preview project for the 3D import, the DRC and the ERC
        # (user decision 2026-11-14): the footprint on disk and the APPLIED
        # symbol, written to scratch/applied so the preview schematic shows
        # what the user applied, not an older exported file. Works without
        # a STEP (the board then has no model node).
        preview, preview_error = None, ''
        if fp.is_file():
            try:
                applied_dir = scratch / 'applied'
                applied_dir.mkdir(parents=True, exist_ok=True)
                if isinstance(symbol, str) and symbol:
                    _atomic_write_text(applied_dir / f'{name}.kicad_sym', symbol)
                wrl = root / f'{name}.3dshapes' / f'{name}.wrl'
                artifacts = ComponentArtifacts(name, applied_dir, fp, step,
                                               wrl if wrl.is_file() else None,
                                               model_offset=tuple(model_offset))
                preview = create_preview_project(artifacts, scratch / 'project')
            except (OSError, ValueError, RuntimeError) as exc:
                preview_error = (describe_error(exc) if callable(describe_error)
                                 and isinstance(exc, KiCadLibraryError) else str(exc))
        else:
            preview_error = tx('Footprint mancante.')

        try:
            if not step.is_file():
                # Delivery without STEP (user decision A, 2026-11-12): a PC
                # without FreeCAD delivers the footprint alone. A missing model
                # is a warning, never an ERROR («NON USARE»); a STEP that exists
                # but is truncated stays an ERROR below.
                add('WARN', tx('Modello STEP'), tx('Nessun modello STEP: il componente sarà consegnato a KiCad senza vista 3D.'))
                add('SKIP', tx('Importazione 3D in KiCad'), tx('Nessun modello STEP da importare.'))
                raise _NoStep()
            if not step_file_is_complete(step):
                raise ValueError(tx('STEP incompleto o troncato.'))
            add('OK', tx('Integrità STEP'), str(step), code='step_integrity')
            if preview is not None:
                output = scratch / 'assembly.step'
                if run(tx('Importazione 3D in KiCad'), ['pcb', 'export', 'step', '--no-board-body', '-o', output, preview.board_path], output,
                       code='kicad_3d_import'):
                    solid = re.search(r'\b(MANIFOLD_SOLID_BREP|BREP_WITH_VOIDS)\s*\(', output.read_text(errors='replace'))
                    if not solid:
                        add('WARN', tx('Solidi 3D'), tx('Esportazione riuscita, ma presenza di solidi non confermata.'),
                            code='solids_unconfirmed')
            elif fp.is_file():
                add('SKIP', tx('Importazione 3D in KiCad'),
                    tx('Anteprima KiCad non creata: {error}', error=preview_error), code='kicad_3d_import')
            else:
                add('SKIP', tx('Importazione 3D in KiCad'), tx('Footprint mancante.'))
        except _NoStep:
            pass
        except (OSError, ValueError, RuntimeError) as exc:
            add('ERROR', tx('Modello STEP'), exc, code='step_model')

        def run_json(args, output, key):
            """(report, '') of one kicad-cli JSON report, or (None, reason): never an ERROR."""
            try:
                proc = subprocess.run([cli, *map(str, args)], capture_output=True, text=True,
                                      errors='replace', timeout=60, cwd=str(neutral_working_dir(cli)),
                                      creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            except subprocess.TimeoutExpired:
                return None, tx('kicad-cli non ha risposto entro 60 secondi.')
            except OSError as exc:
                return None, str(exc)
            if proc.returncode:
                first = next((line.strip() for line in (proc.stderr or '').splitlines() if line.strip()), '')
                return None, (tx('kicad-cli ha restituito il codice {code}: {line}', code=proc.returncode,
                                 line=first) if first
                              else tx('kicad-cli ha restituito il codice {code}.', code=proc.returncode))
            try:
                report = json.loads(Path(output).read_text(encoding='utf-8'))
            except (OSError, ValueError):
                return None, tx('kicad-cli non ha scritto un rapporto leggibile.')
            if not isinstance(report, dict) or not isinstance(report.get(key), list):
                return None, tx('kicad-cli non ha scritto un rapporto leggibile.')
            return report, ''

        # DRC of the preview board: KiCad's default rules on the footprint.
        drc_title = tx('DRC del footprint in KiCad')
        if preview is None:
            add('SKIP', drc_title, tx('DRC non eseguito: {reason}', reason=preview_error), code='drc')
        elif not cli:
            publish(Check('SKIP', drc_title, tx('Richiede KiCad CLI.'), code='drc', needs_cli=True))
        else:
            out = scratch / 'drc.json'
            report, reason = run_json(['pcb', 'drc', '--format', 'json', '--severity-error',
                                       '--severity-warning', '--units', 'mm', '-o', out,
                                       preview.board_path], out, 'violations')
            if report is None:
                add('SKIP', drc_title, tx('DRC non eseguito: {reason}', reason=reason), code='drc')
            else:
                try:
                    board_pads, outline = preview_board_pads(preview.board_path.read_text(encoding='utf-8'))
                except (OSError, ValueError, IndexError):
                    board_pads, outline = [], set()
                for result in drc_checks(report, board_pads, overlap_pairs, outline):
                    publish(result)

        # ERC of the preview schematic: the applied symbol placed alone.
        erc_title = tx('ERC del simbolo in KiCad')
        power_pins = []
        if preview is not None and not (isinstance(symbol, str) and symbol):
            add('SKIP', erc_title, tx('ERC non eseguito: {reason}', reason=tx(
                'il simbolo applicato non si può costruire; vedi la riga «Simbolo».')), code='erc')
        elif preview is None or preview.schematic_path is None:
            skipped = getattr(preview, 'schematic_skipped', None)
            reason = preview_error or (tx(SCHEMATIC_SKIP_TEXTS[skipped]) if skipped in SCHEMATIC_SKIP_TEXTS
                                       else tx('schema di anteprima non creato ({reason}).',
                                               reason=skipped or '?'))
            add('SKIP', erc_title, tx('ERC non eseguito: {reason}', reason=reason), code='erc')
        elif not cli:
            publish(Check('SKIP', erc_title, tx('Richiede KiCad CLI.'), code='erc', needs_cli=True))
        else:
            out = scratch / 'erc.json'
            report, reason = run_json(['sch', 'erc', '--format', 'json', '--severity-error',
                                       '--severity-warning', '--units', 'mm', '-o', out,
                                       preview.schematic_path], out, 'sheets')
            if report is None:
                add('SKIP', erc_title, tx('ERC non eseguito: {reason}', reason=reason), code='erc')
            else:
                try:
                    pins = _schematic_pin_uuids(preview.schematic_path.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    pins = {}
                checks, power_pins = erc_checks(report, pins)
                for result in checks:
                    publish(result)

        # The KiCad libraries the component was installed in.
        if footprint_text is not None:
            for result in library_checks(name, footprint_text, installs, step, model_offset):
                publish(result)
    manual = tx('ERC del tuo schema e DRC della tua scheda in KiCad; controlla a vista posizione, '
                'scala e orientamento del modello STEP.')
    if power_pins:
        manual += ' ' + tx('I pin di alimentazione {pins} vanno pilotati nel tuo schema da '
                           'un\'uscita di alimentazione o da un PWR_FLAG.', pins=pin_ranges(power_pins))
    add('WARN', tx('Verifiche manuali'), manual, category='manual', code='manual_checks')
    return results
