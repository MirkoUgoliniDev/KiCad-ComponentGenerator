"""@brief Fixes of the 2026-11-16 review, photo and drawing scope (R8-1 .. R8-9, fixer C7).

Each rule is tested with a positive and a negative case that differ for a
structural reason:

- R8-1 the EXIF orientation is applied by every local reader exactly as the
  AI request applies it (Pillow's tag and transforms), whatever the decoder;
- R8-2 «Interrompi» (Stop) stops the local photo phases between two steps and
  the review window never opens after it;
- R8-3 the board search no longer converts the whole photo to float L*a*b*
  at once (same values, bounded memory), a photo too large for the memory is
  a warning, the search is done once;
- R8-4 pin numbers that str.isdigit() accepts but int() refuses, and
  infinite counts, no longer raise;
- R8-5 an unwritable detector audit is logged, the detections are returned;
- R8-6 the response caches are written through a temporary file;
- R8-7 the SMD provenance names the network file actually loaded;
- R8-8 default names and review reasons go through tx, provenance codes are
  English codes;
- R8-9 a board box leaving the photo gets its own reason.
"""
from __future__ import annotations

import io
import json
import math
import threading
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image, ImageOps

from kicadgen.services import drawing_holes, messages, photo_inventory, photo_refine, photo_yolo
from kicadgen.services import pinout_coherence as pc
from kicadgen.services.ai_diagnostics import CallCancelled
from kicadgen.services.photo_orientation import apply_orientation, decode_upright, exif_orientation

from test_drawing_holes import STAGGERED, draw_part
from test_photo_yolo import _raw, setup  # noqa: F401 - `setup` is a fixture used by name


# ---------------------------------------------------------------------------
# R8-1: EXIF orientation as the AI request applies it
# ---------------------------------------------------------------------------

def _tagged(pixels_bgr, orientation, fmt="PNG"):
    """Encode BGR pixels with an EXIF orientation tag (pixels stored as given)."""
    exif = Image.Exif()
    exif[0x0112] = orientation
    out = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(pixels_bgr[:, :, ::-1])).save(
        out, format=fmt, exif=exif.tobytes())
    return out.getvalue()


def _as_request_sees(data):
    """The pixels the AI request sends: Pillow's exif_transpose, as BGR."""
    with Image.open(io.BytesIO(data)) as picture:
        return np.asarray(ImageOps.exif_transpose(picture).convert("RGB"))[:, :, ::-1]


@pytest.mark.parametrize("fmt", ["PNG", "TIFF"])
@pytest.mark.parametrize("orientation", range(1, 9))
def test_decoded_pixels_are_the_pixels_the_request_sends(fmt, orientation):
    """@brief Every orientation and container gives the request's picture.

    @note TIFF is the structural negative of «let OpenCV apply the tag»: its
        decoder applies the tag even when told to ignore it, so applying the
        tag again on OpenCV's pixels would turn the picture twice.
    """
    base = np.random.default_rng(orientation).integers(0, 256, (12, 20, 3), dtype=np.uint8)
    data = _tagged(base, orientation, fmt)
    expected = _as_request_sees(data)
    assert np.array_equal(decode_upright(data), expected)
    assert np.array_equal(decode_upright(data, keep_alpha=True)[:, :, :3], expected)


def test_pillow_failure_falls_back_to_opencv_with_the_same_transform(monkeypatch):
    """@brief A rotated image Pillow cannot decode is turned by the same table."""
    from kicadgen.services import photo_orientation
    base = np.random.default_rng(7).integers(0, 256, (12, 20, 3), dtype=np.uint8)
    data = _tagged(base, 6)
    expected = _as_request_sees(data)
    monkeypatch.setattr(photo_orientation, "_pillow_upright", lambda raw, keep_alpha: None)
    assert np.array_equal(decode_upright(data), expected)
    # Negative: without a tag nothing is turned.
    assert np.array_equal(decode_upright(_tagged(base, 1)), base)


def test_orientation_tag_values_outside_the_standard_are_upright():
    """@brief Only 1..8 is an orientation; anything else, or no header, is «upright»."""
    base = np.zeros((4, 6, 3), np.uint8)
    assert exif_orientation(_tagged(base, 6)) == 6
    assert exif_orientation(b"not an image") == 1
    assert exif_orientation(_tagged(base, 9)) == 1
    assert apply_orientation(base, 1) is base


