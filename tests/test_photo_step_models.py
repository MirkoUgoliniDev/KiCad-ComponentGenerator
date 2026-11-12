"""@brief Every photo call uses the model of its analysis step (user request 2026-11-12).

The «Modello AI» table gives each step its own model (prompt_models). A photo
call resolves it AT THE CALL SITE, before its images are sized and before its
cache key: these tests fake the paid call (no network), record the model each
request carries, and check both directions -- a chosen step model reaches the
call, while None (no table) or "" (the single model) keeps the model used
before. The cache contract: an answer cached for model A is never replayed
for model B.
"""
from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from kicadgen.services import photo_global_inventory as global_inventory
from kicadgen.services import photo_grounding as grounding
from kicadgen.services import photo_inventory as inventory
from kicadgen.services import photo_markings as markings
from kicadgen.services.photo_grounding import detail_grid, model_image_limits
from kicadgen.services.photo_leads import refine_leads
from kicadgen.services.photo_terminals import observe_terminals
from tests.test_photo_global_inventory import candidate, response

#: Two OpenRouter-form identifiers (same provider as the fallbacks): no key or
#: credit check runs when a step chooses one of them.
STEP_A, STEP_B = "vendor/step-a", "vendor/step-b"
FALLBACK = "anthropic/claude-opus-5"


def _recording(answer):
    """@brief A fake paid call returning a deep copy of `answer` and recording every request."""
    calls = []

    def call(**kwargs):
        calls.append(kwargs)
        return deepcopy(answer(kwargs) if callable(answer) else answer)
    return call, calls


# ── global inventory ─────────────────────────────────────────────────────

def _global(step_models, cache=None, photo=None):
    """@brief One review_global_inventory run with the fallback model and the given step models."""
    call, calls = _recording(response())
    image = np.zeros((60, 120, 3), np.uint8) if photo is None else photo
    review = global_inventory.review_global_inventory(
        image, [candidate(1), candidate(2)], 40, 20, FALLBACK, call, lambda _m: None, cache,
        **({} if step_models is None else {"step_models": step_models}))
    return review, calls


@pytest.mark.parametrize("step_models,expected", [
    ({"inventory_prompt": STEP_A}, STEP_A),
    (None, FALLBACK),
    ({"inventory_prompt": ""}, FALLBACK),
    ({"crop_prompt": STEP_A}, FALLBACK),
])
def test_the_global_inventory_sends_its_step_model(step_models, expected):
    """@brief Positive: the inventory step's model is sent; negative: no table, blank or another step's choice keeps the caller's."""
    _, calls = _global(step_models)
    assert [kw["model"] for kw in calls] == [expected]


def test_the_global_inventory_cache_is_keyed_on_the_step_model(tmp_path):
    """@brief An answer cached for step model A is replayed for A and never for B."""
    assert len(_global({"inventory_prompt": STEP_A}, tmp_path)[1]) == 1
    assert _global({"inventory_prompt": STEP_A}, tmp_path)[1] == [], "same model: replay"
    calls = _global({"inventory_prompt": STEP_B}, tmp_path)[1]
    assert [kw["model"] for kw in calls] == [STEP_B], "another model: asked again"
    # The fallback model has no entry either: the step model is not a synonym.
    assert len(_global(None, tmp_path)[1]) == 1


def test_the_global_inventory_sizes_its_images_for_the_step_model():
    """@brief The detail tiles follow the limits of the model that receives them, not of the fallback."""
    photo = np.zeros((1500, 3000, 3), np.uint8)
    coarse = "plain-model"                          # no provider form: UNKNOWN limits, enlarges
    expected = {model: len(detail_grid(40, 20, (3000, 1500), model_image_limits(model)))
                for model in (FALLBACK, coarse)}
    assert expected[FALLBACK] != expected[coarse], "the two models must size differently"
    for step_models, model in ((None, FALLBACK), ({"inventory_prompt": coarse}, coarse)):
        _, calls = _global(step_models, photo=photo)
        tiles = [b for b in calls[0]["content_blocks"]
                 if b.get("type") == "text" and b["text"].startswith("Context tile")]
        assert len(tiles) == expected[model]


# ── markings ─────────────────────────────────────────────────────────────

