"""@brief Drawn holes and KiCad library footprints as two geometry witnesses of one layout.

Generality audit of 2026-10-31 on the part-number feature: the library must
never override the drawing (C2), and whether the side of a layout can be told
must not depend on how the drawing is turned on the page (C3).
No network, no KiCad install, no image: both witnesses are stubbed.
"""
from types import SimpleNamespace

import pytest

from kicadgen.services import datasheet_extractor as ext
from kicadgen.services import drawing_holes


def _verdict(status, reason, source=None):
    fields = dict(status=status, reason=reason, image="x", max_error_mm=0.0, tolerance_mm=0.1,
                  circles=5)
    if source:
        fields["source"] = source
    return SimpleNamespace(**fields)


@pytest.fixture
def witnesses(monkeypatch):
    """Set what the drawn holes and the library say, whatever the layout."""
    said = {}
    monkeypatch.setattr(drawing_holes, "check_pad_layout", lambda images, pads, body: said["holes"])
    from kicadgen.services import library_footprints
    monkeypatch.setattr(library_footprints, "check_layout",
                        lambda pads, view, footprints, tolerance: said["library"])
    return said


PADS = {"1": (0.0, 0.0), "2": (5.0, 0.0), "3": (0.0, 7.0)}


def _check(said, holes, library, documents=("drawing.png",)):
    said["holes"], said["library"] = holes, library
    checker = ext._layout_checker(list(documents), ["footprint"], lambda: "bottom")
    return checker(PADS, None)


def test_holes_saying_no_and_the_library_saying_yes_is_a_conflict(witnesses):
    verdict = _check(witnesses, _verdict("not_confirmed", "no_match"),
                     _verdict("confirmed", "confirmed", "library"))
    assert isinstance(verdict, ext._WitnessConflict)
    assert not ext._holes_confirm(verdict) and verdict.reason != "no_match"
    assert "si contraddicono" in ext._hole_text(verdict)
    # And the other way round.
    verdict = _check(witnesses, _verdict("confirmed", "confirmed"),
                     _verdict("not_confirmed", "no_match", "library"))
    assert isinstance(verdict, ext._WitnessConflict)


@pytest.mark.parametrize("reason", ["distorted", "body_scale"])
def test_a_library_no_never_replaces_a_drawing_that_may_still_be_right(witnesses, reason):
    holes = _verdict("not_confirmed", reason)
    verdict = _check(witnesses, holes, _verdict("not_confirmed", "no_match", "library"))
    assert verdict is holes


@pytest.mark.parametrize("holes", [None, _verdict("unverifiable", "too_few_circles"),
                                   _verdict("unverifiable", "not_a_drawing")])
def test_a_drawing_that_says_nothing_leaves_the_library_verdict(witnesses, holes):
    """Contrast with the case above: no evidence from the drawing is not «may be right»."""
    library = _verdict("not_confirmed", "no_match", "library")
    assert _check(witnesses, holes, library, documents=() if holes is None else ("d.png",)) \
        is library


def test_one_confirming_witness_and_no_contrary_evidence_confirms(witnesses):
    library = _verdict("confirmed", "confirmed", "library")
    assert _check(witnesses, _verdict("unverifiable", "too_few_circles"), library) is library


# -- C3: the side question in the library's own transformation group ------------------

#: Two rows of pins with one pin between them on the axis: mirror symmetric.
SYMMETRIC = {"11": (0.0, 6.0), "A1": (2.0, 0.0), "A2": (2.0, 12.0),
             "14": (14.2, 0.0), "12": (14.2, 12.0)}
CHIRAL = dict(SYMMETRIC, **{"11": (0.0, 4.0)})


def _turned(pads):
    return {n: (-y, x) for n, (x, y) in pads.items()}


class _Library:
    """Library witness stub: confirms exactly the given layouts (as point sets up to quarter turns)."""

    def __init__(self, *layouts):
        self.layouts = [list(layout.values()) for layout in layouts]
        self.views, self.views_differ, self.discarded = {}, False, []

    def __call__(self, pads):
        fits = any(ext._same_up_to_quarter_turns(list(pads.values()), layout)
                   for layout in self.layouts)
        return _verdict("confirmed", "confirmed", "library") if fits else \
            _verdict("not_confirmed", "no_match", "library")


