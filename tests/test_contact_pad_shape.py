"""@brief Un contatto half_and_through e' UNA piazzola di rame continua.

Decisione dell'utente del 2026-09-23, guardando la foto ad alta risoluzione di
una scheda di sviluppo reale accanto alla nostra vista 3D («i pin castellation
sono orrendi, guarda la realta'!!!»): sulla scheda vera ogni contatto e' una
sola piazzola dorata allungata (a stadio), perpendicolare al bordo, che parte
dal bordo -- dove si apre il semi-foro metallizzato -- e prosegue fino oltre il
foro passante, senza scheda scoperta in mezzo. Noi disegnavamo una «U» dorata
sul bordo e, piu' dentro, una corona separata con del nero fra le due.

Qui si verifica, per STRUTTURA e non per impronta:

* footprint: un solo pad per contatto, a stadio, che contiene il segmento fra
  i due centri dei fori e l'intera corona, con il foro decentrato (drill offset);
* 3D: la stessa piazzola su entrambe le facce contiene i due centri, il foro
  passante ha la sua canna ma non piu' corone sovrapposte alla piazzola;
* le regole dichiarate (larghezza, lunghezza minima, foro lontano, contatti
  che si toccherebbero), ognuna con un caso positivo e uno negativo;
* che half_hole e through_hole restino byte per byte quelli di prima.
"""

import ast
import copy
import hashlib
import json
import math
import re
from pathlib import Path

import pytest

from kicadgen.core.footprint import FpLine, FpRect, Pad
from kicadgen.generators.castellated_module import (
    CONTACT_HALF_AND_THROUGH, CONTACT_HALF_HOLE, CONTACT_THROUGH_HOLE,
    CastellatedModuleParams, CastellatedSide, OffsetDrillPad, build_castellated_module,
    continuous_pad, continuous_pads, pad_positions,
)
from kicadgen.cad3d import step_generator
from test_contact_style import _uuidless, dev_kit, four_sided, rs485

SCRIPT = Path(__file__).resolve().parents[1] / "kicadgen" / "cad3d" / "freecad_build_body.py"
PLATING, PAD_T, Z1 = 0.06, 0.025, 1.0


# ── Aiuti geometrici indipendenti dal generatore ────────────────────

def _copper_centre(pad):
    """Centro del RAME di un pad, secondo il modello KiCad.

    CONTRATTO CORRETTO il 2026-09-25 (verificato con pcbnew 10): in KiCad `at`
    e' la posizione del FORO (GetPosition) e `(drill D (offset dx dy))` sposta
    il RAME, centrato in at + offset (ShapePos). Questi aiuti leggevano il
    contrario (rame in `at`, foro in at + offset), cioe' lo stesso errore del
    generatore, e per questo non potevano accorgersene; ora lo verifica anche
    tests/test_fixG_kicad_roundtrip.py con il pcbnew vero.
    """
    offset = getattr(pad, "drill_offset", (0.0, 0.0))
    return (pad.at[0] + offset[0], pad.at[1] + offset[1])


def _stadium_of(pad):
    """Asse (A, B) e raggio del rame di un pad KiCad "oval", dai soli campi del pad.

    Non usa ContactPad: il controllo deve reggere anche se il helper sbagliasse.
    """
    (x, y), (w, h) = _copper_centre(pad), pad.size
    r = min(w, h) / 2
    half = max(w, h) / 2 - r
    if w >= h:
        return ((x - half, y), (x + half, y)), r
    return ((x, y - half), (x, y + half)), r


def _distance_to_segment(p, a, b):
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    t = 0.0 if length_sq == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def _in_pad(pad, point, slack=1e-6):
    (a, b), r = _stadium_of(pad)
    return _distance_to_segment(point, a, b) <= r + slack


def _drill_centre(pad):
    # KiCad: il foro sta SEMPRE in `at`, con o senza offset (vedi _copper_centre).
    return pad.at