def test_read_photo_applies_the_tag_and_leaves_an_untagged_photo_as_stored(tmp_path):
    """@brief The calibration reads the phone photo upright, an untagged one as stored."""
    stored = np.zeros((30, 50, 3), np.uint8)
    stored[:5, :10] = 255
    tagged, plain = tmp_path / "phone.png", tmp_path / "plain.png"
    tagged.write_bytes(_tagged(stored, 6))
    plain.write_bytes(_tagged(stored, 1))
    assert photo_inventory.read_photo(tagged).shape == (50, 30, 3)
    assert np.array_equal(photo_inventory.read_photo(tagged), _as_request_sees(tagged.read_bytes()))
    assert np.array_equal(photo_inventory.read_photo(plain), stored)


def test_phone_photo_of_a_drawing_is_checked_upright(tmp_path):
    """@brief A drawing stored mirrored with its EXIF tag is confirmed as the model sees it.

    @note Positive: the tagged file, upright for the model and now for the
        hole check too. Negative: the SAME stored pixels without the tag are
        really mirrored, and a mirrored drawing is not confirmed (the check
        tells a mirror apart, test_mirrored_reading_is_not_confirmed): the
        two differ only by the tag. Before the fix both were read mirrored.
    """
    drawing_holes.clear_analysis_cache()
    upright = cv2.imread(str(draw_part(tmp_path / "upright.png", STAGGERED)))
    stored = upright[:, ::-1].copy()   # what a mirror tag (2) restores
    tagged, untagged = tmp_path / "phone.png", tmp_path / "bare.png"
    tagged.write_bytes(_tagged(stored, 2))
    untagged.write_bytes(_tagged(stored, 1))
    try:
        assert drawing_holes.check_pad_layout([tagged], STAGGERED).status == "confirmed"
        assert drawing_holes.check_pad_layout([untagged], STAGGERED).status != "confirmed"
    finally:
        drawing_holes.clear_analysis_cache()


# ---------------------------------------------------------------------------
# R8-2: «Interrompi» during the local phases
# ---------------------------------------------------------------------------

def test_detector_stops_between_views(setup):  # noqa: F811
    """@brief A cancel set during the first view stops before the second; unset, every view runs."""
    session, _ = setup
    session.raw = _raw(([320, 320, 100, 100], 10, 0.9))
    photo = np.zeros((640, 1000, 3), np.uint8)
    stop = threading.Event()

    def runs_then_stop(index):
        stop.set()
        return _raw(([320, 320, 100, 100], 10, 0.9))
    session.raw = runs_then_stop
    with pytest.raises(CallCancelled):
        photo_yolo.detect_yolo_components(photo, 100, 64, cancel=stop)
    assert len(session.calls) == 1
    # Negative: the same detection without a request runs every view.
    session.calls.clear()
    session.raw = _raw(([320, 320, 100, 100], 10, 0.9))
    found = photo_yolo.detect_yolo_components(photo, 100, 64, cancel=threading.Event())
    assert len(session.calls) > 1 and found


def test_detector_accepts_a_predicate_and_a_broken_channel_is_no_request(setup):  # noqa: F811
    """@brief A predicate is a channel; one that raises never stops the work."""
    session, _ = setup
    photo = np.zeros((640, 640, 3), np.uint8)
    with pytest.raises(CallCancelled):
        photo_yolo.detect_yolo_components(photo, 64, 64, cancel=lambda: True)
    assert not session.calls

    def broken():
        raise RuntimeError("channel gone")
    photo_yolo.detect_yolo_components(photo, 64, 64, cancel=broken)
    assert session.calls


def _board_photo(path):
    """A dark 200 x 100 px board centred on a light background, 10 px/mm for 20 x 10 mm."""
    image = np.full((300, 400, 3), 230, np.uint8)
    image[100:200, 100:300] = (40, 90, 30)
    assert cv2.imwrite(str(path), image)
    return path