@pytest.mark.parametrize("layout", [SYMMETRIC, _turned(SYMMETRIC)])
def test_a_mirror_symmetric_layout_is_never_settled_by_the_library_in_any_orientation(layout):
    holes = _Library(layout)
    assert ext._pick_by_holes([("main", layout)], holes) is None


def _real_t7n_page():
    """The T7N datasheet page of the user's run of 17:00 (fixture, user 2026-10-31)."""
    from pathlib import Path
    return Path(__file__).parent / "data" / "drawings" / "t7n_datasheet_page.png"


def test_end_to_end_positions_measured_on_the_holes_numbered_by_the_reading(monkeypatch,
                                                                            tmp_path):
    """User run of 2026-10-31, 17:00, replayed offline: the main answer was
    wrong everywhere, the dedicated reading right but for pin 11 (2 mm on the
    wrong side of the coil column). The drawn holes give the positions, the
    reading the numbers; pin 11 goes onto the only free hole."""
    import shutil
    from unittest.mock import Mock
    image = tmp_path / "Pinout.png"
    shutil.copy(_real_t7n_page(), image)
    monkeypatch.setattr(ext, "api_key", lambda: "offline")
    monkeypatch.setattr(ext, "check_credit", lambda **kwargs: None)
    main = dict(drawing_unit="mm", contacts_view="bottom", confidence="medium",
                body_length_mm=22.5, body_width_mm=16.5, body_height_mm=16.9,
                lead_exit="bottom", pads_origin="custom", value_sources={},
                pads=[{"number": n, "x_mm": x, "y_mm": y, "pad_type": "thru_hole",
                       "drill_mm": 1.3, "pad_length_mm": 2.2, "pad_width_mm": 2.2}
                      for n, (x, y) in {"11": (14.25, 0), "12": (14.25, 6), "14": (14.25, 12),
                                        "A1": (0, 0), "A2": (0, 6)}.items()])
    dedicated = {"drawing_unit": "mm", "contacts_view": "bottom", "chains": [
        {"axis": "x", "stops": ["edge", "A1,A2", "11", "14,12"], "values": [2.05, 2.0, 10.2]},
        {"axis": "y", "stops": ["A1,14", "11", "A2,12"], "values": [6.0, 6.0]}]}
    monkeypatch.setattr(ext, "call_tool", Mock(side_effect=[main, dedicated, {"drawing_unit": "mm"}]))
    params = ext.extract_pin_header_params([image], component_family="single_tht",
                                           document_roles={"Pinout.png": ["pinout"]})
    assert not params.pin_map_conflict
    pads = {p["number"]: (p["x_mm"], p["y_mm"]) for p in params.package_geometry["pins"]}
    # Drawing: 11 is 2.0 mm LEFT of the A1/A2 column, half way between the rows.
    assert pads["A1"][0] - pads["11"][0] == pytest.approx(2.0, abs=0.2)
    # Y was read right («6,0» printed): kept, not replaced by a pixel estimate
    # (offsets: the body may be placed on the drawn outline, moving the origin).
    assert pads["11"][1] - pads["A1"][1] == pytest.approx(6.0, abs=1e-6)
    assert (pads["14"][0] - pads["A1"][0], pads["A2"][1] - pads["A1"][1]) == \
        pytest.approx((12.2, 12.0))
    assert "misurate sui fori" in params.notes and "pad 11" in params.notes


def _crop():
    from pathlib import Path
    return Path(__file__).parent / "data" / "drawings" / "t7n_drawing_crop.png"


