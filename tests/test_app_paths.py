"""@brief Dove il programma legge e scrive, da sorgente e installato (2026-09-25).

Da sorgente nulla cambia (repository). Installato, la cartella del programma
e' in sola lettura: si scrive in %LOCALAPPDATA%\\KiCadGen e i progetti vanno
in Documenti\\KiCadGen. L'eseguibile si simula con `sys.frozen` e
`sys._MEIPASS`, come li imposta PyInstaller.
"""
import sys
from pathlib import Path

import pytest

from kicadgen import app_paths

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("KICADGEN_DATA_DIR", "KICADGEN_CONFIG_PATH", "KICADGEN_PROFILE_DB",
                 "KICADGEN_MATCH_DB", "KICADGEN_YOLO_MODEL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def frozen(monkeypatch, tmp_path, clean_env):
    """Programma «installato»: pacchetto in sola lettura, dati utente altrove."""
    bundle = tmp_path / "Program Files" / "KiCadGen" / "_internal"
    (bundle / "data").mkdir(parents=True)
    (bundle / "data" / "analysis_profiles.sqlite3").write_bytes(b"iniziale")
    # Dal 2026-10-02 i rilevatori stanno in models/yolo (app_paths.YOLO_MODELS_DIR).
    (bundle / "models" / "yolo").mkdir(parents=True)
    (bundle / "models" / "yolo" / "pcb_yolov8s.onnx").write_bytes(b"onnx")
    local = tmp_path / "AppData" / "Local"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(app_paths, "_documents_dir", lambda: tmp_path / "Documenti")
    return bundle, local, tmp_path / "Documenti"


def test_from_the_sources_everything_stays_in_the_repository(clean_env):
    """Negativo al cambio: lo sviluppo non si accorge di nulla."""
    assert not app_paths.is_frozen()
    assert app_paths.install_dir() == REPO
    assert app_paths.user_data_dir() == REPO
    assert app_paths.default_projects_dir() == REPO / "generated"
    assert app_paths.resource_path("models", "x.onnx") == REPO / "models" / "x.onnx"
    assert app_paths.data_file("analysis_profiles.sqlite3") == REPO / "data" / "analysis_profiles.sqlite3"
    assert app_paths.env_files() == [REPO / ".env"]


def test_installed_writes_to_the_users_folders(frozen):
    bundle, local, documents = frozen
    assert app_paths.install_dir() == bundle
    assert app_paths.user_data_dir() == local / "KiCadGen"
    assert app_paths.default_projects_dir() == documents / "KiCadGen"
    assert app_paths.yolo_model_path().read_bytes() == b"onnx"
    assert app_paths.env_files()[0] == local / "KiCadGen" / ".env"


def test_the_shipped_database_seeds_a_new_user_and_is_never_touched(frozen):
    bundle, local, _ = frozen
    target = app_paths.data_file("analysis_profiles.sqlite3", seed=True)
    assert target == local / "KiCadGen" / "data" / "analysis_profiles.sqlite3"
    assert target.read_bytes() == b"iniziale"
    target.write_bytes(b"modificato dall'utente")
    # Negativo: un avvio successivo non rimette la copia distribuita.
    app_paths.data_file("analysis_profiles.sqlite3", seed=True)
    assert target.read_bytes() == b"modificato dall'utente"
    assert (bundle / "data" / "analysis_profiles.sqlite3").read_bytes() == b"iniziale"


def test_a_file_without_a_shipped_copy_is_not_created(frozen):
    """L'archivio dei modelli accettati nasce solo quando si accetta qualcosa."""
    path = app_paths.data_file("accepted_components.sqlite3", seed=True)
    assert not path.exists()


def test_the_data_folder_can_be_moved_for_both_layouts(monkeypatch, tmp_path, clean_env):
    monkeypatch.setenv("KICADGEN_DATA_DIR", str(tmp_path / "altrove"))
    assert app_paths.user_data_dir() == tmp_path / "altrove"
    assert app_paths.default_projects_dir() == tmp_path / "altrove" / "generated"


def test_the_services_follow_the_installed_layout(frozen, monkeypatch):
    """I punti che prima usavano `Path(__file__)` seguono ora la regola unica."""
    bundle, local, _ = frozen
    from kicadgen.services import accepted_models, photo_yolo
    from kicadgen.services.analysis_profiles import ProfileStore
    assert accepted_models.default_path() == local / "KiCadGen" / "data" / "accepted_components.sqlite3"
    assert photo_yolo.resolve_yolo_model() == bundle / "models" / "yolo" / "pcb_yolov8s.onnx"
    monkeypatch.setattr(ProfileStore, "_initialize", lambda self, *a: None)
    store = ProfileStore()
    assert store.path == local / "KiCadGen" / "data" / "analysis_profiles.sqlite3"
    assert store.path.read_bytes() == b"iniziale"


def test_the_documents_folder_is_a_real_folder_on_windows(clean_env):
    documents = app_paths._documents_dir()
    assert documents.is_absolute() and documents.name


# -- Seed copy: atomic, and a damaged user file is set aside (fix C6) ---------

def _make_sqlite(path, rows=200):
    """A real SQLite database spanning several pages, like the shipped seed."""
    import sqlite3
    connection = sqlite3.connect(path)
    with connection:
        connection.execute("CREATE TABLE t (v TEXT)")
        connection.executemany("INSERT INTO t VALUES (?)", [("x" * 200,)] * rows)
    connection.close()
    return path


@pytest.fixture
def sqlite_seed(frozen):
    """The package ships a real SQLite seed instead of placeholder bytes."""
    bundle, local, _ = frozen
    shipped = bundle / "data" / "analysis_profiles.sqlite3"
    shipped.unlink()
    _make_sqlite(shipped)
    target = local / "KiCadGen" / "data" / "analysis_profiles.sqlite3"
    return shipped, target


def test_a_truncated_user_database_is_set_aside_and_reseeded(sqlite_seed):
    """A copy cut short by a crash would otherwise stop the program forever."""
    shipped, target = sqlite_seed
    target.parent.mkdir(parents=True)
    target.write_bytes(shipped.read_bytes()[:4096])  # header intact, body cut
    assert app_paths.is_damaged_sqlite(target)
    assert app_paths.data_file("analysis_profiles.sqlite3", seed=True) == target
    assert target.read_bytes() == shipped.read_bytes()
    aside = list(target.parent.glob(target.name + ".corrupt-*"))
    # The damaged file is kept, never deleted.
    assert len(aside) == 1 and aside[0].stat().st_size == 4096


def test_an_empty_or_foreign_user_file_is_not_a_seeded_database(sqlite_seed):
    shipped, target = sqlite_seed
    target.parent.mkdir(parents=True)
    target.write_bytes(b"")
    (target.parent / (target.name + "-journal")).write_bytes(b"stale journal")
    app_paths.data_file("analysis_profiles.sqlite3", seed=True)
    assert target.read_bytes() == shipped.read_bytes()
    aside = sorted(p.name for p in target.parent.glob(target.name + ".corrupt-*"))
    # The journal follows the damaged file: never replayed onto the new seed.
    assert len(aside) == 2 and aside[1] == aside[0] + "-journal"
    assert not (target.parent / (target.name + "-journal")).exists()


def test_a_valid_user_database_is_never_replaced(sqlite_seed):
    """Negative: the user's own sound database differs from the seed and stays."""
    shipped, target = sqlite_seed
    target.parent.mkdir(parents=True)
    _make_sqlite(target, rows=3)
    before = target.read_bytes()
    app_paths.data_file("analysis_profiles.sqlite3", seed=True)
    assert target.read_bytes() == before != shipped.read_bytes()
    assert not list(target.parent.glob(target.name + ".corrupt-*"))


def test_a_locked_database_is_not_damage(sqlite_seed):
    """Negative: a transient lock must never make user data be set aside."""
    import sqlite3
    shipped, target = sqlite_seed
    target.parent.mkdir(parents=True)
    _make_sqlite(target, rows=3)
    holder = sqlite3.connect(target, isolation_level=None)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        assert app_paths.is_damaged_sqlite(target) is False
        app_paths.data_file("analysis_profiles.sqlite3", seed=True)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert not list(target.parent.glob(target.name + ".corrupt-*"))


def test_an_interrupted_copy_leaves_no_file_under_the_final_name(sqlite_seed, monkeypatch):
    shipped, target = sqlite_seed

    def broken_copy(source, destination):
        with open(destination, "wb") as handle:
            handle.write(b"SQLite format 3\x00 half")
        raise OSError("disk full")

    monkeypatch.setattr(app_paths.shutil, "copy2", broken_copy)
    with pytest.raises(OSError):
        app_paths.data_file("analysis_profiles.sqlite3", seed=True)
    assert not target.exists()
    assert not list(target.parent.iterdir())  # no temporary left behind


def test_a_non_sqlite_seed_never_quarantines_the_user_file(frozen):
    """Negative: only a SQLite seed defines what a damaged user file is."""
    bundle, local, _ = frozen
    target = local / "KiCadGen" / "data" / "analysis_profiles.sqlite3"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"not sqlite, but the seed is not either")
    app_paths.data_file("analysis_profiles.sqlite3", seed=True)
    assert target.read_bytes() == b"not sqlite, but the seed is not either"
