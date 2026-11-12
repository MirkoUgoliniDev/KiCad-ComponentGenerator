"""@brief The tie-break of the body height: a third reading on enlarged tiles (user, 2026-11-03).

The main model listed its misread «19,3» as a printed quote, the dedicated
reading «16,9»: one point each, no value. A third transcription of the
drawing sent as the whole page plus four enlarged tiles, by the main
analysis model, adds its points; the value ahead wins, another tie stays a
conflict. The tiles are checked structurally: four overlapping quarters,
each larger than its crop, within the provider's limits, no metadata.
"""
import base64
from io import BytesIO
from unittest.mock import Mock

import pytest
from PIL import Image

from kicadgen.services import datasheet_extractor as ext
from kicadgen.services.datasheet_extractor import apply_dedicated_body

from test_single_component_prompt import _CHAINS_ANSWER, _drawing, _single, offline  # noqa: F401


def _main(**sizes):
    data = {"drawing_unit": "mm", "package_geometry": {"origin": "body_center", "pins": []},
            "value_sources": {"body_height_mm": "read"}, "printed_vertical_quotes": [19.3, 0.4, 3.5]}
    data.update(sizes)
    for key, value in sizes.items():
        data["package_geometry"][key] = value
    return data


def _dedicated(height=16.9, printed=(16.9, 0.4, 3.5)):
    return {"values": {"body_height": height}, "printed": list(printed), "notes": "vista frontale"}


# ── The vote with a third list ──────────────────────────────────────────

def test_a_tie_calls_the_tie_break_once_and_the_value_it_prints_wins():
    data = _main(body_length_mm=22.5, body_width_mm=16.5, body_height_mm=19.3)
    third = Mock(return_value={"values": {"body_height": 16.9}, "printed": [16.9, 0.4, 3.5], "notes": ""})
    logs = []
    apply_dedicated_body(data, _dedicated(), logs.append, tie_break=third)
    assert third.call_count == 1
    assert data["body_height_mm"] == pytest.approx(16.9)
    assert any("Spareggio" in line and "16.90" in line for line in logs)


def test_a_third_list_printing_the_main_value_keeps_it():
    """Negative twin: the enlarged reading agrees with the main model, the dedicated one loses."""
    data = _main(body_length_mm=22.5, body_height_mm=19.3)
    third = Mock(return_value={"values": {"body_height": 19.3}, "printed": [19.3, 0.4, 3.5], "notes": ""})
    apply_dedicated_body(data, _dedicated(), tie_break=third)
    assert data["body_height_mm"] == 19.3 and "_body_conflict" not in data


def test_a_third_list_printing_neither_leaves_the_conflict():
    data = _main(body_length_mm=22.5, body_height_mm=19.3)
    third = Mock(return_value={"values": {"body_height": 17.7}, "printed": [17.7, 0.4, 3.5], "notes": ""})
    apply_dedicated_body(data, _dedicated(), tie_break=third)
    assert data["body_height_mm"] is None and "19.30 mm" in data["_body_conflict"]


def test_a_failed_tie_break_leaves_the_conflict():
    data = _main(body_length_mm=22.5, body_height_mm=19.3)
    third = Mock(return_value={"values": {}, "notes": "", "failed": "down"})
    apply_dedicated_body(data, _dedicated(), tie_break=third)
    assert data["body_height_mm"] is None and "_body_conflict" in data


def test_no_tie_no_tie_break_call():
    """Both lists print 16.9 and neither prints 19.3: decided without a paid third call."""
    data = _main(body_length_mm=22.5, body_height_mm=19.3)
    data["printed_vertical_quotes"] = [16.9, 0.4, 3.5]
    third = Mock()
    apply_dedicated_body(data, _dedicated(), tie_break=third)
    assert third.call_count == 0 and data["body_height_mm"] == pytest.approx(16.9)


# ── The enlarged tiles ──────────────────────────────────────────────────

def _page(tmp_path, width=977, height=767):
    path = tmp_path / "drawing.png"
    image = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    image.save(path)
    return path


