"""@brief Cambio, rinomina e chiusura di un progetto: nessun dato passa da un progetto all'altro.

Difetti 5-9 del revisore 2 (verifica del 2026-09-24), riprodotti prima della
correzione. Ogni regola ha un caso positivo e uno negativo che la regola deve
distinguere per ragioni strutturali (quale cartella, quale progetto), non per
un valore numerico.
"""
import json
from pathlib import Path

import pytest
from PyQt6.QtWidgets import QDialog

from test_photo_inventory_gui import gui
from kicadgen.gui.generation_options_dialog import GenerationOptionsDialog
from kicadgen.gui.project_dialogs import OpenProjectDialog, ProjectNameDialog
from kicadgen.gui.project_state import (PROJECT_CONFIG_NAME, read_project_config,
                                        write_project_config)


def _open(window, monkeypatch, name):  # noqa: F811
    monkeypatch.setattr(OpenProjectDialog, "selected_name", lambda self: name)
    monkeypatch.setattr(OpenProjectDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    window._open_project()


def _name_dialog_returns(monkeypatch, name):
    def accept(self):
        self.field.setText(name)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(ProjectNameDialog, "exec", accept)


def _png() -> bytes:
    import io
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (40, 30), "green").save(buffer, "PNG")
    return buffer.getvalue()


# ── 5: la memoria del progetto precedente ────────────────────────────────

def test_opening_another_project_forgets_the_photo_and_the_extraction(window, monkeypatch):  # noqa: F811
    """@brief Positivo: foto, sovrapposizione e ultima estrazione di A non arrivano a B.

    Negativo: cio' che B ha salvato (i suoi campi) torna com'era.
    """
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    root = window._library_root()
    write_project_config(root / "B", {"widgets": {"field_description": "modulo B"}})
    (root / "B" / "docs").mkdir()
    window._photo_png = window._photo_overlay_png = _png()
    window._last_extracted = object()
    window._reviewed_this_extraction = True
    window._refresh_component_review_button()
    assert window.btn_component_review_top.isVisibleTo(window)

    _open(window, monkeypatch, "B")
    assert window._photo_png is None and window._photo_overlay_png is None
    assert window._last_extracted is None and window._reviewed_this_extraction is False
    assert window._last_mesh_path is None and window.package_geometry is None
    assert not window.btn_component_review_top.isVisibleTo(window)
    assert window.field_description.text() == "modulo B"


def test_a_new_project_forgets_the_photo_too(window, monkeypatch):  # noqa: F811
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    window._photo_png = _png()
    _name_dialog_returns(monkeypatch, "Fresco")
    window._new_project()
    assert window._photo_png is None


# ── 6: file rifiutato e rinomina ─────────────────────────────────────────

def test_a_refused_file_stays_untouched_after_a_rename(window, monkeypatch):  # noqa: F811
    """@brief Positivo: il file di versione piu' nuova resta com'e' anche col nome nuovo.

    Negativo: un file leggibile, dopo la rinomina, si aggiorna normalmente.
    """
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    root = window._library_root()
    newer = json.dumps({"format": "kicadgen-project", "version": 99, "state": {}})
    (root / "Nuovo" / "docs").mkdir(parents=True)
    (root / "Nuovo" / PROJECT_CONFIG_NAME).write_text(newer, encoding="utf-8")
    _open(window, monkeypatch, "Nuovo")
    _name_dialog_returns(monkeypatch, "Nuovo2")
    window._rename_project()
    assert window._component_name() == "Nuovo2"
    window.field_description.setText("modifica")
    assert not window._save_project_config()
    assert (root / "Nuovo2" / PROJECT_CONFIG_NAME).read_text(encoding="utf-8") == newer

    _name_dialog_returns(monkeypatch, "Leggibile")
    window._new_project()
    _name_dialog_returns(monkeypatch, "Leggibile2")
    window._rename_project()
    window.field_description.setText("altra modifica")
    assert window._save_project_config()
    assert read_project_config(root / "Leggibile2")["widgets"]["field_description"] == "altra modifica"


# ── 7: cambio della cartella di lavoro ───────────────────────────────────

