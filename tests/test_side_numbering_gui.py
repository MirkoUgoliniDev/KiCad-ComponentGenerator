"""@brief The «side_numbering» question in the main window (docs/pinout-coherence-plan.md, part C).

@details When the castellated sides and the document pinout are not one
numbering, the extractor asks «side_numbering». The window treats it like the
pad-layout question: an open one blocks every footprint write, «Rigenera
footprint» / «Applica ai file» ask it first, and an answer brings the sides'
numbering and the pinout into the window. The DOCUMENT pinout names the pins
whenever the analysis has one (coordinator decision 2026-11-13); the side
labels only without one, or after «keep_sides» (where the extractor already
made the pinout the side labels). Positive and negative cases each.
"""

from types import SimpleNamespace

from kicadgen.gui import main_window as gui

QUESTION = {"key": "side_numbering:abc", "field": "side_numbering",
            "text": "Numerazione dei contatti?", "evidence": None,
            "options": [{"label": "Rinumera", "value": "renumber"},
                        {"label": "Tieni", "value": "keep_sides"}]}


def _side(edge, count, first_pin, reverse=False, names=None):
    """A castellated side dict as the extraction gives it."""
    side = {"edge": edge, "count": count, "pitch_mm": 2.54, "offset_mm": 0.0,
            "first_pin": first_pin, "reverse": reverse, "contact_style": "half_hole"}
    if names is not None:
        side["pin_names"] = list(names)
    return side


def _params(pinout, sides):
    """A minimal settled castellated extraction."""
    return SimpleNamespace(pinout=pinout, castellated_sides=sides, is_castellated=lambda: True)


def test_the_question_is_a_known_footprint_question(window):
    """@brief Positive: «side_numbering» passes the question filter; negative: an unknown field does not."""
    kept = window._valid_extraction_questions([QUESTION, dict(QUESTION, key="x:1", field="unknown")])
    assert [q["field"] for q in kept] == ["side_numbering"]
    assert "side_numbering" in window.FOOTPRINT_QUESTION_FIELDS


def test_an_open_side_numbering_question_blocks_footprint_writes(window, monkeypatch):
    """@brief Positive: open question -> unattended write refused with its own text; negative: none -> ''."""
    window._extraction_questions = []
    assert window._pad_layout_question_block() == ""
    window._extraction_questions = [dict(QUESTION)]
    block = window._pad_layout_question_block()
    assert "numerazione dei contatti" in block and "posizioni dei pad" not in block
    logged = []
    monkeypatch.setattr(window, "_append_log", lambda text, *a, **k: logged.append(str(text)))
    monkeypatch.setattr(window, "_generate_applied", lambda **k: logged.append("WRITTEN") or True)
    assert window._generate(automatic=True) is False
    assert "WRITTEN" not in logged and any("numerazione dei contatti" in line for line in logged)


def test_a_user_command_asks_it_and_back_writes_nothing(window, monkeypatch):
    """@brief Positive: «Torna indietro» keeps it open and nothing is written; the paragraph says so."""
    window._extraction_questions = [dict(QUESTION)]
    monkeypatch.setattr(window, "_settled_extraction", lambda: SimpleNamespace(
        questions=[dict(QUESTION)], candidates={QUESTION["key"]: {"renumber": {}}}))
    asked = []
    monkeypatch.setattr(window, "_ask_extraction_question",
                        lambda question: asked.append(question["field"]) or gui.QUESTION_BACK)
    assert window._resolve_stored_pad_layout_question() is False
    assert asked == ["side_numbering"]
    assert window._extraction_questions and window._extraction_questions[0]["field"] == "side_numbering"


