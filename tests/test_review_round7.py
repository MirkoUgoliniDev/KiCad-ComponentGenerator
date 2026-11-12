"""@brief Round 7 (2026-11-11): the fixes of the independent review V4 (services) and V5-4.

- Decision of the coordinator (V4-1, V4-2): the drawn-hole witness cannot be
  perfect -- perspective, stray circles touching the rings and page stretch
  produced false blocks or false confirmations in three successive rounds --
  so a contradiction of two AGREEING readings is ALWAYS the "pad_layout"
  question (the readings' layout, plus the hole-backed one when it exists),
  never a hard block and never a silent adoption; its text says only what was
  measured. A «no_match» without a decisive fit contradicted nothing in
  round 7; since round 7b (F1) it is the question too, with its own text.
- V4-3: the wide second search of the witness may find a fit the narrow one
  missed but never CONFIRM a stretch beyond the narrow gate's span.
- V4-4: every registered prompt has a set of former-default digests, and the
  digest of every current default is pinned here.
- V4-5: a stored text equal to the default apart from surrounding blanks and
  line ends sends the default's bytes.
- V5-4: CRLF and LF are one text for the status, the store and the notice.
- V4-6: the analysis log attributes a prompt by the field the request
  declares, and never one found only inside another registered prompt.
- V4-7: a partial re-read strips the «_» keys of a saved raw answer.

Every rule has a positive and a negative case that differ for a structural
reason (user's rule, .claude/CLAUDE.md «Correzioni»). No network, no AI call.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from kicadgen.services import datasheet_extractor as ext
from kicadgen.services import drawing_holes as dh
from kicadgen.services import prompt_catalog as pc
from kicadgen.services.analysis_profiles import ProfileStore
from kicadgen.services.component_types import COMPONENT_TYPES, text_digest
from kicadgen.services.datasheet_extractor import apply_answers, apply_dedicated_pads

from test_extraction_review_round6 import (  # noqa: E402 - shared doubles of round 6
    AGREED, CHAINS, DRAWN, STRAY, DrawnPage, _main, _offsets, _layout_offsets, _reading)
from test_review_round6b import draw_page  # noqa: E402

#: Words the question must never say: the witness cannot prove them (round 7).
FORBIDDEN = ("in scala", "smentisc", "non e' dove le letture")


class _Verdict(SimpleNamespace):
    """The fields of drawing_holes.LayoutCheck the extractor reads."""


def _no_match(max_error_mm, misplaced_pad=None):
    """A witness that refuses ANY layout as an observable «no_match», with or without a fit."""
    def check(pads, body):
        return _Verdict(status="not_confirmed", reason="no_match", image="drawing.png",
                        max_error_mm=max_error_mm, tolerance_mm=0.15, circles=8,
                        scale_observable=True, misplaced_pad=misplaced_pad)
    return check


def _texts(data):
    """Every text the user reads about the pads: note, block and question texts."""
    return [data.get("_numbering_note") or "", data.get("_pin_map_conflict") or ""] + [
        question["text"] for question in data.get("_questions") or ()]


def _pad_question(data):
    question, = [q for q in data.get("_questions") or () if q["field"] == "pad_layout"]
    return question


# ---------------------------------------------------------------------------
# V4-1 / V4-2: a contradicted agreement is a question, never a block
# ---------------------------------------------------------------------------

def test_a_decisive_no_match_against_an_agreement_is_a_one_option_question():
    """Positive: the witness fitted the pads and refused them -- the user is asked, nothing written."""
    data = _main()
    before = copy.deepcopy(data["package_geometry"]["pins"])
    apply_dedicated_pads(data, _reading(), check_layout=_no_match(0.9))
    assert "_pin_map_conflict" not in data, "never a hard block (round 7)"
    question = _pad_question(data)
    assert [o["value"].split(":")[0] for o in question["options"]] == ["main+dedicated"]
    assert "non confermano questa disposizione" in question["text"]
    assert "Confermare la disposizione letta" in question["text"]
    assert data["package_geometry"]["pins"] == before, "nothing adopted while the question is open"
    assert not any(word in text for text in _texts(data) for word in FORBIDDEN)


def test_a_no_match_without_any_fit_is_a_question_that_claims_no_fit():
    """Twin (V4-2): the same refusal with NO fitted assignment (max_error None).

    CONTRACT CHANGED (round 7b, F1, 2026-11-11): round 7 let this agreement
    stand with a warning, a regression against 1266fb7, which refused it --
    three WRONG readings on pages crowded with stray circles were confirmed
    (V6, stray6.py). Since a contradiction costs only a question, it is now
    the one-option question too. The structural difference from the decisive
    case is in the TEXT: no fit was made, so it says the holes do not
    coincide with this layout and confirm no other -- never what a fit
    measured.
    """
    data = _main()
    before = copy.deepcopy(data["package_geometry"]["pins"])
    apply_dedicated_pads(data, _reading(), check_layout=_no_match(None))
    assert "_pin_map_conflict" not in data, "never a hard block (round 7)"
    question = _pad_question(data)
    assert [o["value"].split(":")[0] for o in question["options"]] == ["main+dedicated"]
    assert "non coincidono con questa disposizione e non ne confermano nessun'altra" in question["text"]
    assert "confermate dalla lettura dedicata" not in data["_numbering_note"]
    assert data["package_geometry"]["pins"] == before, "nothing adopted while the question is open"
    assert not any(word in text for text in _texts(data) for word in FORBIDDEN)
    decisive = _main()
    apply_dedicated_pads(decisive, _reading(), check_layout=_no_match(0.9))
    assert "nessun'altra" not in _pad_question(decisive)["text"], "a decisive fit says what it measured"


def test_the_question_names_a_pad_only_when_the_witness_singles_one_out():
    """Positive and negative together: «il pad N e' il piu' lontano» needs the witness's misplaced_pad."""
    named = _main()
    apply_dedicated_pads(named, _reading(), check_layout=_no_match(0.9, misplaced_pad="5"))
    assert "il pad 5 e' il piu' lontano dai fori" in _pad_question(named)["text"]
    silent = _main()
    apply_dedicated_pads(silent, _reading(), check_layout=_no_match(0.9))
    assert "il piu' lontano" not in _pad_question(silent)["text"]
    # A name that is not a pad of the layout is not repeated either.
    foreign = _main()
    apply_dedicated_pads(foreign, _reading(), check_layout=_no_match(0.9, misplaced_pad="99"))
    assert "il pad 99" not in _pad_question(foreign)["text"]


