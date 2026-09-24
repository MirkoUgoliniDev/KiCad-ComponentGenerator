"""@brief Provide the main KiCadGen-Tool desktop window.

@details The window uses the shared dark Qt theme and runs long generation and
AI operations in worker threads. Its six stable tabs cover project input,
advanced footprint parameters, generation, 2D review, 3D review and the final
KiCad validation/installation workflow. Standard-user mode hides only the two
advanced parameter tabs while preserving their indexes for existing handlers.
"""

from __future__ import annotations

from copy import deepcopy
import json
import math
import re
import threading
import shutil
import sqlite3
import sys
import time
import webbrowser
from pathlib import Path

from PyQt6.QtCore import QEvent, QObject, QRectF, QThread, QTimer, pyqtSignal, Qt, QSize
from PyQt6.QtGui import QBrush, QColor, QIcon, QKeySequence, QPainterPath, QPen, QPixmap, QTextCursor
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QFrame, QGraphicsEllipseItem, QGraphicsRectItem, QGraphicsScene,
    QGraphicsView, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QDialog, QListWidget, QMainWindow, QMenu, QMessageBox, QPushButton, QScrollArea,
    QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QListWidgetItem, QTabWidget, QTextEdit, QVBoxLayout,
    QToolButton, QWidget,
)

from ..cad3d.annotations import EDGE_NAMES, FACE_NAMES, EdgeFilletAnnotation, FaceTextAnnotation
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
from ..services import component_types
from ..services.analysis_profiles import ProfileStore, ProfileError
from ..services.i18n import DEFAULT_TRANSLATIONS, I18n, LANGUAGES
from ..services.document_roles import DocumentSet, MANIFEST, SUFFIXES
from ..services.datasheet_extractor import (
    COMPONENT_KINDS, DEFAULT_SYSTEM_PROMPT, ExtractedParams, ExtractionError,
    extract_pin_header_params,
)
from ..services.openrouter import DEFAULT_MODEL
from ..services.system_dependencies import detect_external_applications
from ..services.kicad_library import (
    KiCadLibraryError,
    create_preview_project,
    discover_component_artifacts,
    global_fp_lib_table_path,
    install_component_library,
    launch_preview,
)
from .app_config import (CONFIG_PATH, DEFAULTS, browse_start_dir, config_path, load_config,
                         remember_browse_dir, save_config)
from .accepted_models_dialog import AcceptedModelsDialog, ConsolidateDialog
from .analysis_overlay import AnalysisOverlay
from .generation_options_dialog import GenerationOptionsDialog
from .guided_documents import GuidedDocuments
from .model_dialog import pick_model
from ..services.openrouter import PHOTO_CAPABILITIES
from .pinout_dialog import PinoutDialog
from .photo_review_dialog import PhotoReviewDialog, PhotoTransform
from .preview_3d_vtk import Preview3DVtk
from .settings_dialog import LlmSettingsDialog
from .profile_dialog import ProfilesDialog, icon_button
from .translation_dialog import TranslationDialog
from .theme import BASE, BLUE, RED, SURFACE0, SURFACE1, SURFACE2, TEXT, YELLOW, APP_QSS
from .tooltips import install_tooltip_filter

#: Icona di ogni scheda, nell'ordine delle schede: SVG a linea nello stile
#: delle barre di icone.
TAB_ICONS = ("tab-project.svg", "tab-footprint.svg", "tab-generate.svg",
             "tab-preview.svg", "tab-model3d.svg", "tab-kicad.svg")

APP_DIR = Path(__file__).resolve().parent.parent.parent
# Generated working files stay separate from installed KiCad libraries.
DEFAULT_LIBRARY_ROOT = APP_DIR / "generated"
DOCS_SUBDIR = "docs"

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
    "vlm": {"it": "Solo modello di visione (nessun rilevatore locale)",
            "en": "Vision model only (no local detector)",
            "fr": "Modèle de vision seul (aucun détecteur local)",
            "de": "Nur Sehmodell (kein lokaler Detektor)"},
    "model": {"it": "Modello YOLO:", "en": "YOLO model:",
              "fr": "Modèle YOLO :", "de": "YOLO-Modell:"},
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
    "picker": {"it": "Seleziona modello YOLO ONNX per questa sessione",
               "en": "Select a YOLO ONNX model for this session",
               "fr": "Choisir un modèle YOLO ONNX pour cette session",
               "de": "YOLO-ONNX-Modell für diese Sitzung auswählen"},
    "filter": {"it": "Modelli ONNX (*.onnx)", "en": "ONNX models (*.onnx)",
               "fr": "Modèles ONNX (*.onnx)", "de": "ONNX-Modelle (*.onnx)"},
    "default": {"it": "Predefinito: {path}", "en": "Default: {path}",
                "fr": "Par défaut : {path}", "de": "Standard: {path}"},
    "path_help": {
        "it": "File .onnx facoltativo, solo per questa sessione. Lascia vuoto per usare {path}. La presenza del file non attiva YOLO automaticamente.",
        "en": "Optional .onnx file, for this session only. Leave empty to use {path}. An existing file does not enable YOLO automatically.",
        "fr": "Fichier .onnx facultatif, pour cette session uniquement. Laisser vide pour utiliser {path}. La présence du fichier n’active pas YOLO automatiquement.",
        "de": "Optionale .onnx-Datei, nur für diese Sitzung. Leer lassen, um {path} zu verwenden. Eine vorhandene Datei aktiviert YOLO nicht automatisch.",
    },
    "help": {
        "it": "YOLO locale propone i riquadri dei componenti. Il verificatore AI configurato continua a consumare credito AI. L’abbinamento ai modelli KiCad avviene dopo; i rilevamenti incerti richiedono revisione. La scelta vale solo per questa sessione.",
        "en": "Local YOLO proposes component boxes. The configured AI verifier still uses AI credit. KiCad model matching happens afterwards; uncertain detections require review. This choice applies only to this session.",
        "fr": "YOLO local propose les rectangles des composants. Le vérificateur IA configuré consomme toujours du crédit IA. L’association aux modèles KiCad intervient ensuite ; les détections incertaines nécessitent une révision. Ce choix s’applique uniquement à cette session.",
        "de": "Lokales YOLO schlägt Begrenzungsrahmen für Bauteile vor. Die konfigurierte KI-Prüfung verbraucht weiterhin KI-Guthaben. Die Zuordnung zu KiCad-Modellen erfolgt anschließend; unsichere Erkennungen müssen überprüft werden. Diese Auswahl gilt nur für diese Sitzung.",
    },
    "error_title": {"it": "YOLO locale non disponibile", "en": "Local YOLO unavailable",
                    "fr": "YOLO local indisponible", "de": "Lokales YOLO nicht verfügbar"},
    "error": {
        "it": "Impossibile avviare YOLO locale: {error}\nSeleziona un modello ONNX valido e verifica che onnxruntime sia installato, oppure scegli AI. Nessuna richiesta AI è stata avviata; i risultati precedenti sono conservati.",
        "en": "Cannot start local YOLO: {error}\nSelect a valid ONNX model and check that onnxruntime is installed, or choose AI. No AI request was started; previous results are preserved.",
        "fr": "Impossible de démarrer YOLO local : {error}\nChoisir un modèle ONNX valide et vérifier qu’onnxruntime est installé, ou choisir IA. Aucune requête IA n’a été lancée ; les résultats précédents sont conservés.",
        "de": "Lokales YOLO kann nicht gestartet werden: {error}\nWählen Sie ein gültiges ONNX-Modell und prüfen Sie, ob onnxruntime installiert ist, oder wählen Sie KI. Es wurde keine KI-Anfrage gestartet; bisherige Ergebnisse bleiben erhalten.",
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


def matching_cancel_kwargs(function, cancel) -> dict:
    """@brief `{'model_cancel': evento}` solo se il generatore dichiara quel canale.

    @param function Generatore 3D che si sta per chiamare.
    @param cancel Evento con cui la modale chiede di fermare il lavoro.
    @return Dizionario da espandere nella chiamata, vuoto quasi sempre.
    @note L'abbinamento dei modelli KiCad (`kicad_models.attach_models`, che
        per misurare un candidato non ancora in cache avvia `freecadcmd`) puo'
        durare minuti da solo, e la sua interruzione sta nascendo in
        `cad3d/`: il canale ordinario e' lo stesso `cancel` che gia' arriva al
        figlio FreeCAD (vedi `cancel_kwargs`), ma se l'abbinamento ricevesse un
        canale PROPRIO questa e' la porta per passarglielo. Finche' la firma
        non lo dichiara non si passa nulla: mai un TypeError per una
        funzionalita' accessoria, e le due modifiche possono atterrare in
        qualunque ordine.
    """
    return {"model_cancel": cancel} if accepts_keyword(function, "model_cancel") else {}


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
                 progress=None, model_progress=None, cancel=None):
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
    """
    generator = _generation_target(params)
    return generator(
        params, body, project_dir, fillets=fillets, face_texts=face_texts,
        progress=progress, model_progress=model_progress,
        **cancel_kwargs(generator, cancel),
        **matching_cancel_kwargs(generator, cancel),
    )


#: Livelli della riga di stato del 3D (`MainWindow._set_3d_status`): solo gli
#: ultimi due vengono mostrati sotto il modello.
STATUS_INFO = "info"
STATUS_WARNING = "warning"
STATUS_ERROR = "error"


#: Riga con cui lo script FreeCAD annuncia il componente su cui lavora.
_FREECAD_COMPONENT = re.compile(r"KICADGEN_COMPONENT_START:(\d+)/(\d+)")


def freecad_progress(message: str):
    """@brief Avanzamento del 3D letto dalle righe di FreeCAD.

    @param message Riga di log, gia' prefissata con «FreeCAD: ».
    @return (completati, totale) quando la riga annuncia un componente,
        altrimenti None.
    @note Il conteggio in modale si fermava a «100% · 50/50» mentre FreeCAD
        era ancora al primo componente: quel numero era l'abbinamento dei
        modelli, finito prima che FreeCAD partisse, e nessuno lo aggiornava
        dopo. FreeCAD pero' annuncia ogni componente che comincia: i
        completati sono quelli prima di lui. Su un lavoro da cinque minuti la
        differenza fra «finito» e «al primo di cinquanta» e' tutto cio' che
        chi aspetta ha bisogno di sapere.
    """
    match = _FREECAD_COMPONENT.search(message or "")
    if not match:
        return None
    current, total = int(match.group(1)), int(match.group(2))
    return max(0, current - 1), total


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


def accepts_keyword(function, name: str) -> bool:
    """@brief Dice se si puo' passare `name=` a questa funzione senza TypeError.

    @param function Funzione che si sta per chiamare.
    @param name Nome del parametro facoltativo da passare.
    @return True quando la firma lo dichiara, oppure quando ha un `**kwargs`
        insieme ad almeno un parametro con un nome.
    @note Regole e motivazioni sono quelle di `phase_kwargs`, di cui questa e'
        la parte generale: un `**kwargs` DA SOLO e' l'impronta di un wrapper
        scritto senza `functools.wraps`, quindi non dimostra nulla.
    """
    try:
        import inspect
        parameters = list(inspect.signature(function).parameters.values())
    except (TypeError, ValueError):  # builtin o oggetto non introspezionabile
        return False
    named = [p for p in parameters if p.kind not in (
        inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)]
    for parameter in parameters:
        if parameter.kind is inspect.Parameter.VAR_KEYWORD and named:
            return True
        if parameter.name == name and parameter.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY):
            return True
    return False


def cancel_kwargs(function, cancel) -> dict:
    """@brief `{'cancel': evento}` solo se il generatore 3D accetta il canale.

    @param function Generatore 3D che si sta per chiamare.
    @param cancel Evento con cui la modale chiede di fermare il figlio FreeCAD.
    @return Dizionario da espandere nella chiamata, vuoto se il canale non e'
        accettato.
    @note Stessa ragione di `phase_kwargs`: la funzione chiamata puo' essere
        sostituita (prove, strumenti) da una che non conosce questo parametro,
        e un TypeError li' farebbe fallire una generazione buona per colpa di
        una funzionalita' accessoria.
    @note A differenza dell'annuncio della fase, qui la rinuncia NON si perde
        in silenzio: chi chiama deve dirlo nel log (vedi
        `GenerateWorker._generation_kwargs`), perche' una modale che offre
        «Interrompi» su un lavoro non interrompibile prometterebbe il falso.
    """
    return {"cancel": cancel} if accepts_keyword(function, "cancel") else {}


def phase_kwargs(function, callback) -> dict:
    """@brief `{'phase': callback}` solo se la funzione di servizio lo accetta.

    @param function Funzione di servizio che si sta per chiamare.
    @param callback Callable(tool, detail) da inoltrare alla modale.
    @return Dizionario da espandere nella chiamata: vuoto se il parametro non
        esiste ancora o se la firma non dice nulla di utile.
    @note I servizi e l'interfaccia evolvono separatamente: passare `phase=` a
        una firma che non lo prevede fa fallire l'analisi con un TypeError, e
        non passarlo quando c'e' fa perdere l'annuncio della fase. Si guarda
        la firma installata invece di scommettere sull'ordine di arrivo delle
        modifiche.
    @note `**kwargs` conta come consenso solo se la firma dichiara anche
        almeno un parametro con un nome. Una firma fatta di soli `*args,
        **kwargs` e' l'impronta di un decoratore scritto SENZA
        `functools.wraps`: `inspect.signature` vede il wrapper, non la
        funzione vera, quindi `**kwargs` non dimostra nulla e passare `phase=`
        ricrea esattamente il TypeError che questo aiutante esiste per
        evitare. In quel caso si rinuncia all'annuncio: perdere una riga in
        modale e' meno grave che far cadere l'analisi. Un decoratore con
        `functools.wraps` espone `__wrapped__`, `inspect.signature` lo segue e
        la firma vera torna visibile: e' quella la soluzione, non una regola
        piu' furba qui.
    """
    return accepted_kwargs(function, phase=callback)


def accepted_kwargs(function, **values) -> dict:
    """@brief Keep only the keyword arguments the service function accepts.

    @param function Service function about to be called.
    @param values Candidate keyword arguments.
    @return The subset of `values` whose names the signature declares; all of
        them when the signature has `**kwargs` next to at least one named
        parameter; none when the signature cannot be inspected.
    @note Same rule and same reasons as `phase_kwargs` (see its notes): the
        interface and the services change separately, and an argument the
        installed service does not know must not turn into a TypeError.
    """
    try:
        import inspect
        parameters = list(inspect.signature(function).parameters.values())
    except (TypeError, ValueError):  # builtin o oggetto non introspezionabile
        return {}
    named = [p for p in parameters if p.kind not in (
        inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)]
    if named and any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters):
        return dict(values)
    keyword = {p.name for p in parameters if p.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)}
    return {name: value for name, value in values.items() if name in keyword}


class GenerateWorker(QThread):
    """QThread che costruisce/salva un Footprint e, se richiesto, il modello 3D.

    Il footprint 2D e il modello 3D sono riportati separatamente: un
    fallimento della generazione 3D (es. freecadcmd non trovato) non deve
    invalidare un footprint 2D generato correttamente.

    Segnali:
        done(str)       -- percorso assoluto del file .kicad_mod scritto
        error(str)      -- errore nella generazione del footprint 2D
        cad3d_done(str) -- percorso assoluto del file .step scritto
        cad3d_error(str) -- errore nella generazione del modello 3D
    """

    done = pyqtSignal(str)
    error = pyqtSignal(str)
    cad3d_done = pyqtSignal(str)
    cad3d_error = pyqtSignal(str)
    warning = pyqtSignal(str)
    progress = pyqtSignal(str)
    #: True/False all'inizio del 3D: dice alla finestra se «Interrompi» ha
    #: davvero un lavoro da fermare. Falso = il generatore in uso non conosce
    #: il canale, e la modale deve togliere il comando invece di prometterlo.
    cancellable = pyqtSignal(bool)
    #: (strumento, dettaglio, fatti, totale). Dice alla modale CHI sta
    #: lavorando, che dai messaggi di avanzamento non si capiva. Un totale non
    #: positivo significa "nessun conteggio": la modale resta animata invece di
    #: mostrare una percentuale inventata.
    phase = pyqtSignal(str, str, int, int)

    def __init__(
        self, params: PinHeaderParams | CastellatedModuleParams | PackageGeometry, project_dir: Path,
        body: Body3DParams | None = None,
        fillets: list | None = None, face_texts: list | None = None,
    ):
        super().__init__()
        self.params = params
        self.project_dir = project_dir
        self.body = body
        self.fillets = fillets or []
        self.face_texts = face_texts or []
        #: Canale di rinuncia condiviso col processo FreeCAD figlio. Vive sul
        #: worker e non sulla finestra: e' la corsa a poter essere fermata, e
        #: una corsa successiva deve nascere con un canale pulito.
        self.cancel = threading.Event()

    def _cancel_kwargs(self, generator) -> dict:
        """@brief Il canale di rinuncia da passare al generatore, se lo accetta.

        @param generator La funzione di generazione 3D che si sta per chiamare.
        @return `{'cancel': evento}` oppure `{}`.
        @note Quando il canale non viene accettato la corsa NON e' fermabile, e
            la cosa si dice invece di scoprirla premendo un pulsante che non fa
            niente. La riga finisce nel log e sulla modale come ogni altro
            avviso di questo worker, e `cancellable(False)` fa TOGLIERE il
            comando dalla modale: un pulsante che non ferma niente e' peggio
            di nessun pulsante.
        @note La domanda si fa a `generation_accepts_cancel`, non alla sola
            firma dell'involucro `_generate_3d`: e' il generatore reale a
            dover conoscere il canale.
        """
        kwargs = (cancel_kwargs(generator, self.cancel)
                  if generation_accepts_cancel(generator, self.params) else {})
        if not kwargs:
            self.progress.emit(
                "ATTENZIONE: il generatore 3D in uso non accetta il canale di "
                "interruzione; questa generazione non potrà essere fermata.")
        self.cancellable.emit(bool(kwargs))
        return kwargs

    def request_abort(self) -> None:
        """@brief Ask the FreeCAD child of this run to die.

        @note Non uccide il QThread: l'ordine viaggia sul canale e il figlio
            viene ucciso da `process_log.run_logged`, cioe' dalla stessa
            macchina del watchdog. Il worker esce poi dalla sua via d'errore
            normale (`cad3d_error` e `finished`), che e' cio' che riporta la
            finestra in quiete: terminare il thread di forza lascerebbe invece
            un processo figlio orfano e la GUI senza segnale di fine.
        """
        self.cancel.set()

    def _model_progress(self, done: int, total: int) -> None:
        """@brief Porta in modale l'unico conteggio vero della generazione.

        @param done Componenti gia' esaminati.
        @param total Componenti da esaminare.
        @note Ogni componente puo' costare una misura CAD, quindi il conto
            avanza al ritmo del lavoro vero e non di un timer.
        @note Lo strumento dichiarato e' `kicad` perche' la fase e' la scelta
            del file da usare nella libreria dei modelli di KiCad: e' quello
            che avanza componente per componente ed e' quello che spiega
            l'attesa. Non si dice che FreeCAD sia fermo -- `attach_models`
            avvia `freecadcmd` per misurare i candidati che non sono ancora in
            cache -- ma quelle misure servono all'abbinamento, non alla
            costruzione del solido: mostrare il solido di FreeCAD qui faceva
            credere che il modello finale fosse gia' in lavorazione.
        """
        self.phase.emit('kicad', 'abbinamento modelli KiCad', done, total)

    def _cad_progress(self, message: str) -> None:
        """@brief Inoltra la riga al log e, se annuncia un componente, al conteggio."""
        self.progress.emit(message)
        step = freecad_progress(message)
        if step is not None:
            done, total = step
            self.phase.emit('freecad', 'modello 3D', done, total)

    def run(self) -> None:
        """@brief Provide the run operation for this workflow."""
        try:
            self.phase.emit('kicad', 'footprint 2D', 0, 0)
            self.progress.emit('Generazione del footprint KiCad…')
            footprint = (
                build_castellated_module(self.params)
                if isinstance(self.params, CastellatedModuleParams)
                else build_package(self.params)
                if isinstance(self.params, PackageGeometry)
                else build_pin_header(self.params)
            )
            pretty_dir = self.project_dir / f"{self.params.name}.pretty"
            out_path = pretty_dir / f"{self.params.name}.kicad_mod"
            footprint.save(str(out_path))
            self.done.emit(str(out_path))
        except Exception as exc:  # noqa: BLE001 - riportato in UI, non e' fatale
            self.error.emit(str(exc))
            return

        if self.body is not None:
            try:
                # La prima cosa che fa `_generate_3d` e' l'abbinamento dei
                # modelli di KiCad, non la costruzione del solido: annunciare
                # qui `freecad` accendeva il cubo di FreeCAD per tutta la fase
                # che la correzione vuole attribuita a KiCad, per poi tornare
                # indietro. Da `freecad` ci passa `_cad_progress`, quando il
                # solido comincia davvero.
                self.phase.emit('kicad', 'abbinamento modelli KiCad', 0, 0)
                self.progress.emit('Generazione del modello 3D tramite FreeCAD…')
                # Risolto una volta sola: introspezione e chiamata devono
                # riguardare lo stesso oggetto, anche quando e' stato sostituito.
                generator = _generate_3d
                result = generator(
                    self.params, self.body, self.project_dir,
                    self.fillets, self.face_texts,
                    progress=self._cad_progress,
                    model_progress=self._model_progress,
                    **self._cancel_kwargs(generator),
                )
                for warning in result.get('warnings', []):
                    self.warning.emit(warning)
                # Le note della risoluzione modelli vanno nel log anche nella
                # generazione completa, non solo nella rigenerazione 3D.
                for note in result.get('notes', []):
                    self.progress.emit(note)
                self.cad3d_done.emit(str(result["step_path"]))
            except Cad3DError as exc:
                self.cad3d_error.emit(str(exc))
            except Exception as exc:  # noqa: BLE001 - riportato in UI, non e' fatale
                self.cad3d_error.emit(f"Errore inatteso: {exc}")


class Cad3DWorker(QThread):
    """QThread che rigenera solo il modello 3D (STEP/VRML), senza rifare il footprint.

    Serve al pulsante "Rigenera modello 3D" del tab Modello 3D: dopo aver
    aggiunto/tolto annotazioni non ha senso riscrivere anche il .kicad_mod.

    Segnali:
        done(str)    -- percorso assoluto del file .step scritto
        error(str)   -- messaggio di errore leggibile
        warning(str) -- annotazione non applicata (es. fillet impossibile)
    """

    done = pyqtSignal(str)
    error = pyqtSignal(str)
    warning = pyqtSignal(str)
    #: Annotazioni informative (es. quanti modelli reali di KiCad sono stati
    #: usati): vanno nel log senza il prefisso di avviso, perche' non c'e'
    #: nulla che richieda attenzione.
    progress = pyqtSignal(str)
    #: Vedi GenerateWorker.cancellable.
    cancellable = pyqtSignal(bool)
    #: Vedi GenerateWorker.phase.
    phase = pyqtSignal(str, str, int, int)

    def __init__(
        self, params: PinHeaderParams | CastellatedModuleParams, body: Body3DParams,
        project_dir: Path, fillets: list, face_texts: list,
    ):
        super().__init__()
        self.params = params
        self.body = body
        self.project_dir = project_dir
        self.fillets = fillets
        self.face_texts = face_texts
        #: Vedi GenerateWorker.cancel.
        self.cancel = threading.Event()

    def _cancel_kwargs(self, generator) -> dict:
        """@brief Vedi GenerateWorker._cancel_kwargs: stesso canale, stesso avviso."""
        kwargs = (cancel_kwargs(generator, self.cancel)
                  if generation_accepts_cancel(generator, self.params) else {})
        if not kwargs:
            self.progress.emit(
                "ATTENZIONE: il generatore 3D in uso non accetta il canale di "
                "interruzione; questa generazione non potrà essere fermata.")
        self.cancellable.emit(bool(kwargs))
        return kwargs

    def request_abort(self) -> None:
        """@brief Ask the FreeCAD child of this regeneration to die.

        @note Vedi GenerateWorker.request_abort: stesso canale, stessa uscita
            dalla via d'errore esistente (`error` e `finished`).
        """
        self.cancel.set()

    def _model_progress(self, done: int, total: int) -> None:
        """@brief Porta in modale l'unico conteggio vero della generazione.

        @param done Componenti gia' esaminati.
        @param total Componenti da esaminare.
        @note Strumento `kicad`: si sta scegliendo nella libreria di KiCad il
            modello da abbinare, non si sta costruendo il solido -- anche se
            per misurare un candidato non ancora in cache `attach_models` puo'
            avviare `freecadcmd`. Vedi GenerateWorker._model_progress.
        """
        self.phase.emit('kicad', 'abbinamento modelli KiCad', done, total)

    def _cad_progress(self, message: str) -> None:
        """@brief Inoltra la riga al log e, se annuncia un componente, al conteggio."""
        self.progress.emit(message)
        step = freecad_progress(message)
        if step is not None:
            done, total = step
            self.phase.emit('freecad', 'modello 3D', done, total)

    def run(self) -> None:
        try:
            # Stessa ragione di GenerateWorker.run: `_generate_3d` comincia
            # dall'abbinamento dei modelli di KiCad. `_cad_progress` passa a
            # `freecad` quando il solido comincia davvero.
            self.phase.emit('kicad', 'abbinamento modelli KiCad', 0, 0)
            # Vedi GenerateWorker.run: un solo oggetto per introspezione e chiamata.
            generator = _generate_3d
            result = generator(
                self.params, self.body, self.project_dir,
                self.fillets, self.face_texts,
                progress=self._cad_progress,
                model_progress=self._model_progress,
                **self._cancel_kwargs(generator),
            )
            for note in result.get("notes", []):
                self.progress.emit(note)
            for warning in result.get("warnings", []):
                self.warning.emit(warning)
            self.done.emit(str(result["step_path"]))
        except Cad3DError as exc:
            self.error.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - riportato in UI, non e' fatale
            self.error.emit(f"Errore inatteso: {exc}")


class TerminalDetailWorker(QThread):
    """Optional photo detail pass; never regenerates the footprint."""
    done = pyqtSignal(object)
    error = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, photo, components, model, only_leads=False, system_prompt=None,
                 cache_dir=None):
        super().__init__()
        self.photo, self.components, self.model = photo, deepcopy(components), model
        self.only_leads = only_leads
        self.system_prompt = system_prompt
        # Cache del PROGETTO (vedi photo_inventory.PHOTO_CACHE_SUBDIR); None =
        # nessuna cache, mai una cartella comune.
        self.cache_dir = cache_dir

    def run(self):
        """@brief Provide the run operation for this workflow."""
        from ..services.photo_terminals import observe_terminals
        from ..services.photo_leads import refine_leads
        from ..services.openrouter import call_tool
        from ..services.ai_diagnostics import make_logged_call
        try:
            operation = refine_leads if self.only_leads else observe_terminals
            self.done.emit(operation(self.photo, self.components, self.model,
                make_logged_call(call_tool, self.progress.emit, log_prompt=True), self.progress.emit,
                self.cache_dir,
                **({'system_prompt': self.system_prompt} if self.system_prompt else {})))
        except Exception as exc:
            self.error.emit(f"Dettaglio terminali fallito: {exc}")


class ExtractWorker(QThread):
    """@brief Extract datasheet parameters with session-selected photo detection.

    `model` e' l'id OpenRouter da usare per questa estrazione: puo' essere
    il default del profilo SQLite oppure un modello diverso scelto solo per
    la sessione corrente.

    Segnali:
        done(ExtractedParams) -- parametri estratti con successo
        error(str)            -- messaggio di errore leggibile
        progress(str)        -- avanzamento dell'analisi AI, incluso ogni crop
    """

    done = pyqtSignal(object)
    error = pyqtSignal(str)
    progress = pyqtSignal(str)
    #: Vedi GenerateWorker.phase.
    phase = pyqtSignal(str, str, int, int)
    #: (foto PNG, proposte del rilevatore, lunghezza mm, larghezza mm). Emesso
    #: fra il rilevamento locale e la riconciliazione a pagamento: il worker si
    #: ferma qui finche' l'interfaccia non risponde con deliver_review, perche'
    #: una proposta tolta adesso non viene ne' inviata ne' pagata. Le quote
    #: sono quelle con cui la foto e' stata calibrata, non quelle dei campi:
    #: durante l'estrazione i campi non sono ancora stati compilati.
    review_requested = pyqtSignal(object, object, float, float)

    def __init__(
        self, doc_paths: list[Path], system_prompt: str | None = None,
        extra_instructions: str | None = None, include_nc_pins: bool = True,
        model: str | None = None, vision_model: str | None = None,
        grounding_prompt: str | None = None, verification_prompt: str | None = None,
        verification_model: str | None = None, terminal_prompt: str | None = None,
        document_roles: dict | None = None,
        component_family: str | None = None,
        *, photo_detector: str = "ai", yolo_model_path: Path | None = None,
    ):
        """@brief Snapshot extraction settings before starting background work.

        @param doc_paths Documents selected for extraction.
        @param system_prompt Optional datasheet system prompt.
        @param extra_instructions Optional component instructions.
        @param include_nc_pins Whether to include physically present NC pins.
        @param model Datasheet AI model identifier.
        @param vision_model Photo AI model identifier.
        @param grounding_prompt Optional photo localization prompt.
        @param verification_prompt Optional photo verification prompt.
        @param verification_model Configured AI verifier model identifier.
        @param terminal_prompt Optional terminal observation prompt.
        @param document_roles Selected document roles, copied for this request.
        @param component_family Selected component geometry family.
        @param photo_detector Photo proposal backend, ai by default or yolo.
        @param yolo_model_path Optional session ONNX path; None uses the service default.
        @note YOLO changes photo proposals only; AI verification remains configured.
        """
        super().__init__()
        self.doc_paths = doc_paths
        self.system_prompt = system_prompt
        self.extra_instructions = extra_instructions
        self.include_nc_pins = include_nc_pins
        self.model = model
        self.vision_model = vision_model
        self.grounding_prompt = grounding_prompt
        self.verification_prompt = verification_prompt
        self.verification_model = verification_model
        self.terminal_prompt = terminal_prompt
        self.document_roles = dict(document_roles) if document_roles is not None else None
        self.component_family = component_family
        self.photo_detector = photo_detector
        self.yolo_model_path = yolo_model_path
        self._review_answered = threading.Event()
        self._review_result = None

    def _review_proposals(self, photo_png, proposals, length_mm, width_mm):
        """@brief Chiede all'interfaccia quali corpi tenere, e aspetta.

        @param photo_png Fotografia calibrata, gia' codificata in PNG.
        @param proposals Proposte del rilevatore locale.
        @param length_mm Estensione della scheda lungo X, in mm.
        @param width_mm Estensione lungo Y, in mm.
        @return Le proposte da tenere, oppure None se la revisione e' annullata.
        @note Gira nel thread del worker: il segnale attraversa i thread e la
            risposta torna da deliver_review. L'attesa e' voluta -- la chiamata
            a pagamento non deve partire prima che qualcuno abbia guardato.
        """
        self._review_result = None
        self._review_answered.clear()
        self.review_requested.emit(photo_png or b"", [dict(p) for p in proposals],
                                   float(length_mm), float(width_mm))
        self._review_answered.wait()
        return self._review_result

    def deliver_review(self, kept) -> None:
        """@brief Consegna al worker l'esito della revisione e lo sblocca.

        @param kept Proposte da tenere, o None per annullare l'estrazione.
        @note Va chiamata SEMPRE, anche quando qualcosa va storto: un worker
            lasciato in attesa non finisce piu'.
        """
        self._review_result = kept
        self._review_answered.set()

    def _forward_phase(self, tool: str, detail: str = "") -> None:
        """@brief Porta in modale lo strumento che il servizio dichiara al lavoro.

        @param tool Chiave in analysis_overlay.TOOLS, es. yolo o llm.
        @param detail Testo libero: l'etichetta del rilevatore o il modello.
        @note Contratto del servizio: `phase(tool, detail)`. Senza conteggio
            (0, 0) la modale resta animata invece di mostrare una percentuale
            inventata. Serve soprattutto al rilevatore locale ONNX, che prima
            girava senza che nulla lo annunciasse: chi guardava vedeva la nuvola
            del modello a pagamento anche mentre non si spendeva niente.
        """
        self.phase.emit(str(tool or ""), str(detail or ""), 0, 0)

    def run(self) -> None:
        """@brief Forward the captured settings and emit extracted data or an error."""
        try:
            params = extract_pin_header_params(
                self.doc_paths,
                model=self.model,
                vision_model=self.vision_model,
                grounding_prompt=self.grounding_prompt, verification_prompt=self.verification_prompt,
                verification_model=self.verification_model, terminal_prompt=self.terminal_prompt,
                document_roles=self.document_roles,
                component_family=self.component_family,
                photo_detector=self.photo_detector,
                yolo_model_path=self.yolo_model_path,
                system_prompt=self.system_prompt,
                extra_instructions=self.extra_instructions,
                include_nc_pins=self.include_nc_pins,
                progress=self.progress.emit,
                review_proposals=self._review_proposals,
                # Cartella del PROGETTO assegnata da chi avvia; None = nessuna cache.
                **accepted_kwargs(extract_pin_header_params,
                                  photo_cache_dir=getattr(self, "photo_cache_dir", None)),
                **phase_kwargs(extract_pin_header_params, self._forward_phase),
            )
            self.done.emit(params)
        except ExtractionError as exc:
            self.error.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - errori rete/API riportati in UI
            self.error.emit(f"Errore API: {exc}")


class ReconcileWorker(QThread):
    """@brief Rilancia la sola riconciliazione a pagamento su proposte gia' riviste.

    E' la coda di ExtractWorker senza la testa: niente documenti, niente
    rilevatore locale, niente filtro dei contatti ne' misura dei bordi. Riceve
    le proposte tenute nella finestra «Corpi rilevati», riaperta sull'ultimo
    salvataggio, e fa UNA richiesta al modello che guarda la fotografia, poi
    campiona i colori e salva l'evidenza come farebbe l'estrazione normale.

    Segnali:
        done(InventoryResult) -- inventario riconciliato
        error(str)            -- messaggio di errore leggibile
        progress(str)         -- avanzamento, compresa la riga della chiamata
        phase(str, str, int, int) -- vedi GenerateWorker.phase
    """

    done = pyqtSignal(object)
    error = pyqtSignal(str)
    progress = pyqtSignal(str)
    phase = pyqtSignal(str, str, int, int)

    def __init__(self, photo_png, proposals, length_mm, width_mm, model, cache_dir,
                 *, declared=None, verification_prompt=None, extra_instructions=None,
                 detector='yolo'):
        """@brief Fotografa le proposte approvate prima di lasciare il thread GUI.

        @param photo_png Fotografia calibrata su cui le proposte furono misurate.
        @param proposals Proposte tenute dalla revisione, inoltrate cosi' come sono.
        @param length_mm Estensione della scheda lungo X, in mm (calibrazione).
        @param width_mm Estensione lungo Y, in mm.
        @param model Modello a pagamento della riconciliazione.
        @param cache_dir Cartella della cache delle risposte e dell'evidenza.
        @param declared Quote dichiarate della scheda, solo per la nota di coerenza.
        @param verification_prompt Note del profilo, come nel percorso normale.
        @param extra_instructions Note dell'utente, come nel percorso normale.
        @param detector Nome del rilevatore da registrare nella calibrazione.
        """
        super().__init__()
        self.photo_png = photo_png
        self.proposals = deepcopy(proposals)
        self.length_mm, self.width_mm = float(length_mm), float(width_mm)
        self.model = model
        self.cache_dir = cache_dir
        self.declared = deepcopy(declared) if declared else None
        self.verification_prompt = verification_prompt
        self.extra_instructions = extra_instructions
        self.detector = detector

    def _forward_phase(self, tool: str, detail: str = "") -> None:
        """@brief Inoltra alla modale la fase dichiarata dal servizio.

        @param tool Chiave in analysis_overlay.TOOLS.
        @param detail Testo libero accanto al nome dello strumento.
        @note Vedi ExtractWorker._forward_phase: stesso contratto
            `phase(tool, detail)`, cosi' il rilancio annuncia le sue fasi
            esattamente come l'estrazione completa.
        """
        self.phase.emit(str(tool or ""), str(detail or ""), 0, 0)

    def run(self) -> None:
        """@brief Esegue la riconciliazione con la stessa chiamata registrata dell'estrazione."""
        from ..services.photo_inventory import reconcile_saved_proposals
        from ..services.openrouter import call_tool
        from ..services.ai_diagnostics import make_logged_call
        try:
            self.phase.emit('llm', self.model, 0, 0)
            report = reconcile_saved_proposals(
                self.photo_png, self.proposals, self.length_mm, self.width_mm, self.model,
                make_logged_call(call_tool, self.progress.emit, log_prompt=True),
                self.progress.emit, self.cache_dir, declared=self.declared,
                verification_prompt=self.verification_prompt,
                extra_instructions=self.extra_instructions, detector=self.detector,
                **phase_kwargs(reconcile_saved_proposals, self._forward_phase))
            self.done.emit(report)
        except Exception as exc:  # noqa: BLE001 - errori rete/API riportati in UI
            self.error.emit(f"Rilancio dell'inventario fallito: {exc}")


