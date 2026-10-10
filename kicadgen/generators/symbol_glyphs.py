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
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

from ..services.messages import tx

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


def instance_from_glyph(glyph: Glyph, x: float, y: float, rotation: int = 0, mirror: bool = False) -> dict:
    """@brief The project record of a placed glyph: self-contained, JSON-friendly.

    @param glyph Catalogue glyph.
    @param x Placement X of the glyph origin, symbol mm.
    @param y Placement Y, symbol mm (Y up).
    @param rotation Quarter turns in degrees (0, 90, 180, 270), counter-clockwise.
    @param mirror Mirror about the glyph's vertical axis, applied before the rotation.
    @return {key, label, source, x, y, rotation, mirror, primitives, terminals}
        with the UNTRANSFORMED primitives and terminals, so a move or a turn
        never accumulates rounding.
    """
    return {
        "key": glyph.key, "label": glyph.label, "source": glyph.source,
        "x": float(x), "y": float(y), "rotation": int(rotation) % 360, "mirror": bool(mirror),
        "primitives": [{"kind": p.kind, "points": [list(pt) for pt in p.points], "radius": p.radius,
                        "fill": p.fill, "width": p.width} for p in glyph.primitives],
        "terminals": [{"x": t.x, "y": t.y, "angle": t.angle, "length": t.length,
                       "name": t.name, "number": t.number} for t in glyph.terminals],
    }


def placed_primitives(instance: dict) -> list[Primitive]:
    """@brief The primitives of a placed glyph in symbol coordinates (moved, mirrored, turned)."""
    x, y = float(instance.get("x", 0.0)), float(instance.get("y", 0.0))
    rotation, mirror = int(instance.get("rotation", 0)), bool(instance.get("mirror", False))
    result = []
    for p in instance.get("primitives", []):
        points = tuple(_transform((float(pt[0]), float(pt[1])), x, y, rotation, mirror)
                       for pt in p.get("points", []))
        result.append(Primitive(str(p.get("kind", "polyline")), points, float(p.get("radius", 0.0)),
                                str(p.get("fill", "none")), float(p.get("width", 0.0))))
    return result


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
    @return A list of records with every field typed.
    @throws ValueError When a record is not an object with finite numbers.
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
            value = record.get(key, 0.0)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(tx("Elementi grafici del simbolo non validi."))
        if int(record.get("rotation", 0)) % 90 != 0:
            raise ValueError(tx("Elementi grafici del simbolo non validi."))
        primitives = record.get("primitives")
        if not isinstance(primitives, list) or not primitives:
            raise ValueError(tx("Elementi grafici del simbolo non validi."))
        result.append(record)
    return result


# ── Serialisation ──────────────────────────────────────────────────────────

def _num(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".") or "0"


def serialize_graphics(instances: Iterable[dict], indent: str = "      ") -> list[str]:
    """@brief The `.kicad_sym` lines of every placed glyph, for the symbol's graphic unit.

    @param instances Placed glyph records (`instance_from_glyph`).
    @param indent Indentation of the first level.
    @return Lines of polyline/circle/arc/rectangle forms, KiCad 6+ syntax.
    """
    lines = []
    for instance in instances:
        for p in placed_primitives(instance):
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
    @return The validated list of the first row that carries `symbol_graphics`, else [].
    @throws ValueError When rows disagree or a record is malformed.
    """
    lists = [row["symbol_graphics"] for row in rows if isinstance(row, dict) and "symbol_graphics" in row]
    if not lists:
        return []
    if any(entry != lists[0] for entry in lists):
        raise ValueError(tx("Elementi grafici del simbolo incoerenti fra i pin."))
    return validate_instances(lists[0])
