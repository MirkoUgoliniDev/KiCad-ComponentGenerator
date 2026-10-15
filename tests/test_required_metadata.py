"""@brief Required metadata block the installation (user, 2026-10-15).

«Se ci sono campi obbligatori allora deve essere inibita l'installazione
finché non tutti i campi sono compilati». Required: designator, description,
keywords, datasheet; the footprint filters stay optional. While one is empty
(blanks count as empty) the KLC list shows a ⛔ naming it, both installation
commands (buttons and «Strumenti» menu entries) are disabled with a tooltip
naming the missing fields, and every install method refuses on its own
without an «install anyway» question. «Applica ai file» and the isolated
preview stay enabled. The «Metadati del componente» box opens by itself on a
transition into the blocked state, never on every keystroke.

Every rule has a positive case (filled -> allowed) and a negative one
(emptied -> blocked) that differ only in the field under test.
"""
import re
from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QFileDialog, QMessageBox

from kicadgen.services import klc_checks
from tests.test_photo_inventory_gui import gui

KICAD = gui.MainWindow.KICAD_TAB_INDEX
NAME = "SCHEDA"
INSTALL_ATTRS = ("btn_export", "btn_install_global", "action_export", "action_install_global")
#: Field key -> (editor attribute, Italian label shown in the messages).
REQUIRED = {
    "description": ("field_meta_description", "Descrizione"),
    "keywords": ("field_meta_keywords", "Parole chiave"),
    "datasheet": ("field_datasheet", "Datasheet (URL o file)"),
}


def _write_outputs(root, name=NAME, *, window):
    """A valid footprint (attribute + the pads the fields generate) and a STEP: the install commands' file gate is open.

    CONTRACT CHANGED 2026-10-15 (audit H1): the installation is refused while the
    .kicad_mod pads differ from those the window's fields generate, so the
    footprint written here carries exactly those pads (`minimal_aligned_footprint`).
    """
    from tests.test_photo_inventory_gui import minimal_aligned_footprint
    pretty = root / name / f"{name}.pretty"
    shapes = root / name / f"{name}.3dshapes"
    pretty.mkdir(parents=True, exist_ok=True)
    shapes.mkdir(parents=True, exist_ok=True)
    (pretty / f"{name}.kicad_mod").write_text(minimal_aligned_footprint(window, name), encoding="utf-8")
    (shapes / f"{name}.step").write_bytes(b"ISO-10303-21;\nEND-ISO-10303-21;\n")


def _fill(window):
    window.field_meta_description.setText("Modulo radio")
    window.field_meta_keywords.setText("radio modulo")
    window.field_datasheet.setText("https://example.org/ds.pdf")


def _mark_written(window):
    """Record the current fields as written into the footprint, as `_on_generated` does."""
    window._note_metadata_written(window._metadata_snapshot())


def _open(window, tmp_path, *, filled=True, name=NAME):
    """Project with complete outputs; the required fields filled or all three emptied.

    The window used to start with keywords «pin header THT» (empty since
    2026-10-15); «not filled» still empties every field explicitly instead of
    trusting the defaults.
    """
    if filled:
        _fill(window)
    else:
        for editor, _label in REQUIRED.values():
            getattr(window, editor).setText("")
    # The files written below stand for a generation made with these values
    # (review pass 2, M1: installation waits for the fields to be applied).
    _mark_written(window)
    window.field_library_root.setText(str(tmp_path))
    _write_outputs(tmp_path, name, window=window)
    window.field_name.setText(name)
    window._refresh_output_actions()
    assert window.tabs.isTabEnabled(KICAD)


def _install_enabled(window):
    return {attr: getattr(window, attr).isEnabled() for attr in INSTALL_ATTRS}


def _items(window):
    return [window.klc_list.item(i).text() for i in range(window.klc_list.count())]


@pytest.fixture
def no_dialogs(monkeypatch):
    """Record every dialog an installation could open; none may be answered."""
    seen = {"folder": [], "question": [], "warning": [], "installed": []}
    monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                        lambda *a, **k: seen["folder"].append(a) or "")
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: seen["question"].append(a) or QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: seen["warning"].append(a))
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(gui, "install_component_library",
                        lambda *a, **k: seen["installed"].append(k) or SimpleNamespace(
                            identifier="KiCadGen:" + NAME, footprint_path="fp", table_path="t"))
    monkeypatch.setattr(gui, "record_kicad_export", lambda **k: True)
    return seen