def test_an_answer_renumbers_the_sides_and_names_the_pins_from_the_document(window, monkeypatch):
    """@brief Positive: «renumber» moves the side's first pin and the document names follow.

    @note Negative: the side the answer does not mention keeps its numbering.
    """
    window.group_castellated.setChecked(True)
    window._sides.clear()
    window._add_side_row(_side("top", 2, 3, reverse=True))
    window._add_side_row(_side("bottom", 2, 7))
    window._extraction_questions = [dict(QUESTION)]
    extraction = SimpleNamespace(questions=[dict(QUESTION)],
                                 candidates={QUESTION["key"]: {"renumber": {}}})
    monkeypatch.setattr(window, "_settled_extraction", lambda: extraction)
    monkeypatch.setattr(window, "_ask_extraction_question", lambda question: "renumber")
    document = [{"pin": 1, "name": "GND"}, {"pin": 2, "name": "GPIO14 (ADC2_CH3)"}]
    updated = SimpleNamespace(questions=[], package_geometry=None, pin_map_conflict=None,
                              castellated_sides=[_side("top", 2, 1, reverse=False)],
                              pinout=document, is_castellated=lambda: True,
                              unread_pad_geometry=None)
    from kicadgen.services import datasheet_extractor
    monkeypatch.setattr(datasheet_extractor, "apply_answers", lambda extracted, answers: updated)
    for name in ("_dropped_footprint_pads", "_pads_with_unread_holes", "_pads_with_unfit_values",
                 "_holes_read_elsewhere", "_unquoted_unread_pads"):
        monkeypatch.setattr(window, name, lambda *a, **k: [])
    monkeypatch.setattr(window, "_unread_pads_account", lambda *a, **k: None)
    settled = []
    monkeypatch.setattr(window, "_settle_extracted_pinout", lambda rows: settled.append(rows))
    assert window._resolve_stored_pad_layout_question() is True
    by_edge = {side["edge"]: side for side in window._sides}
    assert (by_edge["top"]["first_pin"], by_edge["top"]["reverse"]) == (1, False)
    assert (by_edge["bottom"]["first_pin"], by_edge["bottom"]["reverse"]) == (7, False)
    assert settled == [document]
    assert window._extraction_questions == []
    assert window._extraction_answers.get(QUESTION["key"]) == "renumber"


def test_document_names_win_over_side_labels_when_one_numbering(window):
    """@brief Positive: with a document pinout its names are proposed, not the silkscreen «14».

    @note Negative: without a document pinout the side labels are kept.
    """
    sides = [_side("left", 2, 1, names=["13", "14"])]
    document = [{"pin": 1, "name": "GPIO13 (TOUCH13)"}, {"pin": 2, "name": "GPIO14 (TOUCH14)"}]
    side_labels = gui._side_pinout(sides[0])
    assert side_labels == [{"pin": 1, "name": "13"}, {"pin": 2, "name": "14"}]
    chosen = window._pinout_candidate_after_sides(_params(document, sides), side_labels)
    assert [row["name"] for row in chosen] == ["GPIO13 (TOUCH13)", "GPIO14 (TOUCH14)"]
    assert window._pinout_candidate_after_sides(_params(None, sides), side_labels) == side_labels


def test_after_keep_sides_the_side_labels_name_the_pins(window):
    """@brief Positive: «keep_sides» made the pinout the labels, so the labels are used.

    @note The rule is the same precedence: the extractor already replaced the
        pinout with the side labels (`kept_sides_pinout`), the window does not guess.
    """
    from kicadgen.services.pinout_coherence import kept_sides_pinout
    sides = [_side("left", 2, 1, names=["G", "3V3"])]
    kept = kept_sides_pinout(sides, [{"pin": 5, "name": "GND"}])
    chosen = window._pinout_candidate_after_sides(_params(kept, sides), gui._side_pinout(sides[0]))
    assert [row["name"] for row in chosen] == ["G", "3V3"]


def test_unmatched_entries_are_logged_with_the_remedy_only_while_asked(window, monkeypatch):
    """@brief Positive: open question -> one line names the entries and the remedy; negative: none -> silent."""
    logged = []
    monkeypatch.setattr(window, "_append_log", lambda text, *a, **k: logged.append(str(text)))
    params = _params([{"pin": n, "name": f"P{n}"} for n in range(1, 6)], [_side("left", 2, 1)])
    window._extraction_questions = []
    window._log_pinout_without_pads(params)
    assert logged == []
    window._extraction_questions = [dict(QUESTION)]
    window._log_pinout_without_pads(params)
    assert len(logged) == 1 and "3-5" in logged[0] and "Rigenera footprint" in logged[0]


