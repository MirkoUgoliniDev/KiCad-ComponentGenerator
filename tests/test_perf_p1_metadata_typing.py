"""@brief Perf audit 2026-10-17: a metadata keystroke must not redo photo work.

Measured on the user's RS485 board (42 photo bodies): 450-870 ms per
character typed in «Metadati del componente». Every keystroke refreshed the
KiCad gate, which built the castellated parameters four times, and each build
decoded the board photo and cropped and described every photo body, although
the gate only compares pads, outline and body sizes.

The contracts below are expressed by call counts and equal outputs, never by
wall-clock time:
- the footprint side of the gate (pads, outline, symbol pad ids, STEP body
  record, 2D preview) never builds the photo bodies;
- the generation still gets every photo body with its image descriptor;
- the decoded photo and each body's descriptor are reused while their inputs
  are unchanged, and recomputed when the photo bytes or the body geometry
  change (the cache never hides a real change);
- the footprint does not depend on the photo bodies at all (structural).
"""
import re

import cv2
import numpy as np
import pytest

from kicadgen.gui import main_window as gui
from kicadgen.services import component_image_match as image_match


def _textured_png(width_px, height_px, seed):
    """A noisy calibrated photo, so every body crop has its own descriptor."""
    rng = np.random.default_rng(seed)
    image = rng.integers(0, 255, (height_px, width_px, 3), dtype=np.uint8)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


def _rows(count=6):
    return [dict(id=f"U{i}", name=f"U{i}", x_mm=-15.0 + 6.0 * i, y_mm=(-1) ** i * 4.0,
                 length_mm=4.0, width_mm=2.0, height_mm=1.0, rotation_deg=0.0,
                 kind="generic") for i in range(count)]


def _castellated_board(window, rows=6, seed=1):
    window.group_castellated.setChecked(True)
    window.field_cast_body_length.setValue(40.0)
    window.field_cast_body_width.setValue(20.0)
    window._photo_body_fields = "cast"
    window._photo_png = _textured_png(400, 200, seed)
    window.group_3d.setChecked(True)
    window._set_photo_components(_rows(rows), replace_excluded=True)


class _Counter:
    """Wrap a module function and count its calls."""

    def __init__(self, monkeypatch, module, name):
        self.calls = 0
        real = getattr(module, name)

        def wrapper(*args, **kwargs):
            self.calls += 1
            return real(*args, **kwargs)
        monkeypatch.setattr(module, name, wrapper)


def _count_components_builds(monkeypatch):
    calls = []
    real = gui.MainWindow._components_from_table
    monkeypatch.setattr(gui.MainWindow, "_components_from_table",
                        lambda self: calls.append(1) or real(self))
    return calls


def test_metadata_typing_never_builds_the_photo_bodies(window, monkeypatch):
    """Negative: the gate chain does not build photo bodies, decode or crop the photo."""
    _castellated_board(window)
    builds = _count_components_builds(monkeypatch)
    decodes = _Counter(monkeypatch, cv2, "imdecode")
    crops = _Counter(monkeypatch, image_match, "extract_component_crop")
    for index in range(5):
        window.field_meta_description.setText(f"descr {index}")
        window.field_meta_keywords.setText(f"kw {index}")
        window.field_datasheet.setText(f"https://example.com/{index}.pdf")
        window.field_fp_filters.setText(f"X{index}*")
    # The same chain the gate walks when the files exist on disk.
    window._footprint_inputs_now()
    window._symbol_pad_ids()
    window._step_body_fingerprint()
    window._current_body_params()
    window._preview_params()
    assert builds == []
    assert decodes.calls == 0
    assert crops.calls == 0


def test_the_generation_still_gets_every_photo_body_with_its_descriptor(window):
    """Positive: the parameters handed to the generator keep the photo bodies."""
    _castellated_board(window, rows=6)
    params = window._current_castellated_params()
    assert len(params.components) == 6
    image = cv2.imdecode(np.frombuffer(window._photo_png, np.uint8), cv2.IMREAD_COLOR)
    for box, row in zip(params.components, window._photo_rows):
        expected = image_match.descriptor_bytes(image_match.describe_crop(
            image_match.extract_component_crop(image, row, 40.0, 20.0)))
        assert box.match_evidence["_accepted_image_descriptor"] == expected


def test_the_photo_is_decoded_once_and_each_body_described_once(window, monkeypatch):
    _castellated_board(window, rows=6)
    decodes = _Counter(monkeypatch, cv2, "imdecode")
    describes = _Counter(monkeypatch, image_match, "describe_crop")
    first = [box.match_evidence for box in window._components_from_table()]
    for _ in range(4):
        again = [box.match_evidence for box in window._components_from_table()]
        assert again == first
    assert decodes.calls == 1
    assert describes.calls == 6
    # The same bytes in a new object are the same photo: nothing recomputed.
    window._photo_png = bytes(bytearray(window._photo_png))
    window._components_from_table()
    assert (decodes.calls, describes.calls) == (1, 6)