def test_prepare_photo_stops_when_cancelled_and_calibrates_otherwise(tmp_path):
    """@brief Set channel: CallCancelled before any work; unset: the calibrated crop."""
    photo = _board_photo(tmp_path / "board.png")
    with pytest.raises(CallCancelled):
        photo_inventory.prepare_photo(photo, 20.0, 10.0, {}, cancel=lambda: True)
    crop = photo_inventory.prepare_photo(photo, 20.0, 10.0, {}, cancel=lambda: False)
    assert crop is not None and abs(crop.shape[1] - 200) <= 4 and abs(crop.shape[0] - 100) <= 4


def _yolo_only_run(monkeypatch, tmp_path, *, press_stop_in_detector):
    """Run the yolo_only branch with a fake calibration and detector; return the reviews opened."""
    photo = np.zeros((100, 200, 3), np.uint8)
    stop = threading.Event()
    monkeypatch.setattr(photo_inventory, "prepare_photo", lambda *a, **k: photo.copy())

    def detect(*args, **kwargs):
        if press_stop_in_detector:
            stop.set()   # the user presses «Interrompi» while YOLO runs
        return [dict(_id=1, _box=(0.1, 0.1, 0.3, 0.3), name="x", kind="generic",
                     x_mm=-6.0, y_mm=-4.0, length_mm=4.0, width_mm=2.0, height_mm=1.0,
                     rotation_deg=0, color_hex="#303030", _uncertain=True)]
    monkeypatch.setattr(photo_yolo, "detect_yolo_components", detect)
    opened = []

    def review(photo_png, proposals, length, width):
        opened.append(len(proposals))
        return proposals
    tmp_path.joinpath("top.png").write_bytes(b"")
    run = lambda: photo_inventory.extract_inventory(  # noqa: E731
        [tmp_path / "top.png"], "", 20.0, 10.0, [], lambda **kw: pytest.fail("paid call"),
        top_photo_index=0, detector="yolo_only", review_proposals=review, cancel=stop)
    return run, opened


def test_review_window_never_opens_after_interrompi(monkeypatch, tmp_path):
    """@brief Stop pressed during detection: CallCancelled and no review; not pressed: one review."""
    run, opened = _yolo_only_run(monkeypatch, tmp_path, press_stop_in_detector=True)
    with pytest.raises(CallCancelled):
        run()
    assert opened == []
    run, opened = _yolo_only_run(monkeypatch, tmp_path, press_stop_in_detector=False)
    run()
    assert opened == [1]


def test_calibration_loop_stops_between_photos(monkeypatch, tmp_path):
    """@brief Stop pressed while the first photo is calibrated: the second is never read."""
    for name in ("a.png", "b.png"):
        tmp_path.joinpath(name).write_bytes(b"")
    for press in (True, False):
        stop, prepared = threading.Event(), []

        def prepare(path, *args, **kwargs):
            prepared.append(Path(path).name)
            if press:
                stop.set()
            return None
        monkeypatch.setattr(photo_inventory, "prepare_photo", prepare)
        call = lambda: photo_inventory.extract_inventory(  # noqa: E731
            [tmp_path / "a.png", tmp_path / "b.png"], "", 20.0, 10.0, [],
            lambda **kw: pytest.fail("paid call"), top_photo_index=[0, 1],
            detector="yolo_only", cancel=stop)
        if press:
            with pytest.raises(CallCancelled):
                call()
            assert prepared == ["a.png"]
        else:
            call()
            assert prepared == ["a.png", "b.png"]


def test_without_a_channel_the_calibration_gets_no_cancel_argument(monkeypatch, tmp_path):
    """@brief Callers and replacements without a channel keep their signature."""
    seen = []

    def prepare(path, length, width, calibration):
        seen.append(path)
        return None
    monkeypatch.setattr(photo_inventory, "prepare_photo", prepare)
    tmp_path.joinpath("a.png").write_bytes(b"")
    photo_inventory.extract_inventory([tmp_path / "a.png"], "", 20.0, 10.0, [],
                                      lambda **kw: pytest.fail("paid call"), top_photo_index=0,
                                      detector="yolo_only")
    assert len(seen) == 1


# ---------------------------------------------------------------------------
# R8-3: memory of the board search
# ---------------------------------------------------------------------------