def test_answering_the_one_option_question_writes_the_agreed_layout_saved_or_fresh():
    """The single option is the layout the agreement used to write, by the one writer."""
    def extraction(answers=None):
        data = _main()
        for pin in data["package_geometry"]["pins"]:
            pin.update(pad_type="thru_hole", drill_mm=1.0, pad_length_mm=1.8, pad_width_mm=1.8,
                       shape="circle")
        data.update(layout="matrix", name="X", body_length_mm=18.0, body_width_mm=12.0,
                    body_height_mm=5.0)
        data["package_geometry"]["name"] = "X"
        if answers:
            data["_answers"] = answers
        apply_dedicated_pads(data, _reading(), check_layout=_no_match(0.9))
        ext._canonicalize_answer(data)
        return ext._params_from_tool_arguments(data)

    asked = extraction()
    question, = [q for q in asked.questions if q["field"] == "pad_layout"]
    value = question["options"][0]["value"]
    fresh = apply_answers(asked, {question["key"]: value})
    saved = extraction({question["key"]: value})
    for params in (fresh, saved):
        assert params.pin_map_conflict is None
        assert not [q for q in params.questions or () if q["field"] == "pad_layout"]
        assert _offsets(params.package_geometry["pins"]) == _layout_offsets(AGREED)
    assert ([(p["number"], p["x_mm"], p["y_mm"]) for p in fresh.package_geometry["pins"]]
            == [(p["number"], p["x_mm"], p["y_mm"]) for p in saved.package_geometry["pins"]])


