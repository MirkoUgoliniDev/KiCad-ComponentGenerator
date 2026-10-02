"""@brief «Copia formato» of the mounted components (user, 2026-09-27)."""
from copy import deepcopy

import pytest

from kicadgen.services.component_format import FORMAT_FIELDS, copy_format, paste_format


def _sot(**changes):
    row = dict(name="Q1", kind="ic", **{"class": "semiconductor_unknown"}, package="SOT-23",
               x_mm=5.0, y_mm=-2.0, length_mm=2.9, width_mm=2.4, height_mm=1.1,
               rotation_deg=90.0, color_hex="#202020", marking="A15HB",
               pins_by_side={"left": 2, "right": 1}, pin_count=3,
               model_selection={"status": "confirmed", "path": "C:/kicad/SOT-23.step",
                                "rotation_deg": 90.0})
    row.update(changes)
    return row


def _generic(**changes):
    row = dict(name="C23", kind="generic", **{"class": "unknown"}, package="",
               x_mm=-8.0, y_mm=4.0, length_mm=4.0, width_mm=1.0, height_mm=0.5,
               rotation_deg=0.0, color_hex="#FF00FF", marking="2Aa")
    row.update(changes)
    return row


def test_paste_copies_shape_type_and_model_and_keeps_place_and_identity():
    source, target = _sot(), _generic()
    before = deepcopy(target)
    result = paste_format(target, copy_format(source))
    assert target == before                                    # the input is not modified
    for key in ("length_mm", "width_mm", "height_mm", "kind", "class", "package",
                "pin_count", "model_selection"):
        assert result[key] == source[key], key
    for key in ("x_mm", "y_mm", "rotation_deg", "name", "marking", "color_hex"):
        assert result[key] == target[key], key                 # place, identity, colour
    assert result["_format_copied_from"] == "Q1"


def test_an_automatic_model_becomes_a_confirmed_choice():
    source = _sot(model_selection=None,
                  model_match={"status": "auto_matched", "selected_path": "C:/kicad/SOT-23.step"})
    selection = paste_format(_generic(), copy_format(source))["model_selection"]
    assert selection == {"status": "confirmed", "path": "C:/kicad/SOT-23.step",
                         "rotation_deg": None}


def test_what_the_source_lacks_is_removed_not_mixed():
    """Negative: the target becomes the same body, not half of each."""
    source = {k: v for k, v in _sot().items()
              if k not in ("package", "model_selection", "pins_by_side", "pin_count")}
    target = _generic(package="0805", pins_by_side={"left": 1},
                      model_selection={"status": "confirmed", "path": "x.step"})
    result = paste_format(target, copy_format(source))
    assert "package" not in result and "model_selection" not in result
    assert "pins_by_side" not in result and "pin_count" not in result


@pytest.mark.parametrize("source_rotation,target_rotation,expected", [
    (90.0, 0.0, {"bottom": 2, "top": 1}),     # vertical -> horizontal: leads on the long sides
    (90.0, 90.0, {"left": 2, "right": 1}),    # same orientation: same photo sides
    (0.0, 0.0, {"left": 2, "right": 1}),
])
def test_observed_leads_follow_the_target_orientation(source_rotation, target_rotation, expected):
    source = _sot(rotation_deg=source_rotation)
    result = paste_format(_generic(rotation_deg=target_rotation), copy_format(source))
    assert result["pins_by_side"] == expected
    assert result["rotation_deg"] == target_rotation


def test_the_copied_fields_are_the_declared_ones():
    fmt = copy_format(_sot())
    assert set(fmt) <= set(FORMAT_FIELDS) | {"model_selection", "lead_sides_local", "source_name"}
    assert not set(fmt) & {"x_mm", "y_mm", "rotation_deg", "name", "marking", "color_hex"}


def test_pasting_a_format_withdraws_the_target_network_identity():
    """@brief Audit 2026-10-02: a SOIC pasted on a body read as chip_R kept the chip's pose.

    @note Negative: the source keeps its own identity, the target's other
        fields (position, rotation) stay untouched.
    """
    from kicadgen.services.component_format import copy_format, paste_format
    source = dict(kind="ic", package="SOIC-8", length_mm=6.0, width_mm=4.9, x_mm=0, y_mm=0,
                  rotation_deg=0, _smd_accepted=True, _smd_rotation_deg=180,
                  _smd_rotation_kind="full", _smd_views_agree=True)
    target = dict(kind="resistor", package="SMD chip resistor", length_mm=2.0, width_mm=1.2,
                  x_mm=3.0, y_mm=1.0, rotation_deg=90, _smd_accepted=True, _smd_body="endcap_chip",
                  _smd_rotation_deg=90, _smd_rotation_kind="axis", _smd_prediction={"class": "chip_R"})
    pasted = paste_format(target, copy_format(source))
    assert "_smd_accepted" not in pasted and "_smd_body" not in pasted
    assert "_smd_rotation_deg" not in pasted and pasted["_smd_superseded"] is True
    assert (pasted["x_mm"], pasted["rotation_deg"]) == (3.0, 90)
    assert source["_smd_accepted"] is True and target["_smd_accepted"] is True