# ── Positive: everything required is filled ─────────────────────────────

def test_all_required_fields_filled_enable_both_installations(window, tmp_path):  # noqa: F811
    _open(window, tmp_path)
    assert all(_install_enabled(window).values()), _install_enabled(window)
    assert window._missing_required_metadata() == []
    assert not [item for item in _items(window) if item.startswith("⛔")], _items(window)
    assert "Installazione bloccata" not in window.btn_export.toolTip()
    assert "Crea KiCadGen.pretty" in window.btn_export.toolTip()


def test_empty_footprint_filters_never_block(window, tmp_path, monkeypatch):  # noqa: F811
    """The filters are optional: empty field (proposal) and empty effective value both pass."""
    _open(window, tmp_path)
    window.field_fp_filters.setText("")
    assert all(_install_enabled(window).values())
    monkeypatch.setattr(window, "_effective_fp_filters", lambda: "")
    window._refresh_klc_findings()
    assert any(item.startswith("⚠") for item in _items(window)), "the empty filter is advice"
    assert all(_install_enabled(window).values())


def test_the_designator_proposal_counts_as_filled(window, tmp_path):  # noqa: F811
    """Positive: an empty designator FIELD follows its proposal («U»), which is
    the value written in the files, so it does not block."""
    _open(window, tmp_path)
    window.field_reference.setText("")
    assert window._effective_reference() == "U"
    assert all(_install_enabled(window).values())


# ── Negative: one required field empty ──────────────────────────────────

@pytest.mark.parametrize("blank", ["", "   "])
@pytest.mark.parametrize("key", sorted(REQUIRED))
def test_one_empty_required_field_blocks_both_installations(window, tmp_path, key, blank):  # noqa: F811
    _open(window, tmp_path)
    editor, label = REQUIRED[key]
    getattr(window, editor).setText(blank)
    assert window._missing_required_metadata() == [key]
    errors = [item for item in _items(window) if item.startswith("⛔")]
    assert len(errors) == 1 and "obbligatorio" in errors[0]
    assert not any(_install_enabled(window).values()), _install_enabled(window)
    for attr in INSTALL_ATTRS:
        tooltip = getattr(window, attr).toolTip()
        assert "Installazione bloccata" in tooltip and label in tooltip, (attr, tooltip)
    # The other required fields are not named: the tooltip is precise.
    for other, (_editor, other_label) in REQUIRED.items():
        if other != key:
            assert other_label not in window.btn_export.toolTip().split(":", 2)[-1]
    # Never blocked by the metadata: the isolated preview and «Applica ai file».
    assert window.btn_test_kicad.isEnabled() and window.action_test_kicad.isEnabled()
    assert window.btn_apply_metadata.isEnabled() and window.group_metadata.isEnabled()


def test_an_empty_effective_designator_blocks_too(window, tmp_path, monkeypatch):  # noqa: F811
    """Negative for the fourth required field: no typed value AND no proposal."""
    _open(window, tmp_path)
    monkeypatch.setattr(window, "_effective_reference", lambda: "")
    window._refresh_klc_findings()
    assert window._missing_required_metadata() == ["reference"]
    assert not any(_install_enabled(window).values())
    assert "Designatore di riferimento" in window.btn_install_global.toolTip()


def test_several_missing_fields_are_all_named_in_form_order(window, tmp_path):  # noqa: F811
    _open(window, tmp_path, filled=False)
    assert window._missing_required_metadata() == ["description", "keywords", "datasheet"]
    tooltip = window.action_export.toolTip()
    positions = [tooltip.index(label) for _editor, label in
                 (REQUIRED["description"], REQUIRED["keywords"], REQUIRED["datasheet"])]
    assert positions == sorted(positions)


def test_without_outputs_the_file_reason_wins(window, tmp_path):  # noqa: F811
    """Negative of the gate's scope: with no STEP the commands keep «Genera prima…»,
    the metadata reason never hides the missing file."""
    window.field_library_root.setText(str(tmp_path))
    _write_outputs(tmp_path, window=window)
    (tmp_path / NAME / f"{NAME}.3dshapes" / f"{NAME}.step").unlink()
    window.field_name.setText(NAME)
    window._refresh_output_actions()
    assert not any(_install_enabled(window).values())
    assert "Genera prima" in window.btn_export.toolTip()
    assert "Installazione bloccata" not in window.btn_export.toolTip()


