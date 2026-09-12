"""Regressioni geometriche e orchestrazione con risposte AI simulate."""
from pathlib import Path

import pytest
import numpy as np

from kicadgen.services import photo_inventory as inv
from kicadgen.services.openrouter import OpenRouterError


def detection(**updates):
    item = dict(name="R", kind="resistor", bbox=[.1,.2,.3,.4],
                height_mm=.5, rotation_deg=0, color_hex="#303030", marking="",
                confidence="high", object_type="component", fully_visible=True)
    item.update(updates)
    return item


def decode(items, region=(0,0,1,1), source="A"):
    return inv.decode_detections(items, region, 40, 20, source, lambda msg:None)


def test_coordinates_are_transformed_from_crop_not_guessed_in_mm():
    item = decode([detection()], region=(.25,0,.75,.5))[0]
    assert item["x_mm"] == pytest.approx(-6)
    assert item["y_mm"] == pytest.approx(-7)
    assert item["length_mm"] == pytest.approx(4)
    assert item["width_mm"] == pytest.approx(2)


def test_vertical_component_keeps_long_axis_and_polarity():
    item = decode([detection(bbox=[.1,.1,.12,.3], rotation_deg=270)])[0]
    assert item["length_mm"] == pytest.approx(4)
    assert item["width_mm"] == pytest.approx(.8)
    assert item["rotation_deg"] == 270


@pytest.mark.parametrize("bad", [
    None, "R1", detection(bbox=[0,0,0,.1]), detection(bbox=[0,0,2,.1]),
    detection(bbox=[float('nan'),0,.2,.1]), detection(bbox=[0,0,.2]),
    detection(height_mm=float('inf')), detection(rotation_deg=45),
    detection(fully_visible=False), detection(object_type="pcb_feature"),
])
def test_invalid_partial_or_pcb_features_are_reported(bad):
    messages = []
    assert inv.decode_detections([bad], (0,0,1,1),40,20,"crop",messages.append) == []
    assert messages


def test_different_classifications_same_box_merge_but_are_flagged():
    first = decode([detection(kind="resistor")])
    second = decode([detection(kind="capacitor")], source="B")
    merged = inv.merge_components([first, second])
    assert len(merged) == 1
    assert merged[0]["_uncertain"]


def test_small_component_inside_large_box_is_not_absorbed():
    big = decode([detection(bbox=[.1,.1,.8,.8], kind="module")])
    small = decode([detection(bbox=[.2,.2,.25,.25])], source="B")
    assert len(inv.merge_components([big,small])) == 2


def test_same_crop_adjacent_parts_are_preserved():
    group = decode([detection(), detection(bbox=[.11,.2,.31,.4])])
    assert len(inv.merge_components([group])) == 2


def test_cross_crop_matching_is_one_to_one():
    a = decode([detection()])
    b = decode([detection(), detection(bbox=[.11,.2,.31,.4])],source="B")
    assert len(inv.merge_components([a,b])) == 2


def test_full_photo_keeps_large_component_when_tiles_are_cut(monkeypatch):
    photo = np.full((200,400,3),30,np.uint8)
    monkeypatch.setattr(inv,"prepare_photo",lambda *args:photo)
    calls = []
    def call(**kwargs):
        calls.append(kwargs)
        return {"components": [detection(fully_visible=len(calls)==1)]}
    result = inv.extract_inventory([Path("top.png")],"mock",40,20,["resistor"],call,top_photo_index=0)
    assert len(calls) == 7
    assert len(result.components) == 1
    assert result.overlay_png.startswith(b"\x89PNG")
    assert result.warnings  # Partial boxes were explicitly reported.


