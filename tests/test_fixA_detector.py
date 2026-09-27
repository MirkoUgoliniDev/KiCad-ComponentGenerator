"""@brief Regressions for the local detector and the global-inventory unit handling (audit fix A).

Fake runtimes only: no real inference, no network, no paid call.
"""
from copy import deepcopy
import json
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from kicadgen.services import photo_global_inventory as inventory
from kicadgen.services import photo_yolo as yolo


# ── fake ONNX runtime (same contract as tests/test_photo_yolo.py) ─────────────

def _raw(*entries):
    result = np.zeros((1, 25, 8400), dtype=np.float32)
    for index, (xywh, class_id, confidence) in enumerate(entries):
        result[0, :4, index] = xywh
        result[0, 4 + class_id, index] = confidence
    return result


@pytest.fixture
def session(monkeypatch, tmp_path):
    """@brief Fake CPU onnxruntime whose output is chosen per inference call."""
    path = tmp_path / "fake.onnx"
    path.write_bytes(b"inert ONNX test weights; never execute")
    fake = SimpleNamespace(raw=_raw(), calls=[])
    fake.get_inputs = lambda: [SimpleNamespace(name="images", type="tensor(float)",
                                               shape=[1, 3, 640, 640])]
    fake.get_outputs = lambda: [SimpleNamespace(name="output0", type="tensor(float)",
                                                shape=[1, 25, 8400])]
    fake.get_modelmeta = lambda: SimpleNamespace(custom_metadata_map={
        "task": "detect", "imgsz": "[640, 640]", "batch": "1", "stride": "32",
        "names": repr(dict(enumerate(yolo._CLASSES)))})
    fake.get_providers = lambda: ["CPUExecutionProvider"]

    def run(output_names, feeds):
        index = len(fake.calls)
        fake.calls.append(index)
        return [fake.raw(index) if callable(fake.raw) else fake.raw]

    fake.run = run
    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace(
        InferenceSession=lambda weights, **_: fake, SessionOptions=SimpleNamespace,
        __version__="fake-cpu"))
    monkeypatch.setenv("KICADGEN_YOLO_MODEL", str(path))
    return fake


# ── A10: a view identical to another one is not inferred again ───────────────

def test_a_photo_smaller_than_a_tile_is_inferred_once(session):
    """Positive: 640 x 640 used to run overview, tile640 and tile1280 on the same pixels."""
    assert [name for name, _ in yolo._views(640, 640)] == ["overview"]
    yolo.detect_yolo_components(np.zeros((640, 640, 3), np.uint8), 64, 64)
    assert len(session.calls) == 1


def test_distinct_tiles_are_all_kept():
    """Negative: on a large photo no tile repeats a view, so nothing is skipped."""
    views = list(yolo._views(4000, 1848))
    assert len({bounds for _, bounds in views}) == len(views) == 41
    # A 1280 tile equal to the overview is skipped, the distinct 640 tiles stay.
    names = [name for name, _ in yolo._views(700, 640)]
    assert names == ["overview", "tile640-0-0", "tile640-60-0"]


# ── A6 + A11: presence from a confident clipped body, extent from a weak whole view ──

#: Overview of a 1000 x 640 photo: letterbox ratio 0.64, top padding 115 px.
def _overview_box(x0, y0, x1, y1):
    ratio, top = 0.64, 115
    lx0, lx1 = x0 * ratio, x1 * ratio
    ly0, ly1 = y0 * ratio + top, y1 * ratio + top
    return [(lx0 + lx1) / 2, (ly0 + ly1) / 2, lx1 - lx0, ly1 - ly0]


def _seam_outputs(whole_confidence):
    """Overview sees the body whole (image x 480..760); tile640-0-0 sees it cut at x=640."""
    whole = _raw((_overview_box(480, 220, 760, 420), 10, whole_confidence))
    fragment = _raw(([560, 320, 160, 200], 10, .80))   # tile px 480..640, clipped right
    return lambda index: {0: whole, 1: fragment}.get(index, _raw())


