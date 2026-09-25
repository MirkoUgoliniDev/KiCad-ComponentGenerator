"""@brief Verify session-only photo detector controls without paid API calls."""

from copy import deepcopy
from pathlib import Path
import sys
from types import ModuleType

import pytest

from test_photo_inventory_gui import window
from kicadgen.gui import main_window as gui


def _fake_yolo(monkeypatch, validate):
    module = ModuleType("kicadgen.services.photo_yolo")
    module.validate_yolo_setup = validate
    monkeypatch.setitem(sys.modules, module.__name__, module)


def _unexpected(*args, **kwargs):
    pytest.fail("Unexpected extraction, network request or preference write")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """@brief Block network requests even if a regression starts extraction."""
    import socket
    monkeypatch.setattr(socket.socket, "connect", _unexpected)
    monkeypatch.setattr(gui, "extract_pin_header_params", _unexpected)


@pytest.mark.parametrize("options, detector, path", [
    ({}, "ai", None),
    ({"photo_detector": "ai"}, "ai", None),
    ({"photo_detector": "yolo", "yolo_model_path": Path("custom.onnx")},
     "yolo", Path("custom.onnx")),
    ({"photo_detector": "yolo"}, "yolo", None),
])
def test_worker_forwards_detector_and_preserves_ai_defaults(window, monkeypatch, options, detector, path):
    calls, results, errors = [], [], []
    result = object()

    def extract(doc_paths, **kwargs):
        calls.append((doc_paths, kwargs))
        return result

    monkeypatch.setattr(gui, "extract_pin_header_params", extract)
    worker = gui.ExtractWorker(
        [Path("board.png")], model="data/model", vision_model="photo/model",
        verification_model="configured/verifier", verification_prompt="verify", **options,
    )
    worker.done.connect(results.append)
    worker.error.connect(errors.append)
    worker.run()

    assert errors == []
    assert results == [result]
    assert len(calls) == 1
    assert calls[0][0] == [Path("board.png")]
    forwarded = calls[0][1]
    assert forwarded["photo_detector"] == detector
    assert forwarded["yolo_model_path"] == path
    assert forwarded["model"] == "data/model"
    assert forwarded["vision_model"] == "photo/model"
    assert forwarded["verification_model"] == "configured/verifier"
    assert forwarded["verification_prompt"] == "verify"


def test_selection_is_session_only_and_picker_can_cancel(window, monkeypatch, tmp_path):
    before = deepcopy(window._config)
    profile = window.profile_store.defaults()
    monkeypatch.setattr(gui, "save_config", _unexpected)
    assert window._photo_detector_options() == ("yolo", None)
    assert window.field_yolo_model.isEnabled()
    assert window.btn_pick_yolo_model.isEnabled()
    assert window.group_ai_tools.isAncestorOf(window.combo_photo_detector)

    window.combo_photo_detector.setCurrentIndex(window.combo_photo_detector.findData("yolo"))
    assert window._photo_detector_options() == ("yolo", None)
    assert window.field_yolo_model.isEnabled()
    path = tmp_path / "custom weights.onnx"
    dialogs = []

    def pick(*args):
        dialogs.append(args)
        return str(path), ""

    monkeypatch.setattr(gui.QFileDialog, "getOpenFileName", pick)
    window.btn_pick_yolo_model.click()
    assert "*.onnx" in dialogs[0][3]
    assert window._photo_detector_options() == ("yolo", path)
    monkeypatch.setattr(gui.QFileDialog, "getOpenFileName", lambda *args: ("", ""))
    window.btn_pick_yolo_model.click()
    assert window._photo_detector_options() == ("yolo", path)

    window.combo_photo_detector.setCurrentIndex(0)
    assert window._photo_detector_options() == ("ai", None)
    assert window.field_yolo_model.text() == str(path)
    assert not window.btn_pick_yolo_model.isEnabled()
    window.combo_photo_detector.setCurrentIndex(1)
    assert window._photo_detector_options() == ("yolo", path)
    window.field_yolo_model.clear()
    assert window._photo_detector_options() == ("yolo", None)
    assert window._config == before
    assert window.profile_store.defaults() == profile


