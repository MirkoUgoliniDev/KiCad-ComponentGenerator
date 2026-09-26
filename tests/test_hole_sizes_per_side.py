"""@brief Hole sizes per side, fitted to the pitch, and row spacing for every through-hole style.

Difetti confermati dal coordinatore (2026-09-26):

* (3) le quote dei fori del modulo (semi-foro 1.7, foro 1.0, corona 1.7) sono
  quelle del campione storico RS485 a passo 2.54, mai misurate: su un modulo a
  passo 1.27 validate() passava e il 3D tagliava semi-fori da 1.7 a 1.27 l'uno
  dall'altro, fusi in un'asola. Ora una quota DICHIARATA che non sta nel passo
  viene rifiutata, una ASSUNTA viene ridotta al massimo che ci sta e la stima
  finisce nelle note; ogni lato puo' dichiarare le sue.
* (8) la distanza fra le file dichiarata valeva solo per i lati through_hole:
  un half_and_through la ignorava in silenzio. Ora vale per ogni tipo con foro
  passante, con il controllo della parete verso il semi-foro.

Ogni regola ha un caso positivo e uno negativo che differiscono per una ragione
strutturale (quota dichiarata o assunta, passo, attribuibilita' delle file).
"""

import copy
import json
import math

import pytest

from kicadgen.generators.castellated_module import (
    CONTACT_HALF_AND_THROUGH, CONTACT_HALF_HOLE, CONTACT_THROUGH_HOLE, EDGES, MIN_HOLE_WALL_MM,
    REFERENCE_HALF_HOLE_MM, AssumedSize, CastellatedModuleParams, CastellatedSide,
    build_castellated_module, is_declared_size, pad_positions,
)
from kicadgen.cad3d import step_generator


# ── Moduli sintetici ───────────────────────────────────────────────

def fine_pitch(style=CONTACT_HALF_HOLE, pitch=1.27, count=8, **changes):
    """Quattro lati a passo fine, piazzole strette: nessun campione reale."""
    params = CastellatedModuleParams(
        name="Fine", body_length=20, body_width=20, pad_length=1.8, pad_width=0.9,
        pad_overhang=0.5,
        sides=[CastellatedSide(edge=edge, count=count, pitch=pitch, first_pin=1 + count * i,
                               contact_style=style)
               for i, edge in enumerate(EDGES)])
    for key, value in changes.items():
        setattr(params, key, value)
    return params


def two_rows(style=CONTACT_HALF_AND_THROUGH, row_spacing=22.86, **changes):
    """Due file opposte su 63.3x25.4 (le quote del difetto 8), passo 2.54."""
    params = CastellatedModuleParams(
        name="Rows", body_length=63.3, body_width=25.4, row_spacing=row_spacing,
        sides=[CastellatedSide(edge="top", count=22, pitch=2.54, first_pin=1, contact_style=style),
               CastellatedSide(edge="bottom", count=22, pitch=2.54, first_pin=23,
                               reverse=True, contact_style=style)])
    for key, value in changes.items():
        setattr(params, key, value)
    return params


def _walls(holes):
    """Minimum board web (mm) between drilled holes of different contacts."""
    walls = []
    for i, (a, _, xa, ya, ra) in enumerate(holes):
        for b, _, xb, yb, rb in holes[i + 1:]:
            if a.number != b.number:
                walls.append(math.hypot(xa - xb, ya - yb) - ra - rb)
    return min(walls)


def _notch_walls(notches):
    return min(math.hypot(a["x"] - b["x"], a["y"] - b["y"]) - a["radius"] - b["radius"]
               for i, a in enumerate(notches) for b in notches[i + 1:])


# ── Provenienza delle quote ─────────────────────────────────────────

def test_defaults_are_assumed_and_explicit_values_are_declared():
    assert not is_declared_size(CastellatedModuleParams().hole_diameter)
    assert not is_declared_size(CastellatedModuleParams(hole_diameter=None).hole_diameter)
    assert CastellatedModuleParams(hole_diameter=None).hole_diameter == 1.7
    assert is_declared_size(CastellatedModuleParams(hole_diameter=1.7).hole_diameter)
    params = fine_pitch()
    params.hole_diameter = 1.7                      # come fa la GUI con un campo
    assert is_declared_size(params.hole_diameter)


def test_an_assumed_size_behaves_as_its_float_and_keeps_its_type_when_copied():
    value = REFERENCE_HALF_HOLE_MM
    assert value / 2 == 0.85 and f"{value}" == "1.7" and json.dumps(value) == "1.7"
    assert isinstance(copy.deepcopy(CastellatedModuleParams()).hole_diameter, AssumedSize)


