"""Finestra principale della GUI di KiCadGen-Tool.

Stesso linguaggio grafico di Pm3ToWav (C:\\Lavori\\Python\\Pm3ToWav):
tema scuro Catppuccin Mocha (kicadgen.gui.theme), QTabWidget con tab
arrotondati e bordo inferiore blu sulla tab attiva, generazione
eseguita in un QThread worker per non bloccare la UI (stesso pattern
di ConvertWorker in Pm3ToWav/main.py), log in una QTextEdit.

Cinque tab:
    1. Progetto     -- cartella che ospitera' il componente + documentazione (docs/)
    2. Footprint 2D -- parametri del generatore (kicadgen.generators.pin_header)
                       + gruppo opzionale "Modello 3D" (corpo/offset/pin length)
    3. Genera       -- riepilogo, pulsante Genera, log (footprint 2D + STEP 3D)
    4. Anteprima    -- disegno 2D dei pad a scala, con quote (margini, span, totali)
    5. Modello 3D   -- anteprima 3D interattiva (VTK, kicadgen.gui.preview_3d_vtk)
                       + annotazioni di post-elaborazione (fillet su spigoli,
                       serigrafie sulle facce) con tabella di revisione prima di generare

La cartella del componente segue la stessa convenzione gia' usata nel
resto del repo (es. ESP32-S3-N16R8/ESP32_S3_N16R8.pretty/): il nome
della cartella coincide con il nome del footprint. Il campo "Nome" vive
nel tab Progetto (determina anche il path); il tab Footprint 2D ne
mostra solo uno specchio in sola lettura.

Il tab Progetto include anche "Estrai parametri con AI dal datasheet"
(kicadgen.services.datasheet_extractor): manda i file selezionati in
docs/ a un modello LLM via OpenRouter e pre-compila
righe/colonne/passo/pad/drill (+ i campi del corpo 3D, se il datasheet ha
un disegno meccanico quotato). Prima di lanciarla si dichiara la
tipologia di componente (THT/SMD, singolo/aggregato, castellated), il cui
testo finisce nel prompt specifico del componente, dove resta
modificabile; il modello si sceglie fra il default globale e uno piu'
capace valido solo per la sessione. Richiede la variabile d'ambiente
OPENROUTER_API_KEY.
"""

from __future__ import annotations

from copy import deepcopy
import math
import shutil
import sys
import webbrowser
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal, Qt, QSize
from PyQt6.QtGui import QBrush, QColor, QIcon, QPen, QPixmap, QTextCursor
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGraphicsEllipseItem, QGraphicsRectItem, QGraphicsScene,
    QGraphicsView, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QDialog, QListWidget, QMainWindow, QMessageBox, QPushButton, QScrollArea,
    QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QListWidgetItem, QTabWidget, QTextEdit, QVBoxLayout,
    QWidget,
)

from ..cad3d.annotations import EDGE_NAMES, FACE_NAMES, EdgeFilletAnnotation, FaceTextAnnotation
from ..cad3d.body_params import Body3DParams
from ..cad3d.step_generator import (
    Cad3DError, find_freecadcmd, generate_3d_model, generate_castellated_3d_model,
)
from ..generators.castellated_module import (
    EDGE_LABELS, EDGES as CASTELLATED_EDGES, CastellatedModuleParams,
    CastellatedSide, ComponentBox, build_castellated_module, pad_positions,
)
from ..generators.pin_header import PinHeaderParams, build_pin_header, pin_positions
from ..services import component_types
from ..services.datasheet_extractor import (
    COMPONENT_KINDS, DEFAULT_SYSTEM_PROMPT, ExtractedParams, ExtractionError,
    extract_pin_header_params,
)
from ..services.openrouter import DEFAULT_MODEL
from .app_config import CONFIG_PATH, DEFAULTS, load_config, save_config
from .analysis_overlay import AnalysisOverlay
from .generation_options_dialog import GenerationOptionsDialog
from .model_dialog import pick_model
from .pinout_dialog import PinoutDialog
from .photo_review_dialog import PhotoReviewDialog, PhotoTransform
from .preview_3d_vtk import Preview3DVtk
from .settings_dialog import LlmSettingsDialog
from .theme import BASE, BLUE, SURFACE0, SURFACE1, SURFACE2, TEXT, YELLOW, APP_QSS

APP_DIR = Path(__file__).resolve().parent.parent.parent
# Area di lavoro: i componenti si generano qui dentro, e si esportano nella
# libreria vera solo quando sono stati verificati (pulsante "Esporta in libreria").
DEFAULT_LIBRARY_ROOT = APP_DIR / "generated"
DEFAULT_EXPORT_ROOT = APP_DIR.parent  # .../CustomComponents
DOCS_SUBDIR = "docs"

#: Chiave con cui si marcano le linee della griglia nell'anteprima 2D, per
#: poterle escludere dal calcolo dell'inquadratura.
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


