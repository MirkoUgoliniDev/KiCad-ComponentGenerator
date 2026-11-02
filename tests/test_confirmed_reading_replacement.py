"""@brief A confirmed pad reading replaces a numbering that carries no information; unknown views of chiral layouts block.

User run of 2026-10-31 (T7N relay, Opus 4.1 main model): the main answer
was a grid numbered 1..5 by convention, the dedicated reading read the
printed names 11, 12, 14, A1, A2 and the drawn holes confirmed it, yet the
generation was blocked. A second run lost the «bottom view» caption in a
crop and the footprint came out mirrored with no warning.
No network: the model calls and the geometry witness are stubbed, the
end-to-end replay uses a real datasheet crop.
"""
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from kicadgen.services import datasheet_extractor as ext

#: The T7N layout as the dedicated chains sum it (page orientation, mm).
T7N = {"11": (0.0, 6.0), "A1": (2.0, 0.0), "A2": (2.0, 12.0),
       "14": (14.2, 0.0), "12": (14.2, 12.0)}


def _verdict(status, reason):
    return SimpleNamespace(status=status, reason=reason, image="x", max_error_mm=0.05,
                           tolerance_mm=0.3, circles=5)


def _witness(*confirmed):
    """check_layout stub: confirms exactly the given layouts, «no_match» for anything else."""
    def check(pads, body):
        if any(pads == dict(layout) for layout in confirmed):
            return _verdict("confirmed", "confirmed")
        return _verdict("not_confirmed", "no_match")
    return check


def _pin(number, x, y, **extra):
    return dict({"number": number, "x_mm": x, "y_mm": y, "pad_type": "thru_hole",
                 "drill_mm": 1.3, "pad_length_mm": 2.2, "pad_width_mm": 2.2, "shape": "circle"},
                **extra)


def _data(pins, *, convention=False, view="bottom"):
    data = {"drawing_unit": "mm", "contacts_view": view,
            "package_geometry": {"family": "unknown", "origin": "pin1", "source_view": "top",
                                 "pins": pins, "body_length_mm": 22.5, "body_width_mm": 16.5}}
    if convention:
        data["_numbers_by_convention"] = True
    return data


def _reading(pads, view="bottom", unplaced=()):
    return {"view": view, "pads": dict(pads), "notes": "", "problems": [], "malformed": [],
            "span_quotes": {}, "unplaced": list(unplaced),
            "spans": {"x": None, "y": None, "edges": {}}}


#: A 2x3 grid numbered column by column, 5 positions populated: the Opus answer's shape.
GRID = [_pin("1", 0, 0), _pin("2", 0, 7), _pin("3", 7, 0), _pin("4", 7, 7), _pin("5", 14, 0)]


def _numbers(data):
    return sorted(pin["number"] for pin in data["package_geometry"]["pins"])


# -- TASK 1: replacement ----------------------------------------------------------------

def test_a_conventional_numbering_is_replaced_by_the_confirmed_reading():
    data = _data([dict(p) for p in GRID], convention=True)
    ext.apply_dedicated_pads(data, _reading(T7N), check_layout=_witness(T7N))
    assert "_pin_map_conflict" not in data
    assert _numbers(data) == ["11", "12", "14", "A1", "A2"]
    pins = {p["number"]: p for p in data["package_geometry"]["pins"]}
    # Offsets are the reading's; attributes the main answer's uniform ones.
    assert pins["14"]["x_mm"] - pins["A1"]["x_mm"] == pytest.approx(12.2)
    assert all(p["drill_mm"] == 1.3 and p["shape"] == "circle" for p in pins.values())
    assert "sostituito" in data["_numbering_note"] and "convenzione" in data["_numbering_note"]
    assert data["package_geometry"]["needs_review"] is True


def test_real_numbers_of_another_scheme_are_not_replaced():
    """Generality audit S1: numbers READ by the main answer (1..5) against
    other read names: the holes see positions, not which numbering is right,
    so nobody decides (contrast: the same numbers by convention are replaced)."""
    data = _data([dict(p) for p in GRID])
    ext.apply_dedicated_pads(data, _reading(T7N), check_layout=_witness(T7N))
    assert "_pin_map_conflict" in data and _numbers(data) == ["1", "2", "3", "4", "5"]