def test_disagreeing_readings_refuted_by_the_holes_keep_the_hard_block():
    """Negative: the round-7 rule is for AGREEING readings only; a single refuted layout of a
    disagreement is still excluded from the question (audit A1) and the block stays."""
    pins = [{"number": n, "x_mm": x, "y_mm": y} for n, (x, y) in AGREED.items()]
    data = {"package_geometry": {"origin": "custom", "pins": pins}}
    holes = ext._HoleVerdicts(_no_match(0.9), None)
    ext._block_or_ask(data, pins, [("main", dict(AGREED), {}, False)], holes, 1.0, "msg", None)
    assert data.get("_pin_map_conflict") and not data.get("_questions")
    # The same single layout as a CONTRADICTED AGREEMENT is asked.
    data = {"package_geometry": {"origin": "custom", "pins": copy.deepcopy(pins)}}
    ext._block_or_ask(data, data["package_geometry"]["pins"], [("main", dict(AGREED), {}, False)],
                      holes, 1.0, "msg", None, contradicted=True)
    assert not data.get("_pin_map_conflict") and len(_pad_question(data)["options"]) == 1


def test_pads_sharing_a_number_still_block_a_contradicted_agreement():
    """Structural limit, declared: no chosen layout can be written back pad by pad."""
    pins = [{"number": "1", "x_mm": 0.0, "y_mm": 0.0}, {"number": "1", "x_mm": 5.0, "y_mm": 0.0},
            {"number": "2", "x_mm": 0.0, "y_mm": 5.0}]
    data = {"package_geometry": {"origin": "custom", "pins": pins}}
    holes = ext._HoleVerdicts(_no_match(0.9), None)
    ext._block_or_ask(data, pins, [("main", {"1": (5.0, 0.0), "2": (0.0, 5.0)}, {}, False)], holes,
                      1.0, "msg", None, contradicted=True)
    assert data.get("_pin_map_conflict") and not data.get("_questions")


def test_the_chain_reading_path_asks_instead_of_blocking():
    """`_resolve_chain_candidate`: the dedicated reading sides with the main answer's chains, the holes refuse both."""
    data = _main(dict(AGREED, **{"3": (15.0, 2.0)}))     # the model's own sums are off on pad 3
    data["_chain_candidate"] = {"pads": dict(AGREED), "centred": True, "spans": _reading()["spans"]}
    data["_pin_map_conflict"] = "Le coordinate non tornano con le catene."
    apply_dedicated_pads(data, _reading(), check_layout=_no_match(0.9))
    assert "_pin_map_conflict" not in data
    question = _pad_question(data)
    assert {o["value"].split(":")[0] for o in question["options"]} == {"main", "chains+dedicated"}
    assert not any(word in text for text in _texts(data) for word in FORBIDDEN)


def test_the_partial_re_read_asks_instead_of_blocking(monkeypatch, tmp_path):
    """reread_pad_positions goes through the same rule: a refused agreement is a question there too."""
    from PIL import Image
    image = tmp_path / "drawing.png"
    Image.new("RGB", (300, 200), "white").save(image)
    stored = _main()
    for pin in stored["package_geometry"]["pins"]:
        pin.update(pad_type="thru_hole", drill_mm=1.0, pad_length_mm=1.8, pad_width_mm=1.8,
                   shape="circle")
    stored.update(layout="matrix", name="X", body_length_mm=18.0, body_width_mm=12.0)
    stored["package_geometry"]["name"] = "X"
    monkeypatch.setattr(ext, "_layout_checker", lambda *args, **kwargs: _no_match(0.9))
    monkeypatch.setattr(ext, "_layout_measurer", lambda *args, **kwargs: None)
    call = Mock(return_value={"drawing_unit": "mm", "contacts_view": "bottom", "chains": CHAINS})
    params = ext.reread_pad_positions(stored, [image], model="vendor/reader", call=call)
    assert call.call_count == 1 and params.pin_map_conflict is None
    assert [q for q in params.questions if q["field"] == "pad_layout"]
    # Negative twin: a witness that cannot judge the page leaves the agreement confirmed.
    monkeypatch.setattr(ext, "_layout_checker", lambda *args, **kwargs: None)
    params = ext.reread_pad_positions(stored, [image], model="vendor/reader",
                                      call=Mock(return_value=call.return_value))
    assert params.pin_map_conflict is None and "confermate" in params.notes
    assert not [q for q in params.questions or () if q["field"] == "pad_layout"]