def test_a_confident_clipped_body_keeps_its_presence_at_the_operating_threshold(session):
    """A6 positive: the weak whole view becomes an operating-threshold body, always returned."""
    session.raw = _seam_outputs(.10)
    rows = yolo.detect_yolo_components(np.zeros((640, 1000, 3), np.uint8), 100, 64,
                                       settings={"tile_sizes": [640]})
    assert len(rows) == 1, "returned even without include_review_candidates"
    row = rows[0]
    assert row["_yolo_review_only"] is False
    assert row["_yolo_origin"] == "operating_threshold"
    assert row["_yolo_geometry_from_weaker_view"] is True
    assert row["_yolo_presence_attested_by"] == ["tile640-0-0:0"]
    assert row["_yolo_presence_confidence"] == pytest.approx(.80)
    assert row["_yolo_confidence"] == pytest.approx(.10)
    assert not row["_yolo_clipped_sides"]
    assert row["_box"][0] == pytest.approx(.48, abs=.01)
    assert row["_box"][2] == pytest.approx(.76, abs=.01)
    # yolo_only without review keeps it too (it is not a review-only hypothesis).
    from kicadgen.services.photo_inventory import yolo_only_components
    assert len(yolo_only_components(rows, reviewed=False, log=lambda _m: None)) == 1


def test_a_whole_view_already_at_threshold_needs_no_promotion(session):
    """A6 negative: the covering view is itself confident; nothing is marked as weaker."""
    session.raw = _seam_outputs(.60)
    rows = yolo.detect_yolo_components(np.zeros((640, 1000, 3), np.uint8), 100, 64,
                                       settings={"tile_sizes": [640]})
    assert len(rows) == 1
    assert rows[0]["_yolo_geometry_from_weaker_view"] is False
    assert "_yolo_presence_attested_by" not in rows[0]
    assert rows[0]["_yolo_confidence"] == pytest.approx(.60)


def test_a_weak_whole_view_without_a_confident_fragment_stays_review_only(session):
    """A6 structural negative: the presence was never attested by a confident detection."""
    whole = _raw((_overview_box(480, 220, 760, 420), 10, .10))
    fragment = _raw(([560, 320, 160, 200], 10, .10))
    session.raw = lambda index: {0: whole, 1: fragment}.get(index, _raw())
    rows = yolo.detect_yolo_components(np.zeros((640, 1000, 3), np.uint8), 100, 64,
                                       settings={"tile_sizes": [640]})
    assert rows == []


def test_the_chosen_tile_overlap_decides_what_a_tile_can_measure(session):
    """A11: the fragment (200 px) is wider than a 25% overlap (160 px) but not 50% (320 px).

    With the default the tile cannot measure it and the whole view takes over;
    with overlap 0.5 the same tile can, and its clipped detection is kept.
    This fails if detect_yolo_components ignores settings["tile_overlap"] in
    view_overlap_px / _prefer_complete_views.
    """
    photo = np.zeros((640, 1000, 3), np.uint8)
    session.raw = _seam_outputs(.10)
    default = yolo.detect_yolo_components(photo, 100, 64, settings={"tile_sizes": [640]})
    session.calls.clear()
    session.raw = _seam_outputs(.10)
    wide = yolo.detect_yolo_components(photo, 100, 64,
                                       settings={"tile_sizes": [640], "tile_overlap": 0.5})
    assert [row["_yolo_geometry_from_weaker_view"] for row in default] == [True]
    kept = [row for row in wide if not row["_yolo_review_only"]]
    assert len(kept) == 1 and kept[0]["_yolo_clipped_sides"] == ["right"]
    assert kept[0]["_yolo_confidence"] == pytest.approx(.80)
    assert yolo.view_overlap_px("tile640-0-0", 0.5) == 320


# ── A3: a fused end-to-end checkpoint is read with the standard head ──────────

class _Tensor:
    def __init__(self, array):
        self.array = array

    def detach(self):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return self.array


class _NoGrad:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_torch(monkeypatch, head, output):
    """Fake torch + ultralytics whose network returns output(head) as zeros."""
    class Network:
        model = [head]

        def float(self):
            return self

        def eval(self):
            return self

        def __call__(self, tensor):
            return (_Tensor(np.zeros(output(head), np.float32)), None)

    class YOLO:
        def __init__(self, path, task=None):
            self.task, self.names, self.model = task, dict(enumerate(yolo._CLASSES)), Network()

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        __version__="fake-torch", no_grad=_NoGrad, from_numpy=lambda array: _Tensor(array)))
    monkeypatch.setitem(sys.modules, "ultralytics",
                        SimpleNamespace(__version__="fake-ultra", YOLO=YOLO))


class _FusedHead:
    """What Ultralytics leaves after fuse(): one2one_cv2 None, end2end stuck at False."""
    one2one_cv2 = None

    @property
    def end2end(self):
        return False

    @end2end.setter
    def end2end(self, value):
        pass


