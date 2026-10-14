"""Test del generatore di moduli con piazzole a mezzo foro (castellated).

Il caso di riferimento e' un convertitore RS485/TTL isolato da
42.8 x 15.2 mm: 3 contatti a passo 5.08mm sul lato sinistro (PE, B-, A+)
e 5 a passo 2.54mm sul lato destro. E' proprio il componente che il
generatore a matrice non puo' rappresentare, perche' i due lati hanno
passi diversi.
"""

import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from kicadgen.generators.castellated_module import (
    CastellatedModuleParams, CastellatedSide, build_castellated_module,
    pad_positions,
)


def rs485_module() -> CastellatedModuleParams:
    return CastellatedModuleParams(
        name="RS485_TTL_Isolated_42x15",
        body_length=42.8,
        body_width=15.2,
        pad_length=1.8,
        pad_width=1.2,
        pad_overhang=0.5,
        sides=[
            CastellatedSide(edge="left", count=3, pitch=5.08, first_pin=1),
            CastellatedSide(edge="right", count=5, pitch=2.54, first_pin=4),
        ],
    )


def test_two_sides_keep_their_own_pitch():
    """Il punto di tutto il generatore: passi diversi sui due lati."""
    placements = {p.number: p for p in pad_positions(rs485_module())}

    assert len(placements) == 8
    # Lato sinistro: 3 pad a 5.08mm, centrati sul bordo.
    assert placements["1"].y == pytest.approx(-5.08)
    assert placements["2"].y == pytest.approx(0.0)
    assert placements["3"].y == pytest.approx(5.08)
    # Lato destro: 5 pad a 2.54mm, indipendenti dal passo di sinistra.
    assert placements["4"].y == pytest.approx(-5.08)
    assert placements["5"].y == pytest.approx(-2.54)
    assert placements["8"].y == pytest.approx(5.08)


def test_pads_sit_on_the_body_edges_and_overhang_outward():
    params = rs485_module()
    placements = {p.number: p for p in pad_positions(params)}

    # Bordo esterno del pad = bordo del corpo + overhang, sui due lati.
    left = placements["1"]
    assert left.x - left.width / 2 == pytest.approx(-42.8 / 2 - 0.5)
    right = placements["4"]
    assert right.x + right.width / 2 == pytest.approx(42.8 / 2 + 0.5)


def test_pads_are_oriented_along_their_edge():
    params = CastellatedModuleParams(
        body_length=20, body_width=10, pad_length=1.8, pad_width=1.0,
        sides=[
            CastellatedSide(edge="left", count=2, pitch=2.54, first_pin=1),
            CastellatedSide(edge="top", count=2, pitch=2.54, first_pin=3),
        ],
    )
    placements = {p.number: p for p in pad_positions(params)}
    # Lato verticale: il lato lungo del pad e' orizzontale (perpendicolare al bordo).
    assert (placements["1"].width, placements["1"].height) == (1.8, 1.0)
    # Lato orizzontale: le dimensioni si scambiano.
    assert (placements["3"].width, placements["3"].height) == (1.0, 1.8)
    assert placements["3"].y < 0  # "top" e' a Y negativo, come in KiCad


def test_reverse_numbering_along_the_edge():
    side = CastellatedSide(edge="right", count=3, pitch=2.54, first_pin=4, reverse=True)
    assert side.numbers() == ["6", "5", "4"]


def test_explicit_pin_numbers_win():
    side = CastellatedSide(
        edge="left", count=3, pitch=5.08, pin_numbers=["PE", "B-", "A+"]
    )
    assert side.numbers() == ["PE", "B-", "A+"]


def test_offset_shifts_the_group_along_the_edge():
    params = CastellatedModuleParams(
        body_length=20, body_width=10,
        sides=[CastellatedSide(edge="left", count=2, pitch=2.54, offset=1.0)],
    )
    ys = [p.y for p in pad_positions(params)]
    assert ys == pytest.approx([-1.27 + 1.0, 1.27 + 1.0])


# ── Validazione ─────────────────────────────────────────────────────

