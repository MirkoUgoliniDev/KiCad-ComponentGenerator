"""@brief The structured questions of an extraction and the one deferral reason (review 2026-11-04, H7 / M16 / low findings, GUI part).

The extractor may return `ExtractedParams.questions` (view, body height,
pad layout) with the candidates as options. The window asks each one in a
dialog shaped like the geometry doubts, records the answer in the project
(`extraction_answers`), applies it locally through
`datasheet_extractor.apply_answers` and never asks a known key again;
«Torna indietro» holds the automatic generation back. The STEP deferral
has ONE reason (conflict or missing quotes) shared by the log, the final
account and the STEP tab, saved with the project.

Offline: the paid worker never starts (`tests.ui_flow_helpers`), FreeCAD is
absent, every dialog is replaced.
"""
from PyQt6.QtWidgets import QApplication, QMessageBox

from kicadgen.gui import main_window as gui
from kicadgen.services import datasheet_extractor
from tests.ui_flow_helpers import header_extraction, package_extraction, prepare, run_extraction, soic_geometry


#: The real dialog, captured at import (tests replace it per test).
_REAL_ASK = gui.MainWindow._ask_extraction_question

CONFLICT = "altezza: le due letture non coincidono (18 principale, 23 dedicata)."


def _question(key, field="body_height_mm", values=(18.0, 23.0), evidence=None):
    """A well-formed question with two candidates."""
    return {"key": key, "field": field, "text": f"Quale valore per {key}?",
            "options": [{"label": f"{v:g} mm" if isinstance(v, float) else str(v), "value": v}
                        for v in values],
            "evidence": evidence}


def _with_questions(params, *questions):
    params.questions = list(questions)
    return params


def _answer_first(monkeypatch, seen):
    """Every question is answered with its first option; the questions are recorded."""
    def ask(self, question):
        seen.append(dict(question))
        return question["options"][0]["value"]
    monkeypatch.setattr(gui.MainWindow, "_ask_extraction_question", ask)


def _fake_apply(monkeypatch, calls):
    """A stand-in for the extractor's apply_answers: records the calls and
    writes a chosen height (the field the window can also apply alone)."""
    def apply(params, answers):
        calls.append(dict(answers))
        for question in list(getattr(params, "questions", None) or ()):
            value = answers.get(question["key"])
            if value is not None and question["field"] == "body_height_mm":
                params.body_height_mm = float(value)
                params.body_conflict = None
        params.questions = [q for q in params.questions if q["key"] not in answers] or None
        return params
    monkeypatch.setattr(datasheet_extractor, "apply_answers", apply, raising=False)


def _footprint(window, name="HDR"):
    return window._project_dir() / f"{name}.pretty" / f"{name}.kicad_mod"


# ── Questions (H7) ──

def test_two_questions_open_two_dialogs_answers_saved_applied_and_generation_runs(window, monkeypatch, tmp_path):  # noqa: F811
    prepare(window, monkeypatch, tmp_path, "HDR")
    seen, calls = [], []
    _answer_first(monkeypatch, seen)
    _fake_apply(monkeypatch, calls)
    params = _with_questions(header_extraction(body_height_mm=None, body_conflict=CONFLICT),
                             _question("h:1"), _question("v:1", field="contacts_view",
                                                          values=("top", "bottom")))
    run_extraction(window, params)
    assert [q["key"] for q in seen] == ["h:1", "v:1"]
    assert calls == [{"h:1": 18.0, "v:1": "top"}]
    assert window._project_state()["extraction_answers"] == {"h:1": 18.0, "v:1": "top"}
    # The height chosen is the one applied: no conflict, no deferral.
    assert window._last_extracted.body_height_mm == 18.0
    assert window._last_extracted.body_conflict is None
    assert _footprint(window).exists()
    assert window._step_deferral_reason() == ("", "")


def test_a_known_key_is_not_asked_again_but_still_applied(window, monkeypatch, tmp_path):  # noqa: F811
    prepare(window, monkeypatch, tmp_path, "HDR")
    seen, calls = [], []
    _answer_first(monkeypatch, seen)
    _fake_apply(monkeypatch, calls)
    window._extraction_answers = {"h:1": 23.0}
    run_extraction(window, _with_questions(header_extraction(body_height_mm=None), _question("h:1")))
    assert seen == []
    assert calls == [{"h:1": 23.0}]
    assert window._last_extracted.body_height_mm == 23.0
    assert "Domanda già risolta per questo progetto" in window._log_plain_text()


