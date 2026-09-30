"""@brief Provide the main KiCadGen-Tool desktop window.

@details The window uses the shared dark Qt theme and runs long generation and
AI operations in worker threads. Its six stable tabs cover project input,
advanced footprint parameters, generation, 2D review, 3D review and the final
KiCad validation/installation workflow. Standard-user mode hides only the two
advanced parameter tabs while preserving their indexes for existing handlers.
"""

from __future__ import annotations

from copy import deepcopy
from html import escape
import json
from functools import partial
import math
import re
import shutil
import sqlite3
import sys
import time
import webbrowser
from pathlib import Path

from PyQt6.QtCore import QEvent, QObject, QPointF, QRectF, QThread, QTimer, Qt, QSize
from PyQt6.QtGui import (QBrush, QColor, QIcon, QKeySequence, QPainterPath, QPen, QPixmap,
                         QPolygonF, QShortcut, QTextCursor)
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGraphicsEllipseItem, QGraphicsRectItem, QGraphicsScene,
    QGraphicsView, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QDialog, QListWidget, QMainWindow, QMenu, QMessageBox, QPushButton, QScrollArea,
    QSplitter, QListWidgetItem, QStackedWidget, QTabWidget, QTextEdit, QVBoxLayout,
    QToolButton, QWidget,
)

from ..cad3d.annotations import (ANNOTATION_CONVENTION, EdgeFilletAnnotation,
                                  FaceTextAnnotation, swap_front_back)
from ..cad3d.body_params import Body3DParams
from ..cad3d.step_generator import (
    CAD_ABORT_MESSAGE, Cad3DError, find_freecadcmd, generate_3d_model,
    generate_castellated_3d_model,
)
from ..generators.castellated_module import (
    EDGE_LABELS, EDGES as CASTELLATED_EDGES, CastellatedModuleParams,
    CastellatedSide, ComponentBox, build_castellated_module, pad_positions,
)
from ..generators import castellated_module as _castellated_module
from ..generators.pin_header import PinHeaderParams, build_pin_header, pin_positions
from ..generators.package import build_package
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
from .app_config import config_path, load_config, save_config
from .accepted_models_dialog import AcceptedModelsDialog, ConsolidateDialog
from ..app_paths import default_projects_dir, install_dir, logs_dir, resource_path, user_data_dir
# Solo la validazione dei parametri (libreria standard): onnxruntime e PyTorch
# restano caricati al bisogno. Legata qui, all'import, e non al clic: i test
# che sostituiscono il modulo con un finto non devono cambiarne il contratto.
from ..services.photo_yolo import yolo_settings as validate_yolo_settings
from .component_editor_dialog import ComponentEditorDialog
from .workflow_bar import WorkflowBar
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
from .preview_3d_vtk import Preview3DVtk
from .settings_dialog import LlmSettingsDialog
from .profile_dialog import ProfilesDialog, icon_button
from .theme import BASE, BLUE, RED, SURFACE1, SURFACE2, TEXT, YELLOW, APP_QSS
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
TAB_ICONS = ("tab-project.svg", "tab-preview.svg", "tab-model3d.svg", "tab-kicad.svg")

#: Positions of the tabs in `MainWindow.tabs`. Footprint 2D and Genera were
#: removed on 2026-09-26 with the «Utente avanzato» mode; their parameters
#: live in `component_state` values, not in widgets.
TAB_PROJECT, TAB_PREVIEW, TAB_MODEL_3D, TAB_KICAD = range(4)

#: I tre passi di una generazione, come li mostra la modale (utente,
#: 2026-09-26). Chi lavora in ciascuno lo dice l'intestazione: vedi
#: `analysis_overlay.TOOLS`.
GENERATION_STEPS = ("Footprint 2D (KiCadGen)", "Modelli 3D dalla libreria KiCad",
                    "Solido 3D con FreeCAD")
#: Passo di ogni fase annunciata dai worker. Le misure di FreeCAD appartengono
#: alla scelta dei modelli: servono a confrontare i candidati.
GENERATION_STEP_OF = {"footprint": 0, "kicad": 1, "freecad_measure": 1, "freecad": 2}

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
    "field_yolo_model", "group_castellated",
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
    "chk_deepseek_reasoning",
)
#: Margine, in pixel, fra il bordo della finestra e il contenuto delle schede:
#: la riga della lingua sopra le schede usa lo stesso, cosi' i bordi combaciano.
CONTENT_MARGIN_PX = 10
#: Opzioni di Configurazione -> Opzioni di generazione che seguono il progetto.
PROJECT_OPTIONS = ("include_nc_pins", "silk_pin_labels", "pause_after_inventory",
                   "show_pins_3d")
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


