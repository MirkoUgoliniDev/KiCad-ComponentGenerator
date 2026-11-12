"""@brief Round 7, GUI part (2026-11-11): the fixes of the independent review V5.

- V5-1: the type block in a project's notes is «the program's text» when its
  digest equals the one the program stored with the project when it wrote
  it (`generated_block_sha256`), not when it matches a text the profile
  database holds NOW: a project saved before the user changed the type's
  instructions in the profile is refreshed, a hand edit is kept. A project
  saved before the digest existed keeps the older rule and gets the digest
  written once on open.
- V5-2: the prompt notice names each (field, digest) once per session,
  whatever the type switch; a second notice reuses the open box; only the
  prompts the active family sends are named.
- V5-3: an extraction question shows the full option descriptions in the
  dialog body, word-wrapped, and short «Opzione N» buttons.
- V5-5: the open notice follows a language change; it waits while another
  modal window is up.

Every rule has a positive and a negative case that differ for a structural
reason (user's rule, .claude/CLAUDE.md «Correzioni»). Offline: no AI call,
no write outside the test folders.
"""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication, QDialog, QLabel, QMessageBox, QPushButton

from kicadgen.gui import main_window as gui
from kicadgen.gui.project_state import read_project_config
from kicadgen.services import component_types
from kicadgen.services.component_types import text_digest

#: Notes text the user writes below the generated block.
USER_NOTES = "Note mie: relè da 5 V."


# ---------------------------------------------------------------------------
# V5-1: provenance of the generated block (unit level)
# ---------------------------------------------------------------------------

def _type(text, key="single_tht"):
    """A component type whose current instruction (profile text) is `text`."""
    return component_types.ComponentType(key, "x", text)


def test_a_block_the_program_wrote_is_refreshed_even_when_the_database_forgot_its_text():
    """The profile's instruction changed after the save: the stored digest still names the block."""
    written = component_types.apply_block(USER_NOTES, _type("ISTRUZIONI v1"))
    digest = component_types.block_digest(written)
    notes, outcome = component_types.refresh_block(written, _type("ISTRUZIONI v2"),
                                                   ["ISTRUZIONI v2"], written_digest=digest)
    assert outcome == component_types.BLOCK_REFRESHED
    assert component_types.block_text(notes) == "ISTRUZIONI v2" and notes.endswith(USER_NOTES)
    # Without the digest (a project of an older version) the same notes are
    # judged by the database texts, which no longer hold v1: kept.
    notes, outcome = component_types.refresh_block(written, _type("ISTRUZIONI v2"), ["ISTRUZIONI v2"])
    assert outcome == component_types.BLOCK_EDITED and notes == written


def test_a_hand_edit_is_kept_even_when_it_equals_a_text_the_program_knows():
    """Negative twin: the digest decides, not a coincidence with a built-in text.

    The user typed a former built-in instruction of another type into the
    block: by text it would count as the program's, by provenance it is not.
    """
    program = component_types.apply_block(USER_NOTES, _type("ISTRUZIONI v1"))
    digest = component_types.block_digest(program)
    smd = component_types.get("single_smd")
    edited = component_types.apply_block(USER_NOTES, smd)
    notes, outcome = component_types.refresh_block(edited, _type("ISTRUZIONI v2"), [],
                                                   written_digest=digest)
    assert outcome == component_types.BLOCK_EDITED and notes == edited


def test_the_digest_ignores_line_ends_and_the_block_s_own_line_breaks():
    """CRLF from a hand-edited file is the same text; a changed word is not."""
    block = component_types.apply_block(USER_NOTES, _type("riga 1\nriga 2"))
    assert (component_types.block_digest(block.replace("\n", "\r\n"))
            == component_types.block_digest(block) == text_digest("riga 1\nriga 2"))
    assert component_types.block_digest(block.replace("riga 2", "riga 3")) != \
        component_types.block_digest(block)
    assert component_types.block_digest(USER_NOTES) is None


# ---------------------------------------------------------------------------
# V5-1: through the real window
# ---------------------------------------------------------------------------

def _new(window, monkeypatch, name):
    """Create project `name` through the real «Nuovo progetto» flow."""
    from kicadgen.gui.project_dialogs import ProjectNameDialog

    def accept(self):
        self.field.setText(name)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(ProjectNameDialog, "exec", accept)
    window._new_project()


