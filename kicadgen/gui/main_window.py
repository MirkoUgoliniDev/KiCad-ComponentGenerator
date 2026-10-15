"""@brief Provide the main KiCadGen-Tool desktop window.

@details The window uses the shared dark Qt theme and runs long generation and
AI operations in worker threads. Its five tabs cover project input, footprint
preview, electrical symbol editing, 3D review and KiCad installation.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from html import escape
import json
from functools import partial
import math
import os
import re
import shutil
import sqlite3
import sys
import time
import webbrowser
from pathlib import Path

from PyQt6.QtCore import QEvent, QEventLoop, QObject, QPointF, QRectF, QThread, QTimer, Qt, QSize, pyqtSignal
from PyQt6.QtGui import (QBrush, QColor, QCursor, QFont, QIcon, QKeySequence, QPainter, QPainterPath, QPen,
                         QPixmap, QPolygonF, QShortcut, QTextCursor)
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGraphicsEllipseItem, QGraphicsRectItem, QGraphicsScene,
    QGraphicsView, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QDialog, QListWidget, QMainWindow, QMenu, QMessageBox, QPushButton, QScrollArea,
    QSplitter, QListWidgetItem, QStackedWidget, QTabWidget, QTextEdit, QVBoxLayout,
    QToolButton, QWidget, QTableWidget, QTableWidgetItem, QHeaderView, QWizard, QWizardPage,
    QGridLayout, QInputDialog,
)

from ..cad3d.annotations import (ANNOTATION_CONVENTION, EdgeFilletAnnotation,
                                  FaceTextAnnotation, swap_front_back)
from ..cad3d.body_params import Body3DParams
from ..cad3d.step_generator import (
    CAD_ABORT_MESSAGE, Cad3DError, find_freecadcmd, generate_3d_model,
    generate_castellated_3d_model, generate_single_package_3d_model,
    generate_single_tht_3d_model,
)
from ..generators.castellated_module import (
    EDGE_LABELS, EDGES as CASTELLATED_EDGES, CastellatedModuleParams,
    CastellatedSide, ComponentBox, build_castellated_module, pad_positions,
)
from ..generators import castellated_module as _castellated_module
from ..generators.pin_header import PinHeaderParams, build_pin_header, pin_positions
from ..generators.package import build_package
from ..generators.package import preview_grid_origin as package_preview_grid_origin
from ..generators.castellated_module import preview_grid_origin as castellated_preview_grid_origin
from ..core.footprint import text_grid_origin
from ..core.package_geometry import PackageGeometry, package_from_mapping
from ..services import component_types, recent_projects
from ..services.analysis_profiles import ProfileStore, ProfileError
from ..services.i18n import DEFAULT_TRANSLATIONS, I18n, LANGUAGES
from ..services.document_roles import DocumentSet, MANIFEST, SUFFIXES
from ..services.datasheet_extractor import (
    DEFAULT_SYSTEM_PROMPT, ExtractedParams,
    extract_pin_header_params,
)
from ..services.system_dependencies import detect_external_applications
from ..services.kicad_library import (
    KiCadLibraryError,
    create_preview_project,
    discover_component_artifacts,
    global_fp_lib_table_path,
    install_component_library,
    launch_preview,
)
from ..services.kicad_export_history import load_history as load_kicad_export_history
from ..services.kicad_export_history import record_export as record_kicad_export
from .app_config import config_path, load_config, save_config
from .accepted_models_dialog import AcceptedModelsDialog, ConsolidateDialog
from ..app_paths import default_projects_dir, install_dir, is_frozen, logs_dir, resource_path, user_data_dir
from ..app_paths import (DEFAULT_YOLO_ONNX, DEFAULT_YOLO_PT, SMD_MODELS_DIR, YOLO_MODELS_DIR,
                         smd_model_path as smd_weights_path,
                         yolo_model_path as yolo_weights_path)
# Solo la validazione dei parametri (libreria standard): onnxruntime e PyTorch
# restano caricati al bisogno. Legata qui, all'import, e non al clic: i test
# che sostituiscono il modulo con un finto non devono cambiarne il contratto.
from ..services.photo_yolo import yolo_settings as validate_yolo_settings
from .component_editor_dialog import ComponentEditorDialog
from .workflow_bar import WorkflowBar
from .project_wizard import CollapsibleTools, ProjectWizard
from ..generators.host_mounting import header_footprint_params
from .start_page import NO_PROJECTS_TOOLTIP, StartPage
from .component_state import (BoundedValue, FlagValue, MAX_LENGTH_MM, MIN_HEIGHT_MM,
                              MIN_LENGTH_MM, TextValue, normalised_side)
from .analysis_overlay import AnalysisOverlay
from .generation_options_dialog import GenerationOptionsDialog
from .guided_documents import GuidedDocuments
from .model_dialog import pick_model
from ..services.openrouter import PHOTO_CAPABILITIES
from .pinout_dialog import PinoutDialog
from .photo_review_dialog import PhotoReviewDialog, PhotoTransform, ai_only_excluded
from .preview_3d_pending import Preview3DPending

#: The VTK viewer class, imported on first use by `_preview_3d_class`: its
#: libraries (pyvistaqt, pyvista, VTK, matplotlib) cost about six seconds of
#: the start-up and are not needed until a model is shown (user, 2026-10-12).
#: Tests install a stub here before building the window; the window then
#: builds the viewer at once, as before.
Preview3DVtk = None


#: Milliseconds the splash stays after `window.show()`: covers the first
#: paint and the desktop's window-open animation (Windows 11: ~200 ms).
SPLASH_HANDOVER_MS = 300


def _preview_3d_class():
    """@brief The 3D viewer class, importing its module the first time.

    @return `Preview3DVtk`: the real class, or the stub a test installed.
    @throws ImportError, OSError When the viewer's libraries cannot load.
    @note Set once: later calls return the cached class without importing.
    """
    global Preview3DVtk
    if Preview3DVtk is None:
        from .preview_3d_vtk import Preview3DVtk as viewer_class
        Preview3DVtk = viewer_class
    return Preview3DVtk


class _ModulePreloader(QObject):
    """@brief Signals of a background import: delivered on the GUI thread (queued)."""
    loaded = pyqtSignal()
    failed = pyqtSignal(str)

from .settings_dialog import LlmSettingsDialog
from .profile_dialog import ProfilesDialog, icon_button
from .theme import BASE, BLUE, GREEN, RED, SUBTEXT, SURFACE1, SURFACE2, TEXT, YELLOW, APP_QSS
from .tooltips import install_tooltip_filter
# Worker e aiutanti del contratto di chiamata: spostati in gui/workers.py
# (HANDOFF 130, T6/G5) e ri-esportati qui, dove chiamanti e test li cercano.
from .workers import (
    Cad3DWorker, ExtractWorker, GenerateWorker, ReconcileWorker, TerminalDetailWorker,
    UpdateCheckWorker, YoloPretestWorker, _announce_freecad_measuring, _photo_scale_kwargs,
    accepted_kwargs, accepts_keyword, cancel_kwargs, freecad_progress, matching_cancel_kwargs,
    phase_kwargs,
)
from ..services.messages import tx

#: Icona di ogni scheda, nell'ordine delle schede: SVG a linea nello stile
#: delle barre di icone.
TAB_ICONS = ("tab-project.svg", "tab-preview.svg", "tab-symbol.svg", "tab-model3d.svg", "tab-kicad.svg",
             "tab-exports.svg")

#: Positions of the tabs in `MainWindow.tabs`. Footprint 2D and Genera were
#: removed on 2026-09-26 with the «Utente avanzato» mode; their parameters
#: live in `component_state` values, not in widgets.
TAB_PROJECT, TAB_PREVIEW, TAB_SYMBOL, TAB_MODEL_3D, TAB_KICAD, TAB_EXPORTS = range(6)

#: I tre passi di una generazione, come li mostra la modale (utente,
#: 2026-09-26). Chi lavora in ciascuno lo dice l'intestazione: vedi
#: `analysis_overlay.TOOLS`.
GENERATION_STEPS = ("Footprint 2D (KiCadGen)", "Confronto con i consolidati", "Modelli dalla libreria KiCad",
                    "Solido 3D con FreeCAD")
#: Passo di ogni fase annunciata dai worker. Le misure di FreeCAD appartengono
#: alla scelta dei modelli: servono a confrontare i candidati.
GENERATION_STEP_OF = {"footprint": 0, "consolidated": 1, "kicad": 2, "freecad_measure": 2, "freecad": 3}

#: Cartella del programma (sola lettura una volta installato) e cartella di
#: lavoro predefinita dei progetti: vedi `kicadgen.app_paths`.
APP_DIR = install_dir()
# Generated working files stay separate from installed KiCad libraries.
DEFAULT_LIBRARY_ROOT = default_projects_dir()
DOCS_SUBDIR = "docs"
#: Link della finestra About (utente, 2026-09-24): repository del progetto e
#: sviluppatore, dal remoto su cui il branch viene pubblicato.
REPOSITORY_URL = "https://github.com/MirkoUgoliniDev/KiCad-ComponentGenerator"
DEVELOPER_URL = "https://github.com/MirkoUgoliniDev"
DEVELOPER_NAME = "Mirko Ugolini"

#: Campi che appartengono al PROGETTO e finiscono in `project_config.json`
#: (decisione dell'utente del 2026-09-24). Nell'ordine in cui si riapplicano:
#: la tipologia prima di tutto, perche' cambiandola cambiano altri campi, e i
#: gruppi attivabili prima dei loro campi. Restano fuori, di proposito:
#: lingua, cartella di lavoro, nome del progetto (e' la cartella stessa), i
#: campi di sola immissione dell'editor delle annotazioni e la griglia
#: dell'anteprima, che sono modi di guardare.
#: `group_3d` is not saved: since 2026-09-26 it follows FreeCAD's availability
#: only, and an old file with it off would silently disable every 3D model.
PROJECT_WIDGETS = (
    "combo_component_type", "field_component_prompt", "combo_photo_detector",
    "field_yolo_model", "field_smd_model", "group_castellated",
    "field_description", "field_tags",
    "field_rows", "field_cols", "field_pitch", "field_row_spacing", "field_pad",
    "field_drill", "field_populated_pins",
    "field_body_length", "field_body_width", "field_body_height",
    "field_body_offset_x", "field_body_offset_y", "field_body_color",
    "field_pin_color", "field_pin_length_3d",
    "field_cast_body_length", "field_cast_body_width", "field_cast_pcb",
    "field_cast_pad_length", "field_cast_pad_width", "field_cast_overhang",
    "field_cast_inset", "field_cast_hole", "field_cast_hole_inset",
    "field_cast_thru_drill", "field_cast_thru_pad", "field_cast_thru_offset",
)
#: Campi dei file di pesi locali: vuoto = il predefinito. Un progetto che non
#: li dichiara li riporta vuoti invece di ereditare quelli del precedente.
MODEL_FILE_WIDGETS = ("field_yolo_model", "field_smd_model")
#: Pausa prima di riattivare «Estrai parametri con AI» dopo una preparazione
#: interrotta (documenti mancanti, rilevatore non disponibile...): i clic
#: arrivati mentre l'interfaccia era ferma vengono consegnati prima e trovano il
#: pulsante ancora spento. Tempo d'interfaccia, non una misura di nulla.
EXTRACT_REENABLE_DELAY_MS = 300
#: Margine, in pixel, fra il bordo della finestra e il contenuto delle schede:
#: la riga della lingua sopra le schede usa lo stesso, cosi' i bordi combaciano.
CONTENT_MARGIN_PX = 10
#: Opzioni di Configurazione -> Opzioni di generazione che seguono il progetto.
PROJECT_OPTIONS = ("include_nc_pins", "silk_pin_labels", "pause_after_inventory",
                   "show_pins_3d", "pin_numbers_3d")
#: Ogni quanto, in millisecondi, si controlla se il progetto e' cambiato. Una
#: scelta: abbastanza spesso da non perdere nulla chiudendo di colpo, abbastanza
#: di rado da non scrivere a ogni tasto. Un file si scrive solo se lo stato e'
#: davvero diverso da quello salvato.
PROJECT_AUTOSAVE_MS = 2000

#: Tipologie che dichiarano UN SOLO componente discreto (vedi
#: `services/component_types.py`). Non sono schede con corpi montati sopra,
#: quindi l'estrattore non guarda la fotografia per inventariarli: niente
#: rilevatore locale, niente revisione «Corpi rilevati», niente componenti in
#: tabella. E' un attributo dichiarato della tipologia scelta, non il nome del
#: pezzo: vale per qualunque componente di quella famiglia.
_SINGLE_COMPONENT_FAMILIES = ("single_tht", "single_smd")
_BODY_OVERRIDE_UNSET = object()
_PROJECT_KIND_BOARD = "board_module"
_PROJECT_KIND_SINGLE = "single_component"
_PROJECT_KINDS = {_PROJECT_KIND_BOARD, _PROJECT_KIND_SINGLE}

#: Designator prefix proposed from the DECLARED data of a project: the family
#: of its component type (`services/component_types.py`) and, for a single
#: component, the package family (`core/package_geometry.PACKAGE_FAMILIES`).
#: Every value is «U» on purpose: a board module is one device, and an IC
#: package (DIP, SOIC, QFN...) is a device too. No family in the code names
#: the FUNCTION of a part: a connector («J»), a relay («K») or a resistor
#: («R») is not deducible from its envelope or lead count (a U.FL and a board
#: jack can measure the same, a DIP relay and a DIP IC share the grid), so the
#: prefix is proposed only where the data decide it and the field stays
#: editable. Keyed by declared attributes, never by part names.
REFERENCE_BY_TYPE_FAMILY = {
    "aggregate_tht": "U", "aggregate_castellated": "U",
    "single_tht": "U", "single_smd": "U",
}
REFERENCE_BY_PACKAGE_FAMILY = {
    "dip": "U", "sip": "U", "soic": "U", "tssop": "U", "qfn": "U", "qfp": "U",
    "module": "U", "castellated": "U",
}
#: KiCad's generic prefix when nothing above applies (KLC S6.1 example).
DEFAULT_REFERENCE = "U"


def proposed_reference(type_family: str, package_family: str | None = None) -> str:
    """@brief Designator prefix proposed for a project from its declared families.

    @param type_family Family of the chosen component type ('single_tht',
        'single_smd', 'aggregate_tht', 'aggregate_castellated' or '').
    @param package_family Family of the explicit package geometry, when the
        project has one (`PackageGeometry.family`), else None.
    @return Letters-only prefix; `DEFAULT_REFERENCE` when neither table
        knows the input.
    @note See `REFERENCE_BY_TYPE_FAMILY`: the tables are the only place the
        proposal comes from, so a new family with a different prefix is one
        table entry, not a new rule.
    """
    if package_family in REFERENCE_BY_PACKAGE_FAMILY:
        return REFERENCE_BY_PACKAGE_FAMILY[package_family]
    return REFERENCE_BY_TYPE_FAMILY.get(type_family or "", DEFAULT_REFERENCE)

#: Tipo di contatto di un lato castellated, nel caso in cui il generatore
#: installato non esponga ancora le proprie costanti. I valori sono quelli
#: fissati dal contratto di `castellated_module` (CONTACT_HALF_HOLE,
#: CONTACT_THROUGH_HOLE, CONTACT_HALF_AND_THROUGH): la GUI e il generatore
#: vengono modificati separatamente e nessuno dei due deve rompersi se
#: l'altro arriva prima.
_CONTACT_STYLES_FALLBACK = ("half_hole", "through_hole", "half_and_through")

#: Etichette in italiano semplice: chi sceglie guarda la scheda, non il
#: datasheet, quindi il nome descrive cio' che si vede.
CONTACT_STYLE_LABELS = {
    "half_hole": "Mezzo foro sul bordo",
    "through_hole": "Foro passante",
    "half_and_through": "Mezzo foro + foro passante",
}

#: Come distinguere i tre tipi guardando la scheda. Il criterio e' strutturale
#: (il bordo taglia il foro oppure no), non una misura: e' l'unico che regge
#: su qualunque modulo, qualunque passo abbia.
CONTACT_STYLE_TOOLTIP = (
    "Come riconoscere il tipo di contatto guardando la scheda:\n"
    "- Mezzo foro sul bordo: il foro metallizzato e' tagliato dal bordo della "
    "scheda, si vede una mezzaluna aperta verso l'esterno.\n"
    "- Foro passante: il foro e' intero e chiuso, con materiale della scheda "
    "fra il foro e il bordo.\n"
    "- Mezzo foro + foro passante: ogni contatto ha entrambi, la mezzaluna sul "
    "bordo e un foro intero poco piu' dentro.\n"
    "Cambiarlo non richiede nessuna analisi AI: basta rigenerare."
)

#: Voce informativa del selettore «tutti i lati» quando i lati differiscono.
CONTACT_STYLE_MIXED_LABEL = "Diverso per lato"
#: Voce informativa del selettore quando almeno un lato non ha un tipo di
#: contatto: la foto non lo mostrava e nessuno lo ha scelto (2026-09-26).
CONTACT_STYLE_UNKNOWN_LABEL = "Da scegliere"

#: Spessore di una scheda quando i documenti non lo quotano, in mm: 1.6 mm e'
#: lo spessore standard delle schede FR-4 (decisione dell'utente, 26/09: per
#: TUTTE le disposizioni, dichiarato nel log). E' lo stesso valore che il
#: generatore 3D usa per una scheda a fori passanti senza spessore
#: (`step_generator`, ramo `pcb_thickness_mm is None`).
STANDARD_PCB_THICKNESS_MM = 1.6
#: Where the PCB thickness shown on the Component page comes from (HANDOFF
#: point 132), as the Italian text of its label. "read"/"derived"/"estimated"
#: and "undeclared" are the extractor's provenances of `pcb_thickness_mm`;
#: "standard" is STANDARD_PCB_THICKNESS_MM because nothing declared one;
#: "user" is a value typed on the page, which a new analysis never replaces;
#: "project" is a project saved before the provenance was recorded.
PCB_THICKNESS_SOURCE_LABELS = {
    "read": "letto dai documenti",
    "derived": "ricavato dai documenti",
    "estimated": "stimato dall'AI, da verificare",
    "undeclared": "dall'AI senza provenienza, da verificare",
    "standard": "standard FR-4, non quotato",
    "user": "impostato da te",
    "project": "salvato nel progetto",
}
#: Colore di una scheda quando l'analisi non lo dichiara e la foto non si puo'
#: campionare: il solder mask verde standard, il preset «Green» dello stackup
#: di KiCad (RGB 20, 51, 36). Dichiarato nel log ogni volta che si usa.
STANDARD_SOLDER_MASK_HEX = "#143324"
#: Colore del corpo di un componente singolo quando l'analisi non lo dichiara:
#: il valore iniziale del campo (plastica nera), dichiarato nel log.
DEFAULT_BODY_COLOR_HEX = "#1a1a1a"
#: Intervallo PLAUSIBILE del passo e dell'interasse, in mm. Euristica
#: dichiarata, non un limite: dal passo fine piu' stretto dei package comuni
#: (0.3 mm) al pollice dei morsetti di potenza (25.4 mm). Fuori da qui il
#: valore si applica comunque e il log avvisa, perche' e' piu' probabile un
#: errore di lettura (per esempio pollici letti come mm) che un pezzo reale;
#: lo renderebbe superfluo un controllo di coerenza con il land pattern.
PLAUSIBLE_PITCH_MM = (0.3, 25.4)


class _ToolTipFollower(QObject):
    """@brief Re-run a callback whenever the watched widget's tooltip changes.

    @note Used by the icon-only copies of the Project-tab commands in the 3D
        tab: the copy composes its tooltip from the original's text and
        tooltip, so it must be recomposed whenever the original changes --
        on a language switch, or when some code rewrites the original's
        tooltip. Qt delivers `QEvent.ToolTipChange` synchronously from
        `setToolTip`, so the copy is never observed out of step.
    """

    def __init__(self, watched, callback) -> None:
        """@brief Install the filter on `watched`, parented to it for lifetime.

        @param watched Widget whose tooltip is followed.
        @param callback Zero-argument callable run after each change.
        """
        super().__init__(watched)
        self._callback = callback
        watched.installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:
        """@brief Qt override: run the callback on ToolTipChange, never consume.

        @param watched The followed widget.
        @param event Incoming event.
        @return Always False: the original widget keeps handling its events.
        """
        if event.type() == QEvent.Type.ToolTipChange:
            try:
                self._callback()
            except RuntimeError:
                # La copia e' gia' stata distrutta (chiusura della finestra):
                # non c'e' piu' nulla da allineare.
                pass
        return False


def contact_styles() -> tuple:
    """@brief The contact styles offered for a castellated side.

    @return The generator's `CONTACT_STYLES` when it exists, otherwise
        `_CONTACT_STYLES_FALLBACK`, which carries the same contract values.
    @note Read at call time, not at import: the generator module may gain the
        constant after this module was written, and tests simulate both states.
    """
    return tuple(getattr(_castellated_module, "CONTACT_STYLES", _CONTACT_STYLES_FALLBACK))


def default_contact_style() -> str:
    """@brief The contact style a side has when nothing declared another one.

    @return The generator's `CONTACT_HALF_AND_THROUGH`, or its contract value
        `"half_and_through"` when the constant is not there yet.
    @note It is today's geometry (edge half-hole plus inner through-hole): an
        extraction that does not state the style must keep producing exactly
        what it produced before, not a guess.
    """
    return str(getattr(_castellated_module, "CONTACT_HALF_AND_THROUGH", "half_and_through"))

# Local fallbacks use I18n.tr without adding rows to the profile database.
_PHOTO_DETECTOR_TRANSLATIONS = {
    "label": {"it": "Rilevamento foto:", "en": "Photo detection:",
              "fr": "Détection photo :", "de": "Fotoerkennung:"},
    "ai": {"it": "AI (attuale)", "en": "AI (current)",
           "fr": "IA (actuelle)", "de": "KI (bisherig)"},
    "yolo": {"it": "YOLO locale + verifica AI", "en": "Local YOLO + AI verification",
             "fr": "YOLO local + vérification IA", "de": "Lokales YOLO + KI-Prüfung"},
    "yolo_smd": {"it": "YOLO + rete SMD locale (v3)", "en": "YOLO + local SMD network (v3)",
                 "fr": "YOLO + réseau SMD local (v3)", "de": "YOLO + lokales SMD-Netz (v3)"},
    "smd_unavailable": {
        "it": "Non disponibile nell'app Windows installata: rete SMD e runtime PyTorch non inclusi.",
        "en": "Unavailable in the installed Windows app: the SMD network and PyTorch runtime are not included.",
        "fr": "Indisponible dans l’application Windows installée : le réseau SMD et le runtime PyTorch ne sont pas inclus.",
        "de": "In der installierten Windows-App nicht verfügbar: SMD-Netz und PyTorch-Laufzeit sind nicht enthalten.",
    },
    "yolo_only": {"it": "Solo YOLO locale (pretest, nessuna AI)",
                  "en": "Local YOLO only (pretest, no AI)",
                  "fr": "YOLO local seul (prétest, aucune IA)",
                  "de": "Nur lokales YOLO (Vortest, keine KI)"},
    "vlm": {"it": "Solo modello di visione (nessun rilevatore locale)",
            "en": "Vision model only (no local detector)",
            "fr": "Modèle de vision seul (aucun détecteur local)",
            "de": "Nur Sehmodell (kein lokaler Detektor)"},
    "model": {"it": "Modello YOLO:", "en": "YOLO model:",
              "fr": "Modèle YOLO :", "de": "YOLO-Modell:"},
    "smd_model": {"it": "Modello rete SMD:", "en": "SMD network model:",
                  "fr": "Modèle réseau SMD :", "de": "SMD-Netzmodell:"},
    "smd_picker": {"it": "Seleziona la rete SMD (.pt) per questa sessione",
                   "en": "Select the SMD network (.pt) for this session",
                   "fr": "Choisir le réseau SMD (.pt) pour cette session",
                   "de": "SMD-Netz (.pt) für diese Sitzung auswählen"},
    "smd_help": {
        "it": "File .pt della rete che classifica i corpi, facoltativo, solo per questa sessione. Lascia vuoto per usare {path}. Viene controllato prima di partire.",
        "en": "Optional .pt file of the network that classifies the bodies, for this session only. Leave empty to use {path}. It is checked before starting.",
        "fr": "Fichier .pt facultatif du réseau qui classe les corps, pour cette session uniquement. Laisser vide pour utiliser {path}. Il est vérifié avant le démarrage.",
        "de": "Optionale .pt-Datei des Netzes, das die Körper klassifiziert, nur für diese Sitzung. Leer lassen, um {path} zu verwenden. Sie wird vor dem Start geprüft."},
    "params": {"it": "Parametri YOLO:", "en": "YOLO settings:",
               "fr": "Paramètres YOLO :", "de": "YOLO-Parameter:"},
    "summary": {"it": "soglia {confidence:.2f} · doppioni {nms_iou:.2f} · tessere {tiles}",
                "en": "threshold {confidence:.2f} · duplicates {nms_iou:.2f} · tiles {tiles}",
                "fr": "seuil {confidence:.2f} · doublons {nms_iou:.2f} · tuiles {tiles}",
                "de": "Schwelle {confidence:.2f} · Duplikate {nms_iou:.2f} · Kacheln {tiles}"},
    "no_tiles": {"it": "nessuna (solo foto intera)", "en": "none (whole photo only)",
                 "fr": "aucune (photo entière seulement)", "de": "keine (nur ganzes Foto)"},
    "settings": {"it": "Parametri YOLO", "en": "YOLO settings",
                 "fr": "Paramètres YOLO", "de": "YOLO-Parameter"},
    "settings_help": {
        "it": "Soglia di confidenza, sovrapposizione per i doppioni e viste ingrandite del "
              "rilevatore. Valgono per entrambe le modalità YOLO e si salvano con il progetto.",
        "en": "Confidence threshold, duplicate overlap and magnified views of the detector. "
              "They apply to both YOLO modes and are saved with the project.",
        "fr": "Seuil de confiance, chevauchement des doublons et vues agrandies du détecteur. "
              "Ils valent pour les deux modes YOLO et sont enregistrés avec le projet.",
        "de": "Konfidenzschwelle, Überlappung für Duplikate und vergrößerte Ansichten des "
              "Detektors. Sie gelten für beide YOLO-Modi und werden mit dem Projekt gespeichert."},
    "threshold_help": {
        "it": "Confidenza minima perché un riquadro YOLO conti come corpo (0.25 predefinito). "
              "Dipende dal modello: alzala se il pretest mostra riquadri su scritte, fori o "
              "piazzole, abbassala se perde componenti. È lo stesso cursore «Confidence "
              "Threshold» di Roboflow. I riquadri sotto soglia restano nell'audit.",
        "en": "Minimum confidence for a YOLO box to count as a body (default 0.25). It "
              "depends on the model: raise it if the pretest shows boxes on text, holes or "
              "pads, lower it if components are missed. It is Roboflow's «Confidence "
              "Threshold» slider. Boxes below the threshold stay in the audit.",
        "fr": "Confiance minimale pour qu'un rectangle YOLO compte comme un corps (0.25 par "
              "défaut). Elle dépend du modèle : augmentez-la si le prétest montre des "
              "rectangles sur des textes, trous ou pastilles, baissez-la s'il manque des "
              "composants. C'est le curseur « Confidence Threshold » de Roboflow. Les "
              "rectangles sous le seuil restent dans l'audit.",
        "de": "Mindestkonfidenz, ab der ein YOLO-Rahmen als Körper zählt (Standard 0.25). "
              "Sie hängt vom Modell ab: erhöhen, wenn der Vortest Rahmen auf Beschriftungen, "
              "Löchern oder Pads zeigt, senken, wenn Bauteile fehlen. Es ist Roboflows "
              "Regler „Confidence Threshold“. Rahmen unter der Schwelle bleiben im Audit."},
    "vlm_model": {"it": "Modello di visione:", "en": "Vision model:",
                  "fr": "Modèle de vision :", "de": "Sehmodell:"},
    "vlm_pick": {"it": "Scegli modello…", "en": "Choose model…",
                 "fr": "Choisir le modèle…", "de": "Modell wählen…"},
    "vlm_help": {"it": "Modello che guarda la fotografia ed elenca i corpi. "
                       "Vuoto: lo stesso modello AI dell'estrazione.",
                 "en": "Model that looks at the photograph and lists the bodies. "
                       "Empty: the same AI model as the extraction.",
                 "fr": "Modèle qui regarde la photographie et liste les corps. "
                       "Vide : le même modèle IA que l'extraction.",
                 "de": "Modell, das das Foto ansieht und die Körper auflistet. "
                       "Leer: dasselbe KI-Modell wie die Extraktion."},
    "browse": {"it": "Sfoglia…", "en": "Browse…",
               "fr": "Parcourir…", "de": "Durchsuchen…"},
    # .onnx con onnxruntime, .pt con PyTorch/Ultralytics (utente, 2026-09-25).
    "picker": {"it": "Seleziona modello YOLO (ONNX o PyTorch) per questa sessione",
               "en": "Select a YOLO model (ONNX or PyTorch) for this session",
               "fr": "Choisir un modèle YOLO (ONNX ou PyTorch) pour cette session",
               "de": "YOLO-Modell (ONNX oder PyTorch) für diese Sitzung auswählen"},
    "filter": {"it": "Modelli YOLO (*.onnx *.pt)", "en": "YOLO models (*.onnx *.pt)",
               "fr": "Modèles YOLO (*.onnx *.pt)", "de": "YOLO-Modelle (*.onnx *.pt)"},
    "default": {"it": "Predefinito: {path}", "en": "Default: {path}",
                "fr": "Par défaut : {path}", "de": "Standard: {path}"},
    "path_help": {
        "it": "File .onnx o .pt facoltativo, solo per questa sessione (.pt richiede PyTorch). Lascia vuoto per usare {path}. La presenza del file non attiva YOLO automaticamente.",
        "en": "Optional .onnx or .pt file, for this session only (.pt needs PyTorch). Leave empty to use {path}. An existing file does not enable YOLO automatically.",
        "fr": "Fichier .onnx ou .pt facultatif, pour cette session uniquement (.pt nécessite PyTorch). Laisser vide pour utiliser {path}. La présence du fichier n’active pas YOLO automatiquement.",
        "de": "Optionale .onnx- oder .pt-Datei, nur für diese Sitzung (.pt benötigt PyTorch). Leer lassen, um {path} zu verwenden. Eine vorhandene Datei aktiviert YOLO nicht automatisch.",
    },
    "help": {
        "it": "YOLO locale propone i riquadri dei componenti. Il verificatore AI configurato continua a consumare credito AI. L’abbinamento ai modelli KiCad avviene dopo; i rilevamenti incerti richiedono revisione. «Solo YOLO locale» è un pretest: mostra soltanto i riquadri trovati da YOLO sulla foto, senza nessuna chiamata AI. La scelta vale solo per questa sessione.",
        "en": "Local YOLO proposes component boxes. The configured AI verifier still uses AI credit. KiCad model matching happens afterwards; uncertain detections require review. «Local YOLO only» is a pretest: it only shows the boxes YOLO finds on the photo, with no AI call at all. This choice applies only to this session.",
        "fr": "YOLO local propose les rectangles des composants. Le vérificateur IA configuré consomme toujours du crédit IA. L’association aux modèles KiCad intervient ensuite ; les détections incertaines nécessitent une révision. « YOLO local seul » est un prétest : il montre seulement les rectangles trouvés par YOLO sur la photo, sans aucun appel IA. Ce choix s’applique uniquement à cette session.",
        "de": "Lokales YOLO schlägt Begrenzungsrahmen für Bauteile vor. Die konfigurierte KI-Prüfung verbraucht weiterhin KI-Guthaben. Die Zuordnung zu KiCad-Modellen erfolgt anschließend; unsichere Erkennungen müssen überprüft werden. „Nur lokales YOLO“ ist ein Vortest: Es zeigt nur die von YOLO auf dem Foto gefundenen Rahmen, ohne jeden KI-Aufruf. Diese Auswahl gilt nur für diese Sitzung.",
    },
    "error_title": {"it": "YOLO locale non disponibile", "en": "Local YOLO unavailable",
                    "fr": "YOLO local indisponible", "de": "Lokales YOLO nicht verfügbar"},
    "error": {
        "it": "Impossibile avviare YOLO locale: {error}\nSeleziona un modello .onnx o .pt valido e verifica che sia installato onnxruntime (per .onnx) o PyTorch/Ultralytics (per .pt), oppure scegli AI. Nessuna richiesta AI è stata avviata; i risultati precedenti sono conservati.",
        "en": "Cannot start local YOLO: {error}\nSelect a valid .onnx or .pt model and check that onnxruntime (for .onnx) or PyTorch/Ultralytics (for .pt) is installed, or choose AI. No AI request was started; previous results are preserved.",
        "fr": "Impossible de démarrer YOLO local : {error}\nChoisir un modèle .onnx ou .pt valide et vérifier qu’onnxruntime (pour .onnx) ou PyTorch/Ultralytics (pour .pt) est installé, ou choisir IA. Aucune requête IA n’a été lancée ; les résultats précédents sont conservés.",
        "de": "Lokales YOLO kann nicht gestartet werden: {error}\nWählen Sie ein gültiges .onnx- oder .pt-Modell und prüfen Sie, ob onnxruntime (für .onnx) oder PyTorch/Ultralytics (für .pt) installiert ist, oder wählen Sie KI. Es wurde keine KI-Anfrage gestartet; bisherige Ergebnisse bleiben erhalten.",
    },
}


#: Label column shared by the forms of a panel, in pixels: the width every
#: form gets so that their fields start on the same vertical line.
FORM_LABEL_MINIMUM_WIDTH = 132
#: Room between the end of the longest label and its field, in pixels.
FORM_LABEL_PADDING = 6


def _form_labels(form: QFormLayout) -> list[QLabel]:
    """@brief The label widgets of a form, one per row that has one.
    @param form Form layout to inspect.
    @return Labels in row order; rows spanning both columns are skipped.
    """
    labels = []
    for row in range(form.rowCount()):
        label_item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
        label = label_item.widget() if label_item is not None else None
        if isinstance(label, QLabel):
            labels.append(label)
    return labels


def _align_form_labels(form: QFormLayout, width: int = FORM_LABEL_MINIMUM_WIDTH) -> int:
    """@brief Give every label of a form the same column width, wide enough for all of them.

    @param form Form layout whose labels are aligned.
    @param width Minimum column width in pixels, shared with the other forms
        of the panel so their fields line up.
    @return The width applied.
    @note The column is never narrower than the longest label plus
        `FORM_LABEL_PADDING`: a fixed column cut «Designatore di riferimento:»
        (user, 2026-10-12) and would cut any longer translation. The fields
        take the remaining room. Label texts change with the language, so the
        caller must align again after every retranslation
        (`MainWindow._realign_form_labels`).
    """
    labels = _form_labels(form)
    for label in labels:
        # The application style sheet sets the font (theme.APP_QSS, 10 pt):
        # measured before the style is applied, a label reports the smaller
        # default font and the column comes out short (user, 2026-10-12).
        label.ensurePolished()
        needed = label.fontMetrics().horizontalAdvance(label.text()) + FORM_LABEL_PADDING
        width = max(width, needed)
    for label in labels:
        label.setFixedWidth(width)
    return width

#: Marks preview grid lines so fit-to-content calculations can exclude them.
_GRID_ITEM = 0


def _side_pinout(side: dict) -> list[dict]:
    """Numero pin -> sigla serigrafata, per un lato del modulo.

    Le sigle arrivano nell'ordine in cui si incontrano lungo il bordo, la
    stessa convenzione con cui il generatore numera i pad: basta appaiarle
    ai numeri, rispettando l'eventuale numerazione inversa.
    """
    first = int(side.get("first_pin", 1))
    numbers = list(range(first, first + int(side["count"])))
    if side.get("reverse"):
        numbers.reverse()
    return [
        {"pin": number, "name": name}
        for number, name in zip(numbers, side.get("pin_names") or ())
    ]


def _castellated_body_available(params) -> bool:
    """@brief Dice se l'estrazione ha stabilito il solido di un modulo castellato.

    @param params Parametri estratti (`ExtractedParams`) del componente.
    @return True quando l'ingombro della scheda del modulo e' noto.
    @note Per un modulo castellato il corpo 3D *e'* il suo PCB: il solido lo
        descrivono lunghezza e larghezza del modulo, con lo spessore della
        scheda al posto di un'altezza complessiva -- che infatti puo' mancare
        anche quando il modulo c'e' (un assemblaggio somma spessore PCB e
        altezze dei singoli componenti). Per questo la condizione non e'
        `has_3d_body()`, che pretende anche `body_height_mm`. Senza contorno
        non c'e' niente da costruire e il gruppo 3D non va acceso.
    """
    return bool(params.body_length_mm) and bool(params.body_width_mm)


def _generation_target(params):
    """@brief Il generatore 3D che costruira' DAVVERO questo componente.

    @param params Parametri del componente (matrice/package o modulo castellated).
    @return La funzione di `step_generator` che verra' chiamata.
    @note Esiste per una ragione sola: `_generate_3d` e' un involucro, e chi
        vuole sapere se la corsa e' interrompibile deve guardare la funzione
        vera. L'involucro accetta `cancel` per costruzione, quindi
        interrogarlo risponderebbe sempre di si' -- anche quando il
        generatore sotto non conosce il canale e il pulsante «Interrompi»
        non fermerebbe nulla.
    @note Si legge dal modulo al momento della chiamata (non si cattura alla
        definizione) cosi' una sostituzione -- prove, strumenti -- riguarda
        introspezione e chiamata insieme.
    """
    if isinstance(params, PackageGeometry):
        return generate_single_package_3d_model
    return (generate_castellated_3d_model
            if isinstance(params, CastellatedModuleParams) else generate_3d_model)


def generation_accepts_cancel(generator, params) -> bool:
    """@brief Dice se l'ordine di fermarsi arriva al lavoro vero, non solo all'involucro.

    @param generator Funzione che il worker sta per chiamare.
    @param params Parametri del componente, che scelgono il generatore reale.
    @return True solo quando chi costruisce il solido conosce il canale.
    @note Con `generator is _generate_3d` la domanda si gira al generatore
        reale: l'involucro accetta `cancel` sempre, quindi da solo direbbe
        «interrompibile» anche su un generatore che non lo e'. Per un
        generatore sostituito (prove, strumenti) vale la sua stessa firma.
    """
    if not accepts_keyword(generator, "cancel"):
        return False
    if generator is _generate_3d:
        return accepts_keyword(_generation_target(params), "cancel")
    return True


def _generate_3d(params, body: Body3DParams, project_dir: Path, fillets, face_texts,
                 progress=None, model_progress=None, cancel=None, photo_scale_px_per_mm=None):
    """Genera il 3D con il generatore adatto al tipo di componente.

    I due modelli sono diversi in modo sostanziale: il pin header e' un
    corpo con pin cilindrici che sporgono sotto, il modulo castellated e'
    un PCB con i semi-fori tagliati sul bordo e i componenti sopra.

    `cancel` e' il canale con cui il pulsante «Interrompi» della modale
    chiede la fine del lavoro: oggi il processo FreeCAD figlio (vedi
    step_generator), e l'abbinamento dei modelli KiCad appena `cad3d/` gli
    dara' un canale. Qui viene inoltrato in modo DIFENSIVO -- `cancel_kwargs`
    e `matching_cancel_kwargs` guardano la firma installata -- perche' questo
    involucro e i generatori evolvono separatamente e una funzionalita'
    accessoria non deve far cadere una generazione buona con un TypeError.

    `photo_scale_px_per_mm` e' la scala della foto calibrata (pixel per mm
    lungo la lunghezza della scheda), o None senza foto: il generatore ne
    ricava l'incertezza di misura dei corpi (`cad3d.measurement_uncertainty`).
    Stessa disciplina: passata solo se la firma installata la dichiara.
    """
    generator = _generation_target(params)
    scale = ({"photo_scale_px_per_mm": photo_scale_px_per_mm}
             if accepts_keyword(generator, "photo_scale_px_per_mm") else {})
    return generator(
        params, body, project_dir, fillets=fillets, face_texts=face_texts,
        progress=progress, model_progress=model_progress,
        **cancel_kwargs(generator, cancel),
        **matching_cancel_kwargs(generator, cancel),
        **scale,
    )


#: Names that must stay importable from this module after the worker move
#: (HANDOFF 130, T6). The re-exports of `gui.workers` keep old imports and
#: `gui.<name>` lookups working; the collaborators are the patch points the
#: workers resolve HERE at call time (`workers._patch_point`), so a test or a
#: tool that replaces one of them on this module still reaches the running
#: worker. Referencing them keeps pyflakes from calling them unused.
_WORKER_COMPATIBILITY = (
    QThread, Cad3DWorker, ExtractWorker, GenerateWorker, ReconcileWorker,
    TerminalDetailWorker, UpdateCheckWorker, YoloPretestWorker,
    _announce_freecad_measuring, _photo_scale_kwargs, accepted_kwargs, freecad_progress,
    phase_kwargs,
    extract_pin_header_params, build_pin_header, build_castellated_module, build_package,
    _generate_3d, generation_accepts_cancel,
)


#: Livelli della riga di stato del 3D (`MainWindow._set_3d_status`): solo gli
#: ultimi due vengono mostrati sotto il modello.
STATUS_INFO = "info"
STATUS_WARNING = "warning"
STATUS_ERROR = "error"


#: Campo macchina scritto in minuscolo_con_underscore (review_required,
#: auto_matched): non e' una frase per chi guarda, e' il valore di un campo
#: dell'esito di UN elemento.
_ITEM_STATUS_FIELD = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+")
#: Assegnazione macchina senza spazi (parts=1, local_rotation_deg=0,
#: package=R_0603): stessa natura del token di stato, scritta come coppia.
#: Nessuna prosa entra qui, perche' una frase contiene spazi e la
#: corrispondenza e' sull'intera clausola.
_ITEM_FIELD_ASSIGNMENT = re.compile(r"[a-z][a-z0-9_]*=\S*")
#: Punto di fine frase: un punto seguito da spazio o dalla fine del testo.
#: `C_0603_1608Metric.step` o `photo-review/x.json` NON lo contengono, quindi
#: un nome di file nell'intestazione non la trasforma in una frase.
_SENTENCE_END = re.compile(r"\.(?:\s|$)")


def _item_field_clause(clause: str) -> bool:
    """@brief Dice se la clausola e' un campo macchina e non un pezzo di prosa.

    @param clause Clausola gia' ripulita, cioe' senza spazi ai bordi ne' punto finale.
    @return True per `review_required`, `parts=1`, `local_rotation_deg=0`.
    """
    return bool(_ITEM_STATUS_FIELD.fullmatch(clause)
                or _ITEM_FIELD_ASSIGNMENT.fullmatch(clause))


def per_item_detail(message: str) -> bool:
    """@brief Dice se la riga e' il rendiconto di UN elemento fra i tanti in corso.

    @param message Riga di avanzamento, come arriva dai worker.
    @return True se e' dettaglio per-elemento da tenere fuori dalla modale.
    @note Richiesta dell'utente: «evitare di mettere il log dei componenti, non
        servirebbe a nulla». Durante l'abbinamento dei modelli la modale
        mostrava righe come «SOT-23-6 / …, 6 leads: 3 dimensional candidates;
        review_required; identity not verified.»: cambiavano a ogni componente
        e non dicevano nulla in piu' del contatore «7/41 componenti» che sta
        appena sopra. Nel log invece servono a ricostruire la corsa, e li'
        restano: questo filtro riguarda solo la modale.
    @note Forma riconosciuta: «intestazione dell'elemento: campo; campo; campo».
        L'intestazione e' cio' che precede l'ULTIMO «: », non il primo: una
        frase che contiene due punti a meta' («Estrazione completata.
        Confidenza: low. CONTATTI CASTELLATED: …») spezzava sul primo e
        finiva classificata come rendiconto. L'intestazione deve poi essere
        un'etichetta, cioe' non contenere una fine di frase: e' questo che
        distingue «QFN-24» da «Estrazione completata. Confidenza».
    @note EURISTICA DICHIARATA, unico criterio sul corpo: almeno una clausola
        deve essere un campo macchina (`_item_field_clause`). Prima bastavano
        tre clausole qualsiasi, e quel conteggio ha cancellato dalla modale
        righe vere e importanti, verificate sui log di due corse reali: la
        conferma di fine estrazione, l'avviso di credito OpenRouter residuo,
        gli inventari globali parziali e il riepilogo del rilevatore YOLO.
        Contare i punti e virgola non distingue un rendiconto da una frase con
        tre incisi, quindi il conteggio e' stato rimosso invece di essere
        tarato. Il prezzo dichiarato del cambio: restano fuori dal filtro le
        righe per-elemento fatte di sola prosa (le catene «<package>:
        rejected N family/terminal-incompatible candidates; …» e «<package>:
        topologia contatti sconosciuta; …»), che ora arrivano in modale. E' il
        verso sbagliato meno dannoso: mostrare una riga in piu' costa
        un'occhiata, cancellare l'avviso di spesa o di fallimento parziale
        costa una diagnosi. Cio' che renderebbe superfluo il filtro e' un
        marcatore esplicito sulle righe per-elemento alla sorgente (in
        `kicad_models.attach_models`), non una regola piu' fine qui.
    @note Nessun tetto sulla lunghezza dell'intestazione: il vecchio
        `len(head) > 120` non aveva derivazione. Il controllo di fine frase
        respinge gia' le righe lunghe di prosa, e una riga di diagnostica
        (`AI_CALL_PROMPT_*`) non e' un rendiconto per-elemento -- va filtrata
        dove si decide cosa e' diagnostica, non qui.
    """
    text = " ".join(str(message or "").split())
    if "crop_reading=" in text:
        return True
    head, separator, body = text.rpartition(": ")
    if not separator or ";" not in body:
        return False
    # Un'intestazione vuota resta valida: «: 3 dimensional candidates;
    # review_required; …» e' la stessa riga senza etichetta, e prima le due
    # forme ricevevano verdetti opposti.
    if _SENTENCE_END.search(head):
        return False
    clauses = [clause.strip().rstrip(".") for clause in body.split(";")]
    return any(_item_field_clause(clause) for clause in clauses)


#: Provenances that make a per-side size a DECLARED measure for the
#: generator: read or derived from the documents, or measured by the program
#: on the calibrated photo (HANDOFF 169). Estimated and undeclared stay
#: estimates the generator may reduce.
DECLARED_SOURCES = ("read", "derived", "measured")


def _side_size(side: dict, name: str):
    """A side's hole or pad size for the generator: declared as is, estimated as AssumedSize, or None."""
    value = side.get(f"{name}_mm")
    if value is None:
        return None
    if side.get(f"{name}_declared") is True:
        return float(value)
    from ..generators.castellated_module import AssumedSize
    return AssumedSize(float(value))


def _reference_pad(name: str) -> float:
    """The generator's reference pad size (dataclass default of CastellatedModuleParams), mm."""
    return float(getattr(CastellatedModuleParams, name))


def _declared_size(value, reference):
    """The field value as a declared size, or None when it is still the reference value."""
    return None if abs(float(value) - reference) < 1e-9 else float(value)


def _positive_frame(length, width):
    """(length, width) in mm when both are finite and positive, else None."""
    try:
        values = (float(length), float(width))
    except (TypeError, ValueError):
        return None
    return values if all(math.isfinite(v) and v > 0 for v in values) else None


#: Scene px per footprint mm of the 2D footprint preview (every drawer of
#: the preview, the drawing snap and the handles use this scale).
_FP_PREVIEW_MM_TO_PX = 20.0


#: Pixel size of the reference font a user text is laid out in before the
#: item is scaled to KiCad's size (see `_drawing_text_item`).
_FP_TEXT_REFERENCE_PX = 100


def _cap_height_px(font):
    """@brief Cap height of `font` in px: the outline height of «H».

    @param font The font to measure.
    @return Height of the QPainterPath outline of "H" (independent of the
        platform's capHeight metric, and real glyph geometry, not hinted
        integer metrics).
    """
    path = QPainterPath()
    path.addText(0.0, 0.0, font, "H")
    return path.boundingRect().height()


def _drawing_text_item(record, mm_to_px):
    """@brief Unplaced Qt item of a user text drawing, drawn at KiCad's size.

    @param record Validated text record (text, size, layer).
    @param mm_to_px Scene px per mm of the preview.
    @return A QGraphicsSimpleTextItem at the origin, not in any scene, with
        the layer colour, laid out in a `_FP_TEXT_REFERENCE_PX` px font and
        SCALED (setScale) so the outline of «H» is size x mm_to_px px tall:
        KiCad's text size is the cap height. Scaling the item, not choosing
        an integer font size, avoids a rounding error of up to 3 %.
    @note Same definition as the symbol editor (user, 2026-10-14: same UX in
        every tab). No horizontal stretch: measured against kicad-cli 10,
        no single factor brings the widths of KiCad's stroke font within
        5 % (mean error 13.6 %, 8 % with 1.117, while strings like GND,
        iiiiii, MMMMMM get worse), so the preview width stays the Qt font's
        own and remains an approximation of KiCad's.
    """
    return _kicad_size_text_item(record["text"], record.get("size", 1.0),
                                 _layer_color(record["layer"]), mm_to_px)


def _kicad_size_text_item(text, size, colour, mm_to_px):
    """@brief Unplaced Qt text item whose capitals are `size` mm tall, as KiCad draws them.

    @param text The text (one line).
    @param size KiCad text size in mm (= cap height).
    @param colour Brush colour.
    @param mm_to_px Scene px per mm of the preview.
    @return A QGraphicsSimpleTextItem at the origin, not in any scene, laid
        out in a `_FP_TEXT_REFERENCE_PX` px font and SCALED (setScale) so the
        outline of «H» is size x mm_to_px px tall.
    @note Shared by the user's text drawings and the Reference/Value
        properties (user, 2026-10-15: every text at the size the file gives it).
    """
    from PyQt6.QtWidgets import QGraphicsSimpleTextItem
    item = QGraphicsSimpleTextItem(text)
    item.setBrush(QBrush(QColor(colour)))
    font = item.font()
    font.setPixelSize(_FP_TEXT_REFERENCE_PX)
    item.setFont(font)
    cap = _cap_height_px(font)
    if cap > 0:
        item.setScale(size * mm_to_px / cap)
    return item


def _legacy_drawing_text_item(record, mm_to_px):
    """@brief Qt item of a user text with the font the preview used BEFORE 2026-10-14.

    @param record Validated text record (text, size, layer).
    @param mm_to_px Scene px per mm of the preview.
    @return An unplaced item whose font em is `size` mm (point size
        size x mm_to_px x 0.75, at least 6 pt): the text the user saw for a
        legacy centred record, measured only by `_migrate_drawing_texts` so
        the migrated anchor is the corner the user saw.
    """
    from PyQt6.QtWidgets import QGraphicsSimpleTextItem
    item = QGraphicsSimpleTextItem(record["text"])
    font = item.font()
    font.setPointSizeF(max(6.0, record.get("size", 1.0) * mm_to_px * 0.75))
    item.setFont(font)
    return item


def _drawing_text_baseline(item):
    """@brief Distance, scene px, from the top of a QGraphicsSimpleTextItem to its baseline.

    @param item The text item (its text and font are read).
    @return The ascent of the single line QTextLayout lays out from y = 0,
        which is where the item draws its baseline (fallback fonts included);
        the font's ascent when no line can be laid out.
    @note Same definition as the symbol editor's `_text_baseline` (user rule:
        same UX in every tab), duplicated to keep the two tabs uncoupled.
    """
    from PyQt6.QtGui import QFontMetricsF, QTextLayout
    layout = QTextLayout(item.text(), item.font())
    layout.beginLayout()
    line = layout.createLine()
    layout.endLayout()
    return line.ascent() if line.isValid() else QFontMetricsF(item.font()).ascent()


def _drawing_text_position(record, mm_to_px, item):
    """@brief Scene position (px) of a text drawing's Qt item.

    @param record Validated text record.
    @param mm_to_px Scene px per mm of the preview.
    @param item The text item built by `_drawing_text_item`.
    @return (x, y) for setPos: an anchored record (`USER_TEXT_ANCHOR`) puts
        the left edge of the drawn box and the text BASELINE on its point, so
        the text grows right and up with descenders below, as KiCad's
        ``(justify left bottom)`` and the symbol editor's bottom-left texts;
        a legacy record (no anchor) is centred on its point, as before.
    """
    from ..core.footprint import USER_TEXT_ANCHOR
    rect, scale = item.boundingRect(), item.scale()
    x, y = (v * mm_to_px for v in record["points"][0])
    if record.get("anchor") == USER_TEXT_ANCHOR:
        return x - rect.left() * scale, y - _drawing_text_baseline(item) * scale
    return x - rect.width() * scale / 2, y - rect.height() * scale / 2


def _migrate_drawing_texts(records):
    """@brief Convert the legacy centred text drawings to bottom-left anchors, keeping their drawn box.

    @param records Validated footprint drawing records.
    @return A new list: every text without ``anchor`` gets its point moved to
        the left edge and baseline of the text the preview drew for it
        BEFORE 2026-10-14 (measured with `_legacy_drawing_text_item`, the
        old em-based font the user saw), and the anchor mark; every other
        record is copied unchanged. Idempotent.
    @note The corner (left edge, baseline) stays where the user saw it; the
        text itself is then drawn at KiCad's size (cap height = size), so it
        grows from that corner. See `core.footprint.migrate_centred_text`:
        KiCad's stroke font may place a MIGRATED text a fraction of a
        character away from where it was; new texts are exact.
    """
    from ..core.footprint import migrate_centred_text
    scale = _FP_PREVIEW_MM_TO_PX
    result = []
    for record in records:
        if record.get("kind") == "text" and record.get("anchor") is None:
            item = _legacy_drawing_text_item(record, scale)
            rect = item.boundingRect()
            # Legacy item top-left was (point - size/2); the new anchor is
            # its left edge and baseline.
            offset = ((rect.left() - rect.width() / 2) / scale,
                      (_drawing_text_baseline(item) - rect.height() / 2) / scale)
            result.append(migrate_centred_text(record, offset))
        else:
            result.append(deepcopy(record))
    return result


def _arc_path(start, mid, end):
    """@brief QPainterPath of the circular arc through three scene points.

    @param start Arc start, scene px.
    @param mid Point the arc passes through, scene px.
    @param end Arc end, scene px.
    @return A path from start to end through mid: the circular arc when the
        points define one, else the two straight segments (collinear clicks).
    @note Sampled as short segments: the preview only needs the drawn look,
        the exported fp_arc keeps the exact three points.
    """
    (x1, y1), (x2, y2), (x3, y3) = start, mid, end
    path = QPainterPath(QPointF(x1, y1))
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-9:
        path.lineTo(x2, y2)
        path.lineTo(x3, y3)
        return path
    sq1, sq2, sq3 = x1 * x1 + y1 * y1, x2 * x2 + y2 * y2, x3 * x3 + y3 * y3
    cx = (sq1 * (y2 - y3) + sq2 * (y3 - y1) + sq3 * (y1 - y2)) / d
    cy = (sq1 * (x3 - x2) + sq2 * (x1 - x3) + sq3 * (x2 - x1)) / d
    radius = math.hypot(x1 - cx, y1 - cy)
    a1, am, a3 = (math.atan2(y - cy, x - cx) for x, y in (start, mid, end))
    ccw_mid, ccw_end = (am - a1) % (2 * math.pi), (a3 - a1) % (2 * math.pi)
    sweep = ccw_end if ccw_mid <= ccw_end else ccw_end - 2 * math.pi
    steps = max(16, int(abs(sweep) / 0.08))
    for index in range(1, steps + 1):
        angle = a1 + sweep * index / steps
        path.lineTo(cx + radius * math.cos(angle), cy + radius * math.sin(angle))
    return path


#: Layer colours of the 2D footprint preview: the default theme of KiCad's
#: PCB editor (user, 2026-10-13: «i colori predefiniti di KiCad»), so a
#: drawing looks the same here and in KiCad. One table for the generated
#: silkscreen, the user drawings, the layer selector and the ghost shape.
KICAD_LAYER_COLORS = {"F.SilkS": "#f2eda1", "F.Fab": "#afafaf", "Cmts.User": "#5994dc"}


def _layer_color(layer: str) -> str:
    """@brief The KiCad default colour of a footprint layer (silkscreen when unknown)."""
    return KICAD_LAYER_COLORS.get(layer, KICAD_LAYER_COLORS["F.SilkS"])


def _ghost_pen(color: str = YELLOW) -> QPen:
    """@brief The dashed pen of a shape in progress, as in the symbol editor.

    @param color The colour of the layer the shape goes to; yellow when
        no layer is known.
    """
    pen = QPen(QColor(color), 1.6, Qt.PenStyle.DashLine)
    pen.setCosmetic(True)
    return pen


def _layer_swatch(color: str) -> QIcon:
    """@brief A small filled square of a layer colour for the layer selector."""
    pixmap = QPixmap(12, 12)
    pixmap.fill(QColor(color))
    return QIcon(pixmap)


def _screen_dot(scene, x, y, size, outline, fill, role, z):
    """@brief A round marker of constant screen size at a scene point.

    @param scene Scene to draw in.
    @param x Scene x (px) of the centre.
    @param y Scene y (px) of the centre.
    @param size Diameter in screen px.
    @param outline Outline colour (hex).
    @param fill Fill colour (hex).
    @param role Data role set to True so the drawer can remove it later.
    @param z Z value.
    @return The ellipse item.
    """
    from PyQt6.QtWidgets import QGraphicsItem
    half = size / 2
    dot = scene.addEllipse(-half, -half, size, size, QPen(QColor(outline), 1.6), QBrush(QColor(fill)))
    dot.setPos(x, y)
    dot.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
    dot.setData(role, True)
    dot.setZValue(z)
    return dot


def _screen_handle(scene, x, y, size, solid):
    """@brief A drawing handle square like the symbol editor's (`SymbolTab._handle`).

    @param scene Scene to draw in.
    @param x Scene x (px) of the centre.
    @param y Scene y (px) of the centre.
    @param size Side in screen px.
    @param solid True for the whole-drawing anchor (opaque fill), False for
        a vertex (translucent fill, alpha 150 as in the symbol editor).
    @return The rect item, tagged with role 0x48474F and a cross cursor;
        the caller sets its handle key and tooltip.
    """
    from PyQt6.QtWidgets import QGraphicsItem
    fill = QColor(GREEN)
    if not solid:
        fill.setAlpha(150)
    half = size / 2
    square = scene.addRect(-half, -half, size, size, QPen(QColor(YELLOW), 1.0), QBrush(fill))
    square.setPos(x, y)
    square.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
    square.setData(0x48474F, True)
    square.setZValue(9)
    square.setCursor(Qt.CursorShape.CrossCursor)
    return square


def _step_older_than_footprint(footprint: Path, step: Path) -> bool:
    """@brief Tell whether the STEP predates the footprint it would ship with.

    @param footprint Generated .kicad_mod.
    @param step Generated .step of the same project.
    @return True when the STEP was written before the footprint.
    @note Heuristic, declared (audit 2026-09-25): a full generation writes the
        footprint first and the STEP after it, a 3D regeneration rewrites only
        the STEP; only a footprint-only generation leaves the STEP older. That
        STEP may still be right (the body may not have changed), so this is a
        warning, never a block. What would make it superfluous: a record of
        the parameters each file was generated from.
    """
    try:
        return step.stat().st_mtime < footprint.stat().st_mtime
    except OSError:
        return False


def _saved_pcb_thickness(saved) -> tuple[str, tuple[float, str] | None]:
    """@brief The PCB thickness provenance and last analysis proposal of a project file.

    @param saved The "pcb_thickness" object of a project state, or anything
        else for a file written before it existed.
    @return (provenance key of PCB_THICKNESS_SOURCE_LABELS, (value mm,
        provenance) of the last analysis or None). An older file says
        "project": its value is the project's, its origin was not recorded.
    """
    if not isinstance(saved, dict):
        return "project", None
    source = saved.get("source")
    if source not in PCB_THICKNESS_SOURCE_LABELS:
        source = "project"
    analysis = saved.get("analysis")
    proposal = None
    if (isinstance(analysis, (list, tuple)) and len(analysis) == 2
            and analysis[1] in PCB_THICKNESS_SOURCE_LABELS):
        value = analysis[0]
        if (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value > 0):
            proposal = (float(value), analysis[1])
    return source, proposal


def _project_list(mapping: dict, key: str, skipped: list, label: str | None = None) -> list:
    """@brief A list-valued key of a saved project state, or [] when it is not a list.

    @param mapping The state (or a nested object of it) read from the file.
    @param key Key whose value must be a JSON array.
    @param skipped List of ignored settings, logged by `_apply_project_state`.
    @param label Name used in the log (default: `key`).
    @return The list (a tuple from a direct caller is accepted too); [] for
        an absent or null key; [] and one `skipped` entry for any other type.
    @note One rule for every list-valued key: a hand-edited file with
        ``"sides": 5`` used to crash the opening (iterating an int), while a
        string or an object was iterated character by character or key by
        key and silently produced nothing.
    """
    value = mapping.get(key) if isinstance(mapping, dict) else None
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    skipped.append(f"{label or key} ({type(value).__name__})")
    return []


class MainWindow(QMainWindow):
    """@brief Coordinate the six-tab component generation workflow."""

    def __init__(self, profile_store=None):
        super().__init__()
        self._refresh_window_title()
        # Size requested for startup; reserve space for the native frame/taskbar.
        screen = self.screen()
        available = screen.availableGeometry() if screen is not None else None
        self.resize(min(1390, max(1, available.width() - 40)) if available else 1390,
                    min(1050, max(1, available.height() - 40)) if available else 1050)
        self.setMinimumSize(min(660, self.width()), min(700, self.height()))

        self._worker: GenerateWorker | None = None
        self._extract_worker: ExtractWorker | None = None
        self._cad3d_worker: Cad3DWorker | None = None
        self._terminal_worker: TerminalDetailWorker | None = None
        self._reconcile_worker: ReconcileWorker | None = None
        self._last_params: PinHeaderParams | CastellatedModuleParams | PackageGeometry | None = None
        #: Ultimo risultato dell'estrazione applicato ai campi. Serve a
        #: rilanciare l'inventario sulle proposte salvate: le quote con cui la
        #: foto fu calibrata e i lati dichiarati stanno qui, non nei campi,
        #: che nel frattempo l'utente puo' aver ritoccato.
        self._last_extracted: ExtractedParams | None = None
        #: Marca temporale (stesso formato dei file di evidenza) dell'ultima
        #: estrazione avviata: le proposte salvate valgono solo se sono nate
        #: da quella: la cartella photo-cache del progetto conserva anche le
        #: evidenze delle estrazioni precedenti.
        self._extraction_started_stamp: str | None = None
        #: Vero quando la finestra di revisione ha gia' scritto il bundle
        #: photo-review per l'estrazione in corso: allora _on_extracted non
        #: deve sovrascriverlo con le righe pre-revisione della cache.
        self._reviewed_this_extraction = False
        # Rilevatore dell'ultima estrazione (vedi _start: pacchetti di ripiego).
        self._extraction_detector = 'yolo'
        #: Vero quando la base dell'ultimo rilancio inventario e' stata
        #: sintetizzata dai campi (dopo un riavvio) e non estratta davvero:
        #: su quella base non si genera automaticamente.
        self._relaunch_base_synthesized = False
        self.package_geometry: PackageGeometry | None = None
        self.fillet_annotations: list[EdgeFilletAnnotation] = []
        self.face_text_annotations: list[FaceTextAnnotation] = []
        self.pinout: list[dict] = []
        # Componenti del modulo letti dalla foto: alimentano il 3D, non il
        # footprint (vedi `_photo_rows`, che ne tiene anche i metadati).
        self.castellated_components: list[ComponentBox] = []
        #: Componenti della foto come dict (geometria + metadati del
        #: rilevamento): la fonte di verita' dei corpi, vedi `_photo_row`.
        self._photo_rows: list[dict] = []
        #: Corpi aggiunti dal solo modello e non (ancora) tenuti
        #: (`_ai_only_excluded` True, HANDOFF 141): fuori dalla tabella e dal
        #: 3D, ma conservati qui -- e nel progetto salvato -- perche' la
        #: revisione fotografica li possa riproporre.
        self._photo_excluded_rows: list[dict] = []
        self._extracted_pcb_thickness = None
        #: Provenance of the PCB thickness in use (PCB_THICKNESS_SOURCE_LABELS).
        self._pcb_thickness_source = "standard"
        #: (value mm, provenance) proposed by the last analysis, or None:
        #: what «Ripristina» goes back to.
        self._analysis_pcb_thickness = None
        self.pin_map: dict | None = None
        self.header_pad_overrides: dict[str, dict] = {}
        #: Chosen positions of the footprint's Reference and Value texts,
        #: {key: (x, y)} mm in footprint coordinates (user, 2026-10-13);
        #: a key left out keeps the generator's default.
        self.footprint_text_positions: dict[str, tuple] = {}
        #: User drawings of the footprint preview (validated records, see
        #: core.footprint.validate_footprint_drawings); drawn in every 2D
        #: preview and appended to the exported .kicad_mod (user, 2026-10-13).
        self.footprint_drawings: list = []
        #: Armed palette tool of the footprint preview, or None.
        self._fp_draw_tool: str | None = None
        #: Points already clicked for the shape in progress, footprint mm.
        self._fp_draw_clicks: list = []
        #: Snapped pointer position while a tool is armed, footprint mm, or None.
        self._fp_draw_hover: tuple | None = None
        #: Index of the drawing whose handles are shown (-1: none).
        self._fp_hovered_drawing: int = -1
        #: (drawing index, point index, snapped point mm) of the handle being
        #: dragged; point index -1 = the whole drawing, and the third item is
        #: then the (dx, dy) translation in mm.
        self._fp_handle_drag: tuple | None = None
        #: Snapped grab point (mm) of a whole-drawing drag, or None.
        self._fp_handle_origin: tuple | None = None
        #: True between a handle press and its drop.
        self._fp_handle_active: bool = False
        #: True when the drawings list changed during that drag: its drop
        #: (and any further move) is ignored, its indices are stale.
        self._fp_handle_cancelled: bool = False
        #: «Footprint dirty»: a saved edit is not yet in the .kicad_mod.
        #: Consumed by `_flush_footprint_rewrite` just before its single
        #: attempt (a failed start is logged once, never retried in a loop)
        #: and cleared when any generation starts or a project is opened.
        self._fp_rewrite_dirty: bool = False
        #: True when a pending edit is a pad edit: those rewrite the
        #: .kicad_mod even when none exists yet (drawings and texts do not).
        self._fp_rewrite_pad_edit: bool = False
        #: True while one rewrite attempt is queued on the event loop.
        self._fp_rewrite_pending: bool = False
        #: Undo/redo stacks of the footprint edits, like the symbol editor:
        #: each entry is a full edit state (`_footprint_edit_state`: drawings,
        #: text positions, host pad overrides, package pads), deep-copied.
        self._fp_drawings_undo: list = []
        self._fp_drawings_redo: list = []
        #: Footprint draft (user, 2026-10-14): the four edit fields above ARE
        #: the draft the preview draws; this is the applied state (what the
        #: project file and the .kicad_mod hold), captured at the first edit
        #: after an apply, or None when nothing was edited since.
        self._fp_applied: dict | None = None
        #: Package object the applied state and the undo history refer to:
        #: an analysis that replaces the package rebases them
        #: (`_sync_footprint_draft_package`).
        self._fp_draft_package = None
        #: Draft offered by «Recupera bozza» (JSON form, from the recovery
        #: file after a crash), or None.
        self._fp_recoverable: dict | None = None
        #: Where the offered draft is stored: "main" (the current-draft
        #: file) or "kept" (the kept-drafts file); None when none is offered.
        self._fp_recoverable_source: str | None = None
        #: True when the project file holds applied footprint edits that the
        #: .kicad_mod does not (a rewrite that could not run before leaving
        #: the project); saved with the project, cleared by the next footprint
        #: written, shown in the KiCad tab.
        self._fp_file_stale: bool = False
        #: True while `_flush_footprint_rewrite` starts the rewrite of an
        #: APPLIED edit: `_generate` must not ask about a newer draft then.
        self._fp_rewrite_flushing: bool = False
        #: True while `_run_pending_regen_3d` restarts the 3D model by itself:
        #: `_regenerate_3d` does not ask about the footprint draft then.
        self._regen_3d_automatic: bool = False
        #: Pending state last shown by the KiCad tab (refreshed on a change).
        self._fp_draft_was_pending: bool = False
        self._last_mesh_path: Path | None = None
        self._model_revision = 0
        #: Revisione dell'analisi AI, gemella di `_model_revision` per i
        #: risultati CAD tardivi. Una chiamata HTTP gia' partita non si puo'
        #: interrompere (urllib bloccante): interromperla vuol dire
        #: ABBANDONARLA, e il suo risultato -- che arrivera' comunque -- va
        #: riconosciuto come scaduto e buttato. Il worker viene marchiato con
        #: il valore corrente alla partenza; l'interruzione lo incrementa.
        self._analysis_revision = 0
        #: `_analysis_revision` of the last run whose result started being
        #: applied to the fields (`_on_extracted`), None when none did. Read
        #: and consumed by `_on_extract_finished`: only a run that changed the
        #: fields owns the output; a failed one leaves a pending footprint
        #: edit to its retry. Abandoning a run bumps the revision, so a stale
        #: value never matches a later run.
        self._extraction_applied_revision: int | None = None
        #: Workers abandoned with «Interrompi» that are still running. The
        #: list keeps a reference of its own (a live QThread collected by the
        #: garbage collector aborts the process) and removes them from the
        #: candidates of a second abort (`_abortable_operation`). It is NOT
        #: what the close guard reads: `closeEvent` sees an abandoned worker
        #: through its slot (`_extract_worker`, `_reconcile_worker`), which
        #: nothing reassigns while it runs because every paid start checks
        #: `_background_work_running()` first; `_forget_abandoned_worker`
        #: frees the slot when the worker finally emits `finished`.
        self._abandoned_workers: list = []
        #: Vero fra la conferma dell'interruzione e la fine effettiva del
        #: lavoro che si sta fermando: impedisce che un secondo clic riapra la
        #: domanda o mandi un secondo ordine.
        self._abort_pending = False
        self._acquisition_completed = False
        #: Vero quando l'analisi appena conclusa ha prodotto un inventario
        #: fotografico da far rivedere: e' cio' che rende dovuta la pausa fra
        #: l'inventario a pagamento e l'abbinamento modelli/3D. Si consuma
        #: nella pausa stessa, cosi' una seconda `finished` non la ripropone.
        self._inventory_pause_pending = False
        #: Vero mentre la pausa e' aperta: la revisione gira in un event loop
        #: annidato e da li' si puo' rientrare in questa finestra.
        self._inventory_pause_active = False
        #: Vero mentre una generazione manuale sta mostrando la modale: e' la
        #: finestra ad averla aperta, quindi e' lei a doverla chiudere.
        self._manual_cad_overlay = False
        #: Vero dall'avvio di un'estrazione, di un rilancio o di un pretest
        #: fino alla loro fine (o all'abbandono): finche' e' vero, testo e
        #: suggerimento del pulsante di estrazione e i comandi YOLO non si
        #: toccano (audit 25/09, B6). An explicit flag and not
        #: QThread.isRunning, because the run's lifetime is not the lifetime
        #: of any one thread: it goes on after the extraction thread into the
        #: automatic generation (cleared in `_on_generation_finished`), and
        #: it ends BEFORE the thread on «Interrompi», whose
        #: abandoned worker keeps running until its HTTP call returns. (The
        #: older justification -- isRunning still true inside a queued
        #: `finished` slot -- was wrong: measured, it is already False there.)
        self._extraction_busy = False
        # La rotellina scorre la pagina e non cambia tendine o numeri non
        # cliccati (2026-09-26: la tipologia cambiata scorrendo la scheda).
        from .wheel_guard import install as install_wheel_guard
        install_wheel_guard()
        #: Quale insieme di campi del corpo descrive la scheda della foto in
        #: `_photo_png`: "cast" (castellated) o "matrix". Se ne leggono i
        #: valori attuali, cosi' una correzione delle quote vale e un cambio di
        #: tipologia non sposta la scala. None = ignoto.
        self._photo_body_fields = None
        #: Vero quando le quote della scheda sono NOTE (da un'analisi o scritte
        #: dall'utente). Prima valevano sempre i valori iniziali dei campi
        #: (42.8 x 15.2 mm, un modulo di prova), e il pretest calibrava la foto
        #: su quelli: sull'ESP32 la scala usciva del 32% sbagliata (26/09).
        self._board_size_known = False
        self._config = load_config()
        #: Problems found reading config.json, (template, values) pairs:
        #: logged once the translator and the log exist (end of __init__).
        from .app_config import take_load_problems
        self._config_load_notes: list = take_load_problems()
        self.profile_store = profile_store or ProfileStore(legacy_config=self._config, legacy_path=config_path())
        self.i18n = I18n(self.profile_store, self._config.get("language", "it"))
        # Log, avanzamento ed errori passano da `messages.tx`: da qui in avanti
        # si scrivono nella lingua scelta (utente, 2026-09-24).
        from ..services.messages import set_translator
        set_translator(self.i18n.raw)
        # Anche i testi propri di Qt (Si'/No/Annulla dei QMessageBox, menu
        # contestuali) nella lingua scelta, non in inglese (26/09).
        from .qt_translations import install_qt_translations
        install_qt_translations(self.i18n.language)
        # Modello scelto solo per questa sessione (datasheet difficile): non
        # viene salvato in config.json e si perde alla chiusura, per scelta.
        self._session_model: str | None = None
        #: Scelta di sessione per il modello che guarda la fotografia. None
        #: significa «quello configurato nel profilo»: all'avvio non si impone
        #: un modello deciso qui dentro, perche' una costante nel codice della
        #: finestra vince in silenzio sulle impostazioni dell'utente e manda la
        #: chiamata a pagamento a un modello che lui non ha scelto.
        self._vlm_model: str | None = None
        self._auto_after_extraction = False
        self._auto_generation_params = None
        self._applying_extraction = False
        self._extract_finish_pending = False
        self._automatic_generation_active = False
        # Rigenerazione 3D chiesta da un «Applica» e non ancora partita
        # (vedi `_run_pending_regen_3d`).
        self._regen_3d_pending = False
        self._automatic_footprint_done = False
        self._automatic_3d_done = False
        #: True quando la corsa automatica in atto ha chiesto anche il modello
        #: 3D. Falso = generazione di solo footprint, che e' un esito completo.
        self._automatic_3d_expected = True
        self._automatic_3d_deferred_reason = ""
        self._automatic_generation_errors = []
        self._startup_dependencies_checked = False
        #: Messaggi del log della sessione, in ordine (vedi `_log_plain_text`).
        self._log_lines: list[str] = []
        #: True/False appena `_init_parameters` ha interrogato il sistema; None
        #: prima. Va ricordato: `find_freecadcmd()` e' una
        #: ricerca su disco e l'estrazione non deve rifarla per sapere se il
        #: modello 3D e' generabile.
        self._freecad_available: bool | None = None
        #: Coppie (copia nella scheda Modello 3D, originale nella scheda
        #: Progetto), per allinearne la visibilita' a genitore acquisito.
        self._project_action_mirrors: list = []
        #: Comandi a sola icona della barra 3D con testi propri (rigenera,
        #: accetta): i loro tooltip si ricompongono a ogni cambio di lingua.
        self._workflow_buttons: list = []
        #: Rendering del modello 3D rimandato all'ingresso nella scheda:
        #: (revisione del modello, percorso della mesh) finche' la chiamata
        #: differita non e' partita, None altrimenti. Vedi `_enter_3d_tab`.
        self._pending_3d_render = None
        #: True mentre `render_mesh` sta lavorando: un secondo ingresso nella
        #: scheda in quel momento non deve accodare un altro rendering.
        self._rendering_3d = False
        #: Preparazione della mesh in corso fuori dal thread GUI
        #: (`Preview3DVtk.prepare_mesh`): (token, revisione del modello, cosa
        #: fare a mesh pronta, cosa fare in errore), None altrimenti. Solo la
        #: richiesta piu' recente viene servita. Vedi `_prepare_mesh_then`.
        self._mesh_request = None
        #: True finche' il cursore di attesa messo dall'indicatore e' attivo:
        #: `restoreOverrideCursor` va chiamato una volta per ogni `set`.
        self._loading_cursor_set = False

        self._build_menu()
        self._build_central()
        self._install_shortcuts()
        # Project type is restored before the footprint parameters are used.
        self.group_castellated.setChecked(
            self._selected_family() == "aggregate_castellated"
        )
        self._refresh_docs_list()
        # Il campo nasce vuoto, quindi nessun segnale di modifica lo annuncia:
        # il blocco va applicato una volta all'avvio.
        self._apply_component_name_gate()

        # Connect after initialization so construction does not invalidate a worker.
        for key, value in vars(self).copy().items():
            if key.startswith("field_"):
                if isinstance(value, BoundedValue) or (isinstance(value, TextValue) and key in {
                        "field_body_color", "field_pin_color", "field_populated_pins"}):
                    value.on_change(lambda _value: self._invalidate_generated_mesh())
        self._init_project_settings()
        self._refresh_start_page()
        self._prepare_native_preview_3d_slot()
        # A config.json value replaced by its default, or a corrupt file
        # kept aside, is said in the session log (not in a project log:
        # it is not about any project).
        for template, values in self._config_load_notes:
            self._append_log(tx(template, **values), persist=False)

    def _prepare_native_preview_3d_slot(self) -> None:
        """@brief Create the native windows of the 3D pane's ancestor chain before the window shows.

        @note The VTK viewer is a native window. When it is inserted into a
            window already on screen, Qt makes every ancestor native and
            re-creates their windows: the whole main window blinked off and
            on at the swap (user, 2026-10-12, seen frame by frame). The
            placeholder asks for a native window (`WA_NativeWindow`), but a
            widget in a hidden tab is not created until shown; `winId()`
            creates it now, hidden, with its ancestors, so the later swap
            finds the chain native and re-creates nothing.
        """
        if not self.preview_3d_loaded():
            self.preview_3d.winId()

    # ── Menu ────────────────────────────────────────────────────────

    def _build_menu(self) -> None:
        file_menu = self.menu_file = self.menuBar().addMenu(self.i18n.tr("app.file"))
        # Il progetto E' la sua cartella (utente, 2026-09-24): si crea o si
        # apre esplicitamente, non scrivendo un nome che per caso coincide con
        # una cartella esistente.
        self.action_new_project = file_menu.addAction(
            self.i18n.text("Nuovo progetto..."), self._new_project)
        self.action_new_project.setShortcut(QKeySequence.StandardKey.New)
        self.action_open_project = file_menu.addAction(
            self.i18n.text("Apri progetto..."), self._open_project)
        self.action_open_project.setShortcut(QKeySequence.StandardKey.Open)
        self.action_rename_project = file_menu.addAction(
            self.i18n.text("Rinomina progetto..."), self._rename_project)
        # Progetti recenti (PIANO-FIX 1): riempito a ogni apertura, cosi'
        # elenca solo le cartelle che sono ancora progetti.
        self.menu_recent = file_menu.addMenu(self.i18n.text("Progetti recenti"))
        self.menu_recent.setToolTipsVisible(True)
        self.menu_recent.aboutToShow.connect(self._fill_recent_menu)
        # «Apri cartella componente» e i comandi KiCad, tolti da qui il
        # 2026-09-25, stanno nel menu «Strumenti» (decisione del 2026-09-26).
        file_menu.addSeparator()
        self.action_exit = file_menu.addAction(self.i18n.text("Esci"), self.close)

        config_menu = self.menu_config = self.menuBar().addMenu(self.i18n.tr("app.configuration"))
        config_menu.setToolTipsVisible(True)
        self.action_profiles = config_menu.addAction(
            QIcon(str(Path(__file__).parent / 'assets' / 'profile-edit.svg')),
            self.i18n.text("Gestione tipologie e profili AI..."), self._edit_profiles)
        self.action_defaults = config_menu.addAction(
            self.i18n.text("Default generali AI..."), self._edit_llm_settings)
        # Chiave OpenRouter nel Gestore credenziali di Windows (PIANO-FIX 2).
        self.action_ai_account = config_menu.addAction(
            self.i18n.text("Account AI..."), self._open_ai_account)
        self.action_ai_account.setToolTip(self.i18n.text(
            "Chiave OpenRouter, credito e limiti: incolla, verifica e salva la chiave nel "
            "Gestore credenziali di Windows."))
        config_menu.addSeparator()

        self.action_generation_options = config_menu.addAction(
            self.i18n.text("Opzioni di generazione..."), self._edit_generation_options)
        self.action_accepted_models = config_menu.addAction(
            self.i18n.text("Archivio abbinamenti accettati..."), self._show_accepted_models)
        # «Apri config.json» e' nel menu «Strumenti» (decisione del 2026-09-26).

        # Strumenti (utente, 2026-09-26): le sei voci tolte il 25/09 dai menu
        # File, Configurazione e «?». I comandi KiCad restano anche nella
        # scheda KiCad; voci e pulsanti si accendono insieme
        # (`_refresh_output_actions_inner` gestisce entrambi per nome).
        tools_menu = self.menu_tools = self.menuBar().addMenu(self.i18n.tr("app.tools"))
        tools_menu.setToolTipsVisible(True)
        self.action_open_output = tools_menu.addAction(
            self.i18n.text("Apri cartella componente"), self._open_project_dir)
        tools_menu.addSeparator()
        self.action_test_kicad = tools_menu.addAction(
            self.i18n.text("Prova in KiCad..."), self._test_in_kicad)
        self.action_export = tools_menu.addAction(
            self.i18n.text("Installa nel progetto KiCad..."), self._install_project_library)
        self.action_install_global = tools_menu.addAction(
            self.i18n.text("Installa come libreria globale..."), self._install_global_library)
        tools_menu.addSeparator()
        # Installato, la cartella scrivibile (config, database) e' quella
        # dell'utente, non quella del programma.
        self.action_open_tool_dir = tools_menu.addAction(
            self.i18n.text("Apri cartella progetto tool"),
            lambda: webbrowser.open(user_data_dir().as_uri()))
        self.action_open_config = tools_menu.addAction(
            self.i18n.text("Apri config.json"), self._open_config_file)

        help_menu = self.menu_help = self.menuBar().addMenu(self.i18n.tr("app.help"))
        # «Apri cartella progetto tool» tolta dall'utente il 2026-09-25.
        # Aiuto e assistenza (PIANO-FIX 5 e 6, 2026-09-26): guida, log,
        # segnalazione su GitHub, aggiornamenti solo a richiesta (nessuna
        # chiamata di rete all'avvio senza una decisione dell'utente).
        self.action_user_guide = help_menu.addAction(
            self.i18n.text("Guida d'uso"), self._open_user_guide)
        self.action_user_guide.setShortcut(QKeySequence.StandardKey.HelpContents)
        self.action_open_logs = help_menu.addAction(
            self.i18n.text("Apri cartella dei log"), self._open_log_folder)
        self.action_report_problem = help_menu.addAction(
            self.i18n.text("Segnala un problema..."), self._report_problem)
        self.action_check_updates = help_menu.addAction(
            self.i18n.text("Controlla aggiornamenti..."), self._check_for_updates)
        help_menu.addSeparator()
        # Licenza del programma e componenti di terze parti (utente, 2026-09-25).
        self.action_licenses = help_menu.addAction(self.i18n.text("Licenze"), self._show_licenses)
        self.action_about = help_menu.addAction(
            self.i18n.text("Informazioni su KiCadGen"), self._show_about)
        help_menu.addSeparator()
        self.action_shortcuts = help_menu.addAction(
            self.i18n.text("Scorciatoie da tastiera..."), self._show_shortcuts)

    def _edit_generation_options(self) -> None:
        dialog = GenerationOptionsDialog(self._include_nc_pins(), self,
                                         silk_pin_labels=self._silk_pin_labels(),
                                         pause_after_inventory=self._inventory_pause_enabled())
        self.i18n.retranslate_widgets(dialog)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        checked = dialog.include_nc_pins()
        pause = dialog.pause_after_inventory()
        pause_changed = pause != self._inventory_pause_enabled()
        self._set_project_option("include_nc_pins", checked)
        self._set_project_option("silk_pin_labels", dialog.silk_pin_labels())
        self._save_config_safely()
        if pause_changed:
            self._on_inventory_pause_toggled(pause)
        self._append_log(
            tx("Pin NC: verranno generati.") if checked
            else tx("Pin NC: NON verranno generati (attenzione: il piedino fisico resta).")
        )

    def _edit_llm_settings(self) -> None:
        try:
            defaults = self.profile_store.defaults()
        except (ProfileError, sqlite3.Error, OSError) as exc:
            QMessageBox.warning(self, self.i18n.text('Database profili'), str(exc))
            return
        dialog = LlmSettingsDialog(
            defaults["system_prompt"], defaults["model"], self,
            current_vision_model=defaults["vision_model"],
            current_grounding_prompt=defaults["grounding_prompt"],
            current_verification_prompt=defaults["verification_prompt"],
            current_verification_model=defaults["verification_model"],
            current_single_prompt=defaults.get("system_prompt_single"),
            current_numbering_prompt=defaults.get("numbering_prompt"),
            current_family=self._selected_family(),
        )
        self.i18n.retranslate_widgets(dialog)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        try:
            self.profile_store.save_defaults(dict(
                system_prompt=dialog.prompt() or DEFAULT_SYSTEM_PROMPT, model=dialog.model(),
                vision_model=dialog.vision_model(), verification_model=dialog.verification_model(),
                grounding_prompt=dialog.grounding_prompt(), verification_prompt=dialog.verification_prompt(),
                system_prompt_single=dialog.single_prompt(),
                numbering_prompt=dialog.numbering_prompt()))
        except (ProfileError, sqlite3.Error, OSError) as exc:
            QMessageBox.warning(
                self, self.i18n.text("Impostazioni non salvate"), str(exc))
            return
        self._refresh_model_combo()
        self._append_log(
            tx("Impostazioni LLM aggiornate (SQLite). Modello unico: {default_model}.",
               default_model=self._default_model()))

    def _project_option(self, key: str, default):
        """@brief Valore di un'opzione che segue il progetto (`PROJECT_OPTIONS`).

        @param key Nome dell'opzione.
        @param default Valore quando ne' il progetto ne' config.json la dicono.
        @return Il valore del progetto aperto se lo dichiara, altrimenti
            quello generale di config.json.
        @note Le opzioni di un progetto stanno in `_project_options`, NON in
            `self._config`: prima l'apertura di un progetto le scriveva in
            `self._config`, e il primo `save_config` per qualunque altro
            motivo le rendeva generali, cosi' dopo un riavvio i progetti nuovi
            partivano dall'ultimo progetto aperto (revisore 2, difetto 8).
        """
        options = getattr(self, "_project_options", None) or {}
        return options[key] if key in options else self._config.get(key, default)

    def _set_project_option(self, key: str, value: bool) -> None:
        """@brief Scelta dell'utente da Opzioni di generazione: vale per il progetto E come generale.

        @param key Nome dell'opzione (`PROJECT_OPTIONS`).
        @param value Valore scelto.
        @note Il chiamante salva config.json. Anche la fotografia generale da
            cui partono i progetti nuovi si aggiorna, cosi' in questa sessione
            e dopo un riavvio un progetto nuovo parte dallo stesso valore.
        """
        self._config[key] = bool(value)
        self._project_options = dict(getattr(self, "_project_options", None) or {})
        self._project_options[key] = bool(value)
        general = getattr(self, "_general_project_state", None)
        if isinstance(general, dict):
            general.setdefault("options", {})[key] = bool(value)

    def _include_nc_pins(self) -> bool:
        """@brief Dice se i pin NC fisicamente presenti vanno generati (predefinito si')."""
        return bool(self._project_option("include_nc_pins", True))

    def _inventory_pause_enabled(self) -> bool:
        """@brief Dice se dopo l'inventario a pagamento ci si ferma a rivederlo.

        @return True finche' qualcuno non ha chiesto esplicitamente di non
            fermarsi piu'.
        @note Predefinito ACCESO: la pausa e'
            cio' che l'utente ha chiesto («dopo l'elaborazione AI stessa cosa,
            non ho visto la pausa»), e un impianto che comincia a correre da
            solo verso l'abbinamento modelli e FreeCAD gli toglie l'unico
            momento in cui puo' correggere la disposizione dei corpi prima che
            qualcosa venga costruito su di essa. Chi non la vuole la spegne una
            volta, dalla pausa stessa, e la scelta resta in config.json.
        @note `is not False` e non `get(..., True)`: un valore scritto a mano
            che non sia un booleano (una stringa, un numero) non deve valere
            come «spenta» per caso.
        """
        return self._project_option('pause_after_inventory', True) is not False

    def _remember_inventory_pause(self, enabled: bool) -> None:
        """@brief Ricorda in config.json se la pausa dell'inventario va offerta.

        @param enabled False quando l'utente ha chiesto di non essere piu' fermato.
        @note Stesso deposito delle altre opzioni di generazione: la scelta
            vale per le corse successive, non solo per questa sessione.
        """
        self._set_project_option('pause_after_inventory', enabled)
        self._save_config_safely()

    def _photo_inventory_family(self) -> bool | None:
        """@brief Prevede se l'analisi passera' dall'inventario fotografico.

        @return True quando la tipologia dichiara una scheda POPOLATA (modulo
            castellated o aggregato a fori passanti), False quando dichiara UN
            SOLO componente discreto, None quando la tipologia non basta a
            deciderlo.
        @note Condizione dell'estrattore (`datasheet_extractor`, variabile
            `populated_board`: `layout == 'castellated' or component_family ==
            'aggregate_tht'`), qui ripetuta perche' e' l'unica cosa che la
            finestra sa PRIMA di spendere. Da essa dipendono a cascata il
            rilevatore locale dei corpi, la revisione «Corpi rilevati», i
            componenti della tabella e la pausa dopo l'inventario: per un
            componente singolo nulla di tutto cio' viene eseguito.
        @note I tre valori non sono un vezzo. La disposizione `castellated` la
            decide il MODELLO leggendo i documenti, e qui non e' ancora nota:
            con la tipologia «(non specificata)» o con una tipologia
            personalizzata l'inventario puo' benissimo partire, quindi si
            risponde None e non si annuncia nulla. Rispondere False in quel
            caso cancellerebbe dal log la dichiarazione della chiamata a
            pagamento sulla fotografia -- cioe' proprio l'informazione che non
            deve mai mancare.
        @note Il criterio non e' il NOME del componente ma l'attributo
            dichiarato dalla tipologia scelta: un componente discreto singolo
            non e' una scheda con corpi montati sopra, per costruzione, e
            questo vale per qualunque pezzo di quella famiglia.
        """
        # The project wizard's explicit choice takes precedence over a stale
        # component profile. Old projects can carry a profile from the prior
        # workflow even after being reopened as a single component.
        project_kind = getattr(self, "project_kind", None)
        if project_kind == _PROJECT_KIND_SINGLE:
            return False
        if project_kind == _PROJECT_KIND_BOARD:
            return True
        if self._is_castellated() or self._selected_family() == "aggregate_tht":
            return True
        if self._selected_family() in _SINGLE_COMPONENT_FAMILIES:
            return False
        return None

    def _background_work_running(self):
        if getattr(self, '_component_review_dialog', None) is not None:
            return True
        return any(worker is not None and worker.isRunning() for worker in
                   (self._worker, self._extract_worker, self._cad3d_worker, self._terminal_worker,
                    self._reconcile_worker))

    def showEvent(self, event):  # noqa: N802 - firma Qt
        """@brief Centre the window on its screen the first time it is shown.

        @param event Qt show event.
        @note Only the first time (user, 2026-09-27): later shows -- restore
            from minimised, a move by the user -- keep the position chosen.
            It is done here and not in __init__ because the frame (title bar
            and borders) is known only once the window exists on screen.
        """
        super().showEvent(event)
        if getattr(self, "_centred_once", False):
            return
        self._centred_once = True
        self._centre_on_screen()

    def centre_before_show(self) -> None:
        """@brief Centre the window on its screen before it is shown, so it appears in place.

        @note Centring in `showEvent` moved an already mapped window: the
            desktop's open animation played the window at its first
            position and then at the centre, which looked like a window
            closing and reopening (user, 2026-10-12, seen frame by frame).
            The frame geometry is known before the show on this platform
            (measured: same frame before and after), so the launcher
            centres first; `showEvent` then finds nothing to do. Should the
            frame not be known yet on some platform, `showEvent` still
            centres as before.
        """
        self._centre_on_screen()
        self._centred_once = True

    def _centre_on_screen(self) -> None:
        """Place the window frame in the middle of its screen's available area."""
        screen = self.screen()
        if screen is None:
            return
        area = screen.availableGeometry()
        frame = self.frameGeometry()
        frame.moveCenter(area.center())
        # Mai oltre l'angolo in alto a sinistra: una finestra piu' grande
        # dell'area resterebbe con la barra del titolo fuori schermo.
        self.move(max(frame.left(), area.left()), max(frame.top(), area.top()))

    def closeEvent(self, event):
        """@brief Refuse to close while any QThread worker is still running.

        @param event Qt close event; ignored while a worker runs, otherwise
            forwarded to the base implementation.
        @note A running QThread destroyed with its window aborts the process,
            so the guard covers every worker slot (extract, reconcile,
            generate, CAD 3D, terminals), not just one of them.
        @note Once the close is accepted the project log handle is closed and
            the 3D viewer is closed explicitly (VTK render window finalized);
            a refused close touches neither.
        """
        # Il worker terminali segnala l'attesa nella barra di stato 3D, senza
        # dialogo modale: comportamento su cui si appoggiano i test esistenti.
        if self._terminal_worker is not None and self._terminal_worker.isRunning():
            self._set_3d_status(self.i18n.text(
                "Attendi la fine dell'analisi terminali prima di chiudere."), STATUS_WARNING)
            event.ignore()
            return
        if self._background_work_running():
            QMessageBox.information(
                self, self.i18n.text('Operazione in corso'),
                self.i18n.text('Attendi la conclusione dell’operazione prima di chiudere.'))
            event.ignore()
        else:
            # Le ultime modifiche non aspettano il prossimo giro del salvataggio.
            if not self._can_leave_project():
                event.ignore()
                return
            self._window_closed = True
            self.ai_account.detach()
            # Una finestra chiusa davvero sta solo per essere distrutta, e
            # distruggendola Qt svuota elenchi, testi e schede: i figli
            # emettono rowsRemoved, modelReset, textChanged, currentChanged, e
            # gli slot Python collegati girerebbero su widget a meta'
            # distruzione. Segfault 139 intermittente all'uscita, 2026-09-25:
            # prima corretto solo per il cambio di scheda, poi riprodotto in
            # modo deterministico distruggendo il «Log analisi AI»
            # (`_refresh_ai_copy_buttons`). Si blocca l'intera classe: tutti i
            # segnali di tutti gli oggetti figli, non un collegamento alla volta.
            for child in self.findChildren(QObject):
                try:
                    child.blockSignals(True)
                except RuntimeError:
                    pass  # oggetto C++ gia' distrutto
            self._close_log_handle()
            # G9 (HANDOFF 130): pyvistaqt finalizza la render window VTK in
            # close(); lasciarla al distruttore della finestra e' una fonte nota
            # di crash nativi all'uscita su Windows. Solo a chiusura accettata.
            preview = getattr(self, "preview_3d", None)
            if preview is not None:
                try:
                    preview.close()
                except RuntimeError:
                    pass  # oggetto C++ gia' distrutto
            super().closeEvent(event)

    def _edit_profiles(self):
        dialog = None
        try:
            previous = self.profile_store.get_type(self._profile_key())
            dialog = ProfilesDialog(self.profile_store, self)
            self.i18n.retranslate_widgets(dialog)
            dialog.exec()
            if dialog.changed:
                current = self.profile_store.get_type(self._profile_key())
                changed_instructions = previous != current
                self._reload_profile_types(update_prompt=changed_instructions)
                self._append_log(tx("Tipologie e profili AI aggiornati nel database SQLite."))
        except (ProfileError, sqlite3.Error, OSError) as exc:
            QMessageBox.warning(self, self.i18n.text('Database profili'), str(exc))
        finally:
            if dialog is not None:
                dialog.deleteLater()

    def _reload_profile_types(self, *, update_prompt=True):
        combo = self.combo_component_type
        original_prompt = self.field_component_prompt.toPlainText()
        selected = combo.currentData() or ''
        rows = self.profile_store.list_types()
        blocked = combo.blockSignals(True)
        combo.clear()
        combo.addItem(self.i18n.component_type('', '(non specificata)'), '')
        for row in rows:
            combo.addItem(self.i18n.component_type(row['key'], row['label']), row['key'])
        index = combo.findData(selected)
        combo.setCurrentIndex(max(index, 0))
        combo.blockSignals(blocked)
        if selected and index < 0:
            self._append_log(tx("Tipologia eliminata o disattivata: selezionare una tipologia attiva."))
        self._on_component_type_changed()
        if not update_prompt and index >= 0:
            self.field_component_prompt.setPlainText(original_prompt)

    def _profile_key(self):
        return (self.combo_component_type.currentData() or '') if hasattr(self, 'combo_component_type') else ''

    def _profile_values(self):
        return self.profile_store.resolve(self._profile_key())

    def _selected_family(self):
        row = self.profile_store.get_type(self._profile_key())
        return row['family'] if row else ''

    @staticmethod
    def _project_kind_for_family(family: str) -> str | None:
        if family in _SINGLE_COMPONENT_FAMILIES:
            return _PROJECT_KIND_SINGLE
        if family in {"aggregate_tht", "aggregate_castellated"}:
            return _PROJECT_KIND_BOARD
        return None

    def _is_single_component_project(self) -> bool:
        return (getattr(self, "project_kind", None) == _PROJECT_KIND_SINGLE
                or self._selected_family() in _SINGLE_COMPONENT_FAMILIES)

    # ── Modello LLM ─────────────────────────────────────────────────

    def _default_model(self) -> str:
        """Resolved profile default; session selection remains separate."""
        return self._profile_values()['model']

    def _effective_model(self) -> str:
        """Modello da usare per la prossima estrazione.

        E' il modello di sessione, se ne e' stato scelto uno dal tab
        "Progetto" per questo componente; altrimenti il default globale.
        """
        return self._session_model or self._default_model()

    def _vision_model(self) -> str:
        """@brief Return the single AI model also used for photo analysis."""
        return self._effective_model()

    def _photo_detector_model(self) -> str:
        """@brief Modello che guarda la fotografia nella prossima estrazione.

        @return Identificatore del modello: la scelta di sessione se c'e',
            altrimenti quella configurata nel profilo.
        @note E' la chiamata a pagamento del percorso fotografico -- con 'vlm'
            rileva ed elenca i corpi, con 'yolo' riconcilia le proposte
            locali -- e finisce nel worker come `verification_model`.
        @note All'avvio segue il modello configurato per l'estrazione, non una
            costante scritta qui. Prima il valore iniziale era un modello
            fissato nel codice della finestra: chi apriva l'app trovava gia'
            scelto un modello che non aveva configurato, e la chiamata a
            pagamento sulla fotografia ci andava senza che nulla lo dicesse.
            Chi vuole un modello diverso, magari piu' economico, lo sceglie dal
            pannello e vale per la sessione.
        """
        return self._vlm_model or self._effective_model()

    def _verification_model(self) -> str:
        """@brief Return the single AI model also used for photo verification."""
        return self._effective_model()

    def _refresh_model_combo(self) -> None:
        """Riallinea la tendina del tab Progetto a default + scelta di sessione."""
        if not hasattr(self, "combo_model"):
            return
        blocked = self.combo_model.blockSignals(True)
        self.combo_model.clear()
        self.combo_model.addItem(
            self.i18n.tr("model.default", model=self._default_model()), None
        )
        if self._session_model:
            self.combo_model.addItem(
                self.i18n.tr("model.session", model=self._session_model), self._session_model
            )
            self.combo_model.setCurrentIndex(1)
        else:
            self.combo_model.setCurrentIndex(0)
        self.combo_model.blockSignals(blocked)
        # Ogni strada che cambia il modello passa di qui (ripristino del
        # progetto, finestra dei modelli, profilo): il saldo mostrato segue il
        # fornitore del modello in uso (utente, 27/09: restava OpenRouter dopo
        # aver riaperto un progetto con un modello DeepSeek).
        self._sync_balance_provider()

        if hasattr(self, "combo_photo_detector"):
            self._refresh_photo_detector_controls()

    def _photo_detector_text(self, key: str, **values: object) -> str:
        """Resolve local fallbacks through the existing translation mechanism."""
        translations = _PHOTO_DETECTOR_TRANSLATIONS[key]
        return self.i18n.tr(
            "photo_detector." + key,
            default=translations.get(self.i18n.language, translations["en"]),
            **values,
        )

    def _refresh_photo_detector_controls(self) -> None:
        """Translate session controls without changing selected data or paths."""
        self.label_photo_detector.setText(self._photo_detector_text("label"))
        for index, key in enumerate(("ai", "yolo", "vlm", "yolo_only", "yolo_smd")):
            self.combo_photo_detector.setItemText(index, self._photo_detector_text(key))
        smd_index = self.combo_photo_detector.findData("yolo_smd")
        if smd_index >= 0:
            smd_item = self.combo_photo_detector.model().item(smd_index)
            if is_frozen():
                reason = self._photo_detector_text("smd_unavailable")
                smd_item.setEnabled(False)
                self.combo_photo_detector.setItemData(
                    smd_index, reason, Qt.ItemDataRole.ToolTipRole)
            else:
                smd_item.setEnabled(True)
                self.combo_photo_detector.setItemData(
                    smd_index, None, Qt.ItemDataRole.ToolTipRole)
        self.combo_photo_detector.setToolTip(self._photo_detector_text("help"))
        self.label_yolo_model.setText(self._photo_detector_text("model"))
        self.label_smd_model.setText(self._photo_detector_text("smd_model"))
        smd_default = smd_weights_path()
        self.btn_pick_smd_model.setAccessibleName(self._photo_detector_text("smd_picker"))
        self.btn_pick_smd_model.setToolTip(
            self._photo_detector_text("browse") + "\n" + self._photo_detector_text("smd_picker"))
        self.field_smd_model.setPlaceholderText(
            self._photo_detector_text("default", path=smd_default))
        self.field_smd_model.setToolTip(self._photo_detector_text("smd_help", path=smd_default))
        self.label_yolo_params.setText(self._photo_detector_text("params"))
        self._refresh_yolo_summary()
        self.btn_yolo_settings.setAccessibleName(self._photo_detector_text("settings"))
        self.btn_yolo_settings.setToolTip(
            self._photo_detector_text("settings") + "\n" + self._photo_detector_text("settings_help"))
        # Icona, non testo (utente, 2026-09-24): nome «Sfoglia…» come prima riga
        # del tooltip, spiegazione sotto; il nome accessibile resta quello del
        # dialogo che apre.
        self.btn_pick_yolo_model.setAccessibleName(self._photo_detector_text("picker"))
        self.btn_pick_yolo_model.setToolTip(
            self._photo_detector_text("browse") + "\n" + self._photo_detector_text("picker"))
        default_path = self._default_yolo_weights(self.combo_photo_detector.currentData())
        self.field_yolo_model.setPlaceholderText(
            self._photo_detector_text("default", path=default_path))
        self.field_yolo_model.setToolTip(
            self._photo_detector_text("path_help", path=default_path))
        self.label_vlm_model.setText(self._photo_detector_text("vlm_model"))
        self.btn_pick_vlm_model.setAccessibleName(self._photo_detector_text("vlm_pick"))
        self.btn_pick_vlm_model.setToolTip(
            self._photo_detector_text("vlm_pick") + "\n" + self._photo_detector_text("vlm_help"))
        self.field_vlm_model.setToolTip(self._photo_detector_text("vlm_help"))
        self.field_vlm_model.setText(self._photo_detector_model())
        self._on_photo_detector_changed()

    def _on_photo_detector_changed(self) -> None:
        """@brief Mostra i pesi locali o il modello di visione, secondo il rilevatore.

        @note Una riga per volta: i pesi ONNX non c'entrano nulla quando a
            guardare la fotografia e' un modello remoto, e il contrario. La
            riga che resta e' quella su cui si puo' davvero intervenire.
        """
        single = self._is_single_component_project()
        detector = "ai" if single else self.combo_photo_detector.currentData()
        local = detector in ("yolo", "yolo_only", "yolo_smd") and not (
            detector == "yolo_smd" and is_frozen())
        smd = detector == "yolo_smd" and not is_frozen()
        default_path = self._default_yolo_weights(detector)
        self.field_yolo_model.setPlaceholderText(self._photo_detector_text("default", path=default_path))
        self.field_yolo_model.setToolTip(self._photo_detector_text("path_help", path=default_path))
        # Durante una corsa (audit 25/09, B6) non si riaccendono i comandi
        # YOLO e non si riscrive il pulsante: ci pensa la fine della corsa.
        busy = getattr(self, "_extraction_busy", False)
        if not busy:
            self._refresh_extract_button()
            self.field_yolo_model.setEnabled(local)
            self.btn_pick_yolo_model.setEnabled(local)
            self.field_smd_model.setEnabled(smd)
            self.btn_pick_smd_model.setEnabled(smd)
            self.btn_yolo_settings.setEnabled(local)
            # La riga della revisione dipende dal rilevatore: con «Solo YOLO
            # locale» riapre il pretest (vedi _refresh_reopen_review_button).
            if hasattr(self, "btn_component_review") and not self._background_work_running():
                self._refresh_reopen_review_button()
        if hasattr(self, "_models_form"):
            self._models_form.setRowVisible(self._row_photo_detector, not single)
            self._models_form.setRowVisible(self._row_yolo_model, local and not single)
            self._models_form.setRowVisible(self._row_smd_model, smd and not single)
            self._models_form.setRowVisible(self._row_yolo_params, local and not single)
            # Il modello di visione serve nei due percorsi fotografici, ma di
            # norma coincide con «Modello AI». Mostra la riga solo quando e'
            # stato scelto un override diverso, evitando di ripetere lo stesso
            # nome due volte. Con yolo_only nessun modello AI lavora.
            vision_override = bool(
                self._vlm_model and self._vlm_model != self._effective_model())
            self._models_form.setRowVisible(
                self._row_vlm_model,
                not single and detector in ("yolo", "vlm") and vision_override)
            self._models_form.setRowVisible(self._row_ai_model, detector != "yolo_only")
            # Anche il credito (utente, 2026-09-25): il pretest non spende nulla.
            if hasattr(self, "_row_ai_account"):
                self._models_form.setRowVisible(self._row_ai_account, detector != "yolo_only")

    def _open_yolo_settings(self) -> None:
        """@brief «Parametri YOLO»: edit the detector settings in a modal, then keep them.

        @note This modal is the only place where the settings are chosen
            (threshold included: user, 2026-09-25, «solo nella finestra»).
            Confirming keeps them in `_yolo_settings`, updates the summary
            and saves the project. Cancelling changes nothing. No detection
            starts from here.
        """
        from .yolo_settings_dialog import YoloSettingsDialog
        dialog = YoloSettingsDialog(self._chosen_yolo_settings(), self)
        self._yolo_settings_dialog = dialog
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        self._yolo_settings = values
        self._refresh_yolo_summary()
        self._append_log(tx("Parametri YOLO: soglia {confidence:g}; sovrapposizione per i "
                            "doppioni {nms_iou:g}; viste ingrandite {tiles}; sovrapposizione "
                            "fra tessere {overlap:g}.",
                            confidence=values["confidence"],
                            nms_iou=values["nms_iou"],
                            tiles=", ".join(f"{size} px" for size in values["tile_sizes"])
                            or tx("nessuna (solo foto intera)"),
                            overlap=values["tile_overlap"]))
        self._save_project_config()

    def _chosen_yolo_settings(self) -> dict:
        """@brief The detector settings as the user chose them.

        @return Complete dict (confidence, nms_iou, tile_sizes, tile_overlap):
            the defaults until the user changes them or a project restores them.
        """
        return validate_yolo_settings(getattr(self, "_yolo_settings", None) or {})

    def _refresh_yolo_summary(self) -> None:
        """@brief Show the chosen settings, read-only, in the «Parametri YOLO» row."""
        label = getattr(self, "label_yolo_summary", None)
        if label is None:
            return
        values = self._chosen_yolo_settings()
        tiles = (" + ".join(str(size) for size in values["tile_sizes"]) + " px"
                 if values["tile_sizes"] else self._photo_detector_text("no_tiles"))
        label.setText(self._photo_detector_text(
            "summary", confidence=values["confidence"], nms_iou=values["nms_iou"], tiles=tiles))
        label.setToolTip(self._photo_detector_text("settings_help"))

    def _pick_session_yolo_model(self) -> None:
        """@brief Choose the YOLO weights for this session in «Scegli il modello YOLO».

        @note The window lists the files of the models folder with their
            format, origin, license, training data and compatibility (user's
            request, 2026-09-26: information about the model, not a bare file
            dialog). Nothing is saved as a preference, as before.
        """
        from .yolo_model_dialog import YoloModelDialog
        default = str(self._default_yolo_weights(self.combo_photo_detector.currentData()))
        dialog = YoloModelDialog(self.field_yolo_model.text().strip(), resource_path(*YOLO_MODELS_DIR),
                                 default=default, tr=self.i18n.text, parent=self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        selected = dialog.selected_path()
        # Chiusa per davvero e rilasciata subito: tenuta in vita dalla finestra
        # avviava controlli del modello quando il garbage collector la
        # distruggeva, in un momento qualsiasi (HANDOFF 131).
        dialog.shutdown()
        dialog.deleteLater()
        if accepted and selected:
            # Il predefinito del rilevatore resta il campo vuoto, come per la
            # rete SMD: cambiando rilevatore cambia anche il predefinito.
            self.field_yolo_model.setText(
                "" if os.path.normcase(os.path.abspath(selected))
                == os.path.normcase(os.path.abspath(default)) else selected)

    @staticmethod
    def _default_yolo_weights(detector) -> Path:
        """@brief The YOLO weights used when the session field is empty.

        @param detector Photo detector key of the combo.
        @return KICADGEN_YOLO_MODEL when set, else the default of the
            detector: the PyTorch weights for 'yolo_smd' (the SMD route runs
            ultralytics), the ONNX file for the other local detectors.
        @note Before, the picker always proposed the ONNX file as «in uso»,
            also in 'yolo_smd', and OK replaced the weights actually used
            (adversarial review B2, 2026-10-02); 'yolo_smd' also ignored the
            environment variable (C-L3).
        """
        override = os.environ.get("KICADGEN_YOLO_MODEL", "").strip()
        if override:
            return Path(override)
        return yolo_weights_path(DEFAULT_YOLO_PT if detector == "yolo_smd" else DEFAULT_YOLO_ONNX)

    def _pick_session_vlm_model(self) -> None:
        """@brief Sceglie il modello che guarda la fotografia, solo per questa sessione.

        @note Chiede le capacita' del lavoro fotografico -- risposta strutturata
            e lettura di immagini -- non quelle dei datasheet: pretendere anche
            i PDF nasconderebbe i modelli di sola visione, che sono proprio
            quelli che si vogliono provare qui.
        """
        chosen = pick_model(self._photo_detector_model(), self,
                            title=self._photo_detector_text("vlm_model"),
                            capabilities=PHOTO_CAPABILITIES)
        if chosen:
            self._vlm_model = chosen
            self.field_vlm_model.setText(chosen)
            self._on_photo_detector_changed()
            self._append_log(tx("Modello di visione per questa sessione: {chosen}.", chosen=chosen))

    def _pick_session_smd_model(self) -> None:
        """@brief «Scegli la rete SMD»: the checkpoints of models/smd with what each declares.

        @note User request (2026-10-02): a window with the model's
            information, like «Scegli il modello YOLO», not a bare file
            dialog. The choice is checked again (validate_smd_setup) when the
            analysis starts, before any paid request.
        """
        from .smd_model_dialog import SmdModelDialog
        dialog = SmdModelDialog(self.field_smd_model.text().strip(),
                                resource_path(*SMD_MODELS_DIR),
                                default=str(smd_weights_path()), tr=self.i18n.text, parent=self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        selected = dialog.selected_path()
        dialog.shutdown()
        dialog.deleteLater()
        if accepted and selected:
            # Il predefinito resta «campo vuoto»: cosi' segue la cartella.
            self.field_smd_model.setText(
                "" if os.path.normcase(os.path.abspath(selected))
                == os.path.normcase(os.path.abspath(str(smd_weights_path()))) else selected)

    def _smd_model_option(self) -> Path | None:
        """@brief The SMD network file chosen for this session, or None for the default."""
        path = self.field_smd_model.text().strip()
        return Path(path) if path else None

    def _photo_detector_options(self) -> tuple[str, Path | None]:
        """Snapshot the selected backend; AI ignores any remembered ONNX path."""
        detector = self.combo_photo_detector.currentData() or "ai"
        path = self.field_yolo_model.text().strip()
        if detector == "yolo_smd" and not path:
            return detector, self._default_yolo_weights(detector)
        return detector, Path(path) if detector in ("yolo", "yolo_only", "yolo_smd") and path else None

    def _balance_provider(self) -> str:
        """@brief Provider of the model the next analysis will use (for the balance line)."""
        from ..services.providers import provider_of
        return provider_of(self._effective_model())

    def _sync_balance_provider(self) -> None:
        """@brief The balance line shows the credit of the provider of the model in use."""
        account = getattr(self, "ai_account", None)
        if account is not None:
            account.set_provider(self._balance_provider())

    def _on_model_combo_changed(self) -> None:
        self._session_model = self.combo_model.currentData()
        self._sync_balance_provider()
        if self._session_model:
            self._append_log(
                tx("Modello per questa sessione: {session_model} (il default globale non "
                   "cambia).",
                   session_model=self._session_model)
            )
        else:
            self._append_log(tx("Modello: default globale ({default_model}).",
                                default_model=self._default_model()))

    def _pick_session_model(self) -> None:
        """Sceglie un modello valido solo per le estrazioni di questa sessione.

        Serve per i datasheet difficili: si passa a un modello piu' capace
        (e piu' costoso) senza cambiare il default salvato nel profilo SQLite.
        """
        chosen = pick_model(
            self._effective_model(), self, title="Modello solo per questa sessione"
        )
        if not chosen:
            return
        if chosen == self._default_model():
            self._session_model = None
        else:
            self._session_model = chosen
        self._refresh_model_combo()
        self._on_model_combo_changed()

    def _open_config_file(self) -> None:
        if not config_path().is_file() and not self._save_config_safely():
            return
        webbrowser.open(config_path().as_uri())

    def _about_html(self) -> str:
        """@brief Testo della finestra About: descrizione e link a repository e sviluppatore."""
        description = "<br>".join(escape(line) for line in
                                  self.i18n.tr("about.description").split("\n"))
        from .. import __version__
        return (f"<p><b>KiCadGen {escape(__version__)}</b></p>"
                f"<p>{description}</p>"
                f"<p>{escape(self.i18n.text('Repository GitHub'))}: "
                f"<a href='{REPOSITORY_URL}'>{escape(REPOSITORY_URL)}</a><br>"
                f"{escape(self.i18n.text('Sviluppatore'))}: "
                f"<a href='{DEVELOPER_URL}'>{escape(DEVELOPER_NAME)}</a></p>")

    def _show_about(self) -> None:
        """@brief Finestra About con i link al repository GitHub e allo sviluppatore.

        @note I link si aprono nel browser (QMessageBox apre i link esterni
            della sua etichetta); per questo il testo e' rich text, con le
            parti tradotte escapate.
        """
        box = QMessageBox(self)
        box.setWindowTitle(self.i18n.text("Informazioni su KiCadGen"))
        box.setIcon(QMessageBox.Icon.Information)
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        box.setText(self._about_html())
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        self._about_box = box
        box.exec()

    def _license_texts(self) -> tuple[str, str]:
        """@brief Read the program license and the third-party notices.

        @return (LICENSE text, THIRD_PARTY_NOTICES.md markdown). A missing or
            unreadable file becomes a sentence saying so, never an exception:
            the dialog must open even in an incomplete installation.
        @note Both files ship next to the program (packaging/KiCadGen.spec)
            and are found through resource_path, as in the repository.
        """
        texts = []
        for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
            try:
                texts.append(resource_path(name).read_text(encoding="utf-8"))
            except OSError:
                texts.append(self.i18n.text("File non trovato:") + f" {name}")
        return texts[0], texts[1]

    #: Scorciatoie da tastiera (PIANO-FIX 7, 2026-09-26): una sola tabella da
    #: cui nascono i tasti E l'elenco del menu «?», cosi' non possono divergere.
    #: (tasto, descrizione italiana, nome del metodo). Nessuna scorciatoia avvia
    #: l'analisi AI: e' a pagamento e un tasto premuto per errore costa.
    SHORTCUTS = (
        ("Ctrl+N", "Nuovo progetto", "_new_project"),
        ("Ctrl+O", "Apri progetto", "_open_project"),
        ("F2", "Rinomina progetto", "_rename_project"),
        ("Ctrl+1", "Scheda Progetto", "_shortcut_tab_project"),
        ("Ctrl+2", "Scheda Anteprima footprint", "_shortcut_tab_preview"),
        ("Ctrl+3", "Scheda Simbolo elettrico", "_shortcut_tab_symbol"),
        ("Ctrl+4", "Scheda Modello 3D", "_shortcut_tab_model_3d"),
        ("Ctrl+5", "Scheda KiCad", "_shortcut_tab_kicad"),
        ("Ctrl+6", "Scheda Esportazioni", "_shortcut_tab_exports"),
        ("F5", "Rigenera il modello 3D (FreeCAD, gratuito)", "_shortcut_regenerate_3d"),
        ("Ctrl+E", "Modifica componenti", "_shortcut_edit_components"),
        ("Ctrl+L", "Apri il log AI", "_shortcut_ai_log"),
        ("Ctrl+Q", "Esci", "close"),
    )

    def _install_shortcuts(self) -> None:
        """@brief Bind every entry of SHORTCUTS to the window.

        @note Ctrl+N and Ctrl+O are also on their File menu actions (standard
            keys shown next to the entries); the window shortcuts here cover
            the others. Each handler checks that its command is available, so
            a key never does what the interface would not allow by mouse.
        """
        menu_keys = {"Ctrl+N", "Ctrl+O"}
        self._shortcuts = []
        for key, _label, method in self.SHORTCUTS:
            if key in menu_keys:
                continue
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(getattr(self, method))
            self._shortcuts.append(shortcut)
        self.action_rename_project.setShortcut(QKeySequence("F2"))
        self.action_exit.setShortcut(QKeySequence("Ctrl+Q"))

    def _shortcut_tab(self, index: int) -> None:
        if self.tabs.isEnabled() and self.tabs.isTabEnabled(index):
            self.tabs.setCurrentIndex(index)

    def _shortcut_tab_project(self) -> None:
        self._shortcut_tab(TAB_PROJECT)

    def _shortcut_tab_preview(self) -> None:
        self._shortcut_tab(TAB_PREVIEW)

    def _shortcut_tab_model_3d(self) -> None:
        self._shortcut_tab(TAB_MODEL_3D)

    def _shortcut_tab_symbol(self) -> None:
        self._shortcut_tab(TAB_SYMBOL)

    def _shortcut_tab_kicad(self) -> None:
        self._shortcut_tab(TAB_KICAD)

    def _shortcut_tab_exports(self) -> None:
        self._shortcut_tab(TAB_EXPORTS)

    def _click_if_available(self, button) -> None:
        """Press a command only when the interface offers it (visible and enabled)."""
        if button is not None and button.isEnabled() and button.isVisibleTo(self):
            button.click()

    def _shortcut_regenerate_3d(self) -> None:
        self._click_if_available(getattr(self, "btn_regen_3d", None))

    def _shortcut_edit_components(self) -> None:
        """@brief Ctrl+E: open the hand editor of the bodies, when there are any.

        The editor has no toolbar button any more (HANDOFF 152), so the
        shortcut opens it directly under the same condition the button had:
        bodies to edit. Without them there is nothing to open.
        """
        if getattr(self, "_photo_rows", None):
            self._edit_components()

    def _shortcut_ai_log(self) -> None:
        self._click_if_available(getattr(self, "btn_ai_log", None))

    def _show_shortcuts(self) -> None:
        """@brief List the keyboard shortcuts, from the same table that binds them."""
        rows = "".join(
            f"<tr><td style='padding-right:18px'><b>{escape(key)}</b></td>"
            f"<td>{escape(self.i18n.text(label))}</td></tr>"
            for key, label, _method in self.SHORTCUTS)
        box = QMessageBox(self)
        box.setWindowTitle(self.i18n.text("Scorciatoie da tastiera"))
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(f"<table>{rows}</table>")
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        self._shortcuts_box = box
        box.exec()

    def _open_user_guide(self) -> None:
        """@brief «Guida d'uso»: open the HTML guide shipped with the program."""
        guide = resource_path("kicadgen", "gui", "assets", "help", "guida.html")
        if not guide.is_file():
            QMessageBox.warning(self, self.i18n.text("Guida d'uso"),
                                tx("Guida d'uso non trovata: {path}", path=guide))
            return
        webbrowser.open(guide.as_uri())

    def _log_folder(self) -> Path:
        """@brief Folder whose logs describe the current work.

        @return The open project's folder (it holds `analisi-ai.log` and
            `freecad.log`); without a project, the program's log folder,
            created if needed, where crash reports are written.
        @throws OSError When the program's log folder cannot be created.
        """
        project = self._project_dir_or_none() if self._component_name() else None
        if project is not None and project.is_dir():
            return project
        folder = logs_dir()
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _open_log_folder(self) -> None:
        """@brief «Apri cartella dei log»: show the log folder in the file manager."""
        try:
            folder = self._log_folder()
        except OSError as exc:
            QMessageBox.warning(self, self.i18n.text("Apri cartella dei log"), str(exc))
            return
        webbrowser.open(folder.as_uri())

    def _report_problem(self) -> None:
        """@brief «Segnala un problema…»: prefilled GitHub issue plus the log folder.

        @note Nothing is sent from here: the browser opens a new-issue page
            filled with versions only (no user name, machine name or path,
            see `issue_report`), and the user attaches the logs after checking
            them. The latest crash report is named so it is easy to find.
        """
        from .. import crash_report
        from ..services.issue_report import build_issue_url
        message = self.i18n.text(
            "Si apre la pagina di GitHub per una nuova segnalazione, già compilata con la "
            "versione del programma e del sistema. Si apre anche la cartella dei log: allega "
            "i file utili dopo averli controllati, perché possono contenere i nomi delle tue "
            "cartelle. La segnalazione è pubblica e parte solo quando la invii.")
        latest = crash_report.latest_reports(limit=1, base=logs_dir())
        if latest:
            message += "\n\n" + tx("Ultimo rapporto di crash: {path}", path=latest[0])
        answer = QMessageBox.information(
            self, self.i18n.text("Segnala un problema"), message,
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Ok:
            return
        self._open_log_folder()
        # Con un progetto aperto la cartella dei log e' la sua: i rapporti di
        # crash stanno altrove e si aprono a parte.
        try:
            folder = self._log_folder()
        except OSError:
            folder = None
        if latest and (folder is None or not latest[0].is_relative_to(folder)):
            webbrowser.open(latest[0].parent.as_uri())
        webbrowser.open(build_issue_url())

    def _check_for_updates(self) -> None:
        """@brief «Controlla aggiornamenti…»: ask GitHub, only when the user asks.

        @note The request can take a few seconds (timeout 5 s): it runs in
            `UpdateCheckWorker`, the window stays responsive and the command
            is disabled until the answer arrives.
        """
        if getattr(self, "_update_worker", None) is not None:
            return
        self.action_check_updates.setEnabled(False)
        self.statusBar().showMessage(self.i18n.text("Controlla aggiornamenti") + "\u2026")
        worker = UpdateCheckWorker()
        worker.done.connect(self._on_update_checked)
        self._update_worker = worker
        worker.start()

    def _on_update_checked(self, result) -> None:
        """@brief Show the outcome of an update check.

        @param result `update_check.UpdateCheckResult`.
        """
        worker, self._update_worker = self._update_worker, None
        if worker is not None:
            worker.wait()
        if getattr(self, "_window_closed", False):
            # The worker outlives the window (it belongs to QApplication):
            # an answer arriving after the close has nowhere to be shown.
            return
        self.action_check_updates.setEnabled(True)
        if not self.analysis_overlay.isVisible():
            self.statusBar().showMessage(self.i18n.text("Pronto"))
        title = self.i18n.text("Controlla aggiornamenti")
        if result.update_available:
            answer = QMessageBox.question(
                self, title, tx("È disponibile KiCadGen {latest} (questa è la {current}). "
                                "Aprire la pagina della versione?",
                                latest=result.latest_version, current=result.current_version))
            if answer == QMessageBox.StandardButton.Yes:
                webbrowser.open(result.release_url)
            return
        if result.ok:
            QMessageBox.information(self, title, tx("Hai già la versione più recente ({current}).",
                                                    current=result.current_version))
        elif result.error_kind == "no_release":
            QMessageBox.information(self, title, self.i18n.text(
                "Su GitHub non c'è ancora nessuna versione pubblicata."))
        elif result.error_kind in ("timeout", "network"):
            QMessageBox.warning(self, title, tx(
                "Impossibile contattare GitHub: controlla la connessione e riprova. ({error})",
                error=result.error))
        else:
            QMessageBox.warning(self, title, tx("Risposta di GitHub non utilizzabile ({error}).",
                                                error=result.error))

    def _show_licenses(self) -> None:
        """@brief Show the program license and the third-party notices, read-only.

        @note Two tabs: the full AGPL-3.0 text, and the notices for model
            weights, dataset and dependencies. The notices are the English
            file of the repository, shown as it is: it is a legal record and
            is not translated.
        @note The Close button text comes from the catalogue (Qt would show
            its own English «Close»), and the dialog is released with
            deleteLater once closed: each opening builds a new one.
        """
        from PyQt6.QtWidgets import QDialogButtonBox, QTextBrowser
        license_text, notices = self._license_texts()
        dialog = QDialog(self)
        dialog.setWindowTitle(self.i18n.text("Licenze"))
        dialog.resize(820, 640)
        layout = QVBoxLayout(dialog)
        summary = QLabel(self.i18n.text(
            "KiCadGen è software libero: codice e documentazione sono distribuiti con la "
            "GNU Affero General Public License v3.0. Pesi dei modelli YOLO, dataset e "
            "dipendenze hanno licenze proprie, elencate nella seconda scheda."))
        summary.setWordWrap(True)
        layout.addWidget(summary)
        tabs = QTabWidget(dialog)
        program = QTextBrowser(tabs)
        program.setPlainText(license_text)
        third_party = QTextBrowser(tabs)
        third_party.setOpenExternalLinks(True)
        third_party.setMarkdown(notices)
        tabs.addTab(program, self.i18n.text("Licenza del programma (AGPL-3.0)"))
        tabs.addTab(third_party, self.i18n.text("Componenti di terze parti"))
        layout.addWidget(tabs, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dialog)
        close = buttons.button(QDialogButtonBox.StandardButton.Close)
        close.setText(self.i18n.text("Chiudi"))
        close.setToolTip(self.i18n.text("Chiudi"))
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        self._licenses_dialog = dialog
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def _warn_missing_external_applications(self) -> bool:
        """@brief Warn once when KiCad or FreeCAD is not installed.

        @return True when the warning was displayed, otherwise False.
        """
        if self._startup_dependencies_checked:
            return False
        self._startup_dependencies_checked = True

        applications = detect_external_applications()
        if not applications.missing:
            return False

        def status(name: str, path: str | None) -> str:
            if path:
                return self.i18n.tr(
                    "dependencies.detected", application=name, path=path)
            return self.i18n.tr(
                "dependencies.not_detected", application=name)

        statuses = "\n".join((
            status("KiCad", applications.kicad_cli),
            status("FreeCAD", applications.freecadcmd),
        ))
        message = QMessageBox(self)
        message.setIcon(QMessageBox.Icon.Warning)
        message.setWindowTitle(self.i18n.tr("dependencies.title"))
        message.setText(self.i18n.tr(
            "dependencies.missing", applications=", ".join(applications.missing)))
        message.setInformativeText(self.i18n.tr(
            "dependencies.explanation", statuses=statuses))
        message.setTextFormat(Qt.TextFormat.PlainText)
        message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        continue_button = message.addButton(
            self.i18n.text("Continua"), QMessageBox.ButtonRole.AcceptRole)
        message.setDefaultButton(continue_button)
        message.exec()
        return True

    # ── Layout ──────────────────────────────────────────────────────

    def _build_central(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # I parametri esistono prima di ogni scheda: le schede li leggono.
        self._init_parameters()
        project_tab = self._build_project_tab()
        preview_tab = self._build_preview_tab()
        symbol_tab = self._build_symbol_tab()
        model_3d_tab = self._build_3d_tab()
        kicad_tab = self._build_kicad_tab()
        exports_tab = self._build_exports_tab()

        language_row = QHBoxLayout()
        language_row.setSpacing(10)
        # Stesso margine laterale del contenuto delle schede (10 px): il
        # layout centrale non ne ha, e la tendina stava incollata al bordo
        # destro della finestra (utente, 2026-09-24).
        language_row.setContentsMargins(0, 6, CONTENT_MARGIN_PX, 6)
        # Barra dei passi del flusso (PIANO-FIX 4): a sinistra della lingua,
        # sulla stessa riga, per non togliere altezza alle schede.
        self.workflow_bar = WorkflowBar(self.i18n.text)
        self.workflow_bar.step_clicked.connect(self._on_workflow_step_clicked)
        language_row.addWidget(self.workflow_bar)
        language_row.addStretch()
        # Un globo al posto della scritta «Lingua:» (utente, 2026-09-24), come
        # le altre icone: il nome sta nel tooltip e nel nome accessibile della
        # tendina, e si ritraduce con la lingua (`_refresh_language_texts`).
        self.language_label = QLabel()
        self.language_label.setPixmap(self._asset_icon("language.svg").pixmap(QSize(20, 20)))
        self.language_label.setProperty("i18n_skip", True)
        language_row.addWidget(self.language_label)
        self.language_combo = QComboBox()
        for code, label in LANGUAGES.items():
            self.language_combo.addItem(label, code)
        self.language_combo.setCurrentIndex(max(0, self.language_combo.findData(self.i18n.language)))
        self.language_combo.setProperty("i18n_skip", True)
        self.language_label.setBuddy(self.language_combo)
        self._refresh_language_texts()
        self.language_combo.setMinimumWidth(120)
        self.language_combo.currentIndexChanged.connect(self._on_language_changed)
        language_row.addWidget(self.language_combo)
        layout.addLayout(language_row)

        self.tabs = QTabWidget()
        # Navigation tabs use stable translation keys and are refreshed explicitly.
        self.tabs.setProperty("i18n_skip_tabs", True)
        self.tabs.addTab(project_tab, self.i18n.tr("tab.project.name"))
        self.tabs.addTab(preview_tab, self.i18n.tr("tab.preview.name"))
        self.tabs.addTab(symbol_tab, self.i18n.tr("tab.symbol.name"))
        self.tabs.addTab(model_3d_tab, self.i18n.tr("tab.model3d.name"))
        self.tabs.addTab(kicad_tab, self.i18n.tr("tab.kicad.name"))
        self.tabs.addTab(exports_tab, self.i18n.tr("tab.exports.name"))
        # Icone a linea nello stile delle barre, al posto delle emoji a colori
        # che stavano nel titolo (utente, 2026-09-24: «stonata con il resto»).
        for index, asset in enumerate(TAB_ICONS):
            self.tabs.setTabIcon(index, self._asset_icon(asset))
        self._set_acquisition_tabs_enabled(False)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        # Pagina iniziale (PIANO-FIX 1): al posto delle schede finche' non c'e'
        # un progetto aperto (`_refresh_start_page`). Le schede senza progetto
        # erano riquadri vuoti e un pulsante inutilizzabile.
        self.start_page = StartPage(tr=self.i18n.text, type_label=self.i18n.component_type)
        self.start_page.new_requested.connect(self._new_project)
        self.start_page.open_requested.connect(self._open_project)
        self.start_page.open_recent_requested.connect(self._open_recent_project)
        self.start_page.remove_recent_requested.connect(self._forget_recent_project)
        self.start_page.choose_root_requested.connect(self._browse_library_root)
        self.start_page.set_library_root(self._library_root())
        self.field_library_root.textChanged.connect(
            lambda _text: self.start_page.set_library_root(self._library_root()))
        # «Apri progetto…» solo se c'e' qualcosa da aprire (HANDOFF 170): la
        # cartella di lavoro decide, e viene riletta a ogni suo cambio.
        self.field_library_root.textChanged.connect(
            lambda _text: self._refresh_open_project_commands())
        self._refresh_open_project_commands()
        self.central_stack = QStackedWidget()
        self.central_stack.addWidget(self.start_page)
        self.central_stack.addWidget(self.tabs)
        layout.addWidget(self.central_stack)

        self.setCentralWidget(central)
        self.analysis_overlay = AnalysisOverlay(central, i18n=self.i18n)
        self.analysis_overlay.log_requested.connect(self.ai_log_dialog.exec)
        self.analysis_overlay.abort_requested.connect(self._request_abort)
        self.analysis_overlay.closed.connect(self._on_completed_overlay_closed)
        # Barra di stato: progetto aperto. Il costo dell'analisi compare nella
        # modale AI, accanto allo stato del lavoro.
        self.status_project = QLabel()
        self.status_project.setProperty("i18n_skip", True)
        # Solo il testo del costo, che la modale AI copia (set_cost): non sta in
        # nessuna barra e non si mostra mai. Un QLabel senza genitore reso
        # visibile diventava una finestrella «python» a se' (utente,
        # 2026-10-02): ha la finestra come genitore e resta nascosto.
        self.status_cost = QLabel(self)
        self.status_cost.setProperty("i18n_skip", True)
        self.status_cost.hide()
        self.statusBar().addPermanentWidget(self.status_project)
        self.statusBar().showMessage(self.i18n.text("Pronto"))
        self.analysis_overlay.operation_started.connect(
            lambda title: self.statusBar().showMessage(title + "\u2026"))
        self.analysis_overlay.operation_finished.connect(
            lambda: self.statusBar().showMessage(self.i18n.text("Pronto")))
        self._analysis_tabs_enabled = None
        # Apply the persisted language to all catalogued widgets on first render.
        self.i18n.retranslate_widgets(self)
        self._realign_form_labels()
        if hasattr(self, "project_wizard"):
            self.project_wizard.retranslate()

    def _align_form(self, form: QFormLayout, panel: str) -> None:
        """@brief Align a form's labels now and after every language change, with its panel's forms.

        @param form Form layout built by the window; kept in `_aligned_forms`.
        @param panel Key of the tab the form is shown in: every form of a
            panel gets the same label column (the widest label among them),
            so their fields line up (review, 2026-10-12: a fixed 132 px
            aligned them but cut long labels; measured columns alone made
            adjacent group boxes differ by up to 60 px).
        """
        if not hasattr(self, "_aligned_forms"):
            self._aligned_forms = []
        self._aligned_forms.append((panel, form))
        self._realign_form_labels()

    def _realign_form_labels(self) -> None:
        """@brief Recompute the label columns, per panel: translated labels have other widths."""
        widths: dict = {}
        for panel, form in getattr(self, "_aligned_forms", ()):
            widths[panel] = max(widths.get(panel, FORM_LABEL_MINIMUM_WIDTH), _align_form_labels(form))
        for panel, form in getattr(self, "_aligned_forms", ()):
            _align_form_labels(form, widths[panel])

    def _refresh_language_texts(self) -> None:
        """@brief Nome dell'icona e della tendina della lingua, nella lingua in uso."""
        name = self.i18n.tr("language.name")
        self.language_label.setToolTip(name)
        self.language_label.setAccessibleName(name)
        self.language_combo.setToolTip(name)
        self.language_combo.setAccessibleName(name)

    def _on_language_changed(self, index: int) -> None:
        """@brief Persist the selected language and refresh navigation labels."""
        language = self.language_combo.itemData(index)
        if not language or language == self.i18n.language:
            return
        self.i18n.set_language(language)
        from .qt_translations import install_qt_translations
        install_qt_translations(language)
        self._config["language"] = language
        self._save_config_safely()
        self._refresh_language_texts()
        self.tabs.setTabText(TAB_PROJECT, self.i18n.tr("tab.project.name"))
        self.tabs.setTabText(TAB_PREVIEW, self.i18n.tr("tab.preview.name"))
        self.tabs.setTabText(TAB_SYMBOL, self.i18n.tr("tab.symbol.name"))
        self.symbol_tab.retranslate()
        self.tabs.setTabText(TAB_MODEL_3D, self.i18n.tr("tab.model3d.name"))
        self.tabs.setTabText(TAB_KICAD, self.i18n.tr("tab.kicad.name"))
        self.tabs.setTabText(TAB_EXPORTS, self.i18n.tr("tab.exports.name"))
        self.i18n.retranslate_widgets(self)
        self._realign_form_labels()
        self._refresh_metadata_help_tooltips()
        self.project_wizard.retranslate()
        self._refresh_ai_tools_summary()
        if hasattr(self, "workflow_bar"):
            self.workflow_bar.retranslate()
            self._refresh_workflow_state()
            if not self.analysis_overlay.isVisible():
                self.statusBar().showMessage(self.i18n.text("Pronto"))
        self.ai_account.refresh()
        self.menu_file.setTitle(self.i18n.tr("app.file"))
        self.menu_config.setTitle(self.i18n.tr("app.configuration"))
        self.menu_tools.setTitle(self.i18n.tr("app.tools"))
        self.menu_help.setTitle(self.i18n.tr("app.help"))
        self.action_open_output.setText(self.i18n.text("Apri cartella componente"))
        self.action_test_kicad.setText(self.i18n.text("Prova in KiCad..."))
        self.action_export.setText(self.i18n.text("Installa nel progetto KiCad..."))
        self.action_install_global.setText(self.i18n.text("Installa come libreria globale..."))
        self.action_open_tool_dir.setText(self.i18n.text("Apri cartella progetto tool"))
        self.action_open_config.setText(self.i18n.text("Apri config.json"))
        self.action_exit.setText(self.i18n.text("Esci"))
        # Voci del menu File aggiunte il 2026-09-24: prima restavano nella
        # lingua dell'avvio (verifica «testi cablati» dell'utente).
        self.action_new_project.setText(self.i18n.text("Nuovo progetto..."))
        self.action_open_project.setText(self.i18n.text("Apri progetto..."))
        self.action_rename_project.setText(self.i18n.text("Rinomina progetto..."))
        self.menu_recent.setTitle(self.i18n.text("Progetti recenti"))
        self.start_page.refresh()
        self.action_profiles.setText(self.i18n.text("Gestione tipologie e profili AI..."))
        self.action_defaults.setText(self.i18n.text("Default generali AI..."))
        self.action_ai_account.setText(self.i18n.text("Account AI..."))
        self.action_ai_account.setToolTip(self.i18n.text(
            "Chiave OpenRouter, credito e limiti: incolla, verifica e salva la chiave nel "
            "Gestore credenziali di Windows."))
        self.action_generation_options.setText(self.i18n.text("Opzioni di generazione..."))
        self.action_accepted_models.setText(
            self.i18n.text("Archivio abbinamenti accettati..."))
        self.action_licenses.setText(self.i18n.text("Licenze"))
        self.action_about.setText(self.i18n.text("Informazioni su KiCadGen"))
        if hasattr(self, "action_shortcuts"):
            self.action_shortcuts.setText(self.i18n.text("Scorciatoie da tastiera..."))
        self.action_user_guide.setText(self.i18n.text("Guida d'uso"))
        self.action_open_logs.setText(self.i18n.text("Apri cartella dei log"))
        self.action_report_problem.setText(self.i18n.text("Segnala un problema..."))
        self.action_check_updates.setText(self.i18n.text("Controlla aggiornamenti..."))
        self._retranslate_component_types()
        self._refresh_model_combo()
        self._refresh_output_actions()
        self._apply_view_cube_labels()
        # Testi che la ripassata generica non copre: voci dei selettori del
        # tipo di contatto, tooltip composti delle icone della barra 3D e
        # quote disegnate nell'anteprima 2D (nota 8 dell'utente).
        self._retranslate_contact_style_combos()
        # Etichetta della provenienza dello spessore: testo composto, escluso
        # dalla ripassata generica (i18n_skip).
        self._refresh_pcb_thickness_row()
        self._refresh_workflow_tooltips()
        # Full-paths toggle of the KiCad tab: state-dependent text, i18n_skip.
        self.file_paths_toggle.setText(
            self._file_paths_toggle_text(self.file_paths_toggle.isChecked()))
        # Icone della modale di avanzamento: tooltip composti, non chiavi.
        self.analysis_overlay.refresh_command_texts()
        self._draw_preview()

    def _retranslate_component_types(self) -> None:
        """@brief Refresh built-in component type labels without changing selection."""
        if not hasattr(self, "combo_component_type"):
            return
        combo = self.combo_component_type
        blocked = combo.blockSignals(True)
        for index in range(combo.count()):
            key = combo.itemData(index) or ""
            row = self.profile_store.get_type(key) if key else None
            fallback = row["label"] if row else "(non specificata)"
            combo.setItemText(index, self.i18n.component_type(key, fallback))
        combo.blockSignals(blocked)

    def _set_acquisition_tabs_enabled(self, enabled: bool) -> None:
        """@brief Enable previews only after the first successful parameter acquisition.

        @note When the current tab is one being switched off, go to «Progetto»
            explicitly: Qt would otherwise land on the next enabled tab, which
            since «Esportazioni» exists is the history, not the project.
        """
        indices = (TAB_PREVIEW, TAB_SYMBOL, TAB_MODEL_3D)
        if not enabled and self.tabs.currentIndex() in indices:
            self.tabs.setCurrentIndex(TAB_PROJECT)
        for index in indices:
            self.tabs.setTabEnabled(index, enabled)

    def _refresh_preview_tabs(self, footprint_available: bool) -> None:
        """@brief Open «Footprint preview» and «3D model» when there is something to show.

        @param footprint_available True when the current project folder holds
            its generated footprint.
        @note Before, only an extraction of THIS session opened them: reopening
            a finished project left both tabs off (user, 2026-09-25). There is
            something to preview when the project was extracted in this
            session or when its footprint was generated -- the parameters that
            produced it are restored with the project. A project with neither
            keeps them off: they would show only default values.
        """
        self._set_acquisition_tabs_enabled(
            bool(getattr(self, "_acquisition_completed", False)) or footprint_available)

    def _restore_saved_mesh(self) -> None:
        """@brief Point the 3D tab at the model saved in the project folder, if any.

        @note Called after the project state is applied: applying fields clears
            the mesh on purpose (`_invalidate_generated_mesh`). The file on disk
            is the model KiCad would use for this project, so it is what the
            tab shows until the user regenerates; it is rendered on entering
            the tab (`_draw_preview_3d`), not while the project opens.
        """
        name = self._component_name()
        if not name:
            return
        shapes = self._project_dir() / f"{name}.3dshapes"
        mesh = shapes / f"{name}.preview.json"
        if (shapes / f"{name}.step").is_file() and mesh.is_file():
            self._last_mesh_path = mesh

    def _restore_saved_photo(self) -> None:
        """@brief Reload the calibrated photo of a reopened project from its review bundle.

        @note The photo lived only in memory after an extraction: reopening a
            project left it None, and «Modifica la revisione», the double click
            on a body and «Cambia modello KiCad...» had nothing to open (user,
            2026-09-26). The extraction's bundle keeps the calibrated photo the
            bodies are measured on, the same frame the photo review uses. A
            pretest bundle is not the extraction and is not used; a missing or
            unreadable file leaves things as they were.
        """
        if self._photo_png is not None:
            return
        bundle = self._review_bundle()
        path = getattr(bundle, "photo_path", None)
        if path is None:
            return
        try:
            self._photo_png = Path(path).read_bytes()
        except OSError as exc:
            self._append_log(tx("Foto calibrata del progetto non leggibile: {exc}", exc=exc))
            return
        self._photo_body_fields = self._body_fields_matching(bundle.body_length_mm,
                                                             bundle.body_width_mm)
        self._photo_source = Path(path).name
        self._refresh_component_review_button()

    # ── Tab 1: Progetto ────────────────────────────────────────────

    def _build_project_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(*(CONTENT_MARGIN_PX,) * 4)
        outer.setSpacing(8)

        # Barra di icone in alto (utente, 2026-09-24), riempita a fine
        # costruzione della scheda, quando i comandi esistono.
        self.project_action_bar = QHBoxLayout()
        self.project_action_bar.setContentsMargins(0, 0, 0, 0)
        self.project_action_bar.setSpacing(4)
        outer.addLayout(self.project_action_bar)

        folder_group = QGroupBox("Cartella componente")
        folder_layout = QFormLayout(folder_group)

        # Vuoto all'avvio, per scelta: un nome scritto qui dal programma
        # diventa il nome della cartella, del footprint, del simbolo e del
        # modello 3D. Un esempio lasciato nel campo si ritrova stampato su
        # tutto cio' che viene generato, e chi lavora non se ne accorge finche'
        # non apre KiCad. Finche' il campo e' vuoto non si caricano documenti e
        # non si estrae nulla: vedi `_apply_component_name_gate`.
        self.field_name = QLineEdit()
        # Sola lettura (utente, 2026-09-24): il nome si sceglie con «Nuovo
        # progetto…», «Apri progetto…» o «Rinomina progetto…» del menu File.
        # Scritto qui a mano, il nome di una cartella esistente la apriva di
        # nascosto e l'analisi scriveva nel progetto di qualcun altro.
        self.field_name.setReadOnly(True)
        self.field_name.setPlaceholderText(
            "Crea o apri un progetto dal menu File: il suo nome e' quello di cartella, "
            "footprint e modello 3D")
        self.field_name.setToolTip(
            "Si cambia dal menu File: «Nuovo progetto…», «Apri progetto…», «Rinomina progetto…».")
        self.field_name.textChanged.connect(self._on_name_changed)
        folder_layout.addRow("Nome componente:", self.field_name)

        # Niente piu' riga «L'analisi propone: … / Usa questo nome» (utente,
        # 2026-09-24): il nome si cambia solo da File > Rinomina progetto, e
        # il nome proposto dall'analisi resta scritto nel Log AI
        # (`_log_proposed_name`).

        self.field_library_root = QLineEdit(str(DEFAULT_LIBRARY_ROOT))
        self.field_library_root.textChanged.connect(self._update_project_path_label)
        btn_browse_root = QPushButton()
        btn_browse_root.setObjectName("browseRootButton")
        btn_browse_root.setIcon(QIcon(str(Path(__file__).resolve().parent / "assets" / "docs-open.svg")))
        btn_browse_root.setIconSize(QSize(20, 20))
        btn_browse_root.setFixedSize(36, 32)
        btn_browse_root.setToolTip("Scegli la cartella di lavoro")
        btn_browse_root.setAccessibleName("Scegli la cartella di lavoro")
        btn_browse_root.setStyleSheet(
            "QPushButton { background: #313244; border: 1px solid #45475a;"
            " border-radius: 4px; padding: 4px; }"
            "QPushButton:hover { background: #45475a; border-color: #89b4fa; }"
            "QPushButton:pressed { background: #585b70; }"
        )
        btn_browse_root.clicked.connect(self._browse_library_root)
        root_row = QHBoxLayout()
        root_row.setContentsMargins(0, 0, 0, 0)
        root_row.setSpacing(6)
        root_row.addWidget(self.field_library_root)
        root_row.addWidget(btn_browse_root)
        root_row_widget = QWidget()
        root_row_widget.setLayout(root_row)
        folder_layout.addRow("Area di lavoro:", root_row_widget)

        self.label_project_path = QLabel()
        self.label_project_path.setWordWrap(True)
        self.label_project_path.setStyleSheet(f"color: {BLUE};")
        path_row = QHBoxLayout()
        path_row.addWidget(self.label_project_path, 1)
        self.btn_project_info = QPushButton()
        self.btn_project_info.setIcon(QIcon(str(Path(__file__).resolve().parent / "assets" / "info.svg")))
        self.btn_project_info.setIconSize(QSize(22, 22))
        self.btn_project_info.setStyleSheet("QPushButton { padding: 4px; }")
        self.btn_project_info.setToolTip("Informazioni sulla cartella componente e sull'esportazione")
        self.btn_project_info.setAccessibleName("Informazioni sulla cartella componente")
        self.btn_project_info.setFixedSize(36, 32)
        path_row.addWidget(self.btn_project_info)
        folder_layout.addRow("Cartella componente:", path_row)
        self._align_form(folder_layout, "project")

        self.project_info_dialog = QDialog(self)
        self.project_info_dialog.setWindowTitle("Informazioni sulla cartella componente")
        self.project_info_dialog.setModal(True)
        info_layout = QVBoxLayout(self.project_info_dialog)

        note = QLabel(
            "Il nome del componente e' anche il nome del footprint generato "
            "(es. area_di_lavoro/MioComponente/MioComponente.pretty/...). "
            "Tutto viene generato qui; per provarlo in KiCad o installarlo come "
            "libreria usa il tab \"KiCad\"."
        )
        note.setWordWrap(True)
        info_layout.addWidget(note)
        info_close = QPushButton("Chiudi")
        info_close.clicked.connect(self.project_info_dialog.accept)
        info_layout.addWidget(info_close, alignment=Qt.AlignmentFlag.AlignRight)
        self.project_info_dialog.resize(540, 160)
        self.btn_project_info.clicked.connect(self.project_info_dialog.exec)

        outer.addWidget(folder_group)

        docs_group = QGroupBox(f"Documentazione ({DOCS_SUBDIR}/)")
        docs_layout = QVBoxLayout(docs_group)

        # Keep the old document index for compatibility, never as an AI selection UI.
        self.docs_list = QListWidget(docs_group)
        self.docs_list.hide()
        self.guided_docs = GuidedDocuments(self._docs_dir, docs_group, i18n=self.i18n,
                                           config=self._config, body_size=self._body_size)
        self.guided_docs.changed.connect(self._refresh_docs_list)
        docs_layout.addWidget(self.guided_docs)
        outer.addWidget(docs_group)
        self.group_component_analysis = QGroupBox("Analisi componente")
        analysis_layout = QVBoxLayout(self.group_component_analysis)

        ai_form = QFormLayout()

        self.combo_component_type = QComboBox()
        self.combo_component_type.addItem(self.i18n.component_type("", "(non specificata)"), "")
        for component_type in self.profile_store.list_types():
            self.combo_component_type.addItem(
                self.i18n.component_type(component_type['key'], component_type['label']),
                component_type['key'])
        self.combo_component_type.setToolTip(
            "Dice al modello che cosa sta leggendo: da questo dipende quali quote "
            "cercare nel datasheet (fori e drill per un THT, piazzole consigliate per un "
            "SMD, semi-fori sul bordo per un modulo castellated)."
        )
        self.combo_component_type.currentIndexChanged.connect(
            self._on_component_type_changed
        )
        ai_form.addRow("Tipologia componente:", self.combo_component_type)
        self.group_single_tht_pads = QGroupBox("Piazzole del componente THT")
        single_tht_layout = QHBoxLayout(self.group_single_tht_pads)
        single_tht_layout.setContentsMargins(10, 6, 10, 6)
        self.spin_single_tht_pad = QDoubleSpinBox()
        self.spin_single_tht_drill = QDoubleSpinBox()
        for spin in (self.spin_single_tht_pad, self.spin_single_tht_drill):
            spin.setRange(0.01, MAX_LENGTH_MM)
            spin.setDecimals(2)
            spin.setSingleStep(0.1)
            spin.setSuffix(" mm")
            spin.setKeyboardTracking(False)
        self.spin_single_tht_pad.setAccessibleName("Diametro piazzola componente THT")
        self.spin_single_tht_drill.setAccessibleName("Diametro foro componente THT")
        self.spin_single_tht_pad.valueChanged.connect(
            lambda value: self._set_single_tht_footprint_value("field_pad", value))
        self.spin_single_tht_drill.valueChanged.connect(
            lambda value: self._set_single_tht_footprint_value("field_drill", value))
        single_tht_layout.addWidget(QLabel("Pad Ø:"))
        single_tht_layout.addWidget(self.spin_single_tht_pad)
        single_tht_layout.addWidget(QLabel("Foro Ø:"))
        single_tht_layout.addWidget(self.spin_single_tht_drill)
        single_tht_layout.addStretch(1)
        self.btn_regenerate_single_tht = QPushButton("Rigenera footprint")
        self.btn_regenerate_single_tht.setToolTip(
            "Usa le dimensioni dei pad indicate qui e rigenera il footprint 2D.")
        self.btn_regenerate_single_tht.clicked.connect(
            lambda: self._generate(footprint_only=True))
        single_tht_layout.addWidget(self.btn_regenerate_single_tht)
        self.group_single_tht_pads.hide()
        self._sync_single_tht_pad_controls()
        analysis_layout.addWidget(self.group_single_tht_pads)
        # Solo modello 3D: gli strip non cambiano ne' l'analisi ne' il
        # footprint (utente, 2026-09-26). Spenti di default.
        self.chk_header_pins = QCheckBox("Mostra gli header nel modello 3D")
        self.chk_header_pins.setToolTip(
            "Aggiunge sotto la scheda gli strip maschi a pin quadrati, come una scheda "
            "con gli strip gia' saldati. Sulle schede castellated solo sui contatti con "
            "foro passante. Non cambia l'analisi ne' il footprint: basta rigenerare il 3D.")
        self.chk_header_pins.toggled.connect(self._on_header_pins_toggled)
        self.group_mounting = QGroupBox("Montaggio sulla scheda principale")
        mounting_layout = QVBoxLayout(self.group_mounting)
        self.combo_mounting = QComboBox()
        self.combo_mounting.addItem("Saldatura diretta / piazzole del modulo", "direct")
        self.combo_mounting.addItem("Strip header / pad circolari con foro", "headers")
        self.combo_mounting.addItem("Strip header / pad quadrati con foro", "headers_square")
        mounting_layout.addWidget(self.combo_mounting)
        mounting_note = QLabel(
            "La scelta cambia solo il footprint della scheda ospitante. "
            "Il modello 3D della board resta fedele alla foto.")
        mounting_note.setWordWrap(True)
        mounting_layout.addWidget(mounting_note)
        mounting_layout.addWidget(self.chk_header_pins)
        self.header_dimensions = QWidget()
        dimensions_form = QFormLayout(self.header_dimensions)
        self.spin_header_pad = QDoubleSpinBox()
        self.spin_header_drill = QDoubleSpinBox()
        self.header_pad_label = QLabel("Diametro pad sulla scheda ospitante:")
        for spin, value in ((self.spin_header_pad, 1.7), (self.spin_header_drill, 1.0)):
            spin.setRange(0.1, 10.0)
            spin.setDecimals(2)
            spin.setSingleStep(0.1)
            spin.setSuffix(" mm")
            spin.setKeyboardTracking(False)
            spin.setValue(value)
            spin.valueChanged.connect(self._on_mounting_changed)
        dimensions_form.addRow(self.header_pad_label, self.spin_header_pad)
        dimensions_form.addRow("Diametro foro per il pin:", self.spin_header_drill)
        dimensions_note = QLabel("Valori iniziali da verificare con gli header utilizzati.")
        dimensions_note.setWordWrap(True)
        dimensions_form.addRow(dimensions_note)
        mounting_layout.addWidget(self.header_dimensions)
        self.header_dimensions.hide()
        self.label_mounting_error = QLabel()
        self.label_mounting_error.setWordWrap(True)
        self.label_mounting_error.setProperty("i18n_skip", True)
        mounting_layout.addWidget(self.label_mounting_error)
        self.btn_update_host_footprint = QPushButton("Aggiorna solo il footprint")
        self.btn_update_host_footprint.setEnabled(False)
        self.btn_update_host_footprint.clicked.connect(lambda: self._generate(footprint_only=True))
        mounting_layout.addWidget(self.btn_update_host_footprint)
        self.combo_mounting.currentIndexChanged.connect(self._on_mounting_changed)
        # Spessore della scheda (utente, 27/09, punto 132): per le sole schede,
        # accanto ai pin quadrati perche' anche lui cambia soltanto il 3D.
        self.spin_pcb_thickness = QDoubleSpinBox()
        # Una lastra di spessore nullo non esiste; 10 mm e' il limite del campo.
        self.spin_pcb_thickness.setRange(0.1, 10.0)
        self.spin_pcb_thickness.setDecimals(2)
        self.spin_pcb_thickness.setSingleStep(0.1)
        self.spin_pcb_thickness.setSuffix(" mm")
        # Il valore vale quando si conferma, non a ogni cifra digitata.
        self.spin_pcb_thickness.setKeyboardTracking(False)
        self.spin_pcb_thickness.setToolTip(
            "Spessore della sola scheda, senza componenti. Un valore scritto qui vale per "
            "il progetto e una nuova analisi non lo cambia. Basta rigenerare il 3D.")
        self.spin_pcb_thickness.valueChanged.connect(self._on_pcb_thickness_edited)
        self.label_pcb_thickness_source = QLabel()
        # Testo gia' tradotto da _refresh_pcb_thickness_row: la ripassata
        # generica lo userebbe come chiave.
        self.label_pcb_thickness_source.setProperty("i18n_skip", True)
        self.btn_pcb_thickness_reset = QPushButton("Ripristina")
        self.btn_pcb_thickness_reset.setAutoDefault(False)
        self.btn_pcb_thickness_reset.setToolTip(
            "Torna allo spessore dell'ultima analisi, o allo standard di 1,6 mm se "
            "l'analisi non ne ha dato uno.")
        self.btn_pcb_thickness_reset.clicked.connect(self._reset_pcb_thickness)
        pcb_row = QHBoxLayout()
        pcb_row.setContentsMargins(0, 0, 0, 0)
        pcb_row.addWidget(self.spin_pcb_thickness)
        pcb_row.addWidget(self.label_pcb_thickness_source, 1)
        pcb_row.addWidget(self.btn_pcb_thickness_reset)
        # Etichetta collegata al campo (audit 138, C6): con un layout come
        # campo QFormLayout non imposta il «buddy», e un lettore di schermo non
        # annunciava lo spessore con il suo nome.
        self.label_pcb_thickness = QLabel("Spessore PCB:")
        self.label_pcb_thickness.setBuddy(self.spin_pcb_thickness)
        self.spin_pcb_thickness.setAccessibleName("Spessore PCB")
        ai_form.addRow(self.label_pcb_thickness, pcb_row)
        self._ai_form = ai_form
        self._row_pcb_thickness = ai_form.rowCount() - 1
        self._align_form(ai_form, "project")
        models_form = QFormLayout()

        self.combo_model = QComboBox()
        self.combo_model.setToolTip(
            "Unico modello AI usato per datasheet, analisi foto e verifica. "
            "Il default si imposta da Configurazione -> Gestione tipologie e profili AI."
        )
        self.combo_model.currentIndexChanged.connect(self._on_model_combo_changed)
        self.btn_pick_model = QPushButton()
        self.btn_pick_model.setIcon(QIcon(str(Path(__file__).parent / 'assets' / 'model-edit.svg')))
        self.btn_pick_model.setIconSize(QSize(20, 20))
        self.btn_pick_model.setFixedSize(40, 32)
        self.btn_pick_model.setAutoDefault(False)
        self.btn_pick_model.setToolTip("Change the AI model for this session")
        self.btn_pick_model.setAccessibleName("Change AI model")
        self.btn_pick_model.setToolTip(
            "Sceglie un modello (filtrabile per costo) valido solo per le "
            "estrazioni di questa sessione: il default globale non cambia."
        )
        self.btn_pick_model.clicked.connect(self._pick_session_model)
        model_row = QHBoxLayout()
        # La casella «Ragionamento DeepSeek» (HANDOFF 161) e' stata tolta
        # dall'utente il 2026-10-02: il ragionamento resta sempre spento.
        model_row.addWidget(self.combo_model, 1)
        model_row.addWidget(self.btn_pick_model)
        self.label_photo_detector = QLabel()
        self.combo_photo_detector = QComboBox()
        self.combo_photo_detector.addItem("", "ai")
        self.combo_photo_detector.addItem("", "yolo")
        self.combo_photo_detector.addItem("", "vlm")
        # In coda, non in mezzo (utente, 2026-09-25): le tre voci esistenti
        # restano nella loro posizione. I progetti salvati ricordano la voce
        # per valore (project_state.widget_value), non per posizione.
        self.combo_photo_detector.addItem("", "yolo_only")
        self.combo_photo_detector.addItem("", "yolo_smd")
        # Default dell'utente (2026-10-02): la rete SMD locale classifica e solo
        # cio' che non risolve va all'AI. Nel binario frozen la rete non c'e'
        # (niente PyTorch ne' pesi): li' resta YOLO con la riconciliazione AI.
        self.combo_photo_detector.setCurrentIndex(
            self.combo_photo_detector.findData("yolo" if is_frozen() else "yolo_smd"))
        self.combo_photo_detector.currentIndexChanged.connect(self._on_photo_detector_changed)
        # Prima riga del riquadro (utente, 2026-09-25): la scelta del
        # rilevatore decide quali delle righe sotto servono.
        models_form.addRow(self.label_photo_detector, self.combo_photo_detector)
        self._row_photo_detector = models_form.rowCount() - 1
        models_form.addRow("Modello AI:", model_row)
        self._row_ai_model = models_form.rowCount() - 1
        self.label_yolo_model = QLabel()
        self.field_yolo_model = QLineEdit()
        self.field_yolo_model.setClearButtonEnabled(True)
        # Stessa cartella e stessa misura del pulsante accanto ad «Area di
        # lavoro», che fa la stessa cosa: scegliere un file o una cartella.
        self.btn_pick_yolo_model = QPushButton()
        self.btn_pick_yolo_model.setAutoDefault(False)
        self.btn_pick_yolo_model.setIcon(self._asset_icon("docs-open.svg"))
        self.btn_pick_yolo_model.setIconSize(QSize(20, 20))
        self.btn_pick_yolo_model.setFixedSize(40, 32)
        self.btn_pick_yolo_model.clicked.connect(self._pick_session_yolo_model)
        yolo_row = QHBoxLayout()
        yolo_row.addWidget(self.field_yolo_model, 1)
        yolo_row.addWidget(self.btn_pick_yolo_model)
        models_form.addRow(self.label_yolo_model, yolo_row)
        self._row_yolo_model = models_form.rowCount() - 1
        # «Modello rete SMD» (utente, 2026-10-02): come «Modello YOLO», per
        # la rete che classifica i corpi; si vede solo con la rete SMD.
        self.label_smd_model = QLabel()
        self.field_smd_model = QLineEdit()
        self.field_smd_model.setClearButtonEnabled(True)
        self.btn_pick_smd_model = QPushButton()
        self.btn_pick_smd_model.setAutoDefault(False)
        self.btn_pick_smd_model.setIcon(self._asset_icon("docs-open.svg"))
        self.btn_pick_smd_model.setIconSize(QSize(20, 20))
        self.btn_pick_smd_model.setFixedSize(40, 32)
        self.btn_pick_smd_model.clicked.connect(self._pick_session_smd_model)
        smd_row = QHBoxLayout()
        smd_row.addWidget(self.field_smd_model, 1)
        smd_row.addWidget(self.btn_pick_smd_model)
        models_form.addRow(self.label_smd_model, smd_row)
        self._row_smd_model = models_form.rowCount() - 1
        # Parametri del rilevatore (utente, 2026-09-25): soglia, doppioni e
        # tessere si scelgono SOLO nella modale «Parametri YOLO»; qui un
        # riepilogo in sola lettura e il pulsante, stessa misura e stile di
        # quello del modello. La soglia dipende dal modello, come su Roboflow.
        self.label_yolo_params = QLabel()
        self.label_yolo_summary = QLabel()
        self.btn_yolo_settings = QPushButton()
        self.btn_yolo_settings.setAutoDefault(False)
        self.btn_yolo_settings.setIcon(self._asset_icon("sliders.svg"))
        self.btn_yolo_settings.setIconSize(QSize(20, 20))
        self.btn_yolo_settings.setFixedSize(40, 32)
        self.btn_yolo_settings.clicked.connect(self._open_yolo_settings)
        params_row = QHBoxLayout()
        params_row.addWidget(self.label_yolo_summary, 1)
        params_row.addWidget(self.btn_yolo_settings)
        models_form.addRow(self.label_yolo_params, params_row)
        self._row_yolo_params = models_form.rowCount() - 1
        self.label_vlm_model = QLabel()
        self.field_vlm_model = QLineEdit()
        self.field_vlm_model.setReadOnly(True)
        # Stessa icona e stessa misura del pulsante accanto a «Modello AI»
        # (utente, 2026-09-24: «uniforma questo per scegliere il modello»);
        # il nome sta nel tooltip e nel nome accessibile.
        self.btn_pick_vlm_model = QPushButton()
        self.btn_pick_vlm_model.setIcon(self._asset_icon("model-edit.svg"))
        self.btn_pick_vlm_model.setIconSize(QSize(20, 20))
        self.btn_pick_vlm_model.setFixedSize(40, 32)
        self.btn_pick_vlm_model.setAutoDefault(False)
        self.btn_pick_vlm_model.clicked.connect(self._pick_session_vlm_model)
        vlm_row = QHBoxLayout()
        vlm_row.addWidget(self.field_vlm_model, 1)
        vlm_row.addWidget(self.btn_pick_vlm_model)
        models_form.addRow(self.label_vlm_model, vlm_row)
        self._row_vlm_model = models_form.rowCount() - 1
        # Saldo dell'account e costo TOTALE del progetto, tutti i passaggi
        # compresi (`ai_account`): ultima riga di «Strumenti AI» (utente,
        # 2026-09-24), accanto ai modelli che quel credito lo spendono. Non e'
        # il consuntivo dell'ULTIMA analisi, tolto dalla scheda: quello resta
        # nella modale e nel Log AI (`_show_ai_cost`).
        from .ai_account import AiAccountLine
        self.ai_account = AiAccountLine(self._project_dir_or_none, self._append_log)
        # Il saldo mostrato e' quello del fornitore del modello in uso, letto
        # a ogni aggiornamento (utente, 27/09).
        self.ai_account.provider_source = self._balance_provider
        self.label_ai_account = QLabel("Credito AI:")
        models_form.addRow(self.label_ai_account, self.ai_account)
        self._row_ai_account = models_form.rowCount() - 1
        self._models_form = models_form
        # These data-bearing widgets use explicit keys and local I18n fallbacks.
        for widget in (self.label_photo_detector, self.combo_photo_detector,
                       self.label_yolo_model, self.field_yolo_model, self.btn_pick_yolo_model,
                       self.label_smd_model, self.field_smd_model, self.btn_pick_smd_model,
                       self.label_yolo_params, self.label_yolo_summary,
                       self.btn_yolo_settings,
                       self.label_vlm_model, self.field_vlm_model, self.btn_pick_vlm_model):
            widget.setProperty("i18n_skip", True)
        self._align_form(models_form, "project")

        analysis_layout.addLayout(ai_form)
        analysis_layout.addWidget(self.group_mounting)

        self.group_ai_tools = CollapsibleTools("Strumenti AI")
        tools_layout = QVBoxLayout(self.group_ai_tools.content)
        tools_layout.setContentsMargins(12, 16, 12, 12)
        tools_layout.setSpacing(12)
        tools_layout.addLayout(models_form)
        analysis_layout.addWidget(self.group_ai_tools)
        # Only now that the form sits in a widget: `setRowVisible(..., True)`
        # on the rows of an unparented layout showed every label and field as
        # a top-level window for a moment at start-up (user, 2026-10-13).
        self._refresh_photo_detector_controls()

        self.btn_extract_ai = QPushButton("Estrai parametri con AI")
        self.btn_extract_ai.setObjectName("primary")
        self.btn_extract_ai.setIcon(QIcon(str(Path(__file__).resolve().parent / "assets" / "extract-ai.svg")))
        self.btn_extract_ai.setIconSize(QSize(24, 24))
        self.btn_extract_ai.setMinimumHeight(46)
        # Il costo sta nel suggerimento perche' questo e' l'unico comando della
        # scheda che spende da solo: la regola della riga in fondo («ogni
        # comando che spende lo dice prima, ogni comando gratuito si riconosce»)
        # comincia da qui, non dalla riga dei rientri.
        self.btn_extract_ai.setToolTip(
            "Avvia l'estrazione dai documenti e dalle foto selezionati. "
            "Analisi A PAGAMENTO: consuma credito OpenRouter. I modelli usati e la "
            "spesa vengono scritti nel log prima di partire.")
        self.btn_extract_ai.clicked.connect(self._extract_with_ai)
        # Testo e suggerimento seguono il rilevatore: con «Solo YOLO locale» il
        # comando e' un pretest gratuito, non un'analisi a pagamento.
        self._refresh_extract_button()
        # UNA revisione dei componenti (utente, 2026-09-30, HANDOFF 162): la
        # stessa finestra mostra le proposte del rilevatore locale salvate in
        # photo-review/ (prima dell'inventario AI: all'OK le risalva li') e,
        # dopo l'inventario, i corpi riconciliati con la lettura del
        # rilevatore accanto. Lo stadio lo sceglie `_open_component_review`.
        # Decisione dell'utente (2026-09-24): «se il file json esiste nella
        # cartella deve essere possibile aprire la revisione bypassando
        # interrogazione AI»; e (2026-09-30): «non ha senso che ci siano due
        # viste separate».
        self.btn_component_review = QPushButton("Revisione componenti…")
        self.btn_component_review.setToolTip(
            "Passi 1 e 3 della pipeline — Apre la revisione dei componenti: prima "
            "dell'inventario AI mostra le proposte del rilevatore locale salvate in "
            "photo-review/ e, alla conferma, le risalva; dopo l'inventario mostra i corpi "
            "riconciliati con accanto la lettura del rilevatore (mai sovrascritta), le "
            "quote e la scelta dei modelli KiCad. Non avvia la generazione e non fa "
            "richieste AI: gratuito. Dopo averla applicata, «Rigenera modello 3D» nella "
            "scheda Modello 3D ricostruisce lo STEP."
        )
        self.btn_component_review.setEnabled(False)
        self.btn_component_review.clicked.connect(self._open_component_review)
        # La spesa sta in un pulsante a parte: chi vuole solo rivedere non
        # deve poter pagare per sbaglio con lo stesso OK.
        self.btn_relaunch_inventory = QPushButton("Rilancia inventario AI (a pagamento)…")
        self.btn_relaunch_inventory.setToolTip(
            "Passo 2 della pipeline — Rilancia solo l'inventario AI sulle proposte "
            "salvate in photo-review/: una richiesta A PAGAMENTO al modello che guarda "
            "la foto, poi abbinamento modelli e 3D come dopo un'estrazione. È l'unico "
            "comando di questa riga che spende."
        )
        self.btn_relaunch_inventory.setEnabled(False)
        self.btn_relaunch_inventory.clicked.connect(self._relaunch_inventory)
        # `btn_photo_comparison` non e' un secondo widget: e' l'altro nome
        # dello stesso comando, cosi' i percorsi che aprivano il confronto
        # fotografico puntano al comando vero e non a un doppione nascosto.
        self.btn_photo_comparison = self.btn_component_review
        # Vero quando c'e' un pacchetto di proposte del rilevatore da riaprire
        # (cartella del componente o cache di sessione): lo stadio «rilevatore»
        # della revisione. Scritto da `_set_review_buttons_available`.
        self._review_bundle_available = False
        self._photo_png = None
        self._photo_body_fields = None
        self._photo_overlay_png = None
        self._photo_source = ""
        # Icona nella barra in alto della scheda (utente, 2026-09-24): nome e
        # spiegazione nel tooltip, come le altre barre di icone.
        self.btn_ai_log = self._workflow_tool_button(
            "ai-log.svg", "Log AI",
            "Apri il log completo dell'analisi AI, aggiornato in tempo reale")
        self.ai_log_dialog = QDialog(self)
        self.ai_log_dialog.setWindowTitle("Log analisi AI")
        self.ai_log_dialog.setModal(True)
        log_layout = QVBoxLayout(self.ai_log_dialog)

        log_splitter = QSplitter(Qt.Orientation.Horizontal)
        actions_panel = QWidget()
        actions_layout = QVBoxLayout(actions_panel)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_header = QHBoxLayout()
        actions_header.addWidget(QLabel("Azioni analisi"), 1)
        self.btn_copy_ai_log = icon_button('', 'profile-copy.svg', lambda: self._copy_ai_output())
        self.btn_copy_ai_log.setToolTip('Copia tutte le azioni analisi negli appunti')
        self.btn_copy_ai_log.setAccessibleName('Copia log azioni analisi')
        self.btn_copy_ai_log.setEnabled(False)
        actions_header.addWidget(self.btn_copy_ai_log)
        actions_layout.addLayout(actions_header)
        self.ai_progress_text = QTextEdit()
        self.ai_progress_text.setReadOnly(True)
        self.ai_progress_text.setPlaceholderText("Qui compariranno i passi dell'analisi AI.")
        self.ai_progress_text.textChanged.connect(self._refresh_ai_copy_buttons)
        actions_layout.addWidget(self.ai_progress_text)
        elements_panel = QWidget()
        elements_layout = QVBoxLayout(elements_panel)
        elements_layout.setContentsMargins(0, 0, 0, 0)
        elements_header = QHBoxLayout()
        elements_header.addWidget(QLabel("Elementi rilevati"), 1)
        self.btn_copy_ai_elements = icon_button('', 'profile-copy.svg', lambda: self._copy_ai_output(elements=True))
        self.btn_copy_ai_elements.setToolTip('Copia tutti gli elementi rilevati negli appunti')
        self.btn_copy_ai_elements.setAccessibleName('Copia elementi rilevati')
        self.btn_copy_ai_elements.setEnabled(False)
        elements_header.addWidget(self.btn_copy_ai_elements)
        elements_layout.addLayout(elements_header)
        self.ai_elements_list = QListWidget()
        self.ai_elements_list.setToolTip("Elementi e ingombri individuati durante l'analisi fotografica")
        for signal in (self.ai_elements_list.model().rowsInserted,
                       self.ai_elements_list.model().rowsRemoved,
                       self.ai_elements_list.model().modelReset):
            signal.connect(self._refresh_ai_copy_buttons)
        elements_layout.addWidget(self.ai_elements_list)
        log_splitter.addWidget(actions_panel)
        log_splitter.addWidget(elements_panel)
        log_splitter.setSizes([540, 380])
        log_layout.addWidget(log_splitter)
        log_close = QPushButton("Chiudi")
        log_close.clicked.connect(self.ai_log_dialog.accept)
        log_layout.addWidget(log_close, alignment=Qt.AlignmentFlag.AlignRight)
        self.ai_log_dialog.resize(1000, 520)
        self.btn_ai_log.clicked.connect(self.ai_log_dialog.exec)

        self.btn_component_prompt = self._workflow_tool_button(
            "prompt-edit.svg", "Modifica prompt…",
            "Modifica le istruzioni AI specifiche di questo componente")
        analysis_layout.addSpacing(10)
        analysis_layout.addWidget(self.btn_extract_ai, alignment=Qt.AlignmentFlag.AlignHCenter)
        # Nessun consuntivo sotto il pulsante (utente, 2026-09-24): saldo e
        # costo del progetto stanno nel riquadro «Strumenti AI».
        self.field_name.textChanged.connect(self.ai_account.refresh)
        # La barra di stato segue ogni scrittura del file dei costi, non solo
        # i cambi di progetto: prima restava indietro durante un'analisi (B4).
        self.ai_account.costs_changed.connect(self._on_costs_changed)
        self.field_library_root.textChanged.connect(self.ai_account.refresh)
        self.component_prompt_dialog = QDialog(self)
        self.component_prompt_dialog.setWindowTitle("Prompt specifico del componente")
        self.component_prompt_dialog.setModal(True)
        prompt_layout = QVBoxLayout(self.component_prompt_dialog)
        prompt_note = QLabel(
            "Prompt specifico di QUESTO componente -- ci finisce il testo della "
            "tipologia scelta qui sopra, modificabile, piu' le note che vuoi "
            "aggiungere. Il prompt generale si imposta da Configurazione -> "
            "Modello e prompt di sistema LLM:"
        )
        prompt_note.setWordWrap(True)
        prompt_layout.addWidget(prompt_note)
        self.field_component_prompt = QTextEdit()
        self.field_component_prompt.setAcceptRichText(False)
        self.field_component_prompt.setPlaceholderText(
            "es. il case e' metallico, non plastico -- oppure: ignora il connettore "
            "laterale, considera solo i pin del lato inferiore"
        )
        prompt_layout.addWidget(self.field_component_prompt)
        prompt_buttons = QHBoxLayout()
        prompt_buttons.addStretch()
        cancel_prompt = QPushButton("Annulla")
        cancel_prompt.clicked.connect(self.component_prompt_dialog.reject)
        save_prompt = QPushButton("Salva")
        save_prompt.setObjectName("primary")
        save_prompt.clicked.connect(self.component_prompt_dialog.accept)
        prompt_buttons.addWidget(cancel_prompt)
        prompt_buttons.addWidget(save_prompt)
        prompt_layout.addLayout(prompt_buttons)
        self.component_prompt_dialog.resize(900, 560)
        self.btn_component_prompt.clicked.connect(self._edit_component_prompt)

        outer.addWidget(self.group_component_analysis)
        outer.addStretch()
        # La pausa dopo l'inventario sta in Configurazione -> Opzioni di
        # generazione, e la riga di testo che elencava i rientri e' stata tolta
        # (utente, 2026-09-24): ordine e costo di ogni passo sono nel tooltip
        # del suo pulsante («Passo 1 della pipeline — …»).
        # Richiesta dell'utente: «un pulsante in basso nella scheda
        # principale» per riaprire la revisione dopo averla salvata. Sta
        # sotto tutto, non nella riga degli strumenti dell'analisi, cosi' si
        # trova anche a estrazione chiusa e senza cercarlo fra le opzioni.
        # L'ordine dei tre comandi e' quello della pipeline (rilevatore ->
        # inventario a pagamento -> revisione dei componenti), non quello in
        # cui sono stati aggiunti: e' l'ordine a dire che sono passi di una
        # stessa catena e non tre azioni scorrelate.
        # Dal 2026-09-24 (utente) la riga di pulsanti in fondo non c'e' piu':
        # i tre comandi sono icone nella barra in alto (`project_action_bar`),
        # copie costruite con lo stesso meccanismo della scheda Modello 3D.
        # I pulsanti originali restano la FONTE di testo, tooltip e stato, in
        # un contenitore sempre nascosto: le copie li leggono, i percorsi che
        # li usano restano gli stessi, e una `setVisible(True)` su di loro non
        # puo' aprire una finestra a se' perche' hanno un genitore.
        self._project_action_sources = QWidget(tab)
        sources_layout = QHBoxLayout(self._project_action_sources)
        for source in (self.btn_component_review, self.btn_relaunch_inventory):
            sources_layout.addWidget(source)
        self._project_action_sources.hide()
        bar = self.project_action_bar
        from .review_tools import toolbar_divider
        # Nuovo e Apri progetto in testa alla barra (utente, 2026-09-25): gli
        # stessi comandi del menu File, con le stesse guardie (nessun cambio di
        # progetto mentre un lavoro scrive nella cartella in uso).
        self.btn_new_project_top = self._workflow_tool_button(
            "project-new.svg", "Nuovo progetto",
            "Crea la cartella di un nuovo componente nella cartella di lavoro (Ctrl+N)")
        self.btn_new_project_top.clicked.connect(self._new_project)
        self.btn_open_project_top = self._workflow_tool_button(
            "project-open.svg", "Apri progetto",
            "Apre un componente esistente della cartella di lavoro (Ctrl+O)")
        self.btn_open_project_top.clicked.connect(self._open_project)
        bar.addWidget(self.btn_new_project_top)
        bar.addWidget(self.btn_open_project_top)
        bar.addWidget(toolbar_divider())
        bar.addWidget(self.btn_ai_log)
        bar.addWidget(self.btn_component_prompt)
        self._project_bar_divider = toolbar_divider()
        bar.addWidget(self._project_bar_divider)
        # Stesso ordine della pipeline e stesse icone della scheda Modello 3D:
        # UNA revisione (gratuita), poi il rilancio (a pagamento).
        self.btn_component_review_top = self._mirror_project_action(
            self.btn_component_review, self._open_component_review, bar, "wf-review.svg")
        self.btn_relaunch_inventory_top = self._mirror_project_action(
            self.btn_relaunch_inventory, self._relaunch_inventory, bar, "wf-relaunch-ai.svg")
        bar.addStretch()
        # Stato iniziale coerente con la disciplina della riga: cio' che non
        # puo' agire non si vede, invece di stare acceso a vuoto.
        self._set_review_buttons_enabled(False)
        self._refresh_ai_log_button()

        self._refresh_model_combo()
        self._restore_component_type()
        self._update_project_path_label()
        # Keep the editors and their existing signals; only their navigation
        # changes. The primary action lives outside the scrolling page.
        for group in (folder_group, docs_group, self.group_component_analysis):
            outer.removeWidget(group)
        outer.takeAt(outer.count() - 1)  # the old bottom stretch
        analysis_layout.removeWidget(self.btn_extract_ai)
        documents_page = QWidget()
        documents_layout = QVBoxLayout(documents_page)
        documents_layout.setContentsMargins(0, 0, 0, 0)
        self.project_details = CollapsibleTools("Dati del progetto")
        self.project_details.summary.setText(self._component_name())
        self.field_name.textChanged.connect(self.project_details.summary.setText)
        details_layout = QVBoxLayout(self.project_details.content)
        details_layout.addWidget(folder_group)
        documents_layout.addWidget(self.project_details)
        documents_layout.addWidget(docs_group)
        self.project_wizard = ProjectWizard(
            (documents_page, self.group_component_analysis),
            self.btn_extract_ai, self.i18n.text, tab)
        self.project_wizard.page_changed.connect(self._on_project_wizard_page)
        self.project_wizard.projects_requested.connect(self._return_to_projects)
        outer.addWidget(self.project_wizard, 1)
        self._project_scroll = self.project_wizard.scrolls[0]
        for combo in (self.combo_model, self.combo_photo_detector):
            combo.currentIndexChanged.connect(self._refresh_ai_tools_summary)
        self._refresh_ai_tools_summary()
        return tab

    def _on_project_wizard_page(self, index: int) -> None:
        """@brief Track the current scroll area without changing project data."""
        self._project_scroll = self.project_wizard.scrolls[index]

    def _return_to_projects(self) -> None:
        """@brief Save the current project and return to New/Open/Recent projects."""
        if self._project_change_blocked():
            return
        if not self._can_leave_project():
            return
        self._switch_project("", self._general_project_state)

    def _refresh_ai_tools_summary(self) -> None:
        """@brief Riassume solo gli strumenti pertinenti al tipo di progetto."""
        model = self.combo_model.currentText()
        if self._is_single_component_project():
            summary = self.i18n.tr(
                "ui:Componente singolo: analisi AI dei documenti · {model}",
                model=model)
        else:
            summary = f"{self.combo_photo_detector.currentText()} · {model}"
        self.group_ai_tools.summary.setText(summary)

    def _mounting_state(self) -> dict:
        """@brief Serialize the host mounting independently from the physical board."""
        # The APPLIED pad overrides: an unapplied footprint draft never
        # reaches the project file (user, 2026-10-14).
        return {"mode": self.combo_mounting.currentData(),
                "pad_diameter": self.spin_header_pad.value(),
                "drill": self.spin_header_drill.value(),
                "pad_overrides": deepcopy(self._applied_footprint_state()["pad_overrides"])}

    def _host_footprint_params(self, params):
        """@brief Derive host pads only; callers retain params for the photo-faithful 3D."""
        mode = self.combo_mounting.currentData()
        if (mode not in {"headers", "headers_square"}
                or self._photo_inventory_family() is not True):
            return params
        shape = "rect" if mode == "headers_square" else "circle"
        return header_footprint_params(params, self.spin_header_pad.value(),
                                      self.spin_header_drill.value(), shape=shape,
                                      pad_overrides=self.header_pad_overrides)

    def _on_mounting_changed(self, *_args) -> None:
        """@brief Refresh the footprint preview without invalidating the board mesh."""
        square = self.combo_mounting.currentData() == "headers_square"
        self.header_pad_label.setText(self.i18n.text(
            "Lato pad sulla scheda ospitante:" if square else
            "Diametro pad sulla scheda ospitante:"))
        self.header_dimensions.setVisible(
            self.combo_mounting.currentData() in {"headers", "headers_square"})
        self._set_mounting_error("")
        self._sync_preview_mounting()
        if getattr(self, "_restoring_project", False):
            return
        self._last_params = None
        self._draw_preview()

    def _refresh_mounting_actions(self) -> None:
        """@brief Allow footprint-only updates once real component data is available."""
        button = getattr(self, "btn_update_host_footprint", None)
        enabled = (bool(getattr(self, "_acquisition_completed", False)
                        or self._project_has_footprint())
                   and not self._background_work_running())
        if button is not None:
            button.setEnabled(enabled)
        if hasattr(self, "btn_update_host_footprint_preview"):
            self.btn_update_host_footprint_preview.setEnabled(enabled)

    def _set_mounting_error(self, message: str) -> None:
        """@brief Show validation feedback beside both editors of the same host footprint."""
        self.label_mounting_error.setText(message)
        label = getattr(self, "label_preview_mounting_error", None)
        if label is not None:
            label.setText(message)
            label.setVisible(bool(message))

    def _sync_preview_mounting(self) -> None:
        """@brief Mirror project mounting values without triggering another edit or CAD work."""
        if not hasattr(self, "preview_mounting_group"):
            return
        board = self._photo_inventory_family() if self._selected_family() else None
        self.preview_mounting_group.setVisible(board is True)
        pairs = ((self.combo_preview_mounting, self.combo_mounting),
                 (self.spin_preview_header_pad, self.spin_header_pad),
                 (self.spin_preview_header_drill, self.spin_header_drill))
        for target, source in pairs:
            blocked = target.blockSignals(True)
            if isinstance(source, QComboBox):
                target.setCurrentIndex(target.findData(source.currentData()))
            else:
                target.setValue(source.value())
            target.blockSignals(blocked)
        square = self.combo_mounting.currentData() == "headers_square"
        self.preview_header_pad_label.setText(self.i18n.text("Lato:" if square else "Pad Ø:"))
        self.preview_header_dimensions.setVisible(
            self.combo_mounting.currentData() in {"headers", "headers_square"})

    @staticmethod
    def _valid_pad_overrides(raw) -> dict:
        """@brief Keep the well-formed host pad overrides of a saved mapping.

        @param raw {pad number: {"shape", "size"}} as read from a file.
        @return Overrides with a known shape ("circle", "rect") and a finite
            size of 0.1-10 mm, keyed by the pad number as text; the others
            are dropped.
        @note One validator for the project file and the footprint recovery
            file (`_footprint_state_from_json`).
        """
        overrides = {}
        if not isinstance(raw, dict):
            return overrides
        for number, override in raw.items():
            if not isinstance(override, dict) or override.get("shape") not in {"circle", "rect"}:
                continue
            try:
                size = float(override.get("size"))
            except (TypeError, ValueError):
                continue
            if math.isfinite(size) and 0.1 <= size <= 10.0:
                overrides[str(number)] = {"shape": override["shape"], "size": size}
        return overrides

    def _restore_mounting_state(self, state) -> None:
        """@brief Restore host settings, defaulting legacy projects to their original lands."""
        state = state if isinstance(state, dict) else {}
        mode = state.get("mode", "direct")
        index = self.combo_mounting.findData(mode)
        controls = ((self.combo_mounting, max(0, index)),
                    (self.spin_header_pad, state.get("pad_diameter", 1.7)),
                    (self.spin_header_drill, state.get("drill", 1.0)))
        for widget, value in controls:
            blocked = widget.blockSignals(True)
            if isinstance(widget, QComboBox):
                widget.setCurrentIndex(value)
            else:
                fallback = 1.7 if widget is self.spin_header_pad else 1.0
                valid = (isinstance(value, (int, float)) and not isinstance(value, bool)
                         and math.isfinite(value) and widget.minimum() <= value <= widget.maximum())
                widget.setValue(value if valid else fallback)
            widget.blockSignals(blocked)
        self.header_pad_overrides = self._valid_pad_overrides(state.get("pad_overrides", {}))
        square = self.combo_mounting.currentData() == "headers_square"
        self.header_pad_label.setText(self.i18n.text(
            "Lato pad sulla scheda ospitante:" if square else
            "Diametro pad sulla scheda ospitante:"))
        self.header_dimensions.setVisible(
            self.combo_mounting.currentData() in {"headers", "headers_square"})
        self._set_mounting_error("")
        self._sync_preview_mounting()

    # ── Tipologia componente ────────────────────────────────────────

    def _edit_component_prompt(self) -> None:
        original = self.field_component_prompt.toPlainText()
        if self.component_prompt_dialog.exec() != QDialog.DialogCode.Accepted:
            # Annulla, Escape and the window close button all discard edits.
            self.field_component_prompt.setPlainText(original)

    def _restore_component_type(self) -> None:
        """Ripropone l'ultima tipologia usata, inserendone il testo nel prompt."""
        saved = (self._config.get("component_type") or "").strip()
        index = self.combo_component_type.findData(saved) if saved else 0
        self.combo_component_type.setCurrentIndex(max(index, 0))

    def _on_component_type_changed(self) -> None:
        """Sostituisce nel prompt specifico il blocco della tipologia scelta.

        Il blocco e' delimitato da marcatori: cambiando tipologia si riscrive
        solo quello, mentre le note aggiunte a mano restano al loro posto. Da
        qui in avanti il testo e' liberamente modificabile.
        """
        key = self.combo_component_type.currentData() or ""
        row = self.profile_store.get_type(key)
        component_type = component_types.ComponentType(key, row['label'], row['instructions']) if row else None
        # The type chosen here is as explicit as the wizard's choice: a type
        # whose family decides nothing («(non specificata)», a custom family)
        # leaves the kind unknown, so `_photo_inventory_family` answers None
        # and the paid photo call stays declared (test contract of 2026-10-02).
        self.project_kind = self._project_kind_for_family(row['family'] if row else "")
        # La tipologia sblocca (o blocca) l'analisi: vedi _extract_block_reason.
        if hasattr(self, "btn_extract_ai") and not self._background_work_running():
            self._apply_component_name_gate()

        self.field_component_prompt.setPlainText(
            component_types.apply_block(
                self.field_component_prompt.toPlainText(), component_type
            )
        )

        # La tipologia decide anche quali parametri del footprint si usano:
        # i moduli castellated non stanno in una matrice.
        if key and hasattr(self, "group_castellated"):
            self.group_castellated.setChecked(self._selected_family() == "aggregate_castellated")

        # La tipologia riapplicata aprendo un progetto e' del progetto, non una
        # scelta generale: scriverla in config.json farebbe partire i progetti
        # nuovi dall'ultimo aperto (revisore 2, difetto 8).
        if not getattr(self, "_restoring_project", False):
            self._config["component_type"] = key
            self._save_config_safely()
        self._refresh_model_combo()
        self._refresh_type_effect()
        if hasattr(self, "combo_photo_detector"):
            self._refresh_photo_detector_controls()
            self._refresh_ai_tools_summary()
        if component_type is not None:
            self._append_log(tx("Tipologia componente: {label}.",
                                label=self.i18n.component_type(key, component_type.label)))

    def _refresh_type_effect(self) -> None:
        """@brief Offer the header pins choice for boards only, showing the project's value.

        @note Structural, not by name: `_photo_inventory_family` tells boards
            from single components. The line that described the analysis under
            the type was removed by the user (2026-09-26).
        """
        if getattr(self, "chk_header_pins", None) is None:
            return
        if getattr(self, "group_single_tht_pads", None) is not None:
            self.group_single_tht_pads.setVisible(self._selected_family() == "single_tht")
        # Senza tipologia la disposizione rimasta dalla scelta precedente non
        # dice nulla su che cosa si sta analizzando.
        board = self._photo_inventory_family() if self._selected_family() else None
        blocked = self.chk_header_pins.blockSignals(True)
        self.chk_header_pins.setChecked(self._show_pins_3d())
        self.chk_header_pins.blockSignals(blocked)
        self.chk_header_pins.setVisible(board is True)
        self._sync_header_pins_3d()
        self.group_mounting.setVisible(board is True)
        self._sync_preview_mounting()
        if getattr(self, "_ai_form", None) is not None:
            self._ai_form.setRowVisible(self._row_pcb_thickness, board is True)
        self._refresh_pcb_thickness_row()

    def _sync_single_tht_pad_controls(self) -> None:
        """Mirror extracted/current THT land sizes without firing edit handlers."""
        if not hasattr(self, "spin_single_tht_pad"):
            return
        for spin, field in ((self.spin_single_tht_pad, self.field_pad),
                            (self.spin_single_tht_drill, self.field_drill)):
            blocked = spin.blockSignals(True)
            spin.setValue(field.value())
            spin.blockSignals(blocked)

    def _set_single_tht_footprint_value(self, field_name: str, value: float) -> None:
        """Apply a reviewed THT pad/drill value to the shared footprint parameters."""
        getattr(self, field_name).setValue(value)
        self._draw_preview()
        self._invalidate_generated_mesh()
        self._save_project_config()

    def _on_header_pins_toggled(self, checked: bool) -> None:
        """@brief Record the pins choice for the project; the solid on screen is stale.

        @param checked New state of the checkbox.
        """
        self._set_project_option("show_pins_3d", checked)
        self._sync_header_pins_3d()
        self._save_config_safely()
        self._invalidate_generated_mesh()

    def _sync_header_pins_3d(self) -> None:
        """@brief Mirror the optional headers in the 3D toolbar without changing host pads."""
        checkbox = getattr(self, "chk_header_pins_3d", None)
        if checkbox is None:
            return
        blocked = checkbox.blockSignals(True)
        checkbox.setChecked(self._show_pins_3d())
        checkbox.blockSignals(blocked)
        checkbox.setVisible(bool(self._selected_family()) and self._photo_inventory_family() is True)
        checkbox.setEnabled(not bool(self.btn_regen_3d.property("workflow_busy")))
        busy = bool(self.btn_regen_3d.property("workflow_busy"))
        single = self._is_single_component_project()
        bar = getattr(self, "top_text_bar_3d", None)
        if bar is not None:
            bar.setVisible(single)
            self.field_top_text_3d.setEnabled(not busy)
        numbers = getattr(self, "chk_pin_numbers_3d", None)
        if numbers is not None:
            blocked = numbers.blockSignals(True)
            numbers.setChecked(self._pin_numbers_3d())
            numbers.blockSignals(blocked)
            numbers.setVisible(self._selected_family() == "single_tht")
            numbers.setEnabled(not busy)

    def _refresh_pcb_thickness_row(self) -> None:
        """@brief Show the PCB thickness in use and where it comes from.

        @note Signals are blocked: showing a value is not the user typing it.
        """
        spin = getattr(self, "spin_pcb_thickness", None)
        field = getattr(self, "field_cast_pcb", None)
        if spin is None or field is None:
            return
        blocked = spin.blockSignals(True)
        spin.setValue(max(field.value(), spin.minimum()))
        spin.blockSignals(blocked)
        label = PCB_THICKNESS_SOURCE_LABELS.get(self._pcb_thickness_source,
                                                PCB_THICKNESS_SOURCE_LABELS["project"])
        self.label_pcb_thickness_source.setText(self.i18n.text(label))
        self.btn_pcb_thickness_reset.setEnabled(self._pcb_thickness_source == "user")

    def _on_pcb_thickness_edited(self, value: float) -> None:
        """@brief The user typed a PCB thickness: it wins over every later analysis.

        @param value New thickness in mm.
        @note The 3D on screen no longer matches: it is invalidated, like the
            header pins choice; the footprint does not depend on the thickness.
        """
        if abs(value - self.field_cast_pcb.value()) < 1e-9 and self._pcb_thickness_source == "user":
            return
        self._pcb_thickness_source = "user"
        self.field_cast_pcb.setValue(value)
        self._refresh_pcb_thickness_row()
        self._append_log(tx("Spessore PCB impostato da te: {value} mm (una nuova analisi non "
                            "lo cambia).", value=f"{self.field_cast_pcb.value():.2f}"))
        self._invalidate_generated_mesh()

    def _reset_pcb_thickness(self) -> None:
        """@brief Drop the user's PCB thickness: back to the last analysis, or the standard."""
        value, source = self._analysis_pcb_thickness or (STANDARD_PCB_THICKNESS_MM, "standard")
        self._pcb_thickness_source = source
        self.field_cast_pcb.setValue(value)
        self._refresh_pcb_thickness_row()
        self._append_log(tx("Spessore PCB ripristinato: {value} mm ({source}).",
                            value=f"{self.field_cast_pcb.value():.2f}",
                            source=self.i18n.text(PCB_THICKNESS_SOURCE_LABELS[source])))
        self._invalidate_generated_mesh()

    def _component_name(self) -> str:
        return self.field_name.text().strip() if hasattr(self, "field_name") else ""

    def _project_dir_or_none(self) -> Path | None:
        """@brief Project folder, or None while the project has no name."""
        try:
            return self._project_dir()
        except ValueError:
            return None

    def _project_dir(self) -> Path:
        """@brief Cartella del componente in area di lavoro.

        @return Percorso della cartella, che porta il nome scelto.
        @throws ValueError quando il componente non ha ancora un nome.
        @note Prima qui c'era un nome di ripiego, `NUOVO_COMPONENTE`. Un
            ripiego in un percorso non resta un percorso: prima o poi qualcuno
            ci scrive, e in area di lavoro compare una cartella che nessuno ha
            chiesto, accanto a quelle vere. E' successo con il diario
            dell'analisi. Meglio che non esista affatto un percorso da usare
            senza nome: chi ha bisogno della cartella prima che il nome ci sia
            deve accorgersene qui, non dopo, guardando l'area di lavoro.
        """
        name = self._component_name()
        if not name:
            raise ValueError(
                tx("Il componente non ha ancora un nome: nessuna cartella da usare."))
        return self._library_root() / name

    def _docs_dir(self) -> Path:
        """@brief Cartella dei documenti del componente.

        @throws ValueError quando il componente non ha ancora un nome.
        """
        return self._project_dir() / DOCS_SUBDIR

    def _update_project_path_label(self) -> None:
        # Senza nome non c'e' una cartella: mostrarne una inventata farebbe
        # credere che i documenti finiranno li'.
        self.label_project_path.setText(
            str(self._project_dir()) if self._component_name() else "")
        self._refresh_output_actions()
        self._refresh_docs_list()
        # La radice della libreria entra in `_project_dir()` esattamente come
        # il nome del componente: cambiandola cambiano il file del diario e la
        # cartella `photo-review/`, quindi i due comandi che dipendono da
        # quei file vanno ricalcolati qui. Prima solo `_on_name_changed` li
        # aggiornava, e spostare la cartella di lavoro lasciava «Log AI»
        # nascosto su un log che esisteva, oppure visibile su uno che non
        # c'era piu'. Guardia `hasattr` come in `_apply_component_name_gate`:
        # questo metodo gira anche durante la costruzione della finestra,
        # prima che i pulsanti esistano.
        if hasattr(self, "btn_ai_log"):
            self._refresh_ai_log_button()
        # Come in `_apply_component_name_gate`: durante un'analisi i comandi di
        # revisione restano spenti fino alla fine del worker, e ricalcolarli
        # qui li riaccenderebbe a meta' corsa.
        if hasattr(self, "btn_component_review") and not self._background_work_running():
            self._refresh_reopen_review_button()

    def _named(self) -> bool:
        """@brief Vero quando esiste una cartella del componente da guardare."""
        return bool(self._component_name())

    def _project_dir_text(self) -> str:
        """@brief La cartella del componente da mostrare, o un trattino.

        @note Serve ai riepiloghi, che si aggiornano anche prima che il nome
            esista: senza nome non c'e' una cartella, e scriverne una inventata
            sarebbe una promessa falsa su dove finiranno i file.
        """
        return str(self._project_dir()) if self._named() else "—"

    def _refresh_output_actions(self) -> bool:
        footprint_available = self._refresh_output_actions_inner()
        self._refresh_mounting_actions()
        self._refresh_workflow_state()
        return footprint_available

    def _workflow_facts(self) -> dict:
        """@brief Facts of the open project that decide the workflow steps.

        @return dict with name, documents, analysis, board, review, step (bools).
        @note Only facts, no guesses: files on disk and what happened in this
            session. A reopened project whose footprint exists has been
            analysed, even if not in this session.
        """
        name = self._component_name()
        facts = dict(name=bool(name), documents=False, analysis=False, board=False,
                     review=False, step=False)
        if not name:
            return facts
        project = self._project_dir()
        footprint = (project / f"{name}.pretty" / f"{name}.kicad_mod").is_file()
        facts["step"] = footprint and (project / f"{name}.3dshapes" / f"{name}.step").is_file()
        docs_list = getattr(self, "docs_list", None)
        facts["documents"] = bool(docs_list is not None and docs_list.count())
        facts["analysis"] = bool(getattr(self, "_acquisition_completed", False)) or footprint
        facts["board"] = self._photo_inventory_family() is True if self._selected_family() else False
        facts["review"] = facts["board"] and (
            bool(getattr(self, "_reviewed_this_extraction", False))
            or self._review_bundle() is not None)
        return facts

    def _refresh_workflow_state(self) -> None:
        """@brief Tick the done steps, highlight the next one, refresh the status bar."""
        bar = getattr(self, "workflow_bar", None)
        if bar is None:
            return
        facts = self._workflow_facts()
        done = {key for key, flag in (("documents", facts["documents"]),
                                      ("analysis", facts["analysis"]),
                                      ("review", facts["review"]),
                                      ("model3d", facts["step"])) if flag}
        skipped = {}
        if facts["name"] and not facts["board"]:
            skipped["review"] = "Solo per le schede con componenti."
        order = ("documents", "analysis", "review", "model3d", "kicad")
        current = (next((key for key in order if key not in done and key not in skipped), None)
                   if facts["name"] else None)
        bar.set_state(done, current, skipped)
        self._refresh_status_bar(facts["name"])

    def _on_costs_changed(self) -> None:
        """@brief Refresh the status bar cost after the cost file changed (Qt slot).

        @note Never raises: an exception leaving a slot aborts the application.
        """
        try:
            self._refresh_status_bar(bool(self._component_name()))
            overlay = getattr(self, "analysis_overlay", None)
            if (overlay is not None and overlay.isVisible()
                    and not self._generation_phase_active()):
                overlay.set_cost(self.status_cost.text())
                if self.status_cost.toolTip():
                    overlay.cost.setToolTip(self.status_cost.toolTip())
        except Exception as exc:  # noqa: BLE001 - informativo, mai fatale
            self._append_log(tx("Costo dell'analisi non aggiornato: {exc}", exc=exc))

    def _refresh_status_bar(self, named: bool) -> None:
        """@brief Show the open project and the AI cost of its current analysis in the status bar.

        @param named True when a project is open.
        @note Only the current analysis (user, 2026-09-28): from the last
            «Estrai parametri con AI», reruns included. The project total is
            in the tooltip.
        """
        label = getattr(self, "status_project", None)
        if label is None:
            return
        if not named:
            label.setText(self.i18n.text("Nessun progetto aperto"))
            self.status_cost.setText("")
            return
        label.setText(self.i18n.tr("status.project", name=self._component_name()))
        try:
            from ..services.project_costs import load_project_costs
            folder = self._project_dir()
            # La stessa analisi che mostra la riga «Credito AI» (audit 138, B4).
            account = getattr(self, "ai_account", None)
            costs = load_project_costs(folder, account.shown_run(folder) if account else None)
        except Exception:  # noqa: BLE001 - il costo e' informativo
            costs = None
        start_queued, waiting = (account.queued_for(folder) if costs is not None and account
                                 else (False, 0))
        if costs is not None and (start_queued or waiting):
            # Come la riga del credito (audit 139, revisione finale): con
            # chiamate ancora da scrivere il numero del file e' solo un minimo,
            # e con l'inizio dell'analisi in coda l'analisi mostrata dal file e'
            # ancora la precedente.
            from .ai_account import format_usd
            shown = 0.0 if start_queued else costs.run_usd
            self.status_cost.setText(self.i18n.tr(
                "status.analysis_cost_pending", total=format_usd(shown), waiting=waiting))
            self.status_cost.setToolTip(self.i18n.tr(
                "status.project_cost", total=format_usd(costs.total_usd), calls=costs.calls)
                + " (+?)")
            return
        if costs is None or not costs.calls:
            self.status_cost.setText("")
            return
        if costs.run:
            # Stesso formato della riga del credito: sotto 1 USD quattro
            # decimali, non «0.00» per un'analisi che costa millesimi (B4).
            from .ai_account import format_usd
            text = self.i18n.tr("status.analysis_cost", total=format_usd(costs.run_usd),
                                calls=costs.run_calls)
            self.status_cost.setText(text + ("" if costs.run_complete else " (+?)"))
        else:
            # Calls recorded before analyses were recorded: no current analysis.
            self.status_cost.setText(self.i18n.tr("status.analysis_cost_unknown"))
        from .ai_account import format_usd
        self.status_cost.setToolTip(
            self.i18n.tr("status.project_cost", total=format_usd(costs.total_usd), calls=costs.calls)
            + ("" if costs.complete else " (+?)"))

    def _on_workflow_step_clicked(self, step: str) -> None:
        """@brief Open the tab where a step is done, when the tab is available."""
        target = {"model3d": TAB_MODEL_3D, "kicad": TAB_KICAD}.get(step, TAB_PROJECT)
        if self.tabs.isEnabled() and self.tabs.isTabEnabled(target):
            self.tabs.setCurrentIndex(target)
            if target == TAB_PROJECT and hasattr(self, "project_wizard"):
                self.project_wizard.set_page(0 if step == "documents" else 1)
                if step == "review" and self.btn_component_review.isEnabled():
                    self._open_component_review()

    def _refresh_output_actions_inner(self) -> bool:
        name = self._component_name()
        if name:
            project = self._project_dir()
            footprint = project / f"{name}.pretty" / f"{name}.kicad_mod"
            step = project / f"{name}.3dshapes" / f"{name}.step"
            symbol = project / f"{name}.kicad_sym"
            footprint_available = footprint.is_file()
            complete_available = footprint_available and step.is_file()
        else:
            # Senza nome non c'e' una cartella, quindi niente da aprire ne' da
            # installare: nessun percorso da mostrare e nessun file da cercare.
            footprint = step = symbol = None
            footprint_available = complete_available = False
        for attr in ("btn_open_output", "action_open_output"):
            action = getattr(self, attr, None)
            if action is not None:
                action.setEnabled(footprint_available)
                reason = ("" if footprint_available else
                          "Genera prima il footprint del componente corrente")
                if isinstance(action, QWidget) and action.property("workflow_title_source"):
                    self._set_workflow_detail(
                        action, reason or "Apre la cartella del componente con footprint, "
                        "modello STEP e documenti.")
                else:
                    action.setToolTip(self.i18n.text(reason) if reason else "")
        for attr in (
                "btn_test_kicad", "btn_export", "btn_install_global",
                "action_test_kicad", "action_export", "action_install_global"):
            action = getattr(self, attr, None)
            if action is not None:
                action.setEnabled(complete_available)
                enabled_tooltips = {
                    "btn_test_kicad": "Apre un progetto temporaneo: non installa e non modifica librerie KiCad",
                    "btn_export": "Crea KiCadGen.pretty e KiCadGen.3dshapes nel progetto selezionato",
                    "btn_install_global": "Installa nella cartella scelta e registra la libreria globale di KiCad",
                }
                source = (enabled_tooltips.get(attr, "") if complete_available else
                          "Genera prima footprint e modello STEP del componente corrente")
                if isinstance(action, QWidget) and action.property("workflow_title_source"):
                    # Icona: prima riga il nome, poi la spiegazione o il motivo.
                    self._set_workflow_detail(action, source)
                    continue
                rendered = self.i18n.text(source) if source else ""
                if isinstance(action, QWidget):
                    action.setProperty("i18n_tooltip_source", source)
                    action.setProperty("i18n_tooltip_rendered", rendered)
                action.setToolTip(rendered)
        # Verification must remain reachable when a footprint lacks its STEP.
        self._refresh_kicad_tab(footprint_available)
        if hasattr(self, "group_metadata"):
            self.group_metadata.setEnabled(footprint_available)
            self._refresh_metadata_proposals()
        if hasattr(self, "tabs"):
            self._refresh_preview_tabs(footprint_available)
        if hasattr(self, "label_kicad_footprint"):
            step_available = step is not None and step.is_file()
            symbol_available = symbol is not None and symbol.is_file()
            self.label_kicad_footprint.setText(footprint.name if footprint_available else "—")
            self.label_kicad_footprint.setToolTip(str(footprint) if footprint_available else "")
            self.label_kicad_step.setText(step.name if step_available else "—")
            self.label_kicad_step.setToolTip(str(step) if step_available else "")
            self.label_kicad_symbol.setText(
                symbol.name if symbol_available else self.i18n.text("Da esportare"))
            self.label_kicad_symbol.setToolTip(
                str(symbol) if symbol is not None else self.i18n.text(
                    "Imposta il nome del componente per preparare il simbolo."))
            if hasattr(self, "label_kicad_footprint_path"):
                footprint_path_text = ("Percorso footprint: " + str(footprint)
                                        if footprint_available else "Percorso footprint: —")
                step_path_text = ("Percorso modello STEP: " + str(step)
                                  if step_available else "Percorso modello STEP: —")
                symbol_path_text = ("Percorso simbolo elettrico: " + str(symbol)
                                    if symbol_available else
                                    "Percorso simbolo elettrico previsto: " + str(symbol)
                                    if symbol is not None else
                                    "Percorso simbolo elettrico: —")
                self.label_kicad_footprint_path.setText(footprint_path_text)
                self.label_kicad_step_path.setText(step_path_text)
                self.label_kicad_symbol_path.setText(symbol_path_text)
            status = (self.i18n.text("Footprint e modello STEP pronti per la verifica.")
                      if complete_available else
                      self.i18n.text("Genera footprint e modello STEP per continuare."))
            if complete_available and _step_older_than_footprint(footprint, step):
                status += "\n" + self.i18n.text(
                    "ATTENZIONE: il modello STEP è più vecchio del footprint. Se dopo "
                    "l'ultimo 3D hai cambiato il corpo, rigenera il modello 3D prima di "
                    "installare.")
            if self.has_pending_footprint_edits():
                # Test, export and installation copy the .kicad_mod on disk,
                # which holds the APPLIED footprint (user, 2026-10-14).
                status += "\n" + self.i18n.text(
                    "ATTENZIONE: il footprint ha modifiche non applicate. Verifica, test, "
                    "esportazione e installazione usano il footprint applicato: usa Applica "
                    "nella scheda Footprint per includerle.")
            if getattr(self, "_fp_file_stale", False):
                status += "\n" + self.i18n.text(
                    "ATTENZIONE: il file .kicad_mod non contiene le ultime modifiche applicate "
                    "al footprint: usa «Aggiorna solo il footprint» prima di installare.")
            self.label_kicad_status.setText(status)
        return footprint_available

    #: Position of the «KiCad» tab in `self.tabs`.
    KICAD_TAB_INDEX = TAB_KICAD

    def _refresh_kicad_tab(self, complete: bool) -> None:
        """@brief A footprint enables verification even if STEP is missing.

        Installation and visual preview retain their own complete-file guards.
        """
        tabs = getattr(self, "tabs", None)
        index = self.KICAD_TAB_INDEX
        if tabs is None or tabs.count() <= index:
            return
        if not complete and tabs.currentIndex() == index:
            tabs.setCurrentIndex(0)
        tabs.setTabEnabled(index, complete)
        tabs.setTabToolTip(index, "" if complete else self.i18n.text(
            "Disponibile quando il progetto ha un footprint generato."))

    def _on_name_changed(self, text: str) -> None:
        """@brief Il nome e' la cartella: da qui in avanti il progetto aperto e' quella.

        @param text Nome nuovo.
        @note Solo il nome definisce `_open_project_folder`, non la cartella di
            lavoro: cambiare quella non sposta il progetto aperto, lo chiude
            (`_on_library_root_changed`).
        """
        if hasattr(self, "_open_project_folder"):
            previous = self._open_project_folder
            self._open_project_folder = self._project_dir() if self._component_name() else None
            self._remember_recent_project(previous)
        self._update_project_path_label()
        self._apply_component_name_gate()
        self._refresh_window_title()
        self._refresh_start_page()

    def _remember_recent_project(self, previous) -> None:
        """@brief Put the open project first among the recent projects.

        @param previous Project folder open before this name change, or None.
        @note Every way of opening a project passes through the name (new,
            open, recent, rename, name proposed by the analysis), so this is
            the one place that records it. A rename drops the old folder only
            when it no longer exists, instead of letting it hold a slot until
            the cap pushes it out.
        """
        folder = self._open_project_folder
        if folder is None:
            return
        if previous is not None and previous != folder and not previous.exists():
            recent_projects.remove(self._config, previous)
        recent_projects.remember(self._config, folder, save=save_config)

    def _refresh_start_page(self) -> None:
        """@brief Show the start page without an open project, the tabs with one."""
        stack = getattr(self, "central_stack", None)
        if stack is None:
            return
        self.start_page.set_library_root(self._library_root())
        self._refresh_open_project_commands()
        if self._component_name():
            stack.setCurrentWidget(self.tabs)
            return
        self.start_page.set_recent(recent_projects.load(self._config))
        stack.setCurrentWidget(self.start_page)

    def _projects_available(self) -> bool:
        """@brief True when the working folder holds at least one project folder."""
        from .project_dialogs import list_projects
        try:
            return bool(list_projects(self._library_root()))
        except OSError:
            return False

    def _refresh_open_project_commands(self) -> None:
        """@brief Enable every «Apri progetto…» only while the working folder holds a project.

        @note One condition for the three copies of the command (start page,
            File menu, icon at the top of the Progetto tab), so they never
            disagree (user, 2026-09-30, HANDOFF 170). Called when the working
            folder changes, when the start page is shown again and after a
            project is created or opened.
        """
        available = self._projects_available()
        start_page = getattr(self, "start_page", None)
        if start_page is not None:
            start_page.set_projects_available(available)
        action = getattr(self, "action_open_project", None)
        if action is not None:
            action.setEnabled(available)
            action.setStatusTip("" if available else self.i18n.text(NO_PROJECTS_TOOLTIP))
        button = getattr(self, "btn_open_project_top", None)
        if button is not None:
            button.setEnabled(available)

    def _forget_recent_project(self, path: str) -> None:
        """@brief «Rimuovi dall'elenco»: drop a recent project; its folder is not touched.

        @param path Absolute project folder.
        """
        recent_projects.forget(self._config, path, save=save_config)
        self._refresh_start_page()

    def _fill_recent_menu(self) -> None:
        """@brief Fill File > «Progetti recenti» with the projects that still exist.

        @note Two projects with the same name in different working folders
            also show their parent folder; the tooltip always has the full path.
        """
        menu = self.menu_recent
        menu.clear()
        paths = recent_projects.load(self._config)
        names = [Path(path).name for path in paths]
        for path, name in zip(paths, names):
            text = name if names.count(name) == 1 else f"{name} \u2014 {Path(path).parent}"
            action = menu.addAction(text)
            action.setToolTip(path)
            action.setStatusTip(path)
            action.triggered.connect(lambda _checked=False, target=path:
                                     self._open_recent_project(target))
        if not paths:
            menu.addAction(self.i18n.text("Nessun progetto recente")).setEnabled(False)

    def _open_recent_project(self, path: str) -> None:
        """@brief Reopen a recent project, moving to its working folder if needed.

        @param path Absolute project folder.
        @note `_project_dir()` is always «working folder / name», so a project
            of another working folder is opened by moving the working folder
            to its parent (for this session, like the folder button); that
            change saves and closes the project open before, as always.
        """
        if self._project_change_blocked():
            return
        folder = Path(path)
        if not recent_projects.is_project(folder):
            # Elencato un attimo fa ma sparito (disco scollegato, cartella
            # tolta): resta nell'elenco, come fa `load`, e si spiega il motivo.
            QMessageBox.information(self, self.i18n.text("Progetti recenti"),
                                    tx("Il progetto {path} non e' raggiungibile.", path=folder))
            self._refresh_start_page()
            return
        if getattr(self, "_open_project_folder", None) == folder:
            self._refresh_start_page()
            return
        if not self._can_leave_project():
            return
        if folder.parent != self._library_root():
            previous = self.field_library_root.blockSignals(True)
            self.field_library_root.setText(str(folder.parent))
            self.field_library_root.blockSignals(previous)
            self._update_project_path_label()
            self._switch_project("", self._general_project_state)
        self._open_project_at(folder, save_current=False)

    def _refresh_window_title(self) -> None:
        """@brief Show the open project and the product version in the title bar.

        @note «<progetto> — KiCadGen <versione>», or «KiCadGen <versione>»
            without a project (PIANO-FIX, identity): the same product name as
            the installer and the About window; the name is a proper noun and
            is not translated.
        """
        from .. import __version__
        name = self._component_name()
        product = f"KiCadGen {__version__}"
        self.setWindowTitle(f"{name} \u2014 {product}" if name else product)

    def _apply_component_name_gate(self) -> None:
        """@brief Senza nome non si carica nulla e non si estrae nulla.

        @note Il nome non e' un'etichetta: e' la cartella in cui finiscono i
            documenti e il nome con cui nascono footprint, simbolo e modello
            3D. Lasciar caricare le immagini prima vorrebbe dire scriverle in
            una cartella scelta dal programma, e poi doverle spostare quando il
            nome arriva -- che e' esattamente il pasticcio che questo blocco
            evita. Meglio un pulsante spento con scritto perche'.
        """
        named = bool(self._component_name())
        reason = "" if named else "Crea o apri prima un progetto (menu File)."
        # Cambiare nome mentre un worker gira non deve riaccendere i pulsanti
        # che lanciano un'altra analisi: li riabilita la fine del worker.
        busy = self._background_work_running()
        for attr in ("guided_docs", "docs_list", "btn_extract_ai"):
            widget = getattr(self, attr, None)
            if widget is None:
                continue
            if attr == "btn_extract_ai":
                blocked = self._extract_block_reason()
                widget.setEnabled(not blocked and not busy)
                # Stesso testo del costruttore, letterale in entrambi i posti:
                # e' cosi' che il controllo del catalogo (tests/test_i18n.py)
                # lo vede. Se cambia qui deve cambiare la' -- il costo
                # dichiarato e' la parte che non puo' divergere.
                # Tradotti qui E ricordati come sorgente: scritti in italiano
                # restavano tali in en/fr/de fino al cambio di lingua
                # successivo (revisore 3, difetto 10).
                self._set_translated_tooltip(
                    widget, blocked or self._extract_button_sources()[1])
            else:
                widget.setEnabled(named)
                self._set_translated_tooltip(widget, reason)
        # Il nome decide anche la cartella in cui cercare le proposte salvate:
        # cambiato il nome, i pulsanti della revisione vanno ricalcolati --
        # ma non durante un'analisi, dove restano spenti fino alla fine.
        if hasattr(self, "btn_component_review") and not busy:
            self._refresh_reopen_review_button()
        # Il nome decide anche la cartella del diario: cambiandolo, il log
        # apribile e' un altro (o non c'e').
        if hasattr(self, "btn_ai_log"):
            self._refresh_ai_log_button()

    def _extract_block_reason(self) -> str:
        """@brief Why the analysis cannot start now, or "" when it can.

        @return Italian catalogue source of the reason.
        @note Without a component type nothing starts (user's decision,
            2026-09-26): the type decides whether the photograph is inventoried
            as a populated board or the documents are read as a single part,
            and which body fields the footprint and the 3D use. Left
            «(non specificata)», the model guessed (a board read as a pin
            header, no calibrated photo, no body review). A custom type
            (non-empty key) is a real choice and is accepted.
        """
        if not self._component_name():
            return "Crea o apri prima un progetto (menu File)."
        combo = getattr(self, "combo_component_type", None)
        if combo is not None and not (combo.currentData() or ""):
            return ("Scegli prima la tipologia del componente: decide come vengono "
                    "analizzati documenti e foto.")
        return ""

    def _refresh_extract_enabled(self) -> None:
        """@brief Re-enable the analysis button only when nothing blocks it."""
        self.btn_extract_ai.setEnabled(not self._extract_block_reason())

    def _browse_library_root(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, self.i18n.text("Scegli la cartella libreria"),
            self.field_library_root.text()
        )
        if path:
            self.field_library_root.setText(path)
            # Explicit: a chosen folder is a decision even when the text
            # signal was already handled (it returns early then).
            self._on_library_root_changed()

    def _body_size(self):
        """Quote correnti del corpo, per giudicare le viste dall'alto.

        None finche' non sono note: senza di esse non si puo' dire se una
        foto sia calibrabile, e dichiararlo comunque sarebbe una supposizione.
        """
        if not getattr(self, "_board_size_known", False):
            return None
        try:
            length = (self.field_cast_body_length.value() if self._is_castellated()
                      else self.field_body_length.value())
            width = (self.field_cast_body_width.value() if self._is_castellated()
                     else self.field_body_width.value())
        except AttributeError:
            return None
        return (length, width) if length > 0 and width > 0 else None

    def _ask_board_size(self):
        """@brief Ask the board size when it is not known yet (pretest without analysis).

        @return (length, width) in mm, or None when the user cancels.
        @note The values go into the body fields of the current type and the
            size becomes known for this project: the same fields the analysis
            fills, so a later analysis simply replaces them.
        """
        from PyQt6.QtWidgets import QDialogButtonBox, QDoubleSpinBox
        dialog = QDialog(self)
        dialog.setWindowTitle(self.i18n.text("Quote della scheda"))
        form = QFormLayout(dialog)
        note = QLabel(self.i18n.text(
            "Il pretest non legge i documenti: servono le quote della scheda per ritagliare "
            "e calibrare la foto. La lunghezza e' lungo l'asse X della foto."))
        note.setWordWrap(True)
        form.addRow(note)
        fields = []
        for label in ("Lunghezza (mm)", "Larghezza (mm)"):
            field = QDoubleSpinBox()
            # Stessi limiti fisici dei campi che ricevono i valori.
            field.setRange(MIN_LENGTH_MM, MAX_LENGTH_MM)
            field.setDecimals(2)
            field.setValue(MIN_LENGTH_MM)
            form.addRow(self.i18n.text(label), field)
            fields.append(field)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        self._board_size_dialog = dialog
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        length, width = (field.value() for field in fields)
        if self._is_castellated():
            self.field_cast_body_length.setValue(length)
            self.field_cast_body_width.setValue(width)
        else:
            self.field_body_length.setValue(length)
            self.field_body_width.setValue(width)
        self._board_size_known = True
        if hasattr(self, "guided_docs"):
            self.guided_docs.refresh()
        return length, width

    def _refresh_docs_list(self) -> None:
        if not hasattr(self, "docs_list"):
            return
        self.docs_list.clear()
        if not self._named():
            # Senza nome non c'e' una cartella da leggere: la lista resta vuota
            # e il riquadro guidato non va interrogato, altrimenti chiederebbe
            # un percorso che non esiste.
            return
        docs_dir = self._docs_dir()
        if docs_dir.is_dir():
            for f in sorted(docs_dir.iterdir()):
                if f.is_file() and f.suffix.lower() in SUFFIXES:
                    item = QListWidgetItem(f.name)
                    item.setFlags(
                        item.flags() | Qt.ItemFlag.ItemIsUserCheckable
                    )
                    # La documentazione appena caricata deve essere inclusa
                    # nell'estrazione senza richiedere anche una selezione
                    # temporanea della riga.
                    item.setCheckState(Qt.CheckState.Checked)
                    self.docs_list.addItem(item)
        if hasattr(self, 'guided_docs'):
            self.guided_docs.refresh()
            if hasattr(self, 'i18n'):
                self.i18n.retranslate_widgets(self.guided_docs)
        # Il passo «Documenti» segue l'elenco appena riletto.
        self._refresh_workflow_state()

    def _selected_documents(self):
        return DocumentSet(self._docs_dir()).selected()

    def _extract_with_ai(self) -> None:
        """@brief «Estrai parametri con AI»: show at once that the click was taken, then start.

        @note The checks before the progress modal (YOLO and SMD network
            loaded on this thread) take several seconds (~7 s measured on
            2026-10-02), and an unchanged button invited a second click. The
            button is switched to «Preparazione…» with an hourglass and
            disabled before them; if the run does not start, it goes back
            to rest (see _finish_extract_preparation).
        """
        if (getattr(self, "_extract_preparing", False) or self._background_work_running()
                or self._automatic_generation_active):
            return
        # The analysis ends in a generation: a pending footprint draft is
        # applied or discarded first, or the run does not start.
        if not self._settle_footprint_draft_before_start():
            return
        self._extract_preparing = True
        try:
            self._extract_with_ai_prepared()
        finally:
            self._finish_extract_preparation()

    def _show_extract_preparing(self) -> None:
        """@brief Disable the extraction button and show «Preparazione…» with an hourglass, painted now."""
        button = self.btn_extract_ai
        button.setEnabled(False)
        button.setIcon(QIcon(str(Path(__file__).resolve().parent / "assets" / "wf-loading.svg")))
        self._set_extract_button_text("Preparazione…")
        self._keep_extract_button_visible()
        button.repaint()
        # Ridisegno subito, senza consegnare clic o tasti: quelli arrivati
        # durante la preparazione non devono far ripartire nulla.
        QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)

    def _restore_extract_button_icon(self) -> None:
        """@brief Put the idle icon back on the extraction button."""
        button = getattr(self, "btn_extract_ai", None)
        if button is not None:
            button.setIcon(QIcon(str(Path(__file__).resolve().parent / "assets" / "extract-ai.svg")))

    def _finish_extract_preparation(self) -> None:
        """@brief End the preparation: a started run keeps the button, an early exit restores it.

        @note Re-enabled a moment later, not at once: clicks queued while the
            interface was busy are delivered first and find it still disabled.
        """
        self._extract_preparing = False
        if getattr(self, "_extraction_busy", False) or self._background_work_running():
            return
        self._restore_extract_button_icon()
        self._refresh_extract_button()
        QTimer.singleShot(EXTRACT_REENABLE_DELAY_MS, self._reenable_extract_button)

    def _reenable_extract_button(self) -> None:
        """Re-enable the button after an early exit, unless a run has started meanwhile."""
        if (getattr(self, "_extraction_busy", False) or self._background_work_running()
                or getattr(self, "_extract_preparing", False)):
            return
        self.btn_extract_ai.setEnabled(bool(self._component_name()))

    def _extract_with_ai_prepared(self) -> None:
        """Body of «Estrai parametri con AI» once the button shows the preparation."""
        blocked = self._extract_block_reason()
        if blocked and self._component_name():
            QMessageBox.information(self, self.i18n.text("Tipologia componente"),
                                    self.i18n.text(blocked))
            return
        if not self._component_name():
            # I pulsanti sono gia' spenti; questo vale per le scorciatoie e per
            # chi arriva qui da un'automazione. Senza nome i documenti non
            # hanno una cartella e cio' che verrebbe generato non ha un nome.
            QMessageBox.information(
                self, self.i18n.text("Nome componente"),
                self.i18n.text("Crea o apri prima un progetto dal menu File: il suo nome e' "
                               "quello della cartella dei documenti, del footprint e del modello 3D."))
            return

        self._show_extract_preparing()
        # Single packages are analyzed from their own documents. Board-photo
        # inventory and its detector models do not participate in this path.
        chosen_detector, yolo_model_path = self._photo_detector_options()
        photo_detector = chosen_detector
        if self._is_single_component_project():
            # The chosen detector is kept apart so the log below can still
            # tell the user that it will not run (test contract of 2026-10-02).
            photo_detector, yolo_model_path = "ai", None
        if photo_detector == "yolo_smd" and is_frozen():
            self.group_ai_tools.setExpanded(True)
            QMessageBox.warning(
                self, self._photo_detector_text("error_title"),
                self._photo_detector_text("smd_unavailable"),
            )
            return
        if photo_detector in ("yolo", "yolo_only", "yolo_smd"):
            try:
                # Optional runtime stays lazy: AI-only sessions do not require it.
                # Validate before paid requests or clearing any previous results.
                from ..services.photo_yolo import validate_yolo_setup
                validate_yolo_setup(yolo_model_path)
                if photo_detector == "yolo_smd":
                    from ..services.smd_component_classifier import validate_smd_setup
                    validate_smd_setup(self._smd_model_option())
            except Exception as exc:
                self.group_ai_tools.setExpanded(True)
                QMessageBox.warning(
                    self, self._photo_detector_text("error_title"),
                    self._photo_detector_text("error", error=str(exc)),
                )
                return

        try:
            doc_paths, document_roles = self._selected_documents()
        except (OSError, ValueError) as exc:
            self.project_wizard.set_page(0)
            QMessageBox.information(
                self, self.i18n.text("Completa la documentazione"), str(exc)
            )
            return

        if photo_detector == "yolo_only":
            # Pretest (utente, 2026-09-25): solo i riquadri di YOLO sulla
            # foto, nessuna chiamata AI, nessun campo modificato.
            self._start_yolo_pretest(doc_paths, document_roles, yolo_model_path)
            return
        if not self._ensure_api_key():
            self.group_ai_tools.setExpanded(True)
            return

        try:
            profile = self.profile_store.snapshot(self._profile_key())
        except (ProfileError, sqlite3.Error, OSError) as exc:
            self.group_ai_tools.setExpanded(True)
            QMessageBox.warning(
                self, self.i18n.text('Profilo AI non disponibile'), str(exc))
            return

        # Solo «Estrai parametri con AI» apre un'analisi nuova per il costo
        # mostrato (utente, 28/09): _begin_ai_run serve anche al «Rilancio
        # inventario», che si somma all'analisi in corso (audit 138, B1).
        if hasattr(self, "ai_account"):
            self.ai_account.start_analysis(self._project_dir_or_none())
        self._begin_ai_run("Analisi AI")
        # Da qui in poi lo stato "analisi in corso" e' gia' esposto all'utente:
        # qualunque eccezione prima di worker.start() deve ripristinarlo, non
        # lasciare i tab spenti e la modale ferma.
        try:
            self._start_extract_worker(doc_paths, document_roles, profile,
                                       photo_detector, yolo_model_path,
                                       chosen_detector=chosen_detector)
        except Exception as exc:  # noqa: BLE001 - qualunque guasto riporta la UI in quiete
            self._on_extract_finished()
            self._on_extract_error(str(exc))

    def _ensure_api_key(self) -> bool:
        """@brief Before a paid AI run: True when an OpenRouter key is configured.

        @return False when there is none; the user is offered «Account AI...»
            and the run does not start either way (a key saved there is used
            by the next run, which the user starts again).
        @note Checked on the GUI thread before any worker or modal starts:
            the worker would fail with the same explanation, but only after
            the progress window had opened. `openrouter.api_key` reads no
            network (environment, Credential Manager, `.env`).
        """
        from ..services import openrouter
        from ..services.providers import (PROVIDER_DEEPSEEK, ProviderKeyMissing,
                                          deepseek_api_key, provider_of)
        models = (self._effective_model(), self._vision_model())
        if any(provider_of(model) == PROVIDER_DEEPSEEK for model in models):
            try:
                deepseek_api_key()
            except ProviderKeyMissing as exc:
                QMessageBox.warning(self, self.i18n.text("Account AI"), str(exc))
                return False
            if all(provider_of(model) == PROVIDER_DEEPSEEK for model in models):
                return True
        try:
            openrouter.api_key()
        except openrouter.MissingApiKeyError:
            answer = QMessageBox.question(
                self, self.i18n.text("Account AI"),
                self.i18n.text("Nessuna chiave OpenRouter configurata. Aprire «Account AI...» "
                               "per inserirla?"))
            if answer == QMessageBox.StandardButton.Yes:
                self._open_ai_account()
            return False
        return True

    def _open_ai_account(self) -> None:
        """@brief «Account AI...»: every provider's key and balance; paste, verify, save or remove.

        @note The dialog translates itself through this window's `i18n`; it
            opens on the provider of the model in use and reads the balances
            in the background (free). A saved or removed key refreshes the
            balance line at once.
        """
        from .ai_account_dialog import AiAccountDialog
        dialog = AiAccountDialog(self, read_balances=True)
        dialog.select_provider(self._balance_provider())
        dialog.key_changed.connect(self.ai_account.refresh_balance)
        self._ai_account_dialog = dialog
        dialog.exec()

    def _start_extract_worker(self, doc_paths, document_roles, profile,
                              photo_detector, yolo_model_path,
                              chosen_detector: str | None = None) -> None:
        """@brief Configure and start the full extraction worker.

        @param doc_paths Selected document/photo paths.
        @param document_roles Role of each selected document.
        @param profile AI profile snapshot (prompts and model).
        @param photo_detector Photo detector this run will use.
        @param chosen_detector Detector chosen in the settings when it differs
            from `photo_detector` (a single component forces "ai"): the log
            then says that the chosen detector will not run.
        @param yolo_model_path Optional YOLO weights path.
        @throws Exception Anything raised by the worker set-up or start; the
            caller restores the idle UI state.
        """
        self._extraction_started_stamp = time.strftime('%Y%m%d-%H%M%S')
        # Una quota della scheda precedente non vale per quella nuova.
        self._declared_row_spacing = None
        # Ogni estrazione nuova riparte senza revisione registrata e senza base
        # sintetizzata: questi flag riguardano solo l'analisi che sta partendo.
        self._reviewed_this_extraction = False
        self._relaunch_base_synthesized = False
        self._last_extracted = None
        # Le foto si azzerano PRIMA di ricalcolare i pulsanti: la modifica
        # della revisione dipende dalla foto in memoria, e spegnerla mentre la
        # vecchia foto e' ancora li' la lascerebbe accesa su dati appena
        # invalidati.
        self._photo_png = None
        self._photo_body_fields = None
        self._photo_overlay_png = None
        self._photo_source = ""
        self._set_review_buttons_enabled(False)

        model = self._session_model or profile['model']
        photo_model = model
        # E' questo modello a guardare la fotografia: con 'vlm' rileva ed
        # elenca i corpi, con 'yolo' riconcilia le proposte locali. In
        # entrambi i casi e' una chiamata sola, e non e' detto che debba
        # costare quanto il modello che legge i datasheet.
        verification_model = (self._photo_detector_model()
                              if photo_detector in ('yolo', 'vlm') else model)
        self._append_log(
            tx("Estrazione da {n_doc_paths} file con il modello AI {model}",
               n_doc_paths=len(doc_paths),
               model=model)
            + (tx(" (solo questa sessione).") if self._session_model else ".")
        )
        self._on_ai_progress(tx("Profilo AI: {currentText}.",
                                currentText=self.combo_component_type.currentText()))
        self._on_ai_progress(tx("Modello AI unico: {model}.", model=model))
        try:
            ignored = self.profile_store.ignored_model_overrides(self._profile_key())
        except (ProfileError, sqlite3.Error, OSError):
            ignored = {}
        if ignored:
            # Modello unico (utente, 2026-10-02): un modello salvato su questa
            # tipologia prima della modifica non vale piu', e va detto.
            self._on_ai_progress(tx(
                "La tipologia aveva un modello suo ({models}): ignorato, il modello e' unico "
                "e si sceglie nel tab «Modello» delle impostazioni LLM.",
                models=", ".join(sorted(set(ignored.values())))))
        if ((chosen_detector or photo_detector) in ('yolo', 'vlm', 'yolo_smd')
                and self._photo_inventory_family() is False):
            # Difetto visto dall'utente: con «YOLO locale + verifica AI» e una
            # tipologia da COMPONENTE SINGOLO il rilevatore non parte, la
            # revisione dei corpi rilevati non si apre e nessun corpo entra
            # nella tabella -- perche' l'estrattore guarda la fotografia solo
            # per le schede popolate (vedi `_photo_inventory_family`). Prima si
            # annunciava lo stesso «chiamata sulla fotografia affidata a …»:
            # una fase promessa che non sarebbe mai avvenuta, e chi aspettava
            # la pausa la aspettava per sempre. Qui si dice cosa succede
            # davvero, senza bloccare nulla: la scelta della tipologia resta
            # dell'utente, e l'estrazione dai documenti ha comunque senso.
            self._on_ai_progress(
                tx("Tipologia «{currentText}»: l'analisi NON passa dall'inventario "
                   "fotografico, quindi il rilevatore dei corpi e la revisione «Corpi "
                   "rilevati» non verranno eseguiti e nessuna chiamata guardera' la "
                   "fotografia. I corpi dalla foto si ottengono con le tipologie di "
                   "scheda popolata (aggregato a fori passanti o modulo castellated).",
                   currentText=self.combo_component_type.currentText()))
        elif photo_detector == 'yolo_smd':
            self._on_ai_progress(tx("Inventario fotografico con YOLO e rete SMD v3 locali; "
                                    "solo i corpi che la rete non riconosce passano alla "
                                    "riverifica AI dei ritagli. L'estrazione dai documenti "
                                    "usa il modello AI configurato."))
        elif photo_detector in ('yolo', 'vlm'):
            # Detto sempre, non solo quando differisce dal modello principale:
            # e' la chiamata a pagamento che guarda la fotografia, e va scritta
            # nel log prima di partire, non dedotta dopo dalla fattura.
            self._on_ai_progress(
                f"Chiamata sulla fotografia affidata a {verification_model}"
                + (tx(" (scelto per questa sessione).") if self._vlm_model else tx(" (da impostazioni).")))
        elif verification_model != model:
            self._on_ai_progress(tx("Rilevamento dei corpi affidato a {verification_model}.",
                                    verification_model=verification_model))

        self._extract_worker = ExtractWorker(
            doc_paths,
            model=model,
            vision_model=photo_model, verification_model=verification_model,
            grounding_prompt=profile['grounding_prompt'],
            verification_prompt=profile['verification_prompt'],
            terminal_prompt=profile['terminal_prompt'],
            crop_prompt=profile['crop_prompt'],
            crop_model=profile['crop_model'],
            document_roles=document_roles,
            component_family=self._selected_family(),
            photo_detector=photo_detector,
            yolo_model_path=yolo_model_path,
            smd_model_path=self._smd_model_option() if photo_detector == "yolo_smd" else None,
            # Stessi parametri del pretest (utente, 2026-09-25: «la scelta dei
            # parametri rimane quando scelgo la modalità YOLO + AI»).
            yolo_settings=(self._chosen_yolo_settings() if photo_detector in ("yolo", "yolo_smd") else None),
            # The datasheet prompt follows the family of the type: one part reads
            # its drawing pad by pad, a board reads its contacts and its photo.
            system_prompt=(profile.get('system_prompt_single') or None
                           if self._selected_family() in _SINGLE_COMPONENT_FAMILIES
                           else profile['system_prompt']),
            numbering_prompt=profile.get('numbering_prompt') or None,
            extra_instructions=self.field_component_prompt.toPlainText().strip() or None,
            include_nc_pins=self._include_nc_pins(),
        )
        # Risposte in cache ed evidenze nella cartella del progetto (utente,
        # 2026-09-24). Qui il nome c'e' sempre: senza nome non si estrae.
        self._extract_worker.photo_cache_dir = self._photo_cache_dir()
        self._extract_worker.done.connect(self._on_extracted)
        self._extract_worker.error.connect(self._on_extract_error)
        self._extract_worker.progress.connect(self._on_ai_progress)
        self._extract_worker.phase.connect(self._on_phase)
        self._extract_worker.review_requested.connect(self._on_review_requested)
        self._extract_worker.finished.connect(self._on_extract_finished)
        # Marchio della corsa: da qui in poi il risultato di questo worker vale
        # solo finche' la finestra non dichiara scaduta la corsa (interruzione).
        self._extract_worker._analysis_revision = self._analysis_revision
        # A new run has applied nothing yet (see `_on_extract_finished`).
        self._extraction_applied_revision = None
        self._analysis_tabs_enabled = self.tabs.isEnabled()
        self.tabs.setEnabled(False)
        self.analysis_overlay.start(abortable=True)
        # Il rilevatore di QUESTA estrazione: i pacchetti di ripiego scritti
        # dopo lo devono ricordare (audit 2026-10-02: tornava 'yolo' e il
        # rilancio pagava l'inventario globale). Solo quando la corsa parte
        # davvero: un clic fermato dai controlli non deve cambiare quello
        # della corsa precedente (verifica avversariale B6).
        self._extraction_detector = photo_detector
        self._extract_worker.start()

    #: Estensioni delle foto che l'inventario prova come vista dall'alto
    #: (stesso filtro di `photo_inventory.extract_inventory`).
    _PRETEST_PHOTO_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif")

    @classmethod
    def _pretest_top_views(cls, doc_paths, document_roles) -> list:
        """@brief The photographs the pretest would try as top view.

        @param doc_paths Selected documents.
        @param document_roles Roles by file name, or None when not assigned.
        @return Paths of the images declared as a photograph (role
            `document_roles.PHOTO_ROLE`, among the roles of the file); with no
            roles at all, every image (the extractor then picks one
            heuristically and says so).
        @note Same selection as YoloPretestWorker._top_views and the
            extractor: with roles assigned but none of them a top view the
            extractor stops at «Nessuna vista dall'alto indicata», so the
            window asks for the role BEFORE starting (audit 25/09, B9).
        """
        images = [Path(path) for path in doc_paths
                  if Path(path).suffix.lower() in cls._PRETEST_PHOTO_SUFFIXES]
        if document_roles is None:
            return images
        # Stessa scelta del worker e dell'estrattore (`photo_document_indices`
        # quando c'e'): solo i file con il ruolo foto.
        from ..services import datasheet_extractor
        indices = getattr(datasheet_extractor, "photo_document_indices", None)
        from ..services.document_roles import photo_paths
        chosen = set(photo_paths(doc_paths, document_roles))
        if callable(indices):
            paths = [Path(path) for path in doc_paths]
            try:
                chosen = {paths[i] for i in (indices(paths, document_roles) or []) if i >= 0}
            except Exception:  # noqa: BLE001 - ExtractionError non e' un ValueError
                # Ruoli che l'estrattore rifiuterebbe: lo dira' l'analisi; qui
                # vale la lettura diretta dei ruoli, mai un'eccezione da uno slot.
                pass
        return [path for path in images if path in chosen]

    def _start_yolo_pretest(self, doc_paths, document_roles, yolo_model_path) -> None:
        """@brief Run only the local YOLO detector on the top view and show its boxes.

        @param doc_paths Selected documents; only the top views are used.
        @param document_roles Roles of the documents (None when not assigned).
        @param yolo_model_path Session weights path, or None for the default.
        @note No AI call of any kind (user, 2026-09-25: «un pretest di come
            vengono individuati i componenti senza fare altro»). The board
            extents come from the «Lunghezza/Larghezza corpo» fields: without
            the document analysis there is no other source, and the photo
            calibration needs them. They are read through `_body_size()`, the
            same source the document cards use to say «calibrabile, N px/mm»:
            the castellated fields for a castellated type, the matrix body
            fields otherwise. Reading the matrix fields for every type made a
            castellated board fail on their 10 x 10 mm defaults while its
            card reported the photo as usable (user, 2026-09-25). Fields, component table and 3D are not
            touched; the boxes kept in the review are saved in
            `<project>/yolo-pretest/photo-review/` and the detector evidence
            in `<project>/yolo-pretest/photo-cache/`, apart from the
            extraction's (audit 25/09, B2): the paid relaunch never picks
            them up, and «Riapri revisione» reopens them for free while
            «Solo YOLO locale» is selected.
        @note A missing top view is reported before anything starts (B9).
            Everything after the first visible state change runs under the
            same try/except as `_extract_with_ai`: an exception escaping a
            Qt slot aborts PyQt6, and a half-started pretest would leave the
            button off and the tabs disabled (B8).
        """
        size = self._body_size() or self._ask_board_size()
        if size is None:
            return
        if not self._pretest_top_views(doc_paths, document_roles):
            QMessageBox.information(self, self.i18n.text("Pretest YOLO"),
                                    self.i18n.text("Il pretest YOLO lavora sulla foto vista dall'alto: spunta «Foto» sulla fotografia della scheda nei documenti e riprova."))
            return
        length, width = (float(value) for value in size)
        self.btn_extract_ai.setEnabled(False)
        self._extraction_busy = True
        try:
            self._start_log_session("Pretest YOLO")
            self.ai_progress_text.clear()
            self.ai_elements_list.clear()
            # Il consuntivo dell'ultima analisi a pagamento non riguarda il
            # pretest, che non spende nulla (audit 25/09, B4).
            self.analysis_overlay.clear_cost()
            self._on_ai_progress(
                tx("Pretest solo YOLO locale: nessuna chiamata AI. Quote della scheda dai campi "
                   "del corpo: {length:.2f} x {width:.2f} mm.", length=length, width=width))
            from ..services.photo_inventory import photo_cache_dir
            worker = YoloPretestWorker(doc_paths, document_roles, length, width,
                                       yolo_model_path, photo_cache_dir(self._pretest_root()),
                                       yolo_settings=self._chosen_yolo_settings())
            self._extract_worker = worker
            worker.done.connect(self._on_yolo_pretest_done)
            worker.error.connect(self._on_yolo_pretest_error)
            worker.progress.connect(self._on_ai_progress)
            worker.phase.connect(self._on_phase)
            worker.review_requested.connect(self._on_review_requested)
            worker.finished.connect(self._on_yolo_pretest_finished)
            worker._analysis_revision = self._analysis_revision
            self._analysis_tabs_enabled = self.tabs.isEnabled()
            self.tabs.setEnabled(False)
            # Titolo proprio: «Analisi AI in corso» direbbe il falso (nessuna AI).
            self.analysis_overlay.start("Pretest YOLO in corso",
                                        "Rilevamento dei componenti sulla foto…", abortable=True)
            worker.start()
        except Exception as exc:  # noqa: BLE001 - qualunque guasto riporta la UI in quiete
            self._on_yolo_pretest_finished()
            self._on_yolo_pretest_error(str(exc))

    def _on_yolo_pretest_done(self, report) -> None:
        """@brief Report what the pretest found.

        @param report InventoryResult of the yolo_only path, or None when the
            review was cancelled.
        @note When nothing was kept, the popup explains why with the report's
            warnings. The hint about the board extents is added only when a
            photograph was tried and none could be calibrated: when the
            photograph was calibrated (YOLO simply found nothing) or no top
            view could be tried, the extents are not the cause (B9).
        """
        if not self._analysis_result_is_current():
            return
        if report is None:
            self._on_ai_progress(tx("Pretest YOLO annullato nella revisione: nulla salvato."))
            return
        found = len(report.components)
        self._on_ai_progress(tx("Pretest YOLO concluso: {found} riquadri tenuti; nessuna chiamata AI.",
                                found=found))
        if not found and report.warnings:
            worker = self.sender()
            tried = bool(worker is None or self._pretest_top_views(
                getattr(worker, "doc_paths", ()), getattr(worker, "document_roles", None)))
            message = "\n".join(report.warnings)
            if tried and report.photo_png is None:
                message += "\n\n" + self.i18n.text(
                    'Se la foto non si calibra, controlla Lunghezza corpo e Larghezza corpo nei parametri della tipologia scelta: il pretest non legge i documenti e usa quei valori come quote della scheda.')
            QMessageBox.information(self, self.i18n.text("Pretest YOLO"), message)

    def _on_yolo_pretest_error(self, message: str) -> None:
        """@brief Say that the pretest failed, in pretest words, and give the window back.

        @param message Error text from the worker.
        @note Dedicated to the pretest (audit 25/09, B5): `_on_extract_error`
            speaks of a failed «Estrazione», which never started. The error
            of a pretest abandoned with «Interrompi» goes to the log only.
        """
        if not self._analysis_result_is_current():
            self._append_log(tx("Errore di un pretest YOLO interrotto dall'utente: {message}",
                                message=message))
            return
        self._hide_analysis_overlay()
        self._on_ai_progress(tx("Pretest YOLO non riuscito: {message}", message=message))
        QMessageBox.warning(self, self.i18n.text("Pretest YOLO non riuscito"), message)

    def _on_yolo_pretest_finished(self) -> None:
        """@brief Give the window back after the pretest, with no cost to report.

        @note A pretest abandoned with «Interrompi» already gave the window
            back; its late end only releases the worker and, as a safety net,
            closes a modal that no remaining operation owns (B1).
        """
        if not self._analysis_result_is_current():
            self._forget_abandoned_worker(self.sender())
            self._release_orphan_overlay()
            self._apply_component_name_gate()
            self._refresh_reopen_review_button()
            return
        self._hide_analysis_overlay()
        self._refresh_extract_enabled()
        self._end_extraction_busy()
        self._apply_component_name_gate()
        self._refresh_reopen_review_button()

    def _end_extraction_busy(self) -> None:
        """@brief Clear the run flag and bring the detector controls and the button back to rest.

        @note The flag, not QThread.isRunning, tells `_on_photo_detector_changed`
            that a run owns the button (B6): the run outlives the extraction
            thread (automatic generation) and ends before an abandoned one,
            see `_extraction_busy` in `__init__`.
        """
        self._extraction_busy = False
        self._restore_extract_button_icon()
        if hasattr(self, "combo_photo_detector"):
            self._on_photo_detector_changed()
        else:
            self._refresh_extract_button()

    def _release_orphan_overlay(self) -> None:
        """@brief Hide the modal when no operation that could own it is left.

        @note Safety net for the late end of an abandoned run (audit 25/09,
            B1): the abort already hid the modal, but anything that showed it
            again afterwards would leave it on screen with nobody to close
            it. A running operation, the automatic generation, a manual CAD
            run or the inventory pause still own it and are left alone.
        """
        if (self._abortable_operation() is not None
                or self._automatic_generation_active
                or getattr(self, "_manual_cad_overlay", False)
                or getattr(self, "_inventory_pause_active", False)):
            return
        if not self.analysis_overlay.isHidden():
            self._hide_analysis_overlay()

    def _extract_button_sources(self) -> tuple[str, str]:
        """@brief Italian sources of the extraction button's text and tooltip.

        @return (text, tooltip) for the selected photo detector.
        @note With «Solo YOLO locale» the button starts the free local pretest
            (user, 2026-09-25: «se ho selezionato solo YOLO il pulsante e il
            popup non sono corretti»); every other choice starts the paid AI
            extraction, and the tooltip says so before anything is spent. Both
            texts are written literally here so the catalogue check sees them.
        """
        combo = getattr(self, "combo_photo_detector", None)
        if combo is not None and combo.currentData() == "yolo_only":
            return ('Rileva componenti con YOLO',
                    "Pretest locale e GRATUITO: YOLO cerca i componenti nella foto dall'alto e mostra i riquadri da rivedere. Nessuna chiamata AI, nessun credito consumato, documenti non letti; campi, tabella e 3D non cambiano.")
        return ("Estrai parametri con AI",
                "Avvia l'estrazione dai documenti e dalle foto selezionati. Analisi A PAGAMENTO: consuma credito OpenRouter. I modelli usati e la spesa vengono scritti nel log prima di partire.")

    def _refresh_extract_button(self) -> None:
        """@brief Put the idle text and tooltip of the extraction button in line with the detector.

        @note Does nothing while `_extraction_busy` is set: the detector combo
            is not the only way here -- a model change from Configurazione ->
            Default generali AI goes through `_refresh_model_combo` and
            `_on_photo_detector_changed` in the middle of a run, and used to
            put the idle text back over «Analisi in corso...» (audit 25/09,
            B6). The run's end clears the flag and calls this again through
            `_end_extraction_busy`. It does not ask `_background_work_running`:
            after «Interrompi» the abandoned thread keeps running while the
            button must already be back at rest, and during the automatic
            generation the busy text must stay although the extraction thread
            has ended.
            Both properties are recorded as catalogue sources, so a later
            language switch translates them instead of freezing the rendered text.
        """
        button = getattr(self, "btn_extract_ai", None)
        if button is None or getattr(self, "_extraction_busy", False):
            return
        text, tooltip = self._extract_button_sources()
        self._set_extract_button_text(text)
        if self._component_name():
            self._set_translated_tooltip(button, tooltip)

    def _set_extract_button_text(self, text: str) -> None:
        """@brief Show `text` (Italian catalogue source) on the extraction button, translated.

        @param text Italian source string.
        @note Records source and rendering like the other catalogued widgets,
            so a language switch during a run translates the busy text too
            instead of replacing it with the stale idle source.
        """
        button = self.btn_extract_ai
        rendered = self.i18n.text(text)
        button.setText(rendered)
        button.setProperty("i18n_source", text)
        button.setProperty("i18n_rendered", rendered)

    def _begin_ai_run(self, title: str) -> None:
        """@brief Prepara log, consuntivo e pulsanti per un'analisi AI che sta per partire.

        @param title Titolo della sezione nel diario.
        @note Condiviso fra l'estrazione completa e il rilancio dell'inventario:
            entrambi finiscono in _on_extracted/_on_extract_finished, quindi
            devono partire dallo stesso stato. Il consuntivo si azzera qui,
            all'inizio di una NUOVA analisi: farlo quando la modale riparte lo
            cancellava subito dopo averlo mostrato, perche' la modale riparte
            anche per la generazione FreeCAD.
        """
        self.btn_extract_ai.setEnabled(False)
        # Sempre automatica: la modalita' manuale e' stata tolta (utente, 2026-09-26).
        self._auto_after_extraction = True
        self._auto_generation_params = None
        self._extract_finish_pending = False
        self.analysis_overlay.clear_cost()
        # Ogni chiamata di questa corsa si addebita al progetto di adesso, anche
        # se nel frattempo se ne apre un altro (verifica del 25/09, D7). Si
        # rilascia quando il worker finisce davvero (_on_extract_finished).
        if hasattr(self, "ai_account"):
            self.ai_account.bind_run_project(self._project_dir_or_none())
        self._set_extract_button_text("Analisi in corso...")
        # Dopo il testo: da qui fino alla fine della corsa il pulsante e i
        # comandi YOLO non si toccano piu' (vedi _refresh_extract_button).
        self._extraction_busy = True
        self._start_log_session(title)
        self.ai_progress_text.clear()
        self.ai_elements_list.clear()
        # Log e contatori svuotati cambiano l'altezza della pagina: il
        # pulsante con la clessidra resta in vista, ora e al prossimo giro.
        self._keep_extract_button_visible()
        QTimer.singleShot(0, self._keep_extract_button_visible)

    def _keep_extract_button_visible(self) -> None:
        """@brief Scroll the Project tab so that «Estrai parametri con AI» stays in view.

        @note User, 2026-10-02: on click the page moved and the button with
            its hourglass went out of view, so the click looked lost. The
            page can change height while a run starts (log and lists
            cleared, labels updated); this brings the button back each time.
        """
        scroll = getattr(self, "_project_scroll", None)
        button = getattr(self, "btn_extract_ai", None)
        if (scroll is not None and button is not None and button.isVisible()
                and scroll.widget().isAncestorOf(button)):
            scroll.ensureWidgetVisible(button, 0, 24)

    def _photo_cache_dir(self) -> Path:
        """@brief Cartella in cui l'inventario fotografico salva cache ed evidenza.

        @return `<cartella del progetto>/photo-cache`: risposte AI in cache,
            `detector-*.json`, `inventory-*-photo.png` e `yolo-audit/` del
            progetto corrente (decisione dell'utente del 2026-09-24: ogni
            interrogazione AI e' specifica del progetto).
        @throws ValueError quando il componente non ha ancora un nome.
        @note Nello stesso progetto possono esserci le evidenze di piu'
            estrazioni: le proposte salvate si accettano solo se nate
            dall'ultima (vedi _saved_detector_proposals).
        """
        from ..services.photo_inventory import photo_cache_dir
        return photo_cache_dir(self._project_dir())

    def _saved_detector_proposals(self):
        """@brief Le proposte del rilevatore salvate dall'ultima estrazione, se ci sono.

        @return SavedDetectorProposals, oppure None quando non c'e' nulla di
            riutilizzabile: nessun file, oppure un file piu' vecchio
            dell'ultima estrazione avviata in questa sessione, che potrebbe
            appartenere a un altro componente.
        @note Residuo noto: dopo un riavvio `_extraction_started_stamp` e'
            None e il filtro temporale non scatta, quindi il ripiego accetta
            il detector JSON piu' recente della cache comune qualunque sia il
            componente da cui e' nato. Il ramo si raggiunge solo quando il
            pacchetto `photo-review/` del componente manca (con il pacchetto
            presente comanda quello, che sta nella cartella del componente).
            La cache non registra il componente di origine: finche' non lo
            fara', questo ripiego resta un'euristica dichiarata.
        """
        from ..services.photo_inventory import load_saved_detector_proposals
        try:
            saved = load_saved_detector_proposals(self._photo_cache_dir())
        except OSError:
            return None
        if saved is None or not saved.proposals:
            return None
        if self._extraction_started_stamp and saved.stamp < self._extraction_started_stamp:
            return None
        return saved

    @staticmethod
    def _calibrated_extents(params) -> bool:
        """@brief Vero quando i parametri portano quote della scheda finite e positive.

        @note Sono le quote con cui la foto fu calibrata: senza, i riquadri
            normalizzati non hanno scala e non si possono ne' rivedere ne'
            rilanciare.
        """
        return (params is not None
                and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                        and math.isfinite(v) and v > 0
                        for v in (params.body_length_mm, params.body_width_mm)))

    def _review_bundle(self):
        """@brief Il pacchetto di revisione DELL'ESTRAZIONE salvato nella cartella del componente.

        @return ReviewBundle, oppure None: senza nome non c'e' cartella, e un
            pacchetto assente o corrotto vale come nessun pacchetto.
        @note E' l'unica fonte che sopravvive al riavvio: sta nella cartella
            del componente, non nella cache comune, quindi con il solo nome
            digitato si sa gia' se c'e' qualcosa da riaprire.
        @note Un pacchetto il cui rilevatore dichiarato e' `yolo_only` non e'
            dell'estrazione: e' un pretest scritto qui prima che i pretest
            avessero la loro cartella (audit 25/09, B2). Non vale come base
            per il rilancio a pagamento; si riapre gratis come pretest
            (`_pretest_bundle`). La regola guarda l'attributo registrato nel
            pacchetto, non il nome di un file.
        """
        bundle = self._load_bundle(None)
        if bundle is not None and bundle.detector == self._PRETEST_DETECTOR:
            return None
        return bundle

    #: Rilevatore registrato nei pacchetti del pretest solo YOLO.
    _PRETEST_DETECTOR = "yolo_only"
    #: Sotto-cartella del progetto riservata al pretest: contiene il suo
    #: `photo-review/` e la sua `photo-cache/`, separati da quelli
    #: dell'estrazione (audit 25/09, B2).
    PRETEST_SUBDIR = "yolo-pretest"

    def _pretest_root(self) -> Path:
        """@brief Folder that plays the role of the component folder for the pretest.

        @return `<project>/yolo-pretest`. The service functions append their
            usual `photo-review/` and `photo-cache/` to it, so the pretest
            bundle and its detector evidence never mix with the extraction's.
        @throws ValueError When the component has no name yet.
        """
        return self._project_dir() / self.PRETEST_SUBDIR

    def _pretest_bundle(self):
        """@brief The reviewed boxes of the last «Solo YOLO locale» pretest, if any.

        @return ReviewBundle from `<project>/yolo-pretest/photo-review/`, or a
            legacy `photo-review/` bundle recorded with detector `yolo_only`
            (written by the pretest before it had its own folder); None
            otherwise.
        """
        bundle = self._load_bundle(self._pretest_root() if self._named() else None)
        if bundle is not None:
            return bundle
        legacy = self._load_bundle(None)
        if legacy is not None and legacy.detector == self._PRETEST_DETECTOR:
            return legacy
        return None

    def _load_bundle(self, root):
        """Legge il pacchetto sotto `root` (None = cartella del componente); None se inutilizzabile."""
        from ..services.photo_inventory import load_review_bundle
        if not self._named():
            return None
        try:
            return load_review_bundle(root if root is not None else self._project_dir())
        except (OSError, ValueError):
            return None

    def _write_review_bundle(self, proposals, photo_png, length, width, *,
                             source_json='', detector='yolo', root=None, reviewed=True):
        """@brief Scrive il pacchetto di revisione nella cartella del componente.

        @param root Cartella che fa da cartella del componente: None per il
            pacchetto dell'estrazione, `_pretest_root()` per quello del pretest.
        @return Percorso di `proposals.json`, oppure None se non c'e' un nome
            o la scrittura fallisce (annotato nel log, senza fermare nulla).
        @note Operazione offline e gratuita: nessuna chiamata a modelli.
        """
        from ..services.photo_inventory import save_review_bundle
        if not self._named():
            return None
        try:
            return save_review_bundle(root if root is not None else self._project_dir(),
                                      proposals, photo_png, length, width,
                                      detector=detector, source_json=source_json,
                                      reviewed=reviewed, allow_empty=reviewed and not proposals)
        except (OSError, ValueError) as exc:
            self._append_log(tx("ATTENZIONE proposte non salvate nella cartella del componente: {exc}", exc=exc))
            return None

    def _fresh_cache_evidence(self):
        """@brief Ripiego di sessione: proposte fresche in cache + quote dell'ultima estrazione.

        @return (SavedDetectorProposals, ExtractedParams) oppure None.
        @note Vale solo finche' il pacchetto nella cartella del componente
            non esiste: appena c'e', comanda quello.
        """
        params = self._last_extracted
        if not self._calibrated_extents(params):
            return None
        saved = self._saved_detector_proposals()
        return (saved, params) if saved is not None else None

    def _ensure_review_bundle(self):
        """@brief Il pacchetto da riaprire, scrivendolo prima dalla cache se manca.

        @return ReviewBundle oppure None.
        @note Decisione dell'utente: il percorso dalla cache comune resta
            solo come ripiego, e anche in quel caso il pacchetto va scritto
            PRIMA, cosi' il prossimo avvio lo trova nella cartella del
            componente senza dipendere dalla cache.
        """
        bundle = self._review_bundle()
        if bundle is not None:
            return bundle
        fresh = self._fresh_cache_evidence()
        if fresh is None:
            return None
        saved, params = fresh
        photo_png = None
        if saved.photo_path is not None:
            try:
                photo_png = saved.photo_path.read_bytes()
            except OSError as exc:
                self._append_log(tx("Foto calibrata in cache non leggibile ({exc}): uso quella "
                                    "dell'estrazione.",
                                    exc=exc))
        if self._write_review_bundle(saved.proposals, photo_png or self._photo_png,
                                     params.body_length_mm, params.body_width_mm,
                                     source_json=saved.json_path.name,
                                     detector=self._extraction_detector, reviewed=False) is None:
            return None
        return self._review_bundle()

    def _store_extraction_review_bundle(self) -> None:
        """@brief Dopo un'estrazione, copia le proposte fresche della cache nella cartella del componente.

        @note Scrive solo quando l'estrazione ha lasciato proposte in cache con
            quote valide, e solo se il pacchetto manca o e' PIU' VECCHIO
            dell'estrazione appena avviata. Il secondo vincolo conta: anche il
            rilancio dell'inventario rientra da `_on_extracted`, e senza di
            esso le proposte ORIGINALI della cache tornerebbero a coprire
            quelle tenute dalla revisione, che stanno nel pacchetto con una
            marca piu' recente. Quando la finestra di revisione ha gia'
            scritto il pacchetto durante l'estrazione
            (`_reviewed_this_extraction`) questo ripiego non fa nulla: resta
            per le analisi senza revisione (rilevatore senza dialogo).
        """
        if self._reviewed_this_extraction:
            return
        fresh = self._fresh_cache_evidence()
        if fresh is None:
            return
        saved, params = fresh
        bundle = self._review_bundle()
        started = self._extraction_started_stamp
        if bundle is not None and not (started and bundle.stamp < started):
            return
        photo_png = None
        if saved.photo_path is not None:
            try:
                photo_png = saved.photo_path.read_bytes()
            except OSError:
                photo_png = None
        path = self._write_review_bundle(saved.proposals, photo_png or self._photo_png,
                                         params.body_length_mm, params.body_width_mm,
                                         source_json=saved.json_path.name,
                                         detector=self._extraction_detector, reviewed=False)
        if path is not None:
            self._append_log(tx("Proposte del rilevatore salvate in {path} ({n_proposals} corpi): la "
                                "revisione si puo' riaprire senza chiamate AI.",
                                path=path,
                                n_proposals=len(saved.proposals)))

    def _refresh_reopen_review_button(self) -> None:
        """@brief Abilita revisione (gratuita) e rilancio (a pagamento) quando c'e' cosa riaprire.

        @note La revisione richiede un pacchetto valido nella
            cartella del componente (anche svuotato esplicitamente),
            oppure -- ripiego di sessione -- proposte fresche in cache con le
            quote dell'ultima estrazione. Senza nome non c'e' cartella, quindi
            entrambi spenti. Non serve un'estrazione in questa sessione:
            appena il nome punta a una cartella con il pacchetto, si riapre.
            Il rilancio a pagamento richiede anche almeno una proposta.
        """
        if self._pretest_mode():
            # Con «Solo YOLO locale» la riga riguarda il pretest (audit 25/09,
            # B2): si riapre gratis il suo pacchetto, e il rilancio A
            # PAGAMENTO sparisce come spariscono modelli AI e credito.
            bundle = self._pretest_bundle()
            self._set_review_buttons_available(
                bundle is not None and bundle.photo_path is not None, False)
            return
        bundle = self._review_bundle()
        if bundle is not None:
            # Senza foto calibrata i riquadri non si possono ne' vedere ne'
            # riconciliare: pulsanti spenti invece di un avviso al clic.
            available = bundle.photo_path is not None or bool(self._photo_png)
            relaunch = available and bool(bundle.proposals)
        else:
            available = self._fresh_cache_evidence() is not None
            relaunch = available
        self._set_review_buttons_available(available, relaunch)

    def _pretest_mode(self) -> bool:
        """@brief True when the detector choice is the free «Solo YOLO locale» pretest."""
        combo = getattr(self, "combo_photo_detector", None)
        return combo is not None and combo.currentData() == self._PRETEST_DETECTOR

    def _set_action_available(self, name: str, available: bool) -> None:
        """@brief Un comando che non puo' agire si nasconde, non resta acceso a vuoto.

        @param name Nome dell'attributo del pulsante; assente = niente da fare.
        @param available True quando l'azione e' davvero possibile adesso.
        @note Richiesta dell'utente: «se il log non è presente il pulsante non
            si deve vedere». Vale per ogni comando della riga: un pulsante
            spento che resta visibile e' una promessa che l'interfaccia non
            puo' mantenere, e in una riga di cinque pulsanti e' solo confusione.
            Lo stato abilitato resta allineato alla visibilita' cosi' che i
            controlli che lo leggono (e i test) vedano una cosa sola.
        @note L'allineamento vale per cio' che questo metodo imposta, non per
            cio' che Qt calcola. Su una COPIA nella scheda Modello 3D --
            `btn_component_review_3d`, `btn_relaunch_inventory_3d` --
            `isEnabled()` e' in AND con lo
            stato della scheda, che resta spenta fino alla prima acquisizione:
            li' `isEnabled()` puo' essere falso mentre
            `isVisibleTo(parentWidget())` e' vero, e non e' un difetto. Un
            controllo sulla visibilita' propria della copia deve usare
            `isVisibleTo(...)`, non `isEnabled()`.
        """
        button = getattr(self, name, None)
        if button is None:
            return
        button.setEnabled(bool(available))
        button.setVisible(bool(available))
        self._refresh_project_bar_divider()

    def _refresh_project_bar_divider(self) -> None:
        """Il separatore della barra in alto si vede solo se separa qualcosa.

        A sinistra stanno sempre le icone dell'analisi; a destra i comandi della
        pipeline, che compaiono solo quando c'e' cosa riaprire: senza di loro un
        separatore da solo in coda alla barra sembra un difetto.
        """
        divider = getattr(self, "_project_bar_divider", None)
        if divider is None:
            return
        pipeline = [getattr(self, name, None) for name in (
            "btn_component_review_top", "btn_relaunch_inventory_top")]
        divider.setVisible(any(b is not None and b.isVisibleTo(b.parentWidget())
                               for b in pipeline))

    def changeEvent(self, event):  # noqa: N802 - Qt override
        """@brief Re-check the generated files when the window becomes active again.

        @param event Qt change event.
        @note Files can be deleted or regenerated outside the program (File
            Explorer, KiCad): coming back to the window is the moment the user
            looks at the tabs again, and a few `is_file` calls are cheap.
        """
        super().changeEvent(event)
        if (event.type() == QEvent.Type.ActivationChange and self.isActiveWindow()
                and hasattr(self, "tabs") and not self._background_work_running()):
            self._refresh_output_actions()

    def _mirror_project_action(self, source, slot, row, asset: str):
        """@brief Crea in una barra di icone (scheda Progetto o Modello 3D) la copia di un comando.

        @param source Pulsante originale: unica fonte di testo, suggerimento e stato.
        @param slot Slot da collegare -- e' lo STESSO comando, non una variante.
        @param row Layout della barra in cui inserire la copia.
        @param asset Icona SVG della copia, in `gui/assets`.
        @return Il pulsante copia, gia' inserito nella barra.
        @note Le tre copie passano tutte da qui. Prima due copiavano solo
            `setEnabled(...)` e la terza anche `isVisibleTo(...)`: partivano in
            stati diversi pur essendo lo stesso comando, e bastava che il
            primo ricalcolo arrivasse dopo per far vedere un pulsante che
            l'originale aveva gia' nascosto.
        @note Scelta dell'utente (2026-09-23): nella scheda Modello 3D la copia
            e' un'icona senza testo; il testo dell'originale diventa la prima
            riga del tooltip e il nome accessibile, il tooltip dell'originale
            la spiegazione. Un `_ToolTipFollower` ricompone il tooltip a ogni
            cambio del tooltip dell'originale -- anche quello del cambio di
            lingua -- quindi la copia segue l'originale per costruzione. Lo
            stato abilitato/visibile resta quello impostato insieme da
            `_set_action_available`.
        @note La visibilita' NON si applica qui: `_apply_project_action_mirror_visibility()`
            la allinea dopo che la barra e' montata. Oggi la barra ha gia' il
            suo widget quando le copie nascono, ma l'ordine resta quello
            sicuro: `setVisible(True)` su un widget senza genitore lo
            mostrerebbe come finestra a se' stante.
        """
        mirror = self._icon_only_button(asset)
        mirror.setEnabled(source.isEnabled())
        mirror.clicked.connect(slot)
        row.addWidget(mirror)
        self._project_action_mirrors.append((mirror, source))
        self._apply_icon_tooltip(mirror, source.text(), source.toolTip())
        _ToolTipFollower(source, lambda: self._apply_icon_tooltip(
            mirror, source.text(), source.toolTip()))
        return mirror

    def _apply_project_action_mirror_visibility(self) -> None:
        """@brief Allinea la visibilita' delle copie agli originali, a genitore acquisito."""
        for mirror, source in self._project_action_mirrors:
            mirror.setVisible(source.isVisibleTo(source.parentWidget()))

    def _set_review_buttons_enabled(self, enabled: bool) -> None:
        """@brief Accende o spegne insieme tutte le copie dei pulsanti di revisione/rilancio.

        @param enabled True quando c'e' un pacchetto di proposte da riaprire.
        @note Richiesta dell'utente: «dovrebbe sempre essere possibile
            ripartire anche da li'» -- i pulsanti stanno in fondo alla scheda
            Progetto e in fondo alla scheda Modello 3D, dove si guarda il
            risultato e ci si accorge che manca qualcosa. Sono lo stesso
            comando, quindi hanno sempre lo stesso stato.
        @note La modifica della revisione sta nella stessa riga ma ha una
            condizione propria: le serve la fotografia calibrata in memoria,
            che e' cio' che `_show_photo_comparison` apre. Il pacchetto
            salvato non basta -- dopo un riavvio, senza foto, quel comando non
            potrebbe fare nulla. Sta qui e non in un metodo a parte perche' le
            copie devono restare per costruzione nello stesso stato.
        """
        self._set_review_buttons_available(bool(enabled), bool(enabled))

    def _set_review_buttons_available(self, reopen: bool, relaunch: bool) -> None:
        """@brief Set the free review commands and the paid relaunch commands separately.

        @param reopen True when «Riapri revisione» has a bundle to open.
        @param relaunch True when the paid relaunch has an extraction bundle.
        @note A pretest or an explicitly empty review can be reopened for
            editing, but cannot start a paid reconciliation. The
            copies of each command still move together.
        """
        self._review_bundle_available = bool(reopen)
        for name in ("btn_relaunch_inventory", "btn_relaunch_inventory_top",
                     "btn_relaunch_inventory_3d"):
            self._set_action_available(name, bool(relaunch))
        self._refresh_component_review_button()

    def _component_review_stage(self):
        """@brief Which stage «Revisione componenti…» would open right now.

        @return `photo_review_dialog.STAGE_INVENTORY` when the calibrated
            photograph is in memory (the reconciled bodies, or an empty
            inventory to draw into); `STAGE_DETECTOR` when, without it, a
            bundle of detector proposals can be reopened (component folder or
            session cache); the string "overlay" when only the read-only
            overlay of the last extraction exists; None when nothing can be
            opened.
        @note The photograph decides, not the bundle: after an analysis both
            exist, and what the user corrects then is the inventory. The
            bundle saved at the mid-run pause stays what the paid relaunch
            reconciles.
        """
        from .photo_review_dialog import STAGE_DETECTOR, STAGE_INVENTORY
        if getattr(self, "_photo_png", None):
            return STAGE_INVENTORY
        if getattr(self, "_review_bundle_available", False):
            return STAGE_DETECTOR
        if getattr(self, "_photo_overlay_png", None):
            return "overlay"
        return None

    def _refresh_component_review_button(self) -> None:
        """@brief Allinea le copie di «Revisione componenti…» a cio' che il comando puo' aprire.

        @note Il comando apre uno stadio o l'altro della stessa finestra
            (`_component_review_stage`): senza fotografia, senza pacchetto e
            senza sovrapposizione non puo' fare nulla e sparisce, secondo la
            disciplina di `_set_action_available`.
        @note Metodo unico perche' le copie (scheda Progetto e scheda Modello
            3D) sono lo stesso comando: devono restare per costruzione nello
            stesso stato, da qualunque percorso si arrivi.
        """
        available = self._component_review_stage() is not None
        for name in ("btn_component_review", "btn_component_review_top",
                     "btn_component_review_3d"):
            self._set_action_available(name, available)

    def _open_component_review(self) -> None:
        """@brief «Revisione componenti…»: the one review window, in the stage that applies.

        @note Free in every branch. With the photograph in memory it is the
            inventory review (`_show_photo_comparison`); otherwise the saved
            detector proposals (`_reopen_detector_review`), whose OK rewrites
            `photo-review/proposals.json`; otherwise the read-only overlay.
        """
        from .photo_review_dialog import STAGE_DETECTOR
        stage = self._component_review_stage()
        if stage is None:
            self._refresh_component_review_button()
            return
        if stage == STAGE_DETECTOR:
            self._reopen_detector_review()
            return
        self._show_photo_comparison()

    def _on_inventory_pause_toggled(self, checked: bool) -> None:
        """@brief Ricorda la scelta sulla pausa e la scrive nel diario.

        @param checked True quando l'utente vuole essere fermato dopo l'inventario.
        @note Chiamato da Configurazione -> Opzioni di generazione quando la
            scelta cambia (dal 2026-09-24 la casella non sta piu' nel tab
            Progetto): quello e' l'unico posto da cui si accende e si spegne.
        """
        self._remember_inventory_pause(checked)
        self._append_log(
            tx("Pausa dopo l'inventario AI: attiva. La revisione dei componenti verrà "
               "proposta prima dell'abbinamento modelli e del 3D (gratuita).")
            if checked else
            tx("Pausa dopo l'inventario AI: disattivata. L'analisi proseguirà da sola "
               "con i corpi proposti dall'AI; la revisione resta disponibile dai "
               "pulsanti in basso."))

    def _ai_log_available(self) -> bool:
        """@brief Dice se esiste davvero un log dell'analisi da aprire.

        @return True quando il diario del componente ha righe, su disco o in
            attesa di una cartella, oppure quando la sessione ha gia' scritto
            qualcosa a schermo.
        @note E' la condizione del pulsante «Log AI»: si guarda il file scritto
            da `_write_log_line` (`_log_file()`) e il testo in memoria, non il
            fatto che un'analisi sia stata avviata. Senza nome del componente
            non c'e' cartella e le righe restano nel buffer: anche quelle sono
            un log apribile.
        @note Si conta anche l'elenco degli elementi, non solo il testo:
            `_on_ai_progress` manda le righe che cominciano con `#<numero>` in
            `ai_elements_list`, e `_append_log` le tiene fuori da
            `ai_progress_text`. Con una radice della libreria che
            non e' una cartella (il file del diario non si puo' scrivere ne'
            leggere) quelle righe erano l'unico contenuto della finestra del
            log, e il pulsante si nascondeva su un log che aveva davvero
            qualcosa da mostrare.
        """
        text = getattr(self, "ai_progress_text", None)
        if text is not None and text.toPlainText().strip():
            return True
        elements = getattr(self, "ai_elements_list", None)
        if elements is not None and elements.count():
            return True
        if getattr(self, "_pending_log_lines", None):
            return True
        if not self._component_name():
            return False
        try:
            path = self._log_file()
            return path.is_file() and path.stat().st_size > 0
        except (OSError, AttributeError):
            # Un diario illeggibile non e' un diario apribile.
            return False

    def _refresh_ai_log_button(self) -> None:
        """@brief Mostra «Log AI» solo quando c'e' un log, e appena c'e'.

        @note Chiamato dove il log o il nome del componente cambiano -- la
            modifica del testo dell'analisi (`_refresh_ai_copy_buttons`) e il
            controllo del nome (`_apply_component_name_gate`) -- e non da un
            timer: un pulsante che appare per un giro di orologio invece che
            per un fatto avvenuto e' peggio di uno spento.
        """
        self._set_action_available("btn_ai_log", self._ai_log_available())

    def _review_photo(self, bundle):
        """@brief La foto calibrata del pacchetto, o quella in memoria come ripiego.

        @return PNG in byte, oppure None (gia' segnalato con un avviso).
        """
        photo_png = None
        if bundle.photo_path is not None:
            try:
                photo_png = bundle.photo_path.read_bytes()
            except OSError as exc:
                self._append_log(tx("Foto calibrata del pacchetto non leggibile ({exc}).", exc=exc))
        if not photo_png and self._photo_png:
            photo_png = self._photo_png
            self._append_log(tx("Foto calibrata assente in photo-review/: uso quella dell'estrazione "
                                "in memoria."))
        if not photo_png:
            # Testo neutro: arriva sia dal rilancio a pagamento sia dalla
            # riapertura gratuita, e nessuno dei due e' "il rilancio".
            QMessageBox.warning(
                self, self.i18n.text("Foto calibrata mancante"),
                self.i18n.text("Manca la foto calibrata delle proposte salvate: senza non si "
                               "possono ne' rivedere ne' riconciliare i riquadri. Serve una "
                               "nuova estrazione dalla foto."))
        return photo_png

    def _reopen_detector_review(self) -> None:
        """@brief Riapre la revisione (stadio rilevatore) sul pacchetto salvato e, all'OK, lo risalva. Gratis.

        @note Nessuna chiamata AI in nessun ramo: la revisione e' sul thread
            GUI e l'OK scrive soltanto `photo-review/proposals.json` con le
            proposte tenute -- comprese le disegnate a mano (`_yolo_manual_box`)
            e tutti i campi cosi' come sono. Chi vuole ripagare l'inventario
            ha il pulsante accanto (`_relaunch_inventory`). Annullare non
            tocca il file.
        @note Con «Solo YOLO locale» scelto il pacchetto riaperto e risalvato
            e' quello del pretest (`<progetto>/yolo-pretest/photo-review/`),
            e il rilancio a pagamento non e' offerto (audit 25/09, B2).
        """
        if self._background_work_running() or self._automatic_generation_active:
            return
        # Con «Solo YOLO locale» si riapre il pacchetto del pretest, che si
        # risalva nella sua cartella: mai in quella dell'estrazione.
        pretest = self._pretest_mode()
        bundle = self._pretest_bundle() if pretest else self._ensure_review_bundle()
        if bundle is None:
            self._refresh_reopen_review_button()
            return
        photo_png = self._review_photo(bundle)
        if not photo_png:
            return
        self._append_log(tx("Proposte riaperte da {json_path}: {n_proposals} corpi, nessuna "
                            "richiesta AI.",
                            json_path=bundle.json_path,
                            n_proposals=len(bundle.proposals)))
        try:
            # Letto dal modulo a ogni chiamata, cosi' una prova puo' sostituire
            # la finestra; stadio «rilevatore» della revisione unificata.
            from . import photo_review_dialog as review_module
            dialog = review_module.PhotoReviewDialog(
                photo_png, [dict(p) for p in bundle.proposals],
                bundle.body_length_mm, bundle.body_width_mm, self,
                stage=review_module.STAGE_DETECTOR)
            self.i18n.retranslate_widgets(dialog)
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            kept = list(dialog.kept_components()) if accepted else None
        except Exception as exc:  # noqa: BLE001 - la vista non deve far cadere la finestra
            self._append_log(tx("ATTENZIONE revisione dei corpi non disponibile: {exc}", exc=exc))
            return
        if kept is None:
            self._append_log(tx("Revisione dei corpi rilevati annullata: nessuna richiesta avviata, "
                                "file invariato."))
            return
        path = self._write_review_bundle(kept, photo_png, bundle.body_length_mm, bundle.body_width_mm,
                                         source_json=bundle.source_json, detector=bundle.detector,
                                         root=self._pretest_root() if pretest else None)
        if path is not None and pretest:
            # Il pretest non ha rilancio: non si indica un comando a pagamento
            # che in questa modalita' non c'e'.
            self._append_log(tx("Revisione del pretest YOLO salvata offline in {path}: {n_kept} "
                                "proposte tenute, nessuna richiesta AI (gratuito).",
                                path=path, n_kept=len(kept)))
        elif path is not None:
            self._append_log(tx("Revisione salvata offline in {path}: {n_kept} proposte tenute, "
                                "nessuna richiesta AI (gratuito). Per l'inventario usa «Rilancia "
                                "inventario AI (a pagamento)…».",
                                path=path,
                                n_kept=len(kept)))
        self._refresh_reopen_review_button()

    def _relaunch_base_params(self, bundle):
        """@brief I parametri su cui innestare l'inventario rilanciato.

        @return L'ultima estrazione della sessione, oppure -- dopo un riavvio,
            quando non ce n'e' una -- una base ricavata dal pacchetto e dai
            campi correnti, con la disposizione che la finestra mostra ora:
            castellated (lati della tabella) oppure matrice (righe, colonne,
            passo, distanza righe, pad, foro), cosi' `_apply_extracted_result`
            ritrova campi valorizzati e non `None` da assegnare agli spin box.
        @note Le quote vengono dal pacchetto, non dai campi: sono quelle con
            cui i riquadri furono calibrati.
        """
        if self._last_extracted is not None:
            return self._last_extracted
        common = dict(
            name=self._component_name(), description=self.field_description.text(),
            tags=self.field_tags.text(), confidence='low', notes='', components_3d=[],
            body_length_mm=bundle.body_length_mm, body_width_mm=bundle.body_width_mm,
            photo_png=None, photo_source=str(bundle.photo_path or ''))
        if self._is_castellated():
            # Il tipo di contatto viaggia con i lati: il risultato del rilancio
            # rientra da `_apply_extracted_castellated`, che senza la chiave
            # rimetterebbe il valore predefinito sopra la scelta dell'utente.
            sides = self._serialised_sides()
            spacing = getattr(self, "_declared_row_spacing", None)
            if spacing is not None:
                # Senza, il rilancio rientrerebbe da _apply_extracted_castellated
                # senza quota e i fori passanti tornerebbero alla stima.
                common = dict(common, row_spacing_mm=spacing)
            return ExtractedParams(layout='castellated', castellated_sides=sides, **common)
        return ExtractedParams(
            layout='matrix',
            rows=self.field_rows.value(), cols=self.field_cols.value(),
            pitch_mm=self.field_pitch.value(), row_spacing_mm=self.field_row_spacing.value(),
            pad_diameter_mm=self.field_pad.value(), drill_mm=self.field_drill.value(),
            **common)

    def _relaunch_inventory(self) -> None:
        """@brief Ripaga la sola riconciliazione sulle proposte del pacchetto salvato.

        @note Solo il pacchetto dell'ESTRAZIONE (`_ensure_review_bundle`):
            i riquadri di un pretest solo YOLO non vengono mai riconciliati a
            pagamento da qui (audit 25/09, B2).

        @note E' l'unica spesa di questo percorso e viene dichiarata PRIMA
            di partire, nel log e nella modale. Le proposte arrivano dal file
            cosi' come furono salvate dalla revisione (gia' filtrate e
            misurate; le disegnate a mano incluse) e vanno alla
            riconciliazione senza altre trasformazioni. Il risultato rientra
            per `_on_reconciled` come dopo un'estrazione.
        """
        if self._background_work_running() or self._automatic_generation_active:
            return
        bundle = self._ensure_review_bundle()
        if bundle is None or not bundle.proposals:
            self._refresh_reopen_review_button()
            return
        photo_png = self._review_photo(bundle)
        if not photo_png:
            return
        # The reconciliation ends in a generation: settle a footprint draft first.
        if not self._settle_footprint_draft_before_start():
            return
        # La base sintetizzata finisce in _last_extracted per far rientrare il
        # risultato da _on_reconciled: da li' in poi `_last_extracted is None`
        # non basta piu' a riconoscerla, serve il flag esplicito, che solo una
        # estrazione vera azzera.
        synthesized = self._last_extracted is None or self._relaunch_base_synthesized
        params = self._relaunch_base_params(bundle)
        model = self._photo_detector_model()
        try:
            profile = self.profile_store.snapshot(self._profile_key())
        except (ProfileError, sqlite3.Error, OSError) as exc:
            QMessageBox.warning(self, self.i18n.text('Profilo AI non disponibile'), str(exc))
            return
        if not self._ensure_api_key():
            return
        self._begin_ai_run("Rilancio inventario")
        try:
            self._start_reconcile_worker(bundle, photo_png, params, model, profile, synthesized)
        except Exception as exc:  # noqa: BLE001 - qualunque guasto riporta la UI in quiete
            self._on_extract_finished()
            self._on_extract_error(str(exc))

    def _start_reconcile_worker(self, bundle, photo_png, params, model, profile,
                                synthesized) -> None:
        """@brief Configure and start the paid reconciliation worker of a relaunch.

        @param bundle ReviewBundle whose proposals are reconciled.
        @param photo_png Calibrated photograph the proposals refer to.
        @param params Base ExtractedParams the inventory is grafted on.
        @param model Model that receives the single paid request.
        @param profile AI profile snapshot (verification and crop prompts,
            crop model), the same ProfileStore.snapshot source as the
            ExtractWorker.
        @param synthesized True when `params` was built from the fields rather
            than extracted: automatic generation is then withheld.
        @throws Exception Anything raised by the worker set-up or start; the
            caller restores the idle UI state.
        """
        if synthesized:
            # Senza un'estrazione in questa sessione la base viene dai campi
            # correnti, che possono essere vuoti: l'inventario va applicato
            # ai campi, non usato per far partire da solo footprint e 3D.
            self._relaunch_base_synthesized = True
            self._auto_after_extraction = False
            self._append_log(tx("Nessuna estrazione in questa sessione: l'inventario rilanciato "
                                "aggiorna i componenti senza avviare la generazione automatica."))
        self._set_review_buttons_enabled(False)
        # Dichiarata PRIMA di partire, nel log e nella modale: chi preme deve
        # sapere prima, non dopo, che cosa si paga. Oltre alla richiesta
        # sull'inventario il rilancio rilegge ogni corpo dal suo ritaglio e le
        # sigle, come l'estrazione (HANDOFF 146 V2).
        announcement = (tx("Rilancio dell'inventario su {n_proposals} proposte da {name}: rete "
                           "SMD locale, gratuita; solo i corpi che non riconosce passano alle "
                           "letture a pagamento dei ritagli con il modello {model} (riprese "
                           "dalla cache se gia' pagate).",
                           n_proposals=len(bundle.proposals),
                           name=bundle.json_path.name,
                           model=model)
                        if bundle.detector == 'yolo_smd' else
                        tx("Rilancio dell'inventario su {n_proposals} proposte da {name}: una "
                           "richiesta a pagamento al modello {model} per l'inventario, poi le "
                           "letture a pagamento dei singoli corpi e delle sigle (riprese dalla "
                           "cache se gia' pagate).",
                           n_proposals=len(bundle.proposals),
                           name=bundle.json_path.name,
                           model=model))
        self._on_ai_progress(announcement)
        self._last_extracted = params
        declared = dict(layout=params.layout, castellated_sides=params.castellated_sides,
                        body_length_mm=params.body_length_mm, body_width_mm=params.body_width_mm)
        self._reconcile_worker = ReconcileWorker(
            photo_png, bundle.proposals, bundle.body_length_mm, bundle.body_width_mm, model,
            self._photo_cache_dir(),
            declared=declared, verification_prompt=profile['verification_prompt'],
            crop_prompt=profile['crop_prompt'],
            crop_model=profile['crop_model'],
            extra_instructions=self.field_component_prompt.toPlainText().strip() or None,
            # Il nome del rilevatore e' quello con cui i riquadri sono nati,
            # registrato nel pacchetto, qualunque cosa dica ora la tendina.
            detector=bundle.detector, reviewed=bundle.reviewed,
            smd_model_path=self._smd_model_option())
        self._reconcile_worker.done.connect(self._on_reconciled)
        self._reconcile_worker.error.connect(self._on_extract_error)
        self._reconcile_worker.progress.connect(self._on_ai_progress)
        self._reconcile_worker.phase.connect(self._on_phase)
        self._reconcile_worker.finished.connect(self._on_extract_finished)
        # Vedi _start_extract_worker: marchio della corsa per i risultati tardivi.
        self._reconcile_worker._analysis_revision = self._analysis_revision
        # A new run has applied nothing yet (see `_on_extract_finished`).
        self._extraction_applied_revision = None
        self._analysis_tabs_enabled = self.tabs.isEnabled()
        self.tabs.setEnabled(False)
        self.analysis_overlay.start("Rilancio dell'inventario", announcement,
                                    abortable=True)
        self._reconcile_worker.start()

    def _on_reconciled(self, report) -> None:
        """@brief Sostituisce i componenti e rientra nella via di un'estrazione normale.

        @param report InventoryResult di reconcile_saved_proposals.
        @note I nuovi componenti prendono il posto di `components_3d` negli
            ultimi parametri estratti, e il resto (nome, quote, lati) resta
            quello. Da qui in poi e' _on_extracted: stessi campi, stesso
            ripristino delle scelte KiCad, e -- se la modalita' lo prevede --
            la generazione automatica con abbinamento modelli e 3D parte da
            _on_extract_finished come dopo l'estrazione.
        """
        from dataclasses import replace
        if not self._analysis_result_is_current():
            self._append_log(tx("Inventario di un rilancio interrotto dall'utente: scartato, i "
                                "componenti restano come sono."))
            return
        base = self._last_extracted
        if base is None:
            self._on_extract_error(tx("Nessuna estrazione a cui applicare l'inventario rilanciato."))
            return
        notes = str(base.notes or '').strip()
        suffix = (tx("Inventario rilanciato: {n_components} ingombri riconciliati da {get} "
                     "proposte riviste. Modello meccanico semplificato, senza marcature; "
                     "altezze indicative, non misure certificate.",
                     n_components=len(report.components),
                     get=report.calibration.get('proposal_count', 0)))
        if report.warnings:
            suffix += "\n" + "\n".join(report.warnings[:6])
        # Il rilancio ripaga l'inventario, non la lettura dei contatti: un tipo
        # di contatto corretto a mano dopo l'estrazione deve sopravvivergli.
        # Stessa normalizzazione dell'estrazione (tipi fuori elenco -> generico,
        # corpi senza ingombro scartati): prima il rilancio la saltava e un
        # tipo grezzo come 'fuse' arrivava al 3D (audit 2026-10-02, D7).
        from ..services.datasheet_extractor import _components_3d
        normalized_notes = []
        normalized = _components_3d([dict(c) for c in report.components], normalized_notes,
                                    trusted=True) or []
        if normalized_notes:
            suffix += "\n" + "\n".join(normalized_notes[:6])
        params = replace(base, components_3d=normalized,
                         castellated_sides=self._sides_with_table_styles(base.castellated_sides),
                         photo_overlay_png=report.overlay_png,
                         photo_png=report.photo_png or base.photo_png,
                         notes=f"{notes} {suffix}".strip())
        self._on_extracted(params)

    def _hold_completed_overlay(self, message: str) -> None:
        """@brief Leave the modal open on a finished generation, steps ticked, until «Chiudi».

        @param message Italian source of the final outcome.
        @note The window underneath is given back exactly as `_hide_analysis_overlay`
            does; only the card stays. Without steps (an operation of one
            phase) there is nothing to show ticked and the card simply closes.
        """
        if not self.analysis_overlay._step_labels:
            self._hide_analysis_overlay()
            return
        if self._analysis_tabs_enabled is not None:
            self.tabs.setEnabled(self._analysis_tabs_enabled)
            self._analysis_tabs_enabled = None
        self.analysis_overlay.show_completed(self.i18n.text(message))

    def _on_completed_overlay_closed(self) -> None:
        """@brief The user closed the finished card: a regeneration waiting for it may start."""
        if self._regen_3d_pending:
            QTimer.singleShot(0, self._run_pending_regen_3d)

    def _hide_analysis_overlay(self) -> None:
        self.analysis_overlay.stop()
        if self._analysis_tabs_enabled is not None:
            self.tabs.setEnabled(self._analysis_tabs_enabled)
            self._analysis_tabs_enabled = None
        if self._regen_3d_pending:
            # A regeneration asked while this work ran: its turn now.
            QTimer.singleShot(0, self._run_pending_regen_3d)

    # ── Interruzione dell'operazione in corso ──────────────────────

    #: Le quattro vie che aprono la modale, con cio' che di ciascuna si puo'
    #: DAVVERO fermare. La distinzione non e' un dettaglio interno: e' quello
    #: che si promette a chi preme il pulsante.
    #:
    #: * `cad` -- il lavoro sta in un processo figlio (freecadcmd) avviato da
    #:   questo programma, che lo sa uccidere: interrompere vuol dire ucciderlo.
    #: * `ai` -- il lavoro e' una richiesta HTTP gia' partita verso OpenRouter,
    #:   fatta con `urllib` bloccante (`kicadgen/services/openrouter.py`).
    #:   Nessuno la puo' annullare dall'esterno del thread che la sta
    #:   aspettando, e soprattutto ANNULLARE NON RIMBORSA: il modello sta gia'
    #:   elaborando cio' che e' stato inviato. Interrompere vuol dire
    #:   abbandonare la corsa, restituire subito la finestra e scartare il
    #:   risultato quando arrivera'. Fingere di "fermarla" sarebbe una bugia
    #:   sul denaro speso.
    _ABORTABLE_OPERATIONS = (
        ("cad", "_worker", "Generazione di footprint e modello 3D"),
        ("cad", "_cad3d_worker", "Rigenerazione del modello 3D"),
        ("ai", "_extract_worker", "Analisi AI dei documenti"),
        ("ai", "_reconcile_worker", "Rilancio dell'inventario"),
    )
    #: Nome dell'operazione quando nello slot dell'estrazione gira il pretest
    #: solo YOLO (vedi _abortable_operation).
    _PRETEST_LABEL = "Pretest YOLO locale"

    def _abortable_operation(self):
        """@brief L'operazione in corso che «Interrompi» puo' fermare, se c'e'.

        @return (natura, worker, fase) con natura in {'cad', 'ai', 'local'},
            oppure None quando non c'e' nulla da fermare. 'local' e' il
            pretest solo YOLO: si abbandona come una corsa AI, ma non costa,
            e la fase e' `_PRETEST_LABEL`, non l'etichetta dello slot.
        @note Un worker gia' abbandonato NON e' piu' un candidato: continua a
            girare (e a contare per la guardia di chiusura) ma la finestra e'
            gia' tornata all'utente, quindi non c'e' piu' niente da chiedergli.
        """
        for kind, attribute, label in self._ABORTABLE_OPERATIONS:
            worker = getattr(self, attribute, None)
            if worker is None or not worker.isRunning():
                continue
            if any(worker is abandoned for abandoned in self._abandoned_workers):
                continue
            if isinstance(worker, YoloPretestWorker):
                # Lo slot e' quello dell'estrazione, ma il lavoro no: la
                # domanda di conferma e il log devono nominare il pretest,
                # non un'«Analisi AI» che non e' mai partita.
                return "local", worker, self._PRETEST_LABEL
            return kind, worker, label
        return None

    def _abort_phase_text(self, label: str) -> str:
        """@brief La fase da scrivere nel log e nella domanda di conferma.

        @param label Nome dell'operazione (vedi _ABORTABLE_OPERATIONS).
        @return Operazione e, quando la modale lo dichiara, strumento al lavoro.
        @note «A che punto era» e' l'unica cosa che rende utile la riga nel
            log: due mesi dopo, «interrotta» da sola non dice se si stavano
            abbinando modelli o costruendo il solido.
        """
        tool = ""
        label_widget = getattr(self.analysis_overlay, "tool", None)
        if label_widget is not None and label_widget.isVisibleTo(self.analysis_overlay):
            tool = " ".join(label_widget.text().split())
        return f"{label} — {tool}" if tool else label

    def _confirm_abort(self, kind: str, phase: str) -> bool:
        """@brief Chiede conferma dicendo che cosa si perde, senza addolcirlo.

        @param kind 'cad' o 'ai' (vedi _ABORTABLE_OPERATIONS).
        @param phase Fase in corso, come la scrive _abort_phase_text.
        @return True solo se l'utente ha risposto di si'.
        @note Una domanda sola, con la risposta prudente preselezionata:
            interrompere butta via minuti di attesa o denaro gia' speso, e chi
            preme il pulsante puo' averlo fatto per sbaglio.
        @note Nel caso `ai` la spesa va detta QUI, prima del danno: la
            richiesta e' gia' partita e nessuna interfaccia la puo' richiamare.
        """
        if kind == "local":
            # Pretest solo YOLO: nessuna richiesta a pagamento da dichiarare.
            detail = self.i18n.text(
                "Il rilevamento YOLO locale viene abbandonato: nessun costo, la "
                "finestra torna subito utilizzabile e il risultato, se arriverà, "
                "verrà scartato.")
        elif kind == "cad":
            detail = self.i18n.text(
                "Il processo FreeCAD in corso verrà terminato e il modello 3D non "
                "sarà completato: il lavoro fatto finora va perso e la generazione "
                "andrà rifatta da capo.\n\n"
                "Se in questo momento si stanno misurando modelli della libreria "
                "KiCad, l'interruzione ha effetto appena quella misura finisce.")
        else:
            detail = self.i18n.text(
                "La richiesta già inviata al modello NON si può annullare: è già "
                "stata addebitata e interromperla non la rimborsa.\n\n"
                "L'analisi viene abbandonata, la finestra torna subito utilizzabile "
                "e il risultato, se arriverà, verrà scartato: il denaro già speso "
                "resta speso e il lavoro andrà rifatto (e ripagato) da capo.")
        answer = QMessageBox.question(
            self, self.i18n.text("Interrompere l'operazione?"),
            f"{phase}\n\n{detail}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes

    def _request_abort(self) -> None:
        """@brief Slot del pulsante «Interrompi» della modale.

        @note Due clic non fanno due interruzioni: `_abort_pending` copre
            l'intervallo fra la conferma e la fine effettiva del lavoro, che
            per il CAD non e' istantanea (il figlio deve morire e il worker
            deve uscire dalla sua via d'errore).
        @note La domanda gira un event loop annidato: fra la domanda e la
            risposta il lavoro puo' finire da solo. Si ricontrolla dopo, e in
            quel caso non si ordina niente a nessuno.
        """
        if self._abort_pending:
            return
        operation = self._abortable_operation()
        if operation is None:
            # Il lavoro e' finito fra il clic e questo slot: la finestra sta
            # gia' tornando da sola e il pulsante non ha piu' un oggetto.
            self.analysis_overlay.set_abortable(False)
            return
        kind, worker, label = operation
        phase = self._abort_phase_text(label)
        if not self._confirm_abort(kind, phase):
            # Rifiutare non tocca nulla: il lavoro continua e il pulsante
            # resta dov'era, per ripensarci.
            return
        if self._abort_pending or not worker.isRunning():
            self._append_log(
                tx("Interruzione chiesta dall'utente ({phase}), ma nel frattempo "
                   "l'operazione era già terminata: niente da fermare.",
                   phase=phase))
            self.analysis_overlay.set_abortable(False)
            return
        self._abort_pending = True
        self.analysis_overlay.set_abortable(False)
        if kind == "cad":
            self._abort_cad_run(worker, phase)
        else:
            self._abort_ai_run(worker, phase, paid=kind == "ai")

    def _on_cad_cancellable(self, cancellable: bool) -> None:
        """@brief Toglie «Interrompi» quando il generatore in uso non sa fermarsi.

        @param cancellable False quando il canale di rinuncia non e' accettato.
        @note Richiesta dell'utente: «la possibilità di stoppare deve essere
            presente in ogni step». Dove non c'e', non si finge: il comando
            sparisce e il log dice perche'. Un pulsante «Interrompi» che non
            interrompe e' una promessa peggiore della sua assenza -- chi lo
            preme smette di guardare e crede che il lavoro si stia fermando.
        @note Il segnale arriva dal thread del worker: Qt lo consegna qui in
            coda sul thread della finestra, che e' l'unico che puo' toccare la
            modale.
        """
        if cancellable:
            return
        self.analysis_overlay.set_abortable(False)
        self._on_ai_progress(
            tx("«Interrompi» non è disponibile per questa generazione: il generatore "
               "3D in uso non accetta il canale di interruzione, quindi premerlo non "
               "fermerebbe nulla. L'attesa va lasciata finire, oppure si chiude il "
               "programma."))

    def _abort_cad_run(self, worker, phase: str) -> None:
        """@brief Uccide il processo FreeCAD e lascia che il worker finisca da se'.

        @param worker GenerateWorker o Cad3DWorker in corso.
        @param phase Fase in corso, per il log.
        @note La modale resta finche' il worker non emette `finished`: e' quel
            segnale a rimettere in piedi schede, pulsanti e testo del comando,
            ed e' la via che esiste gia' per ogni altro modo in cui il 3D non
            si completa. Anticiparla a mano vorrebbe dire avere due percorsi di
            ripristino da tenere allineati per sempre.
        """
        self._on_ai_progress(
            tx("INTERRUZIONE chiesta dall'utente durante: {phase}. Il processo "
               "FreeCAD viene terminato; il modello 3D non sarà completato.",
               phase=phase))
        self.analysis_overlay.set_status(self.i18n.text(
            "Interruzione in corso: chiusura del processo FreeCAD…"))
        worker.request_abort()

    def _abort_ai_run(self, worker, phase: str, *, paid: bool = True) -> None:
        """@brief Abbandona la corsa AI: la finestra torna subito, il risultato si scarta.

        @param worker ExtractWorker, ReconcileWorker o YoloPretestWorker in corso.
        @param phase Fase in corso, per il log.
        @param paid False per il pretest solo YOLO: stesso abbandono, ma il
            log non parla di una spesa che non c'e' e non c'e' consuntivo.
        @note Non si finge di aver fermato la chiamata: il thread resta vivo
            finche' `urllib` non restituisce, e per questo il worker continua a
            contare per la guardia di `closeEvent` -- chiudere la finestra con
            un QThread vivo abbatte il processo.
        @note Cio' che cambia e' la VALIDITA' del suo risultato: si incrementa
            `_analysis_revision`, esattamente come `_invalidate_generated_mesh`
            fa con i risultati CAD tardivi, e i segnali che arrivano dopo
            vengono riconosciuti come scaduti e scartati.
        @note I comandi che spendono altro denaro (estrazione, rilancio) NON
            vengono riaccesi qui: finche' la richiesta abbandonata e' in volo
            `_background_work_running()` e' vero, e riaccenderli prometterebbe
            un'operazione che partirebbe subito con un rifiuto silenzioso. Li
            riaccende la fine della corsa fantasma, in `_on_extract_finished`.
        """
        self._analysis_revision += 1
        self._abandoned_workers.append(worker)
        # Le chiamate a pagamento non ancora partite non partono piu'.
        cancel_event = getattr(worker, "cancel_event", None)
        if cancel_event is not None:
            cancel_event.set()
        self._on_ai_progress(
            tx("INTERRUZIONE chiesta dall'utente durante: {phase}. La richiesta già "
               "inviata al modello non si può annullare ed è già a pagamento: la "
               "corsa viene abbandonata e il suo risultato, quando arriverà, verrà "
               "scartato.",
               phase=phase) if paid else
            tx("INTERRUZIONE chiesta dall'utente durante: {phase}. Il rilevamento "
               "YOLO locale viene abbandonato, senza costi; il suo risultato verrà "
               "scartato.", phase=phase))
        self._auto_after_extraction = False
        self._auto_generation_params = None
        self._extract_finish_pending = False
        # Una corsa abbandonata non produce l'inventario su cui fermarsi: la
        # pausa non deve sopravviverle e riaprirsi sui dati di prima.
        self._inventory_pause_pending = False
        # Il consuntivo di cio' che e' gia' stato fatturato resta sotto il
        # pulsante: dopo un'interruzione e' l'informazione piu' utile che ci
        # sia. Non comprende la richiesta ancora in volo, che verra' pagata
        # lo stesso ma non ha ancora restituito i propri conteggi: e' scritto
        # nella riga di log qui sopra.
        if paid:
            self._show_ai_cost()
        self._hide_analysis_overlay()
        # La finestra torna all'utente: il pulsante torna a riposo.
        self._end_extraction_busy()
        self._apply_component_name_gate()
        self._refresh_ai_log_button()
        # Niente da attendere: la finestra e' gia' tornata all'utente.
        self._abort_pending = False

    def _analysis_result_is_current(self) -> bool:
        """@brief Dice se il risultato che sta arrivando appartiene alla corsa valida.

        @return False per i segnali di una corsa abbandonata con «Interrompi».
        @note Gemello di `_cad_result_is_current`: stesso meccanismo (un
            numero di revisione impresso sul worker alla partenza, confrontato
            con quello della finestra) applicato ai worker dell'analisi AI.
            Una chiamata diretta -- non via segnale -- non ha mittente e vale
            sempre: e' il caso di `_on_extracted` chiamato da `_on_reconciled`.
        """
        worker = self.sender()
        if worker is None:
            return True
        return getattr(worker, "_analysis_revision",
                       self._analysis_revision) == self._analysis_revision

    def _forget_abandoned_worker(self, worker) -> None:
        """@brief Lascia andare un worker abbandonato che ha finalmente finito.

        @param worker Worker che ha appena emesso `finished`.
        @note Si libera lo slot solo se e' ancora lui: nel frattempo l'utente
            puo' aver avviato un'altra analisi, e azzerare lo slot di quella
            nuova la renderebbe invisibile alla guardia di chiusura.
        """
        self._abandoned_workers = [
            other for other in self._abandoned_workers if other is not worker]
        for _, attribute, _ in self._ABORTABLE_OPERATIONS:
            if getattr(self, attribute, None) is worker:
                setattr(self, attribute, None)

    def _on_extract_finished(self) -> None:
        """@brief Close an extraction run: release it, offer the review, start the automatic generation.

        @note A footprint edit left pending while the extraction ran is NOT
            retried here when the result is applied: the extraction has just
            put new, not yet reviewed parameters in the window, and a
            footprint-only rewrite would write them without the review and
            without `_confirm_output_overwrite`. The edit itself is saved in
            the project and every GenerateWorker copies drawings and text
            positions, so the next generation (automatic or the user's)
            carries it. An abandoned run changed nothing: there the retry runs.
        @note A run that failed before its result reached `_on_extracted`
            (worker error, nothing applied) also changed nothing: the pending
            edit is kept and retried once (`_retry_footprint_rewrite`), under
            the usual rules (never while busy or while
            `_pipeline_owns_output`). A result that raised half way through
            being applied counts as applied: some fields may be new.
        """
        # Il worker ha finito, anche se abbandonato: le sue chiamate sono
        # tutte registrate, il progetto della corsa si puo' rilasciare.
        if hasattr(self, "ai_account"):
            self.ai_account.release_run_project()
        if not self._analysis_result_is_current():
            # Corsa abbandonata con «Interrompi»: la finestra e' gia' tornata
            # all'utente da un pezzo. Qui si prende atto che la chiamata a
            # pagamento e' finalmente rientrata, si lascia andare il worker e
            # si riaccendono i comandi che erano spenti perche' girava.
            worker = self.sender()
            self._forget_abandoned_worker(worker)
            self._retry_footprint_rewrite()
            self._append_log(
                tx("La corsa interrotta dall'utente è terminata adesso: il suo risultato "
                   "è stato scartato, i comandi tornano disponibili."))
            # Rete di sicurezza (audit 25/09, B1): una modale rimasta aperta
            # senza piu' nessuna operazione che la possa chiudere.
            self._release_orphan_overlay()
            self._apply_component_name_gate()
            self._refresh_reopen_review_button()
            self._refresh_ai_log_button()
            return
        # Modal rename/validation dialogs run a nested Qt event loop. The worker
        # may finish there, before _on_extracted has populated all fields.
        if self._applying_extraction:
            self._extract_finish_pending = True
            return
        # New parameters own the output now: the pending edit goes with the
        # next generation (see the docstring), not with a rewrite of its own.
        # A run that failed before applying anything changed no field: its
        # pending edit is kept and retried once the window is idle.
        # Automatic-generation parameters exist only once a result was applied.
        applied = (getattr(self, "_extraction_applied_revision", None) == self._analysis_revision
                   or self._auto_generation_params is not None)
        self._extraction_applied_revision = None
        if applied:
            self._clear_footprint_rewrite()
        # Il consuntivo riguarda l'analisi AI, che a questo punto e' conclusa:
        # va mostrato anche quando parte subito la generazione automatica, che
        # non fa alcuna chiamata al modello e uscirebbe da qui senza dirlo.
        self._show_ai_cost()
        if self._automatic_generation_active:
            return
        self._hide_analysis_overlay()
        self._refresh_extract_enabled()
        self._end_extraction_busy()
        params = self._auto_generation_params
        self._auto_generation_params = None
        self._auto_after_extraction = False
        # La pausa sta QUI e non dentro _on_extracted: i campi e la tabella dei
        # componenti sono gia' pieni (quindi la revisione mostra cio' che l'AI
        # ha davvero prodotto) e l'abbinamento dei modelli con la generazione
        # 3D non e' ancora partito, quindi le correzioni fanno ancora in tempo
        # a entrarci. Vale per entrambe le modalita': in quella standard
        # precede la generazione automatica, in quella avanzata e' comunque il
        # momento in cui il risultato va guardato.
        self._offer_inventory_review()
        if params is not None:
            self._start_automatic_generation(params)
        elif not applied:
            self._retry_footprint_rewrite()

    def _show_ai_cost(self) -> None:
        """@brief Write the cost of the last analysis in the AI log and the analysis modal.

        @note There is no label under the button any more (user,
            2026-09-24): the log keeps the history, the modal shows it while
            the user is looking. The text is built from the raw values, so it
            follows the interface language. The running PROJECT total and the
            balance are the «Credito AI» line (`ai_account`), refreshed here.
        """
        from ..services.openrouter import usage_totals

        totals = usage_totals()
        if not totals.calls:
            text = self.i18n.tr("analysis.cost.cached")
        else:
            amount = (f"{totals.cost_usd:.4f}" if totals.cost_usd >= 0.01
                      else f"{totals.cost_usd:.6f}")
            text = self.i18n.tr(
                "analysis.cost", amount=amount, calls=totals.calls,
                tokens=totals.prompt_tokens + totals.completion_tokens)
            if not totals.complete:
                text = self.i18n.tr("analysis.cost.partial", summary=text,
                                    missing=totals.calls_without_cost)
        # La label sotto il pulsante e' stata tolta (utente, 2026-09-24): lo
        # storico del costo resta nel Log AI -- anche dopo un'interruzione,
        # quando e' l'informazione piu' utile -- e il dato va nella modale
        # dell'ANALISI, dove l'utente sta gia' guardando. Non in quella della
        # generazione: vedi
        # `_clear_cost_for_generation` (decisione dell'utente, 2026-09-23).
        # Il caso conta davvero: nella corsa automatica `finished`
        # dell'estrazione arriva quando la generazione e' gia' partita.
        self._append_log(text)
        # Il saldo e' cambiato: si rilegge (gratuito, in background).
        self.ai_account.refresh_balance()
        if self._generation_phase_active():
            return
        self.analysis_overlay.set_cost(text)

    def _generation_phase_active(self) -> bool:
        """@brief Tell whether the progress modal is currently the generation modal.

        @return True during the automatic generation after an extraction, a
            manual generation with a 3D body, or a 3D regeneration.
        """
        worker = getattr(self, "_cad3d_worker", None)
        return bool(getattr(self, "_automatic_generation_active", False)
                    or getattr(self, "_manual_cad_overlay", False)
                    or (worker is not None and worker.isRunning()))

    def _clear_cost_for_generation(self) -> None:
        """@brief Remove the analysis cost from the modal as a generation phase starts.

        @note User decision (2026-09-23, note 4), which REVERSES the earlier
            choice: the automatic generation used to keep «Costo ultima
            analisi: … USD» on its modal, on the grounds that extraction and
            generation were one continuous operation. The user disagrees:
            footprint, KiCad matching and FreeCAD make no model call, so a cost
            on that modal reads as the price of what is happening now. Every
            generation path -- automatic, manual with a 3D body, «Rigenera
            modello 3D» -- now starts with a modal without cost. The cost stays
            where it belongs: in the AI modal during the analysis and in the
            AI log, where `_show_ai_cost` writes it (the label under the
            extraction button was removed on 2026-09-24, user decision).
        """
        self.analysis_overlay.clear_cost()

    def _offer_inventory_review(self) -> bool:
        """@brief Pausa fra l'inventario a pagamento e tutto cio' che ne discende.

        @return True quando la revisione dei componenti e' stata davvero aperta.
        @note Richiesta dell'utente: «dopo l'elaborazione AI stessa cosa, non ho
            visto la pausa». La revisione dei corpi esisteva gia' (lo stesso
            `PhotoReviewDialog` del comando «Modifica la revisione dei
            componenti…»), ma bisognava sapere di doverla cercare: l'analisi
            proseguiva da sola verso l'abbinamento dei modelli e FreeCAD, cioe'
            costruiva tutto sulla disposizione dell'AI senza che nessuno
            l'avesse guardata. Qui ci si ferma PRIMA, dove le correzioni
            contano ancora.
        @note Applicare prosegue con le correzioni: la revisione le scrive
            nella tabella dei componenti (`_apply_photo_review`), ed e' da li'
            che `_current_body_params()` prende i corpi per la generazione.
            Rifiutare (o annullare la revisione) prosegue con il risultato
            dell'AI cosi' com'e': non si tocca nulla.
        @note La pausa e' GRATUITA in ogni ramo: nessuna richiesta al modello,
            ne' per aprirla ne' per applicarla. Va detto nella domanda, perche'
            e' esattamente la paura dell'utente («devo poter tornare sui miei
            passi senza fare intervenire AI»).
        @note Si consuma `_inventory_pause_pending` all'ingresso: `finished`
            puo' arrivare due volte (vedi `_extract_finish_pending`) e la stessa
            pausa non va offerta due volte. `_inventory_pause_active` copre
            l'event loop annidato della revisione, da cui si puo' rientrare in
            questa finestra.
        @note Disciplina della modale identica a `_on_review_requested`: si
            nasconde solo se era visibile e si rimette esattamente com'era. Il
            tempo passato nascosta non viene contato come attesa del modello
            (`AnalysisOverlay.hideEvent/showEvent`), quindi l'orologio misura
            il lavoro e non il revisore.
        """
        pending, self._inventory_pause_pending = self._inventory_pause_pending, False
        if not pending or self._inventory_pause_active:
            return False
        if not self._inventory_pause_enabled():
            self._append_log(
                tx("Pausa dopo l'inventario AI: disattivata nelle impostazioni, si "
                   "prosegue con i corpi come li ha proposti l'AI."))
            return False
        components = self._photo_components_from_table()
        # Anche con i soli corpi esclusi c'e' qualcosa da rivedere: sono
        # proprio quelli che solo la revisione puo' tenere (HANDOFF 141).
        if not components and not self._photo_excluded_rows:
            return False
        if not (self._photo_png or self._photo_overlay_png):
            # Senza immagine la revisione non ha su cosa disegnare: fermare la
            # corsa per una finestra che non si aprirebbe sarebbe una pausa a
            # vuoto (stessa disciplina di `_refresh_component_review_button`).
            self._append_log(tx("Pausa dopo l'inventario AI non offerta: manca la fotografia calibrata "
                                "su cui mostrare i corpi."))
            return False
        answer = QMessageBox.question(
            self, self.i18n.text("Inventario AI completato"),
            self.i18n.tr(
                "inventory_pause.question", count=len(components),
                default=(
                    "L'inventario AI ha prodotto {count} ingombri.\n\n"
                    "Vuoi rivederli adesso (sposta, ridimensiona, aggiungi, elimina, "
                    "cambia tipo e modello KiCad) PRIMA dell'abbinamento dei modelli e "
                    "della generazione 3D?\n\n"
                    "La revisione è gratuita: nessuna chiamata AI. Rispondendo No si "
                    "prosegue con i corpi come li ha proposti l'AI; puoi comunque "
                    "riaprirla dopo con «Modifica la revisione dei componenti…»."),
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes)
        if answer != QMessageBox.StandardButton.Yes:
            self._append_log(tx("Pausa dopo l'inventario AI: revisione rifiutata, si prosegue con i "
                                "{n_components} corpi proposti dall'AI.",
                                n_components=len(components)))
            return False
        self._append_log(tx("Pausa dopo l'inventario AI: revisione dei {n_components} corpi aperta "
                            "su richiesta. Nessuna chiamata AI (gratuito).",
                            n_components=len(components)))
        overlay_was_visible = self.analysis_overlay.isVisible()
        self._inventory_pause_active = True
        try:
            if overlay_was_visible:
                self.analysis_overlay.hide()
            self._consult_consolidated_before_review()
            self._show_photo_comparison()
        except Exception as exc:  # noqa: BLE001 - vedi _on_review_requested
            # Un guasto della revisione non deve buttare via l'analisi appena
            # pagata: si annota e si prosegue con i corpi dell'AI, che e' lo
            # stesso esito del rifiuto.
            self._append_log(tx("ATTENZIONE revisione dei componenti non disponibile: {exc}", exc=exc))
        finally:
            self._inventory_pause_active = False
            if overlay_was_visible:
                self.analysis_overlay.show()
                self.analysis_overlay.raise_()
        return True

    def _consult_consolidated_before_review(self) -> None:
        """@brief Bring the accepted-matches archive into the review, before the build.

        @note User, 2026-10-08: «vedo l'applicazione del consolidato e ho la
            possibilita' di editare». The archive used to be consulted only
            inside the generation (step «Confronto con i consolidati»), so the
            review opened on bare bodies and the user's own earlier choices
            appeared only in the finished solid. Here the same comparison
            (`kicad_models._archive_prepass`: SQLite and crop descriptors, no
            FreeCAD, no AI) runs on the photo rows first: a body the archive
            MATCHES gets the model applied (`model_match` with attachment
            «accepted_library» and `model_path`), visible and editable in the
            review like any choice; a body «da verificare» carries the report
            with its single archive candidate, which the picker lists first,
            and no model is applied. Bodies with a confirmed manual choice are
            left alone by the archive itself. The generation repeats the
            comparison for whatever has no choice, so declining the pause
            changes nothing.
        @note Never fatal: an unreadable archive or library is logged and the
            review opens as before.
        """
        if self._is_single_component_project():
            return
        rows = self._photo_rows
        if not rows:
            return
        from ..cad3d import kicad_models
        try:
            attached, _count = self._attach_accepted_component_images(rows)
            results = kicad_models._archive_prepass(attached, None, self._append_log, None)
        except Exception as exc:  # noqa: BLE001 - l'archivio aiuta, non blocca
            self._append_log(tx("Consolidati prima della revisione non disponibili: {exc}", exc=exc))
            return
        applied = proposed = 0
        for row, item in zip(rows, attached):
            record = item.get('consolidated_match')
            if isinstance(record, dict):
                row['consolidated_match'] = record
            found = results.get(id(item)) if isinstance(results, dict) else None
            if not isinstance(found, dict) or not isinstance(record, dict):
                continue
            report = dict(found)
            if record.get('status') == 'matched' and report.get('selected_path'):
                report['attachment'] = 'accepted_library'
                report['attachment_reason'] = tx(
                    "Modello dall'archivio degli abbinamenti accettati, applicato prima della "
                    "revisione: modificabile nel selettore.")
                row['model_path'] = report['selected_path']
                # The verse comes with the model, as in the generation
                # (kicad_models.attach_models): only an individually verified
                # crop with an unambiguous angle transfers it; otherwise the
                # verse stays automatic and the report says it is unverified.
                orientation = report.get('accepted_orientation') or {}
                angle = orientation.get('rotation_deg')
                if (orientation.get('status') == 'matched' and isinstance(angle, (int, float))
                        and not isinstance(angle, bool) and math.isfinite(angle)):
                    row['model_rotation_deg'] = float(angle)
                applied += 1
            else:
                report['status'] = 'review_required'
                report['selected_path'] = None
                proposed += 1
            row['model_match'] = report
        self._append_log(tx("Consolidati prima della revisione: {applied} modelli applicati, "
                            "{proposed} proposti da confermare, su {total} corpi.",
                            applied=applied, proposed=proposed, total=len(rows)))

    def _on_review_requested(self, photo_png, proposals, length_mm, width_mm) -> None:
        """@brief Mostra le proposte del rilevatore (revisione unificata, stadio rilevatore) e restituisce al worker quelle da tenere.

        @param photo_png Fotografia calibrata, codificata in PNG.
        @param proposals Proposte del rilevatore locale.
        @param length_mm Estensione della scheda lungo X, in mm.
        @param width_mm Estensione lungo Y, in mm.
        @note Il worker e' fermo in attesa: qualunque cosa succeda qui, la
            risposta va consegnata, altrimenti l'estrazione non finisce piu'.
            Annullare consegna None, e l'estrazione si ferma senza pagare la
            riconciliazione -- che e' il motivo per cui questa vista sta prima
            di quella chiamata e non dopo. Se invece la vista non si apre
            affatto si prosegue con tutte le proposte: un difetto della
            revisione non deve buttare via l'analisi gia' pagata fin qui.
        @note La risposta va al MITTENTE del segnale, non allo slot
            `_extract_worker`: dopo «Interrompi» lo slot puo' gia' ospitare
            un'altra corsa (o nessuna), e sbloccare quella sbagliata lascerebbe
            il worker vero appeso per sempre.
        @note Una corsa abbandonata con «Interrompi» riceve None SENZA che si
            apra nulla (audit 25/09, B1): prima la vista si apriva lo stesso,
            l'OK sovrascriveva `photo-review/proposals.json`, rimostrava la
            modale -- che nessuna fine di corsa scaduta avrebbe piu' nascosto
            -- e con il rilevatore 'yolo' la corsa abbandonata proseguiva fino
            alla riconciliazione A PAGAMENTO. None la ferma prima di spendere.
        @note Il pretest solo YOLO salva nel proprio pacchetto
            (`_pretest_root`), mai in quello dell'estrazione: vedi
            `_store_reviewed_proposals`.
        """
        worker = self.sender() or self._extract_worker
        if self._review_request_is_stale(worker):
            self._append_log(tx("Revisione dei corpi di una corsa interrotta dall'utente: non "
                                "aperta, la corsa si ferma senza altre richieste."))
            if worker is not None:
                worker.deliver_review(None)
            return
        pretest = isinstance(worker, YoloPretestWorker)
        kept = list(proposals)
        try:
            from . import photo_review_dialog as review_module
            self.analysis_overlay.hide()
            # Stadio «rilevatore» della revisione unificata (HANDOFF 162): le
            # proposte tenute all'OK sono quelle che vanno alla chiamata.
            dialog = review_module.PhotoReviewDialog(
                photo_png, proposals, length_mm, width_mm, self,
                stage=review_module.STAGE_DETECTOR)
            self.i18n.retranslate_widgets(dialog)
            if getattr(worker, 'photo_detector', None) == 'yolo_smd':
                from PyQt6.QtWidgets import QDialogButtonBox
                dialog.buttons.button(QDialogButtonBox.StandardButton.Apply).setToolTip(
                    tx("I riquadri tenuti vengono classificati dalla rete SMD locale."))
                dialog.model_status.setText(tx("Footprint e STEP si cercano dopo la classificazione locale."))
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            kept = dialog.kept_components() if accepted else None
            self._append_log(
                tx("Revisione dei corpi rilevati: {n_kept} tenute su {n_proposals}.",
                   n_kept=len(kept),
                   n_proposals=len(proposals))
                if kept is not None else tx("Revisione dei corpi rilevati annullata."))
        except Exception as exc:  # noqa: BLE001 - un errore qui non deve appendere il worker
            self._append_log(tx("ATTENZIONE revisione dei corpi non disponibile: {exc}", exc=exc))
        finally:
            # La vista gira un event loop annidato: se nel frattempo la corsa
            # e' stata dichiarata scaduta, non si salva e non si riapre la
            # modale -- vale lo stesso ragionamento dell'inizio.
            if kept is not None and self._review_request_is_stale(worker):
                kept = None
            if worker is not None:
                worker.deliver_review(kept)
            if kept is not None:
                # Il pacchetto nella cartella del componente deve contenere le
                # proposte TENUTE, non quelle grezze: il detector-*.json in cache
                # viene scritto prima della revisione, quindi copiarlo dopo
                # riporterebbe i falsi positivi cancellati e perderebbe i
                # riquadri corretti. Si scrive qui, e _on_extracted non lo tocca.
                self._store_reviewed_proposals(
                    kept, photo_png, length_mm, width_mm,
                    detector=getattr(worker, 'photo_detector', 'yolo'), pretest=pretest)
                self.analysis_overlay.show()
                self.analysis_overlay.raise_()

    def _review_request_is_stale(self, worker) -> bool:
        """@brief True when a review request comes from a run the user abandoned.

        @param worker Worker that asked for the review (the signal sender).
        @return True for a worker carrying an older `_analysis_revision` or
            listed among the abandoned workers; False for None (direct call).
        @note Asked about the worker itself, not about `self.sender()`: the
            question is repeated after the nested event loop of the dialog,
            where the sender is no longer defined.
        """
        if worker is None:
            return False
        if any(worker is abandoned for abandoned in self._abandoned_workers):
            return True
        return getattr(worker, "_analysis_revision",
                       self._analysis_revision) != self._analysis_revision

    def _store_reviewed_proposals(self, kept, photo_png, length_mm, width_mm, *,
                                  detector='yolo', pretest=False) -> None:
        """@brief Write the proposals kept by the in-extraction review to the component folder.

        @param kept Proposal rows kept by the review dialog (may be empty).
        @param photo_png Calibrated photograph the review was made on.
        @param length_mm Board X extent in mm the boxes were calibrated with.
        @param width_mm Board Y extent in mm.
        @param detector Name of the detector the proposals came from.
        @param pretest True for the «Solo YOLO locale» pretest: the rows go to
            the pretest bundle (`_pretest_root`) and the extraction state is
            left alone.
        @note Marks the extraction as reviewed in every case, including an
            empty selection or a failed write: the cache rows are the
            pre-review set and must never replace what the user decided. A
            pretest is not an extraction and does not set the mark.
        """
        if not pretest:
            self._reviewed_this_extraction = True
        path = self._write_review_bundle([dict(p) for p in kept], photo_png, length_mm, width_mm,
                                         detector=detector,
                                         root=self._pretest_root() if pretest else None)
        if path is not None:
            self._append_log(tx("Proposte tenute dalla revisione salvate in {path} ({n_kept} corpi): "
                                "la revisione si puo' riaprire senza chiamate AI.",
                                path=path,
                                n_kept=len(kept)))

    def _on_phase(self, tool: str, detail: str, done: int, total: int) -> None:
        """@brief Porta in modale lo strumento al lavoro e il suo avanzamento.

        @param tool Chiave in analysis_overlay.TOOLS.
        @param detail Testo libero accanto al nome, es. il modello usato.
        @param done Elementi completati.
        @param total Elementi previsti; non positivo significa nessun conteggio.
        @note Il testo integrale continua ad andare nel log: qui arriva solo
            cio' che si legge a colpo d'occhio mentre si aspetta.
        """
        if tool == 'headers':
            self.analysis_overlay.set_steps(GENERATION_STEPS[-1:])
            self._generation_step_of = {'freecad': 0}
            self._append_log(detail)
            tool = 'freecad'
        self.analysis_overlay.set_tool(tool, detail)
        self.analysis_overlay.set_progress(done, total, tx("componenti"))
        # Fuori da una generazione la mappa non c'e' (o la modale non ha passi):
        # `set_current_step` allora non fa nulla.
        step = (getattr(self, "_generation_step_of", None) or {}).get(tool)
        if tool == 'consolidated' and step is not None:
            self.analysis_overlay.set_step_result(step, detail)
        if step is not None and step != self.analysis_overlay._step_current:
            self.analysis_overlay.set_current_step(step)
            # Nel log con l'ora: dice quando la finestra e' passata al passo,
            # cioe' quando e' cominciato il lavoro di chi lo esegue.
            if self.analysis_overlay._step_current == step:
                self._append_log(tx("Passo {n} di {total}: {label}", n=step + 1,
                                    total=len(self.analysis_overlay._step_labels),
                                    label=self.analysis_overlay._step_labels[step]))

    def _overlay_header_detail(self) -> str:
        """@brief Il dettaglio che l'intestazione della modale sta gia' mostrando.

        @return Il dettaglio dichiarato con l'ultimo `set_tool`, es. il modello
            in uso; stringa vuota quando nessuno strumento e' dichiarato.
        @note Si preferisce il valore memorizzato dalla modale
            (`AnalysisOverlay.tool_detail`) alla lettura dell'etichetta
            dipinta. L'etichetta e' presentazione: se un giorno viene mandata a
            capo o accorciata con i puntini, un confronto sul testo dipinto
            comincerebbe a mentire senza che nulla segnali il guasto. La
            vecchia giustificazione («`start()` azzera l'intestazione, una
            copia sopravviverebbe») era sbagliata: `start()` azzera passando da
            `set_tool(None)`, che e' esattamente il punto in cui anche il
            valore memorizzato viene azzerato.
        @note Ripiego sull'analisi dell'etichetta finche' la modale non espone
            l'attributo: l'accessorio e l'etichetta accorciata arrivano da due
            modifiche diverse e devono poter atterrare in qualunque ordine.
        """
        stored = getattr(self.analysis_overlay, "tool_detail", None)
        if stored is not None:
            return " ".join(str(stored).split())
        label = getattr(self.analysis_overlay, "tool", None)
        text = label.text() if label is not None else ""
        _, separator, detail = str(text).partition("·")
        return " ".join(detail.split()) if separator else ""

    def _restates_overlay_header(self, message: str) -> bool:
        """@brief Dice se la riga non aggiunge nulla all'intestazione della modale.

        @param message Riga di avanzamento.
        @return True solo quando la riga INTERA e' il dettaglio che
            l'intestazione mostra gia'.
        @note Richiesta dell'utente: «non ripetere sotto il modello lo hai
            scritto sopra, ma spiega cosa stai facendo». L'intestazione dice
            «Modello AI (remoto, a consumo) · anthropic/claude-opus-5» e la
            riga sotto ripeteva lo stesso identificatore, sprecando l'unico
            posto in cui si puo' dire cosa sta accadendo.
        @note Il confronto e' sull'intera riga. La versione precedente
            scartava anche la forma «testa: <dettaglio>» buttando via la
            testa, che e' la meta' informativa: «Fallita la chiamata a:
            anthropic/claude-opus-5.» spariva dalla modale proprio perche'
            finiva col nome del modello, cioe' un avviso di fallimento
            cancellato dall'unico posto in cui si guardava. Distinguere
            un'etichetta («Modello AI unico») da un evento («Estrazione
            fallita») richiede di sapere se la testa contiene un verbo:
            l'italiano non si analizza con un'espressione regolare, e ogni
            criterio ortografico provato (numero di parole, parole funzione,
            maiuscole) separava l'esemplare osservato e non la classe. Meglio
            far passare una riga in parte ridondante che cancellare un avviso.
        @note Il punto finale si toglie da ENTRAMBI i lati. Prima veniva
            tolto solo alla riga: un dettaglio che finisce con un punto --
            e' il caso di ogni frase messa come dettaglio di fase -- rendeva il
            confronto sempre falso, cioe' spegneva il filtro in silenzio.
        """
        detail = self._overlay_header_detail().rstrip(".")
        if not detail:
            return False
        text = " ".join(str(message or "").split()).rstrip(".")
        return text == detail

    def _on_ai_progress(self, message: str) -> None:
        """@brief Porta la riga nel log e, se e' leggibile a colpo d'occhio, in modale.

        @param message Riga di avanzamento di un worker.
        @note Il log riceve SEMPRE il testo integrale: la modale e' l'unico
            posto filtrato, perche' ha una riga sola e serve a dire quale fase
            e' in corso. Restano fuori il dettaglio per-elemento, che il
            contatore riassume gia' (vedi per_item_detail), e la ripetizione
            del dettaglio dell'intestazione.
        """
        if not (per_item_detail(message) or self._restates_overlay_header(message)):
            self.analysis_overlay.set_status(message)
        # Testo del modello: inserimento letterale, mai interpretato come HTML.
        if re.match(r"^#\d+", message.strip()):
            self.ai_elements_list.addItem(message)
        self._append_log(message)

    def _refresh_ai_copy_buttons(self, *args) -> None:
        """@brief Riallinea i comandi che dipendono dal contenuto del log.

        @note Collegato ai cambi del testo dell'analisi e dell'elenco degli
            elementi: e' il punto in cui il log passa da assente a presente,
            quindi anche il punto in cui «Log AI» deve comparire.
        """
        self.btn_copy_ai_log.setEnabled(bool(self.ai_progress_text.toPlainText()))
        self.btn_copy_ai_elements.setEnabled(self.ai_elements_list.count() > 0)
        self._refresh_ai_log_button()

    def _copy_ai_output(self, *, elements=False) -> None:
        text = ('\n'.join(self.ai_elements_list.item(i).text() for i in range(self.ai_elements_list.count()))
                if elements else self.ai_progress_text.toPlainText())
        if text:
            QApplication.clipboard().setText(text)

    def _show_photo_comparison(self, _checked=False, *, component_row=None) -> None:
        if component_row is not None and not self._photo_png:
            # Dal 3D (doppio clic o «Cambia modello KiCad...»): senza foto la
            # revisione non si apre, e prima il clic restava muto.
            QMessageBox.information(
                self, self.i18n.text("Foto non disponibile"),
                self.i18n.text("La scelta del modello KiCad passa dalla revisione fotografica, "
                               "che richiede la foto calibrata del progetto: ripeti l'analisi "
                               "con la foto dall'alto. Nome, tipo, posizione e quote si "
                               "modificano comunque con «Modifica componenti…»."))
            return
        if self._photo_png:
            length, width = self._photo_review_frame()
            # I corpi solo-AI esclusi tornano in coda, dopo quelli della
            # tabella: le righe della tabella restano gli stessi indici
            # (`component_row`), e la revisione e' l'unico posto dove si
            # possono tenere (HANDOFF 141).
            try:
                dialog = PhotoReviewDialog(
                    self._photo_png,
                    self._photo_components_from_table() + deepcopy(self._photo_excluded_rows),
                    length, width, self,
                )
            except ValueError as exc:
                QMessageBox.warning(
                    self, self.i18n.text("Revisione foto non disponibile"), str(exc))
                return
            dialog.setWindowTitle(
                self.i18n.tr("photo_review.title")
            )
            # «Applica» emette solo i corpi tenuti; quelli lasciati fuori si
            # leggono dalla finestra nello stesso istante e restano da parte.
            dialog.components_applied.connect(
                lambda components: self._apply_photo_review(
                    components, left_out=dialog.left_out_components()))
            self.i18n.retranslate_widgets(dialog)
            if component_row is not None and 0 <= component_row < len(dialog._items):
                dialog._select_row(component_row)
                QTimer.singleShot(0, dialog.choose_model)
            dialog.exec()
            return
        if not self._photo_overlay_png:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(
            self.i18n.tr("photo_review.last_extraction", source=self._photo_source)
        )
        dialog.resize(1100, 650)
        layout = QVBoxLayout(dialog)
        note = QLabel(
            "Rettangoli dell'ultima estrazione: verde = rilevato, arancione = da verificare. "
            "I numeri seguono l'ordine dei componenti di «Modifica componenti…» prima delle "
            "modifiche manuali."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        area = QScrollArea()
        label = QLabel()
        pixmap = QPixmap()
        pixmap.loadFromData(self._photo_overlay_png, "PNG")
        label.setPixmap(pixmap)
        area.setWidget(label)
        layout.addWidget(area)
        self.i18n.retranslate_widgets(dialog)
        dialog.exec()

    def _body_sizes(self) -> dict:
        """Current body size (mm) of each set of body fields."""
        return {"cast": (self.field_cast_body_length.value(), self.field_cast_body_width.value()),
                "matrix": (self.field_body_length.value(), self.field_body_width.value())}

    def _body_fields_matching(self, length, width):
        """The set of body fields holding `length` x `width` (within 0.01 mm), or None."""
        frame = _positive_frame(length, width)
        if frame is None:
            return None
        current = "cast" if self._is_castellated() else "matrix"
        sizes = self._body_sizes()
        for key in (current, "cast", "matrix"):
            if all(abs(a - b) <= 0.01 for a, b in zip(sizes[key], frame)):
                return key
        return None

    def _project_has_footprint(self) -> bool:
        """@brief True when the open project already has its generated footprint."""
        name = self._component_name()
        return bool(name) and (self._project_dir() / f"{name}.pretty"
                               / f"{name}.kicad_mod").is_file()

    def _photo_review_frame(self) -> tuple[float, float]:
        """@brief Board size (mm) that gives the photo its scale in the review.

        @return The CURRENT values of the body fields that describe the
            photographed board: those of the type used by the extraction, or,
            for a reopened project, those that match the saved review bundle.
            Without that knowledge, the fields of the current type.
        @note Two different situations must not be confused (2026-09-26): a
            correction of the body size after the extraction must change the
            scale (the photo shows the real board, now measured better), a
            change of component type must not (its fields describe another
            body; reading them put the boxes six times off the photo). When
            the two sets disagree, the log says which one the review uses.
        """
        sizes = self._body_sizes()
        current = "cast" if self._is_castellated() else "matrix"
        key = getattr(self, "_photo_body_fields", None) or current
        if key != current and any(abs(a - b) > 0.01 for a, b in zip(sizes[key], sizes[current])):
            self._append_log(tx(
                "Revisione foto: la foto mostra la scheda di {length:.2f} x {width:.2f} mm "
                "dell'analisi; la tipologia attuale usa un corpo di {current_length:.2f} x "
                "{current_width:.2f} mm, controlla la tipologia.",
                length=sizes[key][0], width=sizes[key][1],
                current_length=sizes[current][0], current_width=sizes[current][1]))
        return sizes[key]

    def _photo_components_from_table(self) -> list[dict]:
        """Current photo components: geometry plus the detection metadata of each one."""
        length = (self.field_cast_body_length.value() if self._is_castellated()
                  else self.field_body_length.value())
        width = (self.field_cast_body_width.value() if self._is_castellated()
                 else self.field_body_width.value())
        transform = PhotoTransform(1, 1, length, width)
        result = []
        for row in self._photo_rows:
            entry = deepcopy(row)
            if "_box" in entry:
                entry["_box"] = transform.normalized_box(entry)
            result.append(entry)
        return result

    def _apply_photo_review(self, components: list[dict], left_out=None) -> None:
        """@brief Apply reviewed components and bring the 3D model up to date.

        @param components Corrected component list (review dialog or 3D menu).
        @param left_out AI-only bodies the photo review left out
            (`PhotoReviewDialog.left_out_components`), or None when the caller
            is not the photo review (hand editor, format painter): the bodies
            already set aside are then kept as they are.
        @note The old solid no longer matches the data and is cleared (rule 7
            of the project strategy). When a solid WAS on screen, the 3D is
            regenerated automatically (user, 2026-09-24: otherwise the 3D tab
            stays black): FreeCAD only, no AI request. Without a previous
            solid nothing turns black, and generation stays the user's choice.
        """
        had_solid = self._last_mesh_path is not None
        self._set_photo_components(components)
        if left_out is not None:
            self._photo_excluded_rows = [deepcopy(c) for c in left_out]
            if self._photo_excluded_rows:
                self._log_excluded_ai_only()
        if self._photo_png:
            from ..services.model_choices import save_choices
            try:
                path = save_choices(self._docs_dir().parent, self._photo_png, components)
                self._append_log(tx("Scelte e candidati KiCad salvati: {path}", path=path))
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, tx("Scelte KiCad non salvate"), str(exc))
        self._invalidate_generated_mesh()
        if had_solid:
            self._append_log(tx("Revisione fotografica applicata: {n_components} componenti; "
                                "rigenerazione automatica del modello 3D.",
                                n_components=len(components)))
            self._regen_3d_pending = True
            # Dopo la chiusura della finestra di revisione, che emette le
            # correzioni mentre e' ancora aperta.
            QTimer.singleShot(0, self._run_pending_regen_3d)
            return
        self._set_3d_status(self.i18n.text(
            "Componenti corretti dalla foto. Modello 3D da rigenerare: "
            "premi \"Rigenera modello 3D\" per aggiornare STEP e anteprima."
        ), STATUS_INFO)
        self._append_log(tx("Revisione fotografica applicata: {n_components} componenti; 3D da "
                            "rigenerare.",
                            n_components=len(components)))

    def _run_pending_regen_3d(self) -> None:
        """@brief Start the 3D regeneration an «Apply» asked for, when possible.

        @note Nothing to do once a solid of the current data is on screen
            (another generation got there first, e.g. the automatic pipeline).
            While other work runs the request stays pending and is retried
            when the progress window closes (`_hide_analysis_overlay`): two
            FreeCAD runs never overlap. A disabled 3D model is a choice, not
            an error: the request is dropped with a line in the log, no dialog.
        """
        if not self._regen_3d_pending:
            return
        if self._last_mesh_path is not None:
            self._regen_3d_pending = False
            return
        if self._background_work_running() or self._automatic_generation_active:
            return
        self._regen_3d_pending = False
        if self._current_body_params() is None:
            self._set_3d_status(self.i18n.text(
                "Componenti corretti dalla foto. Modello 3D da rigenerare: "
                "premi \"Rigenera modello 3D\" per aggiornare STEP e anteprima."
            ), STATUS_INFO)
            self._append_log(tx("Rigenerazione automatica del 3D non avviata: il gruppo "
                                "«Modello 3D» è spento."))
            return
        # Automatic retry: no question (`_regenerate_3d` still gives the
        # worker the applied pads, never the footprint draft's).
        self._regen_3d_automatic = True
        try:
            self._regenerate_3d()
        finally:
            self._regen_3d_automatic = False

    def _invalidate_generated_mesh(self) -> None:
        # Running CAD workers still describe the previous input revision.
        self._model_revision += 1
        self._last_mesh_path = None
        self._drop_mesh_request()
        # Il solido che si stava per accettare non corrisponde piu' ai dati.
        self._set_accept_models_ready(False)
        self.preview_3d.clear_preview()
        self._set_3d_status(
            self.i18n.text("Dati modificati. Modello 3D da rigenerare."), STATUS_INFO)

    def _cad_result_is_current(self) -> bool:
        worker = self.sender()
        return worker is None or worker._model_revision == self._model_revision

    def _log_proposed_name(self, name) -> None:
        """@brief Scrive nel Log AI il nome che l'analisi propone, se diverso.

        @param name Nome proposto dall'estrazione.
        @note Solo una riga di log (utente, 2026-09-24): la riga con «Usa
            questo nome» e' stata tolta, il nome del progetto si cambia da
            File > Rinomina progetto. La proposta non tocca nulla.
        """
        proposed = " ".join(str(name or "").split())
        if proposed and proposed != self._component_name():
            self._append_log(tx("L'analisi propone il nome «{name}»; per usarlo: File > "
                                "Rinomina progetto…", name=proposed))

    def _init_project_settings(self) -> None:
        """@brief Fotografa le impostazioni generali e avvia il salvataggio automatico.

        @note La fotografia e' presa a finestra costruita e prima di qualunque
            progetto: e' lo stato da cui parte un progetto NUOVO (decisione
            dell'utente del 2026-09-24).
        """
        self._restoring_project = False
        self._project_config_refused = None
        self._project_options = {}
        self._open_project_folder = None
        self._general_project_state = self._project_state()
        # '' in `top_text` means «no writing» (a choice); the snapshot of a
        # window without a project is not a choice, so a new project starts
        # without the key and gets the component name (review C, 1).
        self._general_project_state.pop("top_text", None)
        # Collegato qui e non alla costruzione del campo: prima della
        # fotografia generale non c'e' un progetto da salvare e chiudere.
        # Typed text is committed only by Enter (returnPressed): a keystroke
        # is not a decision, and neither is leaving the field (Alt+Tab, a
        # click on «Sfoglia»), which restores the committed folder. A text
        # set by the program (the browse button, a restored folder) is
        # already a decision.
        self._library_root_committed = self.field_library_root.text()
        self.field_library_root.textChanged.connect(self._on_library_root_text_changed)
        self.field_library_root.returnPressed.connect(self._on_library_root_edit_finished)
        self.field_library_root.editingFinished.connect(self._on_library_root_focus_left)
        self._last_saved_project_state = None
        self._project_autosave = QTimer(self)
        self._project_autosave.setInterval(PROJECT_AUTOSAVE_MS)
        self._project_autosave.timeout.connect(self._save_project_config)
        self._project_autosave.start()

    def _project_state(self) -> dict:
        """@brief Tutte le impostazioni del progetto aperto, come oggetti JSON.

        @return Dizionario con campi, modelli di sessione, tabelle, annotazioni,
            pinout e opzioni. Mai chiavi API: non stanno in nessun campo e le
            impostazioni del profilo AI restano nel database.
        """
        from dataclasses import asdict
        from .project_state import widget_value
        widgets = {}
        for name in PROJECT_WIDGETS:
            widget = getattr(self, name, None)
            if widget is not None:
                widgets[name] = widget_value(widget)
        # The footprint fields are the APPLIED ones: an unapplied draft of
        # the footprint preview never reaches the file, whoever saves it
        # (autosave timer, a parameter change, a project switch).
        applied = self._applied_footprint_state()
        package = None
        if self.package_geometry is not None:
            package = {**asdict(self.package_geometry), "name": self._component_name()}
            if applied["package_pins"] is not None:
                package["pins"] = [asdict(pin) for pin in applied["package_pins"]]
        state = {
            "project_kind": getattr(self, "project_kind", None),
            "widgets": widgets,
            "package_geometry": package,
            "host_mounting": self._mounting_state(),
            "footprint_texts": {key: list(value) for key, value in applied["texts"].items()},
            "footprint_drawings": deepcopy(applied["drawings"]),
            "metadata": self._metadata_state(),
            "models": {"session_model": getattr(self, "_session_model", None),
                       "vlm_model": getattr(self, "_vlm_model", None)},
            "sides": self._serialised_sides(),
            # Con i corpi solo-AI esclusi in coda, marcati: riaprendo il
            # progetto tornano da parte, non in tabella (HANDOFF 141).
            "components": (self._photo_components_from_table()
                           + deepcopy(getattr(self, "_photo_excluded_rows", []))),
            "annotations": {"convention": ANNOTATION_CONVENTION,
                            "fillets": [asdict(a) for a in self.fillet_annotations],
                            "texts": [asdict(a) for a in self.face_text_annotations]},
            # '' is a choice (no writing); a missing key is a project saved
            # before the field existed and gets the component name.
            "top_text": self._top_text_3d(),
            "pinout": deepcopy(self.pinout or []),
            "pin_map": (None if not getattr(self, "pin_map", None) else
                        [[pin, row, col] for pin, (row, col) in sorted(self.pin_map.items())]),
            "declared_row_spacing": getattr(self, "_declared_row_spacing", None),
            "board_size_known": bool(getattr(self, "_board_size_known", False)),
            "yolo_settings": self._chosen_yolo_settings(),
            "pcb_thickness": {
                "source": getattr(self, "_pcb_thickness_source", "standard"),
                "analysis": (list(self._analysis_pcb_thickness)
                             if getattr(self, "_analysis_pcb_thickness", None) else None)},
            "options": {"include_nc_pins": self._include_nc_pins(),
                        "silk_pin_labels": self._silk_pin_labels(),
                        "show_pins_3d": self._show_pins_3d(),
                        "pin_numbers_3d": self._pin_numbers_3d(),
                        "pause_after_inventory": self._inventory_pause_enabled()},
        }
        if getattr(self, "_fp_file_stale", False):
            # Only when true: projects without the key read as «up to date».
            state["footprint_file_stale"] = True
        return state

    def _apply_project_state(self, state: dict) -> None:
        """@brief Rimette nella finestra le impostazioni salvate di un progetto.

        @param state Oggetto prodotto da `_project_state` (letto dal file o
            fotografato all'avvio). Chiavi mancanti o valori che non entrano in
            un campo lasciano il campo com'e', con una riga di log; fanno
            eccezione i file di pesi (MODEL_FILE_WIDGETS), che tornano al
            predefinito.
        @note Le opzioni vanno in `_project_options`, mai in `self._config`:
            config.json resta il default dei progetti nuovi finche' l'utente
            non le cambia dalle Opzioni di generazione. Un'opzione che lo
            stato non dichiara torna al valore generale.
        """
        from .project_state import set_widget_value, saved_package_geometry
        from ..cad3d.annotations import EdgeFilletAnnotation, FaceTextAnnotation
        # Validate before changing fields. Files read from disk have already
        # passed this check; direct callers must obey the same contract.
        package_geometry = saved_package_geometry(state.get("package_geometry"))
        if package_geometry is not None:
            # The folder is the project's identity, including after a rename.
            package_geometry.name = self._component_name()
        skipped = []
        for name in PROJECT_WIDGETS:
            widget = getattr(self, name, None)
            values = state.get("widgets") if isinstance(state.get("widgets"), dict) else {}
            if widget is not None and name in values and not set_widget_value(widget, values[name]):
                skipped.append(name)
            elif widget is not None and name not in values and name in MODEL_FILE_WIDGETS:
                # Un file di pesi non dichiarato dal progetto (salvato prima del
                # campo) e' il predefinito, non quello del progetto aperto prima
                # (verifica avversariale B3, 2026-10-02).
                widget.clear()
        saved_kind = state.get("project_kind")
        family_kind = self._project_kind_for_family(self._selected_family())
        self.project_kind = (saved_kind if isinstance(saved_kind, str)
                             and saved_kind in _PROJECT_KINDS else family_kind)
        # Parametri YOLO del progetto; assenti (progetti precedenti) = predefiniti.
        raw_yolo = state.get("yolo_settings")
        if raw_yolo is None:
            saved_yolo = {}
        elif isinstance(raw_yolo, dict):
            saved_yolo = dict(raw_yolo)
        else:
            # Prima veniva scartato in silenzio (audit 25/09, B10).
            saved_yolo = {}
            skipped.append(f"yolo_settings ({type(raw_yolo).__name__})")
        # Salvati il 25/09 prima di questa versione: la soglia stava in un campo.
        legacy_threshold = (state.get("widgets") or {}).get("field_yolo_confidence") \
            if isinstance(state.get("widgets"), dict) else None
        if "confidence" not in saved_yolo and isinstance(legacy_threshold, (int, float)) \
                and not isinstance(legacy_threshold, bool):
            saved_yolo["confidence"] = round(float(legacy_threshold), 2)
        self._yolo_settings = self._valid_yolo_settings(saved_yolo, skipped)
        self._refresh_yolo_summary()
        models = state.get("models") if isinstance(state.get("models"), dict) else {}
        session = models.get("session_model")
        self._session_model = session if isinstance(session, str) and session else None
        self._refresh_model_combo()
        vlm = models.get("vlm_model")
        self._vlm_model = vlm if isinstance(vlm, str) and vlm else None
        self.field_vlm_model.setText(self._photo_detector_model())
        self._sides.clear()
        for side in _project_list(state, "sides", skipped):
            if isinstance(side, dict):
                # Un progetto salvato prima della chiave e' stato generato con
                # lo stile di allora: lo si conserva, non e' una deduzione.
                self._add_side_row(side, missing_style=default_contact_style())
        # Anche senza lati salvati: la barra del tipo contatto non deve restare
        # quella del progetto precedente.
        self._sync_contact_style_bar()
        components = [c for c in _project_list(state, "components", skipped) if isinstance(c, dict)]
        try:
            self._set_photo_components(components, replace_excluded=True)
        except (KeyError, TypeError, ValueError) as exc:
            skipped.append(f"componenti ({exc})")
            self._set_photo_components([], replace_excluded=True)
        annotations = state.get("annotations") if isinstance(state.get("annotations"), dict) else {}
        self.fillet_annotations.clear()
        self.face_text_annotations.clear()
        # Progetti salvati prima del 2026-09-25 (convenzione 1 o assente):
        # front era il lato in alto della vista dall'alto. Il lato fisico
        # resta quello, cambia solo il nome: si converte una volta, lo si dice.
        legacy = annotations.get("convention") != ANNOTATION_CONVENTION
        converted = 0
        for kind, target, values, key in ((EdgeFilletAnnotation, self.fillet_annotations,
                                           _project_list(annotations, "fillets", skipped,
                                                         "annotations.fillets"), "edge"),
                                          (FaceTextAnnotation, self.face_text_annotations,
                                           _project_list(annotations, "texts", skipped,
                                                         "annotations.texts"), "face")):
            for entry in values:
                try:
                    entry = dict(entry)
                    if legacy and isinstance(entry.get(key), str):
                        renamed = swap_front_back(entry[key])
                        converted += renamed != entry[key]
                        entry[key] = renamed
                    target.append(kind(**entry))
                except (TypeError, ValueError):
                    skipped.append(kind.__name__)
        if converted:
            self._append_log(tx("Annotazioni 3D: {count} nomi fronte/retro convertiti alla "
                                "convenzione del cubo 3D (il lato fisico non cambia).",
                                count=converted))
        self.pinout = [dict(entry) for entry in _project_list(state, "pinout", skipped)
                       if isinstance(entry, dict)]
        pin_map = state.get("pin_map")
        try:
            self.pin_map = ({int(p): (int(r), int(c)) for p, r, c in pin_map}
                            if pin_map else None)
        except (TypeError, ValueError):
            self.pin_map = None
            skipped.append("pin_map")
        spacing = state.get("declared_row_spacing")
        self._declared_row_spacing = (
            float(spacing) if isinstance(spacing, (int, float)) and not isinstance(spacing, bool)
            and math.isfinite(float(spacing)) and float(spacing) > 0 else None)
        known = state.get("board_size_known")
        # Progetti salvati prima del 26/09: le quote sono note se il progetto
        # ha gia' un footprint, cioe' se un'analisi le ha scritte.
        self._board_size_known = (known if isinstance(known, bool)
                                  else self._project_has_footprint())
        options = state.get("options") if isinstance(state.get("options"), dict) else {}
        self._project_options = {key: options[key] for key in PROJECT_OPTIONS
                                 if isinstance(options.get(key), bool)}
        self._restore_top_text(state.get("top_text"))
        self._pcb_thickness_source, self._analysis_pcb_thickness = _saved_pcb_thickness(
            state.get("pcb_thickness"))
        self._restore_mounting_state(state.get("host_mounting"))
        from ..core.footprint import validate_text_positions, validate_footprint_drawings
        self.footprint_text_positions = validate_text_positions(state.get("footprint_texts"))
        # Texts saved before 2026-10-14 are centred on their point: move the
        # point once to the bottom-left corner the preview drew, so they stay
        # where the user sees them (saved with the project's next save).
        self.footprint_drawings = _migrate_drawing_texts(
            validate_footprint_drawings(state.get("footprint_drawings")))
        self._fp_drawings_undo, self._fp_drawings_redo = [], []
        # What the file holds is the applied state: no draft, nothing offered
        # until `_offer_footprint_recovery` reads this project's recovery file.
        self._fp_applied = None
        self._fp_recoverable = None
        self._fp_recoverable_source = None
        self._fp_draft_was_pending = False
        self._fp_file_stale = state.get("footprint_file_stale") is True
        # A rewrite still pending belongs to the project being left: the
        # one now open has its own .kicad_mod, written by its own edits.
        self._clear_footprint_rewrite()
        self._end_footprint_drawing_interaction()
        self._refresh_footprint_undo_buttons()
        self._cancel_footprint_drawing()
        self._refresh_type_effect()
        self.package_geometry = package_geometry
        # After the package: the designator proposal reads its family.
        self._restore_metadata_state(state.get("metadata"))
        self._set_regen_busy(False)
        if skipped:
            self._append_log(tx("Impostazioni del progetto non applicate (valori non validi): ")
                             + ", ".join(skipped) + ".")

    @staticmethod
    def _valid_yolo_settings(saved: dict, skipped: list) -> dict:
        """@brief Keep the valid YOLO settings of a project, key by key.

        @param saved Settings read from the project file.
        @param skipped List that receives one entry per ignored key, for the log.
        @return Complete validated settings: every valid saved key, the
            default for the others.
        @note One bad key used to reset them all to the defaults (audit 25/09,
            B10). Each key is checked on its own against the service's
            validator, which stays the only definition of a valid value. The
            file is not rewritten here: `_switch_project` records the applied
            state as already saved, so it is written only when the user
            changes something.
        """
        accepted = {}
        for key, value in saved.items():
            try:
                validate_yolo_settings({**accepted, key: value})
            except (TypeError, ValueError) as exc:
                skipped.append(f"yolo_settings.{key} ({exc})")
                continue
            accepted[key] = value
        return validate_yolo_settings(accepted)

    def _switch_project(self, name: str, state: dict) -> None:
        """@brief Apre `name` con le impostazioni `state`, senza salvarle a meta'."""
        self._restoring_project = True
        try:
            self._reset_project_runtime_state()
            self.field_name.setText(name)
            self._apply_project_state(state)
            self._restore_saved_mesh()
            self._restore_saved_photo()
        finally:
            self._restoring_project = False
        # La selezione della tipologia e il prompt sono ripristinati da widget
        # separati; i segnali del combo possono essere bloccati durante il
        # ripristino. Risincronizzare il blocco generato evita, per esempio,
        # che un progetto THT singolo invii ancora istruzioni castellated.
        selected_key = self.combo_component_type.currentData() or ""
        selected_row = self.profile_store.get_type(selected_key)
        selected_type = (component_types.ComponentType(
            selected_key, selected_row["label"], selected_row["instructions"])
            if selected_row else None)
        old_prompt = self.field_component_prompt.toPlainText()
        # Only a prompt that CARRIES a generated block is resynchronised (a
        # stale castellated block in a THT project); a prompt without one is
        # the project's own text, saved as such, and must come back verbatim
        # (contract of test_project_config since 2026-09-24).
        if component_types.BLOCK_START in old_prompt:
            restored_prompt = component_types.apply_block(old_prompt, selected_type)
        else:
            restored_prompt = old_prompt
        prompt_was_normalized = restored_prompt != old_prompt
        self.field_component_prompt.setPlainText(restored_prompt)
        # Ripristinare i widget non emette sempre il segnale del tipo
        # componente. Aggiornare esplicitamente i gruppi dipendenti evita di
        # lasciare nel progetto singolo i controlli di montaggio della board
        # aperta prima (anteprima footprint, analisi e toolbar 3D).
        self._refresh_type_effect()
        if hasattr(self, "combo_photo_detector"):
            self._refresh_photo_detector_controls()
            self._refresh_ai_tools_summary()
        # Le schede di anteprima seguono cio' che il progetto aperto contiene.
        self._refresh_output_actions()
        # Il simbolo applicato fa parte degli artefatti del componente. Nei
        # progetti gia' generati lo creiamo se manca, senza toccare un file
        # .kicad_sym eventualmente gia' esportato o personalizzato.
        # The symbol is written by a generation or by the user's export, never
        # on open (review C, 5: an unreviewed .kicad_sym with default rows
        # bypassed the export review), and the footprint is never rebuilt on
        # open (review C, 4: every open overwrote the .kicad_mod).
        self._refresh_symbol_tab(force=True)
        # La scheda aperta mostra il progetto nuovo, non quello chiuso: senza,
        # restava il disegno del precedente e il 3D non si ridisegnava (audit
        # 25/09). Le altre schede si ridisegnano all'ingresso, come sempre.
        self.project_wizard.set_page(1 if self._project_has_footprint() else 0)
        self.project_details.setExpanded(False)
        self.group_ai_tools.setExpanded(False)
        # Another project, another footprint: the camera follows it again.
        if hasattr(self, "preview_view"):
            self.preview_view.auto_fit = True
        index = self.tabs.currentIndex()
        if index == TAB_PREVIEW:
            self._draw_preview()
        elif index == TAB_SYMBOL:
            self._refresh_symbol_tab(force=True)
        elif index == TAB_MODEL_3D:
            self._enter_3d_tab()
        # Lo stato appena applicato e' quello salvato: non va riscritto.
        self._last_saved_project_state = self._project_state_text()
        # A footprint draft left by a crash is offered, never applied.
        self._offer_footprint_recovery()
        if prompt_was_normalized:
            self._save_project_config(force=True)
        # Dopo il nome e dopo lo stato: i pulsanti di revisione guardano la
        # cartella del progetto appena aperto, non piu' la foto del precedente.
        if hasattr(self, "btn_component_review") and not self._background_work_running():
            self._refresh_reopen_review_button()

    def _reset_project_runtime_state(self) -> None:
        """@brief Dimentica cio' che resta in memoria del progetto chiuso.

        @note Revisore 2, difetto 5: aprendo o creando un progetto restavano
            la foto calibrata, la sovrapposizione, l'ultima estrazione, la mesh
            e i flag di revisione del progetto PRECEDENTE, e «Modifica la
            revisione» del nuovo lavorava sulla foto del vecchio scrivendo
            nella cartella del nuovo. Si azzera la stessa lista dell'inizio di
            un'estrazione, piu' cio' che solo l'estrazione produce e che il
            file di progetto non conserva (geometria del package, spessore
            PCB estratto): un progetto riaperto riparte dai suoi campi.
        """
        self._extraction_started_stamp = None
        self.project_kind = None
        self._declared_row_spacing = None
        self._reviewed_this_extraction = False
        self._relaunch_base_synthesized = False
        self._last_extracted = None
        self._photo_png = None
        self._photo_body_fields = None
        self._photo_overlay_png = None
        self._photo_source = ""
        self.package_geometry = None
        self._extracted_pcb_thickness = None
        # La provenienza dello spessore torna dal file del progetto aperto
        # (_apply_project_state); qui non resta quella del progetto chiuso.
        self._pcb_thickness_source = "standard"
        self._analysis_pcb_thickness = None
        # Una rigenerazione chiesta nel progetto chiuso non riguarda il nuovo.
        self._regen_3d_pending = False
        # L'estrazione fatta nella sessione era del progetto chiuso.
        self._acquisition_completed = False
        # Anche l'ultima generazione: l'anteprima 2D non deve mostrare il
        # footprint del progetto precedente (`_preview_params`).
        self._last_params = None
        self._invalidate_generated_mesh()
        self._set_review_buttons_enabled(False)

    def _project_state_text(self) -> str | None:
        """Stato del progetto come testo confrontabile, o None se non si puo' leggere.

        Gira anche dal timer del salvataggio automatico: in PyQt6 un'eccezione
        che esce da uno slot chiude il programma, quindi qui si assorbe tutto
        e lo si dice una volta nel log.
        """
        try:
            text = json.dumps(self._project_state(), sort_keys=True, allow_nan=False,
                              ensure_ascii=False, default=str)
        except Exception as exc:  # noqa: BLE001 - il salvataggio non deve far cadere la finestra
            if not getattr(self, "_project_state_error_logged", False):
                self._project_state_error_logged = True
                self._append_log(tx("Impostazioni del progetto non leggibili: {exc}", exc=exc))
            return None
        self._project_state_error_logged = False
        return text

    def _save_project_config(self, force: bool = False) -> bool:
        """@brief Scrive `project_config.json` se le impostazioni sono cambiate.

        @param force Scrive anche se lo stato non e' cambiato (progetto nuovo).
        @return True quando il file e' stato scritto.
        @note Mai senza progetto, mai durante un cambio di progetto, mai sopra
            un file che all'apertura non si e' potuto applicare, mai fuori
            dalla cartella del progetto aperto (`_open_project_folder`).
        """
        if getattr(self, "_restoring_project", True) or not self._named():
            return False
        folder = getattr(self, "_open_project_folder", None)
        # Il file va nella cartella del progetto APERTO. Se la cartella di
        # lavoro e' cambiata, `_project_dir()` punta a un progetto omonimo di
        # un'altra cartella, che non si deve sovrascrivere (revisore 2,
        # difetto 7): finche' `_on_library_root_changed` non chiude il
        # progetto, qui non si scrive.
        if folder is None or folder != self._project_dir():
            return False
        if self._project_config_refused is not None and folder == self._project_config_refused:
            return False
        text = self._project_state_text()
        if text is None or (not force and text == self._last_saved_project_state):
            return False
        from .project_state import write_project_config
        try:
            write_project_config(folder, json.loads(text))
        except (OSError, TypeError, ValueError) as exc:
            self._append_log(tx("Impostazioni del progetto non salvate: {exc}", exc=exc))
            return False
        self._last_saved_project_state = text
        return True

    def _flush_project_config(self) -> None:
        """@brief Salva subito le impostazioni del progetto aperto (cambio progetto, chiusura)."""
        if hasattr(self, "_project_autosave"):
            self._save_project_config()

    #: Answers of `_ask_pending_edits_choice` and `_ask_footprint_draft_before_start`.
    PENDING_EDITS_APPLY, PENDING_EDITS_DISCARD, PENDING_EDITS_STAY = "apply", "discard", "stay"

    def _ask_pending_edits_choice(self, editors: tuple, can_apply: bool) -> str:
        """@brief Ask, in a centred modal, what to do with the unapplied symbol and/or footprint edits.

        @param editors The editors holding unapplied edits, in display order:
            a non-empty tuple of "symbol" and/or "footprint".
        @param can_apply False disables «Applica» (some edits not valid).
        @return PENDING_EDITS_APPLY, PENDING_EDITS_DISCARD or PENDING_EDITS_STAY
            (also for Esc and the window's close button).
        @note Replaces the status-bar sentence the user could not see
            (user, 2026-10-14); one question for both editors, whose text
            names the editors concerned (symbol, footprint or both). A
            separate method so tests answer it without opening a window.
        """
        editors = tuple(editors)
        # Literal catalogue keys in every branch (the i18n scan reads them).
        if editors == ("footprint",):
            title = self.i18n.text("Footprint")
            text = self.i18n.text("Il footprint contiene modifiche non applicate.")
            question = self.i18n.text("Applicarle, scartarle oppure restare sul footprint?")
        elif "footprint" in editors:
            title = self.i18n.text("Modifiche non applicate")
            text = self.i18n.text("Il simbolo e il footprint contengono modifiche non applicate.")
            question = self.i18n.text("Applicarle tutte, scartarle tutte oppure restare?")
        else:
            title = self.i18n.text("Simbolo elettrico")
            text = self.i18n.text("Il simbolo contiene modifiche non applicate.")
            question = self.i18n.text("Applicarle, scartarle oppure restare sul simbolo?")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(title)
        box.setText(text)
        box.setInformativeText(question)
        apply_button = box.addButton(self.i18n.text("Applica"), QMessageBox.ButtonRole.AcceptRole)
        discard_button = box.addButton(self.i18n.text("Scarta"), QMessageBox.ButtonRole.DestructiveRole)
        stay_button = box.addButton(self.i18n.text("Resta"), QMessageBox.ButtonRole.RejectRole)
        apply_button.setEnabled(bool(can_apply))
        box.setDefaultButton(stay_button)
        box.setEscapeButton(stay_button)
        box.exec()
        chosen = box.clickedButton()
        if chosen is apply_button:
            return self.PENDING_EDITS_APPLY
        if chosen is discard_button:
            return self.PENDING_EDITS_DISCARD
        return self.PENDING_EDITS_STAY

    def _resolve_pending_edits(self, symbol, footprint: bool, folder: Path | None = None) -> bool:
        """@brief Settle the unapplied symbol and/or footprint edits before leaving the project.

        @param symbol The symbol editor when it holds pending edits, else None.
        @param footprint True when the footprint preview holds a draft.
        @param folder The open project's folder when the working folder has
            already changed (`_on_library_root_changed`): `_project_dir()`
            then names a folder of the NEW working folder, so the footprint
            is saved explicitly here and its .kicad_mod is not rewritten
            (it would land in the other working folder); the project is
            marked stale instead (`_fp_file_stale`); the symbol is saved and
            its .kicad_sym written here too (`_symbol_apply_folder`). None
            for a normal leave.
        @return True when every pending edit was applied or discarded and the
            caller may go on (close, change project); False to stay.
        @note «Applica» is offered only when every pending edit can be
            applied (`SymbolTab.can_apply`; a footprint draft is validated
            edit by edit, so it always can). The footprint is applied FIRST:
            it is the part that can fail on the project file, and when it
            fails nothing is applied. If the symbol then refuses (pad
            mismatch, `reject_apply`) the footprint stays applied and the
            user is told which part was applied; the answer is «stay».
        @note An applied footprint is written to the .kicad_mod before the
            project is left (`_finish_footprint_rewrite_now`): the deferred
            rewrite would otherwise be dropped by the next project.
        """
        editors = (("symbol",) if symbol is not None else ()) + (("footprint",) if footprint else ())
        if not editors:
            return True
        can_apply = symbol.can_apply() if symbol is not None else True
        choice = self._ask_pending_edits_choice(editors, can_apply)
        if choice == self.PENDING_EDITS_APPLY and can_apply:
            if footprint:
                if not self.apply_footprint_draft(folder=folder):
                    return False
                if folder is None:
                    self._finish_footprint_rewrite_now()
            if symbol is not None and symbol.can_apply():
                # `_apply_symbol_pinout` runs inside `apply()` (signal): it
                # reads the folder to write into from here.
                self._symbol_apply_folder = folder
                try:
                    symbol.apply()
                finally:
                    self._symbol_apply_folder = None
            if symbol is not None and symbol.has_pending_edits() and footprint:
                QMessageBox.warning(self, self.i18n.text("Modifiche non applicate"), self.i18n.text(
                    "Il footprint è stato applicato; il simbolo no e resta in bozza."))
        elif choice == self.PENDING_EDITS_DISCARD:
            if symbol is not None:
                symbol.discard_edits()
            if footprint:
                self.discard_footprint_draft()
        else:
            return False
        return not ((symbol is not None and symbol.has_pending_edits())
                    or self.has_pending_footprint_edits())

    def _can_leave_project(self, folder: Path | None = None) -> bool:
        """Save pending edits before navigation; a failed write retains the open project."""
        if getattr(self, '_component_review_dialog', None) is not None:
            return False
        symbol = getattr(self, 'symbol_tab', None)
        symbol_pending = symbol is not None and symbol.has_pending_edits()
        footprint_pending = self.has_pending_footprint_edits()
        if symbol_pending or footprint_pending:
            # Show the editor the question is about (the symbol first).
            self.tabs.setCurrentIndex(TAB_SYMBOL if symbol_pending else TAB_PREVIEW)
            if not self._resolve_pending_edits(symbol if symbol_pending else None,
                                               footprint_pending, folder):
                return False
        if getattr(self, "_fp_rewrite_dirty", False):
            # An applied footprint edit not yet in the .kicad_mod (applied
            # while a pipeline owned the output, or just now): write it while
            # this project is still open; when that cannot run (busy, or the
            # working folder already changed) the project records that its
            # .kicad_mod is behind (`_fp_file_stale`), never silently.
            if folder is None:
                self._finish_footprint_rewrite_now()
            if getattr(self, "_fp_rewrite_dirty", False):
                self._fp_file_stale = True
        if not self._named():
            return True
        if folder is None:
            self._flush_project_config()
        else:
            self._write_open_project_config(folder)
        state = self._project_state_text()
        if state is not None and state == self._last_saved_project_state:
            return True
        current = folder or getattr(self, "_open_project_folder", None)
        refused = current is not None and current == self._project_config_refused
        if refused or state is None:
            # Two cases where saving can never succeed, so a plain refusal
            # would trap the user in the project: a file that must not be
            # overwritten, and a state that cannot be serialised (a value
            # JSON cannot hold). The user chooses to abandon the edits.
            question = ("Il file di progetto non e' modificabile. Abbandonare le modifiche non salvate?"
                        if refused else
                        "Le impostazioni del progetto contengono valori non salvabili. "
                        "Abbandonare le modifiche non salvate?")
            answer = QMessageBox.question(
                self, self.i18n.text("Progetto"), self.i18n.text(question),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            return answer == QMessageBox.StandardButton.Yes
        QMessageBox.warning(self, self.i18n.text("Progetto"), self.i18n.text(
            "Impossibile salvare le impostazioni: il progetto resta aperto."))
        return False

    def _on_library_root_text_changed(self, _text: str = "") -> None:
        """@brief A change of the «Area di lavoro» text: act now only if the program set it.

        @note `isModified()` is True only for text the user is typing (Qt
            sets it on keyboard edits, `setText` clears it): that text waits
            for `_on_library_root_edit_finished`.
        """
        if self.field_library_root.isModified():
            return
        self._on_library_root_changed()
        self._library_root_committed = self.field_library_root.text()

    def _on_library_root_edit_finished(self) -> None:
        """@brief The user confirmed the typed «Area di lavoro» with Enter."""
        self.field_library_root.setModified(False)
        self._on_library_root_changed()
        self._library_root_committed = self.field_library_root.text()

    def _on_library_root_focus_left(self) -> None:
        """@brief Leaving the «Area di lavoro» field without Enter drops the typed text.

        @note `editingFinished` fires on Enter too, after `returnPressed`:
            the text is then no longer modified and nothing happens here. A
            text still modified was never confirmed: the committed folder is
            put back (no project change), so a half-typed path left behind
            by Alt+Tab or a click on «Sfoglia» neither closes the project nor
            stays on screen as if it were the working folder.
        """
        field = self.field_library_root
        if not field.isModified():
            return
        committed = getattr(self, "_library_root_committed", None)
        folder = getattr(self, "_open_project_folder", None)
        if folder is not None and (committed is None or os.path.normcase(
                os.path.normpath(committed)) != os.path.normcase(os.path.normpath(str(folder.parent)))):
            # The open project's working folder is the truth.
            committed = str(folder.parent)
        if committed is None:
            return
        # setText clears the modified state: the text-changed slot then sees
        # a program-set text, the folder already in use, and changes nothing.
        field.setText(committed)

    def _on_library_root_changed(self, _text: str = "") -> None:
        """@brief Cambiare la cartella di lavoro salva e chiude il progetto aperto.

        @note Revisore 2, difetto 7: prima il nome restava e il salvataggio
            automatico scriveva nel progetto OMONIMO dell'altra cartella di
            lavoro, sovrascrivendolo. Ora il progetto aperto e' una cartella
            (`_open_project_folder`): le sue impostazioni si salvano li' e poi
            il progetto si chiude; come per «Nuovo» restano le impostazioni
            generali. Un lavoro in corso scrive nella cartella del progetto:
            in quel caso la cartella di lavoro torna com'era.
        """
        folder = getattr(self, "_open_project_folder", None)
        if folder is None or getattr(self, "_restoring_project", False):
            return
        # Normalised comparison: «C:\Lib\» or «c:/lib» is the same working
        # folder, and confirming it must not close the project.
        def normalised(path):
            return os.path.normcase(os.path.normpath(str(path)))
        if normalised(self._library_root() / self._component_name()) == normalised(folder):
            return
        if self._background_work_running() or self._automatic_generation_active:
            self.field_library_root.blockSignals(True)
            self.field_library_root.setText(str(folder.parent))
            self.field_library_root.blockSignals(False)
            self._update_project_path_label()
            self._refresh_start_page()
            self.ai_account.refresh()
            self._project_change_blocked()
            return
        # Il salvataggio normale guarda `_project_dir()`, che adesso punta
        # all'altra cartella: si scrive esplicitamente in quella del progetto.
        if not self._can_leave_project(folder):
            previous = self.field_library_root.blockSignals(True)
            self.field_library_root.setText(str(folder.parent))
            self.field_library_root.blockSignals(previous)
            self._update_project_path_label()
            self._refresh_start_page()
            self.ai_account.refresh()
            return
        self._switch_project("", self._general_project_state)
        self._append_log(tx("Cartella di lavoro cambiata: progetto {name} salvato in {folder} e "
                            "chiuso.",
                            name=folder.name,
                            folder=folder))

    def _write_open_project_config(self, folder: Path) -> None:
        """Salva le impostazioni correnti nella cartella del progetto aperto, se cambiate."""
        if self._project_config_refused is not None and folder == self._project_config_refused:
            return
        text = self._project_state_text()
        if text is None or text == self._last_saved_project_state:
            return
        from .project_state import write_project_config
        try:
            write_project_config(folder, json.loads(text))
            self._last_saved_project_state = text
        except (OSError, TypeError, ValueError) as exc:
            self._append_log(tx("Impostazioni del progetto non salvate: {exc}", exc=exc))

    def _write_project_config_at(self, folder: Path) -> bool:
        """@brief Write the current settings into `folder`, the OPEN project's folder, now.

        @param folder Must be `_open_project_folder`; any other folder is refused.
        @return True when the file was written.
        @note For the leave question after a working-folder change, when
            `_save_project_config` refuses because `_project_dir()` already
            names the other working folder. Never writes elsewhere.
        """
        open_folder = getattr(self, "_open_project_folder", None)
        if open_folder is None or Path(folder) != Path(open_folder):
            return False
        if self._project_config_refused is not None and folder == self._project_config_refused:
            return False
        text = self._project_state_text()
        if text is None:
            return False
        from .project_state import write_project_config
        try:
            write_project_config(folder, json.loads(text))
        except (OSError, TypeError, ValueError) as exc:
            self._append_log(tx("Impostazioni del progetto non salvate: {exc}", exc=exc))
            return False
        self._last_saved_project_state = text
        return True

    def _library_root(self) -> Path:
        """@brief Cartella di lavoro in cui stanno i progetti.

        @return The COMMITTED working folder: while the user is typing a
            new path in «Area di lavoro» (text modified, not yet confirmed
            with Enter) the folder in use stays the previous one, so a
            worker finishing meanwhile, an autosave or the symbol recovery
            never write into a half-typed path, possibly a homonymous
            project of another working folder (final review, 2026-10-14).
        """
        field = self.field_library_root
        text = field.text()
        if field.isModified():
            committed = getattr(self, "_library_root_committed", None)
            if committed is not None:
                text = committed
        return Path(text.strip() or str(DEFAULT_LIBRARY_ROOT))

    def _project_change_blocked(self) -> bool:
        """@brief Vero, con un avviso, quando un lavoro in corso impedisce di cambiare progetto.

        @note Un'analisi o una generazione scrive nella cartella del progetto
            in uso: cambiarlo a meta' manderebbe i risultati nel posto sbagliato.
        """
        if not (self._background_work_running() or self._automatic_generation_active):
            return False
        QMessageBox.information(
            self, self.i18n.text("Progetto"),
            self.i18n.text("Aspetta la fine del lavoro in corso prima di cambiare progetto."))
        return True

    def _new_project(self) -> None:
        """@brief «Nuovo progetto…»: chiede un nome libero e crea la cartella.

        @note Il nome di una cartella esistente e' rifiutato nel dialogo
            (`project_dialogs.name_problem`): un progetto nuovo non puo'
            diventare, per un nome uguale, un progetto vecchio.
        @note La cartella nasce subito con `docs/`, cosi' il nome e' occupato
            e compare in «Apri progetto…» anche prima di caricare documenti.
        """
        if self._project_change_blocked():
            return
        from .project_dialogs import ProjectNameDialog
        from .project_dialogs import ProjectKindDialog
        kind_dialog = ProjectKindDialog(
            tr=lambda key: self.i18n.tr("ui:" + key, default=key),
            selected="", parent=self)
        if kind_dialog.exec() != QDialog.DialogCode.Accepted:
            return
        selected_type = kind_dialog.selected_type()
        dialog = ProjectNameDialog(self._library_root(), "Nuovo progetto",
                                   tr=self.i18n.text, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name = dialog.project_name()
        # La creazione di docs/ non basta a rifiutare un nome gia' usato: una
        # cartella di progetto senza docs/ verrebbe riusata (audit 25/09).
        from .project_dialogs import name_problem
        problem = name_problem(self._library_root(), name)
        if problem:
            QMessageBox.warning(self, self.i18n.text("Nuovo progetto"), self.i18n.text(problem))
            return
        if not self._can_leave_project():
            return
        try:
            (self._library_root() / name / DOCS_SUBDIR).mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            QMessageBox.warning(self, self.i18n.text("Nuovo progetto"), str(exc))
            return
        # Un progetto nuovo parte dalle impostazioni generali, non da quelle
        # del progetto aperto prima (decisione dell'utente del 2026-09-24).
        try:
            initial_state = self._state_for_project_type(self._general_project_state, selected_type)
        except ValueError as exc:
            QMessageBox.warning(self, self.i18n.text("Nuovo progetto"), str(exc))
            return
        self._switch_project(name, initial_state)
        self._save_project_config(force=True)
        self._append_log(tx("Nuovo progetto: {project_dir}", project_dir=self._project_dir()))

    def _open_project(self) -> None:
        """@brief «Apri progetto…»: sceglie una cartella esistente della cartella di lavoro.

        @note Torna cio' che il programma sa gia' ricostruire dalla cartella
            (scelta dell'utente del 2026-09-24): documenti, corpi rivisti,
            scelte dei modelli 3D, footprint e 3D generati.
        """
        if self._project_change_blocked():
            return
        from .project_dialogs import OpenProjectDialog
        dialog = OpenProjectDialog(self._library_root(), self._component_name(),
                                   tr=self.i18n.text, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.selected_name():
            return
        self._open_project_at(self._library_root() / dialog.selected_name())

    def _open_project_at(self, folder: Path, *, save_current: bool = True) -> None:
        """@brief Open the project `folder` of the current working folder.

        @param folder Project folder; its parent must be `_library_root()`,
            because from here on the project is addressed by name.
        @note Applies the project's own settings when its project_config.json
            is readable; a refused file is neither applied nor overwritten.
        """
        if save_current and not self._can_leave_project():
            return
        name = folder.name
        from .project_state import ProjectConfigError, read_project_config
        non_finite = []
        try:
            saved = read_project_config(folder, non_finite)
            self._project_config_refused = None
            if non_finite:
                # NaN/infinity from a hand-edited file: dropped (None) so the
                # state stays serialisable, and said like any invalid value.
                self._append_log(tx("Impostazioni del progetto non applicate (valori non validi): ")
                                 + ", ".join(non_finite) + ".")
        except ProjectConfigError as exc:
            saved = None
            # Ne' applicato ne' sovrascritto: resta com'e' finche' qualcuno lo guarda.
            self._project_config_refused = folder
            self._append_log(tx("ATTENZIONE impostazioni del progetto non applicate: {exc}", exc=exc))
        config_loaded = isinstance(saved, dict)
        current_type = self._saved_component_type(saved) if config_loaded else ""
        needs_kind = self._needs_project_kind(saved if config_loaded else None, current_type)
        kind_migrated = needs_kind and self._project_config_refused is None
        if needs_kind:
            from .project_dialogs import ProjectKindDialog
            kind_dialog = ProjectKindDialog(
                tr=lambda key: self.i18n.tr("ui:" + key, default=key),
                selected=current_type or self._profile_key(), parent=self)
            if kind_dialog.exec() != QDialog.DialogCode.Accepted:
                return
            base_state = saved if isinstance(saved, dict) else self._general_project_state
            try:
                saved = self._state_for_project_type(base_state, kind_dialog.selected_type())
            except ValueError as exc:
                # A built-in type missing from the profile store: say it and
                # open with what the project carries (review C, 11).
                self._append_log(tx("ATTENZIONE tipologia non applicata: {exc}", exc=exc))
                saved = base_state
        # Senza file (progetto nato prima di project_config.json) valgono le
        # impostazioni generali, non quelle del progetto aperto prima.
        self._switch_project(name, saved if saved is not None else self._general_project_state)
        if (not config_loaded or kind_migrated) and self._project_config_refused is None:
            # Progetto nato prima di project_config.json: il file nasce subito,
            # non alla prima modifica, cosi' ogni progetto aperto ha il suo.
            self._save_project_config(force=True)
        self._append_log(tx("Progetto aperto: {folder}", folder=self._project_dir())
                         + (tx(" (impostazioni del progetto applicate).") if config_loaded else ""))

    @staticmethod
    def _saved_component_type(saved: dict) -> str:
        """@brief The component type key a saved project state carries, '' when none."""
        widget_values = saved.get("widgets") if isinstance(saved, dict) else None
        raw_type = widget_values.get("combo_component_type") if isinstance(widget_values, dict) else None
        if isinstance(raw_type, dict) and isinstance(raw_type.get("data"), str):
            return raw_type["data"]
        return ""

    @staticmethod
    def _needs_project_kind(saved, current_type: str) -> bool:
        """@brief Whether opening a project must ask its kind (board or single component).

        @param saved The saved project state, or None without a config file.
        @param current_type The component type key the state carries, '' when none.
        @return True only when nothing says the kind: no saved kind AND no
            component type. A project with a type keeps it (the kind follows
            the type's family, None for a custom family) instead of being
            asked on every open and overwritten by a built-in type (review C, 10).
        """
        saved_kind = saved.get("project_kind") if isinstance(saved, dict) else None
        if isinstance(saved_kind, str) and saved_kind in _PROJECT_KINDS:
            return False
        return not current_type

    def _state_for_project_type(self, state: dict, type_key: str) -> dict:
        """Return a project-state copy with the selected workflow/profile persisted."""
        result = deepcopy(state)
        result.pop("top_text", None)
        row = self.profile_store.get_type(type_key)
        if row is None:
            raise ValueError(tx("Tipo componente non disponibile: {type_key}", type_key=type_key))
        widgets = result.get("widgets")
        widgets = dict(widgets) if isinstance(widgets, dict) else {}
        widgets["combo_component_type"] = {
            "data": type_key,
            "text": self.i18n.component_type(type_key, row["label"]),
        }
        result["widgets"] = widgets
        result["project_kind"] = self._project_kind_for_family(row["family"])
        return result

    def _rename_project(self) -> None:
        """@brief «Rinomina progetto…»: nome nuovo, mai quello di un altro progetto.

        @note Stesso spostamento del nome proposto dall'analisi
            (`_migrate_docs_to_new_name`): documenti, diario, `photo-cache/` e
            `photo-review/` seguono il nome.
        """
        if self._project_change_blocked():
            return
        current = self._component_name()
        if not current:
            QMessageBox.information(self, self.i18n.text("Rinomina progetto"),
                                    self.i18n.text("Crea o apri prima un progetto (menu File)."))
            return
        from .project_dialogs import ProjectNameDialog
        dialog = ProjectNameDialog(self._library_root(), "Rinomina progetto", current=current,
                                   tr=self.i18n.text, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        # Controllo anche qui, non solo nel pulsante OK della finestra: il
        # nome di un altro progetto fonderebbe due progetti (audit 25/09).
        from .project_dialogs import name_problem
        problem = name_problem(self._library_root(), dialog.project_name(), current)
        if problem:
            self._append_log(tx("Rinomina non applicata: {problem}",
                                problem=self.i18n.text(problem)))
            return
        docs_dir = self._project_dir() / DOCS_SUBDIR
        try:
            moved_documents = sum(1 for path in docs_dir.iterdir() if path.is_file()) \
                if docs_dir.is_dir() else 0
        except OSError:
            moved_documents = 0
        applied = self._rename_project_folder(dialog.project_name())
        if applied != current:
            self.field_name.setText(applied)
            self._last_mesh_path = None
            self._restore_saved_mesh()
            self._append_log(tx("Progetto rinominato: {old_name} diventa {new_name}.",
                                old_name=current, new_name=applied))
            if moved_documents:
                self._on_ai_progress(tx(
                    "Rinomina automatica: il progetto {old_name} diventa {new_name}; "
                    "{n_files} documenti spostati nella nuova cartella.",
                    old_name=current, new_name=applied, n_files=moved_documents))
            if self.tabs.currentIndex() == TAB_MODEL_3D:
                self._enter_3d_tab()

    def _migrate_docs_to_new_name(self, new_name: str) -> str:
        """Sposta i documenti nella cartella del nuovo nome componente.

        La cartella docs/ vive dentro la cartella del componente, che prende il
        nome dal campo "Nome": quando l'estrazione AI propone un nome diverso,
        senza questo spostamento i file caricati prima resterebbero nella
        vecchia cartella e sparirebbero dalla lista (e non sarebbero piu'
        selezionabili per una nuova estrazione). Restituisce il nome effettivo:
        rinomina rifiutata o documenti in conflitto mantengono il progetto corrente,
        senza scartare il risultato AI e senza interrompere la generazione.
        """
        old_name = self._component_name()
        if not old_name:
            # Senza nome non esiste una cartella del componente (`_project_dir`
            # lo rifiuta apposta, dalle 21:55) e il blocco sul nome impedisce
            # di caricare documenti: non c'e' nulla da spostare, la proposta
            # si prende cosi' com'e'. Le righe di log bufferizzate le
            # scarica il primo scrittore con il nome nuovo.
            return new_name
        old_docs = self._docs_dir()
        new_docs = Path(self.field_library_root.text().strip()) / new_name / DOCS_SUBDIR
        if new_docs.resolve() == old_docs.resolve():
            if new_name != old_name:
                # Cambiano solo le maiuscole: su un disco che non le distingue
                # (Windows) e' la stessa cartella, e prima la rinomina non
                # faceva nulla, senza dirlo (revisore 2, difetto 9).
                return self._rename_folder_case(old_docs.parent, new_docs.parent,
                                                old_name, new_name)
            return old_name
        files = [f for f in old_docs.iterdir() if f.is_file()] if old_docs.is_dir() else []
        if not files:
            if not self._move_log_to(new_docs.parent):
                return old_name
            self._move_analysis_folders_to(old_docs.parent, new_docs.parent)
            return new_name

        # La rinomina non chiede conferma: interrompeva l'analisi a meta' con
        # una modale, proprio mentre il worker stava ancora lavorando. Resta
        # tracciata nel Log AI, cosi' si sa sempre quale cartella e' in uso.
        self._on_ai_progress(
            tx("Rinomina automatica: il progetto {old_name} diventa {new_name}; "
               "{n_files} documenti spostati nella nuova cartella.",
               old_name=old_name,
               new_name=new_name,
               n_files=len(files)))

        if (old_docs / MANIFEST).is_file() and any((new_docs / f.name).exists() for f in files):
            # Avviso, non domanda: non c'e' nulla da decidere e nulla e' stato
            # toccato. Una modale qui interrompeva l'analisi e copriva il
            # consuntivo appena mostrato.
            message = self.i18n.tr(
                'component.rename_conflict', new_name=new_name, old_name=old_name)
            self._on_ai_progress(message)
            return old_name
        if not self._move_log_to(new_docs.parent):
            return old_name
        new_docs.mkdir(parents=True, exist_ok=True)
        for f in files:
            target = new_docs / f.name
            if target.exists():
                continue
            shutil.move(str(f), str(target))
        self._move_analysis_folders_to(old_docs.parent, new_docs.parent)
        return new_name

    def _rename_project_folder(self, new_name: str) -> str:
        """@brief Rename the whole project and keep generated KiCad outputs usable.

        @param new_name New, already validated project name.
        @return The applied name, or the old name when the rename fails.
        @note The project folder is staged beside itself so case-only renames
            work on Windows. Footprint/model directories and filenames follow
            the project name, and the footprint's embedded model path is
            updated. Every completed move is reversed if a later step fails.
        """
        old_name = self._component_name()
        old_project = self._project_dir()
        new_project = old_project.with_name(new_name)

        def linked_or_reparse(path: Path) -> bool:
            """Recognize symbolic links, junctions and other reparse points."""
            import stat
            if path.is_symlink():
                return True
            is_junction = getattr(path, "is_junction", None)
            if callable(is_junction) and is_junction():
                return True
            try:
                info = path.lstat()
            except OSError:
                return False
            reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            return bool(getattr(info, "st_file_attributes", 0) & reparse)

        def refuse_link(path: Path) -> str:
            # Persisting this message through a linked project root would
            # itself write to the external target we are refusing to touch.
            self._append_log(tx("Rinomina annullata: {path} è un collegamento simbolico o una "
                                "giunzione; non è stato modificato.", path=path),
                             persist=path != old_project)
            return old_name

        # Renaming through a linked project root would change the target
        # project outside the selected workspace.
        if linked_or_reparse(old_project):
            return refuse_link(old_project)
        if not old_project.is_dir():
            # A name can be assigned before any project files have been made.
            import os
            if os.path.lexists(new_project):
                self._append_log(tx("Rinomina annullata: esiste gia' {new_name}.", new_name=new_name))
                return old_name
            return new_name
        if old_name == new_name:
            return old_name
        import os
        if os.path.lexists(new_project):
            if linked_or_reparse(new_project):
                return refuse_link(new_project)
            try:
                same_directory = old_project.samefile(new_project)
            except OSError:
                same_directory = False
            if not same_directory:
                self._append_log(tx("Rinomina annullata: esiste gia' {new_name}.", new_name=new_name))
                return old_name

        # These directories are the only generated outputs whose contents
        # are rewritten below. Refuse linked/reparse directories before the
        # project is staged, so no shared external library can be changed.
        for output_dir in (old_project / f"{old_name}.pretty",
                           old_project / f"{old_name}.3dshapes"):
            if linked_or_reparse(output_dir):
                return refuse_link(output_dir)
        # The footprint is atomically rewritten and the named model files are
        # moved. Refuse file links too, preserving external and relative-link
        # targets exactly as they are.
        for output_file in (
                old_project / f"{old_name}.pretty" / f"{old_name}.kicad_mod",
                *(old_project / f"{old_name}.3dshapes" / f"{old_name}{suffix}"
                  for suffix in (".step", ".stp", ".wrl", ".preview.json", ".FCStd"))):
            if linked_or_reparse(output_file):
                return refuse_link(output_file)

        self._flush_project_config()
        import uuid
        stage = old_project.with_name(f".{old_project.name}-rinomina-{uuid.uuid4().hex[:8]}")
        moves: list[tuple[Path, Path]] = []
        original_footprint: bytes | None = None
        footprint_path: Path | None = None
        staged_project_link_rejected = False

        def write_bytes_atomically(path: Path, payload: bytes) -> None:
            """Replace one footprint file atomically in its own directory."""
            import os
            import tempfile
            from ..fsutil import replace_file
            handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                                 dir=path.parent)
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(payload)
                replace_file(temporary, path)
            except BaseException:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise

        def move_case_safe(source: Path, target: Path) -> None:
            """Move one path, using an intermediate name for case-only changes."""
            import os
            if not source.exists():
                return
            case_only = (source.name.casefold() == target.name.casefold()
                         and source.name != target.name)
            if os.path.lexists(target):
                # On a case-insensitive filesystem both spellings resolve to
                # the same entry; on a case-sensitive filesystem they may be
                # distinct files. Never let Path.rename replace a real or
                # dangling destination (POSIX permits replacing an empty dir
                # and regular files).
                same_entry = False
                if case_only:
                    try:
                        same_entry = source.samefile(target)
                    except OSError:
                        pass
                if not same_entry:
                    raise FileExistsError(tx("Rinomina annullata: esiste gia' {new_name}.",
                                             new_name=target))
            if source.name.casefold() == target.name.casefold():
                intermediate = source.with_name(f".{source.name}-rinomina-{uuid.uuid4().hex[:8]}")
                source.rename(intermediate)
                try:
                    intermediate.rename(target)
                except OSError:
                    intermediate.rename(source)
                    raise
            else:
                source.rename(target)
            moves.append((source, target))

        def reject_staged_links() -> None:
            """Recheck mutable outputs after staging, before following any paths."""
            nonlocal staged_project_link_rejected
            if linked_or_reparse(stage):
                staged_project_link_rejected = True
                raise OSError(tx(
                    "Rinomina annullata: {path} è diventato un collegamento simbolico o "
                    "una giunzione durante la rinomina.", path=stage))
            staged_pretty = stage / f"{old_name}.pretty"
            staged_shapes = stage / f"{old_name}.3dshapes"
            for output in (staged_pretty, staged_shapes):
                if linked_or_reparse(output):
                    raise OSError(tx(
                        "Rinomina annullata: {path} è diventato un collegamento simbolico o "
                        "una giunzione durante la rinomina.", path=output))
            for output in (
                    staged_pretty / f"{old_name}.kicad_mod",
                    *(staged_shapes / f"{old_name}{suffix}"
                      for suffix in (".step", ".stp", ".wrl", ".preview.json", ".FCStd"))):
                if linked_or_reparse(output):
                    raise OSError(tx(
                        "Rinomina annullata: {path} è diventato un collegamento simbolico o "
                        "una giunzione durante la rinomina.", path=output))

        try:
            self._close_log_handle()
            old_project.rename(stage)
            moves.append((old_project, stage))

            # The initial lstat checks prevent ordinary link traversal. Check
            # again at the staged paths before moving or reading any generated
            # output, so a link introduced during the root move is rolled back
            # without touching its target.
            reject_staged_links()

            old_pretty = stage / f"{old_name}.pretty"
            new_pretty = stage / f"{new_name}.pretty"
            move_case_safe(old_pretty, new_pretty)
            old_shapes = stage / f"{old_name}.3dshapes"
            new_shapes = stage / f"{new_name}.3dshapes"
            move_case_safe(old_shapes, new_shapes)

            # Recheck under their final staged names before footprint reads,
            # atomic writes, or model moves follow them.
            for output in (new_pretty, new_shapes):
                if linked_or_reparse(output):
                    raise OSError(tx(
                        "Rinomina annullata: {path} è diventato un collegamento simbolico o "
                        "una giunzione durante la rinomina.", path=output))
            for output in (
                    new_pretty / f"{old_name}.kicad_mod",
                    *(new_shapes / f"{old_name}{suffix}"
                      for suffix in (".step", ".stp", ".wrl", ".preview.json", ".FCStd"))):
                if linked_or_reparse(output):
                    raise OSError(tx(
                        "Rinomina annullata: {path} è diventato un collegamento simbolico o "
                        "una giunzione durante la rinomina.", path=output))

            footprint_path = new_pretty / f"{old_name}.kicad_mod"
            renamed_footprint = new_pretty / f"{new_name}.kicad_mod"
            if footprint_path.is_file():
                original_footprint = footprint_path.read_bytes()
                move_case_safe(footprint_path, renamed_footprint)
                footprint_path = renamed_footprint
                source_text = original_footprint.decode("utf-8")
                source_text = re.sub(
                    r'(\(footprint\s+)"' + re.escape(old_name) + r'"',
                    lambda match: match.group(1) + '"' + new_name + '"',
                    source_text, count=1)
                source_text = re.sub(
                    r'(\(property\s+"Value"\s+)"' + re.escape(old_name) + r'"',
                    lambda match: match.group(1) + '"' + new_name + '"',
                    source_text, count=1)

                def renamed_model_reference(match):
                    path = match.group(2)
                    for extension in (".step", ".wrl"):
                        old_reference = f"${{KIPRJMOD}}/{old_name}.3dshapes/{old_name}{extension}"
                        if path == old_reference:
                            path = f"${{KIPRJMOD}}/{new_name}.3dshapes/{new_name}{extension}"
                            break
                    return match.group(1) + '"' + path + match.group(3)

                source_text = re.sub(
                    r'(\(model\s+)"([^"\r\n]+)(")', renamed_model_reference, source_text)
                write_bytes_atomically(footprint_path, source_text.encode("utf-8"))

            if new_shapes.is_dir():
                for suffix in (".step", ".stp", ".wrl", ".preview.json", ".FCStd"):
                    move_case_safe(new_shapes / f"{old_name}{suffix}",
                                   new_shapes / f"{new_name}{suffix}")
            if os.path.lexists(new_project):
                if linked_or_reparse(new_project):
                    raise FileExistsError(tx(
                        "Rinomina annullata: {path} è un collegamento simbolico o una giunzione; "
                        "non è stato modificato.", path=new_project))
                try:
                    same_directory = stage.samefile(new_project)
                except OSError:
                    same_directory = False
                if not same_directory:
                    raise FileExistsError(f"Destination project appeared during rename: {new_project}")
            stage.rename(new_project)
        except (OSError, UnicodeError) as exc:
            # Restore the footprint before reversing its filename and folders.
            if original_footprint is not None and footprint_path is not None:
                try:
                    write_bytes_atomically(footprint_path, original_footprint)
                except OSError:
                    pass
            for source, target in reversed(moves):
                current = target
                if current == new_project and not current.exists():
                    current = stage
                if current.exists():
                    try:
                        current.rename(source)
                    except OSError:
                        self._append_log(tx("ATTENZIONE: ripristino rinomina incompleto; file conservati in {path}.",
                                            path=current),
                                         persist=not staged_project_link_rejected)
            self._append_log(tx("Rinomina annullata: {old_name} non rinominabile ({exc}).",
                                old_name=old_name, exc=exc),
                             persist=not staged_project_link_rejected)
            return old_name

        if self._project_config_refused is not None and self._project_config_refused == old_project:
            self._project_config_refused = new_project
        return new_name

    def _rename_folder_case(self, old_project: Path, new_project: Path,
                            old_name: str, new_name: str) -> str:
        """@brief Compatibility wrapper for a case-only whole-project rename."""
        if old_project != self._project_dir() or new_project.name != new_name:
            return old_name
        return self._rename_project_folder(new_name)

    def _move_analysis_folders_to(self, old_project: Path, new_project: Path) -> None:
        """@brief Porta nella cartella rinominata tutto cio' che l'analisi ha prodotto.

        @param old_project Cartella del progetto prima della rinomina.
        @param new_project Cartella del progetto con il nome nuovo.
        @note Il progetto e' la sua cartella (decisione dell'utente del
            2026-09-24): con `docs/` e il diario si spostano anche la cache
            delle risposte AI e le evidenze (`photo-cache/`) e le proposte
            riviste (`photo-review/`). Prima si spostavano solo documenti e
            diario, e la revisione restava nella cartella vecchia: «Rivedi
            corpi» non la trovava piu' sotto il nome nuovo.
        @note Mai sovrascrivere: un file che esiste gia' nella destinazione
            resta com'e' e quello vecchio resta dov'era, con una riga di log.
            Le risposte in cache hanno il nome dato dal loro contenuto e le
            evidenze una marca temporale, quindi un conflitto vero e' raro.
        """
        import shutil
        from ..services.photo_inventory import PHOTO_CACHE_SUBDIR, REVIEW_BUNDLE_SUBDIR
        from .project_state import PROJECT_CONFIG_NAME
        # Un file rifiutato all'apertura resta rifiutato anche dopo la
        # rinomina: il blocco segue la cartella (revisore 2, difetto 6).
        # Prima restava sulla cartella vecchia e il salvataggio automatico
        # riscriveva in versione 1 il file spostato.
        if self._project_config_refused is not None and self._project_config_refused == old_project:
            self._project_config_refused = new_project
        # Le impostazioni del progetto e il suo costo AI seguono il nome come
        # il resto (il costo restava nella vecchia cartella: audit 25/09).
        from ..services.project_costs import COSTS_FILE
        for file_name in (PROJECT_CONFIG_NAME, COSTS_FILE):
            settings = old_project / file_name
            if not settings.is_file():
                continue
            if (new_project / file_name).exists():
                self._append_log(tx("Rinomina: {PROJECT_CONFIG_NAME} non spostato ({exc}).",
                                    PROJECT_CONFIG_NAME=file_name,
                                    exc=tx("esiste già nella cartella nuova")))
                continue
            try:
                new_project.mkdir(parents=True, exist_ok=True)
                shutil.move(str(settings), str(new_project / file_name))
            except OSError as exc:
                self._append_log(tx("Rinomina: {PROJECT_CONFIG_NAME} non spostato ({exc}).",
                                    PROJECT_CONFIG_NAME=file_name,
                                    exc=exc))
        for name in (PHOTO_CACHE_SUBDIR, REVIEW_BUNDLE_SUBDIR):
            source = old_project / name
            if not source.is_dir():
                continue
            target = new_project / name
            kept = 0
            try:
                for path in sorted(source.rglob("*")):
                    if not path.is_file():
                        continue
                    destination = target / path.relative_to(source)
                    if destination.exists():
                        kept += 1
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(path), str(destination))
                for folder in sorted(source.rglob("*"), reverse=True):
                    if folder.is_dir() and not any(folder.iterdir()):
                        folder.rmdir()
                if not any(source.iterdir()):
                    source.rmdir()
            except OSError as exc:
                self._append_log(tx("Rinomina: {name}/ non spostata del tutto ({exc}).", name=name, exc=exc))
                continue
            if kept:
                self._append_log(
                    tx("Rinomina: {kept} file di {name}/ esistevano gia' nella nuova cartella "
                       "e sono rimasti in {source}.",
                       kept=kept,
                       name=name,
                       source=source))

    def _on_extracted(self, params: ExtractedParams) -> None:
        """@brief Applica i parametri estratti, se sono ancora quelli attesi.

        @param params Parametri estratti dal worker.
        @note Un risultato che arriva da una corsa interrotta con «Interrompi»
            viene scartato qui: l'utente ha gia' ripreso in mano la finestra e
            puo' aver cambiato campi, nome o componente. Vedi
            `_analysis_result_is_current`.
        @note Qui si segna che c'e' un inventario da far rivedere: la pausa
            vera arriva a `finished` (vedi `_offer_inventory_review`), quando
            l'applicazione dei campi e' conclusa. Un risultato senza corpi non
            segna nulla -- non ci sarebbe niente da correggere.
        """
        if not self._analysis_result_is_current():
            self._append_log(tx("Risultato di un'analisi interrotta dall'utente: scartato, i campi "
                                "restano come sono."))
            return
        self._inventory_pause_pending = bool(getattr(params, "components_3d", None))
        # From here the fields may change (even if applying fails half way):
        # the run owns the output, see `_on_extract_finished`.
        self._extraction_applied_revision = self._analysis_revision
        self._applying_extraction = True
        try:
            self._apply_extracted_result(params)
        except Exception as exc:
            self._auto_generation_params = None
            # Campi applicati a meta': non c'e' un inventario coerente da far
            # rivedere, e fermarsi per una revisione su dati incerti non
            # aiuterebbe nessuno.
            self._inventory_pause_pending = False
            self._on_extract_error(tx("Impossibile applicare i dati estratti: {exc}", exc=exc))
        finally:
            self._applying_extraction = False
            if self._extract_finish_pending:
                self._extract_finish_pending = False
                self._on_extract_finished()

    def _apply_extracted_result(self, params: ExtractedParams) -> None:
        self._hide_analysis_overlay()
        validated_package_geometry = None
        if self._selected_family() == "single_smd" and params.package_geometry:
            # Without pad geometry the result is still applied and shown: the
            # block is `_validate_automatic_input`'s, so nothing paid is lost
            # (review C, 3).
            try:
                geometry_data = dict(params.package_geometry)
                geometry_data.setdefault("name", params.name or self._component_name())
                validated_package_geometry = package_from_mapping(geometry_data)
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(tx(
                    "Geometria SMD non valida: correggi i dati del datasheet prima di generare.")) from exc
        # Apply the extraction before validating automatic generation. If a
        # pad conflicts with its pitch, the user must see and correct the
        # extracted THT pad/drill values instead of losing the result.
        if self._auto_after_extraction:
            # A new acquisition must not silently reuse previous pin maps or annotations.
            self.pinout, self.pin_map = [], None
            self.fillet_annotations.clear()
            self.face_text_annotations.clear()
            self.field_body_offset_x.setValue(0)
            self.field_body_offset_y.setValue(0)
            self.field_pin_length_3d.setValue(3.0)
        self._acquisition_completed = True
        self._set_acquisition_tabs_enabled(True)
        self._invalidate_generated_mesh()
        self._photo_png = getattr(params, "photo_png", None)
        # Le quote della scheda vengono dall'analisi: da qui sono note.
        if _positive_frame(getattr(params, "body_length_mm", None),
                           getattr(params, "body_width_mm", None)) is not None:
            self._board_size_known = True
        # Stesso criterio con cui l'estrazione sceglie i campi da riempire (piu'
        # sotto): qui i campi non sono ancora stati applicati.
        is_castellated = getattr(params, "is_castellated", None)
        self._photo_body_fields = ("cast" if callable(is_castellated) and is_castellated()
                                   else "matrix")
        self._photo_overlay_png = params.photo_overlay_png
        self._photo_source = params.photo_source
        self._refresh_component_review_button()
        self._last_extracted = params
        # Il nome lo decide chi lavora, non l'estrazione: la proposta va solo
        # nel log, e si adotta con File > Rinomina progetto, che e' anche il
        # momento in cui i documenti la seguono. Riscriverlo da soli
        # significava spostare docs/ a ogni analisi e lasciare in area di
        # lavoro una cartella per ogni nome mai proposto.
        self._log_proposed_name(params.name)
        if params.name and not self._component_name():
            # Nessun nome scelto: non c'e' niente da rispettare e la proposta
            # e' meglio di "NUOVO_COMPONENTE".
            params.name = self._migrate_docs_to_new_name(params.name)
            self.field_name.setText(params.name)
        else:
            # Tutto cio' che viene generato porta il nome scelto: footprint,
            # modello e cartelle devono restare d'accordo fra loro.
            params.name = self._component_name() or params.name
        # Dopo il nome, perche' il pacchetto vive nella cartella del componente.
        self._store_extraction_review_bundle()
        self._refresh_reopen_review_button()
        if self._photo_png and params.components_3d:
            from ..services.model_choices import restore_choices
            try:
                params.components_3d = restore_choices(
                    self._docs_dir().parent, self._photo_png, params.components_3d)
            except (OSError, ValueError, TypeError) as exc:
                self._append_log(tx("Scelte KiCad precedenti non ripristinate: {exc}", exc=exc))
        self.field_description.setText(params.description)
        if params.tags:
            self.field_tags.setText(params.tags)

        self.package_geometry = validated_package_geometry
        if params.package_geometry:
            try:
                if self.package_geometry is None:
                    geometry_data = dict(params.package_geometry)
                    geometry_data.setdefault("name", params.name or self._component_name())
                    self.package_geometry = package_from_mapping(geometry_data)
                self._append_log(
                    f"Geometria package acquisita: {self.package_geometry.family}, "
                    f"{len(self.package_geometry.pins)} pad espliciti."
                )
                # Un'origine diversa dal centro del corpo viene stimata dal
                # campo dei pad: la stima va detta, mai silenziosa.
                for note in self.package_geometry.frame_notes():
                    self._append_log(tx("ATTENZIONE: {note}", note=note))
            except (KeyError, TypeError, ValueError) as exc:
                self._append_log(tx("ATTENZIONE geometria package ignorata: {exc}", exc=exc))
        self._set_regen_busy(False)

        # Le due disposizioni riempiono gruppi di campi diversi; tutto il
        # resto (corpo 3D, pinout, colori, fillet, riepilogo) e' comune.
        if params.is_castellated():
            self._apply_extracted_castellated(params)
        else:
            self._set_photo_components(params.components_3d or [], replace_excluded=True)
            self.group_castellated.setChecked(False)
            # Dal 26/09 l'estrazione lascia None cio' che la risposta non ha:
            # il passo per una sola colonna, l'interasse per una sola fila, la
            # griglia intera per un componente a pad espliciti (SMD). Si scrive
            # solo cio' che c'e'; il resto resta com'era e non viene usato.
            for widget, value in ((self.field_rows, params.rows), (self.field_cols, params.cols),
                                  (self.field_pitch, params.pitch_mm),
                                  (self.field_row_spacing, params.row_spacing_mm),
                                  (self.field_pad, params.pad_diameter_mm),
                                  (self.field_drill, params.drill_mm)):
                if value is not None:
                    widget.setValue(value)
            self._sync_single_tht_pad_controls()
            self.field_populated_pins.setText(
                ",".join(str(p) for p in sorted(params.populated_pins))
                if params.populated_pins else ""
            )

        self._apply_extracted_pcb_thickness(params)

        if params.is_castellated() and _castellated_body_available(params):
            self._enable_3d_group_from_extraction()
            # A board assembly uses PCB thickness and individual component heights.
            # Missing overall height must neither block it nor reuse an old value.
            self.field_body_height.setValue(params.body_height_mm or self.field_cast_pcb.value())
        if params.has_3d_body() and not params.is_castellated():
            self._enable_3d_group_from_extraction()
            self.field_body_length.setValue(params.body_length_mm)
            self.field_body_width.setValue(params.body_width_mm)
            self.field_body_height.setValue(params.body_height_mm)
            if params.body_offset_x_mm is not None:
                self.field_body_offset_x.setValue(params.body_offset_x_mm)
            if params.body_offset_y_mm is not None:
                self.field_body_offset_y.setValue(params.body_offset_y_mm)
            if params.pin_length_3d_mm is not None:
                self.field_pin_length_3d.setValue(params.pin_length_3d_mm)

        if params.pinout:
            self.pinout = [dict(entry) for entry in params.pinout]

        if params.pin_map:
            self.pin_map = {
                int(e["pin"]): (int(e["row"]), int(e["col"])) for e in params.pin_map
            }
            self._append_log(
                tx("Posizione dei pin letta dal disegno ({n_pin_map} pin): la numerazione "
                   "non segue l'ordine colonna per colonna.",
                   n_pin_map=len(self.pin_map))
            )

        self._apply_extracted_body_color(params)
        for warning in self._plausibility_warnings(params):
            self._append_log(tx("ATTENZIONE: ") + warning)
        self._log_values_to_review(params)

        # Defence in depth: the extraction cleans the fillets upstream, but
        # one malformed entry (not an object, a missing key, a radius that
        # is not a number) must cost that entry only, never the whole apply.
        applied_fillets = 0
        raw_fillets = params.edge_fillets if isinstance(params.edge_fillets, (list, tuple)) else ()
        for f in raw_fillets:
            try:
                ann = EdgeFilletAnnotation(edge=f["edge"], radius_mm=float(f["radius_mm"]))
                ann.validate()
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            self.fillet_annotations.append(ann)
            applied_fillets += 1

        fillet_note = (
            self.i18n.tr(
                "extraction.fillet_note", count=applied_fillets)
            if applied_fillets else ""
        )
        if self._auto_after_extraction:
            self._auto_generation_params = params
            # Persist the exact extraction before generation can be blocked:
            # the user must be able to correct its geometry and resume without
            # losing pin numbering or the values just reviewed.
            self._save_project_config()
            self._on_ai_progress(tx("Estrazione completata. Confidenza: {confidence}. {value}{fillet_note}",
                                    confidence=params.confidence,
                                    value=params.notes or '',
                                    fillet_note=fillet_note))
            return
        if self._relaunch_base_synthesized:
            # Nessuna estrazione e' avvenuta: la base viene dai campi e la
            # "confidenza low" che porta con se' non descrive alcuna analisi.
            # Va detto cio' che e' successo davvero: un inventario rilanciato.
            QMessageBox.information(
                self, self.i18n.text("Inventario rilanciato"),
                self.i18n.text("Inventario fotografico aggiornato sulle proposte salvate. "
                               "Nessuna estrazione dai documenti: quote e contatti restano "
                               "quelli dei campi correnti.")
                + "\n\n" + (params.notes or self.i18n.text('Nessuna nota.')))
            return
        QMessageBox.information(
            self, self.i18n.text("Estrazione completata"),
            self.i18n.tr(
                "extraction.completed", confidence=params.confidence,
                notes=params.notes or self.i18n.text('Nessuna nota.'),
                fillet_note=fillet_note)
        )

    def _extraction_has_board(self, params) -> bool:
        """@brief Whether the extracted component is a board (module or populated board).

        @param params Extracted parameters.
        @return True for a castellated module, for any result carrying photo
            components, and for the declared «aggregate_tht» family: the cases
            whose 3D has a PCB, so a PCB thickness and a board colour.
        """
        return bool(params.is_castellated() or getattr(params, "components_3d", None)
                    or self._selected_family() == "aggregate_tht")

    def _apply_extracted_pcb_thickness(self, params) -> None:
        """@brief Set the PCB thickness of a new extraction and say which value is used.

        @param params Extracted parameters.
        @note Decision of the user (2026-09-26): without a thickness in the
            documents every layout uses STANDARD_PCB_THICKNESS_MM, the field is
            reset at every extraction (the previous project's value never
            survives) and the log states the value actually used. Before, a
            castellated module kept the field's 1.0 mm while the log spoke of
            1.6 mm for THT boards.
        """
        if self._relaunch_base_synthesized:
            # Inventario rilanciato su una base costruita dai campi: nessuna
            # nuova lettura dei documenti, lo spessore resta quello del progetto.
            return
        value = getattr(params, "pcb_thickness_mm", None)
        declared = (float(value) if isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(float(value)) and float(value) > 0 else None)
        self._extracted_pcb_thickness = declared
        # The provenance is the one the model declared (value_sources): a
        # value it estimated is not «from the documents» (HANDOFF point 132).
        if declared is None:
            source = "standard"
        else:
            source = (getattr(params, "value_sources", None) or {}).get("pcb_thickness_mm")
            if source not in ("read", "derived", "estimated"):
                source = "undeclared"
        proposed = declared if declared is not None else STANDARD_PCB_THICKNESS_MM
        self._analysis_pcb_thickness = (proposed, source)
        board = self._extraction_has_board(params)
        if self._pcb_thickness_source == "user":
            # The user's value wins over any analysis; the proposal is kept
            # for «Ripristina» and said in the log.
            self._refresh_pcb_thickness_row()
            if board:
                self._append_log(tx(
                    "Spessore PCB: resta {value} mm impostato da te; l'analisi proponeva "
                    "{proposed} mm ({source}), non usato.",
                    value=f"{self.field_cast_pcb.value():.2f}", proposed=f"{proposed:.2f}",
                    source=self.i18n.text(PCB_THICKNESS_SOURCE_LABELS[source])))
            return
        self._pcb_thickness_source = source
        self.field_cast_pcb.setValue(proposed)
        self._refresh_pcb_thickness_row()
        if not board:
            return
        used = self.field_cast_pcb.value()
        if source == "standard":
            self._append_log(tx("Spessore PCB non quotato nei documenti: usato {value} mm, lo "
                                "spessore standard delle schede FR-4 (stima, da verificare con "
                                "una misura diretta).", value=f"{used:.2f}"))
        elif source == "read":
            self._append_log(tx("Spessore PCB dai documenti: {value} mm.", value=f"{used:.2f}"))
        elif source == "derived":
            self._append_log(tx("Spessore PCB ricavato dai documenti: {value} mm.",
                                value=f"{used:.2f}"))
        elif source == "estimated":
            self._append_log(tx("Spessore PCB stimato dall'AI, non letto dai documenti: {value} "
                                "mm (da verificare con una misura diretta).", value=f"{used:.2f}"))
        else:
            self._append_log(tx("Spessore PCB dall'AI senza provenienza dichiarata: {value} mm, "
                                "trattato come stima da verificare.", value=f"{used:.2f}"))

    def _sample_board_color(self, photo_png):
        """@brief The board colour sampled from the calibrated photo, or why it could not be.

        @param photo_png PNG bytes of the calibrated board crop, or None.
        @return (colour '#rrggbb' or None, reason when None).
        @note `photo_refine.sample_board_color(crop)` takes a BGR crop and
            returns '#rrggbb'. It is looked up at call time: when the function
            is missing (older installation) the caller falls back to the
            declared standard colour instead of failing.
        """
        if not isinstance(photo_png, (bytes, bytearray)) or not photo_png:
            return None, tx("nessuna foto calibrata")
        try:
            import cv2
            import numpy as np
            from ..services import photo_refine
        except ImportError:
            return None, tx("campionamento del colore non disponibile")
        sampler = getattr(photo_refine, "sample_board_color", None)
        if not callable(sampler):
            return None, tx("campionamento del colore non disponibile")
        try:
            crop = cv2.imdecode(np.frombuffer(bytes(photo_png), np.uint8), cv2.IMREAD_COLOR)
            if crop is None:
                return None, tx("foto calibrata non leggibile")
            colour = sampler(crop)
        except Exception as exc:  # noqa: BLE001 - un campionamento fallito non ferma l'analisi
            return None, str(exc)
        if not isinstance(colour, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", colour.strip()):
            return None, tx("colore campionato non valido")
        return colour.strip().lower(), ""

    def _apply_extracted_body_color(self, params) -> None:
        """@brief Set the body/board colour of a new extraction; a missing one is never carried over.

        @param params Extracted parameters.
        @note Decision of the user (2026-09-26): a colour the analysis does not
            declare is, for a board, sampled from the calibrated photo; without
            a usable photo the standard green solder mask
            (STANDARD_SOLDER_MASK_HEX) is used and said in the log. A single
            component falls back to DEFAULT_BODY_COLOR_HEX, also declared.
            Either way the field is reset at every extraction: the previous
            project's colour never survives.
        """
        if self._relaunch_base_synthesized:
            # Come lo spessore: senza una nuova analisi il colore del progetto resta.
            return
        declared = str(getattr(params, "body_color_hex", "") or "").strip()
        if re.fullmatch(r"#[0-9a-fA-F]{6}", declared):
            self.field_body_color.setText(declared)
            return
        if not self._extraction_has_board(params):
            self.field_body_color.setText(DEFAULT_BODY_COLOR_HEX)
            self._append_log(tx("Colore del corpo non dichiarato: usato il nero predefinito "
                                "{color}.", color=DEFAULT_BODY_COLOR_HEX))
            return
        colour, reason = self._sample_board_color(getattr(params, "photo_png", None))
        if colour is not None:
            self.field_body_color.setText(colour)
            self._append_log(tx("Colore della scheda non dichiarato: campionato dalla foto "
                                "calibrata ({color}).", color=colour))
            return
        self.field_body_color.setText(STANDARD_SOLDER_MASK_HEX)
        self._append_log(tx("Colore della scheda non dichiarato e non campionabile ({reason}): "
                            "usato il verde standard del solder mask {color}.",
                            reason=reason, color=STANDARD_SOLDER_MASK_HEX))

    def _log_values_to_review(self, params) -> None:
        """@brief Say in the log which extracted values are estimates or were not declared.

        @param params Extracted parameters.
        @note The extractor marks the provenance of each value
            (`ExtractedParams.value_sources`: read, derived, estimated,
            undeclared); the fields are not shown anywhere since 2026-09-26, so
            the log is where an estimate must be visible as such. Looked up at
            call time: an older extractor without the method logs nothing.
        """
        review = getattr(params, "values_to_review", None)
        if not callable(review):
            return
        try:
            keys = list(review())
        except Exception:  # noqa: BLE001 - informativo: non deve fermare l'applicazione
            return
        if keys:
            self._append_log(tx("Valori stimati o non dichiarati nei documenti, da verificare: "
                                "{keys}.", keys=", ".join(str(key) for key in keys)))

    def _plausibility_warnings(self, params) -> list:
        """@brief Warnings for extracted values that are possible but unusual.

        @param params Extracted parameters.
        @return Messages for the log; nothing here blocks the generation.
        @note Impossible values are refused by `_validate_automatic_input`;
            these are the physically possible ones that more often reveal a
            reading error: a pin grid wider than the body (true for a DIP,
            whose rows lie outside the moulding, so only a warning) and a pitch
            outside PLAUSIBLE_PITCH_MM (declared heuristic, see there).
        """
        warnings = []

        def number(value):
            return (float(value) if isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(float(value)) and float(value) > 0 else None)

        low, high = PLAUSIBLE_PITCH_MM
        pitches = []
        if params.is_castellated():
            pitches = [(tx("passo del lato {edge}",
                           edge=EDGE_LABELS.get(side.get("edge"), side.get("edge"))),
                        number(side.get("pitch_mm")))
                       for side in params.castellated_sides or () if isinstance(side, dict)]
        elif not getattr(params, "package_geometry", None):
            pitches = [(tx("passo"), number(params.pitch_mm)),
                       (tx("interasse fra le file"), number(params.row_spacing_mm))]
            rows, cols = number(params.rows), number(params.cols)
            pad = number(params.pad_diameter_mm) or 0.0
            for count, pitch, body, axis in (
                    (cols, number(params.pitch_mm), number(params.body_length_mm), "X"),
                    (rows, number(params.row_spacing_mm), number(params.body_width_mm), "Y")):
                if count and count > 1 and pitch and body:
                    extent = (count - 1) * pitch + pad
                    if extent > body:
                        warnings.append(tx(
                            "i pin occupano {extent} mm lungo {axis}, più del corpo dichiarato "
                            "({body} mm): possibile (per esempio un DIP), ma controlla file, "
                            "passo e quote.", extent=f"{extent:.2f}", axis=axis, body=f"{body:.2f}"))
        for label, value in pitches:
            if value is not None and not low <= value <= high:
                warnings.append(tx("{label} di {value} mm, fuori dall'intervallo abituale "
                                   "{low}-{high} mm: controlla l'unità di misura del disegno.",
                                   label=label, value=f"{value:g}", low=f"{low:g}", high=f"{high:g}"))
        return warnings

    def _ask_contact_style(self, sides: list) -> str | None:
        """@brief Ask the user the contact style of sides whose style nobody observed.

        @param sides The side dicts (edge, count, pitch_mm) without a style.
        @return One of `contact_styles()`, or None when the user cancels.
        @note Decision of the user (2026-09-26): the photo may not show the
            edge, and then the style cannot be read; it is asked, never taken
            from a default. No choice is preselected, so «OK» without looking
            is impossible. The labels are CONTACT_STYLE_LABELS and the help is
            CONTACT_STYLE_TOOLTIP, the same as the 3D-tab selector.
        """
        from PyQt6.QtWidgets import QButtonGroup, QDialogButtonBox, QRadioButton
        dialog = QDialog(self)
        dialog.setWindowTitle(self.i18n.text("Tipo di contatto dei lati"))
        layout = QVBoxLayout(dialog)
        intro = QLabel(self.i18n.text(
            "L'analisi non ha determinato il tipo di contatto di questi lati. "
            "Guarda la scheda e scegli:"))
        intro.setWordWrap(True)
        layout.addWidget(intro)
        listing = QLabel("\n".join(self._side_summary(side) for side in sides))
        listing.setProperty("i18n_skip", True)
        listing.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(listing)
        group = QButtonGroup(dialog)
        for style in contact_styles():
            radio = QRadioButton(self.i18n.text(CONTACT_STYLE_LABELS.get(style, style)))
            radio.setProperty("contact_style", style)
            group.addButton(radio)
            layout.addWidget(radio)
        explanation = QLabel(self.i18n.text(CONTACT_STYLE_TOOLTIP))
        explanation.setWordWrap(True)
        explanation.setStyleSheet("color: #a6adc8;")
        layout.addWidget(explanation)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setEnabled(False)
        group.buttonClicked.connect(lambda _button: ok.setEnabled(True))
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        self._contact_style_dialog = dialog
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted or group.checkedButton() is None:
                return None
            return group.checkedButton().property("contact_style")
        finally:
            self._contact_style_dialog = None
            dialog.deleteLater()

    @staticmethod
    def _side_summary(side: dict) -> str:
        """@brief One castellated side in words: edge, contacts and pitch."""
        return tx("lato {edge}: {count} contatti a {pitch} mm",
                  edge=EDGE_LABELS.get(side.get("edge"), side.get("edge")),
                  count=side.get("count"), pitch=side.get("pitch_mm"))

    def _resolve_unknown_contact_styles(self) -> None:
        """@brief Ask once for the style of every side that has none, and log the answer.

        @note A cancelled question leaves the sides without a style: the
            generation then stops with `_missing_contact_style_message`, it is
            never done with a guessed style.
        """
        unknown = [side for side in self._sides if side["contact_style"] is None]
        if not unknown:
            return
        names = ", ".join(str(EDGE_LABELS.get(side["edge"], side["edge"])) for side in unknown)
        choice = self._ask_contact_style(unknown)
        if choice in contact_styles():
            for side in unknown:
                side["contact_style"] = choice
            self._append_log(tx("Tipo di contatto dei lati {sides} scelto dall'utente: «{style}» "
                                "(non determinato dall'analisi).", sides=names,
                                style=self.i18n.text(CONTACT_STYLE_LABELS.get(choice, choice))))
        else:
            self._append_log(tx("ATTENZIONE: ") + self._missing_contact_style_message())
        self._on_sides_changed()

    def _missing_contact_style_message(self) -> str:
        """@brief Why the generation cannot run while a side has no contact style.

        @return The explanation, or "" when every side has a known style.
        """
        missing = [side for side in self._sides if side["contact_style"] not in contact_styles()]
        if not missing:
            return ""
        return tx("tipo di contatto non indicato per i lati {sides}: l'analisi non lo ha determinato e "
                  "non viene indovinato. Sceglilo nel tab Modello 3D (Tipo contatto) e rigenera.",
                  sides=", ".join(str(EDGE_LABELS.get(side["edge"], side["edge"]))
                                  for side in missing))

    def _apply_extracted_castellated(self, params: ExtractedParams) -> None:
        """Riempie il gruppo "Modulo castellated" con i lati estratti.

        Le quote del corpo arrivano dagli stessi campi body_* della matrice
        (li' descrivono l'ingombro del case, qui il contorno del modulo).
        Le misure delle piazzole restano LATO PER LATO, con la loro
        provenienza (28/09): lette o ricavate dai documenti sono dichiarate,
        stimate o senza provenienza restano stime che il generatore riduce
        se fanno toccare due contatti (e lo scrive nelle note). Prima
        venivano copiate nei campi unici del modulo: vinceva l'ultimo lato,
        per tutti i lati, e come misura dichiarata. I campi del modulo
        tornano al land pattern di riferimento a ogni estrazione e valgono
        solo per i lati senza misura propria.
        """
        self.group_castellated.setChecked(True)
        # Distanza fra le file dichiarata dall'estrazione: e' la quota da cui
        # il generatore ricava dove stanno i fori passanti rispetto al bordo,
        # (larghezza - distanza) / 2. Senza, per i contatti a foro passante
        # restano solo una stima per difetto e una riga di log che lo dice.
        # Si azzera a ogni estrazione: una quota di un'altra scheda non vale.
        # L'estrazione la consegna solo se LETTA in un documento (HANDOFF
        # 167): una derivata o stimata arriva qui gia' a None, con la nota.
        spacing = getattr(params, "row_spacing_mm", None)
        self._declared_row_spacing = (
            float(spacing) if isinstance(spacing, (int, float)) and not isinstance(spacing, bool)
            and math.isfinite(float(spacing)) and float(spacing) > 0 else None)

        if params.body_length_mm:
            self.field_cast_body_length.setValue(params.body_length_mm)
        if params.body_width_mm:
            self.field_cast_body_width.setValue(params.body_width_mm)
        # Come la distanza fra le file: una misura di un'altra scheda (o
        # quella che un progetto salvato prima del 28/09 vi aveva copiato da
        # un solo lato) non vale per questa. Il riferimento arriva al
        # generatore come stima (vedi `_current_castellated_params`).
        self.field_cast_pad_length.setValue(_reference_pad("pad_length"))
        self.field_cast_pad_width.setValue(_reference_pad("pad_width"))

        self._sides.clear()
        names = []
        pinout = []
        sources = getattr(params, "value_sources", None) or {}
        for side in params.castellated_sides or ():
            side = dict(side)
            # Diametri dei fori (26/09) e piazzole del lato (28/09): dichiarati
            # solo se letti, ricavati o misurati; stimati o senza provenienza
            # restano stime. Senza la voce di provenienza (lati rientrati da
            # `_serialised_sides`, che la portano gia' nel lato, es. il
            # rilancio dopo un riavvio) vale quella del lato; se manca anche
            # quella, la misura resta una stima. Prima i fori non avevano il
            # ripiego: un foro dichiarato tornava stima e veniva ridotto
            # (verifica avversariale del 2026-10-02, C-M3).
            for field in ("half_hole_diameter_mm", "through_hole_drill_mm",
                          "pad_length_mm", "pad_width_mm"):
                key = f"castellated_sides.{side.get('edge')}.{field}"
                flag = field.replace("_mm", "_declared")
                side[flag] = (sources[key] in DECLARED_SOURCES if key in sources
                              else side.get(flag) is True)
            self._add_side_row(side)
            label = EDGE_LABELS.get(side["edge"], side["edge"])
            if side.get("pin_names"):
                names.append(f"{label}: {', '.join(side['pin_names'])}")
                pinout.extend(_side_pinout(side))

        # Tabella pinout: se il modello non l'ha compilata a parte, la si
        # ricava dalle sigle serigrafate accanto ai contatti di ogni lato.
        if pinout:
            self.pinout = pinout

        self._set_photo_components(params.components_3d or [], replace_excluded=True)
        if not self.castellated_components:
            self._append_log(
                tx("ATTENZIONE: il modello non ha restituito alcun componente dalla foto "
                   "(components_3d vuoto): nel 3D andra' un blocco unico. Rilancia "
                   "l'estrazione con la foto del lato superiore selezionata, oppure "
                   "aggiungi i componenti a mano nel tab Modello 3D.")
            )
        if self.castellated_components:
            self._append_log(
                tx("{n_castellated_components} componenti letti dalla foto (stime "
                   "visive): compariranno nel modello 3D, modificabili nel tab Modello 3D.",
                   n_castellated_components=len(self.castellated_components))
            )

        # Il tipo di contatto non osservato si chiede, non si indovina
        # (decisione dell'utente, 26/09); poi e' scritto accanto a ogni lato con
        # la sua provenienza: letto dall'estrazione, scelto dall'utente o
        # ancora da scegliere.
        self._resolve_unknown_contact_styles()
        styles = self._table_contact_styles()
        described = []
        for index, s in enumerate(params.castellated_sides or ()):
            style = styles[index] if index < len(styles) else None
            if s.get("contact_style") in contact_styles():
                origin = ""
            elif style is None:
                origin = tx(", da scegliere")
            else:
                origin = tx(", scelto dall'utente")
            label = CONTACT_STYLE_LABELS.get(style, style) if style else CONTACT_STYLE_UNKNOWN_LABEL
            described.append(
                f"lato {EDGE_LABELS.get(s['edge'], s['edge'])} "
                f"{s['count']} contatti a {s['pitch_mm']}mm "
                f"({label}{origin})")
        self._append_log(tx("Contatti del modulo: ") + ", ".join(described))
        for line in names:
            self._append_log(tx("Sigle serigrafate {line}", line=line))

    def _on_extract_error(self, message: str) -> None:
        """@brief Riporta la finestra in quiete e dice perche' l'analisi e' fallita.

        @param message Testo dell'errore, come arriva dal worker.
        @note L'errore di una corsa abbandonata con «Interrompi» non apre
            nessuna finestra: l'utente ha gia' deciso di lasciarla perdere, e
            un avviso di guasto su cio' che ha interrotto lui sarebbe un
            rimprovero per una sua scelta. Resta nel log, dove serve.
        """
        if not self._analysis_result_is_current():
            self._append_log(tx("Errore di un'analisi interrotta dall'utente: {message}", message=message))
            return
        self._auto_generation_params = None
        self._auto_after_extraction = False
        self._hide_analysis_overlay()
        # Un rilancio fallito lascia validi i parametri e le proposte salvate:
        # il pulsante torna disponibile per riprovare, o resta spento se non
        # c'era nulla.
        self._refresh_reopen_review_button()
        self._on_ai_progress(tx("Estrazione fallita: {message}", message=message))
        QMessageBox.warning(self, self.i18n.text("Estrazione fallita"), message)

    # ── Parametri del footprint e del corpo 3D (dati, nessun widget) ──

    def _init_parameters(self) -> None:
        """@brief Create the footprint and 3D parameters the generation reads.

        @note Same defaults and precision as the editors of the removed
            Footprint 2D tab (user, 2026-09-26). The ranges are PHYSICAL
            limits since the same day (decision of the user): lengths up to
            MAX_LENGTH_MM, heights from MIN_HEIGHT_MM, pitches above zero,
            counts limited only by what fits (checked from the declared sizes,
            see `_validate_automatic_input`). An extracted value outside a
            range is rejected by `_validate_automatic_input`; an unusual one is
            applied and reported by `_plausibility_warnings`.
        """
        # Dati del componente e matrice THT.
        self.field_description = TextValue()
        self.field_tags = TextValue("pin header THT")
        # Righe e colonne: nessun tetto proprio; quante ne stanno lo dicono il
        # passo e le quote (`_validate_automatic_input`). Il massimo qui tiene
        # solo il numero memorizzabile.
        self.field_rows = BoundedValue(2, 1, 10000, decimals=0, integer=True)
        self.field_cols = BoundedValue(4, 1, 10000, decimals=0, integer=True)
        # Passo e interasse: maggiori di zero (0.01 mm e' la risoluzione).
        self.field_pitch = BoundedValue(2.54, 0.01, MAX_LENGTH_MM)
        self.field_row_spacing = BoundedValue(2.54, 0.01, MAX_LENGTH_MM)
        self.field_populated_pins = TextValue()
        self.field_pad = BoundedValue(1.7, 0.01, MAX_LENGTH_MM)
        self.field_drill = BoundedValue(1.0, 0.01, MAX_LENGTH_MM)
        # The spin boxes of «Piazzole del componente THT» mirror these fields
        # whenever they change (project open, type change: review C, 6).
        self.field_pad.on_change(lambda _value: self._sync_single_tht_pad_controls())
        self.field_drill.on_change(lambda _value: self._sync_single_tht_pad_controls())

        # Modulo castellated. La matrice e i moduli castellated si escludono:
        # in un modulo ogni lato ha il suo numero di contatti e il suo passo,
        # che una griglia a passo unico non sa descrivere.
        self.group_castellated = FlagValue(False)
        self.field_cast_body_length = BoundedValue(42.8, 0.0, MAX_LENGTH_MM)
        self.field_cast_body_width = BoundedValue(15.2, 0.0, MAX_LENGTH_MM)
        self.field_cast_pad_length = BoundedValue(3.6, 0.0, MAX_LENGTH_MM)
        self.field_cast_pad_width = BoundedValue(1.8, 0.0, MAX_LENGTH_MM)
        #: Sporgenza della piazzola oltre il bordo (menisco ispezionabile).
        self.field_cast_overhang = BoundedValue(1.4, 0.0, 5.0)
        #: Diametro del semi-foro metallizzato: i semi-fori si tagliano nel 3D.
        self.field_cast_hole = BoundedValue(1.7, 0.0, 10.0)
        #: Spessore della scheda; senza quota, lo standard dichiarato (vedi
        #: `_apply_extracted_pcb_thickness`).
        # Stesso minimo del campo visibile (0.1 mm): prima 0.0 era accettato qui
        # e mostrato come 0.10, mentre il 3D usava 0.0 (audit 138, C5).
        self.field_cast_pcb = BoundedValue(STANDARD_PCB_THICKNESS_MM, 0.1, 10.0)
        self.field_cast_pcb.on_change(lambda _value: self._refresh_pcb_thickness_row())
        #: Rientro del blocco componenti dal bordo del PCB.
        self.field_cast_inset = BoundedValue(1.0, 0.0, 20.0)
        #: Foro passante chiuso accanto al semi-foro (0 = nessuno): ci si
        #: salda uno strip; generando solo il semi-foro il bordo restava aperto.
        self.field_cast_thru_drill = BoundedValue(1.0, 0.0, 10.0)
        self.field_cast_thru_pad = BoundedValue(1.7, 0.0, 10.0)
        #: Distanza del foro passante dal bordo (0 = la calcola il generatore).
        self.field_cast_thru_offset = BoundedValue(0.0, 0.0, 20.0)
        #: Quanto il centro del semi-foro sta dentro il bordo (0 = convenzione).
        self.field_cast_hole_inset = BoundedValue(0.0, 0.0, 10.0)
        #: Lati del modulo, uno per contatti con passo proprio (vedi
        #: `component_state.normalised_side`).
        self._sides: list[dict] = []

        # Corpo 3D. Acceso di default se FreeCAD c'e': chi lo ha installato
        # vuole il 3D; senza FreeCAD resta spento e la generazione non fallisce.
        try:
            find_freecadcmd()
            freecad_available = True
        except Cad3DError:
            freecad_available = False
        # Ricordato perche' l'estrazione deve poterlo consultare senza rifare
        # la ricerca su disco: vedi `_enable_3d_group_from_extraction`.
        self._freecad_available = freecad_available
        self.group_3d = FlagValue(freecad_available)
        self.field_body_length = BoundedValue(10.0, MIN_LENGTH_MM, MAX_LENGTH_MM)
        self.field_body_width = BoundedValue(10.0, MIN_LENGTH_MM, MAX_LENGTH_MM)
        self.field_body_height = BoundedValue(5.0, MIN_HEIGHT_MM, MAX_LENGTH_MM)
        #: Bordo del corpo rispetto al pin 1 (X) e alla prima riga (Y).
        self.field_body_offset_x = BoundedValue(0.0, -MAX_LENGTH_MM, MAX_LENGTH_MM)
        self.field_body_offset_y = BoundedValue(0.0, -MAX_LENGTH_MM, MAX_LENGTH_MM)
        self.field_pin_length_3d = BoundedValue(3.0, 0.01, MAX_LENGTH_MM)
        self.field_body_color = TextValue(DEFAULT_BODY_COLOR_HEX)
        self.field_pin_color = TextValue("#b0b0b0")

        self.group_castellated.on_change(self._on_castellated_toggled)
        for value in (self.field_cast_body_length, self.field_cast_body_width,
                      self.field_cast_hole, self.field_cast_pcb, self.field_cast_inset,
                      self.field_cast_thru_drill, self.field_cast_thru_pad,
                      self.field_cast_thru_offset, self.field_cast_hole_inset):
            value.on_change(lambda _value: self._on_sides_changed())

    # ── Modulo castellated ──────────────────────────────────────────

    def _on_castellated_toggled(self, checked: bool) -> None:
        """La matrice e i moduli castellated sono alternativi, mai insieme."""
        if checked and not self._sides:
            # Due lati opposti sono la configurazione di gran lunga piu' comune.
            # Segnaposto di chi accende il modulo a mano, prima di ogni analisi:
            # un'estrazione li sostituisce tutti (e chiede lo stile che la foto
            # non mostra). Lo stile qui e' quello del selettore del tab 3D.
            style = default_contact_style()
            self._add_side_row({"edge": "left", "count": 3, "pitch_mm": 2.54, "first_pin": 1,
                                "contact_style": style})
            self._add_side_row({"edge": "right", "count": 3, "pitch_mm": 2.54, "first_pin": 4,
                                "contact_style": style})
        self._sync_3d_fields_for_castellated(checked)
        self._sync_contact_style_bar()
        self._draw_preview()

    def _sync_3d_fields_for_castellated(self, castellated: bool) -> None:
        """Nei moduli castellated il corpo 3D viene dal modulo, non dai campi 3D.

        I contatti dei moduli sono quasi sempre dorati (ENIG): se il colore
        pin e' ancora quello di default dei piedini stagnati, si passa all'oro.
        """
        if castellated and self.field_pin_color.text().strip().lower() == "#b0b0b0":
            self.field_pin_color.setText("#c9a227")

    def _add_side_row(self, side: dict | None = None, *, missing_style: str | None = None) -> None:
        """Append one castellated side, filled from an extracted/serialised side dict.

        Programmatic: it does NOT invalidate the generated 3D, because its
        callers (a new extraction, the castellated switch, a reopened project)
        already describe a new input revision. An unknown edge falls back to
        the first edge, as the former table did. `missing_style` is the style
        of a dict WITHOUT the key (a project saved before the key existed,
        generated with that style); an extraction passes None, so an
        unobserved style stays unknown and is asked.
        """
        side = dict(side or {})
        if side.get("edge") not in CASTELLATED_EDGES:
            side["edge"] = CASTELLATED_EDGES[0]
        side["contact_style"] = self._declared_contact_style(side, missing_style)
        self._sides.append(normalised_side(side))
        self._on_sides_changed()

    def _declared_contact_style(self, side: dict, missing_style: str | None = None) -> str | None:
        """Contact style declared by a side dict, or None when nobody observed it.

        Since 2026-09-26 (decision of the user) a style that is absent or None
        is not replaced by a default: it stays None and the user is asked
        (`_resolve_unknown_contact_styles`). Only a dict without the key at all
        takes `missing_style` (see `_add_side_row`). An unknown value is NOT
        guessed into one of the three either: it becomes None and the log
        says so.
        """
        if "contact_style" not in side:
            return missing_style
        declared = side.get("contact_style")
        if declared is None:
            return None
        if declared in contact_styles():
            return declared
        self._append_log(
            tx("ATTENZIONE: tipo di contatto sconosciuto {declared!r} sul lato {get}: non "
               "viene indovinato, va scelto.",
               declared=declared,
               get=EDGE_LABELS.get(side.get('edge'), side.get('edge'))))
        return None

    def _contact_style_combo(self, style: str) -> QComboBox:
        """Combo offering the contact styles, with the telling-apart tooltip.

        Item texts are translated here and again by
        `_retranslate_contact_style_combos` on every language switch; item
        DATA (the style keys) never change, so a switch cannot alter a choice.
        """
        combo = QComboBox()
        for value in contact_styles():
            combo.addItem(self.i18n.text(CONTACT_STYLE_LABELS.get(value, value)), value)
        combo.setCurrentIndex(max(combo.findData(style), 0))
        self._set_translated_tooltip(combo, CONTACT_STYLE_TOOLTIP)
        return combo

    def _contact_style_combos(self) -> list:
        """@brief Every contact-style combo of the window: today only the 3D-tab one."""
        combo_3d = getattr(self, "combo_contact_style_3d", None)
        return [combo_3d] if combo_3d is not None else []

    def _retranslate_contact_style_combos(self) -> None:
        """@brief Translate the contact-style items again after a language switch.

        @note Note 8 of the user (2026-09-23): the items stayed Italian in an
            English interface. `I18n.retranslate_widgets` deliberately leaves
            combo ITEMS alone (they are often data), so these combos are
            refreshed here. The Italian source is derived from the item data
            -- the style key, or None for the informative «mixed» entry --
            hence the data, the current index and the signals stay untouched.
        """
        for combo in self._contact_style_combos():
            for index in range(combo.count()):
                value = combo.itemData(index)
                source = (CONTACT_STYLE_MIXED_LABEL if value is None
                          else CONTACT_STYLE_UNKNOWN_LABEL if value == ""
                          else CONTACT_STYLE_LABELS.get(value, value))
                combo.setItemText(index, self.i18n.text(source))

    def _on_sides_changed(self) -> None:
        self._draw_preview()
        self._sync_contact_style_bar()

    def _on_side_edited(self, *_args) -> None:
        """@brief A user edit of the sides table: refresh views and drop the old 3D.

        @param _args Signal payload (index, value, checked), unused.
        @note Stessa disciplina delle modifiche alla tabella componenti e ai
            campi numerici: il solido gia' generato descrive l'input di prima,
            quindi l'anteprima va tolta e un risultato CAD tardivo, marchiato
            con la revisione vecchia, non deve poterla ripristinare. Prima i
            lati cambiavano footprint e anteprima 2D ma lasciavano a video il
            vecchio 3D, che sembrava gia' aggiornato.
        """
        self._on_sides_changed()
        self._invalidate_generated_mesh()

    def _on_contact_style_edited(self, *_args) -> None:
        """@brief The contact style of one side was changed in the table.

        @param _args Signal payload (combo index), unused.
        @note Nessuna chiamata AI: il tipo di contatto e' un dato che l'utente
            vede sulla scheda e corregge qui; basta rigenerare footprint e 3D.
        """
        self._warn_if_contact_style_unsupported()
        self._on_side_edited()

    def _table_contact_styles(self) -> list[str]:
        """Contact style of each castellated side, in side order."""
        return [side["contact_style"] for side in self._sides]

    @staticmethod
    def _contact_style_supported() -> bool:
        """True when the installed `CastellatedSide` accepts `contact_style`."""
        return accepts_keyword(CastellatedSide, "contact_style")

    def _warn_if_contact_style_unsupported(self) -> None:
        """Say in the log when a chosen style cannot reach the generator.

        Without the field a non-default choice would vanish on its way to
        generation; generating the default while the combo shows another
        style must not look like a success.
        """
        if self._contact_style_supported():
            return
        chosen = {s for s in self._table_contact_styles()
                  if s is not None and s != default_contact_style()}
        if chosen:
            self._append_log(
                tx("ATTENZIONE: il generatore installato non conosce ancora il tipo di "
                   "contatto: la scelta viene ignorata e footprint e 3D restano «{get}».",
                   get=CONTACT_STYLE_LABELS.get(default_contact_style(), default_contact_style())))

    def _sides_from_table(self) -> list[CastellatedSide]:
        """@brief The castellated sides as the table describes them now.

        @return One `CastellatedSide` per row, in row order.
        @note `contact_style` e' passato solo se la classe installata lo
            accetta (stessa introspezione di `phase_kwargs`/`cancel_kwargs`):
            GUI e generatore evolvono separatamente e un TypeError qui
            bloccherebbe ogni generazione per un campo accessorio.
        """
        style_supported = self._contact_style_supported()
        sizes_supported = accepts_keyword(CastellatedSide, "hole_diameter")
        sides = []
        for side in self._sides:
            extra = {"contact_style": side["contact_style"]} if style_supported else {}
            if sizes_supported:
                # Diametri del lato: una misura dichiarata si passa com'e' (il
                # generatore la rispetta o la rifiuta), una stima come
                # AssumedSize (il generatore la riduce se non sta e lo annota).
                extra.update(
                    hole_diameter=_side_size(side, "half_hole_diameter"),
                    thru_hole_drill=_side_size(side, "through_hole_drill"))
            sides.append(CastellatedSide(
                edge=side["edge"], count=side["count"], pitch=side["pitch_mm"],
                offset=side["offset_mm"], first_pin=side["first_pin"],
                reverse=side["reverse"],
                # Piazzole del lato (28/09), stessa regola dei fori: None = si
                # usa il campo del modulo; stima = AssumedSize.
                pad_length=_side_size(side, "pad_length"),
                pad_width=_side_size(side, "pad_width"), **extra,
            ))
        return sides

    def _serialised_sides(self) -> list[dict]:
        """@brief The table's sides as extraction-shaped dicts, style included.

        @return Dicts with the keys `_apply_extracted_castellated` reads back.
        @note Lo stile viene dalla tabella e non dal `CastellatedSide`: e' la
            scelta dell'utente e va conservata anche quando il generatore
            installato non la conosce ancora.
        """
        return [dict(edge=s.edge, count=s.count, pitch_mm=s.pitch, offset_mm=s.offset,
                     first_pin=s.first_pin, reverse=s.reverse, contact_style=style,
                     half_hole_diameter_mm=raw.get("half_hole_diameter_mm"),
                     through_hole_drill_mm=raw.get("through_hole_drill_mm"),
                     half_hole_diameter_declared=bool(raw.get("half_hole_diameter_declared")),
                     through_hole_drill_declared=bool(raw.get("through_hole_drill_declared")),
                     pad_length_mm=raw.get("pad_length_mm"),
                     pad_width_mm=raw.get("pad_width_mm"),
                     pad_length_declared=bool(raw.get("pad_length_declared")),
                     pad_width_declared=bool(raw.get("pad_width_declared")))
                for s, style, raw in zip(self._sides_from_table(), self._table_contact_styles(),
                                         self._sides)]

    def _sides_with_table_styles(self, sides):
        """Carry the table's contact styles onto extraction side dicts.

        Used when an extraction is re-applied without a new reading of the
        board (inventory relaunch): the style the user corrected must not
        silently revert to the one of the earlier extraction. Sides are
        matched by edge, and only when that edge appears exactly once on both
        sides of the match: with duplicates the pairing would be a guess.
        """
        if not sides or not self._is_castellated():
            return sides
        rows = [(side["edge"], side["contact_style"]) for side in self._sides]
        edges = [edge for edge, _style in rows]
        declared = [side.get("edge") for side in sides]
        result = []
        for side in sides:
            edge = side.get("edge")
            if edges.count(edge) == 1 and declared.count(edge) == 1:
                side = dict(side, contact_style=rows[edges.index(edge)][1])
            result.append(side)
        return result

    def _sync_contact_style_bar(self) -> None:
        """Mirror the table's contact styles into the 3D-tab combo.

        Shown only for castellated modules with at least one side. When the
        sides disagree the combo shows a disabled «mixed» entry instead of
        pretending one style applies to all.
        """
        combo = getattr(self, "combo_contact_style_3d", None)
        if combo is None:
            return
        styles = self._table_contact_styles()
        self.contact_style_bar_3d.setVisible(self._is_castellated() and bool(styles))
        unique = set(styles)
        blocked = combo.blockSignals(True)
        if None in unique:
            # Almeno un lato senza tipo: la voce «Da scegliere» (ultima).
            combo.setCurrentIndex(combo.count() - 1)
        elif len(unique) == 1:
            combo.setCurrentIndex(max(combo.findData(styles[0]), 0))
        else:
            # Lati diversi: la voce «Diverso per lato» (penultima).
            combo.setCurrentIndex(combo.count() - 2)
        combo.blockSignals(blocked)

    def _on_contact_style_all_changed(self, index: int) -> None:
        """@brief Apply the 3D-tab contact style to every side of the table.

        @param index Chosen combo index; the «mixed» entry is informative only.
        @note E' l'unica via per cambiarlo: i difetti dei contatti si
            scoprono guardando il 3D. Il cambio segue la stessa strada di ogni
            modifica ai lati: anteprima invalidata, nessuna chiamata AI.
        """
        style = self.combo_contact_style_3d.itemData(index)
        if not style:
            # «Diverso per lato» e «Da scegliere» sono solo informative.
            return
        for side in self._sides:
            side["contact_style"] = style
        self._on_contact_style_edited()

    def _current_castellated_params(self) -> CastellatedModuleParams:
        return CastellatedModuleParams(
            name=self.field_name.text().strip(),
            description=self.field_description.text().strip(),
            tags=self.field_tags.text().strip(),
            datasheet=self._effective_datasheet(),
            body_length=self.field_cast_body_length.value(),
            body_width=self.field_cast_body_width.value(),
            # Default dei lati senza misura propria: il land pattern di
            # riferimento arriva come stima (None -> AssumedSize), un valore
            # diverso (progetto salvato, utente) come misura dichiarata.
            pad_length=_declared_size(self.field_cast_pad_length.value(),
                                      _reference_pad("pad_length")),
            pad_width=_declared_size(self.field_cast_pad_width.value(),
                                     _reference_pad("pad_width")),
            pad_overhang=self.field_cast_overhang.value(),
            # Un valore di riferimento non e' una misura: passato come None il
            # generatore lo usa come stima e, se non sta nel passo di un lato,
            # lo riduce e lo scrive nelle note invece di rifiutare (26/09).
            hole_diameter=_declared_size(self.field_cast_hole.value(), 1.7),
            thru_hole_drill=_declared_size(self.field_cast_thru_drill.value(), 1.0),
            thru_pad_diameter=_declared_size(self.field_cast_thru_pad.value(), 1.7),
            thru_hole_offset=self.field_cast_thru_offset.value(),
            hole_inset=(self.field_cast_hole_inset.value() or None),
            pcb_thickness=self.field_cast_pcb.value(),
            component_inset=self.field_cast_inset.value(),
            # Volutamente NON il colore del corpo: il blocco di ripiego (senza
            # componenti dalla foto) deve distinguersi dal PCB, non sparirci sopra.
            component_color_hex="#3a3a3a",
            components=self._components_from_table(),
            silk_labels=self._silk_labels(),
            sides=self._sides_from_table(),
            **self._declared_row_spacing_kwargs(),
        )

    def _declared_row_spacing_kwargs(self) -> dict:
        """@brief La distanza fra le file dichiarata, se c'e' e il generatore la accetta.

        @return {"row_spacing": mm} oppure {}.
        @note Serve ai contatti a foro passante: dalla distanza fra le file e
            dalla larghezza del corpo il generatore ricava a che distanza dal
            bordo stanno i fori. Sulla scheda dell'utente (25,4 mm, file a
            22,86 mm) sono 1,27 mm; senza questa quota il generatore ripiega su
            una stima per difetto (1,05 mm) e lo scrive nel log.
        """
        spacing = getattr(self, "_declared_row_spacing", None)
        if spacing is None or not accepts_keyword(CastellatedModuleParams, "row_spacing"):
            return {}
        return {"row_spacing": spacing}

    def _show_pins_3d(self) -> bool:
        """@brief Tell whether a board's 3D model shows male header pins below it.

        @return False unless the project turned them on (user, 2026-09-26:
            «possibilità di aggiungere» -- off by default on both boards).
        """
        return self._project_option('show_pins_3d', False) is True

    def _restore_top_text(self, saved) -> None:
        """@brief Put the saved top-face writing in its field; a project without one gets the name.

        @param saved The `top_text` of the project state: a string (possibly
            empty, meaning «no writing») or None when the project predates
            the field (prefilled with the component name, the assumption
            declared on 2026-10-10).
        """
        field = getattr(self, "field_top_text_3d", None)
        if field is None:
            return
        text = saved if isinstance(saved, str) else self._component_name()
        blocked = field.blockSignals(True)
        field.setText(text)
        field.blockSignals(blocked)
        self._applied_top_text = text.strip()

    def _pin_numbers_3d(self) -> bool:
        """@brief Tell whether a single THT part prints its pin numbers on the bottom face.

        @return True unless the project turned them off: the user asked for
            them (2026-10-10, «i numeri di pin sotto il device, come nel
            castellated»), so a new project prints them. A project saved
            with the option off keeps it off.
        """
        return self._project_option('pin_numbers_3d', True) is not False

    def _top_text_3d(self) -> str:
        """@brief The writing printed on the top face of a single component, '' for none."""
        field = getattr(self, "field_top_text_3d", None)
        return field.text().strip() if field is not None else ""

    def _face_texts_for_3d(self) -> list:
        """@brief Face texts handed to FreeCAD: the saved annotations plus the top-face writing.

        @return FaceTextAnnotation list. The top-face writing comes from the
            field in the 3D tab, sized so that its estimated width stays
            within 80% of the body length (glyph width taken as 0.65 of the
            size, the same estimate as the pin labels), at most 2 mm; it is
            printed only on a single component, which has a real body, and
            never on a board module.
        """
        texts = list(self.face_text_annotations)
        text = self._top_text_3d()
        if not text or not self._is_single_component_project() or not self.group_3d.isChecked():
            return texts
        body = self._current_body_params()
        if body is None:
            return texts
        # Sized on the SAME body FreeCAD receives (a package takes its
        # dimensions from package_geometry, not from the fields: review C, 2).
        # Both extents of the face: 80% of the length for the width of the
        # text, 80% of the width for its height; the script fits the solid
        # again and says when it gets illegible.
        size = min(2.0, max(0.3, 0.8 * body.length_mm / (0.65 * len(text))),
                   max(0.3, 0.8 * body.width_mm))
        texts.append(FaceTextAnnotation(face="top", text=text, size_mm=round(size, 3)))
        return texts

    def _on_pin_numbers_3d_toggled(self, checked: bool) -> None:
        """@brief Record the pin-numbers choice for the project; the solid on screen is stale.

        @param checked New state of the checkbox.
        """
        self._set_project_option("pin_numbers_3d", checked)
        self._save_config_safely()
        self._invalidate_generated_mesh()

    def _on_top_text_edited(self) -> None:
        """@brief The top-face writing changed: keep it with the project, the solid is stale.

        @note `editingFinished` also fires on Enter or focus loss without a
            change: then nothing is stale and a running 3D build is not
            discarded (review C, 8).
        """
        text = self._top_text_3d()
        if text == getattr(self, "_applied_top_text", None):
            return
        self._applied_top_text = text
        self._invalidate_generated_mesh()
        self._save_project_config()

    def _silk_pin_labels(self) -> bool:
        """@brief Tell whether pin names are engraved on the 3D silkscreen."""
        return self._project_option('silk_pin_labels', False) is True

    def _silk_labels(self) -> dict[str, str]:
        """Sigla serigrafata per ogni pad, dalla tabella pinout.

        Sul modulo accanto al contatto c'e' la sigla corta (PE, B-, A+, G...),
        non la descrizione: si tiene la prima parola, senza parentesi.

        Vuoto quando l'opzione e' spenta: su un modulo a passo fitto le sigle
        si sovrappongono fra loro e finiscono sopra i componenti.
        """
        if not self._silk_pin_labels():
            return {}
        labels = {}
        for entry in self.pinout:
            name = str(entry.get("name", "")).split("(")[0].strip()
            short = name.split()[0] if name else ""
            if short:
                labels[str(entry.get("pin"))] = short[:6]
        return labels

    def _is_castellated(self) -> bool:
        return hasattr(self, "group_castellated") and self.group_castellated.isChecked()

    def _enable_3d_group_from_extraction(self) -> None:
        """@brief Accende il gruppo «Modello 3D» quando l'estrazione porta un solido.

        @note Senza FreeCAD non si accende affatto: riaccenderlo faceva finire
            ogni generazione con un `cad3d_error`. Dal 2026-09-26 l'interruttore
            non e' piu' a disposizione dell'utente (il tab Footprint 2D e' stato
            tolto), quindi il 3D si chiede sempre quando FreeCAD c'e'.
        """
        if self._freecad_available is False:
            return
        self.group_3d.setChecked(True)

    # ── Tab 3: Genera ──────────────────────────────────────────────

    # ── Tab 6: KiCad ──────────────────────────────────────────────

    def _build_kicad_tab(self) -> QWidget:
        """@brief Build the preflight, preview and installation workflow."""
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(14)

        intro = QLabel(
            "Controlla il risultato in KiCad prima di aggiungerlo a una libreria. "
            "Il test usa un progetto isolato e non modifica la configurazione di KiCad.")
        intro.setWordWrap(True)
        outer.addWidget(intro)

        artifacts_group = QGroupBox("File pronti per KiCad")
        artifacts_form = QFormLayout(artifacts_group)
        self.label_kicad_footprint = QLabel("—")
        self.label_kicad_footprint.setProperty("i18n_skip", True)
        self.label_kicad_footprint.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.label_kicad_footprint.setWordWrap(True)
        self.label_kicad_step = QLabel("—")
        self.label_kicad_step.setProperty("i18n_skip", True)
        self.label_kicad_step.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.label_kicad_step.setWordWrap(True)
        self.label_kicad_symbol = QLabel("Da esportare")
        self.label_kicad_symbol.setProperty("i18n_skip", True)
        self.label_kicad_symbol.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.label_kicad_symbol.setWordWrap(True)
        artifacts_form.addRow("Footprint:", self.label_kicad_footprint)
        symbol_row = QWidget()
        symbol_row_layout = QHBoxLayout(symbol_row)
        symbol_row_layout.setContentsMargins(0, 0, 0, 0)
        symbol_row_layout.setSpacing(8)
        symbol_row_layout.addWidget(self.label_kicad_symbol, 1)
        self.btn_export_symbol = self._workflow_tool_button(
            "tab-symbol.svg", "Esporta simbolo KiCad…",
            "Esporta il simbolo elettrico applicato in un file .kicad_sym")
        self.btn_export_symbol.clicked.connect(self.symbol_tab.export_requested.emit)
        symbol_row_layout.addWidget(self.btn_export_symbol)
        artifacts_form.addRow("Simbolo elettrico:", symbol_row)
        artifacts_form.addRow("Modello STEP:", self.label_kicad_step)
        self.file_paths_toggle = QToolButton()
        self.file_paths_toggle.setText(self._file_paths_toggle_text(False))
        self.file_paths_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.file_paths_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.file_paths_toggle.setCheckable(True)
        self.file_paths_toggle.setProperty("i18n_skip", True)
        self.file_paths_toggle.toggled.connect(self._toggle_kicad_file_paths)
        artifacts_form.addRow(self.file_paths_toggle)
        self.file_paths_detail = QWidget()
        paths_layout = QVBoxLayout(self.file_paths_detail)
        paths_layout.setContentsMargins(18, 2, 4, 4)
        paths_layout.setSpacing(3)
        self.label_kicad_footprint_path = QLabel("Percorso footprint: —")
        self.label_kicad_symbol_path = QLabel("Percorso simbolo elettrico: —")
        self.label_kicad_step_path = QLabel("Percorso modello STEP: —")
        for path_label in (self.label_kicad_footprint_path, self.label_kicad_symbol_path,
                           self.label_kicad_step_path):
            path_label.setWordWrap(True)
            path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            path_label.setProperty("i18n_skip", True)
            paths_layout.addWidget(path_label)
        self.file_paths_detail.hide()
        artifacts_form.addRow(self.file_paths_detail)
        self.symbol_tab.export_enabled_changed.connect(self.btn_export_symbol.setEnabled)
        self.btn_export_symbol.setEnabled(self.symbol_tab.export_button.isEnabled())
        self._align_form(artifacts_form, "kicad")
        outer.addWidget(artifacts_group)
        outer.addWidget(self._build_metadata_group())

        review_group = QGroupBox("Verifica prima dell’installazione")
        review_layout = QVBoxLayout(review_group)
        review_note = QLabel(
            "Crea un piccolo progetto temporaneo con il footprint e il modello 3D, "
            "quindi lo apre nel PCB Editor. In KiCad premi Alt+3 per la vista 3D.")
        review_note.setWordWrap(True)
        # Icone al posto dei pulsanti di testo, nome e spiegazione nel tooltip
        # (utente, 2026-09-24), con lo stesso stile delle altre barre: la
        # spiegazione della sezione resta scritta accanto all'icona.
        self.btn_test_kicad = self._workflow_tool_button(
            "kicad-test.svg", "Prova in KiCad...",
            "Apre un progetto temporaneo: non installa e non modifica librerie KiCad")
        self.btn_test_kicad.clicked.connect(self._test_in_kicad)
        self.btn_verify_component = self._workflow_tool_button(
            'wf-review.svg', 'Verifica componente per KiCad',
            'Controlla footprint, simbolo, corrispondenza pin-pad e importazione 3D con KiCad')
        self.btn_verify_component.clicked.connect(self._verify_component)
        self.btn_kicad_guide = self._workflow_tool_button(
            'info.svg', 'Guida rapida per usare il componente in KiCad',
            'Apri la procedura guidata per installare e usare footprint e simbolo')
        self.btn_kicad_guide.clicked.connect(self._show_kicad_usage_wizard)
        review_layout.addLayout(self._note_with_icons(
            review_note, self.btn_kicad_guide, self.btn_verify_component, self.btn_test_kicad))
        outer.addWidget(review_group)

        install_group = QGroupBox("Installazione nella libreria")
        install_layout = QVBoxLayout(install_group)
        install_note = QLabel(
            "La libreria di progetto è portabile ed è la scelta consigliata. "
            "La libreria globale rende il footprint disponibile in tutti i progetti "
            "di questo computer.")
        install_note.setWordWrap(True)
        self.btn_export = self._workflow_tool_button(
            "install-project.svg", "Installa nel progetto KiCad...",
            "Crea KiCadGen.pretty e KiCadGen.3dshapes nel progetto selezionato")
        self.btn_export.clicked.connect(self._install_project_library)
        self.btn_install_global = self._workflow_tool_button(
            "install-global.svg", "Installa come libreria globale...",
            "Installa nella cartella scelta e registra la libreria globale di KiCad")
        self.btn_install_global.clicked.connect(self._install_global_library)
        install_layout.addLayout(
            self._note_with_icons(install_note, self.btn_export, self.btn_install_global))
        outer.addWidget(install_group)

        # La riga in fondo sta in un riquadro come le altre (utente,
        # 2026-09-24). Lo stato resta TESTO: e' un esito da leggere subito, non
        # il nome di un comando da scoprire passando col mouse.
        folder_group = QGroupBox("Cartella del componente")
        folder_layout = QVBoxLayout(folder_group)
        self.label_kicad_status = QLabel()
        self.label_kicad_status.setProperty("i18n_skip", True)
        self.label_kicad_status.setWordWrap(True)
        self.label_kicad_status.setStyleSheet(f"color: {YELLOW};")
        self.btn_open_output = self._workflow_tool_button(
            "docs-open.svg", "Apri cartella componente",
            "Apre la cartella del componente con footprint, modello STEP e documenti.")
        self.btn_open_output.clicked.connect(self._open_project_dir)
        folder_layout.addLayout(self._note_with_icons(self.label_kicad_status,
                                                      self.btn_open_output))
        outer.addWidget(folder_group)
        outer.addStretch()
        self._refresh_output_actions()
        return tab

    def _build_exports_tab(self) -> QWidget:
        """@brief Build «Esportazioni»: the persistent list of KiCad exports.

        @return The tab widget, always enabled.
        @note The history is global to the installation, not to the open
            project (user, 2026-10-11: moved out of the KiCad tab into its
            own tab, where the table takes the whole height).
        """
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(14)
        intro = QLabel(
            "Storico delle installazioni nelle librerie di KiCad fatte da questo "
            "computer: componente, tipo di esportazione, destinazione e data.")
        intro.setWordWrap(True)
        outer.addWidget(intro)
        history_group = QGroupBox("Componenti esportati in KiCad")
        history_layout = QVBoxLayout(history_group)
        self.kicad_export_table = QTableWidget(0, 4)
        self.kicad_export_table.setHorizontalHeaderLabels(
            ["Componente", "Esportazione", "Destinazione", "Data"])
        self.kicad_export_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.kicad_export_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.kicad_export_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.kicad_export_table.verticalHeader().setVisible(False)
        self.kicad_export_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents)
        self.kicad_export_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents)
        self.kicad_export_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch)
        self.kicad_export_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents)
        history_layout.addWidget(self.kicad_export_table)
        self._refresh_kicad_export_history()
        outer.addWidget(history_group, 1)
        return tab

    # ── Metadati del componente (scheda KiCad) ──────────────────────

    def _build_metadata_group(self) -> QGroupBox:
        """@brief Build «Metadati del componente»: library metadata and the live KLC list.

        @return The group box, enabled with the KiCad tab (footprint on disk).
        @note Description and keywords are VIEWS of `field_description` and
            `field_tags` (the footprint's descr/tags, data objects without a
            widget of their own): the same text goes to the footprint and to
            the symbol's Description/ki_keywords, so there is one value. The
            designator and the footprint filters follow a proposal while
            empty (shown as placeholder, see `_effective_reference` and
            `_effective_fp_filters`); typing replaces the proposal.
        """
        from PyQt6.QtCore import QRegularExpression
        from PyQt6.QtGui import QRegularExpressionValidator
        from .collapsible_group import CollapsibleGroupBox
        # Collapsed by default (user, 2026-10-11); the title sits on the
        # frame like every other block of the tab and a click on it toggles.
        group = CollapsibleGroupBox("Metadati del componente")
        group.setToolTip(
            "Designatore, descrizione, parole chiave, datasheet e filtri del "
            "componente, con le regole KLC. Clic sul titolo per aprire o chiudere.")
        self.metadata_body = group.body
        self._metadata_help_buttons: dict[str, QToolButton] = {}
        self._metadata_help_dialog = None
        form = QFormLayout(self.metadata_body)
        form.setContentsMargins(0, 6, 0, 0)
        self.field_reference = QLineEdit()
        # Letters only: KiCad appends the number (KLC S6.1); the text is
        # kept upper case because «u1» is not a prefix KiCad recognises.
        self.field_reference.setValidator(
            QRegularExpressionValidator(QRegularExpression("[A-Za-z]*"), self.field_reference))
        self.field_reference.setMaxLength(8)
        self.field_reference.setToolTip(
            "Prefisso del riferimento nello schema (U, J, K...): solo lettere, "
            "KiCad aggiunge il numero.")
        self.field_reference.textChanged.connect(self._on_reference_edited)
        form.addRow("Designatore di riferimento:", self._metadata_field_row(self.field_reference, "reference"))
        self.field_meta_description = QLineEdit()
        self.field_meta_description.setToolTip(
            "Una riga che descrive il componente: va nel footprint (descr) e nel "
            "simbolo (Description).")
        form.addRow("Descrizione:", self._metadata_field_row(self.field_meta_description, "description"))
        self.field_meta_keywords = QLineEdit()
        self.field_meta_keywords.setToolTip(
            "Parole separate da spazi per la ricerca nel selettore: footprint (tags) "
            "e simbolo (ki_keywords).")
        form.addRow("Parole chiave:", self._metadata_field_row(self.field_meta_keywords, "keywords"))
        self._mirror_text_value(self.field_meta_description, self.field_description)
        self._mirror_text_value(self.field_meta_keywords, self.field_tags)
        self.field_meta_description.textChanged.connect(lambda _text: self._refresh_klc_findings())
        self.field_meta_keywords.textChanged.connect(lambda _text: self._refresh_klc_findings())
        self.field_datasheet = QLineEdit()
        self.field_datasheet.setToolTip(
            "URL o percorso del documento, senza spazi: proprietà Datasheet di "
            "simbolo e footprint.")
        self.field_datasheet.textChanged.connect(lambda _text: self._refresh_klc_findings())
        form.addRow("Datasheet (URL o file):", self._metadata_field_row(self.field_datasheet, "datasheet"))
        self.field_fp_filters = QLineEdit()
        self.field_fp_filters.setToolTip(
            "Schemi con * e ? che limitano i footprint proponibili per il simbolo "
            "(ki_fp_filters).")
        self.field_fp_filters.textChanged.connect(lambda _text: self._refresh_klc_findings())
        form.addRow("Filtri footprint:", self._metadata_field_row(self.field_fp_filters, "fp_filters"))
        self.btn_apply_metadata = self._workflow_tool_button(
            "confirm-use.svg", "Applica ai file",
            "Salva il progetto, rigenera il footprint e riesporta il simbolo se esiste già")
        self.btn_apply_metadata.clicked.connect(self._apply_metadata_to_files)
        apply_row = QWidget()
        apply_layout = QHBoxLayout(apply_row)
        apply_layout.setContentsMargins(0, 0, 0, 0)
        apply_layout.addStretch()
        apply_layout.addWidget(self.btn_apply_metadata)
        form.addRow(apply_row)
        self.klc_list = QListWidget()
        # Findings are messages computed at run time, not catalogue sources.
        self.klc_list.setProperty("i18n_skip", True)
        self.klc_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.klc_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.klc_list.setMaximumHeight(120)
        self.label_klc = QLabel("Regole KLC:")
        self.label_klc.setToolTip(
            "Osservazioni sulle convenzioni delle librerie KiCad: ⛔ errore, ⚠ avviso.")
        form.addRow(self.label_klc, self._metadata_field_row(self.klc_list, "klc", top=True))
        QWidget.setTabOrder(self.field_reference, self.field_meta_description)
        QWidget.setTabOrder(self.field_meta_description, self.field_meta_keywords)
        QWidget.setTabOrder(self.field_meta_keywords, self.field_datasheet)
        QWidget.setTabOrder(self.field_datasheet, self.field_fp_filters)
        QWidget.setTabOrder(self.field_fp_filters, self.btn_apply_metadata)
        self._align_form(form, "kicad")
        self.group_metadata = group
        group.setEnabled(False)
        return group

    #: Help topics of «Metadati del componente», in row order: key -> source
    #: of the dialog title. The texts live in `_metadata_help_content`.
    METADATA_HELP_TITLES = {
        "reference": "Designatore di riferimento",
        "description": "Descrizione",
        "keywords": "Parole chiave",
        "datasheet": "Datasheet (URL o file)",
        "fp_filters": "Filtri footprint",
        "klc": "Regole KLC",
    }

    def _metadata_field_row(self, field: QWidget, key: str, top: bool = False) -> QWidget:
        """@brief A metadata row: the field, then its help icon at the right end.

        @param field Editor (or the KLC list) that takes the free width.
        @param key Help topic, a key of `METADATA_HELP_TITLES`.
        @param top True to keep the icon on the first line of a tall field.
        @return The row widget to put in the form.
        @note The icon opens a modal explanation (`_show_metadata_help`), like
            the ⓘ of the component folder (user, 2026-10-12): the tooltip
            alone was not enough to say what each property is for in KiCad.
        """
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(field, 1)
        button = self._icon_only_button("info.svg")
        button.setProperty("metadata_help", key)
        button.clicked.connect(lambda _checked=False, key=key: self._show_metadata_help(key))
        alignment = Qt.AlignmentFlag.AlignTop if top else Qt.AlignmentFlag.AlignVCenter
        layout.addWidget(button, 0, alignment)
        self._metadata_help_buttons[key] = button
        self._refresh_metadata_help_tooltips()
        return row

    def _refresh_metadata_help_tooltips(self) -> None:
        """@brief Tooltip and accessible name of every metadata help icon, in the current language.

        @note Icon-only buttons opt out of the generic retranslation (their
            tooltip is composed), so this runs at build time and on every
            language change (review, 2026-10-12: they stayed Italian).
        """
        for key, button in self._metadata_help_buttons.items():
            title = self.i18n.text("Aiuto: {field}").format(
                field=self.i18n.text(self.METADATA_HELP_TITLES[key]))
            self._apply_icon_tooltip(button, title, self.i18n.text(
                "Che cosa inserire, dove finisce nei file KiCad e quali regole KLC lo riguardano."))

    def _metadata_help_content(self, key: str) -> list[tuple[str, str]]:
        """@brief The explanation of a metadata field, as (section title, text) pairs.

        @param key A key of `METADATA_HELP_TITLES`.
        @return Translated sections in reading order; empty for an unknown key.
        @note The KLC statements must agree with `services.klc_checks`: an
            «error» there blocks the installation (after a confirmation), a
            «warning» is advice. Keep both in step when a rule changes.
        """
        where = self.i18n.text("Dove finisce")
        what = self.i18n.text("Che cosa inserire")
        rules = self.i18n.text("Regole KLC")
        examples = self.i18n.text("Esempi")
        content = {
            "reference": [
                (what, self.i18n.text(
                    "Il prefisso del riferimento che il componente avrà nello schema: solo lettere, "
                    "senza cifre, spazi o «?». KiCad aggiunge da sé il numero progressivo (U1, U2…). "
                    "Vuoto, vale la proposta mostrata in grigio, scelta dalla tipologia del componente.")),
                (where, self.i18n.text(
                    "Proprietà «Reference» del simbolo. Il footprint eredita il riferimento dal simbolo "
                    "quando lo schema viene annotato e portato sul PCB.")),
                (rules, self.i18n.text(
                    "KLC S6.1: prefisso di sole lettere. Un designatore vuoto o con altri caratteri è un "
                    "errore ⛔: «Applica ai file» lo rifiuta e l’installazione chiede conferma.")),
                (examples, self.i18n.text(
                    "U circuiti integrati e moduli · J connettori · K relè · Q transistor · D diodi e LED · "
                    "R resistori · C condensatori · L induttori · Y quarzi · SW interruttori.")),
            ],
            "description": [
                (what, self.i18n.text(
                    "Una sola riga che dice che cosa è il componente: funzione, package o modulo, "
                    "dimensioni principali. È il testo che si legge nel selettore di KiCad accanto al nome, "
                    "quindi va scritto per chi cerca il componente, non per chi lo ha generato.")),
                (where, self.i18n.text(
                    "Proprietà «Description» del simbolo e campo «descr» del footprint: lo stesso testo "
                    "in entrambi, così simbolo e footprint restano riconoscibili come coppia.")),
                (rules, self.i18n.text(
                    "Le convenzioni KiCad la richiedono: vuota è un avviso ⚠ (non blocca). Su più righe o "
                    "con caratteri di controllo è un errore ⛔.")),
                (examples, self.i18n.text(
                    "«Modulo ESP32-S3-WROOM-1U, 2x22 contatti castellated, 63.30 x 25.40 mm» · "
                    "«Regolatore LDO 3.3 V 1 A, SOT-223».")),
            ],
            "keywords": [
                (what, self.i18n.text(
                    "Parole separate da spazi con cui il componente si trova cercando nel selettore: "
                    "famiglia, funzione, package, produttore. Non ripetere il nome del componente, che "
                    "viene già cercato; meglio pochi termini precisi che un elenco lungo.")),
                (where, self.i18n.text(
                    "Proprietà «ki_keywords» del simbolo e campo «tags» del footprint.")),
                (rules, self.i18n.text(
                    "Vuote è un avviso ⚠: la ricerca funziona peggio ma l’installazione procede. "
                    "Con caratteri di controllo è un errore ⛔.")),
                (examples, self.i18n.text(
                    "«ESP32-S3 wifi bluetooth modulo castellated» · «ldo regolatore 3.3V sot-223».")),
            ],
            "datasheet": [
                (what, self.i18n.text(
                    "L’indirizzo del documento tecnico del componente: un URL (preferito, lo apre chiunque "
                    "riceva la libreria) oppure il percorso di un file. Senza spazi: KiCad lo tratta come un "
                    "collegamento.")),
                (where, self.i18n.text(
                    "Proprietà «Datasheet» del simbolo e del footprint. In KiCad si apre dal menu del "
                    "componente con «Mostra datasheet».")),
                (rules, self.i18n.text(
                    "Vuoto o con spazi è un avviso ⚠: si installa lo stesso. Con caratteri di controllo "
                    "è un errore ⛔.")),
                (examples, self.i18n.text(
                    "«https://www.espressif.com/documentation/esp32-s3-wroom-1_wroom-1u_datasheet_en.pdf» · "
                    "«docs/ESP32-S3-DEVKIT.pdf».")),
            ],
            "fp_filters": [
                (what, self.i18n.text(
                    "Schemi, separati da spazi, che dicono quali footprint sono adatti al simbolo: "
                    "«*» vale qualunque sequenza, «?» un solo carattere. Lo strumento di assegnazione "
                    "dei footprint propone solo quelli che corrispondono. Vuoto, vale la proposta in grigio "
                    "ricavata dal nome del footprint generato.")),
                (where, self.i18n.text(
                    "Proprietà «ki_fp_filters» del simbolo. Il footprint non ne ha bisogno.")),
                (rules, self.i18n.text(
                    "Un filtro con separatori di percorso («/» o «\\») o con più di un «:» è un errore ⛔: "
                    "«Applica ai file» lo rifiuta e l’installazione chiede conferma. Un filtro che non "
                    "corrisponde a nulla fa solo sparire il footprint dalle proposte.")),
                (examples, self.i18n.text(
                    "«ESP32-S3-DEVKIT-NXRX*» · «SOT-223*» · «*QFN*48*».")),
            ],
            "klc": [
                (what, self.i18n.text(
                    "Le KiCad Library Conventions (KLC) sono le regole con cui vengono scritte le librerie "
                    "ufficiali di KiCad. Qui vengono controllate quelle sui metadati e sul footprint, "
                    "mentre scrivi: la lista si aggiorna da sola.")),
                (where, self.i18n.text(
                    "⛔ errore: ciò che KiCad o le convenzioni rifiutano; l’installazione chiede conferma "
                    "prima di procedere. ⚠ avviso: un consiglio, l’installazione procede.")),
                (rules, self.i18n.text(
                    "Designatore vuoto o non alfabetico, testi su più righe, filtri con separatori, nome "
                    "del footprint vuoto o con caratteri vietati: errori. Descrizione, parole chiave o "
                    "datasheet vuoti, spazi nel nome del footprint: avvisi. Il riferimento completo è "
                    "kicad.org, sezione «Library Conventions».")),
                (examples, self.i18n.text(
                    "«Nessuna osservazione.» vuol dire che i controlli eseguiti qui sono passati, non che "
                    "il componente sia verificato in KiCad: per quello c’è «Verifica prima "
                    "dell’installazione».")),
            ],
        }
        return content.get(key, [])

    def _show_metadata_help(self, key: str) -> None:
        """@brief Open the modal explanation of one metadata field.

        @param key A key of `METADATA_HELP_TITLES`.
        @note One dialog at a time, kept in `_metadata_help_dialog` so tests
            can reach it; it is modal and closes with «Chiudi» or Esc.
        """
        from html import escape
        dialog = QDialog(self)
        dialog.setProperty("i18n_skip", True)
        dialog.setWindowTitle(self.i18n.text("Aiuto: {field}").format(
            field=self.i18n.text(self.METADATA_HELP_TITLES[key])))
        dialog.setModal(True)
        layout = QVBoxLayout(dialog)
        parts = [f"<p><b>{escape(title)}</b><br>{escape(body)}</p>"
                 for title, body in self._metadata_help_content(key)]
        note = QLabel("".join(parts))
        note.setProperty("i18n_skip", True)
        note.setTextFormat(Qt.TextFormat.RichText)
        note.setWordWrap(True)
        note.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(note)
        close = QPushButton(self.i18n.text("Chiudi"))
        close.setProperty("i18n_skip", True)
        close.clicked.connect(dialog.accept)
        layout.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        dialog.resize(560, 320)
        self._metadata_help_dialog = dialog
        dialog.exec()
        if self._metadata_help_dialog is dialog:
            self._metadata_help_dialog = None
        dialog.deleteLater()

    @staticmethod
    def _mirror_text_value(line_edit: QLineEdit, value) -> None:
        """@brief Keep a QLineEdit and a `TextValue` equal in both directions.

        @param line_edit Visible editor.
        @param value `component_state.TextValue` holding the project datum.
        @note No loop: `TextValue.setText` notifies only on an actual change
            and the editor is written only when its text differs.
        """
        line_edit.setText(value.text())

        def to_editor(text):
            if line_edit.text() != text:
                line_edit.setText(text)

        value.on_change(to_editor)
        line_edit.textChanged.connect(value.setText)

    def _on_reference_edited(self, text: str) -> None:
        """@brief Upper-case the designator prefix and refresh the KLC list."""
        upper = text.upper()
        if upper != text:
            self.field_reference.setText(upper)
            return
        self._refresh_klc_findings()
        if hasattr(self, 'symbol_tab'):
            self.symbol_tab.set_reference(self._effective_reference())

    def _effective_reference(self) -> str:
        """@brief The designator prefix the files get: the field, or the proposal while empty."""
        typed = self.field_reference.text().strip() if hasattr(self, "field_reference") else ""
        if typed:
            return typed
        package = getattr(self, "package_geometry", None)
        return proposed_reference(self._selected_family(),
                                  package.family if package is not None else None)

    def _effective_fp_filters(self) -> str:
        """@brief The footprint filters the symbol gets: the field, or `<footprint>*` while empty."""
        typed = self.field_fp_filters.text().strip() if hasattr(self, "field_fp_filters") else ""
        if typed:
            return typed
        name = self._component_name()
        return f"{name}*" if name else ""

    def _effective_datasheet(self) -> str:
        """@brief Datasheet URL or path for the Datasheet property; '' when not given."""
        return self.field_datasheet.text().strip() if hasattr(self, "field_datasheet") else ""

    def _metadata_dict(self) -> dict:
        """@brief Library metadata as the KLC checks and the symbol exporter read them.

        @return Keys reference, description, keywords, datasheet, fp_filters
            (effective values: proposals already applied to empty fields).
        """
        return {"reference": self._effective_reference(),
                "description": self.field_description.text().strip(),
                "keywords": self.field_tags.text().strip(),
                "datasheet": self._effective_datasheet(),
                "fp_filters": self._effective_fp_filters()}

    def _symbol_metadata(self):
        """@brief `SymbolMetadata` for the symbol export, from the current fields."""
        from ..generators.symbol import SymbolMetadata
        return SymbolMetadata(**self._metadata_dict())

    def _metadata_state(self) -> dict:
        """@brief Metadata of the project file: what the user TYPED, not the proposals.

        @return {reference, datasheet, fp_filters}; description and keywords
            live in their own widgets (`field_description`, `field_tags`).
        @note Empty text is saved as empty: the proposal is recomputed on
            open, so a renamed project proposes its new footprint filter.
        """
        if not hasattr(self, "field_reference"):
            return {"reference": "", "datasheet": "", "fp_filters": ""}
        return {"reference": self.field_reference.text().strip(),
                "datasheet": self.field_datasheet.text().strip(),
                "fp_filters": self.field_fp_filters.text().strip()}

    def _restore_metadata_state(self, state) -> None:
        """@brief Put back the saved metadata; a missing key (older project) clears the fields.

        @param state The `metadata` object of the project file, or None.
        """
        if not hasattr(self, "field_reference"):
            return
        values = state if isinstance(state, dict) else {}
        for widget, key in ((self.field_reference, "reference"),
                            (self.field_datasheet, "datasheet"),
                            (self.field_fp_filters, "fp_filters")):
            value = values.get(key, "")
            if key == "reference" and isinstance(value, str) and not value.isalpha():
                # setText bypasses the line edit's validator: a hand-edited
                # project file must not put «U1» or «12» into the field.
                value = ""
            widget.setText(value if isinstance(value, str) else "")
        self._refresh_metadata_proposals()

    def _refresh_metadata_proposals(self) -> None:
        """@brief Show the current proposals as placeholders and refresh the KLC list."""
        if not hasattr(self, "field_reference"):
            return
        package = getattr(self, "package_geometry", None)
        self.field_reference.setPlaceholderText(proposed_reference(
            self._selected_family(), package.family if package is not None else None))
        name = self._component_name()
        self.field_fp_filters.setPlaceholderText(f"{name}*" if name else "")
        self._refresh_klc_findings()

    def _klc_findings(self) -> list:
        """@brief Current KLC findings: metadata, footprint name and the files on disk.

        @return `klc_checks.Finding` list, errors first; empty when nothing
            is to report or no project is open.
        """
        from ..services import klc_checks
        name = self._component_name()
        if not name:
            return []
        project = self._project_dir()
        symbol = project / f"{name}.kicad_sym"
        footprint = project / f"{name}.pretty" / f"{name}.kicad_mod"
        texts = []
        for path in (symbol, footprint):
            try:
                texts.append(path.read_text(encoding="utf-8") if path.is_file() else None)
            except OSError:
                texts.append(None)
        return klc_checks.check_all(self._metadata_dict(), name,
                                    symbol_text=texts[0], footprint_text=texts[1])

    def _refresh_klc_findings(self) -> None:
        """@brief Rewrite the read-only KLC list from the current fields and files."""
        if not hasattr(self, "klc_list"):
            return
        self.klc_list.clear()
        findings = self._klc_findings()
        if not findings:
            self.klc_list.addItem(self.i18n.text("Nessuna osservazione."))
            return
        for finding in findings:
            prefix = "⛔ " if finding.severity == "error" else "⚠ "
            self.klc_list.addItem(prefix + finding.message)

    def _confirm_klc_errors(self) -> bool:
        """@brief Ask before installing a component whose KLC checks report ERRORS.

        @return True to go on (no error, or the user confirmed).
        @note Warnings never block: they are advice. Errors are what KiCad or
            the conventions reject, so installing them is a choice to make
            knowingly, not a default.
        """
        errors = [f for f in self._klc_findings() if f.severity == "error"]
        if not errors:
            return True
        answer = QMessageBox.question(
            self, self.i18n.text("Osservazioni KLC bloccanti"),
            self.i18n.text("Il componente ha {count} errori KLC:\n{errors}\n\n"
                           "Installare comunque?").format(
                count=len(errors), errors="\n".join("⛔ " + f.message for f in errors)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes

    def _apply_metadata_to_files(self) -> None:
        """@brief Write the metadata into the files: save the project and regenerate the footprint.

        @note The symbol follows the footprint: `_on_generated` re-exports
            the applied symbol beside the artifacts (with `_symbol_metadata`)
            once the worker has written the .kicad_mod, so there is one write
            path for the symbol and no stale copy between the two files.
        """
        if self._background_work_running() or self._automatic_generation_active:
            QMessageBox.information(self, self.i18n.text("Metadati del componente"),
                                    self.i18n.text("Attendi la fine del lavoro in corso."))
            return
        errors = [f for f in self._klc_findings() if f.severity == "error"]
        if errors:
            # An error means KiCad or the KLC refuse the value (e.g. a
            # designator with digits): writing it would only move the
            # problem into the files. The list under the fields shows it.
            QMessageBox.warning(self, self.i18n.text("Metadati del componente"), self.i18n.text(
                "Correggi prima gli errori KLC elencati: i file non sono stati riscritti."))
            return
        self._save_project_config(force=True)
        if not self._generate(footprint_only=True):
            self._append_log(self.i18n.text(
                "Metadati salvati nel progetto; i file verranno riscritti alla prossima generazione."))
            self._refresh_klc_findings()
            return
        self._append_log(self.i18n.text(
            "Metadati applicati: footprint e simbolo in rigenerazione."))
        self._refresh_klc_findings()

    def _verify_component(self):
        from .component_check_dialog import ComponentCheckDialog
        if self._background_work_running() or self._automatic_generation_active:
            QMessageBox.information(self, self.i18n.text('Verifica componente per KiCad'),
                                    self.i18n.text('Attendi la fine del lavoro in corso.'))
            return
        if not self.symbol_tab.has_pending_edits():
            self._refresh_symbol_tab()
        pending = self.symbol_tab.has_pending_edits()
        # The check reads the .kicad_mod on disk: the applied footprint. A
        # pending footprint draft is reported, like the symbol's.
        args = dict(directory=self._project_dir(), name=self._component_name(),
                    rows=deepcopy(self.symbol_tab.rows()), pending=pending,
                    issues=self.symbol_tab.review_issues() if not pending else (),
                    footprint_pending=self.has_pending_footprint_edits())
        dialog = ComponentCheckDialog(args, self, tx=self.i18n.text)
        self._component_review_dialog = dialog
        try:
            dialog.exec()
        finally:
            self._component_review_dialog = None
            self._retry_footprint_rewrite()
        if getattr(dialog, "ai_review_requested", False):
            self.tabs.setCurrentIndex(TAB_SYMBOL)
            self._suggest_pin_types()

    def _file_paths_toggle_text(self, expanded: bool) -> str:
        """@brief Translated label of the full-paths toggle of the KiCad tab.

        @param expanded True when the paths are shown (the label offers to hide them).
        @return «Nascondi/Mostra percorsi completi» in the interface language.
        @note The button is excluded from the generic retranslation
            (`i18n_skip`, its text depends on its state): the window sets it
            at construction, on toggle and on a language change.
        """
        return self.i18n.text("Nascondi percorsi completi" if expanded
                              else "Mostra percorsi completi")

    def _toggle_kicad_file_paths(self, expanded: bool) -> None:
        """Show or collapse the full generated-file paths in the KiCad tab."""
        self.file_paths_detail.setVisible(expanded)
        self.file_paths_toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.file_paths_toggle.setText(self._file_paths_toggle_text(expanded))

    def _refresh_kicad_export_history(self) -> None:
        """Refresh the concise, persistent list of KiCad exports."""
        table = getattr(self, "kicad_export_table", None)
        if table is None:
            return
        records = load_kicad_export_history()
        # The table has a tab of its own: every record kept on disk is shown.
        table.setRowCount(len(records))
        for row, record in enumerate(records):
            destination = record.get("destination", "")
            values = (record.get("component", ""), record.get("kind", ""),
                      Path(destination).name or destination,
                      record.get("timestamp", "").replace("T", " ")[:19])
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(destination) if column == 2 else str(value))
                table.setItem(row, column, item)

    def _show_kicad_usage_wizard(self) -> None:
        """Guide the user through installing and using the generated component."""
        wizard = QWizard(self)
        wizard.setWindowTitle(self.i18n.text("Guida rapida: usare il componente in KiCad"))
        wizard.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        wizard.setMinimumWidth(650)

        def add_page(title, description, actions=()):
            page = QWizardPage(wizard)
            page.setTitle(self.i18n.text(title))
            layout = QVBoxLayout(page)
            label = QLabel(self.i18n.text(description))
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(label)
            for caption, callback in actions:
                button = QPushButton(self.i18n.text(caption), page)
                button.clicked.connect(callback)
                layout.addWidget(button)
            layout.addStretch()
            wizard.addPage(page)

        add_page(
            "1. Installa la libreria del componente",
            "La libreria di progetto è consigliata se il componente serve solo in questo progetto. "
            "La libreria globale è disponibile in tutti i progetti di questo computer. "
            "L’installazione registra footprint e modello 3D in KiCad.",
            (("Installa nel progetto KiCad…", self._install_project_library),
             ("Installa come libreria globale…", self._install_global_library)),
        )
        add_page(
            "2. Posiziona il footprint sul PCB",
            self.i18n.text(
                "Apri il PCB Editor del progetto. Scegli Posiziona > Aggiungi footprint, "
                "cerca KiCadGen:{name} e posiziona il componente. Se non compare, verifica "
                "di aver installato la libreria nel progetto giusto.").format(
                    name=self._component_name()),
        )
        add_page(
            "3. Esporta e registra il simbolo elettrico",
            "Esporta il simbolo .kicad_sym, poi in KiCad apri Gestisci librerie di simboli "
            "e aggiungi il file alla tabella del progetto o a quella globale.",
            (("Esporta simbolo KiCad…", self.btn_export_symbol.click),),
        )
        add_page(
            "4. Collega simbolo e footprint",
            self.i18n.text(
                "Nello Schematic Editor aggiungi il simbolo dalla libreria registrata. "
                "Apri le proprietà del simbolo e imposta Footprint su KiCadGen:{name}. "
                "Salva lo schema.").format(name=self._component_name()),
        )
        add_page(
            "5. Aggiorna e verifica il PCB",
            "Nel PCB Editor aggiorna la scheda dallo schema con F8, controlla le "
            "connessioni e apri la vista 3D con Alt+3 per verificare il modello. "
            "La scheda KiCadGen conserva lo storico delle esportazioni effettuate da questo tool.",
        )
        self.i18n.retranslate_widgets(wizard)
        wizard.exec()

    @staticmethod
    def _note_with_icons(note, *buttons) -> QHBoxLayout:
        """@brief A section row: its explanation on the left, its icon commands on the right.

        @param note QLabel with the section text (it takes the free width).
        @param buttons Icon-only commands of the section, in order.
        @return The row layout.
        """
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(note, 1)
        for button in buttons:
            row.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
        return row

    def _open_project_dir(self) -> None:
        if not self._refresh_output_actions():
            self._append_log(tx("Genera prima il footprint del componente corrente."))
            return
        project_dir = self._project_dir()
        webbrowser.open(project_dir.as_uri())

    def _current_kicad_artifacts(self):
        """@brief Return the complete footprint/STEP set or report the problem."""
        try:
            return discover_component_artifacts(
                self._project_dir(), self._component_name(), require_3d=True)
        except (KiCadLibraryError, OSError) as exc:
            QMessageBox.warning(
                self, self.i18n.text("File KiCad incompleti"),
                self._kicad_error_text(exc))
            return None

    def _kicad_error_text(self, error: Exception) -> str:
        """@brief Translate expected KiCad workflow failures for display."""
        if isinstance(error, KiCadLibraryError):
            return self.i18n.tr(
                f"kicad.error.{error.code}", **error.context)
        return self.i18n.tr("kicad.error.unexpected", error=error)

    def _kicad_outputs_went_missing(self) -> None:
        """@brief Say what is missing and realign the KiCad tab (audit 2026-09-25).

        @note Footprint or STEP deleted outside the program while the tab was
            open: the command used to return without a word, the tab stayed
            open and still said «ready». Now the existing message names the
            missing file, and the gate is recomputed.
        """
        self._current_kicad_artifacts()
        self._refresh_output_actions()

    def _step_still_on_disk(self) -> bool:
        """@brief Return whether the current component's STEP file exists."""
        name = self._component_name()
        return bool(name) and (
            self._project_dir() / f"{name}.3dshapes" / f"{name}.step").is_file()

    def _kicad_output_complete(self) -> bool:
        """@brief Return whether the current footprint and STEP both exist."""
        name = self._component_name()
        if not name:
            return False
        project = self._project_dir()
        return (
            project / f"{name}.pretty" / f"{name}.kicad_mod").is_file() and (
            project / f"{name}.3dshapes" / f"{name}.step").is_file()

    def _test_in_kicad(self) -> None:
        """@brief Open an isolated preview board without installing a library."""
        if not self._kicad_output_complete():
            self._kicad_outputs_went_missing()
            return
        artifacts = self._current_kicad_artifacts()
        if artifacts is None:
            return
        applications = detect_external_applications()
        if not applications.pcbnew:
            QMessageBox.warning(
                self, self.i18n.text("KiCad non disponibile"),
                self.i18n.text(
                    "PCB Editor non rilevato. Installa KiCad e riavvia l'applicazione."))
            return
        try:
            preview = create_preview_project(artifacts)
            launch_preview(applications.pcbnew, preview.board_path)
        except (KiCadLibraryError, OSError) as exc:
            QMessageBox.warning(
                self, self.i18n.text("Impossibile aprire KiCad"),
                self._kicad_error_text(exc))
            return
        message = self.i18n.tr(
            "kicad.preview_opened", path=preview.board_path)
        self._append_log(message)
        self.label_kicad_status.setText(message)

    def _install_project_library(self) -> None:
        """@brief Install the current artifacts as a portable project library."""
        if not self._kicad_output_complete():
            self._kicad_outputs_went_missing()
            return
        artifacts = self._current_kicad_artifacts()
        if artifacts is None:
            return
        destination = QFileDialog.getExistingDirectory(
            self, self.i18n.text("Scegli la cartella del progetto KiCad"),
            str(Path.home() / "Documents"),
        )
        if not destination:
            return
        root = Path(destination)
        if not (list(root.glob("*.kicad_pro")) or list(root.glob("*.pro"))):
            answer = QMessageBox.question(
                self, self.i18n.text("Cartella di progetto non riconosciuta"),
                self.i18n.text(
                    "La cartella non contiene un progetto KiCad. Vuoi installare qui comunque?"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._install_kicad_library(
            root, project_library=True, artifacts=artifacts)

    def _install_global_library(self) -> None:
        """@brief Install the current artifacts in the global KiCad table."""
        if not self._kicad_output_complete():
            self._kicad_outputs_went_missing()
            return
        artifacts = self._current_kicad_artifacts()
        if artifacts is None:
            return
        applications = detect_external_applications()
        if not applications.kicad_cli:
            QMessageBox.warning(
                self, self.i18n.text("KiCad non disponibile"),
                self.i18n.text(
                    "KiCad non è stato rilevato: la libreria globale non può essere registrata."))
            return
        destination = QFileDialog.getExistingDirectory(
            self, self.i18n.text("Scegli la cartella per le librerie globali"),
            str(Path.home() / "Documents" / "KiCad"),
        )
        if not destination:
            return
        try:
            table_path = global_fp_lib_table_path(applications.kicad_cli)
        except KiCadLibraryError as exc:
            QMessageBox.warning(
                self, self.i18n.text("Configurazione KiCad non disponibile"),
                self._kicad_error_text(exc))
            return
        self._install_kicad_library(
            Path(destination), project_library=False, table_path=table_path,
            artifacts=artifacts)

    def _install_kicad_library(
            self, destination: Path, *, project_library: bool,
            table_path: Path | None = None, artifacts=None) -> None:
        """@brief Install with an explicit overwrite decision and final report."""
        artifacts = artifacts or self._current_kicad_artifacts()
        if artifacts is None:
            return
        if not self._confirm_klc_errors():
            return
        targets = (
            destination / "KiCadGen.pretty" / f"{artifacts.name}.kicad_mod",
            destination / "KiCadGen.3dshapes" / f"{artifacts.name}.step",
            destination / "KiCadGen.3dshapes" / f"{artifacts.name}.wrl",
        )
        existing = [path for path in targets if path.exists()]
        overwrite = False
        if existing:
            answer = QMessageBox.question(
                self, self.i18n.text("Componente già presente"),
                self.i18n.tr(
                    "kicad.overwrite_component", files="\n".join(map(str, existing))),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            overwrite = True
        replace_entry = False
        while True:
            try:
                result = install_component_library(
                    artifacts, destination, library_name="KiCadGen",
                    project_library=project_library, table_path=table_path,
                    overwrite=overwrite, replace_library_entry=replace_entry,
                )
                break
            except KiCadLibraryError as exc:
                # La voce «KiCadGen» della tabella punta gia' a un'altra
                # cartella: ripuntarla toglierebbe a ogni progetto i componenti
                # installati la'. Nulla e' stato scritto; si chiede, nominando
                # la cartella vecchia e quella nuova (verifica del 25/09, F2).
                if (getattr(exc, "code", None) == "library_entry_conflict"
                        and not replace_entry):
                    answer = QMessageBox.question(
                        self, self.i18n.text("Libreria KiCadGen già registrata"),
                        self.i18n.tr("kicad.confirm_replace_library_entry",
                                     **dict(getattr(exc, "context", {}) or {})),
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No,
                    )
                    if answer != QMessageBox.StandardButton.Yes:
                        return
                    replace_entry = True
                    continue
                QMessageBox.warning(
                    self, self.i18n.text("Installazione KiCad non riuscita"),
                    self._kicad_error_text(exc))
                return
            except OSError as exc:
                QMessageBox.warning(
                    self, self.i18n.text("Installazione KiCad non riuscita"),
                    self._kicad_error_text(exc))
                return
        message = self.i18n.tr(
            "kicad.install_complete", identifier=result.identifier,
            footprint=result.footprint_path, table=result.table_path)
        kind = "Libreria di progetto" if project_library else "Libreria globale"
        if record_kicad_export(component=artifacts.name, kind=kind,
                               source=str(artifacts.component_dir),
                               destination=str(destination)):
            self._refresh_kicad_export_history()
        else:
            self._append_log(self.i18n.text(
                "Installazione riuscita, ma non è stato possibile aggiornare lo storico KiCad."))
        self._append_log(message)
        self.label_kicad_status.setText(message)
        QMessageBox.information(
            self, self.i18n.text("Installazione KiCad completata"), message)

    #: Nome del diario scritto nella cartella del componente. Sta li' e non in
    #: una cartella di sistema perche' appartiene al componente: chi apre quella
    #: cartella trova accanto ai file generati il racconto di come sono stati
    #: prodotti, senza doverlo ricopiare dall'interfaccia.
    LOG_FILENAME = "analisi-ai.log"

    def _log_file(self) -> Path:
        """@brief Return the per-component log file inside the project folder."""
        return self._project_dir() / self.LOG_FILENAME

    def _write_log_line(self, message: str) -> None:
        """Aggiunge una riga al diario del componente.

        Scrive subito invece di accumulare in memoria: se l'analisi si
        interrompe a meta', quello che e' successo fino a quel punto resta
        leggibile. Un errore di scrittura non ferma l'analisi -- il diario e'
        un aiuto, non un requisito.

        @note Finche' il componente non ha un nome non c'e' una cartella dove
            scrivere, e il diario resta solo a schermo. Prima il diario creava
            da solo la cartella di ripiego: bastava aprire l'applicazione per
            ritrovarsi in area di lavoro una cartella che nessuno aveva chiesto,
            con dentro il log dell'avvio. Le righe si accumulano e finiscono su
            disco appena un nome c'e'.
        """
        if not self._component_name():
            buffer = getattr(self, "_pending_log_lines", None)
            if buffer is None:
                buffer = self._pending_log_lines = []
            # Il diario di un avvio non vale una crescita senza fine.
            if len(buffer) < 500:
                buffer.append(str(message).rstrip())
            return
        try:
            from datetime import datetime
            handle = self._project_log_handle(self._log_file())
            pending = getattr(self, "_pending_log_lines", None)
            stamp = datetime.now().astimezone().isoformat(timespec="seconds")
            for line in (pending or ()):
                handle.write(f"[{stamp}] {line}\n")
            if pending:
                self._pending_log_lines = []
            handle.write(f"[{stamp}] {str(message).rstrip()}\n")
            handle.flush()
            self._release_log_handle_when_idle()
        except (OSError, AttributeError, ValueError) as exc:
            # Un file rimasto aperto su un errore non va riusato: la riga dopo
            # riapre da capo (anche la cartella, se e' sparita).
            self._close_log_handle()
            # Startup can emit messages before project widgets exist.
            if not getattr(self, "_log_write_failed", False):
                self._log_write_failed = True
                # Stessa frase (a capo compreso) della voce di catalogo.
                # NON nel pannello del Log AI: quello e' il log dell'analisi, e
                # una riga di servizio lo farebbe comparire vuoto di analisi
                # (test_ai_log_button_counts_the_elements_list_too).
                self._log_lines.append(
                    tx("Log file non disponibile: {exc}\n", exc=exc).rstrip("\n"))

    #: How long the project log stays open after the last line while a worker
    #: runs, in ms. Resource policy, not a data threshold: long enough to
    #: cover a burst of progress lines (FreeCAD writes several per second),
    #: short enough that the file does not stay locked once the burst is over.
    LOG_HANDLE_IDLE_MS = 2000

    def _project_log_handle(self, path: Path):
        """@brief The open append handle of the project log @p path, opened when needed.

        @param path Log file of the current project (`_log_file()`).
        @return A text file object in append mode.
        @throws OSError when the folder cannot be created or the file opened.
        @note G8 (HANDOFF 130): the log used to be opened and closed for every
            line on the GUI thread, including each FreeCAD progress line. The
            handle is now reused; another project (change or rename) gives
            another path and reopens, and a file whose folder disappeared is
            reopened -- folder included -- instead of writing into the void.
        """
        handle = getattr(self, "_log_handle", None)
        if handle is not None and (getattr(self, "_log_handle_path", None) != path
                                   or not path.exists()):
            self._close_log_handle()
            handle = None
        if handle is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(path, "a", encoding="utf-8")
            self._log_handle, self._log_handle_path = handle, path
        return handle

    def _release_log_handle_when_idle(self) -> None:
        """@brief Keep the log open only while a worker streams lines, then close it.

        @note On Windows an open file locks its folder: the project could not
            be renamed or deleted, from Explorer or from this window, while the
            log were open. So the handle outlives the line only while background
            work runs (the case with many lines per second), and is closed
            `LOG_HANDLE_IDLE_MS` after the last one; with nothing running it is
            closed at once, which is the previous behaviour.
        """
        try:
            busy = bool(self._background_work_running())
        except Exception:  # noqa: BLE001 - early in __init__ the worker slots do not exist yet
            busy = False
        if not busy:
            self._close_log_handle()
            return
        timer = getattr(self, "_log_idle_timer", None)
        if timer is None:
            timer = self._log_idle_timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(self.LOG_HANDLE_IDLE_MS)
            timer.timeout.connect(self._close_log_handle)
        timer.start()

    def _close_log_handle(self) -> None:
        """@brief Close the project log handle, if open; never raises.

        @note Called before the log or the project folder moves (rename), when
            the window closes and after an idle interval.
        """
        handle = getattr(self, "_log_handle", None)
        self._log_handle = None
        self._log_handle_path = None
        timer = getattr(self, "_log_idle_timer", None)
        if timer is not None:
            try:
                timer.stop()
            except RuntimeError:
                pass  # oggetto C++ gia' distrutto
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass  # il contenuto e' gia' stato scritto con flush a ogni riga

    def _start_log_session(self, title: str) -> None:
        """@brief Open a dated section so separate runs stay distinguishable."""
        from datetime import datetime
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._write_log_line("")
        self._write_log_line("=" * 78)
        self._write_log_line(stamp + "  " + str(title))
        self._write_log_line("=" * 78)

    def _move_log_to(self, destination: Path) -> bool:
        """Porta il diario nella nuova cartella quando il componente e' rinominato.

        Senza, il racconto di una stessa analisi resterebbe spezzato fra la
        cartella vecchia e quella nuova.
        """
        import shutil
        # Un file aperto non si sposta su Windows.
        self._close_log_handle()
        try:
            source = self._log_file()
            if not source.is_file():
                return True
            destination.mkdir(parents=True, exist_ok=True)
            target = destination / self.LOG_FILENAME
            if target.resolve() == source.resolve():
                return True
            if target.exists():
                target.write_text(target.read_text(encoding="utf-8")
                                  + source.read_text(encoding="utf-8"), encoding="utf-8")
                source.unlink()
            else:
                shutil.move(str(source), str(target))
            return True
        except OSError as exc:
            self._append_log(tx("Rinomina annullata: impossibile trasferire il log ({exc}).", exc=exc))
            return False

    def _save_config_safely(self) -> bool:
        """@brief Save config.json from a Qt slot without letting a failure escape.

        @return True when the preferences were written, False otherwise.
        @note A slot that raises ends the process through the crash report
            hook; a preference that cannot be written (locked, read-only or
            full disk) must only cost one log line, and the choice still holds
            for the current session.
        """
        # Only an explicit False is a failure: substitutes that return None
        # (older callers and test doubles) keep meaning "saved".
        if save_config(self._config) is not False:
            return True
        if not hasattr(self, "_log_lines"):
            # Called before the log exists (early in __init__): nothing to show yet.
            return False
        self._append_log(tx("Preferenze non salvate in {path}: la modifica vale solo per questa sessione.",
                            path=config_path()))
        return False

    def _append_log(self, msg: str, *, persist: bool = True) -> None:
        """@brief Add a message to the session log and, by default, the project log.

        @param msg Message to show.
        @param persist False to keep a safety refusal in the UI/session log
            without writing through a linked project path to an external target.
        """
        if persist:
            self._write_log_line(msg)
        if hasattr(self, "ai_progress_text") and not re.match(r"^#\d+", msg.strip()):
            self.ai_progress_text.moveCursor(QTextCursor.MoveOperation.End)
            self.ai_progress_text.insertPlainText(msg + "\n")
            self.ai_progress_text.ensureCursorVisible()
        # Copia in memoria del log della sessione (il tab «Genera» che lo
        # mostrava e' stato tolto il 2026-09-26): la legge `_log_plain_text`.
        self._log_lines.append(msg)

    def _log_plain_text(self) -> str:
        """@brief Return the session log written so far, one message per line.

        @return Every message passed to `_append_log`, newline-terminated.
        """
        return "".join(line + "\n" for line in self._log_lines)

    def _parse_populated_pins(self) -> set | None:
        text = self.field_populated_pins.text().strip()
        if not text:
            return None
        pins = set()
        for token in text.split(","):
            token = token.strip()
            if not token:
                continue
            if not token.isdigit():
                raise ValueError(tx("\"{token}\" in \"Pin popolati\" non e' un numero valido.", token=token))
            pins.add(int(token))
        return pins or None

    def _current_params(self) -> PinHeaderParams:
        return PinHeaderParams(
            name=self.field_name.text().strip(),
            description=self.field_description.text().strip(),
            tags=self.field_tags.text().strip(),
            datasheet=self._effective_datasheet(),
            rows=self.field_rows.value(),
            cols=self.field_cols.value(),
            pitch=self.field_pitch.value(),
            row_spacing=self.field_row_spacing.value(),
            pad_diameter=self.field_pad.value(),
            drill=self.field_drill.value(),
            pad_overrides=deepcopy(self.header_pad_overrides),
            populated_pins=self._parse_populated_pins(),
            pin_map=self.pin_map,
            body_length=self.field_body_length.value() if self.group_3d.isChecked() else None,
            body_width=self.field_body_width.value() if self.group_3d.isChecked() else None,
            body_offset_x=self.field_body_offset_x.value(),
            body_offset_y=self.field_body_offset_y.value(),
        )

    def _current_body_params(self) -> Body3DParams | None:
        """@brief I parametri del solido da costruire, o None se non ne va costruito uno.

        @return Body3DParams dai campi correnti; None quando il gruppo
            «Modello 3D» e' spento.
        @note None NON significa «dati mancanti»: significa «nessun modello 3D
            richiesto». Il gruppo spento e' una scelta -- dell'utente col clic,
            o del programma quando FreeCAD non c'e' -- e chi chiama deve
            trattare i due casi diversamente: un None con il gruppo ACCESO e'
            un guasto da segnalare, un None con il gruppo spento e' la
            generazione di solo footprint, che deve andare a termine. Questa
            distinzione e' il fulcro di `_start_automatic_generation` e di
            `_generate`.
        @note Per un modulo castellated l'ingombro non viene dai campi a mano
            ma dal PCB del modulo, che e' esso stesso il corpo.
        """
        if not self.group_3d.isChecked():
            return None
        if self.package_geometry is not None:
            package = self.package_geometry
            # A package STEP must reflect document dimensions. Do not silently
            # substitute the board workflow's default 10x10x5 mm values.
            if not all(value is not None for value in (
                    package.body_length_mm, package.body_width_mm, package.body_height_mm)):
                return None
            length = package.body_length_mm
            width = package.body_width_mm
            height = package.body_height_mm
            return Body3DParams(
                length_mm=length, width_mm=width, height_mm=height,
                offset_x_mm=-length / 2, offset_y_mm=-width / 2,
                pin_diameter_mm=max(0.3, min(self.field_drill.value() * 0.65, 0.8)),
                pin_length_mm=self.field_pin_length_3d.value(),
                color_hex=self.field_body_color.text().strip(),
                pin_color_hex=self.field_pin_color.text().strip(),
                # The package path prints the numbers beside its through-hole
                # leads too (VE12H-K relay, 2026-10-10: the choice was lost here).
                pin_numbers_3d=self._pin_numbers_3d(),
            )
        if self._is_castellated():
            # Il PCB del modulo e' centrato sull'origine come il footprint:
            # ingombro e offset si ricavano da li', non dai campi a mano.
            module = self._current_castellated_params()
            return Body3DParams(
                length_mm=module.body_length,
                width_mm=module.body_width,
                height_mm=self.field_body_height.value(),
                offset_x_mm=-module.body_length / 2,
                offset_y_mm=-module.body_width / 2,
                pin_diameter_mm=module.hole_diameter,
                pin_length_mm=module.pcb_thickness,
                color_hex=self.field_body_color.text().strip(),
                pin_color_hex=self.field_pin_color.text().strip(),
                **self._pins_3d_kwargs(),
            )
        return Body3DParams(
            length_mm=self.field_body_length.value(),
            width_mm=self.field_body_width.value(),
            height_mm=self.field_body_height.value(),
            offset_x_mm=self.field_body_offset_x.value(),
            offset_y_mm=self.field_body_offset_y.value(),
            pin_diameter_mm=self.field_drill.value(),
            pin_length_mm=self.field_pin_length_3d.value(),
            color_hex=self.field_body_color.text().strip(),
            pin_color_hex=self.field_pin_color.text().strip(),
            # A board shows its thickness on the Component page and the 3D
            # uses that one (point 132); a single component keeps the value
            # of its documents, None when they give none.
            pcb_thickness_mm=(self.field_cast_pcb.value()
                              if self._photo_inventory_family() is True
                              else self._extracted_pcb_thickness),
            components=self._components_from_table(),
            populated_board=self._selected_family() == "aggregate_tht",
            pin_numbers_3d=(self._selected_family() == "single_tht"
                            and self._pin_numbers_3d()),
            **self._pins_3d_kwargs(),
        )

    def _pins_3d_kwargs(self) -> dict:
        """@brief The pins choice for Body3DParams, for boards only.

        @return {"show_pins_3d": choice} for a board with components; {} for a
            single component, whose leads are part of the component and always
            drawn (Body3DParams default).
        """
        if self._photo_inventory_family() is not True:
            return {}
        return {"show_pins_3d": self._show_pins_3d()}

    def _automatic_generation_blocked(self, message):
        if self._selected_family() == "single_tht":
            text = message + "\n\n" + self.i18n.text(
                "Correggi i valori nella sezione Piazzole del componente THT, poi usa "
                "«Rigenera footprint»."
            )
        else:
            text = self.i18n.tr("generation.blocked", reason=message)
        if self._project_has_footprint():
            text += "\n\n" + self.i18n.text(
                "I file già presenti non sono stati modificati.")
        self._on_ai_progress(tx("Generazione automatica non avviata: ") + message)
        QMessageBox.warning(
            self, self.i18n.text('Generazione automatica non avviata'), text)

    @staticmethod
    def _validate_output_name(name):
        if (not name or name in ('.', '..') or name != name.rstrip(' .')
                or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)):
            raise ValueError(tx("Il nome del componente non è un nome di file valido."))

    def _validate_automatic_input(self, params, *, require_body_dimensions=True):
        """@brief Refuse an extraction the automatic generation cannot use safely.

        @param params Extracted parameters.
        @param require_body_dimensions Whether the 3D body quotes are mandatory.
        @throws ValueError With the reason shown to the user; nothing is written.
        @note The name validated is the one the files will carry: the name the
            analysis proposes is only shown («File > Rinomina progetto»), so an
            unsafe proposal cannot block a generation that never uses it.
        """
        self._validate_output_name(self._component_name())
        conflict = getattr(params, "pin_map_conflict", None)
        if conflict:
            # Two readings of the contacts that disagree (numbering of a grid,
            # or positions of explicit pads): the user checks before any file
            # is written (user, 2026-10-10: «ti ferma e ti fa controllare»).
            # Checked FIRST: the explicit-pad branch below returns early, and
            # the VE12H-K run of 16:47 generated despite the disagreement.
            raise ValueError(conflict)
        if self._selected_family() == "single_smd" and not params.package_geometry:
            raise ValueError(tx(
                "Componente SMD: manca la geometria esplicita delle piazzole. "
                "Completa il datasheet e ripeti l'analisi."))
        if params.package_geometry:
            # Explicit package pads use the same validated geometry for both
            # the footprint and the direct FreeCAD path. Never fall back to
            # the board's pin matrix if the package payload is malformed.
            try:
                geometry_data = dict(params.package_geometry)
                geometry_data.setdefault("name", params.name or self._component_name())
                package_from_mapping(geometry_data)
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    tx("Geometria a pad espliciti non valida: nessun file generato.")) from exc
            return
        if require_body_dimensions:
            required_dimensions = ('body_length_mm', 'body_width_mm')
            if not params.is_castellated():
                required_dimensions += ('body_height_mm',)
            for field in required_dimensions:
                value = getattr(params, field)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                    raise ValueError(tx("Manca una dimensione valida del corpo 3D: {field}.", field=field))
            dimensions = (('body_length_mm', self.field_cast_body_length if params.is_castellated() else self.field_body_length),
                          ('body_width_mm', self.field_cast_body_width if params.is_castellated() else self.field_body_width))
            if not params.is_castellated():
                dimensions += (('body_height_mm', self.field_body_height),)
            for field, widget in dimensions:
                self._require_in_range(field, getattr(params, field), widget)
        if params.is_castellated():
            if not params.castellated_sides:
                raise ValueError(tx("Non sono stati estratti i contatti del modulo castellated."))
            for side in params.castellated_sides:
                if not isinstance(side, dict) or side.get('edge') not in CASTELLATED_EDGES:
                    raise ValueError(tx("Lato del modulo non valido."))
                for field in ('count', 'pitch_mm'):
                    value = side.get(field)
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                        raise ValueError(tx("Contatti del modulo incompleti: {field}.", field=field))
                if side['count'] != int(side['count']):
                    raise ValueError(tx("Numero di contatti non intero: {value}.", value=side['count']))
                # Quanti contatti stanno sul lato lo decide il generatore dalle
                # quote della scheda; qui si rifiuta solo cio' che non puo'
                # esistere: una fila piu' lunga della scheda piu' grande.
                self._require_extent(side['count'], side['pitch_mm'],
                                     tx("lato {edge}", edge=EDGE_LABELS.get(side['edge'], side['edge'])))
        else:
            # Griglia di pin passanti: passo solo con piu' colonne, interasse
            # solo con piu' file (stesso contratto dell'estrattore dal 26/09).
            required = ['rows', 'cols', 'pad_diameter_mm', 'drill_mm']
            if isinstance(params.cols, (int, float)) and params.cols > 1:
                required.append('pitch_mm')
            if isinstance(params.rows, (int, float)) and params.rows > 1:
                required.append('row_spacing_mm')
            for field in required:
                value = getattr(params, field)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                    raise ValueError(tx("Parametro footprint mancante o non valido: {field}.", field=field))
            for field, widget in (('rows', self.field_rows), ('cols', self.field_cols),
                                  ('pitch_mm', self.field_pitch), ('row_spacing_mm', self.field_row_spacing),
                                  ('pad_diameter_mm', self.field_pad), ('drill_mm', self.field_drill)):
                value = getattr(params, field)
                if value is None:
                    continue
                if field in ('rows', 'cols') and value != int(value):
                    raise ValueError(tx("Numero di file o colonne non intero: {field} = {value}.",
                                        field=field, value=value))
                self._require_in_range(field, value, widget)
            if params.cols > 1:
                self._require_extent(params.cols, params.pitch_mm, 'cols')
            if params.rows > 1:
                self._require_extent(params.rows, params.row_spacing_mm, 'rows')
            self._require_leads_inside_a_bottom_exit_body(params)

    @staticmethod
    def _require_leads_inside_a_bottom_exit_body(params) -> None:
        """@brief Leads that leave the underside of the body must all lie inside its outline.

        @param params Extracted parameters of a through-hole pin grid.
        @throws ValueError When `lead_exit` is "bottom" and the pins that exist
            span more than the body on an axis, or (with the body offset of
            that axis known) a pin centre falls outside the body.
        @note Structural, not a threshold: a lead coming straight out of the
            underside is inside the moulding by construction, so a grid wider
            than the body is a reading error (the TEL8 runs of 2026-10-10 put
            15.24 mm, the distance between pins 1 and 7, in row_spacing_mm of
            a 13.7 mm wide body). With "side" (DIP) or unknown exit the rows
            may legitimately lie outside the body: only a warning then
            (`_plausibility_warnings`). The user asked that the exit be read
            from the documents, never inferred from a size or a name.
        """
        if getattr(params, "lead_exit", None) != "bottom":
            return
        from ..services.datasheet_extractor import _pin_field_positions
        positions = _pin_field_positions(params)
        if not positions:
            return
        for axis, index, extent, offset in (
                ("X", 0, params.body_length_mm, params.body_offset_x_mm),
                ("Y", 1, params.body_width_mm, params.body_offset_y_mm)):
            if not isinstance(extent, (int, float)) or isinstance(extent, bool) or extent <= 0:
                continue
            low = min(p[index] for p in positions)
            high = max(p[index] for p in positions)
            outside = high - low > extent + 1e-6
            if not outside and isinstance(offset, (int, float)) and not isinstance(offset, bool):
                outside = low < offset - 1e-6 or high > offset + extent + 1e-6
            if outside:
                raise ValueError(tx(
                    "I terminali escono dal fondo del corpo (lead_exit = bottom) ma lungo "
                    "{axis} i pin occupano {span:.2f} mm contro un corpo di {extent:.2f} mm: "
                    "con i reofori dal fondo ogni pin sta dentro il corpo, quindi una delle "
                    "due quote e' letta male (spesso l'interasse fra le file scambiato con "
                    "un'altra distanza). Correggi passo, interasse o quote del corpo e "
                    "rigenera.", axis=axis, span=high - low, extent=extent))

    @staticmethod
    def _require_in_range(field, value, widget) -> None:
        """@brief Refuse an extracted value outside the physical range of its parameter.

        @param field Name of the extracted field, for the message.
        @param value Extracted value.
        @param widget The BoundedValue that will store it.
        @throws ValueError Naming the field, the value and the accepted range.
        """
        if not widget.minimum() <= value <= widget.maximum():
            raise ValueError(tx("Valore impossibile: {field} = {value} (ammesso da {minimum} a "
                                "{maximum}). Controlla i documenti e ripeti l’analisi.",
                                field=field, value=value, minimum=widget.minimum(),
                                maximum=widget.maximum()))

    @staticmethod
    def _require_extent(count, pitch, what) -> None:
        """@brief Refuse a row of contacts longer than the largest board the program handles.

        @param count Number of contacts in the row.
        @param pitch Distance between neighbouring contacts, mm.
        @param what Name of the row, for the message.
        @throws ValueError When (count - 1) * pitch exceeds MAX_LENGTH_MM.
        """
        extent = (count - 1) * pitch
        if extent > MAX_LENGTH_MM:
            raise ValueError(tx("Valore impossibile: {what} occupa {extent} mm, più della scheda "
                                "più grande gestita ({maximum} mm).", what=what,
                                extent=f"{extent:.2f}", maximum=f"{MAX_LENGTH_MM:g}"))

    def _start_automatic_generation(self, extracted):
        """@brief Avvia la generazione automatica dopo un'estrazione, se i dati la permettono.

        @param extracted Parametri estratti, gia' applicati ai campi.
        @note Il modello 3D e' opzionale, il footprint no. Con il gruppo
            «Modello 3D» spento `_current_body_params()` restituisce None per
            scelta, non per un dato mancante: trattarlo come errore fatale
            bloccava TUTTA la generazione automatica -- avviso e nessun
            `.kicad_mod` -- a chi aveva spento il 3D di proposito. Quindi il
            corpo si pretende solo quando il gruppo e' acceso, e i controlli
            sulle quote si fanno solo se c'e' un corpo da controllare.
        """
        try:
            self._validate_automatic_input(extracted, require_body_dimensions=False)
            if extracted.package_geometry:
                self._generate(automatic=True, body_override=self._current_body_params())
                return
            body_dimensions = (extracted.body_length_mm, extracted.body_width_mm)
            if not extracted.is_castellated():
                body_dimensions += (extracted.body_height_mm,)
            body_dimensions_valid = all(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value > 0 for value in body_dimensions
            )
            body = self._current_body_params() if body_dimensions_valid else None
            if body is None and body_dimensions_valid and self.group_3d.isChecked():
                # The 3D was asked for and the documents gave every quote, yet
                # no body came out: a fault to report, not a deferral (the
                # deferral is only for missing quotes, which `_generate` says).
                raise ValueError(tx("I parametri del modello 3D non sono disponibili."))
            if body is not None:
                # QSpinBox limits must not silently replace extracted dimensions.
                dimensions = ((body.length_mm, extracted.body_length_mm),
                              (body.width_mm, extracted.body_width_mm))
                if not extracted.is_castellated():
                    dimensions += ((body.height_mm, extracted.body_height_mm),)
                if any(not math.isclose(actual, expected, abs_tol=.011)
                       for actual, expected in dimensions):
                    raise ValueError(tx("Le dimensioni estratte superano i limiti ammessi: controlla i "
                                        "documenti e ripeti l’analisi."))
        except (ValueError, TypeError) as exc:
            self._automatic_generation_blocked(str(exc))
            return
        self._generate(automatic=True, body_override=body)

    def _confirm_output_overwrite(self, params, project_dir, body):
        targets = [project_dir / f'{params.name}.pretty' / f'{params.name}.kicad_mod']
        if body is not None:
            targets.extend(project_dir / f'{params.name}.3dshapes' / f'{params.name}{suffix}'
                           for suffix in ('.step', '.wrl', '.preview.json'))
        existing = [str(path) for path in targets if path.exists()]
        if not existing:
            return True
        answer = QMessageBox.question(
            self, self.i18n.text('Conferma sovrascrittura'),
            self.i18n.tr('generation.overwrite_files', files='\n'.join(existing)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes

    def _correct_invalid_through_holes(self, params) -> bool:
        """Edit measured drill sizes and the common copper diameter; never rerun AI."""
        from PyQt6.QtWidgets import QDialogButtonBox
        from ..generators.castellated_module import CONTACT_THROUGH_HOLE, CONTACT_HALF_AND_THROUGH
        if not isinstance(params, CastellatedModuleParams):
            return False
        affected = []
        for index, side in enumerate(params.sides):
            if (side.contact_style not in (CONTACT_THROUGH_HOLE, CONTACT_HALF_AND_THROUGH)
                    or side.edge not in CASTELLATED_EDGES):
                continue
            geometry = params.side_geometry(side)
            if geometry.drill <= 0 or geometry.ring <= geometry.drill:
                affected.append((index, side, geometry))
        if not affected:
            return False
        self._hide_analysis_overlay()
        dialog = QDialog(self)
        dialog.setWindowTitle(self.i18n.text("Correggi fori e piazzole"))
        form = QFormLayout(dialog)
        intro = QLabel(self.i18n.text(
            "Inserisci le misure verificate. La piazzola deve essere più larga del foro. "
            "Il diametro piazzola è comune ai lati. Verrà ripresa la generazione senza una nuova analisi AI."))
        intro.setWordWrap(True)
        form.addRow(intro)
        pad = QDoubleSpinBox(dialog)
        pad.setObjectName("recoveryThroughPad")
        pad.setRange(0, MAX_LENGTH_MM)
        pad.setDecimals(2)
        pad.setSuffix(" mm")
        pad.setValue(float(params.thru_pad_diameter if params.thru_pad_diameter is not None
                           else affected[0][2].ring))
        form.addRow(self.i18n.text("Diametro piazzola comune"), pad)
        drills = []
        for index, side, geometry in affected:
            field = QDoubleSpinBox(dialog)
            field.setObjectName(f"recoveryDrill_{side.edge}")
            field.setRange(0, MAX_LENGTH_MM)
            field.setDecimals(2)
            field.setSuffix(" mm")
            field.setValue(float(geometry.drill))
            form.addRow(self.i18n.text("Diametro foro") + " · " + str(EDGE_LABELS.get(side.edge, side.edge)), field)
            drills.append((index, field))
        error = QLabel()
        error.setObjectName("recoveryValidation")
        error.setWordWrap(True)
        form.addRow(error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(self.i18n.text("Applica e riprendi"))
        def accept():
            candidate = deepcopy(params)
            candidate.thru_pad_diameter = pad.value()
            for index, field in drills:
                candidate.sides[index].thru_hole_drill = field.value()
            try:
                candidate.validate()
            except ValueError as exc:
                error.setText(str(exc))
                return
            dialog.accept()
        buttons.accepted.connect(accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return False
            self.field_cast_thru_pad.setValue(pad.value())
            for index, field in drills:
                self._sides[index]["through_hole_drill_mm"] = field.value()
                self._sides[index]["through_hole_drill_declared"] = True
            self._on_sides_changed()
            self._append_log(tx("Fori e piazzole corretti dall'utente: ripresa della generazione senza analisi AI."))
            return True
        finally:
            dialog.deleteLater()

    def _generate(self, automatic=False, footprint_only=False,
                  body_override=_BODY_OVERRIDE_UNSET) -> bool:
        """@brief Start the generation worker for the current parameters.

        @param automatic True when called by the automatic pipeline.
        @param footprint_only True rewrites the .kicad_mod only (pad edits, metadata).
        @param body_override Body size decided by the user, or the sentinel.
        @return True when the worker was started; False when nothing ran
            (work already running, automatic pipeline active, no footprint
            yet for a footprint-only rewrite, invalid parameters, a pending
            footprint draft the user chose to keep).
        @note Footprint draft (user, 2026-10-14): a generation the user
            starts with unapplied footprint edits asks first to apply or
            discard them (`_settle_footprint_draft_before_start`); the
            automatic pipeline asked at its own start, and the rewrite of
            an applied edit (`_flush_footprint_rewrite`) does not ask. In
            every case the files get the APPLIED footprint
            (`_applied_footprint_in_memory`), never a draft.
        """
        if self._background_work_running() or self._automatic_generation_active:
            return False
        if (not automatic and not self._fp_rewrite_flushing
                and not self._settle_footprint_draft_before_start()):
            return False
        with self._applied_footprint_in_memory():
            return self._generate_applied(automatic=automatic, footprint_only=footprint_only,
                                          body_override=body_override)

    def _generate_applied(self, automatic=False, footprint_only=False,
                          body_override=_BODY_OVERRIDE_UNSET) -> bool:
        """@brief Body of `_generate`, run with the applied footprint in the fields.

        @param automatic See `_generate`.
        @param footprint_only See `_generate`.
        @param body_override See `_generate`.
        @return See `_generate`.
        """
        if self._background_work_running() or self._automatic_generation_active:
            return False
        if automatic:
            self._automatic_3d_deferred_reason = ""
        if footprint_only and not (self._acquisition_completed or self._project_has_footprint()):
            self._set_mounting_error(self.i18n.text("Analizza prima i documenti del componente."))
            return False

        try:
            params = (
                self.package_geometry if self.package_geometry is not None else
                self._current_castellated_params() if self._is_castellated()
                else self._current_params()
            )
            if self._selected_family() == "single_smd" and not isinstance(params, PackageGeometry):
                raise ValueError(tx(
                    "Componente SMD senza geometria esplicita valida: genera i pad dal datasheet prima di esportare."))
            if isinstance(params, CastellatedModuleParams) and self._missing_contact_style_message():
                raise ValueError(self._missing_contact_style_message())
            params.validate()
            host_params = self._host_footprint_params(params)
            self._validate_output_name(params.name)
            body = (None if footprint_only else
                    body_override if body_override is not _BODY_OVERRIDE_UNSET else
                    self._current_body_params())
            if body is not None:
                body.validate()
            # Il corpo si pretende solo se il 3D e' stato chiesto. Un gruppo
            # «Modello 3D» spento e' una scelta, non un dato mancante: la
            # generazione automatica deve comunque scrivere il footprint.
            # Vedi `_current_body_params` e `_start_automatic_generation`.
            required_body_fields = ("body_length_mm", "body_width_mm")
            if not isinstance(params, CastellatedModuleParams):
                required_body_fields += ("body_height_mm",)
            extracted_body_complete = all(
                isinstance(getattr(params, field, None), (int, float))
                and not isinstance(getattr(params, field, None), bool)
                and math.isfinite(getattr(params, field))
                and getattr(params, field) > 0
                for field in required_body_fields
            )
            if (automatic and body is None and self.group_3d.isChecked()
                    and not isinstance(params, PackageGeometry) and extracted_body_complete):
                raise ValueError(tx('Mancano i parametri 3D.'))
        except ValueError as exc:
            self._append_log(tx("ERRORE: {exc}", exc=exc))
            self._set_mounting_error(str(exc))
            if not footprint_only and self._correct_invalid_through_holes(locals().get("params")):
                self._generate(automatic=automatic)
                return False
            if automatic:
                self._automatic_generation_blocked(str(exc))
            return False

        project_dir = self._project_dir()
        if automatic and not self._confirm_output_overwrite(params, project_dir, body):
            self._on_ai_progress(tx("Generazione annullata: file esistenti conservati."))
            return False

        if isinstance(params, PackageGeometry):
            self._append_log(
                tx("Generazione {name}: package {family}, {n_pins} pad espliciti",
                   name=params.name,
                   family=params.family,
                   n_pins=len(params.pins))
            )
        elif isinstance(params, CastellatedModuleParams):
            self._append_log(
                tx("Generazione {name}: modulo castellated {body_length}x{body_width}mm, ",
                   name=params.name,
                   body_length=params.body_length,
                   body_width=params.body_width)
                + ", ".join(
                    f"{EDGE_LABELS.get(s.edge, s.edge)} {s.count}x{s.pitch}mm"
                    for s in params.sides
                )
            )
        else:
            self._append_log(
                tx("Generazione {name}: {rows}x{cols}, pitch={pitch}mm, "
                   "row_spacing={effective_row_spacing}mm, pad={pad_diameter}mm, "
                   "drill={drill}mm",
                   name=params.name,
                   rows=params.rows,
                   cols=params.cols,
                   pitch=params.pitch,
                   effective_row_spacing=params.effective_row_spacing(),
                   pad_diameter=params.pad_diameter,
                   drill=params.drill)
            )

        if body is None and (self.fillet_annotations or self.face_text_annotations):
            self._append_log(
                tx("ATTENZIONE: annotazioni 3D presenti ma il modello 3D non viene generato "
                   "(quote del corpo mancanti o 3D disattivato): verranno ignorate.")
            )
        if body is None and self.group_3d.isChecked() and not footprint_only:
            required_labels = [("body_length_mm", "lunghezza"), ("body_width_mm", "larghezza")]
            if not getattr(params, "is_castellated", lambda: False)():
                required_labels.append(("body_height_mm", "altezza"))
            # `params` is the footprint object (PinHeaderParams has no body_*
            # attributes): the quotes come from the extraction, when there is
            # one, otherwise from the body fields of the GUI. Reading them off
            # `params` listed all three as missing even when only the height
            # was (TEL8 run of 2026-10-10, 11:56).
            extracted = getattr(self, "_last_extracted", None)
            fields = {"body_length_mm": self.field_body_length,
                      "body_width_mm": self.field_body_width,
                      "body_height_mm": self.field_body_height}
            missing = []
            for field, label in required_labels:
                if extracted is not None:
                    value = getattr(extracted, field, None)
                else:
                    value = fields[field].value()
                if (isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(value) or value <= 0):
                    missing.append(label)
            if missing:
                if automatic:
                    self._automatic_3d_deferred_reason = ", ".join(missing)
                self._append_log(tx(
                    "Modello 3D rinviato: mancano le quote documentate del corpo ({fields}). "
                    "Il footprint viene comunque generato. Aggiungi un disegno quotato e ripeti "
                    "l'analisi per completare il 3D.", fields=", ".join(missing)))

        self._last_params = params
        self._worker = GenerateWorker(
            params, project_dir, body,
            fillets=list(self.fillet_annotations), face_texts=self._face_texts_for_3d(),
            single_component=self._is_single_component_project(),
        )
        self._worker.footprint_params = host_params
        self._worker.text_positions = deepcopy(self.footprint_text_positions)
        self._worker.drawings = deepcopy(self.footprint_drawings)
        # PackageGeometry carries no datasheet: the worker passes it to
        # build_package itself (the other generators read it from params).
        self._worker.datasheet = self._effective_datasheet()
        self._worker.description = self.field_description.text().strip()
        self._worker._model_revision = self._model_revision
        self._worker.photo_scale_px_per_mm = self._photo_scale_px_per_mm()
        self._worker.done.connect(self._on_generated)
        self._worker.error.connect(self._on_error)
        self._worker.cad3d_done.connect(self._on_cad3d_generated)
        self._worker.warning.connect(self._on_cad3d_warning)
        self._worker.cad3d_error.connect(self._on_cad3d_error)
        self._worker.progress.connect(self._on_ai_progress)
        self._worker.phase.connect(self._on_phase)
        self._worker.cancellable.connect(self._on_cad_cancellable)
        self._worker.finished.connect(self._on_generation_finished)
        if not automatic and body is not None:
            # Richiesta dell'utente: «la possibilità di stoppare deve essere
            # presente in ogni step». La generazione manuale con modello 3D
            # passa dalle stesse due fasi lunghe della corsa automatica --
            # abbinamento dei modelli KiCad e costruzione del solido in
            # FreeCAD, minuti su una scheda popolata -- ma non apriva la
            # modale, quindi restava l'unica strada senza «Interrompi». Il
            # criterio e' la presenza del corpo 3D, cioe' del lavoro lungo:
            # il solo footprint 2D si scrive in un istante e una modale li'
            # sarebbe solo un lampo. Schede e menu NON si spengono come nella
            # corsa automatica: qui l'utente ha avviato lui il lavoro e il
            # resto della finestra non e' in mezzo alla catena.
            self._manual_cad_overlay = True
            self._clear_cost_for_generation()
            self.analysis_overlay.start('Generazione footprint e modello 3D',
                                        'Preparazione della generazione…', abortable=True)
            self._show_generation_steps(params, body, self._is_single_component_project())
        if automatic:
            self._automatic_generation_active = True
            self._automatic_footprint_done = self._automatic_3d_done = False
            # Un modello 3D che non e' stato chiesto non puo' mancare: senza
            # questo, una corsa di solo footprint finiva con «Generazione
            # incompleta -- modello 3D non completato», cioe' un guasto
            # annunciato per un risultato corretto e completo.
            self._automatic_3d_expected = body is not None
            self._automatic_generation_errors = []
            self._analysis_tabs_enabled = self.tabs.isEnabled()
            self.tabs.setEnabled(False)
            self._generation_menu_enabled = self.menuBar().isEnabled()
            self.menuBar().setEnabled(False)
            self.btn_extract_ai.setEnabled(False)
            self._clear_cost_for_generation()
            self.analysis_overlay.start('Generazione footprint e modello 3D',
                                        'Preparazione della generazione…', abortable=True)
            self._show_generation_steps(params, body, self._is_single_component_project())
        try:
            self._worker.start()
        except Exception as exc:
            self._on_error(str(exc))
            self._on_generation_finished()
            return False
        # Every generation writes the .kicad_mod from the data copied into
        # the worker above: a pending footprint rewrite is now covered,
        # whoever started it (see `_schedule_footprint_rewrite`).
        self._clear_footprint_rewrite()
        return True

    def _show_generation_steps(self, params, body, single_component=False) -> None:
        """@brief Put the three generation steps on the modal, with the missing ones explained.

        @param params Footprint parameters about to be generated.
        @param body 3D body, or None when no 3D model will be built.
        @note Steps 2 and 3 need FreeCAD and a 3D body; when they will not run
            the modal says why instead of letting them silently not happen.
        """
        unavailable = {}
        if single_component or isinstance(params, PackageGeometry):
            steps = ("Footprint KiCadGen", "Corpo e terminali FreeCAD")
            step_of = {"footprint": 0, "freecad": 1}
            if body is None:
                unavailable = {1: "modello 3D non richiesto"}
        else:
            steps = GENERATION_STEPS
            step_of = dict(GENERATION_STEP_OF)
        if body is None:
            required_body_fields = ("body_length_mm", "body_width_mm")
            if not getattr(params, "is_castellated", lambda: False)():
                required_body_fields += ("body_height_mm",)
            missing_body_dimensions = any(
                getattr(params, field, None) is None
                for field in required_body_fields
            )
            reason = ("FreeCAD non installato" if self._freecad_available is False
                      else "quote 3D mancanti: completa il datasheet"
                      if self.group_3d.isChecked() and missing_body_dimensions
                      else "modello 3D non richiesto")
            if single_component or isinstance(params, PackageGeometry):
                unavailable = {1: reason}
            if not single_component and not isinstance(params, PackageGeometry):
                unavailable = {1: reason, 2: reason, 3: reason}
        self.analysis_overlay.set_steps(steps, unavailable)
        self._generation_step_of = step_of

    def _on_generation_finished(self):
        """@brief Riporta la finestra in quiete alla fine della generazione.

        @note E' la stessa via per l'esito buono, per il guasto e per
            l'interruzione chiesta dall'utente: il worker emette `finished` in
            tutti e tre i casi. L'interruzione si riconosce da
            `_abort_pending`, che viene azzerato qui perche' qui finisce
            davvero il lavoro che si stava fermando.
        @note A footprint edit saved while this worker ran is rewritten now
            (`_retry_footprint_rewrite`).
        """
        self._retry_footprint_rewrite()
        QTimer.singleShot(0, self._refresh_mounting_actions)
        aborted = self._abort_pending
        self._abort_pending = False
        if self._manual_cad_overlay:
            # La modale della generazione manuale la chiude chi l'ha aperta:
            # la corsa automatica ha la sua chiusura piu' sotto, insieme a
            # schede, menu e consuntivo, che qui non sono stati toccati.
            self._manual_cad_overlay = False
            self._hide_analysis_overlay()
        if not self._automatic_generation_active:
            return
        self._automatic_generation_active = False
        expected_3d = getattr(self, "_automatic_3d_expected", True)
        complete = self._automatic_footprint_done and (self._automatic_3d_done or not expected_3d)
        succeeded = not aborted and not self._automatic_generation_errors and complete
        if not succeeded:
            self._hide_analysis_overlay()
        self.menuBar().setEnabled(self._generation_menu_enabled)
        self._refresh_extract_enabled()
        if self._extraction_busy:
            # L'estrazione e' finita con la generazione automatica gia'
            # attiva (_on_extract_finished esce prima): il pulsante torna a
            # riposo solo adesso.
            self._end_extraction_busy()
        if self._automatic_3d_done:
            self.tabs.setCurrentIndex(TAB_MODEL_3D)
        elif self._automatic_footprint_done:
            self.tabs.setCurrentIndex(TAB_PREVIEW)
        if aborted:
            # Nessun avviso di «generazione incompleta»: l'incompletezza e'
            # esattamente cio' che l'utente ha chiesto, e restituirgliela come
            # un guasto sarebbe rimproverargli la sua decisione. Il consuntivo
            # va comunque scritto, perche' dice che cosa e' rimasto sul disco.
            self._on_ai_progress(
                tx("Generazione interrotta dall’utente. ")
                + (tx("Il footprint scritto prima dell’interruzione resta sul disco; ")
                   if self._automatic_footprint_done else tx("Nessun footprint scritto; "))
                + tx("il modello 3D non è stato completato."))
        elif self._automatic_generation_errors or not complete:
            details = ('\n'.join(self._automatic_generation_errors)
                       or self.i18n.text('La generazione non ha prodotto entrambi i risultati.'))
            prefix = (self.i18n.text('Footprint generato, modello 3D non completato.') + '\n'
                      if self._automatic_footprint_done and expected_3d else '')
            QMessageBox.warning(
                self, self.i18n.text('Generazione incompleta'), prefix + details)
        elif expected_3d:
            self._on_ai_progress(tx("Footprint e modello 3D generati. Verifica le anteprime prima "
                                    "dell’utilizzo."))
            self._hold_completed_overlay("Completato: footprint e modello 3D generati.")
        else:
            if getattr(self, "_automatic_3d_deferred_reason", ""):
                message = tx("Footprint generato; modello 3D rinviato: mancano le quote del corpo ({fields}).",
                             fields=self._automatic_3d_deferred_reason)
                self._on_ai_progress(message)
                self._hold_completed_overlay("Footprint generato; modello 3D da completare.")
            else:
                # Consuntivo fedele: il modello 3D non e' stato chiesto.
                self._on_ai_progress(tx("Footprint generato; modello 3D non richiesto. Verifica l’anteprima "
                                        "prima dell’utilizzo."))
                self._hold_completed_overlay("Completato: footprint generato.")

    def _on_generated(self, path: str) -> None:
        """@brief A footprint was written: keep the symbol rows in step and save the symbol.

        @param path Path of the footprint written.
        """
        if self._automatic_generation_active:
            self._automatic_footprint_done = True
        # The rows must follow the pads just written, or the quiet save
        # refuses a stale baseline after every first generation (review C, 5).
        self._refresh_symbol_tab()
        self._save_applied_electrical_symbol(quiet=True)
        # Every generation writes the APPLIED footprint: the file is in step
        # with the project again (the autosave records it).
        self._fp_file_stale = False
        self._refresh_output_actions()
        self._append_log(f"OK -> {path}")
        if not self._cad_result_is_current():
            return
        self._draw_preview()
        self._draw_preview_3d()

    def _on_error(self, message: str) -> None:
        self._append_log(tx("ERRORE: {message}", message=message))
        if self._automatic_generation_active:
            self._automatic_generation_errors.append(message)

    def _on_cad3d_generated(self, path: str) -> None:
        """@brief Show a finished 3D model, or say why a stale one is not shown.

        @param path STEP file written by the worker.
        """
        if not self._cad_result_is_current():
            self._log_stale_cad_result(path)
            return
        if self._automatic_generation_active:
            self._automatic_3d_done = True
        self._append_log(f"OK (3D) -> {path}")
        self._set_3d_status(
            self.i18n.tr("status.step_updated", path=path), STATUS_INFO)
        self._refresh_output_actions()
        self._show_generated_mesh(Path(path))
        self._set_accept_models_ready(True)

    def _on_cad3d_error(self, message: str) -> None:
        """@brief Registra il mancato completamento del modello 3D.

        @param message Testo dell'esito, come arriva dal worker.
        @note L'interruzione chiesta dall'utente passa da qui -- e' la via
            d'uscita che esisteva gia' -- ma non si chiama «ERRORE»: nessun
            guasto e' avvenuto, e non entra nell'elenco dei guasti da
            riportare a fine corsa.
        """
        self._realign_after_3d_failure()
        if not self._cad_result_is_current():
            return
        if CAD_ABORT_MESSAGE in message:
            self._append_log(tx("INTERROTTO 3D: {message}", message=message))
            return
        self._append_log(tx("ERRORE 3D: {message}", message=message))
        if self._automatic_generation_active:
            self._automatic_generation_errors.append(message)

    def _log_stale_cad_result(self, path: str) -> None:
        """Say that a finished 3D model was discarded because the data changed.

        The worker still wrote its STEP (and WRL/preview) to disk: without this
        line the user would find files on disk that no longer match the data
        in the window and nothing explaining why the preview did not update.
        """
        self._append_log(tx("Modello 3D scartato: i dati sono cambiati durante la generazione. "
                            "I file su disco ({path}) non corrispondono ai dati attuali: "
                            "rigenera il modello 3D.", path=path))

    def _realign_after_3d_failure(self) -> None:
        """@brief After a failed or cancelled 3D run, show what is really on disk.

        @note A run that got as far as starting FreeCAD deletes the previous
            STEP, WRL and preview (step_generator._run_freecad, HANDOFF 131:
            an old model must never pass for the new one). The KiCad actions,
            «Accetto, mi piace» and the preview path must follow the disk, also
            when the result itself is stale and otherwise ignored.
        """
        mesh = getattr(self, "_last_mesh_path", None)
        if mesh is not None and not Path(mesh).exists():
            self._last_mesh_path = None
        self._set_accept_models_ready(False)
        self._refresh_output_actions()

    def _on_cad3d_warning(self, message: str) -> None:
        """Annotazione non applicata: il modello c'e' comunque, ma va detto."""
        if not self._cad_result_is_current():
            return
        self._append_log(tx("ATTENZIONE 3D: {message}", message=message))

    # ── Tab 4: Anteprima ───────────────────────────────────────────

    def _build_preview_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(*(CONTENT_MARGIN_PX,) * 4)
        outer.setSpacing(8)

        header = QHBoxLayout()
        header.addWidget(QLabel("Anteprima 2D (silkscreen + pad, con quote):"))
        self.preview_pad_hint = QLabel(self.i18n.text(
            "Tasto destro su un pad per modificarne forma e dimensioni."))
        self.preview_pad_hint.setProperty("i18n_skip", True)
        header.addWidget(self.preview_pad_hint)
        header.addStretch()
        for asset, tooltip, callback in (
            ('zoom-in.svg', 'Ingrandisci', lambda: self.preview_view.zoom(1.2)),
            ('zoom-out.svg', 'Riduci', lambda: self.preview_view.zoom(1 / 1.2)),
            ('view-fit.svg', 'Adatta alla vista', lambda: self._fit_preview(force=True)),
        ):
            button = self._view_tool_button(asset, tooltip)
            button.clicked.connect(callback)
            header.addWidget(button)
        # Undo/redo of the drawings, same icons and shortcuts as the symbol
        # editor (user rule, 2026-10-13: same feature, same look everywhere).
        self.btn_fp_undo = self._view_tool_button('edit-undo.svg', 'Annulla modifica')
        self.btn_fp_undo.clicked.connect(self._undo_footprint_drawing)
        self.btn_fp_redo = self._view_tool_button('edit-redo.svg', 'Ripeti modifica')
        self.btn_fp_redo.clicked.connect(self._redo_footprint_drawing)
        header.addWidget(self.btn_fp_undo)
        header.addWidget(self.btn_fp_redo)
        # Footprint draft (user, 2026-10-14: «come il simbolo»): same icons,
        # texts and behaviour as the symbol editor's Annulla/Applica and
        # «Recupera bozza» (click: recover; arrow: «Scarta bozza»).
        self.btn_fp_cancel = self._view_tool_button('cancel.svg', 'Annulla')
        self.btn_fp_cancel.clicked.connect(self.discard_footprint_draft)
        self.btn_fp_apply = self._view_tool_button('confirm-use.svg', 'Applica')
        # A lambda: `clicked` passes a bool that would land in `folder`.
        self.btn_fp_apply.clicked.connect(lambda _checked=False: self.apply_footprint_draft())
        self.btn_fp_recover = self._view_tool_button('project-open.svg', 'Recupera bozza')
        self.btn_fp_recover.clicked.connect(self.recover_footprint_draft)
        recovery_menu = QMenu(self.btn_fp_recover)
        recover_action = recovery_menu.addAction(self.i18n.text("Recupera bozza"))
        recover_action.triggered.connect(lambda _checked=False: self.recover_footprint_draft())
        self.action_fp_discard_recovery = recovery_menu.addAction(self.i18n.text("Scarta bozza"))
        self.action_fp_discard_recovery.triggered.connect(
            lambda _checked=False: self.discard_footprint_recovery())
        # Menu entries are not widgets: the language pass does not reach
        # them, so they take the current language when the menu opens.
        recovery_menu.aboutToShow.connect(lambda: (
            recover_action.setText(self.i18n.text("Recupera bozza")),
            self.action_fp_discard_recovery.setText(self.i18n.text("Scarta bozza"))))
        self.btn_fp_recover.setMenu(recovery_menu)
        self.btn_fp_recover.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        for button in (self.btn_fp_cancel, self.btn_fp_apply, self.btn_fp_recover):
            header.addWidget(button)
        self._refresh_footprint_undo_buttons()
        # Stessa icona e stesso tooltip della barra 3D (utente, 2026-09-24):
        # e' lo stesso comando, deve riconoscersi uguale nelle due schede.
        self.btn_pinout_2d = self._view_tool_button(
            "pinout-table.svg", "Tabella pinout: numero, nome e funzione di ogni piedino.")
        self.btn_pinout_2d.clicked.connect(self._show_pinout_dialog)
        header.addWidget(self.btn_pinout_2d)
        self.chk_grid = QCheckBox("Griglia")
        self.chk_grid.setChecked(bool(self._config.get("show_grid", True)))
        self.chk_grid.toggled.connect(self._on_grid_toggled)
        header.addWidget(self.chk_grid)

        self.combo_grid_unit = QComboBox()
        # Il passo da 0.1" (2.54mm) e' quello dei componenti THT standard: con
        # quella griglia i pad cadono esattamente sulle intersezioni.
        self.combo_grid_unit.addItem("1 mm", "mm")
        self.combo_grid_unit.addItem("0.1 in (2.54 mm)", "inch")
        index = self.combo_grid_unit.findData(self._config.get("grid_unit", "mm"))
        self.combo_grid_unit.setCurrentIndex(max(index, 0))
        self.combo_grid_unit.setEnabled(self.chk_grid.isChecked())
        self.combo_grid_unit.currentIndexChanged.connect(self._on_grid_unit_changed)
        header.addWidget(self.combo_grid_unit)
        # Drawing grid as a fraction of the main grid (user, 2026-10-13:
        # «griglia puntinata a un decimo di quella attuale per ancorare gli
        # elementi grafici»); pads and holes never leave the main grid.
        from .footprint_view import DEFAULT_FINE_GRID_DIVISOR, FootprintView
        self.combo_fine_grid = QComboBox()
        for divisor in self.FOOTPRINT_FINE_GRID_DIVISORS:
            self.combo_fine_grid.addItem(f"1/{divisor}", divisor)
        self.combo_fine_grid.setProperty("i18n_skip", True)
        self.combo_fine_grid.setToolTip(self.i18n.text(
            "Passo della griglia del disegno, frazione della griglia principale: "
            "disegni e testi si agganciano ai suoi punti, piazzole e fori restano sulla griglia principale."))
        divisor = self._config.get("draw_grid_divisor", DEFAULT_FINE_GRID_DIVISOR)
        index = self.combo_fine_grid.findData(divisor)
        self.combo_fine_grid.setCurrentIndex(max(index, 0))
        self.combo_fine_grid.setEnabled(self.chk_grid.isChecked())
        self.combo_fine_grid.currentIndexChanged.connect(self._on_fine_grid_changed)
        header.addWidget(self.combo_fine_grid)
        outer.addLayout(header)

        self.preview_scene = QGraphicsScene()
        self.preview_view = FootprintView(self.preview_scene)
        self.preview_view.set_fine_divisor(self.combo_fine_grid.currentData())
        self.preview_view.pad_context_requested.connect(self._edit_preview_pad)
        self.preview_view.text_moved.connect(self._on_footprint_text_moved)
        self.preview_view.text_context_requested.connect(self._footprint_text_context_menu)
        self.preview_view.setMinimumHeight(500)
        # The footprint is drawn at a relatively high scene scale (20 px/mm).
        # Without these hints circles, square-pad corners and small pin labels
        # look visibly jagged in the dark preview even though KiCad's exported
        # geometry is correct.
        self.preview_view.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing
        )
        self.preview_mounting_group = QGroupBox("Footprint sulla scheda ospitante")
        mounting_layout = QVBoxLayout(self.preview_mounting_group)
        mounting_row = QHBoxLayout()
        self.combo_preview_mounting = QComboBox()
        for index in range(self.combo_mounting.count()):
            self.combo_preview_mounting.addItem(self.combo_mounting.itemText(index),
                                               self.combo_mounting.itemData(index))
        self.combo_preview_mounting.currentIndexChanged.connect(
            lambda: self.combo_mounting.setCurrentIndex(self.combo_mounting.findData(
                self.combo_preview_mounting.currentData())))
        # Combo al contenuto, non a tutta la riga; Pad/Foro alla sua destra
        # (utente, 2026-10-13: «accorcia la combo, i fori a destra»).
        self.combo_preview_mounting.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents)
        mounting_row.addWidget(self.combo_preview_mounting)
        self.btn_update_host_footprint_preview = icon_button(
            '', 'wf-regenerate.svg', lambda: self._generate(footprint_only=True))
        self.btn_update_host_footprint_preview.setToolTip("Aggiorna solo il footprint")
        self.btn_update_host_footprint_preview.setAccessibleName("Aggiorna solo il footprint")
        self.btn_update_host_footprint_preview.setEnabled(self.btn_update_host_footprint.isEnabled())
        self.preview_header_dimensions = QWidget()
        dimensions = QHBoxLayout(self.preview_header_dimensions)
        dimensions.setContentsMargins(0, 0, 0, 0)
        self.spin_preview_header_pad = QDoubleSpinBox()
        self.spin_preview_header_drill = QDoubleSpinBox()
        self.preview_header_pad_label = QLabel("Pad Ø:")
        for spin, source, label in ((self.spin_preview_header_pad, self.spin_header_pad,
                                     self.preview_header_pad_label),
                                    (self.spin_preview_header_drill, self.spin_header_drill,
                                     QLabel("Foro Ø:"))):
            spin.setRange(source.minimum(), source.maximum())
            spin.setDecimals(source.decimals())
            spin.setSingleStep(source.singleStep())
            spin.setSuffix(" mm")
            spin.setKeyboardTracking(False)
            spin.setValue(source.value())
            spin.valueChanged.connect(source.setValue)
            label.setBuddy(spin)
            dimensions.addWidget(label)
            dimensions.addWidget(spin)
        mounting_row.addWidget(self.preview_header_dimensions)
        mounting_row.addStretch(1)
        mounting_row.addWidget(self.btn_update_host_footprint_preview)
        mounting_layout.addLayout(mounting_row)
        self.label_preview_mounting_error = QLabel()
        self.label_preview_mounting_error.setWordWrap(True)
        self.label_preview_mounting_error.setProperty("i18n_skip", True)
        self.label_preview_mounting_error.hide()
        mounting_layout.addWidget(self.label_preview_mounting_error)
        outer.addWidget(self.preview_mounting_group)
        self._sync_preview_mounting()
        canvas_row = QHBoxLayout()
        canvas_row.setSpacing(4)
        canvas_row.addWidget(self.preview_view, 1)
        # Palette minima di disegno, a destra come nell'editor del simbolo
        # (utente, 2026-10-13): soli strumenti liberi, niente pad numerati.
        canvas_row.addWidget(self._build_footprint_palette())
        outer.addLayout(canvas_row, 1)
        self.preview_view.draw_clicked.connect(self._on_footprint_draw_clicked)
        self.preview_view.draw_finish_requested.connect(self._on_footprint_draw_finish)
        self.preview_view.draw_cancelled.connect(self._cancel_footprint_drawing)
        self.preview_view.draw_hovered.connect(self._on_footprint_draw_hovered)
        self.preview_view.draw_hover_left.connect(self._on_footprint_draw_hover_left)
        self.preview_view.drawing_hovered.connect(self._on_footprint_drawing_hovered)
        from PyQt6.QtGui import QShortcut
        for keys, slot in (('Ctrl+Z', self._undo_footprint_drawing),
                           ('Ctrl+Y', self._redo_footprint_drawing),
                           ('Ctrl+Shift+Z', self._redo_footprint_drawing),
                           ('Ctrl+D', self._duplicate_hovered_drawing),
                           ('Delete', self._delete_hovered_drawing)):
            shortcut = QShortcut(QKeySequence(keys), self.preview_view)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(slot)
        self.preview_view.handle_pressed.connect(self._on_footprint_handle_pressed)
        self.preview_view.handle_dragged.connect(self._on_footprint_handle_dragged)
        self.preview_view.handle_dropped.connect(self._on_footprint_handle_dropped)
        self.preview_view.drawing_context_requested.connect(self._footprint_drawing_context_menu)

        return tab

    #: Layers a preview drawing may go to: (KiCad layer, source label).
    #: Drawing-grid divisors offered in the preview header (1/10 default).
    FOOTPRINT_FINE_GRID_DIVISORS = (10, 5, 2, 1)

    FOOTPRINT_DRAW_LAYERS = (("F.SilkS", "Serigrafia (stampata)"),
                             ("F.Fab", "Fabbricazione (documentazione)"),
                             ("Cmts.User", "Commenti (solo progetto)"))

    def _build_footprint_palette(self) -> QWidget:
        """@brief Build the minimal drawing palette of the footprint preview.

        @return A narrow column: the target layer selector, then one
            checkable button per free drawing tool (line, rectangle,
            circle, arc, text). No library elements and no numbered pads:
            a pad must match a symbol pin, so it is not free drawing.
        """
        from .symbol_tab import glyph_icon, TOOL_HINTS
        from ..generators.symbol_glyphs import shape_tool_glyphs
        panel = QWidget()
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        title = QLabel(self.i18n.text("Disegno"))
        column.addWidget(title)
        self.combo_fp_draw_layer = QComboBox()
        for layer, label in self.FOOTPRINT_DRAW_LAYERS:
            # The colour square tells which colour the drawing will take.
            self.combo_fp_draw_layer.addItem(_layer_swatch(_layer_color(layer)),
                                             self.i18n.text(label), layer)
        self.combo_fp_draw_layer.setToolTip(self.i18n.text(
            "Layer del disegno: la serigrafia viene stampata sulla scheda, "
            "la fabbricazione documenta il montaggio, i commenti restano nel progetto."))
        column.addWidget(self.combo_fp_draw_layer)
        self._fp_draw_buttons = {}
        grid = QGridLayout()
        grid.setSpacing(2)
        tools = [g for g in shape_tool_glyphs() if g.key != "bezier"]
        for index, glyph in enumerate(tools):
            button = QToolButton()
            button.setIcon(glyph_icon(glyph))
            button.setIconSize(QSize(28, 28))
            button.setFixedSize(34, 34)
            button.setCheckable(True)
            button.setAutoRaise(True)
            button.setProperty("i18n_skip", True)
            button.setAccessibleName(self.i18n.text(glyph.label))
            button.setToolTip(self.i18n.text(TOOL_HINTS.get(glyph.key, glyph.label)))
            button.clicked.connect(lambda _checked=False, k=glyph.key:
                                   self._toggle_footprint_draw_tool(k))
            grid.addWidget(button, index // 2, index % 2)
            self._fp_draw_buttons[glyph.key] = button
        column.addLayout(grid)
        column.addStretch(1)
        return panel

    def _toggle_footprint_draw_tool(self, key: str) -> None:
        """@brief Arm a palette tool of the preview, or disarm the armed one."""
        armed = self._fp_draw_tool != key
        self._cancel_footprint_drawing()
        if armed:
            self._fp_draw_tool = key
            self._fp_draw_buttons[key].setChecked(True)
            self.preview_view.set_draw_mode(True)

    def _cancel_footprint_drawing(self) -> None:
        """@brief Disarm the tool and forget the clicks of the shape in progress."""
        self._fp_draw_tool = None
        self._fp_draw_clicks = []
        self._fp_draw_hover = None
        self._fp_handle_drag = None
        self._fp_handle_origin = None
        for button in getattr(self, "_fp_draw_buttons", {}).values():
            button.setChecked(False)
        if hasattr(self, "preview_view"):
            self.preview_view.set_draw_mode(False)
        self._refresh_draw_markers()

    def _on_footprint_draw_clicked(self, x: float, y: float) -> None:
        """@brief Collect one drawing click (scene px), snapped to the grid, in mm."""
        if self._fp_draw_tool is None:
            return
        # Nodes of the grid actually drawn (its origin is the first pad, not
        # the footprint origin); 0.1 mm when the grid is hidden.
        point = self._snapped_draw_point(x, y)
        # A double click sends its own press first: ignore the repeat. On a
        # coarse grid two separate clicks can land on the SAME node (user,
        # 2026-10-13: armed tool, one cross, «non funziona»): say it instead
        # of silently waiting forever.
        if self._fp_draw_clicks and self._fp_draw_clicks[-1] == point:
            self._draw_feedback_toast(self.i18n.text(
                "Punto coincidente con il precedente."), self.i18n.text(
                "Con la griglia attiva i click si agganciano ai suoi nodi: "
                "clicca un altro nodo o scegli una griglia più fine."))
            return
        self._fp_draw_clicks.append(point)
        from ..core.footprint import USER_DRAW_KINDS
        count = USER_DRAW_KINDS[self._fp_draw_tool]
        if count and len(self._fp_draw_clicks) >= count:
            self._on_footprint_draw_finish()
        else:
            self._refresh_draw_markers()

    def _on_footprint_draw_finish(self) -> None:
        """@brief Close the shape in progress and store it as a drawing record."""
        from ..core.footprint import (USER_DRAW_KINDS, USER_DRAW_TEXT_MM, USER_TEXT_ANCHOR,
                                      USER_DRAW_WIDTH_MM, validate_footprint_drawings)
        tool, points = self._fp_draw_tool, list(self._fp_draw_clicks)
        if tool is None:
            return
        count = USER_DRAW_KINDS[tool]
        if count and len(points) != count or not count and len(points) < 2:
            self._cancel_footprint_drawing()
            return
        record = {"kind": tool, "layer": self.combo_fp_draw_layer.currentData(),
                  "width": USER_DRAW_WIDTH_MM, "points": [list(p) for p in points]}
        if tool == "arc":
            # Click order is start, end, mid (same hint as the symbol);
            # the file stores start, mid, end as KiCad writes fp_arc.
            start, end, mid = points
            record["points"] = [list(start), list(mid), list(end)]
        if tool == "text":
            text, accepted = QInputDialog.getText(
                self, self.i18n.text("Testo"), self.i18n.text("Testo da scrivere:"))
            if not accepted or not text.strip():
                self._cancel_footprint_drawing()
                return
            record["text"], record["size"] = text.strip(), USER_DRAW_TEXT_MM
            # The clicked (snapped) node is the bottom-left corner of the text.
            record["anchor"] = USER_TEXT_ANCHOR
        valid = validate_footprint_drawings([record])
        self._cancel_footprint_drawing()
        if not valid:
            # Degenerate shape (zero radius, flat rectangle...): say why
            # nothing appears instead of silently dropping it.
            self._draw_feedback_toast(self.i18n.text("Disegno scartato."),
                                      self._degenerate_drawing_text(record))
            return
        self._apply_footprint_drawings(self.footprint_drawings + valid)

    def _degenerate_drawing_text(self, record: dict) -> str:
        """@brief Translated reason why a drawing record was refused by the validator.

        @param record The refused record (kind and points as drawn).
        @return The aligned-arc explanation when the record is an arc through
            three distinct points whose centre KiCad cannot hold (collinear,
            or so nearly collinear that the centre leaves KiCad's coordinate
            range); otherwise the generic zero-radius/zero-side text.
        """
        from ..core.footprint import arc_centre_representable
        points = [tuple(p) for p in record.get("points") or ()]
        if (record.get("kind") == "arc" and len(points) == 3 and len(set(points)) == 3
                and not arc_centre_representable(*points)):
            return self.i18n.text(
                "Arco senza centro: i tre punti sono allineati o quasi. "
                "Sposta il punto intermedio fuori dalla retta.")
        return self.i18n.text(
            "La forma è degenere (raggio nullo o lato nullo): "
            "clicca punti distinti sulla griglia.")

    def _draw_feedback_toast(self, title: str, body: str) -> None:
        """@brief Small non-modal card for drawing feedback, next to the cursor.

        @param title Already translated first line.
        @param body Already translated explanation.
        """
        from .toast import Toast
        toast = getattr(self, "_draw_toast", None)
        if toast is None:
            toast = self._draw_toast = Toast(self)
        toast.show_message(title, body)

    def _apply_footprint_drawings(self, updated: list, *, history: str = "push") -> bool:
        """@brief Put a new drawings list in the footprint draft (nothing is saved).

        @param updated The new list of validated records.
        @param history "push" records the previous edit state for undo and
            clears redo; "none" records nothing (undo and redo go through
            `_step_footprint_history`).
        @return True (the draft always takes a validated list).
        @note CONTRACT CHANGED 2026-10-14 (footprint draft, user): the list is
            no longer saved and the .kicad_mod no longer rewritten here; both
            happen on «Applica» (`apply_footprint_draft`).
        """
        return self._edit_footprint_draft({"drawings": updated}, history=history)

    # ── Footprint draft: Applica / Annulla / Recupera bozza (user, 2026-10-14) ──

    def _footprint_edit_state(self) -> dict:
        """@brief The footprint edits the preview shows now (the draft), by reference.

        @return {"drawings", "texts", "pad_overrides", "package_pins"}; the
            last is the list of the package pads, or None without a package.
        @note Not copied: callers that keep it deep-copy it.
        """
        package = getattr(self, "package_geometry", None)
        return {"drawings": self.footprint_drawings,
                "texts": self.footprint_text_positions,
                "pad_overrides": self.header_pad_overrides,
                "package_pins": list(package.pins) if package is not None else None}

    def _sync_footprint_draft_package(self) -> None:
        """@brief Rebase the draft when an analysis replaced the package object.

        @note The applied package pads and the undo history refer to the
            package object they were taken from. A new package (extraction,
            project switch) makes them meaningless: the applied pads become
            the new package's, and the history is dropped (its steps would
            put another package's pads back). Drawings, texts and pad
            overrides of the draft are untouched.
        """
        package = getattr(self, "package_geometry", None)
        old_package = getattr(self, "_fp_draft_package", None)
        if package is old_package:
            return
        self._fp_draft_package = package
        applied = getattr(self, "_fp_applied", None)
        if applied is not None:
            dropped = (old_package is not None and applied["package_pins"] is not None
                       and list(old_package.pins) != list(applied["package_pins"]))
            applied["package_pins"] = list(package.pins) if package is not None else None
            if dropped:
                # Said, never silent: the pad edits belonged to the package
                # the analysis replaced. The KiCad tab, the recovery file and
                # the buttons follow on the next turn (this runs inside
                # state reads, e.g. a project save).
                self._append_log(self.i18n.text(
                    "Footprint: una nuova analisi ha sostituito il package; le modifiche "
                    "ai pad non applicate sono state scartate."))
                QTimer.singleShot(0, self._after_footprint_package_rebase)
        self._fp_drawings_undo, self._fp_drawings_redo = [], []
        self._refresh_footprint_undo_buttons()

    def _after_footprint_package_rebase(self) -> None:
        """@brief Refresh what follows the draft after `_sync_footprint_draft_package` dropped pad edits."""
        if getattr(self, "_window_closed", False):
            return
        self._checkpoint_footprint_draft()
        self._refresh_footprint_undo_buttons()
        self._refresh_footprint_draft_notice()

    def _applied_footprint_state(self) -> dict:
        """@brief The applied footprint edits: what the project file and the .kicad_mod hold.

        @return The same keys as `_footprint_edit_state`; the draft itself
            when nothing was edited since the last apply. Not copied.
        """
        if getattr(self, "_fp_applied", None) is None:
            return self._footprint_edit_state()
        self._sync_footprint_draft_package()
        return self._fp_applied

    def has_pending_footprint_edits(self) -> bool:
        """@brief Tell whether the footprint preview shows edits not yet applied.

        @return True when the draft differs from the applied state; an edit
            undone back to the applied state is not pending.
        """
        if getattr(self, "_fp_applied", None) is None:
            return False
        self._sync_footprint_draft_package()
        return self._footprint_edit_state() != self._fp_applied

    def _set_footprint_edit_state(self, state: dict) -> None:
        """@brief Make `state` (a deep copy of it) the draft the preview shows.

        @param state Edit state as `_footprint_edit_state` returns it. Its
            package pads are applied only to a package with the same pad
            numbers, in the same order (never another package's pads).
        """
        self.footprint_drawings = deepcopy(state["drawings"])
        self.footprint_text_positions = deepcopy(state["texts"])
        self.header_pad_overrides = deepcopy(state["pad_overrides"])
        pins = state.get("package_pins")
        package = self.package_geometry
        if (package is not None and pins is not None and list(pins) != list(package.pins)
                and [p.number for p in pins] == [p.number for p in package.pins]):
            self._rebind_package_pins(pins)

    def _rebind_package_pins(self, pins) -> None:
        """@brief Give the package new pads by REBINDING it to a copy, never by mutating it.

        @param pins The new pad list.
        @note A worker started earlier may hold the package object (a
            generation started with no draft pending passes the live
            object): mutating it would let that worker see a later draft
            edit. The copy replaces the package everywhere the window refers
            to the same object (the draft's package identity, the
            parameters the 2D preview draws), so it is not mistaken for a
            package replaced by an analysis (`_sync_footprint_draft_package`).
        """
        old = self.package_geometry
        new = replace(old, pins=list(pins))
        self.package_geometry = new
        if getattr(self, "_fp_draft_package", None) is old:
            self._fp_draft_package = new
        if getattr(self, "_last_params", None) is old:
            self._last_params = new

    def _edit_footprint_draft(self, changes: dict, *, history: str = "push") -> bool:
        """@brief Change fields of the footprint draft, with undo, and show it.

        @param changes Subset of the edit-state keys with their new values
            ("package_pins": the full new pad list of the package).
        @param history "push" records the previous state for undo and clears
            redo; "none" records nothing.
        @return True.
        @note The first edit after an apply captures the applied state
            (`_fp_applied`). Nothing is written to the project file or the
            .kicad_mod: «Applica» does (`apply_footprint_draft`). The draft
            is checkpointed to the recovery file (`_checkpoint_footprint_draft`).
        """
        self._sync_footprint_draft_package()
        before = deepcopy(self._footprint_edit_state())
        if self._fp_applied is None:
            self._fp_applied = deepcopy(before)
            self._fp_draft_package = self.package_geometry
        for key, value in changes.items():
            if key == "drawings":
                self.footprint_drawings = value
            elif key == "texts":
                self.footprint_text_positions = value
            elif key == "pad_overrides":
                self.header_pad_overrides = value
            elif key == "package_pins" and self.package_geometry is not None and value is not None:
                self._rebind_package_pins(value)
        if history == "push":
            self._fp_drawings_undo.append(before)
            self._fp_drawings_redo.clear()
        self._footprint_draft_changed()
        return True

    def _step_footprint_history(self, source: list, target: list) -> bool:
        """@brief Move one step of the footprint history (undo: source=undo, target=redo).

        @param source Stack the restored state is popped from.
        @param target Stack the current state is pushed to.
        @return True when a step was taken.
        @note A step changes the draft only; the applied state is captured
            first when there was no draft (undo right after an apply).
        """
        self._cancel_footprint_drawing()
        self._sync_footprint_draft_package()
        if not source:
            return False
        current = deepcopy(self._footprint_edit_state())
        if self._fp_applied is None:
            self._fp_applied = deepcopy(current)
            self._fp_draft_package = self.package_geometry
        self._set_footprint_edit_state(source.pop())
        target.append(current)
        self._footprint_draft_changed()
        return True

    def _footprint_draft_changed(self) -> None:
        """@brief Show the draft after any change: preview, buttons, recovery file, KiCad tab.

        @note Any change of the drawings ends a handle or text drag in
            progress: the drag holds indices into the OLD list, and its drop
            would otherwise edit whichever drawing now sits at that index (a
            Delete or Ctrl+Z pressed while the mouse button is still down).
        """
        self._end_footprint_drawing_interaction()
        self._fp_hovered_drawing = -1
        view = getattr(self, "preview_view", None)
        if view is not None:
            # The view's own hover memory must forget the old list too, or a
            # pointer that lands again on the same index emits nothing.
            view.reset_hover()
        self._refresh_footprint_undo_buttons()
        self._draw_preview()
        self._checkpoint_footprint_draft()
        self._refresh_footprint_draft_notice()

    def _refresh_footprint_draft_notice(self) -> None:
        """@brief Refresh the KiCad tab when the footprint draft starts or stops being pending."""
        pending = self.has_pending_footprint_edits()
        if pending != getattr(self, "_fp_draft_was_pending", False):
            self._fp_draft_was_pending = pending
            self._refresh_output_actions()

    def apply_footprint_draft(self, folder: Path | None = None) -> bool:
        """@brief «Applica»: save the draft in the project and rewrite the .kicad_mod once.

        @param folder None saves through `_save_project_config` (the open
            project in the current working folder). The open project's
            folder when the working folder has already changed (leave
            question of `_on_library_root_changed`): the project file is
            written there explicitly (`_write_project_config_at`).
        @return True when nothing was pending or the draft was saved; False
            when the project file could not be written (the draft is kept,
            like `SymbolTab.reject_apply`, and the user is told).
        @note One project write, then one rewrite through
            `_schedule_footprint_rewrite` (deferred, coalesced, never while a
            worker runs or an analysis pipeline owns the output; a pad edit
            rewrites even before a .kicad_mod exists, as before). The undo
            history is kept, as in the symbol editor: undoing after an apply
            starts a new draft over the new applied state.
        """
        if not self.has_pending_footprint_edits():
            self._fp_applied = None
            self._refresh_footprint_undo_buttons()
            self._refresh_footprint_draft_notice()
            return True
        self._cancel_footprint_drawing()
        applied = self._fp_applied
        current = self._footprint_edit_state()
        pad_edit = (current["pad_overrides"] != applied["pad_overrides"]
                    or current["package_pins"] != applied["package_pins"])
        self._fp_applied = None
        was_stale = self._fp_file_stale
        if folder is not None:
            # No rewrite can run for a project being left from another
            # working folder (it would write there): the file says so.
            self._fp_file_stale = True
        saved = (self._save_project_config(force=True) if folder is None
                 else self._write_project_config_at(folder))
        if not saved:
            self._fp_file_stale = was_stale
            self._fp_applied = applied
            QMessageBox.warning(self, self.i18n.text("Errore"), self.i18n.text(
                "Modifiche al footprint non salvate: restano da applicare."))
            return False
        self._checkpoint_footprint_draft()
        self._refresh_footprint_undo_buttons()
        self._refresh_footprint_draft_notice()
        if folder is None:
            self._schedule_footprint_rewrite(pad_edit=pad_edit)
        return True

    def discard_footprint_draft(self) -> None:
        """@brief «Annulla»: show the applied footprint again, dropping the draft.

        @note Undoable: the discarded draft is pushed on the undo stack, so
            Ctrl+Z brings it back (as a draft). Nothing is written to the
            project or the .kicad_mod; the recovery file goes, unless a
            crash draft is still offered (it is retired only explicitly).
        """
        if not self.has_pending_footprint_edits():
            return
        self._cancel_footprint_drawing()
        self._fp_drawings_undo.append(deepcopy(self._footprint_edit_state()))
        self._fp_drawings_redo.clear()
        self._set_footprint_edit_state(self._fp_applied)
        self._fp_applied = None
        self._footprint_draft_changed()

    def _settle_footprint_draft_before_start(self) -> bool:
        """@brief Before a generation or analysis starts, apply or discard a pending footprint draft.

        @return True to go on with the start (no draft, or applied, or
            discarded); False to cancel it (the user's choice, or a draft
            that could not be saved).
        """
        if not self.has_pending_footprint_edits():
            return True
        choice = self._ask_footprint_draft_before_start()
        if choice == self.PENDING_EDITS_APPLY:
            return self.apply_footprint_draft()
        if choice == self.PENDING_EDITS_DISCARD:
            self.discard_footprint_draft()
            return True
        return False

    def _ask_footprint_draft_before_start(self) -> str:
        """@brief Ask, in a centred modal, what to do with the footprint draft before a new run.

        @return PENDING_EDITS_APPLY, PENDING_EDITS_DISCARD, or
            PENDING_EDITS_STAY to cancel the start (also Esc and the close
            button).
        @note Same look as `_ask_pending_edits_choice`. A separate method so
            tests answer it without opening a window (conftest answers
            «Applica»: before the draft every edit was already saved when a
            run started, so that answer keeps the earlier behaviour).
        """
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(self.i18n.text("Footprint"))
        box.setText(self.i18n.text("Il footprint contiene modifiche non applicate."))
        box.setInformativeText(self.i18n.text(
            "Applicarle o scartarle prima di avviare? Con «Resta» non parte nulla."))
        apply_button = box.addButton(self.i18n.text("Applica"), QMessageBox.ButtonRole.AcceptRole)
        discard_button = box.addButton(self.i18n.text("Scarta"), QMessageBox.ButtonRole.DestructiveRole)
        stay_button = box.addButton(self.i18n.text("Resta"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(stay_button)
        box.setEscapeButton(stay_button)
        box.exec()
        chosen = box.clickedButton()
        if chosen is apply_button:
            return self.PENDING_EDITS_APPLY
        if chosen is discard_button:
            return self.PENDING_EDITS_DISCARD
        return self.PENDING_EDITS_STAY

    def _finish_footprint_rewrite_now(self) -> None:
        """@brief Run the pending .kicad_mod rewrite to its end before the project is left.

        @note Used after «Applica» in the leave question: the deferred
            rewrite would be dropped by the project opened next (or by the
            closed window). When the window is busy the rewrite cannot start;
            the applied edits are in the project file and the next generation
            writes them (same rule as `_flush_footprint_rewrite`).
        """
        if not getattr(self, "_fp_rewrite_dirty", False):
            return
        self._flush_footprint_rewrite()
        worker = getattr(self, "_worker", None)
        if worker is not None and worker.isRunning():
            loop = QEventLoop()
            worker.finished.connect(loop.quit)
            if worker.isRunning():
                loop.exec()

    @contextmanager
    def _applied_footprint_in_memory(self):
        """@brief Context in which the footprint fields hold the APPLIED state, then the draft again.

        @note `_generate` runs inside it: whatever starts a generation (the
            deferred rewrite of an earlier apply, the automatic pipeline),
            the files get the applied footprint, never an unapplied draft
            (user, 2026-10-14: exports always use the applied state). The
            package is swapped for a copy carrying the applied pads, so the
            worker keeps that copy and the preview keeps its draft object.
        """
        if not self.has_pending_footprint_edits():
            yield
            return
        applied = self._fp_applied
        saved = (self.footprint_drawings, self.footprint_text_positions,
                 self.header_pad_overrides, self.package_geometry)
        self.footprint_drawings = deepcopy(applied["drawings"])
        self.footprint_text_positions = deepcopy(applied["texts"])
        self.header_pad_overrides = deepcopy(applied["pad_overrides"])
        if self.package_geometry is not None and applied["package_pins"] is not None:
            self.package_geometry = replace(self.package_geometry, pins=list(applied["package_pins"]))
        # The swapped package is not a new package: the draft keeps its own.
        draft_package = self._fp_draft_package
        swapped = self.package_geometry
        self._fp_draft_package = swapped
        try:
            yield
        finally:
            (self.footprint_drawings, self.footprint_text_positions,
             self.header_pad_overrides, self.package_geometry) = saved
            self._fp_draft_package = draft_package
            # The 2D preview draws `_last_params` first: it must keep showing
            # the draft package, not the applied copy the worker received.
            if swapped is not saved[3] and getattr(self, "_last_params", None) is swapped:
                self._last_params = saved[3]

    # ── Footprint recovery file (same conventions as the symbol's) ──

    def _footprint_recovery_path(self, name: str | None = None) -> Path | None:
        """@brief A recovery file of the open project, or None without an existing project folder.

        @param name File name; None means the current-draft file
            (`footprint_draft.FOOTPRINT_DRAFT_FILE`).
        @return The path in the OPEN project folder (`_open_project_folder`),
            never in a folder derived from a working folder being typed or
            changed.
        """
        folder = getattr(self, "_open_project_folder", None)
        if folder is None or not Path(folder).is_dir():
            return None
        from ..services.footprint_draft import FOOTPRINT_DRAFT_FILE
        return Path(folder) / (name or FOOTPRINT_DRAFT_FILE)

    def _footprint_kept_path(self) -> Path | None:
        """@brief File of the offered crash drafts displaced by newer edits, or None without a folder."""
        from ..services.footprint_draft import KEPT_DRAFTS_FILE
        return self._footprint_recovery_path(KEPT_DRAFTS_FILE)

    @staticmethod
    def _footprint_state_json(state: dict) -> dict:
        """@brief JSON form of a footprint edit state (lists, pad mappings), for the recovery file.

        @param state Edit state (`_footprint_edit_state`).
        @return The same data as plain JSON values.
        @throws ValueError When a value cannot be written as JSON (NaN).
        """
        from dataclasses import asdict
        pins = state.get("package_pins")
        return json.loads(json.dumps({
            "drawings": state["drawings"],
            "texts": {key: list(value) for key, value in state["texts"].items()},
            "pad_overrides": state["pad_overrides"],
            "package_pins": [asdict(pin) for pin in pins] if pins is not None else None,
        }, ensure_ascii=False, allow_nan=False))

    def _footprint_state_from_json(self, data: dict) -> dict:
        """@brief Validated edit state from the JSON form read back from a recovery file.

        @param data JSON form (`_footprint_state_json`).
        @return An edit state; values that do not validate are dropped with
            the same validators as the project file. Package pads are kept
            only when they rebuild a valid package with the open package's
            pad numbers, else the open package's pads stay.
        """
        from dataclasses import asdict
        from ..core.footprint import validate_footprint_drawings, validate_text_positions
        package = self.package_geometry
        pins = list(package.pins) if package is not None else None
        raw_pins = data.get("package_pins")
        if package is not None and isinstance(raw_pins, list):
            try:
                rebuilt = package_from_mapping({**asdict(package), "pins": raw_pins})
            except (AttributeError, KeyError, TypeError, ValueError):
                rebuilt = None
            if rebuilt is not None and [p.number for p in rebuilt.pins] == [p.number for p in package.pins]:
                pins = list(rebuilt.pins)
        return {"drawings": validate_footprint_drawings(data.get("drawings")),
                "texts": validate_text_positions(data.get("texts")),
                "pad_overrides": self._valid_pad_overrides(data.get("pad_overrides")),
                "package_pins": pins}

    def _keep_offered_footprint_draft(self, extra: dict | None = None) -> bool:
        """@brief Move the offered crash draft (and `extra`) into the kept-drafts file.

        @param extra Another draft (JSON form) to keep, newest, or None.
        @return True when nothing needed keeping or the kept file was written;
            False when it could not be written (the caller must not overwrite
            the current-draft file then).
        @note Called before the current-draft file is overwritten or removed
            while it still holds the offered draft (`_fp_recoverable_source`
            "main"): the offered draft stays offered, now from the kept file,
            until the user recovers or discards it.
        """
        drafts = []
        if extra is not None:
            drafts.append(extra)
        if self._fp_recoverable is not None and self._fp_recoverable_source == "main":
            drafts.append(self._fp_recoverable)
        if not drafts:
            return True
        path = self._footprint_kept_path()
        if path is None:
            return True
        from ..services.footprint_draft import load_kept_drafts, save_kept_drafts
        try:
            if not save_kept_drafts(path, drafts + load_kept_drafts(path)):
                return False
        except (OSError, TypeError, ValueError) as exc:
            self._append_log(self.i18n.text(
                "Bozza non salvata: verifica lo spazio e i permessi della cartella.") + f" ({exc})")
            return False
        if self._fp_recoverable is not None and self._fp_recoverable_source == "main":
            self._fp_recoverable_source = "kept"
        return True

    def _checkpoint_footprint_draft(self) -> None:
        """@brief Write the pending draft to the recovery file, or remove the file when nothing is pending.

        @note An offered crash draft still held by that file is first moved
            to the kept-drafts file (`_keep_offered_footprint_draft`), so the
            current edits are always checkpointed and the offered draft stays
            recoverable: a second crash offers the newest edits first, then
            the kept one. A write failure is logged, never raised (the draft
            stays in memory).
        """
        path = self._footprint_recovery_path()
        if path is None:
            return
        from ..services.footprint_draft import remove_footprint_draft, save_footprint_draft
        try:
            pending = self.has_pending_footprint_edits()
            current = self._footprint_state_json(self._footprint_edit_state()) if pending else None
            if (self._fp_recoverable is not None and self._fp_recoverable_source == "main"
                    and current != self._fp_recoverable
                    and not self._keep_offered_footprint_draft()):
                return
            if pending:
                save_footprint_draft(path, self._footprint_state_json(self._fp_applied), current)
            else:
                remove_footprint_draft(path)
        except (OSError, TypeError, ValueError) as exc:
            self._append_log(self.i18n.text(
                "Bozza non salvata: verifica lo spazio e i permessi della cartella.") + f" ({exc})")

    def _next_footprint_offer(self) -> None:
        """@brief Offer the next recoverable draft: the current-draft file first, then the kept ones.

        @note Read only. The current-draft file is offered only when bound to
            the applied state now in the project (as for the symbol,
            `symbol_draft.load_draft`) and different from what the preview
            shows; kept drafts are complete states, offered whatever the
            applied state, and re-validated when recovered.
        """
        self._fp_recoverable = None
        self._fp_recoverable_source = None
        path = self._footprint_recovery_path()
        if path is None:
            return
        from ..services.footprint_draft import load_footprint_draft, load_kept_drafts
        current = self._footprint_state_json(self._footprint_edit_state())
        try:
            main = load_footprint_draft(
                path, self._footprint_state_json(self._applied_footprint_state()))
            if main is not None and main != current:
                self._fp_recoverable, self._fp_recoverable_source = main, "main"
                return
            for kept in load_kept_drafts(self._footprint_kept_path()):
                if kept != current:
                    self._fp_recoverable, self._fp_recoverable_source = kept, "kept"
                    return
        except (OSError, ValueError, RecursionError):
            self._append_log(self.i18n.text("Impossibile leggere la bozza di recupero."))

    def _offer_footprint_recovery(self) -> None:
        """@brief Offer the draft a crash left in this project's recovery files («Recupera bozza»).

        @note Read only: the draft is offered, never applied nor loaded, and
            its file stays until the user recovers or discards it. Called
            when a project is opened; an unreadable file (even one nested
            too deeply for the parser) is logged and never blocks the open.
        """
        try:
            self._next_footprint_offer()
        except (OSError, ValueError, TypeError, RecursionError):
            self._fp_recoverable = None
            self._fp_recoverable_source = None
            self._append_log(self.i18n.text("Impossibile leggere la bozza di recupero."))
        if self._fp_recoverable is not None:
            self._append_log(self.i18n.text("Footprint") + ": " + self.i18n.text(
                "Bozza disponibile: usa Recupera bozza per riprenderla."))
        self._refresh_footprint_undo_buttons()

    def _drop_kept_footprint_draft(self, draft: dict) -> bool:
        """Remove one draft from the kept-drafts file; False when the file cannot be written."""
        path = self._footprint_kept_path()
        if path is None:
            return True
        from ..services.footprint_draft import load_kept_drafts, save_kept_drafts
        try:
            return save_kept_drafts(path, [d for d in load_kept_drafts(path) if d != draft])
        except (OSError, TypeError, ValueError):
            self._append_log(self.i18n.text(
                "Bozza non salvata: verifica lo spazio e i permessi della cartella."))
            return False

    def recover_footprint_draft(self) -> None:
        """@brief «Recupera bozza»: make the offered crash draft the footprint draft (not applied).

        @note Nothing is lost: unapplied edits shown before are kept as a
            recoverable draft (kept-drafts file) and pushed on the undo stack;
            if they cannot be kept the recovery is refused. The recovered
            draft then becomes the content of the current-draft file, and
            the next recoverable draft, if any, is offered.
        """
        offered, source = self._fp_recoverable, self._fp_recoverable_source
        if offered is None:
            return
        if self.has_pending_footprint_edits():
            own = self._footprint_state_json(self._footprint_edit_state())
            if own != offered and not self._keep_offered_footprint_draft(extra=own):
                return
            source = self._fp_recoverable_source
        if source == "kept" and not self._drop_kept_footprint_draft(offered):
            return
        state = self._footprint_state_from_json(offered)
        self._fp_recoverable = None
        self._fp_recoverable_source = None
        self._edit_footprint_draft(state)
        self._offer_next_after_recovery()

    def _offer_next_after_recovery(self) -> None:
        """Offer the next kept draft, if any, after a recovery or a discard."""
        try:
            self._next_footprint_offer()
        except (OSError, ValueError, TypeError, RecursionError):
            self._fp_recoverable = None
            self._fp_recoverable_source = None
        self._refresh_footprint_undo_buttons()

    def discard_footprint_recovery(self) -> bool:
        """@brief «Scarta bozza»: retire the offered crash draft explicitly.

        @return True when an offered draft was discarded; False when none was
            offered or its file could not be changed (the offer stays).
        @note A draft offered from the current-draft file removes that file
            (the preview has no own edits then: they would have moved it to
            the kept file); a kept draft leaves the kept file. The next
            recoverable draft, if any, is offered.
        """
        if self._fp_recoverable is None:
            return False
        if self._fp_recoverable_source == "kept":
            if not self._drop_kept_footprint_draft(self._fp_recoverable):
                return False
        else:
            path = self._footprint_recovery_path()
            from ..services.footprint_draft import remove_footprint_draft
            try:
                if path is not None:
                    remove_footprint_draft(path)
            except OSError:
                self._append_log(self.i18n.text(
                    "Bozza non salvata: verifica lo spazio e i permessi della cartella."))
                return False
        self._fp_recoverable = None
        self._fp_recoverable_source = None
        self._checkpoint_footprint_draft()
        self._append_log(self.i18n.text("Footprint") + ": " + self.i18n.text("Bozza scartata."))
        self._offer_next_after_recovery()
        return True

    def _end_footprint_drawing_interaction(self) -> None:
        """@brief Forget any handle or text drag in progress, in the view and here.

        @note Called whenever the drawings list changes: the drag's indices
            refer to the list it started on.
        """
        view = getattr(self, "preview_view", None)
        if view is not None:
            view.cancel_interaction()
        if self._fp_handle_active:
            self._fp_handle_cancelled = True
        self._fp_handle_active = False
        self._fp_handle_drag = None
        self._fp_handle_origin = None

    def _schedule_footprint_rewrite(self, immediate: bool = False, pad_edit: bool = False) -> None:
        """@brief Mark the .kicad_mod as stale and rewrite it as soon as nothing else runs.

        @param immediate True tries the rewrite now (an edit confirmed in a
            modal dialog: no drag frame to protect); False defers it to the
            next event-loop turn. Either way a busy window keeps the flag.
        @param pad_edit True for a pad edit, which goes to `_generate` as it
            always did (it rewrites the footprint of an acquired component
            even before a .kicad_mod exists); False (drawings, text
            positions) rewrites only a .kicad_mod already in the project.

        @note Every saved edit of the footprint (drawings, text positions,
            pad overrides) calls this. The «footprint dirty» flag is set here
            and consumed by the first rewrite attempt made while the window
            is idle, or by any generation that starts (it copies the same
            data): an edit made while another worker runs is not lost, it is
            retried from the finished handler of every worker
            (`_retry_footprint_rewrite`). A burst of edits gives one rewrite,
            with the data current when the worker starts.
        @note The start is deferred to the next event-loop turn so the frame
            that ends a drag paints first and the drop does not stutter
            (profiling 2026-10-13: 20-115 ms at the drop depending on the pad
            count).
        """
        self._fp_rewrite_dirty = True
        self._fp_rewrite_pad_edit = self._fp_rewrite_pad_edit or pad_edit
        if immediate:
            self._flush_footprint_rewrite()
        else:
            self._retry_footprint_rewrite()

    def _clear_footprint_rewrite(self) -> None:
        """@brief Forget the pending .kicad_mod rewrite (covered, attempted or obsolete)."""
        self._fp_rewrite_dirty = False
        self._fp_rewrite_pad_edit = False

    def _retry_footprint_rewrite(self) -> None:
        """@brief Queue one attempt of the pending .kicad_mod rewrite, if any.

        @note Safe to call from any worker's finished handler: it does
            nothing when no edit is pending, and several calls in the same
            turn coalesce into one attempt.
        """
        if not getattr(self, "_fp_rewrite_dirty", False):
            return
        if getattr(self, "_fp_rewrite_pending", False):
            return
        self._fp_rewrite_pending = True
        QTimer.singleShot(0, self._flush_footprint_rewrite)

    def _pipeline_owns_output(self) -> bool:
        """@brief Tell whether an analysis pipeline is between its steps and owns the output.

        @return True from the moment an extraction starts applying its result
            until the automatic generation it leads to has finished: result
            being applied (`_applying_extraction`, `_extract_finish_pending`),
            automatic generation still to start (`_auto_generation_params`),
            inventory review open (`_inventory_pause_active`) or automatic
            generation running (`_automatic_generation_active`).
        @note In that window the parameters on screen are fresh and not yet
            reviewed: only the pipeline may write the footprint, after the
            review and the overwrite confirmation.
        """
        return bool(getattr(self, "_automatic_generation_active", False)
                    or getattr(self, "_applying_extraction", False)
                    or getattr(self, "_extract_finish_pending", False)
                    or getattr(self, "_inventory_pause_active", False)
                    or getattr(self, "_auto_generation_params", None) is not None)

    def _flush_footprint_rewrite(self) -> None:
        """@brief Make the one attempt of the pending footprint-only rewrite when the window is idle.

        @note Busy (a worker, a modal review) or a pipeline between its steps
            (`_pipeline_owns_output`): the flag stays set and the next
            finished handler retries.
        @note Otherwise the flag is consumed BEFORE the attempt, whatever its
            outcome: a start that fails (invalid parameters, a thread that
            cannot start) is logged once by `_generate` and is not re-armed
            by the finished handler it triggers, which would loop once per
            event-loop turn. The edit stays saved in the project; the next
            generation carries it.
        @note Drawings and text positions rewrite only an existing .kicad_mod
            (no file yet: the first generation will carry them); a pad edit
            goes to `_generate`, which decides as it always did.
        """
        self._fp_rewrite_pending = False
        if not getattr(self, "_fp_rewrite_dirty", False):
            return
        if self._background_work_running() or self._pipeline_owns_output():
            return
        open_folder = getattr(self, "_open_project_folder", None)
        if open_folder is not None and self._named() and Path(open_folder) != self._project_dir():
            # The working folder changed under the open project: a rewrite
            # now would write into the other working folder. Kept pending;
            # leaving the project marks it stale (`_can_leave_project`).
            return
        pad_edit = self._fp_rewrite_pad_edit
        self._clear_footprint_rewrite()
        if not pad_edit and not self._project_has_footprint():
            return
        # The rewrite of APPLIED edits: a draft made since then is not asked
        # about, and not written (`_generate` works on the applied state).
        self._fp_rewrite_flushing = True
        try:
            self._generate(footprint_only=True)
        finally:
            self._fp_rewrite_flushing = False

    def _refresh_footprint_undo_buttons(self) -> None:
        """@brief Enable undo/redo, Applica/Annulla and «Recupera bozza» from the draft state."""
        if hasattr(self, "btn_fp_undo"):
            self.btn_fp_undo.setEnabled(bool(self._fp_drawings_undo))
            self.btn_fp_redo.setEnabled(bool(self._fp_drawings_redo))
        if hasattr(self, "btn_fp_apply"):
            pending = self.has_pending_footprint_edits()
            self.btn_fp_apply.setEnabled(pending)
            self.btn_fp_cancel.setEnabled(pending)
            offered = getattr(self, "_fp_recoverable", None) is not None
            self.btn_fp_recover.setEnabled(offered)
            self.action_fp_discard_recovery.setEnabled(offered)

    def _undo_footprint_drawing(self) -> None:
        """@brief Go back to the footprint edits before the last change (Ctrl+Z), inside the draft."""
        self._step_footprint_history(self._fp_drawings_undo, self._fp_drawings_redo)

    def _redo_footprint_drawing(self) -> None:
        """@brief Re-apply the last undone footprint edit (Ctrl+Y), inside the draft."""
        self._step_footprint_history(self._fp_drawings_redo, self._fp_drawings_undo)

    def _delete_hovered_drawing(self) -> None:
        """@brief Delete key: remove the drawing under the pointer, if any."""
        index = self._fp_hovered_drawing
        if 0 <= index < len(self.footprint_drawings):
            updated = deepcopy(self.footprint_drawings)
            del updated[index]
            self._apply_footprint_drawings(updated)

    def _duplicate_hovered_drawing(self) -> None:
        """@brief Ctrl+D: duplicate the drawing under the pointer, one grid step away."""
        index = self._fp_hovered_drawing
        if 0 <= index < len(self.footprint_drawings):
            self._duplicate_drawing(index)

    def _duplicate_drawing(self, index: int) -> None:
        """@brief Copy a drawing shifted by one grid step right and down (as the symbol does)."""
        from ..core.footprint import validate_footprint_drawings
        step_px = self.preview_view.grid_step() or 20.0
        step = step_px / 20.0
        copy = deepcopy(self.footprint_drawings[index])
        copy["points"] = [[round(x + step, 4), round(y + step, 4)] for x, y in copy["points"]]
        valid = validate_footprint_drawings([copy])
        if valid:
            self._apply_footprint_drawings(self.footprint_drawings + valid)

    def _set_drawing_field(self, index: int, field: str, value) -> None:
        """@brief Change one field (layer, width) of a drawing, keeping it valid."""
        from ..core.footprint import validate_footprint_drawings
        record = deepcopy(self.footprint_drawings[index])
        record[field] = value
        valid = validate_footprint_drawings([record])
        if not valid:
            return
        updated = deepcopy(self.footprint_drawings)
        updated[index] = valid[0]
        self._apply_footprint_drawings(updated)

    def _footprint_drawing_context_menu(self, index: int) -> None:
        """@brief Right click on a drawing: delete it, or edit a text's content."""
        if not 0 <= index < len(self.footprint_drawings):
            return
        record = self.footprint_drawings[index]
        menu = QMenu(self)
        # Same entries as the symbol editor's element menu, where they apply.
        duplicate_action = menu.addAction(self.i18n.text("Duplica (Ctrl+D)"))
        layer_menu = menu.addMenu(self.i18n.text("Layer"))
        layer_actions = {}
        for layer, label in self.FOOTPRINT_DRAW_LAYERS:
            action = layer_menu.addAction(_layer_swatch(_layer_color(layer)), self.i18n.text(label))
            action.setIconVisibleInMenu(True)
            action.setCheckable(True)
            action.setChecked(layer == record["layer"])
            layer_actions[action] = layer
        edit_action = width_action = None
        if record["kind"] == "text":
            menu.addSeparator()
            edit_action = menu.addAction(self.i18n.text("Modifica testo…"))
        else:
            width_action = menu.addAction(self.i18n.text("Spessore del tratto…"))
        menu.addSeparator()
        delete_action = menu.addAction(self.i18n.text("Elimina disegno"))
        chosen = menu.exec(QCursor.pos())
        if chosen is None:
            return
        if chosen is delete_action:
            updated = deepcopy(self.footprint_drawings)
            del updated[index]
            self._apply_footprint_drawings(updated)
        elif chosen is duplicate_action:
            self._duplicate_drawing(index)
        elif chosen in layer_actions:
            self._set_drawing_field(index, "layer", layer_actions[chosen])
        elif chosen is width_action:
            value, accepted = QInputDialog.getDouble(
                self, self.i18n.text("Spessore del tratto…"),
                self.i18n.text("Spessore del tratto in mm"),
                float(record.get("width", 0.12)), 0.01, 5.0, 3)
            if accepted:
                self._set_drawing_field(index, "width", round(float(value), 4))
        elif chosen is edit_action:
            text, accepted = QInputDialog.getText(
                self, self.i18n.text("Testo"), self.i18n.text("Testo da scrivere:"),
                text=record.get("text", ""))
            if not accepted or not text.strip() or "\n" in text or "\r" in text:
                return
            self._set_drawing_field(index, "text", text.strip())

    def _draw_footprint_drawings(self, mm_to_px: float) -> None:
        """@brief Draw the user drawings and the clicks in progress on the scene.

        @param mm_to_px Scene scale of the drawer that calls this.
        @note Called by the three 2D drawers after the geometry, like
            `_draw_footprint_texts`. Layer tells the colour: silkscreen like
            the generated silk, fabrication yellow, comments dimmed.
        """
        from .footprint_view import DRAWING_KEY_ROLE
        for index, record in enumerate(self.footprint_drawings):
            pen = QPen(QColor(_layer_color(record["layer"])))
            pen.setWidthF(max(1.0, record.get("width", 0.12) * mm_to_px))
            points = [(x * mm_to_px, y * mm_to_px) for x, y in record["points"]]
            kind = record["kind"]
            items = []
            if kind == "line":
                items = [self.preview_scene.addLine(*a, *b, pen)
                         for a, b in zip(points, points[1:])]
            elif kind == "rectangle":
                (x0, y0), (x1, y1) = points
                items = [self.preview_scene.addRect(min(x0, x1), min(y0, y1),
                                                    abs(x1 - x0), abs(y1 - y0), pen)]
            elif kind == "circle":
                (cx, cy), (rx, ry) = points
                radius = math.hypot(rx - cx, ry - cy)
                items = [self.preview_scene.addEllipse(cx - radius, cy - radius,
                                                       2 * radius, 2 * radius, pen)]
            elif kind == "arc":
                path = _arc_path(*points)
                if path is not None:
                    item = self.preview_scene.addPath(path, pen)
                    items = [item]
            elif kind == "text":
                # Anchored texts grow right and up from their point (its
                # handle sits on the bottom-left corner); legacy ones are
                # centred on it (see _drawing_text_position).
                text_item = _drawing_text_item(record, mm_to_px)
                self.preview_scene.addItem(text_item)
                text_item.setPos(*_drawing_text_position(record, mm_to_px, text_item))
                items = [text_item]
            hint = self.i18n.text("Disegno: tasto destro per eliminarlo o modificarlo.")
            for item in items:
                item.setData(DRAWING_KEY_ROLE, index)
                item.setZValue(6)
                item.setToolTip(hint)
                item.setCursor(Qt.CursorShape.SizeAllCursor)
        self._refresh_draw_markers()
        self._refresh_drawing_handles()

    def _snapped_draw_point(self, x: float, y: float) -> tuple:
        """@brief A scene point (px) snapped to the drawn grid, in footprint mm."""
        mm_to_px = 20.0
        sx, sy = self.preview_view.snap_to_grid(x, y, 0.1 * mm_to_px)
        return (round(sx / mm_to_px, 4), round(sy / mm_to_px, 4))

    def _on_footprint_draw_hovered(self, x: float, y: float) -> None:
        """@brief The pointer moved with a tool armed: snap marker and ghost follow it."""
        if self._fp_draw_tool is None:
            return
        point = self._snapped_draw_point(x, y)
        if point != self._fp_draw_hover:
            self._fp_draw_hover = point
            self._refresh_draw_markers()

    def _on_footprint_draw_hover_left(self) -> None:
        """@brief The pointer left the view: no snap marker, no ghost."""
        if self._fp_draw_hover is not None:
            self._fp_draw_hover = None
            self._refresh_draw_markers()

    def _ghost_path(self, points: list, tool: str | None = None, stored: bool = False):
        """@brief Dashed outline of a shape through `points` (mm).

        @param points The shape's points: clicked ones plus the hovered one
            while drawing, or a stored record's points with one moved.
        @param tool Shape kind; None means the armed tool.
        @param stored True when `points` are in the STORED order (an arc is
            start, mid, end); False for the click order (start, end, mid).
        @return A QPainterPath in scene px, or None when nothing can be
            outlined yet (a text, or a single point).
        """
        mm_to_px = 20.0
        tool = self._fp_draw_tool if tool is None else tool
        pts = [(x * mm_to_px, y * mm_to_px) for x, y in points]
        if tool == "text" or len(pts) < 2:
            return None
        path = QPainterPath()
        if tool == "line":
            path.moveTo(*pts[0])
            for x, y in pts[1:]:
                path.lineTo(x, y)
        elif tool == "rectangle":
            (x0, y0), (x1, y1) = pts[:2]
            path.addRect(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))
        elif tool == "circle":
            (cx, cy), (rx, ry) = pts[:2]
            radius = math.hypot(rx - cx, ry - cy)
            path.addEllipse(cx - radius, cy - radius, 2 * radius, 2 * radius)
        elif tool == "arc":
            if len(pts) < 3:
                path.moveTo(*pts[0])
                path.lineTo(*pts[1])
            else:
                if stored:
                    start, mid, end = pts[:3]
                else:
                    start, end, mid = pts[:3]   # click order: start, end, then mid
                path = _arc_path(start, mid, end)
        return path

    def _refresh_draw_markers(self) -> None:
        """@brief Show the clicked points, the snap node under the pointer and the ghost shape.

        @note Like the symbol editor (user, 2026-10-13: «fare vedere il punto
            di aggancio»): a dot on every clicked node, a brighter dot on the
            grid node the next click would take, and the dashed outline the
            shape would have if clicked there.
        """
        scene = getattr(self, "preview_scene", None)
        if scene is None:
            return
        for item in list(scene.items()):
            if item.data(0x44524D) is not None:
                scene.removeItem(item)
        if self._fp_draw_tool is None:
            return
        mm_to_px = 20.0
        # Same look as the symbol editor (user, 2026-10-13: «uniforma a quello
        # già fatto»): yellow dots on the clicked points, yellow dashed ghost.
        for x_mm, y_mm in self._fp_draw_clicks:
            _screen_dot(scene, x_mm * mm_to_px, y_mm * mm_to_px, 5, YELLOW, YELLOW, 0x44524D, 7)
        if self._fp_draw_hover is None:
            return
        hx, hy = (v * mm_to_px for v in self._fp_draw_hover)
        path = self._ghost_path(self._fp_draw_clicks + [self._fp_draw_hover])
        if path is not None:
            ghost = scene.addPath(path, _ghost_pen(_layer_color(self.combo_fp_draw_layer.currentData())))
            ghost.setData(0x44524D, True)
            ghost.setZValue(6)
        _screen_dot(scene, hx, hy, 7, YELLOW, YELLOW, 0x44524D, 8)

    def _refresh_drawing_handles(self) -> None:
        """@brief Show the handles of the hovered drawing, and the ghost of a handle drag.

        @note Like the symbol editor (user, 2026-10-13: «quando passo sopra il
            disegno devono apparire i punti di ancoraggio»): one square per
            stored point of the hovered drawing, draggable on the grid; the
            dragged one shows the dashed outline the shape will take.
        @note A text has one stored point, its anchor: the bottom-left corner
            of the text (user, 2026-10-14), so its square is drawn there.
        """
        from .footprint_view import HANDLE_KEY_ROLE
        scene = getattr(self, "preview_scene", None)
        if scene is None:
            return
        for item in list(scene.items()):
            if item.data(0x48474F) is not None:
                scene.removeItem(item)
        index = self._fp_hovered_drawing
        if not 0 <= index < len(self.footprint_drawings):
            return
        record = self.footprint_drawings[index]
        mm_to_px = 20.0
        points = [tuple(p) for p in record["points"]]
        drag = self._fp_handle_drag
        if drag is not None and drag[0] == index:
            if drag[1] < 0:
                dx, dy = drag[2]
                points = [(round(x + dx, 4), round(y + dy, 4)) for x, y in points]
            else:
                points[drag[1]] = drag[2]
            path = self._ghost_path(points, record["kind"], stored=True)
            if path is not None:
                ghost = scene.addPath(path, _ghost_pen(_layer_color(record["layer"])))
                ghost.setData(0x48474F, True)
                ghost.setZValue(8)
        # Same handles as the symbol editor: 6 px squares, yellow outline,
        # translucent green fill, constant screen size at any zoom. No extra
        # centre anchor: the whole drawing moves by its body, as an element
        # of the symbol editor (user, 2026-10-13: «più discreto»).
        hint = self.i18n.text("Punto del disegno: trascinalo sulla griglia.")
        for point_index, (x_mm, y_mm) in enumerate(points):
            handle = _screen_handle(scene, x_mm * mm_to_px, y_mm * mm_to_px, 6, solid=False)
            handle.setData(HANDLE_KEY_ROLE, f"{index}:{point_index}")
            handle.setToolTip(hint)

    def _on_footprint_drawing_hovered(self, index: int) -> None:
        """@brief The pointer entered (index >= 0) or left (-1) a drawing: handles follow."""
        if self._fp_handle_drag is not None:
            return
        self._fp_hovered_drawing = index
        self._refresh_drawing_handles()

    def _on_footprint_handle_pressed(self, index: int, point_index: int, x: float, y: float) -> None:
        """@brief A handle or a drawing body was grabbed: remember the grab node."""
        self._fp_handle_active = True
        self._fp_handle_cancelled = False
        self._fp_handle_origin = self._snapped_draw_point(x, y) if point_index < 0 else None
        if 0 <= index < len(self.footprint_drawings):
            self._fp_hovered_drawing = index
            self._refresh_drawing_handles()

    def _whole_drag_delta(self, x: float, y: float) -> tuple:
        """@brief Translation (mm) of a whole-drawing drag from its grab node to the pointer."""
        point = self._snapped_draw_point(x, y)
        origin = self._fp_handle_origin if self._fp_handle_origin is not None else point
        return (round(point[0] - origin[0], 4), round(point[1] - origin[1], 4))

    def _on_footprint_handle_dragged(self, index: int, point_index: int, x: float, y: float) -> None:
        """@brief A handle moves: snap it and show the shape it would give.

        @param point_index Point of the record, or -1 to move the whole drawing.
        """
        if self._fp_handle_cancelled:
            return      # the list changed under the drag: it was cancelled
        if not 0 <= index < len(self.footprint_drawings):
            return
        if point_index < 0:
            drag = (index, -1, self._whole_drag_delta(x, y))
        elif point_index < len(self.footprint_drawings[index]["points"]):
            drag = (index, point_index, self._snapped_draw_point(x, y))
        else:
            return
        if drag == self._fp_handle_drag:
            return      # pointer still within the same node: nothing to redraw
        self._fp_handle_drag = drag
        self._fp_hovered_drawing = index
        self._refresh_drawing_handles()

    def _on_footprint_handle_dropped(self, index: int, point_index: int, x: float, y: float) -> None:
        """@brief A handle was dropped: store the moved point if the shape stays valid.

        @note A drop whose drag was cancelled (the list changed while the
            button was down) is ignored: its indices refer to the old list.
        """
        from ..core.footprint import validate_footprint_drawings
        self._fp_handle_drag = None
        self._fp_handle_active = False
        if self._fp_handle_cancelled:
            self._fp_handle_cancelled = False
            return
        if not 0 <= index < len(self.footprint_drawings):
            return
        record = deepcopy(self.footprint_drawings[index])
        if point_index < 0:
            dx, dy = self._whole_drag_delta(x, y)
            self._fp_handle_origin = None
            if (dx, dy) == (0.0, 0.0):
                self._refresh_drawing_handles()
                return
            record["points"] = [[round(px + dx, 4), round(py + dy, 4)] for px, py in record["points"]]
        else:
            if not point_index < len(record["points"]):
                return
            point = self._snapped_draw_point(x, y)
            if list(point) == list(record["points"][point_index]):
                self._refresh_drawing_handles()
                return
            record["points"][point_index] = list(point)
        if not validate_footprint_drawings([record]):
            self._draw_feedback_toast(self.i18n.text("Spostamento scartato."),
                                      self._degenerate_drawing_text(record))
            self._refresh_drawing_handles()
            return
        updated = deepcopy(self.footprint_drawings)
        updated[index] = record
        self._apply_footprint_drawings(updated)
        self._refresh_drawing_handles()

    def _build_symbol_tab(self):
        from .symbol_tab import SymbolTab
        self.symbol_tab = SymbolTab(tx=self.i18n.text)
        self.symbol_tab.edits_applied.connect(self._apply_symbol_pinout)
        self.symbol_tab.export_requested.connect(self._export_electrical_symbol)
        self.symbol_tab.pin_types_ai_requested.connect(self._suggest_pin_types)
        return self.symbol_tab

    def _suggest_pin_types(self):
        from .pin_type_dialog import PinTypeDialog
        if self._background_work_running() or self._automatic_generation_active:
            QMessageBox.information(self, self.i18n.text('Suggerisci tipi pin con AI'),
                                    self.i18n.text('Attendi la fine del lavoro in corso.'))
            return
        try:
            paths, roles = self._selected_documents()
            documents = [p for p in paths if set(roles.get(p.name, ())) & {'pinout', 'datasheet'}]
            if not documents:
                raise ValueError(self.i18n.text('Seleziona un documento pinout o datasheet nel progetto.'))
        except (ValueError, OSError, RuntimeError) as exc:
            QMessageBox.warning(self, self.i18n.text('Suggerisci tipi pin con AI'), str(exc))
            return
        if not self._ensure_api_key():
            return
        snapshot = self.symbol_tab.draft_rows()
        dialog = PinTypeDialog(dict(name=self._component_name(), rows=snapshot,
                                   documents=documents, model=self._vision_model()), self, tx=self.i18n.text)
        account = getattr(self, 'ai_account', None)
        if account:
            project = self._project_dir_or_none()
            dialog.analysis_started.connect(lambda: account.bind_run_project(project))
            dialog.analysis_finished.connect(account.release_run_project)
        self._component_review_dialog = dialog
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted and snapshot == self.symbol_tab.draft_rows():
                self.symbol_tab.use_pin_type_suggestions(dialog.selected())
        finally:
            self._component_review_dialog = None
            self._retry_footprint_rewrite()

    def _symbol_pad_ids(self):
        """Electrical identities come from the actual footprint, never row indices."""
        params = (self.package_geometry if self.package_geometry is not None else
                  self._current_castellated_params() if self._is_castellated()
                  else self._current_params())
        params = self._host_footprint_params(params)
        footprint = (build_package(params) if isinstance(params, PackageGeometry) else
                     build_castellated_module(params) if isinstance(params, CastellatedModuleParams)
                     else build_pin_header(params))
        return list(dict.fromkeys(str(p.number) for p in footprint.pads
                                 if p.pad_type != 'np_thru_hole' and str(p.number).strip()))

    def _symbol_recovery_path(self) -> Path | None:
        """@brief The symbol recovery file of the OPEN project.

        @return `<open project>/.symbol-draft.json`, or None while the
            component has no name (no project folder to bind).
        @note `_open_project_folder`, not `_project_dir()`: during the leave
            question of a working-folder change `_project_dir()` already names
            the other working folder, and showing the symbol tab (the question
            shows the editor concerned) would rebind the editor there, which
            drops the draft from the editor before the user answers.
        """
        folder = getattr(self, "_open_project_folder", None)
        if folder is None:
            folder = self._project_dir_or_none()
        return None if folder is None else Path(folder) / '.symbol-draft.json'

    def _refresh_symbol_tab(self, force=False):
        if not hasattr(self, 'symbol_tab'):
            return
        # The Reference text of the editor («U?») follows the designator the
        # files get (user, 2026-10-13: both visible properties are drawn
        # where KiCad will draw them, and can be moved).
        self.symbol_tab.set_reference(self._effective_reference())
        try:
            ids = self._symbol_pad_ids()
        except (ValueError, TypeError) as exc:
            self.symbol_tab.set_data(self._component_name(), self.pinout, pad_ids=[], force=force)
            # Bind the recovery file on this path too: set_data only shows a
            # carried draft, set_recovery_path settles it (keeps it durable
            # and offerable); without it a kept draft could be lost.
            recovery = self._symbol_recovery_path()
            if recovery is not None:  # no component name yet: no project folder to bind
                self.symbol_tab.set_recovery_path(recovery)
            self.symbol_tab.set_error(str(exc))
            return
        rows = deepcopy(self.pinout)
        existing = {str(row.get('pin', '')).strip() for row in rows}
        rows.extend({'pin': number, 'name': ''} for number in ids if number not in existing)
        self.symbol_tab.set_data(self._component_name(), rows,
            footprint=f'KiCadGen:{self._component_name()}', pad_ids=ids, force=force)
        recovery = self._symbol_recovery_path()
        if recovery is not None:  # as above: an unnamed project has no folder
            self.symbol_tab.set_recovery_path(recovery)

    def _apply_symbol_pinout(self, rows):
        """@brief «Applica» of the symbol editor: save the rows in the project and the .kicad_sym.

        @param rows The applied symbol rows.
        @note `_symbol_apply_folder` is the open project's folder while the
            leave question of a working-folder change applies the draft
            (`_resolve_pending_edits`): `_project_dir()` then names the other
            working folder, so the project file and the .kicad_sym are
            written explicitly into the open project, never under the new
            working folder. A refused write keeps the draft (`reject_apply`).
        """
        folder = getattr(self, "_symbol_apply_folder", None)
        previous = self.pinout
        self.pinout = deepcopy(rows)
        saved = (self._save_project_config(force=True) if folder is None
                 else self._write_project_config_at(folder))
        if not saved:
            self.pinout = previous
            self.symbol_tab.reject_apply(self.i18n.text(
                "Pinout non salvato. Le modifiche non sono state applicate."))
            return
        self._draw_preview()
        self._sync_3d_pin_functions()
        self.symbol_tab.accept_apply()
        self._save_applied_electrical_symbol(
            None if folder is None else Path(folder) / f'{self._component_name()}.kicad_sym',
            quiet=True)

    def _symbol_pad_mismatch(self) -> str:
        """@brief Why the applied symbol rows do not match the footprint pads, '' when they do.

        @return The message to show, or '' when every electrical pad has exactly
            one row and no row is without a pad.
        """
        try:
            ids = self._symbol_pad_ids()
        except (ValueError, TypeError) as exc:
            return str(exc)
        numbers = [str(row.get('pin', '')).strip() for row in self.symbol_tab.rows()]
        if len(set(numbers)) != len(numbers) or set(numbers) != set(ids) or not ids:
            return self.i18n.text(
                "I numeri del simbolo devono corrispondere ai pad elettrici del footprint.")
        return ""

    def _save_applied_electrical_symbol(self, path=None, *, quiet=False):
        """@brief Write the applied electrical symbol beside the component artifacts.

        @param path Target file; None means `<project>/<name>.kicad_sym`.
        @param quiet True logs a failure instead of showing it in the symbol tab.
        @return The written path, or None when nothing was written.
        """
        from ..generators.symbol import symbol_inputs, export_symbol
        if self.symbol_tab.has_pending_edits():
            return None
        try:
            rows = self.symbol_tab.rows()
            mismatch = self._symbol_pad_mismatch()
            if mismatch:
                raise ValueError(mismatch)
            pins, body = symbol_inputs(rows)
            from ..generators.symbol import symbol_property_positions
            from ..generators.symbol_glyphs import symbol_graphics
            graphics = symbol_graphics(rows)
            properties = symbol_property_positions(rows)
            target = Path(path) if path else (
                self._project_dir() / f'{self._component_name()}.kicad_sym')
            return export_symbol(target, self._component_name(), pins,
                                 footprint=self.symbol_tab.footprint(), body=body, graphics=graphics,
                                 metadata=self._symbol_metadata(), properties=properties)
        except (OSError, ValueError, TypeError) as exc:
            # Generator errors carry a translatable text (SymbolError).
            message = self.symbol_tab.error_text(exc)
            if not quiet:
                self.symbol_tab.set_status(message)
            else:
                self._append_log(self.i18n.text(
                    "Simbolo elettrico non salvato automaticamente: {error}").format(error=message))
            return None

    def _export_electrical_symbol(self):
        # Keep direct invocations as safe as the disabled toolbar button: an
        # export must never silently use older geometry than the visible draft.
        if self.symbol_tab.has_pending_edits():
            self.symbol_tab.set_status(self.i18n.text("Modifiche non applicate."))
            return
        mismatch = self._symbol_pad_mismatch()
        if mismatch:
            # Checked BEFORE the file dialog: the physical mapping may have
            # changed since the rows were applied (pads regenerated), and a
            # dialog for a file that will be refused is a trap.
            self.symbol_tab.set_status(mismatch)
            return
        try:
            if not self.symbol_tab.confirm_export_review():
                return
            path, _ = QFileDialog.getSaveFileName(self,
                self.i18n.text("Esporta simbolo KiCad"),
                str(self._project_dir() / f'{self._component_name()}.kicad_sym'),
                'KiCad (*.kicad_sym)')
            if not path:
                return
            if not path.lower().endswith('.kicad_sym'):
                path += '.kicad_sym'
            exported = self._save_applied_electrical_symbol(path)
            if exported is None:
                return
        except (OSError, ValueError, TypeError) as exc:
            self.symbol_tab.set_status(self.symbol_tab.error_text(exc))
            return
        path = str(exported)
        message = self.i18n.text("Simbolo esportato: {path}").format(path=path)
        if record_kicad_export(component=self._component_name(), kind="File simbolo",
                               source=str(self._project_dir()), destination=str(Path(path))):
            self._refresh_kicad_export_history()
        else:
            self._append_log(self.i18n.text(
                "Simbolo esportato, ma non è stato possibile aggiornare lo storico KiCad."))
        self._refresh_output_actions()
        self.symbol_tab.set_status(message)
        self._append_log(message)

    def _show_pinout_dialog(self) -> None:
        dialog = PinoutDialog(self.pinout, self._component_name(), self)
        self.i18n.retranslate_widgets(dialog)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        edited = dialog.edited_pinout()
        if edited == self.pinout:
            return
        previous = self.pinout
        self.pinout = edited
        if not self._save_project_config(force=True):
            self.pinout = previous
            QMessageBox.warning(self, self.i18n.text("Errore"), self.i18n.text(
                "Pinout non salvato. Le modifiche non sono state applicate."))
            return
        self._draw_preview()
        self._sync_3d_pin_functions()
        self._refresh_symbol_tab()

    # ── Tab 5: Modello 3D ──────────────────────────────────────────

    def _build_3d_tab(self) -> QWidget:
        """@brief Build the 3D tab: one icon toolbar, the preview, the status line.

        @return The tab container (toolbar above the preview).
        @note User choice (2026-09-23, note 6): the workflow commands that sat in
            a row under the model moved into the single toolbar at the top, as
            icon-only buttons after the view tools and a vertical divider. The
            preview gains the height of the old row; names and explanations
            live in the translated tooltips and accessible names.
        """
        # Only the settings scroll. The renderer must occupy its actual visible
        # viewport, not a clipped portion of a taller scrollable document.
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(*(CONTENT_MARGIN_PX,) * 4)
        outer.setSpacing(8)

        # Una sola barra di icone (scelta dell'utente, 2026-09-23): a sinistra
        # gli strumenti di vista, poi un divisore, poi i comandi del flusso
        # di lavoro. Niente testo fisso sopra il modello: i nomi stanno nei
        # tooltip, tradotti come ogni altro testo dell'interfaccia. Il widget
        # conserva il nome `model_3d_action_bar`: e' ancora la barra delle
        # azioni della scheda, solo spostata in alto.
        self.model_3d_action_bar = QWidget()
        toolbar = QHBoxLayout(self.model_3d_action_bar)
        # Stessi margini laterali della scheda sotto: le icone restano in
        # colonna con il bordo del modello.
        toolbar.setContentsMargins(10, 8, 10, 0)
        toolbar.setSpacing(4)
        self.chk_pin_numbers = self._view_tool_button(
            "pin-numbers.svg",
            "Numeri pin: compaiono solo guardando il componente dal lato dei piedini "
            "(clic su SOTTO nel cubo, o ruota la vista sotto il piano della scheda).",
            checkable=True)
        self.chk_pin_numbers.setChecked(bool(self._config.get("show_pin_numbers_3d", True)))
        self.chk_pin_numbers.toggled.connect(self._on_pin_numbers_toggled)
        toolbar.addWidget(self.chk_pin_numbers)
        # Sempre disponibile: nella barra un'icona che compare e scompare
        # sposterebbe le altre sotto il mouse.
        self.btn_pinout_3d = self._view_tool_button(
            "pinout-table.svg", "Tabella pinout: numero, nome e funzione di ogni piedino.")
        self.btn_pinout_3d.clicked.connect(self._show_pinout_dialog)
        toolbar.addWidget(self.btn_pinout_3d)
        # «Inquadra» sta accanto al cubo di navigazione, con gli altri comandi
        # di vista (utente, 27/09), non piu' nella barra.
        self.btn_open_3dshapes = self._view_tool_button(
            "folder-3d.svg", "Apri la cartella 3dshapes con i file STEP generati.")
        self.btn_open_3dshapes.clicked.connect(self._open_3dshapes_dir)
        toolbar.addWidget(self.btn_open_3dshapes)

        # Divisore verticale fra la vista (non cambia nulla) e il flusso di
        # lavoro (rigenera, accetta, torna alla revisione).
        # Lo stesso divisore delle barre delle revisioni: una barra uniforme.
        from .review_tools import toolbar_divider
        self.toolbar_3d_divider = toolbar_divider()
        toolbar.addWidget(self.toolbar_3d_divider)

        # Comando principale della scheda: l'ultimo passo gratuito della pipeline.
        self.btn_regen_3d = self._workflow_tool_button(
            "wf-regenerate.svg",
            "Rigenera modello 3D (STEP) con queste annotazioni",
            "Ultimo passo della pipeline — Ricostruisce lo STEP con FreeCAD dai corpi e "
            "dalle annotazioni attuali, comprese le correzioni appena applicate nella "
            "revisione dei componenti. Nessuna richiesta AI: gratuito.")
        # Nessuna enfasi propria (utente, 2026-09-24: «deve essere una cosa
        # uniforme»): la cornice blu fissa lo faceva sembrare un interruttore
        # acceso. Si distingue per la posizione, ultimo della barra.
        self.btn_regen_3d.clicked.connect(self._regenerate_3d)
        # Aggiunto alla barra PER ULTIMO, dopo il tipo di contatto (scelta
        # dell'utente, 2026-09-24): prima si correggono corpi e contatti, poi
        # si rigenera, e la barra si legge nello stesso ordine.
        # «Accetto, mi piace»: dopo aver guardato il solido, i modelli KiCad
        # agganciati diventano abbinamenti accettati che le schede future
        # riusano subito (come proposta da confermare). Acceso solo quando
        # c'e' un solido generato sui dati correnti: si accetta cio' che si e'
        # visto, non dati cambiati dopo.
        self.btn_accept_models = self._workflow_tool_button(
            "wf-accept.svg",
            "Accetto, mi piace — ricorda questi modelli…",
            "Salva nell'archivio degli abbinamenti accettati i modelli KiCad dei componenti "
            "di questa scheda (categoria, misure, numero di terminali, modello, rotazione "
            "scelta). In una scheda futura un corpo con la stessa firma riceve subito quel "
            "modello, senza la misura dei candidati, come proposta da confermare. "
            "Nessuna richiesta AI: gratuito.")
        self.btn_accept_models.clicked.connect(self._accept_models)
        self.btn_accept_models.setEnabled(False)
        toolbar.addWidget(self.btn_accept_models)
        # Accanto a chi lo riempie, l'archivio si rivede (utente, 2026-09-24):
        # prima stava solo nel menu Configurazione, che resta. Sempre attivo:
        # consultare l'archivio non chiede un solido generato.
        self.btn_accepted_archive = self._workflow_tool_button(
            "wf-archive.svg",
            "Archivio abbinamenti accettati…",
            "Rivedi i modelli KiCad ricordati con «Accetto, mi piace» ed elimina quelli "
            "sbagliati. In cima, evidenziate, le voci che valgono per i componenti di "
            "questa scheda. Nessuna richiesta AI: gratuito.")
        self.btn_accepted_archive.clicked.connect(self._show_accepted_models_for_board)
        toolbar.addWidget(self.btn_accepted_archive)
        # Copie dei pulsanti della scheda Progetto: da qui si vede il solido
        # e si capisce se mancano corpi, quindi da qui si deve poter tornare
        # alle proposte annotate e ripartire (revisione gratuita; rilancio a
        # pagamento, dichiarato -- anche dall'icona, che porta una moneta).
        # Stesso stato delle copie in Progetto.
        # UNA icona per la revisione (utente, 2026-09-30, HANDOFF 162): e'
        # guardando il solido che si scopre un corpo sbagliato, e la stessa
        # finestra mostra proposte del rilevatore o inventario AI a seconda
        # dello stadio raggiunto.
        self.btn_component_review_3d = self._mirror_project_action(
            self.btn_component_review, self._open_component_review, toolbar,
            "wf-review.svg")
        self.btn_relaunch_inventory_3d = self._mirror_project_action(
            self.btn_relaunch_inventory, self._relaunch_inventory, toolbar,
            "wf-relaunch-ai.svg")
        # Modifica a mano dei corpi (utente, 2026-09-26): la tabella sotto il
        # 3D e' stata tolta, i dati no. Serve anche senza la foto in memoria,
        # che la revisione fotografica invece richiede. Il suo pulsante nella
        # barra e' stato tolto (utente, 2026-09-29, HANDOFF 152): l'editor si
        # apre con Ctrl+E (`_shortcut_edit_components`) o dal menu del tasto
        # destro sul corpo nella vista 3D («Modifica componente...»).
        # «Copia formato» (utente, 2026-09-27): accanto al tipo di contatto,
        # dove l'utente l'ha indicata. Interruttore: acceso copia e incolla,
        # spento rigenera una volta sola.
        self.btn_format_painter_3d = self._workflow_tool_button(
            "format-painter.svg", "Copia formato",
            "Accendi, poi clic sul componente giusto per copiarne misure, altezza, "
            "tipologia, package e modello KiCad; clic sugli altri componenti per "
            "incollarli. Posizione, orientamento, nome, sigla e colore restano quelli "
            "di ciascuno. Spegni per rigenerare il modello 3D: FreeCAD, nessuna "
            "richiesta AI.")
        self.btn_format_painter_3d.setCheckable(True)
        self.btn_format_painter_3d.toggled.connect(self._on_format_painter_toggled)
        toolbar.addWidget(self.btn_format_painter_3d)
        self._format_source = None
        #: Table rows of the copied body and of the pasted ones, for the
        #: outlines in the 3D view (HANDOFF 158).
        self._format_source_row = None
        self._format_target_rows = []
        self._format_targets = []
        self._refresh_components_editor_button()

        # Il tipo di contatto va correggibile da qui: e' guardando il solido
        # che si vede un mezzo foro dove la scheda ha un foro passante, e la
        # tabella dei lati sta in un tab che non si mostra. Subito prima di «Rigenera»
        # perche' la correzione e la rigenerazione gratuita sono un unico
        # gesto. L'etichetta testuale e' diventata un'icona con tooltip.
        self.contact_style_bar_3d = QWidget()
        style_layout = QHBoxLayout(self.contact_style_bar_3d)
        style_layout.setContentsMargins(6, 0, 0, 0)
        style_layout.setSpacing(4)
        self.icon_contact_style_3d = QLabel()
        self.icon_contact_style_3d.setPixmap(
            QIcon(str(Path(__file__).parent / "assets" / "wf-contact.svg")).pixmap(QSize(20, 20)))
        self.icon_contact_style_3d.setToolTip("Tipo contatto (tutti i lati)")
        self.icon_contact_style_3d.setAccessibleName("Tipo contatto (tutti i lati)")
        style_layout.addWidget(self.icon_contact_style_3d)
        self.combo_contact_style_3d = self._contact_style_combo(default_contact_style())
        self.combo_contact_style_3d.addItem(self.i18n.text(CONTACT_STYLE_MIXED_LABEL), None)
        self.combo_contact_style_3d.addItem(self.i18n.text(CONTACT_STYLE_UNKNOWN_LABEL), "")
        # Solo informative: si sceglie un tipo per tutti i lati; «diverso» e
        # «da scegliere» descrivono lo stato, non sono una scelta.
        for index in (self.combo_contact_style_3d.count() - 2,
                      self.combo_contact_style_3d.count() - 1):
            self.combo_contact_style_3d.model().item(index).setEnabled(False)
        self._set_translated_tooltip(
            self.combo_contact_style_3d,
            CONTACT_STYLE_TOOLTIP + "\nVale per tutti i lati.")
        self.combo_contact_style_3d.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.combo_contact_style_3d.currentIndexChanged.connect(
            self._on_contact_style_all_changed)
        style_layout.addWidget(self.combo_contact_style_3d)
        toolbar.addWidget(self.contact_style_bar_3d)
        # Nascosta finche' il modulo non e' castellated con almeno un lato:
        # la accende `_sync_contact_style_bar`, che la tiene allineata ai lati.
        self.contact_style_bar_3d.hide()
        self.chk_header_pins_3d = QCheckBox("Mostra gli header nel modello 3D")
        self.chk_header_pins_3d.setToolTip(self.chk_header_pins.toolTip())
        self.chk_header_pins_3d.toggled.connect(self.chk_header_pins.setChecked)
        toolbar.addWidget(self.chk_header_pins_3d)
        # Componente singolo (utente, 2026-10-10): scritta sulla faccia
        # superiore e numeri dei pin sulla faccia inferiore, accanto ai
        # reofori. Serigrafia del solido, non geometria: basta rigenerare il 3D.
        self.top_text_bar_3d = QWidget()
        top_text_layout = QHBoxLayout(self.top_text_bar_3d)
        top_text_layout.setContentsMargins(0, 0, 0, 0)
        top_text_layout.addWidget(QLabel("Scritta sopra:"))
        self.field_top_text_3d = QLineEdit()
        self.field_top_text_3d.setPlaceholderText("nessuna scritta")
        self.field_top_text_3d.setToolTip(
            "Testo stampato al centro della faccia superiore del componente, come una "
            "serigrafia. Vuoto: nessuna scritta. Si salva con il progetto; rigenera il 3D "
            "per vederla.")
        self.field_top_text_3d.setMaximumWidth(220)
        self.field_top_text_3d.editingFinished.connect(self._on_top_text_edited)
        top_text_layout.addWidget(self.field_top_text_3d)
        toolbar.addWidget(self.top_text_bar_3d)
        self.chk_pin_numbers_3d = QCheckBox("Numeri dei pin sotto")
        self.chk_pin_numbers_3d.setToolTip(
            "Stampa il numero di ogni pin sulla faccia inferiore del componente, accanto al "
            "reoforo, leggibile guardando da sotto. Non cambia il footprint: basta rigenerare "
            "il 3D.")
        self.chk_pin_numbers_3d.toggled.connect(self._on_pin_numbers_3d_toggled)
        toolbar.addWidget(self.chk_pin_numbers_3d)
        self._sync_header_pins_3d()
        toolbar.addWidget(self.btn_regen_3d)

        self.btn_terminal_detail = QPushButton("Rileva terminali dalla foto...")
        self.btn_terminal_detail.setToolTip(
            "Analisi AI della foto calibrata tramite OpenRouter; conserva posizioni e footprint.")
        self.btn_terminal_detail.clicked.connect(self._refine_photo_terminals)
        toolbar.addWidget(self.btn_terminal_detail)
        # User decision 2026-09-13 (MEMORY.md): hide manual AI refinement
        # actions, keeping handlers and the automatic extraction unchanged.
        self.btn_terminal_detail.hide()
        # Stessa decisione (MEMORY.md, 2026-09-13): nascosto, non rimosso. Stava
        # sotto la tabella componenti, tolta il 2026-09-26 con il suo pannello.
        self.btn_lead_refinement = QPushButton("Affina reofori / saldature...")
        self.btn_lead_refinement.setToolTip(
            "Passaggio AI facoltativo: conserva corpi e conteggio, separa solo i reofori distinguibili.")
        self.btn_lead_refinement.clicked.connect(self._refine_photo_leads)
        toolbar.addWidget(self.btn_lead_refinement)
        self.btn_lead_refinement.hide()
        toolbar.addStretch()

        # Indicatore di caricamento (nota 10 dell'utente, 2026-09-23): entrare
        # nella scheda con una mesh grande bloccava la finestra per secondi
        # senza alcun segno. Sta nella barra, a destra, e non sopra il
        # disegno: il visore VTK e' una superficie OpenGL nativa su cui un
        # widget figlio non si compone in modo affidabile, e un banner
        # inserito sopra l'anteprima la ridimensionerebbe proprio mentre la si
        # disegna. Il cursore di attesa copre l'area del modello.
        self.loading_3d_indicator = QWidget()
        loading_layout = QHBoxLayout(self.loading_3d_indicator)
        loading_layout.setContentsMargins(0, 0, 0, 0)
        loading_layout.setSpacing(4)
        loading_icon = QLabel()
        loading_icon.setPixmap(
            QIcon(str(Path(__file__).parent / "assets" / "wf-loading.svg")).pixmap(QSize(18, 18)))
        loading_layout.addWidget(loading_icon)
        self.label_3d_loading = QLabel("Caricamento del modello 3D…")
        self.label_3d_loading.setStyleSheet(f"color: {BLUE}; font-weight: bold;")
        loading_layout.addWidget(self.label_3d_loading)
        toolbar.addWidget(self.loading_3d_indicator)
        self.loading_3d_indicator.hide()
        # Adesso le copie hanno un genitore: prima di qui `setVisible(True)`
        # avrebbe aperto finestre a se' stanti.
        self._apply_project_action_mirror_visibility()

        # The viewer is built now only when its class is already at hand (a
        # test stub, or the module imported earlier); otherwise the tab
        # starts with a placeholder and the viewer arrives with
        # `_ensure_preview_3d` (first entry in the tab, or the preload).
        self._preview_3d_layout = outer
        self._preview_3d_preloader = None
        #: Reason of a failed import of the viewer's libraries; set once, after
        #: which no entry in the 3D tab tries again (review, 2026-10-12).
        self._preview_3d_import_error = None
        self.preview_3d = Preview3DVtk() if Preview3DVtk is not None else Preview3DPending()
        self._wire_preview_3d()
        self.preview_3d.setMinimumHeight(120)
        outer.addWidget(self.preview_3d, 1)
        # Le viste standard si scelgono dal cubo dentro l'anteprima, che ha
        # preso il posto della riga «Vista: Iso, Sopra, …».
        self._apply_view_cube_labels()

        # Riga di stato del 3D: visibile SOLO per errori e avvisi (scelta
        # dell'utente). Gli esiti normali restano nel testo -- e nel log -- ma
        # non occupano lo spazio sotto il modello. Vedi `_set_3d_status`.
        self.label_3d_status = QLabel()
        self.label_3d_status.setWordWrap(True)
        outer.addWidget(self.label_3d_status)
        self._set_3d_status(*self._no_3d_status())

        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(0)
        # La barra sta sopra l'anteprima: e' la prima cosa che l'occhio incontra.
        container_layout.addWidget(self.model_3d_action_bar)
        container_layout.addWidget(tab, 1)
        self.model_3d_page = container
        self._refresh_workflow_tooltips()
        return container

    def _view_tool_button(self, asset: str, tooltip: str, *, checkable: bool = False):
        """@brief Icon-only button of the 3D view toolbar.

        @param asset SVG file in `gui/assets`.
        @param tooltip Italian source text; the i18n pass translates it.
        @param checkable True for a toggle (e.g. pin numbers).
        @return The configured QToolButton.
        @note Icon-only, so the tooltip is also the accessible name: a screen
            reader and the i18n catalogue both see the same text.
        """
        button = QToolButton()
        button.setIcon(QIcon(str(Path(__file__).parent / "assets" / asset)))
        button.setIconSize(QSize(22, 22))
        button.setAutoRaise(True)
        button.setCheckable(checkable)
        button.setToolTip(tooltip)
        button.setProperty("i18n_accessible_name_source", tooltip)
        button.setAccessibleName(tooltip)
        return button

    @staticmethod
    def _asset_icon(asset: str) -> QIcon:
        """@brief Icon loaded from `gui/assets`.

        @param asset SVG file name.
        @return The QIcon (null when the file is missing).
        """
        return QIcon(str(Path(__file__).parent / "assets" / asset))

    def _icon_only_button(self, asset: str) -> QToolButton:
        """@brief Bare icon-only button of the 3D toolbar, without texts yet.

        @param asset SVG file in `gui/assets`.
        @return The QToolButton, marked `i18n_skip`.
        @note Its tooltip is COMPOSED (name + explanation) by
            `_refresh_workflow_tooltips`, which translates each half from the
            catalogue. The generic pass `I18n.retranslate_widgets` would take
            the composed text as a catalogue key it cannot find, so the button
            opts out of it and is refreshed explicitly on a language change.
        """
        button = QToolButton()
        button.setIcon(self._asset_icon(asset))
        button.setIconSize(QSize(22, 22))
        button.setAutoRaise(True)
        button.setProperty("i18n_skip", True)
        return button

    def _workflow_tool_button(self, asset: str, title: str, detail: str) -> QToolButton:
        """@brief Icon-only workflow command of the 3D toolbar.

        @param asset SVG file in `gui/assets`.
        @param title Italian source of the command name (the former button text).
        @param detail Italian source of the explanation (the former tooltip).
        @return The QToolButton; its tooltip and accessible name follow the
            interface language through `_refresh_workflow_tooltips`.
        """
        button = self._icon_only_button(asset)
        button.setProperty("workflow_icon", asset)
        button.setProperty("workflow_idle_title_source", title)
        button.setProperty("workflow_title_source", title)
        button.setProperty("workflow_detail_source", detail)
        self._workflow_buttons.append(button)
        self._apply_icon_tooltip(button, self.i18n.text(title), self.i18n.text(detail))
        return button

    @staticmethod
    def _apply_icon_tooltip(button, title: str, detail: str) -> None:
        """@brief Put a command's name and explanation on an icon-only button.

        @param button Target button.
        @param title Translated command name: first tooltip line and accessible name.
        @param detail Translated explanation; may be empty.
        @note The name leads the tooltip because the icon alone does not say
            it; the accessible name is the name only, as a screen reader
            expects, and the explanation becomes the accessible description.
        """
        button.setToolTip(f"{title}\n{detail}" if detail else title)
        button.setAccessibleName(title)
        button.setAccessibleDescription(detail or "")

    def _refresh_workflow_tooltips(self) -> None:
        """@brief Recompose the tooltips of every icon-only workflow command.

        @note Own commands (regenerate, accept) translate their stored Italian
            sources; the copies of the Project-tab commands read the ORIGINAL
            button's current text and tooltip, which the i18n pass has already
            translated, so a copy can never say something its original does not.
        """
        for button in self._workflow_buttons:
            self._apply_icon_tooltip(
                button,
                self.i18n.text(button.property("workflow_title_source") or ""),
                self.i18n.text(button.property("workflow_detail_source") or ""))
        for mirror, source in self._project_action_mirrors:
            self._apply_icon_tooltip(mirror, source.text(), source.toolTip())

    def _set_workflow_detail(self, button, detail: str) -> None:
        """@brief Change the explanation line of an icon-only command, keeping its name.

        @param button Button made by `_workflow_tool_button`.
        @param detail Italian source of the new explanation (or disabled reason).
        @note Stored as the source, so a later language switch translates it
            again (`_refresh_workflow_tooltips`).
        """
        button.setProperty("workflow_detail_source", detail)
        self._apply_icon_tooltip(
            button, self.i18n.text(button.property("workflow_title_source") or ""),
            self.i18n.text(detail) if detail else "")

    def _set_regen_busy(self, busy: bool) -> None:
        """@brief Show on the icon-only «Rigenera» that a generation is running.

        @param busy True while FreeCAD builds the STEP.
        @note The old text button changed its label to «Generazione in corso
            (FreeCAD)...». An icon has no label, so the busy state is carried
            by three signs together: the button is disabled, the icon becomes
            the hourglass, and the tooltip/accessible name read «Generazione in
            corso (FreeCAD)...». The progress modal is open meanwhile anyway.
        """
        button = self.btn_regen_3d
        unsupported = False
        button.setEnabled(not busy)
        button.setProperty("workflow_busy", bool(busy))
        self._sync_header_pins_3d()
        button.setProperty(
            "workflow_title_source",
            "Generazione in corso (FreeCAD)..." if busy
            else button.property("workflow_idle_title_source"))
        button.setIcon(self._asset_icon(
            "wf-loading.svg" if busy else button.property("workflow_icon")))
        self._refresh_workflow_tooltips()

    def _set_translated_tooltip(self, widget, source: str) -> None:
        """@brief Set a translated tooltip that later language switches keep translating.

        @param widget Target widget.
        @param source Italian catalogue source.
        @note Writing `setToolTip(self.i18n.text(source))` alone makes
            `retranslate_widgets` capture the TRANSLATED text as the source on
            its first pass, so the next language switch looks up an English
            sentence in an Italian-keyed catalogue and leaves it unchanged.
            Recording the source and what was rendered keeps the pass exact.
        """
        rendered = self.i18n.text(source)
        widget.setToolTip(rendered)
        widget.setProperty("i18n_tooltip_source", source)
        widget.setProperty("i18n_tooltip_rendered", rendered)

    def _wire_preview_3d(self) -> None:
        """@brief Connect the viewer's optional signals and copy the pin-number switch.

        @note Every signal is optional: the placeholder and the test stubs
            have none, older viewers only some.
        """
        viewer = self.preview_3d
        if hasattr(viewer, 'component_requested'):
            viewer.component_requested.connect(
                lambda row: QTimer.singleShot(0, lambda: self._show_photo_comparison(component_row=row)))
        if hasattr(viewer, 'component_menu_requested'):
            viewer.component_menu_requested.connect(self._show_component_menu)
        if hasattr(viewer, 'component_clicked'):
            viewer.component_clicked.connect(self._on_format_painter_click)
        if hasattr(viewer, 'mesh_prepared'):
            viewer.mesh_prepared.connect(self._on_mesh_prepared)
            viewer.mesh_prepare_failed.connect(self._on_mesh_prepare_failed)
        viewer.show_pin_numbers = self.chk_pin_numbers.isChecked()

    def preview_3d_loaded(self) -> bool:
        """@brief True when the real viewer (or a test stub) is in the 3D tab, not the placeholder."""
        return not isinstance(self.preview_3d, Preview3DPending)

    def _ensure_preview_3d(self) -> bool:
        """@brief Put the real viewer in the 3D tab if the placeholder is still there.

        @return True when a viewer is in place (already, or built now); False
            when its libraries cannot be imported: the placeholder stays, the
            3D status line says so and the log has the reason.
        @note Synchronous: the import takes seconds the first time, which is
            why `schedule_preview_3d_preload` tries to do it beforehand.
        """
        if self.preview_3d_loaded():
            return True
        if self._preview_3d_import_error is not None:
            self._set_3d_status(self.i18n.text("Visualizzatore 3D non disponibile: vedi il log."), STATUS_ERROR)
            return False
        try:
            viewer_class = _preview_3d_class()
        except Exception as exc:  # noqa: BLE001 - a broken VTK/pyvista raises anything at import
            self._note_preview_3d_import_error(str(exc))
            return False
        self._install_preview_3d(viewer_class())
        return True

    def _note_preview_3d_import_error(self, reason: str) -> None:
        """@brief Record that the viewer's libraries cannot be imported: logged once, never retried.

        @param reason The exception text.
        @note Retrying on every entry in the 3D tab paid the partial import
            again each time and appended a log line each time; the status
            line keeps saying so instead (review, 2026-10-12).
        """
        if self._preview_3d_import_error is None:
            self._preview_3d_import_error = reason
            self._append_log(tx("Visualizzatore 3D non disponibile: {exc}", exc=reason))
        self._set_3d_status(self.i18n.text("Visualizzatore 3D non disponibile: vedi il log."), STATUS_ERROR)

    def _install_preview_3d(self, viewer) -> None:
        """@brief Replace the placeholder with a viewer, in the same place of the 3D tab.

        @param viewer The viewer widget, just built.
        @note The viewer is not asked to draw here: the 3D tab redraws from
            the window's state on every entry (`_enter_3d_tab`), and when the
            tab is not shown there is nothing to draw yet. Cube labels and
            tooltips are applied now because they are set once per language.
        """
        pending = self.preview_3d
        layout = self._preview_3d_layout
        index = layout.indexOf(pending)
        layout.removeWidget(pending)
        pending.hide()
        pending.setParent(None)
        pending.deleteLater()
        self.preview_3d = viewer
        self._wire_preview_3d()
        viewer.setMinimumHeight(120)
        layout.insertWidget(index, viewer, 1)
        self._apply_view_cube_labels()
        if pending.show_pin_numbers != viewer.show_pin_numbers:
            viewer.set_pin_numbers(pending.show_pin_numbers)
        painter = getattr(self, "btn_format_painter_3d", None)
        if painter is not None and hasattr(viewer, "format_painter_active"):
            viewer.format_painter_active = bool(painter.isChecked())

    def schedule_preview_3d_preload(self, delay_ms: int = 1500) -> None:
        """@brief Import the viewer's libraries in the background shortly after the window appears.

        @param delay_ms Pause before starting, so the window paints and
            settles first.
        @note Called by `main()` only: tests never start this thread. The
            thread does the import alone (no widget is touched off the GUI
            thread); the widget is built on the GUI thread at the first
            entry in the 3D tab (`_on_preview_3d_preloaded` explains why
            not earlier). If the user enters the 3D tab meanwhile, the
            import there waits on the module lock until the thread is done:
            one import, never two. A daemon thread never delays the exit.
        """
        QTimer.singleShot(delay_ms, self._start_preview_3d_preload)

    def _start_preview_3d_preload(self) -> None:
        """@brief Start the background import, unless the viewer is already there."""
        import threading
        if (self.preview_3d_loaded() or self._preview_3d_preloader is not None
                or self._preview_3d_import_error is not None):
            return
        preloader = _ModulePreloader(self)
        preloader.loaded.connect(self._on_preview_3d_preloaded)
        preloader.failed.connect(self._note_preview_3d_import_error)
        self._preview_3d_preloader = preloader

        def run():
            import importlib
            try:
                importlib.import_module("kicadgen.gui.preview_3d_vtk")
            except Exception as exc:  # noqa: BLE001 - reported on the GUI thread
                outcome = ("failed", str(exc))
            else:
                outcome = ("loaded", None)
            # The window (parent of the preloader) may be gone by now: the
            # user closed it during the import. A dead receiver is nothing
            # to report, not a crash (review, 2026-10-12).
            try:
                if outcome[0] == "loaded":
                    preloader.loaded.emit()
                else:
                    preloader.failed.emit(outcome[1])
            except RuntimeError:
                pass

        threading.Thread(target=run, name="preview-3d-preload", daemon=True).start()

    def _on_preview_3d_preloaded(self) -> None:
        """@brief Slot of the preload: the module is in `sys.modules`; the widget waits for the tab.

        @note The widget is NOT built here on purpose. Building the VTK
            viewer inside a window already on screen made the window blink
            for one frame (user, 2026-10-12; the native handles do not
            change, it is the compositor's first frame of the new OpenGL
            child). Built on the first entry in the 3D tab instead, that
            frame coincides with the tab's own repaint and the construction
            costs a fraction of a second, the import being done.
        """
        self._append_log(tx("Visualizzatore 3D pronto in sottofondo."))

    def _apply_view_cube_labels(self) -> None:
        """@brief Write the translated face names on the navigation cube."""
        preview = getattr(self, "preview_3d", None)
        if preview is None or not hasattr(preview, "set_view_cube_labels"):
            return
        preview.set_view_cube_labels({
            "top": self.i18n.text("Sopra"), "bottom": self.i18n.text("Sotto"),
            "front": self.i18n.text("Fronte"), "back": self.i18n.text("Retro"),
            "right": self.i18n.text("Destra"), "left": self.i18n.text("Sinistra")})
        if hasattr(preview, "set_view_cube_tooltips"):
            preview.set_view_cube_tooltips({
                "home": self.i18n.text("Vista isometrica iniziale"),
                "fit": self.i18n.text("Inquadra tutto il modello senza cambiare l'angolo di vista"),
                "roll_ccw": self.i18n.text("Ruota la vista di 90° in senso antiorario"),
                "roll_cw": self.i18n.text("Ruota la vista di 90° in senso orario")})

    def _set_3d_status(self, text: str, level: str = "info") -> None:
        """@brief Update the 3D status line; show it only for warnings and errors.

        @param text Already translated message.
        @param level STATUS_INFO, STATUS_WARNING or STATUS_ERROR.
        @note User decision (2026-09-23): when everything goes well nothing is
            written under the model. The text is still stored in the label
            (tests and later appends read it) and the callers keep writing the
            log, so an outcome is never lost -- it is only not displayed.
        """
        self.label_3d_status.setText(text)
        self.label_3d_status.setProperty("status_level", level)
        color = {STATUS_WARNING: YELLOW, STATUS_ERROR: RED}.get(level, TEXT)
        self.label_3d_status.setStyleSheet(f"color: {color};")
        self.label_3d_status.setVisible(level in (STATUS_WARNING, STATUS_ERROR))

    def _on_pin_numbers_toggled(self, checked: bool) -> None:
        self._config["show_pin_numbers_3d"] = checked
        self._save_config_safely()
        self.preview_3d.set_pin_numbers(checked)
        # Ridisegna la vista corrente: mesh generata se c'e', altrimenti
        # l'approssimazione dai parametri.
        if self._last_mesh_path and Path(self._last_mesh_path).is_file():
            try:
                self.preview_3d.render_mesh(self._last_mesh_path)
                return
            except Exception as exc:  # noqa: BLE001 - si ricade sull'anteprima parametrica
                self._append_log(tx("Anteprima mesh non disponibile: {exc}", exc=exc))
        self._draw_preview_3d()

    def _open_3dshapes_dir(self) -> None:
        shapes_dir = self._project_dir() / f"{self._component_name()}.3dshapes"
        try:
            shapes_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # Slot: an escaping exception would end the process (crash hook).
            self._append_log(tx("Impossibile creare la cartella {path}: {exc}", path=shapes_dir, exc=exc))
            return
        webbrowser.open(shapes_dir.as_uri())

    def _refine_photo_terminals(self) -> None:
        self._start_terminal_refinement(only_leads=False)

    def _refine_photo_leads(self) -> None:
        self._start_terminal_refinement(only_leads=True)

    def _start_terminal_refinement(self, only_leads=False) -> None:
        """@brief Start the paid photo terminal (or lead) pass on the current components.

        @param only_leads True to refine the leads of terminals already observed.
        @note The pass is charged to the project open when it starts, like
            every other paid run (`ai_account.bind_run_project`). Only one
            bound run can exist at a time, so the pass does not start while
            any other worker -- or an abandoned analysis still waiting for its
            paid answer -- is running.
        """
        if self._background_work_running() or any(
                worker.isRunning() for worker in getattr(self, "_abandoned_workers", ())):
            # Una sola corsa alla volta puo' essere legata a un progetto per i
            # costi: si dice perche' il passo non parte, invece di tacere.
            self._append_log(tx("Dettaglio terminali non avviato: un altro lavoro e' ancora "
                                "in corso; riprova quando e' finito."))
            return
        if not self._photo_png:
            QMessageBox.information(
                self, self.i18n.text("Foto non disponibile"),
                self.i18n.text("Acquisisci prima i componenti dalla foto nel tab Progetto."))
            return
        components = self._photo_components_from_table()
        if only_leads and not any(c.get('terminal_geometry') for c in components):
            QMessageBox.information(
                self, self.i18n.text("Terminali non disponibili"),
                self.i18n.text(
                    "Usa prima Rileva terminali dalla foto. L'affinamento conserva corpo e terminali esistenti."))
            return
        if not self._ensure_api_key():
            return
        question_key = ('terminal.confirm_leads' if only_leads
                        else 'terminal.confirm_geometry')
        if QMessageBox.question(
                self, self.i18n.text("Dettaglio terminali AI"),
                self.i18n.tr(question_key)) != QMessageBox.StandardButton.Yes:
            return
        try:
            profile = self.profile_store.snapshot(self._profile_key())
        except (ProfileError, sqlite3.Error, OSError) as exc:
            QMessageBox.warning(
                self, self.i18n.text('Profilo AI non disponibile'), str(exc))
            return
        self.btn_terminal_detail.setEnabled(False)
        self.btn_lead_refinement.setEnabled(False)
        self._set_3d_status(
            self.i18n.text("Analisi dei terminali nella foto in corso..."), STATUS_INFO)
        worker = TerminalDetailWorker(self._photo_png, components,
                                      self._vision_model(), only_leads=only_leads,
                                      system_prompt=profile['lead_prompt' if only_leads else 'terminal_prompt'],
                                      cache_dir=self._photo_cache_dir() if self._named() else None)
        worker._model_revision = self._model_revision
        worker._photo_snapshot = self._photo_png
        self._terminal_worker = worker
        worker.progress.connect(self._on_ai_progress)
        worker.done.connect(self._on_terminal_detail_done)
        worker.error.connect(self._on_terminal_detail_error)
        worker.finished.connect(self._on_terminal_detail_finished)
        # Every call of this pass is charged to the project open now, even if
        # another one is opened meanwhile; released in _on_terminal_detail_finished.
        if hasattr(self, "ai_account"):
            self.ai_account.bind_run_project(self._project_dir_or_none())
        worker.start()

    def _on_terminal_detail_finished(self) -> None:
        """Release the run project of the terminal pass and re-enable its commands."""
        self._retry_footprint_rewrite()
        if hasattr(self, "ai_account"):
            self.ai_account.release_run_project()
        self.btn_terminal_detail.setEnabled(True)
        self.btn_lead_refinement.setEnabled(True)

    def _on_terminal_detail_error(self, message):
        self._append_log(message)
        self._set_3d_status(message, STATUS_INFO)

    def _on_terminal_detail_done(self, components):
        worker = self._terminal_worker
        if (worker._model_revision != self._model_revision
                or worker._photo_snapshot != self._photo_png
                or worker.components != self._photo_components_from_table()):
            self._append_log(tx("Dettaglio terminali scartato: i dati sono cambiati durante l'analisi."))
            self._set_3d_status(self.i18n.text(
                "Dati modificati durante l'analisi: ripeti Rileva terminali."), STATUS_WARNING)
            return
        if getattr(worker, 'only_leads', False) and components == worker.components:
            self._set_3d_status(self.i18n.text(
                "Nessun nuovo dettaglio dei reofori applicabile: geometria e anteprima conservate."), STATUS_INFO)
            return
        self._set_photo_components(components)
        self._invalidate_generated_mesh()
        count = sum(bool(c.get('terminal_geometry')) for c in components)
        self._set_3d_status(self.i18n.tr(
            "status.terminals_observed", count=count), STATUS_INFO)
        if getattr(worker, 'only_leads', False):
            count = sum(1 for c in components for t in (c.get('terminal_geometry') or {}).get('terminals',[])
                        if t.get('lead_detail'))
            self._set_3d_status(self.i18n.tr(
                "status.leads_observed", count=count), STATUS_INFO)

    def _no_3d_status(self, pending: str = "Il modello 3D compare qui dopo la generazione."):
        """@brief Why the 3D tab has no model, as (text, status level).

        @param pending Italian text for the case FreeCAD is available and the
            model has simply not been generated yet.
        @return (translated text, STATUS_WARNING or STATUS_INFO).
        @note The «Modello 3D» switch lived in the removed Footprint 2D tab;
            today the 3D is off only when FreeCAD is missing, so that is what
            the text says instead of pointing to a control that no longer exists.
        """
        if self._freecad_available is False:
            return (self.i18n.text("FreeCAD non trovato: il modello 3D non può essere generato. "
                                   "Installa FreeCAD o imposta FREECAD_CMD_PATH."), STATUS_WARNING)
        return self.i18n.text(pending), STATUS_INFO

    def _regenerate_3d(self) -> None:
        """@brief Rigenera solo lo STEP con le annotazioni correnti (il footprint resta com'e').

        @note Footprint draft (review, 2026-10-14): the solid is built from
            the package pads (drill, pad size), so a pending footprint draft
            is applied or discarded first, as for a generation, and the
            worker gets the APPLIED pads (`_applied_footprint_in_memory`).
            The automatic retry (`_run_pending_regen_3d`, flag
            `_regen_3d_automatic`) does not ask; it still gets the applied pads.
        """
        if self._background_work_running() or self._automatic_generation_active:
            return
        if (not getattr(self, "_regen_3d_automatic", False)
                and not self._settle_footprint_draft_before_start()):
            return
        with self._applied_footprint_in_memory():
            self._regenerate_3d_applied()

    def _regenerate_3d_applied(self) -> None:
        """@brief Body of `_regenerate_3d`, run with the applied footprint in the fields."""
        if self._background_work_running() or self._automatic_generation_active:
            return
        if self._selected_family() == "single_smd" and self.package_geometry is None:
            self._set_3d_status(self.i18n.text(
                "Componente SMD senza geometria esplicita valida: genera i pad dal datasheet prima di esportare."),
                STATUS_WARNING)
            return

        body = self._current_body_params()
        if body is None:
            QMessageBox.information(
                self, self.i18n.text("Modello 3D non disponibile"), self._no_3d_status()[0])
            return

        try:
            params = (
                self.package_geometry if self.package_geometry is not None else
                self._current_castellated_params() if self._is_castellated()
                else self._current_params()
            )
            if isinstance(params, CastellatedModuleParams) and self._missing_contact_style_message():
                raise ValueError(self._missing_contact_style_message())
            params.validate()
            body.validate()
        except ValueError as exc:
            self._set_3d_status(
                self.i18n.tr("status.parameter_error", error=exc), STATUS_ERROR)
            self._append_log(tx("ERRORE 3D: {exc}", exc=exc))
            return

        self._set_regen_busy(True)
        self._set_3d_status(
            self.i18n.text("Generazione STEP in corso tramite FreeCAD..."), STATUS_INFO)
        self._append_log(
            tx("Rigenerazione 3D {name}: {n_fillet_annotations} fillet, "
               "{n_face_text_annotations} testi incisi",
               name=params.name,
               n_fillet_annotations=len(self.fillet_annotations),
               n_face_text_annotations=len(self.face_text_annotations))
        )

        self._cad3d_worker = Cad3DWorker(
            params, body, self._project_dir(),
            list(self.fillet_annotations), self._face_texts_for_3d(),
            single_component=self._is_single_component_project(),
        )
        self._cad3d_worker._model_revision = self._model_revision
        self._cad3d_worker.photo_scale_px_per_mm = self._photo_scale_px_per_mm()
        self._cad3d_worker.done.connect(self._on_regen_3d_done)
        self._cad3d_worker.progress.connect(self._on_ai_progress)
        self._cad3d_worker.phase.connect(self._on_phase)
        self._cad3d_worker.progress.connect(self._on_regen_3d_progress)
        self._cad3d_worker.warning.connect(self._on_regen_3d_progress)
        self._cad3d_worker.warning.connect(self._on_cad3d_warning)
        self._cad3d_worker.cancellable.connect(self._on_cad_cancellable)
        self._cad3d_worker.error.connect(self._on_regen_3d_error)
        self._cad3d_worker.finished.connect(self._on_regen_3d_finished)
        # FreeCAD gira nel worker, quindi la GUI resta viva: lo spinner si
        # anima davvero e il tempo trascorso scorre. Le schede si bloccano
        # come nella generazione completa, per non cambiare i parametri
        # mentre il solido viene costruito su quelli vecchi.
        self._analysis_tabs_enabled = self.tabs.isEnabled()
        self.tabs.setEnabled(False)
        self._start_log_session(tx("Rigenerazione modello 3D"))
        # Questa e' un'operazione a se', e non chiama il modello AI: lasciare
        # il consuntivo dell'ultima analisi dentro la modale lo farebbe
        # leggere come il costo di cio' che sta accadendo adesso. Resta nel
        # Log AI, che dice esplicitamente "ultima analisi".
        # Vale per OGNI generazione, anche quella automatica: vedi
        # `_clear_cost_for_generation`.
        self._clear_cost_for_generation()
        self.analysis_overlay.start('Generazione modello 3D',
                                    'Avvio di FreeCAD…', abortable=True)
        # Il footprint non si riscrive: restano i due passi del 3D.
        if self._is_single_component_project():
            self.analysis_overlay.set_steps(("Corpo e terminali FreeCAD",))
            self._generation_step_of = {"freecad": 0}
        else:
            self.analysis_overlay.set_steps(GENERATION_STEPS[1:])
            self._generation_step_of = {tool: index - 1 for tool, index in GENERATION_STEP_OF.items()
                                        if index > 0}
        self._cad3d_worker.start()

    def _on_regen_3d_progress(self, message: str) -> None:
        """Porta sulla modale i messaggi che arrivano durante la generazione."""
        self.analysis_overlay.set_status(message)

    def _on_regen_3d_finished(self) -> None:
        """@brief Chiude la modale e riabilita il comando, in ogni esito.

        @note Vale anche per l'interruzione: `finished` scatta comunque, ed e'
            qui che si chiude la richiesta di fermarsi (`_abort_pending`).
        """
        self._retry_footprint_rewrite()
        aborted, self._abort_pending = self._abort_pending, False
        succeeded, self._regen_3d_succeeded = getattr(self, "_regen_3d_succeeded", False), False
        if succeeded and not aborted:
            self._hold_completed_overlay("Completato: modello 3D rigenerato.")
        else:
            self._hide_analysis_overlay()
        self._set_regen_busy(False)

    def _on_regen_3d_done(self, path: str) -> None:
        """@brief Show a regenerated 3D model, or say why a stale one is not shown.

        @param path STEP file written by the worker.
        """
        if not self._cad_result_is_current():
            self._log_stale_cad_result(path)
            return
        self._regen_3d_succeeded = True
        self._append_log(f"OK (3D) -> {path}")
        self._set_3d_status(
            self.i18n.tr("status.step_updated", path=path), STATUS_INFO)
        self._refresh_output_actions()
        self._show_generated_mesh(Path(path))
        self._set_accept_models_ready(True)

    def _set_accept_models_ready(self, ready: bool) -> None:
        """@brief Enable «Accetto, mi piace» only for a solid generated from the current data.

        @param ready True after a successful generation, False once data change.
        """
        button = getattr(self, "btn_accept_models", None)
        if button is not None:
            button.setEnabled(bool(ready))

    def _accept_models(self) -> None:
        """@brief Store the KiCad models of the board just inspected in the archive.

        @note Components are read from the table, where `_show_generated_mesh`
            has just copied model_path and model_match from the generation: it
            is exactly what the user saw in the solid. The dialog lists every
            component, pre-checking those with a model to remember; the outcome
            goes to the log and the 3D status bar, without message boxes.
        """
        components = self._photo_components_from_table()
        if not components:
            self._set_3d_status(self.i18n.text(
                "Nessun componente in tabella: non ci sono modelli KiCad da ricordare."), STATUS_WARNING)
            return
        components, _ = self._attach_accepted_component_images(components, include_png=True)
        dialog = ConsolidateDialog(components, self, i18n=self.i18n)
        self.i18n.retranslate_widgets(dialog)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        chosen = dialog.selected_components()
        if not chosen:
            return
        from ..cad3d.kicad_models import _search_roots
        from ..services.accepted_models import AcceptedModelStore, AcceptedModelsError
        store = AcceptedModelStore()
        try:
            result = store.consolidate(chosen, _search_roots(None),
                                       source=self._component_name() or "")
        except (AcceptedModelsError, sqlite3.Error, OSError) as exc:
            self._append_log(tx("Abbinamenti accettati NON salvati: {exc}", exc=exc))
            self._set_3d_status(self.i18n.tr(
                "status.accepted_models_failed", error=exc), STATUS_ERROR)
            return
        self._append_log(
            tx("Abbinamenti accettati salvati in {path}: {created} voci nuove, "
               "{confirmed} conferme di voci esistenti, {n_skipped} esclusi.",
               path=store.path,
               created=result.created,
               confirmed=result.confirmed,
               n_skipped=len(result.skipped)))
        if result.images_saved:
            self._append_log(tx("Ritagli dei componenti verificati salvati: {count}.",
                                count=result.images_saved))
        without_crop = max(0, len(chosen) - result.images_saved)
        if without_crop:
            self._append_log(tx("Ritaglio immagine non disponibile per {count} componenti; "
                                "gli abbinamenti restano salvati.", count=without_crop))
        for name, reason in result.skipped:
            self._append_log(tx("  escluso {name}: {reason}", name=name, reason=reason))
        self._set_3d_status(self.i18n.tr(
            "status.accepted_models_saved", stored=result.stored,
            created=result.created, confirmed=result.confirmed), STATUS_INFO)

    def _show_accepted_models(self) -> None:
        """@brief Open the archive list, where wrong entries can be deleted."""
        dialog = AcceptedModelsDialog(parent=self, i18n=self.i18n)
        self.i18n.retranslate_widgets(dialog)
        dialog.exec()

    def _show_accepted_models_for_board(self) -> None:
        """@brief Open the archive from the 3D tab, this board's entries first.

        @note The components are the ones in the table now; the library roots
            are those used by the matching, so an entry whose STEP file is gone
            is not claimed for this board, exactly as the generation skips it.
        """
        from ..cad3d.kicad_models import _search_roots
        dialog = AcceptedModelsDialog(parent=self, i18n=self.i18n,
                                      components=self._photo_components_from_table(),
                                      roots=_search_roots(None))
        self.i18n.retranslate_widgets(dialog)
        dialog.exec()

    def _show_generated_mesh(self, step_path: Path) -> None:
        """Mostra in anteprima la mesh del solido appena generato (fillet e testo inclusi)."""
        report_path = step_path.parent.parent / 'kicad-model-candidates.json'
        if report_path.is_file():
            try:
                records = json.loads(report_path.read_text(encoding='utf-8'))['components']
                if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
                    raise ValueError(tx("Elenco componenti non valido nel report KiCad"))
                current = self._components_from_table()
                for fallback_index, record in enumerate(records):
                    index = record.get('source_row', fallback_index)
                    if type(index) is not int or not 0 <= index < len(current):
                        continue
                    component = current[index]
                    if (record.get('name') != component.name
                            or record.get('package') != component.package
                            or record.get('length_mm') != component.length
                            or record.get('width_mm') != component.width):
                        continue
                    metadata = self._photo_rows[index]
                    match = record.get('model_match')
                    if match is not None and not isinstance(match, dict):
                        raise ValueError(tx("Report modello KiCad non valido"))
                    metadata['model_match'] = match
                    for key in ('model_path', 'model_rotation_deg', 'render_3d', 'rotation_deg'):
                        if key in record:
                            metadata[key] = deepcopy(record[key])
                self._append_log(tx("Candidati KiCad e decisioni: {report_path}", report_path=report_path))
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                self._append_log(tx("Report candidati non caricato: {exc}", exc=exc))
        mesh_path = step_path.with_suffix("").with_suffix(".preview.json")
        if not mesh_path.is_file():
            mesh_path = step_path.parent / f"{step_path.stem}.preview.json"
        if not mesh_path.is_file():
            return
        previous = self._last_mesh_path
        if self._prepare_mesh_then(mesh_path, partial(self._apply_generated_mesh, mesh_path),
                                   partial(self._generated_mesh_unavailable, mesh_path, previous)):
            # Il solido e' scritto e si sta preparando: per il resto della
            # finestra (rigenerazione automatica, ingresso nella scheda) c'e'.
            self._last_mesh_path = mesh_path
            return
        self._apply_generated_mesh(mesh_path)

    def _apply_generated_mesh(self, mesh_path: Path) -> None:
        """@brief Draw the generated mesh and say so on the 3D status line.

        @param mesh_path Preview JSON written next to the STEP.
        @note Synchronous tail of `_show_generated_mesh`; after `prepare_mesh`
            the viewer finds the scene in its cache and only builds actors.
        """
        try:
            self._sync_3d_pin_functions()
            self.preview_3d.render_mesh(mesh_path)
            self._last_mesh_path = mesh_path
            self._set_3d_status(
                f"{self.label_3d_status.text()}\n"
                + self.i18n.text(
                    "Anteprima aggiornata con la geometria reale esportata (fillet e testo inclusi)."), STATUS_INFO)
        except Exception as exc:  # noqa: BLE001 - anteprima non critica
            self._append_log(tx("Anteprima mesh non disponibile: {exc}", exc=exc))

    def _generated_mesh_unavailable(self, mesh_path: Path, previous, error: str) -> None:
        """@brief Off-thread preparation of a generated mesh failed: log it, as the render did.

        @param mesh_path Mesh that could not be prepared.
        @param previous Mesh path before the request, restored when still ours.
        @param error Error text from the viewer.
        """
        if self._last_mesh_path == mesh_path:
            self._last_mesh_path = previous
        self._append_log(tx("Anteprima mesh non disponibile: {exc}", exc=error))

    def _on_regen_3d_error(self, message: str) -> None:
        """@brief Scrive nel log e sulla barra 3D perche' lo STEP non c'e'.

        @param message Testo dell'esito, come arriva dal worker.
        @note Come `_on_cad3d_error`: l'interruzione chiesta dall'utente non e'
            un guasto, quindi non viene annunciata come tale ne' nel log ne'
            sulla barra di stato.
        """
        self._realign_after_3d_failure()
        if not self._cad_result_is_current():
            return
        if CAD_ABORT_MESSAGE in message:
            self._append_log(tx("INTERROTTO 3D: {message}", message=message))
            # Interrotta dopo l'avvio di FreeCAD, la corsa ha tolto il modello
            # precedente: dirlo, invece di lasciar credere che sia ancora li'.
            self._set_3d_status(self.i18n.text(
                "Generazione interrotta dall'utente: il modello 3D non è stato "
                "aggiornato.") if self._step_still_on_disk() else self.i18n.text(
                "Generazione interrotta dall'utente: il modello 3D precedente è "
                "stato rimosso, rigeneralo prima di usarlo in KiCad."), STATUS_INFO)
            return
        self._append_log(tx("ERRORE 3D: {message}", message=message))
        self._set_3d_status(self.i18n.tr(
            "status.step_failed", error=message.splitlines()[0]), STATUS_ERROR)

    # ── Componenti del modulo (3D) ─────────────────────────────────

    #: Keys of a photo component carried into `ComponentBox.match_evidence`.
    #: Campi strutturali che il 3D legge al posto del nome (verifica del 25/09,
    #: E11): chi e' solo un'ipotesi del rilevatore, chi e' stato visto
    #: dall'utente, il conteggio completo dei reofori per il caso «tre piu'
    #: linguetta», e il riferimento asimmetrico che il costruttore confronta col
    #: baricentro del modello nativo per stabilire il verso.
    _MATCH_EVIDENCE_KEYS = (
        '_smd_class', '_smd_rotation_deg', '_smd_rotation_confidence', '_smd_accepted',
        '_smd_body', '_smd_rotation_kind', '_smd_views_agree', '_smd_terminal_count',
        'presence_confidence', 'class_confidence', 'confidence',
        '_yolo_review_only', '_yolo_origin', 'terminal_count',
        'terminal_layout', 'terminal_count_basis', '_global_inventory_status', 'class',
        '_yolo_proposal_id', '_presence_reviewed_by_user',
        'lead_count', 'lead_count_complete',
        'landmark_feature', 'landmark_side', 'landmark_confidence', 'marking_confidence',
        '_accepted_image_descriptor',
        '_crop_class_status', '_category_source', '_category_status',
        '_ai_class_proposal', '_ai_class_confidence', '_terminal_status',
        '_crop_terminal_review_required',
        # Scala della foto (o incertezza dichiarata): il 3D ne ricava in mm
        # l'incertezza di misura per sporgenze e archivio degli abbinamenti.
        'measurement_uncertainty_mm', 'photo_scale_px_per_mm')

    @classmethod
    def _component_from_photo(cls, c: dict) -> ComponentBox:
        """@brief Build the CAD box of one photo component dict.

        @param c Photo component (extraction, review or saved project format).
        @return The `ComponentBox` the 3D generator receives.
        @throws KeyError When a geometry key (x_mm, y_mm, length_mm, width_mm) is missing.
        """
        return ComponentBox(
            x=c["x_mm"], y=c["y_mm"], length=c["length_mm"], width=c["width_mm"],
            height=c.get("height_mm", 1.0), name=str(c.get("name") or "").strip() or "Componente",
            color_hex=str(c.get("color_hex") or "").strip() or "#303030",
            kind=c.get("kind") or "generic",
            marking=str(c.get("marking") or "").strip(), rotation=float(c.get("rotation_deg", 0.0)),
            model_rotation_deg=c.get("model_rotation_deg"),
            model_selection=deepcopy(c.get("model_selection")),
            model_match=deepcopy(c.get("model_match")),
            match_evidence={key: deepcopy(c[key]) for key in cls._MATCH_EVIDENCE_KEYS if key in c},
            model_z_offset_mm=float(c.get("model_z_offset_mm") or 0.0),
            pin_count=c.get("pin_count"), pins_by_side=deepcopy(c.get("pins_by_side")),
            pin_count_confidence=str(c.get("pin_count_confidence") or ""),
            pin_count_source=str(c.get("pin_count_source") or ""),
            detected_kind=c.get("_detected_kind"),
            terminal_geometry=deepcopy(c.get("terminal_geometry")),
            package=str(c.get("package") or ""),
            mating=str(c.get("mating") or ""),
            bbox_basis=str(c.get("bbox_basis") or "unknown"),
            package_evidence=str(c.get("package_evidence") or ""),
            render_3d_override=(c.get("render_3d_override")
                                if isinstance(c.get("render_3d_override"), bool) else None),
            model_rotation_delta_deg=float(c.get("model_rotation_delta_deg") or 0.0),
        )

    @staticmethod
    def _photo_row(component: ComponentBox, photo: dict | None) -> dict:
        """@brief One stored photo component: detection metadata plus CAD geometry.

        @param component Normalised CAD box of the component.
        @param photo The dict it came from; unknown keys are kept beside it.
        @return The row as `_photo_components_from_table` returns it.
        @note Absent optional keys mean «the automatism decides»: they are
            removed instead of being written with a neutral value, which would
            read as an explicit choice.
        """
        row = deepcopy(photo or {})
        for key, value in (component.match_evidence or {}).items():
            row.setdefault(key, deepcopy(value))
        # Copie, non riferimenti: chi legge o modifica la riga non deve poter
        # toccare il componente da cui e' nata, e viceversa.
        row.update(pin_count=component.pin_count, pins_by_side=deepcopy(component.pins_by_side),
                   pin_count_confidence=component.pin_count_confidence,
                   pin_count_source=component.pin_count_source,
                   _detected_kind=component.detected_kind,
                   terminal_geometry=deepcopy(component.terminal_geometry),
                   package=component.package, mating=component.mating,
                   bbox_basis=component.bbox_basis,
                   model_selection=deepcopy(component.model_selection),
                   model_match=deepcopy(component.model_match),
                   package_evidence=component.package_evidence)
        # None leaves native-model axis inference enabled; zero is an explicit
        # local CAD override, independent of the photo rectangle's rotation.
        if component.model_rotation_deg is None:
            row.pop("model_rotation_deg", None)
        else:
            row["model_rotation_deg"] = component.model_rotation_deg
        for key, value in (("model_z_offset_mm", component.model_z_offset_mm),
                           ("model_rotation_delta_deg", component.model_rotation_delta_deg)):
            if value:
                row[key] = value
            else:
                row.pop(key, None)
        if isinstance(component.render_3d_override, bool):
            row["render_3d_override"] = component.render_3d_override
        else:
            row.pop("render_3d_override", None)
        # Sei decimali come l'editor che ha preceduto questi dati: le correzioni
        # fotografiche conservano la precisione dei piccoli package.
        row.update(name=component.name, kind=component.kind,
                   x_mm=round(float(component.x), 6), y_mm=round(float(component.y), 6),
                   length_mm=round(float(component.length), 6),
                   width_mm=round(float(component.width), 6),
                   height_mm=round(float(component.height), 6),
                   rotation_deg=float(component.rotation) % 360,
                   color_hex=component.color_hex, marking=component.marking)
        return row

    def _set_photo_components(self, components: list[dict], *,
                              replace_excluded: bool = False) -> None:
        """@brief Replace the photo components, keeping unknown metadata beside each one.

        @param components Photo component dicts.
        @param replace_excluded True when `components` is a whole new
            inventory (extraction, relaunch, project restore): the AI-only
            bodies set aside before are dropped and replaced by those in
            `components`. False (default) for an edit of the current table:
            the bodies already set aside stay.
        @throws KeyError When a component misses a geometry key; nothing is replaced then.
        @note Bodies marked `_ai_only_excluded` True (added by the vision model
            alone and not kept, HANDOFF 141) never enter the table nor the 3D:
            they go to `_photo_excluded_rows`, from which the photo review
            offers them again. A whole new inventory that sets some aside says
            so in the log, because nothing on screen would show them.
        """
        kept = [c for c in components if not ai_only_excluded(c)]
        excluded = [deepcopy(c) for c in components if ai_only_excluded(c)]
        boxes = [self._component_from_photo(c) for c in kept]
        for component in excluded:
            # Stessa validazione della geometria dei corpi tenuti: un corpo
            # escluso malformato non deve far fallire dopo la revisione.
            self._component_from_photo(component)
        self.castellated_components = boxes
        self._photo_rows = [self._photo_row(box, c) for box, c in zip(boxes, kept)]
        if replace_excluded:
            self._photo_excluded_rows = excluded
            if excluded:
                self._log_excluded_ai_only()
        else:
            self._photo_excluded_rows = list(getattr(self, "_photo_excluded_rows", [])) + excluded
        self._refresh_components_editor_button()

    def _log_excluded_ai_only(self) -> None:
        """@brief One log line: how many AI-only bodies are set aside, and how to keep them.

        @note Written whenever such bodies are set aside without the user
            having chosen it on screen (new inventory, restored project) or
            after a review that left some out: the table and the 3D do not
            show them, so the log is the only trace until the review reopens.
        """
        self._append_log(tx(
            "{count} corpi aggiunti dal solo modello, senza proposta del rilevatore, restano "
            "fuori dalla tabella e dal 3D: per tenerli riapri «Modifica la revisione dei "
            "componenti…» e premi T su ciascuno.", count=len(self._photo_excluded_rows)))

    def _refresh_components_editor_button(self) -> None:
        """@brief Offer «Copia formato» only when there are bodies to edit.

        The hand editor's own button was removed from the toolbar (HANDOFF
        152); Ctrl+E applies the same condition by itself.
        """
        button = getattr(self, "btn_format_painter_3d", None)
        if button is not None:
            button.setVisible(bool(self._photo_rows))

    def _edit_components(self, select_row=None) -> None:
        """@brief Open the hand editor of the mounted bodies.

        @param select_row Row to select first (right click on a body in 3D).
        @note Edits the stored components, not a copy that lives with the
            window: «Applica» goes through `_apply_photo_review`, like the
            photo review, so the choices are saved and the 3D is rebuilt.
        """
        from ..services.datasheet_extractor import COMPONENT_KINDS
        dialog = ComponentEditorDialog(self._photo_components_from_table(), COMPONENT_KINDS,
                                       self, select_row=select_row)
        dialog.components_applied.connect(self._apply_photo_review)
        self.i18n.retranslate_widgets(dialog)
        dialog.exec()
        dialog.deleteLater()

    def _on_format_painter_toggled(self, checked: bool) -> None:
        """@brief Start or end the «copia formato» mode of the 3D view.

        @param checked True when the button is switched on.
        @note Ending the mode applies every paste at once through
            `_apply_photo_review` (saved choices, FreeCAD only, no AI): one
            regeneration for all the targets instead of one per click.
        """
        preview = getattr(self, "preview_3d", None)
        if preview is not None:
            preview.format_painter_active = bool(checked)
        if checked:
            self._format_source, self._format_targets = None, []
            self._format_source_row, self._format_target_rows = None, []
            self.statusBar().showMessage(self.i18n.text(
                "Copia formato: clic sul componente da copiare."))
            self._format_toast_message(self.i18n.text("Copia formato attiva"),
                                       self.i18n.text("Clic sul componente da copiare."))
            return
        targets, self._format_source, self._format_targets = self._format_targets, None, []
        self._format_source_row, self._format_target_rows = None, []
        # The outlines say what is still being chosen: the mode is over.
        clear_outlines = getattr(preview, "clear_format_outlines", None)
        if callable(clear_outlines):
            clear_outlines()
        if targets:
            self._format_toast_message(
                tx("Formato applicato a {n} componenti", n=len(targets)),
                ", ".join(targets),
                self.i18n.text("Rigenerazione del modello 3D: FreeCAD, nessuna richiesta AI."))
            self._apply_photo_review(self._photo_components_from_table())
            self.statusBar().showMessage(tx("Formato incollato su {n} componenti: modello 3D "
                                            "rigenerato.", n=len(targets)))
        else:
            self.statusBar().clearMessage()

    def _format_toast_message(self, title: str, body: str = "", hint: str = "") -> None:
        """@brief Show the «copia formato» popup next to the cursor (non-modal, fades away).

        @param title Translated first line.
        @param body Translated main text.
        @param hint Translated next-step line.
        @note User, 2026-09-27: the status bar alone was too discreet. The card
            is created once and reused: a new message replaces the previous one.
        """
        from .toast import Toast
        toast = getattr(self, "_format_toast", None)
        if toast is None:
            toast = self._format_toast = Toast(self, "format-painter.svg")
        toast.show_message(title, body, hint)

    def _on_format_painter_click(self, row: int) -> None:
        """@brief Copy the format of the first component clicked, paste it on the next ones.

        @param row Index in `_photo_rows` of the component under the click.
        """
        from ..services.component_format import copy_format, paste_format
        button = getattr(self, "btn_format_painter_3d", None)
        if button is None or not button.isChecked() or not 0 <= row < len(self._photo_rows):
            return
        name = str(self._photo_rows[row].get("name") or "").strip() or str(row + 1)
        if self._format_source is None:
            self._format_source = copy_format(self._photo_rows[row])
            self._format_source_row = row
            self._show_format_outlines()
            self.statusBar().showMessage(tx(
                "Formato di {name} copiato: clic sui componenti su cui incollarlo, poi "
                "spegni l'icona per rigenerare il 3D.", name=name))
            self._format_toast_message(
                tx("Formato di {name} copiato", name=name),
                self.i18n.text("Misure, altezza, tipologia, package e modello KiCad."),
                self.i18n.text("Contorno giallo sul copiato, verde sugli incollati. Clic sui "
                               "componenti da correggere; ripremi il pennello per applicare."))
            return
        self._photo_rows[row] = paste_format(self._photo_rows[row], self._format_source)
        if name not in self._format_targets:
            self._format_targets.append(name)
        if row not in self._format_target_rows and row != self._format_source_row:
            self._format_target_rows.append(row)
        self._show_format_outlines()
        self._append_log(tx("Copia formato: {source} -> {name}.",
                            source=self._format_source.get("source_name") or "?", name=name))
        self.statusBar().showMessage(tx(
            "Formato di {source} incollato su: {targets}. Spegni l'icona per rigenerare il 3D.",
            source=self._format_source.get("source_name") or "?",
            targets=", ".join(self._format_targets)))
        self._format_toast_message(
            tx("Incollato su {name}", name=name),
            tx("{n} componenti finora: {targets}", n=len(self._format_targets),
               targets=", ".join(self._format_targets)),
            self.i18n.text("Ripremi il pennello per applicare."))

    def _show_format_outlines(self) -> None:
        """@brief Draw the «copia formato» outlines for the current source and targets.

        @note The 3D view may be a stub without outlines (tests, or a view
            that failed to start): then nothing is drawn and nothing breaks.
        """
        preview = getattr(self, "preview_3d", None)
        highlight = getattr(preview, "highlight_format_rows", None)
        if callable(highlight):
            highlight(self._format_source_row, list(self._format_target_rows))

    #: Correzioni di orientamento offerte dal menu contestuale. Solo multipli
    #: di un quarto di giro: i modelli della libreria KiCad seguono la
    #: convenzione del footprint, quindi uno mal orientato lo e' sempre di
    #: 90, 180 o 270 gradi, mai di un angolo qualsiasi.
    MENU_ROTATIONS_DEG = (90, 180, 270)

    def _show_component_menu(self, row: int, position) -> None:
        """@brief Menu del tasto destro su un componente della vista 3D.

        La vista 3D serve solo a indicare quale riga: le voci agiscono sui
        metadati dei componenti salvati (`_photo_rows`), che restano la fonte di verita'.
        Ogni scelta viene scritta nel JSON delle scelte e ha effetto dalla
        rigenerazione FreeCAD successiva, senza ripassare dal modello AI.
        """
        if not 0 <= row < len(self._photo_rows):
            return
        metadata = self._photo_rows[row]
        name = str(metadata.get("name") or "").strip() or "?"
        hidden = metadata.get("render_3d_override") is False
        delta = float(metadata.get("model_rotation_delta_deg") or 0.0)

        menu = QMenu(self)
        header = menu.addAction(name)
        header.setEnabled(False)
        menu.addSeparator()

        if hidden:
            restore = menu.addAction(self.i18n.text("Rimetti nel modello 3D"))
            restore.triggered.connect(lambda: self._set_component_visibility(row, True))
        else:
            remove = menu.addAction(self.i18n.text("Elimina dal modello 3D (falso positivo)"))
            remove.triggered.connect(lambda: self._set_component_visibility(row, False))

        rotate = menu.addMenu(self.i18n.text("Ruota il solido"))
        for degrees in self.MENU_ROTATIONS_DEG:
            action = rotate.addAction(f"+{degrees}°")
            action.triggered.connect(
                lambda _checked=False, d=degrees: self._rotate_component_model(row, d))
        reset = rotate.addAction(self.i18n.text("Azzera la correzione"))
        reset.setEnabled(bool(delta))
        reset.triggered.connect(lambda: self._rotate_component_model(row, None))

        edit = menu.addAction(self.i18n.text("Modifica componente..."))
        edit.triggered.connect(
            lambda: QTimer.singleShot(0, lambda: self._edit_components(select_row=row)))

        change = menu.addAction(self.i18n.text("Cambia modello KiCad..."))
        change.triggered.connect(
            lambda: QTimer.singleShot(0, lambda: self._show_photo_comparison(component_row=row)))
        menu.exec(position)

    def _set_component_visibility(self, row: int, visible: bool) -> None:
        """@brief Includi o escludi un componente dal solido, senza perderlo.

        La riga resta in tabella e nel JSON: un falso positivo tolto dal 3D
        va comunque documentato, altrimenti alla corsa successiva nessuno
        saprebbe che era stato proposto ed e' stato scartato a ragion veduta.
        """
        self._update_component_metadata(row, {"render_3d_override": visible})

    def _rotate_component_model(self, row: int, degrees) -> None:
        """@brief Somma una correzione d'orientamento, o la azzera con None.

        Si somma invece di sostituire perche' l'angolo di partenza lo deduce
        FreeCAD confrontando il modello nativo con la misura fotografica:
        qui quell'angolo non e' noto, e "ruota di 90 gradi" e' comunque cio'
        che si intende guardando il solido storto.
        """
        if degrees is None:
            self._update_component_metadata(row, {"model_rotation_delta_deg": None})
            return
        metadata = self._photo_rows[row] if 0 <= row < len(self._photo_rows) else None
        current = float((metadata or {}).get("model_rotation_delta_deg") or 0.0)
        total = (current + float(degrees)) % 360
        self._update_component_metadata(
            row, {"model_rotation_delta_deg": total or None})

    def _update_component_metadata(self, row: int, changes: dict) -> None:
        """@brief Scrivi i metadati di una riga e propaga la modifica.

        Un valore None toglie la chiave: significa "torna all'automatismo",
        che non e' la stessa cosa di una scelta esplicita dal valore neutro.
        """
        if not 0 <= row < len(self._photo_rows):
            return
        metadata = self._photo_rows[row]
        for key, value in changes.items():
            if value is None:
                metadata.pop(key, None)
            else:
                metadata[key] = value
        self._append_log(
            f"Componente '{str(metadata.get('name') or '').strip() or row}': "
            + ", ".join(f"{k}={metadata.get(k, 'automatico')}" for k in changes))
        # Stesso percorso della revisione fotografica: salva il JSON delle
        # scelte e marca il 3D da rigenerare. La rigenerazione e' FreeCAD e
        # basta -- nessuna chiamata al modello AI.
        self._apply_photo_review(self._photo_components_from_table())

    def _components_from_table(self) -> list[ComponentBox]:
        """@brief The photo components as they are now, corrections included.

        @note Each one carries the scale of the calibrated photo it was
            measured on (`photo_scale_px_per_mm`), unless it already declares
            its own: the 3D turns it into the measurement uncertainty in mm
            (cad3d.measurement_uncertainty) instead of a declared fallback.
        """
        scale = self._photo_scale_px_per_mm()
        rows = []
        for row in self._photo_rows:
            if scale is not None and "photo_scale_px_per_mm" not in row:
                row = dict(row, photo_scale_px_per_mm=scale)
            rows.append(row)
        components = [self._component_from_photo(row) for row in rows]
        image = self._decoded_component_photo()
        if image is not None:
            import cv2
            from ..services.component_image_match import (
                describe_crop, descriptor_bytes, extract_component_crop,
            )
            board_length, board_width = self._component_photo_board_size()
            if board_length and board_width:
                for box, row in zip(components, rows):
                    try:
                        crop = extract_component_crop(
                            image, row, board_length, board_width)
                        evidence = dict(box.match_evidence or {})
                        evidence["_accepted_image_descriptor"] = descriptor_bytes(
                            describe_crop(crop))
                        box.match_evidence = evidence
                    except (KeyError, TypeError, ValueError, cv2.error):
                        # Una foto/box non utilizzabile non blocca la generazione 3D.
                        continue
        return components

    def _component_photo_board_size(self):
        """Calibrated board dimensions corresponding to the current photo crop."""
        current = "cast" if self._is_castellated() else "matrix"
        key = getattr(self, "_photo_body_fields", None) or current
        return self._body_sizes().get(key, (None, None))

    def _decoded_component_photo(self):
        """Decode the calibrated board photo once for component image work."""
        png = getattr(self, "_photo_png", None)
        if not isinstance(png, (bytes, bytearray)) or not png:
            return None
        try:
            import cv2
            import numpy as np
            return cv2.imdecode(np.frombuffer(bytes(png), np.uint8), cv2.IMREAD_COLOR)
        except (ImportError, ValueError):
            return None

    def _attach_accepted_component_images(self, components, *, include_png=False):
        """Attach transient crops/descriptors to chosen accepted components."""
        image = self._decoded_component_photo()
        if image is None:
            return [dict(component) for component in components], 0
        board_length, board_width = self._component_photo_board_size()
        if not board_length or not board_width:
            return [dict(component) for component in components], 0
        import cv2
        from ..services.component_image_match import (
            describe_crop, descriptor_bytes, extract_component_crop,
        )
        attached = []
        count = 0
        for component in components:
            item = dict(component)
            try:
                crop = extract_component_crop(image, item, board_length, board_width)
                descriptor = descriptor_bytes(describe_crop(crop))
                if include_png:
                    ok, encoded = cv2.imencode(".png", crop)
                    if not ok:
                        raise ValueError("PNG encoding failed")
                    item["_accepted_image_png"] = encoded.tobytes()
                item["_accepted_image_descriptor"] = descriptor
                count += 1
            except (KeyError, TypeError, ValueError, cv2.error):
                pass
            attached.append(item)
        return attached, count

    def _photo_scale_px_per_mm(self) -> float | None:
        """@brief Pixels per millimetre of the calibrated photo, along the board length.

        @return Width of `_photo_png` in pixels divided by the length (mm) of
            the photographed board -- the body fields `_photo_review_frame`
            reads (those of the type used by the extraction), without its log
            line -- or None when there is no calibrated PNG or no positive length.
        @note The width is read from the PNG header (IHDR): nothing is decoded
            at every generation. The calibrated photo is always a PNG
            (`photo_inventory`, `cv2.imencode('.png')`); anything else gives None.
        """
        png = getattr(self, "_photo_png", None)
        if (not isinstance(png, (bytes, bytearray)) or len(png) < 24
                or bytes(png[:8]) != b"\x89PNG\r\n\x1a\n"):
            return None
        width_px = int.from_bytes(bytes(png[16:20]), "big")
        current = "cast" if self._is_castellated() else "matrix"
        length_mm = self._body_sizes()[getattr(self, "_photo_body_fields", None) or current][0]
        if width_px <= 0 or not length_mm or length_mm <= 0:
            return None
        return width_px / float(length_mm)

    def _on_fine_grid_changed(self, _index: int) -> None:
        """@brief Keep the chosen drawing-grid divisor and apply it to the preview."""
        divisor = int(self.combo_fine_grid.currentData())
        self._config["draw_grid_divisor"] = divisor
        self._save_config_safely()
        self.preview_view.set_fine_divisor(divisor)

    def _on_grid_toggled(self, checked: bool) -> None:
        self._config["show_grid"] = checked
        self._save_config_safely()
        self.combo_grid_unit.setEnabled(checked)
        self.combo_fine_grid.setEnabled(checked)
        view = getattr(self, "preview_view", None)
        spec = getattr(self, "_preview_grid_spec", None)
        if view is not None and spec is not None:
            step = (2.54 if self.combo_grid_unit.currentData() == "inch" else 1.0) * spec["mm_to_px"]
            origin = tuple(value * spec["mm_to_px"] for value in spec["origin"])
            view.set_grid(step, origin, checked)
        # Grid visibility is not a request to redraw/refit the footprint.
        for item in self.preview_scene.items():
            if item.data(_GRID_ITEM):
                item.setVisible(checked)

    def _on_grid_unit_changed(self) -> None:
        self._config["grid_unit"] = self.combo_grid_unit.currentData()
        self._save_config_safely()
        spec = getattr(self, '_preview_grid_spec', None)
        if spec is not None:
            for item in self.preview_scene.items():
                if item.data(_GRID_ITEM):
                    self.preview_scene.removeItem(item)
            self._draw_grid(**spec)

    def _fit_preview(self, force: bool = False) -> None:
        """@brief Inquadra il footprint, ignorando la griglia, se la camera lo segue.

        La griglia si estende oltre il componente: includendola
        nell'inquadratura, attivarla e disattivarla cambiava la dimensione
        del disegno a parita' di footprint -- confondente proprio quando la
        griglia serve a valutare le distanze.

        @param force True per «Adatta alla vista»: inquadra comunque e
            torna a seguire il footprint.
        @note Dopo uno zoom o uno spostamento dell'utente
            (`FootprintView.auto_fit` False) i ridisegni lasciano la camera
            dov'e': modificare un disegno, un testo o un pad non deve
            cambiare lo zoom (utente, 2026-10-13), come nell'editor del simbolo.
        """
        rect = None
        for item in self.preview_scene.items():
            if item.data(_GRID_ITEM):
                continue
            bounds = item.sceneBoundingRect()
            rect = bounds if rect is None else rect.united(bounds)
        if rect is None or rect.isEmpty():
            rect = self.preview_scene.itemsBoundingRect()
        if rect.isEmpty():
            return
        # Keep the scrollable extent independent of grid visibility/spacing.
        # An implicit scene rect can grow with previously drawn grid items.
        rect = rect.adjusted(-12, -12, 12, 12)
        self.preview_scene.setSceneRect(rect)
        if not force and not self.preview_view.auto_fit:
            return
        self.preview_view.resetTransform()
        self.preview_view.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)
        self.preview_view.auto_fit = True

    def _draw_grid(
        self, mm_to_px: float, width: float, height: float, margin: float,
        x0: float = 0.0, y0: float = 0.0,
        origin: tuple = (0.0, 0.0),
    ) -> None:
        """Griglia di riferimento, con passo in mm o in decimi di pollice.

        Una linea marcata ogni 5 passi fa da riferimento grosso (5mm oppure
        mezzo pollice), le altre sono sottili.

        (x0, y0) e' l'angolo da cui parte l'area da coprire: zero per il
        footprint a matrice, che ha il pin 1 nell'origine, negativo per i
        moduli castellated, centrati sull'origine.
        """
        self._preview_grid_spec = dict(mm_to_px=mm_to_px, width=width, height=height,
                                       margin=margin, x0=x0, y0=y0, origin=origin)
        step = 2.54 if self.combo_grid_unit.currentData() == "inch" else 1.0
        self.preview_view.set_grid(
            step * mm_to_px,
            tuple(value * mm_to_px for value in origin),
            self.chk_grid.isChecked(),
        )

        minor = QPen(QColor(SURFACE1))
        minor.setWidthF(0.5)
        major = QPen(QColor(SURFACE2))
        major.setWidthF(1.0)

        ox, oy = origin
        pad = margin + 2.0
        i_min, i_max = math.floor((x0 - pad - ox) / step), math.ceil((x0 + width + pad - ox) / step)
        j_min, j_max = math.floor((y0 - pad - oy) / step), math.ceil((y0 + height + pad - oy) / step)

        for i in range(i_min, i_max + 1):
            pen = major if i % 5 == 0 else minor
            x = (ox + i * step) * mm_to_px
            line = self.preview_scene.addLine(
                x, (oy + j_min * step) * mm_to_px, x, (oy + j_max * step) * mm_to_px, pen
            )
            line.setZValue(-10)  # dietro pad, silkscreen e quote
            line.setData(_GRID_ITEM, True)
            line.setVisible(self.chk_grid.isChecked())
        for j in range(j_min, j_max + 1):
            pen = major if j % 5 == 0 else minor
            y = (oy + j * step) * mm_to_px
            line = self.preview_scene.addLine(
                (ox + i_min * step) * mm_to_px, y, (ox + i_max * step) * mm_to_px, y, pen
            )
            line.setZValue(-10)
            line.setData(_GRID_ITEM, True)
            line.setVisible(self.chk_grid.isChecked())

    def _pad_tooltip(self, number) -> str:
        """@brief Popup text of one pad of the 2D preview, from the pinout table.

        @param number Pad number as drawn (int or str).
        @return «Pin N: function» when the pinout table has that pin, «Pin N»
            otherwise; the same rows the Pinout dialog shows (user, 2026-09-27).
        @note Pins are matched on their text, so 3 and "3" are the same pin and
            alphanumeric pins ("A2") work as well.
        """
        key = str(number).strip()
        for entry in self.pinout or ():
            if isinstance(entry, dict) and str(entry.get("pin", "")).strip() == key:
                name = str(entry.get("name") or "").strip()
                if name:
                    return tx("Pin {number}: {name}", number=key, name=name)
        return tx("Pin {number}", number=key)

    def _add_preview_pin_function(self, number, side, bounds, fallback="", max_length=None):
        """Draw the documented function inward, separate from the pad number.

        This is a preview annotation, not a change to the electrical pad ID.
        Top/bottom rows use vertical text to leave adjacent pins readable.
        """
        name = next((str(row.get("name") or "").strip() for row in self.pinout or ()
                     if isinstance(row, dict)
                     and str(row.get("pin", "")).strip() == str(number).strip()), fallback)
        if not name:
            return
        label = self.preview_scene.addSimpleText(f"({name})")
        label.setData(0, "pin-function")
        label.setData(1, str(number))
        label.setBrush(QBrush(QColor(TEXT)))
        font = label.font()
        font.setPointSizeF(9)
        label.setFont(font)
        if side in ("top", "bottom"):
            label.setRotation(90)
        rect = label.mapRectToScene(label.boundingRect())
        length = rect.height() if side in ("top", "bottom") else rect.width()
        if max_length and length > max_length:
            label.setScale(max_length / length)
            rect = label.mapRectToScene(label.boundingRect())
        label.setToolTip(name)
        x = bounds.center().x() - rect.width() / 2
        y = bounds.center().y() - rect.height() / 2
        if side == "top":
            y = bounds.bottom() + 4
        elif side == "bottom":
            y = bounds.top() - rect.height() - 4
        elif side == "left":
            x = bounds.right() + 4
        else:
            x = bounds.left() - rect.width() - 4
        label.setPos(x - rect.left(), y - rect.top())
        label.setZValue(5)

    def _draw_preview_castellated(self, p: CastellatedModuleParams) -> None:
        """Anteprima del modulo castellated: corpo, piazzole e quote d'ingombro.

        Disegno separato da quello a matrice perche' la geometria e' diversa
        (origine al centro del corpo, pad rettangolari su piu' lati con passi
        indipendenti), ma come la' i pad vengono da pad_positions(): l'anteprima
        non puo' divergere dal footprint che verra' scritto.
        """
        self.preview_scene.clear()
        mm_to_px = 20.0
        built = self._built_footprint(lambda: build_castellated_module(p))

        self._draw_grid(
            mm_to_px, p.body_length, p.body_width, p.pad_overhang + p.pad_length,
            x0=-p.body_length / 2, y0=-p.body_width / 2,
            origin=castellated_preview_grid_origin(p),
        )

        silk_pen = QPen(QColor(_layer_color("F.SilkS")))
        silk_pen.setWidthF(1.5)
        half_l = p.body_length / 2 * mm_to_px
        half_w = p.body_width / 2 * mm_to_px
        self.preview_scene.addRect(-half_l, -half_w, 2 * half_l, 2 * half_w, silk_pen)

        pad_pen = QPen(QColor(SURFACE1))
        pad_brush = QBrush(QColor(BLUE))
        first_number = None
        xs_px, ys_px = [], []

        for placement in pad_positions(p):
            if first_number is None:
                first_number = placement.number
            w = placement.width * mm_to_px
            h = placement.height * mm_to_px
            x = placement.x * mm_to_px
            y = placement.y * mm_to_px

            copper = self._add_castellated_pad_shape(p, placement, mm_to_px, pad_pen,
                                                     pad_brush, xs_px, ys_px)
            tip = self._pad_tooltip(placement.number)
            if copper is not None:
                copper.setToolTip(tip)

            number = self.preview_scene.addSimpleText(placement.number)
            number.setToolTip(tip)
            number.setBrush(QBrush(QColor(BASE)))
            font = number.font()
            font.setPointSizeF(max(6.0, min(w, h) * 0.45))
            font.setBold(placement.number == first_number)
            number.setFont(font)
            rect = number.boundingRect()
            number.setPos(x - rect.width() / 2, y - rect.height() / 2)
            number.setZValue(5)
            bounds = number.sceneBoundingRect()
            if copper is not None:
                bounds = bounds.united(copper.sceneBoundingRect())
            self._add_preview_pin_function(placement.number, placement.edge, bounds,
                max_length=(p.body_width if placement.edge in ("top", "bottom")
                            else p.body_length) * mm_to_px * .3)

        if xs_px and ys_px:
            left, right = min(xs_px + [-half_l]), max(xs_px + [half_l])
            top, bottom = min(ys_px + [-half_w]), max(ys_px + [half_w])
            self._add_dim_h(-half_l, half_l, top - 40, self._dim_label("dim.body", p.body_length))
            self._add_dim_v(-half_w, half_w, left - 40, self._dim_label("dim.body", p.body_width))
            self._add_dim_h(left, right, bottom + 60, self._dim_label("dim.total", (right - left) / mm_to_px))

        self._draw_footprint_texts(mm_to_px, built)
        self._draw_footprint_drawings(mm_to_px)
        self.preview_scene.setBackgroundBrush(QBrush(QColor(BASE)))
        self._fit_preview()

    def _add_castellated_pad_shape(self, p, placement, mm_to_px, pen, brush,
                                   xs_px, ys_px) -> None:
        """@brief Draw one contact exactly as the footprint writes it.

        @param p Castellated module parameters.
        @param placement Contact from pad_positions().
        @param mm_to_px Scene scale.
        @param pen Outline pen of the copper.
        @param brush Copper fill.
        @param xs_px Extents accumulator (x, scene px), extended in place.
        @param ys_px Extents accumulator (y, scene px), extended in place.
        @note Three shapes, the same three the footprint builder writes: the
            continuous stadium of a half_and_through contact with its offset
            drill (user decision 2026-09-23; the module's edge half-hole is not
            part of the host-board land, so it is not drawn), the round ring of a through_hole contact, and the
            rectangular edge land of a half_hole contact. Drawing the edge
            rectangle for all three showed a footprint that is not the one
            written to the .kicad_mod.
        """
        from ..generators.castellated_module import CONTACT_THROUGH_HOLE, continuous_pad
        hole_brush = QBrush(QColor(BASE))
        hole_pen = QPen(QColor(SURFACE2))
        pad = continuous_pad(p, placement)
        if pad is not None:
            w, h = pad.width * mm_to_px, pad.height * mm_to_px
            x, y = pad.x * mm_to_px, pad.y * mm_to_px
            radius = min(w, h) / 2
            path = QPainterPath()
            path.addRoundedRect(QRectF(x - w / 2, y - h / 2, w, h), radius, radius)
            copper = self.preview_scene.addPath(path, pen, brush)
            copper.setZValue(1)
            # Solo il foro passante: il footprint e' la piazzola sulla scheda
            # che ospita il modulo, e li' il mezzo foro non esiste (e' una
            # lavorazione del bordo del MODULO, che vive nel modello 3D).
            if pad.drill and pad.drill > 0:
                d = pad.drill * mm_to_px
                hole = self.preview_scene.addEllipse(
                    pad.drill_x * mm_to_px - d / 2, pad.drill_y * mm_to_px - d / 2, d, d,
                    hole_pen, hole_brush)
                hole.setZValue(2)
            xs_px.extend([x - w / 2, x + w / 2])
            ys_px.extend([y - h / 2, y + h / 2])
            return copper
        w, h = placement.width * mm_to_px, placement.height * mm_to_px
        x, y = placement.x * mm_to_px, placement.y * mm_to_px
        if placement.style == CONTACT_THROUGH_HOLE:
            ring = self.preview_scene.addEllipse(x - w / 2, y - h / 2, w, h, pen, brush)
            ring.setZValue(1)
            # Foro del lato di questo contatto (i lati possono avere fori diversi).
            geometry = getattr(p, "contact_geometry", None)
            drill = geometry(placement).drill if callable(geometry) else p.thru_hole_drill
            d = drill * mm_to_px
            if d > 0:
                hole = self.preview_scene.addEllipse(x - d / 2, y - d / 2, d, d, hole_pen, hole_brush)
                hole.setZValue(2)
            copper = ring
        else:
            item = QGraphicsRectItem(x - w / 2, y - h / 2, w, h)
            item.setPen(pen)
            item.setBrush(brush)
            item.setZValue(1)
            self.preview_scene.addItem(item)
            copper = item
        xs_px.extend([x - w / 2, x + w / 2])
        ys_px.extend([y - h / 2, y + h / 2])
        return copper

    def _preview_params(self):
        """@brief Parameters the 2D preview draws, or None when there are none.

        @return The parameters of this session's last generation; otherwise
            the ones of the open project, chosen and validated like the
            footprint part of «Generate» (package, castellated module or pin
            header); None when the fields do not make a valid footprint.
        @note User, 2026-09-25: reopening a generated project showed an empty
            preview, because only a generation of THIS session set
            `_last_params`. The fields restored with the project are the ones
            its footprint was generated from.
        """
        try:
            params = (
                self._last_params if self._last_params is not None else
                self.package_geometry if self.package_geometry is not None else
                self._current_castellated_params() if self._is_castellated()
                else self._current_params()
            )
            if (isinstance(params, PinHeaderParams)
                    and (params.pad_overrides or {}) != self.header_pad_overrides):
                # The last generation froze the APPLIED pad overrides; the
                # preview draws the footprint draft (review A, 2026-10-14).
                params = replace(params, pad_overrides=deepcopy(self.header_pad_overrides))
            params.validate()
            params = self._host_footprint_params(params)
            self._set_mounting_error("")
        except (ValueError, TypeError) as exc:
            if self.combo_mounting.currentData() in {"headers", "headers_square"}:
                self._set_mounting_error(str(exc))
            # Campi incompleti o stato di progetto non coerente: nessun
            # disegno, mai un'eccezione fuori da un gestore Qt.
            return None
        return params

    def _draw_preview(self) -> None:
        # I parametri nascono prima delle schede (`_init_parameters`): la
        # tipologia scelta mentre si costruisce il tab Progetto li cambia
        # quando l'anteprima non esiste ancora, e non c'e' nulla da ridisegnare.
        if not hasattr(self, "preview_scene"):
            return
        params = self._preview_params()
        if hasattr(self, "preview_pad_hint"):
            self.preview_pad_hint.setVisible(isinstance(params, PackageGeometry))
        if params is None:
            self.preview_scene.clear()
            return
        if isinstance(params, PackageGeometry):
            self._draw_preview_package(params)
            return
        if isinstance(params, CastellatedModuleParams):
            self._draw_preview_castellated(params)
            return
        p = params
        self.preview_scene.clear()

        mm_to_px = 20.0
        margin = p.margin
        row_spacing = p.effective_row_spacing()
        width = (p.cols - 1) * p.pitch
        height = (p.rows - 1) * row_spacing

        from ..core.footprint import FpLine, FpRect, FpCircle
        try:
            footprint = build_pin_header(p)
        except (ValueError, TypeError, OverflowError) as exc:
            self._set_mounting_error(str(exc))
            return
        courtyard = next(g for g in footprint.graphics if g.layer == "F.CrtYd")
        # The generator's courtyard encloses the body AND populated copper.
        # Remove its clearance to dimension the actual component envelope.
        x0, y0 = courtyard.start[0] + margin, courtyard.start[1] + margin
        x1, y1 = courtyard.end[0] - margin, courtyard.end[1] - margin
        self._draw_grid(mm_to_px, x1 - x0, y1 - y0, max(margin, 1), x0=x0, y0=y0,
                        origin=text_grid_origin(footprint))
        silk_pen = QPen(QColor(_layer_color("F.SilkS")))
        silk_pen.setWidthF(1.5)
        for graphic in footprint.graphics:
            if graphic.layer != "F.SilkS":
                continue
            if isinstance(graphic, FpLine):
                self.preview_scene.addLine(*(v * mm_to_px for v in (*graphic.start, *graphic.end)), silk_pen)
            elif isinstance(graphic, FpRect):
                self.preview_scene.addRect(graphic.start[0] * mm_to_px, graphic.start[1] * mm_to_px,
                                           (graphic.end[0] - graphic.start[0]) * mm_to_px,
                                           (graphic.end[1] - graphic.start[1]) * mm_to_px, silk_pen)
            elif isinstance(graphic, FpCircle):
                r = graphic.radius * mm_to_px
                self.preview_scene.addEllipse(graphic.center[0] * mm_to_px - r,
                                              graphic.center[1] * mm_to_px - r, 2 * r, 2 * r,
                                              silk_pen, QBrush(silk_pen.color()) if graphic.fill else QBrush())

        pad_pen = QPen(QColor(SURFACE1))
        pad_brush = QBrush(QColor(BLUE))

        # Stessa funzione usata dal generatore 2D e da quello 3D: l'anteprima non
        # deve poter divergere dal footprint realmente prodotto (e cosi' rispetta
        # anche la mappa esplicita dei pin letta dal datasheet).
        xs_px, ys_px, label_bounds = [], [], []
        r = (p.pad_diameter / 2) * mm_to_px
        for pin, x_mm, y_mm in pin_positions(p):
            x, y = x_mm * mm_to_px, y_mm * mm_to_px
            xs_px.append(x)
            ys_px.append(y)
            override = (p.pad_overrides or {}).get(str(pin), {})
            size = float(override.get("size", p.pad_diameter))
            radius = size * mm_to_px / 2
            pad_shape = override.get("shape", "rect" if pin == 1 or p.pad_shape == "rect"
                                     else "circle")
            if pad_shape == "rect":
                item: QGraphicsRectItem | QGraphicsEllipseItem = QGraphicsRectItem(
                    x - r, y - r, 2 * r, 2 * r
                )
            else:
                item = QGraphicsEllipseItem(x - radius, y - radius,
                                            2 * radius, 2 * radius)
            if pad_shape == "rect":
                item.setRect(x - radius, y - radius, 2 * radius, 2 * radius)
            item.setPen(pad_pen)
            item.setBrush(pad_brush)
            item.setToolTip(self._pad_tooltip(pin))
            item.setData(0x504144, f"header:{pin}")
            self.preview_scene.addItem(item)

            hole_r = p.drill * mm_to_px / 2
            hole = self.preview_scene.addEllipse(
                x - hole_r, y - hole_r, 2 * hole_r, 2 * hole_r,
                QPen(QColor(SURFACE1)), QBrush(QColor(BASE)))
            hole.setZValue(1)

            # Keep the exported drill visible in the grid-header path too.
            number = self.preview_scene.addSimpleText(str(pin))
            number.setToolTip(item.toolTip())
            number.setBrush(QBrush(QColor(TEXT)))
            font = number.font()
            font.setPointSizeF(max(6.0, radius * 0.55))
            font.setBold(True)
            number.setFont(font)
            number_rect = number.boundingRect()
            number.setPos(x - number_rect.width() / 2,
                          y - radius - number_rect.height() - 3 if y_mm <= height / 2 else
                          y + radius + 3)
            number.setZValue(5)
            label_bounds.append(number.sceneBoundingRect())

        left, right = x0 * mm_to_px, x1 * mm_to_px
        top, bottom = y0 * mm_to_px, y1 * mm_to_px

        # Quota vicina al componente: passo (in alto) e distanza righe (a sinistra)
        labels_top = min((rect.top() for rect in label_bounds), default=top)
        labels_bottom = max((rect.bottom() for rect in label_bounds), default=bottom)
        if p.cols >= 2:
            self._add_dim_h(0, p.pitch * mm_to_px, min(top, labels_top) - 20,
                            self._dim_label("dim.pitch", p.pitch))
        if p.rows >= 2:
            self._add_dim_v(0, row_spacing * mm_to_px, left - 40, self._dim_label("dim.row_spacing", row_spacing))

        # Catena di quote: margine sinistro / span pin / margine destro (sotto)
        def chain_fits(start, end):
            from PyQt6.QtWidgets import QGraphicsSimpleTextItem
            label = QGraphicsSimpleTextItem(f"{(end - start) / mm_to_px:.2f} mm")
            return end - start >= label.boundingRect().width() + 10

        if xs_px:
            leftmost_x, rightmost_x = min(xs_px), max(xs_px)
            chain_y = max(bottom, labels_bottom) + 25
            if chain_fits(left, leftmost_x):
                self._add_dim_h(left, leftmost_x, chain_y, f"{(leftmost_x - left) / mm_to_px:.2f} mm")
            if chain_fits(leftmost_x, rightmost_x):
                self._add_dim_h(leftmost_x, rightmost_x, chain_y, f"{(rightmost_x - leftmost_x) / mm_to_px:.2f} mm")
            if chain_fits(rightmost_x, right):
                self._add_dim_h(rightmost_x, right, chain_y, f"{(right - rightmost_x) / mm_to_px:.2f} mm")

        # Catena di quote: margine superiore / span pin / margine inferiore (a destra)
        if ys_px:
            topmost_y, bottommost_y = min(ys_px), max(ys_px)
            chain_x = right + 25
            if chain_fits(top, topmost_y):
                self._add_dim_v(top, topmost_y, chain_x, f"{(topmost_y - top) / mm_to_px:.2f} mm")
            if chain_fits(topmost_y, bottommost_y):
                self._add_dim_v(topmost_y, bottommost_y, chain_x, f"{(bottommost_y - topmost_y) / mm_to_px:.2f} mm")
            if chain_fits(bottommost_y, bottom):
                self._add_dim_v(bottommost_y, bottom, chain_x, f"{(bottom - bottommost_y) / mm_to_px:.2f} mm")

        # Actual body/copper envelope, excluding courtyard clearance.
        self._add_dim_h(left, right, max(bottom, labels_bottom) + 60, self._dim_label("dim.total", x1 - x0))
        self._add_dim_v(top, bottom, right + 60, self._dim_label("dim.total", y1 - y0))

        self._draw_footprint_texts(mm_to_px, footprint)
        self._draw_footprint_drawings(mm_to_px)
        self.preview_scene.setBackgroundBrush(QBrush(QColor(BASE)))
        self._fit_preview()

    def _draw_preview_package(self, package: PackageGeometry) -> None:
        """@brief Draw package copper shapes and drill openings as exported to KiCad."""
        self.preview_scene.clear()
        mm_to_px = 20.0
        built = self._built_footprint(lambda: build_package(package))
        pins = package.pins_seen_from_top()
        margin = 1.0
        silk_pen = QPen(QColor(_layer_color("F.SilkS")))
        silk_pen.setWidthF(1.5)
        if package.body_length_mm and package.body_width_mm:
            # Same outline as build_package: the body turns with the package.
            from ..core.package_geometry import pad_outline
            corners = pad_outline(0.0, 0.0, package.body_length_mm, package.body_width_mm,
                                  package.orientation_deg)
            self.preview_scene.addPolygon(
                QPolygonF([QPointF(x * mm_to_px, y * mm_to_px) for x, y in corners]), silk_pen)
            body_l = 2 * max(abs(x) for x, _ in corners)
            body_w = 2 * max(abs(y) for _, y in corners)
        else:
            body_l = max((abs(p.x_mm) for p in pins), default=2.0) * 2 + 2
            body_w = max((abs(p.y_mm) for p in pins), default=2.0) * 2 + 2
            self.preview_scene.addRect(
                -body_l / 2 * mm_to_px, -body_w / 2 * mm_to_px,
                body_l * mm_to_px, body_w * mm_to_px, silk_pen,
            )
        pad_pen = QPen(QColor(SURFACE1))
        pad_brush = QBrush(QColor(BLUE))
        # Display margin only pads the grid, never the measured component.
        extent = ([-body_l / 2, -body_w / 2, body_l / 2, body_w / 2]
                  if package.body_length_mm and package.body_width_mm else
                  [math.inf, math.inf, -math.inf, -math.inf])
        for pad_index, pin in enumerate(pins):
            x, y = pin.x_mm * mm_to_px, pin.y_mm * mm_to_px
            w, h = pin.pad_length_mm * mm_to_px, pin.pad_width_mm * mm_to_px
            if pin.shape == "circle":
                item = self.preview_scene.addEllipse(x - w / 2, y - h / 2, w, h)
            elif pin.shape in ("oval", "roundrect"):
                path = QPainterPath()
                radius = min(w, h) * (0.5 if pin.shape == "oval" else 0.25)
                path.addRoundedRect(QRectF(x - w / 2, y - h / 2, w, h), radius, radius)
                item = self.preview_scene.addPath(path)
            else:
                item = self.preview_scene.addRect(x - w / 2, y - h / 2, w, h)
            item.setTransformOriginPoint(x, y)
            item.setRotation(-pin.rotation_deg)  # KiCad is counter-clockwise, Qt clockwise.
            item.setPen(pad_pen)
            item.setBrush(pad_brush)
            item.setToolTip(self._pad_tooltip(pin.number))
            item.setData(0x504144, f"package:{pad_index}:{pin.number}")
            drilled = pin.pad_type in ("thru_hole", "np_thru_hole") and pin.drill_mm
            if drilled:
                diameter = pin.drill_mm * mm_to_px
                hole = self.preview_scene.addEllipse(
                    x - diameter / 2, y - diameter / 2, diameter, diameter,
                    QPen(QColor(SURFACE1)), QBrush(QColor(BASE)))
                hole.setZValue(1)
            angle = math.radians(pin.rotation_deg)
            ca, sa = abs(math.cos(angle)), abs(math.sin(angle))
            a, b = pin.pad_length_mm / 2, pin.pad_width_mm / 2
            if pin.shape == "circle":
                dx, dy = math.hypot(a * ca, b * sa), math.hypot(a * sa, b * ca)
            else:
                radius = min(a, b) * (1 if pin.shape == "oval" else
                                         .5 if pin.shape == "roundrect" else 0)
                dx = (a - radius) * ca + (b - radius) * sa + radius
                dy = (a - radius) * sa + (b - radius) * ca + radius
            if drilled:
                dx, dy = max(dx, pin.drill_mm / 2), max(dy, pin.drill_mm / 2)
            extent = [min(extent[0], pin.x_mm - dx), min(extent[1], pin.y_mm - dy),
                      max(extent[2], pin.x_mm + dx), max(extent[3], pin.y_mm + dy)]
            number = self.preview_scene.addSimpleText(str(pin.number))
            number.setToolTip(item.toolTip())
            number.setBrush(QBrush(QColor(TEXT if drilled else BASE)))
            font = number.font(); font.setPointSizeF(max(6.0, min(w, h) * .45)); font.setBold(True)
            number.setFont(font)
            rect = number.boundingRect()
            label_x, label_y = x - rect.width() / 2, y - rect.height() / 2
            if drilled:
                bounds = item.sceneBoundingRect()
                # Keep the drill visible. Put edge-contact labels inward so
                # they also remain clear of the outer dimension lines.
                if pin.side == "top":
                    label_y = bounds.bottom() + 3
                elif pin.side == "left":
                    label_x = bounds.right() + 3
                elif pin.side == "right":
                    label_x = bounds.left() - rect.width() - 3
                else:
                    label_y = bounds.top() - rect.height() - 3
            number.setPos(label_x, label_y)
            number.setZValue(5)
            if pin.side in ("top", "bottom", "left", "right"):
                self._add_preview_pin_function(pin.number, pin.side,
                    number.sceneBoundingRect().united(item.sceneBoundingRect()), pin.name,
                    max_length=(body_w if pin.side in ("top", "bottom") else body_l) * mm_to_px * .3)
        left, top, right, bottom = [value * mm_to_px for value in extent]
        self._draw_grid(mm_to_px, body_l, body_w, margin,
                        x0=-body_l / 2, y0=-body_w / 2,
                        origin=package_preview_grid_origin(package))
        self._add_dim_h(left, right, bottom + 40, self._dim_label("dim.total", extent[2] - extent[0]))
        self._add_dim_v(top, bottom, right + 40, self._dim_label("dim.total", extent[3] - extent[1]))
        self._draw_footprint_texts(mm_to_px, built)
        self._draw_footprint_drawings(mm_to_px)
        self.preview_scene.setBackgroundBrush(QBrush(QColor(BASE)))
        self._fit_preview()

    def _edit_preview_pad(self, token: str) -> None:
        parts = str(token).split(":", 2)
        mode = self.combo_mounting.currentData()
        if parts[0] == "header" and len(parts) == 2:
            self._edit_header_pad(parts[1])
        elif parts[0] == "package" and len(parts) == 3:
            if mode in {"headers", "headers_square"}:
                self._edit_header_pad(parts[2])
            else:
                self._edit_package_pad(int(parts[1]))

    def _edited_pad_reference(self) -> tuple:
        """@brief Pad diameter and drill the pad editor compares with.

        @return (pad_mm, drill_mm) of the component's own pin grid when the
            preview draws it (direct mode of a THT part), else of the host header.
        """
        try:
            drawn = self._preview_params()
        except Exception:  # noqa: BLE001 - a preview that cannot be built has no reference
            drawn = None
        if isinstance(drawn, PinHeaderParams):
            return float(drawn.pad_diameter), float(drawn.drill)
        return float(self.spin_header_pad.value()), float(self.spin_header_drill.value())

    def _edit_header_pad(self, number: str) -> None:
        """Set an individual circular or square land in the header footprint."""
        number = str(number)
        mode = self.combo_mounting.currentData()
        base_shape = "rect" if mode == "headers_square" or (mode == "headers" and number == "1") else "circle"
        current = self.header_pad_overrides.get(number, {})
        current_shape = current.get("shape", base_shape)
        # Reference pad and drill of the footprint actually drawn: in direct
        # mode they are the component's own, not the host header's (review C, 7).
        reference_pad, reference_drill = self._edited_pad_reference()
        current_size = float(current.get("size", reference_pad))
        from PyQt6.QtWidgets import QDialogButtonBox
        dialog = QDialog(self)
        dialog.setWindowTitle(self.i18n.text("Modifica pad {number}").format(number=number))
        form = QFormLayout(dialog)
        shape = QComboBox(dialog)
        shape.addItem(self.i18n.text("Predefinito"), "default")
        shape.addItem(self.i18n.text("Cerchio"), "circle")
        shape.addItem(self.i18n.text("Quadrato"), "rect")
        if number in self.header_pad_overrides:
            shape.setCurrentIndex(shape.findData(current_shape))
        else:
            shape.setCurrentIndex(0)
        form.addRow(self.i18n.text("Forma del pad:"), shape)
        dimension_label = QLabel()
        size = QDoubleSpinBox(dialog)
        size.setRange(0.1, 10.0)
        size.setDecimals(2)
        size.setSingleStep(0.1)
        size.setSuffix(" mm")
        size.setValue(current_size)
        form.addRow(dimension_label, size)

        def update_dimension_label():
            selected = shape.currentData()
            effective_shape = (base_shape if selected == "default" else selected)
            dimension_label.setText(self.i18n.text(
                "Lato pad:" if effective_shape == "rect" else "Diametro pad:"))
            size.setEnabled(selected != "default")

        shape.currentIndexChanged.connect(update_dimension_label)
        update_dimension_label()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                   QDialogButtonBox.StandardButton.Cancel, parent=dialog)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        updated = deepcopy(self.header_pad_overrides)
        if shape.currentData() == "default":
            updated.pop(number, None)
        else:
            pad_size = size.value()
            if pad_size <= reference_drill:
                QMessageBox.warning(self, self.i18n.text("Dimensione pad non valida"),
                                    self.i18n.text("Il pad deve essere più grande del foro."))
                return
            updated[number] = {"shape": shape.currentData(), "size": pad_size}
        if updated == self.header_pad_overrides:
            return
        # CONTRACT CHANGED 2026-10-14 (footprint draft, user): the land is a
        # draft edit; «Applica» saves it and rewrites the .kicad_mod.
        self._edit_footprint_draft({"pad_overrides": updated})

    # ── Footprint texts (Reference, Value) ───────────────────────────

    @staticmethod
    def _built_footprint(build):
        """@brief Build the footprint a preview drawer shows, or None when it does not build.

        @param build Callable returning the generator's Footprint.
        @return The Footprint, or None on a geometry error (the drawer reports
            invalid geometry itself and still draws what it can).
        """
        try:
            return build()
        except (ValueError, TypeError, OverflowError, ArithmeticError):
            return None

    def _draw_footprint_texts(self, mm_to_px: float, footprint=None) -> None:
        """@brief Draw «REF**» and the footprint name where the file puts them, draggable.

        @param mm_to_px Scene scale of the drawer that calls this.
        @param footprint The footprint the generator writes for this preview,
            or a callable building it; its default Reference/Value anchors
            (placed by the generator on this drawer's grid, decision D1) are
            shown as they are, so the preview equals the file. None, or a
            build that fails, falls back to `core.footprint.DEFAULT_TEXT_POSITIONS`.
        @note Called by the three 2D drawers after the geometry: the texts
            are the footprint's own Reference (silkscreen) and Value
            (fabrication layer) properties, at their chosen or default
            position. Each text is
            a group whose position IS the KiCad anchor (`at`, the centre of
            the text): a small cross marks it, because a centred text alone
            does not show where it is anchored (user, 2026-10-13). The group
            and its children carry the key in `TEXT_KEY_ROLE` for the drag.
        """
        from PyQt6.QtWidgets import QGraphicsItemGroup, QGraphicsLineItem
        from ..core.footprint import DEFAULT_TEXT_POSITIONS, PROPERTY_TEXT_SIZE, TEXT_KEYS
        from .footprint_view import TEXT_KEY_ROLE
        defaults = DEFAULT_TEXT_POSITIONS
        try:
            built = footprint() if callable(footprint) else footprint
            if built is not None:
                defaults = {"Reference": built.reference_at, "Value": built.value_at}
        except (ValueError, TypeError, OverflowError, ArithmeticError):
            # The drawer already reported an invalid geometry; the texts
            # still show, at the fallback places.
            pass
        # An unnamed project still shows (and can drag) its Value text.
        texts = {"Reference": "REF**", "Value": self._component_name() or "Value"}
        colours = {"Reference": TEXT, "Value": SUBTEXT}
        hints = {"Reference": self.i18n.text(
                     "Riferimento (REF**) sulla serigrafia: trascinalo dove deve stare; tasto destro per la posizione predefinita."),
                 "Value": self.i18n.text(
                     "Valore sul layer di fabbricazione: trascinalo dove deve stare; tasto destro per la posizione predefinita.")}
        # The anchor cross in the accent colour, wider than the glyphs are
        # tall, so it reads through the text it is centred on.
        arm = 0.75 * mm_to_px
        for key in TEXT_KEYS:
            x_mm, y_mm = self.footprint_text_positions.get(key, defaults[key])
            group = QGraphicsItemGroup()
            self.preview_scene.addItem(group)
            # Capitals PROPERTY_TEXT_SIZE mm tall, as in the file (user,
            # 2026-10-15); centred on the anchor as KiCad centres the text:
            # half the width to the left, half the cap height above the baseline.
            text = _kicad_size_text_item(texts[key], PROPERTY_TEXT_SIZE, colours[key], mm_to_px)
            self.preview_scene.addItem(text)
            scale = text.scale()
            rect = text.boundingRect()
            centre = _drawing_text_baseline(text) - _cap_height_px(text.font()) / 2
            text.setPos(-rect.width() * scale / 2, -centre * scale)
            group.addToGroup(text)
            cross_pen = QPen(QColor(BLUE))
            cross_pen.setCosmetic(True)
            cross_pen.setWidthF(1.2)
            # Lines only: the preview tests count ellipses and paths as pads
            # and drills, and the anchor mark is neither.
            for mark in (QGraphicsLineItem(-arm, 0, arm, 0), QGraphicsLineItem(0, -arm, 0, arm)):
                mark.setPen(cross_pen)
                group.addToGroup(mark)
            group.setPos(x_mm * mm_to_px, y_mm * mm_to_px)
            tooltip = hints[key] + "\n" + self.i18n.text("Ancoraggio: {x} mm, {y} mm").format(
                x=f"{x_mm:g}", y=f"{y_mm:g}")
            for item in (group, *group.childItems()):
                item.setData(TEXT_KEY_ROLE, key)
                item.setCursor(Qt.CursorShape.SizeAllCursor)
                item.setToolTip(tooltip)
            group.setZValue(50)

    def _set_footprint_text_position(self, key: str, position) -> bool:
        """@brief Put a text position (None: back to default) in the footprint draft and redraw.

        @param key One of `core.footprint.TEXT_KEYS`.
        @param position (x, y) mm in footprint coordinates, or None for the default.
        @return True when the draft took the position (or already had it);
            False for an unknown key or an invalid position.
        @note CONTRACT CHANGED 2026-10-14 (footprint draft, user): the
            position is a draft edit like the drawings, saved in the project
            and written to the .kicad_mod by «Applica» (`apply_footprint_draft`).
        """
        from ..core.footprint import TEXT_KEYS, validate_text_positions
        if key not in TEXT_KEYS:
            return False
        updated = dict(self.footprint_text_positions)
        if position is None:
            if key not in updated:
                return True
            updated.pop(key)
        else:
            checked = validate_text_positions({key: list(position)})
            if key not in checked:
                return False
            if updated.get(key) == checked[key]:
                self._draw_preview()
                return True
            updated[key] = checked[key]
        return self._edit_footprint_draft({"texts": updated})

    def _on_footprint_text_moved(self, key: str, x_scene: float, y_scene: float) -> None:
        """@brief Slot of the view: a text was dropped at a scene point; snap it and keep it.

        @param key Text key.
        @param x_scene Drop centre, scene units (20 px/mm in every drawer).
        @param y_scene Drop centre, scene units.
        """
        # Same snap as the drawings: the drawing grid passes through the
        # first pad, so a text and a drawing dropped at the same point land
        # on the same node.
        self._set_footprint_text_position(key, self._snapped_draw_point(x_scene, y_scene))

    def _footprint_text_context_menu(self, key: str) -> None:
        """@brief Right click on a text: offer its default position."""
        from ..core.footprint import TEXT_KEYS
        if key not in TEXT_KEYS:
            return
        menu = QMenu(self)
        reset = menu.addAction(self.i18n.text("Posizione predefinita"))
        reset.setEnabled(key in self.footprint_text_positions)
        chosen = menu.exec(QCursor.pos())
        if chosen is reset:
            self._set_footprint_text_position(key, None)

    def _edit_package_pad(self, pad_index: int) -> None:
        """@brief Edit the selected package pad's shape, dimensions and, for a holed pad, its drill.

        @param pad_index Index of the pad in `package_geometry.pins`.
        @note Opened from the context click of the 2D preview. The edited
            package is validated as a whole; a rejected value restores the
            previous pad and tells why.
        """
        package = self.package_geometry
        if package is None or not 0 <= pad_index < len(package.pins):
            return
        pin = package.pins[pad_index]
        from PyQt6.QtWidgets import QDialogButtonBox
        dialog = QDialog(self)
        dialog.setWindowTitle(self.i18n.text("Modifica pad {number}").format(number=pin.number))
        form = QFormLayout(dialog)
        shape = QComboBox(dialog)
        choices = (
            ("Cerchio", "circle"), ("Quadrato", "square"),
            ("Rettangolo", "rect"), ("Ovale", "oval"),
            ("Rettangolo arrotondato", "roundrect"), ("Trapezio", "trapezoid"),
        )
        for label, value in choices:
            shape.addItem(self.i18n.text(label), value)
        current_shape = ("square" if pin.shape == "rect" and
                         abs(pin.pad_length_mm - pin.pad_width_mm) < 1e-9 else pin.shape)
        shape_index = shape.findData(current_shape)
        shape.setCurrentIndex(max(shape_index, 0))
        form.addRow(self.i18n.text("Forma del pad:"), shape)

        def size_control(value):
            control = QDoubleSpinBox(dialog)
            control.setRange(0.05, 100.0)
            control.setDecimals(3)
            control.setSingleStep(0.1)
            control.setSuffix(" mm")
            control.setValue(float(value))
            return control

        width = size_control(pin.pad_length_mm)
        height = size_control(pin.pad_width_mm)
        form.addRow(self.i18n.text("Larghezza:"), width)
        form.addRow(self.i18n.text("Altezza:"), height)
        # Only a pad that HAS a hole gets the field: an SMD pad has none and
        # offering one would invent a drill. `package.validate()` rejects a
        # drill at or above the smaller pad side.
        drill = None
        if pin.drill_mm is not None:
            drill = QDoubleSpinBox(dialog)
            drill.setRange(0.05, 20.0)
            drill.setDecimals(3)
            drill.setSingleStep(0.05)
            drill.setSuffix(" mm")
            drill.setValue(float(pin.drill_mm))
            form.addRow(self.i18n.text("Foro:"), drill)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                   QDialogButtonBox.StandardButton.Cancel, parent=dialog)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        selected_shape = shape.currentData()
        pad_width, pad_height = width.value(), height.value()
        if selected_shape == "square":
            selected_shape = "rect"
            pad_height = pad_width
        updated = replace(pin, shape=selected_shape,
                          pad_length_mm=pad_width, pad_width_mm=pad_height,
                          **({"drill_mm": drill.value()} if drill is not None else {}))
        new_pins = list(package.pins)
        new_pins[pad_index] = updated
        if new_pins == list(package.pins):
            return
        # Validated on a copy: the package object itself is never mutated
        # (a worker may hold it, see `_rebind_package_pins`).
        try:
            replace(package, pins=new_pins).validate()
        except (TypeError, ValueError) as exc:
            QMessageBox.warning(self, self.i18n.text("Dimensione pad non valida"), str(exc))
            return
        # CONTRACT CHANGED 2026-10-14 (footprint draft, user): the pad is a
        # draft edit; «Applica» saves it and rewrites the .kicad_mod.
        self._edit_footprint_draft({"package_pins": new_pins})

    #: Italian fallbacks of the dimension-label keys, used only until the
    #: catalogue carries them: `I18n.tr` would otherwise show the bare key.
    _DIM_LABEL_FALLBACKS = {
        "dim.body": "corpo {value:.2f} mm",
        "dim.total": "totale {value:.2f} mm",
        "dim.pitch": "passo {value:.2f} mm",
        "dim.row_spacing": "righe {value:.2f} mm",
    }

    def _dim_label(self, key: str, value_mm: float) -> str:
        """@brief Translated label of a dimension line in the 2D preview.

        @param key Catalogue key (`dim.body`, `dim.total`, `dim.pitch`, `dim.row_spacing`).
        @param value_mm Measured value in millimetres.
        @return The label in the interface language, e.g. «body 12.00 mm».
        @note Note 8 of the user (2026-09-23): these words were Italian
            literals inside f-strings, so the drawing stayed Italian in any
            language. The number is a parameter of the entry, formatted by the
            entry itself (`{value:.2f}`), so every language keeps two decimals.
        """
        return self.i18n.tr(key, self._DIM_LABEL_FALLBACKS.get(key), value=float(value_mm))

    def _add_dim_h(self, x1: float, x2: float, y: float, text: str) -> None:
        """Disegna una linea di quota orizzontale con tacche e testo centrato."""
        pen = QPen(QColor(YELLOW))
        pen.setWidthF(1.0)
        tick = 5
        self.preview_scene.addLine(x1, y - tick, x1, y + tick, pen)
        self.preview_scene.addLine(x2, y - tick, x2, y + tick, pen)
        self.preview_scene.addLine(x1, y, x2, y, pen)
        label = self.preview_scene.addSimpleText(text)
        label.setBrush(QBrush(QColor(YELLOW)))
        label.setPos((x1 + x2) / 2 - label.boundingRect().width() / 2, y - 18)

    def _add_dim_v(self, y1: float, y2: float, x: float, text: str) -> None:
        """Disegna una linea di quota verticale con tacche e testo ruotato.

        Il testo corre lungo la quota (come nei disegni tecnici): scritto in
        orizzontale, le etichette di quote verticali vicine tra loro -- catena
        dei margini e quota totale -- finivano una sopra l'altra.
        """
        pen = QPen(QColor(YELLOW))
        pen.setWidthF(1.0)
        tick = 5
        self.preview_scene.addLine(x - tick, y1, x + tick, y1, pen)
        self.preview_scene.addLine(x - tick, y2, x + tick, y2, pen)
        self.preview_scene.addLine(x, y1, x, y2, pen)

        label = self.preview_scene.addSimpleText(text)
        label.setBrush(QBrush(QColor(YELLOW)))
        label.setRotation(-90)
        # Ruotando di -90 attorno all'origine, il testo si estende verso l'alto
        # (altezza -> larghezza): si riposiziona per centrarlo sulla quota.
        rect = label.boundingRect()
        label.setPos(x + 6, (y1 + y2) / 2 + rect.width() / 2)

    def _sync_3d_pin_functions(self):
        setter = getattr(self.preview_3d, 'set_pin_functions', None)
        if callable(setter):
            setter(self.pinout)

    def _draw_preview_3d(self, mesh_failed: bool = False) -> None:
        """@brief Draw the generated mesh when there is one, else the cheap preview.

        @param mesh_failed True when the mesh was just found unreadable (off-thread
            preparation failed and was logged): it is not tried again here.
        """
        self._sync_3d_pin_functions()
        # Never replace a generated model with a synthetic matrix preview
        # simply because the user revisits the 3D tab.
        if not mesh_failed and self._last_mesh_path and Path(self._last_mesh_path).is_file():
            try:
                self.preview_3d.render_mesh(self._last_mesh_path)
                return
            except Exception as exc:
                self._append_log(tx("Anteprima mesh non disponibile: {exc}", exc=exc))
        if self.package_geometry is not None:
            self.preview_3d.clear_preview()
            package = self.package_geometry
            if not all(value is not None for value in (
                    package.body_length_mm, package.body_width_mm, package.body_height_mm)):
                self._set_3d_status(self.i18n.text(
                    "Completa le quote documentate di lunghezza, larghezza e altezza del corpo "
                    "per generare il modello 3D."), STATUS_WARNING)
            else:
                self._set_3d_status(*self._no_3d_status())
            return
        if self._is_castellated():
            # Per i moduli non esiste un'approssimazione parametrica da
            # disegnare al volo (il solido nasce dal taglio dei semi-fori):
            # si mostra la geometria reale appena generata, se c'e'.
            if (not mesh_failed and self._last_mesh_path
                    and Path(self._last_mesh_path).is_file()):
                try:
                    self.preview_3d.render_mesh(self._last_mesh_path)
                    self._set_3d_status(self.i18n.text(
                        "Geometria reale del modulo (PCB con semi-fori sul bordo "
                        "e blocco dei componenti)."
                    ), STATUS_INFO)
                    return
                except Exception as exc:  # noqa: BLE001 - anteprima best-effort
                    self._append_log(tx("Anteprima mesh non disponibile: {exc}", exc=exc))
            self.preview_3d.clear_preview()
            self._set_3d_status(*self._no_3d_status(
                "Il modello del modulo (PCB con semi-fori + componenti) si vede qui dopo "
                "la generazione con FreeCAD."))
            return

        body = self._current_body_params()
        if body is None:
            self.preview_3d.clear_preview()
            self._set_3d_status(*self._no_3d_status())
            return

        try:
            params = self._current_params()
            params.validate()
            self.preview_3d.render_params(params, body)
        except ValueError as exc:
            self.preview_3d.clear_preview()
            self._set_3d_status(
                self.i18n.tr("status.parameter_error", error=exc), STATUS_ERROR)
            return

        self._set_3d_status(self.i18n.text(
            "Anteprima approssimata (poligoni, per farsi un'idea dell'ingombro). "
            "Il modello .step esatto arriva con la generazione via FreeCAD."
        ), STATUS_INFO)

    # ── Cambio tab ─────────────────────────────────────────────────

    def _on_tab_changed(self, index: int) -> None:
        self._refresh_output_actions()
        # Il riallineamento puo' aver spento questa scheda e spostato la
        # finestra altrove (currentChanged annidato): il lavoro di una scheda
        # che non si vede piu' non si fa (audit 25/09).
        if index != self.tabs.currentIndex():
            return
        if index == TAB_PROJECT:
            self._refresh_docs_list()
        elif index == TAB_PREVIEW:
            self._draw_preview()
        elif index == TAB_SYMBOL:
            self._refresh_symbol_tab()
        elif index == TAB_MODEL_3D:
            self._enter_3d_tab()

    # ── Ingresso nella scheda 3D: caricamento con indicatore ───────

    def _mesh_already_showing(self, path) -> bool:
        """@brief Ask the viewer whether that exact, unchanged mesh is on screen.

        @param path Mesh file about to be shown.
        @return True only when the viewer says so; False when it cannot tell.
        @note `Preview3DVtk.is_showing` is optional (older viewers and the
            test stubs do not have it) and must never break the tab switch:
            any failure means «not known to be showing», i.e. render it.
        """
        is_showing = getattr(self.preview_3d, "is_showing", None)
        if not callable(is_showing):
            return False
        try:
            return bool(is_showing(path))
        except Exception:  # noqa: BLE001 - la risposta e' solo un'ottimizzazione
            return False

    def _enter_3d_tab(self) -> None:
        """@brief Show the model when the user opens the 3D tab, without freezing silently.

        @note Note 10 of the user (2026-09-23): `render_mesh` on a large
            generated mesh (13 MB of JSON on the user's board) blocked the
            window for seconds right after the click, with no feedback. Now:
            nothing heavy happens when the viewer already shows that mesh;
            otherwise the loading indicator appears FIRST and the render is
            deferred with `QTimer.singleShot(0, …)`, so the tab switch and the
            indicator are painted before the event loop is blocked.
        @note Without a generated mesh the preview is the parametric
            approximation (or an empty view with a hint), which is cheap and
            stays synchronous.
        """
        if not self._ensure_preview_3d():
            return
        path = self._last_mesh_path
        if not (path and Path(path).is_file()):
            self._draw_preview_3d()
            return
        if self._mesh_already_showing(path):
            return
        if (self._rendering_3d or self._pending_3d_render is not None
                or self._mesh_request is not None):
            # Gia' in corso o gia' accodato: un secondo rendering della stessa
            # scena non aggiunge nulla, e accodarlo lo farebbe partire due volte.
            return
        self._pending_3d_render = (self._model_revision, Path(path))
        self._show_3d_loading(True)
        QTimer.singleShot(0, self._run_pending_3d_render)

    def _run_pending_3d_render(self) -> None:
        """@brief Deferred half of `_enter_3d_tab`: render, then always hide the indicator.

        @note Skipped -- indicator hidden, nothing rendered -- when, between the
            click and this call, the data changed (model revision), the mesh
            was replaced or invalidated, or the user left the 3D tab. In the
            last case the next visit schedules it again, because the viewer
            still does not show that mesh.
        """
        pending, self._pending_3d_render = self._pending_3d_render, None
        try:
            if pending is None:
                return
            revision, path = pending
            current = self._last_mesh_path
            if (revision != self._model_revision
                    or current is None or Path(current) != path
                    or self.tabs.currentWidget() is not getattr(self, "model_3d_page", None)):
                return
            if self._prepare_mesh_then(path, partial(self._finish_prepared_3d_render, path),
                                       partial(self._prepared_3d_render_failed, path)):
                return  # l'indicatore resta finche' la mesh non e' pronta
            self._rendering_3d = True
            self._draw_preview_3d()
        finally:
            self._rendering_3d = False
            if self._mesh_request is None:
                self._show_3d_loading(False)

    def _still_showing_3d_tab_for(self, path: Path) -> bool:
        """True when @p path is still the current mesh and the 3D tab is still in front."""
        current = self._last_mesh_path
        return (current is not None and Path(current) == path
                and self.tabs.currentWidget() is getattr(self, "model_3d_page", None))

    def _finish_prepared_3d_render(self, path: Path) -> None:
        """@brief Tab-entry render once the viewer has prepared @p path off the GUI thread.

        @param path Mesh requested by `_run_pending_3d_render`.
        @note Same guards as the deferred render: a replaced mesh or a tab the
            user has left is not drawn (the prepared scene stays cached, so the
            next visit is quick).
        """
        if not self._still_showing_3d_tab_for(path):
            return
        self._rendering_3d = True
        try:
            self._draw_preview_3d()
        finally:
            self._rendering_3d = False

    def _prepared_3d_render_failed(self, path: Path, error: str) -> None:
        """@brief The mesh of the tab entry is unreadable: log once, show the fallback preview.

        @param path Mesh requested by `_run_pending_3d_render`.
        @param error Error text from the viewer.
        """
        self._append_log(tx("Anteprima mesh non disponibile: {exc}", exc=error))
        if self._still_showing_3d_tab_for(path):
            self._draw_preview_3d(mesh_failed=True)

    def _prepare_mesh_then(self, path: Path, on_ready, on_failed) -> bool:
        """@brief Prepare @p path off the GUI thread, then run @p on_ready (or @p on_failed).

        @param path Preview JSON to prepare.
        @param on_ready Callable run on the GUI thread once the scene is prepared.
        @param on_failed Callable(error text) run on the GUI thread on failure.
        @return False when the viewer has no off-thread preparation (older
            viewers, test stubs): the caller then renders synchronously, as before.
        @note G8 (HANDOFF 130): parsing a 13 MB preview and computing its
            normals took seconds on the GUI thread. The loading indicator stays
            on until the answer arrives. Only the latest request is served, and
            not at all when the model data changed meanwhile (model revision).
        """
        prepare = getattr(self.preview_3d, "prepare_mesh", None)
        if not callable(prepare) or not hasattr(self.preview_3d, "mesh_prepared"):
            return False
        self._show_3d_loading(True)
        self._mesh_request = None
        token = prepare(path)
        self._mesh_request = (token, self._model_revision, on_ready, on_failed)
        return True

    def _take_mesh_request(self, token: int):
        """The pending mesh request if @p token is its token (it is then consumed), else None."""
        request = self._mesh_request
        if request is None or request[0] != token:
            return None
        self._mesh_request = None
        return request

    def _drop_mesh_request(self) -> None:
        """Forget an in-flight mesh preparation: its answer will be ignored."""
        if getattr(self, "_mesh_request", None) is None:
            return
        self._mesh_request = None
        if self._pending_3d_render is None and not self._rendering_3d:
            self._show_3d_loading(False)

    def _on_mesh_prepared(self, token: int) -> None:
        """@brief Slot of `Preview3DVtk.mesh_prepared`: serve the matching request, if current.

        @param token Request token of the prepared mesh.
        """
        request = self._take_mesh_request(token)
        if request is None:
            return
        try:
            if request[1] == self._model_revision:
                request[2]()
        finally:
            if self._mesh_request is None:
                self._show_3d_loading(False)

    def _on_mesh_prepare_failed(self, token: int, error: str) -> None:
        """@brief Slot of `Preview3DVtk.mesh_prepare_failed`: report it for the matching request.

        @param token Request token of the failed preparation.
        @param error Error text from the viewer.
        """
        request = self._take_mesh_request(token)
        if request is None:
            return
        try:
            if request[1] == self._model_revision:
                request[3](error)
        finally:
            if self._mesh_request is None:
                self._show_3d_loading(False)

    def _show_3d_loading(self, visible: bool) -> None:
        """@brief Show or hide the 3D loading indicator and the wait cursor together.

        @param visible True before a heavy render, False after it (also on error).
        @note The override cursor is counted by Qt: it is set once and
            restored once, whatever the order of the calls.
        """
        indicator = getattr(self, "loading_3d_indicator", None)
        if indicator is not None:
            indicator.setVisible(bool(visible))
        if visible and not self._loading_cursor_set:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            self._loading_cursor_set = True
        elif not visible and self._loading_cursor_set:
            QApplication.restoreOverrideCursor()
            self._loading_cursor_set = False


def main(splash=None) -> None:
    """@brief Start the Qt application, show the main window and run the event loop until exit.

    @param splash Optional QSplashScreen shown by the launcher while this
        module was imported; closed as soon as the window is on screen.
    @note The QApplication may already exist (the launcher creates it for
        the splash); one is created here otherwise. After the window shows,
        the 3D viewer's libraries are imported in the background so the
        first entry in the 3D tab does not pay for them.
    """
    app = QApplication.instance() or QApplication(sys.argv)
    # Icona del programma: finestra e barra delle applicazioni (anche da sorgente).
    app.setWindowIcon(QIcon(str(resource_path("kicadgen", "gui", "assets", "app-icon.svg"))))
    app.setStyleSheet(APP_QSS)
    # Tooltip a capo e dentro la finestra, per tutta l'applicazione: con le
    # barre di sole icone il tooltip e' l'unico testo di un comando.
    install_tooltip_filter(app)
    try:
        window = MainWindow()
    except (ProfileError, sqlite3.Error, OSError, ValueError) as exc:
        language = load_config().get("language", "it")
        title = DEFAULT_TRANSLATIONS["ui:Database profili non disponibile"].get(
            language, DEFAULT_TRANSLATIONS["ui:Database profili non disponibile"]["en"])
        template = DEFAULT_TRANSLATIONS["startup.profile_database_error"].get(
            language, DEFAULT_TRANSLATIONS["startup.profile_database_error"]["en"])
        if splash is not None:
            splash.close()
        QMessageBox.critical(None, title, template.format(error=exc))
        sys.exit(1)
    window.centre_before_show()
    window.show()

    def hand_over():
        # A moment after show: the window has then painted its first frame
        # and the desktop's open animation is over, so the splash goes away
        # without a frame of desktop between the two (user, 2026-10-12).
        # The dependency warning (a modal box) comes after the splash, not
        # over it.
        if splash is not None:
            splash.finish(window)
        window._warn_missing_external_applications()
    QTimer.singleShot(SPLASH_HANDOVER_MS if splash is not None else 0, hand_over)
    window.schedule_preview_3d_preload()
    # Solo l'applicazione vera legge il saldo: i test e le finestre costruite
    # altrove non toccano la rete.
    window.ai_account.balance_enabled = True
    window._sync_balance_provider()
    window.ai_account.refresh_balance()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
