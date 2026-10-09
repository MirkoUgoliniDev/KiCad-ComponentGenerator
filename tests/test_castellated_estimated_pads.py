"""@brief Estimated pad sizes of a castellated module: reduced and noted, declared ones refused.

Difetto della prova reale del 2026-09-28 (HANDOFF punto 140): l'analisi aveva
STIMATO la larghezza delle piazzole del lato destro pari al passo (2.54 mm) e il
generatore rifiutava il modulo («il rame del contatto 4 (lato destro) tocca
quello del contatto 5»). Decisione dell'utente, «Riduci e segnala»: una misura
di piazzola STIMATA che fa toccare due contatti si riduce quanto basta per
lasciare MIN_COPPER_GAP_MM di rame libero, e la riduzione finisce nelle note;
una misura LETTA (numero semplice) che non sta continua a essere rifiutata.

Ogni caso positivo ha il suo negativo: stessi numeri, provenienza diversa.
"""

import math

import pytest

from kicadgen.generators.castellated_module import (
    CONTACT_HALF_HOLE, CONTACT_THROUGH_HOLE, MIN_COPPER_GAP_MM, PAD_ASSUMED, PAD_DECLARED,
    PAD_ESTIMATE_CLEARANCE, PAD_ESTIMATE_PITCH, AssumedSize, CastellatedModuleParams,
    CastellatedSide, _box_distance, _copper_outline, build_castellated_module, pad_positions,
)


def rs485(right_width, left_width=AssumedSize(3.0), length=AssumedSize(2.0)):
    """Il convertitore della prova: 3 contatti a 5.08 a sinistra, 5 a 2.54 a destra."""
    return CastellatedModuleParams(
        name="RS485", body_length=30.0, body_width=15.2,
        sides=[CastellatedSide(edge="left", count=3, pitch=5.08, pad_width=left_width,
                               pad_length=length, contact_style=CONTACT_HALF_HOLE),
               CastellatedSide(edge="right", count=5, pitch=2.54, first_pin=4,
                               pad_width=right_width, pad_length=length,
                               contact_style=CONTACT_HALF_HOLE)])


@pytest.mark.parametrize('length', [1.0, 1.4])
def test_estimated_land_shorter_than_overhang_uses_module_reference(length):
    from tests.test_host_mounting import module
    from kicadgen.generators.host_mounting import header_footprint_params
    params = module()
    params.sides[1].pad_length = AssumedSize(length)
    params.validate()
    assert params.pad_sizes()[1].length == float(params.pad_length)
    assert float(params.sides[1].pad_length) == length
    assert any('sbalzo' in note and 'Quota da verificare' in note
               for note in params.geometry_notes())
    assert len(header_footprint_params(params, 1.3, .5).pins) == 7
    params.sides[1].pad_length = length
    with pytest.raises(ValueError, match='sbalzo'):
        params.validate()


def test_no_usable_reference_still_rejects_incompatible_estimate():
    params = rs485(AssumedSize(1.8), length=AssumedSize(1.0))
    params.pad_length = AssumedSize(1.2)
    with pytest.raises(ValueError, match='sbalzo'):
        params.validate()


def smallest_gap(params, first_edge=None, second_edge=None):
    """Smallest copper gap between two contacts with different numbers, mm."""
    placements = list(pad_positions(params))
    gap = math.inf
    for i, a in enumerate(placements):
        for b in placements[i + 1:]:
            if a.number == b.number:
                continue
            if first_edge and {a.edge, b.edge} != {first_edge, second_edge}:
                continue
            oa, ob = _copper_outline(params, a), _copper_outline(params, b)
            gap = min(gap, _box_distance(oa, ob) - oa[4] - ob[4])
    return gap


def pad_notes(params):
    return [note for note in params.geometry_notes() if "piazzole" in note]


# -- larghezza lungo il bordo -------------------------------------------------

def test_an_estimated_width_equal_to_the_pitch_is_reduced_and_noted():
    """@brief Positivo: 2.54 stimato a passo 2.54 -> 2.34, rame a 0.2 mm, nota scritta."""
    params = rs485(AssumedSize(2.54))
    params.validate()                                   # prima: ValueError «in corto»
    size = params.pad_sizes()[1]
    assert size.width_source == PAD_ESTIMATE_PITCH
    assert size.width == pytest.approx(2.54 - MIN_COPPER_GAP_MM)
    assert size.width_estimate == pytest.approx(2.54)
    assert smallest_gap(params) >= MIN_COPPER_GAP_MM - 1e-9
    footprint = build_castellated_module(params)
    right = [pad for pad in footprint.pads if pad.number in {"4", "5", "6", "7", "8"}]
    assert right and all(pad.size[1] == pytest.approx(2.34) for pad in right)
    notes = pad_notes(params)
    assert len(notes) == 1
    assert "destro" in notes[0] and "2.54" in notes[0] and "2.34" in notes[0]
    assert "stimata" in notes[0]