def test_the_clean_crop_confirms_the_true_layout_and_refuses_a_wrong_one():
    true = {"11": (2.05, 6.0), "A1": (4.05, 0.0), "A2": (4.05, 12.0),
            "14": (16.25, 0.0), "12": (16.25, 12.0)}
    assert drawing_holes.check_pad_layout([_crop()], true, (22.5, 16.5)).status == "confirmed"
    # Opus 4.1 main answer of the user's run of 17:46: right names, wrong places.
    wrong = {"11": (0, 0), "12": (12, 0), "14": (12, 8), "A1": (0, 14), "A2": (12, 14)}
    assert drawing_holes.check_pad_layout([_crop()], wrong, (22.5, 16.5)).reason == "no_match"


def test_end_to_end_the_last_user_run_places_the_body_from_the_edge_quote(monkeypatch, tmp_path):
    """User run of 2026-10-31, 17:46 (Opus 4.1 + gpt-5.6-sol on the clean
    crop), replayed offline: the dedicated reading is chosen by the holes and
    the body is placed by the 2.05 mm edge quote, not centred on the pins."""
    import shutil
    from unittest.mock import Mock
    from kicadgen.core.package_geometry import package_from_mapping
    image = tmp_path / "Pinout.png"
    shutil.copy(_crop(), image)
    monkeypatch.setattr(ext, "api_key", lambda: "offline")
    monkeypatch.setattr(ext, "check_credit", lambda **kwargs: None)
    main = dict(drawing_unit="mm", contacts_view="bottom", confidence="high",
                body_length_mm=22.5, body_width_mm=16.5, body_height_mm=16.9,
                lead_exit="bottom", pads_origin="custom", value_sources={},
                pads=[{"number": n, "x_mm": x, "y_mm": y, "pad_type": "thru_hole",
                       "drill_mm": 1.3, "pad_length_mm": 2.2, "pad_width_mm": 2.2}
                      for n, (x, y) in {"11": (0, 0), "12": (12, 0), "14": (12, 8),
                                        "A1": (0, 14), "A2": (12, 14)}.items()])
    dedicated = {"drawing_unit": "mm", "contacts_view": "unknown", "chains": [
        {"axis": "x", "stops": ["edge", "11", "A1,A2", "14,12"], "values": [2.05, 2.0, 12.2]},
        {"axis": "y", "stops": ["A1,14", "11", "A2,12"], "values": [6.0, 6.0]}]}
    monkeypatch.setattr(ext, "call_tool", Mock(side_effect=[main, dedicated, {"drawing_unit": "mm"}]))
    params = ext.extract_pin_header_params([image], component_family="single_tht",
                                           document_roles={"Pinout.png": ["pinout"]})
    assert not params.pin_map_conflict
    geometry = package_from_mapping(dict(params.package_geometry, name="T7N"))
    assert geometry.origin == "body_center"
    top = {p.number: p.x_mm for p in geometry.pins_seen_from_top()}
    # Seen from above, 11 is 2.05 mm from the right edge of the 22.5 mm body,
    # the contacts 6.25 mm from the left one.
    assert 11.25 - top["11"] == pytest.approx(2.05)
    assert top["14"] + 11.25 == pytest.approx(6.25)


def test_two_pads_off_the_holes_are_not_moved(monkeypatch, tmp_path):
    """Negative: with two pads off, nothing anchors a unique move, the block stays."""
    import shutil
    from unittest.mock import Mock
    image = tmp_path / "Pinout.png"
    shutil.copy(_real_t7n_page(), image)
    monkeypatch.setattr(ext, "api_key", lambda: "offline")
    monkeypatch.setattr(ext, "check_credit", lambda **kwargs: None)
    main = dict(drawing_unit="mm", contacts_view="bottom", confidence="medium",
                body_length_mm=22.5, body_width_mm=16.5, lead_exit="bottom",
                pads_origin="custom", value_sources={},
                pads=[{"number": n, "x_mm": x, "y_mm": y, "pad_type": "thru_hole",
                       "drill_mm": 1.3, "pad_length_mm": 2.2, "pad_width_mm": 2.2}
                      for n, (x, y) in {"11": (14.25, 0), "12": (14.25, 6), "14": (14.25, 12),
                                        "A1": (0, 0), "A2": (0, 6)}.items()])
    dedicated = {"drawing_unit": "mm", "contacts_view": "bottom", "chains": [
        {"axis": "x", "stops": ["edge", "A1,A2", "11", "14", "12"],
         "values": [2.05, 2.0, 10.2, 3.0]},
        {"axis": "y", "stops": ["A1,14", "11", "A2,12"], "values": [6.0, 6.0]}]}
    monkeypatch.setattr(ext, "call_tool", Mock(side_effect=[main, dedicated, {"drawing_unit": "mm"}]))
    params = ext.extract_pin_header_params([image], component_family="single_tht",
                                           document_roles={"Pinout.png": ["pinout"]})
    assert params.pin_map_conflict


