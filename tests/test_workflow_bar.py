"""@brief Workflow steps above the tabs and status bar (PIANO-FIX item 4, 2026-09-26)."""
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QSignalSpy, QTest

from kicadgen.gui.workflow_bar import WORKFLOW_STEPS, WorkflowBar
from tests.test_photo_inventory_gui import gui


def test_the_bar_ticks_done_steps_and_highlights_the_current_one(window):  # noqa: F811
    bar = WorkflowBar()
    bar.set_state({"documents"}, "analysis", {"review": "Solo per le schede con componenti."})
    assert "✓" in bar.labels["documents"].text()
    assert "●" in bar.labels["analysis"].text()
    assert bar.labels["review"].font().strikeOut()
    assert bar.labels["analysis"].font().bold() and not bar.labels["documents"].font().bold()
    assert bar.labels["review"].toolTip() == "Solo per le schede con componenti."
    assert "○" in bar.labels["kicad"].text()
    spy = QSignalSpy(bar.step_clicked)
    QTest.mouseClick(bar.labels["model3d"], Qt.MouseButton.LeftButton, pos=QPoint(2, 2))
    assert spy[0][0] == "model3d"
    assert [key for key, _ in WORKFLOW_STEPS] == ["documents", "analysis", "review", "model3d", "kicad"]


def test_without_a_project_no_step_is_current(window):  # noqa: F811
    """@brief Negativo: senza progetto nessun passo viene proposto."""
    window.field_name.setText("")
    window._refresh_workflow_state()
    done, current, _ = window.workflow_bar.state()
    assert done == set() and current is None
    assert window.status_project.text() == "Nessun progetto aperto"


def test_a_generated_board_points_to_kicad(window, tmp_path):  # noqa: F811
    window.field_library_root.setText(str(tmp_path))
    window.field_name.setText("SCHEDA")
    combo = window.combo_component_type
    combo.setCurrentIndex(combo.findData("aggregate_tht"))
    folder = window._project_dir()
    (folder / "SCHEDA.pretty").mkdir(parents=True)
    (folder / "SCHEDA.pretty" / "SCHEDA.kicad_mod").write_text("(footprint)")
    (folder / "SCHEDA.3dshapes").mkdir()
    (folder / "SCHEDA.3dshapes" / "SCHEDA.step").write_text("step")
    window._refresh_workflow_state()
    done, current, skipped = window.workflow_bar.state()
    assert {"analysis", "model3d"} <= done
    assert "review" not in skipped, "una scheda con componenti ha la revisione"
    assert current in ("documents", "review")
    assert window.status_project.text() == "Progetto: SCHEDA"


def test_a_single_component_skips_the_body_review(window, tmp_path):  # noqa: F811
    """@brief Negativo: un componente singolo non ha corpi da rivedere."""
    window.field_library_root.setText(str(tmp_path))
    window.field_name.setText("RELE")
    combo = window.combo_component_type
    combo.setCurrentIndex(combo.findData("single_tht"))
    window._refresh_workflow_state()
    _done, current, skipped = window.workflow_bar.state()
    assert "review" in skipped and current != "review"


def test_the_status_bar_says_what_is_running(window):  # noqa: F811
    assert window.statusBar().currentMessage() == "Pronto"
    window.analysis_overlay.start("Analisi AI in corso")
    assert window.statusBar().currentMessage().startswith("Analisi AI in corso")
    window.analysis_overlay.stop()
    assert window.statusBar().currentMessage() == "Pronto"


def test_steps_never_overlap_and_do_not_grow_on_refresh(window):  # noqa: F811
    """@brief Negativo di due difetti visti a schermo: etichette sovrapposte e
    larghezze che crescevano a ogni aggiornamento."""
    bar = WorkflowBar()
    bar.show()
    bar.set_state({"documents", "analysis", "review", "model3d"}, "kicad")
    first = [label.width() for label in bar.labels.values()]
    for _ in range(3):
        bar.set_state({"documents", "analysis", "review", "model3d"}, "kicad")
    assert [label.width() for label in bar.labels.values()] == first
    layout = bar.layout()
    edges = [(layout.itemAt(i).widget().geometry().x(), layout.itemAt(i).widget().geometry().right())
             for i in range(layout.count())]
    for (_, right), (left, _) in zip(edges, edges[1:]):
        assert left > right
    for label in bar.labels.values():
        assert label.width() >= label.fontMetrics().horizontalAdvance(label.text())


def test_every_step_label_is_translated_in_the_four_languages():
    """@brief Le etichette stanno in una tupla, non in chiamate `text("…")`:
    il controllo generale del catalogo non le vede. «Modello 3D» e «KiCad»
    erano rimaste senza voce e in inglese si leggeva «Modello 3D»."""
    from kicadgen.services.i18n import DEFAULT_TRANSLATIONS
    for _key, source in WORKFLOW_STEPS:
        entry = DEFAULT_TRANSLATIONS.get("ui:" + source)
        assert entry is not None, source
        assert set(entry) == {"en", "it", "fr", "de"} and entry["it"] == source
        assert all(entry[language].strip() for language in entry), source


def _fresh_window(monkeypatch, tmp_path):
    """Construct a MainWindow as at start-up: no name typed, no refresh forced."""
    from qt_fixtures import PreviewStub
    monkeypatch.setattr(gui, "Preview3DVtk", PreviewStub)
    monkeypatch.setattr(gui, "load_config", lambda: {"component_type": "", "system_prompt": ""})
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    monkeypatch.setattr(gui, "DEFAULT_LIBRARY_ROOT", tmp_path)
    return gui.MainWindow()


def test_a_new_window_says_no_project_without_any_event(qapp, monkeypatch, tmp_path):
    """@brief Positive (2026-10-17, S1): the status label is right as soon as the window exists.

    The label was built after every refresh of the construction and stayed
    empty until the first language switch recomputed it.
    """
    from qt_fixtures import shutdown_main_window
    window = _fresh_window(monkeypatch, tmp_path)
    try:
        assert window.status_project.text() == "Nessun progetto aperto"
        assert window.workflow_bar.state() == (set(), None, set())
    finally:
        shutdown_main_window(window, qapp)


def test_opening_a_project_still_drives_the_status_after_start_up(qapp, monkeypatch, tmp_path):
    """@brief Negative: the start-up pass must not freeze the label; opening a project rewrites it."""
    from qt_fixtures import shutdown_main_window
    window = _fresh_window(monkeypatch, tmp_path)
    try:
        folder = window._library_root() / "APERTO"
        (folder / "APERTO.pretty").mkdir(parents=True)
        (folder / "APERTO.pretty" / "APERTO.kicad_mod").write_text("(footprint)")
        window._open_project_at(folder, save_current=False)
        assert window.status_project.text() == "Progetto: APERTO"
        assert "analysis" in window.workflow_bar.state()[0]
    finally:
        shutdown_main_window(window, qapp)