def test_banded_background_distance_equals_the_whole_image_formula():
    """@brief Same float32 values bit for bit, on a height that leaves a partial band."""
    rng = np.random.default_rng(3)
    image = rng.integers(0, 256, (photo_refine.LAB_BAND_ROWS * 2 + 37, 301, 3), dtype=np.uint8)
    background = photo_refine.estimate_background(image)
    whole = np.linalg.norm(photo_refine._lab(image) - np.array(background.lab, np.float32), axis=2)
    banded = photo_refine._background_distance(image, background)
    assert banded.dtype == whole.dtype and np.array_equal(banded, whole)


def test_background_estimate_from_the_ring_equals_the_whole_conversion():
    """@brief Converting only the ring gives the estimate of the whole conversion."""
    rng = np.random.default_rng(4)
    image = rng.integers(0, 256, (200, 260, 3), dtype=np.uint8)
    image[20:-20, 20:-20] = (10, 200, 40)
    estimate = photo_refine.estimate_background(image)
    lab = photo_refine._lab(image)
    ring = max(photo_refine.BACKGROUND_RING_MIN_PX,
               int(round(photo_refine.BACKGROUND_RING_FRACTION * 200)))
    samples = np.concatenate([lab[:ring].reshape(-1, 3), lab[-ring:].reshape(-1, 3),
                              lab[ring:-ring, :ring].reshape(-1, 3),
                              lab[ring:-ring, -ring:].reshape(-1, 3)])
    assert estimate.lab == tuple(float(v) for v in np.median(samples, axis=0))


def test_a_photo_too_large_for_the_memory_is_a_warning(monkeypatch, tmp_path):
    """@brief MemoryError: a warning and the main inventory; an OSError keeps its own message."""
    tmp_path.joinpath("a.png").write_bytes(b"")

    def run(error):
        def prepare(*args, **kwargs):
            raise error
        monkeypatch.setattr(photo_inventory, "prepare_photo", prepare)
        return photo_inventory.extract_inventory(
            [tmp_path / "a.png"], "", 20.0, 10.0, [], lambda **kw: pytest.fail("paid call"),
            top_photo_index=0, detector="yolo_only")
    report = run(MemoryError())
    assert any("memoria" in str(w) for w in report.warnings) and not report.components
    report = run(OSError("locked"))
    assert any("locked" in str(w) for w in report.warnings)
    assert not any("memoria" in str(w) for w in report.warnings)


def test_the_most_detailed_calibrated_view_wins_and_ties_keep_the_first(monkeypatch, tmp_path):
    """@brief Keeping only the best view while trying the others changes no choice."""
    sizes = {"a.png": (50, 100), "b.png": (100, 200), "c.png": (100, 200)}
    for name in sizes:
        tmp_path.joinpath(name).write_bytes(b"")
    monkeypatch.setattr(photo_inventory, "prepare_photo",
                        lambda path, *a, **k: np.zeros(sizes[Path(path).name] + (3,), np.uint8))
    # The view choice is under test, not the detector: a fake one, so the
    # test needs no model weights (the GitHub runner has none; ci-green).
    monkeypatch.setattr(photo_yolo, "detect_yolo_components", lambda *a, **k: [])
    report = photo_inventory.extract_inventory(
        [tmp_path / n for n in sizes], "", 20.0, 10.0, [], lambda **kw: pytest.fail("paid"),
        top_photo_index=[0, 1, 2], detector="yolo_only")
    assert report.photo == "b.png"


def test_board_search_runs_once_when_the_board_is_not_found(monkeypatch, tmp_path):
    """@brief The reason comes from the one search; a found board searches once too."""
    blank = tmp_path / "blank.png"
    cv2.imwrite(str(blank), np.full((300, 400, 3), 230, np.uint8))
    board = _board_photo(tmp_path / "board.png")
    calls = []
    real = photo_refine.locate_board

    def counted(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)
    monkeypatch.setattr(photo_refine, "locate_board", counted)
    metadata = {}
    assert photo_inventory.prepare_photo(blank, 20.0, 10.0, metadata) is None
    assert metadata["board_failure"] and len(calls) == 1
    calls.clear()
    assert photo_inventory.prepare_photo(board, 20.0, 10.0, {}) is not None
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# R8-9: a board box leaving the photo has its own reason
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("inside", [True, False])
def test_board_box_outside_the_photo_has_a_reason(monkeypatch, tmp_path, inside):
    """@brief Outside: None with a specific reason; inside: the crop and no failure."""
    photo = _board_photo(tmp_path / "board.png")
    centre_x = 200.0 if inside else 360.0   # 200 px wide box: 260..460 leaves a 400 px photo
    frame = photo_refine.BoardFrame(cx=centre_x, cy=150.0, scale=10.0, width_px=200.0,
                                    height_px=100.0)
    monkeypatch.setattr(photo_refine, "locate_board",
                        lambda *a, **k: photo_refine.BoardSearch(frame, photo_refine.BOARD_FOUND))
    metadata = {}
    crop = photo_inventory.prepare_photo(photo, 20.0, 10.0, metadata)
    if inside:
        assert crop is not None and "board_failure" not in metadata
    else:
        assert crop is None and "esce dai bordi" in metadata["board_failure"]