def test_changing_the_work_folder_saves_and_closes_the_open_project(window, monkeypatch, tmp_path):  # noqa: F811
    """@brief Positivo: l'omonimo dell'altra cartella non viene toccato; il progetto
    aperto riceve le ultime modifiche e si chiude.

    Negativo: senza progetto aperto cambiare cartella non scrive nulla.
    """
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    root = window._library_root()
    (root / "P" / "docs").mkdir(parents=True)
    _open(window, monkeypatch, "P")
    window.field_description.setText("ultima modifica di P")
    other = tmp_path / "altra-cartella"
    write_project_config(other / "P", {"widgets": {"field_description": "ALTRO"}})
    window.field_library_root.setText(str(other))

    assert window._component_name() == ""
    assert read_project_config(other / "P")["widgets"]["field_description"] == "ALTRO"
    assert read_project_config(root / "P")["widgets"]["field_description"] == "ultima modifica di P"
    window.field_description.setText("dopo la chiusura")
    assert not window._save_project_config()
    assert read_project_config(other / "P")["widgets"]["field_description"] == "ALTRO"

    third = tmp_path / "terza-cartella"
    window.field_library_root.setText(str(third))
    assert not third.exists()


def test_the_work_folder_does_not_change_while_work_is_running(window, monkeypatch, tmp_path):  # noqa: F811
    """@brief Un lavoro in corso scrive nel progetto: la cartella di lavoro torna com'era."""
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    root = window._library_root()
    (root / "Occupato" / "docs").mkdir(parents=True)
    _open(window, monkeypatch, "Occupato")
    monkeypatch.setattr(window, "_background_work_running", lambda: True)
    monkeypatch.setattr(gui.QMessageBox, "information", lambda *args, **kwargs: None)
    window.field_library_root.setText(str(tmp_path / "altrove"))
    assert window._library_root() == root
    assert window._component_name() == "Occupato"


# ── 8: le impostazioni del progetto non diventano generali ───────────────

def _another_component_type(window):  # noqa: F811
    combo = window.combo_component_type
    return next(combo.itemData(i) for i in range(combo.count())
                if combo.itemData(i) and combo.itemData(i) != combo.currentData())


def test_opening_a_project_never_writes_config_json(window, monkeypatch):  # noqa: F811
    """@brief Positivo: aprire un progetto con altra tipologia e opzioni non cambia le
    impostazioni generali di config.json.

    Negativo: la stessa tipologia scelta a mano dall'utente si salva come generale.
    @note Dal 26/09 (PIANO-FIX 1) aprire un progetto aggiorna l'elenco dei
        progetti recenti, che sta in config.json: quella e' l'unica chiave che
        un'apertura puo' scrivere.
    """
    from kicadgen.services import recent_projects
    written = []
    monkeypatch.setattr(gui, "save_config", lambda config: written.append(dict(config)))
    general = {key: value for key, value in window._config.items()
               if key != recent_projects.CONFIG_KEY}
    root = window._library_root()
    other = _another_component_type(window)
    write_project_config(root / "T", {
        "widgets": {"combo_component_type": {"data": other, "text": ""}},
        "options": {"include_nc_pins": False, "silk_pin_labels": True}})
    (root / "T" / "docs").mkdir()
    _open(window, monkeypatch, "T")
    assert window.combo_component_type.currentData() == other
    assert window._include_nc_pins() is False and window._silk_pin_labels() is True
    for config in written:
        assert {key: value for key, value in config.items()
                if key != recent_projects.CONFIG_KEY} == general
    assert written and str(root / "T") in written[-1][recent_projects.CONFIG_KEY]

    combo = window.combo_component_type
    combo.setCurrentIndex(combo.findData(_another_component_type(window)))
    assert written[-1]["component_type"] == combo.currentData()


def test_project_options_do_not_leak_through_a_later_save(window, monkeypatch):  # noqa: F811
    """@brief Positivo: un save_config per altri motivi non porta con se' le opzioni del progetto.

    Negativo: un'opzione cambiata dall'utente in Opzioni di generazione vale
    per il progetto e come generale.
    """
    saved = []
    monkeypatch.setattr(gui, "save_config", lambda config: saved.append(dict(config)))
    general_nc = window._config.get("include_nc_pins", True)
    root = window._library_root()
    write_project_config(root / "T", {"options": {"include_nc_pins": not general_nc}})
    (root / "T" / "docs").mkdir()
    _open(window, monkeypatch, "T")
    window._on_grid_toggled(True)
    assert saved and saved[-1].get("include_nc_pins", True) is general_nc

    # Il dialogo si apre col valore del PROGETTO; l'utente lo riporta a
    # quello generale, come farebbe lui.
    def choose(dialog):
        assert dialog.include_nc_checkbox.isChecked() is (not general_nc)
        dialog.include_nc_checkbox.setChecked(general_nc)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(GenerationOptionsDialog, "exec", choose)
    window._edit_generation_options()
    assert saved[-1]["include_nc_pins"] is general_nc
    assert window._include_nc_pins() is general_nc
    assert window._general_project_state["options"]["include_nc_pins"] is general_nc


