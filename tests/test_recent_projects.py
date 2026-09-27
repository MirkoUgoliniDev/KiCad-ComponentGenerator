"""@brief Progetti recenti in config.json (PIANO-FIX, voce 1).

Ogni regola ha il suo negativo: l'ordine e il tetto, i duplicati che sono lo
stesso progetto contro quelli che non lo sono, una cartella che non e' piu' un
progetto (saltata, non cancellata) contro una che lo e', un valore di
configurazione rovinato contro uno buono.
"""
import json
import os
from pathlib import Path

import pytest

from kicadgen.services import recent_projects as rp


def _project(root: Path, name: str, component_type=None) -> Path:
    """Crea una cartella di progetto con il suo project_config.json."""
    folder = root / name
    folder.mkdir(parents=True)
    widgets = {}
    if component_type is not None:
        widgets["combo_component_type"] = component_type
    document = {"format": "kicadgen-project", "version": 1, "state": {"widgets": widgets}}
    (folder / rp.PROJECT_CONFIG_NAME).write_text(json.dumps(document), encoding="utf-8")
    return folder


# ── Ordine, tetto, duplicati ─────────────────────────────────────────────

def test_add_puts_the_newest_first_and_moves_instead_of_duplicating(tmp_path):
    config = {}
    a, b, c = (str(tmp_path / name) for name in ("A", "B", "C"))
    rp.add(config, a)
    rp.add(config, b)
    rp.add(config, c)
    assert config[rp.CONFIG_KEY] == [c, b, a]
    # Riaprire un progetto gia' in elenco lo porta in testa, senza copie.
    assert rp.add(config, a) == [a, c, b]


def test_the_list_is_capped_and_the_oldest_entry_goes(tmp_path):
    config = {}
    paths = [str(tmp_path / f"P{i:02d}") for i in range(rp.MAX_RECENT + 3)]
    for path in paths:
        rp.add(config, path)
    assert len(config[rp.CONFIG_KEY]) == rp.MAX_RECENT
    assert config[rp.CONFIG_KEY][0] == paths[-1]
    # Negativo: i tre piu' vecchi sono usciti, gli altri no.
    assert not set(paths[:3]) & set(config[rp.CONFIG_KEY])
    assert paths[3] in config[rp.CONFIG_KEY]


def test_spellings_of_the_same_folder_are_one_entry(tmp_path):
    config = {}
    folder = tmp_path / "Scheda"
    rp.add(config, str(folder))
    rp.add(config, str(folder) + os.sep)          # separatore finale
    rp.add(config, str(tmp_path / "x" / ".." / "Scheda"))  # percorso non normalizzato
    assert config[rp.CONFIG_KEY] == [str(folder)]


@pytest.mark.skipif(os.name != "nt", reason="lettere maiuscole e minuscole si distinguono solo fuori da Windows")
def test_letter_case_is_ignored_on_windows_and_the_new_spelling_wins(tmp_path):
    config = {}
    rp.add(config, str(tmp_path / "Scheda"))
    rp.add(config, str(tmp_path / "SCHEDA"))
    assert config[rp.CONFIG_KEY] == [str(tmp_path / "SCHEDA")]


def test_different_folders_with_similar_names_stay_apart(tmp_path):
    """@brief Negativo della deduplicazione: nomi simili ma cartelle diverse."""
    config = {}
    rp.add(config, str(tmp_path / "Scheda"))
    rp.add(config, str(tmp_path / "Scheda2"))
    rp.add(config, str(tmp_path / "altra" / "Scheda"))
    assert len(config[rp.CONFIG_KEY]) == 3


def test_add_refuses_a_relative_path_and_leaves_the_list_alone(tmp_path):
    config = {rp.CONFIG_KEY: [str(tmp_path / "A")]}
    with pytest.raises(ValueError):
        rp.add(config, "relativo/Scheda")
    with pytest.raises(ValueError):
        rp.add(config, "")
    assert config[rp.CONFIG_KEY] == [str(tmp_path / "A")]


def test_add_accepts_a_path_object(tmp_path):
    config = {}
    rp.add(config, tmp_path / "A")
    assert config[rp.CONFIG_KEY] == [str(tmp_path / "A")]


# ── Rimozione ────────────────────────────────────────────────────────────

def test_remove_forgets_the_entry_but_not_the_folder(tmp_path):
    folder = _project(tmp_path, "A")
    other = str(tmp_path / "B")
    config = {rp.CONFIG_KEY: [str(folder), other]}
    assert rp.remove(config, str(folder) + os.sep) == [other]
    assert (folder / rp.PROJECT_CONFIG_NAME).is_file()


def test_remove_of_an_unknown_or_invalid_path_changes_nothing(tmp_path):
    config = {rp.CONFIG_KEY: [str(tmp_path / "A")]}
    assert rp.remove(config, str(tmp_path / "B")) == [str(tmp_path / "A")]
    assert rp.remove(config, "relativo") == [str(tmp_path / "A")]
    assert rp.remove(config, None) == [str(tmp_path / "A")]


# ── Solo progetti che si possono riaprire ────────────────────────────────

def test_load_skips_missing_folders_and_folders_that_are_no_longer_projects(tmp_path):
    good = _project(tmp_path, "Buono")
    emptied = tmp_path / "Svuotato"
    emptied.mkdir()
    missing = tmp_path / "Sparito"
    config = {rp.CONFIG_KEY: [str(missing), str(emptied), str(good)]}
    assert rp.load(config) == [str(good)]
    # Saltate, non cancellate: il disco torna, la voce ricompare.
    assert config[rp.CONFIG_KEY] == [str(missing), str(emptied), str(good)]
    assert emptied.is_dir()
    _project(tmp_path, "Sparito")
    assert rp.load(config) == [str(missing), str(good)]