def _align_form_labels(form: QFormLayout, width: int = 132) -> None:
    """Keep fields aligned when several forms are shown in the same panel."""
    for row in range(form.rowCount()):
        label_item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
        label = label_item.widget() if label_item is not None else None
        if label is not None:
            label.setFixedWidth(width)

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
        self._last_mesh_path: Path | None = None
        self._model_revision = 0
        #: Revisione dell'analisi AI, gemella di `_model_revision` per i
        #: risultati CAD tardivi. Una chiamata HTTP gia' partita non si puo'
        #: interrompere (urllib bloccante): interromperla vuol dire
        #: ABBANDONARLA, e il suo risultato -- che arrivera' comunque -- va
        #: riconosciuto come scaduto e buttato. Il worker viene marchiato con
        #: il valore corrente alla partenza; l'interruzione lo incrementa.
        self._analysis_revision = 0
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
        )
        self.i18n.retranslate_widgets(dialog)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        try:
            self.profile_store.save_defaults(dict(
                system_prompt=dialog.prompt() or DEFAULT_SYSTEM_PROMPT, model=dialog.model(),
                vision_model=dialog.vision_model(), verification_model=dialog.verification_model(),
                grounding_prompt=dialog.grounding_prompt(), verification_prompt=dialog.verification_prompt()))
        except (ProfileError, sqlite3.Error, OSError) as exc:
            QMessageBox.warning(
                self, self.i18n.text("Impostazioni non salvate"), str(exc))
            return
        self._refresh_model_combo()
        self._append_log(
            tx("Impostazioni LLM aggiornate (SQLite). Modello datasheet: "
               "{default_model}. Modello foto: {value}.",
               default_model=self._default_model(),
               value=self._vision_model() or 'default del servizio foto')
        )

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
        if self._is_castellated() or self._selected_family() == "aggregate_tht":
            return True
        if self._selected_family() in _SINGLE_COMPONENT_FAMILIES:
            return False
        return None

    def _background_work_running(self):
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
            self._window_closed = True
            # Le ultime modifiche non aspettano il prossimo giro del salvataggio.
            self._flush_project_config()
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
        for index, key in enumerate(("ai", "yolo", "vlm", "yolo_only")):
            self.combo_photo_detector.setItemText(index, self._photo_detector_text(key))
        self.combo_photo_detector.setToolTip(self._photo_detector_text("help"))
        self.label_yolo_model.setText(self._photo_detector_text("model"))
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
        default_path = resource_path("models", "pcb_yolov8s.onnx")
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
        detector = self.combo_photo_detector.currentData()
        local = detector in ("yolo", "yolo_only")
        # Durante una corsa (audit 25/09, B6) non si riaccendono i comandi
        # YOLO e non si riscrive il pulsante: ci pensa la fine della corsa.
        busy = getattr(self, "_extraction_busy", False)
        if not busy:
            self._refresh_extract_button()
            self.field_yolo_model.setEnabled(local)
            self.btn_pick_yolo_model.setEnabled(local)
            self.btn_yolo_settings.setEnabled(local)
            # La riga della revisione dipende dal rilevatore: con «Solo YOLO
            # locale» riapre il pretest (vedi _refresh_reopen_review_button).
            if hasattr(self, "btn_component_review") and not self._background_work_running():
                self._refresh_reopen_review_button()
        if hasattr(self, "_models_form"):
            self._models_form.setRowVisible(self._row_yolo_model, local)
            self._models_form.setRowVisible(self._row_yolo_params, local)
            # Il modello di visione serve in entrambi i percorsi fotografici:
            # con vlm rileva ed elenca, con yolo riconcilia le proposte locali.
            # E' sempre «il modello che guarda la fotografia». Con yolo_only
            # nessun modello AI lavora (pretest locale, utente 2026-09-25):
            # ne' quello dei documenti ne' quello della foto, e le due righe
            # spariscono.
            self._models_form.setRowVisible(self._row_vlm_model, detector in ("yolo", "vlm"))
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
        import os
        from .yolo_model_dialog import YoloModelDialog
        default = os.environ.get("KICADGEN_YOLO_MODEL",
                                 str(resource_path("models", "pcb_yolov8s.onnx")))
        dialog = YoloModelDialog(self.field_yolo_model.text().strip(), resource_path("models"),
                                 default=default, tr=self.i18n.text, parent=self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        selected = dialog.selected_path()
        # Chiusa per davvero e rilasciata subito: tenuta in vita dalla finestra
        # avviava controlli del modello quando il garbage collector la
        # distruggeva, in un momento qualsiasi (HANDOFF 131).
        dialog.shutdown()
        dialog.deleteLater()
        if accepted and selected:
            self.field_yolo_model.setText(selected)

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
            self._append_log(tx("Modello di visione per questa sessione: {chosen}.", chosen=chosen))

    def _photo_detector_options(self) -> tuple[str, Path | None]:
        """Snapshot the selected backend; AI ignores any remembered ONNX path."""
        detector = self.combo_photo_detector.currentData() or "ai"
        path = self.field_yolo_model.text().strip()
        return detector, Path(path) if detector in ("yolo", "yolo_only") and path else None

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

    def _on_deepseek_reasoning_toggled(self, checked: bool) -> None:
        """@brief Mirror the «Ragionamento DeepSeek» checkbox into the provider switch.

        @param checked New state of the checkbox (also when restored from a project).
        """
        from ..services import providers
        providers.set_deepseek_reasoning(bool(checked))

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
        ("Ctrl+3", "Scheda Modello 3D", "_shortcut_tab_model_3d"),
        ("Ctrl+4", "Scheda KiCad", "_shortcut_tab_kicad"),
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

    def _shortcut_tab_kicad(self) -> None:
        self._shortcut_tab(TAB_KICAD)

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
        model_3d_tab = self._build_3d_tab()
        kicad_tab = self._build_kicad_tab()

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
        self.tabs.addTab(model_3d_tab, self.i18n.tr("tab.model3d.name"))
        self.tabs.addTab(kicad_tab, self.i18n.tr("tab.kicad.name"))
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
        # Barra di stato (PIANO-FIX 4): cosa sta facendo il programma, il
        # progetto aperto e quanto e' costato finora in chiamate AI.
        self.status_project = QLabel()
        self.status_project.setProperty("i18n_skip", True)
        self.status_cost = QLabel()
        self.status_cost.setProperty("i18n_skip", True)
        self.statusBar().addPermanentWidget(self.status_project)
        self.statusBar().addPermanentWidget(self.status_cost)
        self.statusBar().showMessage(self.i18n.text("Pronto"))
        self.analysis_overlay.operation_started.connect(
            lambda title: self.statusBar().showMessage(title + "\u2026"))
        self.analysis_overlay.operation_finished.connect(
            lambda: self.statusBar().showMessage(self.i18n.text("Pronto")))
        self._analysis_tabs_enabled = None
        # Apply the persisted language to all catalogued widgets on first render.
        self.i18n.retranslate_widgets(self)

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
        self.tabs.setTabText(TAB_MODEL_3D, self.i18n.tr("tab.model3d.name"))
        self.tabs.setTabText(TAB_KICAD, self.i18n.tr("tab.kicad.name"))
        self.i18n.retranslate_widgets(self)
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
        """Enable previews only after the first successful parameter acquisition."""
        for index in (TAB_PREVIEW, TAB_MODEL_3D):
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
        _align_form_labels(folder_layout)

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
        # Solo modello 3D: gli strip non cambiano ne' l'analisi ne' il
        # footprint (utente, 2026-09-26). Spenti di default.
        self.chk_header_pins = QCheckBox(
            "Pin quadrati per il collegamento alla scheda principale (solo modello 3D)")
        self.chk_header_pins.setToolTip(
            "Aggiunge sotto la scheda gli strip maschi a pin quadrati, come una scheda "
            "con gli strip gia' saldati. Sulle schede castellated solo sui contatti con "
            "foro passante. Non cambia l'analisi ne' il footprint: basta rigenerare il 3D.")
        self.chk_header_pins.toggled.connect(self._on_header_pins_toggled)
        ai_form.addRow("", self.chk_header_pins)
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
        _align_form_labels(ai_form)
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
        # «Ragionamento DeepSeek» (utente, 2026-09-29, HANDOFF 161): thinking
        # mode dell'API diretta DeepSeek, spento di default; salvato nel
        # progetto. Per gli altri fornitori non cambia nulla.
        self.chk_deepseek_reasoning = QCheckBox("Ragionamento DeepSeek")
        self.chk_deepseek_reasoning.setToolTip(
            "Accende il ragionamento (thinking) nelle richieste all'API diretta di DeepSeek: "
            "risposte più meditate, ma più lente e con token di ragionamento a pagamento, "
            "contati nel consuntivo. Non ha effetto sugli altri fornitori.")
        self.chk_deepseek_reasoning.toggled.connect(self._on_deepseek_reasoning_toggled)
        model_row.addWidget(self.chk_deepseek_reasoning)
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
        # User-selected startup default; explicit AI fallback remains available.
        self.combo_photo_detector.setCurrentIndex(self.combo_photo_detector.findData("yolo"))
        self.combo_photo_detector.currentIndexChanged.connect(self._on_photo_detector_changed)
        # Prima riga del riquadro (utente, 2026-09-25): la scelta del
        # rilevatore decide quali delle righe sotto servono.
        models_form.addRow(self.label_photo_detector, self.combo_photo_detector)
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
                       self.label_yolo_params, self.label_yolo_summary,
                       self.btn_yolo_settings,
                       self.label_vlm_model, self.field_vlm_model, self.btn_pick_vlm_model):
            widget.setProperty("i18n_skip", True)
        self._refresh_photo_detector_controls()
        _align_form_labels(models_form)

        analysis_layout.addLayout(ai_form)

        self.group_ai_tools = QGroupBox("Strumenti AI")
        tools_layout = QVBoxLayout(self.group_ai_tools)
        tools_layout.setContentsMargins(12, 16, 12, 12)
        tools_layout.setSpacing(12)
        tools_layout.addLayout(models_form)
        analysis_layout.addWidget(self.group_ai_tools)

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
        scroll = QScrollArea()
        scroll.setWidget(tab)
        scroll.setWidgetResizable(True)
        return scroll

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
        # Senza tipologia la disposizione rimasta dalla scelta precedente non
        # dice nulla su che cosa si sta analizzando.
        board = self._photo_inventory_family() if self._selected_family() else None
        blocked = self.chk_header_pins.blockSignals(True)
        self.chk_header_pins.setChecked(self._show_pins_3d())
        self.chk_header_pins.blockSignals(blocked)
        self.chk_header_pins.setVisible(board is True)
        if getattr(self, "_ai_form", None) is not None:
            self._ai_form.setRowVisible(self._row_pcb_thickness, board is True)
        self._refresh_pcb_thickness_row()

    def _on_header_pins_toggled(self, checked: bool) -> None:
        """@brief Record the pins choice for the project; the solid on screen is stale.

        @param checked New state of the checkbox.
        """
        self._set_project_option("show_pins_3d", checked)
        self._save_config_safely()
        self._invalidate_generated_mesh()

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
        root = self.field_library_root.text().strip() or str(DEFAULT_LIBRARY_ROOT)
        return Path(root) / name

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
        except Exception as exc:  # noqa: BLE001 - informativo, mai fatale
            self._append_log(tx("Barra di stato non aggiornata: {exc}", exc=exc))

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
            self.status_cost.setVisible(False)
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
            self.status_cost.setVisible(True)
            return
        if costs is None or not costs.calls:
            self.status_cost.setText("")
            self.status_cost.setVisible(False)
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
        self.status_cost.setVisible(True)

    def _on_workflow_step_clicked(self, step: str) -> None:
        """@brief Open the tab where a step is done, when the tab is available."""
        target = {"model3d": TAB_MODEL_3D, "kicad": TAB_KICAD}.get(step, TAB_PROJECT)
        if self.tabs.isEnabled() and self.tabs.isTabEnabled(target):
            self.tabs.setCurrentIndex(target)

    def _refresh_output_actions_inner(self) -> bool:
        name = self._component_name()
        if name:
            project = self._project_dir()
            footprint = project / f"{name}.pretty" / f"{name}.kicad_mod"
            step = project / f"{name}.3dshapes" / f"{name}.step"
            footprint_available = footprint.is_file()
            complete_available = footprint_available and step.is_file()
        else:
            # Senza nome non c'e' una cartella, quindi niente da aprire ne' da
            # installare: nessun percorso da mostrare e nessun file da cercare.
            footprint = step = None
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
        self._refresh_kicad_tab(complete_available)
        if hasattr(self, "tabs"):
            self._refresh_preview_tabs(footprint_available)
        if hasattr(self, "label_kicad_footprint"):
            self.label_kicad_footprint.setText(
                str(footprint) if footprint_available else "—")
            self.label_kicad_step.setText(
                str(step) if (step is not None and step.is_file()) else "—")
            status = (self.i18n.text("Footprint e modello STEP pronti per la verifica.")
                      if complete_available else
                      self.i18n.text("Genera footprint e modello STEP per continuare."))
            if complete_available and _step_older_than_footprint(footprint, step):
                status += "\n" + self.i18n.text(
                    "ATTENZIONE: il modello STEP è più vecchio del footprint. Se dopo "
                    "l'ultimo 3D hai cambiato il corpo, rigenera il modello 3D prima di "
                    "installare.")
            self.label_kicad_status.setText(status)
        return footprint_available

    #: Position of the «KiCad» tab in `self.tabs`.
    KICAD_TAB_INDEX = TAB_KICAD

    def _refresh_kicad_tab(self, complete: bool) -> None:
        """@brief Let the «KiCad» tab be chosen only for a completed project.

        @param complete True when the current project folder holds both the
            footprint and the STEP model: exactly what every operation of the
            tab needs (test in KiCad, project and global installation).
        @note User, 2026-09-24: at start-up, with no project, the tab could be
            opened and showed only dashes and disabled commands. A tab being
            shown when it gets disabled hands over to «Project», so the window
            never sits on a tab that cannot be chosen.
        """
        tabs = getattr(self, "tabs", None)
        index = self.KICAD_TAB_INDEX
        if tabs is None or tabs.count() <= index:
            return
        if not complete and tabs.currentIndex() == index:
            tabs.setCurrentIndex(0)
        tabs.setTabEnabled(index, complete)
        tabs.setTabToolTip(index, "" if complete else self.i18n.text(
            "Disponibile quando il progetto ha footprint e modello STEP generati."))

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
        self._flush_project_config()
        if folder.parent != self._library_root():
            self.field_library_root.setText(str(folder.parent))
        self._open_project_at(folder)

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
        if self._background_work_running() or self._automatic_generation_active:
            return
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

        photo_detector, yolo_model_path = self._photo_detector_options()
        if photo_detector in ("yolo", "yolo_only"):
            try:
                # Optional runtime stays lazy: AI-only sessions do not require it.
                # Validate before paid requests or clearing any previous results.
                from ..services.photo_yolo import validate_yolo_setup
                validate_yolo_setup(yolo_model_path)
            except Exception as exc:
                QMessageBox.warning(
                    self, self._photo_detector_text("error_title"),
                    self._photo_detector_text("error", error=str(exc)),
                )
                return

        try:
            doc_paths, document_roles = self._selected_documents()
        except (OSError, ValueError) as exc:
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
            return

        try:
            profile = self.profile_store.snapshot(self._profile_key())
        except (ProfileError, sqlite3.Error, OSError) as exc:
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
                                       photo_detector, yolo_model_path)
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
                              photo_detector, yolo_model_path) -> None:
        """@brief Configure and start the full extraction worker.

        @param doc_paths Selected document/photo paths.
        @param document_roles Role of each selected document.
        @param profile AI profile snapshot (prompts and model).
        @param photo_detector Photo detector selected in the settings.
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
        if getattr(self, "chk_deepseek_reasoning", None) is not None \
                and self.chk_deepseek_reasoning.isChecked():
            from ..services import providers as _providers
            if _providers.provider_of(model) == _providers.PROVIDER_DEEPSEEK:
                self._on_ai_progress(tx(
                    "Ragionamento DeepSeek attivo: risposte più lente e token di ragionamento "
                    "a pagamento, contati nel consuntivo."))
            else:
                self._on_ai_progress(tx(
                    "Ragionamento DeepSeek acceso ma il modello {model} non è dell'API diretta "
                    "DeepSeek: nessun effetto.", model=model))
        if photo_detector in ('yolo', 'vlm') and self._photo_inventory_family() is False:
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
            # Stessi parametri del pretest (utente, 2026-09-25: «la scelta dei
            # parametri rimane quando scelgo la modalità YOLO + AI»).
            yolo_settings=(self._chosen_yolo_settings() if photo_detector == "yolo" else None),
            system_prompt=profile['system_prompt'],
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
        self._analysis_tabs_enabled = self.tabs.isEnabled()
        self.tabs.setEnabled(False)
        self.analysis_overlay.start(abortable=True)
        self._extract_worker.start()

    #: Estensioni delle foto che l'inventario prova come vista dall'alto
    #: (stesso filtro di `photo_inventory.extract_inventory`).
    _PRETEST_PHOTO_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")

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
                             source_json='', detector='yolo', root=None):
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
                                      detector=detector, source_json=source_json)
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
                                     source_json=saved.json_path.name) is None:
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
                                         source_json=saved.json_path.name)
        if path is not None:
            self._append_log(tx("Proposte del rilevatore salvate in {path} ({n_proposals} corpi): la "
                                "revisione si puo' riaprire senza chiamate AI.",
                                path=path,
                                n_proposals=len(saved.proposals)))

    def _refresh_reopen_review_button(self) -> None:
        """@brief Abilita revisione (gratuita) e rilancio (a pagamento) quando c'e' cosa riaprire.

        @note Entrambi seguono la stessa condizione: un pacchetto valido nella
            cartella del componente (quote positive e almeno una proposta),
            oppure -- ripiego di sessione -- proposte fresche in cache con le
            quote dell'ultima estrazione. Senza nome non c'e' cartella, quindi
            entrambi spenti. Non serve un'estrazione in questa sessione:
            appena il nome punta a una cartella con il pacchetto, si riapre.
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
        else:
            available = self._fresh_cache_evidence() is not None
        self._set_review_buttons_enabled(available)

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
        @note The two differ only in pretest mode, where the reopened bundle
            is the pretest's and nothing may be paid (audit 25/09, B2). The
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
        if bundle is None:
            self._refresh_reopen_review_button()
            return
        photo_png = self._review_photo(bundle)
        if not photo_png:
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
        announcement = (tx("Rilancio dell'inventario su {n_proposals} proposte da {name}: una "
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
            detector=bundle.detector)
        self._reconcile_worker.done.connect(self._on_reconciled)
        self._reconcile_worker.error.connect(self._on_extract_error)
        self._reconcile_worker.progress.connect(self._on_ai_progress)
        self._reconcile_worker.phase.connect(self._on_phase)
        self._reconcile_worker.finished.connect(self._on_extract_finished)
        # Vedi _start_extract_worker: marchio della corsa per i risultati tardivi.
        self._reconcile_worker._analysis_revision = self._analysis_revision
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
        params = replace(base, components_3d=[dict(c) for c in report.components],
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
        if not kept:
            self._append_log(tx("Revisione dei corpi: nessuna proposta tenuta, pacchetto photo-review "
                                "non scritto (le proposte grezze non vengono copiate)."))
            return
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
        self.analysis_overlay.set_tool(tool, detail)
        self.analysis_overlay.set_progress(done, total, tx("componenti"))
        # Fuori da una generazione la mappa non c'e' (o la modale non ha passi):
        # `set_current_step` allora non fa nulla.
        step = (getattr(self, "_generation_step_of", None) or {}).get(tool)
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
        self._regenerate_3d()

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
        # Collegato qui e non alla costruzione del campo: prima della
        # fotografia generale non c'e' un progetto da salvare e chiudere.
        self.field_library_root.textChanged.connect(self._on_library_root_changed)
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
        return {
            "widgets": widgets,
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
                        "pause_after_inventory": self._inventory_pause_enabled()},
        }

    def _apply_project_state(self, state: dict) -> None:
        """@brief Rimette nella finestra le impostazioni salvate di un progetto.

        @param state Oggetto prodotto da `_project_state` (letto dal file o
            fotografato all'avvio). Chiavi mancanti o valori che non entrano in
            un campo lasciano il campo com'e', con una riga di log.
        @note Le opzioni vanno in `_project_options`, mai in `self._config`:
            config.json resta il default dei progetti nuovi finche' l'utente
            non le cambia dalle Opzioni di generazione. Un'opzione che lo
            stato non dichiara torna al valore generale.
        """
        from .project_state import set_widget_value
        from ..cad3d.annotations import EdgeFilletAnnotation, FaceTextAnnotation
        skipped = []
        for name in PROJECT_WIDGETS:
            widget = getattr(self, name, None)
            values = state.get("widgets") if isinstance(state.get("widgets"), dict) else {}
            if widget is not None and name in values and not set_widget_value(widget, values[name]):
                skipped.append(name)
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
        for side in state.get("sides") or ():
            if isinstance(side, dict):
                # Un progetto salvato prima della chiave e' stato generato con
                # lo stile di allora: lo si conserva, non e' una deduzione.
                self._add_side_row(side, missing_style=default_contact_style())
        # Anche senza lati salvati: la barra del tipo contatto non deve restare
        # quella del progetto precedente.
        self._sync_contact_style_bar()
        components = [c for c in state.get("components") or () if isinstance(c, dict)]
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
                                           annotations.get("fillets"), "edge"),
                                          (FaceTextAnnotation, self.face_text_annotations,
                                           annotations.get("texts"), "face")):
            for entry in values or ():
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
        self.pinout = [dict(entry) for entry in state.get("pinout") or () if isinstance(entry, dict)]
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
        self._pcb_thickness_source, self._analysis_pcb_thickness = _saved_pcb_thickness(
            state.get("pcb_thickness"))
        self._refresh_type_effect()
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
        # Le schede di anteprima seguono cio' che il progetto aperto contiene.
        self._refresh_output_actions()
        # La scheda aperta mostra il progetto nuovo, non quello chiuso: senza,
        # restava il disegno del precedente e il 3D non si ridisegnava (audit
        # 25/09). Le altre schede si ridisegnano all'ingresso, come sempre.
        index = self.tabs.currentIndex()
        if index == TAB_PREVIEW:
            self._draw_preview()
        elif index == TAB_MODEL_3D:
            self._enter_3d_tab()
        # Lo stato appena applicato e' quello salvato: non va riscritto.
        self._last_saved_project_state = self._project_state_text()
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
        if self._library_root() / self._component_name() == folder:
            return
        if self._background_work_running() or self._automatic_generation_active:
            self.field_library_root.blockSignals(True)
            self.field_library_root.setText(str(folder.parent))
            self.field_library_root.blockSignals(False)
            self._update_project_path_label()
            self._project_change_blocked()
            return
        # Il salvataggio normale guarda `_project_dir()`, che adesso punta
        # all'altra cartella: si scrive esplicitamente in quella del progetto.
        self._write_open_project_config(folder)
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

    def _library_root(self) -> Path:
        """@brief Cartella di lavoro in cui stanno i progetti."""
        return Path(self.field_library_root.text().strip() or str(DEFAULT_LIBRARY_ROOT))

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
        try:
            (self._library_root() / name / DOCS_SUBDIR).mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            QMessageBox.warning(self, self.i18n.text("Nuovo progetto"), str(exc))
            return
        self._flush_project_config()
        # Un progetto nuovo parte dalle impostazioni generali, non da quelle
        # del progetto aperto prima (decisione dell'utente del 2026-09-24).
        self._switch_project(name, self._general_project_state)
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

    def _open_project_at(self, folder: Path) -> None:
        """@brief Open the project `folder` of the current working folder.

        @param folder Project folder; its parent must be `_library_root()`,
            because from here on the project is addressed by name.
        @note Applies the project's own settings when its project_config.json
            is readable; a refused file is neither applied nor overwritten.
        """
        self._flush_project_config()
        name = folder.name
        from .project_state import ProjectConfigError, read_project_config
        try:
            saved = read_project_config(folder)
            self._project_config_refused = None
        except ProjectConfigError as exc:
            saved = None
            # Ne' applicato ne' sovrascritto: resta com'e' finche' qualcuno lo guarda.
            self._project_config_refused = folder
            self._append_log(tx("ATTENZIONE impostazioni del progetto non applicate: {exc}", exc=exc))
        # Senza file (progetto nato prima di project_config.json) valgono le
        # impostazioni generali, non quelle del progetto aperto prima.
        self._switch_project(name, saved if saved is not None else self._general_project_state)
        if saved is None and self._project_config_refused is None:
            # Progetto nato prima di project_config.json: il file nasce subito,
            # non alla prima modifica, cosi' ogni progetto aperto ha il suo.
            self._save_project_config(force=True)
        self._append_log(tx("Progetto aperto: {folder}", folder=self._project_dir())
                         + ("" if saved is None else tx(" (impostazioni del progetto applicate).")))

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
        applied = self._migrate_docs_to_new_name(dialog.project_name())
        self.field_name.setText(applied)

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

    def _rename_folder_case(self, old_project: Path, new_project: Path,
                            old_name: str, new_name: str) -> str:
        """Rinomina la cartella del progetto cambiando solo le maiuscole, in due passi.

        Si passa da un nome temporaneo accanto, che su un disco senza
        distinzione di maiuscole e' l'unico modo sicuro. Se il secondo passo
        fallisce si torna al nome vecchio: la cartella non resta col nome
        temporaneo. Tutto il contenuto segue, perche' si sposta la cartella
        intera. Restituisce il nome effettivo.
        """
        if not old_project.is_dir():
            return new_name
        # Su Windows una cartella con un file aperto non si rinomina.
        self._close_log_handle()
        import uuid
        temporary = old_project.parent / f".{old_project.name}-rinomina-{uuid.uuid4().hex[:8]}"
        try:
            old_project.rename(temporary)
        except OSError as exc:
            self._append_log(tx("Rinomina annullata: {old_name} non rinominabile ({exc}).",
                                old_name=old_name,
                                exc=exc))
            return old_name
        try:
            temporary.rename(new_project)
        except OSError as exc:
            try:
                temporary.rename(old_project)
            except OSError:
                self._append_log(tx("ATTENZIONE: la cartella del progetto e' rimasta {temporary}.",
                                    temporary=temporary))
            self._append_log(tx("Rinomina annullata: {old_name} non rinominabile ({exc}).",
                                old_name=old_name,
                                exc=exc))
            return old_name
        if self._project_config_refused is not None and self._project_config_refused == old_project:
            self._project_config_refused = new_project
        self._append_log(tx("Progetto rinominato: {old_name} diventa {new_name}.",
                            old_name=old_name,
                            new_name=new_name))
        return new_name

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
        if self._auto_after_extraction:
            try:
                self._validate_automatic_input(params)
            except (ValueError, TypeError) as exc:
                self._auto_generation_params = None
                self._automatic_generation_blocked(str(exc))
                return
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

        self.package_geometry = None
        if params.package_geometry:
            try:
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

        if params.edge_fillets:
            for f in params.edge_fillets:
                ann = EdgeFilletAnnotation(edge=f["edge"], radius_mm=float(f["radius_mm"]))
                try:
                    ann.validate()
                except ValueError:
                    continue
                self.fillet_annotations.append(ann)

        fillet_note = (
            self.i18n.tr(
                "extraction.fillet_note", count=len(params.edge_fillets))
            if params.edge_fillets else ""
        )
        if self._auto_after_extraction:
            self._auto_generation_params = params
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
            "La foto non mostra il tipo di contatto di questi lati e non viene indovinato. "
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
                                "(la foto non lo mostrava).", sides=names,
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
        return tx("tipo di contatto non indicato per i lati {sides}: la foto non lo mostrava e "
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
            # Diametri dei fori: dichiarati solo se letti o ricavati dai
            # documenti; stimati o senza provenienza restano stime (26/09).
            for field in ("half_hole_diameter_mm", "through_hole_drill_mm"):
                source = sources.get(f"castellated_sides.{side.get('edge')}.{field}")
                side[field.replace("_mm", "_declared")] = source in DECLARED_SOURCES
            # Piazzole del lato, stessa regola (28/09). Senza la voce di
            # provenienza (lati rientrati da `_serialised_sides`, che la
            # portano gia' nel lato) vale quella del lato; se manca anche
            # quella, la misura resta una stima.
            for field in ("pad_length_mm", "pad_width_mm"):
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
        artifacts_form.addRow("Footprint:", self.label_kicad_footprint)
        artifacts_form.addRow("Modello STEP:", self.label_kicad_step)
        _align_form_labels(artifacts_form)
        outer.addWidget(artifacts_group)

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
        review_layout.addLayout(self._note_with_icons(review_note, self.btn_test_kicad))
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

    def _append_log(self, msg: str) -> None:
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
            rows=self.field_rows.value(),
            cols=self.field_cols.value(),
            pitch=self.field_pitch.value(),
            row_spacing=self.field_row_spacing.value(),
            pad_diameter=self.field_pad.value(),
            drill=self.field_drill.value(),
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
        text = self.i18n.tr("generation.blocked", reason=message)
        self._on_ai_progress(tx("Generazione automatica non avviata: ") + message)
        QMessageBox.warning(
            self, self.i18n.text('Generazione automatica non avviata'), text)

    @staticmethod
    def _validate_output_name(name):
        if (not name or name in ('.', '..') or name != name.rstrip(' .')
                or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)):
            raise ValueError(tx("Il nome del componente non è un nome di file valido."))

    def _validate_automatic_input(self, params):
        self._validate_output_name(params.name or self._component_name())
        if params.package_geometry:
            # Pad espliciti: il generatore 3D non li supporta, e le quote del
            # corpo e della griglia qui sotto non li descrivono. Si scrive solo
            # il footprint (vedi `_generate`), come faceva la modalita' manuale
            # tolta il 2026-09-26. Si controlla con lo stesso parser che
            # l'applicazione usera': una geometria che li' verrebbe scartata
            # lascerebbe `_generate` sui campi a griglia, cioe' un footprint
            # che non e' quello del componente.
            try:
                geometry_data = dict(params.package_geometry)
                geometry_data.setdefault("name", params.name or self._component_name())
                package_from_mapping(geometry_data)
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    tx("Geometria a pad espliciti non valida: nessun file generato.")) from exc
            return
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
            self._validate_automatic_input(extracted)
            if extracted.package_geometry:
                self._generate(automatic=True)
                return
            body = self._current_body_params()
            if body is None and self.group_3d.isChecked():
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
        self._generate(automatic=True)

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

    def _generate(self, automatic=False) -> None:
        if self._background_work_running() or self._automatic_generation_active:
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
            self._validate_output_name(params.name)
            body = None if isinstance(params, PackageGeometry) else self._current_body_params()
            if body is not None:
                body.validate()
            # Il corpo si pretende solo se il 3D e' stato chiesto. Un gruppo
            # «Modello 3D» spento e' una scelta, non un dato mancante: la
            # generazione automatica deve comunque scrivere il footprint.
            # Vedi `_current_body_params` e `_start_automatic_generation`.
            if (automatic and body is None and self.group_3d.isChecked()
                    and not isinstance(params, PackageGeometry)):
                raise ValueError(tx('Mancano i parametri 3D.'))
        except ValueError as exc:
            self._append_log(tx("ERRORE: {exc}", exc=exc))
            if automatic:
                self._automatic_generation_blocked(str(exc))
            return

        project_dir = self._project_dir()
        if automatic and not self._confirm_output_overwrite(params, project_dir, body):
            self._on_ai_progress(tx("Generazione annullata: file esistenti conservati."))
            return

        if isinstance(params, PackageGeometry):
            self._append_log(
                tx("Generazione {name}: package {family}, {n_pins} pad espliciti",
                   name=params.name,
                   family=params.family,
                   n_pins=len(params.pins))
            )
            self._append_log(tx("Modello 3D non generato: il generatore 3D non supporta ancora la "
                                "geometria a pad espliciti. Viene scritto solo il footprint."))
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
                   "(FreeCAD non disponibile): verranno ignorate.")
            )

        self._last_params = params
        self._worker = GenerateWorker(
            params, project_dir, body,
            fillets=list(self.fillet_annotations), face_texts=list(self.face_text_annotations),
        )
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
            self._show_generation_steps(params, body)
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
            self._show_generation_steps(params, body)
        try:
            self._worker.start()
        except Exception as exc:
            self._on_error(str(exc))
            self._on_generation_finished()

    def _show_generation_steps(self, params, body) -> None:
        """@brief Put the three generation steps on the modal, with the missing ones explained.

        @param params Footprint parameters about to be generated.
        @param body 3D body, or None when no 3D model will be built.
        @note Steps 2 and 3 need FreeCAD and a 3D body; when they will not run
            the modal says why instead of letting them silently not happen.
        """
        unavailable = {}
        if body is None:
            reason = ("il generatore 3D non supporta i pad espliciti"
                      if isinstance(params, PackageGeometry)
                      else "FreeCAD non installato" if self._freecad_available is False
                      else "modello 3D non richiesto")
            unavailable = {1: reason, 2: reason}
        self.analysis_overlay.set_steps(GENERATION_STEPS, unavailable)
        self._generation_step_of = dict(GENERATION_STEP_OF)

    def _on_generation_finished(self):
        """@brief Riporta la finestra in quiete alla fine della generazione.

        @note E' la stessa via per l'esito buono, per il guasto e per
            l'interruzione chiesta dall'utente: il worker emette `finished` in
            tutti e tre i casi. L'interruzione si riconosce da
            `_abort_pending`, che viene azzerato qui perche' qui finisce
            davvero il lavoro che si stava fermando.
        """
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
            # Consuntivo fedele: il modello 3D non e' stato chiesto, quindi non
            # va ne' promesso ne' lamentato.
            self._on_ai_progress(tx("Footprint generato; modello 3D non richiesto. Verifica l’anteprima "
                                    "prima dell’utilizzo."))
            self._hold_completed_overlay("Completato: footprint generato.")

    def _on_generated(self, path: str) -> None:
        if self._automatic_generation_active:
            self._automatic_footprint_done = True
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
        header.addWidget(QLabel("Anteprima 2D (silkscreen + pad, scala fissa, con quote):"))
        header.addStretch()
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
        outer.addLayout(header)

        self.preview_scene = QGraphicsScene()
        self.preview_view = QGraphicsView(self.preview_scene)
        self.preview_view.setMinimumHeight(500)
        self.preview_view.setRenderHints(self.preview_view.renderHints())
        outer.addWidget(self.preview_view)

        return tab

    def _show_pinout_dialog(self) -> None:
        dialog = PinoutDialog(self.pinout, self._component_name(), self)
        self.i18n.retranslate_widgets(dialog)
        dialog.exec()

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

        self.preview_3d = Preview3DVtk()
        if hasattr(self.preview_3d, 'component_requested'):
            self.preview_3d.component_requested.connect(
                lambda row: QTimer.singleShot(0, lambda: self._show_photo_comparison(component_row=row)))
        if hasattr(self.preview_3d, 'component_menu_requested'):
            self.preview_3d.component_menu_requested.connect(self._show_component_menu)
        if hasattr(self.preview_3d, 'component_clicked'):
            self.preview_3d.component_clicked.connect(self._on_format_painter_click)
        if hasattr(self.preview_3d, 'mesh_prepared'):
            self.preview_3d.mesh_prepared.connect(self._on_mesh_prepared)
            self.preview_3d.mesh_prepare_failed.connect(self._on_mesh_prepare_failed)
        self.preview_3d.show_pin_numbers = self.chk_pin_numbers.isChecked()
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
        button.setEnabled(not busy)
        button.setProperty("workflow_busy", bool(busy))
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
        """Rigenera solo lo STEP con le annotazioni correnti (il footprint resta com'e')."""
        if self._background_work_running() or self._automatic_generation_active:
            return

        body = self._current_body_params()
        if body is None:
            QMessageBox.information(
                self, self.i18n.text("Modello 3D non disponibile"), self._no_3d_status()[0])
            return

        try:
            params = (
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
            list(self.fillet_annotations), list(self.face_text_annotations),
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
        'presence_confidence', 'class_confidence', 'confidence',
        '_yolo_review_only', '_yolo_origin', 'terminal_count',
        'terminal_layout', 'terminal_count_basis', '_global_inventory_status', 'class',
        '_yolo_proposal_id', '_presence_reviewed_by_user',
        'lead_count', 'lead_count_complete',
        'landmark_feature', 'landmark_side', 'landmark_confidence', 'marking_confidence',
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
        return [self._component_from_photo(row) for row in rows]

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

    def _on_grid_toggled(self, checked: bool) -> None:
        self._config["show_grid"] = checked
        self._save_config_safely()
        self.combo_grid_unit.setEnabled(checked)
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

    def _fit_preview(self) -> None:
        """Inquadra il footprint, ignorando la griglia.

        La griglia si estende oltre il componente: includendola
        nell'inquadratura, attivarla e disattivarla cambiava la dimensione
        del disegno a parita' di footprint -- confondente proprio quando la
        griglia serve a valutare le distanze.
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
        self.preview_view.resetTransform()
        self.preview_view.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)

    def _draw_grid(
        self, mm_to_px: float, width: float, height: float, margin: float,
        x0: float = 0.0, y0: float = 0.0,
    ) -> None:
        """Griglia di riferimento, con passo in mm o in decimi di pollice.

        Una linea marcata ogni 5 passi fa da riferimento grosso (5mm oppure
        mezzo pollice), le altre sono sottili.

        (x0, y0) e' l'angolo da cui parte l'area da coprire: zero per il
        footprint a matrice, che ha il pin 1 nell'origine, negativo per i
        moduli castellated, centrati sull'origine.
        """
        self._preview_grid_spec = dict(mm_to_px=mm_to_px, width=width, height=height,
                                       margin=margin, x0=x0, y0=y0)
        step = 2.54 if self.combo_grid_unit.currentData() == "inch" else 1.0

        minor = QPen(QColor(SURFACE1))
        minor.setWidthF(0.5)
        major = QPen(QColor(SURFACE2))
        major.setWidthF(1.0)

        pad = margin + 2.0
        i_min, i_max = math.floor((x0 - pad) / step), math.ceil((x0 + width + pad) / step)
        j_min, j_max = math.floor((y0 - pad) / step), math.ceil((y0 + height + pad) / step)

        for i in range(i_min, i_max + 1):
            pen = major if i % 5 == 0 else minor
            x = i * step * mm_to_px
            line = self.preview_scene.addLine(
                x, j_min * step * mm_to_px, x, j_max * step * mm_to_px, pen
            )
            line.setZValue(-10)  # dietro pad, silkscreen e quote
            line.setData(_GRID_ITEM, True)
            line.setVisible(self.chk_grid.isChecked())
        for j in range(j_min, j_max + 1):
            pen = major if j % 5 == 0 else minor
            y = j * step * mm_to_px
            line = self.preview_scene.addLine(
                i_min * step * mm_to_px, y, i_max * step * mm_to_px, y, pen
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

    def _draw_preview_castellated(self, p: CastellatedModuleParams) -> None:
        """Anteprima del modulo castellated: corpo, piazzole e quote d'ingombro.

        Disegno separato da quello a matrice perche' la geometria e' diversa
        (origine al centro del corpo, pad rettangolari su piu' lati con passi
        indipendenti), ma come la' i pad vengono da pad_positions(): l'anteprima
        non puo' divergere dal footprint che verra' scritto.
        """
        self.preview_scene.clear()
        mm_to_px = 20.0

        self._draw_grid(
            mm_to_px, p.body_length, p.body_width, p.pad_overhang + p.pad_length,
            x0=-p.body_length / 2, y0=-p.body_width / 2,
        )

        silk_pen = QPen(QColor(TEXT))
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

        if xs_px and ys_px:
            left, right = min(xs_px + [-half_l]), max(xs_px + [half_l])
            top, bottom = min(ys_px + [-half_w]), max(ys_px + [half_w])
            self._add_dim_h(-half_l, half_l, top - 40, self._dim_label("dim.body", p.body_length))
            self._add_dim_v(-half_w, half_w, left - 40, self._dim_label("dim.body", p.body_width))
            self._add_dim_h(left, right, bottom + 60, self._dim_label("dim.total", (right - left) / mm_to_px))

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
        if self._last_params is not None:
            return self._last_params
        try:
            params = (
                self.package_geometry if self.package_geometry is not None else
                self._current_castellated_params() if self._is_castellated()
                else self._current_params()
            )
            params.validate()
        except (ValueError, TypeError):
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
        margin = 1.0
        row_spacing = p.effective_row_spacing()
        width = (p.cols - 1) * p.pitch
        height = (p.rows - 1) * row_spacing

        self._draw_grid(mm_to_px, width, height, margin)

        silk_pen = QPen(QColor(TEXT))
        silk_pen.setWidthF(1.5)
        self.preview_scene.addRect(
            -margin * mm_to_px, -margin * mm_to_px,
            (width + 2 * margin) * mm_to_px, (height + 2 * margin) * mm_to_px,
            silk_pen,
        )

        pad_pen = QPen(QColor(SURFACE1))
        pad_brush = QBrush(QColor(BLUE))

        # Stessa funzione usata dal generatore 2D e da quello 3D: l'anteprima non
        # deve poter divergere dal footprint realmente prodotto (e cosi' rispetta
        # anche la mappa esplicita dei pin letta dal datasheet).
        xs_px, ys_px = [], []
        r = (p.pad_diameter / 2) * mm_to_px
        for pin, x_mm, y_mm in pin_positions(p):
            x, y = x_mm * mm_to_px, y_mm * mm_to_px
            xs_px.append(x)
            ys_px.append(y)

            if pin == 1:
                item: QGraphicsRectItem | QGraphicsEllipseItem = QGraphicsRectItem(
                    x - r, y - r, 2 * r, 2 * r
                )
            else:
                item = QGraphicsEllipseItem(x - r, y - r, 2 * r, 2 * r)
            item.setPen(pad_pen)
            item.setBrush(pad_brush)
            item.setToolTip(self._pad_tooltip(pin))
            self.preview_scene.addItem(item)

            # Numero del pin sul pad: scuro sul blu del pad, e insieme alla
            # tabella pinout rende verificabile la corrispondenza con il
            # datasheet senza aprire KiCad.
            number = self.preview_scene.addSimpleText(str(pin))
            number.setToolTip(item.toolTip())
            number.setBrush(QBrush(QColor(BASE)))
            font = number.font()
            font.setPointSizeF(max(6.0, r * 0.55))
            font.setBold(True)
            number.setFont(font)
            number_rect = number.boundingRect()
            number.setPos(x - number_rect.width() / 2, y - number_rect.height() / 2)
            number.setZValue(5)

        left = -margin * mm_to_px
        right = (width + margin) * mm_to_px
        top = -margin * mm_to_px
        bottom = (height + margin) * mm_to_px

        # Quota vicina al componente: passo (in alto) e distanza righe (a sinistra)
        if p.cols >= 2:
            self._add_dim_h(0, p.pitch * mm_to_px, top - 20, self._dim_label("dim.pitch", p.pitch))
        if p.rows >= 2:
            self._add_dim_v(0, row_spacing * mm_to_px, left - 20, self._dim_label("dim.row_spacing", row_spacing))

        # Catena di quote: margine sinistro / span pin / margine destro (sotto)
        if xs_px:
            leftmost_x, rightmost_x = min(xs_px), max(xs_px)
            chain_y = bottom + 25
            if leftmost_x - left > 1:
                self._add_dim_h(left, leftmost_x, chain_y, f"{(leftmost_x - left) / mm_to_px:.2f} mm")
            if rightmost_x - leftmost_x > 1:
                self._add_dim_h(leftmost_x, rightmost_x, chain_y, f"{(rightmost_x - leftmost_x) / mm_to_px:.2f} mm")
            if right - rightmost_x > 1:
                self._add_dim_h(rightmost_x, right, chain_y, f"{(right - rightmost_x) / mm_to_px:.2f} mm")

        # Catena di quote: margine superiore / span pin / margine inferiore (a destra)
        if ys_px:
            topmost_y, bottommost_y = min(ys_px), max(ys_px)
            chain_x = right + 25
            if topmost_y - top > 1:
                self._add_dim_v(top, topmost_y, chain_x, f"{(topmost_y - top) / mm_to_px:.2f} mm")
            if bottommost_y - topmost_y > 1:
                self._add_dim_v(topmost_y, bottommost_y, chain_x, f"{(bottommost_y - topmost_y) / mm_to_px:.2f} mm")
            if bottom - bottommost_y > 1:
                self._add_dim_v(bottommost_y, bottom, chain_x, f"{(bottom - bottommost_y) / mm_to_px:.2f} mm")

        # Quote totali (piu' lontane): larghezza/altezza silkscreen
        self._add_dim_h(left, right, bottom + 60, self._dim_label("dim.total", width + 2 * margin))
        self._add_dim_v(top, bottom, right + 60, self._dim_label("dim.total", height + 2 * margin))

        self.preview_scene.setBackgroundBrush(QBrush(QColor(BASE)))
        self._fit_preview()

    def _draw_preview_package(self, package: PackageGeometry) -> None:
        """Disegna il package generico con la stessa geometria del builder."""
        self.preview_scene.clear()
        mm_to_px = 20.0
        pins = package.pins_seen_from_top()
        margin = 1.0
        silk_pen = QPen(QColor(TEXT))
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
        xs, ys = [], []
        for pin in pins:
            x, y = pin.x_mm * mm_to_px, pin.y_mm * mm_to_px
            w, h = pin.pad_length_mm * mm_to_px, pin.pad_width_mm * mm_to_px
            item = QGraphicsRectItem(x - w / 2, y - h / 2, w, h)
            item.setTransformOriginPoint(x, y)
            item.setRotation(pin.rotation_deg)
            item.setPen(pad_pen)
            item.setBrush(pad_brush)
            item.setToolTip(self._pad_tooltip(pin.number))
            self.preview_scene.addItem(item)
            xs.append(x); ys.append(y)
            number = self.preview_scene.addSimpleText(str(pin.number))
            number.setToolTip(item.toolTip())
            number.setBrush(QBrush(QColor(BASE)))
            font = number.font(); font.setPointSizeF(max(6.0, min(w, h) * .45)); font.setBold(True)
            number.setFont(font)
            rect = number.boundingRect(); number.setPos(x - rect.width() / 2, y - rect.height() / 2); number.setZValue(5)
        left = (-body_l / 2 - margin) * mm_to_px
        top = (-body_w / 2 - margin) * mm_to_px
        right = (body_l / 2 + margin) * mm_to_px
        bottom = (body_w / 2 + margin) * mm_to_px
        self._draw_grid(mm_to_px, body_l, body_w, margin,
                        x0=-body_l / 2, y0=-body_w / 2)
        self._add_dim_h(left, right, bottom + 40, self._dim_label("dim.total", body_l + 2 * margin))
        self._add_dim_v(top, bottom, right + 40, self._dim_label("dim.total", body_w + 2 * margin))
        self.preview_scene.setBackgroundBrush(QBrush(QColor(BASE)))
        self._fit_preview()

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

    def _draw_preview_3d(self, mesh_failed: bool = False) -> None:
        """@brief Draw the generated mesh when there is one, else the cheap preview.

        @param mesh_failed True when the mesh was just found unreadable (off-thread
            preparation failed and was logged): it is not tried again here.
        """
        # Never replace a generated model with a synthetic matrix preview
        # simply because the user revisits the 3D tab.
        if not mesh_failed and self._last_mesh_path and Path(self._last_mesh_path).is_file():
            try:
                self.preview_3d.render_mesh(self._last_mesh_path)
                return
            except Exception as exc:
                self._append_log(tx("Anteprima mesh non disponibile: {exc}", exc=exc))
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


def main() -> None:
    """@brief Start the Qt application, show the main window and run the event loop until exit."""
    app = QApplication(sys.argv)
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
        QMessageBox.critical(None, title, template.format(error=exc))
        sys.exit(1)
    window.show()
    QTimer.singleShot(0, window._warn_missing_external_applications)
    # Solo l'applicazione vera legge il saldo: i test e le finestre costruite
    # altrove non toccano la rete.
    window.ai_account.balance_enabled = True
    window._sync_balance_provider()
    window.ai_account.refresh_balance()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
