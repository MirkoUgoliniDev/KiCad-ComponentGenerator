"""@brief The AI counts the contacts in its first call, helped by enlarged edge strips (HANDOFF point 142).

User, 2026-09-28: «non voglio una pezza, il numero di contatti deve essere
riconosciuto da AI». The first call of a castellated module receives, besides
the documents, an enlarged strip of each edge of the declared photograph, and
the tool schema tells the model to count the holes, not the labels.
"""
from pathlib import Path

import cv2
import numpy as np
import pytest

from kicadgen.services import datasheet_extractor as extractor
from kicadgen.services.edge_strips import (EDGES, STRIP_LONG_SIDE_PX, board_box,
                                           edge_strips)
from edge_notch_photos import row, tilted_board

ROOT = Path(__file__).resolve().parents[1]


def test_the_board_is_found_without_its_dimensions():
    image = tilted_board(40, 16, 20, angle_deg=0.0, notches={"right": row(4, 2.54, 16)},
                         radius_mm=0.45)
    box = board_box(image)
    assert box is not None
    x0, y0, x1, y1 = box
    assert (x1 - x0) / (y1 - y0) == pytest.approx(40 / 16, rel=0.05)


def test_an_image_without_a_board_gives_no_strip():
    """Negative: a uniform image has no region distinct from its background."""
    blank = np.full((400, 900, 3), 235, np.uint8)
    assert board_box(blank) is None
    assert edge_strips(blank) == []


def test_four_enlarged_strips_in_edge_order():
    image = tilted_board(40, 16, 20, angle_deg=0.0, notches={"right": row(4, 2.54, 16)},
                         radius_mm=0.45)
    strips = edge_strips(image)
    assert [edge for edge, _png in strips] == list(EDGES)
    shapes = {edge: cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR).shape
              for edge, png in strips}
    assert shapes["top"][1] >= STRIP_LONG_SIDE_PX and shapes["top"][1] > shapes["top"][0]
    assert shapes["right"][0] >= STRIP_LONG_SIDE_PX and shapes["right"][0] > shapes["right"][1]


@pytest.mark.parametrize("photo", ["docs/RS485/AltaRisoluzione.jpg",
                                   "docs/ESP32/AltaRisoluzione2.jpg"])
def test_the_sample_photos_give_four_strips(photo):
    path = ROOT / photo
    if not path.is_file():
        pytest.skip("sample photo not available")
    image = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
    assert len(edge_strips(image)) == 4


def _photo_file(tmp_path):
    image = tilted_board(40, 16, 20, angle_deg=0.0, notches={"right": row(4, 2.54, 16)},
                         radius_mm=0.45)
    path = tmp_path / "top.png"
    cv2.imwrite(str(path), image)
    return path


def test_only_a_castellated_module_sends_the_strips(tmp_path):
    path = _photo_file(tmp_path)
    blocks = extractor._edge_strip_blocks([path], {path.name: ("hires",)})
    assert sum(1 for b in blocks if b.get("type") == "image_url") == 4
    assert all("conta qui i fori" in b["text"] for b in blocks if b.get("type") == "text")


def test_a_file_not_declared_a_photograph_sends_no_strip(tmp_path):
    """Negative: a drawing or a pinout is not cut into strips."""
    path = _photo_file(tmp_path)
    assert extractor._edge_strip_blocks([path], {path.name: ("pinout",)}) == []


@pytest.mark.parametrize("family, expected", [("aggregate_castellated", 4),
                                              ("aggregate_tht", 0), ("single_smd", 0)])
def test_the_first_call_carries_the_strips_for_castellated_modules_only(
        tmp_path, monkeypatch, family, expected):
    path = _photo_file(tmp_path)
    sent = []

    class _Stop(Exception):
        pass

    def call(**request):
        sent.append(request)
        raise _Stop

    # Offline: no key, no credit request, the paid call itself is faked.
    monkeypatch.setattr(extractor, "api_key", lambda: "offline")
    monkeypatch.setattr(extractor, "check_credit", lambda **kwargs: None)
    monkeypatch.setattr(extractor, "call_tool", call)
    with pytest.raises(_Stop):
        extractor.extract_pin_header_params(
            [path], document_roles={path.name: ["hires"]}, component_family=family)
    images = [b for b in sent[0]["content_blocks"] if b.get("type") == "image_url"]
    assert len(images) == 1 + expected       # the photo itself, plus the strips


def test_the_tool_tells_the_model_to_count_holes_not_labels():
    side = extractor._openai_tool()["function"]["parameters"]["properties"][
        "castellated_sides"]["items"]["properties"]
    assert "NON sono contatti" in side["count"]["description"]
    assert "un nome per contatto" in side["pin_names"]["description"]
