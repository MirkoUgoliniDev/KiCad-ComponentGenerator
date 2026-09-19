"""@brief Provide the main KiCadGen-Tool desktop window.

@details The window uses the shared dark Qt theme and runs long generation and
AI operations in worker threads. Its six stable tabs cover project input,
advanced footprint parameters, generation, 2D review, 3D review and the final
KiCad validation/installation workflow. Standard-user mode hides only the two
advanced parameter tabs while preserving their indexes for existing handlers.
"""

from __future__ import annotations

from copy import deepcopy
import math
import re
import shutil
import sqlite3
import sys
import webbrowser
from pathlib import Path

from PyQt6.QtCore import QThread, QTimer, pyqtSignal, Qt, QSize
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
from .analysis_overlay import AnalysisOverlay
from .generation_options_dialog import GenerationOptionsDialog
from .guided_documents import GuidedDocuments
from .model_dialog import pick_model
from .pinout_dialog import PinoutDialog
from .photo_review_dialog import PhotoReviewDialog, PhotoTransform
from .preview_3d_vtk import Preview3DVtk
from .settings_dialog import LlmSettingsDialog
from .profile_dialog import ProfilesDialog, icon_button
from .translation_dialog import TranslationDialog
from .theme import BASE, BLUE, SURFACE0, SURFACE1, SURFACE2, TEXT, YELLOW, APP_QSS

APP_DIR = Path(__file__).resolve().parent.parent.parent
# Generated working files stay separate from installed KiCad libraries.
DEFAULT_LIBRARY_ROOT = APP_DIR / "generated"
DOCS_SUBDIR = "docs"


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
    progress = pyqtSignal(str)

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

    def run(self) -> None:
        """@brief Provide the run operation for this workflow."""
        """@brief Provide the run operation for this workflow."""
        """@brief Provide the run operation for this workflow."""
        try:
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
                self.progress.emit('Generazione del modello 3D tramite FreeCAD…')
                result = _generate_3d(
                    self.params, self.body, self.project_dir,
                    self.fillets, self.face_texts,
                )
                for warning in result.get('warnings', []):
                    self.warning.emit(warning)
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


class TerminalDetailWorker(QThread):
    """Optional photo detail pass; never regenerates the footprint."""
    done = pyqtSignal(object)
    error = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, photo, components, model, only_leads=False, system_prompt=None):
        super().__init__()
        self.photo, self.components, self.model = photo, deepcopy(components), model
        self.only_leads = only_leads
        self.system_prompt = system_prompt

    def run(self):
        """@brief Provide the run operation for this workflow."""
        from ..services.photo_terminals import observe_terminals
        from ..services.photo_leads import refine_leads
        from ..services.openrouter import call_tool
        try:
            operation = refine_leads if self.only_leads else observe_terminals
            self.done.emit(operation(self.photo, self.components, self.model,
                call_tool, self.progress.emit,
                Path(__file__).resolve().parents[2] / 'generated' / 'photo-cache',
                **({'system_prompt': self.system_prompt} if self.system_prompt else {})))
        except Exception as exc:
            self.error.emit(f"Dettaglio terminali fallito: {exc}")


