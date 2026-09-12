"""Test dei colori AP214 iniettati nello STEP e dei tipi di componente.

`Part.export` di FreeCAD scrive la sola geometria: e' colorize_step() a
rendere i colori visibili in KiCad. Qui lo si collauda su uno STEP
sintetico, senza FreeCAD.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from kicadgen.cad3d.step_generator import colorize_step
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
