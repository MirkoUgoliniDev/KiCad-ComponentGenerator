"""Offline regressions for rotated CAD rectangles and photo calibration."""

import json
import math
from pathlib import Path

import pytest

from kicadgen.cad3d.step_generator import (
    clamp_component_position, find_overlapping_components,
)


@pytest.mark.parametrize("angle", [30, 45, 60, -30, 135, 225, 390])
@pytest.mark.parametrize("sign_x,sign_y", [(1, 1), (-1, 1), (1, -1), (-1, -1)])
def test_arbitrary_rotation_clamp_contains_every_corner(angle, sign_x, sign_y):
    x, y = clamp_component_position(100 * sign_x, 100 * sign_y, 8, 2, angle, 20, 12)
    c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    corners = [(x + a * 4 * c - b * s, y + a * 4 * s + b * c)
               for a in (-1, 1) for b in (-1, 1)]
    assert max(abs(px) for px, _ in corners) == pytest.approx(9.8)
    assert max(abs(py) for _, py in corners) == pytest.approx(5.8)
    assert clamp_component_position(0.5, -0.5, 8, 2, angle, 20, 12) == (0.5, -0.5)


@pytest.mark.parametrize("angle", [0, 90, 180, 270, -90, 450])
def test_cardinal_clamp_preserves_exact_limits(angle):
    expected = (5.8, 4.8) if angle % 180 == 0 else (8.8, 1.8)
    assert clamp_component_position(100, 100, 8, 2, angle, 20, 12) == expected


def _component(name, x, y, angle, length=8, width=1):
    return dict(name=name, x=x, y=y, rotation_deg=angle,
                length_mm=length, width_mm=width)


def test_diagonal_real_overlap_missed_by_unrotated_boxes():
    components = [_component("A", 0, 0, 45), _component("B", 2, 2, -45)]
    warnings = find_overlapping_components(components)
    assert len(warnings) == 1
    assert "'A'" in warnings[0] and "'B'" in warnings[0]


@pytest.mark.parametrize("angle", [30, 45, -45, 135])
@pytest.mark.parametrize("distance,overlaps", [(0.8, True), (0.96, False), (1, False), (1.2, False)])
def test_parallel_diagonal_rectangles_use_actual_edges(angle, distance, overlaps):
    # Offset perpendicular to the long edge: AABBs overlap in every case.
    theta = math.radians(angle)
    components = [_component("A", 0, 0, angle),
                  _component("B", -distance * math.sin(theta), distance * math.cos(theta), angle)]
    assert bool(find_overlapping_components(components)) is overlaps
    assert bool(find_overlapping_components(components[::-1])) is overlaps


def test_sat_checks_both_rectangles_axes():
    # A's axes overlap but B's diagonal short axis separates the rectangles.
    components = [_component("A", 0, 0, 0, 2, 2),
                  _component("B", 2, 2, -45, 6, 0.5)]
    assert find_overlapping_components(components) == []
    assert find_overlapping_components(components[::-1]) == []


def _board_image(angle, size=(800, 320)):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    image = np.full((600, 1400, 3), 235, dtype=np.uint8)
    corners = cv2.boxPoints(((700, 300), size, angle))
    cv2.fillConvexPoly(image, np.rint(corners).astype(np.int32), (22, 22, 22))
    return image


@pytest.mark.parametrize("angle", [0, 0.2, -0.2])
def test_near_axis_raster_tolerance(angle):
    from kicadgen.services.photo_refine import find_board
    frame = find_board(_board_image(angle), 40, 16)
    assert frame is not None
    assert frame.scale == pytest.approx(20, rel=0.015)
    assert frame.to_mm(700, 300) == pytest.approx((0, 0), abs=0.05)


@pytest.mark.parametrize("angle", [3, -3, 177])
@pytest.mark.parametrize("size", [(800, 320), (100, 40)])
def test_tilted_board_cannot_supply_axis_aligned_metric_frame(angle, size):
    from kicadgen.services.photo_refine import find_board
    assert find_board(_board_image(angle, size), 40, 16) is None


def test_a_moderately_tilted_photo_is_rectified_instead_of_rejected(tmp_path):
    """Una foto a mano libera e' quasi sempre storta di qualche grado.

    Rifiutarla costringeva a ripiegare su un disegno a bassa risoluzione. Il
    raddrizzamento e' una rotazione rigida: la scala resta quella vera, e
    infatti la misura torna quella attesa.
    """
    cv2 = pytest.importorskip("cv2")
    from kicadgen.services.photo_inventory import prepare_photo
    path = tmp_path / "tilted.png"
    assert cv2.imwrite(str(path), _board_image(3))
    crop = prepare_photo(path, 40, 16, {})
    assert crop is not None, "una foto storta di 3 gradi e' ancora rifiutata"
    assert crop.shape[1] / 40 == pytest.approx(20, rel=0.05)


def test_tilt_rejection_uses_existing_calibration_warning_without_api(tmp_path):
    cv2 = pytest.importorskip("cv2")
    from kicadgen.services.photo_inventory import extract_inventory, prepare_photo
    from kicadgen.services.photo_refine import refine_components
    # Oltre il limite di raddrizzamento non e' una vista dall'alto storta ma
    # un'inquadratura prospettica, che una rotazione rigida non correggerebbe.
    path = tmp_path / "tilted.png"
    assert cv2.imwrite(str(path), _board_image(35))
    metadata = {}
    assert prepare_photo(path, 40, 16, metadata) is None
    assert metadata == {}

    def no_api(*args, **kwargs):
        pytest.fail("Rejected calibration must not call a model")

    report = extract_inventory([path], "unused", 40, 16, [], no_api, top_photo_index=0)
    assert report.components == []
    assert report.calibration == {}
    assert report.warnings == ["Nessuna vista dall'alto calibrabile: resta l'inventario principale."]
    components = [dict(name="R1", x_mm=2, y_mm=1, length_mm=2, width_mm=1)]
    refined, refinement = refine_components([path], 40, 16, components)
    assert refined == components
    assert refinement.photo is None and refinement.measured == 0


def test_provided_screenshot_crop_unchanged():
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    from kicadgen.services.photo_inventory import prepare_photo
    reference = json.loads((Path(__file__).parent / "fixtures" / "pcb_reference.json").read_text())
    path = Path(reference["image"]["original_path"])
    if not path.is_file():
        pytest.skip("Local reference screenshot is not installed")
    metadata = {}
    crop = prepare_photo(path, 42.8, 15.2, metadata)
    assert crop is not None
    assert metadata["source_sha256"] == reference["image"]["sha256"]
    assert metadata["crop_bbox_original_px"] == [315, 117, 1435, 519]
    original = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    assert np.array_equal(crop, original[117:519, 315:1435])