def test_new_window_defaults_to_yolo_without_restoring_custom_session_path(window, monkeypatch, tmp_path):
    default_path = tmp_path / "models" / "pcb_yolov8s.onnx"
    default_path.parent.mkdir()
    default_path.touch()
    # Contratto invariato, meccanismo cambiato (2026-09-25): la cartella del
    # programma arriva da `app_paths.resource_path` (repository da sorgente,
    # pacchetto installato nell'eseguibile), non piu' da APP_DIR.
    monkeypatch.setattr(gui, "resource_path", lambda *parts: tmp_path.joinpath(*parts))
    window.combo_photo_detector.setCurrentIndex(0)
    window.field_yolo_model.setText("session.onnx")

    fresh = gui.MainWindow()
    try:
        assert fresh._photo_detector_options() == ("yolo", None)
        assert fresh.field_yolo_model.text() == ""
        assert str(default_path) in fresh.field_yolo_model.placeholderText()
    finally:
        fresh.close()


@pytest.mark.parametrize("language,label,yolo,credit", [
    ("it", "Rilevamento foto:", "YOLO locale + verifica AI", "credito AI"),
    ("en", "Photo detection:", "Local YOLO + AI verification", "AI credit"),
    ("fr", "Détection photo :", "YOLO local + vérification IA", "crédit IA"),
    ("de", "Fotoerkennung:", "Lokales YOLO + KI-Prüfung", "KI-Guthaben"),
])
def test_language_switch_preserves_selection_and_localizes_all_controls(
    window, language, label, yolo, credit,
):
    window.combo_photo_detector.setCurrentIndex(1)
    window.field_yolo_model.setText("session.onnx")
    # Visit a different language first, so each case exercises a real switch.
    for code in ("de" if language != "de" else "it", language):
        window.language_combo.setCurrentIndex(window.language_combo.findData(code))
    assert window.label_photo_detector.text() == label
    assert window.combo_photo_detector.currentText() == yolo
    assert credit in window.combo_photo_detector.toolTip()
    assert "KiCad" in window.combo_photo_detector.toolTip()
    assert window._photo_detector_options() == ("yolo", Path("session.onnx"))
    for key, translations in gui._PHOTO_DETECTOR_TRANSLATIONS.items():
        assert set(translations) == {"it", "en", "fr", "de"}
        assert all(translations.values())
        # Segnaposto di tutti i testi della sezione; dal 25/09 anche quelli del
        # riepilogo «Parametri YOLO» (soglia, doppioni, tessere).
        fields = dict(path="weights.onnx", error="test", confidence=0.25, nms_iou=0.5,
                      tiles="640 + 1280 px")
        assert window._photo_detector_text(key, **fields) == translations[language].format(**fields)
    assert window.btn_pick_yolo_model.accessibleName() == window._photo_detector_text("picker")
    assert not any(row["key"].startswith("photo_detector.")
                   for row in window.profile_store.list_translations(language))


