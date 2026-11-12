"""@brief Round 6b (2026-11-11): the fixes of the independent review V3 of round 6.

- V3-1: «distorted» contradicts two agreeing readings only when the drawn
  holes themselves single out ONE pad no stretch of the page explains
  (drawing_holes._single_misplaced, `LayoutCheck.misplaced_pad`); a correct
  layout on a page stretched by its scan is never blocked. The wide second
  search of the witness keeps a confirmation it finds.
- V3-2: opening a project refreshes the generated type block only when it
  holds a text the program wrote (component_types.refresh_block); a block
  the user edited is kept and the log says so once.
- V3-3/V3-4: `printed_terminals` never raises on malformed entries and
  refuses a size that is not finite in millimetres.
- V3-5: the program's «_» keys are stripped from a model answer.

Every rule has a positive and a negative case that differ for a structural
reason (user's rule, .claude/CLAUDE.md «Correzioni»).
"""

from __future__ import annotations

import math
from pathlib import Path

import cv2
import numpy as np
import pytest

from kicadgen.services import component_types
from kicadgen.services import datasheet_extractor as ext
from kicadgen.services import drawing_holes as dh
from kicadgen.services.datasheet_extractor import printed_terminals, without_private_keys

# ---------------------------------------------------------------------------
# V3-1: synthetic drawings checked by the REAL witness
# ---------------------------------------------------------------------------

#: Holes as drawn, mm (page: X right, Y down): two columns and one pad between.
DRAWN = {"1": (2.0, 2.0), "2": (2.0, 10.0), "3": (14.0, 2.0), "4": (14.0, 10.0), "5": (5.0, 6.0)}
#: A circle that is no pad's hole (a glyph), 0.2 mm from where the wrong reading
#: puts pad 5: near enough for a stretched fit to reach it («distorted»).
STRAY = (2.2, 6.0)
#: A wrong reading: pad 5 in the column of pads 1 and 2 (the user's 17:42 case).
READ = dict(DRAWN, **{"5": (2.0, 6.0)})
#: The pads of a four-pad part: pads 2 and 3 of DRAWN left out.
FOUR = ("1", "4", "5", "3")


def draw_page(path: Path, circles, *, ppmm: float = 20.0, sx: float = 1.0, sy: float = 1.0,
              rotation: float = 0.0) -> Path:
    """@brief Draw hole rings (radius 0.5 mm) on white paper, then stretch the page by (sx, sy) and turn it.

    @param path Image file written.
    @param circles (x, y) mm of the rings.
    @param ppmm Pixels per millimetre before the stretch.
    @param sx Stretch of the page along X (1.0: none), as a scan or a redraw does.
    @param sy Stretch along Y.
    @param rotation Page rotation, radians.
    @return `path`.
    """
    xs, ys = [c[0] for c in circles], [c[1] for c in circles]
    width, height = int((max(xs) - min(xs) + 14) * ppmm), int((max(ys) - min(ys) + 14) * ppmm)
    image = np.full((height, width), 255, np.uint8)
    ox, oy = min(xs) - 7, min(ys) - 7
    for x, y in circles:
        # Anti-aliased ring, sub-pixel centre (shift 4: 1/16 px).
        centre = (int(round((x - ox) * ppmm * 16)), int(round((y - oy) * ppmm * 16)))
        cv2.circle(image, centre, int(round(0.5 * ppmm * 16)), 0, 2, cv2.LINE_AA, 4)
    matrix = np.array([[sx * math.cos(rotation), -sx * math.sin(rotation), 0],
                       [sy * math.sin(rotation), sy * math.cos(rotation), 0]], float)
    size = (int(width * sx * 1.1) + int(height * abs(rotation) * sx) + 4,
            int(height * sy * 1.1) + int(width * abs(rotation)) + 4)
    assert cv2.imwrite(str(path), cv2.warpAffine(image, matrix, size, borderValue=255))
    return path


