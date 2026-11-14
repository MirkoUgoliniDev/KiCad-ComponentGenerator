"""@brief Round 8 questions in the window: «row_spacing» and «contact_style» (2026-11-13).

@details Both decide the footprint pads, so an open one blocks every footprint
write like the pad-layout and side-numbering questions; «Rigenera footprint»
asks it, and the answer reaches the window: the chosen printed distance
between the contact rows (or the estimate), the chosen contact type of a side.
A single recommended pad-layout option is the dialog's default button, and a
photo crop is shown as the evidence of a contact-type question. Offline:
`apply_answers` is the real one, nothing is paid. Positive and negative cases.
"""

from PyQt6.QtGui import QImage

from kicadgen.gui import main_window as gui
from kicadgen.services import datasheet_extractor as ext

ROW_KEY = "row_spacing:21.80|22.86|estimate"
ROW_QUESTION = {"key": ROW_KEY, "field": "row_spacing", "text": "Distanza fra le file?",
                "evidence": None,
                "options": [{"label": "22.86 mm", "value": "22.86"},
                            {"label": "21.80 mm", "value": "21.80"},
                            {"label": "Non so: stima", "value": "estimate"}]}
STYLE_KEY = "contact_style:half_and_through|through_hole|top"
STYLE_QUESTION = {"key": STYLE_KEY, "field": "contact_style", "text": "Lato superiore?",
                  "evidence": {"document": "photo.png", "crop": [0, 0, 40, 20]},
                  "options": [{"label": "Fori passanti", "value": "through_hole", "recommended": True},
                              {"label": "Semi-fori + fori", "value": "half_and_through"}]}


def _side(edge, style):
    """A castellated side as the extraction gives it."""
    return {"edge": edge, "count": 3, "pitch_mm": 2.54, "offset_mm": 0.0, "first_pin": 1,
            "reverse": False, "contact_style": style}


def _params(question, candidates):
    """A settled castellated extraction that still holds `question`."""
    return ext.ExtractedParams(name="", description="", tags="", confidence="low", notes="",
                               layout="castellated",
                               castellated_sides=[_side("top", "half_hole"), _side("bottom", "through_hole")],
                               questions=[dict(question)], candidates=candidates)


def _prepare(window, monkeypatch, params, answer):
    """The window holding the open question, the settled extraction and a scripted answer."""
    window.group_castellated.setChecked(True)
    window._sides.clear()
    window._add_side_row(_side("top", "half_hole"))
    window._add_side_row(_side("bottom", "through_hole"))
    window._extraction_questions = window._valid_extraction_questions(params.questions)
    monkeypatch.setattr(window, "_settled_extraction", lambda: params)
    monkeypatch.setattr(window, "_ask_extraction_question", lambda question: answer)
    for name in ("_dropped_footprint_pads", "_pads_with_unread_holes", "_pads_with_unfit_values",
                 "_holes_read_elsewhere", "_unquoted_unread_pads"):
        monkeypatch.setattr(window, name, lambda *a, **k: [])
    monkeypatch.setattr(window, "_unread_pads_account", lambda *a, **k: None)


def test_both_fields_are_footprint_questions(window, monkeypatch):
    """@brief Positive: both pass the filter and block unattended writes; negative: none open -> ''."""
    assert {"row_spacing", "contact_style"} <= set(window.FOOTPRINT_QUESTION_FIELDS)
    for question, words in ((ROW_QUESTION, "distanza fra le file"), (STYLE_QUESTION, "tipo di contatto")):
        window._extraction_questions = window._valid_extraction_questions([question])
        assert window._extraction_questions
        assert words in window._pad_layout_question_block()
        logged = []
        monkeypatch.setattr(window, "_append_log", lambda text, *a, **k: logged.append(str(text)))
        monkeypatch.setattr(window, "_generate_applied", lambda **k: logged.append("WRITTEN") or True)
        assert window._generate(automatic=True) is False and "WRITTEN" not in logged
    window._extraction_questions = []
    assert window._pad_layout_question_block() == ""


