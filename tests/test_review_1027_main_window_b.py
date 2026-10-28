"""@brief Review of 2026-10-27, block B: main-window findings of the second half of the file.

Each finding has a positive case (the fixed behaviour) and a negative case
(the behaviour that must stay as it was), so that a rule is verified for a
structural reason and not for the one scenario that was observed.
"""
import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QCoreApplication, QEvent
from PyQt6.QtWidgets import QComboBox, QDialog, QMenu, QMessageBox, QWizard

from kicadgen.gui import main_window as gui
from kicadgen.gui.project_state import saved_package_geometry, write_project_config
from kicadgen.services import datasheet_extractor
from kicadgen.services.i18n import DEFAULT_TRANSLATIONS, LANGUAGES
from kicadgen.services.kicad_library import InstallPlan
from tests.test_generated_files_alignment import dialogs  # noqa: F401
from tests.test_kicad_gate_remedies import NAME, _ready, _setup_step_unbuildable

KICAD_COMMANDS = gui.MainWindow.KICAD_COMMAND_ATTRS
STEP_SUFFIX = ".step"


def _set_language(window, language):
    """Switch the window language as the user does, through the combo."""
    window.language_combo.setCurrentIndex(window.language_combo.findData(language))


def _flush_deferred_deletes():
    """Deliver the DeferredDelete events that `deleteLater` posted."""
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _artifacts(window):
    """The real footprint/STEP set of the open component."""
    return gui.discover_component_artifacts(window._project_dir(), window._component_name(),
                                            require_3d=True)


# ── B1: a STEP that could not be deleted keeps its block ──────────────────

