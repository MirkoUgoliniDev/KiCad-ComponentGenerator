"""@brief Round 2 of the 2026-11-15 review in the window: each rule with a positive and a negative case.

- R3: every metadata property of the symbol file is read back (Reference,
  Description, ki_keywords, Datasheet, ki_fp_filters): a symbol the last
  «Applica ai file» could not rewrite (a Symbol-tab draft, a save error, a
  stale CAD result) keeps the metadata «not applied», and the install
  block names the cause.
- R6: the estimated-pad findings and the pad numbers given to the check come
  from ONE build of the applied footprint.
- R7: the STEP source record vouches only for the STEP file it was made for
  (size and SHA-256); a STEP put back by hand falls back to the mtime warning.
- `_LiveText` survives copy, deepcopy and pickle.
- The quiet archive consult before the review drops the per-body lines only:
  an error of the archive still reaches the log.

Offline: no AI, no kicad-cli, no FreeCAD (the workers are faked).
"""
import copy
import os
import pickle

import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("pyvistaqt")

from kicadgen.gui import main_window as gui  # noqa: E402
from tests.test_generated_files_alignment import NAME, _enable_3d, _project, dialogs  # noqa: E402,F401
from tests.test_main_window_c5_review import _package, _symbol_property, _with_symbol_file  # noqa: E402


# ── R3: the symbol file's metadata ──────────────────────────────────────────

def test_a_description_change_applied_normally_reaches_the_symbol_file(window, monkeypatch, tmp_path):
    """R3 negative: no draft, the save succeeds -> the file holds the value, «già nei file»."""
    symbol = _with_symbol_file(window, monkeypatch, tmp_path)
    window.field_description.setText("Nuova descrizione")
    window.field_tags.setText("nuove parole")
    window.field_datasheet.setText("https://example.org/nuovo.pdf")
    window._refresh_output_actions()
    assert window._metadata_not_applied()
    window.btn_apply_metadata.click()
    assert _symbol_property(symbol, "Description") == "Nuova descrizione"
    assert _symbol_property(symbol, "ki_keywords") == "nuove parole"
    assert _symbol_property(symbol, "Datasheet") == "https://example.org/nuovo.pdf"
    window._refresh_output_actions()
    assert not window._metadata_not_applied()
    assert window.label_metadata_apply_state.text() == "I metadati sono già nei file."
    assert window._install_block_reason() == ""


@pytest.mark.parametrize("field, prop, value", [
    ("field_description", "Description", "Altra descrizione"),
    ("field_tags", "ki_keywords", "altre parole"),
    ("field_datasheet", "Datasheet", "https://example.org/altro.pdf"),
])
def test_each_symbol_property_is_read_back(window, monkeypatch, tmp_path, field, prop, value):
    """R3 positive per property: the footprint snapshot matches, the symbol file does not -> not applied."""
    symbol = _with_symbol_file(window, monkeypatch, tmp_path)
    getattr(window, field).setText(value)
    # The footprint is declared to carry the new value: only the symbol file is behind.
    window._note_metadata_written(window._metadata_snapshot())
    assert _symbol_property(symbol, prop) != value
    assert window._symbol_metadata_not_applied() and window._metadata_not_applied()


def test_an_empty_keyword_field_matches_the_property_the_writer_leaves_out(window, monkeypatch, tmp_path):
    """R3: an empty value is no property at all for the writer; that is «applied», not stale."""
    symbol = _with_symbol_file(window, monkeypatch, tmp_path)
    window.field_tags.setText("")
    window.btn_apply_metadata.click()
    assert _symbol_property(symbol, "ki_keywords") is None
    assert not window._symbol_metadata_not_applied()
    # Positive twin: a value typed again is stale until applied.
    window.field_tags.setText("di nuovo")
    assert window._symbol_metadata_not_applied()


def test_a_symbol_draft_keeps_a_description_change_unapplied_and_names_the_symbol_tab(
        window, monkeypatch, tmp_path):
    """R3 positive: «Applica ai file» with a Symbol-tab draft rewrites the footprint only."""
    symbol = _with_symbol_file(window, monkeypatch, tmp_path)
    logged = []
    real_log = window._append_log
    monkeypatch.setattr(window, "_append_log", lambda message, **k: (logged.append(str(message)),
                                                                     real_log(message, **k)))
    window.symbol_tab.table.item(1, 1).setText("HAND")      # an unapplied edit of the Symbol tab
    assert window.symbol_tab.has_pending_edits()
    window.field_description.setText("Descrizione con bozza")
    window.btn_apply_metadata.click()
    assert _symbol_property(symbol, "Description") != "Descrizione con bozza"
    window._refresh_output_actions()
    assert window._metadata_not_applied()
    assert window.label_metadata_apply_state.text() == "Metadati modificati: non sono ancora nei file."
    reason = window._install_block_reason()
    assert "«Simbolo elettrico»" in reason and "«Applica»" in reason
    assert not window.btn_export.isEnabled()
    # The log says why the symbol was not rewritten, not «footprint e simbolo in rigenerazione».
    assert not any("footprint e simbolo in rigenerazione" in line for line in logged)
    assert any("Il simbolo non viene riscritto" in line for line in logged)