# ── (3) Passo fine: quote dichiarate, assunte, rifiutate ────────────

def test_small_declared_half_holes_on_a_fine_pitch_are_used_as_they_are():
    params = fine_pitch()
    for side in params.sides:
        side.hole_diameter = 0.6
    params.validate()
    assert params.geometry_notes() == []
    notches, contacts, _, notes = step_generator.castellated_contact_payload(params, "#d4af37")
    assert {n["radius"] for n in notches} == {0.3} and notes == []
    assert _notch_walls(notches) >= MIN_HOLE_WALL_MM - 1e-9


def test_the_assumed_half_hole_is_reduced_to_fit_the_pitch_and_declared():
    """@brief Positivo di (c): mai asola silenziosa, la stima e' scritta."""
    params = fine_pitch()
    params.validate()
    geometry = params.side_geometry(params.sides[0])
    assert geometry.half_hole_source == "estimate_pitch"
    assert geometry.half_hole == pytest.approx(1.27 - MIN_HOLE_WALL_MM)
    notes = params.geometry_notes()
    assert len(notes) == 4 and all("stimato 1.07mm" in note for note in notes)
    notches, _, _, payload_notes = step_generator.castellated_contact_payload(params, "#d4af37")
    assert payload_notes == notes
    assert _notch_walls(notches) >= MIN_HOLE_WALL_MM - 1e-9
    assert _walls(params.drilled_holes()) >= MIN_HOLE_WALL_MM - 1e-9


def test_a_declared_half_hole_too_large_for_the_pitch_is_refused_not_reduced():
    """@brief Negativo di (c): la stessa quota 1.7, ma dichiarata, e' un errore."""
    params = fine_pitch(hole_diameter=1.7)
    with pytest.raises(ValueError, match="asola"):
        params.validate()
    per_side = fine_pitch()
    per_side.sides[2].hole_diameter = 1.2          # 1.27 - 1.2 = 0.07 < 0.2
    with pytest.raises(ValueError, match="Lato superiore: semi-fori da 1.2mm a passo 1.27mm"):
        per_side.validate()


def test_the_same_module_on_a_2_54_pitch_keeps_the_reference_without_notes():
    """@brief Stesso modulo, passo che contiene il riferimento: nulla da stimare."""
    params = fine_pitch(pitch=2.54, count=5)
    params.validate()
    assert params.side_geometry(params.sides[0]).half_hole == 1.7
    assert params.side_geometry(params.sides[0]).half_hole_source == "reference"
    assert params.geometry_notes() == []


@pytest.mark.parametrize("pitch,expected,source", [
    (2.0, 1.7, "reference"),              # parete 0.3: ci sta
    (1.9, 1.7, "reference"),              # parete esattamente 0.2: ci sta
    (1.8, 1.6, "estimate_pitch"),         # parete 0.1: ridotto
])
def test_2_mm_class_pitches(pitch, expected, source):
    params = fine_pitch(pitch=pitch, count=5)
    params.validate()
    geometry = params.side_geometry(params.sides[0])
    assert geometry.half_hole == pytest.approx(expected) and geometry.half_hole_source == source


def test_through_hole_sizes_on_a_fine_pitch_are_estimated_ring_first():
    """@brief Corona nel passo, foro nella corona con la stessa parete."""
    params = fine_pitch(CONTACT_THROUGH_HOLE, body_length=24, body_width=24)
    params.validate()
    geometry = params.side_geometry(params.sides[0])
    assert geometry.ring == pytest.approx(1.07) and geometry.ring_source == "estimate_pitch"
    assert geometry.drill == pytest.approx(0.67) and geometry.drill_source == "estimate_pitch"
    pads = build_castellated_module(params).pads
    assert all(p.drill == pytest.approx(0.67) for p in pads)
    assert all(p.size == pytest.approx((1.07, 1.07)) for p in pads)
    notes = params.geometry_notes()
    assert any("corona" in n and "stimata 1.07mm" in n for n in notes)
    assert any("foro passante non dichiarato" in n and "stimato 0.67mm" in n for n in notes)
    # Negativo: la corona 1.7 DICHIARATA non si riduce, si rifiuta.
    declared = fine_pitch(CONTACT_THROUGH_HOLE, body_length=24, body_width=24,
                          thru_pad_diameter=1.7)
    with pytest.raises(ValueError, match="corona 1.7mm"):
        declared.validate()


def test_a_declared_ring_is_not_shrunk_to_host_an_assumed_drill():
    """@brief Il foro si riduce per il passo, non per rimediare a una corona dichiarata."""
    params = fine_pitch(CONTACT_THROUGH_HOLE, pitch=2.54, count=5, thru_pad_diameter=1.0)
    with pytest.raises(ValueError, match="piu' larga del foro"):
        params.validate()