@pytest.mark.parametrize("path_text", ["", "selected.onnx"])
def test_preflight_happens_before_worker_and_busy_controls_restore(window, monkeypatch, path_text):
    events = []
    _fake_yolo(monkeypatch, lambda path: events.append(("validate", path)))
    monkeypatch.setattr(window, "_selected_documents", lambda: ([Path("board.png")], None))
    monkeypatch.setattr(gui.ExtractWorker, "start", lambda self: events.append(("start", self)))
    window.combo_photo_detector.setCurrentIndex(1)
    window.field_yolo_model.setText(path_text)
    window._session_model = "configured/model"

    window._extract_with_ai()

    path = Path(path_text) if path_text else None
    worker = window._extract_worker
    assert events == [("validate", path), ("start", worker)]
    assert worker.photo_detector == "yolo"
    assert worker.yolo_model_path == path
    # La chiamata che GUARDA la fotografia -- qui la riconciliazione delle
    # proposte locali -- e' una chiamata sola e puo' andare a un modello
    # diverso da quello che legge i documenti. Ma finche' nessuno ne sceglie
    # uno, segue il modello configurato: all'avvio la finestra non impone un
    # modello suo, altrimenti la chiamata a pagamento finisce a un modello che
    # l'utente non ha scelto e non vede scritto da nessuna parte.
    assert window._vlm_model is None
    assert worker.model == worker.vision_model == "configured/model"
    assert worker.verification_model == "configured/model"
    for control in (window.combo_photo_detector, window.field_yolo_model, window.btn_pick_yolo_model):
        assert not control.isEnabled()
    window._on_extract_finished()
    for control in (window.combo_photo_detector, window.field_yolo_model, window.btn_pick_yolo_model):
        assert control.isEnabled()


def test_explicit_ai_selection_skips_optional_yolo_preflight(window, monkeypatch):
    window.combo_photo_detector.setCurrentIndex(window.combo_photo_detector.findData("ai"))
    _fake_yolo(monkeypatch, _unexpected)
    monkeypatch.setattr(window, "_selected_documents", lambda: ([Path("board.png")], None))
    monkeypatch.setattr(gui.ExtractWorker, "start", lambda self: None)
    window.field_yolo_model.setText("unused.onnx")
    window._extract_with_ai()
    assert window._extract_worker.photo_detector == "ai"
    assert window._extract_worker.yolo_model_path is None
    window._on_extract_finished()
    assert window.combo_photo_detector.isEnabled()
    assert not window.field_yolo_model.isEnabled()
    assert not window.btn_pick_yolo_model.isEnabled()


@pytest.mark.parametrize("failure", ["missing weights", "invalid ONNX", "missing onnxruntime", "import"])
def test_preflight_error_preserves_results_and_never_starts_api(window, monkeypatch, failure):
    validations, warnings = [], []

    def validate(path):
        validations.append(path)
        raise RuntimeError(failure)

    if failure == "import":
        monkeypatch.setitem(sys.modules, "kicadgen.services.photo_yolo", None)
    else:
        _fake_yolo(monkeypatch, validate)
    monkeypatch.setattr(gui.QMessageBox, "warning", lambda *args: warnings.append(args))
    monkeypatch.setattr(gui.ExtractWorker, "start", _unexpected)
    monkeypatch.setattr(window, "_selected_documents", _unexpected)
    monkeypatch.setattr(window.profile_store, "snapshot", _unexpected)
    monkeypatch.setattr(window.analysis_overlay, "clear_cost", _unexpected)
    window.combo_photo_detector.setCurrentIndex(1)
    window.field_yolo_model.setText("broken.onnx")
    previous = object()
    window._last_params = previous
    window._last_mesh_path = Path("previous.preview.json")
    window._photo_png = b"previous photo"
    window._photo_overlay_png = b"previous overlay"
    window._photo_source = "previous source"
    window.ai_progress_text.setPlainText("previous log")
    window.ai_elements_list.addItem("previous detection")
    window.btn_photo_comparison.setEnabled(True)
    revision = window._model_revision

    window._extract_with_ai()

    assert validations == ([] if failure == "import" else [Path("broken.onnx")])
    assert len(warnings) == 1
    assert warnings[0][1] == window._photo_detector_text("error_title")
    assert "Nessuna richiesta AI" in warnings[0][2]
    assert window._extract_worker is None
    assert window._last_params is previous
    assert window._last_mesh_path == Path("previous.preview.json")
    assert window._photo_png == b"previous photo"
    assert window._photo_overlay_png == b"previous overlay"
    assert window._photo_source == "previous source"
    assert window._model_revision == revision
    assert window.ai_progress_text.toPlainText() == "previous log"
    assert window.ai_elements_list.item(0).text() == "previous detection"
    assert window.btn_photo_comparison.isEnabled()
    assert window.btn_extract_ai.isEnabled()
    assert window.tabs.isEnabled()