# -- measured positions never undo a refusal (final generality audit D-A, D-B) --------

class _Holes:
    """Drawn-hole witness stub: confirms the given layouts exactly; records measure calls."""

    def __init__(self, confirmed=(), measure=None):
        self.confirmed = [dict(layout) for layout in confirmed]
        self.views, self.views_differ, self.discarded = {}, False, []
        self.body = None
        self.measure = measure
        self.measured = []

    def __call__(self, pads):
        if any(pads == layout for layout in self.confirmed):
            return _verdict("confirmed", "confirmed")
        return _verdict("not_confirmed", "no_match")


def _pins(numbers):
    return [{"number": n, "x_mm": 0.0, "y_mm": 0.0} for n in numbers]


def _measure_spy(calls, result=None):
    def measure(pads, body):
        calls.append(dict(pads))
        return result
    return measure


def test_a_pad_only_another_reading_found_blocks_even_when_the_holes_fit(monkeypatch):
    """D-B: four correct pads confirmed by four of five holes, the dedicated
    reading has the fifth pin: the answer left a terminal out."""
    four = {k: v for k, v in CHIRAL.items() if k != "11"}
    calls = []
    holes = _Holes(confirmed=[four], measure=_measure_spy(calls))
    data = {"package_geometry": {"pins": _pins(four)}}
    readings = [("main", four, {}, False), ("dedicated", dict(CHIRAL, **{"11": (4.0, 4.0)}), {},
                                            False)]
    assert ext._settle_by_holes(data, data["package_geometry"]["pins"], readings, holes, 1.0,
                                None, "why", "") is None
    assert calls == []


def test_two_confirmed_readings_are_not_settled_by_measuring():
    """D-A: a refusal for ambiguity stands; nothing is measured."""
    other = dict(CHIRAL, **{"11": (0.0, 8.0)})
    calls = []
    holes = _Holes(confirmed=[CHIRAL, other], measure=_measure_spy(calls))
    data = {"package_geometry": {"pins": _pins(CHIRAL)}}
    readings = [("main", CHIRAL, {}, False), ("dedicated", other, {}, False)]
    assert ext._settle_by_holes(data, data["package_geometry"]["pins"], readings, holes, 1.0,
                                None, "why", "") is None
    assert calls == []


def test_a_reading_measured_whole_but_not_confirmed_is_not_adopted():
    """D-A: nothing moved means the reading already fits; it was refused for
    another reason (e.g. a witness conflict) and measuring must not undo it."""
    whole = SimpleNamespace(positions_mm=dict(CHIRAL), measured_mm=dict(CHIRAL), moved=None,
                            moved_from_mm=None, max_error_mm=0.1, tolerance_mm=0.5, image="x")
    calls = []
    holes = _Holes(confirmed=[], measure=_measure_spy(calls, whole))
    data = {"package_geometry": {"pins": _pins(CHIRAL)}}
    assert ext._settle_by_holes(data, data["package_geometry"]["pins"],
                                [("main", CHIRAL, {}, False)], holes, 1.0, None, "why", "") is None
    assert len(calls) == 1 and "_pin_map_conflict" not in data


@pytest.mark.parametrize("layout", [CHIRAL, _turned(CHIRAL)])
def test_a_chiral_layout_is_settled_by_the_library_in_every_orientation(layout):
    holes = _Library(layout)
    chosen = ext._pick_by_holes([("main", layout)], holes)
    assert chosen is not None and chosen[0] == "main"
