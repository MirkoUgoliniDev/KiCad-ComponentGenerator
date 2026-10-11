"""@brief La scheda «KiCad» si sceglie solo con un progetto completato (utente, 2026-09-24).

Completato = nella cartella del progetto corrente ci sono footprint E modello
STEP: cio' che serve a tutte le operazioni della scheda. Positivo e negativi
distinti per i file presenti, non per il nome del progetto.
"""
from tests.test_photo_inventory_gui import gui

KICAD = gui.MainWindow.KICAD_TAB_INDEX


def _outputs(root, name, footprint=True, step=True):
    if footprint:
        (root / name / f"{name}.pretty").mkdir(parents=True, exist_ok=True)
        (root / name / f"{name}.pretty" / f"{name}.kicad_mod").write_text("(footprint)")
    if step:
        (root / name / f"{name}.3dshapes").mkdir(parents=True, exist_ok=True)
        (root / name / f"{name}.3dshapes" / f"{name}.step").write_text("ISO-10303-21;")


def test_without_a_project_the_kicad_tab_cannot_be_chosen(window):  # noqa: F811
    window.field_name.setText("")
    window._refresh_output_actions()
    assert window.tabs.tabText(KICAD) and not window.tabs.isTabEnabled(KICAD)
    assert window.tabs.tabToolTip(KICAD)


def test_a_project_with_footprint_and_step_opens_the_tab(window, tmp_path):  # noqa: F811
    window.field_library_root.setText(str(tmp_path))
    _outputs(tmp_path, "SCHEDA")
    window.field_name.setText("SCHEDA")
    assert window.tabs.isTabEnabled(KICAD)
    assert window.tabs.tabToolTip(KICAD) == ""


def test_footprint_without_step_is_not_completed(window, tmp_path):  # noqa: F811
    """Negativo: le operazioni della scheda chiedono anche il modello STEP."""
    window.field_library_root.setText(str(tmp_path))
    _outputs(tmp_path, "SCHEDA", step=False)
    window.field_name.setText("SCHEDA")
    assert window.tabs.isTabEnabled(KICAD)
    assert not window.btn_test_kicad.isEnabledTo(window.tabs.widget(KICAD))


def test_leaving_a_completed_project_moves_off_the_kicad_tab(window, tmp_path):  # noqa: F811
    """Mentre la si guarda, la scheda si spegne e si torna a «Progetto»."""
    window.field_library_root.setText(str(tmp_path))
    _outputs(tmp_path, "PRONTO")
    window.field_name.setText("PRONTO")
    window.tabs.setCurrentIndex(KICAD)
    assert window.tabs.currentIndex() == KICAD
    window.field_name.setText("VUOTO")
    assert window.tabs.currentIndex() == 0 and not window.tabs.isTabEnabled(KICAD)


# ── «Esportazioni» (user, 2026-10-11): the history table in a tab of its own ──

def test_the_export_history_lives_in_its_own_always_enabled_tab(window, monkeypatch):  # noqa: F811
    """@brief Positive: the table is in «Esportazioni», enabled without a project
    and showing every record. Negative: the KiCad tab no longer holds it."""
    exports = gui.TAB_EXPORTS
    assert exports == window.tabs.count() - 1
    assert window.tabs.tabText(exports) == window.i18n.tr("tab.exports.name")
    assert window.tabs.widget(exports).isAncestorOf(window.kicad_export_table)
    assert not window.tabs.widget(KICAD).isAncestorOf(window.kicad_export_table)
    window.field_name.setText("")
    window._refresh_output_actions()
    assert window.tabs.isTabEnabled(exports) and not window.tabs.isTabEnabled(KICAD)
    records = [{"component": f"C{i}", "kind": "project", "destination": f"d{i}",
                "timestamp": "2026-10-11T10:00:00"} for i in range(15)]
    monkeypatch.setattr(gui, "load_kicad_export_history", lambda: records)
    window._refresh_kicad_export_history()
    assert window.kicad_export_table.rowCount() == 15