def _marked():
    """@brief Two bodies with a printed marking on a 42.8 x 15.2 mm board."""
    return [dict(name="U1", x_mm=-5.0, y_mm=0.0, length_mm=5.0, width_mm=4.0,
                 rotation_deg=90.0, marking="SP3?52", marking_confidence="medium"),
            dict(name="R1", x_mm=10.0, y_mm=-3.0, length_mm=3.2, width_mm=1.6,
                 rotation_deg=0.0, marking="472", marking_confidence="high")]


def _marking_answer(kwargs):
    """@brief An empty reading for every id the request sent."""
    ids = [int(b["text"].split("=")[1]) for b in kwargs["content_blocks"] if b.get("type") == "text"]
    return {"components": [dict(id=i, marking="", marking_confidence="low") for i in ids]}


def _photo(width_px):
    """@brief A uniform board photo of `width_px` with the 42.8 x 15.2 mm proportions."""
    return np.full((round(width_px * 15.2 / 42.8), width_px, 3), 40, np.uint8)


def test_the_marking_reading_follows_its_step_model_for_the_skip_and_the_call():
    """@brief Positive: an enlarging step model reads a photo the fallback already saw; negative: blank or None skip as before."""
    photo = _photo(1024)                             # the reference model saw every pixel
    for step_models in (None, {"marking_prompt": ""}):
        call, calls = _recording(_marking_answer)
        markings.read_markings(photo, _marked(), 42.8, 15.2, FALLBACK, call, lambda _m: None,
                               **({} if step_models is None else {"step_models": step_models}))
        assert calls == [], "the fallback skips, as before 2026-11-12"
    call, calls = _recording(_marking_answer)
    announced = []
    markings.read_markings(photo, _marked(), 42.8, 15.2, FALLBACK, call, lambda _m: None,
                           announce=lambda tool, detail: announced.append((tool, detail)),
                           step_models={"marking_prompt": "plain-model"},
                           inventory_model=FALLBACK)
    assert [kw["model"] for kw in calls] == ["plain-model"]
    assert announced == [("llm_markings", "plain-model")], "the phase names the model used"


def test_the_marking_cache_is_keyed_on_the_step_model(tmp_path):
    """@brief A reading cached for A replays for A, never for B."""
    photo = _photo(4000)

    def run(model):
        call, calls = _recording(_marking_answer)
        markings.read_markings(photo, _marked(), 42.8, 15.2, FALLBACK, call, lambda _m: None,
                               tmp_path, step_models={"marking_prompt": model})
        return calls
    assert len(run(STEP_A)) == 1 and run(STEP_A) == []
    assert [kw["model"] for kw in run(STEP_B)] == [STEP_B]


# ── terminals and leads ──────────────────────────────────────────────────

def _png():
    """@brief Encoded PNG of a plain calibrated photo."""
    return cv2.imencode(".png", np.full((100, 200, 3), 70, np.uint8))[1].tobytes()


def _terminal_answer(_kwargs):
    """@brief A complete terminal observation of target 1."""
    return {"components": [dict(id=1, resolved=True, confidence="high", body_bbox=[.2, .2, .8, .8],
                                terminals=[dict(bbox=[0, .3, .2, .4], attachment="attached"),
                                           dict(bbox=[.8, .6, 1, .7], attachment="attached")])]}


def _ic():
    """@brief One IC body the terminal pass observes."""
    return [dict(name="Package", kind="generic", _detected_kind="ic", _box=[.1, .1, .9, .9],
                 x_mm=1, y_mm=-2, length_mm=8, width_mm=4, height_mm=1, rotation_deg=0)]


@pytest.mark.parametrize("step_models,expected", [
    ({"terminal_prompt": STEP_A}, STEP_A), (None, FALLBACK), ({"terminal_prompt": ""}, FALLBACK),
    ({"lead_prompt": STEP_A}, FALLBACK)])
def test_the_terminal_pass_sends_its_step_model(step_models, expected):
    """@brief Positive: 'terminal_prompt' chooses; negative: None, blank or the lead step do not."""
    call, calls = _recording(_terminal_answer)
    observe_terminals(_png(), _ic(), FALLBACK, call, lambda _m: None,
                      **({} if step_models is None else {"step_models": step_models}))
    assert [kw["model"] for kw in calls] == [expected]


