"""@brief Language-switch follow-ups of the 2026-10-16 i18n review (R2I2-R2I5, «Account AI...»).

Each fix has a positive case (the window or widget started in a language
other than Italian follows a switch) and a negative case that the same rule
must leave alone (no switch, data texts, a state that changes the text).
"""

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QEvent  # noqa: E402
from PyQt6.QtWidgets import (QApplication, QTableWidget, QTableWidgetItem,  # noqa: E402
                             QTabWidget, QWidget)

from kicadgen.services.analysis_profiles import ProfileStore  # noqa: E402
from kicadgen.services.i18n import DEFAULT_TRANSLATIONS, I18n  # noqa: E402

from qt_fixtures import qapp  # noqa: E402,F401

ACCOUNT_TOOLTIP = ("Chiavi OpenRouter e DeepSeek, credito e limiti: incolla, verifica e salva "
                   "le chiavi nel Gestore credenziali di Windows.")
TYPE_REASON = ("Scegli prima la tipologia del componente: decide come vengono "
               "analizzati documenti e foto.")
HEADER = ("Pin", "Funzione", "Tipo elettrico")


def _rendered(language, source):
    return DEFAULT_TRANSLATIONS["ui:" + source][language]


def _i18n(tmp_path, language):
    return I18n(ProfileStore(tmp_path / "profiles.sqlite3"), language)


def _settle():
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()


# ── R2I2: tab texts and table headers, on bare widgets ───────────────


def _table_and_tabs(i18n, parent):
    table = QTableWidget(0, 3, parent)
    table.setHorizontalHeaderLabels([i18n.text(source) for source in HEADER[1:]] + ["R1"])
    tabs = QTabWidget(parent)
    for source in HEADER[1:]:
        tabs.addTab(QWidget(), i18n.text(source))
    tabs.addTab(QWidget(), "R1")
    return table, tabs


def _headers(table):
    return [table.horizontalHeaderItem(i).text() for i in range(table.columnCount())]


def _tab_texts(tabs):
    return [tabs.tabText(i) for i in range(tabs.count())]


def test_headers_and_tabs_written_in_english_follow_a_switch(qapp, tmp_path):  # noqa: F811
    """@brief Positive: texts written in English at start-up are read back to their source."""
    i18n = _i18n(tmp_path, "en")
    parent = QWidget()
    table, tabs = _table_and_tabs(i18n, parent)
    for language in ("fr", "de", "it"):
        i18n.set_language(language)
        i18n.retranslate_widgets(parent)
        expected = [_rendered(language, s) for s in HEADER[1:]] + ["R1"]
        assert _headers(table) == expected, language
        assert _tab_texts(tabs) == expected, language


def test_a_header_rewritten_by_its_owner_is_not_overwritten(qapp, tmp_path):  # noqa: F811
    """@brief Positive: the owner writes the new language first (symbol_tab), the pass keeps it.

    The pass used to put back the text captured at its first run.
    """
    i18n = _i18n(tmp_path, "en")
    parent = QWidget()
    table, _tabs = _table_and_tabs(i18n, parent)
    i18n.set_language("fr")
    i18n.retranslate_widgets(parent)
    i18n.set_language("de")
    table.setHorizontalHeaderLabels([i18n.text(s) for s in HEADER[1:]] + ["R1"])
    i18n.retranslate_widgets(parent)
    assert _headers(table) == [_rendered("de", s) for s in HEADER[1:]] + ["R1"]


def test_italian_headers_and_data_are_unchanged_without_a_switch(qapp, tmp_path):  # noqa: F811
    """@brief Negative: in Italian with no switch the pass changes nothing, data included."""
    i18n = _i18n(tmp_path, "it")
    parent = QWidget()
    table, tabs = _table_and_tabs(i18n, parent)
    before = (_headers(table), _tab_texts(tabs))
    i18n.retranslate_widgets(parent)
    i18n.retranslate_widgets(parent)
    assert (_headers(table), _tab_texts(tabs)) == before
    assert before[0] == list(HEADER[1:]) + ["R1"]


# ── On the real window ──────────────────────────────────────────────