class MainWindow(QMainWindow):
    """@brief Coordinate the six-tab component generation workflow."""

    def __init__(self, profile_store=None):
        super().__init__()
        self.setWindowTitle("KiCadGen-Tool")
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
        # footprint, quindi non vivono nei campi del tab Footprint 2D.
        self.castellated_components: list[ComponentBox] = []
        self._extracted_pcb_thickness = None
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
        #: Worker abbandonati che stanno ancora girando: restano referenziati
        #: (un QThread vivo raccolto dal garbage collector abbatte il
        #: processo) e continuano a contare per la guardia di chiusura, ma non
        #: sono piu' candidati a essere interrotti una seconda volta.
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
        self._config = load_config()
        self.profile_store = profile_store or ProfileStore(legacy_config=self._config, legacy_path=config_path())
        self.i18n = I18n(self.profile_store, self._config.get("language", "it"))
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
        self._automatic_footprint_done = False
        self._automatic_3d_done = False
        #: True quando la corsa automatica in atto ha chiesto anche il modello
        #: 3D. Falso = generazione di solo footprint, che e' un esito completo.
        self._automatic_3d_expected = True
        self._automatic_generation_errors = []
        self._startup_dependencies_checked = False
        #: None finche' l'utente non tocca il gruppo «Modello 3D»; True/False
        #: dopo un suo clic. Serve a non revocare la sua scelta quando arriva
        #: una nuova estrazione.
        self._user_3d_choice: bool | None = None
        #: True/False appena `_build_3d_group` ha interrogato il sistema; None
        #: finche' il gruppo non esiste. Va ricordato: `find_freecadcmd()` e' una
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
        #: True finche' il cursore di attesa messo dall'indicatore e' attivo:
        #: `restoreOverrideCursor` va chiamato una volta per ogni `set`.
        self._loading_cursor_set = False

        self._build_menu()
        self._build_central()
        self._apply_user_mode()
        # Project type is restored before the footprint widgets exist.
        self.group_castellated.setChecked(
            self._selected_family() == "aggregate_castellated"
        )
        self._sync_footprint_subtabs()
        self._refresh_docs_list()
        # Il campo nasce vuoto, quindi nessun segnale di modifica lo annuncia:
        # il blocco va applicato una volta all'avvio.
        self._apply_component_name_gate()

        # Connect after initialization so construction does not invalidate a worker.
        for key, widget in vars(self).copy().items():
            if key.startswith("field_"):
                if isinstance(widget, QDoubleSpinBox) or isinstance(widget, QSpinBox):
                    widget.valueChanged.connect(self._invalidate_generated_mesh)
                elif isinstance(widget, QLineEdit) and key in {
                        "field_body_color", "field_pin_color", "field_populated_pins"}:
                    widget.textChanged.connect(self._invalidate_generated_mesh)

    # ── Menu ────────────────────────────────────────────────────────

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu(self.i18n.tr("app.file"))
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
        file_menu.addSeparator()
        self.action_open_output = file_menu.addAction(self.i18n.text("Apri cartella componente"), self._open_project_dir)
        self.action_test_kicad = file_menu.addAction(
            self.i18n.text("Prova in KiCad..."), self._test_in_kicad)
        self.action_export = file_menu.addAction(
            self.i18n.text("Installa nel progetto KiCad..."), self._install_project_library)
        self.action_install_global = file_menu.addAction(
            self.i18n.text("Installa come libreria globale..."), self._install_global_library)
        file_menu.addSeparator()
        self.action_exit = file_menu.addAction(self.i18n.text("Esci"), self.close)

        config_menu = self.menuBar().addMenu(self.i18n.tr("app.configuration"))
        self.action_profiles = config_menu.addAction(
            QIcon(str(Path(__file__).parent / 'assets' / 'profile-edit.svg')),
            self.i18n.text("Gestione tipologie e profili AI..."), self._edit_profiles)
        self.action_translations = config_menu.addAction(
            self.i18n.text("Manage translations..."), self._edit_translations)
        self.action_defaults = config_menu.addAction(
            self.i18n.text("Default generali AI..."), self._edit_llm_settings)
        config_menu.addSeparator()

        self.action_generation_options = config_menu.addAction(
            self.i18n.text("Opzioni di generazione..."), self._edit_generation_options)
        self.action_accepted_models = config_menu.addAction(
            self.i18n.text("Archivio abbinamenti accettati..."), self._show_accepted_models)

        config_menu.addSeparator()
        self.action_open_config = config_menu.addAction(self.i18n.text("Apri config.json"), self._open_config_file)

        help_menu = self.menuBar().addMenu(self.i18n.tr("app.help"))
        self.action_open_tool_dir = help_menu.addAction(
            self.i18n.text("Apri cartella progetto tool"),
            lambda: webbrowser.open(APP_DIR.as_uri()),
        )
        help_menu.addSeparator()
        self.action_about = help_menu.addAction(self.i18n.text("About"), self._show_about)

    def _edit_generation_options(self) -> None:
        dialog = GenerationOptionsDialog(bool(self._config.get("include_nc_pins", True)), self,
                                         advanced_user=self._advanced_user(),
                                         silk_pin_labels=self._silk_pin_labels())
        self.i18n.retranslate_widgets(dialog)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        checked = dialog.include_nc_pins()
        self._config["include_nc_pins"] = checked
        self._config['advanced_user'] = dialog.advanced_user()
        self._config['silk_pin_labels'] = dialog.silk_pin_labels()
        save_config(self._config)
        self._apply_user_mode()
        self._append_log(
            "Pin NC: verranno generati." if checked
            else "Pin NC: NON verranno generati (attenzione: il piedino fisico resta)."
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
            f"Impostazioni LLM aggiornate (SQLite). "
            f"Modello datasheet: {self._default_model()}. "
            f"Modello foto: {self._vision_model() or 'default del servizio foto'}."
        )

    def _advanced_user(self):
        return self._config.get('advanced_user', False) is True

    def _inventory_pause_enabled(self) -> bool:
        """@brief Dice se dopo l'inventario a pagamento ci si ferma a rivederlo.

        @return True finche' qualcuno non ha chiesto esplicitamente di non
            fermarsi piu'.
        @note Predefinito ACCESO, al contrario di `advanced_user`: la pausa e'
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
        return self._config.get('pause_after_inventory', True) is not False

    def _remember_inventory_pause(self, enabled: bool) -> None:
        """@brief Ricorda in config.json se la pausa dell'inventario va offerta.

        @param enabled False quando l'utente ha chiesto di non essere piu' fermato.
        @note Stesso deposito e stesso metodo di «utente avanzato»: la scelta
            vale per le corse successive, non solo per questa sessione.
        """
        self._config['pause_after_inventory'] = bool(enabled)
        save_config(self._config)

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

    def _apply_user_mode(self):
        advanced = self._advanced_user()
        # Keep widget instances and numeric tab indexes stable for existing handlers.
        if not advanced and self.tabs.currentIndex() in (1, 2):
            self.tabs.setCurrentIndex(0)
        self.tabs.setTabVisible(1, advanced)
        self.tabs.setTabVisible(2, advanced)
        self.model_3d_settings_scroll.setVisible(advanced)
        self.group_annotations.setVisible(advanced)
        # Stessa condizione dell'inventario fotografico: la tabella dei corpi
        # esiste solo per le schede popolate, che sono le sole a produrli. Solo
        # `True` conta: con la previsione incerta (None) non si mostra una
        # tabella che potrebbe restare vuota per sempre.
        self.group_components.setVisible(advanced and self._photo_inventory_family() is True)
        # La modifica della revisione non dipende dalla modalita' d'uso: la sua
        # condizione e' la fotografia in memoria. Qui si ri-applica quella
        # disciplina invece di forzare la visibilita', altrimenti un cambio di
        # modalita' riaccenderebbe un comando che non ha nulla da aprire.
        self._refresh_edit_review_buttons()
        if hasattr(self, "action_translations"):
            self.action_translations.setVisible(advanced)

    def _background_work_running(self):
        return any(worker is not None and worker.isRunning() for worker in
                   (self._worker, self._extract_worker, self._cad3d_worker, self._terminal_worker,
                    self._reconcile_worker))

    def closeEvent(self, event):
        """@brief Refuse to close while any QThread worker is still running.

        @param event Qt close event; ignored while a worker runs, otherwise
            forwarded to the base implementation.
        @note A running QThread destroyed with its window aborts the process,
            so the guard covers every worker slot (extract, reconcile,
            generate, CAD 3D, terminals), not just one of them.
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
                self._append_log("Tipologie e profili AI aggiornati nel database SQLite.")
        except (ProfileError, sqlite3.Error, OSError) as exc:
            QMessageBox.warning(self, self.i18n.text('Database profili'), str(exc))
        finally:
            if dialog is not None:
                dialog.deleteLater()

    def _edit_translations(self) -> None:
        """@brief Open the Advanced-user translation catalog editor."""
        dialog = TranslationDialog(self.profile_store, self)
        self.i18n.retranslate_widgets(dialog)
        dialog.exec()
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
            self._append_log('Tipologia eliminata o disattivata: selezionare una tipologia attiva.')
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

    def _default_vision_model(self) -> str:
        return self._profile_values()['vision_model']

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
        for index, key in enumerate(("ai", "yolo", "vlm")):
            self.combo_photo_detector.setItemText(index, self._photo_detector_text(key))
        self.combo_photo_detector.setToolTip(self._photo_detector_text("help"))
        self.label_yolo_model.setText(self._photo_detector_text("model"))
        self.btn_pick_yolo_model.setText(self._photo_detector_text("browse"))
        self.btn_pick_yolo_model.setAccessibleName(self._photo_detector_text("picker"))
        self.btn_pick_yolo_model.setToolTip(self._photo_detector_text("picker"))
        default_path = APP_DIR / "models" / "pcb_yolov8s.onnx"
        self.field_yolo_model.setPlaceholderText(
            self._photo_detector_text("default", path=default_path))
        self.field_yolo_model.setToolTip(
            self._photo_detector_text("path_help", path=default_path))
        self.label_vlm_model.setText(self._photo_detector_text("vlm_model"))
        self.btn_pick_vlm_model.setText(self._photo_detector_text("vlm_pick"))
        self.btn_pick_vlm_model.setAccessibleName(self._photo_detector_text("vlm_model"))
        for widget in (self.field_vlm_model, self.btn_pick_vlm_model):
            widget.setToolTip(self._photo_detector_text("vlm_help"))
        self.field_vlm_model.setText(self._photo_detector_model())
        self._on_photo_detector_changed()

    def _on_photo_detector_changed(self) -> None:
        """@brief Mostra i pesi locali o il modello di visione, secondo il rilevatore.

        @note Una riga per volta: i pesi ONNX non c'entrano nulla quando a
            guardare la fotografia e' un modello remoto, e il contrario. La
            riga che resta e' quella su cui si puo' davvero intervenire.
        """
        detector = self.combo_photo_detector.currentData()
        self.field_yolo_model.setEnabled(detector == "yolo")
        self.btn_pick_yolo_model.setEnabled(detector == "yolo")
        if hasattr(self, "_models_form"):
            self._models_form.setRowVisible(self._row_yolo_model, detector == "yolo")
            # Il modello di visione serve in entrambi i percorsi fotografici:
            # con vlm rileva ed elenca, con yolo riconcilia le proposte locali.
            # E' sempre «il modello che guarda la fotografia».
            self._models_form.setRowVisible(self._row_vlm_model, detector in ("yolo", "vlm"))

    def _pick_session_yolo_model(self) -> None:
        """Select optional ONNX weights without reading or saving preferences."""
        selected, _ = QFileDialog.getOpenFileName(
            self, self._photo_detector_text("picker"),
            self.field_yolo_model.text().strip() or str(APP_DIR / "models"),
            self._photo_detector_text("filter"),
        )
        if selected:
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
            self._append_log(f"Modello di visione per questa sessione: {chosen}.")

    def _photo_detector_options(self) -> tuple[str, Path | None]:
        """Snapshot the selected backend; AI ignores any remembered ONNX path."""
        detector = self.combo_photo_detector.currentData() or "ai"
        path = self.field_yolo_model.text().strip()
        return detector, Path(path) if detector == "yolo" and path else None

    def _on_model_combo_changed(self) -> None:
        self._session_model = self.combo_model.currentData()
        if self._session_model:
            self._append_log(
                f"Modello per questa sessione: {self._session_model} "
                "(il default globale non cambia)."
            )
        else:
            self._append_log(f"Modello: default globale ({self._default_model()}).")

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
        if not config_path().is_file():
            save_config(self._config)
        webbrowser.open(config_path().as_uri())

    def _show_about(self) -> None:
        QMessageBox.about(
            self, self.i18n.text("About"),
            self.i18n.tr("about.description")
        )

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

        project_tab = self._build_project_tab()
        footprint_tab = self._build_footprint_tab()
        generate_tab = self._build_generate_tab()
        preview_tab = self._build_preview_tab()
        model_3d_tab = self._build_3d_tab()
        kicad_tab = self._build_kicad_tab()

        language_row = QHBoxLayout()
        language_row.setSpacing(10)
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
        self.tabs.addTab(footprint_tab, self.i18n.tr("tab.footprint.name"))
        self.tabs.addTab(generate_tab, self.i18n.tr("tab.generate.name"))
        self.tabs.addTab(preview_tab, self.i18n.tr("tab.preview.name"))
        self.tabs.addTab(model_3d_tab, self.i18n.tr("tab.model3d.name"))
        self.tabs.addTab(kicad_tab, self.i18n.tr("tab.kicad.name"))
        # Icone a linea nello stile delle barre, al posto delle emoji a colori
        # che stavano nel titolo (utente, 2026-09-24: «stonata con il resto»).
        for index, asset in enumerate(TAB_ICONS):
            self.tabs.setTabIcon(index, self._asset_icon(asset))
        self._set_acquisition_tabs_enabled(False)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        layout.addWidget(self.tabs)

        self.setCentralWidget(central)
        self.analysis_overlay = AnalysisOverlay(central, i18n=self.i18n)
        self.analysis_overlay.log_requested.connect(self.ai_log_dialog.exec)
        self.analysis_overlay.abort_requested.connect(self._request_abort)
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
        self._config["language"] = language
        save_config(self._config)
        self._refresh_language_texts()
        self.tabs.setTabText(0, self.i18n.tr("tab.project.name"))
        self.tabs.setTabText(1, self.i18n.tr("tab.footprint.name"))
        self.tabs.setTabText(2, self.i18n.tr("tab.generate.name"))
        self.tabs.setTabText(3, self.i18n.tr("tab.preview.name"))
        self.tabs.setTabText(4, self.i18n.tr("tab.model3d.name"))
        self.tabs.setTabText(5, self.i18n.tr("tab.kicad.name"))
        self.i18n.retranslate_widgets(self)
        self.menuBar().actions()[0].setText(self.i18n.tr("app.file"))
        self.menuBar().actions()[1].setText(self.i18n.tr("app.configuration"))
        self.menuBar().actions()[2].setText(self.i18n.tr("app.help"))
        self.action_open_output.setText(self.i18n.text("Apri cartella componente"))
        self.action_test_kicad.setText(self.i18n.text("Prova in KiCad..."))
        self.action_export.setText(self.i18n.text("Installa nel progetto KiCad..."))
        self.action_install_global.setText(
            self.i18n.text("Installa come libreria globale..."))
        self.action_exit.setText(self.i18n.text("Esci"))
        self.action_profiles.setText(self.i18n.text("Gestione tipologie e profili AI..."))
        self.action_translations.setText(self.i18n.text("Manage translations..."))
        self.action_defaults.setText(self.i18n.text("Default generali AI..."))
        self.action_generation_options.setText(self.i18n.text("Opzioni di generazione..."))
        self.action_accepted_models.setText(
            self.i18n.text("Archivio abbinamenti accettati..."))
        self.action_open_config.setText(self.i18n.text("Apri config.json"))
        self.action_open_tool_dir.setText(self.i18n.text("Apri cartella progetto tool"))
        self.action_about.setText(self.i18n.text("About"))
        self._retranslate_component_types()
        self._refresh_model_combo()
        self._refresh_output_actions()
        self._apply_view_cube_labels()
        # Testi che la ripassata generica non copre: voci dei selettori del
        # tipo di contatto, tooltip composti delle icone della barra 3D e
        # quote disegnate nell'anteprima 2D (nota 8 dell'utente).
        self._retranslate_contact_style_combos()
        self._refresh_workflow_tooltips()
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
        for index in (3, 4):
            self.tabs.setTabEnabled(index, enabled)

    # ── Tab 1: Progetto ────────────────────────────────────────────

    def _build_project_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

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

        # Il nome proposto dall'estrazione vive qui, accanto al campo, e non
        # dentro il campo. Prima l'estrazione lo scriveva da sola: ogni analisi
        # che proponeva un nome diverso spostava docs/ altrove e lasciava
        # indietro la cartella precedente in area di lavoro.
        self._suggested_name = ""
        self.lbl_name_suggestion = QLabel()
        self.lbl_name_suggestion.setWordWrap(True)
        self.lbl_name_suggestion.setProperty("i18n_skip", True)
        self.lbl_name_suggestion.setStyleSheet(f"color: {BLUE};")
        self.btn_use_suggested_name = QPushButton("Usa questo nome")
        self.btn_use_suggested_name.setObjectName("useSuggestedNameButton")
        self.btn_use_suggested_name.clicked.connect(self._apply_suggested_name)
        suggestion_row = QHBoxLayout()
        suggestion_row.setContentsMargins(0, 0, 0, 0)
        suggestion_row.setSpacing(6)
        suggestion_row.addWidget(self.lbl_name_suggestion, 1)
        suggestion_row.addWidget(self.btn_use_suggested_name)
        self.widget_name_suggestion = QWidget()
        self.widget_name_suggestion.setLayout(suggestion_row)
        self.widget_name_suggestion.hide()
        folder_layout.addRow("", self.widget_name_suggestion)

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
            "Tutto viene generato qui; per portarlo nella libreria vera usa "
            "\"Esporta in libreria...\" nel tab \"Genera\"."
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
            "cercare nel datasheet (fori e drill per un THT, land pattern per un "
            "SMD, semi-fori sul bordo per un modulo castellated)."
        )
        self.combo_component_type.currentIndexChanged.connect(
            self._on_component_type_changed
        )
        ai_form.addRow("Tipologia componente:", self.combo_component_type)
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
        model_row.addWidget(self.combo_model, 1)
        model_row.addWidget(self.btn_pick_model)
        models_form.addRow("Modello AI:", model_row)
        self.label_photo_detector = QLabel()
        self.combo_photo_detector = QComboBox()
        self.combo_photo_detector.addItem("", "ai")
        self.combo_photo_detector.addItem("", "yolo")
        self.combo_photo_detector.addItem("", "vlm")
        # User-selected startup default; explicit AI fallback remains available.
        self.combo_photo_detector.setCurrentIndex(self.combo_photo_detector.findData("yolo"))
        self.combo_photo_detector.currentIndexChanged.connect(self._on_photo_detector_changed)
        models_form.addRow(self.label_photo_detector, self.combo_photo_detector)
        self.label_yolo_model = QLabel()
        self.field_yolo_model = QLineEdit()
        self.field_yolo_model.setClearButtonEnabled(True)
        self.btn_pick_yolo_model = QPushButton()
        self.btn_pick_yolo_model.setAutoDefault(False)
        self.btn_pick_yolo_model.clicked.connect(self._pick_session_yolo_model)
        yolo_row = QHBoxLayout()
        yolo_row.addWidget(self.field_yolo_model, 1)
        yolo_row.addWidget(self.btn_pick_yolo_model)
        models_form.addRow(self.label_yolo_model, yolo_row)
        self._row_yolo_model = models_form.rowCount() - 1
        self.label_vlm_model = QLabel()
        self.field_vlm_model = QLineEdit()
        self.field_vlm_model.setReadOnly(True)
        self.btn_pick_vlm_model = QPushButton()
        self.btn_pick_vlm_model.setAutoDefault(False)
        self.btn_pick_vlm_model.clicked.connect(self._pick_session_vlm_model)
        vlm_row = QHBoxLayout()
        vlm_row.addWidget(self.field_vlm_model, 1)
        vlm_row.addWidget(self.btn_pick_vlm_model)
        models_form.addRow(self.label_vlm_model, vlm_row)
        self._row_vlm_model = models_form.rowCount() - 1
        self._models_form = models_form
        # These data-bearing widgets use explicit keys and local I18n fallbacks.
        for widget in (self.label_photo_detector, self.combo_photo_detector,
                       self.label_yolo_model, self.field_yolo_model, self.btn_pick_yolo_model,
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
        tools_row = QHBoxLayout()
        tools_row.setSpacing(16)
        tools_row.setContentsMargins(0, 4, 0, 4)
        tools_layout.addLayout(tools_row)
        tools_row.addStretch()
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
        # Riapre «Corpi rilevati» sulle proposte salvate nella cartella del
        # componente e, all'OK, le RISALVA li': gratuito, nessuna chiamata AI.
        # Decisione dell'utente: «se il file json esiste nella cartella deve
        # essere possibile aprire la revisione bypassando interrogazione AI».
        self.btn_reopen_review = QPushButton("Rivedi proposte salvate…")
        self.btn_reopen_review.setToolTip(
            "Passo 1 della pipeline — Riapre i corpi rilevati salvati in photo-review/ "
            "nella cartella del componente e, alla conferma, salva le proposte tenute "
            "nello stesso file. Nessuna richiesta AI: gratuito."
        )
        self.btn_reopen_review.setEnabled(False)
        self.btn_reopen_review.clicked.connect(self._reopen_detector_review)
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
        # Richiesta dell'utente: «se questa fase è stata completata, dovrebbe
        # essere possibile poterla sempre editare, inserire un pulsante in
        # basso chiaro». E' lo stesso comando della riga degli strumenti, ma
        # qui sta accanto agli altri ritorni sulla revisione, dove si cerca
        # quando l'analisi e' finita e ci si accorge che qualcosa va corretto.
        self.btn_edit_component_review = QPushButton("Modifica la revisione dei componenti…")
        self.btn_edit_component_review.setToolTip(
            "Passo 3 della pipeline — Riapre la revisione della fase completata: "
            "ingombri sulla foto, quote e scelta dei modelli KiCad. Non avvia la "
            "generazione e non fa richieste AI: gratuito. Dopo averla applicata, "
            "«Rigenera modello 3D» nella scheda Modello 3D ricostruisce lo STEP."
        )
        self.btn_edit_component_review.setEnabled(False)
        self.btn_edit_component_review.clicked.connect(self._show_photo_comparison)
        # Richiesta dell'utente: «qui c'è troppa confusione con i pulsanti
        # sotto». Il comando di revisione esisteva due volte -- nella riga
        # degli strumenti come «Rivedi foto / scegli modelli KiCad…» e qui in
        # fondo -- avviando esattamente la stessa finestra. Ne resta uno solo,
        # quello in fondo alla scheda, dove si cerca a fase finita.
        # `btn_photo_comparison` non e' piu' un secondo widget: e' l'altro nome
        # dello stesso pulsante, cosi' i percorsi che aprivano il confronto
        # fotografico puntano al comando vero e non a un doppione nascosto.
        self.btn_photo_comparison = self.btn_edit_component_review
        self._photo_png = None
        self._photo_overlay_png = None
        self._photo_source = ""
        self.btn_ai_log = QPushButton("≡ Log AI")
        self.btn_ai_log.setToolTip("Apri il log completo dell'analisi AI, aggiornato in tempo reale")
        self.btn_ai_log.setAccessibleName("Apri log analisi AI")
        tools_row.addWidget(self.btn_ai_log)
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

        self.btn_component_prompt = QPushButton("Modifica prompt…")
        self.btn_component_prompt.setToolTip("Modifica le istruzioni AI specifiche di questo componente")
        tools_row.addWidget(self.btn_component_prompt)
        tools_row.addStretch()
        analysis_layout.addSpacing(10)
        analysis_layout.addWidget(self.btn_extract_ai, alignment=Qt.AlignmentFlag.AlignHCenter)
        # Consuntivo dell'ultima analisi: resta visibile dopo la chiusura
        # della modale, che sparisce appena il lavoro finisce.
        self.lbl_ai_cost = QLabel()
        self.lbl_ai_cost.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.lbl_ai_cost.setStyleSheet("color: #a6adc8;")
        self.lbl_ai_cost.setTextFormat(Qt.TextFormat.PlainText)
        self.lbl_ai_cost.hide()
        analysis_layout.addWidget(self.lbl_ai_cost)
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
        # La pausa dopo l'inventario e' un'impostazione, non un comando: sta
        # sopra la riga dei rientri perche' e' li' che si ragiona di «dove
        # posso rimettere le mani», e resta visibile anche prima della prima
        # analisi -- e' allora che si decide se farsi fermare o no.
        self.chk_pause_after_inventory = QCheckBox(
            "Fermati dopo l'inventario AI per rivedere i corpi (gratuito)")
        self.chk_pause_after_inventory.setToolTip(
            "Quando l'inventario AI ha finito, la pipeline si ferma e propone la "
            "revisione dei componenti prima dell'abbinamento dei modelli KiCad e della "
            "generazione 3D: da lì si spostano, ridimensionano, aggiungono ed eliminano "
            "i corpi. La revisione non fa richieste AI: è gratuita. Togliendo la spunta "
            "l'analisi prosegue da sola con i corpi proposti dall'AI.")
        self.chk_pause_after_inventory.setChecked(self._inventory_pause_enabled())
        self.chk_pause_after_inventory.toggled.connect(self._on_inventory_pause_toggled)
        pause_row = QHBoxLayout()
        pause_row.addStretch()
        pause_row.addWidget(self.chk_pause_after_inventory)
        pause_row.addStretch()
        outer.addLayout(pause_row)
        # Richiesta dell'utente: «devo poter tornare sui miei passi senza fare
        # intervenire AI». I tre comandi esistevano gia' ma stavano in fila
        # senza dire di essere i passi di UNA pipeline ne' quale costasse: qui
        # l'intestazione li enumera nell'ordine in cui la pipeline li esegue e
        # dichiara il costo di ciascuno. La riga sotto segue lo stesso ordine.
        self.lbl_pipeline_steps = QLabel(
            "Rientri nella pipeline, nell'ordine: 1) rivedi i corpi rilevati — gratuito · "
            "2) rilancia l'inventario AI — a pagamento · 3) modifica la revisione dei "
            "componenti — gratuito · poi «Rigenera modello 3D» nella scheda Modello 3D — "
            "gratuito. Ogni passo riparte da quello che il precedente ha salvato.")
        self.lbl_pipeline_steps.setWordWrap(True)
        self.lbl_pipeline_steps.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.lbl_pipeline_steps.setStyleSheet("color: #a6adc8;")
        outer.addWidget(self.lbl_pipeline_steps)
        # Richiesta dell'utente: «un pulsante in basso nella scheda
        # principale» per riaprire la revisione dopo averla salvata. Sta
        # sotto tutto, non nella riga degli strumenti dell'analisi, cosi' si
        # trova anche a estrazione chiusa e senza cercarlo fra le opzioni.
        # L'ordine dei tre comandi e' quello della pipeline (rilevatore ->
        # inventario a pagamento -> revisione dei componenti), non quello in
        # cui sono stati aggiunti: e' l'ordine a dire che sono passi di una
        # stessa catena e non tre azioni scorrelate.
        reopen_row = QHBoxLayout()
        reopen_row.addStretch()
        reopen_row.addWidget(self.btn_reopen_review)
        reopen_row.addWidget(self.btn_relaunch_inventory)
        reopen_row.addWidget(self.btn_edit_component_review)
        reopen_row.addStretch()
        outer.addLayout(reopen_row)
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

        self.field_component_prompt.setPlainText(
            component_types.apply_block(
                self.field_component_prompt.toPlainText(), component_type
            )
        )

        # La tipologia decide anche quale gruppo di parametri si usa nel tab
        # "Footprint 2D": i moduli castellated non stanno in una matrice.
        if key and hasattr(self, "group_castellated"):
            self.group_castellated.setChecked(self._selected_family() == "aggregate_castellated")

        self._config["component_type"] = key
        save_config(self._config)
        self._refresh_model_combo()
        if component_type is not None:
            self._append_log(f"Tipologia componente: {component_type.label}.")

    def _component_name(self) -> str:
        return self.field_name.text().strip() if hasattr(self, "field_name") else ""

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
                "Il componente non ha ancora un nome: nessuna cartella da usare.")
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
        if hasattr(self, "btn_reopen_review") and not self._background_work_running():
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
                action.setToolTip(
                    "" if footprint_available else
                    self.i18n.text("Genera prima il footprint del componente corrente"))
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
                rendered = self.i18n.text(source) if source else ""
                if isinstance(action, QWidget):
                    action.setProperty("i18n_tooltip_source", source)
                    action.setProperty("i18n_tooltip_rendered", rendered)
                action.setToolTip(rendered)
        if hasattr(self, "label_kicad_footprint"):
            self.label_kicad_footprint.setText(
                str(footprint) if footprint_available else "—")
            self.label_kicad_step.setText(
                str(step) if (step is not None and step.is_file()) else "—")
            self.label_kicad_status.setText(
                self.i18n.text("Footprint e modello STEP pronti per la verifica.")
                if complete_available else
                self.i18n.text("Genera footprint e modello STEP per continuare."))
        return footprint_available

    def _on_name_changed(self, text: str) -> None:
        if hasattr(self, "field_name_mirror"):
            self.field_name_mirror.setText(text)
        self._update_project_path_label()
        self._apply_component_name_gate()

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
                widget.setEnabled(named and not busy)
                # Stesso testo del costruttore, letterale in entrambi i posti:
                # e' cosi' che il controllo del catalogo (tests/test_i18n.py)
                # lo vede. Se cambia qui deve cambiare la' -- il costo
                # dichiarato e' la parte che non puo' divergere.
                widget.setToolTip(reason or
                                  "Avvia l'estrazione dai documenti e dalle foto selezionati. "
                                  "Analisi A PAGAMENTO: consuma credito OpenRouter. I modelli usati e la "
                                  "spesa vengono scritti nel log prima di partire.")
            else:
                widget.setEnabled(named)
                widget.setToolTip(reason)
        # Il nome decide anche la cartella in cui cercare le proposte salvate:
        # cambiato il nome, i pulsanti della revisione vanno ricalcolati --
        # ma non durante un'analisi, dove restano spenti fino alla fine.
        if hasattr(self, "btn_reopen_review") and not busy:
            self._refresh_reopen_review_button()
        # Il nome decide anche la cartella del diario: cambiandolo, il log
        # apribile e' un altro (o non c'e').
        if hasattr(self, "btn_ai_log"):
            self._refresh_ai_log_button()

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
        try:
            length = (self.field_cast_body_length.value() if self._is_castellated()
                      else self.field_body_length.value())
            width = (self.field_cast_body_width.value() if self._is_castellated()
                     else self.field_body_width.value())
        except AttributeError:
            return None
        return (length, width) if length > 0 and width > 0 else None

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

    def _selected_documents(self):
        return DocumentSet(self._docs_dir()).selected()

    def _add_docs(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, self.i18n.text("Seleziona documentazione (datasheet, PDF, immagini...)"),
            browse_start_dir(self._config),
            self.i18n.text(
                "Documenti (*.pdf *.png *.jpg *.jpeg *.txt *.md *.xlsx *.csv);;Tutti i file (*)")
        )
        if not files:
            return
        remember_browse_dir(self._config, files[0])
        docs_dir = self._docs_dir()
        docs_dir.mkdir(parents=True, exist_ok=True)
        for f in files:
            shutil.copy2(f, docs_dir / Path(f).name)
        self._refresh_docs_list()

    def _remove_doc(self) -> None:
        item = self.docs_list.currentItem()
        if item is None:
            return
        target = self._docs_dir() / item.text()
        confirm = QMessageBox.question(
            self, self.i18n.text("Rimuovi file"),
            self.i18n.tr(
                "document.delete_permanently", name=item.text(), directory=self._docs_dir()),
        )
        if confirm == QMessageBox.StandardButton.Yes:
            target.unlink(missing_ok=True)
            self._refresh_docs_list()

    def _open_docs_folder(self) -> None:
        docs_dir = self._docs_dir()
        docs_dir.mkdir(parents=True, exist_ok=True)
        webbrowser.open(docs_dir.as_uri())

    def _extract_with_ai(self) -> None:
        if self._background_work_running() or self._automatic_generation_active:
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
        if photo_detector == "yolo":
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

        try:
            profile = self.profile_store.snapshot(self._profile_key())
        except (ProfileError, sqlite3.Error, OSError) as exc:
            QMessageBox.warning(
                self, self.i18n.text('Profilo AI non disponibile'), str(exc))
            return

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
            f"Estrazione da {len(doc_paths)} file con il modello AI {model}"
            + (" (solo questa sessione)." if self._session_model else ".")
        )
        self._on_ai_progress(f"Profilo AI: {self.combo_component_type.currentText()}.")
        self._on_ai_progress(f"Modello AI unico: {model}.")
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
                f"Tipologia «{self.combo_component_type.currentText()}»: l'analisi NON passa "
                "dall'inventario fotografico, quindi il rilevatore dei corpi e la revisione "
                "«Corpi rilevati» non verranno eseguiti e nessuna chiamata guardera' la "
                "fotografia. I corpi dalla foto si ottengono con le tipologie di scheda "
                "popolata (aggregato a fori passanti o modulo castellated).")
        elif photo_detector in ('yolo', 'vlm'):
            # Detto sempre, non solo quando differisce dal modello principale:
            # e' la chiamata a pagamento che guarda la fotografia, e va scritta
            # nel log prima di partire, non dedotta dopo dalla fattura.
            self._on_ai_progress(
                f"Chiamata sulla fotografia affidata a {verification_model}"
                + (" (scelto per questa sessione)." if self._vlm_model else " (da impostazioni)."))
        elif verification_model != model:
            self._on_ai_progress(f"Rilevamento dei corpi affidato a {verification_model}.")

        self._extract_worker = ExtractWorker(
            doc_paths,
            model=model,
            vision_model=photo_model, verification_model=verification_model,
            grounding_prompt=profile['grounding_prompt'],
            verification_prompt=profile['verification_prompt'],
            terminal_prompt=profile['terminal_prompt'],
            document_roles=document_roles,
            component_family=self._selected_family(),
            photo_detector=photo_detector,
            yolo_model_path=yolo_model_path,
            system_prompt=profile['system_prompt'],
            extra_instructions=self.field_component_prompt.toPlainText().strip() or None,
            include_nc_pins=bool(self._config.get("include_nc_pins", True)),
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
        self._auto_after_extraction = not self._advanced_user()
        self._auto_generation_params = None
        self._extract_finish_pending = False
        self.analysis_overlay.clear_cost()
        self.lbl_ai_cost.hide()
        self.btn_extract_ai.setText(self.i18n.text("Analisi in corso..."))
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
        """@brief Il pacchetto di revisione salvato nella cartella del componente.

        @return ReviewBundle, oppure None: senza nome non c'e' cartella, e un
            pacchetto assente o corrotto vale come nessun pacchetto.
        @note E' l'unica fonte che sopravvive al riavvio: sta nella cartella
            del componente, non nella cache comune, quindi con il solo nome
            digitato si sa gia' se c'e' qualcosa da riaprire.
        """
        from ..services.photo_inventory import load_review_bundle
        if not self._named():
            return None
        try:
            return load_review_bundle(self._project_dir())
        except (OSError, ValueError):
            return None

    def _write_review_bundle(self, proposals, photo_png, length, width, *,
                             source_json='', detector='yolo'):
        """@brief Scrive il pacchetto di revisione nella cartella del componente.

        @return Percorso di `proposals.json`, oppure None se non c'e' un nome
            o la scrittura fallisce (annotato nel log, senza fermare nulla).
        @note Operazione offline e gratuita: nessuna chiamata a modelli.
        """
        from ..services.photo_inventory import save_review_bundle
        if not self._named():
            return None
        try:
            return save_review_bundle(self._project_dir(), proposals, photo_png, length, width,
                                      detector=detector, source_json=source_json)
        except (OSError, ValueError) as exc:
            self._append_log(f"ATTENZIONE proposte non salvate nella cartella del componente: {exc}")
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
                self._append_log(f"Foto calibrata in cache non leggibile ({exc}): uso quella dell'estrazione.")
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
            self._append_log(f"Proposte del rilevatore salvate in {path} ({len(saved.proposals)} corpi): "
                             "la revisione si puo' riaprire senza chiamate AI.")

    def _refresh_reopen_review_button(self) -> None:
        """@brief Abilita revisione (gratuita) e rilancio (a pagamento) quando c'e' cosa riaprire.

        @note Entrambi seguono la stessa condizione: un pacchetto valido nella
            cartella del componente (quote positive e almeno una proposta),
            oppure -- ripiego di sessione -- proposte fresche in cache con le
            quote dell'ultima estrazione. Senza nome non c'e' cartella, quindi
            entrambi spenti. Non serve un'estrazione in questa sessione:
            appena il nome punta a una cartella con il pacchetto, si riapre.
        """
        bundle = self._review_bundle()
        if bundle is not None:
            # Senza foto calibrata i riquadri non si possono ne' vedere ne'
            # riconciliare: pulsanti spenti invece di un avviso al clic.
            available = bundle.photo_path is not None or bool(self._photo_png)
        else:
            available = self._fresh_cache_evidence() is not None
        self._set_review_buttons_enabled(available)

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
            `btn_reopen_review_3d`, `btn_relaunch_inventory_3d`,
            `btn_edit_component_review_3d` -- `isEnabled()` e' in AND con lo
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

    def _mirror_project_action(self, source, slot, row, asset: str):
        """@brief Crea nella barra della scheda Modello 3D la copia a icona di un comando della scheda Progetto.

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
        for name in ("btn_reopen_review", "btn_relaunch_inventory",
                     "btn_reopen_review_3d", "btn_relaunch_inventory_3d"):
            self._set_action_available(name, bool(enabled))
        self._refresh_edit_review_buttons()

    def _refresh_edit_review_buttons(self) -> None:
        """@brief Allinea le copie di «Modifica la revisione…» a cio' che il comando puo' aprire.

        @note La condizione e' la stessa che `_show_photo_comparison` verifica:
            la fotografia calibrata, oppure -- quando quella manca -- la sola
            sovrapposizione degli ingombri, che apre il confronto in sola
            lettura. Legare il pulsante al solo `_photo_png` nascondeva un
            comando che invece funzionava, quindi qui si guardano entrambe le
            immagini. Senza nessuna delle due il comando non puo' fare nulla e
            sparisce, secondo la disciplina di `_set_action_available`.
        @note Metodo unico perche' le copie (scheda Progetto e scheda Modello
            3D) sono lo stesso comando: devono restare per costruzione nello
            stesso stato, da qualunque percorso si arrivi.
        """
        editable = bool(getattr(self, "_photo_png", None)
                        or getattr(self, "_photo_overlay_png", None))
        for name in ("btn_edit_component_review", "btn_edit_component_review_3d"):
            self._set_action_available(name, editable)
        self._refresh_pipeline_steps_label()

    def _refresh_pipeline_steps_label(self) -> None:
        """@brief L'intestazione dei rientri si vede solo quando un rientro esiste.

        @note Stessa disciplina di `_set_action_available`: una riga che
            annuncia tre passi sotto una fila vuota di pulsanti descrive
            qualcosa che non c'e'. Basta UN passo disponibile perche' valga la
            pena spiegare l'ordine, perche' e' da li' che si riparte.
        @note `getattr` difensivo: i pulsanti nascono prima dell'intestazione
            durante la costruzione della scheda, e un ricalcolo anticipato non
            deve far cadere la finestra.
        """
        label = getattr(self, "lbl_pipeline_steps", None)
        if label is None:
            return
        # `isVisibleTo(genitore)` e non `isVisible()`: la finestra dei test non
        # viene mai mostrata, e `isVisible()` direbbe «nascosto» per tutti,
        # spegnendo l'intestazione anche quando i comandi ci sono.
        buttons = [getattr(self, name, None)
                   for name in ("btn_reopen_review", "btn_relaunch_inventory",
                                "btn_edit_component_review")]
        label.setVisible(any(button is not None and button.parentWidget() is not None
                             and button.isVisibleTo(button.parentWidget())
                             for button in buttons))

    def _on_inventory_pause_toggled(self, checked: bool) -> None:
        """@brief Ricorda la scelta sulla pausa e la scrive nel diario.

        @param checked True quando l'utente vuole essere fermato dopo l'inventario.
        @note Lo stato scritto e' quello del comando: e' l'unico posto da cui
            si accende e si spegne, quindi non puo' divergere dalle impostazioni.
        """
        self._remember_inventory_pause(checked)
        self._append_log(
            "Pausa dopo l'inventario AI: attiva. La revisione dei componenti verrà "
            "proposta prima dell'abbinamento modelli e del 3D (gratuita)."
            if checked else
            "Pausa dopo l'inventario AI: disattivata. L'analisi proseguirà da sola con "
            "i corpi proposti dall'AI; la revisione resta disponibile dai pulsanti in basso.")

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
                self._append_log(f"Foto calibrata del pacchetto non leggibile ({exc}).")
        if not photo_png and self._photo_png:
            photo_png = self._photo_png
            self._append_log("Foto calibrata assente in photo-review/: uso quella dell'estrazione in memoria.")
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
        """@brief Riapre «Corpi rilevati» sul pacchetto salvato e, all'OK, lo risalva. Gratis.

        @note Nessuna chiamata AI in nessun ramo: la revisione e' sul thread
            GUI e l'OK scrive soltanto `photo-review/proposals.json` con le
            proposte tenute -- comprese le disegnate a mano (`_yolo_manual_box`)
            e tutti i campi cosi' come sono. Chi vuole ripagare l'inventario
            ha il pulsante accanto (`_relaunch_inventory`). Annullare non
            tocca il file.
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
        self._append_log(f"Proposte riaperte da {bundle.json_path}: {len(bundle.proposals)} corpi, "
                         "nessuna richiesta AI.")
        try:
            from .yolo_review_dialog import YoloReviewDialog
            from ..services.photo_yolo import detector_classes
            dialog = YoloReviewDialog(photo_png, [dict(p) for p in bundle.proposals],
                                      bundle.body_length_mm, bundle.body_width_mm,
                                      detector_classes(), self)
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            kept = list(dialog.kept()) if accepted else None
        except Exception as exc:  # noqa: BLE001 - la vista non deve far cadere la finestra
            self._append_log(f"ATTENZIONE revisione dei corpi non disponibile: {exc}")
            return
        if kept is None:
            self._append_log("Revisione dei corpi rilevati annullata: nessuna richiesta avviata, file invariato.")
            return
        path = self._write_review_bundle(kept, photo_png, bundle.body_length_mm, bundle.body_width_mm,
                                         source_json=bundle.source_json, detector=bundle.detector)
        if path is not None:
            self._append_log(f"Revisione salvata offline in {path}: {len(kept)} proposte tenute, "
                             "nessuna richiesta AI (gratuito). Per l'inventario usa «Rilancia "
                             "inventario AI (a pagamento)…».")
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
        @param profile AI profile snapshot (verification prompt).
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
            self._append_log("Nessuna estrazione in questa sessione: l'inventario rilanciato "
                             "aggiorna i componenti senza avviare la generazione automatica.")
        self._set_review_buttons_enabled(False)
        # Dichiarata PRIMA di partire, nel log e nella modale: e' l'unica
        # spesa di questo percorso e chi preme deve saperlo prima, non dopo.
        announcement = (f"Rilancio dell'inventario su {len(bundle.proposals)} proposte "
                        f"da {bundle.json_path.name}: una richiesta a pagamento al modello {model}.")
        self._on_ai_progress(announcement)
        self._last_extracted = params
        declared = dict(layout=params.layout, castellated_sides=params.castellated_sides,
                        body_length_mm=params.body_length_mm, body_width_mm=params.body_width_mm)
        self._reconcile_worker = ReconcileWorker(
            photo_png, bundle.proposals, bundle.body_length_mm, bundle.body_width_mm, model,
            self._photo_cache_dir(),
            declared=declared, verification_prompt=profile['verification_prompt'],
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
            self._append_log("Inventario di un rilancio interrotto dall'utente: scartato, "
                             "i componenti restano come sono.")
            return
        base = self._last_extracted
        if base is None:
            self._on_extract_error("Nessuna estrazione a cui applicare l'inventario rilanciato.")
            return
        notes = str(base.notes or '').strip()
        suffix = (f"Inventario rilanciato: {len(report.components)} ingombri riconciliati "
                  f"da {report.calibration.get('proposal_count', 0)} proposte riviste. "
                  "Modello meccanico semplificato, senza marcature; altezze indicative, "
                  "non misure certificate.")
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

    def _hide_analysis_overlay(self) -> None:
        self.analysis_overlay.stop()
        if self._analysis_tabs_enabled is not None:
            self.tabs.setEnabled(self._analysis_tabs_enabled)
            self._analysis_tabs_enabled = None

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

    def _abortable_operation(self):
        """@brief L'operazione in corso che «Interrompi» puo' fermare, se c'e'.

        @return (natura, worker, fase) con natura in {'cad', 'ai'}, oppure None
            quando non c'e' nulla da fermare.
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
        if kind == "cad":
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
                f"Interruzione chiesta dall'utente ({phase}), ma nel frattempo "
                "l'operazione era già terminata: niente da fermare.")
            self.analysis_overlay.set_abortable(False)
            return
        self._abort_pending = True
        self.analysis_overlay.set_abortable(False)
        if kind == "cad":
            self._abort_cad_run(worker, phase)
        else:
            self._abort_ai_run(worker, phase)

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
            "«Interrompi» non è disponibile per questa generazione: il generatore 3D in "
            "uso non accetta il canale di interruzione, quindi premerlo non fermerebbe "
            "nulla. L'attesa va lasciata finire, oppure si chiude il programma.")

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
            f"INTERRUZIONE chiesta dall'utente durante: {phase}. "
            "Il processo FreeCAD viene terminato; il modello 3D non sarà completato.")
        self.analysis_overlay.set_status(self.i18n.text(
            "Interruzione in corso: chiusura del processo FreeCAD…"))
        worker.request_abort()

    def _abort_ai_run(self, worker, phase: str) -> None:
        """@brief Abbandona la corsa AI: la finestra torna subito, il risultato si scarta.

        @param worker ExtractWorker o ReconcileWorker in corso.
        @param phase Fase in corso, per il log.
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
        self._on_ai_progress(
            f"INTERRUZIONE chiesta dall'utente durante: {phase}. La richiesta già "
            "inviata al modello non si può annullare ed è già a pagamento: la corsa "
            "viene abbandonata e il suo risultato, quando arriverà, verrà scartato.")
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
        self._show_ai_cost()
        self._hide_analysis_overlay()
        self.btn_extract_ai.setText(self.i18n.text("Estrai parametri con AI"))
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
        if not self._analysis_result_is_current():
            # Corsa abbandonata con «Interrompi»: la finestra e' gia' tornata
            # all'utente da un pezzo. Qui si prende atto che la chiamata a
            # pagamento e' finalmente rientrata, si lascia andare il worker e
            # si riaccendono i comandi che erano spenti perche' girava.
            worker = self.sender()
            self._forget_abandoned_worker(worker)
            self._append_log(
                "La corsa interrotta dall'utente è terminata adesso: il suo "
                "risultato è stato scartato, i comandi tornano disponibili.")
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
        self.btn_extract_ai.setEnabled(True)
        self.btn_extract_ai.setText(self.i18n.text("Estrai parametri con AI"))
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
        """Mostra il consuntivo dell'ultima analisi sotto il pulsante.

        La modale di avanzamento sparisce appena il lavoro finisce: il costo
        deve restare leggibile dopo, senza dover aprire il Log AI. Il testo si
        compone dai valori grezzi, non dalla frase del servizio, cosi' segue
        la lingua scelta nell'interfaccia.
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
        self.lbl_ai_cost.setText(text)
        self.lbl_ai_cost.show()
        # La label sotto il pulsante puo' essere fuori dall'area visibile: il
        # dato va anche nella modale dell'ANALISI, dove l'utente sta gia'
        # guardando. Non in quella della generazione: vedi
        # `_clear_cost_for_generation` (decisione dell'utente, 2026-09-23).
        # Il caso conta davvero: nella corsa automatica `finished`
        # dell'estrazione arriva quando la generazione e' gia' partita.
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
            label under the extraction button (`lbl_ai_cost`), which says
            «ultima analisi» and is untouched here.
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
                "Pausa dopo l'inventario AI: disattivata nelle impostazioni, si prosegue "
                "con i corpi come li ha proposti l'AI.")
            return False
        components = self._photo_components_from_table()
        if not components:
            return False
        if not (self._photo_png or self._photo_overlay_png):
            # Senza immagine la revisione non ha su cosa disegnare: fermare la
            # corsa per una finestra che non si aprirebbe sarebbe una pausa a
            # vuoto (stessa disciplina di `_refresh_edit_review_buttons`).
            self._append_log("Pausa dopo l'inventario AI non offerta: manca la fotografia "
                             "calibrata su cui mostrare i corpi.")
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
            self._append_log(f"Pausa dopo l'inventario AI: revisione rifiutata, si prosegue "
                             f"con i {len(components)} corpi proposti dall'AI.")
            return False
        self._append_log(f"Pausa dopo l'inventario AI: revisione dei {len(components)} corpi "
                         "aperta su richiesta. Nessuna chiamata AI (gratuito).")
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
            self._append_log(f"ATTENZIONE revisione dei componenti non disponibile: {exc}")
        finally:
            self._inventory_pause_active = False
            if overlay_was_visible:
                self.analysis_overlay.show()
                self.analysis_overlay.raise_()
        return True

    def _on_review_requested(self, photo_png, proposals, length_mm, width_mm) -> None:
        """@brief Mostra i corpi rilevati e restituisce al worker quelli da tenere.

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
        """
        worker = self._extract_worker
        kept = list(proposals)
        try:
            from .yolo_review_dialog import YoloReviewDialog
            from ..services.photo_yolo import detector_classes
            self.analysis_overlay.hide()
            dialog = YoloReviewDialog(photo_png, proposals, length_mm, width_mm,
                                      detector_classes(), self)
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            kept = dialog.kept() if accepted else None
            self._append_log(
                f"Revisione dei corpi rilevati: {len(kept)} tenute su {len(proposals)}."
                if kept is not None else "Revisione dei corpi rilevati annullata.")
        except Exception as exc:  # noqa: BLE001 - un errore qui non deve appendere il worker
            self._append_log(f"ATTENZIONE revisione dei corpi non disponibile: {exc}")
        finally:
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
                    detector=getattr(worker, 'photo_detector', 'yolo'))
                self.analysis_overlay.show()
                self.analysis_overlay.raise_()

    def _store_reviewed_proposals(self, kept, photo_png, length_mm, width_mm, *,
                                  detector='yolo') -> None:
        """@brief Write the proposals kept by the in-extraction review to the component folder.

        @param kept Proposal rows kept by the review dialog (may be empty).
        @param photo_png Calibrated photograph the review was made on.
        @param length_mm Board X extent in mm the boxes were calibrated with.
        @param width_mm Board Y extent in mm.
        @param detector Name of the detector the proposals came from.
        @note Marks the extraction as reviewed in every case, including an
            empty selection or a failed write: the cache rows are the
            pre-review set and must never replace what the user decided.
        """
        self._reviewed_this_extraction = True
        if not kept:
            self._append_log("Revisione dei corpi: nessuna proposta tenuta, pacchetto "
                             "photo-review non scritto (le proposte grezze non vengono copiate).")
            return
        path = self._write_review_bundle([dict(p) for p in kept], photo_png, length_mm, width_mm,
                                         detector=detector)
        if path is not None:
            self._append_log(f"Proposte tenute dalla revisione salvate in {path} ({len(kept)} corpi): "
                             "la revisione si puo' riaprire senza chiamate AI.")

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
        self.analysis_overlay.set_progress(done, total, "componenti")

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
        if self._photo_png:
            length = (self.field_cast_body_length.value() if self._is_castellated()
                      else self.field_body_length.value())
            width = (self.field_cast_body_width.value() if self._is_castellated()
                     else self.field_body_width.value())
            try:
                dialog = PhotoReviewDialog(
                    self._photo_png, self._photo_components_from_table(), length, width, self,
                )
            except ValueError as exc:
                QMessageBox.warning(
                    self, self.i18n.text("Revisione foto non disponibile"), str(exc))
                return
            dialog.setWindowTitle(
                self.i18n.tr("photo_review.title", source=self._photo_source)
            )
            dialog.components_applied.connect(self._apply_photo_review)
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
            "I numeri corrispondono alle righe della tabella componenti prima delle modifiche manuali."
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

    def _photo_components_from_table(self) -> list[dict]:
        """Current table geometry plus detection metadata attached to each row."""
        length = (self.field_cast_body_length.value() if self._is_castellated()
                  else self.field_body_length.value())
        width = (self.field_cast_body_width.value() if self._is_castellated()
                 else self.field_body_width.value())
        transform = PhotoTransform(1, 1, length, width)
        result = []
        for row, component in enumerate(self._components_from_table()):
            cell = self.table_components.item(row, 0)
            entry = deepcopy(cell.data(Qt.ItemDataRole.UserRole) or {}) if cell else {}
            entry.update(
                name=component.name, kind=component.kind,
                x_mm=component.x, y_mm=component.y,
                length_mm=component.length, width_mm=component.width,
                height_mm=component.height, rotation_deg=component.rotation,
                color_hex=component.color_hex, marking=component.marking,
            )
            if "_box" in entry:
                entry["_box"] = transform.normalized_box(entry)
            result.append(entry)
        return result

    def _apply_photo_review(self, components: list[dict]) -> None:
        self._set_photo_components(components)
        if self._photo_png:
            from ..services.model_choices import save_choices
            try:
                path = save_choices(self._docs_dir().parent, self._photo_png, components)
                self._append_log(f"Scelte e candidati KiCad salvati: {path}")
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, "Scelte KiCad non salvate", str(exc))
        self._invalidate_generated_mesh()
        self._set_3d_status(self.i18n.text(
            "Componenti corretti dalla foto. Modello 3D da rigenerare: "
            "premi \"Rigenera modello 3D\" per aggiornare STEP e anteprima."
        ), STATUS_INFO)
        self._append_log(f"Revisione fotografica applicata: {len(components)} componenti; 3D da rigenerare.")

    def _invalidate_generated_mesh(self) -> None:
        # Running CAD workers still describe the previous input revision.
        self._model_revision += 1
        self._last_mesh_path = None
        # Il solido che si stava per accettare non corrisponde piu' ai dati.
        self._set_accept_models_ready(False)
        self.preview_3d.clear_preview()
        self._set_3d_status(
            self.i18n.text("Dati modificati. Modello 3D da rigenerare."), STATUS_INFO)

    def _cad_result_is_current(self) -> bool:
        worker = self.sender()
        return worker is None or worker._model_revision == self._model_revision

    def _set_suggested_name(self, name) -> None:
        """@brief Mostra accanto al campo il nome proposto dall'estrazione.

        @param name Nome proposto; vuoto o uguale a quello in uso nasconde la riga.
        @note La proposta non tocca nulla: il nome in uso resta quello scelto
            dall'utente finche' non preme il pulsante.
        """
        proposed = " ".join(str(name or "").split())
        self._suggested_name = "" if proposed == self._component_name() else proposed
        if self._suggested_name:
            self.lbl_name_suggestion.setText(
                f"L'analisi propone: {self._suggested_name}")
        self.widget_name_suggestion.setVisible(bool(self._suggested_name))

    def _apply_suggested_name(self) -> None:
        """@brief Adotta il nome proposto, portandosi dietro i documenti.

        @note La rinomina puo' essere rifiutata (conflitto di documenti): in
            quel caso il progetto resta quello corrente e la proposta rimane
            visibile, perche' non e' stata applicata.
        """
        if not self._suggested_name:
            return
        # Il nome di un altro progetto non si prende (utente, 2026-09-24):
        # la rinomina sposterebbe i documenti dentro una cartella che ne ha
        # gia' di suoi. Si rifiuta e lo si dice; la proposta resta visibile.
        from .project_dialogs import name_problem
        problem = name_problem(self._library_root(), self._suggested_name,
                               self._component_name())
        if problem:
            message = self.i18n.text("Nome proposto non applicato: {problem}").format(
                problem=self.i18n.text(problem))
            self.lbl_name_suggestion.setText(message)
            self._append_log(message)
            return
        applied = self._migrate_docs_to_new_name(self._suggested_name)
        self.field_name.setText(applied)
        self._set_suggested_name(self._suggested_name)

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
        try:
            (self._library_root() / name / DOCS_SUBDIR).mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            QMessageBox.warning(self, self.i18n.text("Nuovo progetto"), str(exc))
            return
        self._set_suggested_name("")
        self.field_name.setText(name)
        self._append_log(f"Nuovo progetto: {self._project_dir()}")

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
        self._set_suggested_name("")
        self.field_name.setText(dialog.selected_name())
        self._append_log(f"Progetto aperto: {self._project_dir()}")

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
        applied = self._migrate_docs_to_new_name(dialog.project_name())
        self.field_name.setText(applied)
        self._set_suggested_name("")

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
            f'Rinomina automatica: il progetto {old_name} diventa {new_name}; '
            f'{len(files)} documenti spostati nella nuova cartella.')

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
                self._append_log(f"Rinomina: {name}/ non spostata del tutto ({exc}).")
                continue
            if kept:
                self._append_log(
                    f"Rinomina: {kept} file di {name}/ esistevano gia' nella nuova cartella "
                    f"e sono rimasti in {source}.")

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
            self._append_log("Risultato di un'analisi interrotta dall'utente: scartato, "
                             "i campi restano come sono.")
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
            self._on_extract_error(f'Impossibile applicare i dati estratti: {exc}')
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
            self._refresh_annotations_table()
            self.field_body_offset_x.setValue(0)
            self.field_body_offset_y.setValue(0)
            self.field_pin_length_3d.setValue(3.0)
        self._acquisition_completed = True
        self._set_acquisition_tabs_enabled(True)
        self._invalidate_generated_mesh()
        self._photo_png = getattr(params, "photo_png", None)
        self._photo_overlay_png = params.photo_overlay_png
        self._photo_source = params.photo_source
        self._refresh_edit_review_buttons()
        self._last_extracted = params
        # Il nome lo decide chi lavora, non l'estrazione. Il nome proposto
        # resta visibile accanto al campo e si applica con un clic -- che e'
        # anche il momento in cui i documenti lo seguono. Riscriverlo da soli
        # significava spostare docs/ a ogni analisi e lasciare in area di
        # lavoro una cartella per ogni nome mai proposto.
        self._set_suggested_name(params.name)
        if params.name and not self._component_name():
            # Nessun nome scelto: non c'e' niente da rispettare e la proposta
            # e' meglio di "NUOVO_COMPONENTE".
            params.name = self._migrate_docs_to_new_name(params.name)
            self.field_name.setText(params.name)
            self._set_suggested_name("")
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
                self._append_log(f"Scelte KiCad precedenti non ripristinate: {exc}")
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
            except (KeyError, TypeError, ValueError) as exc:
                self._append_log(f"ATTENZIONE geometria package ignorata: {exc}")

        # Le due disposizioni riempiono gruppi di campi diversi; tutto il
        # resto (corpo 3D, pinout, colori, fillet, riepilogo) e' comune.
        if params.is_castellated():
            self._apply_extracted_castellated(params)
        else:
            self._set_photo_components(params.components_3d or [])
            self.group_castellated.setChecked(False)
            self.field_rows.setValue(params.rows)
            self.field_cols.setValue(params.cols)
            self.field_pitch.setValue(params.pitch_mm)
            self.field_row_spacing.setValue(params.row_spacing_mm)
            self.field_pad.setValue(params.pad_diameter_mm)
            self.field_drill.setValue(params.drill_mm)
            self.field_populated_pins.setText(
                ",".join(str(p) for p in sorted(params.populated_pins))
                if params.populated_pins else ""
            )

        self._extracted_pcb_thickness = params.pcb_thickness_mm
        if params.pcb_thickness_mm:
            self.field_cast_pcb.setValue(params.pcb_thickness_mm)
        if params.components_3d and params.pcb_thickness_mm is None:
            self._append_log("Spessore PCB non quotato: stima 1.6 mm per la scheda THT; verificare con una misura diretta.")

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
                f"Posizione dei pin letta dal disegno ({len(self.pin_map)} pin): "
                "la numerazione non segue l'ordine colonna per colonna."
            )

        if params.body_color_hex:
            self.field_body_color.setText(params.body_color_hex)
        if params.text_color_hex:
            self.field_text_color.setText(params.text_color_hex)

        if params.edge_fillets:
            for f in params.edge_fillets:
                ann = EdgeFilletAnnotation(edge=f["edge"], radius_mm=float(f["radius_mm"]))
                try:
                    ann.validate()
                except ValueError:
                    continue
                self.fillet_annotations.append(ann)
            self._refresh_annotations_table()

        fillet_note = (
            self.i18n.tr(
                "extraction.fillet_note", count=len(params.edge_fillets))
            if params.edge_fillets else ""
        )
        if self._auto_after_extraction:
            self._auto_generation_params = params
            self._on_ai_progress(f'Estrazione completata. Confidenza: {params.confidence}. {params.notes or ""}{fillet_note}')
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

    def _apply_extracted_castellated(self, params: ExtractedParams) -> None:
        """Riempie il gruppo "Modulo castellated" con i lati estratti.

        Le quote del corpo arrivano dagli stessi campi body_* della matrice
        (li' descrivono l'ingombro del case, qui il contorno del modulo), e
        le dimensioni dei pad dal primo lato che le riporta: il datasheet di
        norma da' un solo land pattern valido per tutti i contatti.
        """
        self.group_castellated.setChecked(True)
        # Distanza fra le file dichiarata dall'estrazione: e' la quota da cui
        # il generatore ricava dove stanno i fori passanti rispetto al bordo,
        # (larghezza - distanza) / 2. Senza, per i contatti a foro passante
        # restano solo una stima per difetto e una riga di log che lo dice.
        # Si azzera a ogni estrazione: una quota di un'altra scheda non vale.
        spacing = getattr(params, "row_spacing_mm", None)
        self._declared_row_spacing = (
            float(spacing) if isinstance(spacing, (int, float)) and not isinstance(spacing, bool)
            and math.isfinite(float(spacing)) and float(spacing) > 0 else None)

        if params.body_length_mm:
            self.field_cast_body_length.setValue(params.body_length_mm)
        if params.body_width_mm:
            self.field_cast_body_width.setValue(params.body_width_mm)
        for side in params.castellated_sides or ():
            if side.get("pad_length_mm"):
                self.field_cast_pad_length.setValue(float(side["pad_length_mm"]))
            if side.get("pad_width_mm"):
                self.field_cast_pad_width.setValue(float(side["pad_width_mm"]))

        self.table_sides.setRowCount(0)
        names = []
        pinout = []
        for side in params.castellated_sides or ():
            self._add_side_row(side)
            label = EDGE_LABELS.get(side["edge"], side["edge"])
            if side.get("pin_names"):
                names.append(f"{label}: {', '.join(side['pin_names'])}")
                pinout.extend(_side_pinout(side))

        # Tabella pinout: se il modello non l'ha compilata a parte, la si
        # ricava dalle sigle serigrafate accanto ai contatti di ogni lato.
        if pinout:
            self.pinout = pinout

        self._set_photo_components(params.components_3d or [])
        if not self.castellated_components:
            self._append_log(
                "ATTENZIONE: il modello non ha restituito alcun componente dalla foto "
                "(components_3d vuoto): nel 3D andra' un blocco unico. Rilancia "
                "l'estrazione con la foto del lato superiore selezionata, oppure "
                "aggiungi i componenti a mano nel tab Modello 3D."
            )
        if self.castellated_components:
            self._append_log(
                f"{len(self.castellated_components)} componenti letti dalla foto "
                "(stime visive): compariranno nel modello 3D, modificabili nel "
                "tab Modello 3D."
            )

        # Il tipo di contatto e' scritto accanto a ogni lato, con la sua
        # provenienza: dichiarato dall'estrazione o predefinito perche' assente.
        # Chi vede «predefinito» sa che nessuno lo ha letto sulla scheda.
        styles = self._table_contact_styles()
        described = []
        for index, s in enumerate(params.castellated_sides or ()):
            style = styles[index] if index < len(styles) else default_contact_style()
            origin = "" if s.get("contact_style") in contact_styles() else ", predefinito"
            described.append(
                f"lato {EDGE_LABELS.get(s['edge'], s['edge'])} "
                f"{s['count']} contatti a {s['pitch_mm']}mm "
                f"({CONTACT_STYLE_LABELS.get(style, style)}{origin})")
        self._append_log("Contatti del modulo: " + ", ".join(described))
        for line in names:
            self._append_log(f"Sigle serigrafate {line}")

    def _on_extract_error(self, message: str) -> None:
        """@brief Riporta la finestra in quiete e dice perche' l'analisi e' fallita.

        @param message Testo dell'errore, come arriva dal worker.
        @note L'errore di una corsa abbandonata con «Interrompi» non apre
            nessuna finestra: l'utente ha gia' deciso di lasciarla perdere, e
            un avviso di guasto su cio' che ha interrotto lui sarebbe un
            rimprovero per una sua scelta. Resta nel log, dove serve.
        """
        if not self._analysis_result_is_current():
            self._append_log(f"Errore di un'analisi interrotta dall'utente: {message}")
            return
        self._auto_generation_params = None
        self._auto_after_extraction = False
        self._hide_analysis_overlay()
        # Un rilancio fallito lascia validi i parametri e le proposte salvate:
        # il pulsante torna disponibile per riprovare, o resta spento se non
        # c'era nulla.
        self._refresh_reopen_review_button()
        self._on_ai_progress(f"Estrazione fallita: {message}")
        QMessageBox.warning(self, self.i18n.text("Estrazione fallita"), message)

    # ── Tab 2: Footprint 2D ────────────────────────────────────────

    def _build_footprint_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        form_group = QGroupBox("Parametri footprint (header THT a matrice)")
        form = QFormLayout(form_group)
        general_group = QGroupBox("Dati del componente")
        general_form = QFormLayout(general_group)

        self.field_name_mirror = QLineEdit(self.field_name.text())
        self.field_name_mirror.setReadOnly(True)
        self.field_name_mirror.setToolTip("Modificabile nel tab \"Progetto\"")
        general_form.addRow("Nome (da tab Progetto):", self.field_name_mirror)

        self.field_description = QLineEdit()
        self.field_description.setPlaceholderText("(auto-generata se vuota)")
        self.field_tags = QLineEdit("pin header THT")

        self.field_rows = QSpinBox()
        self.field_rows.setRange(1, 40)
        self.field_rows.setValue(2)

        self.field_cols = QSpinBox()
        self.field_cols.setRange(1, 40)
        self.field_cols.setValue(4)

        self.field_pitch = QDoubleSpinBox()
        self.field_pitch.setRange(0.5, 20.0)
        self.field_pitch.setSingleStep(0.01)
        self.field_pitch.setDecimals(2)
        self.field_pitch.setSuffix(" mm")
        self.field_pitch.setValue(2.54)
        self.field_pitch.setToolTip("Passo tra pin adiacenti sulla stessa riga")

        self.field_row_spacing = QDoubleSpinBox()
        self.field_row_spacing.setRange(0.5, 50.0)
        self.field_row_spacing.setSingleStep(0.01)
        self.field_row_spacing.setDecimals(2)
        self.field_row_spacing.setSuffix(" mm")
        self.field_row_spacing.setValue(2.54)
        self.field_row_spacing.setToolTip(
            "Distanza perpendicolare tra le due file di pin. Nei moduli DIP-style "
            "(es. convertitori DC/DC) e' quasi sempre molto maggiore del passo."
        )

        self.field_populated_pins = QLineEdit()
        self.field_populated_pins.setPlaceholderText(
            "(vuoto = tutte le posizioni popolate) es. 1,7,8,9,10,16"
        )

        self.field_pad = QDoubleSpinBox()
        self.field_pad.setRange(0.2, 10.0)
        self.field_pad.setSingleStep(0.05)
        self.field_pad.setDecimals(2)
        self.field_pad.setSuffix(" mm")
        self.field_pad.setValue(1.7)

        self.field_drill = QDoubleSpinBox()
        self.field_drill.setRange(0.1, 8.0)
        self.field_drill.setSingleStep(0.05)
        self.field_drill.setDecimals(2)
        self.field_drill.setSuffix(" mm")
        self.field_drill.setValue(1.0)

        general_form.addRow("Descrizione:", self.field_description)
        general_form.addRow("Tag:", self.field_tags)
        form.addRow("Righe:", self.field_rows)
        form.addRow("Colonne:", self.field_cols)
        form.addRow("Passo (pitch, tra pin nella riga):", self.field_pitch)
        form.addRow("Distanza tra righe:", self.field_row_spacing)
        form.addRow("Pin popolati:", self.field_populated_pins)
        form.addRow("Diametro pad:", self.field_pad)
        form.addRow("Drill:", self.field_drill)

        self.group_matrix = form_group
        castellated_group = self._build_castellated_group()
        body_group = self._build_3d_group()
        self.footprint_subtabs = QTabWidget()
        for title, group in (
            ("Generale", general_group),
            ("Matrice THT", form_group),
            ("Modulo castellated", castellated_group),
            ("Parametri 3D", body_group),
        ):
            page = QWidget()
            page_layout = QVBoxLayout(page)
            page_layout.addWidget(group)
            page_layout.addStretch()
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(page)
            self.footprint_subtabs.addTab(scroll, title)
        outer.addWidget(self.footprint_subtabs)
        return tab

    # ── Modulo castellated ──────────────────────────────────────────

    def _build_castellated_group(self) -> QGroupBox:
        """Gruppo alternativo alla matrice, per i moduli con semi-fori sul bordo.

        Quando e' attivo la matrice righe x colonne non viene usata: i due
        modelli geometrici si escludono, perche' in un modulo castellated
        ogni lato ha il suo numero di contatti e il suo passo -- ed e'
        esattamente quello che una griglia a passo unico non sa descrivere.
        """
        group = QGroupBox("Modulo castellated (piazzole a mezzo foro sul bordo)")
        group.setCheckable(True)
        group.setChecked(False)
        group.toggled.connect(self._on_castellated_toggled)
        self.group_castellated = group

        layout = QVBoxLayout(group)
        form = QFormLayout()

        def _mm(value: float, maximum: float = 300.0, minimum: float = 0.0) -> QDoubleSpinBox:
            spin = QDoubleSpinBox()
            spin.setRange(minimum, maximum)
            spin.setDecimals(2)
            spin.setSingleStep(0.05)
            spin.setSuffix(" mm")
            spin.setValue(value)
            return spin

        self.field_cast_body_length = _mm(42.8)
        self.field_cast_body_length.setToolTip(
            "Lunghezza del corpo del modulo, asse X (lati \"top\"/\"bottom\")."
        )
        self.field_cast_body_width = _mm(15.2)
        self.field_cast_body_width.setToolTip(
            "Larghezza del corpo del modulo, asse Y (lati \"left\"/\"right\")."
        )
        self.field_cast_pad_length = _mm(3.6, maximum=20.0)
        self.field_cast_pad_length.setToolTip(
            "Lunghezza della piazzola perpendicolarmente al bordo."
        )
        self.field_cast_pad_width = _mm(1.8, maximum=20.0)
        self.field_cast_pad_width.setToolTip(
            "Larghezza della piazzola lungo il bordo (poco piu' del semi-foro)."
        )
        self.field_cast_overhang = _mm(1.4, maximum=5.0)
        self.field_cast_overhang.setToolTip(
            "Quanto la piazzola sporge oltre il bordo del modulo: serve a lasciare "
            "ispezionabile il menisco di saldatura."
        )

        form.addRow("Lunghezza corpo:", self.field_cast_body_length)
        form.addRow("Larghezza corpo:", self.field_cast_body_width)
        form.addRow("Lunghezza pad:", self.field_cast_pad_length)
        form.addRow("Larghezza pad:", self.field_cast_pad_width)
        self.field_cast_hole = _mm(1.7, maximum=10.0)
        self.field_cast_hole.setToolTip(
            "Diametro del semi-foro metallizzato sul bordo: serve al modello 3D, "
            "dove i semi-fori vengono tagliati nel PCB del modulo."
        )
        self.field_cast_pcb = _mm(1.0, maximum=10.0)
        self.field_cast_pcb.setToolTip(
            "Spessore del PCB del modulo. Nel 3D il modulo e' un PCB sottile con "
            "sopra un blocco che rappresenta i componenti montati: l'altezza "
            "totale si imposta in \"Modello 3D\"."
        )
        self.field_cast_inset = _mm(1.0, maximum=20.0)
        self.field_cast_inset.setToolTip(
            "Quanto il blocco dei componenti rientra dal bordo del PCB: sul bordo "
            "ci sono i semi-fori, li' non e' montato nulla."
        )
        # Ogni contatto di una scheda di sviluppo ha due appigli: il mezzo foro
        # sul bordo per la rifusione SMD e un foro intero poco piu' dentro, in
        # cui si salda uno strip. Generando solo il primo il bordo restava
        # tutto aperto e i pin non si potevano saldare.
        self.field_cast_thru_drill = _mm(1.0, maximum=10.0)
        self.field_cast_thru_drill.setToolTip(
            "Diametro del foro passante chiuso accanto al semi-foro, quello in cui "
            "si salda lo strip. A zero il modulo ha solo i semi-fori sul bordo."
        )
        self.field_cast_thru_pad = _mm(1.7, maximum=10.0)
        self.field_cast_thru_pad.setToolTip(
            "Diametro della piazzola attorno al foro passante: deve essere maggiore "
            "del foro, altrimenti non resta rame su cui saldare."
        )
        self.field_cast_thru_offset = _mm(0.0, maximum=20.0)
        self.field_cast_thru_offset.setToolTip(
            "Distanza del centro del foro passante dal bordo del corpo. A zero la "
            "calcola il programma, tenendo il foro abbastanza lontano dal semi-foro "
            "perche' i due non si fondano in un'unica cavita' aperta: con un semi-foro "
            "piu' stretto il minimo scende da solo. Un valore scritto a mano viene "
            "rispettato, ma rifiutato se i due fori si toccherebbero."
        )
        self.field_cast_hole_inset = _mm(0.0, maximum=10.0)
        self.field_cast_hole_inset.setToolTip(
            "Quanto il centro del semi-foro sta DENTRO il bordo: e' cio' che decide "
            "quanto la mezzaluna resta aperta. A zero vale la convenzione (0,76 raggi); "
            "a zero vero il semi-foro diventa una mezzaluna esatta."
        )
        for field in (self.field_cast_body_length, self.field_cast_body_width,
                      self.field_cast_hole, self.field_cast_pcb, self.field_cast_inset,
                      self.field_cast_thru_drill, self.field_cast_thru_pad,
                      self.field_cast_thru_offset, self.field_cast_hole_inset):
            field.valueChanged.connect(self._on_sides_changed)

        form.addRow("Sporgenza pad dal bordo:", self.field_cast_overhang)
        form.addRow("Diametro semi-foro (3D):", self.field_cast_hole)
        form.addRow("Foro passante (0 = nessuno):", self.field_cast_thru_drill)
        form.addRow("Piazzola foro passante:", self.field_cast_thru_pad)
        form.addRow("Foro passante dal bordo (0 = auto):", self.field_cast_thru_offset)
        form.addRow("Semi-foro dentro il bordo (0 = auto):", self.field_cast_hole_inset)
        form.addRow("Spessore PCB (3D):", self.field_cast_pcb)
        form.addRow("Rientro componenti (3D):", self.field_cast_inset)
        layout.addLayout(form)

        side_note = QLabel(
            "Contatti lato per lato (ogni lato ha il suo passo). Il primo pin di "
            "ogni lato si conta dall'alto in basso sui lati verticali, da sinistra "
            "a destra su quelli orizzontali."
        )
        side_note.setWordWrap(True)
        layout.addWidget(side_note)

        self.table_sides = QTableWidget(0, 7)
        self.table_sides.setHorizontalHeaderLabels(
            ["Lato", "N. contatti", "Passo", "Offset", "Primo pin", "Numerazione inversa",
             "Tipo contatto"]
        )
        self.table_sides.horizontalHeaderItem(self._SIDE_STYLE_COLUMN).setToolTip(
            self.i18n.text(CONTACT_STYLE_TOOLTIP))
        self.table_sides.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.table_sides.setMinimumHeight(150)
        self.table_sides.setMaximumHeight(200)
        layout.addWidget(self.table_sides)

        buttons = QHBoxLayout()
        btn_add = QPushButton("Aggiungi lato")
        btn_add.clicked.connect(self._add_side_row_by_user)
        btn_remove = QPushButton("Rimuovi lato selezionato")
        btn_remove.clicked.connect(self._remove_side_row)
        buttons.addWidget(btn_add)
        buttons.addWidget(btn_remove)
        buttons.addStretch()
        layout.addLayout(buttons)

        return group

    def _on_castellated_toggled(self, checked: bool) -> None:
        """La matrice e i moduli castellated sono alternativi, mai insieme."""
        if hasattr(self, "group_matrix"):
            self.group_matrix.setEnabled(not checked)
        if hasattr(self, "footprint_subtabs"):
            self._sync_footprint_subtabs()
            self.footprint_subtabs.setCurrentIndex(2 if checked else 1)
        if checked and self.table_sides.rowCount() == 0:
            # Due lati opposti sono la configurazione di gran lunga piu' comune.
            self._add_side_row({"edge": "left", "count": 3, "pitch_mm": 2.54, "first_pin": 1})
            self._add_side_row({"edge": "right", "count": 3, "pitch_mm": 2.54, "first_pin": 4})
        self._sync_3d_fields_for_castellated(checked)
        self._sync_contact_style_bar()
        if hasattr(self, "group_components"):
            self.group_components.setVisible(self._advanced_user() and (
                checked or self._selected_family() == "aggregate_tht"))
        self._update_summary()
        self._draw_preview()

    def _sync_footprint_subtabs(self) -> None:
        """Only expose the geometry editor used by the current layout."""
        castellated = self._is_castellated()
        self.footprint_subtabs.setTabVisible(1, not castellated)
        self.footprint_subtabs.setTabVisible(2, castellated)

    def _sync_3d_fields_for_castellated(self, castellated: bool) -> None:
        """Nei moduli castellated il corpo 3D viene dal modulo, non a mano.

        Lunghezza, larghezza e offset descrivono il PCB del modulo, che e'
        gia' definito nel gruppo qui sopra: lasciarli modificabili
        permetterebbe di generare un 3D scollegato dal footprint. La
        lunghezza dei pin non serve: non ci sono pin che sporgono.
        """
        # I contatti dei moduli sono quasi sempre dorati (ENIG): se il colore
        # pin e' ancora quello di default dei piedini stagnati, si passa all'oro.
        if castellated and self.field_pin_color.text().strip().lower() == "#b0b0b0":
            self.field_pin_color.setText("#c9a227")
        for field in (self.field_body_length, self.field_body_width,
                      self.field_body_offset_x, self.field_body_offset_y,
                      self.field_pin_length_3d):
            field.setEnabled(not castellated)
            field.setToolTip(
                "Per i moduli castellated questo valore viene dal gruppo "
                "\"Modulo castellated\"." if castellated else ""
            )

    #: Colonna della tabella lati che porta il tipo di contatto.
    _SIDE_STYLE_COLUMN = 6

    def _add_side_row(self, side: dict | None = None) -> None:
        """Append one side row, filled from an extracted/serialised side dict.

        Programmatic: it does NOT invalidate the generated 3D, because its
        callers (a new extraction, the castellated toggle) already describe a
        new input revision. Edits made afterwards in the row do invalidate it.
        """
        side = side or {}
        row = self.table_sides.rowCount()
        self.table_sides.insertRow(row)

        combo = QComboBox()
        for edge in CASTELLATED_EDGES:
            combo.addItem(EDGE_LABELS.get(edge, edge), edge)
        index = combo.findData(side.get("edge", "left"))
        combo.setCurrentIndex(max(index, 0))
        combo.currentIndexChanged.connect(self._on_side_edited)
        self.table_sides.setCellWidget(row, 0, combo)

        count = QSpinBox()
        count.setRange(1, 100)
        count.setValue(int(side.get("count", 2)))
        count.valueChanged.connect(self._on_side_edited)
        self.table_sides.setCellWidget(row, 1, count)

        for column, key, default, minimum in (
            (2, "pitch_mm", 2.54, 0.0), (3, "offset_mm", 0.0, -200.0),
        ):
            spin = QDoubleSpinBox()
            spin.setRange(minimum, 200.0)
            spin.setDecimals(2)
            spin.setSingleStep(0.01)
            spin.setSuffix(" mm")
            spin.setValue(float(side.get(key, default)))
            spin.valueChanged.connect(self._on_side_edited)
            self.table_sides.setCellWidget(row, column, spin)

        first_pin = QSpinBox()
        first_pin.setRange(1, 999)
        first_pin.setValue(int(side.get("first_pin", 1)))
        first_pin.valueChanged.connect(self._on_side_edited)
        self.table_sides.setCellWidget(row, 4, first_pin)

        reverse = QCheckBox()
        reverse.setChecked(bool(side.get("reverse")))
        reverse.toggled.connect(self._on_side_edited)
        self.table_sides.setCellWidget(row, 5, reverse)

        style = self._contact_style_combo(self._declared_contact_style(side))
        style.currentIndexChanged.connect(self._on_contact_style_edited)
        self.table_sides.setCellWidget(row, self._SIDE_STYLE_COLUMN, style)

        self._on_sides_changed()

    def _add_side_row_by_user(self) -> None:
        """«Aggiungi lato»: a new side changes the module, the old 3D is stale."""
        self._add_side_row()
        self._invalidate_generated_mesh()

    def _declared_contact_style(self, side: dict) -> str:
        """Contact style declared by a side dict, or the default when absent.

        An absent key is today's behaviour, not a question: the default is
        kept silently. An unknown value is NOT guessed into one of the three:
        it falls back to the default and the log says so, so that the user
        knows the combo shows a default and not what the data said.
        """
        declared = side.get("contact_style")
        if declared is None:
            return default_contact_style()
        if declared in contact_styles():
            return declared
        self._append_log(
            f"ATTENZIONE: tipo di contatto sconosciuto {declared!r} sul lato "
            f"{EDGE_LABELS.get(side.get('edge'), side.get('edge'))}: resta "
            f"«{CONTACT_STYLE_LABELS.get(default_contact_style(), default_contact_style())}», "
            "correggilo nella colonna «Tipo contatto».")
        return default_contact_style()

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
        """@brief Every contact-style combo of the window: the 3D-tab one and one per side row."""
        combos = []
        combo_3d = getattr(self, "combo_contact_style_3d", None)
        if combo_3d is not None:
            combos.append(combo_3d)
        table = getattr(self, "table_sides", None)
        if table is not None:
            for row in range(table.rowCount()):
                combo = table.cellWidget(row, self._SIDE_STYLE_COLUMN)
                if isinstance(combo, QComboBox):
                    combos.append(combo)
        return combos

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
                          else CONTACT_STYLE_LABELS.get(value, value))
                combo.setItemText(index, self.i18n.text(source))

    def _remove_side_row(self) -> None:
        row = self.table_sides.currentRow()
        if row >= 0:
            self.table_sides.removeRow(row)
            self._on_side_edited()

    def _on_sides_changed(self) -> None:
        self._update_summary()
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
        """Contact style of each table row, in row order."""
        styles = []
        for row in range(self.table_sides.rowCount()):
            combo = self.table_sides.cellWidget(row, self._SIDE_STYLE_COLUMN)
            styles.append(combo.currentData() if combo is not None else default_contact_style())
        return styles

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
        chosen = {s for s in self._table_contact_styles() if s != default_contact_style()}
        if chosen:
            self._append_log(
                "ATTENZIONE: il generatore installato non conosce ancora il tipo di "
                "contatto: la scelta viene ignorata e footprint e 3D restano "
                f"«{CONTACT_STYLE_LABELS.get(default_contact_style(), default_contact_style())}».")

    def _sides_from_table(self) -> list[CastellatedSide]:
        """@brief The castellated sides as the table describes them now.

        @return One `CastellatedSide` per row, in row order.
        @note `contact_style` e' passato solo se la classe installata lo
            accetta (stessa introspezione di `phase_kwargs`/`cancel_kwargs`):
            GUI e generatore evolvono separatamente e un TypeError qui
            bloccherebbe ogni generazione per un campo accessorio.
        """
        style_supported = self._contact_style_supported()
        styles = self._table_contact_styles()
        sides = []
        for row in range(self.table_sides.rowCount()):
            extra = {"contact_style": styles[row]} if style_supported else {}
            sides.append(CastellatedSide(
                edge=self.table_sides.cellWidget(row, 0).currentData(),
                count=self.table_sides.cellWidget(row, 1).value(),
                pitch=self.table_sides.cellWidget(row, 2).value(),
                offset=self.table_sides.cellWidget(row, 3).value(),
                first_pin=self.table_sides.cellWidget(row, 4).value(),
                reverse=self.table_sides.cellWidget(row, 5).isChecked(),
                **extra,
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
                     first_pin=s.first_pin, reverse=s.reverse, contact_style=style)
                for s, style in zip(self._sides_from_table(), self._table_contact_styles())]

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
        rows = [(self.table_sides.cellWidget(r, 0).currentData(), style)
                for r, style in enumerate(self._table_contact_styles())]
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
        target = styles[0] if len(unique) == 1 else None
        blocked = combo.blockSignals(True)
        combo.setCurrentIndex(max(combo.findData(target), 0) if target is not None
                              else combo.count() - 1)
        combo.blockSignals(blocked)

    def _on_contact_style_all_changed(self, index: int) -> None:
        """@brief Apply the 3D-tab contact style to every side of the table.

        @param index Chosen combo index; the «mixed» entry is informative only.
        @note E' la via della modalita' base: la tabella lati sta nel tab
            Footprint 2D, nascosto se `advanced_user` e' spento, mentre i
            difetti dei contatti si scoprono guardando il 3D. Il cambio passa
            dalla tabella, quindi segue la stessa strada di ogni modifica ai
            lati: anteprima invalidata, nessuna chiamata AI.
        """
        style = self.combo_contact_style_3d.itemData(index)
        if style is None:
            return
        for row in range(self.table_sides.rowCount()):
            combo = self.table_sides.cellWidget(row, self._SIDE_STYLE_COLUMN)
            blocked = combo.blockSignals(True)
            combo.setCurrentIndex(max(combo.findData(style), 0))
            combo.blockSignals(blocked)
        self._on_contact_style_edited()

    def _current_castellated_params(self) -> CastellatedModuleParams:
        return CastellatedModuleParams(
            name=self.field_name.text().strip(),
            description=self.field_description.text().strip(),
            tags=self.field_tags.text().strip(),
            body_length=self.field_cast_body_length.value(),
            body_width=self.field_cast_body_width.value(),
            pad_length=self.field_cast_pad_length.value(),
            pad_width=self.field_cast_pad_width.value(),
            pad_overhang=self.field_cast_overhang.value(),
            hole_diameter=self.field_cast_hole.value(),
            thru_hole_drill=self.field_cast_thru_drill.value(),
            thru_pad_diameter=self.field_cast_thru_pad.value(),
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

    def _silk_pin_labels(self) -> bool:
        """@brief Tell whether pin names are engraved on the 3D silkscreen."""
        return self._config.get('silk_pin_labels', False) is True

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

        @note Non riaccende cio' che l'utente ha spento a mano: `_user_3d_choice`
            registra solo le sue interazioni, perche' `QGroupBox.clicked` scatta
            sul clic e non sulle `setChecked()` del programma. Una nuova
            estrazione descrive i dati, non revoca una decisione presa da chi
            lavora: riaccendere il gruppo qui significherebbe generare (e far
            aspettare) un modello 3D che era stato escluso apposta.
        @note Senza FreeCAD non si accende affatto. Lo spegnimento iniziale di
            `_build_3d_group` e' programmatico, quindi NON lascia traccia in
            `_user_3d_choice` e non frenava questa riaccensione: l'estrazione
            ribaltava la regola dichiarata la' («Senza FreeCAD resta spento,
            cosi' la generazione non fallisce») e ogni generazione finiva con un
            `cad3d_error`. Si guarda la disponibilita' ricordata, non
            `isChecked()`, perche' un gruppo spento puo' esserlo per la scelta
            dell'utente o per l'assenza dello strumento: sono due ragioni
            diverse e solo la seconda e' definitiva.
        """
        if self._user_3d_choice is False or self._freecad_available is False:
            return
        self.group_3d.setChecked(True)

    def _on_3d_group_clicked(self, checked: bool) -> None:
        """@brief Registra la scelta esplicita dell'utente sul gruppo «Modello 3D».

        @param checked Stato del gruppo dopo il clic.
        """
        self._user_3d_choice = bool(checked)

    def _build_3d_group(self) -> QGroupBox:
        group = QGroupBox("Modello 3D (opzionale, richiede FreeCAD installato)")
        group.setCheckable(True)
        # Attivo di default se FreeCAD c'e': chi lo ha installato vuole il 3D,
        # e scoprire a generazione finita che era spento e' solo tempo perso.
        # Senza FreeCAD resta spento, cosi' la generazione non fallisce.
        try:
            find_freecadcmd()
            freecad_available = True
        except Cad3DError:
            freecad_available = False
        # Ricordato perche' l'estrazione deve poterlo consultare senza rifare
        # la ricerca su disco: vedi `_enable_3d_group_from_extraction`.
        self._freecad_available = freecad_available
        group.setChecked(freecad_available)
        group.setToolTip(
            "FreeCAD trovato: il modello 3D viene generato insieme al footprint."
            if freecad_available else
            "freecadcmd non trovato: installa FreeCAD o imposta FREECAD_CMD_PATH."
        )
        self.group_3d = group
        # `clicked` distingue l'interazione umana dalle accensioni del
        # programma: `toggled` scatterebbe anche per le nostre setChecked().
        group.clicked.connect(self._on_3d_group_clicked)
        form3d = QFormLayout(group)

        self.field_body_length = QDoubleSpinBox()
        self.field_body_length.setRange(0.5, 300.0)
        self.field_body_length.setDecimals(2)
        self.field_body_length.setSuffix(" mm")
        self.field_body_length.setValue(10.0)
        self.field_body_length.setToolTip("Lunghezza corpo, asse X (stessa direzione del pitch)")

        self.field_body_width = QDoubleSpinBox()
        self.field_body_width.setRange(0.5, 300.0)
        self.field_body_width.setDecimals(2)
        self.field_body_width.setSuffix(" mm")
        self.field_body_width.setValue(10.0)
        self.field_body_width.setToolTip("Larghezza corpo, asse Y (stessa direzione della distanza tra righe)")

        self.field_body_height = QDoubleSpinBox()
        self.field_body_height.setRange(0.5, 100.0)
        self.field_body_height.setDecimals(2)
        self.field_body_height.setSuffix(" mm")
        self.field_body_height.setValue(5.0)

        self.field_body_offset_x = QDoubleSpinBox()
        self.field_body_offset_x.setRange(-150.0, 150.0)
        self.field_body_offset_x.setDecimals(2)
        self.field_body_offset_x.setSuffix(" mm")
        self.field_body_offset_x.setValue(0.0)
        self.field_body_offset_x.setToolTip("Posizione del bordo corpo lungo X rispetto al pin 1 (x=0)")

        self.field_body_offset_y = QDoubleSpinBox()
        self.field_body_offset_y.setRange(-150.0, 150.0)
        self.field_body_offset_y.setDecimals(2)
        self.field_body_offset_y.setSuffix(" mm")
        self.field_body_offset_y.setValue(0.0)
        self.field_body_offset_y.setToolTip("Posizione del bordo corpo lungo Y rispetto alla prima riga di pin (y=0)")

        self.field_pin_length_3d = QDoubleSpinBox()
        self.field_pin_length_3d.setRange(0.1, 20.0)
        self.field_pin_length_3d.setDecimals(2)
        self.field_pin_length_3d.setSuffix(" mm")
        self.field_pin_length_3d.setValue(3.0)
        self.field_pin_length_3d.setToolTip("Lunghezza pin che sporge sotto il corpo, verso il PCB")

        form3d.addRow("Lunghezza corpo:", self.field_body_length)
        form3d.addRow("Larghezza corpo:", self.field_body_width)
        form3d.addRow("Altezza corpo:", self.field_body_height)
        form3d.addRow("Offset X (da pin 1):", self.field_body_offset_x)
        form3d.addRow("Offset Y (da riga pin):", self.field_body_offset_y)
        self.field_body_color = QLineEdit("#1a1a1a")
        self.field_body_color.setToolTip("Colore del case, formato #rrggbb (estratto dalla foto)")

        self.field_pin_color = QLineEdit("#b0b0b0")
        self.field_pin_color.setToolTip("Colore dei pin, formato #rrggbb")

        form3d.addRow("Lunghezza pin (3D):", self.field_pin_length_3d)
        form3d.addRow("Colore corpo:", self.field_body_color)
        form3d.addRow("Colore pin:", self.field_pin_color)

        return group

    # ── Tab 3: Genera ──────────────────────────────────────────────

    def _build_generate_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        self.label_summary = QLabel()
        self.label_summary.setWordWrap(True)
        self.label_summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.generate_info_dialog = QDialog(self)
        self.generate_info_dialog.setWindowTitle("Riepilogo della generazione")
        self.generate_info_dialog.setModal(True)
        summary_layout = QVBoxLayout(self.generate_info_dialog)
        summary_layout.addWidget(self.label_summary)
        summary_close = QPushButton("Chiudi")
        summary_close.clicked.connect(self.generate_info_dialog.accept)
        summary_layout.addWidget(summary_close, alignment=Qt.AlignmentFlag.AlignRight)
        self.generate_info_dialog.resize(680, 240)

        btn_row = QHBoxLayout()
        self.btn_generate = QPushButton("Genera footprint")
        self.btn_generate.setObjectName("primary")
        self.btn_generate.clicked.connect(self._generate)
        btn_row.addWidget(self.btn_generate)
        btn_row.addStretch()
        self.btn_generate_info = QPushButton()
        self.btn_generate_info.setIcon(QIcon(str(Path(__file__).resolve().parent / "assets" / "info.svg")))
        self.btn_generate_info.setIconSize(QSize(22, 22))
        self.btn_generate_info.setFixedSize(36, 32)
        self.btn_generate_info.setStyleSheet("QPushButton { padding: 4px; }")
        self.btn_generate_info.setToolTip("Riepilogo del componente e dei parametri di generazione")
        self.btn_generate_info.setAccessibleName("Riepilogo della generazione")
        self.btn_generate_info.clicked.connect(self._show_generate_info)
        btn_row.addWidget(self.btn_generate_info)
        outer.addLayout(btn_row)

        outer.addWidget(QLabel("Log:"))
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        outer.addWidget(self.log_text)

        return tab

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
        review_layout.addWidget(review_note)
        self.btn_test_kicad = QPushButton("Prova in KiCad...")
        self.btn_test_kicad.setObjectName("primary")
        self.btn_test_kicad.setMinimumHeight(42)
        self.btn_test_kicad.setToolTip(
            "Apre un progetto temporaneo: non installa e non modifica librerie KiCad")
        self.btn_test_kicad.clicked.connect(self._test_in_kicad)
        review_layout.addWidget(
            self.btn_test_kicad, alignment=Qt.AlignmentFlag.AlignHCenter)
        outer.addWidget(review_group)

        install_group = QGroupBox("Installazione nella libreria")
        install_layout = QVBoxLayout(install_group)
        install_note = QLabel(
            "La libreria di progetto è portabile ed è la scelta consigliata. "
            "La libreria globale rende il footprint disponibile in tutti i progetti "
            "di questo computer.")
        install_note.setWordWrap(True)
        install_layout.addWidget(install_note)
        install_buttons = QHBoxLayout()
        install_buttons.setSpacing(14)
        self.btn_export = QPushButton("Installa nel progetto KiCad...")
        self.btn_export.setToolTip(
            "Crea KiCadGen.pretty e KiCadGen.3dshapes nel progetto selezionato")
        self.btn_export.clicked.connect(self._install_project_library)
        self.btn_install_global = QPushButton("Installa come libreria globale...")
        self.btn_install_global.setToolTip(
            "Installa nella cartella scelta e registra la libreria globale di KiCad")
        self.btn_install_global.clicked.connect(self._install_global_library)
        install_buttons.addStretch()
        install_buttons.addWidget(self.btn_export)
        install_buttons.addWidget(self.btn_install_global)
        install_buttons.addStretch()
        install_layout.addLayout(install_buttons)
        outer.addWidget(install_group)

        footer = QHBoxLayout()
        self.btn_open_output = QPushButton("Apri cartella componente")
        self.btn_open_output.clicked.connect(self._open_project_dir)
        footer.addWidget(self.btn_open_output)
        footer.addStretch()
        self.label_kicad_status = QLabel()
        self.label_kicad_status.setProperty("i18n_skip", True)
        self.label_kicad_status.setWordWrap(True)
        self.label_kicad_status.setStyleSheet(f"color: {YELLOW};")
        footer.addWidget(self.label_kicad_status, 1)
        outer.addLayout(footer)
        outer.addStretch()
        self._refresh_output_actions()
        return tab

    def _show_generate_info(self) -> None:
        self._update_summary()
        self.generate_info_dialog.exec()

    def _open_project_dir(self) -> None:
        if not self._refresh_output_actions():
            self._append_log("Genera prima il footprint del componente corrente.")
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
        try:
            result = install_component_library(
                artifacts, destination, library_name="KiCadGen",
                project_library=project_library, table_path=table_path,
                overwrite=overwrite,
            )
        except (KiCadLibraryError, OSError) as exc:
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

    def _export_component(self) -> None:
        """@brief Preserve the previous handler as a project-install alias."""
        self._install_project_library()

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
            path = self._log_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            pending = getattr(self, "_pending_log_lines", None)
            with open(path, "a", encoding="utf-8") as handle:
                stamp = datetime.now().astimezone().isoformat(timespec="seconds")
                for line in (pending or ()):
                    handle.write(f"[{stamp}] {line}\n")
                if pending:
                    self._pending_log_lines = []
                handle.write(f"[{stamp}] {str(message).rstrip()}\n")
                handle.flush()
        except (OSError, AttributeError) as exc:
            # Startup can emit messages before project widgets exist.
            if hasattr(self, "log_text") and not getattr(self, "_log_write_failed", False):
                self._log_write_failed = True
                self.log_text.insertPlainText(f"Log file non disponibile: {exc}\n")

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
            self._append_log(f"Rinomina annullata: impossibile trasferire il log ({exc}).")
            return False

    def _append_log(self, msg: str) -> None:
        self._write_log_line(msg)
        if hasattr(self, "ai_progress_text") and not re.match(r"^#\d+", msg.strip()):
            self.ai_progress_text.moveCursor(QTextCursor.MoveOperation.End)
            self.ai_progress_text.insertPlainText(msg + "\n")
            self.ai_progress_text.ensureCursorVisible()
        # I tab si costruiscono in ordine: quello "Progetto" puo' gia' emettere
        # messaggi (tipologia componente, modello) quando il log del tab
        # "Genera" non esiste ancora.
        if not hasattr(self, "log_text"):
            return
        self.log_text.moveCursor(QTextCursor.MoveOperation.End)
        self.log_text.insertPlainText(msg + "\n")

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
                raise ValueError(f"\"{token}\" in \"Pin popolati\" non e' un numero valido.")
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
            pcb_thickness_mm=self._extracted_pcb_thickness,
            components=self._components_from_table(),
            populated_board=self._selected_family() == "aggregate_tht",
        )

    def _update_summary(self) -> None:
        if self._is_castellated():
            self._update_summary_castellated()
            return
        try:
            params = self._current_params()
        except ValueError as exc:
            self.label_summary.setText(
                self.i18n.tr("summary.parameter_error", error=exc))
            return
        row_spacing = params.effective_row_spacing()
        populated = (
            self.i18n.tr(
                "summary.populated_pins", count=len(params.populated_pins),
                total=params.rows * params.cols)
            if params.populated_pins else ""
        )
        self.label_summary.setText(self.i18n.tr(
            "summary.pin_header", name=params.name or self.i18n.text('(nome vuoto)'),
            folder=self._project_dir_text(), rows=params.rows, cols=params.cols,
            pitch=params.pitch, row_spacing=row_spacing, pad=params.pad_diameter,
            drill=params.drill, populated=populated))

    def _update_summary_castellated(self) -> None:
        params = self._current_castellated_params()
        sides = ", ".join(
            self.i18n.tr(
                "summary.side", side=self.i18n.text(EDGE_LABELS.get(s.edge, s.edge)),
                count=s.count, pitch=s.pitch)
            for s in params.sides
        ) or self.i18n.text("nessun lato definito")
        try:
            params.validate()
            error = ""
        except ValueError as exc:
            error = self.i18n.tr("summary.parameter_error_line", error=exc)
        self.label_summary.setText(self.i18n.tr(
            "summary.castellated", name=params.name or self.i18n.text('(nome vuoto)'),
            folder=self._project_dir_text(), length=params.body_length,
            width=params.body_width, pad_length=params.pad_length,
            pad_width=params.pad_width, overhang=params.pad_overhang,
            sides=sides, error=error))

    def _automatic_generation_blocked(self, message):
        text = self.i18n.tr("generation.blocked", reason=message)
        self._on_ai_progress('Generazione automatica non avviata: ' + message)
        QMessageBox.warning(
            self, self.i18n.text('Generazione automatica non avviata'), text)

    @staticmethod
    def _validate_output_name(name):
        if (not name or name in ('.', '..') or name != name.rstrip(' .')
                or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)):
            raise ValueError('Il nome del componente non è un nome di file valido.')

    def _validate_automatic_input(self, params):
        self._validate_output_name(params.name or self._component_name())
        if params.package_geometry:
            raise ValueError('Per questa geometria a pad espliciti il generatore 3D non è ancora supportato. '
                             'Il footprint può essere generato manualmente in modalità avanzata.')
        required_dimensions = ('body_length_mm', 'body_width_mm')
        if not params.is_castellated():
            required_dimensions += ('body_height_mm',)
        for field in required_dimensions:
            value = getattr(params, field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f'Manca una dimensione valida del corpo 3D: {field}.')
        dimensions = ((params.body_length_mm, self.field_cast_body_length if params.is_castellated() else self.field_body_length),
                      (params.body_width_mm, self.field_cast_body_width if params.is_castellated() else self.field_body_width))
        if not params.is_castellated():
            dimensions += ((params.body_height_mm, self.field_body_height),)
        if any(not widget.minimum() <= value <= widget.maximum() for value, widget in dimensions):
            raise ValueError('Le dimensioni estratte superano i limiti dell’editor: serve una revisione.')
        if params.is_castellated():
            if not params.castellated_sides:
                raise ValueError('Non sono stati estratti i contatti del modulo castellated.')
            for side in params.castellated_sides:
                if not isinstance(side, dict) or side.get('edge') not in CASTELLATED_EDGES:
                    raise ValueError('Lato del modulo non valido.')
                for field in ('count', 'pitch_mm'):
                    value = side.get(field)
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                        raise ValueError(f'Contatti del modulo incompleti: {field}.')
                if side['count'] != int(side['count']) or side['count'] > 100:
                    raise ValueError('Il numero di contatti estratto non è supportato dall’editor.')
        else:
            for field in ('rows', 'cols', 'pitch_mm', 'row_spacing_mm', 'pad_diameter_mm', 'drill_mm'):
                value = getattr(params, field)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                    raise ValueError(f'Parametro footprint mancante o non valido: {field}.')
            for field, widget in (('rows', self.field_rows), ('cols', self.field_cols),
                                  ('pitch_mm', self.field_pitch), ('row_spacing_mm', self.field_row_spacing),
                                  ('pad_diameter_mm', self.field_pad), ('drill_mm', self.field_drill)):
                value = getattr(params, field)
                if not widget.minimum() <= value <= widget.maximum() or (field in ('rows', 'cols') and value != int(value)):
                    raise ValueError(f'Parametro fuori dai limiti dell’editor: {field}.')

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
            body = self._current_body_params()
            if body is None and self.group_3d.isChecked():
                raise ValueError('I parametri del modello 3D non sono disponibili.')
            if body is not None:
                # QSpinBox limits must not silently replace extracted dimensions.
                dimensions = ((body.length_mm, extracted.body_length_mm),
                              (body.width_mm, extracted.body_width_mm))
                if not extracted.is_castellated():
                    dimensions += ((body.height_mm, extracted.body_height_mm),)
                if any(not math.isclose(actual, expected, abs_tol=.011)
                       for actual, expected in dimensions):
                    raise ValueError('Le dimensioni estratte superano i limiti dell’editor: serve una revisione.')
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
            params.validate()
            self._validate_output_name(params.name)
            body = None if isinstance(params, PackageGeometry) else self._current_body_params()
            if body is not None:
                body.validate()
            # Il corpo si pretende solo se il 3D e' stato chiesto. Un gruppo
            # «Modello 3D» spento e' una scelta, non un dato mancante: la
            # generazione automatica deve comunque scrivere il footprint.
            # Vedi `_current_body_params` e `_start_automatic_generation`.
            if automatic and body is None and self.group_3d.isChecked():
                raise ValueError('Mancano i parametri 3D.')
        except ValueError as exc:
            self._append_log(f"ERRORE: {exc}")
            if automatic:
                self._automatic_generation_blocked(str(exc))
            return

        project_dir = self._project_dir()
        if automatic and not self._confirm_output_overwrite(params, project_dir, body):
            self._on_ai_progress('Generazione annullata: file esistenti conservati.')
            return

        self.btn_generate.setEnabled(False)
        if isinstance(params, PackageGeometry):
            self._append_log(
                f"Generazione {params.name}: package {params.family}, "
                f"{len(params.pins)} pad espliciti"
            )
        elif isinstance(params, CastellatedModuleParams):
            self._append_log(
                f"Generazione {params.name}: modulo castellated "
                f"{params.body_length}x{params.body_width}mm, "
                + ", ".join(
                    f"{EDGE_LABELS.get(s.edge, s.edge)} {s.count}x{s.pitch}mm"
                    for s in params.sides
                )
            )
        else:
            self._append_log(
                f"Generazione {params.name}: {params.rows}x{params.cols}, "
                f"pitch={params.pitch}mm, row_spacing={params.effective_row_spacing()}mm, "
                f"pad={params.pad_diameter}mm, drill={params.drill}mm"
            )

        if body is None and (self.fillet_annotations or self.face_text_annotations):
            self._append_log(
                "ATTENZIONE: annotazioni 3D presenti ma \"Modello 3D\" non e' attivo "
                "nel tab Footprint 2D -- verranno ignorate."
            )

        self._last_params = params
        self._worker = GenerateWorker(
            params, project_dir, body,
            fillets=list(self.fillet_annotations), face_texts=list(self.face_text_annotations),
        )
        self._worker._model_revision = self._model_revision
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
        try:
            self._worker.start()
        except Exception as exc:
            self._on_error(str(exc))
            self._on_generation_finished()

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
        self.btn_generate.setEnabled(True)
        if self._manual_cad_overlay:
            # La modale della generazione manuale la chiude chi l'ha aperta:
            # la corsa automatica ha la sua chiusura piu' sotto, insieme a
            # schede, menu e consuntivo, che qui non sono stati toccati.
            self._manual_cad_overlay = False
            self._hide_analysis_overlay()
        if not self._automatic_generation_active:
            return
        self._automatic_generation_active = False
        self._hide_analysis_overlay()
        self.menuBar().setEnabled(self._generation_menu_enabled)
        self.btn_extract_ai.setEnabled(True)
        if self._automatic_3d_done:
            self.tabs.setCurrentIndex(4)
        elif self._automatic_footprint_done:
            self.tabs.setCurrentIndex(3)
        expected_3d = getattr(self, "_automatic_3d_expected", True)
        complete = self._automatic_footprint_done and (self._automatic_3d_done or not expected_3d)
        if aborted:
            # Nessun avviso di «generazione incompleta»: l'incompletezza e'
            # esattamente cio' che l'utente ha chiesto, e restituirgliela come
            # un guasto sarebbe rimproverargli la sua decisione. Il consuntivo
            # va comunque scritto, perche' dice che cosa e' rimasto sul disco.
            self._on_ai_progress(
                'Generazione interrotta dall’utente. '
                + ('Il footprint scritto prima dell’interruzione resta sul disco; '
                   if self._automatic_footprint_done else 'Nessun footprint scritto; ')
                + 'il modello 3D non è stato completato.')
        elif self._automatic_generation_errors or not complete:
            details = ('\n'.join(self._automatic_generation_errors)
                       or self.i18n.text('La generazione non ha prodotto entrambi i risultati.'))
            prefix = (self.i18n.text('Footprint generato, modello 3D non completato.') + '\n'
                      if self._automatic_footprint_done and expected_3d else '')
            QMessageBox.warning(
                self, self.i18n.text('Generazione incompleta'), prefix + details)
        elif expected_3d:
            self._on_ai_progress('Footprint e modello 3D generati. Verifica le anteprime prima dell’utilizzo.')
        else:
            # Consuntivo fedele: il modello 3D non e' stato chiesto, quindi non
            # va ne' promesso ne' lamentato.
            self._on_ai_progress('Footprint generato; modello 3D non richiesto. '
                                 'Verifica l’anteprima prima dell’utilizzo.')

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
        self._append_log(f"ERRORE: {message}")
        if self._automatic_generation_active:
            self._automatic_generation_errors.append(message)

    def _on_cad3d_generated(self, path: str) -> None:
        if not self._cad_result_is_current():
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
        if not self._cad_result_is_current():
            return
        if CAD_ABORT_MESSAGE in message:
            self._append_log(f"INTERROTTO 3D: {message}")
            return
        self._append_log(f"ERRORE 3D: {message}")
        if self._automatic_generation_active:
            self._automatic_generation_errors.append(message)

    def _on_cad3d_warning(self, message: str) -> None:
        """Annotazione non applicata: il modello c'e' comunque, ma va detto."""
        if not self._cad_result_is_current():
            return
        self._append_log(f"ATTENZIONE 3D: {message}")

    # ── Tab 4: Anteprima ───────────────────────────────────────────

    def _build_preview_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        header = QHBoxLayout()
        header.addWidget(QLabel("Anteprima 2D (silkscreen + pad, scala fissa, con quote):"))
        header.addStretch()
        btn_pinout = QPushButton("Tabella pinout...")
        btn_pinout.clicked.connect(self._show_pinout_dialog)
        header.addWidget(btn_pinout)
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

        @return The tab container (preview/settings splitter).
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
        outer.setContentsMargins(10, 10, 10, 10)
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
        self.btn_fit_3d = self._view_tool_button(
            "view-fit.svg", "Inquadra tutto il modello senza cambiare l'angolo di vista.")
        toolbar.addWidget(self.btn_fit_3d)
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
        # Copie dei pulsanti della scheda Progetto: da qui si vede il solido
        # e si capisce se mancano corpi, quindi da qui si deve poter tornare
        # alle proposte annotate e ripartire (revisione gratuita; rilancio a
        # pagamento, dichiarato -- anche dall'icona, che porta una moneta).
        # Stesso stato delle copie in Progetto.
        self.btn_reopen_review_3d = self._mirror_project_action(
            self.btn_reopen_review, self._reopen_detector_review, toolbar,
            "wf-review.svg")
        self.btn_relaunch_inventory_3d = self._mirror_project_action(
            self.btn_relaunch_inventory, self._relaunch_inventory, toolbar,
            "wf-relaunch-ai.svg")
        # Anche la modifica della revisione va specchiata qui: e' guardando il
        # solido che si scopre un corpo sbagliato, e tornare alla scheda
        # Progetto per correggerlo era lavoro in piu' senza motivo.
        self.btn_edit_component_review_3d = self._mirror_project_action(
            self.btn_edit_component_review, self._show_photo_comparison, toolbar,
            "wf-edit-review.svg")

        # Il tipo di contatto va correggibile da qui anche in modalita' base:
        # e' guardando il solido che si vede un mezzo foro dove la scheda ha un
        # foro passante, e la tabella dei lati sta in un tab nascosto a chi non
        # ha scelto la modalita' avanzata. Subito prima di «Rigenera»
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
        # Solo informativa: si sceglie un tipo per tutti i lati, oppure si usa
        # la tabella lati per differenziarli; «diverso» non e' una scelta.
        self.combo_contact_style_3d.model().item(
            self.combo_contact_style_3d.count() - 1).setEnabled(False)
        self._set_translated_tooltip(
            self.combo_contact_style_3d,
            CONTACT_STYLE_TOOLTIP + "\nVale per tutti i lati; per un tipo diverso "
            "lato per lato usa la colonna «Tipo contatto» della tabella lati "
            "(tab Footprint 2D, modalita' avanzata).")
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
        self.preview_3d.show_pin_numbers = self.chk_pin_numbers.isChecked()
        self.preview_3d.setMinimumHeight(120)
        outer.addWidget(self.preview_3d, 1)
        # Le viste standard si scelgono dal cubo dentro l'anteprima, che ha
        # preso il posto della riga «Vista: Iso, Sopra, …».
        self.btn_fit_3d.clicked.connect(self.preview_3d.fit_view)
        self._apply_view_cube_labels()

        # Riga di stato del 3D: visibile SOLO per errori e avvisi (scelta
        # dell'utente). Gli esiti normali restano nel testo -- e nel log -- ma
        # non occupano lo spazio sotto il modello. Vedi `_set_3d_status`.
        self.label_3d_status = QLabel()
        self.label_3d_status.setWordWrap(True)
        outer.addWidget(self.label_3d_status)
        self._set_3d_status(self.i18n.text(
            "Attiva \"Modello 3D\" nel tab \"Footprint 2D\" per vedere l'anteprima qui."),
            STATUS_WARNING)

        settings = QWidget()
        settings_layout = QVBoxLayout(settings)
        settings_layout.addWidget(self._build_annotations_group())
        settings_layout.addWidget(self._build_components_group())

        scroll = QScrollArea()
        scroll.setWidget(settings)
        scroll.setWidgetResizable(True)
        self.model_3d_settings_scroll = scroll
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(tab)
        splitter.addWidget(scroll)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([600, 200])
        splitter.setCollapsible(0, False)
        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(0)
        # La barra sta sopra il divisore, fuori da scorrimento e splitter:
        # resta sempre raggiungibile, qualunque altezza abbia il pannello
        # delle impostazioni, ed e' la prima cosa che l'occhio incontra.
        container_layout.addWidget(self.model_3d_action_bar)
        container_layout.addWidget(splitter, 1)
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
        save_config(self._config)
        self.preview_3d.set_pin_numbers(checked)
        # Ridisegna la vista corrente: mesh generata se c'e', altrimenti
        # l'approssimazione dai parametri.
        if self._last_mesh_path and Path(self._last_mesh_path).is_file():
            try:
                self.preview_3d.render_mesh(self._last_mesh_path)
                return
            except Exception as exc:  # noqa: BLE001 - si ricade sull'anteprima parametrica
                self._append_log(f"Anteprima mesh non disponibile: {exc}")
        self._draw_preview_3d()

    def _open_3dshapes_dir(self) -> None:
        shapes_dir = self._project_dir() / f"{self._component_name()}.3dshapes"
        shapes_dir.mkdir(parents=True, exist_ok=True)
        webbrowser.open(shapes_dir.as_uri())

    def _refine_photo_terminals(self) -> None:
        self._start_terminal_refinement(only_leads=False)

    def _refine_photo_leads(self) -> None:
        self._start_terminal_refinement(only_leads=True)

    def _start_terminal_refinement(self, only_leads=False) -> None:
        if self._terminal_worker is not None and self._terminal_worker.isRunning():
            return
        if self._extract_worker is not None and self._extract_worker.isRunning():
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
        worker.finished.connect(lambda: self.btn_terminal_detail.setEnabled(True))
        worker.finished.connect(lambda: self.btn_lead_refinement.setEnabled(True))
        worker.start()

    def _on_terminal_detail_error(self, message):
        self._append_log(message)
        self._set_3d_status(message, STATUS_INFO)

    def _on_terminal_detail_done(self, components):
        worker = self._terminal_worker
        if (worker._model_revision != self._model_revision
                or worker._photo_snapshot != self._photo_png
                or worker.components != self._photo_components_from_table()):
            self._append_log("Dettaglio terminali scartato: i dati sono cambiati durante l'analisi.")
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

    def _regenerate_3d(self) -> None:
        """Rigenera solo lo STEP con le annotazioni correnti (il footprint resta com'e')."""
        if self._background_work_running() or self._automatic_generation_active:
            return

        body = self._current_body_params()
        if body is None:
            QMessageBox.information(
                self, self.i18n.text("Modello 3D non attivo"),
                self.i18n.text(
                    "Attiva il gruppo \"Modello 3D\" nel tab \"Footprint 2D\" "
                    "e compila le dimensioni del corpo prima di generare.")
            )
            return

        try:
            params = (
                self._current_castellated_params() if self._is_castellated()
                else self._current_params()
            )
            params.validate()
            body.validate()
        except ValueError as exc:
            self._set_3d_status(
                self.i18n.tr("status.parameter_error", error=exc), STATUS_ERROR)
            self._append_log(f"ERRORE 3D: {exc}")
            return

        self._set_regen_busy(True)
        self._set_3d_status(
            self.i18n.text("Generazione STEP in corso tramite FreeCAD..."), STATUS_INFO)
        self._append_log(
            f"Rigenerazione 3D {params.name}: {len(self.fillet_annotations)} fillet, "
            f"{len(self.face_text_annotations)} testi incisi"
        )

        self._cad3d_worker = Cad3DWorker(
            params, body, self._project_dir(),
            list(self.fillet_annotations), list(self.face_text_annotations),
        )
        self._cad3d_worker._model_revision = self._model_revision
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
        self._start_log_session("Rigenerazione modello 3D")
        # Questa e' un'operazione a se', e non chiama il modello AI: lasciare
        # il consuntivo dell'ultima analisi dentro la modale lo farebbe
        # leggere come il costo di cio' che sta accadendo adesso. Resta sulla
        # label sotto il pulsante, che dice esplicitamente "ultima analisi".
        # Vale per OGNI generazione, anche quella automatica: vedi
        # `_clear_cost_for_generation`.
        self._clear_cost_for_generation()
        self.analysis_overlay.start('Generazione modello 3D',
                                    'Avvio di FreeCAD…', abortable=True)
        self._cad3d_worker.start()

    def _on_regen_3d_progress(self, message: str) -> None:
        """Porta sulla modale i messaggi che arrivano durante la generazione."""
        self.analysis_overlay.set_status(message)

    def _on_regen_3d_finished(self) -> None:
        """@brief Chiude la modale e riabilita il comando, in ogni esito.

        @note Vale anche per l'interruzione: `finished` scatta comunque, ed e'
            qui che si chiude la richiesta di fermarsi (`_abort_pending`).
        """
        self._abort_pending = False
        self._hide_analysis_overlay()
        self._set_regen_busy(False)

    def _on_regen_3d_done(self, path: str) -> None:
        if not self._cad_result_is_current():
            return
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
            self._append_log(f"Abbinamenti accettati NON salvati: {exc}")
            self._set_3d_status(self.i18n.tr(
                "status.accepted_models_failed", error=exc), STATUS_ERROR)
            return
        self._append_log(
            f"Abbinamenti accettati salvati in {store.path}: {result.created} voci nuove, "
            f"{result.confirmed} conferme di voci esistenti, {len(result.skipped)} esclusi.")
        for name, reason in result.skipped:
            self._append_log(f"  escluso {name}: {reason}")
        self._set_3d_status(self.i18n.tr(
            "status.accepted_models_saved", stored=result.stored,
            created=result.created, confirmed=result.confirmed), STATUS_INFO)

    def _show_accepted_models(self) -> None:
        """@brief Open the archive list, where wrong entries can be deleted."""
        dialog = AcceptedModelsDialog(parent=self, i18n=self.i18n)
        self.i18n.retranslate_widgets(dialog)
        dialog.exec()

    def _show_generated_mesh(self, step_path: Path) -> None:
        """Mostra in anteprima la mesh del solido appena generato (fillet e testo inclusi)."""
        report_path = step_path.parent.parent / 'kicad-model-candidates.json'
        if report_path.is_file():
            try:
                records = json.loads(report_path.read_text(encoding='utf-8'))['components']
                if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
                    raise ValueError('Elenco componenti non valido nel report KiCad')
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
                    cell = self.table_components.item(index, 0)
                    metadata = deepcopy(cell.data(Qt.ItemDataRole.UserRole) or {})
                    match = record.get('model_match')
                    if match is not None and not isinstance(match, dict):
                        raise ValueError('Report modello KiCad non valido')
                    metadata['model_match'] = match
                    for key in ('model_path', 'model_rotation_deg', 'render_3d', 'rotation_deg'):
                        if key in record:
                            metadata[key] = deepcopy(record[key])
                    cell.setData(Qt.ItemDataRole.UserRole, metadata)
                self._append_log(f'Candidati KiCad e decisioni: {report_path}')
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                self._append_log(f'Report candidati non caricato: {exc}')
        mesh_path = step_path.with_suffix("").with_suffix(".preview.json")
        if not mesh_path.is_file():
            mesh_path = step_path.parent / f"{step_path.stem}.preview.json"
        if not mesh_path.is_file():
            return
        try:
            self.preview_3d.render_mesh(mesh_path)
            self._last_mesh_path = mesh_path
            self._set_3d_status(
                f"{self.label_3d_status.text()}\n"
                + self.i18n.text(
                    "Anteprima aggiornata con la geometria reale esportata (fillet e testo inclusi)."), STATUS_INFO)
        except Exception as exc:  # noqa: BLE001 - anteprima non critica
            self._append_log(f"Anteprima mesh non disponibile: {exc}")

    def _on_regen_3d_error(self, message: str) -> None:
        """@brief Scrive nel log e sulla barra 3D perche' lo STEP non c'e'.

        @param message Testo dell'esito, come arriva dal worker.
        @note Come `_on_cad3d_error`: l'interruzione chiesta dall'utente non e'
            un guasto, quindi non viene annunciata come tale ne' nel log ne'
            sulla barra di stato.
        """
        if not self._cad_result_is_current():
            return
        if CAD_ABORT_MESSAGE in message:
            self._append_log(f"INTERROTTO 3D: {message}")
            self._set_3d_status(self.i18n.text(
                "Generazione interrotta dall'utente: il modello 3D non è stato "
                "aggiornato."), STATUS_INFO)
            return
        self._append_log(f"ERRORE 3D: {message}")
        self._set_3d_status(self.i18n.tr(
            "status.step_failed", error=message.splitlines()[0]), STATUS_ERROR)

    # ── Componenti del modulo (3D) ─────────────────────────────────

    _COMPONENT_COLUMNS = ("Nome", "Tipo", "X", "Y", "Lung.", "Larg.", "Alt.", "Rot.", "Colore", "Marcatura")

    def _build_components_group(self) -> QGroupBox:
        """Tabella dei componenti montati sul modulo, letti dalla foto.

        Le stime dell'estrazione sono visive e ogni tanto sbagliano (un diodo
        letto come "varistore" e disegnato come una scatola): qui si corregge
        il singolo componente -- tipo, posizione, ingombro, colore, marcatura
        -- invece di rilanciare l'estrazione e sperare. Vale solo per i moduli
        castellated: e' nascosta negli altri casi.
        """
        group = QGroupBox(
            "Componenti sul modulo (letti dalla foto, modificabili) -- "
            "origine al centro del modulo, X a destra, Y in basso come nel footprint; "
            "Lung. = asse lungo del componente, Rot. = come sta sul modulo"
        )
        self.group_components = group
        layout = QVBoxLayout(group)

        self.table_components = QTableWidget(0, len(self._COMPONENT_COLUMNS))
        self.table_components.setHorizontalHeaderLabels(list(self._COMPONENT_COLUMNS))
        header = self.table_components.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(9, QHeaderView.ResizeMode.Stretch)
        self.table_components.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_components.setMinimumHeight(160)
        layout.addWidget(self.table_components)

        buttons = QHBoxLayout()
        btn_add = QPushButton("Aggiungi componente")
        btn_add.clicked.connect(lambda: self._add_component_row())
        btn_remove = QPushButton("Rimuovi componente selezionato")
        btn_remove.clicked.connect(self._remove_component_row)
        buttons.addWidget(btn_add)
        buttons.addWidget(btn_remove)
        self.btn_lead_refinement = QPushButton("Affina reofori / saldature...")
        self.btn_lead_refinement.setToolTip(
            "Passaggio AI facoltativo: conserva corpi e conteggio, separa solo i reofori distinguibili.")
        self.btn_lead_refinement.clicked.connect(self._refine_photo_leads)
        buttons.addWidget(self.btn_lead_refinement)
        # Intentionally hidden, not removed: see project MEMORY.md.
        self.btn_lead_refinement.hide()
        buttons.addStretch()
        layout.addLayout(buttons)

        group.setVisible(False)
        return group

    def _add_component_row(self, component: ComponentBox | None = None,
                           photo_metadata: dict | None = None) -> None:
        component = component or ComponentBox(x=0.0, y=0.0, length=2.0, width=1.2, height=0.6)
        table = self.table_components
        row = table.rowCount()
        table.insertRow(row)
        metadata = QTableWidgetItem()
        row_metadata = deepcopy(photo_metadata or {})
        for key, value in (component.match_evidence or {}).items():
            row_metadata.setdefault(key, deepcopy(value))
        row_metadata.update(pin_count=component.pin_count, pins_by_side=component.pins_by_side,
                            pin_count_confidence=component.pin_count_confidence,
                            pin_count_source=component.pin_count_source,
                            _detected_kind=component.detected_kind,
                            terminal_geometry=component.terminal_geometry,
                            package=component.package, mating=component.mating,
                            bbox_basis=component.bbox_basis,
                            model_selection=deepcopy(component.model_selection),
                            model_match=deepcopy(component.model_match),
                            package_evidence=component.package_evidence)
        # None leaves native-model axis inference enabled; zero is an explicit
        # local CAD override, independent of the photo rectangle's rotation.
        if component.model_rotation_deg is None:
            row_metadata.pop("model_rotation_deg", None)
        else:
            row_metadata["model_rotation_deg"] = component.model_rotation_deg
        if component.model_z_offset_mm:
            row_metadata["model_z_offset_mm"] = component.model_z_offset_mm
        else:
            row_metadata.pop("model_z_offset_mm", None)
        # Scelte del menu contestuale della vista 3D: assenti significa
        # "decide l'automatismo", quindi la chiave si toglie invece di
        # scriverci un valore neutro che sembrerebbe una scelta fatta.
        if isinstance(component.render_3d_override, bool):
            row_metadata["render_3d_override"] = component.render_3d_override
        else:
            row_metadata.pop("render_3d_override", None)
        if component.model_rotation_delta_deg:
            row_metadata["model_rotation_delta_deg"] = component.model_rotation_delta_deg
        else:
            row_metadata.pop("model_rotation_delta_deg", None)
        metadata.setData(Qt.ItemDataRole.UserRole, row_metadata)
        table.setItem(row, 0, metadata)

        name = QLineEdit(component.name)
        table.setCellWidget(row, 0, name)

        kind = QComboBox()
        for key in COMPONENT_KINDS:
            kind.addItem(key, key)
        if kind.findData(component.kind) < 0:
            kind.addItem(component.kind, component.kind)
        kind.setCurrentIndex(max(kind.findData(component.kind), 0))
        table.setCellWidget(row, 1, kind)

        for column, value, minimum in (
            (2, component.x, -300.0), (3, component.y, -300.0),
            (4, component.length, 0.05), (5, component.width, 0.05), (6, component.height, 0.05),
        ):
            spin = QDoubleSpinBox()
            # Keep precision and small packages from manual photo corrections.
            spin.setDecimals(6)
            spin.setRange(min(minimum, float(value)), max(300.0, float(value)))
            spin.setSingleStep(0.1)
            spin.setValue(float(value))
            table.setCellWidget(row, column, spin)

        rotation = QComboBox()
        for degrees in (0, 90, 180, 270):
            rotation.addItem(f"{degrees}\u00b0", float(degrees))
        angle = float(component.rotation) % 360
        if rotation.findData(angle) < 0:
            rotation.addItem(f"{angle:g}\u00b0", angle)
        rotation.setCurrentIndex(max(rotation.findData(float(component.rotation) % 360), 0))
        table.setCellWidget(row, 7, rotation)
        color = QLineEdit(component.color_hex)
        color.setMaxLength(7)
        table.setCellWidget(row, 8, color)
        marking = QLineEdit(component.marking)
        table.setCellWidget(row, 9, marking)
        for column in range(10):
            widget = table.cellWidget(row, column)
            if isinstance(widget, QDoubleSpinBox):
                widget.valueChanged.connect(
                    lambda _value, item=metadata, geometry=column in (4, 5):
                    self._component_edited(item, geometry))
            elif isinstance(widget, QComboBox):
                widget.currentIndexChanged.connect(
                    lambda _value, item=metadata: self._component_edited(item, match=True))
            elif isinstance(widget, QLineEdit):
                widget.textChanged.connect(self._invalidate_generated_mesh)
        if not getattr(self, "_refreshing_component_rows", False):
            self._invalidate_generated_mesh()

    def _component_edited(self, item, geometry=False, match=False) -> None:
        """@brief Invalidate CAD and evidence tied to an edited photo rectangle."""
        if geometry:
            metadata = deepcopy(item.data(Qt.ItemDataRole.UserRole) or {})
            metadata["bbox_basis"] = "unknown"
            metadata.pop("package_evidence", None)
            metadata.pop("terminal_geometry", None)
            metadata.pop("model_match", None)
            metadata.pop("model_selection", None)
            item.setData(Qt.ItemDataRole.UserRole, metadata)
        elif match:
            metadata = deepcopy(item.data(Qt.ItemDataRole.UserRole) or {})
            metadata.pop("model_match", None)
            metadata.pop("model_selection", None)
            item.setData(Qt.ItemDataRole.UserRole, metadata)
        self._invalidate_generated_mesh()

    def _remove_component_row(self) -> None:
        row = self.table_components.currentRow()
        if row >= 0:
            self.table_components.removeRow(row)
            self._invalidate_generated_mesh()

    def _set_photo_components(self, components: list[dict]) -> None:
        """Replace component rows, retaining unknown metadata beside each row."""
        self.castellated_components = [
            ComponentBox(
                x=c["x_mm"], y=c["y_mm"], length=c["length_mm"], width=c["width_mm"],
                height=c.get("height_mm", 1.0), name=c.get("name", "Componente"),
                color_hex=c.get("color_hex") or "#303030", kind=c.get("kind", "generic"),
                marking=c.get("marking", ""), rotation=float(c.get("rotation_deg", 0.0)),
                model_rotation_deg=c.get("model_rotation_deg"),
                model_selection=deepcopy(c.get("model_selection")),
                model_match=deepcopy(c.get("model_match")),
                match_evidence={key: deepcopy(c[key]) for key in (
                    'presence_confidence', 'class_confidence', 'confidence',
                    '_yolo_review_only', '_yolo_origin', 'terminal_count',
                    'terminal_layout', 'terminal_count_basis', '_global_inventory_status', 'class',
                    # Il riferimento asimmetrico deve arrivare fino al
                    # costruttore: e' li' che si confronta col baricentro del
                    # modello nativo per stabilire il verso.
                    'landmark_feature', 'landmark_side', 'landmark_confidence') if key in c},
                model_z_offset_mm=float(c.get("model_z_offset_mm") or 0.0),
                pin_count=c.get("pin_count"), pins_by_side=c.get("pins_by_side"),
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
            ) for c in components
        ]
        self._refresh_components_table(components)

    def _refresh_components_table(self, photo_components: list[dict] | None = None) -> None:
        """Riscrive la tabella dall'elenco appena estratto."""
        if not hasattr(self, "table_components"):
            return
        self._refreshing_component_rows = True
        try:
            self.table_components.setRowCount(0)
            for index, component in enumerate(self.castellated_components):
                metadata = photo_components[index] if photo_components is not None else None
                self._add_component_row(component, metadata)
        finally:
            self._refreshing_component_rows = False

    #: Correzioni di orientamento offerte dal menu contestuale. Solo multipli
    #: di un quarto di giro: i modelli della libreria KiCad seguono la
    #: convenzione del footprint, quindi uno mal orientato lo e' sempre di
    #: 90, 180 o 270 gradi, mai di un angolo qualsiasi.
    MENU_ROTATIONS_DEG = (90, 180, 270)

    def _show_component_menu(self, row: int, position) -> None:
        """@brief Menu del tasto destro su un componente della vista 3D.

        La vista 3D serve solo a indicare quale riga: le voci agiscono sui
        metadati della tabella componenti, che resta la fonte di verita'.
        Ogni scelta viene scritta nel JSON delle scelte e ha effetto dalla
        rigenerazione FreeCAD successiva, senza ripassare dal modello AI.
        """
        table = getattr(self, "table_components", None)
        if table is None or not 0 <= row < table.rowCount():
            return
        cell = table.item(row, 0)
        metadata = deepcopy(cell.data(Qt.ItemDataRole.UserRole) or {}) if cell else {}
        label = table.cellWidget(row, 0)
        name = (label.text().strip() if label is not None else "") or "?"
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
        cell = self.table_components.item(row, 0)
        metadata = cell.data(Qt.ItemDataRole.UserRole) if cell else None
        current = float((metadata or {}).get("model_rotation_delta_deg") or 0.0)
        total = (current + float(degrees)) % 360
        self._update_component_metadata(
            row, {"model_rotation_delta_deg": total or None})

    def _update_component_metadata(self, row: int, changes: dict) -> None:
        """@brief Scrivi i metadati di una riga e propaga la modifica.

        Un valore None toglie la chiave: significa "torna all'automatismo",
        che non e' la stessa cosa di una scelta esplicita dal valore neutro.
        """
        table = getattr(self, "table_components", None)
        cell = table.item(row, 0) if table is not None else None
        if cell is None:
            return
        metadata = deepcopy(cell.data(Qt.ItemDataRole.UserRole) or {})
        for key, value in changes.items():
            if value is None:
                metadata.pop(key, None)
            else:
                metadata[key] = value
        cell.setData(Qt.ItemDataRole.UserRole, metadata)
        name = table.cellWidget(row, 0)
        self._append_log(
            f"Componente '{name.text().strip() if name else row}': "
            + ", ".join(f"{k}={metadata.get(k, 'automatico')}" for k in changes))
        # Stesso percorso della revisione fotografica: salva il JSON delle
        # scelte e marca il 3D da rigenerare. La rigenerazione e' FreeCAD e
        # basta -- nessuna chiamata al modello AI.
        self._apply_photo_review(self._photo_components_from_table())

    def _components_from_table(self) -> list[ComponentBox]:
        """Componenti come sono ORA nella tabella (con le correzioni a mano)."""
        if not hasattr(self, "table_components"):
            return list(self.castellated_components)
        table = self.table_components
        components = []
        for row in range(table.rowCount()):
            cell = table.item(row, 0)
            metadata = deepcopy(cell.data(Qt.ItemDataRole.UserRole) or {}) if cell else {}
            components.append(ComponentBox(
                name=table.cellWidget(row, 0).text().strip() or "Componente",
                kind=table.cellWidget(row, 1).currentData() or "generic",
                x=table.cellWidget(row, 2).value(),
                y=table.cellWidget(row, 3).value(),
                length=table.cellWidget(row, 4).value(),
                width=table.cellWidget(row, 5).value(),
                height=table.cellWidget(row, 6).value(),
                rotation=float(table.cellWidget(row, 7).currentData() or 0.0),
                model_rotation_deg=metadata.get("model_rotation_deg"),
                model_selection=deepcopy(metadata.get("model_selection")),
                model_match=deepcopy(metadata.get("model_match")),
                match_evidence={key: deepcopy(metadata[key]) for key in (
                    'presence_confidence', 'class_confidence', 'confidence',
                    '_yolo_review_only', '_yolo_origin', 'terminal_count',
                    'terminal_layout', 'terminal_count_basis', '_global_inventory_status', 'class',
                    # Il riferimento asimmetrico deve arrivare fino al
                    # costruttore: e' li' che si confronta col baricentro del
                    # modello nativo per stabilire il verso.
                    'landmark_feature', 'landmark_side', 'landmark_confidence') if key in metadata},
                model_z_offset_mm=float(metadata.get("model_z_offset_mm") or 0.0),
                color_hex=table.cellWidget(row, 8).text().strip() or "#303030",
                marking=table.cellWidget(row, 9).text().strip(),
                pin_count=metadata.get("pin_count"),
                pin_count_confidence=str(metadata.get("pin_count_confidence") or ""),
                pin_count_source=str(metadata.get("pin_count_source") or ""),
                pins_by_side=metadata.get("pins_by_side"),
                detected_kind=metadata.get("_detected_kind"),
                terminal_geometry=metadata.get("terminal_geometry"),
                package=str(metadata.get("package") or ""),
                mating=str(metadata.get("mating") or ""),
                bbox_basis=str(metadata.get("bbox_basis") or "unknown"),
                package_evidence=str(metadata.get("package_evidence") or ""),
                render_3d_override=(metadata.get("render_3d_override")
                                    if isinstance(metadata.get("render_3d_override"), bool) else None),
                model_rotation_delta_deg=float(metadata.get("model_rotation_delta_deg") or 0.0),
            ))
        return components

    def _build_annotations_group(self) -> QGroupBox:
        group = QGroupBox(
            "Annotazioni per il modello 3D (fillet su spigoli, testo inciso su facce) "
            "-- rivedi la lista prima di generare"
        )
        self.group_annotations = group
        layout = QVBoxLayout(group)

        fillet_row = QHBoxLayout()
        fillet_row.addWidget(QLabel("Spigolo:"))
        self.combo_fillet_edge = QComboBox()
        self.combo_fillet_edge.addItems(EDGE_NAMES)
        fillet_row.addWidget(self.combo_fillet_edge)
        fillet_row.addWidget(QLabel("Raggio:"))
        self.spin_fillet_radius = QDoubleSpinBox()
        self.spin_fillet_radius.setRange(0.05, 20.0)
        self.spin_fillet_radius.setDecimals(2)
        self.spin_fillet_radius.setSuffix(" mm")
        self.spin_fillet_radius.setValue(0.5)
        fillet_row.addWidget(self.spin_fillet_radius)
        btn_add_fillet = QPushButton("Aggiungi fillet")
        btn_add_fillet.clicked.connect(self._add_fillet_annotation)
        fillet_row.addWidget(btn_add_fillet)
        fillet_row.addStretch()
        layout.addLayout(fillet_row)

        text_row = QHBoxLayout()
        text_row.addWidget(QLabel("Faccia:"))
        self.combo_text_face = QComboBox()
        self.combo_text_face.addItems(FACE_NAMES)
        text_row.addWidget(self.combo_text_face)
        text_row.addWidget(QLabel("Testo da incidere:"))
        self.field_annotation_text = QLineEdit()
        self.field_annotation_text.setPlaceholderText("scrivi qui (es. TEL8WI)")
        self.field_annotation_text.setMinimumWidth(180)
        self.field_annotation_text.setStyleSheet(
            f"border: 1px solid {BLUE}; background: {SURFACE0};"
        )
        text_row.addWidget(self.field_annotation_text, 1)
        text_row.addWidget(QLabel("Dim.:"))
        self.spin_text_size = QDoubleSpinBox()
        self.spin_text_size.setRange(0.5, 30.0)
        self.spin_text_size.setDecimals(2)
        self.spin_text_size.setSuffix(" mm")
        self.spin_text_size.setValue(2.0)
        text_row.addWidget(self.spin_text_size)
        text_row.addWidget(QLabel("Spessore:"))
        self.spin_text_depth = QDoubleSpinBox()
        self.spin_text_depth.setRange(0.02, 5.0)
        self.spin_text_depth.setDecimals(2)
        self.spin_text_depth.setSuffix(" mm")
        self.spin_text_depth.setValue(0.06)
        self.spin_text_depth.setToolTip("Spessore dello strato di vernice serigrafata")
        text_row.addWidget(self.spin_text_depth)
        text_row.addWidget(QLabel("Colore:"))
        self.field_text_color = QLineEdit("#ffffff")
        self.field_text_color.setMaximumWidth(90)
        self.field_text_color.setToolTip("Colore della scritta, formato #rrggbb")
        text_row.addWidget(self.field_text_color)
        btn_add_text = QPushButton("Aggiungi testo")
        btn_add_text.clicked.connect(self._add_text_annotation)
        text_row.addWidget(btn_add_text)
        layout.addLayout(text_row)

        self.table_annotations = QTableWidget(0, 3)
        self.table_annotations.setHorizontalHeaderLabels(["Tipo", "Target", "Dettagli"])
        self.table_annotations.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table_annotations.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_annotations.setMinimumHeight(120)
        layout.addWidget(self.table_annotations)

        btn_remove = QPushButton("Rimuovi annotazione selezionata")
        btn_remove.clicked.connect(self._remove_selected_annotation)
        layout.addWidget(btn_remove)

        return group

    def _refresh_annotations_table(self) -> None:
        rows = (
            [("Fillet", f.edge, f"raggio {f.radius_mm:.2f} mm") for f in self.fillet_annotations]
            + [
                (
                    "Serigrafia", t.face,
                    f"\"{t.text}\", {t.size_mm:.2f}mm, spessore {t.thickness_mm:.2f}mm, {t.color_hex}",
                )
                for t in self.face_text_annotations
            ]
        )
        self.table_annotations.setRowCount(len(rows))
        for i, (tipo, target, dettagli) in enumerate(rows):
            self.table_annotations.setItem(i, 0, QTableWidgetItem(tipo))
            self.table_annotations.setItem(i, 1, QTableWidgetItem(target))
            self.table_annotations.setItem(i, 2, QTableWidgetItem(dettagli))

    def _add_fillet_annotation(self) -> None:
        ann = EdgeFilletAnnotation(edge=self.combo_fillet_edge.currentText(), radius_mm=self.spin_fillet_radius.value())
        try:
            ann.validate()
        except ValueError as exc:
            QMessageBox.warning(
                self, self.i18n.text("Fillet non valido"), str(exc))
            return
        self.fillet_annotations.append(ann)
        self._refresh_annotations_table()

    def _add_text_annotation(self) -> None:
        ann = FaceTextAnnotation(
            face=self.combo_text_face.currentText(),
            text=self.field_annotation_text.text().strip(),
            size_mm=self.spin_text_size.value(),
            thickness_mm=self.spin_text_depth.value(),
            color_hex=self.field_text_color.text().strip(),
        )
        try:
            ann.validate()
        except ValueError as exc:
            QMessageBox.warning(
                self, self.i18n.text("Testo non valido"), str(exc))
            return
        self.face_text_annotations.append(ann)
        self.field_annotation_text.clear()
        self._refresh_annotations_table()

    def _remove_selected_annotation(self) -> None:
        row = self.table_annotations.currentRow()
        if row < 0:
            return
        n_fillets = len(self.fillet_annotations)
        if row < n_fillets:
            del self.fillet_annotations[row]
        else:
            del self.face_text_annotations[row - n_fillets]
        self._refresh_annotations_table()

    def _current_system_prompt(self) -> str | None:
        """Prompt globale da usare per l'estrazione: None se e' quello di default."""
        text = self._profile_values()['system_prompt'].strip()
        if not text or text == DEFAULT_SYSTEM_PROMPT.strip():
            return None
        return text

    def _on_grid_toggled(self, checked: bool) -> None:
        self._config["show_grid"] = checked
        save_config(self._config)
        self.combo_grid_unit.setEnabled(checked)
        # Grid visibility is not a request to redraw/refit the footprint.
        for item in self.preview_scene.items():
            if item.data(_GRID_ITEM):
                item.setVisible(checked)

    def _on_grid_unit_changed(self) -> None:
        self._config["grid_unit"] = self.combo_grid_unit.currentData()
        save_config(self._config)
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

            self._add_castellated_pad_shape(p, placement, mm_to_px, pad_pen, pad_brush,
                                            xs_px, ys_px)

            number = self.preview_scene.addSimpleText(placement.number)
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
            return
        w, h = placement.width * mm_to_px, placement.height * mm_to_px
        x, y = placement.x * mm_to_px, placement.y * mm_to_px
        if placement.style == CONTACT_THROUGH_HOLE:
            ring = self.preview_scene.addEllipse(x - w / 2, y - h / 2, w, h, pen, brush)
            ring.setZValue(1)
            d = p.thru_hole_drill * mm_to_px
            if d > 0:
                hole = self.preview_scene.addEllipse(x - d / 2, y - d / 2, d, d, hole_pen, hole_brush)
                hole.setZValue(2)
        else:
            item = QGraphicsRectItem(x - w / 2, y - h / 2, w, h)
            item.setPen(pen)
            item.setBrush(brush)
            item.setZValue(1)
            self.preview_scene.addItem(item)
        xs_px.extend([x - w / 2, x + w / 2])
        ys_px.extend([y - h / 2, y + h / 2])

    def _draw_preview(self) -> None:
        if self._last_params is None:
            return
        if isinstance(self._last_params, PackageGeometry):
            self._draw_preview_package(self._last_params)
            return
        if isinstance(self._last_params, CastellatedModuleParams):
            self._draw_preview_castellated(self._last_params)
            return
        p = self._last_params
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
            self.preview_scene.addItem(item)

            # Numero del pin sul pad: scuro sul blu del pad, e insieme alla
            # tabella pinout rende verificabile la corrispondenza con il
            # datasheet senza aprire KiCad.
            number = self.preview_scene.addSimpleText(str(pin))
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
        body_l = package.body_length_mm or max((abs(p.x_mm) for p in pins), default=2.0) * 2 + 2
        body_w = package.body_width_mm or max((abs(p.y_mm) for p in pins), default=2.0) * 2 + 2
        margin = 1.0
        silk_pen = QPen(QColor(TEXT))
        silk_pen.setWidthF(1.5)
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
            self.preview_scene.addItem(item)
            xs.append(x); ys.append(y)
            number = self.preview_scene.addSimpleText(str(pin.number))
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

    def _draw_preview_3d(self) -> None:
        # Never replace a generated model with a synthetic matrix preview
        # simply because the user revisits the 3D tab.
        if self._last_mesh_path and Path(self._last_mesh_path).is_file():
            try:
                self.preview_3d.render_mesh(self._last_mesh_path)
                return
            except Exception as exc:
                self._append_log(f"Anteprima mesh non disponibile: {exc}")
        if self._is_castellated():
            # Per i moduli non esiste un'approssimazione parametrica da
            # disegnare al volo (il solido nasce dal taglio dei semi-fori):
            # si mostra la geometria reale appena generata, se c'e'.
            if self._last_mesh_path and Path(self._last_mesh_path).is_file():
                try:
                    self.preview_3d.render_mesh(self._last_mesh_path)
                    self._set_3d_status(self.i18n.text(
                        "Geometria reale del modulo (PCB con semi-fori sul bordo "
                        "e blocco dei componenti)."
                    ), STATUS_INFO)
                    return
                except Exception as exc:  # noqa: BLE001 - anteprima best-effort
                    self._append_log(f"Anteprima mesh non disponibile: {exc}")
            self.preview_3d.clear_preview()
            self._set_3d_status(self.i18n.text(
                "Attiva \"Modello 3D\" e premi \"Genera footprint\": il modello "
                "del modulo (PCB con semi-fori + componenti) si vede qui dopo la "
                "generazione con FreeCAD."
            ), STATUS_WARNING)
            return

        body = self._current_body_params()
        if body is None:
            self.preview_3d.clear_preview()
            self._set_3d_status(self.i18n.text(
                "Attiva \"Modello 3D\" nel tab \"Footprint 2D\" per vedere l'anteprima qui."
            ), STATUS_WARNING)
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
            "Premi \"Genera footprint\" per ottenere il modello .step esatto via FreeCAD."
        ), STATUS_INFO)

    # ── Cambio tab ─────────────────────────────────────────────────

    def _on_tab_changed(self, index: int) -> None:
        self._refresh_output_actions()
        if index == 0:
            self._refresh_docs_list()
        elif index == 2:
            self._update_summary()
        elif index == 3:
            self._draw_preview()
        elif index == 4:
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
        if self._rendering_3d or self._pending_3d_render is not None:
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
            self._rendering_3d = True
            self._draw_preview_3d()
        finally:
            self._rendering_3d = False
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
    """@brief Provide the main operation for this workflow."""
    app = QApplication(sys.argv)
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
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
