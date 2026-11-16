"""@brief Fixes of the 2026-11-15 review in the window (fixer C5): each rule with a positive and a negative case.

- A1: the designator and the footprint filters live only in the symbol file;
  a change of either is «not applied» until «Applica ai file» (or the Symbol
  tab's «Applica») writes it into <name>.kicad_sym; unchanged values stay
  «già nei file».
- A5: the component check receives the estimated pads of the footprint the
  files were written from; the finished card says how many there are, the
  outcome staying «Completato».
- P2: a footprint rewritten for its metadata does not make the STEP «older»
  when the STEP's source record matches; a body change still does.
- P3: the install report lives in step 3, short, with its paths behind a
  toggle; a re-install of identical files says so; a dropped BOM is said;
  «Installato e aggiornato» is said in every state that allows installing.
- Costs: the finished card's fallback follows a language switch.
- Log: the archive consult before the review does not print each body line
  that the generation prints again.
- Naming: «Apri in KiCad» everywhere.
- Relay designator (C7): declared coil + contact pins propose «K»; a typed
  designator is never replaced.

Offline: no AI, no kicad-cli, no FreeCAD (the workers are faked).
"""
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("pyvistaqt")

from PyQt6.QtWidgets import QMessageBox  # noqa: E402

from kicadgen.core.package_geometry import package_from_mapping  # noqa: E402
from kicadgen.gui import main_window as gui  # noqa: E402
from tests.test_generated_files_alignment import NAME, _enable_3d, _project, dialogs  # noqa: E402,F401

ESTIMATED = {"drill_mm": "estimated", "pad_size": "estimated", "shape": "estimated"}


def _symbol_property(path: Path, name: str):
    """The quoted value of property `name` in the .kicad_sym at `path`, or None."""
    match = re.search(r'\(property "%s" "([^"]*)"' % re.escape(name), path.read_text(encoding="utf-8"))
    return match.group(1) if match else None


def _with_symbol_file(window, monkeypatch, tmp_path) -> Path:
    """A generated pin-header project whose symbol file was written from the applied symbol."""
    folder = _project(window, monkeypatch, tmp_path)
    window._refresh_symbol_tab()
    window._save_applied_electrical_symbol(quiet=True)
    window._refresh_output_actions()
    symbol = folder / f"{NAME}.kicad_sym"
    assert symbol.is_file()
    return symbol


# ── A1: designator and footprint filters reach the symbol file ──────────────

def test_a_changed_designator_is_not_applied_until_applica_writes_it(window, monkeypatch, tmp_path):
    symbol = _with_symbol_file(window, monkeypatch, tmp_path)
    # Negative: untouched fields -> «già nei file», installation open.
    assert not window._metadata_not_applied()
    assert window.label_metadata_apply_state.text() == "I metadati sono già nei file."
    assert window.btn_export.isEnabled()
    window.field_reference.setText("RLY")
    window.field_fp_filters.setText("Other_*")
    window._refresh_output_actions()
    # Positive: the symbol file still says otherwise -> not applied, «Applica» highlighted.
    assert window._metadata_not_applied()
    assert window.label_metadata_apply_state.text() == "Metadati modificati: non sono ancora nei file."
    assert window.btn_apply_metadata.objectName() == "primary"
    assert not window.btn_export.isEnabled() and "«Applica ai file»" in window.btn_export.toolTip()
    assert _symbol_property(symbol, "Reference") != "RLY"
    window.btn_apply_metadata.click()
    assert _symbol_property(symbol, "Reference") == "RLY"
    assert _symbol_property(symbol, "ki_fp_filters") == "Other_*"
    window._refresh_output_actions()
    assert not window._metadata_not_applied()
    assert window.label_metadata_apply_state.text() == "I metadati sono già nei file."
    assert window.btn_export.isEnabled()


def test_a_value_typed_back_to_what_the_file_holds_is_neutral(window, monkeypatch, tmp_path):
    """Values are compared, not an «edited» flag: the proposal typed in by hand changes nothing."""
    symbol = _with_symbol_file(window, monkeypatch, tmp_path)
    written = _symbol_property(symbol, "Reference")
    window.field_reference.setText(written)
    window._refresh_output_actions()
    assert not window._metadata_not_applied()
    window.field_reference.setText("J")
    assert window._metadata_not_applied()