def test_a_reading_with_fewer_pads_never_replaces_the_answer():
    """Audit H1: a 4-pad reading «confirmed» by a 5-hole drawing (extra holes
    are allowed) would drop a terminal of the 5-pad answer."""
    four = {k: v for k, v in T7N.items() if k != "11"}
    data = _data([dict(p) for p in GRID], convention=True)
    ext.apply_dedicated_pads(data, _reading(four), check_layout=_witness(four))
    assert "_pin_map_conflict" in data and len(data["package_geometry"]["pins"]) == 5


def test_real_numbers_that_are_a_subset_still_block():
    """D-B kept: a main answer with real printed numbers that lacks a pin the reading has."""
    four = [_pin(n, x, y) for n, (x, y) in T7N.items() if n != "11"]
    data = _data(four)
    ext.apply_dedicated_pads(data, _reading(T7N), check_layout=_witness(T7N))
    assert "_pin_map_conflict" in data and _numbers(data) == ["12", "14", "A1", "A2"]


def test_real_numbers_contradicted_on_shared_pads_are_not_a_replacement():
    """Same names, different places: the ordinary rules decide, not the replacement.

    Without a witness they block; with one confirming the dedicated reading
    the existing choice by the holes applies (_settle_by_holes), which keeps
    the shared numbers and never rewrites the pad list.
    """
    swapped = dict(T7N, **{"A1": T7N["14"], "14": T7N["A1"]})
    data = _data([_pin(n, x, y) for n, (x, y) in swapped.items()])
    ext.apply_dedicated_pads(data, _reading(T7N), check_layout=_witness())
    assert "_pin_map_conflict" in data
    pins = {p["number"]: (p["x_mm"], p["y_mm"]) for p in data["package_geometry"]["pins"]}
    assert pins["A1"] == T7N["14"]
    data = _data([_pin(n, x, y) for n, (x, y) in swapped.items()])
    ext.apply_dedicated_pads(data, _reading(T7N), check_layout=_witness(T7N))
    assert "sostituito" not in data["_numbering_note"]


def test_two_confirmed_readings_are_never_a_replacement():
    main = {p["number"]: (p["x_mm"], p["y_mm"]) for p in GRID}
    data = _data([dict(p) for p in GRID], convention=True)
    ext.apply_dedicated_pads(data, _reading(T7N), check_layout=_witness(T7N, main))
    assert "_pin_map_conflict" in data and _numbers(data) == ["1", "2", "3", "4", "5"]


def _shape_witness(layout):
    """check_layout stub that sees geometry only: confirms any numbering of `layout`'s point set."""
    def check(pads, body):
        same = ext._same_point_set(dict(pads), dict(layout))
        return _verdict("confirmed", "confirmed") if same else _verdict("not_confirmed", "no_match")
    return check


def test_convention_numbers_on_the_same_layout_take_the_names_that_were_read():
    """The likeliest real case: the main answer has the RIGHT grid with numbers
    1..5 by convention, the dedicated reading the same positions with the
    printed names; the holes confirm both because they are one layout."""
    conventional = [_pin(str(i), x + 3.0, y + 1.0) for i, (x, y) in enumerate(T7N.values(), 1)]
    data = _data(conventional, convention=True)
    ext.apply_dedicated_pads(data, _reading(T7N), check_layout=_shape_witness(T7N))
    assert "_pin_map_conflict" not in data
    assert _numbers(data) == ["11", "12", "14", "A1", "A2"]


def test_read_numbers_of_another_scheme_on_the_same_layout_still_block():
    """Negative: numbers the main answer READ (not a convention) on the same
    positions are a second numbering, not an invented one: nobody decides."""
    printed = [_pin(str(i), x + 3.0, y + 1.0) for i, (x, y) in enumerate(T7N.values(), 1)]
    data = _data(printed)
    ext.apply_dedicated_pads(data, _reading(T7N), check_layout=_shape_witness(T7N))
    assert "_pin_map_conflict" in data and _numbers(data) == ["1", "2", "3", "4", "5"]