def _judged(tmp_path, circles, reading, **page):
    """The witness's verdict on `reading` and the extractor's contradiction on it."""
    image = draw_page(tmp_path / f"page{len(list(tmp_path.iterdir()))}.png", circles, **page)

    def holes(pads):
        return dh.check_pad_layout([image], pads)

    return holes(reading), ext._agreement_contradicted(reading, holes)


def test_one_misplaced_pad_on_a_page_to_scale_is_a_contradiction(tmp_path):
    """Positive: pad 5 read beside a stray circle 3 mm from its hole; the other four fit with no stretch."""
    verdict, contradiction = _judged(tmp_path, list(DRAWN.values()) + [STRAY], READ)
    assert (verdict.status, verdict.reason, verdict.misplaced_pad) == ("not_confirmed", "distorted", "5")
    assert contradiction is not None and contradiction["singled"] == ["5"]


@pytest.mark.parametrize("axis", ["sx", "sy"])
@pytest.mark.parametrize("stretch", [1.035, 1.04, 1.05, 1.06, 1.08])
def test_a_correct_layout_on_a_stretched_page_is_never_a_contradiction(tmp_path, axis, stretch):
    """Negative twin: every pad right, the page stretched 3.5-8 % (the cases V3-1 found blocked).

    The structural difference: a stretch moves every pad by one linear map,
    so the whole layout IS an affine image of its holes and no subset fits
    with no stretch at all; the old proof let a subset pass under a stretch
    allowance that grows as pads are removed.
    """
    verdict, contradiction = _judged(tmp_path, list(DRAWN.values()), DRAWN, **{axis: stretch})
    assert verdict.misplaced_pad is None
    # CONTRACT CHANGED (round 7b, F2, 2026-11-11): a «distorted» fit whose
    # stretch exceeds HYPOTHESIS_TOL_SPAN (the stretch the hole search itself
    # attributes to a page not to scale) is a contradiction -- a whole axis
    # read 7 % wrong by both readings looks the same to the witness -- and a
    # contradiction is a QUESTION, never a block (round 7). So the page
    # stretched 8 % now costs a question on a correct layout; up to the span
    # nothing changes. Still never a named pad.
    if verdict.stretch_ratio is not None and verdict.stretch_ratio > dh.HYPOTHESIS_TOL_SPAN:
        assert stretch > 1.0 + dh.HYPOTHESIS_TOL_SPAN - 0.005
        assert contradiction is not None and contradiction["stretch"] == verdict.stretch_ratio
        assert contradiction["singled"] == [] and contradiction["misplaced"] is None
    else:
        assert stretch < 1.08, "an 8 % stretch is beyond the span on either axis"
        assert contradiction is None


def test_a_misplaced_pad_on_a_page_stretched_3_percent_is_only_a_warning(tmp_path):
    """The honest outcome at the margin: a stretch of 3 % moves the other pads by up to 0.36 mm.

    Without pad 5 the other four no longer fit a similarity within the
    per-hole tolerance (0.15 mm): nothing proves the page to scale, so the
    misplaced pad is not named and the agreement stands with the warning.
    A wrong footprint is possible here; the drawing's own stretch hides it.
    (At 2 % the four still fit within the tolerance and pad 5 is named.)
    """
    verdict, contradiction = _judged(tmp_path, list(DRAWN.values()) + [STRAY], READ, sx=1.03)
    assert (verdict.status, verdict.reason, verdict.misplaced_pad) == ("not_confirmed", "distorted", None)
    assert contradiction is None


def test_four_pads_cannot_name_a_misplaced_one(tmp_path):
    """Negative, condition (iii): three remaining pads cannot show a stretch, so nothing is named."""
    four = {number: READ[number] for number in FOUR}
    verdict, contradiction = _judged(tmp_path, [DRAWN[n] for n in FOUR] + [STRAY], four)
    assert verdict.misplaced_pad is None and contradiction is None