@pytest.mark.parametrize("evidence", [
    {"pin_count_confidence": "high", "pin_count_source": "photo_observation"},
    {"pin_count_confidence": "low", "pin_count_source": "photo_observation"},
    {},
    {"pin_count_confidence": None, "pin_count_source": None},
])
def test_pin_count_evidence_survives_photo_and_table_roundtrip(window, evidence):
    """@brief Preserve count evidence without promoting absent or uncertain evidence."""
    source = [dict(
        name="Observed body", kind="generic", x_mm=2, y_mm=3,
        length_mm=4, width_mm=2, height_mm=1, pin_count=6,
        pins_by_side={"left": 3, "right": 3}, **evidence,
    )]
    before = deepcopy(source)
    expected = {key: evidence.get(key) or ""
                for key in ("pin_count_confidence", "pin_count_source")}
    window._set_photo_components(source)
    for key, value in expected.items():
        assert getattr(window.castellated_components[0], key) == value

    # Name/position edits and rebuilding without the original photo dictionary
    # must preserve the observed-count evidence carried by ComponentBox.
    window.table_components.cellWidget(0, 0).setText("Edited body")
    window.table_components.cellWidget(0, 2).setValue(5)
    window.castellated_components = window._components_from_table()
    window._refresh_components_table()
    component, = window._components_from_table()
    photo, = window._photo_components_from_table()
    assert component.name == "Edited body" and component.x == 5
    assert component.pin_count == photo["pin_count"] == 6
    assert component.pins_by_side == photo["pins_by_side"] == {"left": 3, "right": 3}
    for key, value in expected.items():
        assert getattr(component, key) == photo[key] == value
    window._apply_photo_review([photo])
    reviewed, = window._components_from_table()
    for key, value in expected.items():
        assert getattr(reviewed, key) == value
    assert source == before
    assert window._photo_detector_options() == ("yolo", None)


def test_pin_count_evidence_stays_with_surviving_row_and_new_rows_are_empty(window):
    """@brief Keep count evidence attached to its row through deletion and addition."""
    window.table_components.setRowCount(0)
    for count, confidence, source in ((2, "low", "estimate"), (8, "high", "photo_observation")):
        window._add_component_row(gui.ComponentBox(
            x=0, y=0, length=4, width=2, height=1, pin_count=count,
            pin_count_confidence=confidence, pin_count_source=source,
        ))
    window.table_components.setCurrentCell(0, 0)
    window._remove_component_row()
    window._add_component_row()
    survivor, added = window._components_from_table()
    photos = window._photo_components_from_table()
    assert survivor.pin_count == 8
    assert survivor.pin_count_confidence == photos[0]["pin_count_confidence"] == "high"
    assert survivor.pin_count_source == photos[0]["pin_count_source"] == "photo_observation"
    assert added.pin_count is None
    assert added.pin_count_confidence == photos[1]["pin_count_confidence"] == ""
    assert added.pin_count_source == photos[1]["pin_count_source"] == ""