def test_the_same_width_read_from_the_documents_is_still_refused():
    """@brief Negativo: lo stesso 2.54 come misura letta (float) resta un corto e si rifiuta."""
    params = rs485(2.54)
    with pytest.raises(ValueError, match=r"Il rame del contatto 4 \(lato destro\) tocca quello "
                                         r"del contatto 5"):
        params.validate()
    assert params.pad_sizes()[1].width_source == PAD_DECLARED
    assert pad_notes(params) == []


def test_two_sides_keep_their_own_estimated_sizes():
    """@brief Il 2.54 del lato destro non arriva al lato sinistro a passo 5.08."""
    params = rs485(AssumedSize(2.54), left_width=AssumedSize(3.0))
    params.validate()
    left, right = params.pad_sizes()
    assert (left.width, left.width_source) == (pytest.approx(3.0), PAD_ASSUMED)
    assert right.width == pytest.approx(2.34)
    left_pads = [p for p in pad_positions(params) if p.edge == "left"]
    assert all(p.height == pytest.approx(3.0) for p in left_pads)
    assert not any("sinistro" in note for note in pad_notes(params))


def test_an_estimated_width_that_already_fits_is_untouched():
    """@brief Negativo della riduzione: 1.2 stimato a passo 2.54 resta 1.2, nessuna nota."""
    params = rs485(AssumedSize(1.2))
    params.validate()
    size = params.pad_sizes()[1]
    assert (size.width, size.width_source) == (pytest.approx(1.2), PAD_ASSUMED)
    assert pad_notes(params) == []


def test_a_width_leaving_less_than_the_minimum_gap_is_reduced_even_if_not_touching():
    """@brief 2.44 a passo 2.54 non tocca (0.1 mm) ma e' sotto il minimo: stima -> 2.34."""
    params = rs485(AssumedSize(2.44))
    assert params.pad_sizes()[1].width == pytest.approx(2.34)
    declared = rs485(2.44)
    declared.validate()                     # misura letta: 0.1 mm non e' un corto, resta
    assert declared.pad_sizes()[1].width == pytest.approx(2.44)


def test_the_module_reference_pad_is_an_estimate_but_a_declared_module_pad_is_not():
    """@brief Lato senza misura: il riferimento 1.8 a passo 1.27 si riduce; 1.8 dichiarato no."""
    sides = [CastellatedSide(edge="left", count=4, pitch=1.27, contact_style=CONTACT_HALF_HOLE,
                             hole_diameter=AssumedSize(0.8))]
    params = CastellatedModuleParams(name="Fine", body_length=20, body_width=10, sides=sides)
    params.validate()
    assert params.side_pad_width(params.sides[0]) == pytest.approx(1.07)
    assert len(pad_notes(params)) == 1
    declared = CastellatedModuleParams(name="Fine", body_length=20, body_width=10,
                                       pad_width=1.8, sides=[CastellatedSide(
                                           edge="left", count=4, pitch=1.27,
                                           contact_style=CONTACT_HALF_HOLE,
                                           hole_diameter=AssumedSize(0.8))])
    with pytest.raises(ValueError, match="sovrapporrebbero"):
        declared.validate()


def test_changing_only_the_provenance_changes_the_result():
    """@brief La cache distingue AssumedSize da float con lo stesso valore."""
    params = rs485(AssumedSize(2.54))
    params.validate()
    params.sides[1].pad_width = 2.54
    with pytest.raises(ValueError, match="in corto"):
        params.validate()


def test_through_hole_sides_are_not_reduced():
    """@brief Il lato a foro passante usa la corona, non la piazzola: niente riduzione o nota."""
    params = CastellatedModuleParams(
        name="Dev", body_length=30, body_width=15, sides=[
            CastellatedSide(edge="left", count=5, pitch=2.54, pad_width=AssumedSize(2.54),
                            contact_style=CONTACT_THROUGH_HOLE)])
    params.validate()
    assert params.pad_sizes()[0].width_source == PAD_ASSUMED
    assert pad_notes(params) == []


# -- lunghezza verso l'interno -------------------------------------------------

