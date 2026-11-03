"""@brief Hand editor of the bodies mounted on a module, in its own window.

@details The «Componenti sul modulo» table lived under the 3D view until the
parameter panels were removed (2026-09-26). The photo review needs the
calibrated photo in memory; this editor does not, so a body can always be
corrected by hand: name, type, position, size, rotation, color and marking.

The dialog works on copies of the stored photo components and returns the
edited list only on «Applica». Evidence tied to what was changed is dropped,
with the same rules the old table applied:

- a new length or width means the photo rectangle no longer describes the
  body: its geometric evidence (bbox basis, package evidence, terminal
  geometry) and the model match built on it are removed;
- a new type or a new rotation makes the chosen KiCad model a guess about
  another part or another orientation: the model match and selection are
  removed (the old table did the same on every combo change).

Position, height, color and marking keep the evidence: they move or paint the
same body. A height that is an estimate (`component_state.height_is_estimated`)
is marked «stimata» in its cell; changing it by hand records it as the
user's reading (`component_state.mark_height_measured`).

A cell nobody edited gives back the SOURCE value exactly as it was, missing
or None included: the cell has to show something (a missing height shows the
new body's 0.6 mm, a missing colour #303030, a rotation of -90 reads 270°),
but what it shows only to be readable is not a value the user entered, and a
plain «Applica» must not turn an estimate or an absence into a measurement.
"""
from copy import deepcopy

from PyQt6.QtCore import pyqtSignal

from .component_state import height_is_estimated, mark_height_measured, mark_height_placeholder
from ..services.smd_component_classifier import drop_classifier_identity
from .theme import spin_box_width_for_values
from PyQt6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QDialogButtonBox,
                             QDoubleSpinBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                             QPushButton, QTableWidget, QVBoxLayout)

#: Column titles, Italian sources translated by the window's i18n pass.
COLUMNS = ("Nome", "Tipo", "X", "Y", "Lung.", "Larg.", "Alt.", "Rot.", "Colore", "Marcatura")
#: (column, key, minimum) of the numeric cells, in mm.
_NUMBERS = ((2, "x_mm", -300.0), (3, "y_mm", -300.0), (4, "length_mm", 0.05),
            (5, "width_mm", 0.05), (6, "height_mm", 0.05))
#: Keys that describe the photo rectangle; they lose meaning when it is resized.
_GEOMETRY_EVIDENCE = ("package_evidence", "terminal_geometry", "model_match", "model_selection")
#: Keys that describe the KiCad model chosen for the body type.
_MODEL_EVIDENCE = ("model_match", "model_selection")
#: Marker of a key the source component did not have.
_MISSING = object()
#: Columns whose shown text or choice is compared with the opening one.
_NAME_COLUMN, _ROTATION_COLUMN, _COLOR_COLUMN = 0, 7, 8
#: A new body, as the old «Aggiungi componente» created it.
NEW_COMPONENT = dict(name="Componente", kind="generic", x_mm=0.0, y_mm=0.0, length_mm=2.0,
                     width_mm=1.2, height_mm=0.6, rotation_deg=0.0, color_hex="#303030",
                     marking="")


def _number(value) -> float:
    """A stored number as the cells read it: missing or None reads 0.0."""
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _angle(value) -> float:
    """A stored rotation as the «Rot.» cell reads it, in [0, 360) degrees."""
    return _number(value) % 360


