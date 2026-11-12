"""@brief Workflow steps above the tabs and status bar (PIANO-FIX item 4, 2026-09-26)."""
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QSignalSpy, QTest

from kicadgen.gui.workflow_bar import WORKFLOW_STEPS, WorkflowBar
from tests.test_photo_inventory_gui import gui


def test_the_bar_ticks_done_steps_and_highlights_the_current_one(window):  # noqa: F811
    bar = WorkflowBar()
    bar.set_state({"documents"}, "analysis")
    assert "✓" in bar.labels["documents"].text()
    assert "●" in bar.labels["analysis"].text()
    assert bar.labels["analysis"].font().bold() and not bar.labels["documents"].font().bold()
    assert "○" in bar.labels["kicad"].text()
    spy = QSignalSpy(bar.step_clicked)
    QTest.mouseClick(bar.labels["model3d"], Qt.MouseButton.LeftButton, pos=QPoint(2, 2))
    assert spy[0][0] == "model3d"
    assert [key for key, _ in WORKFLOW_STEPS] == ["documents", "analysis", "review", "model3d", "kicad"]


def _shown_texts(bar):
    """Texts of the visible step labels, in bar order."""
    return [bar.labels[key].text() for key, _ in WORKFLOW_STEPS if not bar.labels[key].isHidden()]


def test_a_hidden_step_disappears_with_its_arrow_and_the_rest_is_renumbered(window):  # noqa: F811
    """@brief Positive (user request 2026-11-12): a step that does not apply is
    not shown at all, not struck through, and the visible steps count 1..4."""
    bar = WorkflowBar()
    bar.set_state({"documents", "analysis"}, "model3d", {"review"})
    assert bar.labels["review"].isHidden() and bar.arrows["review"].isHidden()
    assert _shown_texts(bar) == ["✓ 1 · Documenti", "✓ 2 · Analisi AI",
                                 "● 3 · Modello STEP", "○ 4 · KiCad"]
    assert not any(bar.labels[key].font().strikeOut() for key in bar.labels)
    assert not bar.arrows["model3d"].isHidden() and not bar.arrows["kicad"].isHidden()


def test_a_step_shown_again_takes_back_its_number_and_arrow(window):  # noqa: F811
    """@brief Negative: hiding is not sticky; the same bar given no hidden step
    shows the five steps numbered 1..5 again."""
    bar = WorkflowBar()
    bar.set_state(set(), "documents", {"review"})
    bar.set_state(set(), "documents")
    assert not bar.labels["review"].isHidden() and not bar.arrows["review"].isHidden()
    assert [text.split(" · ")[0][-1] for text in _shown_texts(bar)] == ["1", "2", "3", "4", "5"]


def test_hiding_the_first_step_never_leaves_a_leading_arrow(window):  # noqa: F811
    """@brief Negative: the arrow belongs to the step it points to; the first
    visible step has none, whichever step that is."""
    bar = WorkflowBar()
    bar.set_state(set(), None, {"documents"})
    assert bar.arrows["analysis"].isHidden()
    assert _shown_texts(bar)[0] == "○ 1 · Analisi AI"


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
    done, current, hidden = window.workflow_bar.state()
    assert {"analysis", "model3d"} <= done
    assert "review" not in hidden, "una scheda con componenti ha la revisione"
    assert not window.workflow_bar.labels["review"].isHidden()
    assert current in ("documents", "review")
    assert window.status_project.text() == "Progetto: SCHEDA"


def test_a_single_component_hides_the_body_review(window, tmp_path):  # noqa: F811
    """@brief Negativo: un componente singolo non ha corpi da rivedere; the
    step is hidden and comes back as soon as the type becomes a board."""
    window.field_library_root.setText(str(tmp_path))
    window.field_name.setText("RELE")
    combo = window.combo_component_type
    # No manual refresh: the type change itself must update the bar.
    combo.setCurrentIndex(combo.findData("single_tht"))
    _done, current, hidden = window.workflow_bar.state()
    assert "review" in hidden and current != "review"
    assert window.workflow_bar.labels["review"].isHidden()
    combo.setCurrentIndex(combo.findData("aggregate_tht"))
    assert "review" not in window.workflow_bar.state()[2]
    assert not window.workflow_bar.labels["review"].isHidden()


def test_an_undecided_type_keeps_every_step_without_proposing_the_review(window, tmp_path):  # noqa: F811
    """@brief Negative: «(non specificata)» cannot tell board from single
    component (`_photo_inventory_family()` None): nothing is hidden, since
    hiding is a claim the type must declare, but the review, which cannot be
    ticked for it, is never the current step either."""
    window.field_library_root.setText(str(tmp_path))
    window.field_name.setText("INCERTO")
    combo = window.combo_component_type
    combo.setCurrentIndex(combo.findData("single_tht"))
    combo.setCurrentIndex(0)
    assert window._photo_inventory_family() is None
    done, current, hidden = window.workflow_bar.state()
    assert hidden == set() and not window.workflow_bar.labels["review"].isHidden()
    assert current != "review"
    folder = window._project_dir()
    (folder / "INCERTO.pretty").mkdir(parents=True)
    (folder / "INCERTO.pretty" / "INCERTO.kicad_mod").write_text("(footprint)")
    window.docs_list.addItem("datasheet.pdf")
    window._refresh_workflow_state()
    done, current, _hidden = window.workflow_bar.state()
    assert {"documents", "analysis"} <= done and "review" not in done
    assert current == "model3d", "the review it cannot tick must not hold the highlight"


def test_a_board_kind_without_a_type_can_tick_its_review(window, tmp_path, monkeypatch):  # noqa: F811
    """@brief Positive: a board chosen in the wizard with no type yet is a
    board for the bar too (one predicate): its review is proposed, and a done
    review is ticked."""
    window.field_library_root.setText(str(tmp_path))
    window.field_name.setText("MODULO")
    window.combo_component_type.setCurrentIndex(0)
    window.project_kind = gui._PROJECT_KIND_BOARD
    folder = window._project_dir()
    (folder / "MODULO.pretty").mkdir(parents=True)
    (folder / "MODULO.pretty" / "MODULO.kicad_mod").write_text("(footprint)")
    monkeypatch.setattr(window, "_reviewed_this_extraction", True, raising=False)
    window._refresh_workflow_state()
    done, current, hidden = window.workflow_bar.state()
    assert "review" in done and "review" not in hidden


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
