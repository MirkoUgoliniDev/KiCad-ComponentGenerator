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


def test_docs_follow_component_rename(window, tmp_path):
    window.field_library_root.setText(str(tmp_path))
    window.field_name.setText("NOME_VECCHIO")

    old_docs = window._docs_dir()
    old_docs.mkdir(parents=True)
    (old_docs / "datasheet.pdf").write_bytes(b"%PDF fake")
    (old_docs / "foto.png").write_bytes(b"PNG fake")

    window._on_extracted(_extracted("NOME_NUOVO"))

    assert window.field_name.text() == "NOME_NUOVO"
    assert sorted(p.name for p in window._docs_dir().iterdir()) == ["datasheet.pdf", "foto.png"]
    assert window.docs_list.count() == 2


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
