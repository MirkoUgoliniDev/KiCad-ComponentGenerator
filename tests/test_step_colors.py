"""Test dei colori AP214 iniettati nello STEP e dei tipi di componente.

`Part.export` di FreeCAD scrive la sola geometria: e' colorize_step() a
rendere i colori visibili in KiCad. Qui lo si collauda su uno STEP
sintetico, senza FreeCAD.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from kicadgen.cad3d.step_generator import (
    clamp_component_position, colorize_step, find_overlapping_components,
)
from kicadgen.services.datasheet_extractor import COMPONENT_KINDS, _params_from_tool_arguments

FAKE_STEP = (
    "ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\n"
    "#1 = ( GEOMETRIC_REPRESENTATION_CONTEXT(3) );\n"
    "#10 = MANIFOLD_SOLID_BREP('Body',#11);\n"
    "#11 = CLOSED_SHELL('',());\n"
    "#20 = MANIFOLD_SOLID_BREP('Pin1',#21);\n"
    "#21 = CLOSED_SHELL('',());\n"
    "ENDSEC;\nEND-ISO-10303-21;\n"
)


def test_colorize_step_adds_one_styled_item_per_solid():
    out = colorize_step(FAKE_STEP, ["#1a1a1a", "#c9a227"])
    assert out.count("STYLED_ITEM(") == 2
    assert out.count("COLOUR_RGB(") == 2
    assert "MECHANICAL_DESIGN_GEOMETRIC_PRESENTATION_REPRESENTATION" in out
    # Il colore oro va sul secondo solido (#20), nell'ordine di export.
    assert "COLOUR_RGB('',0.7882,0.6353,0.1529)" in out
    assert out.rstrip().endswith("END-ISO-10303-21;")
    # Le entita' nuove hanno numeri oltre l'ultimo esistente: nessuna collisione.
    assert "#22 = " in out and "#10 = MANIFOLD_SOLID_BREP" in out


def test_colorize_step_refuses_mismatched_colour_count():
    with pytest.raises(ValueError, match="2 solidi"):
        colorize_step(FAKE_STEP, ["#ffffff"])


def test_colorize_step_refuses_step_without_solids():
    with pytest.raises(ValueError, match="nessun solido"):
        colorize_step("ISO-10303-21;\nENDSEC;\nEND-ISO-10303-21;\n", [])


def test_component_kind_and_marking_are_kept():
    params = _params_from_tool_arguments({
        "layout": "castellated", "confidence": "high",
        "castellated_sides": [{"edge": "left", "count": 2, "pitch_mm": 2.54}],
        "components_3d": [
            {"name": "SP3485", "kind": "ic_soic", "marking": "SP3485",
             "x_mm": -8, "y_mm": -3, "length_mm": 5, "width_mm": 4, "height_mm": 1.2},
            {"name": "boh", "kind": "astronave",
             "x_mm": 0, "y_mm": 0, "length_mm": 1, "width_mm": 1, "height_mm": 1},
        ],
    })
    soic, unknown = params.components_3d
    assert soic["kind"] == "ic_soic" and soic["marking"] == "SP3485"
    # Un tipo sconosciuto non deve far fallire il 3D: diventa una scatola.
    assert unknown["kind"] == "generic"
    assert "ic_soic" in COMPONENT_KINDS and "led" in COMPONENT_KINDS


def test_component_rotation_is_explicit_not_inferred():
    """Un diodo verticale e' 2.0 (asse lungo) x 0.9 con rotation 90: non si
    deduce dalle proporzioni, che con stime quasi quadrate ingannano."""
    params = _params_from_tool_arguments({
        "layout": "castellated", "confidence": "high",
        "castellated_sides": [{"edge": "left", "count": 2, "pitch_mm": 2.54}],
        "components_3d": [
            {"name": "D1", "kind": "diode_melf", "rotation_deg": 90,
             "x_mm": 18, "y_mm": -5, "length_mm": 2.0, "width_mm": 0.9, "height_mm": 0.9},
            {"name": "R1", "kind": "resistor",
             "x_mm": 0, "y_mm": 0, "length_mm": 1.6, "width_mm": 0.8, "height_mm": 0.5},
        ],
    })
    diode, resistor = params.components_3d
    assert diode["rotation_deg"] == 90.0
    assert resistor["rotation_deg"] == 0.0  # assente = orizzontale


# ── Vincolo di posizione dei componenti dentro il modulo ────────────

def test_component_inside_the_board_is_left_untouched():
    x, y = clamp_component_position(
        x=0.0, y=0.0, length=2.0, width=1.0, rotation=0.0,
        body_length=42.8, body_width=15.2,
    )
    assert (x, y) == (0.0, 0.0)


def test_component_hanging_off_the_right_edge_is_pulled_in():
    """Caso reale: un resistore stimato a x=21 su un modulo lungo 42.8mm
    (bordo a 21.4) con lunghezza 1.6mm sconfina di 0.4mm oltre il bordo."""
    x, y = clamp_component_position(
        x=21.0, y=2.5, length=1.6, width=0.8, rotation=0.0,
        body_length=42.8, body_width=15.2,
    )
    assert x < 21.0
    assert x + 1.6 / 2 <= 42.8 / 2 - 0.2 + 1e-9
    assert y == 2.5  # solo l'asse che sconfina viene corretto


def test_clamp_accounts_for_rotation_swapping_the_footprint():
    """A 90 gradi l'asse lungo del componente e' su Y, non su X: il
    vincolo su X deve usare la larghezza, non la lunghezza."""
    body_length, body_width = 20.0, 10.0
    # Lunga 8mm, se orizzontale (0 gradi) sconfinerebbe da un bordo lungo 20mm
    # centrata a x=8; ruotata a 90 gradi il suo ingombro su X e' la larghezza.
    x0, _ = clamp_component_position(
        x=8.0, y=0.0, length=8.0, width=1.0, rotation=0.0,
        body_length=body_length, body_width=body_width,
    )
    x90, _ = clamp_component_position(
        x=8.0, y=0.0, length=8.0, width=1.0, rotation=90.0,
        body_length=body_length, body_width=body_width,
    )
    assert x0 < x90  # a 0 gradi il vincolo e' piu' stretto (ingombro maggiore su X)
    assert x90 == 8.0  # a 90 gradi (ingombro 1mm su X) il centro originale ci sta


def test_clamp_never_produces_a_positive_half_width_below_zero():
    """Un componente piu' grande del modulo stesso non deve dare un
    intervallo invertito: il centro resta 0, non un valore assurdo."""
    x, y = clamp_component_position(
        x=100.0, y=100.0, length=50.0, width=50.0, rotation=0.0,
        body_length=10.0, body_width=10.0,
    )
    assert x == 0.0 and y == 0.0


# ── Rilevamento componenti sovrapposti ───────────────────────────────

def test_non_overlapping_components_produce_no_warning():
    components = [
        {"name": "R1", "x": 0.0, "y": 0.0, "length_mm": 1.6, "width_mm": 0.8, "rotation_deg": 0.0},
        {"name": "R2", "x": 5.0, "y": 0.0, "length_mm": 1.6, "width_mm": 0.8, "rotation_deg": 0.0},
    ]
    assert find_overlapping_components(components) == []


def test_overlapping_components_are_reported_by_name():
    """Il caso reale: un diodo la cui stima di posizione lo fa cadere
    sopra un altro componente vicino."""
    components = [
        {"name": "D1", "x": 18.0, "y": -4.5, "length_mm": 2.0, "width_mm": 0.9, "rotation_deg": 90.0},
        {"name": "R3", "x": 18.5, "y": -4.5, "length_mm": 1.6, "width_mm": 0.8, "rotation_deg": 0.0},
    ]
    warnings = find_overlapping_components(components)
    assert len(warnings) == 1
    assert "D1" in warnings[0] and "R3" in warnings[0]


def test_components_only_touching_at_the_edge_do_not_count_as_overlapping():
    """Adiacenti ma non sovrapposti: normale su una scheda densa, non un errore."""
    components = [
        {"name": "R1", "x": 0.0, "y": 0.0, "length_mm": 1.6, "width_mm": 0.8, "rotation_deg": 0.0},
        {"name": "R2", "x": 1.6, "y": 0.0, "length_mm": 1.6, "width_mm": 0.8, "rotation_deg": 0.0},
    ]
    assert find_overlapping_components(components) == []


def test_overlap_check_accounts_for_rotation():
    """Senza tener conto della rotazione, un componente ruotato di 90 gradi
    (ingombro stretto su X) sembrerebbe sovrapporsi a un vicino che in
    realta' e' fuori dal suo ingombro reale."""
    components = [
        # Se preso orizzontale (8x1) si sovrappone al vicino a x=5; ruotato
        # a 90 gradi (ingombro 1x8 su X/Y) non lo tocca.
        {"name": "Long", "x": 0.0, "y": 0.0, "length_mm": 8.0, "width_mm": 1.0, "rotation_deg": 90.0},
        {"name": "Near", "x": 5.0, "y": 0.0, "length_mm": 1.6, "width_mm": 0.8, "rotation_deg": 0.0},
    ]
    assert find_overlapping_components(components) == []


def test_three_way_overlap_reports_every_pair():
    components = [
        {"name": "A", "x": 0.0, "y": 0.0, "length_mm": 2.0, "width_mm": 2.0, "rotation_deg": 0.0},
        {"name": "B", "x": 0.5, "y": 0.0, "length_mm": 2.0, "width_mm": 2.0, "rotation_deg": 0.0},
        {"name": "C", "x": 0.5, "y": 0.5, "length_mm": 2.0, "width_mm": 2.0, "rotation_deg": 0.0},
    ]
    warnings = find_overlapping_components(components)
    assert len(warnings) == 3  # A-B, A-C, B-C