def test_torna_indietro_holds_the_generation_back_and_records_nothing(window, monkeypatch, tmp_path):  # noqa: F811
    prepare(window, monkeypatch, tmp_path, "HDR")
    calls = []
    _fake_apply(monkeypatch, calls)
    monkeypatch.setattr(gui.MainWindow, "_ask_extraction_question",
                        lambda self, question: gui.QUESTION_BACK)
    run_extraction(window, _with_questions(header_extraction(body_height_mm=None), _question("h:1")))
    assert not _footprint(window).exists()
    assert calls == [] and window._extraction_answers == {}
    assert "Generazione automatica non avviata: una domanda dell'analisi" in window._log_plain_text()
    # The open question is kept for «Rigenera modello STEP» and saved.
    assert [q["key"] for q in window._project_state()["extraction_questions"]] == ["h:1"]


def test_without_the_extractor_apply_the_window_applies_a_height_alone(window, monkeypatch, tmp_path):  # noqa: F811
    """Defensive contract: no `apply_answers` in the installed extractor,
    a height answer is still written and its conflict closed."""
    prepare(window, monkeypatch, tmp_path, "HDR")
    monkeypatch.delattr(datasheet_extractor, "apply_answers", raising=False)
    seen = []
    _answer_first(monkeypatch, seen)
    run_extraction(window, _with_questions(
        header_extraction(body_height_mm=None, body_conflict=CONFLICT, value_sources={}),
        _question("h:1", values=(23.0, 18.0))))
    extracted = window._last_extracted
    assert extracted.body_height_mm == 23.0 and extracted.body_conflict is None
    assert extracted.value_sources["body_height_mm"] == "user"
    assert window.field_body_height.value() == 23.0 and window._body_height_declared


def test_malformed_questions_are_ignored(window, monkeypatch, tmp_path):  # noqa: F811
    """Negative: an entry without key, with an unknown field or without
    options is not a question (an older or partial extractor never blocks)."""
    prepare(window, monkeypatch, tmp_path, "HDR")
    seen = []
    _answer_first(monkeypatch, seen)
    run_extraction(window, _with_questions(
        header_extraction(), {"field": "body_height_mm", "text": "x", "options": [{"label": "a", "value": 1}]},
        {"key": "k", "field": "colour", "text": "x", "options": [{"label": "a", "value": 1}]},
        {"key": "k2", "field": "body_height_mm", "text": "x", "options": []}, "garbage"))
    assert seen == [] and _footprint(window).exists()


def test_the_real_dialog_offers_the_options_and_torna_indietro_as_escape(window, monkeypatch, tmp_path):  # noqa: F811
    prepare(window, monkeypatch, tmp_path, "HDR")
    seen = {}

    def fake_exec(box):
        seen["buttons"] = [b.text() for b in box.buttons()]
        seen["tooltips"] = [b.toolTip() for b in box.buttons()]
        seen["body"] = box.informativeText()
        seen["escape"] = box.escapeButton().text()
        pixmap = box.iconPixmap()
        seen["pixmap"] = (pixmap.width(), pixmap.height())
        return 0
    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    monkeypatch.setattr(gui.MainWindow, "_ask_extraction_question", _REAL_ASK)
    # Evidence crop from a real document image of the project.
    from PyQt6.QtGui import QImage
    docs = window._docs_dir()
    docs.mkdir(parents=True, exist_ok=True)
    image = QImage(200, 100, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    assert image.save(str(docs / "drawing.png"))
    answer = window._ask_extraction_question(_question(
        "h:1", evidence={"document": "drawing.png", "crop": [10, 10, 110, 60]}))
    assert answer is gui.QUESTION_BACK
    # CONTRACT CHANGED (review V5-3, 2026-11-11): the buttons carry short
    # names and the descriptions are in the dialog body (and the tooltips),
    # because a long description in a button was cut off unreadable.
    assert seen["buttons"][:2] == ["Opzione 1", "Opzione 2"]
    assert seen["tooltips"][:2] == ["18 mm", "23 mm"]
    assert "Opzione 1: 18 mm" in seen["body"] and "Opzione 2: 23 mm" in seen["body"]
    assert seen["escape"] == window.i18n.text("Torna indietro")
    assert seen["pixmap"] == (100, 50)                    # the crop, not the stock icon
    # Negative: no such document, the stock question icon and no failure.
    answer = window._ask_extraction_question(_question(
        "h:2", evidence={"document": "missing.png", "crop": [0, 0, 10, 10]}))
    assert answer is gui.QUESTION_BACK and seen["pixmap"] != (100, 50)


def test_dimentica_le_risposte_clears_and_saves(window, monkeypatch, tmp_path):  # noqa: F811
    prepare(window, monkeypatch, tmp_path, "HDR")
    infos = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: infos.append(a[2]))
    window._extraction_answers = {"h:1": 18.0}
    window._save_project_config(force=True)
    window._forget_extraction_answers()
    assert window._extraction_answers == {} and infos == []
    assert "extraction_answers" not in window._project_state()
    window._forget_extraction_answers()                      # nothing left: said
    assert infos and "Nessuna risposta registrata" in infos[-1]