def _decode(block):
    url = block["image_url"]["url"]
    return Image.open(BytesIO(base64.b64decode(url.split(",", 1)[1])))


def test_the_tiles_are_four_enlarged_quarters_within_the_limits(tmp_path):
    from kicadgen.services.photo_grounding import model_image_limits
    blocks = ext._enlarged_tile_blocks(_page(tmp_path), "vendor/reader")
    images = [b for b in blocks if b.get("type") == "image_url"]
    captions = [b["text"] for b in blocks if b.get("type") == "text"]
    assert len(images) == 5 and len(captions) == 5
    # CONTRACT CHANGED 2026-11-12: prompts in English, fixed and never in the
    # interface language (the captions were tx() texts before).
    assert "whole page" in captions[0] and all(f"tile {i} of 4" in captions[i] for i in range(1, 5))
    limits = model_image_limits("vendor/reader")
    tile_w, tile_h = int(977 * ext.TILE_SIDE_FRACTION), int(767 * ext.TILE_SIDE_FRACTION)
    for block in images[1:]:
        tile = _decode(block)
        assert tile.size[0] > tile_w and tile.size[1] > tile_h, "enlarged"
        assert max(tile.size) <= limits.long_side_px and tile.size[0] * tile.size[1] <= limits.max_pixels
        assert tile.mode == "RGB" and "icc_profile" not in tile.info
        assert tile.getpixel((0, 0)) == (255, 255, 255), "transparent background composited on white"


def test_a_page_already_at_the_limit_still_gets_enlarged_tiles(tmp_path):
    """Negative of the whole-page enlargement: tiles are a fraction, so they still grow."""
    blocks = ext._enlarged_tile_blocks(_page(tmp_path, 1568, 1000), "vendor/reader")
    images = [b for b in blocks if b.get("type") == "image_url"]
    whole = _decode(images[0])
    assert whole.size == (1568, 1000)
    tile = _decode(images[1])
    assert tile.size[0] > int(1568 * ext.TILE_SIDE_FRACTION)


def test_the_enlarged_reading_sends_the_tiles_and_the_body_tool(tmp_path):
    call = Mock(return_value={"drawing_unit": "mm", "body_height": 16.9, "printed_vertical_quotes": [16.9]})
    reading = ext.read_body_dimensions([_page(tmp_path)], model="vendor/main", call=call, enlarged=True)
    blocks = call.call_args.kwargs["content_blocks"]
    assert sum(1 for b in blocks if b.get("type") == "image_url") == 5
    assert call.call_args.kwargs["tool"]["function"]["name"] == "report_body_dimensions"
    # Review 2026-11-04 (A1): every printed quote carries the role the reader
    # declared; a bare number of an older answer is "other".
    assert reading["printed"] == [{"value": 16.9, "measures": "other"}]


# ── End to end: the fourth call happens only on a tie ───────────────────

def test_end_to_end_the_tie_break_uses_the_other_model_on_the_enlarged_drawing(offline, monkeypatch, tmp_path):
    """CONTRACT CHANGED (review 2026-11-04, M13): the third witness is a model OTHER than the
    main one -- the dedicated reader -- never the main model reading itself again."""
    logs = []
    main = _single(body_height_mm=19.3, value_sources={"body_length_mm": "read", "body_height_mm": "read"},
                   printed_vertical_quotes=[19.3, 0.4, 3.5])
    body = {"drawing_unit": "mm", "body_length": 20.5, "body_width": 10.5, "body_height": 16.9,
            "printed_vertical_quotes": [16.9, 0.4, 3.5]}
    third = {"drawing_unit": "mm", "body_height": 16.9, "printed_vertical_quotes": [16.9, 0.4, 3.5]}
    call = Mock(side_effect=[main, _CHAINS_ANSWER, body, third])
    monkeypatch.setattr(ext, "call_tool", call)
    params = ext.extract_pin_header_params(
        [_drawing(tmp_path)], model="vendor/main", component_family="single_tht",
        pad_model="vendor/reader", document_roles={"quote.png": ["top"]}, progress=logs.append)
    assert call.call_count == 4
    assert call.call_args_list[3].kwargs["model"] == "vendor/reader"
    assert sum(1 for b in call.call_args_list[3].kwargs["content_blocks"] if b.get("type") == "image_url") == 5
    assert params.body_height_mm == pytest.approx(16.9)
    assert any("Spareggio sull'altezza" in line for line in logs)