def test_an_unconfirmed_reading_is_never_a_replacement():
    data = _data([dict(p) for p in GRID], convention=True)
    ext.apply_dedicated_pads(data, _reading(T7N), check_layout=_witness())
    assert "_pin_map_conflict" in data and _numbers(data) == ["1", "2", "3", "4", "5"]


def test_a_mirror_fitting_as_well_is_never_a_replacement():
    mirrored = {n: (-x, y) for n, (x, y) in T7N.items()}
    data = _data([dict(p) for p in GRID], convention=True)
    ext.apply_dedicated_pads(data, _reading(T7N), check_layout=_witness(T7N, mirrored))
    assert "_pin_map_conflict" in data and _numbers(data) == ["1", "2", "3", "4", "5"]


def test_declared_views_that_differ_are_never_a_replacement():
    data = _data([dict(p) for p in GRID], convention=True, view="top")
    ext.apply_dedicated_pads(data, _reading(T7N, view="bottom"), check_layout=_witness(T7N))
    assert "_pin_map_conflict" in data and _numbers(data) == ["1", "2", "3", "4", "5"]


def test_a_reading_with_unplaced_pads_is_never_a_replacement():
    data = _data([dict(p) for p in GRID], convention=True)
    ext.apply_dedicated_pads(data, _reading(T7N, unplaced=["13"]), check_layout=_witness(T7N))
    assert "_pin_map_conflict" in data and _numbers(data) == ["1", "2", "3", "4", "5"]


def test_non_uniform_pad_attributes_block_with_their_names():
    pins = [dict(p) for p in GRID]
    pins[0]["drill_mm"] = 1.0
    data = _data(pins, convention=True)
    ext.apply_dedicated_pads(data, _reading(T7N), check_layout=_witness(T7N))
    assert "_pin_map_conflict" in data and "drill_mm" in data["_pin_map_conflict"]
    assert _numbers(data) == ["1", "2", "3", "4", "5"]


def test_the_grid_flags_a_conventional_numbering_and_not_a_read_pin_map():
    grid = {"rows": 2, "cols": 2, "pitch_mm": 2.54, "row_spacing_mm": 2.54,
            "pad_diameter_mm": 1.7, "drill_mm": 1.0}
    answer = {"drawing_unit": "mm", "contacts_view": "top", "confidence": "low", "grid": grid}
    assert ext.single_answer_to_data(answer).get("_numbers_by_convention") is True
    read = dict(grid, pin_map=[{"pin": 1, "row": 0, "col": 0}, {"pin": 2, "row": 1, "col": 0},
                               {"pin": 3, "row": 0, "col": 1}, {"pin": 4, "row": 1, "col": 1}])
    assert "_numbers_by_convention" not in ext.single_answer_to_data(dict(answer, grid=read))


def _crop():
    return Path(__file__).parent / "data" / "drawings" / "t7n_drawing_crop.png"


def _replay(monkeypatch, tmp_path, main_view, dedicated_view):
    image = tmp_path / "Pinout.png"
    shutil.copy(_crop(), image)
    monkeypatch.setattr(ext, "api_key", lambda: "offline")
    monkeypatch.setattr(ext, "check_credit", lambda **kwargs: None)
    main = dict(drawing_unit="mm", contacts_view=main_view, confidence="medium",
                body_length_mm=22.5, body_width_mm=16.5, body_height_mm=16.9,
                lead_exit="bottom", value_sources={},
                grid={"rows": 2, "cols": 3, "pitch_mm": 7.0, "row_spacing_mm": 7.0,
                      "pad_diameter_mm": 2.2, "drill_mm": 1.3, "populated_pins": [1, 2, 3, 4, 5]})
    dedicated = {"drawing_unit": "mm", "contacts_view": dedicated_view, "chains": [
        {"axis": "x", "stops": ["edge", "11", "A1,A2", "14,12"], "values": [2.05, 2.0, 12.2]},
        {"axis": "y", "stops": ["A1,14", "11", "A2,12"], "values": [6.0, 6.0]}]}
    monkeypatch.setattr(ext, "call_tool", Mock(side_effect=[main, dedicated]))
    return ext.extract_pin_header_params([image], component_family="single_tht",
                                         document_roles={"Pinout.png": ["pinout"]})


