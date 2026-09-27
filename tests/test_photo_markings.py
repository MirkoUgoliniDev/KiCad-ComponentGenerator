"""@brief Better marking reading (user, 2026-09-27): rules, confidence, dedicated crops."""
import numpy as np
import pytest

pytest.importorskip("cv2")

from kicadgen.cad3d.kicad_models import _marking_model_tokens
from kicadgen.services import photo_global_inventory as inventory
from kicadgen.services import photo_markings as markings

LENGTH, WIDTH = 42.8, 15.2


def _photo(width_px):
    height_px = round(width_px * WIDTH / LENGTH)
    return np.full((height_px, width_px, 3), 40, np.uint8)


def _components():
    return [dict(name="U1", x_mm=-5.0, y_mm=0.0, length_mm=5.0, width_mm=4.0,
                 rotation_deg=90.0, marking="SP3?52", marking_confidence="medium"),
            dict(name="C1", x_mm=5.0, y_mm=2.0, length_mm=2.0, width_mm=1.2,
                 rotation_deg=0.0, marking=""),
            dict(name="R1", x_mm=10.0, y_mm=-3.0, length_mm=3.2, width_mm=1.6,
                 rotation_deg=0.0, marking="472", marking_confidence="high")]


# ── rules shared by the two prompts ──────────────────────────────────────

def test_both_prompts_carry_the_same_marking_rules():
    for prompt in (inventory.PROMPT, inventory.PROMPT_ENUMERATE, markings.PROMPT):
        assert markings.MARKING_RULES in prompt
    for rule in ("' / '", "0/O/D", "EIA-96", "marking_confidence rates the"):
        assert rule in markings.MARKING_RULES


def test_the_marking_confidence_is_recorded_and_never_blocks():
    item = inventory.inventory_tool()['function']['parameters']['properties']['components']['items']
    assert item['properties']['marking_confidence']['enum'] == ['high', 'medium', 'low']
    assert 'marking_confidence' in inventory.OPTIONAL_REFINEMENTS
    assert inventory.PROMPT_VERSION == 'global-yolo-inventory-9'
    assert inventory.SCHEMA_VERSION == 'global-inventory-6'


# ── a guessed marking is not a model search key ──────────────────────────

@pytest.mark.parametrize("confidence,expected", [
    ("high", ("SP3452",)), ("medium", ("SP3452",)), (None, ("SP3452",)), ("low", ())])
def test_a_low_confidence_marking_is_not_used_to_search_models(confidence, expected):
    component = {"marking": "SP3452 / 25221", "marking_confidence": confidence}
    assert _marking_model_tokens(component) == expected


# ── dedicated reading: only when the photo has more pixels than the model saw ──

def test_a_photo_the_model_already_saw_whole_is_not_read_again():
    """The user's 1024 px photo: every pixel was already sent, nothing to gain."""
    photo = _photo(1024)
    assert markings.inventory_px_per_mm(LENGTH, WIDTH, photo.shape[1::-1]) == pytest.approx(
        1024 / LENGTH, rel=0.01)
    calls, logs = [], []
    replaced = markings.read_markings(photo, _components(), LENGTH, WIDTH, "m",
                                      lambda **kw: calls.append(kw), logs.append)
    assert replaced == 0 and calls == []
    assert any("saltata" in line for line in logs)


def test_a_sharper_photo_sends_only_the_bodies_with_a_marking():
    photo = _photo(4000)
    assert markings.inventory_px_per_mm(LENGTH, WIDTH, photo.shape[1::-1]) < 4000 / LENGTH
    components = _components()
    call = _answer_ids({1: ("SP3452 / 25221", "high"), 3: ("472", "high")})
    replaced = markings.read_markings(photo, components, LENGTH, WIDTH, "m", call, lambda _l: None)
    sent_ids = [block["text"] for block in call.kwargs["content_blocks"] if block["type"] == "text"]
    assert sent_ids == ["id=1", "id=3"]                        # C1 has no text: not sent
    assert replaced == 1
    assert components[0]["marking"] == "SP3452 / 25221"
    assert components[0]["marking_confidence"] == "high"
    assert components[0]["_marking_inventory"] == "SP3?52"
    assert components[2]["marking"] == "472" and "_marking_inventory" not in components[2]


def _answer_ids(by_id):
    def call(**kwargs):
        call.kwargs = kwargs
        return {"components": [dict(id=i, marking=text, marking_confidence=level)
                               for i, (text, level) in by_id.items()]}
    return call


def test_a_weaker_or_empty_reading_never_replaces_a_better_one():
    """Negative: the dedicated reading can only improve the inventory's."""
    components = _components()
    call = _answer_ids({1: ("5P3452", "low"), 3: ("", "high")})
    markings.read_markings(_photo(4000), components, LENGTH, WIDTH, "m", call, lambda _l: None)
    assert components[0]["marking"] == "SP3?52"                # low < medium: kept
    assert components[2]["marking"] == "472"                   # empty: kept


def test_a_failed_reading_keeps_the_inventory_markings():
    components = _components()

    def broken(**kwargs):
        raise ValueError("provider down")
    logs = []
    assert markings.read_markings(_photo(4000), components, LENGTH, WIDTH, "m",
                                  broken, logs.append) == 0
    assert components[0]["marking"] == "SP3?52"
    assert any("non riuscita" in line for line in logs)


def test_crops_have_square_pixels_and_the_long_axis_horizontal():
    """A vertical body on an anisotropic photo comes out horizontal and undistorted."""
    photo = np.zeros((800, 2000, 3), np.uint8)                  # X 46.7 px/mm, Y 52.6 px/mm
    body = dict(x_mm=0.0, y_mm=0.0, length_mm=6.0, width_mm=2.0, rotation_deg=90.0)
    crop = markings._crop(photo, body, LENGTH, WIDTH)
    height, width = crop.shape[:2]
    assert width > height
    assert width / height == pytest.approx(3.0, rel=0.1)
