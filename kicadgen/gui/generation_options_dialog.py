"""@brief Generation preferences, edited transactionally before saving."""
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QGroupBox, QHBoxLayout,
    QScrollArea, QVBoxLayout, QWidget,
)
from .profile_dialog import icon_button, show_info


class GenerationOptionsDialog(QDialog):
    """@brief Edit generation options before a component is produced."""
    def __init__(self, include_nc_pins=True, parent=None, *,
                 silk_pin_labels=False, pause_after_inventory=True):
        super().__init__(parent)
        self.setWindowTitle("Opzioni di generazione")
        self.setModal(True)
        self.resize(580, 420)
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
            "automaticamente i componenti già estratti o i file generati.",
            text_key='help.generation.include_nc'
        )))
        page_layout.addWidget(contacts)
        silk = QGroupBox("Serigrafia del modello 3D")
        silk_layout = QHBoxLayout(silk)
        self.silk_labels_checkbox = QCheckBox("Incidi le sigle dei pin")
        self.silk_labels_checkbox.setChecked(silk_pin_labels)
        silk_layout.addWidget(self.silk_labels_checkbox, 1)
        silk_layout.addWidget(icon_button('Info', 'info.svg', lambda: show_info(self,
            'Serigrafia del modello 3D',
            "Incide accanto a ogni contatto la sigla presa dalla tabella pinout.\n\n"
            "Disattivata di default: su un modulo a passo fitto le sigle lunghe si "
            "sovrappongono fra loro e finiscono sopra i componenti, rendendo il solido "
            "meno leggibile invece che piu'. I numeri dei pin restano disponibili "
            "nell'anteprima 3D, che e' una cosa diversa dal testo inciso nel file STEP.\n\n"
            "L'opzione vale dalla prossima generazione: non modifica i file gia' prodotti.",
            text_key='help.generation.silk_labels')))
        page_layout.addWidget(silk)
        # The pause after the AI inventory: a setting, hence here and not in
        # the Project tab (user, 2026-09-24). Same key as config.json.
        analysis = QGroupBox("Analisi AI")
        analysis_layout = QHBoxLayout(analysis)
        self.pause_checkbox = QCheckBox(
            "Fermati dopo l'inventario AI per rivedere i corpi (gratuito)")
        self.pause_checkbox.setChecked(pause_after_inventory)
        analysis_layout.addWidget(self.pause_checkbox, 1)
        analysis_layout.addWidget(icon_button('Info', 'info.svg', lambda: show_info(self,
            'Analisi AI',
            "Quando l'inventario AI ha finito, la pipeline si ferma e propone la "
            "revisione dei componenti prima dell'abbinamento dei modelli KiCad e della "
            "generazione 3D: da li' si spostano, ridimensionano, aggiungono ed eliminano "
            "i corpi. La revisione non fa richieste AI: e' gratuita.\n\n"
            "Togliendo la spunta l'analisi prosegue da sola con i corpi proposti dall'AI; "
            "la revisione resta disponibile dai pulsanti in fondo al tab Progetto.",
            text_key='help.generation.pause_after_inventory')))
        page_layout.addWidget(analysis)
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
        """@brief Return whether not-connected pins are included in the generated component.

        @return True when the «include NC pins» box is checked.
        """
        return self.include_nc_checkbox.isChecked()

    def silk_pin_labels(self):
        """@brief Tell whether pin names must be engraved on the 3D silkscreen.

        @return True when the silkscreen labels box is checked.
        """
        return self.silk_labels_checkbox.isChecked()

    def pause_after_inventory(self):
        """@brief Tell whether the pipeline stops after the AI inventory for a review.

        @return True when the pause box is checked.
        """
        return self.pause_checkbox.isChecked()