def test_the_page_double_with_the_hole_backed_layout_still_offers_both():
    """The 17:42 shape kept: a hole-backed layout beside the readings' one (two options)."""
    from test_extraction_review_round6 import CHAINS_WITH_DATUM
    data = _main()
    apply_dedicated_pads(data, _reading(CHAINS_WITH_DATUM), check_layout=DrawnPage(
        list(DRAWN.values()) + [STRAY]))
    question = _pad_question(data)
    assert {o["value"].split(":")[0] for o in question["options"]} == {"main+dedicated", "holes"}
    assert "Quale disposizione corrisponde al disegno?" in question["text"]


# ---------------------------------------------------------------------------
# V4-3: the wide search never confirms a stretch the narrow gate would refuse
# ---------------------------------------------------------------------------

def _grid(rows, cols, pitch, spacing):
    return {str(r * cols + c + 1): (c * pitch, r * spacing) for r in range(rows) for c in range(cols)}


@pytest.mark.parametrize("reading", [
    _grid(2, 3, 2.54 * 1.07, 2.54),            # pitch read 7 % long
    _grid(2, 3, 2.54, 2.54 * 0.93),            # row spacing read 7 % short
], ids=["2x3 pitch +7%", "2x3 rows -7%"])
def test_a_reading_with_a_whole_axis_wrong_is_not_confirmed(tmp_path, reading):
    """Positive (V4-3): drawn on 2.54 mm, read 7 % off on one axis -- the wide pass found
    the fit and confirmed it in round 6b; now it is «distorted», never confirmed.

    @note Limit kept, pre-existing (the witness of 1266fb7 does the same): an
        axis read up to about 6 % off is proposed by the NARROW gate itself
        and can still be confirmed (2x4 header, pitch +6 %: maximum error
        0.11 mm under a 0.16 mm tolerance). Only the wide pass is capped here.
    """
    rows = 2
    cols = len(reading) // rows
    page = draw_page(tmp_path / "page.png", list(_grid(rows, cols, 2.54, 2.54).values()))
    verdict = dh.check_pad_layout([page], reading)
    assert verdict.status != "confirmed"


def test_the_same_reading_with_the_drawn_spacing_is_confirmed(tmp_path):
    """Negative twin: the reading the page shows, no stretch at all."""
    drawn = _grid(2, 3, 2.54, 2.54)
    page = draw_page(tmp_path / "page.png", list(drawn.values()))
    assert dh.check_pad_layout([page], drawn).status == "confirmed"


def test_a_fit_carries_the_stretch_it_shows():
    """The confirmation carries its stretch only when the pads can show one (four, not collinear)."""
    import numpy as np
    square = np.array([(0.0, 0.0), (5.0, 0.0), (0.0, 5.0), (5.0, 5.0)])
    stretched = np.column_stack([square[:, 0] * 20.2, square[:, 1] * 20.0])
    circles = [dh._Circle(float(x), float(y), 5.0, 1.0, True) for x, y in stretched]
    fit = dh._fit_assignment(square, circles, np.arange(4), np.zeros((10, 10), np.uint8), None)
    assert fit.stage == "confirmed" and fit.stretch == pytest.approx(0.01, abs=1e-6)
    line = np.array([(0.0, 0.0), (5.0, 0.0), (10.0, 0.0), (15.0, 0.0)])
    circles = [dh._Circle(float(x) * 20.0, 0.0, 5.0, 1.0, True) for x, _y in line]
    fit = dh._fit_assignment(line, circles, np.arange(4), np.zeros((10, 10), np.uint8), None)
    assert fit.stretch is None