@pytest.mark.parametrize("override", [None, 0.0, 90.0, -90.0, 37.5])
@pytest.mark.parametrize("z_offset", [0.0, -0.7, 1.25])
def test_local_model_overrides_survive_gui_roundtrip_independently_of_photo_rotation(window, override, z_offset):
    """@brief Preserve local CAD rotation and Z offsets independently of photo geometry."""
    source = dict(name="Reviewed body", kind="generic", x_mm=1, y_mm=2,
                  length_mm=4, width_mm=2, height_mm=1, rotation_deg=30)
    if override is not None:
        source["model_rotation_deg"] = override
    if z_offset:
        source["model_z_offset_mm"] = z_offset
    before = deepcopy(source)
    window._set_photo_components([source])
    assert window.castellated_components[0].model_rotation_deg == override
    assert window.castellated_components[0].model_z_offset_mm == z_offset

    rotation = window.table_components.cellWidget(0, 7)
    rotation.setCurrentIndex(rotation.findData(90.0))
    component, = window._components_from_table()
    assert component.rotation == 90
    assert component.model_rotation_deg == override
    assert component.model_z_offset_mm == z_offset
    window.castellated_components = [component]
    window._refresh_components_table()
    photo, = window._photo_components_from_table()
    assert photo["rotation_deg"] == 90
    assert photo.get("model_rotation_deg") == override
    assert photo.get("model_z_offset_mm", 0.0) == z_offset
    if not z_offset:
        assert "model_z_offset_mm" not in photo
    if override is None:
        assert "model_rotation_deg" not in photo
    window._apply_photo_review([photo])
    result, = window._current_castellated_params().components
    assert result.rotation == 90
    assert result.model_rotation_deg == override
    assert result.model_z_offset_mm == z_offset
    assert source == before


def test_direct_model_overrides_stay_with_their_row(window):
    """@brief Preserve dataclass-only overrides and prevent inheritance by new rows."""
    window.table_components.setRowCount(0)
    for override, z_offset in ((90.0, 1.25), (-90.0, -0.7)):
        window._add_component_row(gui.ComponentBox(
            x=0, y=0, length=4, width=2, height=1,
            model_rotation_deg=override, model_z_offset_mm=z_offset,
        ))
    window.table_components.setCurrentCell(0, 0)
    window._remove_component_row()
    window._add_component_row()
    survivor, added = window._components_from_table()
    photos = window._photo_components_from_table()
    assert survivor.model_rotation_deg == photos[0]["model_rotation_deg"] == -90.0
    assert survivor.model_z_offset_mm == photos[0]["model_z_offset_mm"] == -0.7
    assert added.model_rotation_deg is None
    assert added.model_z_offset_mm == 0.0
    assert "model_rotation_deg" not in photos[1]
    assert "model_z_offset_mm" not in photos[1]


@pytest.mark.parametrize("kind", ["usb_connector", "generic"])
@pytest.mark.parametrize("x,y", [(-9.5, 0), (9.5, 0), (0, -4.5), (0, 4.5)])
def test_gui_preserves_full_component_body_overhang_at_each_board_edge(window, kind, x, y):
    """@brief Pass calibrated overhanging bodies to CAD without moving or clipping them."""
    window.group_castellated.setChecked(True)
    window.field_cast_body_length.setValue(20)
    window.field_cast_body_width.setValue(10)
    source = dict(name="Edge body", kind=kind, x_mm=x, y_mm=y,
                  length_mm=4, width_mm=2, height_mm=1, rotation_deg=37,
                  mating="edge", bbox_basis="body", _box=(0, 0, 1, 1))
    window._set_photo_components([source])
    photo, = window._photo_components_from_table()
    left, top, right, bottom = photo["_box"]
    assert left < 0 or top < 0 or right > 1 or bottom > 1
    window._apply_photo_review([photo])
    window.castellated_components = window._components_from_table()
    window._refresh_components_table()
    result, = window._current_castellated_params().components
    assert (result.x, result.y) == (x, y)
    assert (result.length, result.width, result.height) == (4, 2, 1)
    assert result.rotation == 37
    assert result.mating == "edge"
    assert result.bbox_basis == "body"
    assert window._photo_detector_options() == ("yolo", None)


def test_without_a_vision_model_the_photo_call_falls_back_to_the_configured_one(window, monkeypatch):
    """@brief Caso negativo: svuotare il campo non deve lasciare la chiamata senza modello."""
    _fake_yolo(monkeypatch, lambda path: None)
    monkeypatch.setattr(window, "_selected_documents", lambda: ([Path("board.png")], None))
    monkeypatch.setattr(gui.ExtractWorker, "start", lambda self: None)
    window.combo_photo_detector.setCurrentIndex(window.combo_photo_detector.findData("yolo"))
    window._session_model = "configured/model"
    window._vlm_model = ""
    window._extract_with_ai()
    worker = window._extract_worker
    assert worker.model == worker.vision_model == worker.verification_model == "configured/model"


