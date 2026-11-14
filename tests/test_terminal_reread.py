"""@brief Terminals read once and the one targeted re-read before blocking pads (2026-11-13, SRD case).

The SRD-05VDC-SL-C relay drawing prints the terminals («2-Ø0.6», «2-1*0.4»,
«1.1 x 0.3») but no hole. The main answer declared no terminal; the
dedicated pad reading transcribed them. Now:
- a pad with no terminal of its own takes the one a dedicated reading
  transcribed, as a terminal READ ONCE (estimated), and its hole and land are
  derived by core.tht_estimates;
- pads still left with neither a hole nor a terminal get ONE paid re-read of
  the same drawing for only those values; the block says «riletto: nessuna
  quota…» only when that re-read was made and found nothing.
Offline: every call is faked by tool name, nothing reaches the network.
"""
import copy
from unittest.mock import Mock

import pytest
from PIL import Image

from kicadgen.services import datasheet_extractor as ext


@pytest.fixture
def offline(monkeypatch):
    """No key, no credit check, no network."""
    monkeypatch.setattr(ext, "api_key", lambda: "offline")
    monkeypatch.setattr(ext, "check_credit", lambda **kw: None)


def _drawing(tmp_path):
    path = tmp_path / "pinout.png"
    Image.new("RGB", (300, 200), "white").save(path)
    return path


#: The SRD main answer (2026-11-13 22:26), reduced: a 3 x 2 grid, no hole, no terminal.
MAIN = {"body_height_mm": 15.3, "body_length_mm": 19.1, "body_width_mm": 15.5,
        "confidence": "medium", "contacts_view": "top", "drawing_unit": "mm",
        "grid": {"cols": 2, "rows": 3, "pitch_mm": 12.2, "row_spacing_mm": 6.1,
                 "pad_diameter_mm": 2.0,
                 "pin_map": [{"col": 0, "pin": "1", "row": 0}, {"col": 1, "pin": "2", "row": 0},
                             {"col": 0, "pin": "4", "row": 1}, {"col": 0, "pin": "5", "row": 2},
                             {"col": 1, "pin": "3", "row": 2}],
                 "populated_pins": ["1", "2", "3", "4", "5"]},
        "lead_exit": "bottom", "name": "Relay", "pads_origin": "pin1",
        "pinout": [{"function": "coil", "name": "Coil", "pin": "1"},
                   {"function": "no", "name": "NO", "pin": "2"},
                   {"function": "nc", "name": "NC", "pin": "3"},
                   {"function": "common", "name": "Common", "pin": "4"},
                   {"function": "coil", "name": "Coil", "pin": "5"}],
        "printed_vertical_quotes": [{"measures": "body_height", "value": 15.3}],
        "value_sources": {"body_height_mm": "read", "body_length_mm": "read",
                          "body_width_mm": "read", "pad_diameter_mm": "estimated",
                          "pitch_mm": "read", "row_spacing_mm": "derived"}}

#: The terminals the SRD drawing prints, as the dedicated reading transcribed them.
TERMINALS = [{"diameter": 0.6, "pads": "1,5", "shape": "round", "text": "2-Ø0.6"},
             {"pads": "2,3", "shape": "flat", "text": "2-1*0.4", "thickness": 0.4, "width": 1},
             {"pads": "4", "shape": "flat", "text": "1.1; 0.3", "thickness": 0.3, "width": 1.1}]


def _pads_reading(terminals=TERMINALS, holes=()):
    """A dedicated pad reading that agrees with the grid, with the given terminal sections."""
    return {"chains": [{"axis": "x", "kind": "consecutive", "stops": ["1,4,5", "2,3"],
                        "values": [12.2], "values_text": ["12.2"]},
                       {"axis": "y", "kind": "consecutive", "stops": ["1,2", "4", "5,3"],
                        "values": [6.1, 6.1], "values_text": ["6.1", "6.1"]}],
            "contacts_view": "top", "drawing_unit": "mm",
            "printed_terminals": list(terminals), "printed_holes": list(holes)}


BODY = {"body_height": 15.3, "body_length": 19.1, "body_width": 15.5, "drawing_unit": "mm",
        "printed_vertical_quotes": [{"measures": "body_height", "value": 15.3}]}


def _run(tmp_path, monkeypatch, main=MAIN, pads=None, reread=None, answers=None,
         pad_model="vendor/reader"):
    """Extract with the calls faked by tool name; returns (params, names of the tools called)."""
    responses = {"report_single_component": main,
                 "report_pad_positions": pads if pads is not None else _pads_reading(),
                 "report_body_dimensions": BODY,
                 ext.PAD_TERMINALS_TOOL_NAME: reread}
    called = []

    def fake(**kwargs):
        name = kwargs["tool"]["function"]["name"]
        called.append(name)
        if responses.get(name) is None:
            raise ext.OpenRouterError(f"unexpected call {name}")
        return copy.deepcopy(responses[name])

    monkeypatch.setattr(ext, "call_tool", Mock(side_effect=fake))
    params = ext.extract_pin_header_params(
        [_drawing(tmp_path)], component_family="single_tht", pad_model=pad_model,
        document_roles={"pinout.png": ["pinout"]}, answers=answers)
    return params, called


def _drills(params):
    pins = (params.package_geometry or {}).get("pins") or []
    return {p["number"]: (p.get("drill_mm"), (p.get("sources") or {}).get("drill_mm")) for p in pins}