class ComponentEditorDialog(QDialog):
    """@brief Edit the mounted bodies by hand; emits the edited list on «Applica»."""

    #: The edited components (photo component dicts), emitted once on «Applica».
    components_applied = pyqtSignal(list)

    def __init__(self, components, kinds, parent=None, *, select_row=None):
        """@brief Build the table from copies of the components.

        @param components Stored photo components (dicts with x_mm, y_mm, ...).
        @param kinds Component types offered in the «Tipo» column.
        @param parent Owner window.
        @param select_row Optional row to select and scroll to (3D right click).
        """
        super().__init__(parent)
        self.setWindowTitle("Modifica componenti")
        self.setModal(True)
        self.resize(1100, 520)
        self._kinds = tuple(kinds)
        #: Copies of the input, one per row, in row order; None for added rows.
        self._originals = []
        #: Full dict of each row (detection metadata travels with the row).
        self._rows_data = []
        #: Per row, key -> (source value or _MISSING, value the cell showed
        #: when opened: spin box value after its own rounding, name/colour
        #: text, rotation choice). A cell still showing that value was not
        #: edited and gives back the source value, never the shown one.
        self._shown_numbers = []
        layout = QVBoxLayout(self)
        note = QLabel("Origine al centro del modulo, X a destra, Y in basso come nel footprint; "
                      "Lung. = asse lungo del componente, Rot. = come sta sul modulo. "
                      "Nessuna richiesta AI: dopo «Applica» il modello STEP si rigenera con FreeCAD.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(9, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        layout.addWidget(self.table, 1)
        for component in components:
            self._add_row(deepcopy(component), original=True)
        buttons_row = QHBoxLayout()
        self.btn_add = QPushButton("Aggiungi componente")
        self.btn_add.clicked.connect(self._add_new_row)
        self.btn_remove = QPushButton("Rimuovi componente selezionato")
        self.btn_remove.clicked.connect(self._remove_selected)
        buttons_row.addWidget(self.btn_add)
        buttons_row.addWidget(self.btn_remove)
        buttons_row.addStretch()
        layout.addLayout(buttons_row)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Apply).setText("Applica")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Annulla")
        self.buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self._apply)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        if select_row is not None and 0 <= select_row < self.table.rowCount():
            self.table.selectRow(select_row)

    def _add_row(self, component: dict, *, original: bool) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self._originals.append(deepcopy(component) if original else None)
        name_text = str(component.get("name") or "")
        self.table.setCellWidget(row, _NAME_COLUMN, QLineEdit(name_text))
        kind = QComboBox()
        for key in self._kinds:
            kind.addItem(key, key)
        current = component.get("kind") or "generic"
        if kind.findData(current) < 0:
            kind.addItem(current, current)
        kind.setCurrentIndex(max(kind.findData(current), 0))
        self.table.setCellWidget(row, 1, kind)
        shown = {}
        for column, key, minimum in _NUMBERS:
            value = float(component.get(key, NEW_COMPONENT[key]) or 0.0)
            spin = QDoubleSpinBox()
            # Six decimals: the photographic corrections of small packages
            # are below a hundredth of a millimetre.
            spin.setDecimals(6)
            spin.setRange(min(minimum, value), max(300.0, value))
            spin.setSingleStep(0.1)
            spin.setValue(value)
            shown[key] = (component.get(key, _MISSING), spin.value())
            if key == "height_mm" and height_is_estimated(component):
                # Same cell, but it reads as an estimate and not a measurement.
                spin.setSuffix(" ~")
                tip = "Altezza stimata, non misurata: correggila se la conosci."
                i18n = getattr(self.parent(), "i18n", None)
                spin.setToolTip(i18n.text(tip) if i18n is not None else tip)
                spin.setStyleSheet("color: #f9e2af;")
            self.table.setCellWidget(row, column, spin)
            self._fit_column_to(column, spin)
        rotation = QComboBox()
        for degrees in (0, 90, 180, 270):
            rotation.addItem(f"{degrees}°", float(degrees))
        angle = float(component.get("rotation_deg") or 0.0) % 360
        if rotation.findData(angle) < 0:
            rotation.addItem(f"{angle:g}°", angle)
        rotation.setCurrentIndex(max(rotation.findData(angle), 0))
        self.table.setCellWidget(row, _ROTATION_COLUMN, rotation)
        color_text = str(component.get("color_hex") or "#303030")
        color = QLineEdit(color_text)
        color.setMaxLength(7)
        self.table.setCellWidget(row, _COLOR_COLUMN, color)
        shown["name"] = (component.get("name", _MISSING), name_text)
        shown["rotation_deg"] = (component.get("rotation_deg", _MISSING),
                                 rotation.currentData())
        # setMaxLength(7) truncates a longer source text: compare with what
        # the cell really shows.
        shown["color_hex"] = (component.get("color_hex", _MISSING), color.text())
        self.table.setCellWidget(row, 9, QLineEdit(str(component.get("marking") or "")))
        # Data not shown (detection metadata) travel with the row.
        self._rows_data.append(component)
        self._shown_numbers.append(shown)

    def _fit_column_to(self, column: int, spin: QDoubleSpinBox) -> None:
        """Widen a numeric column so the cell shows its widest value whole.

        The default section width (about 100 px) left the six-decimal values
        clipped once the style sheet's button column was drawn («1,600000»
        read «1,6000», HANDOFF 138, C3). The width is measured on the spin box
        itself (`spin_box_width_for_values`: range, decimals, suffix and the
        chrome of the current style), so another font, style or range needs no
        change here. The column only grows: a later row with a narrower range
        must not clip an earlier one, and a column the user widened stays so.
        """
        # The grid line is drawn inside the section: the cell is 1 px narrower.
        needed = spin_box_width_for_values(spin) + (1 if self.table.showGrid() else 0)
        if self.table.columnWidth(column) < needed:
            self.table.setColumnWidth(column, needed)

    def _add_new_row(self) -> None:
        """Add a body by hand; its height is a placeholder until typed."""
        component = dict(NEW_COMPONENT)
        mark_height_placeholder(component)
        self._add_row(component, original=False)

    def _remove_selected(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        self.table.removeRow(row)
        del self._originals[row]
        del self._rows_data[row]
        del self._shown_numbers[row]

    @staticmethod
    def _keep_or_set(entry: dict, key: str, shown: tuple, current, value) -> bool:
        """Give back the source value of an unedited cell, else store `value`.

        @param entry Row dict being rebuilt (a copy of the source component).
        @param key Component key of the cell.
        @param shown (source value or _MISSING, what the cell showed when opened).
        @param current What the cell shows now (text, choice or spin value).
        @param value Value to store when the cell was edited.
        @return True when the cell was edited.
        """
        source, opened = shown
        if current == opened:
            if source is _MISSING:
                entry.pop(key, None)
            else:
                entry[key] = source
            return False
        entry[key] = value
        return True

    def components(self) -> list:
        """@brief The components as the table shows them now, evidence adjusted.

        @return New dicts: detection metadata of each row plus its edited values.
        @note A cell nobody edited keeps the source value as it was, absent
            and None included; only an edited height is recorded as measured.
        """
        result = []
        for row in range(self.table.rowCount()):
            entry = deepcopy(self._rows_data[row])
            original = self._originals[row]
            shown = self._shown_numbers[row]
            name = self.table.cellWidget(row, _NAME_COLUMN).text()
            self._keep_or_set(entry, "name", shown["name"], name,
                              name.strip() or "Componente")
            entry["kind"] = self.table.cellWidget(row, 1).currentData() or "generic"
            edited_height = False
            for column, key, _minimum in _NUMBERS:
                current = self.table.cellWidget(row, column).value()
                # The spin box rounds to its decimals and shows a stand-in for
                # a missing value: a cell nobody edited still shows that and
                # must give back the source, or every long-decimal row would
                # read as edited and lose its evidence on a plain «Applica»
                # (Apply), and a missing height would become a measured one.
                edited = self._keep_or_set(entry, key, shown[key], current, current)
                edited_height = edited_height or (edited and key == "height_mm")
            if edited_height:
                mark_height_measured(entry)
            rotation = float(self.table.cellWidget(row, _ROTATION_COLUMN).currentData() or 0.0)
            self._keep_or_set(entry, "rotation_deg", shown["rotation_deg"], rotation, rotation)
            color = self.table.cellWidget(row, _COLOR_COLUMN).text()
            self._keep_or_set(entry, "color_hex", shown["color_hex"], color,
                              color.strip() or "#303030")
            entry["marking"] = self.table.cellWidget(row, 9).text().strip()
            if original is not None:
                # Compared as the cells read them: a source -90 and an
                # unedited 270° cell are the same orientation.
                turned = (_angle(entry.get("rotation_deg"))
                          != _angle(original.get("rotation_deg")))
                retyped = entry["kind"] != (original.get("kind") or "generic")
                resized = any(abs(_number(entry.get(key)) - _number(original.get(key))) > 1e-9
                              for key in ("length_mm", "width_mm"))
                if resized:
                    entry["bbox_basis"] = "unknown"
                    for key in _GEOMETRY_EVIDENCE:
                        entry.pop(key, None)
                elif retyped or turned:
                    for key in _MODEL_EVIDENCE:
                        entry.pop(key, None)
                # The body the SMD network saw is no longer this one: type,
                # size, position or rotation changed by hand remove its
                # identity and its pose, which would otherwise choose model
                # and orientation in place of the correction (audit 2026-10-02).
                moved = any(entry.get(key) != original.get(key) for key in ("x_mm", "y_mm"))
                if resized or moved or retyped or turned:
                    drop_classifier_identity(entry)
            result.append(entry)
        return result

    def _apply(self) -> None:
        self.components_applied.emit(self.components())
        self.accept()