def test_a_failed_symbol_save_names_its_error(window, monkeypatch, tmp_path):
    """R3 positive: the save raised -> the reason carries the error; a later good save clears it."""
    from kicadgen.generators import symbol as symbol_module
    symbol = _with_symbol_file(window, monkeypatch, tmp_path)
    real_export = symbol_module.export_symbol

    def refused(*_a, **_k):
        raise OSError("disco pieno")
    monkeypatch.setattr(symbol_module, "export_symbol", refused)
    window.field_description.setText("Descrizione non scritta")
    window.btn_apply_metadata.click()
    assert _symbol_property(symbol, "Description") != "Descrizione non scritta"
    assert window._metadata_not_applied()
    reason = window._install_block_reason()
    assert "disco pieno" in reason and "«Applica ai file»" in reason
    # Negative twin: the next save succeeds -> nothing recorded, «già nei file».
    monkeypatch.setattr(symbol_module, "export_symbol", real_export)
    window.btn_apply_metadata.click()
    window._refresh_output_actions()
    assert window._symbol_save_skip is None
    assert not window._metadata_not_applied() and window._install_block_reason() == ""


def test_a_stale_cad_result_leaves_the_symbol_old_and_says_why(window, monkeypatch, tmp_path):
    """R3 positive: `_cad_result_is_current()` False -> the symbol is not rewritten, the reason says so."""
    symbol = _with_symbol_file(window, monkeypatch, tmp_path)
    monkeypatch.setattr(window, "_cad_result_is_current", lambda: False)
    window.field_description.setText("Descrizione superata")
    window.btn_apply_metadata.click()
    assert _symbol_property(symbol, "Description") != "Descrizione superata"
    assert window._metadata_not_applied()
    assert "parametri ormai cambiati" in window._install_block_reason()


def test_a_project_switch_forgets_why_the_symbol_was_not_rewritten(window):
    window._note_symbol_save(window.SYMBOL_SKIP_ERROR, "disco pieno")
    window._switch_project("Altro", window._general_project_state)
    assert window._symbol_save_skip is None


# ── R6: one build for the findings and the pad numbers ──────────────────────

def test_the_check_gets_the_pad_numbers_of_the_same_footprint_as_the_findings(window, monkeypatch):
    window.package_geometry = _package(estimated=True)
    builds = []
    real_build = window._build_footprint

    def counted(host):
        footprint = real_build(host)
        builds.append(footprint)
        return footprint
    monkeypatch.setattr(window, "_build_footprint", counted)
    findings, numbers = window._applied_estimated_pads()
    assert len(builds) == 1                     # one Footprint gives both values
    assert numbers == [pad.number for pad in builds[0].pads] == ["1", "2"]
    assert findings and set(findings[0].pads) == {0, 1}
    # The check receives exactly that pair.
    args = window._component_check_args()
    assert args["estimated_pad_numbers"] == numbers and args["estimated_pads"] == findings
    # Negative: inputs that cannot build a footprint -> neither value.
    monkeypatch.setattr(window, "_footprint_inputs_now", lambda: (_ for _ in ()).throw(ValueError("x")))
    args = window._component_check_args()
    assert args["estimated_pads"] is None and args["estimated_pad_numbers"] is None


# ── R7: the STEP record is tied to its file ─────────────────────────────────

def _step_paths(folder):
    return (folder / f"{NAME}.pretty" / f"{NAME}.kicad_mod",
            folder / f"{NAME}.3dshapes" / f"{NAME}.step")


def _make_older(footprint, step):
    stamp = footprint.stat().st_mtime
    os.utime(step, (stamp - 100, stamp - 100))


def test_a_step_put_back_by_hand_is_not_vouched_for_by_the_record(window, monkeypatch, tmp_path, dialogs):
    """R7 positive: other bytes, an old mtime, records still matching the parameters -> the warning stays."""
    folder = _project(window, monkeypatch, tmp_path)
    _enable_3d(window)
    window._regenerate_3d()
    assert window._step_source_recorded()
    footprint, step = _step_paths(folder)
    step.write_bytes(b"ISO-10303-21;\n/* a backup of another build */\nEND-ISO-10303-21;\n")
    _make_older(footprint, step)
    window._refresh_output_actions()
    assert not window._step_source_recorded()
    assert "più vecchio" in window.label_kicad_status.text()


