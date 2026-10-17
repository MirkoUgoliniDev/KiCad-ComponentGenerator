"""@brief Metadata fields bordered by their KLC findings (user, 2026-10-17).

«If it is empty, why not highlight it instead of only writing it below?»:
a field with an error is bordered red, with a warning yellow, the reason in
its tooltip; a field without findings carries no mark.
"""
import pytest

from kicadgen.services import klc_checks


def _codes(findings):
    return {finding.code for finding in findings}


METADATA_CASES = [
    {"reference": "", "description": "", "keywords": "", "datasheet": "", "fp_filters": ""},
    {"reference": "U1", "description": "a\nb", "keywords": "a\nb", "datasheet": "x\ny",
     "fp_filters": "a/b"},
    {"reference": "U", "description": "d", "keywords": "k", "datasheet": "a b", "fp_filters": "F*"},
    {"reference": "U", "description": "d", "keywords": "k", "datasheet": "~", "fp_filters": "F*"},
]


def test_every_metadata_code_names_a_field():
    """@brief Structural: no finding of `check_metadata` is left without its field."""
    seen = set()
    for metadata in METADATA_CASES:
        seen |= _codes(klc_checks.check_metadata(metadata, "Fp"))
    seen.discard("metadata_type")
    assert seen, "precondition: the cases produce findings"
    assert seen <= set(klc_checks.METADATA_FIELD_OF_CODE)
    assert set(klc_checks.METADATA_FIELD_OF_CODE.values()) == {
        "reference", "description", "keywords", "datasheet", "fp_filters"}


def test_an_error_outranks_a_warning_and_other_findings_are_left_out():
    F = klc_checks.Finding
    grouped = klc_checks.field_findings([
        F("datasheet_blanks", "warning", "spaces"), F("datasheet_empty", "error", "empty"),
        F("fp_filters_empty", "warning", "no filter"), F("pin_grid", "error", "grid")])
    assert grouped == {"datasheet": ("error", ["spaces", "empty"]),
                       "fp_filters": ("warning", ["no filter"])}


# ── Window ──────────────────────────────────────────────────────────

def test_an_empty_datasheet_is_bordered_red_with_its_reason(window):
    field = window.field_datasheet
    field.setText("")
    window._refresh_klc_findings()
    assert field.property("klc") == "error"
    assert "Datasheet" in field.toolTip().split("\n\n", 1)[1]
    assert field.toolTip().startswith(window.i18n.text(
        "URL o percorso del documento, senza spazi: proprietà Datasheet di simbolo e footprint."))
    assert field.placeholderText() == "URL, percorso del file, oppure ~ se non c'è"


def test_filling_the_field_clears_the_mark_and_restores_the_tooltip(window):
    """@brief Negative: «~» is a valid datasheet; the field goes back to plain."""
    field = window.field_datasheet
    field.setText("")
    window._refresh_klc_findings()
    field.setText("~")
    assert field.property("klc") == ""
    assert field.toolTip() == window.i18n.text(
        "URL o percorso del documento, senza spazi: proprietà Datasheet di simbolo e footprint.")
    assert window.field_meta_description.property("klc") in ("", "error")


def test_a_warning_is_yellow_and_the_border_colours_come_from_the_theme(window):
    from kicadgen.gui.theme import APP_QSS, RED, YELLOW
    window.field_datasheet.setText("C:/my docs/sheet.pdf")
    assert window.field_datasheet.property("klc") == "warning"
    assert f'QLineEdit[klc="error"] {{\n        border: 1px solid {RED};' in APP_QSS
    assert f'QLineEdit[klc="warning"] {{\n        border: 1px solid {YELLOW};' in APP_QSS
    # After the focus rule, so the colour stays while typing the fix.
    assert APP_QSS.index('QLineEdit[klc="error"]') > APP_QSS.index("QLineEdit:focus")


def test_the_marks_follow_a_language_switch(window, monkeypatch):
    from kicadgen.gui import main_window as gui
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    window.field_datasheet.setText("")
    window._refresh_klc_findings()
    combo = window.language_combo
    combo.setCurrentIndex(combo.findData("en"))
    field = window.field_datasheet
    assert field.property("klc") == "error"
    assert field.placeholderText() == "URL, file path, or ~ if there is none"
    assert field.toolTip().startswith(window.i18n.text(
        "URL o percorso del documento, senza spazi: proprietà Datasheet di simbolo e footprint."))
    assert window.i18n.language == "en" and "URL" in field.toolTip()
    combo.setCurrentIndex(combo.findData("it"))
    assert field.placeholderText() == "URL, percorso del file, oppure ~ se non c'è"
