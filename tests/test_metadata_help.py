"""@brief Every metadata row has a help icon that opens a modal explanation (user, 2026-10-12).

The tooltip of a field could not say what the property is for in KiCad, where
it is written and which KLC rule concerns it; an ⓘ icon at the end of each
row opens a modal with those sections, like the icon of the component folder.
"""
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QLabel, QToolButton

from tests.test_photo_inventory_gui import gui  # noqa: F401 - window fixture

ROWS = ("reference", "description", "keywords", "datasheet", "fp_filters", "klc")


def test_each_metadata_row_has_its_own_help_icon(window):
    buttons = window._metadata_help_buttons
    assert tuple(buttons) == ROWS
    fields = {"reference": window.field_reference, "description": window.field_meta_description,
              "keywords": window.field_meta_keywords, "datasheet": window.field_datasheet,
              "fp_filters": window.field_fp_filters, "klc": window.klc_list}
    for key, button in buttons.items():
        assert isinstance(button, QToolButton)
        # Same row as its field: the icon sits at the end of that line.
        assert button.parentWidget() is fields[key].parentWidget()
        assert window.METADATA_HELP_TITLES[key] in button.toolTip()


def test_the_icon_opens_a_modal_with_the_sections_of_that_field(window):
    observed = []

    def inspect():
        dialog = window._metadata_help_dialog
        text = "".join(label.text() for label in dialog.findChildren(QLabel))
        observed.append((dialog.isVisible(), dialog.isModal(), dialog.windowTitle(), text))
        dialog.accept()

    # The whole KiCad page is disabled until a project is completed, so a
    # click would be swallowed here: the icon's slot is called directly and
    # the wiring is checked apart.
    button = window._metadata_help_buttons["datasheet"]
    assert button.receivers(button.clicked) == 1
    QTimer.singleShot(0, inspect)
    window._show_metadata_help("datasheet")
    assert len(observed) == 1
    visible, modal, title, text = observed[0]
    assert visible and modal
    assert "Datasheet (URL o file)" in title
    for section in ("Che cosa inserire", "Dove finisce", "Regole KLC", "Esempi"):
        assert section in text
    assert "ki_fp_filters" not in text, "the datasheet help must not show another field's text"
    assert window._metadata_help_dialog is None


def test_every_topic_has_the_four_sections_and_matches_the_klc_severity(window):
    """Negative guard: a topic without content would open an empty modal."""
    for key in ROWS:
        sections = window._metadata_help_content(key)
        assert [title for title, _body in sections] == [
            "Che cosa inserire", "Dove finisce", "Regole KLC", "Esempi"], key
        assert all(body.strip() for _title, body in sections), key
    assert window._metadata_help_content("unknown") == []
    # The designator is the only field whose empty value is a KLC error
    # (klc_checks.metadata_findings): its help says it blocks, the others say
    # the installation goes on.
    rules = {key: dict((t, b) for t, b in window._metadata_help_content(key))["Regole KLC"]
             for key in ("reference", "description", "keywords", "datasheet")}
    assert "⛔" in rules["reference"] and "blocca" in rules["reference"]
    for key in ("description", "keywords", "datasheet"):
        assert "avviso ⚠" in rules[key], key