def _shown(window, monkeypatch, question):
    """Open the real question dialog, read it, close it with «Torna indietro»."""
    from PyQt6.QtGui import QTextDocument
    seen = {}

    def run_modal(box):
        seen["html"] = box.informativeText()
        document = QTextDocument()
        document.setHtml(seen["html"])
        seen["plain"] = document.toPlainText()
        seen["default"] = box.defaultButton().text()
        box.escapeButton().click()
    monkeypatch.setattr(window, "_run_modal", run_modal)
    assert window._ask_extraction_question(question) is gui.QUESTION_BACK
    return seen


def test_the_options_are_highlighted_and_the_recommended_one_is_the_default(window, monkeypatch):
    """@brief Positive: bold accent headings, a badge and the default button on the recommended option."""
    question = dict(QUESTION, options=[
        {"label": "Rinumera i contatti", "value": "renumber", "recommended": True},
        {"label": "Tieni i lati", "value": "keep_sides"}])
    seen = _shown(window, monkeypatch, window._valid_extraction_questions([question])[0])
    assert f'<b style="color:{gui.BLUE}">Opzione 1:</b>' in seen["html"]
    assert f'<b style="color:{gui.BLUE}">Opzione 2:</b>' in seen["html"]
    assert seen["html"].count("consigliata") == 1
    assert seen["default"] == "Opzione 1"


def test_without_a_recommended_option_back_stays_the_default(window, monkeypatch):
    """@brief Negative: no declared flag (wording alone does not count) -> «Torna indietro» is the default."""
    question = dict(QUESTION, options=[
        {"label": "Rinumera i contatti (consigliata)", "value": "renumber", "recommended": "yes"},
        {"label": "Tieni i lati", "value": "keep_sides"}])
    valid = window._valid_extraction_questions([question])[0]
    assert all("recommended" not in option for option in valid["options"])
    seen = _shown(window, monkeypatch, valid)
    assert seen["default"] == window.i18n.text("Torna indietro")
    assert "background-color" not in seen["html"]


def test_option_texts_are_escaped_and_shown_literally(window, monkeypatch):
    """@brief Positive: a pin name «<A&B>» is escaped in the markup and reads literally on screen."""
    question = dict(QUESTION, options=[{"label": "pin 3 «<A&B>»", "value": "renumber"},
                                       {"label": "Tieni", "value": "keep_sides"}])
    seen = _shown(window, monkeypatch, window._valid_extraction_questions([question])[0])
    assert "&lt;A&amp;B&gt;" in seen["html"] and "<A&B>" not in seen["html"]
    assert "pin 3 «<A&B>»" in seen["plain"]


def _bottom_view(answer):
    """The same board read on a BOTTOM view: what the model would report."""
    import copy
    from kicadgen.services import datasheet_extractor as de
    seen = copy.deepcopy(answer)
    de._mirror_castellated_sides(seen["castellated_sides"])
    seen["contacts_view"] = "bottom"
    return seen


import pytest  # noqa: E402


@pytest.mark.parametrize("view", ["top", "bottom"])
@pytest.mark.parametrize("convention", ["devkit", "nano"])
def test_after_renumber_the_window_holds_the_extractors_frame_and_the_document_names(
        window, monkeypatch, tmp_path, view, convention):
    """@brief Positive (ESP32 report, 2026-11-13): an analysis applied with the answer «renumber»
    gives the window EXACTLY the sides of `apply_answers` (first pin and direction by edge,
    canonical frame) and a pinout whose names are the document's on the same numbers.

    @note Run on a top and a bottom view: the bottom answer is mirrored by the extractor
        (left/right swap, top/bottom direction flips), so a frame mix-up in the window
        would show as an inverted `reverse`. Offline: the model call is faked.
    """
    from test_extractor_side_pinout import _run, _shifted
    from test_pinout_coherence import CONVENTIONS
    from kicadgen.services import datasheet_extractor as de
    monkeypatch.setattr(de, "api_key", lambda: "offline")
    monkeypatch.setattr(de, "check_credit", lambda **kw: None)
    answer = _shifted(convention)
    if view == "bottom":
        answer = _bottom_view(answer)
    params, _call, _lines = _run(tmp_path, monkeypatch, [answer, answer])
    (question,) = [q for q in params.questions if q["field"] == "side_numbering"]
    expected = de.apply_answers(params, {question["key"]: "renumber"})
    window.group_castellated.setChecked(True)
    window._extraction_answers = {}
    monkeypatch.setattr(window, "_ask_extraction_question", lambda q: "renumber")
    applied, held_back = window._review_extraction_questions(params)
    assert not held_back
    window._extraction_side_pinout = []
    window._apply_extracted_castellated(applied)
    side_pinout, window._extraction_side_pinout = window._extraction_side_pinout, None
    window._settle_extracted_pinout(window._pinout_candidate_after_sides(applied, side_pinout))
    by_edge = {side["edge"]: (side["first_pin"], bool(side["reverse"])) for side in window._sides}
    assert by_edge == {side["edge"]: (side["first_pin"], bool(side.get("reverse")))
                       for side in expected.castellated_sides}
    assert by_edge == CONVENTIONS[convention][3]
    document = {str(entry["pin"]): entry["name"] for entry in CONVENTIONS[convention][0]}
    names = {str(row["pin"]): row["name"] for row in window.pinout}
    assert names == {pin: name for pin, name in document.items() if pin in names}
    assert set(names) == set(document)