def test_the_answers_reach_the_next_extraction_only_when_the_extractor_takes_them(window, monkeypatch, tmp_path):  # noqa: F811
    prepare(window, monkeypatch, tmp_path, "HDR")
    window._extraction_answers = {"h:1": 18.0}
    run_extraction(window, header_extraction())
    worker = window._extract_worker
    takes = gui.accepts_keyword(gui.extract_pin_header_params, "answers")
    assert (getattr(worker, "answers", None) == {"h:1": 18.0}) is takes


# ── One deferral reason (M16) ──

def _deferred(window, monkeypatch, tmp_path, **body):
    prepare(window, monkeypatch, tmp_path, "HDR")
    run_extraction(window, header_extraction(**body))
    window.group_3d.setChecked(True)
    return window


def test_a_height_conflict_is_the_reason_everywhere_and_the_plan_sizes_are_kept(window, monkeypatch, tmp_path):  # noqa: F811
    _deferred(window, monkeypatch, tmp_path, body_height_mm=None, body_conflict=CONFLICT)
    assert _footprint(window).exists()
    code, text = window._step_deferral_reason()
    assert code == "conflict" and text == CONFLICT
    log = window._log_plain_text()
    assert f"Modello STEP rinviato: {CONFLICT}" in log
    assert f"Footprint generato; modello STEP rinviato: {CONFLICT}" in log
    assert "mancano le quote" not in window._missing_body_reason()
    assert CONFLICT in window._missing_body_reason()
    # Field by field: length and width confirmed are applied, the height waits.
    assert window._body_size_declared and not window._body_height_declared
    assert (window.field_body_length.value(), window.field_body_width.value()) == (20, 10)
    assert window._current_params().body_length == 20
    assert window._current_body_params() is None
    assert window._project_state()["step_deferral"]["code"] == "conflict"


def test_missing_quotes_are_the_reason_when_there_is_no_conflict(window, monkeypatch, tmp_path):  # noqa: F811
    _deferred(window, monkeypatch, tmp_path, body_height_mm=None)
    code, text = window._step_deferral_reason()
    assert code == "missing" and "altezza" in text and "non coincidono" not in text
    log = window._log_plain_text()
    assert "Modello STEP rinviato: mancano le quote documentate del corpo" in log
    assert "Footprint generato; modello STEP rinviato: mancano le quote del corpo (altezza)" in log
    assert "mancano le quote documentate del corpo (altezza)" in window._missing_body_reason()


def test_reopening_the_project_restores_the_conflict_reason(window, monkeypatch, tmp_path):  # noqa: F811
    _deferred(window, monkeypatch, tmp_path, body_height_mm=None, body_conflict=CONFLICT)
    state = window._project_state()
    assert state["body_height_declared"] is False
    window._reset_project_runtime_state()
    assert window._last_extracted is None
    window._apply_project_state(state)
    window.group_3d.setChecked(True)
    assert window._step_deferral_reason() == ("conflict", CONFLICT)
    assert CONFLICT in window._missing_body_reason()
    assert not window._body_height_declared and window._body_size_declared


def test_a_written_step_clears_the_deferral(window, monkeypatch, tmp_path):  # noqa: F811
    _deferred(window, monkeypatch, tmp_path, body_height_mm=None, body_conflict=CONFLICT)
    assert window._step_deferral is not None
    run_extraction(window, header_extraction())
    assert window._step_deferral is None and window._step_deferral_reason() == ("", "")