def test_overlapping_pads_are_rejected():
    params = CastellatedModuleParams(
        body_length=20, body_width=10, pad_width=2.0,
        sides=[CastellatedSide(edge="left", count=2, pitch=1.0)],
    )
    with pytest.raises(ValueError, match="sovrapporrebbero"):
        params.validate()


def test_pads_longer_than_the_edge_are_rejected():
    params = CastellatedModuleParams(
        body_length=20, body_width=10,
        sides=[CastellatedSide(edge="left", count=10, pitch=2.54)],
    )
    with pytest.raises(ValueError, match="bordo e' lungo"):
        params.validate()


def test_duplicate_pin_numbers_across_sides_are_rejected():
    params = CastellatedModuleParams(
        body_length=20, body_width=10,
        sides=[
            CastellatedSide(edge="left", count=2, pitch=2.54, first_pin=1),
            CastellatedSide(edge="right", count=2, pitch=2.54, first_pin=2),
        ],
    )
    with pytest.raises(ValueError, match="compare sia sul lato"):
        params.validate()


def test_unknown_edge_is_rejected():
    params = CastellatedModuleParams(
        body_length=20, body_width=10,
        sides=[CastellatedSide(edge="diagonale", count=2, pitch=2.54)],
    )
    with pytest.raises(ValueError, match="Lato non valido"):
        params.validate()


# ── Footprint ───────────────────────────────────────────────────────

def test_every_contact_gets_a_half_hole_and_a_closed_hole():
    """@brief Due appigli per contatto, come sulle schede vere, su UN solo rame.

    Il mezzo foro sul bordo serve alla rifusione, il foro intero poco piu'
    dentro a saldarci uno strip. Generando solo il primo il bordo restava tutto
    aperto e i pin non si potevano saldare.

    Contratto CAMBIATO DI PROPOSITO il 2026-09-23 (decisione dell'utente sulla
    foto di una scheda reale): prima erano due pad con lo stesso numero, uno
    SMD rettangolare di bordo e uno THT tondo, con scheda nuda fra i due. Ora
    e' UN pad per contatto, a stadio ("oval"), dal bordo fino oltre la corona,
    con il foro decentrato al suo interno e la pasta per la rifusione.
    """
    params = rs485_module()
    fp = build_castellated_module(params)
    assert fp.attr == "smd"
    assert len(fp.pads) == 8
    assert sorted(p.number for p in fp.pads) == sorted(p.number for p in pad_positions(params))
    for pad in fp.pads:
        assert pad.pad_type == "thru_hole" and pad.shape == "oval" and pad.drill == 1.0
        assert list(pad.layers) == ["*.Cu", "*.Mask", "F.Paste"]
        # La corona dichiarata (1.7 mm) sta tutta dentro: lo stadio e' largo
        # almeno quanto lei, anche se la piazzola di bordo e' 1.2 mm.
        assert min(pad.size) == pytest.approx(1.7)
        assert pad.drill_offset != (0.0, 0.0)


def test_a_module_with_only_half_holes_declares_it_with_a_zero():
    """Caso negativo: non tutti i moduli hanno il foro passante.

    Lo si dice mettendo a zero il diametro -- un foro di diametro nullo non
    esiste, quindi lo zero non puo' essere scambiato per una quota.
    """
    params = rs485_module()
    params.thru_hole_drill = 0
    fp = build_castellated_module(params)
    assert len(fp.pads) == 8
    assert all(pad.pad_type == "smd" and pad.drill is None for pad in fp.pads)


def test_the_closed_hole_stays_inside_the_body_on_every_edge():
    """Un foro il cui bordo esce dal corpo sarebbe un secondo mezzo foro."""
    params = rs485_module()
    fp = build_castellated_module(params)
    half_l, half_w = params.body_length / 2, params.body_width / 2
    ring = params.thru_pad_diameter / 2
    for pad in (p for p in fp.pads if p.pad_type == "thru_hole"):
        # Il centro del FORO: dal 2026-09-23 il pad e' la piazzola continua e
        # il foro vi sta decentrato. CONTRATTO CORRETTO il 2026-09-25: in KiCad
        # il foro e' `at` e l'offset sposta il RAME (verificato con pcbnew 10);
        # prima qui si leggeva at + offset, lo stesso scambio del generatore.
        x, y = pad.at
        assert abs(x) + ring <= half_l + 1e-6
        assert abs(y) + ring <= half_w + 1e-6