# ── Live update ─────────────────────────────────────────────────────────

def test_filling_the_last_field_enables_and_emptying_one_disables_at_once(window, tmp_path):  # noqa: F811
    _open(window, tmp_path, filled=False)
    window.field_meta_description.setText("Modulo")
    window.field_meta_keywords.setText("modulo")
    assert not any(_install_enabled(window).values())
    window.field_datasheet.setText("d")          # the last one typed...
    # CONTRACT CHANGED 2026-10-15 (review pass 2, M1): filling the last field
    # no longer enables the installation by itself; the footprint on disk
    # was written with the fields empty, so «Applica ai file» is asked for.
    assert not any(_install_enabled(window).values())
    assert "«Applica ai file»" in window.btn_export.toolTip()
    _mark_written(window)                         # ...and written, as Applica does
    window._refresh_klc_findings()
    assert all(_install_enabled(window).values())
    assert "Installazione bloccata" not in window.btn_export.toolTip()
    window.field_meta_keywords.setText(" ")      # emptied: disabled on this edit
    assert not any(_install_enabled(window).values())
    # The project datum (field_tags) drives the same gate as the editor; the
    # applied value typed back lifts the block (values compared, no flag).
    window.field_tags.setText("modulo")
    assert all(_install_enabled(window).values())


# ── Defence in depth: the install methods refuse on their own ───────────

@pytest.mark.parametrize("entry", ["_install_project_library", "_install_global_library"])
def test_a_direct_call_is_refused_without_a_confirmation(window, tmp_path, no_dialogs, entry):  # noqa: F811
    _open(window, tmp_path)
    window.field_datasheet.setText("")
    window.group_metadata.setExpanded(False)
    getattr(window, entry)()
    assert no_dialogs["installed"] == []
    assert no_dialogs["question"] == [], "no «install anyway» question for missing fields"
    assert no_dialogs["folder"] == [], "refused before a folder is asked"
    assert len(no_dialogs["warning"]) == 1 and "Datasheet (URL o file)" in no_dialogs["warning"][0][2]
    assert window.group_metadata.isExpanded(), "the refusal shows the fields to fill"


def test_the_common_sink_refuses_too(window, tmp_path, no_dialogs):  # noqa: F811
    """A caller that skips the two entry points (wizard, future code) is refused by the sink."""
    _open(window, tmp_path)
    window.field_meta_description.setText("")
    artifacts = SimpleNamespace(name=NAME, component_dir=tmp_path / NAME)
    window._install_kicad_library(tmp_path / "dest", project_library=True, artifacts=artifacts)
    assert no_dialogs["installed"] == [] and no_dialogs["question"] == []
    assert len(no_dialogs["warning"]) == 1
    assert not (tmp_path / "dest").exists()


def test_the_disabled_menu_entries_do_nothing(window, tmp_path, no_dialogs):  # noqa: F811
    _open(window, tmp_path)
    window.field_meta_keywords.setText("")
    window.action_export.trigger()
    window.action_install_global.trigger()
    assert no_dialogs == {"folder": [], "question": [], "warning": [], "installed": []}


def test_with_the_fields_filled_the_direct_call_installs(window, tmp_path, no_dialogs):  # noqa: F811
    """Positive of the sink: same call, required fields filled -> installed, no question."""
    _open(window, tmp_path)
    artifacts = SimpleNamespace(name=NAME, component_dir=tmp_path / NAME)
    window._install_kicad_library(tmp_path / "dest", project_library=True, artifacts=artifacts)
    assert len(no_dialogs["installed"]) == 1
    assert no_dialogs["question"] == [] and no_dialogs["warning"] == []


def test_other_klc_errors_still_ask_but_missing_fields_never_do(window, tmp_path, no_dialogs, monkeypatch):  # noqa: F811
    """The confirmation of the other KLC errors is unchanged; a required-field
    error is never offered as «install anyway»."""
    _open(window, tmp_path)
    artifacts = SimpleNamespace(name=NAME, component_dir=tmp_path / NAME)
    other = klc_checks.Finding("reference_letters", "error", "Designatore errato")
    monkeypatch.setattr(klc_checks, "check_all", lambda *a, **k: [other])
    window._install_kicad_library(tmp_path / "dest", project_library=True, artifacts=artifacts)
    assert len(no_dialogs["question"]) == 1 and len(no_dialogs["installed"]) == 1
    required = klc_checks.Finding("datasheet_empty", "error", "Datasheet vuoto")
    monkeypatch.setattr(klc_checks, "check_all", lambda *a, **k: [other, required])
    window._install_kicad_library(tmp_path / "dest", project_library=True, artifacts=artifacts)
    assert len(no_dialogs["question"]) == 1, "no new question"
    assert len(no_dialogs["installed"]) == 1 and len(no_dialogs["warning"]) == 1