def test_an_option_the_project_does_not_declare_falls_back_to_the_general_one(window, monkeypatch):  # noqa: F811
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    root = window._library_root()
    write_project_config(root / "SenzaOpzioni", {"widgets": {}})
    (root / "SenzaOpzioni" / "docs").mkdir()
    window._config["silk_pin_labels"] = True
    _open(window, monkeypatch, "SenzaOpzioni")
    assert window._silk_pin_labels() is True
    window._config["silk_pin_labels"] = False
    assert window._silk_pin_labels() is False


# ── 9: rinomina che cambia solo le maiuscole ─────────────────────────────

def test_a_rename_that_changes_only_the_case_really_renames(window, monkeypatch):  # noqa: F811
    """@brief Positivo: «Scheda» diventa «SCHEDA», cartella compresa, contenuto intatto.

    Negativo: lo stesso nome identico non tocca nulla.
    """
    from kicadgen.services.kicad_library import attach_3d_model

    monkeypatch.setattr(gui, "save_config", lambda config: None)
    root = window._library_root()
    (root / "Scheda" / "docs").mkdir(parents=True)
    (root / "Scheda" / "docs" / "foto.jpg").write_bytes(b"x")
    (root / "Scheda" / "Scheda.pretty").mkdir()
    (root / "Scheda" / "Scheda.pretty" / "Scheda.kicad_mod").write_text(
        attach_3d_model('(footprint "Scheda"\n(property "Value" "Scheda")\n)\n',
                        "${KIPRJMOD}/Scheda.3dshapes/Scheda.step"), encoding="utf-8")
    (root / "Scheda" / "Scheda.3dshapes").mkdir()
    (root / "Scheda" / "Scheda.3dshapes" / "Scheda.step").write_bytes(b"case-step")
    (root / "Scheda" / "Scheda.3dshapes" / "Scheda.preview.json").write_text('{}')
    (root / "Scheda" / "Scheda.3dshapes" / "Scheda.FCStd").write_bytes(b"case-fcstd")
    _open(window, monkeypatch, "Scheda")
    _name_dialog_returns(monkeypatch, "SCHEDA")
    window._rename_project()
    assert window._component_name() == "SCHEDA"
    assert _named_like(root, "scheda") == ["SCHEDA"]
    assert (root / "SCHEDA" / "SCHEDA.pretty" / "SCHEDA.kicad_mod").is_file()
    assert (root / "SCHEDA" / "SCHEDA.3dshapes" / "SCHEDA.step").read_bytes() == b"case-step"
    assert (root / "SCHEDA" / "SCHEDA.3dshapes" / "SCHEDA.preview.json").read_text() == '{}'
    assert (root / "SCHEDA" / "SCHEDA.3dshapes" / "SCHEDA.FCStd").read_bytes() == b"case-fcstd"
    renamed_footprint = (root / "SCHEDA" / "SCHEDA.pretty" / "SCHEDA.kicad_mod").read_text()
    assert renamed_footprint.lstrip().startswith('(footprint "SCHEDA"')
    from kicadgen.services.kicad_library import _direct_child_ranges
    assert len(_direct_child_ranges(renamed_footprint, "model")) == 1
    assert '(property "Value" "SCHEDA")' in renamed_footprint
    assert '${KIPRJMOD}/SCHEDA.3dshapes/SCHEDA.step' in renamed_footprint
    assert (root / "SCHEDA" / "docs" / "foto.jpg").read_bytes() == b"x"
    assert read_project_config(root / "SCHEDA") is not None

    assert window._migrate_docs_to_new_name("SCHEDA") == "SCHEDA"
    assert _named_like(root, "scheda") == ["SCHEDA"]


