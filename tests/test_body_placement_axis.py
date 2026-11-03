"""@brief Body placed on the pads axis by axis from the outline quotes (user, 2026-10-31).

T7N relay: pin 11 is quoted 2.05 mm from the body edge on one side only, and
the body came out centred on the pins (11 at 4.2 mm from the edge). A quote
from ONE edge plus the body size places the body on that axis; an axis with
no outline quote stays centred on the pads and is named as an estimate.
"""
import pytest

from kicadgen.services import datasheet_extractor as ext


def _data(length=22.5, width=16.5):
    return {"notes": "", "package_geometry": {
        "body_length_mm": length, "body_width_mm": width, "origin": "custom",
        "pins": [{"number": n, "x_mm": 0.0, "y_mm": 0.0} for n in ("11", "A1", "A2", "14", "12")]}}


#: The drawing's chains: x from the LEFT outline (2.05 to pin 11, 2.0 to the
#: coil column, 12.2 to the contacts); y between the rows only.
T7N_CHAINS = [{"axis": "x", "stops": ["edge", "11", "A1,A2", "14,12"], "values": [2.05, 2.0, 12.2]},
              {"axis": "y", "stops": ["A1,14", "11", "A2,12"], "values": [6.0, 6.0]}]


def test_the_chains_say_where_the_quoted_outline_stops_are():
    solved = ext.pads_from_chains(T7N_CHAINS)
    assert solved["spans"]["edges"]["x"] == (0.0, None)
    assert solved["spans"]["edges"]["y"] == (None, None)
    assert solved["pads"]["11"][0] == pytest.approx(2.05)


def test_one_quoted_edge_places_the_body_on_that_axis_and_the_other_is_an_estimate():
    solved = ext.pads_from_chains(T7N_CHAINS)
    data = _data()
    ext._write_chain_pads(data, data["package_geometry"]["pins"],
                          {"pads": solved["pads"], "spans": solved["spans"], "centred": False}, 1.0)
    pads = {p["number"]: (p["x_mm"], p["y_mm"]) for p in data["package_geometry"]["pins"]}
    assert data["package_geometry"]["origin"] == "body_center"
    # Pin 11 is 2.05 mm from the left edge of a 22.5 mm body: x = 2.05 - 11.25.
    assert pads["11"][0] == pytest.approx(2.05 - 11.25)
    # The contacts are 22.5 - 2.05 - 14.2 = 6.25 mm from the other edge.
    assert 11.25 - pads["14"][0] == pytest.approx(6.25)
    # Y has no outline quote: the rows are centred, and the notes say so.
    assert pads["A1"][1] == pytest.approx(-6.0) and pads["A2"][1] == pytest.approx(6.0)
    assert "Lungo Y" in data["notes"] and "STIMA" in data["notes"]


def test_an_edge_quote_that_would_put_a_pad_outside_the_body_is_not_used():
    """Negative: a 10 mm body cannot hold pads 16.25 mm from its quoted edge."""
    solved = ext.pads_from_chains(T7N_CHAINS)
    data = _data(length=10.0)
    ext._write_chain_pads(data, data["package_geometry"]["pins"],
                          {"pads": solved["pads"], "spans": solved["spans"], "centred": False}, 1.0)
    assert data["package_geometry"]["origin"] == "custom"


def test_no_outline_quote_keeps_the_former_convention():
    chains = [{"axis": "x", "stops": ["11", "A1,A2", "14,12"], "values": [2.0, 12.2]},
              T7N_CHAINS[1]]
    solved = ext.pads_from_chains(chains)
    data = _data()
    ext._write_chain_pads(data, data["package_geometry"]["pins"],
                          {"pads": solved["pads"], "spans": solved["spans"], "centred": False}, 1.0)
    assert data["package_geometry"]["origin"] == "custom" and data["notes"] == ""