def test_the_terminal_cache_is_keyed_on_the_step_model(tmp_path):
    """@brief An observation cached for A is not replayed for B."""
    call, calls = _recording(_terminal_answer)
    for model in (STEP_A, STEP_A, STEP_B):
        observe_terminals(_png(), _ic(), FALLBACK, call, lambda _m: None, tmp_path,
                          step_models={"terminal_prompt": model})
    assert [kw["model"] for kw in calls] == [STEP_A, STEP_B]


def _with_terminals():
    """@brief One body whose terminals were observed, ready for the lead pass."""
    body = _ic()[0]
    body["terminal_geometry"] = dict(body_bbox=[.2, .2, .8, .8], confidence="high",
                                     source="photo_observation", coordinate_frame="target_image_v2",
                                     terminals=[dict(bbox=[0, .3, .2, .5]), dict(bbox=[.8, .4, 1, .6])])
    return [body]


@pytest.mark.parametrize("step_models,expected", [
    ({"lead_prompt": STEP_A}, STEP_A), (None, FALLBACK), ({"lead_prompt": ""}, FALLBACK),
    ({"terminal_prompt": STEP_A}, FALLBACK)])
def test_the_lead_pass_sends_its_step_model(step_models, expected):
    """@brief Positive: 'lead_prompt' chooses; negative: None, blank or the terminal step do not."""
    call, calls = _recording({"terminals": []})
    refine_leads(_png(), _with_terminals(), FALLBACK, call, lambda _m: None,
                 **({} if step_models is None else {"step_models": step_models}))
    assert [kw["model"] for kw in calls] == [expected]


# ── grounding and verification (detector 'ai') ───────────────────────────

def _grounding_answer(kwargs):
    """@brief One resistor on the localisation calls, a present verdict on the verification."""
    if kwargs["tool"]["function"]["name"] == "locate_components":
        return {"components": [{"box_2d": [200, 100, 400, 300], "label": "Visible resistor",
                                "kind": "resistor"}]}
    return {"components": [dict(id=1, present=True, confidence="high", package="")]}


@pytest.mark.parametrize("step_models,locate,verify", [
    ({"grounding_prompt": STEP_A, "verification_prompt": STEP_B}, STEP_A, STEP_B),
    (None, FALLBACK, "verify/model"),
    ({"grounding_prompt": "", "verification_prompt": ""}, FALLBACK, "verify/model"),
    ({"mechanical_evidence_prompt": STEP_B}, FALLBACK, "verify/model"),
])
def test_grounding_and_verification_send_their_step_models(monkeypatch, step_models, locate, verify):
    """@brief Each request kind carries its own step's model; the mechanical contract is not a step of its own."""
    monkeypatch.setattr(grounding, "detail_grid", lambda *args, **kwargs: [])
    call, calls = _recording(_grounding_answer)
    grounding.grounded_components(np.zeros((90, 300, 3), np.uint8), FALLBACK, 40, 20,
                                  ("generic", "resistor"), call, lambda _m: None,
                                  classifier_model="verify/model",
                                  **({} if step_models is None else {"step_models": step_models}))
    by_tool = {(kw["tool"]["function"]["name"], kw["model"]) for kw in calls}
    assert by_tool == {("locate_components", locate), ("verify_mounted_bodies", verify)}


def test_the_grounding_grid_is_sized_for_the_step_model(monkeypatch):
    """@brief grounding_regions receives the step model, so its limits size the grid."""
    seen = []
    monkeypatch.setattr(grounding, "grounding_regions",
                        lambda photo, length, width, model=None: seen.append(model) or [])
    grounding.grounded_components(np.zeros((90, 300, 3), np.uint8), FALLBACK, 40, 20,
                                  ("generic",), lambda **kw: pytest.fail("no view, no call"),
                                  lambda _m: None, step_models={"grounding_prompt": STEP_A})
    assert seen == [STEP_A]


def test_the_mechanical_verification_alone_takes_its_step_model():
    """@brief mechanical_envelopes called directly resolves 'verification_prompt' itself."""
    body = grounding.decode_boxes({"components": [{"box_2d": [200, 100, 400, 300], "label": "r",
                                                   "kind": "resistor"}]},
                                  (0., 0., 1., 1.), 40, 20, ("generic", "resistor"), "v",
                                  lambda _m: None)[0]
    body["_id"] = 1
    for step_models, expected in (({"verification_prompt": STEP_A}, STEP_A), (None, FALLBACK)):
        call, calls = _recording(_grounding_answer)
        grounding.mechanical_envelopes(np.zeros((90, 300, 3), np.uint8), [dict(body)], FALLBACK,
                                       call, lambda _m: None, None,
                                       **({} if step_models is None else {"step_models": step_models}))
        assert [kw["model"] for kw in calls] == [expected]