def test_two_misplaced_pads_name_none(tmp_path):
    """Negative, condition (ii): no single pad's removal leaves a layout that fits."""
    two = dict(READ, **{"3": (14.0, 4.0)})
    verdict, contradiction = _judged(tmp_path, list(DRAWN.values()) + [STRAY, (14.2, 4.0)], two)
    assert verdict.misplaced_pad is None
    assert contradiction is None or contradiction["verdict"].reason == "no_match"


# -- the criterion on exact points (no image) ----------------------------------

def _points(layout):
    return np.array(list(layout.values()), float)


def _similar(points, scale=20.0, turn=0.01):
    """Exact similarity image of `points`, px."""
    c, s = math.cos(turn), math.sin(turn)
    return points @ np.array([[c, s], [-s, c]]) * scale + 100.0


def test_single_misplaced_names_the_pad_no_stretch_explains():
    points = _points(DRAWN)
    targets = _similar(points)
    targets[4] += (40.0, 0.0)          # pad 5 drawn 2 mm away from where it is read
    assert dh._single_misplaced(points, targets, 3.0) == 4


def test_single_misplaced_names_nothing_on_an_affine_page():
    """Negative: a 6 % stretch on one axis is an affine map of every pad, condition (i)."""
    targets = _similar(_points(DRAWN)) * np.array([1.06, 1.0])
    assert dh._single_misplaced(_points(DRAWN), targets, 3.0) is None


def test_single_misplaced_needs_five_pads_and_one_culprit():
    points = _points(DRAWN)
    four = points[[0, 1, 2, 4]]
    targets = _similar(four)
    targets[3] += (40.0, 0.0)
    assert dh._single_misplaced(four, targets, 3.0) is None, "(iii): three pads left"
    targets = _similar(points)
    targets[4] += (40.0, 0.0)
    targets[2] += (0.0, 40.0)
    assert dh._single_misplaced(points, targets, 3.0) is None, "(ii): two culprits"


# -- the witness's wide search keeps a confirmation ---------------------------

def _header():
    """A 2 x 4 header, 2.54 mm pitch and row spacing."""
    return {str(row * 4 + col + 1): (col * 2.54, row * 2.54) for row in range(2) for col in range(4)}


def test_a_fit_found_only_by_the_wide_search_is_kept_as_distorted(tmp_path):
    """The header on a page stretched 6.5 % along Y, beyond the narrow gate (6 % of the span).

    Before round 6b the wide pass found the assignment and dropped it, and
    the verdict was a «no_match» with no fit at all -- a contradiction of a
    correct layout. Round 6b kept it as a confirmation.

    CONTRACT CHANGED (round 7, review V4-3): a wide-pass fit may not CONFIRM
    a stretch beyond the narrow gate's span (HYPOTHESIS_TOL_SPAN) -- the same
    allowance confirmed readings with a whole axis wrong by 6-7 % -- so it is
    kept as «distorted». That is the price, declared: this correct header is
    no longer confirmed by the holes. It is still no contradiction (no pad is
    singled out), so two agreeing readings of it stay confirmed, with the
    warning.

    CONTRACT CHANGED AGAIN (round 7b, F2, 2026-11-11): «distorted» with a fit
    stretch beyond HYPOTHESIS_TOL_SPAN is a contradiction -- two readings
    with a whole axis 6-7 % wrong produce exactly this verdict (V6, wp6.py,
    refused at 1266fb7) -- and a contradiction is a one-option QUESTION, never
    a block. Price, declared: this correct header on a page stretched 6.5 %
    now costs the user a confirmation. No pad is named.
    """
    header = _header()
    verdict, contradiction = _judged(tmp_path, list(header.values()), header, sy=1.065)
    assert verdict.status == "not_confirmed" and verdict.reason == "distorted"
    assert verdict.max_error_mm is not None, "the fit is kept, not lost to a «no_match»"
    assert verdict.stretch_ratio is not None and verdict.stretch_ratio > dh.HYPOTHESIS_TOL_SPAN
    assert contradiction is not None and contradiction["stretch"] == verdict.stretch_ratio
    assert contradiction["misplaced"] is None and contradiction["singled"] == []


