"""@brief Graphic elements («glyphs») for the schematic symbol editor, taken from KiCad's libraries.

A glyph is a set of drawing primitives (polylines, circles, arcs,
rectangles) plus the terminals where pins attach, in symbol millimetres
with Y up, like a `.kicad_sym` file. The catalogue is read from the symbol
libraries of the installed KiCad (Device, Relay, Switch, Transformer), so a
relay drawn here is the relay every KiCad user knows; when KiCad is not
installed a small built-in set of standard shapes stands in. A placed glyph
carries its own primitives in the project, so a project moved to a machine
without KiCad keeps its drawing.

User request of 2026-10-10: «una palette di elementi grafici presi da
KiCad» for a more realistic symbol than the bare rectangle.

Wires and anchors (user request of 2026-10-10, later the same day): a wire
is a placed record with key "wire" whose single polyline joins the points
the user clicked. A point that was snapped onto an element terminal or onto
a pin stays attached to it: the record stores an *anchor* (the element's
stable id and the terminal index, or the pin number) next to the last
resolved coordinates, and every consumer resolves the anchor against the
CURRENT geometry through `resolve_wire_points`, so moving, turning or
mirroring the element, or moving the pin, drags the wire end along. The
stored coordinates (in the wire's own frame, like a free point) are the
fallback when the anchor no longer resolves; `refresh_wire_anchors` keeps
them at the last resolved position every time a draft is committed.
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

from ..services.messages import tx

#: Key of the placed record that is a free wire rather than a catalogue glyph.
WIRE_KEY = "wire"

#: Unit vector of a pin/terminal angle towards the body: 0 right, 90 up,
#: 180 left, 270 down (KiCad's convention, also the editor's).
_DIRECTION: dict[int, tuple[int, int]] = {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}

#: Palette groups, in display order: (group key, Italian label).
GROUPS: tuple[tuple[str, str], ...] = (
    ("relay", "Relè e contatti"),
    ("passive", "Passivi"),
    ("semiconductor", "Semiconduttori"),
    ("switch", "Interruttori e comandi"),
    ("electromechanical", "Elettromeccanici"),
    ("transformer", "Trasformatori"),
)

#: Catalogue entries: (key, Italian label, group, KiCad library, KiCad symbol name).
#: The label is translated through the message catalogue when shown.
KICAD_GLYPHS: tuple[tuple[str, str, str, str, str], ...] = (
    ("relay_spst_no", "Relè SPST (bobina + contatto NA)", "relay", "Relay", "Relay_SPST-NO"),
    ("relay_spst_nc", "Relè SPST (bobina + contatto NC)", "relay", "Relay", "Relay_SPST-NC"),
    ("relay_spdt", "Relè SPDT (bobina + scambio)", "relay", "Relay", "Relay_SPDT"),
    ("relay_dpdt", "Relè DPDT (bobina + due scambi)", "relay", "Relay", "Relay_DPDT"),
    ("resistor", "Resistenza", "passive", "Device", "R"),
    ("potentiometer", "Potenziometro", "passive", "Device", "R_Potentiometer"),
    ("capacitor", "Condensatore", "passive", "Device", "C"),
    ("capacitor_polarized", "Condensatore polarizzato", "passive", "Device", "C_Polarized"),
    ("inductor", "Induttore", "passive", "Device", "L"),
    ("ferrite_bead", "Ferrite", "passive", "Device", "FerriteBead"),
    ("fuse", "Fusibile", "passive", "Device", "Fuse"),
    ("polyfuse", "Fusibile ripristinabile", "passive", "Device", "Polyfuse"),
    ("thermistor", "Termistore", "passive", "Device", "Thermistor"),
    ("varistor", "Varistore", "passive", "Device", "Varistor"),
    ("crystal", "Quarzo", "passive", "Device", "Crystal"),
    ("battery", "Batteria (cella)", "passive", "Device", "Battery_Cell"),
    ("diode", "Diodo", "semiconductor", "Device", "D"),
    ("diode_zener", "Diodo zener", "semiconductor", "Device", "D_Zener"),
    ("diode_schottky", "Diodo Schottky", "semiconductor", "Device", "D_Schottky"),
    ("photodiode", "Fotodiodo", "semiconductor", "Device", "D_Photo"),
    ("led", "LED", "semiconductor", "Device", "LED"),
    ("npn", "Transistor NPN", "semiconductor", "Transistor_BJT", "Q_NPN_BCE"),
    ("pnp", "Transistor PNP", "semiconductor", "Transistor_BJT", "Q_PNP_BCE"),
    ("nmos", "MOSFET canale N", "semiconductor", "Transistor_FET", "Q_NMOS_DGS"),
    ("pmos", "MOSFET canale P", "semiconductor", "Transistor_FET", "Q_PMOS_DGS"),
    ("switch_spst", "Interruttore SPST", "switch", "Switch", "SW_SPST"),
    ("switch_spdt", "Deviatore SPDT", "switch", "Switch", "SW_SPDT"),
    ("switch_dpdt", "Deviatore doppio DPDT", "switch", "Switch", "SW_DPDT_x2"),
    ("push_button", "Pulsante", "switch", "Switch", "SW_Push"),
    ("dip_switch", "DIP switch (1 via)", "switch", "Switch", "SW_DIP_x01"),
    ("motor_dc", "Motore in corrente continua", "electromechanical", "Motor", "Motor_DC"),
    ("speaker", "Altoparlante", "electromechanical", "Device", "Speaker"),
    ("buzzer", "Cicalino", "electromechanical", "Device", "Buzzer"),
    ("lamp", "Lampada", "electromechanical", "Device", "Lamp"),
    ("transformer", "Trasformatore (1 primario, 1 secondario)", "transformer", "Device", "Transformer_1P_1S"),
    ("transformer_2s", "Trasformatore (1 primario, 2 secondari)", "transformer", "Device", "Transformer_1P_2S"),
)

#: Tolerance, in mm, within which a dropped pin snaps onto a glyph terminal:
#: half a grid step, so a pin aimed at a terminal takes it and a pin dropped
#: a full step away keeps its own position.
TERMINAL_SNAP_MM = 1.27


@dataclass(frozen=True)
class Primitive:
    """@brief One drawing primitive of a glyph, in symbol millimetres (Y up).

    @note `kind` is one of polyline, circle, arc, rectangle. `points`: the
        polyline vertices, the circle centre, the arc start/mid/end, or the
        rectangle's two corners. `radius` for a circle. `fill` as in KiCad:
        none, outline or background. `width` is the stroke width in mm
        (0 = KiCad default).
    """
    kind: str
    points: tuple[tuple[float, float], ...]
    radius: float = 0.0
    fill: str = "none"
    width: float = 0.0


@dataclass(frozen=True)
class Terminal:
    """@brief Where a pin attaches: the pin position and orientation KiCad's symbol uses.

    @note `x`, `y` are the OUTER end of the pin, `angle` the direction towards
        the body (0 right, 90 up, 180 left, 270 down) and `length` the pin
        length, exactly the semantics of `(pin ... (at x y angle) (length l))`
        and of the editor's `symbol_position`/`symbol_angle`.
    """
    x: float
    y: float
    angle: int
    length: float
    name: str = ""
    number: str = ""


@dataclass(frozen=True)
class Glyph:
    """@brief A catalogue element: its primitives, terminals and where it came from."""
    key: str
    label: str
    primitives: tuple[Primitive, ...]
    terminals: tuple[Terminal, ...]
    source: str
    #: Width and height of the body outline the library symbol had, when it
    #: had one (dropped from the primitives: the editor draws its own body).
    body_hint: Optional[tuple[float, float]] = None
    #: Palette group key (see GROUPS); "" puts the glyph in the last group.
    group: str = ""


# ── S-expression reader ────────────────────────────────────────────────────

def parse_sexpr(text: str):
    """@brief Parse a KiCad s-expression text into nested Python lists.

    @param text Source text (a whole `.kicad_sym` file or a fragment).
    @return A list for each parenthesised form; atoms are str (symbols and
        quoted strings alike, quotes removed and escapes resolved) or float
        when they read as a number.
    @throws ValueError On unbalanced parentheses or an unterminated string.
    """
    stack = [[]]
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif c == "(":
            stack.append([])
            i += 1
        elif c == ")":
            if len(stack) == 1:
                raise ValueError(tx("Parentesi chiusa senza apertura nel file di simboli."))
            done = stack.pop()
            stack[-1].append(done)
            i += 1
        elif c == '"':
            j = i + 1
            out = []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    out.append(text[j + 1])
                    j += 2
                else:
                    out.append(text[j])
                    j += 1
            if j >= n:
                raise ValueError(tx("Stringa non terminata nel file di simboli."))
            stack[-1].append("".join(out))
            i = j + 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in "()":
                j += 1
            atom = text[i:j]
            try:
                stack[-1].append(float(atom))
            except ValueError:
                stack[-1].append(atom)
            i = j
    if len(stack) != 1:
        raise ValueError(tx("Parentesi aperta senza chiusura nel file di simboli."))
    return stack[0]


def _child(form, name):
    """First sub-form named `name`, or None."""
    for item in form:
        if isinstance(item, list) and item and item[0] == name:
            return item
    return None


def _children(form, name):
    return [item for item in form if isinstance(item, list) and item and item[0] == name]


def _xy(form) -> tuple[float, float]:
    return (float(form[1]), float(form[2]))


def _fill_of(form) -> str:
    fill = _child(form, "fill")
    kind = _child(fill, "type") if fill else None
    value = kind[1] if kind and len(kind) > 1 else "none"
    return value if value in ("none", "outline", "background") else "none"


def _width_of(form) -> float:
    stroke = _child(form, "stroke")
    width = _child(stroke, "width") if stroke else None
    return float(width[1]) if width and len(width) > 1 else 0.0


def primitives_of(unit_form) -> list[Primitive]:
    """@brief The drawing primitives of one `(symbol "Name_u_s" ...)` unit form.

    @param unit_form Parsed unit form.
    @return Polylines, circles, arcs and rectangles in order; pins and texts
        are not primitives and are skipped.
    """
    result = []
    for item in unit_form:
        if not isinstance(item, list) or not item:
            continue
        kind = item[0]
        if kind == "polyline":
            pts = _child(item, "pts") or []
            points = tuple(_xy(p) for p in pts[1:] if isinstance(p, list) and p and p[0] == "xy")
            if len(points) >= 2:
                result.append(Primitive("polyline", points, 0.0, _fill_of(item), _width_of(item)))
        elif kind == "circle":
            centre = _child(item, "center")
            radius = _child(item, "radius")
            if centre and radius:
                result.append(Primitive("circle", (_xy(centre),), float(radius[1]),
                                        _fill_of(item), _width_of(item)))
        elif kind == "arc":
            start, mid, end = _child(item, "start"), _child(item, "mid"), _child(item, "end")
            if start and mid and end:
                result.append(Primitive("arc", (_xy(start), _xy(mid), _xy(end)), 0.0,
                                        _fill_of(item), _width_of(item)))
        elif kind == "rectangle":
            start, end = _child(item, "start"), _child(item, "end")
            if start and end:
                result.append(Primitive("rectangle", (_xy(start), _xy(end)), 0.0,
                                        _fill_of(item), _width_of(item)))
    return result


def terminals_of(unit_form) -> list[Terminal]:
    """@brief The pins of one unit form as terminals (position, direction, length, name, number)."""
    result = []
    for pin in _children(unit_form, "pin"):
        at = _child(pin, "at")
        length = _child(pin, "length")
        if not at or len(at) < 4:
            continue
        name = _child(pin, "name")
        number = _child(pin, "number")
        result.append(Terminal(float(at[1]), float(at[2]), int(float(at[3])) % 360,
                               float(length[1]) if length and len(length) > 1 else 2.54,
                               str(name[1]) if name and len(name) > 1 else "",
                               str(number[1]) if number and len(number) > 1 else ""))
    return result


def _bounds(primitives: Iterable[Primitive]) -> Optional[tuple[float, float, float, float]]:
    xs, ys = [], []
    for p in primitives:
        for x, y in p.points:
            xs.append(x)
            ys.append(y)
        if p.kind == "circle":
            (cx, cy), r = p.points[0], p.radius
            xs.extend((cx - r, cx + r))
            ys.extend((cy - r, cy + r))
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _drop_body_outline(primitives: list[Primitive]) -> tuple[list[Primitive], Optional[tuple[float, float]]]:
    """Remove the outer body rectangle of a library symbol, keeping its size as a hint.

    The editor draws its own body: a symbol's outline (the largest
    background-filled rectangle that contains every other primitive) would
    be a rectangle inside a rectangle. Nothing else is touched.
    """
    others_bounds = None
    candidates = [p for p in primitives if p.kind == "rectangle" and p.fill == "background"]
    if not candidates:
        return primitives, None
    outline = max(candidates, key=lambda p: abs((p.points[1][0] - p.points[0][0])
                                                 * (p.points[1][1] - p.points[0][1])))
    (x0, y0), (x1, y1) = outline.points
    left, right = min(x0, x1), max(x0, x1)
    bottom, top = min(y0, y1), max(y0, y1)
    rest = [p for p in primitives if p is not outline]
    others_bounds = _bounds(rest)
    if others_bounds is not None:
        ox0, oy0, ox1, oy1 = others_bounds
        if not (left - 1e-6 <= ox0 and ox1 <= right + 1e-6 and bottom - 1e-6 <= oy0 and oy1 <= top + 1e-6):
            return primitives, None
    return rest, (round(right - left, 4), round(top - bottom, 4))


def load_library_glyph(library: Path, symbol_name: str, key: str = "", label: str = "",
                       group: str = "") -> Optional[Glyph]:
    """@brief Read one symbol of a KiCad library as a glyph.

    @param library Path of the `.kicad_sym` file.
    @param symbol_name Name of the symbol inside it (not a derived one).
    @param key Catalogue key to give the glyph (defaults to the symbol name).
    @param label Label to give it (defaults to the symbol name).
    @param group Palette group key.
    @return The glyph with every unit's primitives (body outline dropped) and
        the pins of every unit as terminals; None when the file or the symbol
        is missing or unreadable.
    """
    try:
        text = Path(library).read_text(encoding="utf-8")
        forms = parse_sexpr(text)
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    root = forms[0] if forms and isinstance(forms[0], list) else None
    if not root or root[0] != "kicad_symbol_lib":
        return None
    symbol = next((item for item in _children(root, "symbol")
                   if len(item) > 1 and item[1] == symbol_name), None)
    if symbol is None:
        return None
    primitives: list[Primitive] = []
    terminals: list[Terminal] = []
    for unit in _children(symbol, "symbol"):
        primitives.extend(primitives_of(unit))
        terminals.extend(terminals_of(unit))
    primitives, body_hint = _drop_body_outline(primitives)
    if not primitives:
        return None
    return Glyph(key or symbol_name, label or symbol_name, tuple(primitives), tuple(terminals),
                 f"kicad:{Path(library).stem}:{symbol_name}", body_hint, group)


def kicad_symbols_dir() -> Optional[Path]:
    """@brief The `symbols` folder of the installed KiCad, or None when KiCad is not found.

    @note Derived from kicad-cli's location (`<kicad>/bin/kicad-cli` ->
        `<kicad>/share/kicad/symbols`), the layout of every KiCad install.
    """
    from ..services.system_dependencies import find_kicad_cli
    cli = find_kicad_cli()
    if not cli:
        return None
    root = Path(cli).resolve().parent.parent
    folder = root / "share" / "kicad" / "symbols"
    return folder if folder.is_dir() else None


# ── Built-in fallback ─────────────────────────────────────────────────────

def _poly(*points, fill="none", width=0.254) -> Primitive:
    return Primitive("polyline", tuple(points), 0.0, fill, width)


def relay_part_glyphs() -> tuple[Glyph, ...]:
    """@brief Coil, contacts and dashed link as SEPARATE elements, in KiCad's own relay style.

    @return Coil, dashed link, changeover contact, NO contact, NC contact,
        each centred on its own origin with terminals on the 100 mil grid.
    @note The geometry is that of KiCad's `Relay_SPDT` (Relay library),
        split and re-centred, so a coil placed next to a contact looks
        exactly like the library relay; the user composes relays with any
        number of contacts (user, 2026-10-10: «bobina e contatti inseribili
        separatamente»). Always in the catalogue, KiCad installed or not.
    """
    source = "builtin:relay"
    coil = Glyph("relay_coil", "Bobina di relè", (
        Primitive("rectangle", ((-3.175, 1.905), (3.175, -1.905)), 0.0, "none", 0.254),
        _poly((-2.54, -1.905), (2.54, 1.905)),
        _poly((0, 5.08), (0, 1.905), width=0), _poly((0, -5.08), (0, -1.905), width=0)),
        (Terminal(0, 7.62, 270, 2.54, "", "A1"), Terminal(0, -7.62, 90, 2.54, "", "A2")), source)
    link = Glyph("relay_link", "Collegamento tratteggiato bobina-contatto", (
        _poly((-2.54, 0), (-1.905, 0)), _poly((-1.27, 0), (-0.635, 0)),
        _poly((0, 0), (0.635, 0)), _poly((1.27, 0), (1.905, 0))), (), source)
    nc_marker = Primitive("polyline", ((-2.54, 5.08), (-2.54, 2.54), (-1.905, 3.175), (-2.54, 3.81)),
                          0.0, "outline", 0.0)
    no_marker = Primitive("polyline", ((2.54, 5.08), (2.54, 2.54), (1.905, 3.175), (2.54, 3.81)),
                          0.0, "none", 0.0)
    blade = _poly((0, -2.54), (-1.905, 3.81), width=0.508)
    stem = _poly((0, -2.54), (0, -5.08), width=0)
    common = Terminal(0, -7.62, 90, 2.54, "COM", "11")
    changeover = Glyph("relay_contact_spdt", "Contatto in scambio", (blade, stem, nc_marker, no_marker),
                       (common, Terminal(-2.54, 7.62, 270, 2.54, "NC", "12"),
                        Terminal(2.54, 7.62, 270, 2.54, "NO", "14")), source)
    normally_open = Glyph("relay_contact_no", "Contatto NA (normalmente aperto)", (blade, stem, no_marker),
                          (Terminal(0, -7.62, 90, 2.54, "COM", "13"),
                           Terminal(2.54, 7.62, 270, 2.54, "NO", "14")), source)
    normally_closed = Glyph("relay_contact_nc", "Contatto NC (normalmente chiuso)", (blade, stem, nc_marker),
                            (common, Terminal(-2.54, 7.62, 270, 2.54, "NC", "12")), source)
    return tuple(replace(glyph, group="relay")
                 for glyph in (coil, link, changeover, normally_open, normally_closed))


def _builtin_glyphs() -> tuple[Glyph, ...]:
    """The standard shapes drawn by hand on the 100 mil grid, for a machine without KiCad.

    Terminals follow KiCad's convention (outer end of a 2.54 mm pin, angle
    towards the body). The shapes are the usual IEC ones; a KiCad library,
    when present, replaces them with the originals. The relay parts are
    appended in both cases (see `relay_part_glyphs`).
    """
    source = "builtin"
    resistor = Glyph("resistor", "Resistenza", (
        Primitive("rectangle", ((-1.016, 2.54), (1.016, -2.54)), 0.0, "none", 0.254),),
        (Terminal(0, 5.08, 270, 2.54), Terminal(0, -5.08, 90, 2.54)), source)
    capacitor = Glyph("capacitor", "Condensatore", (
        _poly((-2.032, 0.762), (2.032, 0.762), width=0.508),
        _poly((-2.032, -0.762), (2.032, -0.762), width=0.508)),
        (Terminal(0, 3.81, 270, 2.54), Terminal(0, -3.81, 90, 2.54)), source)
    inductor = Glyph("inductor", "Induttore", tuple(
        Primitive("arc", ((0, y + 1.27), (0.635, y), (0, y - 1.27)), 0.0, "none", 0.254)
        for y in (3.81, 1.27, -1.27, -3.81)),
        (Terminal(0, 7.62, 270, 2.54), Terminal(0, -7.62, 90, 2.54)), source)
    diode = Glyph("diode", "Diodo", (
        _poly((-1.27, 1.27), (-1.27, -1.27), (1.27, 0), (-1.27, 1.27)),
        _poly((1.27, 1.27), (1.27, -1.27), width=0.254)),
        (Terminal(-3.81, 0, 0, 2.54), Terminal(3.81, 0, 180, 2.54)), source)
    led = Glyph("led", "LED", diode.primitives + (
        _poly((0.635, 1.905), (1.905, 3.175)), _poly((1.905, 3.175), (1.27, 3.175)),
        _poly((1.905, 3.175), (1.905, 2.54)),
        _poly((-0.635, 1.905), (0.635, 3.175)), _poly((0.635, 3.175), (0, 3.175)),
        _poly((0.635, 3.175), (0.635, 2.54))), diode.terminals, source)
    return (replace(resistor, group="passive"), replace(capacitor, group="passive"),
            replace(inductor, group="passive"), replace(diode, group="semiconductor"),
            replace(led, group="semiconductor"))


@lru_cache(maxsize=1)
def catalogue() -> tuple[Glyph, ...]:
    """@brief The glyphs offered by the palette: KiCad's when installed, built-in shapes otherwise.

    @return The separate relay parts (coil, link, contacts) first, then the
        KiCad library glyphs for every entry of KICAD_GLYPHS that reads
        correctly, in catalogue order; when none reads (no KiCad), the
        built-in shapes. Cached: the libraries do not change while running.
    """
    folder = kicad_symbols_dir()
    glyphs = []
    if folder is not None:
        for key, label, group, library, symbol in KICAD_GLYPHS:
            glyph = load_library_glyph(folder / f"{library}.kicad_sym", symbol, key, label, group)
            if glyph is not None:
                glyphs.append(glyph)
    base = tuple(glyphs) if glyphs else _builtin_glyphs()
    return relay_part_glyphs() + base


def grouped_catalogue() -> list[tuple[str, str, list[Glyph]]]:
    """@brief The catalogue split by palette group, in GROUPS order.

    @return (group key, Italian label, glyphs) for every group that has at
        least one glyph; glyphs of an unknown group go to a final «Altri».
    """
    glyphs = catalogue()
    known = [key for key, _label in GROUPS]
    result = []
    for key, label in GROUPS:
        members = [glyph for glyph in glyphs if glyph.group == key]
        if members:
            result.append((key, label, members))
    others = [glyph for glyph in glyphs if glyph.group not in known]
    if others:
        result.append(("other", "Altri", others))
    return result


# ── Placement ──────────────────────────────────────────────────────────────

def _transform(point: tuple[float, float], x: float, y: float, rotation: int, mirror: bool):
    px, py = point
    if mirror:
        px = -px
    angle = math.radians(rotation % 360)
    c, s = round(math.cos(angle), 12), round(math.sin(angle), 12)
    rx, ry = px * c - py * s, px * s + py * c
    return (round(rx + x, 4), round(ry + y, 4))


def new_instance_id() -> str:
    """@brief A fresh stable identity for a placed record (uuid4 hex), unique across projects."""
    return uuid.uuid4().hex


def ensure_instance_ids(instances: list) -> list:
    """@brief Give an id to every record that lacks one (legacy projects) or repeats another's.

    @param instances Placed records; mutated in place.
    @return The same list object, every dict record carrying a distinct non-empty `id`.
    @note A repeated id (a record duplicated by copying its dict) is replaced on
        the later record: anchors must name exactly one element.
    """
    seen: set[str] = set()
    for record in instances:
        if not isinstance(record, dict):
            continue
        ident = record.get("id")
        if not isinstance(ident, str) or not ident or ident in seen:
            ident = new_instance_id()
            record["id"] = ident
        seen.add(ident)
    return instances


def instance_from_glyph(glyph: Glyph, x: float, y: float, rotation: int = 0, mirror: bool = False) -> dict:
    """@brief The project record of a placed glyph: self-contained, JSON-friendly.

    @param glyph Catalogue glyph.
    @param x Placement X of the glyph origin, symbol mm.
    @param y Placement Y, symbol mm (Y up).
    @param rotation Quarter turns in degrees (0, 90, 180, 270), counter-clockwise.
    @param mirror Mirror about the glyph's vertical axis, applied before the rotation.
    @return {id, key, label, source, x, y, rotation, mirror, primitives, terminals}
        with the UNTRANSFORMED primitives and terminals, so a move or a turn
        never accumulates rounding; `id` is a fresh `new_instance_id()` that
        wire anchors refer to.
    """
    return {
        "id": new_instance_id(),
        "key": glyph.key, "label": glyph.label, "source": glyph.source,
        "x": float(x), "y": float(y), "rotation": int(rotation) % 360, "mirror": bool(mirror),
        "primitives": [{"kind": p.kind, "points": [list(pt) for pt in p.points], "radius": p.radius,
                        "fill": p.fill, "width": p.width} for p in glyph.primitives],
        "terminals": [{"x": t.x, "y": t.y, "angle": t.angle, "length": t.length,
                       "name": t.name, "number": t.number} for t in glyph.terminals],
    }


def _point_xy(point) -> tuple[float, float]:
    """The stored coordinates of a polyline point, `[x, y]` or `{"x", "y", ...}`."""
    if isinstance(point, dict):
        return (float(point.get("x", 0.0)), float(point.get("y", 0.0)))
    return (float(point[0]), float(point[1]))


def _anchor_of(point) -> Optional[dict]:
    """The anchor dict of a polyline point, None for a fixed point."""
    if isinstance(point, dict):
        anchor = point.get("anchor")
        return anchor if isinstance(anchor, dict) else None
    return None


def is_wire(instance: dict) -> bool:
    """@brief True when a placed record is a free wire (key "wire") rather than a glyph."""
    return isinstance(instance, dict) and instance.get("key") == WIRE_KEY


def terminal_inner_end(terminal: Terminal) -> tuple[float, float]:
    """@brief Where a placed terminal's pin meets the body: the point a wire end attaches to.

    @param terminal A placed terminal (`placed_terminals`): outer end plus angle towards the body.
    @return (x, y) in symbol mm, rounded to 4 decimals.
    """
    vx, vy = _DIRECTION[int(terminal.angle) % 360]
    return (round(terminal.x + vx * terminal.length, 4), round(terminal.y + vy * terminal.length, 4))


#: The two attachment points of a placed terminal. "outer": the terminal's own
#: position, the dot the editor draws (the pin end of the original KiCad symbol,
#: where the user connects a wire); "inner": where its stub meets the body
#: (`terminal_inner_end`). An anchor without `end` means "inner" (files saved
#: before the outer end was anchorable).
TERMINAL_ENDS = ("outer", "inner")


def terminal_end(terminal: Terminal, end: str = "inner") -> tuple[float, float]:
    """@brief An attachment point of a placed terminal: its dot ("outer") or its body end ("inner").

    @param terminal A placed terminal (`placed_terminals`).
    @param end "outer" or "inner" (anything else counts as "inner", the legacy default).
    @return (x, y) in symbol mm, rounded to 4 decimals.
    """
    if end == "outer":
        return (round(terminal.x, 4), round(terminal.y, 4))
    return terminal_inner_end(terminal)


def pin_inner_ends(layout) -> dict[str, tuple[float, float]]:
    """@brief The inner end of every pin of a `SymbolLayout`, by pin number.

    @param layout `symbol.SymbolLayout` (pins with x, y = outer end and angle towards the body).
    @return {pin number: (x, y)} in symbol mm, the convention the editor's
        wire snapping uses (angle 0 right, 90 up, 180 left, 270 down).
    """
    result = {}
    for item in layout.pins:
        vx, vy = _DIRECTION[int(item.angle) % 360]
        result[str(item.pin.number)] = (round(item.x + vx * layout.pin_length, 4),
                                        round(item.y + vy * layout.pin_length, 4))
    return result


def anchor_for_point(graphics: Iterable[dict], pin_ends: Optional[dict], x: float, y: float,
                     tolerance: float = TERMINAL_SNAP_MM) -> Optional[tuple[dict, tuple[float, float]]]:
    """@brief The attachment point nearest to (x, y) within the tolerance, as an anchor.

    @param graphics Placed records; an element without an `id` cannot be
        anchored to and is skipped (call `ensure_instance_ids` first).
    @param pin_ends `pin_inner_ends(layout)`, or None when there are no pins.
    @param x Candidate X, symbol mm.
    @param y Candidate Y, symbol mm.
    @param tolerance Maximum distance in mm (default TERMINAL_SNAP_MM).
    @return (anchor, point): `{"kind": "terminal", "element": id, "index": k,
        "end": "outer"|"inner"}` or `{"kind": "pin", "number": n}` and the
        resolved (x, y); None when nothing lies within the tolerance. Ties
        keep the first found (pins first, then the outer end of a terminal).
    @note Both ends of a terminal are offered: the dot drawn on the canvas
        (outer) is where a wire is connected, the body end (inner) where a
        glyph's own stub starts (user, 2026-10-10: a wire drawn to the
        coil's dot did not follow the coil).
    """
    best, best_distance = None, float(tolerance)
    for number, (px, py) in (pin_ends or {}).items():
        distance = math.hypot(px - x, py - y)
        if distance < best_distance or (best is None and distance <= best_distance):
            best, best_distance = ({"kind": "pin", "number": str(number)}, (px, py)), distance
    for instance in graphics:
        ident = instance.get("id") if isinstance(instance, dict) else None
        if not isinstance(ident, str) or not ident or is_wire(instance):
            continue
        for index, terminal in enumerate(placed_terminals(instance)):
            for end in TERMINAL_ENDS:
                px, py = terminal_end(terminal, end)
                distance = math.hypot(px - x, py - y)
                if distance < best_distance or (best is None and distance <= best_distance):
                    best = ({"kind": "terminal", "element": ident, "index": index, "end": end}, (px, py))
                    best_distance = distance
    return best


def adopt_coincident_anchors(graphics: list, pin_ends: Optional[dict],
                             tolerance: float = 1e-4) -> int:
    """@brief Anchor every free wire point that lies exactly on a pin end or terminal end.

    @param graphics Every placed record; the wires are mutated in place.
    @param pin_ends `pin_inner_ends(layout)` of the current layout.
    @param tolerance Coincidence in mm (default 1e-4: the rounding of stored points).
    @return The number of points anchored.
    @note Migration of wires drawn before anchors existed: the editor has
        always snapped a wire point within TERMINAL_SNAP_MM of an attachment
        onto it, so a free point that coincides with one was attached on
        purpose. Anchors that already exist are left alone; a free point that
        merely passes near an attachment (not on it) stays free. Run before
        the project baseline is taken, so an untouched project does not
        open «modified».
    """
    by_id = elements_by_id(graphics)
    count = 0
    for instance in graphics:
        if not is_wire(instance):
            continue
        primitives = instance.get("primitives") or []
        if not primitives:
            continue
        points = primitives[0].get("points", [])
        resolved = resolve_wire_points(instance, graphics, pin_ends, by_id)
        for index, point in enumerate(points):
            if _anchor_of(point) is not None:
                continue
            found = anchor_for_point(graphics, pin_ends, resolved[index][0], resolved[index][1],
                                     tolerance=tolerance)
            if found is None:
                continue
            anchor, (ax, ay) = found
            lx, ly = wire_local_point(instance, ax, ay)
            points[index] = {"x": lx, "y": ly, "anchor": anchor}
            count += 1
    return count


def _resolve_anchor(anchor: dict, by_id: dict, pin_ends: Optional[dict]) -> Optional[tuple[float, float]]:
    """The current position of an anchor, None when its element/pin is gone."""
    kind = anchor.get("kind")
    if kind == "pin":
        return (pin_ends or {}).get(str(anchor.get("number")))
    if kind == "terminal":
        element = by_id.get(anchor.get("element"))
        if element is None:
            return None
        try:
            index = int(anchor.get("index", -1))
        except (TypeError, ValueError):
            return None
        terminals = placed_terminals(element)
        if not 0 <= index < len(terminals):
            return None
        return terminal_end(terminals[index], str(anchor.get("end", "inner")))
    return None


def elements_by_id(graphics: Optional[Iterable[dict]]) -> dict:
    """@brief The anchorable elements of a drawing by id: every identified non-wire record.

    @param graphics Placed records (None = none).
    @return {id: record}; computed once per redraw and handed to
        `resolve_wire_points`/`placed_primitives` so a scene with many wires
        does not rebuild it per wire.
    """
    return {inst["id"]: inst for inst in (graphics or ()) if isinstance(inst, dict)
            and isinstance(inst.get("id"), str) and inst["id"] and not is_wire(inst)}


def wire_local_point(wire_instance: dict, x: float, y: float) -> tuple[float, float]:
    """@brief An absolute point in the wire's own frame: the inverse of the wire's placement.

    @param wire_instance The wire record (x, y, rotation, mirror).
    @param x Absolute symbol X, mm.
    @param y Absolute symbol Y, mm (Y up).
    @return (x, y) that the wire's placement maps back onto the input,
        rounded to 4 decimals: the coordinates a stored point (a free vertex
        or an anchor's fallback) must carry.
    """
    px, py = x - float(wire_instance.get("x", 0.0)), y - float(wire_instance.get("y", 0.0))
    angle = -math.radians(int(wire_instance.get("rotation", 0)) % 360)
    c, s = round(math.cos(angle), 12), round(math.sin(angle), 12)
    px, py = px * c - py * s, px * s + py * c
    if bool(wire_instance.get("mirror", False)):
        px = -px
    return (round(px, 4), round(py, 4))


def resolve_wire_points(wire_instance: dict, graphics: Optional[Iterable[dict]],
                        pin_ends: Optional[dict], by_id: Optional[dict] = None) -> list[tuple[float, float]]:
    """@brief The vertices of a wire's polyline against the CURRENT geometry.

    @param wire_instance A placed record with key "wire" (its first polyline).
    @param graphics Every placed record (the wire itself may be among them);
        a terminal anchor is resolved on the element with that id, by index.
    @param pin_ends `pin_inner_ends(layout)`; a pin anchor is resolved by number.
    @param by_id `elements_by_id(graphics)` computed by the caller, to spare
        one pass per wire; None builds it from `graphics`.
    @return One (x, y) per stored point: the anchor's position when it
        resolves, else the stored x/y (the last resolved position).
    @note Fixed points and unresolved anchors go through the wire's own
        x/y/rotation/mirror like any primitive; a resolved anchor is absolute,
        the attachment wins over the wire's transform.
    """
    if by_id is None:
        by_id = elements_by_id(graphics)
    x, y = float(wire_instance.get("x", 0.0)), float(wire_instance.get("y", 0.0))
    rotation, mirror = int(wire_instance.get("rotation", 0)), bool(wire_instance.get("mirror", False))
    primitives = wire_instance.get("primitives") or [{}]
    result = []
    for point in primitives[0].get("points", []):
        anchor = _anchor_of(point)
        resolved = _resolve_anchor(anchor, by_id, pin_ends) if anchor else None
        if resolved is None:
            resolved = _transform(_point_xy(point), x, y, rotation, mirror)
        result.append((round(resolved[0], 4), round(resolved[1], 4)))
    return result


def placed_primitives(instance: dict, graphics: Optional[Iterable[dict]] = None,
                      pin_ends: Optional[dict] = None, by_id: Optional[dict] = None) -> list[Primitive]:
    """@brief The primitives of a placed glyph in symbol coordinates (moved, mirrored, turned).

    @param instance Placed record.
    @param graphics Every placed record, for a wire whose anchored points
        must follow their elements; None resolves nothing (stored x/y).
    @param pin_ends `pin_inner_ends(layout)` for pin anchors; None resolves none.
    @param by_id `elements_by_id(graphics)` precomputed by the caller (optional).
    @return Primitives with absolute points; a wire's polyline uses
        `resolve_wire_points` when graphics or pin_ends are given.
    """
    x, y = float(instance.get("x", 0.0)), float(instance.get("y", 0.0))
    rotation, mirror = int(instance.get("rotation", 0)), bool(instance.get("mirror", False))
    result = []
    for position, p in enumerate(instance.get("primitives", [])):
        if position == 0 and is_wire(instance) and (graphics is not None or pin_ends is not None):
            points = tuple(resolve_wire_points(instance, graphics, pin_ends, by_id))
        else:
            points = tuple(_transform(_point_xy(pt), x, y, rotation, mirror) for pt in p.get("points", []))
        result.append(Primitive(str(p.get("kind", "polyline")), points, float(p.get("radius", 0.0)),
                                str(p.get("fill", "none")), float(p.get("width", 0.0))))
    return result


def _freeze_anchors(graphics: list, matches, pin_ends: Optional[dict]) -> None:
    """Replace, in every wire, each anchored point `matches(anchor)` with its resolved
    position as a free `[x, y]` in the wire's own frame (a free point goes
    through the wire's placement, so the absolute position is unchanged)."""
    by_id = elements_by_id(graphics)
    for instance in graphics:
        if not is_wire(instance):
            continue
        primitives = instance.get("primitives") or []
        if not primitives:
            continue
        points = primitives[0].get("points", [])
        resolved = resolve_wire_points(instance, graphics, pin_ends, by_id)
        for index, point in enumerate(points):
            anchor = _anchor_of(point)
            if anchor is not None and matches(anchor):
                points[index] = list(wire_local_point(instance, *resolved[index]))


def refresh_wire_anchors(graphics: list, pin_ends: Optional[dict]) -> None:
    """@brief Write into every anchored wire point the position its anchor resolves to now.

    @param graphics Every placed record; the wires are mutated in place, the
        anchors are kept.
    @param pin_ends `pin_inner_ends(layout)` of the CURRENT layout.
    @note The stored x/y of an anchored point is the fallback used when the
        anchor no longer resolves (pin removed by a new pinout, element gone,
        project reopened in another editor without the previous layout).
        Called on every committed draft, it keeps the documented contract
        «fallback = last resolved position» true. Stored in the wire's own
        frame, like a free point. An anchor that does not resolve now is left
        as it is: its fallback is already the last known position.
    """
    by_id = elements_by_id(graphics)
    for instance in graphics:
        if not is_wire(instance):
            continue
        primitives = instance.get("primitives") or []
        if not primitives:
            continue
        for point in primitives[0].get("points", []):
            anchor = _anchor_of(point)
            if anchor is None:
                continue
            resolved = _resolve_anchor(anchor, by_id, pin_ends)
            if resolved is None:
                continue
            point["x"], point["y"] = wire_local_point(instance, resolved[0], resolved[1])


def anchor_direction(anchor: Optional[dict], graphics: Optional[Iterable[dict]], layout) -> Optional[int]:
    """@brief The direction (angle towards the body) of the pin or terminal an anchor names.

    @param anchor `{"kind": "pin", "number"}` or `{"kind": "terminal", "element", "index"}`.
    @param graphics Placed records, for a terminal anchor (angles turned with the element).
    @param layout `symbol.SymbolLayout` for a pin anchor; None when there is no symbol.
    @return 0, 90, 180 or 270 (0 right, 90 up, 180 left, 270 down, the
        convention of `Terminal.angle` and of the layout's pins), None when
        the anchor is None or does not resolve. The wire tool uses it to make
        the last segment enter the attachment along the pin's own axis.
    """
    if not isinstance(anchor, dict):
        return None
    kind = anchor.get("kind")
    if kind == "pin":
        number = str(anchor.get("number"))
        for item in getattr(layout, "pins", None) or ():
            if str(item.pin.number) == number:
                return int(item.angle) % 360
        return None
    if kind == "terminal":
        element = elements_by_id(graphics).get(anchor.get("element"))
        if element is None:
            return None
        try:
            index = int(anchor.get("index", -1))
        except (TypeError, ValueError):
            return None
        terminals = placed_terminals(element)
        if not 0 <= index < len(terminals):
            return None
        return int(terminals[index].angle) % 360
    return None


def freeze_wire_anchors(graphics: list, removed_id: str, pin_ends: Optional[dict]) -> None:
    """@brief Before an element is deleted, fix every wire point anchored to it where it is now.

    @param graphics Every placed record, the element still present; mutated in place.
    @param removed_id The `id` of the element about to be removed.
    @param pin_ends `pin_inner_ends(layout)` (other anchors are left untouched).
    """
    _freeze_anchors(graphics, lambda a: a.get("kind") == "terminal" and a.get("element") == removed_id, pin_ends)


def freeze_pin_anchors(graphics: list, pin_number: str, pin_ends: Optional[dict]) -> None:
    """@brief Before a pin disappears, fix every wire point anchored to it where it is now.

    @param graphics Every placed record; mutated in place.
    @param pin_number Number of the pin about to be removed.
    @param pin_ends `pin_inner_ends(layout)` of the layout that still has the pin.
    """
    _freeze_anchors(graphics, lambda a: a.get("kind") == "pin" and str(a.get("number")) == str(pin_number),
                    pin_ends)


def placed_terminals(instance: dict) -> list[Terminal]:
    """@brief The terminals of a placed glyph in symbol coordinates, angles turned with it."""
    x, y = float(instance.get("x", 0.0)), float(instance.get("y", 0.0))
    rotation, mirror = int(instance.get("rotation", 0)), bool(instance.get("mirror", False))
    result = []
    for t in instance.get("terminals", []):
        tx_, ty_ = _transform((float(t["x"]), float(t["y"])), x, y, rotation, mirror)
        angle = int(t.get("angle", 0))
        if mirror:
            angle = (180 - angle) % 360
        angle = (angle + rotation) % 360
        result.append(Terminal(tx_, ty_, angle, float(t.get("length", 2.54)),
                               str(t.get("name", "")), str(t.get("number", ""))))
    return result


def arc_points(start, mid, end, segments: int = 16) -> list[tuple[float, float]]:
    """@brief Sample a KiCad three-point arc as a polyline, for drawing on screen.

    @param start Start point (x, y).
    @param mid A point on the arc between the ends.
    @param end End point.
    @param segments Number of segments.
    @return `segments + 1` points from start to end through mid; the three
        points when they are collinear (degenerate arc: a straight line).
    """
    (x1, y1), (x2, y2), (x3, y3) = start, mid, end
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-9:
        return [tuple(start), tuple(mid), tuple(end)]
    ux = ((x1 ** 2 + y1 ** 2) * (y2 - y3) + (x2 ** 2 + y2 ** 2) * (y3 - y1) + (x3 ** 2 + y3 ** 2) * (y1 - y2)) / d
    uy = ((x1 ** 2 + y1 ** 2) * (x3 - x2) + (x2 ** 2 + y2 ** 2) * (x1 - x3) + (x3 ** 2 + y3 ** 2) * (x2 - x1)) / d
    radius = math.hypot(x1 - ux, y1 - uy)
    a1, a2, a3 = (math.atan2(y - uy, x - ux) for x, y in ((x1, y1), (x2, y2), (x3, y3)))
    # Sweep from a1 to a3 in the direction that passes through a2.
    def forward(a, b):
        return (b - a) % (2 * math.pi)
    sweep = forward(a1, a3)
    if forward(a1, a2) > sweep:
        sweep -= 2 * math.pi
    return [(round(ux + radius * math.cos(a1 + sweep * i / segments), 4),
             round(uy + radius * math.sin(a1 + sweep * i / segments), 4)) for i in range(segments + 1)]


def snap_to_terminal(instances: Iterable[dict], x: float, y: float) -> Optional[Terminal]:
    """@brief The terminal within TERMINAL_SNAP_MM of a point, nearest first, or None."""
    best, best_distance = None, TERMINAL_SNAP_MM
    for instance in instances:
        for terminal in placed_terminals(instance):
            distance = math.hypot(terminal.x - x, terminal.y - y)
            if distance <= best_distance:
                best, best_distance = terminal, distance
    return best


def validate_instances(instances) -> list[dict]:
    """@brief Check placed glyph records from a project file; a malformed record is a ValueError.

    @param instances The `symbol_graphics` value of a row (None = none).
    @return A list of the same records (unknown keys kept), every checked field typed.
    @throws ValueError When a record is not an object with finite numbers, an
        `id` is not text, a polyline point is neither `[x, y]` nor
        `{"x", "y", "anchor"?}` with finite numbers, or an anchor is not
        `{"kind": "terminal", "element": str, "index": int, "end"?: "outer"|"inner"}` /
        `{"kind": "pin", "number": str}`.
    """
    if instances is None:
        return []
    if not isinstance(instances, (list, tuple)):
        raise ValueError(tx("Elementi grafici del simbolo non validi."))
    result = []
    for record in instances:
        if not isinstance(record, dict):
            raise ValueError(tx("Elementi grafici del simbolo non validi."))
        for key in ("x", "y"):
            _finite_or_raise(record.get(key, 0.0))
        if "id" in record and not isinstance(record["id"], str):
            raise ValueError(tx("Elementi grafici del simbolo non validi."))
        if int(record.get("rotation", 0)) % 90 != 0:
            raise ValueError(tx("Elementi grafici del simbolo non validi."))
        primitives = record.get("primitives")
        if not isinstance(primitives, list) or not primitives:
            raise ValueError(tx("Elementi grafici del simbolo non validi."))
        for primitive in primitives:
            if not isinstance(primitive, dict):
                raise ValueError(tx("Elementi grafici del simbolo non validi."))
            for point in primitive.get("points", ()):
                _validate_point(point)
        result.append(record)
    return result


def _finite_or_raise(value) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(tx("Elementi grafici del simbolo non validi."))


def _validate_point(point) -> None:
    """A polyline point: `[x, y]` or `{"x", "y"}` plus an optional well-formed anchor."""
    if isinstance(point, dict):
        _finite_or_raise(point.get("x", 0.0))
        _finite_or_raise(point.get("y", 0.0))
        anchor = point.get("anchor")
        if anchor is None:
            return
        if not isinstance(anchor, dict):
            raise ValueError(tx("Elementi grafici del simbolo non validi."))
        kind = anchor.get("kind")
        if kind == "terminal":
            index = anchor.get("index")
            if not isinstance(anchor.get("element"), str) or isinstance(index, bool) \
                    or not isinstance(index, int) or index < 0                     or anchor.get("end", "inner") not in TERMINAL_ENDS:
                raise ValueError(tx("Elementi grafici del simbolo non validi."))
        elif kind == "pin":
            if not isinstance(anchor.get("number"), str):
                raise ValueError(tx("Elementi grafici del simbolo non validi."))
        else:
            raise ValueError(tx("Elementi grafici del simbolo non validi."))
        return
    if not isinstance(point, (list, tuple)) or len(point) < 2:
        raise ValueError(tx("Elementi grafici del simbolo non validi."))
    _finite_or_raise(point[0])
    _finite_or_raise(point[1])


# ── Serialisation ──────────────────────────────────────────────────────────

def _num(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".") or "0"


def serialize_graphics(instances: Iterable[dict], indent: str = "      ",
                       pin_ends: Optional[dict] = None) -> list[str]:
    """@brief The `.kicad_sym` lines of every placed glyph, for the symbol's graphic unit.

    @param instances Placed glyph records (`instance_from_glyph`) and wires.
    @param indent Indentation of the first level.
    @param pin_ends `pin_inner_ends(layout)` so wire points anchored to pins
        are exported where the pins are; terminal anchors are resolved on
        the records themselves.
    @return Lines of polyline/circle/arc/rectangle forms, KiCad 6+ syntax,
        every wire with resolved coordinates.
    """
    instances = list(instances)
    lines = []
    for instance in instances:
        for p in placed_primitives(instance, graphics=instances, pin_ends=pin_ends):
            stroke = f"(stroke (width {_num(p.width)}) (type default))"
            fill = f"(fill (type {p.fill}))"
            if p.kind == "polyline":
                pts = " ".join(f"(xy {_num(x)} {_num(y)})" for x, y in p.points)
                lines.append(f"{indent}(polyline (pts {pts}) {stroke} {fill})")
            elif p.kind == "circle":
                (cx, cy), = p.points
                lines.append(f"{indent}(circle (center {_num(cx)} {_num(cy)}) (radius {_num(p.radius)}) "
                             f"{stroke} {fill})")
            elif p.kind == "arc":
                (sx, sy), (mx, my), (ex, ey) = p.points
                lines.append(f"{indent}(arc (start {_num(sx)} {_num(sy)}) (mid {_num(mx)} {_num(my)}) "
                             f"(end {_num(ex)} {_num(ey)}) {stroke} {fill})")
            elif p.kind == "rectangle":
                (x0, y0), (x1, y1) = p.points
                lines.append(f"{indent}(rectangle (start {_num(x0)} {_num(y0)}) (end {_num(x1)} {_num(y1)}) "
                             f"{stroke} {fill})")
    return lines


def symbol_graphics(rows) -> list[dict]:
    """@brief The placed glyphs carried by the symbol rows (every row holds the same list).

    @param rows Symbol rows of the project.
    @return The validated list of the first row that carries `symbol_graphics`,
        else []; every record has an `id` (`ensure_instance_ids`).
    @throws ValueError When rows disagree or a record is malformed.
    @note Ids assigned to a legacy record are written positionally into
        every row's list, so the rows keep agreeing; they persist only when
        the caller saves the rows back.
    """
    lists = [row["symbol_graphics"] for row in rows if isinstance(row, dict) and "symbol_graphics" in row]
    if not lists:
        return []
    if any(entry != lists[0] for entry in lists):
        raise ValueError(tx("Elementi grafici del simbolo incoerenti fra i pin."))
    result = ensure_instance_ids(validate_instances(lists[0]))
    for entry in lists[1:]:
        for record, source in zip(entry, result):
            if isinstance(record, dict) and record.get("id") != source["id"]:
                record["id"] = source["id"]
    return result