def _generate_3d(params, body: Body3DParams, project_dir: Path, fillets, face_texts):
    """Genera il 3D con il generatore adatto al tipo di componente.

    I due modelli sono diversi in modo sostanziale: il pin header e' un
    corpo con pin cilindrici che sporgono sotto, il modulo castellated e'
    un PCB con i semi-fori tagliati sul bordo e i componenti sopra.
    """
    if isinstance(params, CastellatedModuleParams):
        return generate_castellated_3d_model(
            params, body, project_dir, fillets=fillets, face_texts=face_texts,
        )
    return generate_3d_model(
        params, body, project_dir, fillets=fillets, face_texts=face_texts,
    )


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

    def __init__(
        self, params: PinHeaderParams | CastellatedModuleParams, project_dir: Path,
        body: Body3DParams | None = None,
        fillets: list | None = None, face_texts: list | None = None,
    ):
        super().__init__()
        self.params = params
        self.project_dir = project_dir
        self.body = body
        self.fillets = fillets or []
        self.face_texts = face_texts or []

    def run(self) -> None:
        try:
            footprint = (
                build_castellated_module(self.params)
                if isinstance(self.params, CastellatedModuleParams)
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
                result = _generate_3d(
                    self.params, self.body, self.project_dir,
                    self.fillets, self.face_texts,
                )
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

    def run(self) -> None:
        try:
            result = _generate_3d(
                self.params, self.body, self.project_dir,
                self.fillets, self.face_texts,
            )
            for warning in result.get("warnings", []):
                self.warning.emit(warning)
            self.done.emit(str(result["step_path"]))
        except Cad3DError as exc:
            self.error.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - riportato in UI, non e' fatale
            self.error.emit(f"Errore inatteso: {exc}")


class ExtractWorker(QThread):
    """QThread che chiama OpenRouter per estrarre i parametri dal datasheet.

    `model` e' l'id OpenRouter da usare per questa estrazione: puo' essere
    il default di config.json oppure un modello piu' capace scelto solo per
    la sessione corrente.

    Segnali:
        done(ExtractedParams) -- parametri estratti con successo
        error(str)            -- messaggio di errore leggibile
        progress(str)        -- avanzamento dell'analisi AI, incluso ogni crop
    """

    done = pyqtSignal(object)
    error = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(
        self, doc_paths: list[Path], system_prompt: str | None = None,
        extra_instructions: str | None = None, include_nc_pins: bool = True,
        model: str | None = None, vision_model: str | None = None,
        grounding_prompt: str | None = None, verification_prompt: str | None = None,
    ):
        super().__init__()
        self.doc_paths = doc_paths
        self.system_prompt = system_prompt
        self.extra_instructions = extra_instructions
        self.include_nc_pins = include_nc_pins
        self.model = model
        self.vision_model = vision_model
        self.grounding_prompt = grounding_prompt
        self.verification_prompt = verification_prompt

    def run(self) -> None:
        try:
            params = extract_pin_header_params(
                self.doc_paths,
                model=self.model,
                vision_model=self.vision_model,
                grounding_prompt=self.grounding_prompt, verification_prompt=self.verification_prompt,
                system_prompt=self.system_prompt,
                extra_instructions=self.extra_instructions,
                include_nc_pins=self.include_nc_pins,
                progress=self.progress.emit,
            )
            self.done.emit(params)
        except ExtractionError as exc:
            self.error.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - errori rete/API riportati in UI
            self.error.emit(f"Errore API: {exc}")


class MainWindow(QMainWindow):
    """Finestra principale: 4 tab, Progetto / Footprint 2D / Genera / Anteprima."""

    def __init__(self):
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
        self._last_params: PinHeaderParams | None = None
        self.fillet_annotations: list[EdgeFilletAnnotation] = []
        self.face_text_annotations: list[FaceTextAnnotation] = []
        self.pinout: list[dict] = []
        # Componenti del modulo letti dalla foto: alimentano il 3D, non il
        # footprint, quindi non vivono nei campi del tab Footprint 2D.
        self.castellated_components: list[ComponentBox] = []
        self.pin_map: dict | None = None
        self._last_mesh_path: Path | None = None
        self._model_revision = 0
        self._config = load_config()
        # Modello scelto solo per questa sessione (datasheet difficile): non
        # viene salvato in config.json e si perde alla chiusura, per scelta.
        self._session_model: str | None = None
        self._session_vision_model: str | None = None

        self._build_menu()
        self._build_central()
        # Project type is restored before the footprint widgets exist.
        self.group_castellated.setChecked(
            self.combo_component_type.currentData() == "aggregate_castellated"
        )
        self._sync_footprint_subtabs()
        self._refresh_docs_list()

    # ── Menu ────────────────────────────────────────────────────────

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        self.action_open_output = file_menu.addAction("Apri cartella componente", self._open_project_dir)
        self.action_export = file_menu.addAction("Esporta in libreria...", self._export_component)
        file_menu.addSeparator()
        file_menu.addAction("Esci", self.close)

        config_menu = self.menuBar().addMenu("&Configurazione")
        config_menu.addAction(
            "Modello e prompt di sistema LLM...", self._edit_llm_settings
        )
        config_menu.addSeparator()

        self.action_generation_options = config_menu.addAction(
            "Opzioni di generazione...", self._edit_generation_options)

        config_menu.addSeparator()
        config_menu.addAction("Apri config.json", self._open_config_file)

        help_menu = self.menuBar().addMenu("&?")
        help_menu.addAction(
            "Apri cartella progetto tool", lambda: webbrowser.open(APP_DIR.as_uri())
        )
        help_menu.addSeparator()
        help_menu.addAction("About", self._show_about)

    def _edit_generation_options(self) -> None:
        dialog = GenerationOptionsDialog(bool(self._config.get("include_nc_pins", True)), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        checked = dialog.include_nc_pins()
        self._config["include_nc_pins"] = checked
        save_config(self._config)
        self._append_log(
            "Pin NC: verranno generati." if checked
            else "Pin NC: NON verranno generati (attenzione: il piedino fisico resta)."
        )

    def _edit_llm_settings(self) -> None:
        dialog = LlmSettingsDialog(
            self._config["system_prompt"], self._default_model(), self,
            current_vision_model=self._default_vision_model(),
            current_grounding_prompt=self._config.get("grounding_prompt"),
            current_verification_prompt=self._config.get("verification_prompt"),
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        self._config["system_prompt"] = dialog.prompt() or DEFAULT_SYSTEM_PROMPT
        self._config["model"] = dialog.model()
        self._config["vision_model"] = dialog.vision_model()
        self._config["grounding_prompt"] = dialog.grounding_prompt()
        self._config["verification_prompt"] = dialog.verification_prompt()
        save_config(self._config)
        self._refresh_model_combo()
        self._append_log(
            f"Impostazioni LLM aggiornate (config.json). "
            f"Modello datasheet: {self._default_model()}. "
            f"Modello foto: {self._vision_model() or 'default del servizio foto'}."
        )

    # ── Modello LLM ─────────────────────────────────────────────────

    def _default_model(self) -> str:
        """Modello di default globale (config.json)."""
        return (self._config.get("model") or "").strip() or DEFAULT_MODEL

    def _effective_model(self) -> str:
        """Modello da usare per la prossima estrazione.

        E' il modello di sessione, se ne e' stato scelto uno dal tab
        "Progetto" per questo componente; altrimenti il default globale.
        """
        return self._session_model or self._default_model()

    def _vision_model(self) -> str:
        """Photo model has an independent session selection."""
        return self._session_vision_model or self._default_vision_model()

    def _default_vision_model(self) -> str:
        return (self._config.get("vision_model") or "").strip() or DEFAULTS["vision_model"]

    def _refresh_model_combo(self) -> None:
        """Riallinea la tendina del tab Progetto a default + scelta di sessione."""
        if not hasattr(self, "combo_model"):
            return
        blocked = self.combo_model.blockSignals(True)
        self.combo_model.clear()
        self.combo_model.addItem(f"Default: {self._default_model()}", None)
        if self._session_model:
            self.combo_model.addItem(
                f"{self._session_model} (solo questa sessione)", self._session_model
            )
            self.combo_model.setCurrentIndex(1)
        else:
            self.combo_model.setCurrentIndex(0)
        self.combo_model.blockSignals(blocked)
        if hasattr(self, "combo_vision_model"):
            combo = self.combo_vision_model
            blocked = combo.blockSignals(True)
            combo.clear()
            combo.addItem(f"Default: {self._default_vision_model()}", None)
            if self._session_vision_model:
                combo.addItem(f"{self._session_vision_model} (solo questa sessione)",
                              self._session_vision_model)
                combo.setCurrentIndex(1)
            combo.blockSignals(blocked)

    def _on_vision_model_combo_changed(self) -> None:
        self._session_vision_model = self.combo_vision_model.currentData()
        self._append_log(f"Modello foto: {self._vision_model()} (il default globale non cambia).")

    def _pick_session_vision_model(self) -> None:
        chosen = pick_model(self._vision_model(), self,
                            title="Modello foto solo per questa sessione")
        if not chosen:
            return
        self._session_vision_model = None if chosen == self._default_vision_model() else chosen
        self._refresh_model_combo()
        self._on_vision_model_combo_changed()

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
        (e piu' costoso) senza cambiare il default salvato in config.json.
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
        if not CONFIG_PATH.is_file():
            save_config(self._config)
        webbrowser.open(CONFIG_PATH.as_uri())

    def _show_about(self) -> None:
        QMessageBox.about(
            self, "About",
            "KiCadGen-Tool\n\n"
            "Genera componenti KiCad (impronta 2D + modello 3D)\n"
            "a partire da dimensioni e quote.\n\n"
            "Modulo attivo: generatore footprint 2D (.kicad_mod)."
        )

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

        self.tabs = QTabWidget()
        self.tabs.addTab(project_tab, "\U0001F4C1 Progetto")
        self.tabs.addTab(footprint_tab, "\u25a6 Footprint 2D")
        self.tabs.addTab(generate_tab, "\u2699 Genera")
        self.tabs.addTab(preview_tab, "\U0001F441 Anteprima FootPrint")
        self.tabs.addTab(model_3d_tab, "\U0001F9CA Modello 3D")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        layout.addWidget(self.tabs)

        self.setCentralWidget(central)
        self.analysis_overlay = AnalysisOverlay(central)
        self.analysis_overlay.log_requested.connect(self.ai_log_dialog.exec)
        self._analysis_tabs_enabled = None

    # ── Tab 1: Progetto ────────────────────────────────────────────

    def _build_project_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        folder_group = QGroupBox("Cartella componente")
        folder_layout = QFormLayout(folder_group)

        self.field_name = QLineEdit("PinHeader_2x04_P2.54mm_THT")
        self.field_name.textChanged.connect(self._on_name_changed)
        folder_layout.addRow("Nome componente:", self.field_name)

        self.field_library_root = QLineEdit(str(DEFAULT_LIBRARY_ROOT))
        self.field_library_root.textChanged.connect(self._update_project_path_label)
        btn_browse_root = QPushButton("Sfoglia...")
        btn_browse_root.clicked.connect(self._browse_library_root)
        root_row = QHBoxLayout()
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

        self.docs_list = QListWidget()
        self.docs_list.setFixedHeight(140)
        self.docs_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.docs_list.setToolTip(
            "Spunta i file da inviare all'AI. I nuovi file vengono selezionati automaticamente."
        )
        docs_layout.addWidget(self.docs_list)

        docs_btn_row = QHBoxLayout()
        btn_add_docs = QPushButton("Aggiungi documentazione...")
        btn_add_docs.clicked.connect(self._add_docs)
        btn_remove_doc = QPushButton("Rimuovi selezionato")
        btn_remove_doc.clicked.connect(self._remove_doc)
        btn_open_docs = QPushButton("Apri cartella docs")
        btn_open_docs.clicked.connect(self._open_docs_folder)
        docs_btn_row.addWidget(btn_add_docs)
        docs_btn_row.addWidget(btn_remove_doc)
        docs_btn_row.addWidget(btn_open_docs)
        docs_btn_row.addStretch()
        docs_layout.addLayout(docs_btn_row)
        outer.addWidget(docs_group)
        self.group_component_analysis = QGroupBox("Analisi componente")
        analysis_layout = QVBoxLayout(self.group_component_analysis)

        ai_form = QFormLayout()

        self.combo_component_type = QComboBox()
        self.combo_component_type.addItem("(non specificata)", "")
        for component_type in component_types.COMPONENT_TYPES:
            self.combo_component_type.addItem(component_type.label, component_type.key)
        self.combo_component_type.setToolTip(
            "Dice al modello che cosa sta leggendo: da questo dipende quali quote "
            "cercare nel datasheet (fori e drill per un THT, land pattern per un "
            "SMD, semi-fori sul bordo per un modulo castellated)."
        )
        self.combo_component_type.currentIndexChanged.connect(
            self._on_component_type_changed
        )
        ai_form.addRow("Tipologia componente:", self.combo_component_type)
        models_form = QFormLayout()

        self.combo_model = QComboBox()
        self.combo_model.setToolTip(
            "Modello usato per il datasheet. Il default si imposta da "
            "Configurazione -> Modello e prompt di sistema LLM."
        )
        self.combo_model.currentIndexChanged.connect(self._on_model_combo_changed)
        btn_pick_model = QPushButton("Modello diverso...")
        btn_pick_model.setToolTip(
            "Sceglie un modello (filtrabile per costo) valido solo per le "
            "estrazioni di questa sessione: il default globale non cambia."
        )
        btn_pick_model.clicked.connect(self._pick_session_model)
        model_row = QHBoxLayout()
        model_row.addWidget(self.combo_model, 1)
        model_row.addWidget(btn_pick_model)
        models_form.addRow("Modello datasheet:", model_row)
        self.combo_vision_model = QComboBox()
        self.combo_vision_model.setToolTip(
            "Modello dedicato alla localizzazione dei componenti nella foto. "
            "La scelta vale per questa sessione; il default si imposta da Configurazione."
        )
        self.combo_vision_model.currentIndexChanged.connect(self._on_vision_model_combo_changed)
        self.btn_pick_vision_model = QPushButton("Modello diverso...")
        self.btn_pick_vision_model.setToolTip("Scegli un modello foto per questa sessione")
        self.btn_pick_vision_model.clicked.connect(self._pick_session_vision_model)
        vision_row = QHBoxLayout()
        vision_row.addWidget(self.combo_vision_model, 1)
        vision_row.addWidget(self.btn_pick_vision_model)
        models_form.addRow("Modello foto:", vision_row)

        analysis_layout.addLayout(ai_form)

        self.group_ai_tools = QGroupBox("Strumenti AI")
        tools_layout = QVBoxLayout(self.group_ai_tools)
        tools_layout.addLayout(models_form)
        tools_row = QHBoxLayout()
        tools_layout.addLayout(tools_row)
        tools_row.addStretch()
        analysis_layout.addWidget(self.group_ai_tools)

        self.btn_extract_ai = QPushButton("Estrai parametri con AI")
        self.btn_extract_ai.setObjectName("primary")
        self.btn_extract_ai.setIcon(QIcon(str(Path(__file__).resolve().parent / "assets" / "extract-ai.svg")))
        self.btn_extract_ai.setIconSize(QSize(24, 24))
        self.btn_extract_ai.setMinimumHeight(46)
        self.btn_extract_ai.setToolTip("Avvia l'estrazione dai documenti e dalle foto selezionati")
        self.btn_extract_ai.clicked.connect(self._extract_with_ai)
        self.btn_photo_comparison = QPushButton("Confronta foto...")
        self.btn_photo_comparison.setEnabled(False)
        self.btn_photo_comparison.clicked.connect(self._show_photo_comparison)
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
        self.ai_progress_text = QTextEdit()
        self.ai_progress_text.setReadOnly(True)
        self.ai_progress_text.setPlaceholderText("Qui compariranno i passi dell'analisi AI e i componenti trovati.")
        log_layout.addWidget(self.ai_progress_text)
        log_close = QPushButton("Chiudi")
        log_close.clicked.connect(self.ai_log_dialog.accept)
        log_layout.addWidget(log_close, alignment=Qt.AlignmentFlag.AlignRight)
        self.ai_log_dialog.resize(820, 460)
        self.btn_ai_log.clicked.connect(self.ai_log_dialog.exec)

        self.btn_component_prompt = QPushButton("Modifica prompt…")
        self.btn_component_prompt.setToolTip("Modifica le istruzioni AI specifiche di questo componente")
        tools_row.addWidget(self.btn_component_prompt)
        tools_row.addWidget(self.btn_photo_comparison)
        tools_row.addStretch()
        analysis_layout.addSpacing(10)
        analysis_layout.addWidget(self.btn_extract_ai, alignment=Qt.AlignmentFlag.AlignHCenter)
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
        component_type = component_types.get(key)

        self.field_component_prompt.setPlainText(
            component_types.apply_block(
                self.field_component_prompt.toPlainText(), component_type
            )
        )

        # La tipologia decide anche quale gruppo di parametri si usa nel tab
        # "Footprint 2D": i moduli castellated non stanno in una matrice.
        if key and hasattr(self, "group_castellated"):
            self.group_castellated.setChecked(key == "aggregate_castellated")

        self._config["component_type"] = key
        save_config(self._config)
        if component_type is not None:
            self._append_log(f"Tipologia componente: {component_type.label}.")

    def _component_name(self) -> str:
        return self.field_name.text().strip() if hasattr(self, "field_name") else ""

    def _project_dir(self) -> Path:
        root = self.field_library_root.text().strip() or str(DEFAULT_LIBRARY_ROOT)
        name = self._component_name() or "NUOVO_COMPONENTE"
        return Path(root) / name

    def _docs_dir(self) -> Path:
        return self._project_dir() / DOCS_SUBDIR

    def _update_project_path_label(self) -> None:
        self.label_project_path.setText(str(self._project_dir()))
        self._refresh_output_actions()
        self._refresh_docs_list()

    def _refresh_output_actions(self) -> bool:
        name = self._component_name()
        footprint = self._project_dir() / f"{name}.pretty" / f"{name}.kicad_mod"
        available = bool(name) and footprint.is_file()
        for attr in ("btn_open_output", "btn_export", "action_open_output", "action_export"):
            action = getattr(self, attr, None)
            if action is not None:
                action.setEnabled(available)
                action.setToolTip("" if available else "Genera prima il footprint del componente corrente")
        return available

    def _on_name_changed(self, text: str) -> None:
        if hasattr(self, "field_name_mirror"):
            self.field_name_mirror.setText(text)
        self._update_project_path_label()

    def _browse_library_root(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Scegli la cartella libreria", self.field_library_root.text()
        )
        if path:
            self.field_library_root.setText(path)

    def _refresh_docs_list(self) -> None:
        if not hasattr(self, "docs_list"):
            return
        self.docs_list.clear()
        docs_dir = self._docs_dir()
        if docs_dir.is_dir():
            for f in sorted(docs_dir.iterdir()):
                if f.is_file():
                    item = QListWidgetItem(f.name)
                    item.setFlags(
                        item.flags() | Qt.ItemFlag.ItemIsUserCheckable
                    )
                    # La documentazione appena caricata deve essere inclusa
                    # nell'estrazione senza richiedere anche una selezione
                    # temporanea della riga.
                    item.setCheckState(Qt.CheckState.Checked)
                    self.docs_list.addItem(item)

    def _add_docs(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "Seleziona documentazione (datasheet, PDF, immagini...)", "",
            "Documenti (*.pdf *.png *.jpg *.jpeg *.txt *.md *.xlsx *.csv);;Tutti i file (*)"
        )
        if not files:
            return
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
            self, "Rimuovi file",
            f"Eliminare definitivamente \"{item.text()}\" da {self._docs_dir()}?",
        )
        if confirm == QMessageBox.StandardButton.Yes:
            target.unlink(missing_ok=True)
            self._refresh_docs_list()

    def _open_docs_folder(self) -> None:
        docs_dir = self._docs_dir()
        docs_dir.mkdir(parents=True, exist_ok=True)
        webbrowser.open(docs_dir.as_uri())

    def _extract_with_ai(self) -> None:
        if self._extract_worker is not None and self._extract_worker.isRunning():
            return

        checked = [
            self.docs_list.item(index)
            for index in range(self.docs_list.count())
            if self.docs_list.item(index).checkState() == Qt.CheckState.Checked
        ]
        if not checked:
            QMessageBox.information(
                self, "Nessun file selezionato",
                "Spunta nella lista uno o piu' file di documentazione da analizzare."
            )
            return

        docs_dir = self._docs_dir()
        doc_paths = [docs_dir / item.text() for item in checked]

        self.btn_extract_ai.setEnabled(False)
        self.btn_extract_ai.setText("Analisi in corso...")
        self.ai_progress_text.clear()
        self.btn_photo_comparison.setEnabled(False)
        self._photo_png = None
        self._photo_overlay_png = None
        self._photo_source = ""

        model = self._effective_model()
        self._append_log(
            f"Estrazione da {len(doc_paths)} file con il modello datasheet {model}"
            + (" (solo questa sessione)." if self._session_model else ".")
        )
        self._on_ai_progress(f"Modello foto: {self._vision_model() or 'default del servizio foto'}.")

        self._extract_worker = ExtractWorker(
            doc_paths,
            model=model,
            vision_model=self._vision_model(),
            grounding_prompt=self._config.get("grounding_prompt"),
            verification_prompt=self._config.get("verification_prompt"),
            system_prompt=self._current_system_prompt(),
            extra_instructions=self.field_component_prompt.toPlainText().strip() or None,
            include_nc_pins=bool(self._config.get("include_nc_pins", True)),
        )
        self._extract_worker.done.connect(self._on_extracted)
        self._extract_worker.error.connect(self._on_extract_error)
        self._extract_worker.progress.connect(self._on_ai_progress)
        self._extract_worker.finished.connect(self._on_extract_finished)
        self._analysis_tabs_enabled = self.tabs.isEnabled()
        self.tabs.setEnabled(False)
        self.analysis_overlay.start()
        try:
            self._extract_worker.start()
        except Exception as exc:
            self._on_extract_finished()
            self._on_extract_error(str(exc))

    def _hide_analysis_overlay(self) -> None:
        self.analysis_overlay.stop()
        if self._analysis_tabs_enabled is not None:
            self.tabs.setEnabled(self._analysis_tabs_enabled)
            self._analysis_tabs_enabled = None

    def _on_extract_finished(self) -> None:
        self._hide_analysis_overlay()
        self.btn_extract_ai.setEnabled(True)
        self.btn_extract_ai.setText("Estrai parametri con AI")

    def _on_ai_progress(self, message: str) -> None:
        self.analysis_overlay.set_status(message)
        # Testo del modello: inserimento letterale, mai interpretato come HTML.
        self.ai_progress_text.moveCursor(QTextCursor.MoveOperation.End)
        self.ai_progress_text.insertPlainText(message + "\n")
        self.ai_progress_text.ensureCursorVisible()
        self._append_log(message)

    def _show_photo_comparison(self) -> None:
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
                QMessageBox.warning(self, "Revisione foto non disponibile", str(exc))
                return
            dialog.setWindowTitle(f"Revisione componenti: {self._photo_source}")
            dialog.components_applied.connect(self._apply_photo_review)
            dialog.exec()
            return
        if not self._photo_overlay_png:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Ultima estrazione: {self._photo_source}")
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
        self._invalidate_generated_mesh()
        self.label_3d_status.setText(
            "Componenti corretti dalla foto. Modello 3D da rigenerare: "
            "premi \"Rigenera modello 3D\" per aggiornare STEP e anteprima."
        )
        self._append_log(f"Revisione fotografica applicata: {len(components)} componenti; 3D da rigenerare.")

    def _invalidate_generated_mesh(self) -> None:
        # Running CAD workers still describe the previous input revision.
        self._model_revision += 1
        self._last_mesh_path = None
        self.preview_3d.clear_preview()
        self.label_3d_status.setText("Dati modificati. Modello 3D da rigenerare.")

    def _cad_result_is_current(self) -> bool:
        worker = self.sender()
        return worker is None or worker._model_revision == self._model_revision

    def _migrate_docs_to_new_name(self, new_name: str) -> None:
        """Sposta i documenti nella cartella del nuovo nome componente.

        La cartella docs/ vive dentro la cartella del componente, che prende il
        nome dal campo "Nome": quando l'estrazione AI propone un nome diverso,
        senza questo spostamento i file caricati prima resterebbero nella
        vecchia cartella e sparirebbero dalla lista (e non sarebbero piu'
        selezionabili per una nuova estrazione).
        """
        old_docs = self._docs_dir()
        if not old_docs.is_dir():
            return
        files = [f for f in old_docs.iterdir() if f.is_file()]
        if not files:
            return

        old_name = self._component_name()
        new_docs = Path(self.field_library_root.text().strip()) / new_name / DOCS_SUBDIR
        if new_docs.resolve() == old_docs.resolve():
            return

        answer = QMessageBox.question(
            self, "Rinomina componente",
            f"L'estrazione propone il nome \"{new_name}\" (adesso e' \"{old_name}\").\n\n"
            f"In {old_docs} ci sono {len(files)} file di documentazione.\n"
            "Li sposto nella cartella del nuovo nome? (altrimenti resteranno "
            "dove sono e spariranno dalla lista)",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        new_docs.mkdir(parents=True, exist_ok=True)
        for f in files:
            target = new_docs / f.name
            if target.exists():
                continue
            shutil.move(str(f), str(target))

    def _on_extracted(self, params: ExtractedParams) -> None:
        self._hide_analysis_overlay()
        self._invalidate_generated_mesh()
        self._photo_png = getattr(params, "photo_png", None)
        self._photo_overlay_png = params.photo_overlay_png
        self._photo_source = params.photo_source
        self.btn_photo_comparison.setEnabled(bool(self._photo_png or self._photo_overlay_png))
        self.btn_photo_comparison.setText("Rivedi componenti su foto..." if self._photo_png else "Confronta foto...")
        if params.name and params.name != self._component_name():
            self._migrate_docs_to_new_name(params.name)
        if params.name:
            self.field_name.setText(params.name)
        self.field_description.setText(params.description)
        if params.tags:
            self.field_tags.setText(params.tags)

        # Le due disposizioni riempiono gruppi di campi diversi; tutto il
        # resto (corpo 3D, pinout, colori, fillet, riepilogo) e' comune.
        if params.is_castellated():
            self._apply_extracted_castellated(params)
        else:
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

        if params.has_3d_body() and not params.is_castellated():
            self.group_3d.setChecked(True)
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
            f"\n\n{len(params.edge_fillets)} fillet suggeriti dalla foto -> tab \"Modello 3D\", tabella annotazioni."
            if params.edge_fillets else ""
        )
        QMessageBox.information(
            self, "Estrazione completata",
            f"Confidenza: {params.confidence}\n\n"
            f"{params.notes or 'Nessuna nota.'}"
            f"{fillet_note}\n\n"
            "Controlla i valori nel tab \"Footprint 2D\" prima di generare."
        )

    def _apply_extracted_castellated(self, params: ExtractedParams) -> None:
        """Riempie il gruppo "Modulo castellated" con i lati estratti.

        Le quote del corpo arrivano dagli stessi campi body_* della matrice
        (li' descrivono l'ingombro del case, qui il contorno del modulo), e
        le dimensioni dei pad dal primo lato che le riporta: il datasheet di
        norma da' un solo land pattern valido per tutti i contatti.
        """
        self.group_castellated.setChecked(True)

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

        self._append_log(
            "Modulo con piazzole a mezzo foro: "
            + ", ".join(
                f"lato {EDGE_LABELS.get(s['edge'], s['edge'])} "
                f"{s['count']} contatti a {s['pitch_mm']}mm"
                for s in params.castellated_sides or ()
            )
        )
        for line in names:
            self._append_log(f"Sigle serigrafate {line}")

    def _on_extract_error(self, message: str) -> None:
        self._hide_analysis_overlay()
        self._on_ai_progress(f"Estrazione fallita: {message}")
        QMessageBox.warning(self, "Estrazione fallita", message)

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
        for field in (self.field_cast_body_length, self.field_cast_body_width,
                      self.field_cast_hole, self.field_cast_pcb, self.field_cast_inset):
            field.valueChanged.connect(self._on_sides_changed)

        form.addRow("Sporgenza pad dal bordo:", self.field_cast_overhang)
        form.addRow("Diametro semi-foro (3D):", self.field_cast_hole)
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

        self.table_sides = QTableWidget(0, 6)
        self.table_sides.setHorizontalHeaderLabels(
            ["Lato", "N. contatti", "Passo", "Offset", "Primo pin", "Numerazione inversa"]
        )
        self.table_sides.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.table_sides.setMinimumHeight(150)
        self.table_sides.setMaximumHeight(200)
        layout.addWidget(self.table_sides)

        buttons = QHBoxLayout()
        btn_add = QPushButton("Aggiungi lato")
        btn_add.clicked.connect(lambda: self._add_side_row())
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
        if hasattr(self, "group_components"):
            self.group_components.setVisible(checked)
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

    def _add_side_row(self, side: dict | None = None) -> None:
        side = side or {}
        row = self.table_sides.rowCount()
        self.table_sides.insertRow(row)

        combo = QComboBox()
        for edge in CASTELLATED_EDGES:
            combo.addItem(EDGE_LABELS.get(edge, edge), edge)
        index = combo.findData(side.get("edge", "left"))
        combo.setCurrentIndex(max(index, 0))
        combo.currentIndexChanged.connect(self._on_sides_changed)
        self.table_sides.setCellWidget(row, 0, combo)

        count = QSpinBox()
        count.setRange(1, 100)
        count.setValue(int(side.get("count", 2)))
        count.valueChanged.connect(self._on_sides_changed)
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
            spin.valueChanged.connect(self._on_sides_changed)
            self.table_sides.setCellWidget(row, column, spin)

        first_pin = QSpinBox()
        first_pin.setRange(1, 999)
        first_pin.setValue(int(side.get("first_pin", 1)))
        first_pin.valueChanged.connect(self._on_sides_changed)
        self.table_sides.setCellWidget(row, 4, first_pin)

        reverse = QCheckBox()
        reverse.setChecked(bool(side.get("reverse")))
        reverse.toggled.connect(self._on_sides_changed)
        self.table_sides.setCellWidget(row, 5, reverse)

        self._on_sides_changed()

    def _remove_side_row(self) -> None:
        row = self.table_sides.currentRow()
        if row >= 0:
            self.table_sides.removeRow(row)
            self._on_sides_changed()

    def _on_sides_changed(self) -> None:
        self._update_summary()
        self._draw_preview()

    def _sides_from_table(self) -> list[CastellatedSide]:
        sides = []
        for row in range(self.table_sides.rowCount()):
            sides.append(CastellatedSide(
                edge=self.table_sides.cellWidget(row, 0).currentData(),
                count=self.table_sides.cellWidget(row, 1).value(),
                pitch=self.table_sides.cellWidget(row, 2).value(),
                offset=self.table_sides.cellWidget(row, 3).value(),
                first_pin=self.table_sides.cellWidget(row, 4).value(),
                reverse=self.table_sides.cellWidget(row, 5).isChecked(),
            ))
        return sides

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
            pcb_thickness=self.field_cast_pcb.value(),
            component_inset=self.field_cast_inset.value(),
            # Volutamente NON il colore del corpo: il blocco di ripiego (senza
            # componenti dalla foto) deve distinguersi dal PCB, non sparirci sopra.
            component_color_hex="#3a3a3a",
            components=self._components_from_table(),
            silk_labels=self._silk_labels(),
            sides=self._sides_from_table(),
        )

    def _silk_labels(self) -> dict[str, str]:
        """Sigla serigrafata per ogni pad, dalla tabella pinout.

        Sul modulo accanto al contatto c'e' la sigla corta (PE, B-, A+, G...),
        non la descrizione: si tiene la prima parola, senza parentesi.
        """
        labels = {}
        for entry in self.pinout:
            name = str(entry.get("name", "")).split("(")[0].strip()
            short = name.split()[0] if name else ""
            if short:
                labels[str(entry.get("pin"))] = short[:6]
        return labels

    def _is_castellated(self) -> bool:
        return hasattr(self, "group_castellated") and self.group_castellated.isChecked()

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
        group.setChecked(freecad_available)
        group.setToolTip(
            "FreeCAD trovato: il modello 3D viene generato insieme al footprint."
            if freecad_available else
            "freecadcmd non trovato: installa FreeCAD o imposta FREECAD_CMD_PATH."
        )
        self.group_3d = group
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
        self.btn_open_output = QPushButton("Apri cartella componente")
        self.btn_open_output.clicked.connect(self._open_project_dir)
        self.btn_export = QPushButton("Esporta in libreria...")
        self.btn_export.clicked.connect(self._export_component)
        btn_row.addWidget(self.btn_generate)
        btn_row.addWidget(self.btn_open_output)
        btn_row.addWidget(self.btn_export)
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

    def _export_component(self) -> None:
        """Copia la cartella del componente dall'area di lavoro alla libreria scelta.

        La generazione avviene sempre in `generated/` (area di lavoro): l'export
        e' un passo separato ed esplicito, cosi' la libreria vera riceve solo
        componenti gia' verificati.
        """
        if not self._refresh_output_actions():
            self._append_log("Genera prima il footprint del componente corrente.")
            return
        source = self._project_dir()
        if not source.is_dir():
            self._append_log(f"ERRORE export: {source} non esiste, genera prima il componente.")
            return

        destination_root = QFileDialog.getExistingDirectory(
            self, "Scegli la libreria di destinazione", str(DEFAULT_EXPORT_ROOT)
        )
        if not destination_root:
            return

        target = Path(destination_root) / source.name
        if target.resolve() == source.resolve():
            self._append_log("ERRORE export: origine e destinazione coincidono.")
            return

        if target.exists():
            answer = QMessageBox.question(
                self, "Destinazione esistente",
                f"{target} esiste gia'.\n\nSovrascrivo i file con lo stesso nome? "
                "(i file presenti solo nella destinazione restano)",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        try:
            shutil.copytree(source, target, dirs_exist_ok=True)
        except OSError as exc:
            self._append_log(f"ERRORE export: {exc}")
            return

        self._append_log(f"Esportato -> {target}")

    def _append_log(self, msg: str) -> None:
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
        )

    def _current_body_params(self) -> Body3DParams | None:
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
        )

    def _update_summary(self) -> None:
        if self._is_castellated():
            self._update_summary_castellated()
            return
        try:
            params = self._current_params()
        except ValueError as exc:
            self.label_summary.setText(f"<b>Errore parametri:</b> {exc}")
            return
        row_spacing = params.effective_row_spacing()
        populated = (
            f", {len(params.populated_pins)}/{params.rows * params.cols} pin popolati"
            if params.populated_pins else ""
        )
        self.label_summary.setText(
            f"<b>{params.name or '(nome vuoto)'}</b><br>"
            f"Cartella: {self._project_dir()}<br>"
            f"Griglia: {params.rows}x{params.cols}, passo {params.pitch}mm, "
            f"distanza righe {row_spacing}mm, pad {params.pad_diameter}mm, "
            f"drill {params.drill}mm{populated}"
        )

    def _update_summary_castellated(self) -> None:
        params = self._current_castellated_params()
        sides = ", ".join(
            f"{EDGE_LABELS.get(s.edge, s.edge)}: {s.count} contatti a {s.pitch}mm"
            for s in params.sides
        ) or "nessun lato definito"
        try:
            params.validate()
            error = ""
        except ValueError as exc:
            error = f"<br><b>Errore parametri:</b> {exc}"
        self.label_summary.setText(
            f"<b>{params.name or '(nome vuoto)'}</b><br>"
            f"Cartella: {self._project_dir()}<br>"
            f"Modulo castellated {params.body_length}x{params.body_width}mm, "
            f"pad {params.pad_length}x{params.pad_width}mm "
            f"(sporgenza {params.pad_overhang}mm)<br>{sides}{error}"
        )

    def _generate(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return

        try:
            params = (
                self._current_castellated_params() if self._is_castellated()
                else self._current_params()
            )
            params.validate()
        except ValueError as exc:
            self._append_log(f"ERRORE: {exc}")
            return

        project_dir = self._project_dir()

        self.btn_generate.setEnabled(False)
        if isinstance(params, CastellatedModuleParams):
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

        body = self._current_body_params()
        if body is not None:
            try:
                body.validate()
            except ValueError as exc:
                self._append_log(f"ERRORE parametri 3D: {exc}")
                return

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
        self._worker.finished.connect(lambda: self.btn_generate.setEnabled(True))
        self._worker.start()

    def _on_generated(self, path: str) -> None:
        self._refresh_output_actions()
        self._append_log(f"OK -> {path}")
        if not self._cad_result_is_current():
            return
        self._draw_preview()
        self._draw_preview_3d()

    def _on_error(self, message: str) -> None:
        self._append_log(f"ERRORE: {message}")

    def _on_cad3d_generated(self, path: str) -> None:
        if not self._cad_result_is_current():
            return
        self._append_log(f"OK (3D) -> {path}")
        self.label_3d_status.setText(f"Modello STEP aggiornato: {path}")
        self._show_generated_mesh(Path(path))

    def _on_cad3d_error(self, message: str) -> None:
        if not self._cad_result_is_current():
            return
        self._append_log(f"ERRORE 3D: {message}")

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
        PinoutDialog(self.pinout, self._component_name(), self).exec()

    # ── Tab 5: Modello 3D ──────────────────────────────────────────

    def _build_3d_tab(self) -> QWidget:
        # Only the settings scroll. The renderer must occupy its actual visible
        # viewport, not a clipped portion of a taller scrollable document.
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        header = QHBoxLayout()
        self.chk_pin_numbers = QCheckBox("Numeri pin (vista da sotto)")
        self.chk_pin_numbers.setToolTip(
            "I numeri compaiono solo guardando il componente dal lato dei piedini: "
            "usa il pulsante \"Sotto\" o ruota la vista sotto il piano della scheda."
        )
        self.chk_pin_numbers.setChecked(bool(self._config.get("show_pin_numbers_3d", True)))
        self.chk_pin_numbers.toggled.connect(self._on_pin_numbers_toggled)
        header.addWidget(self.chk_pin_numbers)

        # Il pulsante compare solo con i numeri attivi: e' li' che serve sapere
        # a quale funzione corrisponde il numero letto sul piedino.
        self.btn_pinout_3d = QPushButton("Tabella pinout...")
        self.btn_pinout_3d.clicked.connect(self._show_pinout_dialog)
        self.btn_pinout_3d.setVisible(self.chk_pin_numbers.isChecked())
        header.addWidget(self.btn_pinout_3d)
        header.addStretch()
        outer.addLayout(header)

        outer.addWidget(QLabel(
            "Anteprima 3D (ruotabile col mouse) -- dopo la generazione mostra il "
            "solido reale con fillet e serigrafie:"
        ))
        self.preview_3d = Preview3DVtk()
        self.preview_3d.show_pin_numbers = self.chk_pin_numbers.isChecked()
        self.preview_3d.setMinimumHeight(120)
        outer.addWidget(self.preview_3d, 1)

        view_row = QHBoxLayout()
        view_row.addWidget(QLabel("Vista:"))
        for label, key in (
            ("Iso", "iso"), ("Sopra", "top"), ("Sotto", "bottom"),
            ("Fronte", "front"), ("Retro", "back"),
            ("Sinistra", "left"), ("Destra", "right"),
        ):
            button = QPushButton(label)
            button.clicked.connect(lambda _, k=key: self.preview_3d.show_view(k))
            view_row.addWidget(button)
        btn_fit = QPushButton("Centra")
        btn_fit.clicked.connect(self.preview_3d.fit_view)
        view_row.addWidget(btn_fit)
        view_row.addStretch()
        outer.addLayout(view_row)

        self.label_3d_status = QLabel(
            "Attiva \"Modello 3D\" nel tab \"Footprint 2D\" per vedere l'anteprima qui."
        )
        self.label_3d_status.setWordWrap(True)
        self.label_3d_status.setStyleSheet(f"color: {TEXT}; font-style: italic;")
        outer.addWidget(self.label_3d_status)

        settings = QWidget()
        settings_layout = QVBoxLayout(settings)
        settings_layout.addWidget(self._build_annotations_group())
        settings_layout.addWidget(self._build_components_group())

        regen_row = QHBoxLayout()
        regen_row.addStretch()
        self.btn_regen_3d = QPushButton("Rigenera modello 3D (STEP) con queste annotazioni")
        self.btn_regen_3d.setObjectName("primary")
        self.btn_regen_3d.clicked.connect(self._regenerate_3d)
        regen_row.addWidget(self.btn_regen_3d)
        btn_open_3d = QPushButton("Apri cartella 3dshapes")
        btn_open_3d.clicked.connect(self._open_3dshapes_dir)
        regen_row.addWidget(btn_open_3d)
        regen_row.addStretch()

        scroll = QScrollArea()
        scroll.setWidget(settings)
        scroll.setWidgetResizable(True)
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
        container_layout.addWidget(splitter, 1)
        self.model_3d_action_bar = QWidget()
        self.model_3d_action_bar.setLayout(regen_row)
        container_layout.addWidget(self.model_3d_action_bar)
        return container

    def _on_pin_numbers_toggled(self, checked: bool) -> None:
        self._config["show_pin_numbers_3d"] = checked
        save_config(self._config)
        self.preview_3d.set_pin_numbers(checked)
        self.btn_pinout_3d.setVisible(checked)
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

    def _regenerate_3d(self) -> None:
        """Rigenera solo lo STEP con le annotazioni correnti (il footprint resta com'e')."""
        if self._cad3d_worker is not None and self._cad3d_worker.isRunning():
            return

        body = self._current_body_params()
        if body is None:
            QMessageBox.information(
                self, "Modello 3D non attivo",
                "Attiva il gruppo \"Modello 3D\" nel tab \"Footprint 2D\" "
                "e compila le dimensioni del corpo prima di generare."
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
            self.label_3d_status.setText(f"Errore parametri: {exc}")
            self._append_log(f"ERRORE 3D: {exc}")
            return

        self.btn_regen_3d.setEnabled(False)
        self.btn_regen_3d.setText("Generazione in corso (FreeCAD)...")
        self.label_3d_status.setText("Generazione STEP in corso tramite FreeCAD...")
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
        self._cad3d_worker.warning.connect(self._on_cad3d_warning)
        self._cad3d_worker.error.connect(self._on_regen_3d_error)
        self._cad3d_worker.finished.connect(self._on_regen_3d_finished)
        self._cad3d_worker.start()

    def _on_regen_3d_finished(self) -> None:
        self.btn_regen_3d.setEnabled(True)
        self.btn_regen_3d.setText("Rigenera modello 3D (STEP) con queste annotazioni")

    def _on_regen_3d_done(self, path: str) -> None:
        if not self._cad_result_is_current():
            return
        self._append_log(f"OK (3D) -> {path}")
        self.label_3d_status.setText(f"Modello STEP aggiornato: {path}")
        self._show_generated_mesh(Path(path))

    def _show_generated_mesh(self, step_path: Path) -> None:
        """Mostra in anteprima la mesh del solido appena generato (fillet e testo inclusi)."""
        mesh_path = step_path.with_suffix("").with_suffix(".preview.json")
        if not mesh_path.is_file():
            mesh_path = step_path.parent / f"{step_path.stem}.preview.json"
        if not mesh_path.is_file():
            return
        try:
            self.preview_3d.render_mesh(mesh_path)
            self._last_mesh_path = mesh_path
            self.label_3d_status.setText(
                f"{self.label_3d_status.text()}\n"
                "Anteprima aggiornata con la geometria reale esportata (fillet e testo inclusi)."
            )
        except Exception as exc:  # noqa: BLE001 - anteprima non critica
            self._append_log(f"Anteprima mesh non disponibile: {exc}")

    def _on_regen_3d_error(self, message: str) -> None:
        if not self._cad_result_is_current():
            return
        self._append_log(f"ERRORE 3D: {message}")
        self.label_3d_status.setText(f"Generazione STEP fallita: {message.splitlines()[0]}")

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
        metadata.setData(Qt.ItemDataRole.UserRole, deepcopy(photo_metadata or {}))
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

    def _remove_component_row(self) -> None:
        row = self.table_components.currentRow()
        if row >= 0:
            self.table_components.removeRow(row)

    def _set_photo_components(self, components: list[dict]) -> None:
        """Replace component rows, retaining unknown metadata beside each row."""
        self.castellated_components = [
            ComponentBox(
                x=c["x_mm"], y=c["y_mm"], length=c["length_mm"], width=c["width_mm"],
                height=c.get("height_mm", 1.0), name=c.get("name", "Componente"),
                color_hex=c.get("color_hex") or "#303030", kind=c.get("kind", "generic"),
                marking=c.get("marking", ""), rotation=float(c.get("rotation_deg", 0.0)),
            ) for c in components
        ]
        self._refresh_components_table(components)

    def _refresh_components_table(self, photo_components: list[dict] | None = None) -> None:
        """Riscrive la tabella dall'elenco appena estratto."""
        if not hasattr(self, "table_components"):
            return
        self.table_components.setRowCount(0)
        for index, component in enumerate(self.castellated_components):
            metadata = photo_components[index] if photo_components is not None else None
            self._add_component_row(component, metadata)

    def _components_from_table(self) -> list[ComponentBox]:
        """Componenti come sono ORA nella tabella (con le correzioni a mano)."""
        if not hasattr(self, "table_components"):
            return list(self.castellated_components)
        table = self.table_components
        components = []
        for row in range(table.rowCount()):
            components.append(ComponentBox(
                name=table.cellWidget(row, 0).text().strip() or "Componente",
                kind=table.cellWidget(row, 1).currentData() or "generic",
                x=table.cellWidget(row, 2).value(),
                y=table.cellWidget(row, 3).value(),
                length=table.cellWidget(row, 4).value(),
                width=table.cellWidget(row, 5).value(),
                height=table.cellWidget(row, 6).value(),
                rotation=float(table.cellWidget(row, 7).currentData() or 0.0),
                color_hex=table.cellWidget(row, 8).text().strip() or "#303030",
                marking=table.cellWidget(row, 9).text().strip(),
            ))
        return components

    def _build_annotations_group(self) -> QGroupBox:
        group = QGroupBox(
            "Annotazioni per il modello 3D (fillet su spigoli, testo inciso su facce) "
            "-- rivedi la lista prima di generare"
        )
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
            QMessageBox.warning(self, "Fillet non valido", str(exc))
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
            QMessageBox.warning(self, "Testo non valido", str(exc))
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
        text = (self._config.get("system_prompt") or "").strip()
        if not text or text == DEFAULT_SYSTEM_PROMPT.strip():
            return None
        return text

    def _on_grid_toggled(self, checked: bool) -> None:
        self._config["show_grid"] = checked
        save_config(self._config)
        self.combo_grid_unit.setEnabled(checked)
        self._draw_preview()

    def _on_grid_unit_changed(self) -> None:
        self._config["grid_unit"] = self.combo_grid_unit.currentData()
        save_config(self._config)
        self._draw_preview()

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
        for j in range(j_min, j_max + 1):
            pen = major if j % 5 == 0 else minor
            y = j * step * mm_to_px
            line = self.preview_scene.addLine(
                i_min * step * mm_to_px, y, i_max * step * mm_to_px, y, pen
            )
            line.setZValue(-10)
            line.setData(_GRID_ITEM, True)

    def _draw_preview_castellated(self, p: CastellatedModuleParams) -> None:
        """Anteprima del modulo castellated: corpo, piazzole e quote d'ingombro.

        Disegno separato da quello a matrice perche' la geometria e' diversa
        (origine al centro del corpo, pad rettangolari su piu' lati con passi
        indipendenti), ma come la' i pad vengono da pad_positions(): l'anteprima
        non puo' divergere dal footprint che verra' scritto.
        """
        self.preview_scene.clear()
        mm_to_px = 20.0

        if self.chk_grid.isChecked():
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
            xs_px.extend([x - w / 2, x + w / 2])
            ys_px.extend([y - h / 2, y + h / 2])

            item = QGraphicsRectItem(x - w / 2, y - h / 2, w, h)
            item.setPen(pad_pen)
            item.setBrush(pad_brush)
            self.preview_scene.addItem(item)

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
            self._add_dim_h(-half_l, half_l, top - 40, f"corpo {p.body_length:.2f} mm")
            self._add_dim_v(-half_w, half_w, left - 40, f"corpo {p.body_width:.2f} mm")
            self._add_dim_h(left, right, bottom + 60, f"totale {(right - left) / mm_to_px:.2f} mm")

        self.preview_scene.setBackgroundBrush(QBrush(QColor(BASE)))
        self._fit_preview()

    def _draw_preview(self) -> None:
        if self._last_params is None:
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

        if self.chk_grid.isChecked():
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
            self._add_dim_h(0, p.pitch * mm_to_px, top - 20, f"passo {p.pitch:.2f} mm")
        if p.rows >= 2:
            self._add_dim_v(0, row_spacing * mm_to_px, left - 20, f"righe {row_spacing:.2f} mm")

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
        self._add_dim_h(left, right, bottom + 60, f"totale {width + 2 * margin:.2f} mm")
        self._add_dim_v(top, bottom, right + 60, f"totale {height + 2 * margin:.2f} mm")

        self.preview_scene.setBackgroundBrush(QBrush(QColor(BASE)))
        self._fit_preview()

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
        if self._is_castellated():
            # Per i moduli non esiste un'approssimazione parametrica da
            # disegnare al volo (il solido nasce dal taglio dei semi-fori):
            # si mostra la geometria reale appena generata, se c'e'.
            if self._last_mesh_path and Path(self._last_mesh_path).is_file():
                try:
                    self.preview_3d.render_mesh(self._last_mesh_path)
                    self.label_3d_status.setText(
                        "Geometria reale del modulo (PCB con semi-fori sul bordo "
                        "e blocco dei componenti)."
                    )
                    return
                except Exception as exc:  # noqa: BLE001 - anteprima best-effort
                    self._append_log(f"Anteprima mesh non disponibile: {exc}")
            self.preview_3d.clear_preview()
            self.label_3d_status.setText(
                "Attiva \"Modello 3D\" e premi \"Genera footprint\": il modello "
                "del modulo (PCB con semi-fori + componenti) si vede qui dopo la "
                "generazione con FreeCAD."
            )
            return

        body = self._current_body_params()
        if body is None:
            self.preview_3d.clear_preview()
            self.label_3d_status.setText(
                "Attiva \"Modello 3D\" nel tab \"Footprint 2D\" per vedere l'anteprima qui."
            )
            return

        try:
            params = self._current_params()
            params.validate()
            self.preview_3d.render_params(params, body)
        except ValueError as exc:
            self.preview_3d.clear_preview()
            self.label_3d_status.setText(f"Errore parametri: {exc}")
            return

        self.label_3d_status.setText(
            "Anteprima approssimata (poligoni, per farsi un'idea dell'ingombro). "
            "Premi \"Genera footprint\" per ottenere il modello .step esatto via FreeCAD."
        )

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
            self._draw_preview_3d()


def main() -> None:
    app = QApplication(sys.argv)
    app.setStyleSheet(APP_QSS)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
