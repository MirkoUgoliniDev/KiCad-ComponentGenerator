"""Micro-switch: base squadrata piu' pulsante cilindrico, senza FreeCAD.

Il mock registra le scatole; il cilindro del pulsante passa da makeCylinder,
quindi qui si verificano l'ingombro complessivo e la ripartizione in altezza,
non la topologia OCC.
"""

import pytest

from test_observed_terminals import cad


Z0 = 0.96


def parts_for(cad, size, color="#1d1d1e"):
    module, made = cad
    made.clear()
    return module._tactile_switch(-28.0, 5.0, Z0, *size, color), made


def test_the_button_sits_on_top_of_the_base(cad):
    (base, plunger), made = parts_for(cad, (3.5, 3.5, 1.7))
    base_box = made[0]
    assert base_box[5] == pytest.approx(1.7 * 0.62), "la base non lascia spazio al pulsante"
    assert plunger is not base, "pulsante e base devono restare pezzi distinti"


@pytest.mark.parametrize("size", [(3.5, 3.5, 1.7), (6.0, 6.0, 4.3), (4.5, 3.0, 1.6),
                                  (3.0, 3.0, 0.12), (0.4, 0.4, 0.3)])
def test_the_envelope_matches_the_measured_component(cad, size):
    """Un pezzo che rimpicciolisce falserebbe l'ingombro letto dalla foto."""
    parts, made = parts_for(cad, size)
    base = made[0]
    length, width, height = size
    assert (base[3], base[4]) == pytest.approx((length, width))
    top = base[2] + base[5] + (height * 0.38 if len(parts) == 2 else 0)
    assert top - Z0 == pytest.approx(height)


def test_a_switch_too_flat_for_a_button_stays_a_single_solid(cad):
    parts, _ = parts_for(cad, (3.0, 3.0, 0.12))
    assert len(parts) == 1


def test_the_button_is_shaded_from_the_photographed_colour(cad):
    """Nessun colore inventato: e' lo stesso tono, schiarito."""
    module, _ = cad
    (_, colour), = [(s, c) for s, c in parts_for(cad, (3.5, 3.5, 1.7))[0][1:]]
    assert colour != "#1d1d1e" and colour.startswith("#") and len(colour) == 7


@pytest.mark.parametrize("bad", [None, "", "#xyz", "non-un-colore", 42])
def test_malformed_colours_are_passed_through_untouched(cad, bad):
    """Un colore illeggibile non deve diventare un colore inventato."""
    module, _ = cad
    assert module._shade(bad, 1.45) == bad


def test_the_kind_taxonomy_stays_aligned_across_the_three_copies(cad):
    """BUILDERS, COMPONENT_KINDS e l'enum dello schema devono coincidere.

    La lista e' duplicata perche' freecad_parts gira solo dentro FreeCAD:
    un kind offerto al modello ma senza builder produce una scatola muta,
    e un builder mai offerto e' codice morto.
    """
    from kicadgen.services.datasheet_extractor import COMPONENT_KINDS, _openai_tool

    module, _ = cad
    schema = (_openai_tool()["function"]["parameters"]["properties"]
              ["components_3d"]["items"]["properties"]["kind"]["enum"])
    assert sorted(module.BUILDERS) == sorted(COMPONENT_KINDS) == sorted(schema)