# ── «Applica ai file» stays usable ──────────────────────────────────────

def test_apply_to_files_writes_with_a_missing_required_field(window, tmp_path, monkeypatch):  # noqa: F811
    _open(window, tmp_path)
    window.field_meta_description.setText("")
    generated, warned = [], []
    monkeypatch.setattr(window, "_save_project_config", lambda **k: True)
    monkeypatch.setattr(window, "_generate", lambda **k: generated.append(k) or True)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a))
    window.btn_apply_metadata.click()
    assert generated == [{"footprint_only": True}] and warned == []


def test_apply_to_files_still_refuses_an_unwritable_designator(window, tmp_path, monkeypatch):  # noqa: F811
    """Negative: an empty designator cannot be written (the symbol exporter
    refuses it), so «Applica ai file» keeps refusing it as before."""
    _open(window, tmp_path)
    monkeypatch.setattr(window, "_effective_reference", lambda: "")
    generated, warned = [], []
    monkeypatch.setattr(window, "_generate", lambda **k: generated.append(k) or True)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a))
    window.btn_apply_metadata.click()
    assert generated == [] and len(warned) == 1


# ── The box opens on a transition only ──────────────────────────────────

def test_the_box_stays_collapsed_when_nothing_is_missing(window, tmp_path):  # noqa: F811
    _open(window, tmp_path)
    assert not window.group_metadata.isExpanded()


def test_the_box_opens_on_the_transition_and_does_not_fight_the_user(window, tmp_path):  # noqa: F811
    group = window.group_metadata
    _open(window, tmp_path, filled=False)
    assert group.isExpanded(), "the box opens when it is what blocks the installation"
    group.setExpanded(False)                      # the user closes it
    window.field_meta_description.setText("Modulo")   # still blocked: keystrokes
    window.field_meta_keywords.setText("m")
    window._refresh_output_actions()              # a tab refresh with the same state
    assert not group.isExpanded(), "no reopening while the state stays blocked"
    window.field_datasheet.setText("d")           # filled, not yet applied: still blocked
    assert not group.isExpanded()
    # CONTRACT CHANGED 2026-10-15 (review pass 2, M1): unblocked only once
    # the fields are written into the files.
    _mark_written(window)
    window._refresh_klc_findings()                # unblocked
    assert not group.isExpanded() and window.btn_export.isEnabled()
    window.field_datasheet.setText("")            # a NEW transition into blocked
    assert group.isExpanded()


def test_another_blocked_component_is_a_new_transition(window, tmp_path):  # noqa: F811
    group = window.group_metadata
    _open(window, tmp_path, filled=False)
    group.setExpanded(False)
    _write_outputs(tmp_path, "ALTRO", window=window)
    window.field_name.setText("ALTRO")
    window._refresh_output_actions()
    assert group.isExpanded()


# ── Language ────────────────────────────────────────────────────────────

def test_the_block_reason_follows_the_language(window, tmp_path):  # noqa: F811
    _open(window, tmp_path)
    window.field_meta_keywords.setText("")
    window._on_language_changed(window.language_combo.findData("en"))
    try:
        tooltip = window.btn_export.toolTip()
        assert "Installation blocked" in tooltip and "Keywords" in tooltip, tooltip
        errors = [item for item in _items(window) if item.startswith("⛔")]
        assert errors and "required field" in errors[0]
    finally:
        window._on_language_changed(window.language_combo.findData("it"))
    assert "Installazione bloccata" in window.btn_export.toolTip()