def test_half_and_through_on_a_fine_pitch_keeps_both_holes_apart():
    params = fine_pitch(CONTACT_HALF_AND_THROUGH, body_length=24, body_width=24)
    params.validate()
    geometry = params.side_geometry(params.sides[0])
    assert (geometry.half_hole, geometry.ring, geometry.drill) == pytest.approx((1.07, 1.07, 0.67))
    gap = geometry.thru_inset - geometry.half_hole_inset - geometry.half_hole / 2 - geometry.drill / 2
    assert gap == pytest.approx(MIN_HOLE_WALL_MM, abs=1e-3)
    notches, _, _, _ = step_generator.castellated_contact_payload(params, "#d4af37")
    assert _notch_walls(notches) >= MIN_HOLE_WALL_MM - 1e-3


# ── Passi diversi per lato ──────────────────────────────────────────

def mixed(**changes):
    params = CastellatedModuleParams(
        name="Mixed", body_length=30, body_width=20, pad_length=1.8, pad_width=0.9,
        pad_overhang=0.5,
        sides=[CastellatedSide(edge="left", count=3, pitch=5.08, contact_style=CONTACT_HALF_HOLE),
               CastellatedSide(edge="right", count=5, pitch=2.54, first_pin=4,
                               contact_style=CONTACT_HALF_HOLE)])
    for key, value in changes.items():
        setattr(params, key, value)
    return params


def test_each_side_keeps_its_own_declared_hole():
    params = mixed()
    params.sides[0].hole_diameter = 3.0            # 5.08 - 3.0 = 2.08 di parete
    params.sides[1].hole_diameter = 1.0
    params.validate()
    notches, _, _, _ = step_generator.castellated_contact_payload(params, "#d4af37")
    assert [n["radius"] for n in notches] == [1.5] * 3 + [0.5] * 5
    # Negativo: la quota del lato largo spostata sul lato stretto non ci sta.
    params.sides[1].hole_diameter = 3.0
    with pytest.raises(ValueError, match="Lato destro: semi-fori da 3.0mm a passo 2.54mm"):
        params.validate()


def test_only_the_side_whose_pitch_is_too_small_gets_an_estimate():
    params = mixed()
    params.sides[1].pitch = 1.27
    params.validate()
    left, right = (params.side_geometry(side) for side in params.sides)
    assert (left.half_hole, left.half_hole_source) == (1.7, "reference")
    assert (right.half_hole, right.half_hole_source) == (pytest.approx(1.07), "estimate_pitch")
    notes = params.geometry_notes()
    assert len(notes) == 1 and notes[0].startswith("Lato destro")


def test_per_side_sizes_reach_footprint_3d_and_header_pins():
    params = two_rows(CONTACT_THROUGH_HOLE)
    params.sides[1].thru_hole_drill = 0.8
    params.sides[1].thru_pad_diameter = 1.4
    params.validate()
    pads = build_castellated_module(params).pads
    assert {p.drill for p in pads[:22]} == {1.0} and {p.drill for p in pads[22:]} == {0.8}
    assert {p.size for p in pads[22:]} == {(1.4, 1.4)}
    notches, contacts, _, _ = step_generator.castellated_contact_payload(params, "#d4af37")
    assert {n["radius"] for n in notches[22:]} == {0.4}
    assert {c["pad_width_mm"] for c in contacts[22:]} == {1.4}
    pins, _, skipped = step_generator.castellated_header_pins(params)
    assert len(pins) == 44 and skipped == []


# ── (8) Distanza fra le file per ogni tipo con foro passante ────────

def test_row_spacing_places_the_through_holes_of_a_double_contact():
    """@brief Positivo: file a 22.86 mm su 25.4 -> fori a 1.27 dal bordo, 22.86 fra loro."""
    params = two_rows()
    params.validate()
    ys = sorted({round(p.at[1], 6) for p in build_castellated_module(params).pads})
    assert ys == pytest.approx([-11.43, 11.43])        # 22.86 fra le file, non 21.008
    geometry = params.side_geometry(params.sides[0])
    assert geometry.thru_inset_source == "row_spacing"
    # Il semi-foro di riferimento non lascia la parete dietro a quei fori: e'
    # assunto, quindi viene ridotto e la stima e' scritta.
    assert geometry.half_hole_source == "estimate_rows"
    gap = geometry.thru_inset - geometry.half_hole_inset - geometry.half_hole / 2 - geometry.drill / 2
    assert gap >= MIN_HOLE_WALL_MM - 1e-9
    notes = params.geometry_notes()
    assert any("22.86" in n and "ricavati" in n for n in notes)
    assert any("semi-foro non dichiarato" in n and "22.86" in n for n in notes)
    pins, _, _ = step_generator.castellated_header_pins(params)
    assert {round(abs(pin[2]), 6) for pin in pins} == {11.43}


