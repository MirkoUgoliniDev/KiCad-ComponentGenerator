"""@brief Dialogs for the archive of accepted KiCad model matches.

- `ConsolidateDialog` opens from «Accetto, mi piace» after the solid is
  generated. Only components with an attached usable model are listed,
  pre-checked with opt-out per row; excluded components are counted below.
- `AcceptedModelsDialog` lists the archive entries and deletes wrong ones: a
  match accepted by mistake propagates to later boards, so removing it must be
  easy.
"""
from __future__ import annotations

from datetime import datetime
import sqlite3
from pathlib import Path

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QColor, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QStyle, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout,
)

from ..services.accepted_models import (
    AcceptedModelStore, AcceptedModelsError, IMAGE_CROP_VERSION, category_of, eligibility,
)


def _millimetres(*values) -> str:
    """Millimetre values with two decimals, "?" for a missing value."""
    parts = []
    for value in values:
        try:
            parts.append(f"{float(value):.2f}")
        except (TypeError, ValueError):
            parts.append("?")
    return " × ".join(parts)


#: Background of the archive rows this board uses: the accent, faint.
_BOARD_ROW = QColor(137, 180, 250, 48)


def _item(text, *, data=None) -> QTableWidgetItem:
    """Read-only cell: rows are inspected and checked, never edited."""
    item = QTableWidgetItem(str(text))
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    if data is not None:
        item.setData(Qt.ItemDataRole.UserRole, data)
    return item


