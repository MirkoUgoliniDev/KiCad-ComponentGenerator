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
    (bundle / "models").mkdir()
    (bundle / "models" / "pcb_yolov8s.onnx").write_bytes(b"onnx")
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
    assert app_paths.resource_path("models", "pcb_yolov8s.onnx").read_bytes() == b"onnx"
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
    assert photo_yolo.resolve_yolo_model() == bundle / "models" / "pcb_yolov8s.onnx"
    monkeypatch.setattr(ProfileStore, "_initialize", lambda self, *a: None)
    store = ProfileStore()
    assert store.path == local / "KiCadGen" / "data" / "analysis_profiles.sqlite3"
    assert store.path.read_bytes() == b"iniziale"


def test_the_documents_folder_is_a_real_folder_on_windows(clean_env):
    documents = app_paths._documents_dir()
    assert documents.is_absolute() and documents.name