# ---------------------------------------------------------------------------
# V4-4: former-default digests for every prompt, current defaults pinned
# ---------------------------------------------------------------------------

#: SHA-256 of the CURRENT default of every profile prompt and of every built-in
#: type's instructions. When a default changes, the test below fails: add the
#: OLD digest to its superseded set (prompt_catalog.SUPERSEDED_DIGESTS, or the
#: owning module's set; component_types.SUPERSEDED_INSTRUCTION_DIGESTS for a
#: type), THEN update the digest here.
#: 2026-11-12: every Italian default translated to English (prompts in one
#: language, user decision); each old digest is registered as a former default.
PINNED_DEFAULT_DIGESTS = {
    "system_prompt": "57abf7a6024746011314768b5f9dde6af2c4eaecebd89144c70b5ebd4da6cef2",
    "system_prompt_single": "bf708c59329d60d496b67f1a0c82d7651c9c59a3c0c3ba658e00edbd7dc4aca5",
    "numbering_prompt": "f06548f8dc636b32d64cc92235e44d5ad0315030132eb71fb0d8f44a6ad35e5a",
    "pad_positions_prompt": "e565032585e69d85129d3b8ff1eee72fe54c0d2be273eb8c99106a14f14b504a",
    "body_dimensions_prompt": "ee5bbe4bb854f6a5c0f3d6a790d84ab8e4963813d74de67d6eed77a5a433354b",
    "contact_style_prompt": "ea0e1707ab43a1b89b6bcb5082e17cba38713f4b94dec19d67d5ec8d2482ffdd",
    "role_prompt_pinout": "3bc308855fd9d35e3253ef84b9aa7f7dde85006ac83bbdbd4498a748f5f71e98",
    "role_prompt_top": "9040a4a941513fdd8c16109e418bc964cb76f6ecba8f5d8b2ecad57c1dec4182",
    "role_prompt_hires": "e5029c4fffe1c2182f001c2e2ba63400ee7e1d27b7ad135582e511f55a1c54b7",
    "role_prompt_land_pattern": "57b50af37532d38d9cdd9a5e00539e2f3b095f13f6ff03e773118663e33449eb",
    "role_prompt_side_view": "32df70b7e418af5ad0a6fca211d5aeb43231859051d5ee3f7b63d9ca23c664a0",
    "role_prompt_datasheet": "07b5c7262dd7b0586aea8b46cb131db71e89dd6834e8152c226265aeec403ef7",
    "pin_type_prompt": "64cce1f386941e9a583a1d5d76d0afe91369c6ada27400304ea631a52628da07",
    "grounding_prompt": "94054f8d3c319bf2976bbd042cfd094d29e1672959fa9e5c26d21ac213ac0244",
    "mechanical_evidence_prompt": "e263114e5bf1aff8c5a2e5cc30b99d5df89c9d3779c2bfc40476608a540a0862",
    "image_instructions": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "terminal_prompt": "da9b9ba943be3b2ca0c953681e9373481bf3e6ca142ca19afc84320dfd9b1692",
    "lead_prompt": "4bfcb6e43c58217477476f124d2b760b0f7adfefedfeb1fc9718faef3a2f9f2d",
    "crop_prompt": "4eedebb08aad63489aaf2e6bc1f5285736f108de0fd272e647ff8b8651dfe906",
    "inventory_prompt": "1f5f49d5355d7309b7dd98f8d287a5ace5d60618db5a3418453b9d168015929b",
    "inventory_enumerate_prompt": "d4808fe5b90a21d0bb1ea51fb193c2b0db1c7ca20defc79f1e171c75c86c372f",
    "inventory_enumerate_empty_prompt": "adcbdf10779234138233709679afc11e99140b97cbbe942336bf33aeaef9bbf8",
    "marking_prompt": "ce724a8731e650122668092a30438ed686afefb1ff90701767e4bf72d9fdd9b7",
    "verification_prompt": "4c48ef66f734290c8932888c0ffe62d357c3328193c045be5eebdc6cd1b0173b",
    "instructions:single_tht": "56e5dcf1c88270b8153428740c5ab43b59cd9041170d2aacbdc70235124c74b6",
    "instructions:single_smd": "46f56d032e075a235aa1e18eb467262eb10f4f005c566a6bb5e9f87df64698bb",
    "instructions:aggregate_tht": "26b997aefda8c650c25dd1c0cdea02aaa321ee77b7b4042adbd58c238d495226",
    "instructions:aggregate_castellated": "301177746018b9e6fb4d362602426cc9341c2e5e49ba4a8c2c3a78bec7374e49",
}