def _half_hole_centre(params, placement):
    inward = {"left": (1, 0), "right": (-1, 0), "top": (0, 1), "bottom": (0, -1)}[placement.edge]
    if placement.edge in ("left", "right"):
        edge = (math.copysign(params.body_length / 2, -inward[0]), placement.y)
    else:
        edge = (placement.x, math.copysign(params.body_width / 2, -inward[1]))
    return (edge[0] + inward[0] * params.half_hole_inset, edge[1] + inward[1] * params.half_hole_inset)


def _double(**changes):
    """Modulo a due file di contatti doppi su un corpo largo: spazio per i casi limite."""
    params = CastellatedModuleParams(
        name="Double", body_length=40, body_width=20,
        sides=[CastellatedSide(edge="left", count=3, pitch=2.54),
               CastellatedSide(edge="right", count=3, pitch=2.54, first_pin=4)])
    for key, value in changes.items():
        setattr(params, key, value)
    return params


CASES = {"rs485": rs485, "four": four_sided,
         "dev_kit": lambda: dev_kit(CONTACT_HALF_AND_THROUGH), "double": _double}


# ── Footprint: un pad continuo per contatto ─────────────────────────

@pytest.mark.parametrize("name", sorted(CASES))
def test_each_contact_is_one_plated_stadium_pad(name):
    params = CASES[name]()
    fp = build_castellated_module(params)
    placements = list(pad_positions(params))
    assert len(fp.pads) == len(placements)
    assert sorted(p.number for p in fp.pads) == sorted(p.number for p in placements)
    for pad in fp.pads:
        assert pad.pad_type == "thru_hole" and pad.shape == "oval"
        assert pad.drill == params.thru_hole_drill
        assert list(pad.layers) == ["*.Cu", "*.Mask", "F.Paste"]


@pytest.mark.parametrize("name", sorted(CASES))
def test_copper_is_continuous_from_the_half_hole_to_beyond_the_ring(name):
    """@brief Il cuore della richiesta: niente scheda scoperta fra i due fori.

    Il pad contiene ogni punto del segmento fra il centro del semi-foro e il
    centro del foro passante, e l'intera corona dichiarata intorno al foro.
    """
    params = CASES[name]()
    fp = build_castellated_module(params)
    pads = {p.number: p for p in fp.pads}
    for placement in pad_positions(params):
        pad = pads[placement.number]
        hole = _drill_centre(pad)
        assert hole == pytest.approx(params.thru_hole_centre(placement))
        half = _half_hole_centre(params, placement)
        for i in range(21):
            t = i / 20
            point = (half[0] + t * (hole[0] - half[0]), half[1] + t * (hole[1] - half[1]))
            assert _in_pad(pad, point), (placement.number, point)
        ring = params.thru_pad_diameter / 2
        for k in range(36):
            angle = 2 * math.pi * k / 36
            assert _in_pad(pad, (hole[0] + ring * math.cos(angle), hole[1] + ring * math.sin(angle)))


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_pad_starts_where_the_edge_land_started_and_ends_past_the_ring(name):
    """@brief Quote dai parametri: sbalzo esterno invariato, fine oltre la corona."""
    params = CASES[name]()
    for placement in pad_positions(params):
        land = continuous_pad(params, placement)
        vertical = placement.edge in ("left", "right")
        edge_land_length = placement.width if vertical else placement.height
        assert land.outer_reach == params.pad_overhang
        assert land.inner_reach == pytest.approx(max(
            edge_land_length - params.pad_overhang,
            params.thru_hole_centre_offset + params.thru_pad_diameter / 2,
            land.across - params.pad_overhang, land.across / 2))
        assert land.across == max(placement.height if vertical else placement.width,
                                  params.thru_pad_diameter)
        # Il lato esterno coincide con quello della vecchia piazzola di bordo.
        outer_old = (abs(placement.x) + placement.width / 2 if vertical
                     else abs(placement.y) + placement.height / 2)
        outer_new = (abs(land.x) + land.width / 2 if vertical else abs(land.y) + land.height / 2)
        assert outer_new == pytest.approx(outer_old)