def test_rigenera_asks_the_stored_height_question_and_builds_the_body(window, monkeypatch, tmp_path):  # noqa: F811
    _deferred(window, monkeypatch, tmp_path, body_height_mm=None, body_conflict=CONFLICT)
    window._extraction_questions = [_question("h:1")]
    window._refresh_regen_3d_availability()
    assert window.btn_regen_3d.isEnabled()                 # reachable: it will ask
    monkeypatch.setattr(gui.MainWindow, "_ask_extraction_question",
                        lambda self, question: 23.0)
    assert window._resolve_stored_body_height_question() is True
    assert window._extraction_answers == {"h:1": 23.0}
    assert window.field_body_height.value() == 23.0 and window._body_height_declared
    assert window._current_body_params().height_mm == 23.0
    assert window._missing_body_reason() == "" and window._extraction_questions == []
    # Negative: going back changes nothing.
    window._extraction_questions = [_question("h:2")]
    window._extraction_answers = {}
    monkeypatch.setattr(gui.MainWindow, "_ask_extraction_question",
                        lambda self, question: gui.QUESTION_BACK)
    assert window._resolve_stored_body_height_question() is False
    assert window._extraction_answers == {} and len(window._extraction_questions) == 1


def test_a_package_height_answer_rebinds_the_package(window, monkeypatch, tmp_path):  # noqa: F811
    prepare(window, monkeypatch, tmp_path, "SOIC")
    seen = []
    _answer_first(monkeypatch, seen)
    monkeypatch.delattr(datasheet_extractor, "apply_answers", raising=False)
    geometry = soic_geometry()
    geometry["body_height_mm"] = None
    params = package_extraction(geometry)
    params.body_height_mm, params.body_conflict = None, CONFLICT
    run_extraction(window, _with_questions(params, _question("h:1", values=(1.6, 1.75))))
    assert window.package_geometry.body_height_mm == 1.6
    assert window._current_body_params().height_mm == 1.6


# ── Low findings: one remedy, «Riprova», estimates bordered ──

def test_the_blocked_dialog_writes_the_remedy_once(window, monkeypatch, tmp_path):  # noqa: F811
    prepare(window, monkeypatch, tmp_path, "HDR")
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a[2]))
    window._automatic_generation_blocked("Motivo.")
    assert warnings[-1].count("ripeti l’analisi") + warnings[-1].count("ripeti l'analisi") == 1
    assert warnings[-1].startswith("Motivo.")


def test_riprova_restarts_the_extraction_and_chiudi_does_not(window, monkeypatch, tmp_path):  # noqa: F811
    prepare(window, monkeypatch, tmp_path, "HDR")
    started = []
    monkeypatch.setattr(window, "_extract_with_ai", lambda: started.append(1))
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda *a, **k: QMessageBox.StandardButton.Retry)
    window._on_extract_error("boom")
    for _ in range(5):
        QApplication.processEvents()
    assert started == [1]
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda *a, **k: QMessageBox.StandardButton.Close)
    window._on_extract_error("boom")
    for _ in range(5):
        QApplication.processEvents()
    assert started == [1]


def test_estimated_values_border_their_field_with_the_reason(window, monkeypatch, tmp_path):  # noqa: F811
    prepare(window, monkeypatch, tmp_path, "HDR")
    run_extraction(window, header_extraction(value_sources={"pad_diameter_mm": "estimated",
                                                            "drill_mm": "read"}))
    pad, drill = window.spin_single_tht_pad, window.spin_single_tht_drill
    assert pad.property("klc") == "warning" and pad.lineEdit().property("klc") == "warning"
    assert "Valore stimato" in pad.toolTip()
    assert drill.property("klc") in ("", None)
    # A later analysis that read the value clears the border.
    run_extraction(window, header_extraction(value_sources={"pad_diameter_mm": "read"}))
    assert pad.property("klc") == "" and "Valore stimato" not in pad.toolTip()
    # Per-pad provenance of a package goes to the log.
    geometry = soic_geometry()
    geometry["pins"][0]["source"] = "measured_pixels"
    run_extraction(window, package_extraction(geometry))
    assert "misurata sui pixel del disegno, da verificare: 1." in window._log_plain_text()