def test_a_row_spacing_answer_reaches_the_window(window, monkeypatch):
    """@brief Positive: the chosen printed distance is the declared row spacing and the question
    closes; negative: «estimate» leaves no declared value (today's estimate behaviour)."""
    candidates = {ROW_KEY: {"22.86": {"row_spacing_mm": 22.86}, "21.80": {"row_spacing_mm": 21.80},
                            "estimate": {}}}
    _prepare(window, monkeypatch, _params(ROW_QUESTION, candidates), "22.86")
    window._declared_row_spacing = None
    assert window._resolve_stored_pad_layout_question() is True
    assert window._declared_row_spacing == 22.86
    assert window._extraction_questions == [] and window._extraction_answers.get(ROW_KEY) == "22.86"
    _prepare(window, monkeypatch, _params(ROW_QUESTION, candidates), "estimate")
    assert window._resolve_stored_pad_layout_question() is True
    assert window._declared_row_spacing is None


def test_a_contact_style_answer_reaches_the_window(window, monkeypatch):
    """@brief Positive: the top side takes the chosen style; negative: the bottom side is untouched.

    CONTRACT CHANGED 2026-11-14 (GUI review M3): the extractor stores a
    contact-type question WITHOUT candidates (its answer is applied from the
    option's value); the hand-built candidates hid that such a question could
    never be answered from «Rigenera footprint». The real shape now.
    """
    _prepare(window, monkeypatch, _params(STYLE_QUESTION, None), "half_and_through")
    assert window._resolve_stored_pad_layout_question() is True
    styles = {side["edge"]: side["contact_style"] for side in window._sides}
    assert styles == {"top": "half_and_through", "bottom": "through_hole"}
    assert window._extraction_questions == []


def test_back_keeps_the_question_open_and_writes_nothing(window, monkeypatch):
    """@brief Negative: «Torna indietro» on a contact-type question -> nothing applied, still open."""
    candidates = {STYLE_KEY: {"through_hole": {}, "half_and_through": {}}}
    _prepare(window, monkeypatch, _params(STYLE_QUESTION, candidates), gui.QUESTION_BACK)
    assert window._resolve_stored_pad_layout_question() is False
    assert {side["edge"]: side["contact_style"] for side in window._sides}["top"] == "half_hole"
    assert window._extraction_questions and window._extraction_questions[0]["field"] == "contact_style"


def _shown(window, monkeypatch, question):
    """Open the real dialog, read its default button and icon, close it with «Torna indietro»."""
    seen = {}

    def run_modal(box):
        seen["default"] = box.defaultButton().text()
        seen["pixmap"] = (box.iconPixmap().width(), box.iconPixmap().height())
        box.escapeButton().click()
    monkeypatch.setattr(window, "_run_modal", run_modal)
    assert window._ask_extraction_question(question) is gui.QUESTION_BACK
    return seen


def test_a_single_recommended_pad_layout_is_the_default(window, monkeypatch):
    """@brief Positive (agreed readings): the one recommended option is the default button;
    negative: the same option without the flag leaves «Torna indietro» as the default."""
    flagged = {"key": "pad_layout:x", "field": "pad_layout", "text": "Posizioni dei pad?",
               "options": [{"label": "lettura concorde", "value": "main:1", "recommended": True}]}
    question = window._valid_extraction_questions([flagged])[0]
    assert _shown(window, monkeypatch, question)["default"] == "Opzione 1"
    plain = window._valid_extraction_questions([dict(flagged, options=[
        {"label": "lettura", "value": "main:1"}])])[0]
    assert _shown(window, monkeypatch, plain)["default"] == window.i18n.text("Torna indietro")