def test_the_same_step_with_an_older_mtime_is_still_vouched_for(window, monkeypatch, tmp_path, dialogs):
    """R7 negative: same bytes (only the time moved) -> the record vouches, no warning."""
    folder = _project(window, monkeypatch, tmp_path)
    _enable_3d(window)
    window._regenerate_3d()
    footprint, step = _step_paths(folder)
    _make_older(footprint, step)
    window._refresh_output_actions()
    assert window._step_source_recorded()
    assert "più vecchio" not in window.label_kicad_status.text()


def test_a_record_without_the_file_identity_falls_back_to_the_mtime_heuristic(
        window, monkeypatch, tmp_path, dialogs):
    """R7: a project saved before the identity existed -> the declared heuristic, as before the record."""
    folder = _project(window, monkeypatch, tmp_path)
    _enable_3d(window)
    window._regenerate_3d()
    window._step_source_file = None
    _make_older(*_step_paths(folder))
    window._refresh_output_actions()
    assert not window._step_source_recorded()
    assert "più vecchio" in window.label_kicad_status.text()


def test_the_step_identity_is_saved_with_the_project_and_validated_on_load(
        window, monkeypatch, tmp_path, dialogs):
    _project(window, monkeypatch, tmp_path)
    _enable_3d(window)
    window._regenerate_3d()
    saved = window._project_state()["step_source_file"]
    assert saved == window._step_source_file and len(saved["sha256"]) == 64
    assert gui._valid_step_file_identity(saved) == saved
    # Damaged or absent records are «no identity», never a match.
    assert gui._valid_step_file_identity({"size": -1, "sha256": saved["sha256"]}) is None
    assert gui._valid_step_file_identity({"size": 3, "sha256": "xyz"}) is None
    assert gui._valid_step_file_identity(None) is None


# ── _LiveText is copy-safe ──────────────────────────────────────────────────

def test_live_text_survives_copy_deepcopy_and_pickle():
    language = {"text": "uno"}
    live = gui._LiveText(lambda: language["text"])
    assert copy.copy(live) == "uno" and copy.copy(live)() == "uno"
    assert copy.deepcopy(live) == "uno" and copy.deepcopy([live])[0]() == "uno"
    language["text"] = "one"
    assert copy.deepcopy(live)() == "one"            # the copy still composes again
    restored = pickle.loads(pickle.dumps(live))
    assert restored == "uno" and type(restored) is str   # the text only: a recipe cannot be pickled


# ── The archive consult before the review: errors pass, body lines do not ───

def test_the_quiet_consult_passes_archive_errors_and_drops_body_lines(monkeypatch):
    """With the real `_archive_prepass`: the archive refuses every lookup."""
    from kicadgen.cad3d import kicad_models

    def broken(component, roots, note, diagnostic=None):
        diagnostic['error'] = "archivio illeggibile"
        note("Archivio abbinamenti accettati ignorato: archivio illeggibile")
        return None
    monkeypatch.setattr(kicad_models, "_accepted_match", broken)
    monkeypatch.setattr(kicad_models, "_search_roots", lambda library: [tmp_root()])
    monkeypatch.setattr(kicad_models, "archive_can_match", lambda component: True)
    components = [{"name": "U1"}, {"name": "U2", "consolidated_match": {"status": "old"}}]
    logged = []
    kicad_models._archive_prepass(components, None, gui._PrepassNotes(components, logged.append), None)
    assert logged == ["Archivio abbinamenti accettati ignorato: archivio illeggibile"]
    # Every body got its record (its line was the one dropped).
    assert all(item["consolidated_match"]["status"] == "unavailable" for item in components)


def tmp_root():
    """A search root the faked lookup never reads."""
    from pathlib import Path
    return Path(".")


# ── The status bar follows a language switch (from R2-P) ────────────────────

def test_the_status_bar_ready_message_follows_a_language_switch(window):
    """«Pronto» is shown again in the new language (`_on_language_changed`); and back in Italian."""
    window.statusBar().showMessage(window.i18n.text("Pronto"))
    try:
        window.language_combo.setCurrentIndex(window.language_combo.findData("en"))
        english = window.statusBar().currentMessage()
        assert english == window.i18n.text("Pronto") and english != "Pronto"
        window.language_combo.setCurrentIndex(window.language_combo.findData("it"))
        assert window.statusBar().currentMessage() == "Pronto"
    finally:
        window.language_combo.setCurrentIndex(window.language_combo.findData("it"))