class ExtractWorker(QThread):
    """QThread che chiama OpenRouter per estrarre i parametri dal datasheet.

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

    def __init__(
        self, doc_paths: list[Path], system_prompt: str | None = None,
        extra_instructions: str | None = None, include_nc_pins: bool = True,
        model: str | None = None, vision_model: str | None = None,
        grounding_prompt: str | None = None, verification_prompt: str | None = None,
        verification_model: str | None = None, terminal_prompt: str | None = None,
        document_roles: dict | None = None,
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
        self.verification_model = verification_model
        self.terminal_prompt = terminal_prompt
        self.document_roles = dict(document_roles) if document_roles is not None else None

    def run(self) -> None:
        try:
            params = extract_pin_header_params(
                self.doc_paths,
                model=self.model,
                vision_model=self.vision_model,
                grounding_prompt=self.grounding_prompt, verification_prompt=self.verification_prompt,
                verification_model=self.verification_model, terminal_prompt=self.terminal_prompt,
                document_roles=self.document_roles,
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
        self._last_params: PinHeaderParams | CastellatedModuleParams | PackageGeometry | None = None
        self.package_geometry: PackageGeometry | None = None
        self.fillet_annotations: list[EdgeFilletAnnotation] = []
        self.face_text_annotations: list[FaceTextAnnotation] = []
        self.pinout: list[dict] = []
        # Componenti del modulo letti dalla foto: alimentano il 3D, non il
        # footprint, quindi non vivono nei campi del tab Footprint 2D.
        self.castellated_components: list[ComponentBox] = []
        self.pin_map: dict | None = None
        self._last_mesh_path: Path | None = None
        self._model_revision = 0
        self._acquisition_completed = False
        self._config = load_config()
        self.profile_store = profile_store or ProfileStore(legacy_config=self._config, legacy_path=config_path())
        self.i18n = I18n(self.profile_store, self._config.get("language", "it"))
        # Modello scelto solo per questa sessione (datasheet difficile): non
        # viene salvato in config.json e si perde alla chiusura, per scelta.
        self._session_model: str | None = None
        self._session_vision_model: str | None = None
        self._session_verification_model: str | None = None
        self._auto_after_extraction = False
        self._auto_generation_params = None
        self._applying_extraction = False
        self._extract_finish_pending = False
        self._automatic_generation_active = False
        self._automatic_footprint_done = False
        self._automatic_3d_done = False
        self._automatic_generation_errors = []
        self._startup_dependencies_checked = False

        self._build_menu()
        self._build_central()
        self._apply_user_mode()
        # Project type is restored before the footprint widgets exist.
        self.group_castellated.setChecked(
            self._selected_family() == "aggregate_castellated"
        )
        self._sync_footprint_subtabs()
        self._refresh_docs_list()

    # ── Menu ────────────────────────────────────────────────────────

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu(self.i18n.tr("app.file"))
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
                                         advanced_user=self._advanced_user())
        self.i18n.retranslate_widgets(dialog)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        checked = dialog.include_nc_pins()
        self._config["include_nc_pins"] = checked
        self._config['advanced_user'] = dialog.advanced_user()
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

    def _apply_user_mode(self):
        advanced = self._advanced_user()
        # Keep widget instances and numeric tab indexes stable for existing handlers.
        if not advanced and self.tabs.currentIndex() in (1, 2):
            self.tabs.setCurrentIndex(0)
        self.tabs.setTabVisible(1, advanced)
        self.tabs.setTabVisible(2, advanced)
        self.model_3d_settings_scroll.setVisible(advanced)
        self.group_annotations.setVisible(advanced)
        self.group_components.setVisible(advanced and self._is_castellated())
        self.btn_photo_comparison.setVisible(advanced)
        if hasattr(self, "action_translations"):
            self.action_translations.setVisible(advanced)

    def _background_work_running(self):
        return any(worker is not None and worker.isRunning() for worker in
                   (self._worker, self._extract_worker, self._cad3d_worker, self._terminal_worker))

    def closeEvent(self, event):
        """@brief Provide the closeEvent operation for this workflow."""
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
        """Photo model has an independent session selection."""
        return self._session_vision_model or self._default_vision_model()

    def _default_vision_model(self) -> str:
        return self._profile_values()['vision_model']

    def _verification_model(self):
        return self._session_verification_model or self._profile_values()['verification_model']

    def _on_verification_model_combo_changed(self):
        self._session_verification_model = self.combo_verification_model.currentData()
        self._append_log(f'Modello verifica foto: {self._verification_model()} (default invariato).')

    def _pick_session_verification_model(self):
        chosen = pick_model(self._verification_model(), self, title='Modello verifica foto per questa sessione')
        if chosen:
            self._session_verification_model = None if chosen == self._profile_values()['verification_model'] else chosen
            self._refresh_model_combo()
            self._on_verification_model_combo_changed()

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
        if hasattr(self, "combo_vision_model"):
            combo = self.combo_vision_model
            blocked = combo.blockSignals(True)
            combo.clear()
            combo.addItem(
                self.i18n.tr("model.default", model=self._default_vision_model()), None
            )
            if self._session_vision_model:
                combo.addItem(self.i18n.tr("model.session", model=self._session_vision_model),
                              self._session_vision_model)
                combo.setCurrentIndex(1)
            combo.blockSignals(blocked)
        if hasattr(self, 'combo_verification_model'):
            combo = self.combo_verification_model
            blocked = combo.blockSignals(True)
            combo.clear()
            combo.addItem(
                self.i18n.tr(
                    "model.default", model=self._profile_values()["verification_model"]
                ),
                None,
            )
            if self._session_verification_model:
                combo.addItem(
                    self.i18n.tr("model.session", model=self._session_verification_model),
                    self._session_verification_model,
                )
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
        self.language_label = QLabel(self.i18n.tr("language.label"))
        self.language_label.setProperty("i18n_source", "Lingua:")
        self.language_label.setMinimumWidth(62)
        language_row.addWidget(self.language_label)
        self.language_combo = QComboBox()
        for code, label in LANGUAGES.items():
            self.language_combo.addItem(label, code)
        self.language_combo.setCurrentIndex(max(0, self.language_combo.findData(self.i18n.language)))
        self.language_combo.setToolTip("Select the interface language")
        self.language_combo.setAccessibleName("Interface language")
        self.language_combo.setMinimumWidth(120)
        self.language_combo.currentIndexChanged.connect(self._on_language_changed)
        language_row.addWidget(self.language_combo)
        layout.addLayout(language_row)

        self.tabs = QTabWidget()
        # Navigation tabs use stable translation keys and are refreshed explicitly.
        self.tabs.setProperty("i18n_skip_tabs", True)
        self.tabs.addTab(project_tab, self.i18n.tr("tab.project"))
        self.tabs.addTab(footprint_tab, self.i18n.tr("tab.footprint"))
        self.tabs.addTab(generate_tab, self.i18n.tr("tab.generate"))
        self.tabs.addTab(preview_tab, self.i18n.tr("tab.preview"))
        self.tabs.addTab(model_3d_tab, self.i18n.tr("tab.model3d"))
        self.tabs.addTab(kicad_tab, self.i18n.tr("tab.kicad"))
        self._set_acquisition_tabs_enabled(False)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        layout.addWidget(self.tabs)

        self.setCentralWidget(central)
        self.analysis_overlay = AnalysisOverlay(central, i18n=self.i18n)
        self.analysis_overlay.log_requested.connect(self.ai_log_dialog.exec)
        self._analysis_tabs_enabled = None
        # Apply the persisted language to all catalogued widgets on first render.
        self.i18n.retranslate_widgets(self)

    def _on_language_changed(self, index: int) -> None:
        """@brief Persist the selected language and refresh navigation labels."""
        language = self.language_combo.itemData(index)
        if not language or language == self.i18n.language:
            return
        self.i18n.set_language(language)
        self._config["language"] = language
        save_config(self._config)
        self.tabs.setTabText(0, self.i18n.tr("tab.project"))
        self.tabs.setTabText(1, self.i18n.tr("tab.footprint"))
        self.tabs.setTabText(2, self.i18n.tr("tab.generate"))
        self.tabs.setTabText(3, self.i18n.tr("tab.preview"))
        self.tabs.setTabText(4, self.i18n.tr("tab.model3d"))
        self.tabs.setTabText(5, self.i18n.tr("tab.kicad"))
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
        self.action_open_config.setText(self.i18n.text("Apri config.json"))
        self.action_open_tool_dir.setText(self.i18n.text("Apri cartella progetto tool"))
        self.action_about.setText(self.i18n.text("About"))
        self._retranslate_component_types()
        self._refresh_model_combo()
        self._refresh_output_actions()

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

        self.field_name = QLineEdit("PinHeader_2x04_P2.54mm_THT")
        self.field_name.textChanged.connect(self._on_name_changed)
        folder_layout.addRow("Nome componente:", self.field_name)

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
            "Modello usato per il datasheet. Il default si imposta da "
            "Configurazione -> Gestione tipologie e profili AI."
        )
        self.combo_model.currentIndexChanged.connect(self._on_model_combo_changed)
        btn_pick_model = QPushButton()
        btn_pick_model.setIcon(QIcon(str(Path(__file__).parent / 'assets' / 'model-edit.svg')))
        btn_pick_model.setIconSize(QSize(20, 20))
        btn_pick_model.setFixedSize(40, 32)
        btn_pick_model.setAutoDefault(False)
        btn_pick_model.setToolTip("Change the datasheet model for this session")
        btn_pick_model.setAccessibleName("Change datasheet model")
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
        self.btn_pick_vision_model = QPushButton()
        self.btn_pick_vision_model.setIcon(QIcon(str(Path(__file__).parent / 'assets' / 'model-edit.svg')))
        self.btn_pick_vision_model.setIconSize(QSize(20, 20))
        self.btn_pick_vision_model.setFixedSize(40, 32)
        self.btn_pick_vision_model.setAutoDefault(False)
        self.btn_pick_vision_model.setToolTip("Change the photo model for this session")
        self.btn_pick_vision_model.setAccessibleName("Change photo model")
        self.btn_pick_vision_model.clicked.connect(self._pick_session_vision_model)
        vision_row = QHBoxLayout()
        vision_row.addWidget(self.combo_vision_model, 1)
        vision_row.addWidget(self.btn_pick_vision_model)
        models_form.addRow("Modello foto:", vision_row)
        self.combo_verification_model = QComboBox()
        self.combo_verification_model.setToolTip('Modello di verifica foto, indipendente dalle altre analisi. Scelta solo per questa sessione.')
        self.combo_verification_model.currentIndexChanged.connect(self._on_verification_model_combo_changed)
        self.btn_pick_verification_model = QPushButton()
        self.btn_pick_verification_model.setIcon(QIcon(str(Path(__file__).parent / 'assets' / 'model-edit.svg')))
        self.btn_pick_verification_model.setIconSize(QSize(20, 20))
        self.btn_pick_verification_model.setFixedSize(40, 32)
        self.btn_pick_verification_model.setAutoDefault(False)
        self.btn_pick_verification_model.setToolTip("Change the photo verification model for this session")
        self.btn_pick_verification_model.setAccessibleName("Change photo verification model")
        self.btn_pick_verification_model.clicked.connect(self._pick_session_verification_model)
        verification_row = QHBoxLayout()
        verification_row.addWidget(self.combo_verification_model, 1)
        verification_row.addWidget(self.btn_pick_verification_model)
        models_form.addRow('Verifica foto:', verification_row)
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
        self.btn_extract_ai.setToolTip("Avvia l'estrazione dai documenti e dalle foto selezionati")
        self.btn_extract_ai.clicked.connect(self._extract_with_ai)
        self.btn_photo_comparison = QPushButton("Rivedi rilevamento foto…")
        self.btn_photo_comparison.setToolTip(
            "Mostra gli ingombri rilevati sulla foto superiore e, quando disponibile, "
            "permette di correggerli. Non avvia la generazione del footprint o del modello 3D."
        )
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
        tools_row.addWidget(self.btn_photo_comparison)
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
        step = self._project_dir() / f"{name}.3dshapes" / f"{name}.step"
        footprint_available = bool(name) and footprint.is_file()
        complete_available = footprint_available and step.is_file()
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
            self.label_kicad_step.setText(str(step) if step.is_file() else "—")
            self.label_kicad_status.setText(
                self.i18n.text("Footprint e modello STEP pronti per la verifica.")
                if complete_available else
                self.i18n.text("Genera footprint e modello STEP per continuare."))
        return footprint_available

    def _on_name_changed(self, text: str) -> None:
        if hasattr(self, "field_name_mirror"):
            self.field_name_mirror.setText(text)
        self._update_project_path_label()

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

        self.btn_extract_ai.setEnabled(False)
        self._auto_after_extraction = not self._advanced_user()
        self._auto_generation_params = None
        self._extract_finish_pending = False
        self.btn_extract_ai.setText(self.i18n.text("Analisi in corso..."))
        self.ai_progress_text.clear()
        self.ai_elements_list.clear()
        self.btn_photo_comparison.setEnabled(False)
        self._photo_png = None
        self._photo_overlay_png = None
        self._photo_source = ""

        model = self._session_model or profile['model']
        photo_model = self._session_vision_model or profile['vision_model']
        verification_model = self._session_verification_model or profile['verification_model']
        self._append_log(
            f"Estrazione da {len(doc_paths)} file con il modello datasheet {model}"
            + (" (solo questa sessione)." if self._session_model else ".")
        )
        self._on_ai_progress(f"Profilo AI: {self.combo_component_type.currentText()}.")
        self._on_ai_progress(f"Modello foto: {photo_model}.")
        self._on_ai_progress(f"Modello verifica foto: {verification_model}.")

        self._extract_worker = ExtractWorker(
            doc_paths,
            model=model,
            vision_model=photo_model, verification_model=verification_model,
            grounding_prompt=profile['grounding_prompt'],
            verification_prompt=profile['verification_prompt'],
            terminal_prompt=profile['terminal_prompt'],
            document_roles=document_roles,
            system_prompt=profile['system_prompt'],
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

    def _on_ai_progress(self, message: str) -> None:
        self.analysis_overlay.set_status(message)
        # Testo del modello: inserimento letterale, mai interpretato come HTML.
        if re.match(r"^#\d+", message.strip()):
            self.ai_elements_list.addItem(message)
        else:
            self.ai_progress_text.moveCursor(QTextCursor.MoveOperation.End)
            self.ai_progress_text.insertPlainText(message + "\n")
            self.ai_progress_text.ensureCursorVisible()
        self._append_log(message)

    def _refresh_ai_copy_buttons(self, *args) -> None:
        self.btn_copy_ai_log.setEnabled(bool(self.ai_progress_text.toPlainText()))
        self.btn_copy_ai_elements.setEnabled(self.ai_elements_list.count() > 0)

    def _copy_ai_output(self, *, elements=False) -> None:
        text = ('\n'.join(self.ai_elements_list.item(i).text() for i in range(self.ai_elements_list.count()))
                if elements else self.ai_progress_text.toPlainText())
        if text:
            QApplication.clipboard().setText(text)

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
                QMessageBox.warning(
                    self, self.i18n.text("Revisione foto non disponibile"), str(exc))
                return
            dialog.setWindowTitle(
                self.i18n.tr("photo_review.title", source=self._photo_source)
            )
            dialog.components_applied.connect(self._apply_photo_review)
            self.i18n.retranslate_widgets(dialog)
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
        self._invalidate_generated_mesh()
        self.label_3d_status.setText(self.i18n.text(
            "Componenti corretti dalla foto. Modello 3D da rigenerare: "
            "premi \"Rigenera modello 3D\" per aggiornare STEP e anteprima."
        ))
        self._append_log(f"Revisione fotografica applicata: {len(components)} componenti; 3D da rigenerare.")

    def _invalidate_generated_mesh(self) -> None:
        # Running CAD workers still describe the previous input revision.
        self._model_revision += 1
        self._last_mesh_path = None
        self.preview_3d.clear_preview()
        self.label_3d_status.setText(
            self.i18n.text("Dati modificati. Modello 3D da rigenerare."))

    def _cad_result_is_current(self) -> bool:
        worker = self.sender()
        return worker is None or worker._model_revision == self._model_revision

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
        old_docs = self._docs_dir()
        if not old_docs.is_dir():
            return new_name
        files = [f for f in old_docs.iterdir() if f.is_file()]
        if not files:
            return new_name

        old_name = self._component_name()
        new_docs = Path(self.field_library_root.text().strip()) / new_name / DOCS_SUBDIR
        if new_docs.resolve() == old_docs.resolve():
            return old_name

        answer = QMessageBox.question(
            self, self.i18n.text("Rinomina componente"),
            self.i18n.tr(
                "component.rename_question", new_name=new_name, old_name=old_name,
                directory=old_docs, count=len(files)),
        )
        if answer != QMessageBox.StandardButton.Yes:
            self._on_ai_progress(f'Rinomina non eseguita: mantenuto il progetto {old_name}. Dati AI acquisiti.')
            return old_name

        if (old_docs / MANIFEST).is_file() and any((new_docs / f.name).exists() for f in files):
            message = self.i18n.tr(
                'component.rename_conflict', new_name=new_name, old_name=old_name)
            self._on_ai_progress(message)
            QMessageBox.information(
                self, self.i18n.text('Nome corrente mantenuto'), message)
            return old_name
        new_docs.mkdir(parents=True, exist_ok=True)
        for f in files:
            target = new_docs / f.name
            if target.exists():
                continue
            shutil.move(str(f), str(target))
        return new_name

    def _on_extracted(self, params: ExtractedParams) -> None:
        self._applying_extraction = True
        try:
            self._apply_extracted_result(params)
        except Exception as exc:
            self._auto_generation_params = None
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
        self.btn_photo_comparison.setEnabled(bool(self._photo_png or self._photo_overlay_png))
        if params.name and params.name != self._component_name():
            # Keep the output name and the active document directory consistent.
            # A naming collision must not discard a successful (paid) extraction.
            params.name = self._migrate_docs_to_new_name(params.name)
        if params.name:
            self.field_name.setText(params.name)
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

        if self._auto_after_extraction and params.is_castellated():
            self.group_3d.setChecked(True)
            self.field_body_height.setValue(params.body_height_mm)
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
            self.i18n.tr(
                "extraction.fillet_note", count=len(params.edge_fillets))
            if params.edge_fillets else ""
        )
        if self._auto_after_extraction:
            self._auto_generation_params = params
            self._on_ai_progress(f'Estrazione completata. Confidenza: {params.confidence}. {params.notes or ""}{fillet_note}')
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
        self._auto_generation_params = None
        self._auto_after_extraction = False
        self._hide_analysis_overlay()
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
            self.group_components.setVisible(checked and self._advanced_user())
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
            folder=self._project_dir(), rows=params.rows, cols=params.cols,
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
            folder=self._project_dir(), length=params.body_length,
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
        for field in ('body_length_mm', 'body_width_mm', 'body_height_mm'):
            value = getattr(params, field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f'Manca una dimensione valida del corpo 3D: {field}.')
        dimensions = ((params.body_length_mm, self.field_cast_body_length if params.is_castellated() else self.field_body_length),
                      (params.body_width_mm, self.field_cast_body_width if params.is_castellated() else self.field_body_width),
                      (params.body_height_mm, self.field_body_height))
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
        try:
            self._validate_automatic_input(extracted)
            body = self._current_body_params()
            if body is None:
                raise ValueError('I parametri del modello 3D non sono disponibili.')
            # QSpinBox limits must not silently replace extracted dimensions.
            if any(not math.isclose(actual, expected, abs_tol=.011) for actual, expected in (
                    (body.length_mm, extracted.body_length_mm), (body.width_mm, extracted.body_width_mm),
                    (body.height_mm, extracted.body_height_mm))):
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
            if automatic and body is None:
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
        self._worker.finished.connect(self._on_generation_finished)
        if automatic:
            self._automatic_generation_active = True
            self._automatic_footprint_done = self._automatic_3d_done = False
            self._automatic_generation_errors = []
            self._analysis_tabs_enabled = self.tabs.isEnabled()
            self.tabs.setEnabled(False)
            self._generation_menu_enabled = self.menuBar().isEnabled()
            self.menuBar().setEnabled(False)
            self.btn_extract_ai.setEnabled(False)
            self.analysis_overlay.start('Generazione footprint e modello 3D', 'Preparazione della generazione…')
        try:
            self._worker.start()
        except Exception as exc:
            self._on_error(str(exc))
            self._on_generation_finished()

    def _on_generation_finished(self):
        self.btn_generate.setEnabled(True)
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
        if self._automatic_generation_errors or not (self._automatic_footprint_done and self._automatic_3d_done):
            details = ('\n'.join(self._automatic_generation_errors)
                       or self.i18n.text('La generazione non ha prodotto entrambi i risultati.'))
            prefix = (self.i18n.text('Footprint generato, modello 3D non completato.') + '\n'
                      if self._automatic_footprint_done else '')
            QMessageBox.warning(
                self, self.i18n.text('Generazione incompleta'), prefix + details)
        else:
            self._on_ai_progress('Footprint e modello 3D generati. Verifica le anteprime prima dell’utilizzo.')

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
        self.label_3d_status.setText(
            self.i18n.tr("status.step_updated", path=path))
        self._refresh_output_actions()
        self._show_generated_mesh(Path(path))

    def _on_cad3d_error(self, message: str) -> None:
        if not self._cad_result_is_current():
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
        self.btn_terminal_detail = QPushButton("Rileva terminali dalla foto...")
        self.btn_terminal_detail.setToolTip(
            "Analisi AI della foto calibrata tramite OpenRouter; conserva posizioni e footprint.")
        self.btn_terminal_detail.clicked.connect(self._refine_photo_terminals)
        regen_row.addWidget(self.btn_terminal_detail)
        # User decision 2026-09-13 (MEMORY.md): hide manual AI refinement
        # actions, keeping handlers and the automatic extraction unchanged.
        self.btn_terminal_detail.hide()
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

    def closeEvent(self, event):
        # A QThread doing a network request must not be destroyed mid-request.
        if self._terminal_worker is not None and self._terminal_worker.isRunning():
            self.label_3d_status.setText(self.i18n.text(
                "Attendi la fine dell'analisi terminali prima di chiudere."))
            event.ignore()
            return
        super().closeEvent(event)

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
        self.label_3d_status.setText(
            self.i18n.text("Analisi dei terminali nella foto in corso..."))
        worker = TerminalDetailWorker(self._photo_png, components,
                                      self._vision_model(), only_leads=only_leads,
                                      system_prompt=profile['lead_prompt' if only_leads else 'terminal_prompt'])
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
        self.label_3d_status.setText(message)

    def _on_terminal_detail_done(self, components):
        worker = self._terminal_worker
        if (worker._model_revision != self._model_revision
                or worker._photo_snapshot != self._photo_png
                or worker.components != self._photo_components_from_table()):
            self._append_log("Dettaglio terminali scartato: i dati sono cambiati durante l'analisi.")
            self.label_3d_status.setText(self.i18n.text(
                "Dati modificati durante l'analisi: ripeti Rileva terminali."))
            return
        if getattr(worker, 'only_leads', False) and components == worker.components:
            self.label_3d_status.setText(self.i18n.text(
                "Nessun nuovo dettaglio dei reofori applicabile: geometria e anteprima conservate."))
            return
        self._set_photo_components(components)
        self._invalidate_generated_mesh()
        count = sum(bool(c.get('terminal_geometry')) for c in components)
        self.label_3d_status.setText(self.i18n.tr(
            "status.terminals_observed", count=count))
        if getattr(worker, 'only_leads', False):
            count = sum(1 for c in components for t in (c.get('terminal_geometry') or {}).get('terminals',[])
                        if t.get('lead_detail'))
            self.label_3d_status.setText(self.i18n.tr(
                "status.leads_observed", count=count))

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
            self.label_3d_status.setText(
                self.i18n.tr("status.parameter_error", error=exc))
            self._append_log(f"ERRORE 3D: {exc}")
            return

        self.btn_regen_3d.setEnabled(False)
        self.btn_regen_3d.setText(
            self.i18n.text("Generazione in corso (FreeCAD)..."))
        self.label_3d_status.setText(
            self.i18n.text("Generazione STEP in corso tramite FreeCAD..."))
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
        self._cad3d_worker.warning.connect(self._on_regen_3d_progress)
        self._cad3d_worker.warning.connect(self._on_cad3d_warning)
        self._cad3d_worker.error.connect(self._on_regen_3d_error)
        self._cad3d_worker.finished.connect(self._on_regen_3d_finished)
        # FreeCAD gira nel worker, quindi la GUI resta viva: lo spinner si
        # anima davvero e il tempo trascorso scorre. Le schede si bloccano
        # come nella generazione completa, per non cambiare i parametri
        # mentre il solido viene costruito su quelli vecchi.
        self._analysis_tabs_enabled = self.tabs.isEnabled()
        self.tabs.setEnabled(False)
        self.analysis_overlay.start('Generazione modello 3D',
                                    'Avvio di FreeCAD…')
        self._cad3d_worker.start()

    def _on_regen_3d_progress(self, message: str) -> None:
        """Porta sulla modale i messaggi che arrivano durante la generazione."""
        self.analysis_overlay.set_status(message)

    def _on_regen_3d_finished(self) -> None:
        self._hide_analysis_overlay()
        self.btn_regen_3d.setEnabled(True)
        self.btn_regen_3d.setText(self.i18n.text(
            "Rigenera modello 3D (STEP) con queste annotazioni"))

    def _on_regen_3d_done(self, path: str) -> None:
        if not self._cad_result_is_current():
            return
        self._append_log(f"OK (3D) -> {path}")
        self.label_3d_status.setText(
            self.i18n.tr("status.step_updated", path=path))
        self._refresh_output_actions()
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
                + self.i18n.text(
                    "Anteprima aggiornata con la geometria reale esportata (fillet e testo inclusi).")
            )
        except Exception as exc:  # noqa: BLE001 - anteprima non critica
            self._append_log(f"Anteprima mesh non disponibile: {exc}")

    def _on_regen_3d_error(self, message: str) -> None:
        if not self._cad_result_is_current():
            return
        self._append_log(f"ERRORE 3D: {message}")
        self.label_3d_status.setText(self.i18n.tr(
            "status.step_failed", error=message.splitlines()[0]))

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
        row_metadata.update(pin_count=component.pin_count, pins_by_side=component.pins_by_side,
                            _detected_kind=component.detected_kind,
                            terminal_geometry=component.terminal_geometry)
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
                pin_count=c.get("pin_count"), pins_by_side=c.get("pins_by_side"),
                detected_kind=c.get("_detected_kind"),
                terminal_geometry=deepcopy(c.get("terminal_geometry")),
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
                color_hex=table.cellWidget(row, 8).text().strip() or "#303030",
                marking=table.cellWidget(row, 9).text().strip(),
                pin_count=metadata.get("pin_count"),
                pins_by_side=metadata.get("pins_by_side"),
                detected_kind=metadata.get("_detected_kind"),
                terminal_geometry=metadata.get("terminal_geometry"),
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
        self._add_dim_h(left, right, bottom + 40, f"totale {body_l + 2 * margin:.2f} mm")
        self._add_dim_v(top, bottom, right + 40, f"totale {body_w + 2 * margin:.2f} mm")
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
                    self.label_3d_status.setText(self.i18n.text(
                        "Geometria reale del modulo (PCB con semi-fori sul bordo "
                        "e blocco dei componenti)."
                    ))
                    return
                except Exception as exc:  # noqa: BLE001 - anteprima best-effort
                    self._append_log(f"Anteprima mesh non disponibile: {exc}")
            self.preview_3d.clear_preview()
            self.label_3d_status.setText(self.i18n.text(
                "Attiva \"Modello 3D\" e premi \"Genera footprint\": il modello "
                "del modulo (PCB con semi-fori + componenti) si vede qui dopo la "
                "generazione con FreeCAD."
            ))
            return

        body = self._current_body_params()
        if body is None:
            self.preview_3d.clear_preview()
            self.label_3d_status.setText(self.i18n.text(
                "Attiva \"Modello 3D\" nel tab \"Footprint 2D\" per vedere l'anteprima qui."
            ))
            return

        try:
            params = self._current_params()
            params.validate()
            self.preview_3d.render_params(params, body)
        except ValueError as exc:
            self.preview_3d.clear_preview()
            self.label_3d_status.setText(
                self.i18n.tr("status.parameter_error", error=exc))
            return

        self.label_3d_status.setText(self.i18n.text(
            "Anteprima approssimata (poligoni, per farsi un'idea dell'ingombro). "
            "Premi \"Genera footprint\" per ottenere il modello .step esatto via FreeCAD."
        ))

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
    """@brief Provide the main operation for this workflow."""
    app = QApplication(sys.argv)
    app.setStyleSheet(APP_QSS)
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