def test_contrast_with_the_through_hole_style_is_structural():
    """@brief Negativo: un through_hole ha il bordo intatto, niente rame fino al bordo.

    Stesse quote di foro e corona, stessa distanza dal bordo: cambia solo il
    tipo dichiarato, e con esso la presenza del rame fra bordo e corona.
    """
    double = _double(thru_hole_offset=3.0)
    through = _double(thru_hole_offset=3.0)
    for side in through.sides:
        side.contact_style = CONTACT_THROUGH_HOLE
    edge_point = (-double.body_length / 2 + 0.05, -2.54)
    double_pad = next(p for p in build_castellated_module(double).pads if p.number == "1")
    through_pad = next(p for p in build_castellated_module(through).pads if p.number == "1")
    assert _drill_centre(double_pad) == pytest.approx(_drill_centre(through_pad))
    assert _in_pad(double_pad, edge_point)
    assert through_pad.shape == "circle"
    assert math.dist(edge_point, through_pad.at) > through.thru_pad_diameter / 2


def test_pad_sexpression_is_a_single_oval_with_an_offset_drill():
    """@brief La forma KiCad: `(pad N thru_hole oval ... (drill D (offset dx dy)))`."""
    params = four_sided()
    text = _uuidless(build_castellated_module(params))
    assert text.count("(") == text.count(")")
    blocks = re.findall(r'\(pad "(\d+)" thru_hole oval\n(.*?)\n\t\)', text, re.S)
    assert len(blocks) == 12 and len({n for n, _ in blocks}) == 12
    placement = next(pad_positions(params))
    land = continuous_pad(params, placement)
    dx, dy = land.drill_offset
    # KiCad: `at` = foro, offset = centro del rame - foro (contratto 2026-09-25).
    assert (dx, dy) == pytest.approx((land.x - land.drill_x, land.y - land.drill_y))
    assert f"(at {land.drill_x:g} {land.drill_y:g})" in blocks[0][1]
    assert f"(drill 1\n\t\t\t(offset {dx:g} {dy:g})\n\t\t)" in blocks[0][1]
    assert '(layers "*.Cu" "*.Mask" "F.Paste")' in blocks[0][1]
    assert "pad_prop_castellated" not in text
    assert " smd " not in text


def test_offset_drill_pad_writes_the_offset_only_when_there_is_one():
    centred = OffsetDrillPad(number="1", pad_type="thru_hole", shape="oval", at=(0, 0),
                             size=(3, 1.7), layers=["*.Cu"], drill=1.0)
    plain = Pad(number="1", pad_type="thru_hole", shape="oval", at=(0, 0),
                size=(3, 1.7), layers=["*.Cu"], drill=1.0)
    strip = lambda pad: re.sub(r'\(uuid "[^"]*"\)', "", pad.to_sexpr().render())
    assert strip(centred) == strip(plain)
    moved = OffsetDrillPad(number="1", pad_type="thru_hole", shape="oval", at=(0, 0),
                           size=(3, 1.7), layers=["*.Cu"], drill=1.0, drill_offset=(-0.0, 0.65))
    assert "(offset 0 0.65)" in moved.to_sexpr().render()


def test_pin_one_marker_and_courtyard_follow_the_copper():
    """@brief Silkscreen fuori dal rame; courtyard che contiene ogni pad."""
    params = rs485()   # piazzola di bordo 1.2 mm, corona 1.7: lo stadio e' largo 1.7
    fp = build_castellated_module(params)
    marker = next(g for g in fp.graphics if getattr(g, "fill", False) and g.layer == "F.SilkS")
    pad = next(p for p in fp.pads if p.number == "1")
    (a, b), r = _stadium_of(pad)
    # Centro del marcatore a 0.5 mm dal bordo del RAME (non della vecchia
    # piazzola di bordo, 0.25 mm piu' stretta per lato): resta 0.25 mm d'aria.
    assert _distance_to_segment(marker.center, a, b) - r == pytest.approx(0.5)
    assert _distance_to_segment(marker.center, a, b) - r - marker.radius == pytest.approx(0.25)
    court = next(g for g in fp.graphics if getattr(g, "layer", "") == "F.CrtYd")
    for p in fp.pads:
        cx, cy = _copper_centre(p)
        assert court.start[0] <= cx - p.size[0] / 2 and cx + p.size[0] / 2 <= court.end[0]
        assert court.start[1] <= cy - p.size[1] / 2 and cy + p.size[1] / 2 <= court.end[1]


