"""@brief GUI: la voce «KiCadGen» gia' registrata altrove non si ripunta senza conferma.

Verifica avversariale del 25/09 (F2): il servizio rifiuta di cambiare
l'indirizzo di una libreria esistente (`library_entry_conflict`); la finestra
deve chiedere, nominando la cartella vecchia e la nuova, e ripetere solo dopo
un si'.
"""
from kicadgen.gui import main_window as gui

from test_photo_inventory_gui import _write_complete_kicad_output, window  # noqa: F401


def _project_with_other_kicadgen(tmp_path):
    """Progetto la cui tabella ha gia' «KiCadGen» su un'altra cartella."""
    project = tmp_path / "DestinationProject"
    project.mkdir()
    (project / "DestinationProject.kicad_pro").write_text("{}", encoding="utf-8")
    (project / "fp-lib-table").write_text(
        '(fp_lib_table\n  (version 7)\n'
        '  (lib (name "KiCadGen")(type "KiCad")(uri "C:/Altro/KiCadGen.pretty")'
        '(options "")(descr "altra"))\n)\n', encoding="utf-8")
    return project


def test_a_conflicting_library_entry_is_repointed_only_after_yes(window, tmp_path, monkeypatch):  # noqa: F811
    _write_complete_kicad_output(window)
    project = _project_with_other_kicadgen(tmp_path)
    monkeypatch.setattr(gui.QFileDialog, "getExistingDirectory", lambda *args: str(project))
    asked = []
    monkeypatch.setattr(gui.QMessageBox, "question",
                        lambda _p, _t, text, *a: asked.append(text) or gui.QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(gui.QMessageBox, "information", lambda *a: None)
    window._install_project_library()
    assert len(asked) == 1 and "C:/Altro/KiCadGen.pretty" in asked[0]
    table = (project / "fp-lib-table").read_text(encoding="utf-8")
    assert "C:/Altro/KiCadGen.pretty" not in table and "KIPRJMOD" in table


def test_answering_no_leaves_the_table_and_files_untouched(window, tmp_path, monkeypatch):  # noqa: F811
    _write_complete_kicad_output(window)
    project = _project_with_other_kicadgen(tmp_path)
    before = (project / "fp-lib-table").read_text(encoding="utf-8")
    monkeypatch.setattr(gui.QFileDialog, "getExistingDirectory", lambda *args: str(project))
    monkeypatch.setattr(gui.QMessageBox, "question",
                        lambda *a: gui.QMessageBox.StandardButton.No)
    window._install_project_library()
    assert (project / "fp-lib-table").read_text(encoding="utf-8") == before
    assert not (project / "KiCadGen.pretty" / f"{window._component_name()}.kicad_mod").exists()