def test_confirmed_source_wins_over_larger_bottom_photo(monkeypatch):
    seen = []
    def prepare(path,*args):
        seen.append(path.name)
        return np.full((200,400,3),30,np.uint8)
    monkeypatch.setattr(inv,"prepare_photo",prepare)
    result = inv.extract_inventory([Path("bottom.png"),Path("top.png")],"mock",40,20,[],
                                   lambda **kw:{"components":[]},top_photo_index=1)
    assert seen == ["top.png"]
    assert result.photo == "top.png"


def test_absent_top_view_does_not_run_more_api_calls(monkeypatch):
    def unexpected(*args,**kw):
        pytest.fail("Unexpected photo/API work")
    monkeypatch.setattr(inv,"prepare_photo",unexpected)
    result = inv.extract_inventory([Path("bottom.png")],"mock",40,20,[],unexpected,top_photo_index=-1)
    assert not result.components


def test_failed_crop_keeps_successes(monkeypatch):
    monkeypatch.setattr(inv,"prepare_photo",lambda *a:np.full((200,400,3),30,np.uint8))
    count = 0
    def call(**kw):
        nonlocal count
        count += 1
        if count > 1:
            raise OpenRouterError("simulated failure")
        return {"components":[detection()]}
    result = inv.extract_inventory([Path("top.png")],"mock",40,20,["resistor"],call,top_photo_index=0)
    assert len(result.components) == 1
    assert len(result.warnings) == 6


def test_vertical_board_is_rejected(tmp_path):
    import cv2
    photo = np.full((500,300,3),255,np.uint8)
    cv2.rectangle(photo,(50,50),(250,450),(20,20,20),-1)
    path = tmp_path / "vertical.png"
    cv2.imwrite(str(path),photo)
    assert inv.prepare_photo(path,40,20) is None


def test_pipeline_passes_source_selection_and_retains_overlay(monkeypatch,tmp_path):
    from kicadgen.services import datasheet_extractor as ext
    photo = tmp_path / "top.png"
    photo.write_bytes(b"fake-png")
    monkeypatch.setattr(ext,"api_key",lambda:"test-not-a-key")
    monkeypatch.setattr(inv,"prepare_photo",lambda *a:np.full((200,400,3),30,np.uint8))
    requests = []
    def call(**kw):
        requests.append(kw)
        if len(requests)==1:
            assert kw["content_blocks"][0]["text"] == "FILE 0: top.png"
            return dict(layout="castellated",body_length_mm=40,body_width_mm=20,
                        top_photo_index=0,castellated_sides=[dict(edge="left",count=2,pitch_mm=2.54)])
        return {"components":[detection()] if len(requests)==2 else []}
    monkeypatch.setattr(ext,"call_tool",call)
    result = ext.extract_pin_header_params([photo])
    assert len(requests)==8
    assert len(result.components_3d)==1
    assert result.photo_source=="top.png"
    assert result.photo_overlay_png.startswith(b"\x89PNG")


def test_pipeline_keeps_global_inventory_when_all_photo_calls_fail(monkeypatch,tmp_path):
    from kicadgen.services import datasheet_extractor as ext
    photo = tmp_path / "top.png"
    photo.write_bytes(b"fake-png")
    monkeypatch.setattr(ext,"api_key",lambda:"test-not-a-key")
    monkeypatch.setattr(inv,"prepare_photo",lambda *a:np.full((200,400,3),30,np.uint8))
    def call(**kw):
        if kw["tool"]["function"]["name"] == "report_pin_header_params":
            return dict(layout="castellated",body_length_mm=40,body_width_mm=20,
                        top_photo_index=0,castellated_sides=[dict(edge="left",count=2,pitch_mm=2.54)],
                        components_3d=[dict(name="R",x_mm=1,y_mm=2,length_mm=2,width_mm=1,height_mm=.5)])
        raise OpenRouterError("simulated failure")
    monkeypatch.setattr(ext,"call_tool",call)
    result = ext.extract_pin_header_params([photo])
    assert len(result.components_3d)==1
    assert result.photo_overlay_png is None
    assert "simulated failure" in result.notes