def facing_rows(length):
    """Due file affacciate su un corpo stretto: 3.1 mm dentro per lato su 6 mm."""
    return CastellatedModuleParams(
        name="Narrow", body_length=6.0, body_width=10.0, pad_overhang=0.5, sides=[
            CastellatedSide(edge="left", count=2, pitch=5.0, pad_length=length,
                            pad_width=1.0, contact_style=CONTACT_HALF_HOLE),
            CastellatedSide(edge="right", count=2, pitch=5.0, first_pin=3, pad_length=length,
                            pad_width=1.0, contact_style=CONTACT_HALF_HOLE)])


def test_estimated_lengths_of_facing_rows_are_shortened_to_the_gap():
    """@brief Positivo: 3.6 stimato su file affacciate -> accorciato quanto basta, annotato."""
    params = facing_rows(AssumedSize(3.6))
    params.validate()
    sizes = params.pad_sizes()
    assert [s.length_source for s in sizes] == [PAD_ESTIMATE_CLEARANCE] * 2
    gap = smallest_gap(params)
    assert MIN_COPPER_GAP_MM - 1e-9 <= gap < MIN_COPPER_GAP_MM + 0.03   # quanto basta
    notes = pad_notes(params)
    assert len(notes) == 2 and "lato destro" in notes[0] and "lato sinistro" in notes[1]


def test_declared_lengths_of_facing_rows_are_refused():
    """@brief Negativo: le stesse lunghezze lette dai documenti restano e si rifiutano."""
    params = facing_rows(3.6)
    with pytest.raises(ValueError, match="in corto"):
        params.validate()


def test_only_the_estimated_side_is_shortened_when_the_other_is_declared():
    """@brief Una misura letta non si tocca: si accorcia solo la stima di fronte."""
    params = facing_rows(3.6)
    params.sides[0].pad_length = AssumedSize(3.6)
    params.validate()
    left, right = params.pad_sizes()
    assert left.length_source == PAD_ESTIMATE_CLEARANCE and right.length_source == PAD_DECLARED
    assert right.length == pytest.approx(3.6)
    assert smallest_gap(params) >= MIN_COPPER_GAP_MM - 1e-9


def test_estimated_pads_meeting_at_a_corner_are_shortened():
    """@brief Angolo: le piazzole di riferimento di due lati si sovrappongono -> accorciate."""
    sides = [CastellatedSide(edge="left", count=1, pitch=0, offset=-3.0,
                             contact_style=CONTACT_HALF_HOLE),
             CastellatedSide(edge="top", count=1, pitch=0, offset=-3.0, first_pin=2,
                             contact_style=CONTACT_HALF_HOLE)]
    params = CastellatedModuleParams(name="Corner", body_length=10, body_width=10, sides=sides)
    params.validate()                        # semi-fori separati, piazzole di riferimento no
    declared = CastellatedModuleParams(name="Corner", body_length=10, body_width=10,
                                       pad_length=3.6, pad_width=1.8,
                                       sides=[CastellatedSide(**vars(s)) for s in sides])
    with pytest.raises(ValueError, match="in corto"):
        declared.validate()
    assert smallest_gap(params) >= MIN_COPPER_GAP_MM - 1e-9
    assert all(s.length > params.pad_overhang for s in params.pad_sizes())
    assert len(pad_notes(params)) == 2


def test_lengths_that_do_not_conflict_are_untouched():
    """@brief Negativo della riduzione: corpo largo, stessa stima 3.6 -> nessun cambio."""
    params = facing_rows(AssumedSize(3.6))
    params.body_length = 20.0
    params.validate()
    assert [s.length_source for s in params.pad_sizes()] == [PAD_ASSUMED] * 2
    assert pad_notes(params) == []


def test_a_declared_module_pad_is_only_the_default_of_sides_without_their_own():
    """@brief Il campo del modulo (anche dichiarato) non scavalca la misura propria di un lato.

    @note Diverso dai fori (dove il modulo dichiarato vince sulla stima del
        lato): la piazzola del modulo e' solo il default dei lati senza land
        pattern, e farla vincere e' proprio come il 2.54 di un lato arrivava a
        tutti i lati (28/09).
    """
    params = rs485(AssumedSize(2.54))
    params.pad_width = 2.54
    params.validate()
    left, right = params.pad_sizes()
    assert left.width == pytest.approx(3.0) and right.width == pytest.approx(2.34)
    params.sides[0].pad_width = None                      # lato senza misura: vale il modulo
    assert params.pad_sizes()[0].width_source == PAD_DECLARED
    assert params.pad_sizes()[0].width == pytest.approx(2.54)
