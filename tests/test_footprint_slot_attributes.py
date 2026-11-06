"""@brief Slots (oval holes), pad types and attribute flags in the footprint model (2026-11-06).

Positive and negative cases of the three additions: `Pad.drill_slot` written
as `(drill oval w h)`, `PinSpec.drill_length_mm` carried from the package
geometry to the written pad, and `Footprint.attributes` written after the
mounting type in `(attr ...)`.
"""
from dataclasses import asdict, replace

import pytest

from kicadgen.core.footprint import (FOOTPRINT_ATTRIBUTE_FLAGS, Footprint, Pad,
                                     normalised_attribute_flags)
from kicadgen.core.package_geometry import PackageGeometry, PinSpec, package_from_mapping
from kicadgen.generators.package import build_package
from kicadgen.gui.main_window import footprint_pad_records

THT = dict(pad_type="thru_hole", shape="oval", at=(0.0, 0.0), size=(2.0, 3.0), layers=["*.Cu", "*.Mask"])


# ── Pad ────────────────────────────────────────────────────────────────────

def test_a_slot_is_written_as_an_oval_drill_in_the_pad_axes():
    pad = Pad(number="1", drill_slot=(1.0, 2.0), rotation=45.0, **THT)
    text = pad.to_sexpr().render(0)
    assert "(drill oval 1 2)" in text and "(at 0 0 45)" in text
    assert pad.drill is None and pad.drill_slot == (1.0, 2.0)


def test_equal_slot_sides_are_a_round_drill():
    pad = Pad(number="1", drill_slot=(1.5, 1.5), **THT)
    assert pad.drill == 1.5 and pad.drill_slot is None
    assert "(drill 1.5)" in pad.to_sexpr().render(0)


def test_a_slot_satisfies_the_holed_type_requirement():
    Pad(number="", pad_type="np_thru_hole", shape="oval", at=(0, 0), size=(1, 1),
        layers=["*.Cu", "*.Mask"], drill_slot=(2.0, 3.0))  # a mechanical slot may exceed its mask opening


@pytest.mark.parametrize("bad", [
    dict(drill=1.0, drill_slot=(1.0, 2.0)),   # two holes
    dict(drill_slot=(2.0, 2.5)),              # first side at the pad width
    dict(drill_slot=(1.0, 3.0)),              # second side at the pad height
    dict(drill_slot=(0.0, 1.0)),              # a side of zero
    dict(drill_slot=(1.0, "x")),              # not a number
    dict(drill_slot=(1.0,)),                  # one side only
])
def test_an_invalid_slot_is_refused(bad):
    with pytest.raises(ValueError):
        Pad(number="1", **{**THT, **bad})


def test_a_slot_on_an_smd_pad_is_refused():
    with pytest.raises(ValueError):
        Pad(number="1", pad_type="smd", shape="rect", at=(0, 0), size=(2, 3), layers=["F.Cu"],
            drill_slot=(1.0, 2.0))


def test_the_pad_record_of_a_slot_holds_both_sides_like_the_read_back():
    fp = Footprint(name="T")
    fp.add_pad(Pad(number="1", drill_slot=(1.0, 2.0), **THT))
    fp.add_pad(Pad(number="2", drill=1.2, **{**THT, "at": (5.0, 0.0)}))
    records = footprint_pad_records(fp)
    assert records[0][7] == (1.0, 2.0) and records[1][7] == (1.2,)


# ── PinSpec and the package generator ──────────────────────────────────────

def _pin(**overrides):
    base = dict(number="1", x_mm=0.0, y_mm=0.0, pad_length_mm=2.0, pad_width_mm=3.0,
                pad_type="thru_hole", shape="oval", drill_mm=1.0)
    return PinSpec(**{**base, **overrides})


def test_a_pin_slot_validates_each_side_against_its_own_pad_side():
    _pin(drill_length_mm=2.9).validate()                   # 1.0 < 2.0 and 2.9 < 3.0
    with pytest.raises(ValueError):
        _pin(drill_length_mm=3.0).validate()               # second side at the pad height
    with pytest.raises(ValueError):
        _pin(drill_mm=2.0, drill_length_mm=2.5).validate()  # first side at the pad length
    with pytest.raises(ValueError):
        _pin(pad_type="smd", drill_mm=None, drill_length_mm=1.0).validate()


def test_a_round_hole_still_needs_copper_on_every_side():
    with pytest.raises(ValueError):
        _pin(drill_mm=2.0).validate()      # a round 2.0 hole in a 2.0 x 3.0 pad


