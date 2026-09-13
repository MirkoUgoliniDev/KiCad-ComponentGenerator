"""@brief Runtime internationalization backed by the project SQLite catalog.

@details The catalog stores stable translation keys rather than widget text.
English is the canonical fallback so an incomplete translation never leaves
the interface blank.
"""

from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtWidgets import QCheckBox, QGroupBox, QLabel, QPushButton, QRadioButton, QWidget


LANGUAGES = {
    "it": "Italiano",
    "en": "English",
    "fr": "Français",
    "de": "Deutsch",
}

DEFAULT_TRANSLATIONS = {
    "app.file": {"en": "File", "it": "File", "fr": "Fichier", "de": "Datei"},
    "app.configuration": {"en": "Configuration", "it": "Configurazione", "fr": "Configuration", "de": "Konfiguration"},
    "app.help": {"en": "Help", "it": "?", "fr": "Aide", "de": "Hilfe"},
    "tab.project": {"en": "📁 Project", "it": "📁 Progetto", "fr": "📁 Projet", "de": "📁 Projekt"},
    "tab.footprint": {"en": "▦ Footprint 2D", "it": "▦ Footprint 2D", "fr": "▦ Empreinte 2D", "de": "▦ Footprint 2D"},
    "tab.generate": {"en": "⚙ Generate", "it": "⚙ Genera", "fr": "⚙ Générer", "de": "⚙ Generieren"},
    "tab.preview": {"en": "👁 Footprint preview", "it": "👁 Anteprima FootPrint", "fr": "👁 Aperçu empreinte", "de": "👁 Footprint-Vorschau"},
    "tab.model3d": {"en": "🧊 3D model", "it": "🧊 Modello 3D", "fr": "🧊 Modèle 3D", "de": "🧊 3D-Modell"},
    "language.label": {"en": "Language:", "it": "Lingua:", "fr": "Langue :", "de": "Sprache:"},
    "ui:Lingua:": {"en": "Language:", "it": "Lingua:", "fr": "Langue :", "de": "Sprache:"},
    "ui:Cartella componente": {"en": "Component folder", "it": "Cartella componente", "fr": "Dossier composant", "de": "Komponentenordner"},
    "ui:Documentazione (docs/)": {"en": "Documentation (docs/)", "it": "Documentazione (docs/)", "fr": "Documentation (docs/)", "de": "Dokumentation (docs/)"},
    "ui:Analisi componente": {"en": "Component analysis", "it": "Analisi componente", "fr": "Analyse du composant", "de": "Komponentenanalyse"},
    "ui:Strumenti AI": {"en": "AI tools", "it": "Strumenti AI", "fr": "Outils IA", "de": "KI-Werkzeuge"},
    "ui:Nome componente:": {"en": "Component name:", "it": "Nome componente:", "fr": "Nom du composant :", "de": "Komponentenname:"},
    "ui:Area di lavoro:": {"en": "Workspace:", "it": "Area di lavoro:", "fr": "Espace de travail :", "de": "Arbeitsbereich:"},
    "ui:Tipologia componente:": {"en": "Component type:", "it": "Tipologia componente:", "fr": "Type de composant :", "de": "Komponententyp:"},
    "ui:Modello datasheet:": {"en": "Datasheet model:", "it": "Modello datasheet:", "fr": "Modèle datasheet :", "de": "Datenblattmodell:"},
    "ui:Modello foto:": {"en": "Photo model:", "it": "Modello foto:", "fr": "Modèle photo :", "de": "Fotomodell:"},
    "ui:Verifica foto:": {"en": "Photo verification:", "it": "Verifica foto:", "fr": "Vérification photo :", "de": "Fotoprüfung:"},
    "ui:Estrai parametri con AI": {"en": "Extract parameters with AI", "it": "Estrai parametri con AI", "fr": "Extraire les paramètres avec l’IA", "de": "Parameter mit KI extrahieren"},
    "ui:Log analisi AI": {"en": "AI analysis log", "it": "Log analisi AI", "fr": "Journal d’analyse IA", "de": "KI-Analyseprotokoll"},
    "ui:Chiudi": {"en": "Close", "it": "Chiudi", "fr": "Fermer", "de": "Schließen"},
    "ui:Salva": {"en": "Save", "it": "Salva", "fr": "Enregistrer", "de": "Speichern"},
    "ui:Annulla": {"en": "Cancel", "it": "Annulla", "fr": "Annuler", "de": "Abbrechen"},
    "ui:Parametri footprint (header THT a matrice)": {"en": "Footprint parameters (matrix THT header)", "it": "Parametri footprint (header THT a matrice)", "fr": "Paramètres d’empreinte (header THT matriciel)", "de": "Footprint-Parameter (THT-Matrix-Header)"},
    "ui:Genera footprint": {"en": "Generate footprint", "it": "Genera footprint", "fr": "Générer l’empreinte", "de": "Footprint erzeugen"},
    "ui:Apri cartella componente": {"en": "Open component folder", "it": "Apri cartella componente", "fr": "Ouvrir le dossier du composant", "de": "Komponentenordner öffnen"},
    "ui:Esporta in libreria...": {"en": "Export to library...", "it": "Esporta in libreria...", "fr": "Exporter vers la bibliothèque…", "de": "In Bibliothek exportieren …"},
    "ui:Anteprima 2D (silkscreen + pad, scala fissa, con quote):": {"en": "2D preview (silkscreen + pads, fixed scale, with dimensions):", "it": "Anteprima 2D (silkscreen + pad, scala fissa, con quote):", "fr": "Aperçu 2D (sérigraphie + pastilles, échelle fixe, avec cotes) :", "de": "2D-Vorschau (Silkscreen + Pads, fester Maßstab, mit Bemaßung):"},
    "ui:Vista:": {"en": "View:", "it": "Vista:", "fr": "Vue :", "de": "Ansicht:"},
    "ui:Centra": {"en": "Fit", "it": "Centra", "fr": "Centrer", "de": "Zentrieren"},
    "ui:Tabella pinout...": {"en": "Pinout table...", "it": "Tabella pinout...", "fr": "Table de brochage…", "de": "Pinbelegungstabelle …"},
    "ui:Vista superiore": {"en": "Top view", "it": "Vista superiore", "fr": "Vue de dessus", "de": "Draufsicht"},
    "ui:Vista laterale": {"en": "Side view", "it": "Vista laterale", "fr": "Vue latérale", "de": "Seitenansicht"},
    "ui:Modello 3D (opzionale, richiede FreeCAD installato)": {"en": "3D model (optional, requires FreeCAD)", "it": "Modello 3D (opzionale, richiede FreeCAD installato)", "fr": "Modèle 3D (optionnel, FreeCAD requis)", "de": "3D-Modell (optional, FreeCAD erforderlich)"},
    "ui:Modulo castellated (piazzole a mezzo foro sul bordo)": {"en": "Castellated module (half-hole edge pads)", "it": "Modulo castellated (piazzole a mezzo foro sul bordo)", "fr": "Module castellated (pastilles demi-trou sur le bord)", "de": "Castellated-Modul (Halbloch-Pads am Rand)"},
    "ui:Aggiungi lato": {"en": "Add side", "it": "Aggiungi lato", "fr": "Ajouter un côté", "de": "Seite hinzufügen"},
    "ui:Rimuovi lato selezionato": {"en": "Remove selected side", "it": "Rimuovi lato selezionato", "fr": "Supprimer le côté sélectionné", "de": "Ausgewählte Seite entfernen"},
    "ui:Aggiungi componente": {"en": "Add component", "it": "Aggiungi componente", "fr": "Ajouter un composant", "de": "Komponente hinzufügen"},
    "ui:Rimuovi componente selezionato": {"en": "Remove selected component", "it": "Rimuovi componente selezionato", "fr": "Supprimer le composant sélectionné", "de": "Ausgewählte Komponente entfernen"},
    "ui:Affina reofori / saldature...": {"en": "Refine leads / solder joints...", "it": "Affina reofori / saldature...", "fr": "Affiner les broches / soudures…", "de": "Anschlüsse / Lötstellen verfeinern …"},
    "ui:Aggiungi fillet": {"en": "Add fillet", "it": "Aggiungi fillet", "fr": "Ajouter un congé", "de": "Rundung hinzufügen"},
    "ui:Aggiungi testo": {"en": "Add text", "it": "Aggiungi testo", "fr": "Ajouter du texte", "de": "Text hinzufügen"},
    "ui:Rimuovi annotazione selezionata": {"en": "Remove selected annotation", "it": "Rimuovi annotazione selezionata", "fr": "Supprimer l’annotation sélectionnée", "de": "Ausgewählte Annotation entfernen"},
    "ui:Rileva terminali dalla foto...": {"en": "Detect terminals from photo...", "it": "Rileva terminali dalla foto...", "fr": "Détecter les bornes depuis la photo…", "de": "Anschlüsse aus Foto erkennen …"},
    "ui:Rigenera modello 3D (STEP) con queste annotazioni": {"en": "Regenerate 3D model (STEP) with these annotations", "it": "Rigenera modello 3D (STEP) con queste annotazioni", "fr": "Régénérer le modèle 3D (STEP) avec ces annotations", "de": "3D-Modell (STEP) mit diesen Annotationen neu erzeugen"},
    "ui:Apri cartella 3dshapes": {"en": "Open 3dshapes folder", "it": "Apri cartella 3dshapes", "fr": "Ouvrir le dossier 3dshapes", "de": "3dshapes-Ordner öffnen"},
    "ui:Esci": {"en": "Exit", "it": "Esci", "fr": "Quitter", "de": "Beenden"},
    "ui:About": {"en": "About", "it": "About", "fr": "À propos", "de": "Über"},
    "ui:Aggiorna elenco": {"en": "Refresh list", "it": "Aggiorna elenco", "fr": "Actualiser la liste", "de": "Liste aktualisieren"},
    "ui:Cerca:": {"en": "Search:", "it": "Cerca:", "fr": "Rechercher :", "de": "Suchen:"},
    "ui:Modifica prompt…": {"en": "Edit prompt…", "it": "Modifica prompt…", "fr": "Modifier le prompt…", "de": "Prompt bearbeiten …"},
    "ui:Rivedi rilevamento foto…": {"en": "Review photo detection…", "it": "Rivedi rilevamento foto…", "fr": "Revoir la détection photo…", "de": "Fotodetektion prüfen …"},
    "ui:Informazioni sulla cartella componente": {"en": "Component folder information", "it": "Informazioni sulla cartella componente", "fr": "Informations sur le dossier du composant", "de": "Informationen zum Komponentenordner"},
    "ui:Apri config.json": {"en": "Open config.json", "it": "Apri config.json", "fr": "Ouvrir config.json", "de": "config.json öffnen"},
    "ui:Gestione tipologie e profili AI...": {"en": "Manage AI types and profiles...", "it": "Gestione tipologie e profili AI...", "fr": "Gérer les types et profils IA…", "de": "KI-Typen und -Profile verwalten …"},
    "ui:Default generali AI...": {"en": "General AI defaults...", "it": "Default generali AI...", "fr": "Paramètres IA généraux…", "de": "Allgemeine KI-Standards …"},
    "ui:Opzioni di generazione...": {"en": "Generation options...", "it": "Opzioni di generazione...", "fr": "Options de génération…", "de": "Generierungsoptionen …"},
    "ui:Manage translations...": {"en": "Manage translations...", "it": "Gestione traduzioni...", "fr": "Gérer les traductions…", "de": "Übersetzungen verwalten …"},
    "ui:Change the datasheet model for this session": {"en": "Change the datasheet model for this session", "it": "Cambia il modello datasheet per questa sessione", "fr": "Changer le modèle datasheet pour cette session", "de": "Datenblattmodell für diese Sitzung ändern"},
    "ui:Change the photo model for this session": {"en": "Change the photo model for this session", "it": "Cambia il modello foto per questa sessione", "fr": "Changer le modèle photo pour cette session", "de": "Fotomodell für diese Sitzung ändern"},
    "ui:Change the photo verification model for this session": {"en": "Change the photo verification model for this session", "it": "Cambia il modello di verifica foto per questa sessione", "fr": "Changer le modèle de vérification photo pour cette session", "de": "Fotoprüfungsmodell für diese Sitzung ändern"},
    "ui:Opzioni di generazione": {"en": "Generation options", "it": "Opzioni di generazione", "fr": "Options de génération", "de": "Generierungsoptionen"},
    "ui:Contatti e pin": {"en": "Contacts and pins", "it": "Contatti e pin", "fr": "Contacts et broches", "de": "Kontakte und Pins"},
    "ui:Modalità di utilizzo": {"en": "Usage mode", "it": "Modalità di utilizzo", "fr": "Mode d’utilisation", "de": "Nutzungsmodus"},
    "ui:Includi i pin NC (Not Connected)": {"en": "Include NC pins (Not Connected)", "it": "Includi i pin NC (Not Connected)", "fr": "Inclure les broches NC (non connectées)", "de": "NC-Pins (nicht verbunden) einschließen"},
    "ui:Modello e prompt di sistema LLM": {"en": "LLM system model and prompt", "it": "Modello e prompt di sistema LLM", "fr": "Modèle et prompt système LLM", "de": "LLM-Systemmodell und Prompt"},
    "ui:Tipologia e profilo AI": {"en": "AI type and profile", "it": "Tipologia e profilo AI", "fr": "Type et profil IA", "de": "KI-Typ und -Profil"},
    "ui:Modello LLM:": {"en": "LLM model:", "it": "Modello LLM:", "fr": "Modèle LLM :", "de": "LLM-Modell:"},
    "ui:Prompt base:": {"en": "Base prompt:", "it": "Prompt base:", "fr": "Prompt de base :", "de": "Basis-Prompt:"},
    "ui:Istruzioni tipologia": {"en": "Type instructions", "it": "Istruzioni tipologia", "fr": "Instructions du type", "de": "Typanweisungen"},
    "ui:Scegli modello...": {"en": "Choose model...", "it": "Scegli modello...", "fr": "Choisir un modèle…", "de": "Modell auswählen …"},
    "ui:Revisione componenti da foto": {"en": "Review components from photo", "it": "Revisione componenti da foto", "fr": "Réviser les composants de la photo", "de": "Komponenten aus Foto prüfen"},
    "ui:Elimina selezionato": {"en": "Delete selected", "it": "Elimina selezionato", "fr": "Supprimer la sélection", "de": "Auswahl löschen"},
    "ui:Adatta foto": {"en": "Fit photo", "it": "Adatta foto", "fr": "Ajuster la photo", "de": "Foto einpassen"},
    "ui:Informazioni": {"en": "Information", "it": "Informazioni", "fr": "Informations", "de": "Informationen"},
    "ui:Cartella componente": {"en": "Component folder", "it": "Cartella componente", "fr": "Dossier composant", "de": "Komponentenordner"},
    "ui:Assegna un file a ogni ruolo, oppure indica “Non disponibile”.": {"en": "Assign one file to each role, or choose ‘Not available’.", "it": "Assegna un file a ogni ruolo, oppure indica “Non disponibile”.", "fr": "Attribuez un fichier à chaque rôle ou choisissez « Non disponible ».", "de": "Weisen Sie jeder Rolle eine Datei zu oder wählen Sie „Nicht verfügbar“."},
    "ui:Pinout / disposizione contatti": {"en": "Pinout / contact layout", "it": "Pinout / disposizione contatti", "fr": "Brochage / disposition des contacts", "de": "Pinbelegung / Kontaktanordnung"},
    "ui:Vista superiore": {"en": "Top view", "it": "Vista superiore", "fr": "Vue de dessus", "de": "Draufsicht"},
    "ui:Vista laterale": {"en": "Side view", "it": "Vista laterale", "fr": "Vue latérale", "de": "Seitenansicht"},
    "ui:Nessun file": {"en": "No file", "it": "Nessun file", "fr": "Aucun fichier", "de": "Keine Datei"},
    "ui:Carica...": {"en": "Load...", "it": "Carica...", "fr": "Charger…", "de": "Laden …"},
    "ui:Non disponibile": {"en": "Not available", "it": "Non disponibile", "fr": "Non disponible", "de": "Nicht verfügbar"},
    "ui:Analisi componente": {"en": "Component analysis", "it": "Analisi componente", "fr": "Analyse du composant", "de": "Komponentenanalyse"},
    "ui:Strumenti AI": {"en": "AI tools", "it": "Strumenti AI", "fr": "Outils IA", "de": "KI-Werkzeuge"},
    "ui:Type di componente:": {"en": "Component type:", "it": "Type di componente:", "fr": "Type de composant :", "de": "Komponententyp:"},
    "ui:Informazioni sulla cartella componente": {"en": "Component folder information", "it": "Informazioni sulla cartella componente", "fr": "Informations sur le dossier du composant", "de": "Informationen zum Komponentenordner"},
    "ui:Log analisi AI": {"en": "AI analysis log", "it": "Log analisi AI", "fr": "Journal d’analyse IA", "de": "KI-Analyseprotokoll"},
    "ui:Prompt specifico del componente": {"en": "Component-specific prompt", "it": "Prompt specifico del componente", "fr": "Prompt spécifique au composant", "de": "Komponentenspezifischer Prompt"},
    "ui:Riepilogo della generazione": {"en": "Generation summary", "it": "Riepilogo della generazione", "fr": "Résumé de la génération", "de": "Zusammenfassung der Generierung"},
    "ui:Revisione componenti da foto": {"en": "Review components from photo", "it": "Revisione componenti da foto", "fr": "Réviser les composants de la photo", "de": "Komponenten aus Foto prüfen"},
}