@pytest.mark.parametrize("language", ["it", "en", "fr", "de"])
def test_every_new_string_has_four_languages(language):
    """The catalogue entries of this feature exist in every language and keep the placeholder."""
    from kicadgen.services.i18n import DEFAULT_TRANSLATIONS
    sources = [
        "Installazione bloccata: compila i campi obbligatori in «Metadati del componente»: {fields}.",
        "Installazione bloccata: i metadati non sono ancora nei file del componente. "
        "Premi «Applica ai file» in «Metadati del componente».",
        "Designatore vuoto: campo obbligatorio, serve un prefisso come U, K o J.",
        "Descrizione vuota: campo obbligatorio, l’installazione resta bloccata finché manca.",
        "Parole chiave vuote: campo obbligatorio, l’installazione resta bloccata finché mancano.",
        "Datasheet vuoto: campo obbligatorio, indicare l’URL o il percorso del documento.",
    ]
    for source in sources:
        entry = DEFAULT_TRANSLATIONS["ui:" + source]
        assert entry[language].strip(), (source, language)
        assert ("{fields}" in entry[language]) == ("{fields}" in source)


# ── Review pass 2, M1: the rule holds on the INSTALLED files ─────────────

def _generated_project(window, monkeypatch):
    """A real package project: footprint written by the generator with the
    required metadata EMPTY, plus a STEP, so the installation is possible."""
    from tests.test_package_project_gui import acquire
    acquire(window)
    monkeypatch.setattr(gui.GenerateWorker, "start", lambda worker: worker.run())
    for editor, _label in REQUIRED.values():
        getattr(window, editor).setText("")
    assert window._generate(footprint_only=True)
    folder = window._project_dir()
    name = folder.name
    step = folder / f"{name}.3dshapes" / f"{name}.step"
    step.parent.mkdir(parents=True, exist_ok=True)
    step.write_bytes(b"ISO-10303-21;\nEND-ISO-10303-21;\n")
    window._refresh_output_actions()
    return folder / f"{name}.pretty" / f"{name}.kicad_mod"


def test_typed_but_not_applied_metadata_block_until_applica(window, tmp_path, monkeypatch, no_dialogs):  # noqa: F811
    """The reviewed scenario: generated with empty fields, then typed, never
    applied. Before the fix the gate lifted and the file without the
    datasheet was installed."""
    footprint = _generated_project(window, monkeypatch)
    assert not any(_install_enabled(window).values())     # missing fields
    _fill(window)
    assert "example.org" not in footprint.read_text(encoding="utf-8")
    # Negative: every required field filled, but not in the files.
    assert window._missing_required_metadata() == []
    assert not any(_install_enabled(window).values())
    for attr in INSTALL_ATTRS:
        assert "«Applica ai file»" in getattr(window, attr).toolTip(), attr
    window._install_project_library()
    window._install_global_library()
    assert no_dialogs["installed"] == [] and no_dialogs["question"] == []
    assert len(no_dialogs["warning"]) == 2 and "«Applica ai file»" in no_dialogs["warning"][0][2]
    # «Applica ai file» and the isolated preview stay enabled meanwhile.
    assert window.btn_apply_metadata.isEnabled() and window.btn_test_kicad.isEnabled()
    # Positive: «Applica ai file» writes them, the installation opens.
    window.btn_apply_metadata.click()
    assert "https://example.org/ds.pdf" in footprint.read_text(encoding="utf-8")
    assert all(_install_enabled(window).values())
    artifacts = gui.discover_component_artifacts(window._project_dir(), window._component_name(),
                                                 require_3d=True)
    window._install_kicad_library(tmp_path / "dest", project_library=True, artifacts=artifacts)
    assert len(no_dialogs["installed"]) == 1


def test_a_reverted_edit_is_neutral_and_another_value_blocks(window, monkeypatch):  # noqa: F811
    """Values are compared, not an «edited» flag: typing back the applied value lifts the block."""
    _generated_project(window, monkeypatch)
    _fill(window)
    window.btn_apply_metadata.click()
    assert all(_install_enabled(window).values())
    window.field_datasheet.setText("https://example.org/other.pdf")
    assert not any(_install_enabled(window).values())
    window.field_datasheet.setText("https://example.org/ds.pdf")
    assert all(_install_enabled(window).values())
    # A language change changes no value: still installable.
    window._on_language_changed(window.language_combo.findData("en"))
    try:
        assert all(_install_enabled(window).values())
    finally:
        window._on_language_changed(window.language_combo.findData("it"))