# ── extract_inventory: the whole photo pass ──────────────────────────────

def _proposal():
    """@brief One synthetic detector proposal."""
    return dict(_id=1, _box=[.2, .2, .4, .4], name="target", kind="generic", x_mm=-8, y_mm=-4,
                length_mm=8, width_mm=4, height_mm=1, rotation_deg=0, color_hex="#303030",
                _source="YOLO local")


#: The inventory answer of tests/test_yolo_inventory_branch.py: one resistor kept.
_INVENTORY = dict(components=[dict(
    component_id="R1", source_candidate_ids=[1], **{"class": "resistor"}, bbox=[.2, .2, .4, .4],
    bbox_basis="body", rotation_deg=0, package="R_0603", marking="472", terminal_count=2,
    terminal_layout="two endcaps", height_mm=None, height_source="unknown", confidence="high",
    evidence="complete body", presence_confidence="high", class_confidence="high", lead_count=2,
    lead_count_complete=True, lead_count_confidence="high", grouping_basis="single_body",
    asymmetry_landmark=None)],
    candidate_decisions=[dict(candidate_id=1, status="keep", component_id="R1", reason="body")],
    uncertainties=[])
#: An uninformative crop observation: the row stays the inventory's.
_BLANK = dict(visual_class="unknown", visual_class_confidence="low", visual_class_evidence="",
              contacts_complete=True, bodies_in_frame=1, metal_contacts_total=2,
              two_end_terminals_only=True, body_shape="other", top_translucent=False,
              something_visible_inside=False, coloured_patch_at_end=False, band_at_end=False,
              printed_text="none", marking="", tab=False, size_family="",
              leads_by_side=dict(left=None, right=None, top=None, bottom=None))


def _photo_answer(kwargs):
    """@brief The fake paid call of the whole photo pass, by tool."""
    name = kwargs["tool"]["function"]["name"]
    if name == "observe_body":
        return _BLANK
    if name == "report_markings":
        return _marking_answer(kwargs)
    return _INVENTORY


def _extract(monkeypatch, tmp_path, **kwargs):
    """@brief extract_inventory on detector 'yolo' with the photo, the detector and the call faked."""
    from kicadgen.services import photo_yolo
    monkeypatch.setattr(inventory, "prepare_photo", lambda *args, **kw: np.zeros((400, 400, 3), np.uint8))
    monkeypatch.setattr(photo_yolo, "detect_yolo_components", lambda *args, **kw: [_proposal()])
    call, calls = _recording(_photo_answer)
    phases = []
    inventory.extract_inventory([tmp_path / "image.png"], "photo/model", 40, 20,
                                ["generic", "resistor"], call, detector="yolo",
                                classifier_model="verify/model", top_photo_index=0,
                                phase=lambda tool, detail: phases.append((tool, detail)), **kwargs)
    models = {}
    for kw in calls:
        models.setdefault(kw["tool"]["function"]["name"], set()).add(kw["model"])
    return models, phases


def test_the_photo_pass_sends_each_step_its_model(monkeypatch, tmp_path):
    """@brief Positive: inventory, crops and markings each carry their own step model, and the phases name them."""
    models, phases = _extract(monkeypatch, tmp_path, step_models={
        "inventory_prompt": STEP_A, "crop_prompt": STEP_B, "marking_prompt": "plain-model"})
    assert models["report_global_inventory"] == {STEP_A}
    assert models["observe_body"] == {STEP_B}
    assert models["report_markings"] == {"plain-model"}
    assert ("llm_inventory", STEP_A) in phases and ("llm_crops", STEP_B) in phases


@pytest.mark.parametrize("step_models", [None, {"inventory_prompt": "", "crop_prompt": "",
                                                "marking_prompt": ""}])