def test_renaming_a_generated_project_moves_and_updates_kicad_artifacts(window, monkeypatch):  # noqa: F811
    """@brief The complete generated KiCad library follows a normal project rename."""
    from kicadgen.services.kicad_library import attach_3d_model

    monkeypatch.setattr(gui, "save_config", lambda config: None)
    root = window._library_root()
    folder = root / "OldBoard"
    (folder / "docs").mkdir(parents=True)
    pretty = folder / "OldBoard.pretty"
    pretty.mkdir()
    shapes = folder / "OldBoard.3dshapes"
    shapes.mkdir()
    footprint = pretty / "OldBoard.kicad_mod"
    footprint.write_text(attach_3d_model(
        '(footprint "OldBoard" (layer "F.Cu")\n'
        '  (property "Value" "OldBoard")\n)\n',
        "${KIPRJMOD}/OldBoard.3dshapes/OldBoard.step"), encoding="utf-8")
    (shapes / "OldBoard.step").write_bytes(b"step")
    (shapes / "OldBoard.wrl").write_bytes(b"wrl")
    (shapes / "OldBoard.preview.json").write_text('{"vertices": []}', encoding="utf-8")
    (shapes / "OldBoard.FCStd").write_bytes(b"freecad document")
    _open(window, monkeypatch, "OldBoard")
    _name_dialog_returns(monkeypatch, "NewBoard")

    window._rename_project()

    new_folder = root / "NewBoard"
    assert window._component_name() == "NewBoard"
    assert not folder.exists()
    assert (new_folder / "NewBoard.pretty" / "NewBoard.kicad_mod").is_file()
    assert (new_folder / "NewBoard.3dshapes" / "NewBoard.step").read_bytes() == b"step"
    assert (new_folder / "NewBoard.3dshapes" / "NewBoard.wrl").read_bytes() == b"wrl"
    assert (new_folder / "NewBoard.3dshapes" / "NewBoard.preview.json").read_text() == '{"vertices": []}'
    assert (new_folder / "NewBoard.3dshapes" / "NewBoard.FCStd").read_bytes() == b"freecad document"
    updated = (new_folder / "NewBoard.pretty" / "NewBoard.kicad_mod").read_text(encoding="utf-8")
    assert '(footprint "NewBoard"' in updated
    assert '(property "Value" "NewBoard")' in updated
    assert '${KIPRJMOD}/NewBoard.3dshapes/NewBoard.step' in updated
    from kicadgen.services.kicad_library import _direct_child_ranges
    assert len(_direct_child_ranges(updated, "model")) == 1
    assert read_project_config(new_folder) is not None
    assert window._last_mesh_path == new_folder / "NewBoard.3dshapes" / "NewBoard.preview.json"


def test_rename_preserves_a_model_reference_outside_the_project(window, monkeypatch):  # noqa: F811
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    root = window._library_root()
    folder = root / "OldBoard"
    (folder / "docs").mkdir(parents=True)
    pretty = folder / "OldBoard.pretty"
    pretty.mkdir()
    outside_model = "${KICAD8_3DMODEL_DIR}/Connector/Connector.step"
    (pretty / "OldBoard.kicad_mod").write_text(
        '(footprint "OldBoard"\n'
        f'  (model "{outside_model}" (offset (xyz 0 0 0)))\n)\n', encoding="utf-8")
    _open(window, monkeypatch, "OldBoard")
    _name_dialog_returns(monkeypatch, "NewBoard")

    window._rename_project()

    updated = (root / "NewBoard" / "NewBoard.pretty" / "NewBoard.kicad_mod").read_text()
    assert f'(model "{outside_model}"' in updated
    from kicadgen.services.kicad_library import _direct_child_ranges
    assert len(_direct_child_ranges(updated, "model")) == 1


def test_case_only_rename_does_not_replace_a_distinct_case_variant_directory(
    window, monkeypatch,
):  # noqa: F811
    import os
    if os.name == "nt":
        pytest.skip("Windows cannot create distinct directories that differ only by case")
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    root = window._library_root()
    old = root / "Board"
    new = root / "BOARD"
    (old / "docs").mkdir(parents=True)
    (old / "docs" / "source.txt").write_text("source")
    (new / "docs").mkdir(parents=True)
    (new / "docs" / "destination.txt").write_text("destination")
    _open(window, monkeypatch, "Board")
    _name_dialog_returns(monkeypatch, "BOARD")

    window._rename_project()

    assert window._component_name() == "Board"
    assert (old / "docs" / "source.txt").read_text() == "source"
    assert (new / "docs" / "destination.txt").read_text() == "destination"


def _make_symlink_or_skip(link, target, monkeypatch, *, target_is_directory=False):
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError):
        # Windows CI may lack the privilege to create symbolic links. Model
        # the filesystem's link predicate while keeping a real placeholder
        # entry so the refusal path and no-mutation assertions still run.
        if target_is_directory and target.exists():
            link.mkdir(parents=True, exist_ok=True)
        elif not target_is_directory and target.exists():
            link.write_bytes(b"link placeholder")
        path_type = type(link)
        real_is_symlink = path_type.is_symlink

        def simulated_is_symlink(path):
            return path == link or real_is_symlink(path)

        monkeypatch.setattr(path_type, "is_symlink", simulated_is_symlink)
        if not link.exists():
            import os
            real_lexists = os.path.lexists
            monkeypatch.setattr(os.path, "lexists",
                                lambda path: Path(path) == link or real_lexists(path))