def test_end_to_end_the_opus_grid_is_replaced_by_the_names_the_holes_confirm(monkeypatch,
                                                                              tmp_path):
    """Offline replay of the user's Opus 4.1 run on the clean T7N crop."""
    params = _replay(monkeypatch, tmp_path, "unknown", "bottom")
    assert not params.pin_map_conflict
    pins = params.package_geometry["pins"]
    assert sorted(p["number"] for p in pins) == ["11", "12", "14", "A1", "A2"]
    assert all(p["drill_mm"] == pytest.approx(1.3) for p in pins)
    assert "sostituito" in params.notes


def test_end_to_end_an_unknown_view_in_both_readings_blocks_the_chiral_t7n(monkeypatch,
                                                                         tmp_path):
    """Negative of the above: same drawing, no reading declares the view."""
    params = _replay(monkeypatch, tmp_path, "unknown", "unknown")
    assert params.pin_map_conflict and "SPECCHIATO" in params.pin_map_conflict


# -- TASK 2: an unknown view of a chiral layout -----------------------------------------

def _pads(layout):
    return [(n, p) for n, p in layout.items()]


def test_a_symmetric_point_set_with_distinct_numbers_is_not_mirror_equivalent():
    """The T7N point set is mirror symmetric, but its mirror swaps A1/A2 and 12/14."""
    assert ext._same_up_to_quarter_turns(list(T7N.values()),
                                         [(-x, y) for x, y in T7N.values()])
    assert not ext._mirror_equivalent(_pads(T7N), 0.1)


def test_collinear_pads_and_symmetric_shared_numbers_are_mirror_equivalent():
    assert ext._mirror_equivalent(_pads({"1": (0, 0), "2": (2.54, 0), "3": (5.08, 0)}), 0.1)
    assert ext._mirror_equivalent(_pads({"1": (0, 0), "2": (3, 4)}), 0.1)
    # Two shield tabs sharing a number, symmetric about the line of pins 1-2.
    assert ext._mirror_equivalent([("1", (0, 0)), ("2", (2, 0)), ("S", (1, 3)), ("S", (1, -3))],
                                  0.1)
    assert not ext._mirror_equivalent([("1", (0, 0)), ("2", (2, 0)), ("S", (1, 3)),
                                       ("T", (1, -3))], 0.1)


def _single(layout, view):
    return _data([_pin(n, x, y) for n, (x, y) in layout.items()], view=view)


def test_an_unknown_view_of_a_chiral_layout_blocks():
    data = _single(T7N, "unknown")
    ext._require_declared_view(data)
    assert "SPECCHIATO" in data["_pin_map_conflict"]


@pytest.mark.parametrize("view", ["top", "bottom"])
def test_a_declared_view_of_a_chiral_layout_does_not_block(view):
    data = _single(T7N, view)
    ext._require_declared_view(data)
    assert "_pin_map_conflict" not in data


def test_an_unknown_view_of_a_mirror_equivalent_layout_only_notes():
    data = _single({"1": (0, 0), "2": (2.54, 0), "3": (5.08, 0)}, "unknown")
    ext._require_declared_view(data)
    assert "_pin_map_conflict" not in data and "speculare" in data["notes"]


def test_an_existing_block_keeps_its_reason_and_adds_the_view():
    data = _single(T7N, "unknown")
    data["_pin_map_conflict"] = "earlier reason"
    ext._require_declared_view(data)
    assert data["_pin_map_conflict"].startswith("earlier reason") \
        and "SPECCHIATO" in data["_pin_map_conflict"]