def test_a_fused_end_to_end_checkpoint_uses_the_standard_head(tmp_path, monkeypatch):
    """A3 positive: before, hasattr(one2one_cv2) made [1,25,8400] be read as [K,6]."""
    _fake_torch(monkeypatch, _FusedHead(), lambda head: (1, 25, 8400))
    weights = tmp_path / "fused.pt"
    weights.write_bytes(b"checkpoint")
    info = yolo.validate_yolo_setup(weights)
    assert info["model_metadata"]["raw_head"] == "standard"
    assert info["output_shape"] == [1, 25, 8400]


def test_a_head_that_accepts_end2end_but_still_outputs_raw_rows_is_restored(tmp_path, monkeypatch):
    """A3 negative: the flag alone is not trusted; the probe shape decides and the flag is undone."""
    head = SimpleNamespace(one2one_cv2=object(), end2end=False)
    _fake_torch(monkeypatch, head, lambda head: (1, 25, 8400))
    weights = tmp_path / "odd.pt"
    weights.write_bytes(b"checkpoint")
    info = yolo.validate_yolo_setup(weights)
    assert info["model_metadata"]["raw_head"] == "standard" and head.end2end is False


def test_a_real_end_to_end_head_still_switches(tmp_path, monkeypatch):
    """A3 control: a working one-to-one head keeps its official inference."""
    head = SimpleNamespace(one2one_cv2=object(), end2end=False)
    _fake_torch(monkeypatch, head, lambda head: (1, 300, 6) if head.end2end else (1, 25, 8400))
    weights = tmp_path / "e2e.pt"
    weights.write_bytes(b"checkpoint")
    info = yolo.validate_yolo_setup(weights)
    assert head.end2end is True and info["output_shape"] == [1, 25, 300]
    assert info["model_metadata"]["raw_head"].startswith("one-to-one")


# ── A7 / A8: one-to-one rows ──────────────────────────────────────────────────

def test_only_finite_integral_in_range_class_ids_are_read():
    """A7: 2.7 is not class 2, NaN/-1/99 are no class; 3.0 is class 3."""
    detections = np.array([[[0, 0, 10, 10, .9, 2.7], [20, 0, 30, 10, .9, np.nan],
                            [40, 0, 50, 10, .9, -1], [60, 0, 70, 10, .9, 99],
                            [80, 0, 90, 10, .9, 3.0]]], np.float32)
    rows = yolo._one_to_one_rows(detections, list(range(len(yolo._CLASSES))), np)
    assert rows.shape == (1, 25, 5)
    assert rows[0, 4:, :4].sum() == 0
    assert rows[0, 4 + 3, 4] == pytest.approx(.9) and rows[0, 4:, 4].sum() == pytest.approx(.9)


def _one_to_one_proposal(body_score):
    pads, ic = yolo._CLASSES.index("pads"), yolo._CLASSES.index("ic")
    detections = np.array([[[100, 100, 140, 130, .6, pads],
                            [100, 100, 140, 130, body_score, ic]]], np.float32)
    rows = yolo._one_to_one_rows(detections, list(range(len(yolo._CLASSES))), np)
    proposals, _ = yolo._proposals(rows, (0, 0, 640, 640), (640, 640), (1.0, 0, 0),
                                   "overview", np, yolo._CONFIDENCE)
    return proposals


def test_one_to_one_rows_of_the_same_anchor_are_merged_and_flagged_ambiguous():
    """A8 positive: pads 0.6 and ic 0.3 on the SAME box behave like the ONNX row."""
    proposals = _one_to_one_proposal(.3)
    assert len(proposals) == 1
    assert proposals[0]["class"] == "ic" and proposals[0]["class_ambiguous"] is True
    assert proposals[0]["rejection"] is None


def test_a_body_score_below_threshold_leaves_the_pads_row_excluded():
    """A8 negative: same merge, body evidence below the operating threshold."""
    proposals = _one_to_one_proposal(.1)
    assert len(proposals) == 1 and proposals[0]["rejection"] == "excluded_class"
    assert proposals[0]["class_ambiguous"] is False


def test_settings_defaults_come_from_the_module_constants():
    """A8: one source of truth for the defaults."""
    assert yolo.YOLO_SETTING_DEFAULTS["confidence"] is yolo._CONFIDENCE
    assert yolo.YOLO_SETTING_DEFAULTS["tile_overlap"] == yolo._TILE_OVERLAP_FRACTION


# ── A4 / A9: global inventory boxes ───────────────────────────────────────────