def test_terminals_the_dedicated_reading_transcribed_give_estimated_holes(offline, tmp_path,
                                                                         monkeypatch):
    """Positive: no terminal in the main answer, the dedicated reading has them -> holes derived
    from them as estimates, no re-read paid."""
    params, called = _run(tmp_path, monkeypatch)
    assert ext.PAD_TERMINALS_TOOL_NAME not in called
    drills = _drills(params)
    assert set(drills) == {"1", "2", "3", "4", "5"}
    assert all(drill and word == "estimated" for drill, word in drills.values())
    assert params.terminal_reread is None


def test_the_same_model_reading_still_gives_its_terminals(offline, tmp_path, monkeypatch):
    """The real case: one model read twice -- its terminals confirm nothing, but they are
    terminals read once, never thrown away."""
    params, called = _run(tmp_path, monkeypatch, pad_model="")
    assert ext.PAD_TERMINALS_TOOL_NAME not in called
    pins = [p for p in (params.package_geometry or params.unread_pad_geometry or {}).get("pins") or []]
    assert pins and all(p.get("drill_mm") for p in pins)


def test_without_terminals_one_targeted_re_read_finds_them(offline, tmp_path, monkeypatch):
    """Positive: the dedicated reading saw no terminal -> one re-read, which finds them."""
    reread = {"drawing_unit": "mm", "printed_terminals": TERMINALS, "printed_holes": []}
    params, called = _run(tmp_path, monkeypatch, pads=_pads_reading(terminals=()), reread=reread)
    assert called.count(ext.PAD_TERMINALS_TOOL_NAME) == 1
    assert params.terminal_reread == "found"
    drills = _drills(params)
    assert all(drill and word == "estimated" for drill, word in drills.values())
    assert not params.unread_pads


def test_a_re_read_that_finds_nothing_blocks_with_the_honest_message(offline, tmp_path,
                                                                     monkeypatch):
    """Negative: the re-read finds nothing -> the block, saying it was re-read."""
    reread = {"drawing_unit": "mm", "printed_terminals": [], "printed_holes": []}
    params, called = _run(tmp_path, monkeypatch, pads=_pads_reading(terminals=()), reread=reread)
    assert called.count(ext.PAD_TERMINALS_TOOL_NAME) == 1
    assert params.terminal_reread == "nothing"
    assert params.unread_pads
    assert "Riletto: nessuna quota di terminale o foro trovata" in params.pin_map_conflict


def test_pads_with_printed_holes_cost_no_re_read(offline, tmp_path, monkeypatch):
    """Negative: the holes are printed (callout read by both readings) -> no re-read."""
    main = copy.deepcopy(MAIN)
    main["grid"]["drill_mm"] = 1.0
    main["value_sources"]["drill_mm"] = "read"
    holes = [{"pads": "1,2,3,4,5", "diameter": 1.0, "text": "5-Ø1.0"}]
    params, called = _run(tmp_path, monkeypatch, main=main,
                          pads=_pads_reading(terminals=(), holes=holes))
    assert ext.PAD_TERMINALS_TOOL_NAME not in called
    assert params.terminal_reread is None
    # Two readings of the printed callout: «read».
    assert all(word == "read" for _drill, word in _drills(params).values())


def test_a_saved_hole_answer_settles_without_a_re_read(offline, tmp_path, monkeypatch):
    """Negative: the user already chose a hole for these pads in an earlier analysis."""
    answers = {"pad_layout:x": f"{ext.PAD_HOLE_KIND}:1,4,5,2,3=1"}
    params, called = _run(tmp_path, monkeypatch, pads=_pads_reading(terminals=()),
                          reread={"drawing_unit": "mm", "printed_terminals": [],
                                  "printed_holes": []}, answers=answers)
    assert ext.PAD_TERMINALS_TOOL_NAME not in called


def test_a_saved_block_answer_does_not_settle_the_holes():
    """«Lascia bloccato» settles nothing (the re-read may still find the terminals); a chosen
    hole for the same pads does, a hole for other pads does not."""
    block = {"_answers": {"k": f"{ext.PAD_HOLE_KIND}:1,2=block"}}
    hole = {"_answers": {"k": f"{ext.PAD_HOLE_KIND}:1,2=0.8"}}
    assert ext._saved_hole_answer_settles(block, ["1", "2"]) is False
    assert ext._saved_hole_answer_settles(hole, ["1", "2"]) is True
    assert ext._saved_hole_answer_settles(hole, ["1", "3"]) is False


def test_the_re_read_tool_asks_only_terminals_and_holes():
    properties = ext._pad_terminals_tool()["function"]["parameters"]["properties"]
    assert set(properties) == {"drawing_unit", "printed_terminals", "printed_holes", "notes"}
    assert not any(ch.isdigit() for ch in ext._PAD_TERMINALS_REQUEST.replace("{pads}", ""))



def test_a_hole_read_once_in_the_re_read_is_an_estimate(offline, tmp_path, monkeypatch):
    """Generality audit item 4: a callout only the one-off re-read transcribed is a hole read
    ONCE -- estimated, with its note -- never «read»."""
    reread = {"drawing_unit": "mm", "printed_terminals": [],
              "printed_holes": [{"pads": "1,2,3,4,5", "diameter": 1.1, "text": "5-Ø1.1"}]}
    params, called = _run(tmp_path, monkeypatch, pads=_pads_reading(terminals=()), reread=reread)
    assert called.count(ext.PAD_TERMINALS_TOOL_NAME) == 1
    drills = _drills(params)
    assert set(drills) == {"1", "2", "3", "4", "5"}
    assert all(drill == pytest.approx(1.1) and word == "estimated" for drill, word in drills.values())
    assert "foro letto una sola volta" in params.notes
