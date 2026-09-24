"""@brief Il tipo di contatto di un lato decide quali feature il pezzo offre.

Il difetto da cui nasce: una scheda di sviluppo a 2x22 contatti ha, per ogni
contatto, un foro passante intero con la sua corona dorata, arretrato dal bordo
e con la scheda intatta fra corona e bordo. Il generatore la disegnava sempre
come mezzo foro tagliato nel bordo piu' un foro chiuso, e quel foro chiuso nel
3D era un puntino scuro: veniva tagliato ma mai metallizzato.

Qui si verifica il contratto (`CastellatedSide.contact_style`, tre valori), che
il default riproduca byte per byte l'uscita precedente, e che ogni tipo produca
le piazzole e le feature 3D che il pezzo fisico offre davvero.
"""

import ast
import hashlib
import json
import math
import re
from pathlib import Path
from unittest.mock import Mock

import pytest

from kicadgen.generators.castellated_module import (
    CONTACT_HALF_AND_THROUGH, CONTACT_HALF_HOLE, CONTACT_STYLES, CONTACT_THROUGH_HOLE,
    EDGES, MIN_HOLE_WALL_MM, CastellatedModuleParams, CastellatedSide,
    build_castellated_module, continuous_pad, fallback_through_hole_inset, opposite_row_edges,
    SMD_WITH_DRILLED_PADS_NOTE, pad_positions, through_hole_inset_from_rows,
)
from kicadgen.core.footprint import FpCircle, FpText

SCRIPT = Path(__file__).resolve().parents[1] / "kicadgen" / "cad3d" / "freecad_build_body.py"


# ── Moduli di prova ─────────────────────────────────────────────────

def rs485(style=CONTACT_HALF_AND_THROUGH):
    """Due lati con passi diversi, sigle serigrafate: il caso storico."""
    return CastellatedModuleParams(
        name="RS485", body_length=42.8, body_width=15.2, pad_length=1.8, pad_width=1.2,
        pad_overhang=0.5,
        sides=[CastellatedSide(edge="left", count=3, pitch=5.08, first_pin=1, contact_style=style),
               CastellatedSide(edge="right", count=5, pitch=2.54, first_pin=4, contact_style=style)],
        silk_labels={"1": "PE", "4": "GND"}, show_silk_labels_3d=True)


def four_sided(style=CONTACT_HALF_AND_THROUGH):
    """Contatti su tutti e quattro i lati, meta' numerati al contrario."""
    return CastellatedModuleParams(
        name="Four", body_length=30, body_width=30,
        sides=[CastellatedSide(edge=e, count=3, pitch=2.54, first_pin=1 + 3 * i,
                               reverse=(i % 2 == 1), contact_style=style)
               for i, e in enumerate(EDGES)])


def dev_kit(style=CONTACT_THROUGH_HOLE, row_spacing=22.86, **changes):
    """Scheda 63.3x25.4 a 2x22 contatti a passo 2.54 su due lati opposti.

    Sono le quote registrate dalla corsa dell'utente (2x22, pitch 2.54,
    row_spacing 22.86 su 25.4 di larghezza); nessuna regola le cita.
    """
    params = CastellatedModuleParams(
        name="DevKit_2x22", body_length=63.3, body_width=25.4, row_spacing=row_spacing,
        sides=[CastellatedSide(edge="top", count=22, pitch=2.54, first_pin=1, contact_style=style),
               CastellatedSide(edge="bottom", count=22, pitch=2.54, first_pin=23,
                               reverse=True, contact_style=style)],
        silk_labels={"1": "3V3", "23": "GND"})
    for key, value in changes.items():
        setattr(params, key, value)
    return params


def _uuidless(fp):
    return re.sub(r'\(uuid "[^"]*"\)', "(uuid)", fp.render())


def _payload(params, monkeypatch, tmp_path, pin_color="#d4af37"):
    """What generate_castellated_3d_model hands to FreeCAD, without FreeCAD."""
    from kicadgen.cad3d import step_generator
    from kicadgen.cad3d.body_params import Body3DParams
    captured = {}

    def fake_run(name, pins, body, output_dir, **options):
        captured.update(options)
        return {"step_path": tmp_path / "x.step", "warnings": [], "notes": []}

    monkeypatch.setattr(step_generator, "_run_freecad", fake_run)
    result = step_generator.generate_castellated_3d_model(
        params, Body3DParams(length_mm=params.body_length, width_mm=params.body_width,
                             height_mm=3.0, pin_color_hex=pin_color), tmp_path)
    return captured, result


