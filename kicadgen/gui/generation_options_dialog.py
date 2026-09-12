"""Generation preferences, edited transactionally before saving."""
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QGroupBox, QLabel,
    QScrollArea, QVBoxLayout, QWidget,
)


class GenerationOptionsDialog(QDialog):
    def __init__(self, include_nc_pins=True, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Opzioni di generazione")
        self.setModal(True)
        self.resize(580, 340)
        layout = QVBoxLayout(self)
        page = QWidget()
        page_layout = QVBoxLayout(page)
        contacts = QGroupBox("Contatti e pin")
        contacts_layout = QVBoxLayout(contacts)
        self.include_nc_checkbox = QCheckBox("Includi i pin NC (Not Connected)")
        self.include_nc_checkbox.setChecked(include_nc_pins)
        contacts_layout.addWidget(self.include_nc_checkbox)
        explanation = QLabel(
            "I pin NC non sono collegati elettricamente, ma possono essere "
            "fisicamente presenti. Includerli permette di mantenere le relative "
            "piazzole nel footprint.\n\n"
            "L'opzione viene usata dalla prossima estrazione AI: non modifica "
            "automaticamente i componenti già estratti o i file generati."
        )
        explanation.setWordWrap(True)
        contacts_layout.addWidget(explanation)
        page_layout.addWidget(contacts)
        page_layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        layout.addWidget(scroll)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText("Salva")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Annulla")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def include_nc_pins(self):
        return self.include_nc_checkbox.isChecked()