def test_the_capped_wide_search_confirms_no_header_read_at_a_wrong_pitch(tmp_path):
    """Twin of the above (round 7, V4-3): the page to scale, the READING 7 % long on X.

    The wide pass reaches this fit exactly as it reaches the stretched page's,
    and in round 6b it confirmed it (review V4, wp.py). With the cap a stretch
    beyond HYPOTHESIS_TOL_SPAN is never confirmed, so the wrong pitch is not.
    """
    drawn = {str(r * 3 + c + 1): (c * 2.54, r * 2.54) for r in range(2) for c in range(3)}
    wrong = {n: (x * 1.07, y) for n, (x, y) in drawn.items()}
    verdict, _contradiction = _judged(tmp_path, list(drawn.values()), wrong)
    assert verdict.status != "confirmed"


def test_the_wide_search_confirms_no_misplaced_header(tmp_path):
    """Negative twin: the same page with pad 4 read one pitch off -- never confirmed."""
    header = _header()
    wrong = dict(header, **{"4": (7.62 + 2.54, 0.0)})
    verdict, _contradiction = _judged(tmp_path, list(header.values()), wrong, sy=1.065)
    assert verdict.status != "confirmed"


# ---------------------------------------------------------------------------
# V3-2: the generated block of a saved project
# ---------------------------------------------------------------------------

#: A superseded built-in instruction: the first text of test_extraction_review_round6.
from test_extraction_review_round6 import OLD_SINGLE_THT  # noqa: E402

CURRENT = component_types.get("single_tht")


def _notes(instruction, after="Nota dell'utente: rele' a 5 pin."):
    block = component_types.build_block(component_types.ComponentType("single_tht", "x", instruction))
    return f"{block}\n\n{after}"


def test_a_block_holding_a_former_default_is_refreshed():
    notes, outcome = component_types.refresh_block(_notes(OLD_SINGLE_THT), CURRENT)
    assert outcome == component_types.BLOCK_REFRESHED
    assert component_types.block_text(notes) == CURRENT.prompt
    assert notes.endswith("Nota dell'utente: rele' a 5 pin.")


def test_a_block_the_user_edited_is_kept_verbatim():
    """Negative twin: the same old text with one line of the user's own -- a digest no default has."""
    edited = _notes(OLD_SINGLE_THT + "\n- I miei rele' hanno sempre fori da 1.3.")
    notes, outcome = component_types.refresh_block(edited, CURRENT)
    assert outcome == component_types.BLOCK_EDITED and notes == edited


def test_a_block_with_the_current_default_is_left_alone():
    notes = _notes(CURRENT.prompt)
    assert component_types.refresh_block(notes, CURRENT) == (notes, component_types.BLOCK_CURRENT)


def test_notes_without_a_block_are_left_alone():
    for notes in ("", "Solo note mie.", component_types.BLOCK_START + "\nblocco senza fine"):
        assert component_types.refresh_block(notes, CURRENT) == (notes, component_types.BLOCK_ABSENT)


def test_a_stale_block_of_another_type_is_still_resynchronised():
    """The purpose of the refresh kept: a castellated default left in a single THT project."""
    castellated = component_types.get("aggregate_castellated")
    stale = component_types.apply_block("Nota.", castellated)
    notes, outcome = component_types.refresh_block(stale, CURRENT)
    assert outcome == component_types.BLOCK_REFRESHED
    assert component_types.block_text(notes) == CURRENT.prompt and notes.endswith("Nota.")


def test_the_profile_s_own_instruction_counts_as_generated():
    """A type instruction the user changed IN THE PROFILE is a default the program wrote into the block."""
    profile_text = CURRENT.prompt + "\n- Regola del mio profilo."
    profiled = component_types.ComponentType("single_tht", "x", profile_text)
    notes, outcome = component_types.refresh_block(_notes(OLD_SINGLE_THT + "\n- altro"), profiled,
                                                   [profile_text])
    assert outcome == component_types.BLOCK_EDITED
    notes, outcome = component_types.refresh_block(_notes(CURRENT.prompt), profiled, [profile_text])
    assert outcome == component_types.BLOCK_REFRESHED and component_types.block_text(notes) == profile_text