@pytest.fixture(scope="module")
def script():
    """Pure geometry helpers of the FreeCAD script: the module itself needs FreeCAD."""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    wanted = {"_through_features", "_contact_features", "_flip_y", "_swap_front_back",
              "_covered_by_drill"}
    body = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in wanted)
            or (isinstance(n, ast.Assign)
                and any(getattr(t, "id", "") in ("_INWARD", "_FB_SWAP") for t in n.targets))]
    assert len(body) == 7, [getattr(n, "name", n) for n in body]
    namespace = {}
    exec(compile(ast.Module(body=body, type_ignores=[]), "<contact>", "exec"), namespace)
    return namespace


# ── Contratto ───────────────────────────────────────────────────────

def test_the_contract_names_and_default_are_the_agreed_ones():
    assert (CONTACT_HALF_HOLE, CONTACT_THROUGH_HOLE, CONTACT_HALF_AND_THROUGH) == (
        "half_hole", "through_hole", "half_and_through")
    assert CONTACT_STYLES == (CONTACT_HALF_HOLE, CONTACT_THROUGH_HOLE, CONTACT_HALF_AND_THROUGH)
    assert CastellatedSide(edge="left", count=1, pitch=0).contact_style == CONTACT_HALF_AND_THROUGH


# ── Default: identico a prima, byte per byte ────────────────────────

#: sha256 del .kicad_mod (uuid esclusi) del tipo predefinito half_and_through.
#: CAMBIATO DI PROPOSITO il 2026-09-23 (decisione dell'utente, guardando la
#: foto ad alta risoluzione di una scheda reale: «i pin castellation sono
#: orrendi, guarda la realta'»): ogni contatto e' ora UNA piazzola di rame
#: continua a stadio, dal bordo fino oltre la corona del foro, con il foro
#: decentrato dentro (`(drill D (offset dx dy))`), invece di un pad SMD di
#: bordo piu' un pad THT tondo separato. Valori precedenti, per traccia:
#: rs485 47dcab5e..., four 78160c60... La forma nuova e' verificata per
#: struttura in tests/test_contact_pad_shape.py, non solo dall'impronta.
#: CAMBIATO DI NUOVO il 2026-09-24 (decisione dell'utente: il footprint resta
#: `smd` anche con le piazzole forate, e la ragione va scritta nella
#: descrizione): l'unica differenza e' SMD_WITH_DRILLED_PADS_NOTE in coda a
#: `descr`. Tolta quella frase, l'uscita coincide con i valori del 2026-09-23
#: (rs485 ab8e4527..., four 87c86d6b...), come prova
#: test_the_only_change_of_2026_09_24_is_the_smd_sentence.
PINNED_FOOTPRINT = {
    "rs485": "701474f9d7cafc34bb029b814e365a43f0ebfdd0472ac141b004d36018f503e8",
    "four": "cb4ba41dda8425847de81f9097c107c9730ba215ccebfd4124f848b6810ac894",
}
#: Impronte del 2026-09-23, prima della frase sul tipo SMD.
PINNED_FOOTPRINT_BEFORE_SMD_NOTE = {
    "rs485": "ab8e45275e6f5842bf4a0d79af3e1fdcb52bc7c47c472979a0f2d6a93913ad15",
    "four": "87c86d6b8cd8c540036c119f67cc0385b13507824800474696213dc29aef4002",
}
#: sha256 di notches/edge_contacts/extra_boxes/extra_payload, tolte le voci
#: `through_hole` e `stadium` dei contatti (verificate a parte, qui sotto).
#: "four" e' identico a prima della piazzola continua. "rs485" e' CAMBIATO DI
#: PROPOSITO il 2026-09-23 (stessa decisione) solo in due punti, entrambi
#: conseguenza del rame che ora arriva oltre la corona: le sigle serigrafate
#: 3D stanno oltre la fine della piazzola continua (x -18.7 -> -16.954) e la
#: cornice serigrafica rientra di conseguenza (lunghezza 36.0 -> 33.508).
#: Valore precedente, per traccia: bc59c263...
PINNED_PAYLOAD = {
    "rs485": "f7ad6446f577ccf0c0136a4b36d77ec1c12194e5913a523de979b1ac5258fb71",
    "four": "ba8a6bc6160f30c8aae2a63c798d55b6d34dbff70fc3120fd86bc79cd42ba44e",
}
BUILDERS = {"rs485": rs485, "four": four_sided}