def test_hole_sizes_and_is_slot():
    assert _pin().hole_sizes() == (1.0, 1.0) and not _pin().is_slot()
    assert _pin(drill_length_mm=2.0).hole_sizes() == (1.0, 2.0) and _pin(drill_length_mm=2.0).is_slot()
    assert _pin(drill_length_mm=1.0).hole_sizes() == (1.0, 1.0) and not _pin(drill_length_mm=1.0).is_slot()
    assert _pin(pad_type="smd", drill_mm=None).hole_sizes() is None


def test_the_slot_survives_the_mapping_round_trip_and_the_orientation():
    geometry = PackageGeometry(name="P", family="custom", body_length_mm=10, body_width_mm=10,
                               pins=[_pin(drill_length_mm=2.0), _pin(number="2", x_mm=5.0, shape="circle",
                                                                     pad_length_mm=2.0, pad_width_mm=2.0)])
    again = package_from_mapping(asdict(geometry))
    assert again.pins[0].drill_length_mm == 2.0 and again.pins[1].drill_length_mm is None
    turned = replace(geometry, orientation_deg=90)
    seen = turned.pins_seen_from_top()
    assert seen[0].drill_length_mm == 2.0 and seen[0].rotation_deg == 90.0


def test_a_reflection_keeps_the_slot_sides_and_negates_the_angle():
    geometry = PackageGeometry(name="P", family="custom", body_length_mm=10, body_width_mm=10,
                               source_view="bottom", pins=[_pin(drill_length_mm=2.0, rotation_deg=30.0)])
    seen = geometry.pins_seen_from_top()[0]
    assert (seen.drill_mm, seen.drill_length_mm, seen.rotation_deg) == (1.0, 2.0, 330.0)


def test_a_mechanical_hole_is_written_without_number_but_keeps_it_in_the_model():
    geometry = PackageGeometry(name="P", family="custom", body_length_mm=10, body_width_mm=10,
                               pins=[_pin(number="1", pad_type="np_thru_hole", name="MH"),
                                     _pin(number="2", x_mm=5.0)])
    text = build_package(geometry).render()
    assert '(pad "" np_thru_hole' in text and '(pad "2" thru_hole' in text
    assert geometry.pins[0].number == "1" and geometry.pins[0].name == "MH"


def test_a_slotted_module_pad_gives_round_header_lands():
    """generality audit 2026-11-06: the host header pins are round whatever the module's own hole."""
    from kicadgen.generators.host_mounting import header_footprint_params
    geometry = PackageGeometry(name="P", family="module", body_length_mm=10, body_width_mm=10,
                               pins=[_pin(drill_length_mm=2.0), _pin(number="2", x_mm=5.0, drill_length_mm=2.0)])
    host = header_footprint_params(geometry, 1.7, 1.0)
    assert all(pin.drill_mm == 1.0 and pin.drill_length_mm is None for pin in host.pins)
    assert "(drill oval" not in build_package(host).render()


def test_build_package_writes_the_slot_and_keeps_round_holes_round():
    geometry = PackageGeometry(name="P", family="custom", body_length_mm=10, body_width_mm=10,
                               pins=[_pin(drill_length_mm=2.0), _pin(number="2", x_mm=5.0, shape="circle",
                                                                     pad_length_mm=2.0, pad_width_mm=2.0)])
    text = build_package(geometry).render()
    assert "(drill oval 1 2)" in text and "(drill 1)" in text


# ── Footprint attributes ───────────────────────────────────────────────────

def test_attribute_flags_follow_the_mounting_type_in_kicad_order():
    fp = Footprint(name="T", attr="smd", attributes=("dnp", "exclude_from_bom"))
    assert "(attr smd exclude_from_bom dnp)" in fp.render()


def test_flags_without_a_mounting_type_are_still_written():
    fp = Footprint(name="T", attributes=["exclude_from_pos_files"])
    assert "(attr exclude_from_pos_files)" in fp.render()


def test_no_flags_keeps_the_previous_attr_line():
    assert "(attr through_hole)" in Footprint(name="T", attr="through_hole").render()
    assert "(attr" not in Footprint(name="T").render()


def test_an_unknown_flag_is_refused_before_writing():
    with pytest.raises(ValueError):
        Footprint(name="T", attr="smd", attributes=("bogus",)).render()
    assert normalised_attribute_flags(None) == ()
    assert normalised_attribute_flags(reversed(FOOTPRINT_ATTRIBUTE_FLAGS)) == FOOTPRINT_ATTRIBUTE_FLAGS