def test_rename_refuses_a_dangling_destination_symlink_without_replacing_it(
        window, monkeypatch, tmp_path):  # noqa: F811
    """@brief A dangling destination link is still a collision and survives unchanged."""
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    root = window._library_root()
    old = root / "OldBoard"
    (old / "docs").mkdir(parents=True)
    (old / "docs" / "keep.txt").write_text("project")
    _open(window, monkeypatch, "OldBoard")
    monkeypatch.setattr(window, "_flush_project_config", lambda: None)
    dangling = root / "NewBoard"
    target = tmp_path / "missing-target"
    _make_symlink_or_skip(dangling, target, monkeypatch, target_is_directory=True)

    assert window._rename_project_folder("NewBoard") == "OldBoard"
    assert dangling.is_symlink()
    assert (old / "docs" / "keep.txt").read_text() == "project"
    assert (old / PROJECT_CONFIG_NAME).is_file()


def test_rename_rechecks_dangling_destination_links_after_staging(window, monkeypatch,
                                                                  tmp_path):  # noqa: F811
    """@brief A destination link appearing during staging triggers a full rollback."""
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    root = window._library_root()
    old = root / "OldBoard"
    (old / "docs").mkdir(parents=True)
    marker = old / "docs" / "keep.txt"
    marker.write_text("project", encoding="utf-8")
    _open(window, monkeypatch, "OldBoard")
    monkeypatch.setattr(window, "_flush_project_config", lambda: None)
    destination = root / "NewBoard"
    missing_target = tmp_path / "late-target"
    path_type = type(old)
    real_rename = path_type.rename
    injected = []

    def create_link_after_staging(path, target):
        result = real_rename(path, target)
        if path == old and "rinomina" in Path(target).name:
            _make_symlink_or_skip(destination, missing_target, monkeypatch,
                                  target_is_directory=True)
            injected.append(True)
        return result

    monkeypatch.setattr(path_type, "rename", create_link_after_staging)
    assert window._rename_project_folder("NewBoard") == "OldBoard"

    assert injected == [True]
    assert old.is_dir() and marker.read_text(encoding="utf-8") == "project"
    assert destination.is_symlink()
    assert not any("rinomina" in child.name for child in root.iterdir())


def test_rename_rechecks_project_root_link_after_staging_without_external_writes(
        window, monkeypatch, tmp_path):  # noqa: F811
    """@brief A root replaced with a link during staging is rolled back without logging through it."""
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    root = window._library_root()
    old = root / "OldBoard"
    local_pretty = old / "OldBoard.pretty"
    local_pretty.mkdir(parents=True)
    (local_pretty / "OldBoard.kicad_mod").write_text(
        '(footprint "OldBoard")\n', encoding="utf-8")
    local_shapes = old / "OldBoard.3dshapes"
    local_shapes.mkdir()
    (local_shapes / "OldBoard.step").write_bytes(b"local step")

    external = tmp_path / "external-project"
    external_pretty = external / "OldBoard.pretty"
    external_pretty.mkdir(parents=True)
    external_footprint = external_pretty / "OldBoard.kicad_mod"
    external_footprint.write_text('(footprint "External")\n', encoding="utf-8")
    external_shapes = external / "OldBoard.3dshapes"
    external_shapes.mkdir()
    (external_shapes / "OldBoard.step").write_bytes(b"external step")
    external_log = external / "analisi-ai.log"
    external_log.write_text("external log", encoding="utf-8")

    _open(window, monkeypatch, "OldBoard")
    monkeypatch.setattr(window, "_flush_project_config", lambda: None)
    local_before = {path.relative_to(old): path.read_bytes()
                    for path in old.rglob("*") if path.is_file()}
    displaced = tmp_path / "displaced-local-project"
    staged_paths = []
    path_type = type(old)
    real_rename = path_type.rename

    def replace_root_with_link(path, target):
        if path == old and "rinomina" in Path(target).name:
            stage_path = Path(target)
            real_rename(path, displaced)
            _make_symlink_or_skip(old, external, monkeypatch, target_is_directory=True)
            was_link = path_type.is_symlink

            def staged_root_is_link(candidate):
                return candidate == stage_path or was_link(candidate)

            monkeypatch.setattr(path_type, "is_symlink", staged_root_is_link)
            staged_paths.append(stage_path)
        return real_rename(path, target)

    monkeypatch.setattr(path_type, "rename", replace_root_with_link)
    external_before = {path.relative_to(external): path.read_bytes()
                       for path in external.rglob("*") if path.is_file()}

    assert window._rename_project_folder("NewBoard") == "OldBoard"

    assert displaced.is_dir()
    assert {path.relative_to(displaced): path.read_bytes()
            for path in displaced.rglob("*") if path.is_file()} == local_before
    assert {path.relative_to(external): path.read_bytes()
            for path in external.rglob("*") if path.is_file()} == external_before
    assert external_log.read_text(encoding="utf-8") == "external log"
    assert len(staged_paths) == 1 and not staged_paths[0].exists()
    assert old.is_symlink()