@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_default_footprint_is_byte_identical_to_the_pinned_output(name):
    rendered = _uuidless(build_castellated_module(BUILDERS[name]()))
    assert hashlib.sha256(rendered.encode()).hexdigest() == PINNED_FOOTPRINT[name]


@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_the_only_change_of_2026_09_24_is_the_smd_sentence(name):
    """@brief Tolta la frase sul tipo SMD, l'impronta e' quella del 2026-09-23."""
    rendered = _uuidless(build_castellated_module(BUILDERS[name]()))
    assert rendered.count(SMD_WITH_DRILLED_PADS_NOTE) == 1
    stripped = rendered.replace(" " + SMD_WITH_DRILLED_PADS_NOTE, "")
    assert hashlib.sha256(stripped.encode()).hexdigest() == PINNED_FOOTPRINT_BEFORE_SMD_NOTE[name]


@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_default_3d_payload_is_the_pinned_one_plus_only_the_plated_ring(name, monkeypatch, tmp_path):
    """@brief L'unica differenza ammessa e' la correzione del puntino scuro.

    Il foro chiuso era tagliato (notch) ma nessun contatto lo descriveva, quindi
    FreeCAD non ci metteva ne' canna ne' corona. Ora ogni contatto porta la sua
    voce `through_hole`; tolta quella, il payload e' quello di prima.
    """
    params = BUILDERS[name]()
    captured, _ = _payload(params, monkeypatch, tmp_path, pin_color="#b0b0b0")
    rings = [c.pop("through_hole") for c in captured["edge_contacts"]]
    # La piazzola continua (2026-09-23): stesse quote di quella del footprint.
    stadiums = [c.pop("stadium") for c in captured["edge_contacts"]]
    pinned = {k: captured[k] for k in ("notches", "edge_contacts", "extra_boxes", "extra_payload")}
    assert hashlib.sha256(json.dumps(pinned, sort_keys=True).encode()).hexdigest() == PINNED_PAYLOAD[name]
    for ring, stadium, placement in zip(rings, stadiums, pad_positions(params)):
        assert (ring["x"], ring["y"]) == params.thru_hole_centre(placement)
        assert ring["radius"] == params.thru_hole_drill / 2
        assert ring["pad_diameter_mm"] == params.thru_pad_diameter
        land = continuous_pad(params, placement)
        assert stadium == {"length_mm": land.inner_reach, "width_mm": land.across}
    # Le sole differenze di rs485 rispetto a prima, ricavate dalla regola:
    # sigla oltre la fine del rame continuo, cornice che rientra quanto basta.
    if name == "rs485":
        first = continuous_pad(params, next(pad_positions(params)))
        label_gap = params.hole_diameter / 2 + 0.55
        pe = next(t for t in captured["extra_payload"]["silk_texts"] if t["text"] == "PE")
        assert pe["x"] == pytest.approx(-params.body_length / 2 + first.inner_reach + label_gap)
        assert captured["extra_payload"]["silk_outline"]["length_mm"] == pytest.approx(
            params.body_length - 2 * (first.inner_reach + 1.6))


# ── through_hole ────────────────────────────────────────────────────

def test_through_hole_footprint_has_only_tht_pads_at_the_derived_inset():
    params = dev_kit()
    fp = build_castellated_module(params)
    assert fp.attr == "through_hole"
    assert all(p.pad_type == "thru_hole" for p in fp.pads) and len(fp.pads) == 44
    for pad in fp.pads:
        assert pad.shape == "circle" and pad.drill == params.thru_hole_drill
        assert pad.size == (params.thru_pad_diameter, params.thru_pad_diameter)
        assert list(pad.layers) == ["*.Cu", "*.Mask"]
        # (25.4 - 22.86) / 2 = 1.27 mm dal bordo, su entrambe le file.
        assert abs(pad.at[1]) == pytest.approx(25.4 / 2 - 1.27)
    assert "a fori passanti" in fp.description


def test_through_hole_payload_keeps_the_edge_straight(monkeypatch, tmp_path):
    params = dev_kit()
    captured, result = _payload(params, monkeypatch, tmp_path)
    half_l, half_w = params.body_length / 2, params.body_width / 2
    for notch in captured["notches"]:
        # Nessun cilindro tocca il bordo: tutti chiusi, raggio del foro.
        assert abs(notch["x"]) + notch["radius"] < half_l
        assert abs(notch["y"]) + notch["radius"] < half_w
        assert notch["radius"] == params.thru_hole_drill / 2
        assert half_w - abs(notch["y"]) == pytest.approx(1.27)
    assert len(captured["notches"]) == len(captured["edge_contacts"]) == 44
    for contact in captured["edge_contacts"]:
        assert contact["edge"] == "through" and contact["board_edge"] in ("top", "bottom")
        assert contact["pad_width_mm"] == params.thru_pad_diameter
        assert contact["color_hex"] == "#d4af37"
        assert "through_hole" not in contact
    assert any("22.86" in note for note in result["notes"])