# ── Regole dichiarate: positivi e negativi ──────────────────────────

def test_ring_overlapping_the_half_hole_is_still_one_shorter_pad():
    """@brief Positivo: fori vicini (parete minima) -> un pad solo, piu' corto.

    Con i valori predefiniti la corona (da 1.346 mm) si sovrappone gia' al
    semi-foro (fino a 1.496 mm dal bordo): i FORI restano distinti, il rame e'
    uno e il pad e' semplicemente piu' corto di quello con il foro lontano.
    """
    near = _double()
    far = _double(thru_hole_offset=4.0)
    near_land = next(continuous_pads(near))
    far_land = next(continuous_pads(far))
    ring_start = near.thru_hole_centre_offset - near.thru_pad_diameter / 2
    assert ring_start < near.half_hole_inset + near.hole_diameter / 2
    near.validate()
    assert len(build_castellated_module(near).pads) == 6
    assert near_land.length < far_land.length


def test_a_far_through_hole_still_gives_one_continuous_pad():
    """@brief Regola dichiarata: per quanto lontano stia il foro, il rame e' continuo.

    E' cio' che il tipo half_and_through dichiara fisicamente; il pad si
    allunga fino alla corona. Il limite e' solo fisico (test seguente).
    """
    params = _double(thru_hole_offset=8.0)
    params.validate()
    for pad in build_castellated_module(params).pads:
        (a, b), r = _stadium_of(pad)
        assert max(pad.size) == pytest.approx(params.pad_overhang + 8.0 + params.thru_pad_diameter / 2)
        edge_x = math.copysign(params.body_length / 2, pad.at[0])
        hole = _drill_centre(pad)
        for t in (0.0, 0.25, 0.5, 0.75, 1.0):
            assert _in_pad(pad, (edge_x + t * (hole[0] - edge_x), hole[1]))


def test_a_pad_that_would_reach_the_facing_row_is_refused():
    """@brief Negativo: due pin diversi non possono condividere il rame.

    Il controllo storico sui fori opposti (2 * offset + corona <= corpo) passa;
    e' la piazzola di bordo lunga, ora parte del rame continuo, a toccare la
    fila di fronte. Accorciandola lo stesso modulo e' accettato.
    """
    params = _double(body_length=12.0, pad_length=7.5, pad_overhang=1.0)
    assert 2 * params.thru_hole_centre_offset + params.thru_pad_diameter <= 12.0
    with pytest.raises(ValueError, match="in corto"):
        params.validate()
    params.pad_length = 3.6
    params.validate()


def test_a_pad_that_would_reach_a_perpendicular_side_near_a_corner_is_refused():
    params = CastellatedModuleParams(
        name="Corner", body_length=12, body_width=12, thru_hole_offset=4.0,
        sides=[CastellatedSide(edge="left", count=1, pitch=0, offset=-2.5),
               CastellatedSide(edge="top", count=1, pitch=0, offset=-2.5, first_pin=2)])
    with pytest.raises(ValueError, match="in corto"):
        params.validate()
    # Negativo del negativo: stessi contatti con il solo semi-foro, nessun
    # rame che entra nel corpo -> nessun corto, il modulo e' accettato.
    for side in params.sides:
        side.contact_style = CONTACT_HALF_HOLE
    params.thru_hole_offset = 0.0
    params.validate()


def test_the_pitch_must_leave_room_for_the_ring_inside_the_pad():
    """@brief La corona e' parte della piazzola: il passo si misura su di lei.

    Pad di bordo 1.0 mm, corona 1.7 mm, passo 1.5 mm: prima accettato (con due
    corone vicine sovrapposte), ora rifiutato. Lo stesso passo con il solo
    semi-foro, che una corona non ce l'ha, resta valido.
    """
    params = _double(pad_width=1.0)
    for side in params.sides:
        side.pitch = 1.5
    with pytest.raises(ValueError, match="piazzola continua 1.7mm"):
        params.validate()
    for side in params.sides:
        side.contact_style = CONTACT_HALF_HOLE
    params.validate()