def test_at_boot_the_photo_call_follows_the_configured_model(window, monkeypatch):
    """@brief Caso positivo e negativo sul modello che guarda la fotografia.

    All'avvio la finestra non deve imporre un modello proprio: la chiamata a
    pagamento sulla fotografia segue quello configurato per l'estrazione.
    Prima il valore iniziale era una costante scritta nella finestra, e chi
    apriva l'app trovava gia' scelto un modello che non aveva configurato --
    la riconciliazione ci finiva senza che nulla lo dicesse.

    Scegliere un modello dal pannello resta possibile e vince: e' il caso in
    cui si vuole spendere meno sulla fotografia che sui datasheet.
    """
    _fake_yolo(monkeypatch, lambda path: None)
    monkeypatch.setattr(window, "_selected_documents", lambda: ([Path("board.png")], None))
    monkeypatch.setattr(gui.ExtractWorker, "start", lambda self: None)
    window.combo_photo_detector.setCurrentIndex(window.combo_photo_detector.findData("yolo"))
    window._session_model = "configured/model"

    # Appena aperta: nessuna scelta di sessione, e il campo mostra quello che
    # verra' davvero usato.
    assert window._vlm_model is None
    assert window._photo_detector_model() == "configured/model"
    window._extract_with_ai()
    assert window._extract_worker.verification_model == "configured/model"

    # Scelta esplicita dal pannello: vince, e resta solo per la sessione.
    window._vlm_model = "cheap/vision-model"
    assert window._photo_detector_model() == "cheap/vision-model"
    window._extract_with_ai()
    assert window._extract_worker.verification_model == "cheap/vision-model"


def test_the_photo_call_model_is_always_written_in_the_log(window, monkeypatch):
    """@brief Quale modello riceve la fotografia va detto prima di partire.

    Veniva scritto solo quando differiva dal modello principale: con i due
    uguali il log taceva, e non c'era modo di sapere dal log dove fosse
    finita la chiamata a pagamento se non dalla fattura.
    """
    _fake_yolo(monkeypatch, lambda path: None)
    monkeypatch.setattr(window, "_selected_documents", lambda: ([Path("board.png")], None))
    monkeypatch.setattr(gui.ExtractWorker, "start", lambda self: None)
    window.combo_photo_detector.setCurrentIndex(window.combo_photo_detector.findData("yolo"))
    window._session_model = "configured/model"
    lines = []
    monkeypatch.setattr(window, "_on_ai_progress", lines.append)

    window._extract_with_ai()
    assert any("configured/model" in line and "fotografia" in line for line in lines), lines

    lines.clear()
    window._vlm_model = "cheap/vision-model"
    window._extract_with_ai()
    assert any("cheap/vision-model" in line and "sessione" in line for line in lines), lines


def test_the_yolo_browse_command_is_an_icon_with_a_tooltip(window):  # noqa: F811
    """@brief Utente, 2026-09-24: «Sfoglia» come icona con testo al passaggio del mouse.

    Positivo: nessun testo sul pulsante, icona presente, tooltip con il nome
    «Sfoglia…» in prima riga e la spiegazione sotto. Negativo: il nome
    accessibile resta quello del dialogo, non la parola «Sfoglia».
    """
    button = window.btn_pick_yolo_model
    assert button.text() == "" and not button.icon().isNull()
    lines = button.toolTip().splitlines()
    assert lines[0] == window._photo_detector_text("browse")
    assert lines[1] == window._photo_detector_text("picker")
    assert button.accessibleName() == window._photo_detector_text("picker")
