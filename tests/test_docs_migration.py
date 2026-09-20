"""Regressione: i documenti devono seguire il componente quando l'estrazione AI
propone un nome diverso.

La cartella docs/ vive dentro la cartella del componente, che prende il nome dal
campo "Nome" -- che l'estrazione AI riscrive. Senza migrazione i file caricati
prima restavano nella vecchia cartella e sparivano dalla lista, rendendo
impossibile riusarli (es. la foto del prodotto da cui si deducono i fillet).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

pytest.importorskip("PyQt6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox

from kicadgen.gui.main_window import MainWindow
from kicadgen.services.datasheet_extractor import ExtractedParams


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def window(qapp, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    return MainWindow()


def _extracted(name: str, **kwargs) -> ExtractedParams:
    base = dict(
        name=name, description="d", tags="t", rows=2, cols=8,
        pitch_mm=2.54, row_spacing_mm=10.16, pad_diameter_mm=1.0,
        drill_mm=0.5, confidence="high", notes="",
    )
    base.update(kwargs)
    return ExtractedParams(**base)


def _with_documents(window, tmp_path, name):
    window.field_library_root.setText(str(tmp_path))
    window.field_name.setText(name)
    docs = window._docs_dir()
    docs.mkdir(parents=True)
    (docs / "datasheet.pdf").write_bytes(b"%PDF fake")
    (docs / "foto.png").write_bytes(b"PNG fake")
    return docs


def test_extraction_proposes_a_name_and_does_not_take_it(window, tmp_path):
    """Il nome lo decide chi lavora; l'estrazione lo propone soltanto.

    Riscriverlo da soli spostava docs/ a ogni analisi e lasciava in area di
    lavoro una cartella per ogni nome mai proposto.
    """
    old_docs = _with_documents(window, tmp_path, "NOME_VECCHIO")

    window._on_extracted(_extracted("NOME_NUOVO"))

    assert window.field_name.text() == "NOME_VECCHIO"
    assert window._suggested_name == "NOME_NUOVO"
    assert window.widget_name_suggestion.isVisibleTo(window)
    assert window._docs_dir() == old_docs
    assert sorted(p.name for p in old_docs.iterdir()) == ["datasheet.pdf", "foto.png"]
    assert not (tmp_path / "NOME_NUOVO").exists()


def test_docs_follow_the_component_when_the_proposal_is_accepted(window, tmp_path):
    _with_documents(window, tmp_path, "NOME_VECCHIO")
    window._on_extracted(_extracted("NOME_NUOVO"))

    window.btn_use_suggested_name.click()

    assert window.field_name.text() == "NOME_NUOVO"
    assert sorted(p.name for p in window._docs_dir().iterdir()) == ["datasheet.pdf", "foto.png"]
    assert window.docs_list.count() == 2
    assert not window.widget_name_suggestion.isVisibleTo(window)


def test_an_unnamed_component_takes_the_proposal_without_asking(window, tmp_path):
    """Senza un nome scelto non c'e' nulla da rispettare, e "NUOVO_COMPONENTE"
    come cartella non serve a nessuno."""
    window.field_library_root.setText(str(tmp_path))
    window.field_name.setText("")

    window._on_extracted(_extracted("NOME_NUOVO"))

    assert window.field_name.text() == "NOME_NUOVO"
    assert not window.widget_name_suggestion.isVisibleTo(window)


def test_extracted_fillets_populate_annotation_table(window, tmp_path):
    window.field_library_root.setText(str(tmp_path))
    window.field_name.setText("COMP")

    window._on_extracted(_extracted("COMP", edge_fillets=[
        {"edge": "top-front", "radius_mm": 1.0},
        {"edge": "top-back", "radius_mm": 1.0},
    ]))

    assert len(window.fillet_annotations) == 2
    assert window.table_annotations.rowCount() == 2


def test_invalid_extracted_fillets_are_skipped(window, tmp_path):
    window.field_library_root.setText(str(tmp_path))
    window.field_name.setText("COMP")

    window._on_extracted(_extracted("COMP", edge_fillets=[
        {"edge": "spigolo-inesistente", "radius_mm": 1.0},
        {"edge": "top-left", "radius_mm": 0.5},
    ]))

    assert [f.edge for f in window.fillet_annotations] == ["top-left"]
