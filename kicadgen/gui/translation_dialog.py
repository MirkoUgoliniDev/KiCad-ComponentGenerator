"""@brief Advanced-user CRUD editor for the SQLite translation catalog."""

from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from ..services.analysis_profiles import ProfileError
from ..services.i18n import LANGUAGES


class TranslationDialog(QDialog):
    """@brief Edit translation keys and localized values stored in SQLite."""

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("Translation catalog")
        self.setAccessibleName("Translation catalog editor")
        self.setModal(True)
        self.resize(820, 560)
        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Language:"))
        self.language = QComboBox()
        for code, label in LANGUAGES.items():
            self.language.addItem(label, code)
        self.language.currentIndexChanged.connect(self._reload)
        controls.addWidget(self.language)
        controls.addStretch()
        add = QPushButton("Add key")
        add.clicked.connect(self._add_row)
        controls.addWidget(add)
        remove = QPushButton("Remove selected")
        remove.clicked.connect(self._remove_row)
        controls.addWidget(remove)
        layout.addLayout(controls)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(("Key", "Text"))
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.table)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save |
                                   QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._reload()

    def _reload(self) -> None:
        """@brief Load the selected language rows from SQLite."""
        rows = self.store.list_translations(self.language.currentData())
        self.table.setRowCount(0)
        for row in rows:
            index = self.table.rowCount()
            self.table.insertRow(index)
            self.table.setItem(index, 0, QTableWidgetItem(row["key"]))
            self.table.setItem(index, 1, QTableWidgetItem(row["text"]))

    def _add_row(self) -> None:
        """@brief Add an empty editable translation row."""
        index = self.table.rowCount()
        self.table.insertRow(index)
        self.table.setItem(index, 0, QTableWidgetItem("new.key"))
        self.table.setItem(index, 1, QTableWidgetItem(""))
        self.table.setCurrentCell(index, 0)

    def _remove_row(self) -> None:
        """@brief Remove the selected translation row from the editor."""
        rows = sorted({item.row() for item in self.table.selectedItems()}, reverse=True)
        for row in rows:
            self.table.removeRow(row)

    def _save(self) -> None:
        """@brief Validate and persist edited translation rows."""
        language = self.language.currentData()
        try:
            for row in range(self.table.rowCount()):
                key = self.table.item(row, 0).text().strip()
                text = self.table.item(row, 1).text().strip()
                if key and text:
                    self.store.save_translation(key, language, text)
        except (ProfileError, AttributeError) as exc:
            self.table.setToolTip(str(exc))
            return
        self.accept()