def _open_saved(window, monkeypatch, prompt, saved_before_digest=False):
    """Save a single THT project whose notes are `prompt`, open another, reopen it; return its notes.

    `saved_before_digest` writes the project as a version older than review
    V5-1 did: without `generated_block_sha256`, so the open decides by the
    texts the program can have written.
    """
    from PyQt6.QtWidgets import QDialog
    from kicadgen.gui import main_window as gui
    from kicadgen.gui.project_dialogs import OpenProjectDialog, ProjectNameDialog

    monkeypatch.setattr(gui, "save_config", lambda config: None)

    def new(name):
        def accept(self):
            self.field.setText(name)
            return QDialog.DialogCode.Accepted
        monkeypatch.setattr(ProjectNameDialog, "exec", accept)
        window._new_project()

    new("BloccoTipologia")
    window.combo_component_type.setCurrentIndex(window.combo_component_type.findData("single_tht"))
    window.field_component_prompt.setPlainText(prompt)
    if saved_before_digest:
        window._generated_block_digest = None
    assert window._save_project_config(force=True)
    new("Altro")
    monkeypatch.setattr(OpenProjectDialog, "selected_name", lambda self: "BloccoTipologia")
    monkeypatch.setattr(OpenProjectDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    window._open_project()
    return window.field_component_prompt.toPlainText()


def test_opening_a_project_keeps_an_edited_block_and_says_so(window, monkeypatch):
    edited = _notes(OLD_SINGLE_THT + "\n- I miei rele' hanno sempre fori da 1.3.")
    assert _open_saved(window, monkeypatch, edited) == edited
    assert window._log_plain_text().count("conserva le istruzioni della tipologia modificate") == 1


def test_opening_a_project_refreshes_a_former_default(window, monkeypatch):
    """Negative twin: the block the program wrote follows the current instruction.

    CONTRACT CHANGED (review V5-1, 2026-11-11): a former default typed into
    the notes of a project that records the digest of the block the program
    wrote is no longer «the program's text» -- the stored digest names the
    current one -- so the project is saved as an older version saved it,
    without the digest: there the former default still counts as the
    program's and is refreshed (the rule this test was written for).
    """
    notes = _open_saved(window, monkeypatch, _notes(OLD_SINGLE_THT), saved_before_digest=True)
    stored = window.profile_store.get_type("single_tht")["instructions"]
    assert component_types.block_text(notes) == stored and "stima drill_mm" not in notes
    assert "conserva le istruzioni della tipologia modificate" not in window._log_plain_text()


# ---------------------------------------------------------------------------
# V3-3, V3-4: printed_terminals on malformed entries
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("shape", [["round"], {"round": True}, 7, None])
def test_a_shape_that_is_not_a_word_is_left_out_without_raising(shape):
    leads = printed_terminals([{"pads": "1", "shape": shape, "diameter": 0.6},
                               {"pads": "2", "shape": "round", "diameter": 0.6}], 1.0)
    assert set(leads) == {"2"}, "the well-formed entry beside it is kept"


@pytest.mark.parametrize("pads", [["1"], {"1": 1}, True, None])
def test_pads_that_are_not_a_name_are_left_out_without_raising(pads):
    leads = printed_terminals([{"pads": pads, "shape": "round", "diameter": 0.6}], 1.0)
    assert leads == {}
    assert set(printed_terminals([{"pads": 3, "shape": "round", "diameter": 0.6}], 1.0)) == {"3"}


def test_a_size_infinite_in_millimetres_is_refused():
    """1e308 inches is a finite number that overflows once converted; 0.024 inches is a terminal."""
    leads = printed_terminals([{"pads": "1", "shape": "round", "diameter": 1e308},
                               {"pads": "2", "shape": "flat", "width": 0.04, "thickness": 1e308},
                               {"pads": "3", "shape": "round", "diameter": 0.024}], 25.4)
    assert set(leads) == {"3"}
    assert all(math.isfinite(v) for v in leads["3"]["lead"].values() if isinstance(v, float))


# ---------------------------------------------------------------------------
# V3-5: the program's own keys are never taken from a model answer
# ---------------------------------------------------------------------------

def test_private_keys_are_stripped_at_every_depth():
    answer = {"drawing_unit": "mm", "_dedicated_leads": {"1": {}}, "_dedicated_holes": {},
              "package_geometry": {"pins": [{"number": "1", "x_mm": 0, "_hole_rounding_mm": 9}]},
              "castellated_sides": [{"side": "left", "_contact_style_evidence": "x"}]}
    clean = without_private_keys(answer)
    assert clean == {"drawing_unit": "mm", "package_geometry": {"pins": [{"number": "1", "x_mm": 0}]},
                     "castellated_sides": [{"side": "left"}]}
    assert "_dedicated_leads" in answer, "the answer given is not modified"
    assert without_private_keys(["_x", 1, None]) == ["_x", 1, None], "values are not keys"


def test_no_tool_schema_declares_a_private_field():
    """Negative twin: stripping «_» keys can never drop a field the model was asked for."""
    def names(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "properties" and isinstance(value, dict):
                    yield from value
                yield from names(value)
        elif isinstance(node, list):
            for value in node:
                yield from names(value)

    for tool in (ext._openai_tool(), ext._single_component_tool(), ext._pad_positions_tool()):
        assert not [name for name in names(tool) if name.startswith("_")]


from test_datasheet_extractor import _TINY_PNG, _matrix_payload  # noqa: E402

#: The program's own conclusions, as a model could try to send them.
INJECTED = {"_pin_map_conflict": "il modello dice: conflitto", "_body_conflict": "il modello dice: corpo",
            "_numbering_note": "il modello dice: confermate"}


@pytest.mark.parametrize("family", [None, "single_tht"], ids=["board answer", "single-part answer"])
def test_a_main_answer_cannot_set_the_program_s_conclusions(monkeypatch, tmp_path, family):
    """Positive: a board answer goes to the program AS SENT, so its «_» keys used to become conclusions.

    The single-part answer is the structural negative twin: it is rebuilt
    field by field (`single_answer_to_data`), so the keys never reached the
    program there; both must give no conflict and no note of the model's.
    """
    path = tmp_path / "drawing.png"
    path.write_bytes(_TINY_PNG)
    monkeypatch.setattr(ext, "api_key", lambda: "offline")
    if family is None:
        answer = dict(_matrix_payload(), **INJECTED)
        monkeypatch.setattr(ext, "call_tool", lambda **kw: answer)
        params = ext.extract_pin_header_params([path])
    else:
        from test_single_component_prompt import _single
        answer = dict(_single(), **INJECTED)
        calls = iter([answer])
        monkeypatch.setattr(ext, "call_tool", lambda **kw: next(calls, {"drawing_unit": "mm"}))
        params = ext.extract_pin_header_params([path], component_family=family)
    # The program may reach its own conflict (a single part without a second
    # reading does); none of the model's words may stand as one.
    for text in (params.pin_map_conflict, params.body_conflict, params.notes):
        assert "il modello dice" not in (text or "")
    # The only «_» key of the stored answer is the program's own record of the
    # model that gave it (RAW_MAIN_MODEL_KEY, review finding 1 of 2026-11-12),
    # never one of the injected conclusions.
    raw = params.raw_main or {}
    assert [key for key in raw if key.startswith("_")] == [ext.RAW_MAIN_MODEL_KEY]
    assert raw[ext.RAW_MAIN_MODEL_KEY] == ext.DEFAULT_MODEL
