"""Generation preferences, edited transactionally before saving."""
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QGroupBox, QHBoxLayout,
    QScrollArea, QVBoxLayout, QWidget,
)
from .profile_dialog import icon_button, show_info


class GenerationOptionsDialog(QDialog):
    """@brief Edit generation options before a component is produced."""
    def __init__(self, include_nc_pins=True, parent=None, *, advanced_user=False):
        super().__init__(parent)
        self.setWindowTitle("Opzioni di generazione")
        self.setModal(True)
        self.resize(580, 340)
        layout = QVBoxLayout(self)
        page = QWidget()
        page_layout = QVBoxLayout(page)
        contacts = QGroupBox("Contatti e pin")
        contacts_layout = QHBoxLayout(contacts)
        self.include_nc_checkbox = QCheckBox("Includi i pin NC (Not Connected)")
        self.include_nc_checkbox.setChecked(include_nc_pins)
        contacts_layout.addWidget(self.include_nc_checkbox, 1)
        contacts_layout.addWidget(icon_button('Info', 'info.svg', lambda: show_info(self, 'Contatti e pin',
            "I pin NC non sono collegati elettricamente, ma possono essere "
            "fisicamente presenti. Includerli permette di mantenere le relative "
            "piazzole nel footprint.\n\n"
            "L'opzione viene usata dalla prossima estrazione AI: non modifica "
            "automaticamente i componenti già estratti o i file generati."
        )))
        page_layout.addWidget(contacts)
        mode = QGroupBox('Modalità di utilizzo')
        mode_layout = QHBoxLayout(mode)
        self.advanced_user_checkbox = QCheckBox('Advanced user')
        self.advanced_user_checkbox.setChecked(advanced_user)
        mode_layout.addWidget(self.advanced_user_checkbox, 1)
        mode_layout.addWidget(icon_button('Info', 'info.svg', lambda: show_info(self, 'Advanced user',
            'Disattivato: Footprint 2D e Genera sono nascosti. Dopo un’analisi valida vengono '
            'generati automaticamente footprint e modello 3D, con avanzamento e anteprime. '
            'I file esistenti richiedono conferma prima della sovrascrittura.\n\n'
            'Attivato: i due tab sono visibili e la generazione rimane manuale. '
            'Cambiare modalità non genera file e non cancella i parametri già acquisiti. '
            'La scelta viene salvata e vale dal prossimo avvio dell’analisi.\n\n'
            'Se mancano dati necessari o la geometria non supporta il 3D, la generazione '
            'automatica si ferma. Attiva Advanced user per rivedere i dati e generare manualmente.')))
        page_layout.addWidget(mode)
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
        """@brief Provide the include nc pins operation for this workflow."""
        return self.include_nc_checkbox.isChecked()

    def advanced_user(self):
        """@brief Provide the advanced user operation for this workflow."""
        return self.advanced_user_checkbox.isChecked()