def test_footprint_writes_valid_file(tmp_path):
    fp = build_castellated_module(rs485_module())
    # CONTRACT CHANGED 2026-10-14 (L-4): Footprint.save refuses a file not
    # named <footprint name>.kicad_mod, the name KiCad resolves it by.
    target = tmp_path / "mod.pretty" / f"{fp.name}.kicad_mod"
    fp.save(str(target))
    content = target.read_text(encoding="utf-8")
    assert content.startswith("(footprint")
    assert "(attr smd)" in content
    assert "F.CrtYd" in content
    # Il footprint e' ibrido: rifusione sui mezzi fori, strip nei fori interi.
    # Dal 2026-09-23 il foro sta decentrato nella piazzola continua, quindi il
    # nodo drill porta anche l'offset.
    assert "thru_hole oval" in content and "(drill 1" in content and "(offset " in content


def test_footprint_has_reference_on_fab_layer():
    """KLC F5.2: la serigrafia finisce coperta dal modulo montato, il
    riferimento su F.Fab resta leggibile nei disegni di assemblaggio."""
    fp = build_castellated_module(rs485_module())
    text_items = [g for g in fp.graphics if getattr(g, "kind", None) == "user"]
    assert any(t.text == "${REFERENCE}" and t.layer == "F.Fab" for t in text_items)


def test_footprint_prints_silk_labels_next_to_their_pad():
    params = rs485_module()
    params.silk_labels = {"1": "PE", "4": "GND"}
    fp = build_castellated_module(params)
    text_items = [g for g in fp.graphics if getattr(g, "kind", None) == "user"]
    labels = {t.text: t for t in text_items if t.text in ("PE", "GND")}
    assert set(labels) == {"PE", "GND"}
    # Pin 1 e' sul lato sinistro: l'etichetta sta oltre il bordo interno del
    # pad, verso il centro del modulo (x maggiore del centro del pad).
    pad1 = next(p for p in pad_positions(params) if p.number == "1")
    assert labels["PE"].at[0] > pad1.x
    assert labels["PE"].layer == "F.Fab"


def test_footprint_omits_label_for_pins_without_a_silk_label():
    """Senza silk_labels (nessuna sigla nota) non si stampa nulla di inventato."""
    fp = build_castellated_module(rs485_module())
    text_items = [g for g in fp.graphics if getattr(g, "kind", None) == "user"]
    assert not any(t.text not in ("${REFERENCE}",) for t in text_items)


# ── Componenti sul modulo (solo modello 3D) ─────────────────────────

def test_component_box_requires_a_real_volume():
    from kicadgen.generators.castellated_module import ComponentBox
    assert ComponentBox(x=0, y=0, length=2, width=2, height=1).is_usable()
    assert not ComponentBox(x=0, y=0, length=2, width=0, height=1).is_usable()


def test_components_do_not_affect_the_footprint():
    """I componenti servono al 3D: il footprint 2D non deve cambiare."""
    from kicadgen.generators.castellated_module import ComponentBox
    plain = build_castellated_module(rs485_module())
    with_components = rs485_module()
    with_components.components = [
        ComponentBox(x=0, y=0, length=9, width=6, height=4.5, name="trasformatore")
    ]
    populated = build_castellated_module(with_components)
    assert len(plain.pads) == len(populated.pads)
    assert [(p.number, p.at) for p in plain.pads] == [(p.number, p.at) for p in populated.pads]


def _notches_for(params, monkeypatch, tmp_path):
    """Cattura i cilindri che il 3D sottrae al PCB, senza lanciare FreeCAD."""
    from kicadgen.cad3d import step_generator
    from kicadgen.cad3d.body_params import Body3DParams
    captured = {}

    def fake_run(name, pins, body, output_dir, **options):
        captured.update(options)
        return {"step_path": tmp_path / "x.step", "warnings": [], "notes": []}

    monkeypatch.setattr(step_generator, "_run_freecad", fake_run)
    step_generator.generate_castellated_3d_model(
        params, Body3DParams(length_mm=params.body_length, width_mm=params.body_width,
                             height_mm=3.0), tmp_path)
    return captured["notches"]