def test_a_failed_generation_applies_nothing(window, monkeypatch):  # noqa: F811
    """Negative: the snapshot is recorded only when the worker reports the file written."""
    _generated_project(window, monkeypatch)
    _fill(window)
    monkeypatch.setattr(gui.GenerateWorker, "start", lambda worker: worker.error.emit("guasto"))
    window.btn_apply_metadata.click()
    assert not any(_install_enabled(window).values())
    window._on_generated("direct call without a worker")   # no sender: records nothing
    assert not any(_install_enabled(window).values())


def test_the_applied_record_survives_the_project_file_and_old_projects_fail_closed(window, monkeypatch):  # noqa: F811
    _generated_project(window, monkeypatch)
    _fill(window)
    window.btn_apply_metadata.click()
    folder = window._project_dir()
    assert window._save_project_config(force=True)
    state = window._project_state()
    assert state["metadata_applied"] == {"description": "Modulo radio", "keywords": "radio modulo",
                                         "datasheet": "https://example.org/ds.pdf"}
    window._switch_project("Altro", window._general_project_state)
    window._open_project_at(folder)
    assert all(_install_enabled(window).values()), "reopened: the files still carry the fields"
    # A project saved before this check (no key) or with a damaged record:
    # «not applied», one «Applica ai file» re-establishes it.
    for record in (None, {"description": 3}, "junk"):
        old = dict(state)
        if record is None:
            del old["metadata_applied"]
        else:
            old["metadata_applied"] = record
        window._switch_project(folder.name, old)
        assert not any(_install_enabled(window).values()), record
        assert "«Applica ai file»" in window.btn_export.toolTip()


# ── Review pass 2, L5: the auto-open latch keys on the project folder ────

def test_a_regeneration_refresh_without_the_step_does_not_reopen_the_box(window, tmp_path):  # noqa: F811
    """Before the fix a refresh with footprint/STEP absent reset the latch, so
    the box reopened after it against a user who had closed it."""
    group = window.group_metadata
    _open(window, tmp_path, filled=False)
    assert group.isExpanded()
    group.setExpanded(False)
    step = tmp_path / NAME / f"{NAME}.3dshapes" / f"{NAME}.step"
    step.unlink()
    window._refresh_output_actions()               # STEP absent for a moment
    step.write_bytes(b"ISO-10303-21;\nEND-ISO-10303-21;\n")
    window._refresh_output_actions()               # back, still blocked
    assert not group.isExpanded()


def test_a_same_named_project_in_another_folder_is_a_new_context(window, tmp_path):  # noqa: F811
    """Before the fix the latch keyed on the component NAME: the same name in
    another working folder did not reopen the box."""
    group = window.group_metadata
    first, second = tmp_path / "uno", tmp_path / "due"
    _open(window, first, filled=False)
    assert group.isExpanded()
    group.setExpanded(False)
    _write_outputs(second, window=window)
    # As opening the homonymous project of the other working folder does:
    # the open project folder changes, the name stays the same.
    previous = window.field_library_root.blockSignals(True)
    window.field_library_root.setText(str(second))
    window.field_library_root.blockSignals(previous)
    window._open_project_folder = second / NAME
    window._refresh_output_actions()
    assert group.isExpanded()


# ── Review 2026-10-15, R2-M1: «Applica ai file» refuses only unwritable values ──

def test_a_footprint_without_version_is_repaired_by_applica(window, tmp_path, monkeypatch, no_dialogs):  # noqa: F811
    """The reviewed deadlock: footprint_version is an error whose remedy is
    «Applica ai file»; Apply refused it, the record never updated and the
    installation stayed disabled for good."""
    footprint = _generated_project(window, monkeypatch)
    _fill(window)
    text = footprint.read_text(encoding="utf-8")
    footprint.write_text(re.sub(r"\(version [^)]*\)", "", text, count=1), encoding="utf-8")
    window._refresh_klc_findings()
    assert "footprint_version" in {f.code for f in window._klc_findings()}
    window.btn_apply_metadata.click()
    assert no_dialogs["warning"] == [], "Apply must not be refused for a file it repairs"
    assert "(version" in footprint.read_text(encoding="utf-8")
    assert "footprint_version" not in {f.code for f in window._klc_findings()}
    assert all(_install_enabled(window).values())


@pytest.mark.parametrize("code", sorted(code for code, hint in klc_checks.FIX_HINTS.items()
                                        if "«Applica ai file»" in hint))