# ── Partial re-read wiring (M16) ──

class _FakeReread(gui.QThread):
    """Stands for workers.RereadWorker: records its arguments, never runs a model."""
    done = gui.pyqtSignal(object)
    error = gui.pyqtSignal(str)
    progress = gui.pyqtSignal(object)
    phase = gui.pyqtSignal(str, str, int, int)
    created = []

    def __init__(self, stored, doc_paths, document_roles, *, field, model=None, pad_model=None,
                 component_family=None, system_prompt=None, extra_instructions=None,
                 part_number=None, prompts=None):
        # `prompts`: workers.RereadWorker takes the profile's prompt texts
        # since step P2 (2026-11-11); a subclass that forwards **kwargs (the
        # answering fake of test_review_round3_gui) received it and failed.
        super().__init__()
        self.stored, self.field, self.model = stored, field, model
        self.prompts = prompts
        self.component_family = component_family
        _FakeReread.created.append(self)

    def start(self, *_args):  # noqa: D401 - QThread override in the fake
        pass


def _single_part(window, monkeypatch, tmp_path):
    """A single THT project with an analysed main answer saved in the result."""
    from kicadgen.gui import workers
    prepare(window, monkeypatch, tmp_path, "RELAY")
    monkeypatch.setattr(workers, "RereadWorker", _FakeReread, raising=False)
    _FakeReread.created = []
    monkeypatch.setattr(window, "_ensure_api_key", lambda: True)
    combo = window.combo_component_type
    combo.setCurrentIndex(combo.findData("single_tht"))
    params = header_extraction("RELAY", cols=2)
    params.raw_main = {"name": "RELAY", "_candidates": {}}
    run_extraction(window, params)
    return params


def test_the_reread_entries_follow_the_worker_class_and_the_saved_answer(window, monkeypatch, tmp_path):  # noqa: F811
    from kicadgen.gui import workers
    prepare(window, monkeypatch, tmp_path, "RELAY")
    monkeypatch.delattr(workers, "RereadWorker", raising=False)
    window._refresh_reread_actions()
    assert not window.action_reread_height.isVisible() and not window.action_reread_pads.isVisible()
    monkeypatch.setattr(workers, "RereadWorker", _FakeReread, raising=False)
    window._refresh_reread_actions()
    assert window.action_reread_height.isVisible()
    # No saved main answer: the entry is off with the reason.
    assert not window.action_reread_height.isEnabled()
    assert "Nessuna risposta principale" in window.action_reread_height.toolTip() \
        or window._reread_block_reason()


def test_a_reread_starts_on_the_saved_answer_and_its_result_is_applied(window, monkeypatch, tmp_path):  # noqa: F811
    _single_part(window, monkeypatch, tmp_path)
    if not window._is_single_component_project():
        import pytest
        pytest.skip("the harness project is not a single part here")
    assert window._main_answer_for_reread() == {"name": "RELAY", "_candidates": {}}
    assert window._project_state()["main_answer"] == {"name": "RELAY", "_candidates": {}}
    window._refresh_reread_actions()
    assert window.action_reread_height.isEnabled()
    window._start_reread("body_height_mm")
    assert len(_FakeReread.created) == 1
    worker = _FakeReread.created[0]
    assert worker.field == "body_height_mm" and worker.stored == {"name": "RELAY", "_candidates": {}}
    assert "Rilettura parziale" in window._log_plain_text()
    # The result rejoins the extraction path: the height it brings is applied.
    result = header_extraction("RELAY", cols=2, body_height_mm=7.5)
    worker.done.emit(result)
    worker.finished.emit()
    assert window.field_body_height.value() == 7.5
    # The saved answer survives a result without one (a further re-read).
    assert window._main_answer_for_reread() == {"name": "RELAY", "_candidates": {}}


def test_a_reread_error_reaches_the_extraction_error_path(window, monkeypatch, tmp_path):  # noqa: F811
    _single_part(window, monkeypatch, tmp_path)
    if not window._is_single_component_project():
        import pytest
        pytest.skip("the harness project is not a single part here")
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: errors.append(a[2]))
    window._start_reread("pad_layout")
    worker = _FakeReread.created[-1]
    worker.error.emit("rilettura fallita")
    worker.finished.emit()
    assert errors and "rilettura fallita" in errors[-1]
    assert not window._background_work_running()