def test_b1_a_locked_step_keeps_the_obsolete_block(window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    """@brief Positive: unlink refused for the .step -> records kept, block and commands off."""
    folder = _ready(window, monkeypatch, tmp_path)
    _setup_step_unbuildable(window, monkeypatch, folder)
    window._refresh_output_actions()
    assert window._kicad_block_code() == "step_unbuildable"
    real_unlink = Path.unlink

    def unlink(self, *args, **kwargs):
        if self.suffix == STEP_SUFFIX:
            raise PermissionError(13, "locked by another program", str(self))
        return real_unlink(self, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    logged = []
    monkeypatch.setattr(window, "_append_log", lambda message, **k: logged.append(message))
    window._remove_obsolete_model()
    window._refresh_output_actions()
    assert (folder / f"{NAME}.3dshapes" / f"{NAME}.step").exists()
    assert window._step_source_pads is not None
    assert window._kicad_block_code() == "step_unbuildable"
    assert not any(getattr(window, attr).isEnabled() for attr in KICAD_COMMANDS)
    assert any("non rimosso" in line for line in logged), logged


def test_b1_a_deleted_step_clears_the_records(window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    """@brief Negative: when the .step is really gone the records go and nothing blocks."""
    folder = _ready(window, monkeypatch, tmp_path)
    _setup_step_unbuildable(window, monkeypatch, folder)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    window._remove_obsolete_model()
    assert not (folder / f"{NAME}.3dshapes" / f"{NAME}.step").exists()
    assert window._step_source_pads is None and window._step_source_body is None
    assert window._kicad_files_alignment_reason() == ""


# ── B2 and B8: export history kinds and malformed rows ───────────────────

def _history_cells(window):
    table = window.kicad_export_table
    return [[table.item(r, c).text() for c in range(table.columnCount())]
            for r in range(table.rowCount())]


def test_b2_kinds_are_codes_translated_when_shown(window, monkeypatch):
    """@brief Positive: codes and legacy Italian rows both show in English. Negative: Italian unchanged."""
    records = [
        {"component": "A", "kind": "project_library", "destination": "C:/p/A", "timestamp": "2026-10-27T10:00:00"},
        {"component": "B", "kind": "Libreria globale", "destination": "C:/g", "timestamp": "2026-10-27T11:00:00"},
        {"component": "C", "kind": "symbol_file", "destination": "C:/s/C.kicad_sym", "timestamp": "x"},
        {"component": "D", "kind": "future_kind", "destination": "d", "timestamp": "y"},
    ]
    monkeypatch.setattr(gui, "load_kicad_export_history", lambda: records)
    window._refresh_kicad_export_history()
    assert [row[1] for row in _history_cells(window)] == [
        "Libreria di progetto", "Libreria globale", "File simbolo", "future_kind"]
    _set_language(window, "en")
    assert [row[1] for row in _history_cells(window)] == [
        "Project library", "Global library", "Symbol file", "future_kind"]


def test_b2_installation_and_symbol_export_persist_the_codes(window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    """@brief Positive: the history receives stable codes, never the Italian names."""
    _ready(window, monkeypatch, tmp_path, with_body=False)
    recorded = []
    monkeypatch.setattr(gui, "record_kicad_export", lambda **k: recorded.append(k["kind"]) or True)
    window._install_kicad_library(tmp_path / "dest", project_library=True, artifacts=_artifacts(window))
    target = tmp_path / "copy.kicad_sym"
    monkeypatch.setattr(gui.QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    monkeypatch.setattr(window.symbol_tab, "confirm_export_review", lambda: True)
    window._export_electrical_symbol()
    assert recorded == ["project_library", "symbol_file"]
    assert target.exists()


def test_b8_a_malformed_history_row_never_breaks_the_table(window, monkeypatch):
    """@brief Positive: non-string fields are shown as text. Negative: a sane row is unchanged."""
    records = [
        {"component": ["x"], "kind": 3, "destination": None, "timestamp": 1730000000},
        {"component": "OK", "kind": "project_library", "destination": "C:/a/b", "timestamp": "2026-10-27T09:08:07+01:00"},
    ]
    monkeypatch.setattr(gui, "load_kicad_export_history", lambda: records)
    window._refresh_kicad_export_history()
    cells = _history_cells(window)
    assert cells[0] == ["['x']", "3", "", "1730000000"]
    assert cells[1] == ["OK", "Libreria di progetto", "b", "2026-10-27 09:08:07"]


# ── B3 and K: the install sink re-checks the gate and words the plan ────

def _global_install(window, tmp_path, artifacts):
    window._install_kicad_library(tmp_path / "dest", project_library=False,
                                  table_path=tmp_path / "fp-lib-table", table_version="10.0",
                                  artifacts=artifacts)


def test_b3_a_rewrite_queued_during_the_question_refuses_the_install(window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    """@brief Positive: an edit applied while the question is open stops the copy."""
    _ready(window, monkeypatch, tmp_path, with_body=False)
    artifacts = _artifacts(window)

    def question(*args, **kwargs):
        window._schedule_footprint_rewrite()       # queued, as an applied edit does
        return QMessageBox.StandardButton.Yes
    monkeypatch.setattr(QMessageBox, "question", question)
    # `prepare` (inside `_ready`) silences the warning boxes: the refusal is
    # read from the log line `_refuse_kicad_command` writes with the box.
    logged = []
    monkeypatch.setattr(window, "_append_log", lambda message, **k: logged.append(message))
    _global_install(window, tmp_path, artifacts)
    assert dialogs["installed"] == []
    assert logged and "Applica ai file" in logged[-1]


def test_b3_work_started_during_the_question_refuses_the_install(window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    """@brief Positive: a worker started meanwhile stops the copy (the `busy` block)."""
    _ready(window, monkeypatch, tmp_path, with_body=False)
    artifacts = _artifacts(window)
    running = type("W", (), {"isRunning": lambda self: True})()

    def question(*args, **kwargs):
        monkeypatch.setattr(window, "_terminal_worker", running)
        return QMessageBox.StandardButton.Yes
    monkeypatch.setattr(QMessageBox, "question", question)
    _global_install(window, tmp_path, artifacts)
    assert dialogs["installed"] == []


def test_b3_an_unchanged_state_installs_after_the_question(window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    """@brief Negative: nothing happened during the question -> one install, table and version named."""
    _ready(window, monkeypatch, tmp_path, with_body=False)
    asked = []
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: asked.append(a[2]) or QMessageBox.StandardButton.Yes)
    _global_install(window, tmp_path, _artifacts(window))
    assert len(dialogs["installed"]) == 1
    assert dialogs["installed"][0]["table_path"] == tmp_path / "fp-lib-table"
    assert asked and "10.0" in asked[-1] and str(tmp_path / "fp-lib-table") in asked[-1]


def test_k_a_project_install_without_existing_files_asks_nothing(window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    """@brief Negative: a first project install has no plan to confirm."""
    _ready(window, monkeypatch, tmp_path, with_body=False)
    dialogs["question"].clear()
    window._install_kicad_library(tmp_path / "dest", project_library=True, artifacts=_artifacts(window))
    assert dialogs["question"] == [] and len(dialogs["installed"]) == 1


def test_k_the_prompt_names_overwritten_deleted_and_linked_files(window, tmp_path):
    """@brief Positive: each section of the plan is worded; empty ones are left out."""
    plan = InstallPlan(targets=(), replaced=(tmp_path / "a.kicad_mod",), removed=(tmp_path / "a.wrl",),
                       linked=(tmp_path / "fp-lib-table",), table_path=tmp_path / "fp-lib-table",
                       table_exists=True, library_uri="x")
    text = window._install_plan_text(plan, project_library=False, table_version="9.0")
    for path in ("a.kicad_mod", "a.wrl", "fp-lib-table"):
        assert path in text
    assert "9.0" in text and "eliminati" in text and "collegamenti" in text
    bare = InstallPlan(targets=(), replaced=(tmp_path / "a.kicad_mod",), removed=(), linked=(),
                       table_path=tmp_path / "t", table_exists=True, library_uri="x")
    text = window._install_plan_text(bare, project_library=True, table_version=None)
    assert "eliminati" not in text and "collegamenti" not in text and "KiCad 9.0" not in text


def test_k_the_report_names_replaced_and_removed_files(window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    """@brief Positive: `result.replaced`/`result.removed` reach the final report."""
    _ready(window, monkeypatch, tmp_path, with_body=False)
    replaced, removed = tmp_path / "old.kicad_mod", tmp_path / "old.wrl"
    monkeypatch.setattr(gui, "install_component_library", lambda *a, **k: SimpleNamespace(
        identifier="KiCadGen:" + NAME, footprint_path="fp", table_path="t",
        replaced=(replaced,), removed=(removed,)))
    window._install_kicad_library(tmp_path / "dest", project_library=True, artifacts=_artifacts(window))
    report = window.label_kicad_status.text()
    assert str(replaced) in report and str(removed) in report


def test_k_the_global_install_resolves_the_table_with_its_version(window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    """@brief Positive: `resolve_global_fp_lib_table` feeds both the table path and the version."""
    _ready(window, monkeypatch, tmp_path, with_body=False)
    seen = {}
    monkeypatch.setattr(gui, "resolve_global_fp_lib_table", lambda cli: SimpleNamespace(
        path=tmp_path / "kicad" / "9.0" / "fp-lib-table", version="9.0", version_source="kicad-cli"))
    monkeypatch.setattr(gui.QFileDialog, "getExistingDirectory", lambda *a, **k: str(tmp_path / "lib"))
    monkeypatch.setattr(window, "_install_kicad_library", lambda dest, **k: seen.update(k))
    window._install_global_library()
    assert seen["table_path"] == tmp_path / "kicad" / "9.0" / "fp-lib-table"
    assert seen["table_version"] == "9.0" and seen["project_library"] is False


# ── B4: a stale generation result does not drive the window ──────────────

def test_b4_a_stale_footprint_result_leaves_pinout_and_symbol_alone(window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    """@brief Positive: stale sender -> no reconciliation, no symbol save, file still marked behind."""
    _ready(window, monkeypatch, tmp_path, with_body=False)
    calls = []
    monkeypatch.setattr(window, "_reconcile_pinout_with_written_footprint", lambda: calls.append("pins"))
    monkeypatch.setattr(window, "_save_applied_electrical_symbol", lambda **k: calls.append("symbol"))
    pinout = [dict(row) for row in window.pinout]
    window._fp_file_stale = True
    stale = SimpleNamespace(_model_revision=window._model_revision - 1, metadata_snapshot=None)
    monkeypatch.setattr(window, "sender", lambda: stale)
    window._on_generated("x.kicad_mod")
    assert calls == [] and window._fp_file_stale is True
    assert window.pinout == pinout


def test_b4_a_current_footprint_result_reconciles_and_saves(window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    """@brief Negative: a current sender keeps the old behaviour."""
    _ready(window, monkeypatch, tmp_path, with_body=False)
    calls = []
    monkeypatch.setattr(window, "_reconcile_pinout_with_written_footprint", lambda: calls.append("pins"))
    monkeypatch.setattr(window, "_save_applied_electrical_symbol", lambda **k: calls.append("symbol"))
    window._fp_file_stale = True
    current = SimpleNamespace(_model_revision=window._model_revision, metadata_snapshot=None)
    monkeypatch.setattr(window, "sender", lambda: current)
    window._on_generated("x.kicad_mod")
    assert calls == ["pins", "symbol"] and window._fp_file_stale is False


# ── B5: an applied footprint edit not yet written blocks the gate ────────

@pytest.mark.parametrize("flag", ["_fp_rewrite_dirty", "_fp_file_stale"])
def test_b5_a_pending_footprint_write_blocks_the_kicad_commands(window, monkeypatch, tmp_path, dialogs, flag):  # noqa: F811
    """@brief Positive: either flag blocks with its own code and an enabled remedy."""
    _ready(window, monkeypatch, tmp_path, with_body=False)
    setattr(window, flag, True)
    window._refresh_output_actions()
    assert window._kicad_block_code() == "footprint_pending_write"
    assert not any(getattr(window, attr).isEnabled() for attr in KICAD_COMMANDS)
    remedy = gui.MainWindow.KICAD_BLOCK_REMEDIES["footprint_pending_write"]
    assert getattr(window, remedy).isEnabled()


def test_b5_the_queued_rewrite_clears_the_block_once_it_ran(window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    """@brief Negative: once the deferred rewrite has written the file, nothing blocks."""
    _ready(window, monkeypatch, tmp_path, with_body=False)
    window._schedule_footprint_rewrite()
    assert window._kicad_block_code() == "footprint_pending_write"
    QCoreApplication.processEvents()
    window._refresh_output_actions()
    assert window._kicad_block_code() == "", window._kicad_command_block_reason()


# ── B6: modal dialogs and the text menu are released ─────────────────────

def test_b6_the_usage_wizard_is_released(window, monkeypatch):
    """@brief Positive: after the wizard closes no QWizard child is left on the window."""
    monkeypatch.setattr(QWizard, "exec", lambda self: 0)
    window._show_kicad_usage_wizard()
    _flush_deferred_deletes()
    assert window.findChildren(QWizard) == []


def test_b6_the_pinout_dialog_is_released_and_still_read(window, monkeypatch):
    """@brief Positive: the accepted rows are read, then the dialog is deleted."""
    window.pinout = [{"pin": "1", "name": "A"}]
    monkeypatch.setattr(gui.PinoutDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    window._show_pinout_dialog()
    _flush_deferred_deletes()
    assert window.findChildren(gui.PinoutDialog) == []
    assert window.pinout == [{"pin": "1", "name": "A"}]


def test_b6_the_footprint_text_menu_is_released(window, monkeypatch):
    """@brief Positive: the context menu does not stay as a hidden child."""
    from kicadgen.core.footprint import TEXT_KEYS
    monkeypatch.setattr(QMenu, "exec", lambda self, *a: None)
    before = len(window.findChildren(QMenu))
    window._footprint_text_context_menu(next(iter(TEXT_KEYS)))
    _flush_deferred_deletes()
    assert len(window.findChildren(QMenu)) == before


# ── B7: wizard page 3 calls the export and names the written file ───────

def test_b7_the_wizard_export_explains_a_refusal(window, monkeypatch):
    """@brief Positive: unapplied symbol edits -> a visible reason, no export call."""
    shown, exported = [], []
    monkeypatch.setattr(window.symbol_tab, "has_pending_edits", lambda: True)
    monkeypatch.setattr(QMessageBox, "information", lambda *a: shown.append(a[2]))
    monkeypatch.setattr(window, "_export_electrical_symbol", lambda: exported.append(1))
    window._export_symbol_from_wizard()
    assert exported == [] and shown and "non applicate" in shown[0]


def test_b7_the_wizard_export_runs_when_allowed(window, monkeypatch):
    """@brief Negative: no pending edit and no mismatch -> the export itself runs."""
    exported = []
    monkeypatch.setattr(window.symbol_tab, "has_pending_edits", lambda: False)
    monkeypatch.setattr(window, "_symbol_pad_mismatch", lambda: "")
    monkeypatch.setattr(window, "_export_electrical_symbol", lambda: exported.append(1))
    window._export_symbol_from_wizard()
    assert exported == [1]


def test_b7_every_wizard_text_is_in_the_catalogue():
    """@brief Positive: the five pages, their buttons and page 3's new text have four languages."""
    tree = ast.parse(Path(gui.__file__).read_text(encoding="utf-8"))
    method = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef) and node.name == "_show_kicad_usage_wizard")
    literals = set()
    for node in ast.walk(method):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.strip():
            literals.add(node.value)
    # Adjacent literals are joined by the parser: each is one catalogue source.
    # The method's own docstring is documentation, not interface text.
    literals.discard(method.body[0].value.value)
    texts = {text for text in literals if any(ch.isalpha() for ch in text) and " " in text}
    assert any(text.startswith("3. Registra il simbolo") for text in texts)
    assert not any("Esporta il simbolo .kicad_sym" in text for text in texts)
    for text in texts:
        entry = DEFAULT_TRANSLATIONS.get("ui:" + text)
        assert entry and all(entry.get(language) for language in LANGUAGES), text


# ── B9: the start-up error box never fails on the language lookup ────────

def test_b9_the_start_up_language_survives_a_broken_config(monkeypatch):
    """@brief Positive: a config that raises or is not a dict -> Italian. Negative: a valid language is kept."""
    def broken():
        raise ValueError("config.json is not JSON")
    monkeypatch.setattr(gui, "load_config", broken)
    assert gui._startup_error_language() == "it"
    monkeypatch.setattr(gui, "load_config", lambda: ["not", "a", "dict"])
    assert gui._startup_error_language() == "it"
    monkeypatch.setattr(gui, "load_config", lambda: {"language": "xx"})
    assert gui._startup_error_language() == "it"
    monkeypatch.setattr(gui, "load_config", lambda: {"language": "en"})
    assert gui._startup_error_language() == "en"


def test_b9_main_shows_the_box_even_when_the_config_cannot_be_read(qapp, monkeypatch):
    """@brief Positive: window and config both fail -> the critical box still appears, in Italian."""
    def fail(*a, **k):
        raise OSError("profile database locked")

    def broken():
        raise ValueError("config.json is not JSON")
    shown = []
    monkeypatch.setattr(gui, "MainWindow", fail)
    monkeypatch.setattr(gui, "load_config", broken)
    monkeypatch.setattr(gui, "install_tooltip_filter", lambda app: None)
    monkeypatch.setattr(qapp, "setStyleSheet", lambda sheet: None)
    monkeypatch.setattr(qapp, "setWindowIcon", lambda icon: None)
    monkeypatch.setattr(QMessageBox, "critical", lambda *a: shown.append(a[1]))
    with pytest.raises(SystemExit):
        gui.main()
    assert shown == [DEFAULT_TRANSLATIONS["ui:Database profili non disponibile"]["it"]]


# ── B10: docstrings ──────────────────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "__init__", "_verify_component", "_open_project_dir", "_confirm_output_overwrite",
    "_export_electrical_symbol", "_show_pinout_dialog", "_on_terminal_detail_done",
    "_draw_preview", "_edit_preview_pad", "_on_tab_changed", "_validate_output_name"])
def test_b10_the_listed_methods_are_documented(name):
    """@brief Positive: each listed method starts with an `@brief` docstring."""
    doc = getattr(gui.MainWindow, name).__doc__ or ""
    assert doc.lstrip().startswith("@brief"), name


# ── G7: no trapezoid offered; an old project loads with a note ───────────

def test_g7_the_schema_and_the_pad_combo_no_longer_offer_a_trapezoid(window, monkeypatch):
    """@brief Positive: neither the AI schema nor the pad editor offer «trapezoid»; rect still offered."""
    text = json.dumps(datasheet_extractor._openai_tool())
    assert '"trapezoid"' not in text
    window.package_geometry = saved_package_geometry({"name": "P", "family": "soic", "pins": [
        {"number": "1", "x_mm": 0, "y_mm": 0, "pad_length_mm": 1.0, "pad_width_mm": 0.5, "shape": "rect"}]})
    offered = []

    def inspect(dialog):
        combo = dialog.findChildren(QComboBox)[0]
        offered.extend(combo.itemData(i) for i in range(combo.count()))
        return QDialog.DialogCode.Rejected
    monkeypatch.setattr(QDialog, "exec", inspect)
    window._edit_package_pad(0)
    assert "trapezoid" not in offered and {"rect", "roundrect", "circle", "oval"} <= set(offered)


def test_g7_an_old_trapezoid_project_loads_as_rectangles_with_a_note(window, monkeypatch, tmp_path):
    """@brief Positive: the saved pad loads as «rect» and the log says so. Negative: other pads untouched."""
    state = dict(window._general_project_state)
    state["project_kind"] = "single_component"
    state["package_geometry"] = {"name": "OLD", "family": "soic", "pins": [
        {"number": "1", "x_mm": -1.0, "y_mm": 0, "pad_length_mm": 1.0, "pad_width_mm": 0.5,
         "shape": "trapezoid"},
        {"number": "2", "x_mm": 1.0, "y_mm": 0, "pad_length_mm": 1.0, "pad_width_mm": 0.5,
         "shape": "oval"}]}
    logged = []
    monkeypatch.setattr(window, "_append_log", lambda message, **k: logged.append(message))
    window._apply_project_state(state)
    shapes = [pin.shape for pin in window.package_geometry.pins]
    assert shapes == ["rect", "oval"]
    assert any("trapezio" in line and "1" in line for line in logged), logged
    # The saved mapping itself is not modified (the file stays what it was).
    assert state["package_geometry"]["pins"][0]["shape"] == "trapezoid"
    notes = []
    saved_package_geometry({"name": "N", "family": "soic", "pins": [
        {"number": "1", "x_mm": 0, "y_mm": 0, "pad_length_mm": 1, "pad_width_mm": 1, "shape": "oval"}]},
        notes)
    assert notes == []


def test_g7_an_old_trapezoid_project_file_opens(window, tmp_path):
    """@brief Positive: the file-level check accepts it, so the project is not refused."""
    from kicadgen.gui.project_state import read_project_config
    folder = tmp_path / "Old"
    (folder / "docs").mkdir(parents=True)
    write_project_config(folder, {"project_kind": "single_component", "package_geometry": {
        "name": "Old", "family": "soic", "pins": [
            {"number": "1", "x_mm": 0, "y_mm": 0, "pad_length_mm": 1, "pad_width_mm": 1,
             "shape": "trapezoid"}]}})
    assert read_project_config(folder)["package_geometry"]["pins"][0]["shape"] == "trapezoid"


# ── H4: one output-name rule ─────────────────────────────────────────────

@pytest.mark.parametrize("name,valid", [("ESP32.module", True), ("..\\evil", False), ("a.", False),
                                        ("", False), ("ok name", True)])
def test_h4_the_window_uses_the_shared_rule(name, valid):
    """@brief Positive and negative: the window refuses exactly what the shared rule refuses."""
    from kicadgen.cad3d.output_names import is_valid_output_name
    assert is_valid_output_name(name) is valid
    if valid:
        gui.MainWindow._validate_output_name(name)
    else:
        with pytest.raises(ValueError, match="nome di file valido"):
            gui.MainWindow._validate_output_name(name)


# ── Catalogue entries requested by the other fixers ──────────────────────

@pytest.mark.parametrize("key", [
    "kicad.error.target_link_unresolved", "kicad.plan_global_table", "kicad.plan_replaced",
    "kicad.plan_removed", "kicad.plan_linked", "kicad.plan_question", "kicad.result_replaced",
    "kicad.result_removed"])
def test_catalogue_kicad_keys_have_four_languages(key):
    """@brief Positive: each new key has the four languages with the same placeholders."""
    import string
    entry = DEFAULT_TRANSLATIONS[key]
    fields = {language: sorted(n for _, n, _, _ in string.Formatter().parse(entry[language]) if n)
              for language in LANGUAGES}
    assert len({tuple(v) for v in fields.values()}) == 1, fields


# ── Sweep follow-up: the package carries the project name ────────────────

@pytest.mark.parametrize("ai_name", ["LM358/LM2904", "OK-NAME"])
def test_the_package_keeps_its_pads_whatever_name_the_analysis_proposes(window, monkeypatch, tmp_path, ai_name):
    """@brief Positive: a proposal that is not a file name («/») keeps the geometry.

    Negative: a plain proposal behaves the same; both carry the PROJECT name,
    never the proposal, so footprint, package and folder stay in agreement.
    """
    from tests.ui_flow_helpers import header_extraction, prepare, run_extraction
    prepare(window, monkeypatch, tmp_path, "PKG")
    pins = [{"number": str(i), "x_mm": -1.27 + 2.54 * (i - 1), "y_mm": 0.0, "pad_length_mm": 1.0,
             "pad_width_mm": 0.6, "shape": "rect"} for i in (1, 2)]
    run_extraction(window, header_extraction(name=ai_name, package_geometry={
        "name": ai_name, "family": "soic", "pins": pins}))
    assert window.package_geometry is not None
    assert window.package_geometry.name == "PKG"
    assert [pin.number for pin in window.package_geometry.pins] == ["1", "2"]