def _started(qapp, monkeypatch, tmp_path, language):  # noqa: F811
    """MainWindow started in `language`, with the real analysis gate and no project."""
    pytest.importorskip("pyvistaqt")
    from PyQt6.QtWidgets import QMessageBox
    from kicadgen.gui import main_window as gui
    from qt_fixtures import PreviewStub
    monkeypatch.setattr(gui, "Preview3DVtk", PreviewStub)
    monkeypatch.setattr(gui, "load_config", lambda: {
        "component_type": "", "system_prompt": "", "language": language})
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    monkeypatch.setattr(gui, "DEFAULT_LIBRARY_ROOT", tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-fixture")
    window = gui.MainWindow()
    window._board_size_known = True
    window.field_name.setText("Componente_di_prova")
    _settle()
    return window


def _switch(window, language):
    window.language_combo.setCurrentIndex(window.language_combo.findData(language))
    _settle()


def _expected_summary(window):
    return (f"{window.combo_photo_detector.currentText()} · "
            f"{window.combo_model.currentText()}")


def _standard_label(language):
    from kicadgen.gui.main_window import PCB_THICKNESS_SOURCE_LABELS
    return _rendered(language, PCB_THICKNESS_SOURCE_LABELS["standard"])


def test_a_window_started_in_english_follows_a_switch_to_german(qapp, monkeypatch, tmp_path):  # noqa: F811
    """@brief Positive cases of R2I2-R2I5 and the «Account AI...» tooltip on one window."""
    from qt_fixtures import shutdown_main_window
    window = _started(qapp, monkeypatch, tmp_path, "en")
    try:
        # Start-up, English.
        assert window.action_ai_account.toolTip() == _rendered("en", ACCOUNT_TOOLTIP)
        # R2I5: the PCB thickness row shows the value in use and its source
        # from the start, as after a switch.
        assert window.spin_pcb_thickness.value() == pytest.approx(window.field_cast_pcb.value())
        assert window.spin_pcb_thickness.value() == pytest.approx(1.6)
        assert window.label_pcb_thickness_source.text() == _standard_label("en")
        assert not window.btn_pcb_thickness_reset.isEnabled()
        # R2I4: no component type: the button says why it is blocked.
        assert not window.btn_extract_ai.isEnabled()
        assert window.btn_extract_ai.toolTip() == _rendered("en", TYPE_REASON)
        summary_en = window.group_ai_tools.summary.text()
        assert summary_en == _expected_summary(window)

        _switch(window, "de")
        # R2I2: the symbol pin-table header follows the switch.
        table = window.symbol_tab.table
        assert [table.horizontalHeaderItem(i).text() for i in range(3)] == [
            _rendered("de", source) for source in HEADER]
        # R2I3: the summary quotes the texts of the new language, not the old ones.
        assert window.group_ai_tools.summary.text() == _expected_summary(window)
        assert window.group_ai_tools.summary.text() != summary_en
        # R2I4: still blocked, still the reason, now in German.
        assert window.btn_extract_ai.toolTip() == _rendered("de", TYPE_REASON)
        # R2I5: same value and provenance as at start-up, in German.
        assert window.spin_pcb_thickness.value() == pytest.approx(1.6)
        assert window.label_pcb_thickness_source.text() == _standard_label("de")
        # «Account AI...» names both providers in every language.
        assert window.action_ai_account.toolTip() == _rendered("de", ACCOUNT_TOOLTIP)

        # R2I4 negative: with a component type the button is free and its
        # tooltip is the one of the analysis, not a reason.
        combo = window.combo_component_type
        combo.setCurrentIndex(next(i for i in range(combo.count()) if combo.itemData(i)))
        _settle()
        _switch(window, "fr")
        assert window.btn_extract_ai.isEnabled()
        idle = window._extract_button_sources()[1]
        assert window.btn_extract_ai.toolTip() == _rendered("fr", idle)
    finally:
        shutdown_main_window(window, qapp)


def test_a_window_started_in_italian_is_unchanged_without_a_switch(qapp, monkeypatch, tmp_path):  # noqa: F811
    """@brief Negative: Italian start-up, no switch: the same texts the fixes produce after one."""
    from qt_fixtures import shutdown_main_window
    window = _started(qapp, monkeypatch, tmp_path, "it")
    try:
        table = window.symbol_tab.table
        assert [table.horizontalHeaderItem(i).text() for i in range(3)] == list(HEADER)
        assert window.group_ai_tools.summary.text() == _expected_summary(window)
        assert window.btn_extract_ai.toolTip() == TYPE_REASON
        assert window.label_pcb_thickness_source.text() == _standard_label("it")
        assert window.action_ai_account.toolTip() == ACCOUNT_TOOLTIP
        # A user value has its own provenance and enables «Ripristina».
        window.spin_pcb_thickness.setValue(1.2)
        assert window.label_pcb_thickness_source.text() == _rendered("it", "impostato da te")
        assert window.btn_pcb_thickness_reset.isEnabled()
    finally:
        shutdown_main_window(window, qapp)


def test_the_account_tooltip_names_both_providers_in_each_language():
    """@brief Positive: OpenRouter and DeepSeek in all four; negative: the OpenRouter-only text is gone."""
    entry = DEFAULT_TRANSLATIONS["ui:" + ACCOUNT_TOOLTIP]
    for language in ("it", "en", "fr", "de"):
        assert "OpenRouter" in entry[language] and "DeepSeek" in entry[language], language
    assert not [key for key in DEFAULT_TRANSLATIONS if key.startswith("ui:Chiave OpenRouter, credito")]