def test_a_stadium_is_never_shorter_than_it_is_wide():
    """@brief Vincolo geometrico: altrimenti KiCad girerebbe le estremita' tonde lungo il bordo."""
    params = _double(pad_width=2.4, pad_length=0.5, pad_overhang=0.0,
                     hole_diameter=0.6, thru_hole_drill=0.5, thru_pad_diameter=1.0)
    for side in params.sides:
        side.pitch = 2.6
    land = next(continuous_pads(params))
    assert land.across == 2.4
    assert land.length == pytest.approx(2.4)          # allungato verso l'interno
    assert land.outer_reach == 0.0                     # lo sbalzo non si sposta
    pad = next(p for p in build_castellated_module(params).pads if p.number == "1")
    assert pad.size == (pytest.approx(2.4), pytest.approx(2.4))


def test_without_a_declared_through_hole_the_edge_land_is_kept():
    """@brief Negativo: diametro zero = «solo semi-fori», nessuna piazzola continua."""
    params = rs485()
    params.thru_hole_drill = 0
    assert list(continuous_pads(params)) == []
    assert all(p.pad_type == "smd" and p.shape == "roundrect"
               for p in build_castellated_module(params).pads)


# ── Le altre forme restano quelle di prima, byte per byte ───────────

#: Calcolati PRIMA della piazzola continua (2026-09-23): footprint senza uuid,
#: payload 3D completo, specifiche FreeCAD dei contatti (tolta la chiave
#: `stadiums`, che per questi tipi deve essere vuota).
UNCHANGED = {
    "rs485_half": ("68b398ea85d13078729a4851d5b8da0b192ad989a24270e6f989af0f62e2bb58",
                   "37b3028b97a7d6ac897e51818d0569a3a32bc591c3cf89c6010d81ea30f64416",
                   "a1ae1dd7b034ffd728352774b07c5a99eeb74878b500f41a7f8a8f4de533536b"),
    "four_half": ("03d98fad773efa0771ec6058037719f2d78a4eeaa579eb68212f2b5ee860541e",
                  "f56032783776ebf1b0cc06c89cf3d330d724dd809e26a2829406a76cc4d0e25d",
                  "7b6b36b511151cd0868a68f34109511283296f3fb8d4ddf5c7a5b46a570f2f43"),
    "dev_through": ("0c3e9aa471b4a1cd21640263b6a8e147b01f097a4d0e7cba3f6dfcf787b9e508",
                    "2c853adbffecc9f6448c293dbb42cad9e6a29ca2715a2d01696879774fed0d74",
                    "541fc60733ce7c39987865c4289d0937118ed4e55d2b0f9f2830cdab5e5647c3"),
    "four_through": ("cb0b51c179e468db32e13c1d98227337e4b8534e49c1ad560e0d8c74571e7757",
                     "8ad269c78836ab8e13af53ff05848925e8b69103ffbf02b077bb9bde2cf0e0e0",
                     "e70d82826c1db32dbe648c0e83bf07b756c09cd322feadcbf8d0708a7fac0d30"),
    "rs485_nohole": ("68b398ea85d13078729a4851d5b8da0b192ad989a24270e6f989af0f62e2bb58",
                     "37b3028b97a7d6ac897e51818d0569a3a32bc591c3cf89c6010d81ea30f64416",
                     "a1ae1dd7b034ffd728352774b07c5a99eeb74878b500f41a7f8a8f4de533536b"),
    "rs485_through": ("5dc1789ff219e83442d5b8dd9d5d96e2024bd0e1fa5f767fd4b25ce0daf0c429",
                      "4810a71729f2a67768060c30a52bea814f8528d1335a472aed82d964c17ebeca",
                      "ae697d04ee6ef2765e6072246dc043b6193c41b033e501bcff073ec8e5cf9f85"),
}