def test_row_spacing_that_leaves_no_wall_to_a_declared_half_hole_is_refused():
    """@brief Negativo: con il semi-foro dichiarato non c'e' nulla da stimare."""
    params = two_rows(hole_diameter=1.7)
    with pytest.raises(ValueError, match="le file dichiarate distano 22.86mm"):
        params.validate()
    params.hole_diameter = 0.6                          # 1.27 - 0.228 - 0.3 - 0.5 = 0.242
    params.validate()


def test_row_spacing_is_not_invented_when_it_cannot_be_attributed():
    """@brief Negativo strutturale: quattro lati o una fila sola -> niente file."""
    four = fine_pitch(CONTACT_HALF_AND_THROUGH, pitch=2.54, count=3, body_length=30,
                      body_width=30, row_spacing=27.46)
    four.validate()
    geometry = four.side_geometry(four.sides[0])
    assert geometry.thru_inset_source == "fallback" and geometry.rows_inset is None
    assert geometry.thru_inset == four.thru_hole_centre_offset
    assert any("non dichiarata, stimata" in n for n in four.geometry_notes())
    single = two_rows()
    single.sides = single.sides[:1]
    assert single.side_geometry(single.sides[0]).thru_inset_source == "fallback"


def test_castellation_rows_are_not_used_to_place_through_holes():
    """@brief File a 24.1 mm su 25.4: centri a 0.65, dentro la corona -> sono i semi-fori."""
    params = two_rows(row_spacing=24.1)
    params.validate()
    geometry = params.side_geometry(params.sides[0])
    assert geometry.thru_inset_source == "half_hole_rows"
    assert geometry.thru_inset == params.thru_hole_centre_offset
    assert any("file di semi-fori" in n for n in params.geometry_notes())
    # Negativo: per un lato through_hole la stessa distanza resta un errore
    # (non ha semi-fori a cui attribuirla).
    with pytest.raises(ValueError, match="semi-foro e non un foro passante"):
        two_rows(CONTACT_THROUGH_HOLE, row_spacing=24.1).validate()


def test_an_unusable_row_spacing_is_refused_for_double_contacts_too():
    with pytest.raises(ValueError, match="distanza fra le file"):
        two_rows(row_spacing=float("nan")).validate()


# ── Parete fra fori: la regola non e' solo il passo ─────────────────

def test_half_holes_of_two_sides_meeting_at_a_corner_are_refused():
    """@brief Piazzole separate (0.42 mm), semi-fori a 0.02 mm: il rame non basta a vederlo."""
    sides = [CastellatedSide(edge="left", count=1, pitch=0, offset=-3.9,
                             contact_style=CONTACT_HALF_HOLE, pad_width=0.4, hole_diameter=1.0),
             CastellatedSide(edge="top", count=1, pitch=0, offset=-3.9, first_pin=2,
                             contact_style=CONTACT_HALF_HOLE, pad_width=0.4, hole_diameter=1.0)]
    params = CastellatedModuleParams(name="Corner", body_length=10, body_width=10,
                                     pad_length=0.8, pad_overhang=0.2, sides=sides)
    with pytest.raises(ValueError, match="i due fori si fonderebbero"):
        params.validate()
    for side in params.sides:                   # stessi fori lontani dall'angolo
        side.offset = -3.0
    params.validate()


def test_a_thin_wall_behind_the_half_hole_is_refused_with_an_explicit_offset():
    """@brief Parete 0.1 mm: i fori non si toccano ma il materiale non e' fabbricabile."""
    params = CastellatedModuleParams(
        name="Thin", body_length=42.8, body_width=20, thru_hole_offset=2.096,
        sides=[CastellatedSide(edge="left", count=3, pitch=5.08)])
    with pytest.raises(ValueError, match="restano 0.10mm di scheda"):
        params.validate()
    params.thru_hole_offset = 2.196
    params.validate()


def test_a_module_wide_inset_that_leaves_a_small_half_hole_uncut_is_refused():
    params = fine_pitch(pitch=2.54, count=5, hole_inset=0.5)
    params.validate()                                # 0.5 < 0.85: tagliato dal bordo
    params.sides[0].hole_diameter = 0.8              # 0.5 >= 0.4: non tagliato
    with pytest.raises(ValueError, match="il bordo non lo taglierebbe"):
        params.validate()
