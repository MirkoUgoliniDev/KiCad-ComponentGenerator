"""@brief Property and drawn texts are measured as KiCad shows them: unescaped (2026-10-17).

KiCad shows «{slash}» in a symbol/footprint text or property as «/»
(kicad-cli 10, agent B's measurements); pin names and numbers keep it.
"""
from kicadgen.core.footprint import Footprint, property_corner_offset
from kicadgen.generators.symbol import property_texts


def test_symbol_properties_are_shown_unescaped():
    assert property_texts("U", "N{slash}A") == {"Reference": "U?", "Value": "N/A"}


def test_footprint_properties_are_shown_unescaped_and_anchored_on_the_shown_width():
    fp = Footprint(name="RS{slash}485")
    assert fp.property_texts()["Value"] == "RS/485"
    fp.place_default_texts()
    shown = Footprint(name="RS/485").place_default_texts()
    assert fp.text_anchors()["Value"] == shown.text_anchors()["Value"]


def test_a_plain_name_is_unchanged():
    """@brief Negative: no escape, nothing changes (and a lone brace stays literal)."""
    assert property_texts("U", "ESP32{") == {"Reference": "U?", "Value": "ESP32{"}
    assert property_corner_offset("REF**") == property_corner_offset(Footprint(name="X").property_texts()["Reference"])


def test_the_preview_draws_the_unescaped_value(window):
    window.field_name.setText("N{slash}A")
    assert window._footprint_property_texts()["Value"] == "N/A"
