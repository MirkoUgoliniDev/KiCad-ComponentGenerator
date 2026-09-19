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


def test_conflicting_orientation_keeps_measured_box_and_flags_polarity():
    messages = []
    result = inv.decode_detections(
        [detection(bbox=[.1,.1,.12,.3], rotation_deg=0)],
        (0,0,1,1),40,20,"crop",messages.append,
    )
    assert len(result) == 1
    c = result[0]
    assert c["_uncertain"]
    assert c["rotation_deg"] == 90
    assert c["_reported_rotation_deg"] == 0
    assert c["length_mm"] == pytest.approx(4)
    assert c["width_mm"] == pytest.approx(.8)
    assert c["x_mm"] == pytest.approx(-15.6)
    assert c["y_mm"] == pytest.approx(-6)
    assert messages


def test_nearly_square_box_keeps_reported_axis():
    # 2 x 2.2 mm: troppo simile per correggere un verso dalla sola proporzione.
    c = decode([detection(bbox=[.1,.1,.15,.21], rotation_deg=180)])[0]
    assert c["rotation_deg"] == 180
    assert not c.get("_uncertain")


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
    result = inv.extract_inventory([Path("top.png")],"mock",40,20,["resistor"],call,top_photo_index=0,grounding=False)
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
    result = inv.extract_inventory([Path("top.png")],"mock",40,20,["resistor"],call,top_photo_index=0,grounding=False)
    assert len(result.components) == 1
    # Un avviso per ciascuno dei sei crop falliti. La foto sintetica e' a
    # 10 px/mm, quindi si aggiunge quello sulla risoluzione insufficiente.
    assert sum("analisi fallita" in w for w in result.warnings) == 6
    assert sum("Risoluzione della foto usata" in w for w in result.warnings) == 1
    assert len(result.warnings) == 7


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
        if kw['tool']['function']['name'] == 'classify_fixed_packages':
            return {'components': []}  # A missing verification must retain the box as uncertain.
        return {"components":[dict(label='R',kind='resistor',box_2d=[200,100,400,300])] if len(requests)==2 else []}
    monkeypatch.setattr(ext,"call_tool",call)
    result = ext.extract_pin_header_params([photo],photo_cache_dir=tmp_path/'cache')
    assert len(requests)==5  # The two identical uniform side crops share a cache entry.
    assert len(result.components_3d)==1
    assert result.photo_source=="top.png"
    assert result.photo_overlay_png.startswith(b"\x89PNG")
    assert result.photo_png.startswith(b"\x89PNG")
    assert result.components_3d[0]['_uncertain']


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
    result = ext.extract_pin_header_params([photo],photo_cache_dir=tmp_path/'cache')
    assert len(result.components_3d)==1
    assert result.photo_overlay_png is None
    assert "simulated failure" in result.notes


def test_same_package_seen_twice_with_slightly_different_boxes_is_reconciled():
    """Caso reale: stesso package, due rettangoli concentrici, aree 2.53x.

    Con il tetto a 2.5 le due rilevazioni restavano due componenti distinti
    e nel modello 3D si compenetravano al 100%: il piu' piccolo spariva
    dentro il piu' grande. Il discriminante e' la coincidenza dei centri,
    non il rapporto esatto delle aree.
    """
    big = decode([detection(bbox=[.200, .200, .288, .258], kind="capacitor")])
    small = decode([detection(bbox=[.217, .212, .270, .246], kind="capacitor")],
                   source="B")
    merged = inv.merge_components([big, small], containment=True)
    assert len(merged) == 1, "la stessa rilevazione e' rimasta duplicata"


def test_a_small_part_inside_a_generous_module_box_is_still_kept():
    """Il contenimento da solo non basta: qui sono due pezzi veri.

    Le aree differiscono di tre ordini di grandezza, non di un fattore 2.5:
    e' un passivo dentro il rettangolo di un modulo, non un doppione.
    """
    big = decode([detection(bbox=[.1, .1, .8, .8], kind="module")])
    small = decode([detection(bbox=[.2, .2, .25, .25])], source="B")
    assert len(inv.merge_components([big, small], containment=True)) == 2


def test_a_coarse_top_view_is_declared_incomplete_instead_of_looking_complete():
    """Il limite e' nei pixel, non nell'algoritmo: va detto, non nascosto."""
    messages = []
    # Caso reale ESP32: ritaglio da 770 px per una scheda da 63.3 mm.
    inv._warn_if_too_coarse(770, 63.30, messages.append)
    assert messages, "nessun avviso su una vista troppo grossolana"
    assert "12.2 px/mm" in messages[0] and "incompleto" in messages[0]


def test_a_sharp_enough_top_view_says_nothing():
    messages = []
    inv._warn_if_too_coarse(3200, 63.30, messages.append)
    assert not messages


@pytest.mark.parametrize("crop_px, length", [(0, 63.3), (-5, 63.3), (770, 0), (770, float("nan"))])
def test_degenerate_calibration_does_not_raise(crop_px, length):
    inv._warn_if_too_coarse(crop_px, length, lambda m: None)