def _registered_entries():
    """{pin key: registry entry} of every profile prompt and every built-in type's instructions."""
    entries = {item.field: item for item in pc.PROMPTS}
    for kind in COMPONENT_TYPES:
        item = pc.instruction_entry(kind.key)
        if item is not None:
            entries[f"instructions:{kind.key}"] = item
    return entries


def test_every_current_default_is_pinned_and_a_change_names_the_digest_to_register():
    entries = _registered_entries()
    assert set(entries) == set(PINNED_DEFAULT_DIGESTS), "a prompt was added or removed: pin it"
    problems = []
    for key, item in entries.items():
        pinned = PINNED_DEFAULT_DIGESTS[key]
        current = text_digest(item.current_default())
        if current == pinned:
            continue
        if pinned not in item.superseded():
            problems.append(f"default of {key} changed: add the old digest {pinned} to its "
                            f"superseded set ({item.superseded_source or 'prompt_catalog.SUPERSEDED_DIGESTS'})")
        else:
            problems.append(f"default of {key} changed and its old digest is registered: update "
                            f"PINNED_DEFAULT_DIGESTS[{key!r}] to {current}")
    assert not problems, "\n".join(problems)


def test_the_pin_check_tells_a_registered_change_from_a_forgotten_one(monkeypatch):
    """The checker itself, on a simulated change of one default: forgotten -> named, registered -> silent."""
    from kicadgen.services import photo_markings
    old = photo_markings.PROMPT
    monkeypatch.setattr(photo_markings, "PROMPT", old + " One more sentence.")
    item = pc.entry("marking_prompt")
    assert text_digest(old) not in item.superseded()
    monkeypatch.setattr(pc, "SUPERSEDED_DIGESTS",
                        dict(pc.SUPERSEDED_DIGESTS, marking_prompt=frozenset({text_digest(old)})))
    assert text_digest(old) in item.superseded()
    assert pc.prompt_status("marking_prompt", old) == pc.OUTDATED


def test_every_profile_prompt_has_a_superseded_set_and_the_owning_set_is_merged(monkeypatch):
    assert set(pc.fields()) <= set(pc.SUPERSEDED_DIGESTS)
    from kicadgen.services import photo_crop_reading
    monkeypatch.setattr(photo_crop_reading, "SUPERSEDED_CROP_PROMPT_DIGESTS", frozenset({"a" * 64}))
    monkeypatch.setattr(pc, "SUPERSEDED_DIGESTS", dict(pc.SUPERSEDED_DIGESTS,
                                                       crop_prompt=frozenset({"b" * 64})))
    assert pc.entry("crop_prompt").superseded() == {"a" * 64, "b" * 64}


def test_a_former_default_of_a_new_field_is_replaced_in_the_database(tmp_path, monkeypatch):
    """Positive: a field brought into the profile on 2026-11-11 follows the replacement rule."""
    old = "Read the markings of the former release."
    path = tmp_path / "ai.sqlite3"
    ProfileStore(path).save_defaults({"marking_prompt": old, "lead_prompt": old})
    monkeypatch.setattr(pc, "SUPERSEDED_DIGESTS",
                        dict(pc.SUPERSEDED_DIGESTS, marking_prompt=frozenset({text_digest(old)})))
    reopened = ProfileStore(path).defaults()
    assert reopened["marking_prompt"] == pc.restore_default("marking_prompt")
    # Negative twin: the same text in a field whose set does not list it is a user text, kept.
    assert reopened["lead_prompt"] == old