@dataclass
class I18n:
    """@brief Resolve UI translation keys for one active application language."""

    store: object
    language: str = "it"

    def __post_init__(self) -> None:
        """@brief Normalize unsupported language codes to English."""
        if self.language not in LANGUAGES:
            self.language = "en"

    def tr(self, key: str, default: str | None = None, **values: object) -> str:
        """@brief Return a translated string with English and key fallbacks.

        @param key Stable translation key.
        @param default Optional source text used when the key is unknown.
        @return Localized text with optional named substitutions applied.
        """
        fallback = default or DEFAULT_TRANSLATIONS.get(key, {}).get("en", key)
        value = self.store.translation(key, self.language, fallback)
        try:
            return value.format(**values)
        except (KeyError, IndexError, ValueError):
            return value

    def set_language(self, language: str) -> None:
        """@brief Select a supported language for subsequent lookups."""
        if language not in LANGUAGES:
            raise ValueError(f"Unsupported language: {language}")
        self.language = language

    def text(self, source: str) -> str:
        """@brief Translate a visible source string from the UI catalog."""
        return self.tr("ui:" + source, default=source)

    def retranslate_widgets(self, root) -> None:
        """@brief Refresh translatable widget text without touching user data."""
        translatable_types = (QCheckBox, QGroupBox, QLabel, QPushButton, QRadioButton)
        title = root.property("i18n_window_title_source") if hasattr(root, "property") else None
        if title is None and hasattr(root, "windowTitle"):
            title = root.windowTitle()
            if title and hasattr(root, "setProperty"):
                root.setProperty("i18n_window_title_source", title)
        if title and hasattr(root, "setWindowTitle"):
            root.setWindowTitle(self.text(title))
        if not hasattr(root, "findChildren"):
            return
        for widget in root.findChildren(QWidget):
            if not isinstance(widget, translatable_types):
                continue
            source = widget.property("i18n_source")
            if source is None:
                try:
                    source = widget.text()
                except (AttributeError, TypeError):
                    continue
                if not source:
                    continue
                widget.setProperty("i18n_source", source)
            widget.setText(self.text(source))
            tooltip = widget.property("i18n_tooltip_source")
            if tooltip is None:
                tooltip = widget.toolTip()
                if tooltip:
                    widget.setProperty("i18n_tooltip_source", tooltip)
            if tooltip:
                widget.setToolTip(self.text(tooltip))
