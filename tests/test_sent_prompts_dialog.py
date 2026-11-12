"""@brief «Strumenti > Prompt inviati...»: the number of the analysis log back to the text (user request, 2026-11-12)."""
import sqlite3

from PyQt6.QtWidgets import QDialog

from kicadgen.gui.sent_prompts_dialog import SentPromptsDialog
from kicadgen.services import sent_prompts
from tests.test_photo_inventory_gui import gui  # noqa: F401 - window fixture


def test_the_dialog_shows_the_text_registered_under_a_number(qapp):
    """@brief Positive: title, status at the first send, date, the whole text, the digest on demand."""
    text = "Trascrivi le catene.\nRiga due."
    number = sent_prompts.register("pad_positions_prompt", text, "custom")
    dialog = SentPromptsDialog(lambda source: source, number=number)
    try:
        assert dialog.text.toPlainText() == text
        summary = dialog.summary.text()
        assert f"n. {number}" in summary and "Datasheet: posizioni dei pad" in summary
        assert "personalizzato" in summary and "primo invio" in summary
        assert "sha256" not in summary, "the digest is not shown, only in the tooltip"
        assert dialog.summary.toolTip() == "sha256 " + sent_prompts.lookup(number)["sha256"]
    finally:
        dialog.deleteLater()


def test_an_unknown_number_says_so_and_shows_no_text(qapp):
    """@brief Negative: a number nobody registered shows no text of another prompt."""
    sent_prompts.register("pad_positions_prompt", "testo", "current")
    dialog = SentPromptsDialog(lambda source: source, number=1)
    try:
        assert dialog.show_number(999) is False
        assert dialog.text.toPlainText() == ""
        assert dialog.summary.text() == "Nessun prompt registrato con questo numero."
        assert dialog.show_number(1) is True
    finally:
        dialog.deleteLater()


def test_an_unreadable_registry_is_reported_not_raised(qapp, monkeypatch):
    """@brief Negative (review 2026-11-12, finding 5): a sqlite error in the slot is shown, never raised."""
    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(sent_prompts, "lookup", locked)
    dialog = SentPromptsDialog(lambda source: source, number=1)
    try:
        assert dialog.summary.text() == "database is locked"
        assert dialog.text.toPlainText() == ""
    finally:
        dialog.deleteLater()


def test_without_a_registry_file_nothing_is_created_by_a_lookup(tmp_path):
    """@brief Negative: looking a number up never creates the registry file."""
    path = tmp_path / "none" / "sent_prompts.sqlite3"
    assert sent_prompts.lookup(1, path) is None
    assert not path.exists()


def test_the_tools_menu_opens_it(window, monkeypatch):  # noqa: F811
    """@brief The menu entry exists, is translated and opens the dialog."""
    opened = []
    monkeypatch.setattr(QDialog, "exec", lambda self: opened.append(type(self).__name__) or 0)
    assert window.action_sent_prompts.text() == "Prompt inviati..."
    window.action_sent_prompts.trigger()
    assert opened == ["SentPromptsDialog"]
