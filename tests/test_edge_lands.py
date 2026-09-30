"""@brief The lands of castellated contacts are measured on the calibrated photo (HANDOFF point 169).

User's decision of 2026-09-30: the pad sizes must be recognised precisely,
not estimated by the model run after run. The synthetic boards here draw
lands of KNOWN size at the notch positions, in a copper colour, on a mask
colour, with the disturbances a real board shows: a white silkscreen label
touching a land, a thin trace leaving another, a dark body next to one. Each
rule has its positive and its negative case, told apart by the structure of
the drawing, never by a number tuned to it.
"""
import cv2
import numpy as np
import pytest

from test_photo_inventory_gui import gui  # noqa: F401 - GUI harness (window fixture)
from kicadgen.services import datasheet_extractor as extractor
from kicadgen.services.edge_lands import LandMeasure, land_lines, measure_lands
from kicadgen.services.edge_notches import confirmed_rows
from kicadgen.services.photo_inventory import prepare_photo
from test_edge_notches import BACKGROUND, BOARD, GOLD, LENGTH, MARGIN, ROW_4, SCALE, WIDTH, _px

WHITE = (245, 245, 245)
BLACK = (25, 25, 25)


def land_board(right=ROW_4, land_w=1.8, land_in=2.2, hole_r=0.8, label_on=None, trace_on=None,
               body_on=None, merged=False):
    """A board with lands of `land_w` x `land_in` mm around the right-edge notches.

    @param label_on Index of a contact that gets a white label touching its land.
    @param trace_on Index of a contact with a thin copper trace leaving its land inward.
    @param body_on Index of a contact with a dark body right next to its land.
    @param merged True draws every land as wide as the pitch: copper continuous.
    """
    h, w = _px(WIDTH) + 2 * MARGIN, _px(LENGTH) + 2 * MARGIN
    image = np.full((h, w, 3), BACKGROUND, np.uint8)
    x0, y0, x1, y1 = MARGIN, MARGIN, MARGIN + _px(LENGTH), MARGIN + _px(WIDTH)
    cv2.rectangle(image, (x0, y0), (x1 - 1, y1 - 1), BOARD, -1)
    pitch = 2.54
    for index, position in enumerate(right):
        cy = y0 + _px(position)
        half_w = _px(pitch / 2) if merged else _px(land_w / 2)
        cv2.rectangle(image, (x1 - _px(land_in), cy - half_w), (x1 - 1, cy + half_w - 1), GOLD, -1)
        if trace_on == index:
            cv2.rectangle(image, (x1 - _px(land_in + 6.0), cy - _px(0.15)),
                          (x1 - _px(land_in), cy + _px(0.15)), GOLD, -1)
        if label_on == index:
            cv2.rectangle(image, (x1 - _px(land_in + 1.5), cy - _px(0.4)),
                          (x1 - _px(land_in) - 1, cy + _px(0.4)), WHITE, -1)
        if body_on == index:
            cv2.rectangle(image, (x1 - _px(land_in + 2.5), cy - half_w),
                          (x1 - _px(land_in) - 1, cy + half_w), BLACK, -1)
        # The half-hole: cut by the edge, open outward.
        cv2.circle(image, (x1 - 1, cy), _px(hole_r), BACKGROUND, -1)
    cv2.rectangle(image, (x1, 0), (w - 1, h - 1), BACKGROUND, -1)
    return image


def _measure(image, tmp_path, count=4):
    path = tmp_path / "top.png"
    cv2.imwrite(str(path), image)
    meta = {}
    crop = prepare_photo(path, LENGTH, WIDTH, meta)
    assert crop is not None, meta.get("board_failure")
    profiles = meta["_edge_depth_px"]
    sides = [dict(edge="right", count=count, pitch_mm=2.54, contact_style="half_hole")]
    rows = confirmed_rows(profiles, crop.shape, LENGTH, WIDTH, sides)
    return measure_lands(crop, LENGTH, WIDTH, rows, profiles), rows


def test_clean_lands_are_measured_within_the_pixel_tolerance(tmp_path):
    """@brief Positive: four identical lands give their width and inward extent."""
    lands, rows = _measure(land_board(), tmp_path)
    assert list(rows) == ["right"] and len(rows["right"]["centres_mm"]) == 4
    land = lands["right"]
    assert isinstance(land, LandMeasure) and land.rejected == []
    assert land.width_mm == pytest.approx(1.8, abs=2 * land.tolerance_mm + 1 / SCALE)
    assert land.inward_mm == pytest.approx(2.2, abs=3 / SCALE)
    assert len(land.contacts) == 4
    [(kind, line)] = land_lines(lands)
    assert kind == "log" and "1.8" in line and "4 contatti" in line