def test_end_to_end_no_tie_break_when_the_reader_is_the_main_model(offline, monkeypatch, tmp_path):
    """Negative twin: one model only -> no paid third call, the conflict becomes a question."""
    logs = []
    main = _single(body_height_mm=19.3, value_sources={"body_length_mm": "read", "body_height_mm": "read"},
                   printed_vertical_quotes=[19.3, 0.4, 3.5])
    body = {"drawing_unit": "mm", "body_length": 20.5, "body_width": 10.5, "body_height": 16.9,
            "printed_vertical_quotes": [16.9, 0.4, 3.5]}
    call = Mock(side_effect=[main, _CHAINS_ANSWER, body])
    monkeypatch.setattr(ext, "call_tool", call)
    params = ext.extract_pin_header_params(
        [_drawing(tmp_path)], model="vendor/main", component_family="single_tht",
        document_roles={"quote.png": ["top"]}, progress=logs.append)
    assert call.call_count == 3
    assert any("Spareggio sull'altezza non disponibile" in line for line in logs)
    assert params.body_height_mm is None and params.body_conflict
    question, = [q for q in params.questions if q["field"] == "body_height_mm"]
    assert [o["value"] for o in question["options"]] == [19.3, 16.9]
    chosen = ext.apply_answers(params, {question["key"]: 16.9})
    assert chosen.body_height_mm == pytest.approx(16.9) and chosen.body_conflict is None
    assert chosen.value_sources["body_height_mm"] == ext.USER_SOURCE
    assert all(q["field"] != "body_height_mm" for q in chosen.questions)


# ── Unsettled without a tie (HLK module, 2026-11-04) ────────────────────
#
# Main «18» printed in its own list only (one point); dedicated «23» equal
# to the plan width, excluded from both lists (no point). Not a tie, but
# nothing printed twice either: the third reading decides.

def _hlk_main():
    data = _main(body_length_mm=38.0, body_width_mm=23.0, body_height_mm=18.0)
    data["printed_vertical_quotes"] = [5.0, 18.0, 23.0, 5.0]
    return data


def test_one_point_against_none_calls_the_third_reading_and_a_second_print_wins():
    """Positive: the enlarged reading prints 18 too -> 18 read twice, the plan width discarded."""
    third = Mock(return_value={"values": {"body_height": 18.0}, "printed": [5.0, 18.0, 23.0],
                               "notes": ""})
    data = _hlk_main()
    apply_dedicated_body(data, _dedicated(23.0, (23.0, 5.0, 5.0)), tie_break=third)
    assert third.call_count == 1
    assert data["body_height_mm"] == pytest.approx(18.0) and "_body_conflict" not in data


def test_one_point_against_none_stays_a_conflict_when_the_third_list_prints_neither():
    """Negative: a third list without 18 leaves one point: no value, the conflict said."""
    third = Mock(return_value={"values": {"body_height": 23.0}, "printed": [23.0, 5.0], "notes": ""})
    data = _hlk_main()
    apply_dedicated_body(data, _dedicated(23.0, (23.0, 5.0, 5.0)), tie_break=third)
    assert third.call_count == 1 and data["body_height_mm"] is None and "_body_conflict" in data


def test_a_value_already_printed_twice_never_pays_the_third_reading():
    """Negative twin of the trigger: 2 points against 0 is settled without a call."""
    data = _hlk_main()
    third = Mock()
    apply_dedicated_body(data, _dedicated(18.0, (18.0, 5.0)), tie_break=third)
    assert third.call_count == 0 and data["body_height_mm"] == pytest.approx(18.0)