# ---------------------------------------------------------------------------
# R8-4: pin numbers int() cannot read, infinite counts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["①", "²", "³"])
def test_unicode_digits_are_labels_not_integers(text):
    """@brief isdigit() but not int(): sorted and listed as labels, never converted."""
    assert text.isdigit() and not pc.is_integer_text(text)
    assert pc.pin_sort_key(text) == (1, 0, text)
    assert pc.pin_ranges_text(["1", "2", text, "4"]) == f"1-2, 4, {text}"
    # Negative: ASCII digits stay integers.
    assert pc.is_integer_text("12") and pc.pin_sort_key("12") == (0, 12, "")


def test_pinout_with_unicode_digits_gives_placements_and_shifts():
    """@brief A pinout naming «①» is read without ValueError; integer entries still align."""
    pinout = [{"pin": "1", "name": "GND"}, {"pin": "2", "name": "TX"}, {"pin": "3", "name": "RX"},
              {"pin": "①", "name": "VCC"}]
    side = {"edge": "left", "count": 2, "first_pin": 1, "pin_names": ["GND", "TX"]}
    entries = pc.document_pinout_entries(pinout)
    placements = pc.side_placements(side, entries)
    assert any(item["first_pin"] == 1 and not item["reverse"] for item in placements)
    assert isinstance(pc.uniform_shifts([side], pinout), list)


@pytest.mark.parametrize("value, first, count", [
    (math.inf, 1, 0), (-math.inf, 1, 0), (math.nan, 1, 0), (3, 3, 3)])
def test_infinite_first_pin_and_count_are_unusable_not_a_crash(value, first, count):
    """@brief inf/nan fall back as any unusable value; a finite one is kept."""
    side = {"first_pin": value, "count": value}
    assert pc._first_pin(side) == first and pc._count(side) == count
    assert pc.side_pad_numbers(side) == list(range(first, first + count))


def test_count_above_the_pad_limit_is_unusable_and_at_the_limit_is_kept():
    """@brief Above MAX_PAD_POSITIONS a count is unusable (0, no list built); at the limit it is kept.

    @note The bound is the declared one of kicadgen.core.pad_limits, shared
        with the generators: no threshold of this module.
    """
    from kicadgen.core.pad_limits import MAX_PAD_POSITIONS
    over = {"count": 10 ** 9, "first_pin": 1, "pin_names": []}
    assert pc._count(over) == 0 and pc.side_pad_numbers(over) == []
    assert pc._count({"count": MAX_PAD_POSITIONS + 1}) == 0
    at_limit = {"count": MAX_PAD_POSITIONS, "first_pin": 1}
    assert pc._count(at_limit) == MAX_PAD_POSITIONS
    assert len(pc.side_pad_numbers(at_limit)) == MAX_PAD_POSITIONS


def _sides(*counts):
    """Castellated sides with the given contact counts, numbered one after the other."""
    edges, first, sides = ("left", "bottom", "right", "top"), 1, []
    for edge, count in zip(edges, counts):
        sides.append({"edge": edge, "count": count, "first_pin": first})
        first += count if isinstance(count, int) else 0
    return sides