def test_without_a_symbol_file_nothing_is_stale_in_it(window, monkeypatch, tmp_path):
    """Negative: no .kicad_sym on disk -> the symbol read-back has nothing to call stale."""
    symbol = _with_symbol_file(window, monkeypatch, tmp_path)
    symbol.unlink()
    window.field_reference.setText("RLY")
    assert not window._symbol_metadata_not_applied()


def test_a_symbol_draft_names_the_symbol_tab_as_the_remedy(window, monkeypatch, tmp_path):
    """«Applica ai file» cannot write a symbol with a draft pending: the reason names the remedy that can."""
    symbol = _with_symbol_file(window, monkeypatch, tmp_path)
    window.symbol_tab.table.item(1, 1).setText("HAND")      # an unapplied edit of the Symbol tab
    assert window.symbol_tab.has_pending_edits()
    window.field_reference.setText("RLY")
    window._refresh_output_actions()
    reason = window._install_block_reason()
    assert "scheda «Simbolo elettrico»" in reason
    window.btn_apply_metadata.click()
    assert _symbol_property(symbol, "Reference") != "RLY"   # the save refused the draft
    assert window._metadata_not_applied()
    # Its negative twin, no draft -> «Applica ai file», is the first test of this section.


# ── A5: estimated pads in the check and on the finished card ────────────────

def _package(estimated=True):
    """A two-pad THT package; its pads carry estimated hole, size and shape when `estimated`."""
    pins = [dict(number=str(n), x_mm=x, y_mm=0.0, pad_length_mm=2.0, pad_width_mm=2.0, pad_type="thru_hole",
                 shape="circle", drill_mm=1.0, **({"sources": dict(ESTIMATED)} if estimated else {}))
            for n, x in ((1, -2.54), (2, 2.54))]
    return package_from_mapping(dict(name=NAME, family="custom", body_length_mm=8.0, body_width_mm=4.0,
                                     pins=pins))


def test_the_check_receives_the_estimated_pads_of_the_applied_footprint(window):
    window.package_geometry = _package(estimated=True)
    findings = window._applied_estimated_pad_findings()
    assert findings and set(findings[0].pads) == {0, 1}
    assert window._estimated_pad_count() == 2
    assert window._component_check_args()["estimated_pads"] == findings
    # Negative: pads read from the drawing (no estimate mark) -> nothing to pass.
    window.package_geometry = _package(estimated=False)
    assert window._applied_estimated_pad_findings() == []
    assert window._component_check_args()["estimated_pads"] == []


def _held_card(window, *, footprint_written=True):
    """Hold the finished card of a single-part generation, as `_on_generation_finished` does."""
    overlay = window.analysis_overlay
    overlay.start("Generazione di footprint e modello STEP")
    window._show_generation_steps(window.package_geometry, None, single_component=True)
    window._hold_completed_overlay("Completato: footprint generato.", footprint_written=footprint_written)
    return overlay


def test_the_finished_card_names_the_estimated_pads_and_stays_completed(window):
    window.package_geometry = _package(estimated=True)
    overlay = _held_card(window)
    try:
        assert overlay.is_holding_completed()
        assert "2 pad con valori stimati" in overlay.steps_plain_text()
        assert overlay.status_plain_text().startswith("✓ Completato")
    finally:
        overlay._close_completed()


def test_no_estimate_line_without_estimated_pads_or_without_a_footprint_write(window):
    window.package_geometry = _package(estimated=False)
    overlay = _held_card(window)
    try:
        assert "stimati" not in overlay.steps_plain_text()
    finally:
        overlay._close_completed()
    # A STEP regeneration writes no footprint: no line even with estimates.
    window.package_geometry = _package(estimated=True)
    overlay = _held_card(window, footprint_written=False)
    try:
        assert "stimati" not in overlay.steps_plain_text()
    finally:
        overlay._close_completed()


# ── P2: the STEP «older than the footprint» warning ─────────────────────────

def _paths(folder):
    return (folder / f"{NAME}.pretty" / f"{NAME}.kicad_mod",
            folder / f"{NAME}.3dshapes" / f"{NAME}.step")