def test_a_file_named_like_the_project_file_is_not_enough_if_it_is_a_folder(tmp_path):
    fake = tmp_path / "Finto"
    (fake / rp.PROJECT_CONFIG_NAME).mkdir(parents=True)
    assert not rp.is_project(fake)
    assert rp.load({rp.CONFIG_KEY: [str(fake)]}) == []


# ── Configurazione rovinata ──────────────────────────────────────────────

@pytest.mark.parametrize("value", [None, "C:/progetto", 42, {"a": 1}, True])
def test_a_value_that_is_not_a_list_reads_as_empty(value):
    config = {rp.CONFIG_KEY: value}
    assert rp.stored(config) == []
    assert rp.load(config) == []


def test_bad_entries_are_dropped_and_good_ones_kept(tmp_path):
    good = _project(tmp_path, "Buono")
    config = {rp.CONFIG_KEY: [None, 7, "", "   ", "relativo", ["x"], str(good), str(good)]}
    assert rp.stored(config) == [str(good)]
    assert rp.load(config) == [str(good)]
    # Il primo add ripulisce anche la lista salvata.
    rp.add(config, str(tmp_path / "Nuovo"))
    assert config[rp.CONFIG_KEY] == [str(tmp_path / "Nuovo"), str(good)]


def test_missing_key_and_non_dict_config_read_as_empty():
    assert rp.stored({}) == []
    assert rp.stored(None) == []
    assert rp.load("non un dizionario") == []


# ── Descrizione per la pagina iniziale ───────────────────────────────────

def test_describe_reads_type_and_date_from_the_project_file(tmp_path):
    folder = _project(tmp_path, "Scheda", {"data": "pcb_module", "text": "Scheda con componenti"})
    summary = rp.describe(folder)
    assert summary.name == "Scheda"
    assert summary.path == str(folder)
    assert summary.component_type == "Scheda con componenti"
    assert summary.component_type_key == "pcb_module"
    assert summary.modified is not None


@pytest.mark.parametrize("content", ["{non json", json.dumps([1, 2]),
                                     json.dumps({"state": {"widgets": {"combo_component_type": 5}}}),
                                     json.dumps({"state": "x"})])
def test_describe_of_an_unreadable_project_file_keeps_name_and_path(tmp_path, content):
    folder = tmp_path / "Rotto"
    folder.mkdir()
    (folder / rp.PROJECT_CONFIG_NAME).write_text(content, encoding="utf-8")
    summary = rp.describe(folder)
    assert (summary.name, summary.component_type, summary.component_type_key) == ("Rotto", "", "")
    assert summary.modified is not None  # il file c'e', la data si legge


def test_describe_without_project_file_has_no_date(tmp_path):
    summary = rp.describe(tmp_path / "Assente")
    assert summary.name == "Assente" and summary.modified is None


def test_project_file_name_matches_the_gui_module():
    """@brief Il nome ripetuto qui (senza Qt) resta quello di project_state."""
    from kicadgen.gui.project_state import PROJECT_CONFIG_NAME
    assert rp.PROJECT_CONFIG_NAME == PROJECT_CONFIG_NAME


def test_the_service_does_not_import_qt():
    import subprocess
    import sys
    code = ("import sys, kicadgen.services.recent_projects as r; "
            "r.remember({}, 'relativo'); "
            "print(any(m.startswith('PyQt6') for m in sys.modules))")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            cwd=Path(__file__).resolve().parents[1], check=True)
    assert result.stdout.strip() == "False"


# ── Salvataggio in config.json ───────────────────────────────────────────

def test_remember_and_forget_write_config_json(tmp_path):
    """@brief Positivo sul file vero (isolato da KICADGEN_CONFIG_PATH in conftest)."""
    from kicadgen.gui.app_config import config_path, load_config
    config = load_config()
    folder = _project(tmp_path, "Scheda")
    assert rp.remember(config, folder) is True
    assert json.loads(config_path().read_text(encoding="utf-8"))[rp.CONFIG_KEY] == [str(folder)]
    assert rp.load(load_config()) == [str(folder)]
    assert rp.forget(config, str(folder)) is True
    assert json.loads(config_path().read_text(encoding="utf-8"))[rp.CONFIG_KEY] == []


def test_remember_survives_an_unwritable_config_and_keeps_the_session_list(tmp_path):
    def broken(_config):
        raise PermissionError("sola lettura")
    config = {}
    assert rp.remember(config, tmp_path / "A", save=broken) is False
    assert config[rp.CONFIG_KEY] == [str(tmp_path / "A")]
    assert rp.forget(config, tmp_path / "A", save=broken) is False
    assert config[rp.CONFIG_KEY] == []


def test_remember_of_a_relative_path_writes_nothing():
    calls = []
    assert rp.remember({}, "relativo", save=calls.append) is False
    assert calls == []


def test_a_save_that_reports_failure_is_not_counted_as_saved():
    """Revisione 130: save_config ora segnala il fallimento con False invece di
    sollevare; l'elenco dei recenti non deve dirlo salvato (negativo: None = salvato)."""
    from kicadgen.services import recent_projects
    assert recent_projects._save({}, lambda config: False) is False
    assert recent_projects._save({}, lambda config: None) is True
    assert recent_projects._save({}, lambda config: True) is True