def _generator_verdict(sides):
    """None when the castellated generator accepts the sides, else its error text."""
    from kicadgen.generators.castellated_module import (CONTACT_HALF_HOLE, CastellatedModuleParams,
                                                        CastellatedSide)
    params = CastellatedModuleParams(
        name="M", body_length=1600.0, body_width=1600.0,
        sides=[CastellatedSide(side["edge"], side["count"], 0.5, first_pin=side["first_pin"],
                               pad_width=0.3, contact_style=CONTACT_HALF_HOLE) for side in sides])
    try:
        params.validate()
    except ValueError as exc:
        return str(exc)
    return None


@pytest.mark.parametrize("per_side, usable", [(3000, False), (2500, True)])
def test_total_count_bound_agrees_with_the_generator(per_side, usable):
    """@brief Four sides of 3000 (12000): unusable for coherence AND refused by the generator;
    four sides of 2500 (10000, the limit): accepted by both.

    @note Structural difference: the TOTAL over the sides crosses the shared
        bound (pad_limits.MAX_PAD_POSITIONS) in the first case only; each
        side alone is far below it in both. An unusable report says why
        instead of listing every pinout entry as «off the sides».
    """
    from kicadgen.core.pad_limits import MAX_PAD_POSITIONS
    pinout = [{"pin": str(n), "name": f"P{n}"} for n in range(1, 4)]
    sides = _sides(per_side, per_side, per_side, per_side)
    report = pc.side_pinout_report(sides, pinout)
    kinds = [finding["kind"] for finding in pc.document_findings(report)]
    verdict = _generator_verdict(sides)
    if usable:
        assert pc.count_unusable(sides) is None and report["count_unusable"] is None
        assert report["applicable"] and pc.FINDING_COUNT_UNUSABLE not in kinds
        assert verdict is None
    else:
        total = 4 * per_side
        assert pc.count_unusable(sides) == {"total": total, "limit": MAX_PAD_POSITIONS}
        assert report["count_unusable"] and not report["applicable"]
        assert not report["unexplained_entries"] and not report["needs_question"]
        assert kinds == [pc.FINDING_COUNT_UNUSABLE]
        assert verdict is not None and str(total) in verdict and str(MAX_PAD_POSITIONS) in verdict


def test_an_infinite_count_is_unusable_and_a_missing_one_is_not():
    """@brief inf has no total (None); a missing count is «no contacts», as before."""
    from kicadgen.core.pad_limits import MAX_PAD_POSITIONS
    assert pc.count_unusable(_sides(math.inf)) == {"total": None, "limit": MAX_PAD_POSITIONS}
    assert pc.count_unusable([{"edge": "left"}]) is None


# ---------------------------------------------------------------------------
# R8-5: unwritable detector audit
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("writable", [True, False])
def test_unwritable_audit_keeps_the_detections(setup, tmp_path, writable):  # noqa: F811
    """@brief Unwritable: a warning and the detections; writable: the audit file."""
    session, _ = setup
    session.raw = _raw(([200, 200, 100, 100], 10, 0.9))
    target = tmp_path / "audit"
    if not writable:
        target.write_text("a file, not a folder")
    lines = []
    found = photo_yolo.detect_yolo_components(np.zeros((640, 640, 3), np.uint8), 64, 64,
                                              audit_dir=target, log=lines.append)
    assert len(found) == 1
    failures = [line for line in lines if "Cannot write YOLO audit" in str(line)]
    if writable:
        assert not failures and list(target.glob("yolo-*.json"))
    else:
        assert failures and messages.kind_of(failures[0]) == "warning"


# ---------------------------------------------------------------------------
# R8-6: response caches written through a temporary file
# ---------------------------------------------------------------------------

def _cache_kwargs():
    """Arguments of a cached grounding call (a tool without a schema accepts any dict)."""
    return dict(model="m", system_prompt="s", content_blocks=[{"type": "text", "text": "t"}],
                tool={"function": {"name": "tool"}})