def test_the_drawn_outline_places_an_axis_without_quote_and_a_quote_stays_stronger():
    """Step 3: the outline measured on the drawing places Y (no edge quote
    there); X keeps the printed 2.05 mm edge quote even though the drawn
    outline (not to scale) says otherwise."""
    from types import SimpleNamespace
    solved = ext.pads_from_chains(T7N_CHAINS)
    data = _data()
    data["_outline_measurer"] = lambda pads, body: SimpleNamespace(
        left_mm=0.5, right_mm=22.0, top_mm=-3.0, bottom_mm=14.0)
    ext._write_chain_pads(data, data["package_geometry"]["pins"],
                          {"pads": solved["pads"], "spans": solved["spans"], "centred": False}, 1.0)
    pads = {p["number"]: (p["x_mm"], p["y_mm"]) for p in data["package_geometry"]["pins"]}
    assert pads["11"][0] == pytest.approx(2.05 - 11.25), "the quote wins on X"
    # Y centre of the drawn outline: (-3 + 14) / 2 = 5.5 -> rows at -5.5 and +6.5.
    assert pads["A1"][1] == pytest.approx(-5.5) and pads["A2"][1] == pytest.approx(6.5)
    assert "contorno disegnato" in data["notes"] and "STIMA" not in data["notes"]


def test_a_drawn_outline_that_does_not_hold_the_pads_is_not_used():
    from types import SimpleNamespace
    solved = ext.pads_from_chains(T7N_CHAINS)
    data = _data()
    data["_outline_measurer"] = lambda pads, body: SimpleNamespace(
        left_mm=0.5, right_mm=22.0, top_mm=1.0, bottom_mm=10.0)
    ext._write_chain_pads(data, data["package_geometry"]["pins"],
                          {"pads": solved["pads"], "spans": solved["spans"], "centred": False}, 1.0)
    assert "STIMA" in data["notes"]


def test_end_to_end_the_real_drawn_outline_places_x_when_no_edge_is_quoted(monkeypatch,
                                                                           tmp_path):
    """Real dashed outline of the T7N crop: with the X chain transcribed
    WITHOUT the edge stop, the body is placed by the drawn outline, pin 11
    about 2 mm from the edge (the drawing is slightly under scale)."""
    import shutil
    from pathlib import Path
    from unittest.mock import Mock
    image = tmp_path / "Pinout.png"
    shutil.copy(Path(__file__).parent / "data" / "drawings" / "t7n_drawing_crop.png", image)
    monkeypatch.setattr(ext, "api_key", lambda: "offline")
    monkeypatch.setattr(ext, "check_credit", lambda **kwargs: None)
    true = {"11": (2.05, 6.0), "A1": (4.05, 0.0), "A2": (4.05, 12.0),
            "14": (16.25, 0.0), "12": (16.25, 12.0)}
    main = dict(drawing_unit="mm", contacts_view="bottom", confidence="high",
                body_length_mm=22.5, body_width_mm=16.5, body_height_mm=16.9,
                lead_exit="bottom", pads_origin="custom", value_sources={},
                pads=[{"number": n, "x_mm": x, "y_mm": y, "pad_type": "thru_hole",
                       "drill_mm": 1.3, "pad_length_mm": 2.2, "pad_width_mm": 2.2}
                      for n, (x, y) in true.items()])
    dedicated = {"drawing_unit": "mm", "contacts_view": "bottom", "chains": [
        {"axis": "x", "stops": ["11", "A1,A2", "14,12"], "values": [2.0, 12.2]},
        {"axis": "y", "stops": ["A1,14", "11", "A2,12"], "values": [6.0, 6.0]}]}
    monkeypatch.setattr(ext, "call_tool", Mock(side_effect=[main, dedicated, {"drawing_unit": "mm"}]))
    params = ext.extract_pin_header_params([image], component_family="single_tht",
                                           document_roles={"Pinout.png": ["pinout"]})
    assert not params.pin_map_conflict
    pads = {p["number"]: p["x_mm"] for p in params.package_geometry["pins"]}
    assert params.package_geometry["origin"] == "body_center"
    # Bottom view, page frame: 11 near the LEFT edge of the drawing.
    assert pads["11"] + 11.25 == pytest.approx(2.05, abs=0.5)


