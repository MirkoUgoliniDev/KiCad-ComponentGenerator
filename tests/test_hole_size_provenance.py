"""@brief Provenance of the per-side hole sizes between the analysis and the generator.

Difetto trovato dall'utente il 2026-09-26 (progetto di prova, 3D bloccato con
«le file dichiarate distano 22.86mm ... resterebbero -0.11mm di scheda»):
l'analisi aveva LETTO dal disegno la distanza fra le file e STIMATO dalla foto
semi-foro e foro (value_sources "estimated"), ma la GUI passava i diametri per
lato al generatore come numeri semplici, cioe' come quote dichiarate. Il
generatore rispettava allora la stima e rifiutava la quota letta.

Regola: un diametro per lato e' dichiarato solo se la sua provenienza e'
"read" o "derived"; stimato, senza provenienza o salvato prima di questa
informazione resta una stima (AssumedSize), che il generatore riduce se non
sta e annota. Ogni caso positivo ha il suo negativo: stessi numeri, provenienza
diversa.
"""

import pytest

from test_photo_inventory_gui import gui
from kicadgen.generators.castellated_module import (
    CONTACT_HALF_AND_THROUGH, AssumedSize, MIN_HOLE_WALL_MM, is_declared_size,
)
from kicadgen.services import contact_zones
from kicadgen.services.datasheet_extractor import ExtractedParams

FIELDS = ("half_hole_diameter_mm", "through_hole_drill_mm")


def two_row_extraction(source):
    """Due file da 22 contatti a 2.54 su 63.3x25.4, fori da 1.0 con provenienza `source`."""
    sides = [dict(edge=edge, count=22, pitch_mm=2.54, offset_mm=0.0, first_pin=first,
                  reverse=reverse, contact_style=CONTACT_HALF_AND_THROUGH,
                  half_hole_diameter_mm=1.0, through_hole_drill_mm=1.0)
             for edge, first, reverse in (("top", 1, False), ("bottom", 23, True))]
    sources = {f"castellated_sides.{side['edge']}.{field}": source
               for side in sides for field in FIELDS} if source else {}
    return ExtractedParams(
        name="", description="", tags="", confidence="medium", notes="",
        layout="castellated", castellated_sides=sides, body_length_mm=63.3,
        body_width_mm=25.4, rows=2, cols=22, pitch_mm=2.54, row_spacing_mm=22.86,
        pad_diameter_mm=1.7, drill_mm=1.0, photo_source="", components_3d=[],
        value_sources=sources)


def module_from(window, source):
    window.group_castellated.setChecked(True)
    window._apply_extracted_castellated(two_row_extraction(source))
    window.field_cast_body_length.setValue(63.3)
    window.field_cast_body_width.setValue(25.4)
    window._declared_row_spacing = 22.86
    return window._current_castellated_params()


@pytest.mark.parametrize("source", ["estimated", "undeclared", None])
def test_estimated_hole_sizes_yield_to_the_read_row_spacing(window, source):
    """@brief Positivo: stima o nessuna provenienza -> semi-foro ridotto e annotato."""
    params = module_from(window, source)
    assert all(not is_declared_size(side.hole_diameter) for side in params.sides)
    params.validate()                                    # prima: ValueError
    geometry = params.side_geometry(params.sides[0])
    assert geometry.thru_inset_source == "row_spacing"
    assert geometry.half_hole < 1.0
    gap = geometry.thru_inset - geometry.half_hole_inset - geometry.half_hole / 2 - geometry.drill / 2
    assert gap >= MIN_HOLE_WALL_MM - 1e-9
    assert any("22.86" in note for note in params.geometry_notes())


@pytest.mark.parametrize("source", ["read", "derived"])
def test_hole_sizes_read_from_the_documents_are_still_refused_when_they_conflict(window, source):
    """@brief Negativo: gli stessi 1.0 letti dai documenti sono una misura e non si toccano."""
    params = module_from(window, source)
    assert all(is_declared_size(side.hole_diameter) for side in params.sides)
    with pytest.raises(ValueError, match="le file dichiarate distano 22.86mm"):
        params.validate()


def test_the_provenance_survives_saving_and_reloading_the_sides(window):
    """@brief Il salvataggio conserva la provenienza; un salvataggio vecchio conta come stima."""
    module_from(window, "read")
    saved = window._serialised_sides()
    assert all(side["half_hole_diameter_declared"] is True for side in saved)
    window._sides.clear()
    for side in saved:
        window._add_side_row(side)
    assert all(is_declared_size(side.hole_diameter) for side in window._sides_from_table())
    # Progetto salvato prima della chiave: la provenienza e' ignota -> stima.
    window._sides.clear()
    for side in saved:
        legacy = {k: v for k, v in side.items() if not k.endswith("_declared")}
        window._add_side_row(legacy)
    assert all(isinstance(side.hole_diameter, AssumedSize) for side in window._sides_from_table())


def raw_sides(**side_extra):
    return [dict(edge=edge, count=22, pitch_mm=2.54, contact_style=CONTACT_HALF_AND_THROUGH,
                 half_hole_diameter_mm=1.0, through_hole_drill_mm=1.0, **side_extra)
            for edge in ("top", "bottom")]


@pytest.mark.parametrize("data, declared", [
    (dict(castellated_sides=raw_sides()), False),
    (dict(castellated_sides=raw_sides(value_sources={"half_hole_diameter_mm": "estimated"})), False),
    (dict(castellated_sides=raw_sides(value_sources={"half_hole_diameter_mm": "read"})), True),
    (dict(castellated_sides=raw_sides(half_hole_diameter_declared=True)), True),
    (dict(castellated_sides=raw_sides(), value_sources={
        "castellated_sides.top.half_hole_diameter_mm": "derived",
        "castellated_sides.bottom.half_hole_diameter_mm": "derived"}), True),
])
def test_the_photo_contact_filter_applies_the_same_provenance_rule(data, declared):
    """@brief Il filtro dei contatti della foto disegna gli stessi fori di footprint e 3D."""
    data = dict(data, row_spacing_mm=22.86)
    module, pairs = contact_zones.declared_module(data, 63.3, 25.4)
    assert [is_declared_size(built.hole_diameter) for _, built in pairs] == [declared] * 2
    geometry = contact_zones._geometry(module, pairs[0][1])
    # Una stima si adatta alla quota letta e lascia la parete minima; una
    # misura resta com'e' (il conflitto lo rifiuta validate() nella GUI).
    gap = geometry.thru_inset - geometry.half_hole_inset - geometry.half_hole / 2 - geometry.drill / 2
    assert (geometry.half_hole < 1.0) is not declared
    assert (gap >= MIN_HOLE_WALL_MM - 1e-9) is not declared


@pytest.mark.parametrize("source, drill", [(None, 0.8), ("estimated", 0.8), ("read", 1.2)])
def test_a_module_drill_outranks_a_side_estimate_only_when_it_was_read(source, drill):
    """@brief Precedenza: lato dichiarato, modulo dichiarato, stima del lato, stima del modulo."""
    data = dict(drill_mm=1.2, castellated_sides=[
        dict(edge="top", count=5, pitch_mm=2.54, contact_style="through_hole",
             through_hole_drill_mm=0.8)])
    if source:
        data["value_sources"] = {"drill_mm": source}
    module, pairs = contact_zones.declared_module(data, 30.0, 20.0)
    assert module.side_geometry(pairs[0][1]).drill == pytest.approx(drill)
