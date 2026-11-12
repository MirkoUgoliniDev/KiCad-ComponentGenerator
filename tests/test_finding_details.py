"""@brief The modal window with the full text and remedy of a check row (user request, 2026-11-12).

A click on a row of the footprint's «Controlli» or of the KiCad tab's
«Controlli KiCad (KLC)» opens the same window; the empty-state rows open
nothing. Positive and negative cases for each.
"""
from PyQt6.QtWidgets import QDialog, QLabel, QListWidgetItem

from kicadgen.gui.finding_details import (FINDING_DETAILS_ROLE, build_finding_dialog,
                                          finding_details, show_finding_details)
from tests.test_photo_inventory_gui import gui  # noqa: F401 - window fixture


def _label(dialog, name):
    """Text of the child label with object name `name`, or None."""
    label = dialog.findChild(QLabel, name)
    return None if label is None else label.text()


def test_the_window_shows_severity_whole_message_and_remedy(qapp):
    """@brief Positive: nothing is cut, the remedy has its own heading."""
    message = "Pad con valori stimati o derivati dal programma, non letti dal disegno: 1, 2, 3, 4, 5. " * 3
    dialog = build_finding_dialog(None, finding_details("error", message, "Leggi il foro sul disegno."),
                                  lambda text: text)
    try:
        assert dialog.isModal()
        assert _label(dialog, "findingSeverity") == "⛔ Errore"
        assert _label(dialog, "findingMessage") == message
        assert _label(dialog, "findingRemedyTitle") == "Come correggere:"
        assert _label(dialog, "findingRemedy") == "Leggi il foro sul disegno."
        assert dialog.findChild(QLabel, "findingMessage").wordWrap()
    finally:
        dialog.deleteLater()


def test_a_finding_without_remedy_has_no_empty_remedy_heading(qapp):
    """@brief Negative: no hint, no «Come correggere:» over nothing; a warning reads as one."""
    dialog = build_finding_dialog(None, finding_details("warning", "Testo", ""), lambda text: text)
    try:
        assert _label(dialog, "findingSeverity") == "⚠ Avviso"
        assert _label(dialog, "findingRemedyTitle") is None
    finally:
        dialog.deleteLater()


def test_a_row_that_is_not_a_finding_opens_nothing(qapp, monkeypatch):
    """@brief Negative: the empty-state row carries no details and no window is built."""
    opened = []
    monkeypatch.setattr(QDialog, "exec", lambda self: opened.append(self) or 0)
    assert build_finding_dialog(None, None, lambda text: text) is None
    assert show_finding_details(None, None, lambda text: text) is False
    assert show_finding_details(None, finding_details("warning", "", "x"), lambda text: text) is False
    assert opened == []
    assert show_finding_details(None, finding_details("warning", "Testo", ""), lambda text: text) is True
    assert len(opened) == 1


def test_the_window_speaks_the_interface_language(window):  # noqa: F811
    """@brief Positive: the window's own words come from the catalogue."""
    window.i18n.set_language("en")
    try:
        dialog = build_finding_dialog(window, finding_details("error", "Message", "Remedy"),
                                      window.i18n.text)
        try:
            assert dialog.windowTitle() == "Check details"
            assert _label(dialog, "findingSeverity") == "⛔ " + window.i18n.text("Errore")
            assert _label(dialog, "findingRemedyTitle") == "How to fix it:"
        finally:
            dialog.deleteLater()
    finally:
        window.i18n.set_language("it")


def test_a_klc_row_click_opens_its_details_and_the_empty_row_does_not(window, monkeypatch):  # noqa: F811
    """@brief The KiCad tab's list behaves as the footprint's «Controlli»."""
    shown = []
    monkeypatch.setattr(gui, "show_finding_details",
                        lambda parent, details, translate: shown.append(details) or True)
    window.field_name.setText("RELE")
    window.field_reference.setText("")
    window._refresh_klc_findings()
    rows = [window.klc_list.item(i) for i in range(window.klc_list.count())]
    findings = [row for row in rows if row.data(window.FINDING_CODE_ROLE)]
    assert findings, "precondition: an empty Reference is a KLC finding"
    window.klc_list.itemClicked.emit(findings[0])
    severity, message, hint = shown[-1]
    assert severity in ("error", "warning") and message in findings[0].text()
    assert hint == findings[0].toolTip()
    empty = QListWidgetItem("Nessun problema trovato")
    window._on_klc_finding_clicked(empty)
    assert shown[-1] is None, "the empty-state row carries no details"


def test_footprint_check_rows_follow_a_language_switch(window, monkeypatch):  # noqa: F811
    """@brief Positive (review 2026-11-12, finding 3): after a switch the rows and
    the details they open are in the new language, without editing the footprint;
    negative: switching back restores the Italian text."""
    from tests.test_package_project_gui import acquire
    shown = []
    monkeypatch.setattr(gui, "show_finding_details",
                        lambda parent, details, translate: shown.append(details) or True)
    monkeypatch.setattr(window, "_save_config_safely", lambda: None)
    acquire(window)
    pin = window.package_geometry.pins_seen_from_top()[0]
    window.footprint_drawings = [{"kind": "line", "layer": "F.SilkS", "width": 0.12,
                                  "points": [(pin.x_mm - 1.0, pin.y_mm), (pin.x_mm + 1.0, pin.y_mm)]}]
    window._draw_preview()
    window._refresh_footprint_checks_now()

    def silk_row():
        lst = window.fp_checks_list
        rows = [lst.item(i) for i in range(lst.count()) if lst.item(i).data(FINDING_DETAILS_ROLE)]
        return rows[0]

    italian = silk_row().text()
    assert "Serigrafia" in italian
    combo = window.language_combo
    assert not window._fp_checks_timer.isActive()
    combo.setCurrentIndex(combo.findData("en"))
    try:
        # The switch itself schedules the re-run; firing the coalescing timer
        # stands for the 150 ms wait, no manual refresh.
        assert window._fp_checks_timer.isActive(), "the language switch must re-run the checks"
        window._fp_checks_timer.timeout.emit()
        english = silk_row()
        assert "Serigrafia" not in english.text() and english.text() != italian
        window._on_footprint_check_clicked(english)
        assert "Serigrafia" not in shown[-1][1]
    finally:
        combo.setCurrentIndex(combo.findData("it"))
    window._refresh_footprint_checks_now()
    assert silk_row().text() == italian