def test_a_non_geometric_edit_of_a_body_reuses_its_descriptor(window, monkeypatch):
    _castellated_board(window, rows=6)
    window._components_from_table()
    describes = _Counter(monkeypatch, image_match, "describe_crop")
    window._photo_rows[2]["name"] = "renamed"
    window._photo_rows[2]["height_mm"] = 3.0
    window._components_from_table()
    assert describes.calls == 0


def test_a_moved_body_is_described_again(window, monkeypatch):
    """Negative of the cache: a geometry change of ONE body recomputes that body only."""
    _castellated_board(window, rows=6)
    before = [box.match_evidence["_accepted_image_descriptor"]
              for box in window._components_from_table()]
    describes = _Counter(monkeypatch, image_match, "describe_crop")
    window._photo_rows[3]["x_mm"] += 2.0
    after = [box.match_evidence["_accepted_image_descriptor"]
             for box in window._components_from_table()]
    assert describes.calls == 1
    assert after[3] != before[3]
    assert after[:3] + after[4:] == before[:3] + before[4:]
    window._photo_rows[1]["rotation_deg"] = 90.0
    window._components_from_table()
    assert describes.calls == 2


def test_a_new_photo_is_decoded_and_described_again(window, monkeypatch):
    """Negative of the cache: other photo bytes give other descriptors."""
    _castellated_board(window, rows=6, seed=1)
    before = [box.match_evidence["_accepted_image_descriptor"]
              for box in window._components_from_table()]
    decodes = _Counter(monkeypatch, cv2, "imdecode")
    describes = _Counter(monkeypatch, image_match, "describe_crop")
    window._photo_png = _textured_png(400, 200, seed=2)
    after = [box.match_evidence["_accepted_image_descriptor"]
             for box in window._components_from_table()]
    assert (decodes.calls, describes.calls) == (1, 6)
    assert all(a != b for a, b in zip(after, before))


def test_a_new_board_size_describes_the_bodies_again(window, monkeypatch):
    _castellated_board(window, rows=6)
    window._components_from_table()
    describes = _Counter(monkeypatch, image_match, "describe_crop")
    window.field_cast_body_length.setValue(42.0)
    window._components_from_table()
    assert describes.calls == 6


def test_the_decoded_photo_cannot_be_altered_by_a_caller(window):
    _castellated_board(window)
    image = window._decoded_component_photo()
    with pytest.raises(ValueError):
        image[0, 0] = 0


def test_the_footprint_does_not_depend_on_the_photo_bodies(window):
    """Structural: what `NON_GEOMETRIC_FIELDS` and the gate rely on."""
    _castellated_board(window, rows=6)
    with_bodies = window._current_castellated_params()
    without = window._current_castellated_params(photo_components=False)
    assert with_bodies.components and not without.components
    def text(params):
        # The uuids are random per render; everything else must be identical.
        return re.sub(r'\(uuid "[^"]*"\)', "", window._build_footprint(params).render())
    assert text(with_bodies) == text(without)
    assert gui._geometry_key(with_bodies) == gui._geometry_key(without)


@pytest.mark.parametrize("setup", ["castellated", "matrix_board", "matrix_no_photo"])
def test_the_step_body_record_is_the_one_of_the_generated_body(window, setup):
    """The record built without the photo bodies equals the one of the full body.

    `board` depends on whether photo bodies exist: a pin matrix with photo
    rows is a board even when its family is not `aggregate_tht`.
    """
    if setup == "castellated":
        _castellated_board(window)
    else:
        window.group_castellated.setChecked(False)
        window.group_3d.setChecked(True)
        window._body_size_declared = True
        if setup == "matrix_board":
            window._photo_png = _textured_png(400, 200, 1)
            window._photo_body_fields = "matrix"
            window._set_photo_components(_rows(3), replace_excluded=True)
        else:
            window._set_photo_components([], replace_excluded=True)
    full = window._current_body_params()
    assert full is not None
    assert bool(full.components) == (setup == "matrix_board")
    record = window._step_body_fingerprint()
    assert record == window._step_body_fingerprint(body=full)
    if setup == "matrix_board":
        assert record["board"] is True


@pytest.mark.parametrize("text", ["", " ", "\n\n", " ", "\n  \nabc", "x", "a\n\n"])
def test_the_log_text_check_matches_the_plain_text(qapp, text):
    from PyQt6.QtGui import QTextDocument
    document = QTextDocument()
    document.setPlainText(text)
    assert gui._document_has_visible_text(document) == bool(document.toPlainText().strip())
    assert (not document.isEmpty()) == bool(document.toPlainText())
