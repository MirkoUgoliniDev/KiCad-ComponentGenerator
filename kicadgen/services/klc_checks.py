"""@brief Read-only checks of a generated component against the KiCad Library Conventions.

The checks look at DATA only: the metadata the user typed, the text of the
generated `.kicad_sym` and `.kicad_mod` files. Nothing is deduced from the
name of a part or of a package. Each rule yields a `Finding` with a stable
code, a severity and a message in the interface language; a rule that
cannot be evaluated (no file text given) yields nothing instead of guessing.

Rules covered (KLC 2024 numbering in parentheses where one exists):
designator prefix (S6.1), description and keywords (S6.2), datasheet and
footprint filters (S6.3), symbol pins on the 100 mil grid with 2.54 mm
length and 1.27 mm texts (S4.1, S4.2), body outline 0.254 mm with
background fill (S3.3), unique pin numbers and positions (S4.3), footprint
courtyard/fabrication layers (F5.3, F5.2), Reference/Value texts (F5.1),
footprint attribute and description (F5.4, F9.1) and file names without the
characters KiCad forbids.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import unicodedata

from ..generators.symbol_glyphs import parse_sexpr
from .messages import tx

#: Oldest symbol library format with the KiCad 6+ s-expression syntax the
#: checks understand; older files are the legacy `.lib` text format.
MIN_SYMBOL_FORMAT = 20211014
#: Schematic grid the KLC requires for every pin connection point (mm).
PIN_GRID_MM = 2.54
#: Preferred pin length and text size of the KLC (mm).
PIN_LENGTH_MM = 2.54
#: KLC S4.1: longer pins are allowed in steps of 50 mil (mm).
PIN_LENGTH_STEP_MM = 1.27
#: KiCad's default pin name offset, the KLC value; omitted from files when default (mm).
PIN_NAME_OFFSET_MM = 0.508
TEXT_SIZE_MM = 1.27
#: Body outline width and fill the KLC requires for the main body (mm).
BODY_STROKE_MM = 0.254
BODY_FILL = "background"
#: Tolerance of the grid/size comparisons: KiCad writes at most 4-6 decimals.
TOLERANCE = 1e-4

#: Characters KiCad refuses in library item names: the library separator and
#: the path separators (they would break `Library:Name` references and file
#: names). Control characters are refused as well.
ILLEGAL_NAME_CHARACTERS = ':/\\'

SEVERITIES = ('error', 'warning')


@dataclass(frozen=True)
class Finding:
    """@brief One KLC check result.

    @param code Stable identifier of the rule (e.g. `pin_grid`), for tests
        and for filtering; never shown as the only text.
    @param severity `error` (KiCad or the KLC rejects it) or `warning`
        (the KLC discourages it, the file still loads).
    @param message Translated text for the user.
    """
    code: str
    severity: str
    message: str

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f'severity: expected one of {SEVERITIES}')


def _error(code: str, message: str) -> Finding:
    return Finding(code, 'error', message)


def _warning(code: str, message: str) -> Finding:
    return Finding(code, 'warning', message)


def _has_control(text: str) -> bool:
    return any(unicodedata.category(c) in ('Cc', 'Cs') for c in text)


def _string(metadata: dict, key: str) -> str:
    value = metadata.get(key, '')
    return value if isinstance(value, str) else ''


def name_findings(name: str, *, code: str, label: str) -> list[Finding]:
    """@brief Check a symbol or footprint name for characters KiCad forbids or discourages.

    @param name Name to check (not a path).
    @param code Finding code prefix, e.g. `footprint_name`.
    @param label Translated label of the item, used in the messages.
    @return Error for empty name, `:` `/` `\\` or control characters;
        warning for spaces (allowed by KiCad, discouraged by the KLC).
    """
    findings = []
    if not name or not name.strip():
        return [_error(code + '_empty', tx('{label}: nome vuoto.', label=label))]
    if _has_control(name) or any(c in ILLEGAL_NAME_CHARACTERS for c in name):
        findings.append(_error(code + '_illegal', tx(
            '{label}: il nome «{name}» contiene caratteri vietati da KiCad (: / \\ o di controllo).',
            label=label, name=name)))
    if any(c.isspace() for c in name):
        findings.append(_warning(code + '_spaces', tx(
            '{label}: il nome «{name}» contiene spazi, sconsigliati dalle convenzioni KiCad.',
            label=label, name=name)))
    return findings


def check_metadata(metadata: dict, footprint_name: str) -> list[Finding]:
    """@brief Check the library metadata of a component (KLC S6) and its footprint name.

    @param metadata Keys `reference`, `description`, `keywords`, `datasheet`,
        `fp_filters`; strings, each may be missing (treated as empty).
    @param footprint_name Footprint name the files will carry.
    @return Findings in rule order; empty when everything complies.
    @note The designator prefix must be letters only (S6.1: `U`, `K`, `RV`,
        no digits, spaces or `?`): KiCad appends the number itself.
    """
    findings = []
    if not isinstance(metadata, dict):
        return [_error('metadata_type', tx('Metadati non validi: atteso un dizionario.'))]
    reference = _string(metadata, 'reference').strip()
    if not reference:
        findings.append(_error('reference_empty', tx('Designatore vuoto: serve un prefisso come U, K o J.')))
    elif not reference.isalpha():
        findings.append(_error('reference_letters', tx(
            'Designatore «{reference}»: solo lettere, senza cifre, spazi o «?».', reference=reference)))

    description = _string(metadata, 'description')
    if _has_control(description):
        findings.append(_error('description_multiline', tx(
            'Descrizione su più righe o con caratteri di controllo: deve stare su una riga.')))
    elif not description.strip():
        findings.append(_warning('description_empty', tx('Descrizione vuota: le convenzioni KiCad la richiedono.')))

    keywords = _string(metadata, 'keywords')
    if _has_control(keywords):
        findings.append(_error('keywords_multiline', tx(
            'Parole chiave con caratteri di controllo: devono stare su una riga.')))
    elif not keywords.strip():
        findings.append(_warning('keywords_empty', tx(
            'Parole chiave vuote: aiutano la ricerca nel selettore dei simboli.')))

    datasheet = _string(metadata, 'datasheet')
    if _has_control(datasheet):
        findings.append(_error('datasheet_format', tx(
            'Datasheet «{datasheet}»: deve essere un URL o un percorso senza spazi.', datasheet=datasheet)))
    elif any(c.isspace() for c in datasheet.strip()):
        # KiCad accepts blanks in the Datasheet property (it is a path); the
        # KLC asks for a link, which a path with blanks seldom is: advice only.
        findings.append(_warning('datasheet_blanks', tx(
            'Datasheet «{datasheet}»: deve essere un URL o un percorso senza spazi.', datasheet=datasheet)))
    elif not datasheet.strip():
        findings.append(_warning('datasheet_empty', tx('Datasheet vuoto: indicare l’URL o il percorso del documento.')))

    fp_filters = _string(metadata, 'fp_filters')
    if _has_control(fp_filters):
        findings.append(_error('fp_filters_format', tx(
            'Filtri footprint con caratteri di controllo.')))
    elif not fp_filters.strip():
        findings.append(_warning('fp_filters_empty', tx(
            'Filtri footprint vuoti: un filtro come «{suggestion}» limita le scelte al footprint giusto.',
            suggestion=f'{footprint_name}*' if footprint_name else 'Nome*')))
    else:
        for pattern in fp_filters.split():
            # A filter may carry one library nickname before `:` (KiCad matches
            # «Lib:Name» when the pattern contains a colon); path separators never.
            if pattern.count(':') > 1 or any(c in '/\\' for c in pattern):
                findings.append(_error('fp_filters_format', tx(
                    'Filtro footprint «{pattern}»: ammessi solo nome, * e ?.', pattern=pattern)))

    findings.extend(name_findings(footprint_name, code='footprint_name', label=tx('Footprint')))
    return findings


# ── s-expression helpers (nested lists from symbol_glyphs.parse_sexpr) ──────

def _unit_pins(symbol) -> list[tuple[str, list]]:
    """(unit key, pin form) for every pin of a library symbol.

    Pins live in the sub-symbols `NAME_<unit>_<style>`; the unit key is the
    sub-symbol name, so two units never see each other's pins. A pin written
    directly in the parent symbol (older files) belongs to the unit ''.
    """
    pairs = []
    for item in symbol[2:] if len(symbol) > 2 else []:
        if not isinstance(item, list) or not item:
            continue
        if item[0] == 'pin':
            pairs.append(('', item))
        elif item[0] == 'symbol':
            unit = str(item[1]) if len(item) > 1 else ''
            pairs.extend((unit, pin) for pin in _forms(item, 'pin'))
    return pairs


def _pin_hidden(pin) -> bool:
    """True for a hidden pin: `hide` token (KiCad 6) or `(hide yes)` (KiCad 8+)."""
    for item in pin[1:]:
        if item == 'hide':
            return True
        if isinstance(item, list) and item and item[0] == 'hide':
            return len(item) < 2 or str(item[1]).lower() != 'no'
    return False


def _is_pin_stack(pins: list) -> bool:
    """True when pins at one position are a legal stack (KLC S4.3): the same
    name, at most one of them visible."""
    names = {name for name, _hidden in pins}
    visible = sum(1 for _name, hidden in pins if not hidden)
    return len(names) == 1 and visible <= 1


def _forms(tree, name: str):
    """Every form named `name` at any depth, in document order."""
    found = []
    stack = [tree]
    while stack:
        node = stack.pop(0)
        if isinstance(node, list):
            if node and node[0] == name:
                found.append(node)
            stack[0:0] = [item for item in node if isinstance(item, list)]
    return found


def _child(form, name):
    for item in form:
        if isinstance(item, list) and item and item[0] == name:
            return item
    return None


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _on_grid(value: float) -> bool:
    return abs(value / PIN_GRID_MM - round(value / PIN_GRID_MM)) * PIN_GRID_MM <= TOLERANCE


def _font_size(form) -> tuple[float, float] | None:
    effects = _child(form, 'effects')
    font = _child(effects, 'font') if effects else None
    size = _child(font, 'size') if font else None
    if size is None or len(size) < 3:
        return None
    width, height = _number(size[1]), _number(size[2])
    return None if width is None or height is None else (width, height)


def _parse(text: str, code: str, label: str) -> tuple[list | None, list[Finding]]:
    if not isinstance(text, str) or not text.strip():
        return None, [_error(code, tx('{label}: file vuoto.', label=label))]
    try:
        return parse_sexpr(text), []
    except ValueError as exc:
        return None, [_error(code, tx('{label}: file non leggibile: {error}', label=label, error=exc))]


def check_symbol_text(text: str) -> list[Finding]:
    """@brief Check a `.kicad_sym` text against the symbol rules of the KLC.

    @param text Whole library text (one or more symbols).
    @return Findings: format version (error), pin off the 100 mil grid
        (error), duplicate pin number or two pins at one position (error),
        pin length other than 2.54 mm, pin texts other than 1.27 mm, body
        outline other than 0.254 mm/background fill, missing pin name
        offset (warnings).
    @note The body is the largest rectangle of the symbol: that is what the
        KLC calls the outline, whatever the symbol is called.
    """
    tree, findings = _parse(text, 'symbol_unreadable', tx('Simbolo'))
    if tree is None:
        return findings
    library = next((form for form in tree if isinstance(form, list) and form and form[0] == 'kicad_symbol_lib'), None)
    if library is None:
        return [_error('symbol_format', tx('Simbolo: il file non è una libreria di simboli KiCad.'))]
    version = _child(library, 'version')
    number = _number(version[1]) if version and len(version) > 1 else None
    if number is None:
        findings.append(_error('symbol_version', tx('Simbolo: versione del formato mancante.')))
    elif number < MIN_SYMBOL_FORMAT:
        findings.append(_error('symbol_version', tx(
            'Simbolo: formato {version} troppo vecchio (minimo {minimum}).',
            version=int(number), minimum=MIN_SYMBOL_FORMAT)))

    for symbol in [form for form in library if isinstance(form, list) and form and form[0] == 'symbol']:
        name = str(symbol[1]) if len(symbol) > 1 else ''
        pin_names = _child(symbol, 'pin_names')
        offset = _child(pin_names, 'offset') if pin_names else None
        offset_value = _number(offset[1]) if offset and len(offset) > 1 else None
        # KiCad omits (pin_names (offset ...)) when the offset is its default
        # 0.508 mm, which is the KLC value: only a different offset is advice.
        if offset_value is not None and abs(offset_value - PIN_NAME_OFFSET_MM) > TOLERANCE:
            findings.append(_warning('pin_names_offset', tx(
                'Simbolo «{name}»: offset dei nomi dei pin {offset} mm invece di 0,508 mm.',
                name=name, offset=offset_value)))
        numbers: dict[str, int] = {}
        # Positions are compared within one unit only (KLC S4: units are
        # separate drawings); `positions[unit][(x, y)]` lists (name, hidden).
        positions: dict[str, dict[tuple[float, float], list]] = {}
        for unit, pin in _unit_pins(symbol):
            at = _child(pin, 'at')
            x = _number(at[1]) if at and len(at) > 2 else None
            y = _number(at[2]) if at and len(at) > 2 else None
            number_form = _child(pin, 'number')
            pin_number = str(number_form[1]) if number_form and len(number_form) > 1 else ''
            if x is None or y is None:
                findings.append(_error('pin_position', tx(
                    'Simbolo «{name}»: pin {pin} senza posizione.', name=name, pin=pin_number)))
                continue
            if not (_on_grid(x) and _on_grid(y)):
                findings.append(_error('pin_grid', tx(
                    'Simbolo «{name}»: pin {pin} fuori dalla griglia di 2,54 mm ({x}, {y}).',
                    name=name, pin=pin_number, x=x, y=y)))
            length = _child(pin, 'length')
            length_value = _number(length[1]) if length and len(length) > 1 else None
            if length_value is None or length_value < PIN_LENGTH_MM - TOLERANCE or abs(
                    length_value / PIN_LENGTH_STEP_MM - round(length_value / PIN_LENGTH_STEP_MM)) > 1e-4:
                findings.append(_warning('pin_length', tx(
                    'Simbolo «{name}»: pin {pin} lungo {length} mm invece di 2,54 mm.',
                    name=name, pin=pin_number, length=length_value)))
            for part in ('name', 'number'):
                size = _font_size(_child(pin, part) or [])
                if size is None or any(abs(v - TEXT_SIZE_MM) > TOLERANCE for v in size):
                    findings.append(_warning('pin_text_size', tx(
                        'Simbolo «{name}»: testo del pin {pin} non a 1,27 mm.', name=name, pin=pin_number)))
                    break
            numbers[pin_number] = numbers.get(pin_number, 0) + 1
            name_form = _child(pin, 'name')
            pin_name = str(name_form[1]) if name_form and len(name_form) > 1 else ''
            key = (round(x, 4), round(y, 4))
            positions.setdefault(unit, {}).setdefault(key, []).append((pin_name, _pin_hidden(pin)))
        for pin_number, count in numbers.items():
            if count > 1:
                findings.append(_error('pin_duplicate', tx(
                    'Simbolo «{name}»: numero di pin {pin} ripetuto {count} volte.',
                    name=name, pin=pin_number, count=count)))
        for unit_positions in positions.values():
            for (x, y), stacked in unit_positions.items():
                if len(stacked) > 1 and not _is_pin_stack(stacked):
                    findings.append(_error('pin_overlap', tx(
                        'Simbolo «{name}»: {count} pin nella stessa posizione ({x}, {y}).',
                        name=name, count=len(stacked), x=x, y=y)))
        body = None
        for rectangle in _forms(symbol, 'rectangle'):
            start, end = _child(rectangle, 'start'), _child(rectangle, 'end')
            if not start or not end or len(start) < 3 or len(end) < 3:
                continue
            coords = [_number(v) for v in (start[1], start[2], end[1], end[2])]
            if any(v is None for v in coords):
                continue
            area = abs(coords[2] - coords[0]) * abs(coords[3] - coords[1])
            if body is None or area > body[0]:
                body = (area, rectangle)
        if body is not None:
            stroke = _child(body[1], 'stroke')
            width = _child(stroke, 'width') if stroke else None
            width_value = _number(width[1]) if width and len(width) > 1 else None
            fill = _child(body[1], 'fill')
            fill_type = _child(fill, 'type') if fill else None
            fill_value = fill_type[1] if fill_type and len(fill_type) > 1 else None
            if width_value is None or abs(width_value - BODY_STROKE_MM) > TOLERANCE:
                findings.append(_warning('body_stroke', tx(
                    'Simbolo «{name}»: bordo del corpo {width} mm invece di 0,254 mm.',
                    name=name, width=width_value)))
            if fill_value != BODY_FILL:
                findings.append(_warning('body_fill', tx(
                    'Simbolo «{name}»: il corpo non ha il riempimento «background».', name=name)))
    return findings


def check_footprint_text(text: str) -> list[Finding]:
    """@brief Check a `.kicad_mod` text against the footprint rules of the KLC.

    @param text Whole footprint file text.
    @return Findings: format version missing or no pad (errors); attribute
        (smd/through_hole), Reference on F.SilkS as `REF**`, Value on F.Fab,
        missing F.CrtYd or F.Fab graphics, empty description (warnings).
    """
    tree, findings = _parse(text, 'footprint_unreadable', tx('Footprint'))
    if tree is None:
        return findings
    footprint = next((form for form in tree if isinstance(form, list) and form and form[0] == 'footprint'), None)
    if footprint is None:
        return [_error('footprint_format', tx('Footprint: il file non è un footprint KiCad.'))]
    name = str(footprint[1]) if len(footprint) > 1 else ''
    version = _child(footprint, 'version')
    if version is None or len(version) < 2 or _number(version[1]) is None:
        findings.append(_error('footprint_version', tx('Footprint: versione del formato mancante.')))
    attr = _child(footprint, 'attr')
    if attr is None or len(attr) < 2 or attr[1] not in ('smd', 'through_hole'):
        findings.append(_warning('footprint_attr', tx(
            'Footprint «{name}»: attributo smd/through_hole assente.', name=name)))
    if not _forms(footprint, 'pad'):
        # Legal in KiCad (logos, mounting graphics); for a component it is
        # almost surely a generation gone wrong: advice, not a refusal.
        findings.append(_warning('footprint_pads', tx('Footprint «{name}»: nessun pad.', name=name)))
    descr = _child(footprint, 'descr')
    if descr is None or len(descr) < 2 or not str(descr[1]).strip():
        findings.append(_warning('footprint_descr', tx(
            'Footprint «{name}»: descrizione (descr) vuota.', name=name)))

    properties = {}
    for prop in [form for form in footprint if isinstance(form, list) and form and form[0] == 'property']:
        if len(prop) > 2:
            properties[str(prop[1])] = prop
    reference = properties.get('Reference')
    reference_layer = _child(reference, 'layer') if reference else None
    if (reference is None or str(reference[2]) != 'REF**'
            or not reference_layer or len(reference_layer) < 2 or reference_layer[1] != 'F.SilkS'):
        findings.append(_warning('footprint_reference', tx(
            'Footprint «{name}»: il riferimento deve essere «REF**» sul layer F.SilkS.', name=name)))
    value = properties.get('Value')
    value_layer = _child(value, 'layer') if value else None
    if value is None or not value_layer or len(value_layer) < 2 or value_layer[1] != 'F.Fab':
        findings.append(_warning('footprint_value', tx(
            'Footprint «{name}»: il valore deve stare sul layer F.Fab.', name=name)))

    # Only drawn items count as the courtyard/fabrication outline: the Value
    # text sits on F.Fab too, and a text is not an outline.
    layers = set()
    for graphic in [form for form in footprint if isinstance(form, list) and form
                    and str(form[0]).startswith('fp_')]:
        layer = _child(graphic, 'layer')
        if layer and len(layer) > 1:
            layers.add(str(layer[1]))
    if 'F.CrtYd' not in layers:
        findings.append(_warning('footprint_courtyard', tx(
            'Footprint «{name}»: manca il courtyard (layer F.CrtYd).', name=name)))
    if 'F.Fab' not in layers:
        findings.append(_warning('footprint_fab', tx(
            'Footprint «{name}»: manca il contorno di fabbricazione (layer F.Fab).', name=name)))
    findings.extend(name_findings(name, code='footprint_name', label=tx('Footprint')))
    return findings


def check_all(metadata: dict, footprint_name: str, symbol_text: str | None = None,
              footprint_text: str | None = None) -> list[Finding]:
    """@brief Run every available check and return the findings, errors first.

    @param metadata See `check_metadata`.
    @param footprint_name See `check_metadata`.
    @param symbol_text `.kicad_sym` text, or None when not generated yet.
    @param footprint_text `.kicad_mod` text, or None when not generated yet.
    @return Errors first, then warnings; within a severity the rule order of
        the single checks is kept. Duplicate findings are removed.
    """
    findings = list(check_metadata(metadata, footprint_name))
    if symbol_text is not None:
        findings.extend(check_symbol_text(symbol_text))
    if footprint_text is not None:
        findings.extend(check_footprint_text(footprint_text))
    unique = list(dict.fromkeys(findings))
    return ([f for f in unique if f.severity == 'error']
            + [f for f in unique if f.severity == 'warning'])