def test_rename_refuses_a_symlinked_project_root_without_touching_its_target(
        window, monkeypatch, tmp_path):  # noqa: F811
    """@brief A project reached through a link is not renamed through to its external target."""
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    root = window._library_root()
    external = tmp_path / "external-project"
    write_project_config(external, {"widgets": {}})
    marker = external / "keep.txt"
    marker.write_text("external project", encoding="utf-8")
    before = marker.read_bytes()
    old = root / "OldBoard"
    _make_symlink_or_skip(old, external, monkeypatch, target_is_directory=True)
    _open(window, monkeypatch, "OldBoard")
    monkeypatch.setattr(window, "_flush_project_config", lambda: None)
    project_before = {path.relative_to(old): path.read_bytes()
                      for path in old.rglob("*") if path.is_file()}
    before = {path.relative_to(external): path.read_bytes()
              for path in external.rglob("*") if path.is_file()}

    assert window._rename_project_folder("NewBoard") == "OldBoard"
    project_after = {path.relative_to(old): path.read_bytes()
                     for path in old.rglob("*") if path.is_file()}
    after = {path.relative_to(external): path.read_bytes()
             for path in external.rglob("*") if path.is_file()}
    assert project_after == project_before
    assert after == before
    assert marker.read_bytes() == before[marker.relative_to(external)]
    assert old.is_symlink()
    assert not (root / "NewBoard").exists()


@pytest.mark.parametrize("output", ["pretty", "3dshapes"])
def test_rename_refuses_linked_output_directories_without_touching_external_files(
        window, monkeypatch, tmp_path, output):  # noqa: F811
    """@brief A linked output directory cannot redirect rename edits to an external library."""
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    root = window._library_root()
    old = root / "OldBoard"
    (old / "docs").mkdir(parents=True)
    pretty = old / "OldBoard.pretty"
    shapes = old / "OldBoard.3dshapes"
    if output == "pretty":
        external = tmp_path / "shared.pretty"
        external.mkdir()
        source = external / "OldBoard.kicad_mod"
        source.write_text('(footprint "OldBoard")\n', encoding="utf-8")
        before = source.read_bytes()
        _make_symlink_or_skip(pretty, external, monkeypatch, target_is_directory=True)
        shapes.mkdir()
    else:
        pretty.mkdir()
        (pretty / "OldBoard.kicad_mod").write_text(
            '(footprint "OldBoard" (model "${KIPRJMOD}/OldBoard.3dshapes/OldBoard.step"))\n',
            encoding="utf-8")
        external = tmp_path / "shared.3dshapes"
        external.mkdir()
        source = external / "OldBoard.step"
        source.write_bytes(b"shared model")
        before = source.read_bytes()
        _make_symlink_or_skip(shapes, external, monkeypatch, target_is_directory=True)
    _open(window, monkeypatch, "OldBoard")
    monkeypatch.setattr(window, "_flush_project_config", lambda: None)

    assert window._rename_project_folder("NewBoard") == "OldBoard"
    assert source.read_bytes() == before
    assert (pretty if output == "pretty" else shapes).is_symlink()
    assert old.is_dir() and not (root / "NewBoard").exists()