def test_through_hole_is_plated_with_a_barrel_and_a_gold_ring_on_both_faces(script):
    """@brief Com'e' nella foto: canna passante e corona su entrambe le facce."""
    z1, plating, pad_t = 1.0, 0.06, 0.025
    bounds = (-31.65, -12.7, 31.65, 12.7, z1)
    contact = {"x": 0.0, "y": -11.43, "radius": 0.5, "number": "1", "edge": "through",
               "board_edge": "top", "pad_width_mm": 1.7, "pad_length_mm": 1.7}
    features = script["_contact_features"](contact, bounds, plating, pad_t)
    assert features["boxes"] == []          # nessuna piazzola di bordo
    tubes = features["tubes"]
    barrel = [t for t in tubes if t["z0"] == 0.0 and t["z1"] == z1]
    top = [t for t in tubes if t["z0"] == z1]
    bottom = [t for t in tubes if t["z1"] == 0.0]
    assert len(barrel) == len(top) == len(bottom) == 1
    for ring in top + bottom:
        # Sopra la soldermask, non dentro: e con la bocca del foro aperta.
        assert ring["r_out"] == pytest.approx(0.85) and ring["r_in"] == pytest.approx(0.5)
    assert top[0]["z1"] == pytest.approx(z1 + pad_t) and bottom[0]["z0"] == pytest.approx(-pad_t)
    assert barrel[0]["r_in"] == pytest.approx(0.5)
    # La scheda va forata fino all'esterno della canna: niente facce coincidenti.
    assert features["drills"] == [(0.0, -11.43, 0.5 + plating)]
    # Il numero va verso l'interno oltre la corona, non sopra il foro.
    assert features["label"][1] > -11.43 + 0.85


def test_a_ring_not_wider_than_its_bore_is_refused_by_the_script(script):
    contact = {"x": 0, "y": 0, "radius": 0.5, "number": "1", "edge": "through", "pad_width_mm": 1.0}
    with pytest.raises(ValueError, match="exceed"):
        script["_contact_features"](contact, (-5, -5, 5, 5, 1.0), 0.06, 0.025)


def test_the_closed_hole_of_a_double_contact_is_now_plated_too(script, monkeypatch, tmp_path):
    """@brief La causa del puntino scuro, anche per il default.

    Prima il foro chiuso di un contatto half_and_through era solo sottratto al
    PCB: si vedeva il substrato scuro dentro un buco. Ora ha la sua canna, e il
    rame della faccia -- dal 2026-09-23 la piazzola continua a stadio, non piu'
    due corone separate (decisione dell'utente sulla foto della scheda reale)
    -- lo copre tutto intorno ed e' forato sulla sua bocca.
    """
    params = CastellatedModuleParams(
        name="M", body_length=30, body_width=20, pad_length=3.6, pad_overhang=1.4,
        sides=[CastellatedSide(edge="left", count=2, pitch=2.54)])
    captured, _ = _payload(params, monkeypatch, tmp_path)
    contact = captured["edge_contacts"][0]
    features = script["_contact_features"](contact, (-15, -10, 15, 10, 1.0), 0.06, 0.025)
    through = contact["through_hole"]
    barrels = [t for t in features["tubes"]
               if (t["x"], t["y"]) == (through["x"], through["y"])
               and (t["z0"], t["z1"]) == (0.0, 1.0)]
    assert len(barrels) == 1 and barrels[0]["r_in"] == pytest.approx(through["radius"])
    assert (through["x"], through["y"], through["radius"]) in features["bores"]
    # Il rame di faccia arriva oltre l'intera corona dichiarata (0.85 mm).
    assert len(features["stadiums"]) == 2
    for pad in features["stadiums"]:
        assert pad["x"] + pad["length"] >= through["x"] + 0.85 - 1e-9


def test_each_hole_is_cut_once_and_at_the_barrel_outer_radius(script):
    """@brief La foratura della canna sostituisce il taglio nominale, non si somma."""
    drills = [(1.0, 2.0, 0.56)]
    assert script["_covered_by_drill"]({"x": 1.0, "y": 2.0, "radius": 0.5}, drills) is True
    assert script["_covered_by_drill"]({"x": 1.0, "y": 2.5, "radius": 0.5}, drills) is False
    assert script["_covered_by_drill"]({"x": 1.0, "y": 2.0, "radius": 0.9}, drills) is False
    assert script["_covered_by_drill"]({"x": "bad"}, drills) is False


