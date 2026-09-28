"""@brief Only a MEASURED half-hole diameter reaches the count, and only for width (HANDOFF point 140).

Real run of 2026-09-28 17:22 on the RS485: the model ESTIMATED the half-hole
diameters (1.7-2.4 mm; the log lists them among the «Valori STIMATI»), the
count turned them into a minimum notch depth, and both sides of a photo that
clearly shows 3 and 4 half-holes became «not countable».
"""
from pathlib import Path

import pytest

from kicadgen.services.edge_notches import check_declared_contacts, normalized_sides
from kicadgen.services.photo_inventory import prepare_photo

RS485 = Path(__file__).resolve().parents[1] / "docs" / "RS485" / "AltaRisoluzione.jpg"


def _sides(diameter, source):
    extra = {"half_hole_diameter_mm": diameter}
    if source is not None:
        extra["value_sources"] = {"half_hole_diameter_mm": source}
    return [dict(edge=edge, count=count, pitch_mm=pitch, contact_style="half_hole", **extra)
            for edge, count, pitch in (("left", 3, 5.08), ("right", 4, 2.54))]


@pytest.fixture(scope="module")
def rs485():
    if not RS485.is_file():
        pytest.skip("sample photo not available")
    meta = {}
    crop = prepare_photo(RS485, 42.8, 15.2, meta)
    return meta["_edge_depth_px"], crop.shape


@pytest.mark.parametrize("diameter", [1.7, 2.0, 2.4, 3.0])
@pytest.mark.parametrize("source", ["estimated", "undeclared", None])
def test_an_estimated_diameter_never_hides_the_real_notches(rs485, diameter, source):
    profiles, shape = rs485
    lines = check_declared_contacts(profiles, shape, 42.8, 15.2, _sides(diameter, source))
    assert [message for _kind, message in lines] == [
        "Lato sinistro: sulla foto si contano 3 semi-fori, come i contatti dichiarati.",
        "Lato destro: sulla foto si contano 4 semi-fori, come i contatti dichiarati."]


def test_a_read_diameter_that_fits_the_notches_still_counts(rs485):
    profiles, shape = rs485
    lines = check_declared_contacts(profiles, shape, 42.8, 15.2, _sides(1.6, "read"))
    assert all("come i contatti dichiarati" in message for _kind, message in lines)


def test_a_read_diameter_narrower_than_the_notches_makes_them_not_countable(rs485):
    """Negative: a measured hole narrower than the notches contradicts them."""
    profiles, shape = rs485
    lines = check_declared_contacts(profiles, shape, 42.8, 15.2, _sides(0.5, "read"))
    assert all(kind == "log" and "non contabili" in message and "larghe" in message
               for kind, message in lines), lines


@pytest.mark.parametrize("entry, expected", [
    ({"half_hole_diameter_mm": 1.6, "value_sources": {"half_hole_diameter_mm": "read"}}, 1.6),
    ({"half_hole_diameter_mm": 1.6, "value_sources": {"half_hole_diameter_mm": "derived"}}, 1.6),
    ({"half_hole_diameter_mm": 1.6, "half_hole_diameter_declared": True}, 1.6),
    ({"half_hole_diameter_mm": 1.6, "value_sources": {"half_hole_diameter_mm": "estimated"}}, None),
    ({"half_hole_diameter_mm": 1.6, "half_hole_diameter_declared": False}, None),
    ({"half_hole_diameter_mm": 1.6}, None),
])
def test_only_a_measured_diameter_is_kept(entry, expected):
    side = dict(edge="left", count=3, pitch_mm=5.08, contact_style="half_hole", **entry)
    assert normalized_sides([side])["left"]["hole_diameter_mm"] == expected
