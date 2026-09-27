"""@brief Hand editor of the mounted bodies: edits the stored data, not a table.

User request (2026-09-26): the components table was removed, but the data must
stay editable even without the graphical table and without the photo.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
pytest.importorskip("PyQt6")

from kicadgen.gui.component_editor_dialog import ComponentEditorDialog
from kicadgen.services.datasheet_extractor import COMPONENT_KINDS
from tests.test_photo_inventory_gui import gui


@pytest.fixture(autouse=True)
def qt_app(qapp):
    """@brief Un'applicazione Qt anche per i test che non aprono la finestra principale."""
    return qapp


def _body(**extra):
    base = dict(name="U1", kind="ic", x_mm=1.0, y_mm=2.0, length_mm=4.0, width_mm=3.0,
                height_mm=1.0, rotation_deg=0.0, color_hex="#222222", marking="",
                bbox_basis="body", package_evidence="SOIC-8 seen",
                model_match={"status": "auto_matched"}, model_selection={"path": "x.step"},
                vendor_data={"pins": [1, 2]})
    base.update(extra)
    return base


def test_moving_keeps_the_evidence_resizing_drops_it():
    """@brief Positivo e negativo: spostare non tocca le prove, ridimensionare si'."""
    dialog = ComponentEditorDialog([_body(), _body(name="U2")], COMPONENT_KINDS)
    dialog.table.cellWidget(0, 2).setValue(5.0)          # X: stesso corpo, spostato
    dialog.table.cellWidget(1, 4).setValue(8.0)          # Lung.: rettangolo diverso
    moved, resized = dialog.components()
    assert moved["x_mm"] == 5.0
    assert moved["model_match"] == {"status": "auto_matched"}
    assert moved["bbox_basis"] == "body" and moved["vendor_data"] == {"pins": [1, 2]}
    assert resized["length_mm"] == 8.0 and resized["bbox_basis"] == "unknown"
    assert "model_match" not in resized and "package_evidence" not in resized
    assert resized["vendor_data"] == {"pins": [1, 2]}, "i metadati del rilevamento restano"


def test_a_new_type_forgets_only_the_model_choice():
    dialog = ComponentEditorDialog([_body()], COMPONENT_KINDS)
    combo = dialog.table.cellWidget(0, 1)
    combo.setCurrentIndex(combo.findData("resistor"))
    edited, = dialog.components()
    assert edited["kind"] == "resistor"
    assert "model_match" not in edited and "model_selection" not in edited
    assert edited["package_evidence"] == "SOIC-8 seen"


def test_add_and_remove_rows():
    dialog = ComponentEditorDialog([_body(), _body(name="U2")], COMPONENT_KINDS)
    dialog.table.setCurrentCell(0, 0)
    dialog._remove_selected()
    dialog.btn_add.click()
    survivor, added = dialog.components()
    assert survivor["name"] == "U2" and survivor["vendor_data"] == {"pins": [1, 2]}
    assert added["name"] == "Componente" and "vendor_data" not in added


def test_cancel_changes_nothing_and_apply_writes_the_stored_data(window, monkeypatch):  # noqa: F811
    """@brief Funziona senza foto: i dati stanno nella struttura della finestra."""
    window._photo_png = None
    window._set_photo_components([_body()])
    assert not window.btn_edit_components_3d.isHidden()

    def cancel(self):
        self.table.cellWidget(0, 0).setText("CAMBIATO")
        return 0
    monkeypatch.setattr(ComponentEditorDialog, "exec", cancel)
    window._edit_components()
    assert window._photo_rows[0]["name"] == "U1"

    def apply(self):
        self.table.cellWidget(0, 0).setText("U1-corretto")
        self._apply()
        return 1
    monkeypatch.setattr(ComponentEditorDialog, "exec", apply)
    window._edit_components(select_row=0)
    assert window._photo_rows[0]["name"] == "U1-corretto"
    assert window._components_from_table()[0].name == "U1-corretto"


def test_the_editor_is_offered_only_with_bodies(window):  # noqa: F811
    window._set_photo_components([])
    assert window.btn_edit_components_3d.isHidden()


def test_a_new_rotation_forgets_the_model_choice_like_the_old_table():
    """@brief Positivo: ruotare il corpo rende il modello scelto un'ipotesi sull'orientamento."""
    dialog = ComponentEditorDialog([_body()], COMPONENT_KINDS)
    rotation = dialog.table.cellWidget(0, 7)
    rotation.setCurrentIndex(rotation.findData(90.0))
    edited, = dialog.components()
    assert edited["rotation_deg"] == 90.0
    assert "model_match" not in edited and "model_selection" not in edited


def test_an_untouched_row_keeps_every_evidence():
    """@brief Negativo: aprire e applicare senza cambiare nulla non tocca le prove."""
    original = _body()
    edited, = ComponentEditorDialog([original], COMPONENT_KINDS).components()
    for key in ("model_match", "model_selection", "package_evidence", "bbox_basis"):
        assert edited[key] == original[key]