def test_flip_carries_the_plated_ring_and_the_board_edge_along(script):
    data = {"body": {"offset_y_mm": -10, "width_mm": 20},
            "edge_contacts": [
                {"x": -14, "y": 3, "edge": "top", "through_hole": {"x": -12, "y": 3}},
                {"x": 1, "y": -8.7, "edge": "through", "board_edge": "top"}]}
    flipped = script["_flip_y"](data)
    first, second = flipped["edge_contacts"]
    assert first["through_hole"]["y"] == -3 and first["y"] == -3 and first["edge"] == "bottom"
    assert second["board_edge"] == "bottom" and second["y"] == 8.7


# ── half_hole ───────────────────────────────────────────────────────

def test_half_hole_has_edge_pads_only(monkeypatch, tmp_path):
    params = rs485(CONTACT_HALF_HOLE)
    fp = build_castellated_module(params)
    assert fp.attr == "smd" and len(fp.pads) == 8
    assert all(p.pad_type == "smd" and p.drill is None for p in fp.pads)
    captured, _ = _payload(params, monkeypatch, tmp_path)
    half_l = params.body_length / 2
    assert len(captured["notches"]) == 8
    assert all(abs(n["x"]) + n["radius"] > half_l for n in captured["notches"])
    assert all("through_hole" not in c and c["edge"] in EDGES for c in captured["edge_contacts"])


def test_half_hole_features_have_no_ring(script, monkeypatch, tmp_path):
    captured, _ = _payload(rs485(CONTACT_HALF_HOLE), monkeypatch, tmp_path)
    features = script["_contact_features"](captured["edge_contacts"][0],
                                           (-21.4, -7.6, 21.4, 7.6, 1.0), 0.06, 0.025)
    assert len(features["tubes"]) == 1 and features["tubes"][0]["clip"] is True
    assert len(features["boxes"]) == 2


# ── Distanza dal bordo dei fori passanti ────────────────────────────

def test_inset_follows_from_declared_extent_and_row_spacing():
    assert through_hole_inset_from_rows(25.4, 22.86) == pytest.approx(1.27)
    params = dev_kit()
    assert params.through_hole_inset_source("top") == "row_spacing"
    assert params.through_hole_inset("bottom") == pytest.approx(1.27)
    # Stessa regola su un altro corpo: cambia da sola, non e' un numero fissato.
    other = dev_kit(row_spacing=15.24)
    other.body_width = 17.78
    assert other.through_hole_inset("top") == pytest.approx(1.27)
    wide = dev_kit(row_spacing=20.32)
    assert wide.through_hole_inset("top") == pytest.approx((25.4 - 20.32) / 2)


@pytest.mark.parametrize("extent,spacing", [
    (25.4, None), (None, 22.86), (25.4, 0), (25.4, -1), (25.4, 25.4), (25.4, 30),
    (25.4, float("nan")), (True, 22.86), (25.4, "x"),
])
def test_inset_is_not_derived_from_unusable_quantities(extent, spacing):
    assert through_hole_inset_from_rows(extent, spacing) is None


def test_explicit_offset_is_respected():
    params = dev_kit(thru_hole_offset=2.0)
    assert params.through_hole_inset_source("top") == "explicit"
    fp = build_castellated_module(params)
    assert all(abs(p.at[1]) == pytest.approx(12.7 - 2.0) for p in fp.pads)


def test_explicit_offset_that_opens_the_ring_on_the_edge_is_refused():
    """@brief Una corona che esce dal bordo e' un semi-foro, non un foro passante."""
    with pytest.raises(ValueError, match="semi-foro e non un foro passante"):
        dev_kit(thru_hole_offset=0.5).validate()


def test_row_spacing_that_would_open_the_ring_is_refused_with_its_reason():
    with pytest.raises(ValueError, match="le file dichiarate distano 24.4mm"):
        dev_kit(row_spacing=24.4).validate()


def test_declared_fallback_when_nothing_is_declared(monkeypatch, tmp_path):
    params = dev_kit(row_spacing=None)
    assert params.through_hole_inset_source("top") == "fallback"
    expected = params.thru_pad_diameter / 2 + MIN_HOLE_WALL_MM
    assert params.through_hole_inset("top") == pytest.approx(expected)
    assert fallback_through_hole_inset(params.thru_pad_diameter) == pytest.approx(expected)
    _, result = _payload(params, monkeypatch, tmp_path)
    assert any("stimata" in note and "non dichiarata" in note for note in result["notes"])