@pytest.mark.parametrize("replace_fails", [True, False])
def test_interrupted_cache_write_keeps_the_previous_entry(monkeypatch, tmp_path, replace_fails):
    """@brief A failed replace leaves the previous bytes whole; a working one replaces them.

    @note No temporary file is left behind in either case.
    """
    import hashlib
    from kicadgen import fsutil
    from kicadgen.services import photo_grounding as grounding
    kwargs = _cache_kwargs()
    key = hashlib.sha256(json.dumps(kwargs, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    entry = tmp_path / f"{key}.json"
    entry.write_bytes(b"previous, unreadable entry")
    if replace_fails:
        def broken_replace(source, target, **options):
            raise OSError("disk full")
        monkeypatch.setattr(fsutil, "replace_file", broken_replace)
    logs = []
    answer = grounding.cached_call(lambda **kw: {"components": [1]}, kwargs, tmp_path, "stage",
                                   logs.append)
    assert answer == {"components": [1]}
    assert not [path for path in tmp_path.iterdir() if path != entry]
    if replace_fails:
        assert entry.read_bytes() == b"previous, unreadable entry"
        assert any("non scrivibile" in str(line) for line in logs)
    else:
        assert json.loads(entry.read_text(encoding="utf-8"))["response"] == {"components": [1]}


@pytest.mark.parametrize("module_name", ["photo_crop_reading", "photo_global_inventory",
                                         "photo_grounding"])
def test_every_response_cache_goes_through_the_atomic_writer(module_name):
    """@brief The three cache writers use fsutil.atomic_write_bytes, none writes in place."""
    import inspect
    import importlib
    source = inspect.getsource(importlib.import_module(f"kicadgen.services.{module_name}"))
    assert "atomic_write_bytes" in source
    assert ".write_text(json.dumps(" not in source


# ---------------------------------------------------------------------------
# R8-7: SMD provenance = the network file loaded
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("network", ["models/smd/smd_classifier.onnx", "elsewhere/my_net.pt"])
def test_smd_provenance_names_the_loaded_network(monkeypatch, network):
    """@brief Two different files give two different provenances, each its own name."""
    from kicadgen.services import smd_component_classifier as smd
    received = []

    def classify(photo, boxes, **kwargs):
        received.append(Path(kwargs["model_path"]))
        return [{"class": "chip_R", "kind": "resistor", "package": "0603", "accepted": True,
                 "rotation_deg": 0.0, "rotation_confidence": 0.9, "model_match": None,
                 "footprint": None, "footprint_candidates": []} for _ in boxes]
    monkeypatch.setattr(smd, "classify_photo_boxes", classify)
    rows = [dict(_id=1, _box=(0.1, 0.1, 0.3, 0.3), length_mm=1.6, width_mm=0.8)]
    photo_inventory.classify_smd_rows(rows, np.zeros((50, 50, 3), np.uint8), None, (0.1, 0.1),
                                      lambda m: None, None, lambda *a: None, model_path=network)
    assert received == [Path(network)]
    assert rows[0]["_category_source"] == rows[0]["package_source"] == Path(network).name


# ---------------------------------------------------------------------------
# R8-8: translated names and reasons, English provenance codes
# ---------------------------------------------------------------------------

def test_default_names_and_reasons_follow_the_interface_language(monkeypatch, setup):  # noqa: F811
    """@brief Names and review reasons pass through the catalog; provenance codes do not."""
    monkeypatch.setattr(messages, "_translator",
                        lambda key, default: "<" + default + ">")
    assert photo_yolo.candidate_name(4) == "<Candidato YOLO 4>"
    manual = photo_yolo.manual_proposal((0.1, 0.1, 0.2, 0.2), 9, 20.0, 10.0)
    assert manual["name"] == "<Corpo aggiunto 9>"
    session, _ = setup
    session.raw = _raw(([200, 200, 100, 100], 10, 0.9))
    found = photo_yolo.detect_yolo_components(np.zeros((640, 640, 3), np.uint8), 64, 64)
    assert found[0]["name"] == "<Candidato YOLO 1>"
    # Negative: the provenance code sent to the paid reconciliation is not
    # translated -- the prompts are English whatever the interface language.
    assert found[0]["_source"] == photo_yolo.LOCAL_SOURCE == "YOLO local"
    assert photo_yolo.RECOVERED_SOURCE.isascii()


def test_smd_review_reason_is_translated(monkeypatch):
    """@brief Accepted and refused verdicts both pass through the catalog."""
    from kicadgen.services import smd_component_classifier as smd
    monkeypatch.setattr(messages, "_translator", lambda key, default: "<" + default + ">")
    for accepted in (True, False):
        monkeypatch.setattr(smd, "classify_photo_boxes", lambda photo, boxes, **kw: [
            {"class": "chip_R", "kind": "resistor", "package": "0603", "accepted": accepted,
             "rotation_deg": 0.0, "rotation_confidence": 0.9, "model_match": None,
             "footprint": None, "footprint_candidates": []} for _ in boxes])
        rows = [dict(_id=1, _box=(0.1, 0.1, 0.3, 0.3), length_mm=1.6, width_mm=0.8)]
        photo_inventory.classify_smd_rows(rows, np.zeros((50, 50, 3), np.uint8), None,
                                          (0.1, 0.1), lambda m: None, None, lambda *a: None,
                                          model_path="n.onnx")
        reason = rows[0]["_review_reason"]
        assert reason.startswith("<") and reason.endswith(">")
        assert ("rete locale;" in reason) is accepted


# ---------------------------------------------------------------------------
# R1-6 support: the terminal-detail reading can be cancelled
# ---------------------------------------------------------------------------

def _terminal_inputs():
    """A board photo and two multi-lead bodies, each observed by its own paid call."""
    from test_photo_terminals import component
    png = cv2.imencode(".png", np.full((100, 200, 3), 70, np.uint8))[1].tobytes()
    return png, [component(), component()]


def _terminal_answer(**kwargs):
    """A valid answer for the target named in the request."""
    from test_photo_terminals import observation
    text = kwargs["content_blocks"][0]["text"]
    identifier = int(text.split("Target ID: ")[1].split(".")[0])
    return {"components": [observation(identifier)]}


@pytest.mark.parametrize("pressed", [True, False])
def test_terminal_reading_stops_between_components(pressed):
    """@brief Stop pressed during the first call: no second paid call; not pressed: both bodies read."""
    from kicadgen.services.photo_terminals import observe_terminals
    png, components = _terminal_inputs()
    stop, calls = threading.Event(), []

    def call(**kwargs):
        calls.append(1)
        if pressed:
            stop.set()
        return _terminal_answer(**kwargs)
    if pressed:
        with pytest.raises(CallCancelled):
            observe_terminals(png, components, "m", call, lambda line: None, cancel=stop)
        assert len(calls) == 1
    else:
        result = observe_terminals(png, components, "m", call, lambda line: None, cancel=stop)
        assert len(calls) == 2 and all("terminal_geometry" in row for row in result)


def test_terminal_reading_cancelled_before_any_call_pays_nothing():
    """@brief A channel already set: CallCancelled before the first request; None: unchanged reading."""
    from kicadgen.services.photo_terminals import observe_terminals
    png, components = _terminal_inputs()
    calls = []

    def call(**kwargs):
        calls.append(1)
        return _terminal_answer(**kwargs)
    with pytest.raises(CallCancelled):
        observe_terminals(png, components, "m", call, lambda line: None, cancel=lambda: True)
    assert calls == []
    observe_terminals(png, components, "m", call, lambda line: None)
    assert len(calls) == 2


def test_a_cancel_during_an_in_flight_call_is_not_turned_into_a_failed_component(monkeypatch):
    """@brief The watch closes the request in flight: CallCancelled, never «fallita» for that body.

    @note Negative: an ordinary provider failure of the same call is still
        logged as a failed component and the reading goes on.
    """
    from kicadgen.services import ai_diagnostics, photo_terminals
    from kicadgen.services.openrouter import OpenRouterError
    monkeypatch.setattr(ai_diagnostics, "_CANCEL_POLL_S", 0.01)
    png, components = _terminal_inputs()
    stop = threading.Event()

    def slow_call(**kwargs):
        stop.set()
        # Waits as a request waiting for its answer: the watch fires meanwhile.
        threading.Event().wait(0.3)
        raise OpenRouterError("connection closed")
    lines = []
    with pytest.raises(CallCancelled):
        photo_terminals.observe_terminals(png, components, "m", slow_call, lines.append, cancel=stop)
    assert not any("fallita" in str(line) for line in lines)

    def failing(**kwargs):
        raise OpenRouterError("offline")
    lines = []
    result = photo_terminals.observe_terminals(png, components, "m", failing, lines.append,
                                               cancel=threading.Event())
    assert result == components and sum("fallita" in str(line) for line in lines) == 2