def test_a_label_a_trace_and_a_body_do_not_change_the_measure(tmp_path):
    """@brief Positive: other colours next to the land, and a trace narrower than it, are left out."""
    lands, _ = _measure(land_board(label_on=0, trace_on=1, body_on=2), tmp_path)
    land = lands["right"]
    assert land.rejected == []
    assert land.width_mm == pytest.approx(1.8, abs=2 * land.tolerance_mm + 1 / SCALE)
    # Every contact, including the disturbed ones, measures the same width;
    # a body touching the land inward is not mask either and lengthens the
    # inward extent of that contact (declared limit), never the width.
    for index, (width, inward) in enumerate(land.contacts):
        assert width == pytest.approx(1.8, abs=3 / SCALE)
        if index != 2:
            assert inward == pytest.approx(2.2, abs=3 / SCALE)


def test_copper_continuous_between_contacts_is_not_measured(tmp_path):
    """@brief Negative: lands as wide as the pitch merge; nothing is measured, and it is said."""
    lands, _ = _measure(land_board(merged=True), tmp_path)
    land = lands["right"]
    assert land.width_mm is None and land.inward_mm is None
    assert "rame continuo" in land.reason
    [(kind, line)] = land_lines(lands)
    assert kind == "warning" and "non misurabili" in line


def test_a_side_the_photo_does_not_confirm_is_not_measured(tmp_path):
    """@brief Negative: five contacts declared, four notches: no row, no measure."""
    lands, rows = _measure(land_board(), tmp_path, count=5)
    assert rows == {} and lands == {}


# ── From the photo into the result ────────────────────────────────────────

def _answer(pad_width=None, source=None, lands=None):
    side = dict(edge="right", count=4, pitch_mm=2.54, first_pin=1, contact_style="half_hole",
                value_sources={"pitch_mm": "read"})
    if pad_width is not None:
        side["pad_width_mm"] = pad_width
        side["value_sources"]["pad_width_mm"] = source
    data = dict(layout="castellated", confidence="high", drawing_unit="mm", contacts_view="top",
                body_length_mm=LENGTH, body_width_mm=WIDTH, castellated_sides=[side])
    extractor._canonicalize_answer(data)          # the photo stage runs on a converted answer
    if lands is not None:
        data["_measured_lands"] = lands
    return data


MEASURED = {"right": LandMeasure("right", 1.82, 2.2, 0.1, [(1.82, 2.2)] * 4).as_dict()}


@pytest.mark.parametrize("pad_width,source", [(2.54, "derived"), (2.54, "estimated"), (None, None)])
def test_the_measured_width_replaces_what_the_model_did_not_read(pad_width, source):
    params = extractor._params_from_tool_arguments(_answer(pad_width, source, MEASURED))
    [side] = params.castellated_sides
    assert side["pad_width_mm"] == 1.82 and side["land_inward_mm"] == 2.2
    assert params.value_sources["castellated_sides.right.pad_width_mm"] == "measured"
    assert "misurata sulla foto: 1.82 mm" in params.notes
    assert "non e' una misura" not in params.notes      # nothing to downgrade any more


def test_a_width_read_in_the_documents_outranks_the_photo_and_is_compared():
    """@brief Negative: a document measure stays; the photo only confirms or contests it."""
    params = extractor._params_from_tool_arguments(_answer(1.8, "read", MEASURED))
    [side] = params.castellated_sides
    assert side["pad_width_mm"] == 1.8
    assert params.value_sources["castellated_sides.right.pad_width_mm"] == "read"
    assert "si tiene la lettura, coerente" in params.notes
    params = extractor._params_from_tool_arguments(_answer(2.4, "read", MEASURED))
    assert params.castellated_sides[0]["pad_width_mm"] == 2.4
    assert "DIVERSA" in params.notes


def test_a_raw_answer_cannot_smuggle_a_measurement():
    """@brief Negative: `_measured_lands` in a model answer is dropped by the conversion."""
    data = dict(layout="castellated", confidence="high", drawing_unit="mm", contacts_view="top",
                body_length_mm=LENGTH, body_width_mm=WIDTH, _measured_lands=MEASURED,
                castellated_sides=[dict(edge="right", count=4, pitch_mm=2.54, first_pin=1,
                                        contact_style="half_hole")])
    params = extractor._params_from_tool_arguments(data)
    assert "pad_width_mm" not in params.castellated_sides[0]
    assert "castellated_sides.right.pad_width_mm" not in params.value_sources


def test_the_gui_treats_a_measured_width_as_declared(window):  # noqa: F811
    from kicadgen.generators.castellated_module import is_declared_size
    params = extractor._params_from_tool_arguments(_answer(2.54, "derived", MEASURED))
    window.group_castellated.setChecked(True)
    window._apply_extracted_castellated(params)
    window.field_cast_body_length.setValue(LENGTH)
    window.field_cast_body_width.setValue(WIDTH)
    module = window._current_castellated_params()
    right = next(side for side in module.sides if side.edge == "right")
    assert is_declared_size(right.pad_width) and float(right.pad_width) == 1.82
    module.validate()
