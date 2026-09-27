"""@brief La cache AI e le evidenze stanno nella cartella del progetto.

Decisione dell'utente del 2026-09-24: «deve andare tutto dentro il progetto
specifico, ogni interrogazione AI e' specifica del progetto». Prima risposte
in cache, evidenze del rilevatore e `yolo-audit/` finivano in
`generated/photo-cache`, comune a tutti i progetti.

Ogni regola ha il suo negativo: senza nome non c'e' una cartella (e non ne
nasce una comune), due progetti non condividono la cache, una rinomina non
sovrascrive nulla.
"""
from pathlib import Path
import sqlite3

import pytest
from PIL import Image

from test_photo_inventory_gui import gui
from kicadgen.services import datasheet_extractor as ext
from kicadgen.services.analysis_profiles import ProfileStore
from kicadgen.services.document_roles import DocumentSet
from kicadgen.services.i18n import DEFAULT_TRANSLATIONS, SUPERSEDED_DEFAULTS, I18n
from kicadgen.services.photo_inventory import (PHOTO_CACHE_SUBDIR, REVIEW_BUNDLE_SUBDIR,
                                               photo_cache_dir)


def test_the_cache_is_a_folder_of_the_project(window):  # noqa: F811
    """@brief Positivo: dentro la cartella del progetto. Negativo: due progetti, due cache."""
    first = window._photo_cache_dir()
    assert first == window._project_dir() / PHOTO_CACHE_SUBDIR
    assert first == photo_cache_dir(window._project_dir())
    window.field_name.setText("AltroProgetto")
    second = window._photo_cache_dir()
    assert second != first and second.parent.name == "AltroProgetto"


def test_without_a_name_there_is_no_cache_folder_at_all(window):  # noqa: F811
    """@brief Negativo: senza progetto non si ripiega su una cartella comune."""
    window.field_name.setText("")
    with pytest.raises(ValueError):
        window._photo_cache_dir()


def test_the_extraction_receives_the_project_cache(window, monkeypatch, tmp_path):  # noqa: F811
    """@brief Il worker passa al servizio la cache del progetto, non un percorso fisso."""
    seen = {}

    def fake_extract(paths, photo_cache_dir=None, **kwargs):
        seen.update(kwargs, photo_cache_dir=photo_cache_dir)
        raise ext.ExtractionError("stop")

    monkeypatch.setattr(gui, "extract_pin_header_params", fake_extract)
    worker = gui.ExtractWorker([tmp_path / "a.png"], model="m")
    worker.photo_cache_dir = window._photo_cache_dir()
    worker.run()
    assert seen["photo_cache_dir"] == window._project_dir() / PHOTO_CACHE_SUBDIR


def test_the_service_has_no_shared_fallback_folder():
    """@brief Negativo: il servizio non conosce piu' `generated/photo-cache`."""
    source = Path(ext.__file__).read_text(encoding="utf-8")
    assert '"photo-cache"' not in source and "'photo-cache'" not in source


def _image(path):
    Image.new("RGB", (40, 30), "blue").save(path)
    return path


def test_a_rename_carries_cache_and_review_along(window, tmp_path):  # noqa: F811
    """@brief Positivo: photo-cache/ (anche le sottocartelle) e photo-review/ seguono il nome.

    Prima si spostavano solo documenti e diario: la revisione restava nella
    cartella vecchia e «Rivedi corpi» non la trovava sotto il nome nuovo.
    """
    DocumentSet(window._docs_dir()).import_file("top", _image(tmp_path / "a.png"))
    old = window._project_dir()
    (old / PHOTO_CACHE_SUBDIR / "yolo-audit").mkdir(parents=True)
    (old / PHOTO_CACHE_SUBDIR / "global-inventory-abc.json").write_text("{}", encoding="utf-8")
    (old / PHOTO_CACHE_SUBDIR / "yolo-audit" / "yolo-1.json").write_text("[]", encoding="utf-8")
    (old / REVIEW_BUNDLE_SUBDIR).mkdir()
    (old / REVIEW_BUNDLE_SUBDIR / "proposals.json").write_text("{}", encoding="utf-8")
    assert window._migrate_docs_to_new_name("NomeNuovo") == "NomeNuovo"
    new = old.parent / "NomeNuovo"
    assert (new / PHOTO_CACHE_SUBDIR / "global-inventory-abc.json").is_file()
    assert (new / PHOTO_CACHE_SUBDIR / "yolo-audit" / "yolo-1.json").is_file()
    assert (new / REVIEW_BUNDLE_SUBDIR / "proposals.json").is_file()
    assert not (old / PHOTO_CACHE_SUBDIR).exists() and not (old / REVIEW_BUNDLE_SUBDIR).exists()


def test_a_rename_never_overwrites_what_the_destination_already_has(window, tmp_path):  # noqa: F811
    """@brief Negativo: un file gia' presente nella destinazione non viene toccato."""
    DocumentSet(window._docs_dir()).import_file("top", _image(tmp_path / "a.png"))
    old = window._project_dir()
    (old / REVIEW_BUNDLE_SUBDIR).mkdir(parents=True)
    (old / REVIEW_BUNDLE_SUBDIR / "proposals.json").write_text("vecchia", encoding="utf-8")
    (old / REVIEW_BUNDLE_SUBDIR / "photo.png").write_text("foto", encoding="utf-8")
    new = old.parent / "NomeNuovo"
    (new / REVIEW_BUNDLE_SUBDIR).mkdir(parents=True)
    (new / REVIEW_BUNDLE_SUBDIR / "proposals.json").write_text("destinazione", encoding="utf-8")
    window._migrate_docs_to_new_name("NomeNuovo")
    assert (new / REVIEW_BUNDLE_SUBDIR / "proposals.json").read_text(encoding="utf-8") == "destinazione"
    assert (old / REVIEW_BUNDLE_SUBDIR / "proposals.json").read_text(encoding="utf-8") == "vecchia"
    assert (new / REVIEW_BUNDLE_SUBDIR / "photo.png").is_file()
    assert "esistevano gia'" in window.ai_progress_text.toPlainText()


def test_a_corrected_default_text_reaches_an_existing_database(tmp_path):
    """@brief Positivo: la vecchia voce predefinita viene aggiornata all'apertura.

    Negativo: una voce modificata dall'utente resta com'e'.
    """
    path = tmp_path / "profiles.sqlite3"
    ProfileStore(path)
    key = "help.settings.stage.1"
    old_it = SUPERSEDED_DEFAULTS[key]["it"][0]
    assert "generated/photo-cache" in old_it
    with sqlite3.connect(path) as db:
        db.execute("UPDATE translations SET text=? WHERE key=? AND language='it'", (old_it, key))
        db.execute("UPDATE translations SET text='Testo mio' WHERE key=? AND language='en'", (key,))
    store = ProfileStore(path)
    assert I18n(store, "it").tr(key) == DEFAULT_TRANSLATIONS[key]["it"]
    assert "photo-cache del progetto" in I18n(store, "it").tr(key)
    assert I18n(store, "en").tr(key) == "Testo mio"