def test_without_step_models_the_photo_pass_is_unchanged(monkeypatch, tmp_path, step_models):
    """@brief Negative: no table or blank steps keep `classifier_model` everywhere, as before."""
    models, phases = _extract(monkeypatch, tmp_path,
                              **({} if step_models is None else {"step_models": step_models}))
    assert set().union(*models.values()) == {"verify/model"}
    assert ("llm_inventory", "verify/model") in phases


def test_the_crop_step_replaces_the_profile_crop_model(monkeypatch, tmp_path):
    """@brief With the table the profile's crop model is not used; without it, it still is."""
    models, _ = _extract(monkeypatch, tmp_path, crop_model="profile/crop",
                         step_models={"crop_prompt": ""})
    assert models["observe_body"] == {"verify/model"}, "blank step = the single model"
    models, _ = _extract(monkeypatch, tmp_path / "old", crop_model="profile/crop")
    assert models["observe_body"] == {"profile/crop"}, "no table: the profile field as before"


def test_detector_ai_resolves_grounding_and_verification(monkeypatch, tmp_path):
    """@brief The 'ai' path hands grounded_components the resolved models and announces the grounding one."""
    monkeypatch.setattr(inventory, "prepare_photo", lambda *args, **kw: np.zeros((400, 400, 3), np.uint8))
    received = []
    monkeypatch.setattr(grounding, "grounded_components",
                        lambda photo, model, *args, **kw: received.append((model, args[7], kw)) or [])
    for step_models, expected in (
            ({"grounding_prompt": STEP_A, "verification_prompt": STEP_B}, (STEP_A, STEP_B)),
            (None, ("photo/model", None))):
        received.clear()
        phases = []
        inventory.extract_inventory([tmp_path / "image.png"], "photo/model", 40, 20, ["generic"],
                                    lambda **kw: pytest.fail("faked"), detector="ai",
                                    top_photo_index=0,
                                    phase=lambda tool, detail: phases.append((tool, detail)),
                                    **({} if step_models is None else {"step_models": step_models}))
        assert (received[0][0], received[0][1]) == expected
        assert "step_models" not in received[0][2], "resolved once, never twice"
        assert ("llm_inventory", expected[0]) in phases


# ── relaunch from the saved review and the SMD recheck ───────────────────

def test_the_relaunch_resolves_the_inventory_and_crop_steps(monkeypatch):
    """@brief reconcile_saved_proposals: the global request and the crops take their step models; None keeps `model`."""
    for step_models, inventory_model, crop_model in (
            ({"inventory_prompt": STEP_A, "crop_prompt": STEP_B}, STEP_A, STEP_B),
            (None, "photo/model", "photo/model")):
        call, calls = _recording(_photo_answer)
        phases = []
        inventory.reconcile_saved_proposals(
            np.zeros((400, 400, 3), np.uint8), [_proposal()], 40, 20, "photo/model", call,
            lambda _m: None, phase=lambda tool, detail: phases.append((tool, detail)),
            **({} if step_models is None else {"step_models": step_models}))
        models = {}
        for kw in calls:
            models.setdefault(kw["tool"]["function"]["name"], set()).add(kw["model"])
        assert models["report_global_inventory"] == {inventory_model}
        assert models["observe_body"] == {crop_model}
        assert (("llm_inventory", STEP_A) in phases) is (step_models is not None)


def test_the_smd_recheck_reads_the_crops_with_the_crop_step(monkeypatch):
    """@brief recheck_unresolved_smd_rows: 'crop_prompt' replaces crop_model; None keeps crop_model."""
    import kicadgen.services.photo_crop_reading as crop_reading
    seen = []

    def fake_read_bodies(photo, rows, length, width, model, *args, **kwargs):
        seen.append(model)
        return crop_reading.CropReadingSummary()
    monkeypatch.setattr(crop_reading, "read_bodies", fake_read_bodies)
    for step_models, expected in (({"crop_prompt": STEP_A}, STEP_A), ({"crop_prompt": ""}, "photo/model"),
                                  (None, "profile/crop")):
        seen.clear()
        row = dict(_proposal(), _smd_prediction={"class": "_other", "accepted": False})
        report = inventory.InventoryResult()
        inventory.recheck_unresolved_smd_rows(
            None, report, [row], 40, 20, "photo/model", lambda **kw: None, lambda _m: None, None,
            None, lambda *args: None, crop_model="profile/crop",
            **({} if step_models is None else {"step_models": step_models}))
        assert seen == [expected]