def _with_legacy_silk(fp):
    """The same footprint with the body silk written as the pre-2026-09-25 FpRect.

    CONTRACT CHANGED 2026-09-25 (silk-to-pad clearance): the edge pads overhang
    the body, so the silk body outline crossed them; it is now split into
    FpLine pieces that keep clear of the copper. Folding those pieces back into
    the single rectangle they come from must give back EXACTLY the pinned
    output: that proves the silk clipping is the only change, while the pieces
    themselves are checked in tests/test_fixG_castellated.py.
    """
    fp = copy.deepcopy(fp)
    pieces = [g for g in fp.graphics if isinstance(g, FpLine) and g.layer == "F.SilkS"]
    if pieces:
        xs = [c for g in pieces for c in (g.start[0], g.end[0])]
        ys = [c for g in pieces for c in (g.start[1], g.end[1])]
        first = fp.graphics.index(pieces[0])
        fp.graphics = [g for g in fp.graphics if g not in pieces]
        fp.graphics.insert(first, FpRect(start=(min(xs), min(ys)), end=(max(xs), max(ys)),
                                         layer="F.SilkS"))
    return fp


def _unchanged_case(name):
    if name == "rs485_half":
        return rs485(CONTACT_HALF_HOLE)
    if name == "four_half":
        return four_sided(CONTACT_HALF_HOLE)
    if name == "dev_through":
        return dev_kit()
    if name == "four_through":
        params = four_sided(CONTACT_THROUGH_HOLE)
        params.thru_hole_offset = 2.5
        return params
    if name == "rs485_nohole":
        params = rs485()
        params.thru_hole_drill = 0
        return params
    params = rs485(CONTACT_THROUGH_HOLE)
    params.thru_hole_offset = 2.5
    return params


@pytest.fixture(scope="module")
def script():
    """Pure helpers of the FreeCAD script (the module itself needs FreeCAD)."""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    wanted = {"_through_features", "_contact_features", "_flip_y", "_swap_front_back",
              "_stadium_solid"}
    body = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in wanted)
            or (isinstance(n, ast.Assign)
                and any(getattr(t, "id", "") in ("_INWARD", "_FB_SWAP") for t in n.targets))]
    namespace = {}
    exec(compile(ast.Module(body=body, type_ignores=[]), "<pad-shape>", "exec"), namespace)
    return namespace


def _bounds(params):
    return (-params.body_length / 2, -params.body_width / 2,
            params.body_length / 2, params.body_width / 2, Z1)


@pytest.mark.parametrize("name", sorted(UNCHANGED))
def test_half_hole_and_through_hole_outputs_are_byte_identical(name, script):
    params = _unchanged_case(name)
    footprint, payload, features = UNCHANGED[name]
    rendered = _uuidless(_with_legacy_silk(build_castellated_module(params)))
    assert hashlib.sha256(rendered.encode()).hexdigest() == footprint
    described = step_generator.castellated_contact_payload(params, "#d4af37")
    assert hashlib.sha256(json.dumps(described, sort_keys=True).encode()).hexdigest() == payload
    specs = [script["_contact_features"](copy.deepcopy(c), _bounds(params), PLATING, PAD_T)
             for c in described[1]]
    assert all(spec.pop("stadiums") == [] for spec in specs)
    assert hashlib.sha256(json.dumps(specs, sort_keys=True).encode()).hexdigest() == features


# ── 3D: la stessa piazzola, su entrambe le facce ───────────────────

def _in_d_shape(spec, point, slack=1e-6):
    """Point inside a stadium spec whose outer half-disc is cut by the board edge."""
    rel = (point[0] - spec["x"], point[1] - spec["y"])
    along = rel[0] * spec["nx"] + rel[1] * spec["ny"]
    if along < -slack or along > spec["length"] + slack:
        return False
    end = (spec["x"] + spec["nx"] * (spec["length"] - spec["r"]),
           spec["y"] + spec["ny"] * (spec["length"] - spec["r"]))
    return _distance_to_segment(point, (spec["x"], spec["y"]), end) <= spec["r"] + slack