def test_rename_rechecks_output_links_after_staging_and_rolls_back(
        window, monkeypatch, tmp_path):  # noqa: F811
    """@brief A generated output replaced by a link during staging is never followed."""
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    root = window._library_root()
    old = root / "OldBoard"
    pretty = old / "OldBoard.pretty"
    pretty.mkdir(parents=True)
    original = pretty / "OldBoard.kicad_mod"
    original.write_text('(footprint "OldBoard")\n', encoding="utf-8")
    external = tmp_path / "shared.pretty"
    external.mkdir()
    external_footprint = external / "OldBoard.kicad_mod"
    external_footprint.write_text('(footprint "External")\n', encoding="utf-8")
    external_before = {path.name: path.read_bytes() for path in external.iterdir() if path.is_file()}
    _open(window, monkeypatch, "OldBoard")
    monkeypatch.setattr(window, "_flush_project_config", lambda: None)

    path_type = type(old)
    real_rename = path_type.rename
    injected = []

    def replace_pretty_with_link_during_staging(path, target):
        result = real_rename(path, target)
        if path == old and "rinomina" in Path(target).name and not injected:
            staged_pretty = Path(target) / "OldBoard.pretty"
            staged_pretty.rename(Path(target) / "preserved-OldBoard.pretty")
            _make_symlink_or_skip(staged_pretty, external, monkeypatch,
                                  target_is_directory=True)
            injected.append(True)
        return result

    monkeypatch.setattr(path_type, "rename", replace_pretty_with_link_during_staging)
    assert window._rename_project_folder("NewBoard") == "OldBoard"

    assert injected == [True]
    assert old.is_dir() and (old / "OldBoard.pretty").exists()
    assert (old / "preserved-OldBoard.pretty" / "OldBoard.kicad_mod").read_text(
        encoding="utf-8") == '(footprint "OldBoard")\n'
    assert {path.name: path.read_bytes() for path in external.iterdir() if path.is_file()} == \
        external_before
    assert not (root / "NewBoard").exists()
    assert not any("rinomina" in child.name for child in root.iterdir())


def test_rename_refuses_a_linked_footprint_file_without_rewriting_its_target(
        window, monkeypatch, tmp_path):  # noqa: F811
    """@brief An external footprint symlink remains byte-for-byte unchanged on refusal."""
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    root = window._library_root()
    old = root / "OldBoard"
    (old / "docs").mkdir(parents=True)
    pretty = old / "OldBoard.pretty"
    pretty.mkdir()
    external = tmp_path / "shared.kicad_mod"
    external.write_text('(footprint "OldBoard")\n', encoding="utf-8")
    before = external.read_bytes()
    _make_symlink_or_skip(pretty / "OldBoard.kicad_mod", external, monkeypatch)
    _open(window, monkeypatch, "OldBoard")
    monkeypatch.setattr(window, "_flush_project_config", lambda: None)

    assert window._rename_project_folder("NewBoard") == "OldBoard"
    assert external.read_bytes() == before
    assert (pretty / "OldBoard.kicad_mod").is_symlink()
    assert old.is_dir() and not (root / "NewBoard").exists()


def test_rename_refuses_to_overwrite_a_preexisting_new_step_file(
        window, monkeypatch):  # noqa: F811
    """@brief A same-folder target model is preserved and the project rename rolls back."""
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    root = window._library_root()
    old = root / "OldBoard"
    (old / "docs").mkdir(parents=True)
    shapes = old / "OldBoard.3dshapes"
    shapes.mkdir()
    (shapes / "OldBoard.step").write_bytes(b"old model")
    (shapes / "NewBoard.step").write_bytes(b"preexisting model")
    pretty = old / "OldBoard.pretty"
    pretty.mkdir()
    original_footprint = (
        '(footprint "OldBoard"\n'
        '(model "${KIPRJMOD}/OldBoard.3dshapes/OldBoard.step")\n)\n').encode()
    (pretty / "OldBoard.kicad_mod").write_bytes(original_footprint)
    _open(window, monkeypatch, "OldBoard")
    _name_dialog_returns(monkeypatch, "NewBoard")

    window._rename_project()

    assert window._component_name() == "OldBoard"
    assert (shapes / "OldBoard.step").read_bytes() == b"old model"
    assert (shapes / "NewBoard.step").read_bytes() == b"preexisting model"
    assert (pretty / "OldBoard.kicad_mod").read_bytes() == original_footprint
    assert not (root / "NewBoard").exists()
    assert not any("rinomina" in child.name for child in root.iterdir())


def test_rename_refuses_to_replace_an_empty_new_footprint_directory(
        window, monkeypatch):  # noqa: F811
    """@brief An empty destination directory is preserved instead of replaced by the old library."""
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    root = window._library_root()
    old = root / "OldBoard"
    (old / "docs").mkdir(parents=True)
    old_pretty = old / "OldBoard.pretty"
    old_pretty.mkdir()
    (old_pretty / "OldBoard.kicad_mod").write_text("old footprint", encoding="utf-8")
    new_pretty = old / "NewBoard.pretty"
    new_pretty.mkdir()
    _open(window, monkeypatch, "OldBoard")
    _name_dialog_returns(monkeypatch, "NewBoard")

    window._rename_project()

    assert window._component_name() == "OldBoard"
    assert (old_pretty / "OldBoard.kicad_mod").read_text(encoding="utf-8") == "old footprint"
    assert new_pretty.is_dir() and list(new_pretty.iterdir()) == []
    assert not (root / "NewBoard").exists()
    assert not any("rinomina" in child.name for child in root.iterdir())