# ---------------------------------------------------------------------------
# V4-5 and V5-4: blanks and line ends are not content
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field", ["marking_prompt", "inventory_prompt", "crop_prompt",
                                   "system_prompt_single", "pad_positions_prompt"])
def test_a_default_with_other_blanks_or_line_ends_sends_the_default_bytes(field):
    default = pc.restore_default(field)
    for variant in ("  " + default + "\n\n", default.replace("\n", "\r\n") + " "):
        assert pc.prompt_status(field, variant) == pc.CURRENT
        assert pc.effective_text(field, {field: variant}) == default
    # Negative twin: one word changed is a user text, sent as it is.
    edited = default.replace(default.split()[0], default.split()[0] + "X", 1)
    assert pc.effective_text(field, {field: "  " + edited}) == "  " + edited


def test_the_photo_helpers_send_the_default_bytes_for_a_default_equal_text():
    from kicadgen.services import photo_crop_reading, photo_global_inventory
    crop = photo_crop_reading.SYSTEM_PROMPT
    assert photo_crop_reading.effective_prompt(crop + "\n") == crop
    assert photo_crop_reading.effective_prompt("my crop text") == "my crop text"
    inventory = photo_global_inventory.PROMPT
    assert photo_global_inventory._profile_or_default(" " + inventory, "inventory_prompt") == inventory
    assert photo_global_inventory._profile_or_default("mine", "inventory_prompt") == "mine"
    assert photo_global_inventory._profile_or_default(None, "inventory_enumerate_prompt") \
        == photo_global_inventory.PROMPT_ENUMERATE


def test_crlf_is_one_text_for_the_status_and_the_notice(monkeypatch):
    """V5-4: the store, the notice and the dialog (LF editor) agree on a CRLF copy."""
    default = pc.restore_default("verification_prompt") + "\nSecond line."
    custom_lf = default
    custom_crlf = default.replace("\n", "\r\n")
    assert pc.prompt_status("verification_prompt", custom_crlf) == \
        pc.prompt_status("verification_prompt", custom_lf) == pc.CUSTOM
    assert pc.notice_digest(custom_crlf) == pc.notice_digest(custom_lf)
    # A CRLF copy of a former default is that former default.
    old = "Old verification text.\nSecond line."
    monkeypatch.setattr(pc, "SUPERSEDED_DIGESTS", dict(
        pc.SUPERSEDED_DIGESTS, verification_prompt=frozenset({text_digest(old)})))
    assert pc.prompt_status("verification_prompt", old.replace("\n", "\r\n")) == pc.OUTDATED
    # Negative twin: a different word stays custom, CRLF or not.
    assert pc.prompt_status("verification_prompt",
                            old.replace("Old", "New").replace("\n", "\r\n")) == pc.CUSTOM


def test_the_notice_keys_a_crlf_text_like_its_lf_form(tmp_path):
    store = ProfileStore(tmp_path / "ai.sqlite3")
    store.save_defaults({"lead_prompt": "my leads\r\nsecond line"})
    entry, = [row for row in pc.notice_entries(store) if row["field"] == "lead_prompt"]
    assert entry["digest"] == text_digest("my leads\nsecond line")
    assert not [row for row in pc.notice_entries(store, dismissed=[("lead_prompt", entry["digest"])])
                if row["field"] == "lead_prompt"]


# ---------------------------------------------------------------------------
# V4-6: the log attributes the prompt a request declares, never one found inside another
# ---------------------------------------------------------------------------

def _logged(prompts, *requests):
    """Lines SentPromptLog writes for the requests [(declared or None, kwargs)]."""
    lines = []
    log = pc.SentPromptLog(prompts, lines.append)
    for declared, kwargs in requests:
        if declared is None:
            log.observe(kwargs)
        else:
            with pc.sending(declared):
                log.observe(kwargs)
    return lines