def test_the_numbering_convention_question_no_longer_exists(window):
    """@brief Negative (CONTRACT CHANGED 2026-11-13, user decision): «numbering_convention» is no
    question field any more -- a stale one is dropped, never asked nor blocking."""
    question = dict(QUESTION, key="numbering_convention:abc", field="numbering_convention")
    assert window._valid_extraction_questions([question]) == []
    assert "numbering_convention" not in window.FOOTPRINT_QUESTION_FIELDS


def test_without_printed_numbers_the_convention_applies_by_itself(window, monkeypatch, tmp_path):
    """@brief Positive (user decision 2026-11-13): no printed numbers -> no question, and the
    extractor's conventional numbering reaches the window unchanged (sides and pinout).

    @note Replaces the test of the dropped «numbering_convention» answer (CONTRACT CHANGED).
        Negative: the component check then makes no «document» claim about numbers.
    """
    from test_extractor_side_pinout import _run, _shifted
    from kicadgen.services import datasheet_extractor as de
    from kicadgen.services.component_check import document_checks
    monkeypatch.setattr(de, "api_key", lambda: "offline")
    monkeypatch.setattr(de, "check_credit", lambda **kw: None)
    answer = _shifted("devkit")
    answer["pin_numbers_printed"] = False
    # Two identical answers: whether the extractor retries is its own contract
    # (tests/test_extractor_side_pinout.py); here only the window side is checked.
    params, _call, _lines = _run(tmp_path, monkeypatch, [answer, answer])
    assert not [q for q in params.questions
                if q["field"] in ("numbering_convention", "side_numbering")]
    window.group_castellated.setChecked(True)
    window._extraction_answers = {}
    monkeypatch.setattr(window, "_ask_extraction_question",
                        lambda q: pytest.fail("no question expected"))
    applied, held_back = window._review_extraction_questions(params)
    assert not held_back and window._pin_numbers_printed is False
    window._extraction_side_pinout = []
    window._apply_extracted_castellated(applied)
    side_pinout, window._extraction_side_pinout = window._extraction_side_pinout, None
    window._settle_extracted_pinout(window._pinout_candidate_after_sides(applied, side_pinout))
    by_edge = {side["edge"]: (side["first_pin"], bool(side["reverse"])) for side in window._sides}
    assert by_edge == {side["edge"]: (side["first_pin"], bool(side.get("reverse")))
                       for side in applied.castellated_sides}
    # The window shows the extractor's conventional pinout exactly: whether that
    # pinout puts each name on its own contact is the extractor's contract
    # (tests/test_extractor_side_pinout.py), not re-derived here.
    names = {str(row["pin"]): row["name"] for row in window.pinout}
    assert names == {str(row["pin"]): row["name"] for row in applied.pinout
                     if str(row["pin"]) in names}
    monkeypatch.setattr(window, "_main_answer_for_reread", lambda: params.raw_main)
    window._document_sides = window._canonical_document_sides(applied.castellated_sides)
    document = window._component_check_document()
    rows = [{"pin": pin, "name": name} for pin, name in names.items()]
    results = document_checks(document, rows, set(names))
    assert results[0].code == "document_numbering_convention"
    assert not any(r.code == "document_dropped" or r.status == "ERROR" for r in results)