def test_the_solid_gets_both_holes_for_every_contact(monkeypatch, tmp_path):
    """@brief Il bordo non e' tutto aperto: mezzo foro fuori, foro intero dentro.

    Un cilindro solo per contatto lasciava una U spalancata sul bordo, che non
    somiglia al pezzo reale. I due cilindri si distinguono per dove stanno, non
    per come si chiamano: quello del semi-foro tocca il bordo, l'altro no.
    """
    params = rs485_module()
    notches = _notches_for(params, monkeypatch, tmp_path)
    assert len(notches) == 2 * 8
    half_l, half_w = params.body_length / 2, params.body_width / 2
    def crosses_the_edge(n):
        return (abs(n["x"]) + n["radius"] > half_l + 1e-6
                or abs(n["y"]) + n["radius"] > half_w + 1e-6)

    touching = [n for n in notches if crosses_the_edge(n)]
    inside = [n for n in notches if not crosses_the_edge(n)]
    assert len(touching) == len(inside) == 8
    assert all(n["radius"] == params.thru_hole_drill / 2 for n in inside)
    for n in inside:
        assert abs(n["x"]) + n["radius"] < half_l + 1e-6
        assert abs(n["y"]) + n["radius"] < half_w + 1e-6


def test_a_module_without_thru_holes_keeps_one_cylinder_per_contact(monkeypatch, tmp_path):
    """Caso negativo: dichiarare zero fori passanti non ne aggiunge nessuno."""
    params = rs485_module()
    params.thru_hole_drill = 0
    assert len(_notches_for(params, monkeypatch, tmp_path)) == 8


def test_the_two_holes_cannot_merge_into_one_keyhole(monkeypatch, tmp_path):
    """@brief Due cilindri passanti restano distinti solo se gli assi distano
    piu' della somma dei raggi.

    Misurato sul solido generato prima di questa correzione: 44 pareti da
    164,7 gradi invece di 44 fori chiusi, perche' gli assi distavano 0,624 mm
    contro una somma dei raggi di 1,350. Non era un caso limite: con quei
    valori si fondono sempre, su qualunque scheda.
    """
    params = rs485_module()
    assert params.thru_hole_offset == 0, "il default si ricava, non e' un numero fisso"
    gap = (params.thru_hole_centre_offset - params.half_hole_inset
           - params.hole_diameter / 2 - params.thru_hole_drill / 2)
    assert gap >= 0.2 - 1e-9

    # Nessuna coppia di cilindri si tocca: e' l'invariante, e vale fra tutti,
    # non solo fra il semi-foro e il suo foro.
    notches = _notches_for(params, monkeypatch, tmp_path)
    assert len(notches) == 2 * 8
    for i, a in enumerate(notches):
        for b in notches[i + 1:]:
            distance = math.hypot(a["x"] - b["x"], a["y"] - b["y"])
            assert distance > a["radius"] + b["radius"], (a, b, distance)


@pytest.mark.parametrize("offset,expected", [
    (1.27, True),    # il valore che generava il buco di serratura
    (2.196, False),  # il minimo calcolato con i parametri predefiniti
    (3.0, False),
])
def test_an_explicit_offset_that_would_merge_is_refused(offset, expected):
    """Un numero scritto a mano viene rispettato, ma controllato."""
    params = rs485_module()
    params.body_width = 20.0
    params.thru_hole_offset = offset
    if expected:
        with pytest.raises(ValueError, match="si toccano"):
            params.validate()
    else:
        params.validate()


def test_a_smaller_half_hole_lets_the_closed_hole_come_closer():
    """Il minimo si RICAVA dal semi-foro scelto, non e' una quota fissa.

    E' la differenza fra una regola e un numero tarato: cambiando il semi-foro
    cambia da solo, e resta valido.
    """
    params = rs485_module()
    wide = params.thru_hole_centre_offset
    params.hole_diameter = 1.0
    narrow = params.thru_hole_centre_offset
    assert narrow < wide
    assert narrow == pytest.approx(params.half_hole_inset + 0.5 + 0.5 + 0.2, abs=1e-3)