def _reopen(window, monkeypatch, name):
    """Open another project, then `name` again; return its notes."""
    from kicadgen.gui.project_dialogs import OpenProjectDialog
    _new(window, monkeypatch, name + "Altro")
    monkeypatch.setattr(OpenProjectDialog, "selected_name", lambda self: name)
    monkeypatch.setattr(OpenProjectDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    window._open_project()
    return window.field_component_prompt.toPlainText()


def _select(window, key):
    """Choose the component type `key` in the combo, as the user does."""
    window.combo_component_type.setCurrentIndex(window.combo_component_type.findData(key))


def _set_instructions(window, key, text):
    """Change a type's instructions in the profile database (as «Profili AI» does)."""
    row = window.profile_store.get_type(key)
    window.profile_store.save_type(key, label=row["label"], family=row["family"],
                                   instructions=text, active=True, overrides=None)


_EDITED_LOG = "conserva le istruzioni della tipologia modificate"


@pytest.fixture
def quiet_config(monkeypatch):
    """Never write the user's config.json from these tests."""
    monkeypatch.setattr(gui, "save_config", lambda config: None)


def test_profile_changed_after_saving_refreshes_the_program_s_block(window, monkeypatch, quiet_config):
    _set_instructions(window, "single_tht", "ISTRUZIONI DEL PROFILO v1")
    _new(window, monkeypatch, "Blocco")
    _select(window, "single_tht")
    window.field_component_prompt.setPlainText(
        window.field_component_prompt.toPlainText() + "\n\n" + USER_NOTES)
    assert window._save_project_config(force=True)
    saved = read_project_config(window._project_dir())
    assert saved["generated_block_sha256"] == text_digest("ISTRUZIONI DEL PROFILO v1")
    _set_instructions(window, "single_tht", "ISTRUZIONI DEL PROFILO v2")
    notes = _reopen(window, monkeypatch, "Blocco")
    assert component_types.block_text(notes) == "ISTRUZIONI DEL PROFILO v2"
    assert notes.endswith(USER_NOTES)
    assert _EDITED_LOG not in window._log_plain_text()


def test_a_user_edited_block_is_kept_after_the_profile_changed(window, monkeypatch, quiet_config):
    """Negative twin: same profile change, but the user rewrote the block."""
    _set_instructions(window, "single_tht", "ISTRUZIONI DEL PROFILO v1")
    _new(window, monkeypatch, "Blocco")
    _select(window, "single_tht")
    edited = component_types.apply_block(
        USER_NOTES, _type("ISTRUZIONI DEL PROFILO v1\n- I miei relè hanno fori da 1.3."))
    window.field_component_prompt.setPlainText(edited)
    assert window._save_project_config(force=True)
    _set_instructions(window, "single_tht", "ISTRUZIONI DEL PROFILO v2")
    assert _reopen(window, monkeypatch, "Blocco") == edited
    assert window._log_plain_text().count(_EDITED_LOG) == 1


def test_an_old_project_without_the_digest_keeps_today_s_rule_and_gets_it(window, monkeypatch,
                                                                           quiet_config):
    """A project saved before the digest: judged by the texts the program can have written.

    The current database text counts as the program's (refreshed, here
    already current), and the digest is then written into the file, so a
    LATER profile change no longer turns this block into a «hand edit».
    """
    _new(window, monkeypatch, "Vecchio")
    _select(window, "single_tht")
    window._generated_block_digest = None              # saved by an older version
    assert window._save_project_config(force=True)
    assert "generated_block_sha256" not in read_project_config(window._project_dir())
    current = window.profile_store.get_type("single_tht")["instructions"]
    notes = _reopen(window, monkeypatch, "Vecchio")
    assert component_types.block_text(notes) == current
    assert read_project_config(window._project_dir())["generated_block_sha256"] == text_digest(
        current.strip())
    # Negative: an old project whose block matches no text the program wrote
    # is kept (today's rule), and gets no digest that would adopt it.
    _new(window, monkeypatch, "VecchioModificato")
    _select(window, "single_tht")
    edited = component_types.apply_block(USER_NOTES, _type("Istruzioni scritte da me."))
    window.field_component_prompt.setPlainText(edited)
    window._generated_block_digest = None
    assert window._save_project_config(force=True)
    assert _reopen(window, monkeypatch, "VecchioModificato") == edited
    assert "generated_block_sha256" not in read_project_config(window._project_dir())


def test_a_type_change_refreshes_the_other_type_s_program_block(window, monkeypatch, quiet_config):
    """The project's type is single THT, its notes hold the SMD block the program wrote."""
    _set_instructions(window, "single_smd", "ISTRUZIONI SMD DEL PROFILO")
    _new(window, monkeypatch, "Cambio")
    _select(window, "single_smd")
    smd_notes = window.field_component_prompt.toPlainText()
    combo = window.combo_component_type
    blocked = combo.blockSignals(True)                 # the type changes, the block stays
    combo.setCurrentIndex(combo.findData("single_tht"))
    combo.blockSignals(blocked)
    assert window.field_component_prompt.toPlainText() == smd_notes
    assert window._save_project_config(force=True)
    _set_instructions(window, "single_smd", "ISTRUZIONI SMD CAMBIATE")   # the DB forgets the text
    notes = _reopen(window, monkeypatch, "Cambio")
    assert component_types.block_text(notes) == window.profile_store.get_type("single_tht")[
        "instructions"].strip()
    assert _EDITED_LOG not in window._log_plain_text()


# ---------------------------------------------------------------------------
# V5-2: one notice per text per session, one box, only the prompts sent
# ---------------------------------------------------------------------------

def _notice_boxes(window):
    """The prompt notice boxes open on the window."""
    title = window.i18n.text("Prompt diversi dal predefinito")
    return [b for b in window.findChildren(QMessageBox) if b.isVisible() and b.windowTitle() == title]


def _close_notices(window):
    """Close every open notice (they delete themselves)."""
    for box in _notice_boxes(window):
        box.close()


def test_three_type_switches_show_one_notice(window, monkeypatch, quiet_config):
    window.profile_store.save_defaults({"marking_prompt": "my marking"})
    monkeypatch.setattr(window, "isVisible", lambda: True)
    try:
        window._check_prompt_notice()
        for key in ("single_tht", "aggregate_tht", "single_smd"):
            _select(window, key)
            QApplication.processEvents()
        assert len(_notice_boxes(window)) == 1
        heading = "Il profilo AI attivo invia prompt diversi dal predefinito"
        assert window._log_plain_text().count(heading) == 1
    finally:
        _close_notices(window)


def test_a_new_digest_is_named_again_in_the_same_box(window, monkeypatch, quiet_config):
    """Negative twin: another text of the same field is a new difference, named in the open box."""
    window.profile_store.save_defaults({"marking_prompt": "my marking"})
    monkeypatch.setattr(window, "isVisible", lambda: True)
    try:
        window._check_prompt_notice()
        [box] = _notice_boxes(window)
        window.profile_store.save_defaults({"pin_type_prompt": "my pin types"})
        _select(window, "single_tht")
        QApplication.processEvents()
        assert _notice_boxes(window) == [box], "the open box is reused, not stacked"
        assert window.i18n.text("Simbolo: tipi elettrici dei pin") in box.text()
        assert window.i18n.text("Foto: lettura delle marcature") in box.text()
    finally:
        _close_notices(window)


def test_each_family_is_told_only_about_the_datasheet_prompt_it_sends(window, monkeypatch, quiet_config):
    """Custom board and single-part prompts: each family hears about its own only."""
    window.profile_store.save_defaults({"system_prompt": "my board prompt",
                                        "system_prompt_single": "my single prompt",
                                        "pad_positions_prompt": "my pad prompt"})
    monkeypatch.setattr(window, "isVisible", lambda: True)
    shown = []
    monkeypatch.setattr(window, "_show_prompt_notice", lambda entries: shown.append(
        {row["field"] for row in entries}))
    _select(window, "aggregate_tht")
    QApplication.processEvents()
    window._check_prompt_notice()
    named_board = set().union(*shown)
    assert "system_prompt" in named_board
    assert not named_board & {"system_prompt_single", "pad_positions_prompt"}
    shown.clear()
    _select(window, "single_tht")
    QApplication.processEvents()
    named_single = set().union(*shown)
    # The single part now hears about ITS prompts, never again about the board's.
    assert {"system_prompt_single", "pad_positions_prompt"} <= named_single
    assert "system_prompt" not in named_single


# ---------------------------------------------------------------------------
# V5-5: language change, other modal windows
# ---------------------------------------------------------------------------

def test_the_open_notice_follows_a_language_change(window, monkeypatch, quiet_config):
    window.profile_store.save_defaults({"marking_prompt": "my marking"})
    monkeypatch.setattr(window, "isVisible", lambda: True)
    try:
        window._check_prompt_notice()
        [box] = _notice_boxes(window)
        assert "Apri la gestione dei profili AI" in [b.text() for b in box.buttons()]
        window._on_language_changed(window.language_combo.findData("en"))
        assert "Open the AI profile manager" in [b.text() for b in box.buttons()]
        assert box.windowTitle() == "Prompts that differ from the default"
        assert "Il profilo AI attivo" not in box.text()
    finally:
        window._on_language_changed(window.language_combo.findData("it"))
        _close_notices(window)


def test_the_notice_waits_while_another_modal_window_is_up(window, monkeypatch, quiet_config):
    window.profile_store.save_defaults({"marking_prompt": "my marking"})
    monkeypatch.setattr(window, "isVisible", lambda: True)
    shown = []
    monkeypatch.setattr(window, "_show_prompt_notice", lambda entries: shown.append(entries))
    retries = []
    monkeypatch.setattr(gui.QTimer, "singleShot", lambda ms, slot: retries.append(slot))
    modal = QDialog(window)
    monkeypatch.setattr(gui.QApplication, "activeModalWidget", staticmethod(lambda: modal))
    try:
        window._check_prompt_notice()
        window._check_prompt_notice()
        assert shown == [] and len(retries) == 1, "one retry pending, nothing consumed"
        assert not getattr(window, "_prompt_notice_seen", set())
        # Negative twin: once the modal window has closed, the retry shows it.
        monkeypatch.setattr(gui.QApplication, "activeModalWidget", staticmethod(lambda: None))
        retries[0]()
        assert len(shown) == 1
    finally:
        modal.deleteLater()


# ---------------------------------------------------------------------------
# V5-3: the pad_layout question of 17:42 is readable
# ---------------------------------------------------------------------------

#: The two options of the real question of 2026-11-11 17:42 (relay, pad 4),
#: 205 and 380 characters: only the coordinates of pad 4 tell them apart.
LABEL_MAIN = ("coordinate della risposta principale, lettura dedicata (i fori disegnati in pinout.jpeg "
              "coincidono solo deformando il disegno): 1@(3.40,0.00), 2@(15.60,0.00), "
              "3@(15.60,12.00), 4@(3.40,6.00), 5@(3.40,12.00)")
LABEL_HOLES = ("disposizione confermata dai fori disegnati -- pad 4 sulla quota stampata che la lettura "
               "dedicata non ha attribuito a nessun pad («linea_riferimento_x»): i fori disegnati in "
               "pinout.jpeg coincidono (scarto massimo stimato 0.09 mm, tolleranza 0.15 mm); scala "
               "verificata dal contorno del corpo disegnato -- 1@(3.40,0.00), 2@(15.60,0.00), "
               "3@(15.60,12.00), 4@(1.40,6.00), 5@(3.40,12.00)")


def _question_1742():
    """The pad_layout question of the 17:42 case, as the extractor stored it."""
    return {"key": "pad_layout:holes:46b50b1a|main+dedicated:2c5c6a5f", "field": "pad_layout",
            "text": "Posizioni dei pad: Posizioni dei pad NON confermate. Quale disposizione "
                    "corrisponde al disegno?",
            "options": [{"label": LABEL_MAIN, "value": "main+dedicated:2c5c6a5f"},
                        {"label": LABEL_HOLES, "value": "holes:46b50b1a"}],
            "evidence": None}


def test_the_17_42_question_shows_both_descriptions_and_fits_the_screen(window, monkeypatch):
    seen = {}

    def run_modal(box):
        box.show()
        QApplication.processEvents()
        screen = box.screen().availableGeometry().width()
        labels = [label for label in box.findChildren(QLabel) if label.isVisible()]
        seen["readable"] = [label.text() for label in labels if label.wordWrap()]
        seen["selectable"] = all(
            label.textInteractionFlags() & gui.Qt.TextInteractionFlag.TextSelectableByMouse
            for label in labels if LABEL_HOLES in label.text())
        buttons = box.findChildren(QPushButton)
        seen["fits"] = box.width() <= screen and all(b.width() <= screen for b in buttons)
        seen["texts"] = [b.text() for b in buttons]
        seen["tips"] = [b.toolTip() for b in buttons]
        # The keyboard/mouse choice of the second option.
        next(b for b in buttons if b.text() == "Opzione 2").click()
        QApplication.processEvents()
    monkeypatch.setattr(window, "_run_modal", run_modal)
    answer = window._ask_extraction_question(_question_1742())
    assert answer == "holes:46b50b1a"
    body = "\n".join(seen["readable"])
    assert LABEL_MAIN in body and LABEL_HOLES in body, "both full descriptions, word-wrapped"
    assert seen["selectable"]
    assert seen["fits"]
    assert sorted(seen["texts"]) == sorted(["Opzione 1", "Opzione 2", "Torna indietro"])
    assert LABEL_HOLES in seen["tips"] and LABEL_MAIN in seen["tips"]


def test_torna_indietro_stays_the_escape_of_the_17_42_question(window, monkeypatch):
    """Negative twin: pressing Esc chooses nothing, whatever the option texts."""

    def run_modal(box):
        assert box.escapeButton().text() == window.i18n.text("Torna indietro")
        assert box.defaultButton() is box.escapeButton()
        box.escapeButton().click()
    monkeypatch.setattr(window, "_run_modal", run_modal)
    assert window._ask_extraction_question(_question_1742()) is gui.QUESTION_BACK