def test_a_short_custom_text_inside_another_prompt_is_not_reported_as_sent():
    """Positive (V4-6): the custom numbering text is one sentence of the main prompt; only the main one is sent."""
    main = pc.restore_default("system_prompt")
    sentence = main.split(". ")[1].strip() + "."
    assert sentence in main
    lines = _logged({"numbering_prompt": sentence}, (None, {"system_prompt": main}))
    assert not any(pc.tx(pc.entry("numbering_prompt").title) in line for line in lines)
    assert any(pc.tx(pc.entry("system_prompt").title) in line for line in lines)


def test_the_same_short_text_sent_on_its_own_is_reported():
    """Negative twin: the same custom text, now the system prompt of its own request."""
    main = pc.restore_default("system_prompt")
    sentence = main.split(". ")[1].strip() + "."
    lines = _logged({"numbering_prompt": sentence},
                    (None, {"system_prompt": main + "\n\n" + sentence}))
    assert any(pc.tx(pc.entry("numbering_prompt").title) in line for line in lines)


def test_a_declared_field_is_reported_with_the_status_of_the_text_sent():
    custom = "My own pad-position instructions."
    lines = _logged({}, ({"pad_positions_prompt": custom}, {"system_prompt": custom}))
    line, = [line for line in lines if pc.tx(pc.entry("pad_positions_prompt").title) in line]
    # Since 2026-11-12 the visible line names the registry number, the machine
    # line of the file keeps the full digest.
    assert pc.tx("personalizzato") in line and ", n. " in line and text_digest(custom)[:8] not in line
    assert (f"AI_PROMPT_SENT number={line.rsplit('n. ', 1)[1].rstrip('.')} "
            f"field=pad_positions_prompt sha256={text_digest(custom)}") in lines
    # Declared, it is reported even when the request text holds it only inside another prompt.
    main = pc.restore_default("system_prompt")
    lines = _logged({}, ({"system_prompt": main}, {"system_prompt": main}))
    assert any(pc.tx(pc.entry("system_prompt").title) in line for line in lines)


def test_the_dedicated_readings_declare_their_field(tmp_path):
    """The call sites of the extractor pass the field they send (no substring search needed)."""
    from PIL import Image
    image = tmp_path / "drawing.png"
    Image.new("RGB", (300, 200), "white").save(image)
    seen = []

    def call(**kwargs):
        seen.append(dict(pc._DECLARED.get() or {}))
        return {"drawing_unit": "mm", "contacts_view": "top", "chains": []}

    ext.read_pad_positions([image], model="m", call=call, prompt=None)
    assert seen[-1] == {"pad_positions_prompt": pc.restore_default("pad_positions_prompt")}
    ext.read_pad_positions([image], model="m", call=call, prompt="custom chains")
    assert seen[-1] == {"pad_positions_prompt": "custom chains"}
    assert pc._DECLARED.get() is None, "the declaration ends with the request"


# ---------------------------------------------------------------------------
# V4-7: a partial re-read strips the «_» keys of a saved raw answer
# ---------------------------------------------------------------------------

def test_the_saved_raw_answer_loses_injected_private_keys():
    stored = _main()
    stored["_dedicated_leads"] = {"1": "read"}
    stored["package_geometry"]["pins"][0]["_hole_rounding_mm"] = 0.3
    data = ext._reread_start(stored, None)
    assert "_dedicated_leads" not in data and "_dedicated_leads" not in data["_raw_main"]
    assert "_hole_rounding_mm" not in data["package_geometry"]["pins"][0]
    assert "_dedicated_leads" in stored, "the stored answer itself is not changed"


def test_the_keys_single_answer_to_data_derives_are_kept():
    """Negative twin: the program's own derivations of the main shape are not injected state."""
    stored = _main()
    stored["_numbers_by_convention"] = True
    stored["_grid_missing"] = ["pitch"]
    data = ext._reread_start(stored, None)
    assert data["_numbers_by_convention"] is True and data["_grid_missing"] == ["pitch"]
    assert data["_raw_main"]["_numbers_by_convention"] is True