def test_a_row_spacing_is_not_attributed_when_it_cannot_be():
    """@brief Caso negativo strutturale: un solo numero, due assi possibili."""
    assert opposite_row_edges(["top", "bottom"]) == ("top", "bottom")
    assert opposite_row_edges(["left", "right", "top"]) == ("left", "right")
    assert opposite_row_edges(EDGES) is None            # quattro lati: ambiguo
    assert opposite_row_edges(["top"]) is None          # una fila: niente da misurare
    four = four_sided(CONTACT_THROUGH_HOLE)
    four.row_spacing = 27.46
    assert four.through_hole_inset_source("top") == "fallback"
    single = dev_kit()
    single.sides = single.sides[:1]
    assert single.through_hole_inset_source("top") == "fallback"


def test_row_spacing_does_not_move_the_hole_of_a_double_contact():
    """@brief La distanza fra le file vale per i lati through_hole soltanto."""
    plain = rs485()
    declared = rs485()
    declared.row_spacing = 38.0
    assert declared.thru_hole_centre_offset == plain.thru_hole_centre_offset
    assert _uuidless(build_castellated_module(declared)) == _uuidless(build_castellated_module(plain))


# ── Validazione ─────────────────────────────────────────────────────

def test_unknown_style_is_rejected_not_remapped():
    params = rs485()
    params.sides[1].contact_style = "castellated"
    with pytest.raises(ValueError, match="tipo di contatto 'castellated' non riconosciuto"):
        params.validate()


@pytest.mark.parametrize("pad", [1.0, 0.8])
def test_through_hole_side_needs_a_ring_wider_than_its_drill(pad):
    with pytest.raises(ValueError, match="piu' larga del foro"):
        dev_kit(thru_pad_diameter=pad).validate()


def test_through_hole_side_needs_a_drill():
    with pytest.raises(ValueError, match="diametro del foro e' zero"):
        dev_kit(thru_hole_drill=0).validate()


def test_half_hole_quantities_only_matter_where_half_holes_exist():
    """@brief Positivo e negativo sulla stessa quota, per ragioni strutturali."""
    dev_kit(hole_diameter=0).validate()                  # nessun semi-foro: irrilevante
    params = rs485(CONTACT_HALF_HOLE)
    params.hole_diameter = 0
    with pytest.raises(ValueError, match="semi-foro deve essere > 0"):
        params.validate()


def test_the_keyhole_check_applies_only_where_both_holes_exist():
    """@brief 1.27 mm fondeva foro e semi-foro; senza semi-foro non fonde nulla."""
    double = rs485()
    double.body_width = 20.0
    double.thru_hole_offset = 1.27
    with pytest.raises(ValueError, match="si toccano"):
        double.validate()
    through = rs485(CONTACT_THROUGH_HOLE)
    through.body_width = 20.0
    through.thru_hole_offset = 1.27
    through.validate()


def test_rings_that_would_overlap_along_the_edge_are_refused():
    with pytest.raises(ValueError, match="corone|corona"):
        dev_kit(thru_pad_diameter=2.8).validate()


# ── Numerazione, sigle e pin 1 non dipendono dal tipo ───────────────

def _pin_one_marker(fp):
    return [g for g in fp.graphics if isinstance(g, FpCircle) and g.layer == "F.SilkS" and g.fill]


