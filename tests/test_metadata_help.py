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
    # CONTRACT CHANGED 2026-10-15: designator, description, keywords and
    # datasheet are REQUIRED fields (klc_checks.REQUIRED_FIELD_CODES): an
    # empty one is an error that blocks the installation, so every help says
    # so. Before, only the designator was an error and the other three said
    # «avviso ⚠». The footprint filters stay optional: their help must not
    # claim a block.
    rules = {key: dict((t, b) for t, b in window._metadata_help_content(key))["Regole KLC"]
             for key in ("reference", "description", "keywords", "datasheet", "fp_filters")}
    assert "⛔" in rules["reference"] and "rifiuta" in rules["reference"]
    assert "⛔" in rules["fp_filters"], \
        "fp_filters has a format error rule too (klc_checks fp_filters_format)"
    for key in ("reference", "description", "keywords", "datasheet"):
        assert "obbligatorio" in rules[key] and "blocca l’installazione" in rules[key], key
    assert "obbligatorio" not in rules["fp_filters"]
    assert "avviso ⚠" in rules["datasheet"], "blanks in a datasheet path stay advice"
    # CONTRACT CHANGED 2026-10-15 (review pass 2, M1): installation also waits
    # until description, keywords and datasheet are WRITTEN into the files,
    # and their help says so, with the one «Applica ai file» an older project
    # needs. Negative: the designator (only in the symbol, not installed) and
    # the optional filters do not claim it.
    for key in ("description", "keywords", "datasheet"):
        assert "scritto nei file con «Applica ai file»" in rules[key], key
        assert "prima della prima installazione" in rules[key], key
        # Review 2026-10-15, L-a: the sentence says the footprint is
        # regenerated, not merely «written» (an old project's first Apply
        # rewrites it with the current generator).
        assert "rigenera il footprint" in rules[key], key
    for key in ("reference", "fp_filters"):
        assert "scritto nei file" not in rules[key], key
    klc = dict(window._metadata_help_content("klc"))
    assert "scritti nei file con «Applica ai file»" in klc["Dove finisce"]
    assert "prima della prima installazione" in klc["Dove finisce"]
    assert "rigenera il footprint" in klc["Dove finisce"]


def test_the_applied_note_follows_the_language(window):
    """The appended sentence is a catalogue entry: English help, English note."""
    window._on_language_changed(window.language_combo.findData("en"))
    try:
        sections = window._metadata_help_content("keywords")
        assert any("written into the files with «Apply to files»" in body for _t, body in sections)
        assert not any("Applica ai file" in body for _t, body in sections)
    finally:
        window._on_language_changed(window.language_combo.findData("it"))


def test_each_icon_opens_the_help_of_its_own_field(window):
    """Click wiring: two icons, two titles (a late-binding lambda would open
    the same topic for every icon)."""
    window.tabs.widget(gui.MainWindow.KICAD_TAB_INDEX).setEnabled(True)
    window.group_metadata.setEnabled(True)
    titles = []

    def grab():
        titles.append(window._metadata_help_dialog.windowTitle())
        window._metadata_help_dialog.accept()
    for key in ("reference", "fp_filters"):
        QTimer.singleShot(0, grab)
        window._metadata_help_buttons[key].click()
    assert titles == ["Aiuto: Designatore di riferimento", "Aiuto: Filtri footprint"]


def test_the_help_icons_follow_the_language(window):
    button = window._metadata_help_buttons["datasheet"]
    assert "Datasheet (URL o file)" in button.toolTip()
    window._on_language_changed(window.language_combo.findData("en"))
    assert "Datasheet (URL or file)" in button.toolTip() and "Help:" in button.toolTip()
    assert "Help:" in button.accessibleName()