@pytest.mark.parametrize("name", sorted(CASES))
def test_3d_gold_pad_contains_both_hole_centres_on_both_faces(name, script):
    params = CASES[name]()
    _, contacts, _, _, _ = step_generator.castellated_contact_payload(params, "#d4af37")
    for contact, placement in zip(contacts, pad_positions(params)):
        land = continuous_pad(params, placement)
        assert contact["stadium"] == {"length_mm": land.inner_reach, "width_mm": land.across}
        features = script["_contact_features"](copy.deepcopy(contact), _bounds(params), PLATING, PAD_T)
        through = contact["through_hole"]
        assert features["boxes"] == []
        assert sorted((s["z0"], s["z1"]) for s in features["stadiums"]) == [
            (-PAD_T, 0.0), (Z1, Z1 + PAD_T)]
        for spec in features["stadiums"]:
            assert _in_d_shape(spec, (contact["x"], contact["y"]))          # semi-foro
            assert _in_d_shape(spec, (through["x"], through["y"]))          # foro passante
            # Tutta la corona dichiarata e' sotto il rame continuo.
            ring = through["pad_diameter_mm"] / 2
            for k in range(24):
                a = 2 * math.pi * k / 24
                assert _in_d_shape(spec, (through["x"] + ring * math.cos(a),
                                          through["y"] + ring * math.sin(a)))
        # Le bocche dei due fori restano aperte nel rame.
        assert (contact["x"], contact["y"], contact["radius"]) in features["bores"]
        assert (through["x"], through["y"], through["radius"]) in features["bores"]


def test_3d_has_barrels_but_no_face_rings_coplanar_with_the_pad(script):
    """@brief Niente facce coincidenti: le corone starebbero dentro la piazzola.

    Positivo e negativo sulla stessa canna: con la piazzola continua il foro
    ha la sola canna; un contatto through_hole, che la piazzola non ce l'ha,
    conserva le sue corone su entrambe le facce.
    """
    params = _double()
    _, contacts, _, _, _ = step_generator.castellated_contact_payload(params, "#d4af37")
    features = script["_contact_features"](contacts[0], _bounds(params), PLATING, PAD_T)
    through = contacts[0]["through_hole"]
    on_faces = [t for t in features["tubes"] if t["z0"] >= Z1 or t["z1"] <= 0.0]
    assert on_faces == []
    barrels = [t for t in features["tubes"] if (t["x"], t["y"]) == (through["x"], through["y"])]
    assert len(barrels) == 1 and (barrels[0]["z0"], barrels[0]["z1"]) == (0.0, Z1)
    # La canna del semi-foro sul bordo resta, ritagliata sul contorno.
    assert any(t["clip"] and (t["x"], t["y"]) == (contacts[0]["x"], contacts[0]["y"])
               for t in features["tubes"])
    ring_contact = {"x": 0.0, "y": 0.0, "radius": 0.5, "number": "1", "edge": "through",
                    "board_edge": "left", "pad_width_mm": 1.7}
    ring_features = script["_contact_features"](ring_contact, _bounds(params), PLATING, PAD_T)
    assert len([t for t in ring_features["tubes"] if t["z0"] >= Z1 or t["z1"] <= 0.0]) == 2
    assert ring_features["stadiums"] == []


def test_3d_pad_follows_the_flip_to_model_coordinates(script):
    """@brief Dopo `_flip_y` (Y verso l'alto) la piazzola parte ancora dal bordo giusto."""
    params = dev_kit(CONTACT_HALF_AND_THROUGH)
    _, contacts, _, _, _ = step_generator.castellated_contact_payload(params, "#d4af37")
    data = {"body": {"offset_y_mm": -params.body_width / 2, "width_mm": params.body_width},
            "edge_contacts": copy.deepcopy(contacts)}
    flipped = script["_flip_y"](data)["edge_contacts"]
    for contact in flipped:
        features = script["_contact_features"](contact, _bounds(params), PLATING, PAD_T)
        through = contact["through_hole"]
        for spec in features["stadiums"]:
            assert abs(spec["y"]) == pytest.approx(params.body_width / 2)
            assert _in_d_shape(spec, (through["x"], through["y"]))
            assert _in_d_shape(spec, (contact["x"], contact["y"]))