@pytest.mark.parametrize("builder", [rs485, four_sided, dev_kit])
def test_numbers_names_and_pin_one_do_not_change_across_styles(builder):
    by_style = {}
    for style in CONTACT_STYLES:
        params = builder(style)
        if params.row_spacing is None and style == CONTACT_THROUGH_HOLE:
            params.thru_hole_offset = 2.5  # quattro lati: distanza esplicita
        fp = build_castellated_module(params)
        placements = list(pad_positions(params))
        markers = _pin_one_marker(fp)
        assert len(markers) == 1
        first = placements[0]
        # Il marcatore segue il rame che il tipo offre davvero: per il contatto
        # doppio, dal 2026-09-23, la piazzola continua, larga quanto la
        # maggiore fra piazzola di bordo e corona (qui conta per rs485, 1.2 vs
        # 1.7 mm). Resta a 0.5 mm dal bordo del RAME, come per gli altri tipi.
        land = continuous_pad(params, first)
        if land is not None:
            first = first._replace(**({"height": land.across} if first.edge in ("left", "right")
                                      else {"width": land.across}))
        along = (markers[0].center[1] if first.edge in ("left", "right") else markers[0].center[0])
        labels = sorted(g.text for g in fp.graphics if isinstance(g, FpText) and g.kind == "user")
        # Il marcatore sta accanto al pad del pin 1, lungo il bordo.
        nearest = min(fp.pads, key=lambda p: math.dist(p.at, markers[0].center))
        by_style[style] = dict(
            order=[p.number for p in placements],
            numbers=sorted({p.number for p in fp.pads}),
            labels=labels, first=first.number, nearest=nearest.number,
            # Stessa distanza dal BORDO della piazzola (0.5 mm), dallo stesso
            # lato: la piazzola cambia misura fra i tipi, il marcatore la segue.
            gap=round(abs(along - (first.y if first.edge in ("left", "right") else first.x))
                      - (first.height if first.edge in ("left", "right") else first.width) / 2, 6),
            side=math.copysign(1, along - (first.y if first.edge in ("left", "right") else first.x)),
            along_pad=round(first.y if first.edge in ("left", "right") else first.x, 6))
    reference = by_style[CONTACT_HALF_AND_THROUGH]
    for style, found in by_style.items():
        assert found == reference, style
        assert found["nearest"] == found["first"]


# ── Estrazione: si chiede al modello, non si indovina ───────────────

def test_the_schema_asks_the_model_for_the_style_from_the_photo():
    from kicadgen.services.datasheet_extractor import DEFAULT_SYSTEM_PROMPT, _openai_tool
    item = _openai_tool()["function"]["parameters"]["properties"]["castellated_sides"]["items"]
    field = item["properties"]["contact_style"]
    assert field["enum"] == list(CONTACT_STYLES)
    assert "FOTO" in field["description"] and "bordo" in field["description"]
    assert "contact_style" not in item["required"]
    assert "contact_style" in DEFAULT_SYSTEM_PROMPT


def test_a_declared_style_is_parsed():
    from kicadgen.services.datasheet_extractor import _castellated_sides, _unobserved_contact_style_notice
    sides = _castellated_sides([dict(edge="top", count=2, pitch_mm=2.54, contact_style="through_hole"),
                                dict(edge="bottom", count=2, pitch_mm=2.54, contact_style="half_hole")])
    assert [s["contact_style"] for s in sides] == ["through_hole", "half_hole"]
    assert _unobserved_contact_style_notice(sides) is None


def test_a_missing_style_keeps_the_default_and_says_so():
    from kicadgen.services.datasheet_extractor import _castellated_sides, _unobserved_contact_style_notice
    sides = _castellated_sides([dict(edge="top", count=2, pitch_mm=2.54),
                                dict(edge="bottom", count=2, pitch_mm=2.54, contact_style="half_hole")])
    # Assente resta assente: chi costruisce il modulo applica il default, e la
    # GUI continua a distinguere un tipo osservato da uno assunto.
    assert "contact_style" not in sides[0]
    notice = _unobserved_contact_style_notice(sides)
    assert "tipo di contatto non osservato: si assume" in notice
    assert "half_and_through" in notice and "superiore" in notice and "inferiore" not in notice


@pytest.mark.parametrize("bad", ["castellated", "HALF_HOLE", "", 3, True])
def test_an_invalid_style_from_the_model_is_rejected(bad):
    from kicadgen.services.datasheet_extractor import ExtractionError, _castellated_sides
    with pytest.raises(ExtractionError, match="non riconosciuto"):
        _castellated_sides([dict(edge="left", count=2, pitch_mm=2.54, contact_style=bad)])


def _run_extraction(monkeypatch, tmp_path, sides):
    from kicadgen.services import datasheet_extractor as ext
    source = tmp_path / "drawing.pdf"
    source.write_bytes(b"%PDF-1.4 offline")
    call = Mock(return_value=dict(layout="castellated", confidence="high", castellated_sides=sides))
    monkeypatch.setattr(ext, "api_key", lambda: "offline")
    monkeypatch.setattr(ext, "check_credit", lambda **kw: None)
    monkeypatch.setattr(ext, "call_tool", call)
    log = []
    return ext, call, log, lambda: ext.extract_pin_header_params([source], progress=log.append)