def test_a_photo_crop_is_the_evidence_of_a_contact_type_question(window, monkeypatch):
    """@brief Positive: the photo file of the project shows its crop strip; negative: a missing
    file falls back to the stock icon without failing."""
    docs = window._docs_dir()
    docs.mkdir(parents=True, exist_ok=True)
    image = QImage(200, 100, QImage.Format.Format_RGB32)
    image.fill(0x808080)
    assert image.save(str(docs / "photo.png"))
    question = window._valid_extraction_questions([STYLE_QUESTION])[0]
    assert _shown(window, monkeypatch, question)["pixmap"] == (40, 20)
    missing = window._valid_extraction_questions([dict(STYLE_QUESTION, evidence={
        "document": "missing.png", "crop": [0, 0, 40, 20]})])[0]
    assert _shown(window, monkeypatch, missing)["pixmap"] != (40, 20)


def test_a_weakened_agreed_layout_is_shown_without_recommendation(window, monkeypatch):
    """@brief Positive (round 10): the extractor's question for an agreed layout the drawn holes
    weaken shows its warning sentence in the dialog, no «consigliata» badge, and «Torna indietro»
    as the default. Negative: a layout no witness weakens keeps the badge and is the default."""
    from test_extractor_round8_real_runs import _agreed, _questions, _witness
    seen = {}

    def run_modal(box):
        seen["text"] = box.text()
        seen["html"] = box.informativeText()
        seen["default"] = box.defaultButton().text()
        box.escapeButton().click()
    monkeypatch.setattr(window, "_run_modal", run_modal)
    (weak,) = _questions(_agreed(_witness("unverifiable", "unanchored", misplaced="3")), "pad_layout")
    question = window._valid_extraction_questions([weak])[0]
    assert window._ask_extraction_question(question) is gui.QUESTION_BACK
    assert "Controllare il disegno prima di confermare" in seen["text"]
    assert "scarto massimo 0.21 mm, tolleranza 0.15 mm" in seen["text"]
    assert "consigliata" not in seen["html"]
    assert seen["default"] == window.i18n.text("Torna indietro")
    (sound,) = _questions(_agreed(_witness("unverifiable", "too_few_circles")), "pad_layout")
    assert window._ask_extraction_question(window._valid_extraction_questions([sound])[0]) \
        is gui.QUESTION_BACK
    assert "Controllare il disegno" not in seen["text"]
    assert "consigliata" in seen["html"] and seen["default"] == "Opzione 1"


def test_an_unconfirmed_row_spacing_reading_is_never_the_default(window, monkeypatch):
    """@brief Positive (round 11): a row distance read only from the model's own labels is the
    first option but carries no badge, and «Torna indietro» stays the default. Negative: the
    question still lists the printed quotes and the estimate as plain options."""
    from kicadgen.services.messages import tx
    seen = {}

    def run_modal(box):
        seen["text"] = box.text()
        seen["html"] = box.informativeText()
        seen["default"] = box.defaultButton().text()
        seen["buttons"] = [b.text() for b in box.buttons()]
        box.escapeButton().click()
    monkeypatch.setattr(window, "_run_modal", run_modal)
    question = {
        "key": "row_spacing:21.80|22.86|estimate", "field": "row_spacing",
        "text": tx("Distanza fra le file di contatti {value} mm letta dall'analisi solo da come ha "
                   "etichettato le quote stampate, senza un riscontro: quale quota stampata e' la "
                   "distanza fra i centri delle due file?", value="22.86"),
        "options": [
            {"label": tx("{value} mm (letta dall'analisi come distanza fra le file, su una sola "
                         "lettura delle etichette delle quote)", value="22.86"), "value": "22.86"},
            {"label": "21.80 mm", "value": "21.80"},
            {"label": tx("Non so: stima la distanza dei fori dal bordo"), "value": "estimate"}]}
    valid = window._valid_extraction_questions([question])[0]
    assert window._ask_extraction_question(valid) is gui.QUESTION_BACK
    assert "senza un riscontro" in seen["text"]
    assert "consigliata" not in seen["html"] and "una sola lettura" in seen["html"]
    assert seen["default"] == window.i18n.text("Torna indietro")
    assert seen["buttons"][:3] == ["Opzione 1", "Opzione 2", "Opzione 3"]