def test_every_remedy_that_names_applica_is_executable(window, tmp_path, monkeypatch, code):  # noqa: F811
    """Structural: a finding whose hint sends the user to «Applica ai file» never stops it."""
    _open(window, tmp_path)
    monkeypatch.setattr(klc_checks, "check_all",
                        lambda *a, **k: [klc_checks.Finding(code, "error", "m")])
    generated, warned = [], []
    monkeypatch.setattr(window, "_save_project_config", lambda **k: True)
    monkeypatch.setattr(window, "_generate", lambda **k: generated.append(k) or True)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a))
    window.btn_apply_metadata.click()
    assert generated == [{"footprint_only": True}] and warned == [], code


@pytest.mark.parametrize("code", sorted(klc_checks.WRITE_BLOCKING_CODES))
def test_only_unwritable_metadata_values_stop_applica(window, tmp_path, monkeypatch, code):  # noqa: F811
    """Negative: a value Apply cannot write (designator with digits, multi-line
    text…) still stops it, and its remedy is a field, not Apply."""
    assert "«Applica ai file»" not in klc_checks.FIX_HINTS[code]
    assert "«Metadati del componente»" in klc_checks.FIX_HINTS[code] or code == "metadata_type"
    _open(window, tmp_path)
    monkeypatch.setattr(klc_checks, "check_all",
                        lambda *a, **k: [klc_checks.Finding(code, "error", "m")])
    generated, warned = [], []
    monkeypatch.setattr(window, "_generate", lambda **k: generated.append(k) or True)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a))
    window.btn_apply_metadata.click()
    assert generated == [] and len(warned) == 1


@pytest.mark.parametrize("code", ["pin_grid", "symbol_version", "footprint_name_illegal"])
def test_symbol_and_name_errors_keep_the_install_confirmation(window, tmp_path, monkeypatch, no_dialogs, code):  # noqa: F811
    """Symbol and name errors do not stop Apply, so the installation is reached
    and asks its usual «Installare comunque?» (the contract of
    `_confirm_klc_errors`), instead of a hard block."""
    _open(window, tmp_path)
    monkeypatch.setattr(klc_checks, "check_all",
                        lambda *a, **k: [klc_checks.Finding(code, "error", "Errore " + code)])
    monkeypatch.setattr(window, "_save_project_config", lambda **k: True)
    monkeypatch.setattr(window, "_generate", lambda **k: True)
    window.btn_apply_metadata.click()
    assert no_dialogs["warning"] == []
    artifacts = SimpleNamespace(name=NAME, component_dir=tmp_path / NAME)
    window._install_kicad_library(tmp_path / "dest", project_library=True, artifacts=artifacts)
    assert len(no_dialogs["question"]) == 1 and "Errore " + code in no_dialogs["question"][0][2]
    assert len(no_dialogs["installed"]) == 1


def test_the_applica_refusal_names_exactly_the_blocking_findings(window, tmp_path, monkeypatch):  # noqa: F811
    """Review 2026-10-15, L-4: the list also holds errors that do not stop
    «Applica ai file»; the refusal names the ones that do, and only those."""
    _open(window, tmp_path)
    findings = [klc_checks.Finding("reference_letters", "error", "Designatore con cifre"),
                klc_checks.Finding("description_multiline", "error", "Descrizione su due righe"),
                klc_checks.Finding("footprint_version", "error", "Footprint senza versione"),
                klc_checks.Finding("pin_grid", "error", "Pin fuori griglia"),
                klc_checks.Finding("datasheet_blanks", "warning", "Datasheet con spazi")]
    monkeypatch.setattr(klc_checks, "check_all", lambda *a, **k: findings)
    warned, generated = [], []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    monkeypatch.setattr(window, "_generate", lambda **k: generated.append(k) or True)
    window.btn_apply_metadata.click()
    assert generated == [] and len(warned) == 1
    text = warned[0]
    assert "⛔ Designatore con cifre" in text and "⛔ Descrizione su due righe" in text
    for other in ("Footprint senza versione", "Pin fuori griglia", "Datasheet con spazi"):
        assert other not in text, other
    # The same refusal in English: translated sentence, findings kept.
    window._on_language_changed(window.language_combo.findData("en"))
    try:
        warned.clear()
        window.btn_apply_metadata.click()
        assert warned[0].startswith("First fix these metadata errors") and "Designatore con cifre" in warned[0]
    finally:
        window._on_language_changed(window.language_combo.findData("it"))