def test_extraction_logs_the_assumed_style(monkeypatch, tmp_path):
    ext, call, log, run = _run_extraction(monkeypatch, tmp_path,
                                          [dict(edge="top", count=2, pitch_mm=2.54)])
    result = run()
    assert call.call_count == 1
    assert any("tipo di contatto non osservato: si assume" in line for line in log)
    assert "tipo di contatto non osservato" in result.notes
    assert "contact_style" not in result.castellated_sides[0]


def test_extraction_with_observed_styles_logs_no_assumption(monkeypatch, tmp_path):
    ext, call, log, run = _run_extraction(monkeypatch, tmp_path,
                                          [dict(edge="top", count=2, pitch_mm=2.54,
                                                contact_style="through_hole")])
    result = run()
    assert result.castellated_sides[0]["contact_style"] == "through_hole"
    assert not any("non osservato" in line for line in log)


def test_extraction_refuses_an_invalid_style_before_any_further_call(monkeypatch, tmp_path):
    ext, call, log, run = _run_extraction(monkeypatch, tmp_path,
                                          [dict(edge="top", count=2, pitch_mm=2.54,
                                                contact_style="mezzo foro")])
    with pytest.raises(ext.ExtractionError, match="'mezzo foro' non riconosciuto"):
        run()
    assert call.call_count == 1


# ── Maschera dei contatti per l'inventario fotografico ──────────────

BOARD = (63.3, 25.4)


def _proposal(x_mm, y_mm, length_mm, width_mm):
    L, W = BOARD
    return {'_box': (x_mm/L + .5 - length_mm/2/L, y_mm/W + .5 - width_mm/2/W,
                     x_mm/L + .5 + length_mm/2/L, y_mm/W + .5 + width_mm/2/W)}


def _declared(style):
    side = dict(count=22, pitch_mm=2.54, pad_length_mm=1.0)
    if style:
        side["contact_style"] = style
    return dict(layout="castellated", row_spacing_mm=22.86, drill_mm=1.0, pad_diameter_mm=1.7,
                castellated_sides=[dict(side, edge="top"), dict(side, edge="bottom", first_pin=23)])


def test_the_through_hole_strip_covers_ring_and_label_and_nothing_inside():
    from kicadgen.services.contact_zones import contact_positions, contact_proposals, contact_rects_mm
    positions = contact_positions(_declared("through_hole"), *BOARD)
    depth = positions[0]["depth_mm"]
    # Web + corona + meta' foro + margine della sigla, dalle quote dichiarate.
    assert depth == pytest.approx(1.27 + 0.85 + 0.5 + 0.55)
    edge = -12.7
    x = positions[0]["x_mm"]
    ring = _proposal(x, edge + 1.27, 1.7, 1.7)
    label = _proposal(x, edge + 2.9, 1.0, 0.7)
    inside = _proposal(x, edge + depth + 0.9, 1.6, 0.8)   # un 0603 appena oltre la striscia
    assert contact_proposals([ring, label, inside], positions, *BOARD) == {0, 1}
    rect = contact_rects_mm(positions, *BOARD)[0]
    assert rect[3] - rect[1] == pytest.approx(depth)


def test_the_edge_pad_strip_does_not_reach_a_ring_label():
    """@brief Negativo: con la piazzola di bordo dichiarata corta, la sigla
    di una corona arretrata resterebbe fuori. E' la geometria del tipo a
    decidere la striscia, non un margine allargato per tutti.

    Dal 2026-09-23 un half_and_through OSSERVATO e' una piazzola continua fino
    oltre la corona, e la sua striscia arriva li' (caso positivo in
    tests/test_contact_pad_shape.py). Il tipo non dichiarato resta qui fra i
    negativi: a valle e' disegnato come half_and_through, ma sulla foto non si
    maschera un'ipotesi."""
    from kicadgen.services.contact_zones import contact_positions, contact_proposals
    x = contact_positions(_declared("through_hole"), *BOARD)[0]["x_mm"]
    label = _proposal(x, -12.7 + 2.9, 1.0, 0.7)
    for style in ("half_hole", None):
        positions = contact_positions(_declared(style), *BOARD)
        assert positions[0]["depth_mm"] == pytest.approx(1.0 + 0.5 + 0.55)
        assert contact_proposals([label], positions, *BOARD) == set()


@pytest.mark.parametrize("bad", ["22.86", float("nan"), -1, 0, True])
def test_an_unusable_row_spacing_is_refused_not_ignored(bad):
    """@brief Una distanza fra le file dichiarata ma illeggibile non diventa una stima."""
    with pytest.raises(ValueError, match="distanza fra le file"):
        dev_kit(row_spacing=bad).validate()