def test_generated_project_rename_rolls_back_if_final_move_fails(window, monkeypatch):  # noqa: F811
    """@brief A failed final rename restores the original files and references."""
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    root = window._library_root()
    folder = root / "OldBoard"
    (folder / "docs").mkdir(parents=True)
    pretty = folder / "OldBoard.pretty"
    pretty.mkdir()
    shapes = folder / "OldBoard.3dshapes"
    shapes.mkdir()
    original = ('(footprint "OldBoard"\n'
                '(model "${KIPRJMOD}/OldBoard.3dshapes/OldBoard.step")\n)\n').encode()
    (pretty / "OldBoard.kicad_mod").write_bytes(original)
    (shapes / "OldBoard.step").write_bytes(b"original step")
    _open(window, monkeypatch, "OldBoard")
    _name_dialog_returns(monkeypatch, "NewBoard")

    path_type = type(folder)
    real_rename = path_type.rename

    def fail_final_move(path, target):
        if Path(target).name == "NewBoard":
            raise OSError("simulated final move failure")
        return real_rename(path, target)

    monkeypatch.setattr(path_type, "rename", fail_final_move)
    window._rename_project()

    assert window._component_name() == "OldBoard"
    assert (folder / "OldBoard.pretty" / "OldBoard.kicad_mod").read_bytes() == original
    assert (folder / "OldBoard.3dshapes" / "OldBoard.step").read_bytes() == b"original step"
    assert not (root / "NewBoard").exists()
    assert not any("rinomina" in child.name for child in root.iterdir())


def _named_like(root, name):
    """Cartelle di `root` con quel nome a meno delle maiuscole, piu' le temporanee rimaste."""
    return sorted(p.name for p in root.iterdir()
                  if p.name.casefold() == name or "rinomina" in p.name)


# ── Rinomina durante un lavoro in corso ──────────────────────────────────

def test_a_rename_waits_for_the_running_work(window, monkeypatch):  # noqa: F811
    """@brief Positivo: con un lavoro in corso la rinomina non parte. Negativo: a lavoro finito si'.

    Prima valeva per «Usa questo nome», tolto il 2026-09-24 (utente): il nome
    si cambia solo da «Rinomina progetto…», con la stessa guardia.
    """
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    root = window._library_root()
    (root / "Vecchio" / "docs").mkdir(parents=True)
    _open(window, monkeypatch, "Vecchio")
    busy = {"value": True}
    monkeypatch.setattr(window, "_background_work_running", lambda: busy["value"])
    monkeypatch.setattr(gui.QMessageBox, "information", lambda *args, **kwargs: None)
    _name_dialog_returns(monkeypatch, "Proposto")
    window._rename_project()
    assert window._component_name() == "Vecchio"
    busy["value"] = False
    window._rename_project()
    assert window._component_name() == "Proposto"

# ── ramo «documenti in conflitto» di `_migrate_docs_to_new_name` ─────────

def test_a_rename_onto_documents_with_the_same_names_moves_nothing(window, monkeypatch):  # noqa: F811
    """@brief Ramo difensivo: la cartella di destinazione ha gia' documenti omonimi.

    Il dialogo rifiuta i nomi di progetti esistenti (`name_problem`), quindi
    qui si arriva solo se la cartella nasce fra il dialogo e lo spostamento:
    si chiama il metodo direttamente (revisore 4, difetto 14). Positivo: resta
    il nome vecchio, nessun file toccato, il motivo nel log. Negativo: senza
    omonimi gli stessi documenti si spostano.
    """
    from kicadgen.services.document_roles import MANIFEST
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    root = window._library_root()
    (root / "Origine" / "docs").mkdir(parents=True)
    _open(window, monkeypatch, "Origine")
    source = root / "Origine" / "docs"
    (source / MANIFEST).write_text("{}", encoding="utf-8")
    (source / "foto.jpg").write_bytes(b"mia")
    (root / "Occupata" / "docs").mkdir(parents=True)
    (root / "Occupata" / "docs" / "foto.jpg").write_bytes(b"altrui")

    assert window._migrate_docs_to_new_name("Occupata") == "Origine"
    assert (source / "foto.jpg").read_bytes() == b"mia"
    assert (root / "Occupata" / "docs" / "foto.jpg").read_bytes() == b"altrui"
    assert "Occupata" in window.ai_progress_text.toPlainText()

    assert window._migrate_docs_to_new_name("Libera") == "Libera"
    assert (root / "Libera" / "docs" / "foto.jpg").read_bytes() == b"mia"
    assert not (source / "foto.jpg").exists()