def test_3d_label_sits_beyond_the_continuous_pad(script):
    params = _double(thru_hole_offset=5.0)
    _, contacts, _, _, _ = step_generator.castellated_contact_payload(params, "#d4af37")
    features = script["_contact_features"](contacts[0], _bounds(params), PLATING, PAD_T)
    assert features["label"][0] > -params.body_length / 2 + contacts[0]["stadium"]["length_mm"]


class _Vec(tuple):
    def __new__(cls, x, y, z):
        return super().__new__(cls, (x, y, z))


class _FakePart:
    """Records the outline _stadium_solid draws, instead of building it."""

    class LineSegment:
        def __init__(self, a, b):
            self.a, self.b = a, b

        def toShape(self):
            return ("line", self.a, self.b)

    class Arc:
        def __init__(self, a, m, b):
            self.a, self.m, self.b = a, m, b

        def toShape(self):
            return ("arc", self.a, self.m, self.b)

    @staticmethod
    def Wire(edges):
        return list(edges)

    class Face:
        def __init__(self, wire):
            self.wire = wire

        def extrude(self, vector):
            return {"outline": self.wire, "extrusion": vector}


def test_the_freecad_outline_is_a_stadium_cut_by_the_board_edge(script):
    """@brief Il contorno disegnato: lato dritto sul bordo, estremita' tonda dentro."""
    namespace = dict(script)
    namespace["Part"] = _FakePart
    namespace["FreeCAD"] = type("FreeCAD", (), {"Vector": _Vec})
    exec(compile(ast.Module(body=[n for n in ast.parse(SCRIPT.read_text(encoding="utf-8")).body
                                  if isinstance(n, ast.FunctionDef) and n.name == "_stadium_solid"],
                            type_ignores=[]), "<stadium>", "exec"), namespace)
    spec = dict(x=-20.0, y=-2.54, nx=1.0, ny=0.0, length=3.046, r=0.9, z0=Z1, z1=Z1 + PAD_T)
    solid = namespace["_stadium_solid"](spec)
    kinds = [edge[0] for edge in solid["outline"]]
    assert kinds == ["line", "arc", "line", "line"]
    arc = solid["outline"][1]
    assert arc[2] == pytest.approx((-20.0 + 3.046, -2.54, Z1))            # punta interna
    closing = solid["outline"][3]
    assert closing[1][0] == closing[2][0] == -20.0                         # sul bordo
    assert abs(closing[1][1] - closing[2][1]) == pytest.approx(1.8)
    assert solid["extrusion"] == (0, 0, pytest.approx(PAD_T))


# ── Maschera dei contatti sulla foto ────────────────────────────────

def test_the_photo_mask_of_a_double_contact_covers_its_continuous_pad():
    """@brief Positivo: la striscia di un half_and_through OSSERVATO arriva oltre la corona.

    Negativi strutturali nello stesso test: il solo semi-foro, che una corona
    non ce l'ha, e il tipo non dichiarato, che a valle diventa half_and_through
    ma resta un'ipotesi (sulla foto non si maschera un'ipotesi), conservano la
    striscia della piazzola di bordo.
    """
    from kicadgen.services.contact_zones import contact_positions, contact_proposals
    from test_contact_style import BOARD, _declared, _proposal
    defaults = CastellatedModuleParams(thru_hole_drill=1.0, thru_pad_diameter=1.7)
    ring_end = defaults.thru_hole_centre_offset + defaults.thru_pad_diameter / 2
    x = contact_positions(_declared("through_hole"), *BOARD)[0]["x_mm"]
    beyond_ring = _proposal(x, -12.7 + ring_end + 0.4, 1.0, 0.7)   # sigla oltre il rame
    positions = contact_positions(_declared(CONTACT_HALF_AND_THROUGH), *BOARD)
    assert positions[0]["depth_mm"] == pytest.approx(ring_end + 0.5 + 0.55)
    assert contact_proposals([beyond_ring], positions, *BOARD) == {0}
    for style in (CONTACT_HALF_HOLE, None):
        positions = contact_positions(_declared(style), *BOARD)
        assert positions[0]["depth_mm"] == pytest.approx(1.0 + 0.5 + 0.55)
        assert contact_proposals([beyond_ring], positions, *BOARD) == set()