class ConsolidateDialog(QDialog):
    """@brief Confirm which components to remember, showing why others are excluded."""

    #: Colonna della spunta (e del nome del componente).
    COLUMN_NAME = 1

    def __init__(self, components: list[dict], parent=None, i18n=None):
        """@brief Build the list from the board components.

        @param components Components with matching metadata (model_path,
            model_match, model_selection) as read from the table.
        @param parent Owner window.
        @param i18n Optional translator for text composed at runtime.
        """
        super().__init__(parent)
        self.i18n = i18n
        self.setWindowTitle("Ricorda questi modelli KiCad")
        self.resize(1180, 620)
        supplied = list(components or [])
        self._components = [component for component in supplied if eligibility(component)[0]]
        self._excluded_count = len(supplied) - len(self._components)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Sono mostrati solo i componenti con un modello KiCad associato. "
            "Conferma quelli verificati: modello e ritaglio saranno disponibili anche negli altri progetti. "
            "Togli la spunta a quelli che non ti convincono.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.table = QTableWidget(len(self._components), 6)
        self.table.setHorizontalHeaderLabels(
            ["Foto", "Componente", "Categoria", "Misure L × W (mm)", "Modello KiCad", "Stato"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 96)
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        self._eligible = []
        for row, component in enumerate(self._components):
            self.table.setRowHeight(row, 72)
            image_item = QTableWidgetItem()
            image_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            pixmap = QPixmap()
            image_png = component.get("_accepted_image_png")
            if image_png and pixmap.loadFromData(image_png, "PNG"):
                image_item.setData(Qt.ItemDataRole.DecorationRole, pixmap.scaled(
                    80, 60, Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation))
                image_item.setToolTip(self._tr("Ritaglio del componente dalla scheda"))
            else:
                image_item.setText("—")
            self.table.setItem(row, 0, image_item)
            ok, reason = eligibility(component)
            self._eligible.append(ok)
            name = _item(component.get("name") or "?")
            if ok:
                name.setFlags(name.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                # Una proposta provvisoria (rete SMD o geometria) non e' una
                # scelta di nessuno: entra nell'archivio solo se la spunti tu
                # (audit 2026-10-02: altrimenti l'archivio si riempiva da solo).
                provisional = (component.get("model_match") or {}).get("attachment") == "provisional"
                name.setCheckState(Qt.CheckState.Unchecked if provisional
                                   else Qt.CheckState.Checked)
            else:
                # Non spuntabile: non c'e' niente da ricordare, e il motivo lo dice.
                name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsUserCheckable
                              & ~Qt.ItemFlag.ItemIsEnabled)
            self.table.setItem(row, 1, name)
            self.table.setItem(row, 2, _item(category_of(component)))
            self.table.setItem(row, 3, _item(_millimetres(
                component.get("length_mm"), component.get("width_mm"))))
            model = component.get("model_path")
            self.table.setItem(row, 4, _item(Path(model).stem if ok else "—"))
            status = _item(self._tr("da ricordare") if ok else self._tr(reason))
            status.setToolTip(status.text())
            self.table.setItem(row, 5, status)
        # Il motivo va letto per intero (utente, 2026-09-25: «nessun modello
        # KiCad …» troncato): la colonna «Stato» va a capo e la riga si alza,
        # e il testo completo e' anche nel tooltip della cella.
        self.table.setWordWrap(True)
        self.table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.cellDoubleClicked.connect(self._show_component_crop)
        layout.addWidget(self.table, 1)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        # Testo composto a runtime e gia' tradotto da `_tr`: il passaggio di
        # `retranslate_widgets` non deve prenderlo come sorgente.
        self.summary.setProperty("i18n_skip", True)
        layout.addWidget(self.summary)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Ricorda i selezionati")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.table.itemChanged.connect(self._refresh_summary)
        self._refresh_summary()

    def _tr(self, source: str) -> str:
        """Translate runtime text when a translator is available."""
        return self.i18n.text(source) if self.i18n is not None else source

    def _refresh_summary(self, *_):
        chosen = len(self.selected_components())
        excluded = self._excluded_count
        self.summary.setText(self._tr("Da ricordare: {chosen}. Senza modello da ricordare: {excluded}.")
                             .format(chosen=chosen, excluded=excluded))
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(chosen > 0)

    def _show_component_crop(self, row: int, column: int) -> None:
        """Open a larger view of the row's PCB crop on double click."""
        if column != 0 or not 0 <= row < len(self._components):
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(self._components[row].get("_accepted_image_png", b""), "PNG"):
            return
        preview = QDialog(self)
        preview.setWindowTitle(str(self._components[row].get("name") or "Componente"))
        layout = QVBoxLayout(preview)
        label = QLabel()
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setPixmap(pixmap.scaled(640, 480, Qt.AspectRatioMode.KeepAspectRatio,
                                      Qt.TransformationMode.SmoothTransformation))
        layout.addWidget(label)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(preview.reject)
        layout.addWidget(close)
        preview.exec()

    def selected_components(self) -> list[dict]:
        """@brief Checked components, in table order.

        @return List of the original component mappings.
        """
        chosen = []
        for row, component in enumerate(self._components):
            item = self.table.item(row, self.COLUMN_NAME)
            if (self._eligible[row] and eligibility(component)[0] and item is not None
                    and item.checkState() == Qt.CheckState.Checked):
                chosen.append(component)
        return chosen


class AcceptedModelsDialog(QDialog):
    """@brief List archive entries and delete the wrong ones."""

    def __init__(self, store: AcceptedModelStore | None = None, parent=None, i18n=None,
                 components=None, roots=None):
        """@brief Build the dialog and load the entries.

        @param store Archive to show; default the user's archive.
        @param parent Owner window.
        @param i18n Optional translator for text composed at runtime.
        @param components Components of the board in view, or None (menu).
        @param roots 3D library roots, needed with `components`.
        @note With `components` the entries the archive would give to this
            board come first and are highlighted, with the component names in
            the tooltip. «Would give» is decided by `AcceptedModelStore.find`,
            the very rule used when matching: no second criterion that could
            disagree with what the generation does.
        """
        super().__init__(parent)
        self.i18n = i18n
        self.store = store or AcceptedModelStore()
        self._components = list(components or ())
        self._roots = list(roots or ())
        self.setWindowTitle("Archivio abbinamenti accettati")
        self.resize(1120, 540)
        layout = QVBoxLayout(self)
        self.path_label = QLabel()
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        # Porta un percorso dell'utente: non si traduce.
        self.path_label.setProperty("i18n_skip", True)
        layout.addWidget(self.path_label)
        self.table = QTableWidget(0, 10)
        self.table.setHorizontalHeaderLabels(
            ["Categoria", "Base", "Misure L × W (mm)", "Terminali", "Modello KiCad",
             "Rotazione", "Volte", "Ritagli", "Ultima accettazione", "Foto"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        header = self.table.horizontalHeader()
        for column, width in {
            0: 86, 1: 54, 2: 132, 3: 62, 5: 72, 6: 48, 7: 52, 8: 132,
        }.items():
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            self.table.setColumnWidth(column, width)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(9, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(9, 42)
        layout.addWidget(self.table, 1)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setProperty("i18n_skip", True)
        layout.addWidget(self.status_label)
        row = QHBoxLayout()
        self.btn_delete = QPushButton("Elimina le voci selezionate")
        self.btn_delete.setToolTip(
            "Toglie dall'archivio gli abbinamenti sbagliati: le schede future non li "
            "proporranno piu'. Non tocca i componenti gia' generati.")
        self.btn_delete.clicked.connect(self._delete_selected)
        row.addWidget(self.btn_delete)
        self.btn_preview = QPushButton("Mostra ritaglio")
        self.btn_preview.setEnabled(False)
        self.btn_preview.clicked.connect(self._show_selected_crop)
        row.addWidget(self.btn_preview)
        row.addStretch()
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        row.addWidget(close)
        layout.addLayout(row)
        self.table.itemSelectionChanged.connect(self._refresh_buttons)
        self.reload()

    def _tr(self, source: str) -> str:
        """Translate runtime text when a translator is available."""
        return self.i18n.text(source) if self.i18n is not None else source

    def reload(self) -> None:
        """@brief Reload the entries; an unreadable archive is reported, not hidden."""
        self.path_label.setText(str(self.store.path))
        board = {}
        try:
            entries = self.store.entries()
            self._entries_by_id = {entry["id"]: entry for entry in entries}
            board = self._board_matches()
        except (AcceptedModelsError, sqlite3.Error, OSError) as exc:
            entries = []
            self._entries_by_id = {}
            self.status_label.setText(str(exc))
        else:
            text = (self._tr("Nessun abbinamento accettato finora.") if not entries else
                    self._tr("Voci nell'archivio: {count}.").format(count=len(entries)))
            if self._components and entries:
                text += " " + self._tr(
                    "Usate da questa scheda: {count} (in cima, evidenziate).").format(
                        count=len(board))
            self.status_label.setText(text)
        # Prima le voci di questa scheda, nell'ordine dell'archivio.
        entries.sort(key=lambda entry: entry.get("id") not in board)
        self.table.setRowCount(len(entries))
        for index, entry in enumerate(entries):
            rotation = entry.get("rotation_deg")
            accepted = str(entry.get("last_accepted") or "")
            try:
                accepted = datetime.fromisoformat(accepted).astimezone().strftime("%Y-%m-%d %H:%M")
            except ValueError:
                pass
            values = [entry.get("category"), entry.get("basis"),
                      _millimetres(entry.get("long_mm"), entry.get("short_mm")),
                      entry.get("terminal_count") if entry.get("terminal_count") is not None else "—",
                      entry.get("model_name"),
                      self._tr("per ritaglio") if entry.get("image_count") else self._tr("da verificare"),
                      entry.get("samples"), entry.get("image_count", 0), accepted]
            names = board.get(entry.get("id"))
            for column, value in enumerate(values):
                item = _item(value, data=entry.get("id") if column == 0 else None)
                usage = (self._tr("Usata da questa scheda per: {names}").format(
                    names=", ".join(names)) if names else "")
                # Il percorso del modello resta nel tooltip della sua colonna,
                # anche sulle righe evidenziate (prima veniva sovrascritto).
                tip = [str(entry.get("model_ref") or "")] if column == 4 else []
                tip += [usage] if usage else []
                if tip:
                    item.setToolTip("\n".join(part for part in tip if part))
                if names:
                    item.setBackground(_BOARD_ROW)
                self.table.setItem(index, column, item)
            preview = QToolButton(self.table)
            preview.setAutoRaise(True)
            preview.setIcon(self.style().standardIcon(
                QStyle.StandardPixmap.SP_FileDialogContentsView))
            preview.setIconSize(QSize(18, 18))
            preview.setToolTip(self._tr("Mostra i ritagli verificati"))
            preview.setEnabled(bool(entry.get("image_count", 0)))
            preview.clicked.connect(lambda _checked=False, entry_id=entry.get("id"):
                                    self._show_entry_crop(entry_id))
            self.table.setCellWidget(index, 9, preview)
        self._refresh_buttons()

    def _board_matches(self) -> dict:
        """Entry id -> components actually attached from this archive in the report."""
        matches = {}
        for index, component in enumerate(self._components):
            report = component.get("model_match") or {}
            selection = component.get("model_selection") or {}
            if (report.get("attachment") != "accepted_library"
                    or report.get("status") == "import_failed"
                    or selection.get("status") in ("confirmed", "rejected")):
                continue
            entry_id = (report.get("accepted_entry") or {}).get("id")
            if type(entry_id) is not int or entry_id not in self._entries_by_id:
                continue
            name = str(component.get("name") or "").strip() or f"#{index + 1}"
            matches.setdefault(entry_id, []).append(name)
        return matches

    def _selected_ids(self) -> list[int]:
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        ids = []
        for row in rows:
            item = self.table.item(row, 0)
            value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
            if type(value) is int:
                ids.append(value)
        return ids

    def _refresh_buttons(self) -> None:
        ids = self._selected_ids()
        self.btn_delete.setEnabled(bool(ids))
        entry = self._entries_by_id.get(ids[0]) if len(ids) == 1 else None
        self.btn_preview.setEnabled(bool(entry and entry.get("image_count", 0)))

    def _show_selected_crop(self) -> None:
        """Open the latest verified crop of the selected accepted model."""
        ids = self._selected_ids()
        if len(ids) != 1:
            return
        self._show_entry_crop(ids[0])

    def _show_entry_crop(self, entry_id: int) -> None:
        """Open the latest verified crop for one archive entry."""
        try:
            examples = self.store.images(entry_id)
        except (AcceptedModelsError, sqlite3.Error, OSError) as exc:
            self.status_label.setText(str(exc))
            return
        if not examples:
            return
        entry = self._entries_by_id.get(entry_id, {})
        preview = QDialog(self)
        preview.setWindowTitle(self._tr("Ritaglio verificato: {model}").format(
            model=entry.get("model_name", "")))
        layout = QVBoxLayout(preview)
        label = QLabel()
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setMinimumSize(480, 360)
        layout.addWidget(label)
        index = [0]
        info = QLabel()
        info.setObjectName("cropLegend")
        info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(info)
        controls = QHBoxLayout()
        previous = QPushButton(self._tr("‹ Precedente"))
        next_image = QPushButton(self._tr("Successivo ›"))
        previous.setObjectName("cropPrevious")
        next_image.setObjectName("cropNext")
        previous.setVisible(len(examples) > 1)
        next_image.setVisible(len(examples) > 1)
        controls.addStretch()
        controls.addWidget(previous)
        controls.addWidget(next_image)
        controls.addStretch()
        layout.addLayout(controls)

        def show_image(position: int) -> None:
            index[0] = position
            previous.setEnabled(position > 0)
            next_image.setEnabled(position + 1 < len(examples))
            label.clear()
            example = examples[position]
            pixmap = QPixmap()
            if not pixmap.loadFromData(example["image_png"], "PNG"):
                info.setText(self._tr("Ritaglio immagine non leggibile."))
                return
            label.setPixmap(pixmap.scaled(640, 480, Qt.AspectRatioMode.KeepAspectRatio,
                                          Qt.TransformationMode.SmoothTransformation))
            component_id = str(example.get("component_name") or "").strip()
            if not component_id:
                component_id = self._tr("ID ritaglio {id}").format(id=example["id"])
            info.setText(self._tr("ID: {component} · ritaglio {current} di {total}").format(
                component=component_id, current=position + 1, total=len(examples)))
            angle = example.get("model_rotation_deg")
            info.setText(info.text() + "\n" + self._tr("Rotazione modello") + ": " +
                         (f"{angle:g}°" if angle is not None else self._tr("da verificare")))
            if example.get("crop_version") != IMAGE_CROP_VERSION:
                info.setText(info.text() + " · " + self._tr(
                    "ritaglio precedente, escluso dal confronto"))
            previous.setEnabled(position > 0)
            next_image.setEnabled(position + 1 < len(examples))

        previous.clicked.connect(lambda: show_image(index[0] - 1))
        next_image.clicked.connect(lambda: show_image(index[0] + 1))
        show_image(0)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(preview.reject)
        layout.addWidget(close)
        preview.exec()

    def _delete_selected(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        try:
            removed = self.store.delete(ids)
        except AcceptedModelsError as exc:
            self.status_label.setText(str(exc))
            return
        self.reload()
        self.status_label.setText(
            self._tr("Voci eliminate: {count}.").format(count=removed))