def _make_step_older(folder):
    footprint, step = _paths(folder)
    stamp = footprint.stat().st_mtime
    os.utime(step, (stamp - 100, stamp - 100))


def test_a_metadata_apply_does_not_call_the_step_older(window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    folder = _project(window, monkeypatch, tmp_path)
    _enable_3d(window)
    window._regenerate_3d()
    assert window._step_source_recorded()
    window.field_description.setText("Un'altra descrizione")
    window._apply_metadata_to_files()
    _make_step_older(folder)
    window._refresh_output_actions()
    assert "più vecchio" not in window.label_kicad_status.text()
    assert "Modello STEP non aggiornato" not in window.label_kicad_status.text()


def test_a_body_change_with_only_the_footprint_rewritten_is_flagged(window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    folder = _project(window, monkeypatch, tmp_path)
    _enable_3d(window)
    window._regenerate_3d()
    window.field_body_length.setValue(12.0)
    assert window._generate(footprint_only=True)
    _make_step_older(folder)
    window._refresh_output_actions()
    assert "Modello STEP non aggiornato" in window.label_kicad_status.text()


def test_without_the_source_record_the_older_step_still_warns(window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    """A project saved before the record (or a STEP written outside): the declared heuristic stays."""
    folder = _project(window, monkeypatch, tmp_path)
    _enable_3d(window)
    window._regenerate_3d()
    window._step_source_body = None
    _make_step_older(folder)
    window._refresh_output_actions()
    assert "più vecchio" in window.label_kicad_status.text()


# ── P3: the install report in step 3 ────────────────────────────────────────

def _result(tmp_path, **fields):
    """A LibraryInstallResult-like record of an installation into `tmp_path/dest`."""
    dest = tmp_path / "dest"
    base = dict(identifier=f"KiCadGen:{NAME}", footprint_path=dest / "KiCadGen.pretty" / f"{NAME}.kicad_mod",
                step_path=dest / "KiCadGen.3dshapes" / f"{NAME}.step", wrl_path=None,
                table_path=dest / "fp-lib-table", replaced=(), removed=(), rollback_left=None,
                symbol_library_path=dest / "KiCadGen.kicad_sym", symbol_table_path=dest / "sym-lib-table",
                symbol_skipped=None, symbol_replaced=False, symbol_left_stale=False, unchanged=(),
                symbol_identical=False, bom_removed=(),
                # CONTRACT CHANGED 2026-11-15 (round 3): the installation
                # declares whether it wrote a table row; an undeclared flag
                # is read as «written», so the stand-in declares both.
                table_changed=False, symbol_table_changed=False)
    base.update(fields)
    return SimpleNamespace(**base)


def test_the_report_of_a_new_install_is_short_in_step_3_with_paths_behind_the_toggle(window, tmp_path):
    result = _result(tmp_path)
    window._last_install_report = (result, True)
    window._refresh_install_report()
    summary = window.label_install_report.text()
    assert not window.label_install_report.isHidden()
    assert summary == f"Installato KiCadGen:{NAME} nella libreria di progetto."
    assert str(result.footprint_path) not in summary
    assert window.label_install_report_detail.isHidden()
    window.install_report_toggle.setChecked(True)
    assert not window.label_install_report_detail.isHidden()
    assert str(result.footprint_path) in window.label_install_report_detail.text()
    assert window.install_report_toggle.text() == "Nascondi percorsi completi"
    # The report is not at the top of step 1 any more.
    assert str(result.footprint_path) not in window.label_kicad_status.text()


def test_a_project_switch_forgets_the_install_report(window, tmp_path):
    window._last_install_report = (_result(tmp_path), True)
    window._refresh_install_report()
    window._switch_project("Altro", window._general_project_state)
    assert window.label_install_report.isHidden() and window.install_report_toggle.isHidden()


def test_identical_files_say_nothing_was_written(window, tmp_path):
    files = _result(tmp_path)
    identical = _result(tmp_path, unchanged=(files.footprint_path, files.step_path), symbol_identical=True)
    assert window._install_wrote_nothing(identical)
    assert window._install_report_summary(identical, True) == "Già installato e identico: niente da scrivere."
    assert window._install_result_message(identical).startswith("Già installato e identico")
    # CONTRACT CHANGED 2026-11-15 (round 2, T4): nothing written -> only the
    # identical sentence and where the component is, never «Installato …».
    message = window._install_result_message(identical)
    assert "Installato" not in message and "Tabella librerie" not in message
    assert f"KiCadGen:{NAME}" in message and str(files.footprint_path) in message
    assert str(files.symbol_library_path) in message
    # Negative twin: a written install still says «Installato …» with its paths.
    written = window._install_result_message(files)
    assert written.startswith(f"Installato KiCadGen:{NAME}") and "Già installato" not in written
    # Negatives: a file written, a symbol rewritten, a BOM dropped -> something was written.
    assert not window._install_wrote_nothing(_result(tmp_path, unchanged=(files.footprint_path,),
                                                     symbol_identical=True))
    assert not window._install_wrote_nothing(_result(tmp_path, unchanged=identical.unchanged))
    assert not window._install_wrote_nothing(_result(tmp_path, unchanged=identical.unchanged,
                                                     symbol_identical=True,
                                                     bom_removed=(files.table_path,)))
    assert not window._install_wrote_nothing(_result(tmp_path))


def test_a_dropped_bom_is_said_once_per_file(window, tmp_path):
    files = _result(tmp_path)
    result = _result(tmp_path, bom_removed=(files.symbol_library_path, files.symbol_table_path))
    lines = window._install_bom_lines(result)
    assert lines[0] == window.i18n.text(window.INSTALL_BOM_REMOVED_TEXT)
    assert str(files.symbol_table_path) in lines[1] and "tabella" in lines[1]
    summary = window._install_report_summary(result, True)
    assert all(line in summary for line in lines)
    assert window._install_bom_lines(_result(tmp_path)) == []


def test_reinstalling_the_same_files_asks_nothing_and_says_identical(window, tmp_path, monkeypatch):
    """End to end with the real installer: the second install of unchanged files writes nothing."""
    from test_kicad_symbol_install_gui import _answer, _project as _kicad_project, _symbol_file
    from test_photo_inventory_gui import _write_complete_kicad_output
    _write_complete_kicad_output(window)
    _symbol_file(window)
    project = _kicad_project(tmp_path)
    seen = _answer(monkeypatch, project, QMessageBox.StandardButton.Yes)
    window._install_project_library()
    assert window.label_install_report.text().startswith("Installato KiCadGen:")
    seen["asked"].clear()
    window._install_project_library()
    assert seen["asked"] == []
    assert window.label_install_report.text().startswith("Già installato e identico")


# ── Costs: the fallback of the finished card follows the language ───────────

def test_the_finished_card_cost_fallback_follows_a_language_switch(window, monkeypatch):
    from kicadgen.services.openrouter import UsageTotals
    monkeypatch.setattr(window, "_project_dir_or_none", lambda: None)   # no cost file: the fallback
    totals = UsageTotals(calls=2, prompt_tokens=10, completion_tokens=5, cost_usd=0.12,
                         by_model={"a/one": [0.08, 1, 0], "b/two": [0.04, 1, 0]})
    window._last_analysis_cost = totals
    window.package_geometry = _package(estimated=False)
    overlay = window.analysis_overlay
    overlay.start("Generazione di footprint e modello STEP")
    window._show_generation_steps(window.package_geometry, None, single_component=True)
    window._hold_completed_overlay("Completato: footprint generato.", analysis_cost=True)
    italian = (overlay.cost.text(), overlay.cost_models.text())
    assert italian[0] == window._usage_cost_parts(totals)[0]
    try:
        window.language_combo.setCurrentIndex(window.language_combo.findData("en"))
        english = window._usage_cost_parts(totals)
        assert overlay.cost.text() == english[0] != italian[0]
        assert overlay.cost_models.text() == "\n".join(english[1])
    finally:
        window.language_combo.setCurrentIndex(window.language_combo.findData("it"))
        overlay._close_completed()


def test_old_composed_cost_records_are_shown_as_written(window, monkeypatch):
    """Negative twin: a tuple of composed lines (older callers) is not recomposed."""
    monkeypatch.setattr(window, "_project_dir_or_none", lambda: None)
    window._last_analysis_cost = ("Costo ultima analisi: 0.0500 USD", ["m: 0.0500 USD (1)"])
    summary = window._analysis_cost_for_card()
    assert summary == ("Costo ultima analisi: 0.0500 USD", ["m: 0.0500 USD (1)"], [], False)


# ── Log: no per-body line twice ────────────────────────────────────────────

def test_the_consult_before_the_review_logs_its_summary_not_each_body(window, monkeypatch):
    from kicadgen.cad3d import kicad_models
    logged = []
    monkeypatch.setattr(window, "_append_log", lambda message, **_k: logged.append(str(message)))
    monkeypatch.setattr(window, "_is_single_component_project", lambda: False)
    window._photo_rows = [{"name": "U1"}, {"name": "U2"}]
    monkeypatch.setattr(window, "_attach_accepted_component_images", lambda rows: ([dict(r) for r in rows], 0))

    # CONTRACT CHANGED 2026-11-15 (round 2): the fake follows the real
    # prepass -- a body's record is attached right before its line -- since
    # the window tells a per-body line from an error by that record
    # (`main_window._PrepassNotes`), never by the text.
    def prepass(components, library, note, cancel):
        for component in components:
            note("Archivio abbinamenti accettati ignorato: bloccato")
            component['consolidated_match'] = {'name': component['name'], 'status': 'unavailable'}
            note(f"Consolidati — {component['name']}: nessuna voce")
        return {}
    monkeypatch.setattr(kicad_models, "_archive_prepass", prepass)
    window._consult_consolidated_before_review()
    assert not any(line.startswith("Consolidati — ") for line in logged)
    # Positive twin: the consult still says what it did, once.
    assert sum(line.startswith("Consolidati prima della revisione:") for line in logged) == 1
    # Round 2: an error of the archive is not a per-body line -- it reaches
    # the log, once although every body repeats it.
    assert logged.count("Archivio abbinamenti accettati ignorato: bloccato") == 1


# ── Naming: «Apri in KiCad» everywhere ─────────────────────────────────────

def test_the_preview_command_has_one_name(window):
    import inspect
    assert window.action_test_kicad.text() == "Apri in KiCad..."
    assert "«Apri in KiCad»" in window.PREVIEW_SCHEMATIC_SKIP_TEXTS["schematic_locked"]
    assert "Prova in KiCad" not in inspect.getsource(gui)


# ── Relay designator proposal from the declared pin functions (C7) ─────────

RELAY = [{"pin": "1", "function": "coil"}, {"pin": "2", "function": "coil"},
         {"pin": "3", "function": "common"}, {"pin": "4", "function": "no"}]


def test_declared_coil_and_contact_propose_k():
    assert gui.proposed_reference("single_tht", "dip", [row["function"] for row in RELAY]) == "K"
    # Negatives: a coil alone, other functions or nothing declared keep the family's proposal.
    assert gui.proposed_reference("single_tht", "dip", ["coil", "coil"]) == "U"
    assert gui.proposed_reference("single_tht", "dip", ["other", None]) == "U"
    assert gui.proposed_reference("single_tht", "dip", None) == "U"


def test_the_window_proposes_k_for_a_relay_and_keeps_a_typed_designator(window):
    window.pinout = [dict(row) for row in RELAY]
    window.field_reference.setText("")
    assert window._effective_reference() == "K"
    window._refresh_metadata_proposals()
    assert window.field_reference.placeholderText() == "K"
    # A typed designator is the user's: never replaced; the KLC advises «K» as a warning.
    window.field_reference.setText("U")
    assert window._effective_reference() == "U"
    codes = {(finding.code, finding.severity) for finding in window._klc_findings()}
    assert ("reference_convention", "warning") in codes
    # Negative: no declared functions -> «U» proposed, no convention warning.
    window.pinout = [{"pin": "1"}, {"pin": "2"}]
    window.field_reference.setText("")
    assert window._effective_reference() == "U"
    assert "reference_convention" not in {finding.code for finding in window._klc_findings()}