def test_both_edges_quoted_on_one_axis_place_that_axis_even_without_the_other():
    """Audit H2: X quoted edge to edge (3 + 10 + 7 = 20), Y not quoted."""
    chains = [{"axis": "x", "stops": ["edge", "1,3", "2,4", "edge"], "values": [3.0, 10.0, 7.0]},
              {"axis": "y", "stops": ["1,2", "3,4"], "values": [6.0]}]
    solved = ext.pads_from_chains(chains)
    data = {"notes": "", "package_geometry": {"body_length_mm": 20.0, "body_width_mm": 10.0,
                                              "pins": [{"number": n} for n in "1234"]}}
    ext._write_chain_pads(data, data["package_geometry"]["pins"],
                          {"pads": solved["pads"], "spans": solved["spans"], "centred": False}, 1.0)
    pads = {p["number"]: (p["x_mm"], p["y_mm"]) for p in data["package_geometry"]["pins"]}
    assert data["package_geometry"]["origin"] == "body_center"
    assert pads["1"][0] == pytest.approx(3.0 - 10.0), "pad 1 is 3 mm from the left edge"
    assert "Lungo Y" in data["notes"]


def _agreeing(main_pads, dedicated_chains, body=(20.0, 10.0)):
    """Main answer centred on the body, and a dedicated reading agreeing on the pads."""
    data = {"notes": "", "drawing_unit": "mm", "contacts_view": "top", "package_geometry": {
        "family": "unknown", "origin": "body_center", "source_view": "top",
        "body_length_mm": body[0], "body_width_mm": body[1],
        "pins": [{"number": n, "x_mm": x, "y_mm": y, "pad_type": "thru_hole", "drill_mm": 1.0,
                  "pad_length_mm": 1.8, "pad_width_mm": 1.8} for n, (x, y) in main_pads.items()]}}
    solved = ext.pads_from_chains(dedicated_chains)
    reading = dict(solved, view="top", notes="")
    ext.apply_dedicated_pads(data, reading)
    return data


#: Pads not centred on Y in the main answer's body frame (y from -4 to 2).
FRAMED = {"1": (-5.0, -4.0), "2": (5.0, -4.0), "3": (-5.0, 2.0), "4": (5.0, 2.0)}


def test_a_declared_body_frame_is_kept_when_the_edge_quote_agrees():
    """Audit M1: the main frame (Y not centred on the pads) is not replaced by
    a pad-centre estimate; the X edge quote (5 mm, as the frame says) agrees."""
    data = _agreeing(FRAMED, [
        {"axis": "x", "stops": ["edge", "1,3", "2,4"], "values": [5.0, 10.0]},
        {"axis": "y", "stops": ["1,2", "3,4"], "values": [6.0]}])
    assert "_pin_map_conflict" not in data
    pads = {p["number"]: (p["x_mm"], p["y_mm"]) for p in data["package_geometry"]["pins"]}
    assert pads["1"] == (-5.0, -4.0) and pads["3"] == (-5.0, 2.0)


def test_an_edge_quote_contradicting_the_declared_frame_blocks():
    """Audit M2: the edge quote puts pad 1 at 3 mm from the edge, the main
    frame at 5 mm: a disagreement, never resolved silently."""
    data = _agreeing(FRAMED, [
        {"axis": "x", "stops": ["edge", "1,3", "2,4"], "values": [3.0, 10.0]},
        {"axis": "y", "stops": ["1,2", "3,4"], "values": [6.0]}])
    assert "_pin_map_conflict" in data and "posizione del corpo" in data["_pin_map_conflict"]


def test_both_edges_quoted_centre_the_body_between_them():
    chains = [{"axis": "x", "stops": ["edge", "1", "2", "edge"], "values": [2.0, 10.0, 4.0]},
              {"axis": "y", "stops": ["edge", "1,2", "edge"], "values": [3.0, 3.0]}]
    solved = ext.pads_from_chains(chains)
    data = {"notes": "", "package_geometry": {"body_length_mm": 16.0, "body_width_mm": 6.0,
                                              "pins": [{"number": "1"}, {"number": "2"}]}}
    ext._write_chain_pads(data, data["package_geometry"]["pins"],
                          {"pads": solved["pads"], "spans": solved["spans"], "centred": True}, 1.0)
    pads = {p["number"]: (p["x_mm"], p["y_mm"]) for p in data["package_geometry"]["pins"]}
    assert pads == {"1": pytest.approx((-6.0, 0.0)), "2": pytest.approx((4.0, 0.0))}
