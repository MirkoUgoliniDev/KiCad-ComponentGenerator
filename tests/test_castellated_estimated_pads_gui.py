"""@brief Per-side pad sizes and their provenance between the analysis, the project and the generator.

Difetto della prova reale del 2026-09-28 (HANDOFF punto 140): la finestra
copiava pad_length_mm/pad_width_mm di OGNI lato nei campi unici del modulo, cosi'
vinceva l'ultimo lato (il 2.54 del destro valeva anche per il sinistro a passo
5.08) e arrivava al generatore come misura dichiarata. Ora le misure restano
lato per lato con la loro provenienza: lette/ricavate = dichiarate, stimate o
senza provenienza = AssumedSize, che il generatore riduce e annota.
"""

import pytest

from test_photo_inventory_gui import gui  # noqa: F401 - stessa preparazione dei test GUI
from kicadgen.generators.castellated_module import (
    CONTACT_HALF_HOLE, PAD_ASSUMED, PAD_DECLARED, PAD_ESTIMATE_PITCH, AssumedSize,
    is_declared_size,
)
from kicadgen.services.datasheet_extractor import ExtractedParams


def extraction(right_source, left_source="read"):
    """Il convertitore RS485 della prova: sinistra 3 a 5.08, destra 5 a 2.54 con 2.54 di larghezza."""
    sides = [dict(edge="left", count=3, pitch_mm=5.08, first_pin=1, contact_style=CONTACT_HALF_HOLE,
                  pad_length_mm=2.0, pad_width_mm=3.0),
             dict(edge="right", count=5, pitch_mm=2.54, first_pin=4, contact_style=CONTACT_HALF_HOLE,
                  pad_length_mm=2.0, pad_width_mm=2.54)]
    sources = {}
    for edge, source in (("left", left_source), ("right", right_source)):
        if source:
            for field in ("pad_length_mm", "pad_width_mm"):
                sources[f"castellated_sides.{edge}.{field}"] = source
    return ExtractedParams(
        name="RS485", description="", tags="", confidence="medium", notes="",
        layout="castellated", castellated_sides=sides, body_length_mm=30.0, body_width_mm=15.2,
        photo_source="", components_3d=[], value_sources=sources)


def module_from(window, right_source, left_source="read"):
    window.group_castellated.setChecked(True)
    window._apply_extracted_castellated(extraction(right_source, left_source))
    window.field_cast_body_length.setValue(30.0)
    window.field_cast_body_width.setValue(15.2)
    return window._current_castellated_params()


@pytest.mark.parametrize("source", ["estimated", "undeclared", None])
def test_an_estimated_right_width_is_reduced_and_the_left_side_keeps_its_own(window, source):
    """@brief Positivo: stima 2.54 a destra -> 2.34 annotato; il sinistro resta 3.0 letto."""
    params = module_from(window, source)
    left, right = params.sides
    assert is_declared_size(left.pad_width) and left.pad_width == pytest.approx(3.0)
    assert isinstance(right.pad_width, AssumedSize)
    params.validate()                                   # prima: ValueError «in corto»
    sizes = params.pad_sizes()
    assert (sizes[0].width, sizes[0].width_source) == (pytest.approx(3.0), PAD_DECLARED)
    assert (sizes[1].width, sizes[1].width_source) == (pytest.approx(2.34), PAD_ESTIMATE_PITCH)
    assert any("2.34" in note for note in params.geometry_notes())
    # I campi del modulo non prendono la misura di nessun lato.
    assert window.field_cast_pad_width.value() == pytest.approx(1.8)
    assert not is_declared_size(params.pad_width)


@pytest.mark.parametrize("source", ["read", "derived"])
def test_a_right_width_read_from_the_documents_is_still_refused(window, source):
    """@brief Negativo: lo stesso 2.54 letto dai documenti e' una misura e resta un corto."""
    params = module_from(window, source)
    assert is_declared_size(params.sides[1].pad_width)
    with pytest.raises(ValueError, match=r"contatto 4 \(lato destro\) tocca quello del contatto 5"):
        params.validate()


def test_a_new_extraction_resets_a_module_pad_left_by_an_older_project(window):
    """@brief Un 2.54 rimasto nel campo del modulo non vale per la scheda nuova."""
    window.field_cast_pad_width.setValue(2.54)
    params = module_from(window, "estimated")
    assert window.field_cast_pad_width.value() == pytest.approx(1.8)
    params.validate()


def test_project_save_and_restore_keeps_per_side_sizes_and_provenance(window):
    """@brief Salvataggio e riapertura: stesse misure per lato, stessa provenienza."""
    module_from(window, "estimated")
    state = window._project_state()
    saved = {side["edge"]: side for side in state["sides"]}
    assert saved["left"]["pad_width_mm"] == pytest.approx(3.0)
    assert saved["left"]["pad_width_declared"] is True
    assert saved["right"]["pad_width_mm"] == pytest.approx(2.54)
    assert saved["right"]["pad_width_declared"] is False
    window._sides.clear()
    window._apply_project_state(state)
    params = window._current_castellated_params()
    left, right = params.sides
    assert is_declared_size(left.pad_width) and left.pad_width == pytest.approx(3.0)
    assert isinstance(right.pad_width, AssumedSize) and right.pad_width == pytest.approx(2.54)
    params.validate()
    assert params.pad_sizes()[1].width == pytest.approx(2.34)


def test_serialised_sides_reapplied_without_sources_keep_their_provenance(window):
    """@brief Rilancio senza value_sources: la provenienza viaggia nel lato."""
    module_from(window, "estimated")
    sides = window._serialised_sides()
    window._sides.clear()
    window._apply_extracted_castellated(ExtractedParams(
        name="RS485", description="", tags="", confidence="low", notes="",
        layout="castellated", castellated_sides=sides, body_length_mm=30.0, body_width_mm=15.2,
        photo_source="", components_3d=[]))
    left, right = window._current_castellated_params().sides
    assert is_declared_size(left.pad_width)
    assert isinstance(right.pad_width, AssumedSize)


def test_an_old_project_without_per_side_pads_behaves_as_before(window):
    """@brief Progetto salvato prima del 28/09: nessuna misura per lato, vale il campo del modulo."""
    module_from(window, "estimated")
    state = window._project_state()
    state["sides"] = [{k: v for k, v in side.items() if not k.startswith("pad_")}
                      for side in state["sides"]]
    state["widgets"]["field_cast_pad_width"] = 2.54     # quello che il difetto vi copiava
    window._sides.clear()
    window._apply_project_state(state)
    params = window._current_castellated_params()
    assert all(side.pad_width is None for side in params.sides)
    assert is_declared_size(params.pad_width) and params.pad_width == pytest.approx(2.54)
    with pytest.raises(ValueError, match="in corto"):   # come prima: 2.54 dichiarato a passo 2.54
        params.validate()
    assert [s.width_source for s in params.pad_sizes()] == [PAD_DECLARED, PAD_DECLARED]


def test_an_old_project_with_the_reference_pad_keeps_the_reference_as_an_estimate(window):
    """@brief Progetto vecchio con il campo al riferimento 1.8: stima, invariata dove sta."""
    module_from(window, "estimated")
    state = window._project_state()
    state["sides"] = [{k: v for k, v in side.items() if not k.startswith("pad_")}
                      for side in state["sides"]]
    window._sides.clear()
    window._apply_project_state(state)
    params = window._current_castellated_params()
    params.validate()
    assert [s.width_source for s in params.pad_sizes()] == [PAD_ASSUMED, PAD_ASSUMED]
    assert [s.width for s in params.pad_sizes()] == [pytest.approx(1.8)] * 2