def _body(**changes):
    row = dict(component_id='assembly', grouping_basis='assembly', source_candidate_ids=[1, 2],
               **{'class': 'rf_module'}, bbox=[.1, .1, .8, .8], bbox_basis='body',
               rotation_deg=0, package='', marking='?', terminal_count=None,
               terminal_layout='unresolved', lead_count=None, lead_count_complete=False,
               lead_count_confidence='low', height_mm=None, height_source='unknown',
               confidence='medium', evidence='Contiguous substrate with shield',
               presence_confidence='medium', class_confidence='medium',
               asymmetry_landmark=None)
    row.update(changes)
    return row


def _response(*rows):
    return dict(components=list(rows), candidate_decisions=[
        dict(candidate_id=i, status='merge', component_id='assembly',
             reason='Part of retained assembly') for i in (1, 2)],
        uncertainties=['Height cannot be measured from top'])


def _candidates():
    return [dict(_id=i, _box=[.2, .2, .4, .4], kind='generic', name='candidate', x_mm=-8,
                 y_mm=-4, length_mm=8, width_mm=4, height_mm=1, rotation_deg=0,
                 color_hex='#303030') for i in (1, 2)]


def _review(reply, cache=None, logs=None):
    calls = []

    def fake(**request):
        calls.append(request)
        return deepcopy(reply)
    result = inventory.review_global_inventory(
        np.zeros((60, 120, 3), np.uint8), _candidates(), 40, 20, 'offline/model', fake,
        (logs if logs is not None else []).append, cache)
    return result, calls


def test_rescaling_skips_non_dict_rows_instead_of_crashing():
    """A4 unit: the dict row is converted, the junk row is left for validation."""
    # A square box on a photo narrower than 1000 px: only the 0-1000 grid fits
    # (pixels overflow, [y,x] reads the same box), so no evidence is needed.
    rows = ['junk', dict(bbox=[100, 100, 800, 800])]
    assert inventory._rescale_boxes(rows, 400, 300, lambda _m: None) == 'normalized_1000'
    assert rows[0] == 'junk' and rows[1]['bbox'] == pytest.approx([.1, .1, .8, .8])


def test_a_non_dict_row_after_the_paid_call_is_an_invalid_row_not_a_crash():
    """A4 positive: before, zip(all rows, dict boxes) raised TypeError after paying."""
    result, calls = _review(_response('junk', _body(bbox=[100, 100, 800, 800])))
    assert len(calls) == 1
    assembly = [row for row in result if row.get('component_id') == 'assembly']
    assert len(assembly) == 1
    assert assembly[0]['_global_inventory_box_scale'] == 'normalized_1000'
    assert any('invalid or duplicate component' in error
               for row in result for error in row['_global_inventory_errors'])


def test_a_live_and_a_replayed_run_carry_the_same_box_scale(tmp_path):
    """A9 positive: the cache keeps the provider's units, so replay converts again."""
    reply = _response(_body(bbox=[100, 100, 800, 800]))
    live, calls = _review(reply, cache=tmp_path)
    assert len(calls) == 1
    stored = json.loads(next(tmp_path.glob('global-inventory-*.json')).read_text())
    assert stored['response']['components'][0]['bbox'] == [100, 100, 800, 800]
    replayed, calls = _review(reply, cache=tmp_path)
    assert calls == []
    assert [row['_global_inventory_box_scale'] for row in replayed] \
        == [row['_global_inventory_box_scale'] for row in live] == ['normalized_1000']
    assert [row['_box'] for row in replayed] == [row['_box'] for row in live]


def test_fractions_need_no_scale_live_or_replayed(tmp_path):
    """A9 negative: a response already in fractions carries no conversion either way."""
    reply = _response(_body())
    live, _ = _review(reply, cache=tmp_path)
    replayed, calls = _review(reply, cache=tmp_path)
    assert calls == []
    assert all('_global_inventory_box_scale' not in row for row in live + replayed)


def test_the_enumeration_says_whether_a_detector_ran():
    """A5 unit: the paid line and the prompt tell apart «no detector» and «found none»."""
    ran = inventory.paid_request_message(4, 0, enumerating=True, detector_ran=True)
    absent = inventory.paid_request_message(4, 0, enumerating=True)
    assert 'nessun rilevatore locale' in absent.lower()
    assert 'nessun rilevatore locale' not in ran.lower()
    assert 'non ha trovato corpi' in ran
    assert 'NO local detector ran' in inventory.PROMPT_ENUMERATE
    assert 'NO local detector ran' not in inventory.PROMPT_ENUMERATE_AFTER_EMPTY_DETECTOR
    assert 'A local detector ran' in inventory.PROMPT_ENUMERATE_AFTER_EMPTY_DETECTOR
