"""@brief Verify session-only photo detector controls without paid API calls."""

from copy import deepcopy
from pathlib import Path
import sys
from types import ModuleType

import pytest
from PyQt6.QtWidgets import QDialog

from kicadgen.gui import main_window as gui



#: Rilevatore scelto all'avvio dai sorgenti (utente, 2026-10-02).
STARTUP_DETECTOR = "yolo_smd"

def _fake_yolo(monkeypatch, validate):
    module = ModuleType("kicadgen.services.photo_yolo")
    module.validate_yolo_setup = validate
    monkeypatch.setitem(sys.modules, module.__name__, module)


def _unexpected(*args, **kwargs):
    pytest.fail("Unexpected extraction, network request or preference write")



def _rebuild_from_boxes(window):
    """@brief Rebuild the stored photo components from their CAD boxes only.

    The photo dictionaries are dropped: what survives is what `ComponentBox`
    carries. (It used to be the rebuild of the removed components table.)
    """
    window._photo_rows = [window._photo_row(box, None)
                          for box in window._components_from_table()]

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
    ({"photo_detector": "yolo_smd"}, "yolo_smd", None),
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
    assert window._photo_detector_options()[0] == STARTUP_DETECTOR
    assert window.field_yolo_model.isEnabled()
    assert window.btn_pick_yolo_model.isEnabled()
    assert window.group_ai_tools.isAncestorOf(window.combo_photo_detector)

    window.combo_photo_detector.setCurrentIndex(window.combo_photo_detector.findData("yolo"))
    assert window._photo_detector_options() == ("yolo", None)
    assert window.field_yolo_model.isEnabled()
    path = tmp_path / "custom weights.onnx"
    # Dal 2026-09-26 il pulsante apre «Scegli il modello YOLO» al posto del
    # selettore di file: stesso contratto, scelta di sessione e annullabile.
    from kicadgen.gui import yolo_model_dialog
    dialogs = []

    def choose(self):
        dialogs.append(self)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(yolo_model_dialog.YoloModelDialog, "exec", choose)
    monkeypatch.setattr(yolo_model_dialog.YoloModelDialog, "selected_path",
                        lambda self: str(path))
    window.btn_pick_yolo_model.click()
    assert len(dialogs) == 1
    assert window._photo_detector_options() == ("yolo", path)
    monkeypatch.setattr(yolo_model_dialog.YoloModelDialog, "exec",
                        lambda self: QDialog.DialogCode.Rejected)
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


def test_new_window_defaults_to_smd_network_without_restoring_custom_session_path(
        window, monkeypatch, tmp_path):
    """@brief Dai sorgenti si parte dalla rete SMD locale (utente, 2026-10-02).

    @note Il percorso scelto nella sessione precedente non torna: la finestra
        nuova usa i pesi YOLO verificati per la rete SMD.
    """
    default_path = tmp_path / "models" / "yolo" / "pcb_component_detector_yolov8m_best.pt"
    default_path.parent.mkdir(parents=True)
    default_path.touch()
    # Since 2026-10-02 the detectors live in models/yolo (app_paths.yolo_model_path).
    monkeypatch.setattr(gui, "yolo_weights_path",
                        lambda name: tmp_path.joinpath("models", "yolo", name))
    # CONTRACT CHANGED 2026-10-29 (review P3): the 'yolo_smd' default comes
    # from app_paths.default_yolo_smd_model(), so its folder is redirected too.
    from kicadgen import app_paths
    monkeypatch.setattr(app_paths, "yolo_model_path",
                        lambda name: tmp_path.joinpath("models", "yolo", name))
    window.combo_photo_detector.setCurrentIndex(0)
    window.field_yolo_model.setText("session.onnx")

    fresh = gui.MainWindow()
    try:
        assert fresh._photo_detector_options() == ("yolo_smd", default_path)
        assert fresh.field_yolo_model.text() == ""
        assert str(default_path) in fresh.field_yolo_model.placeholderText()
    finally:
        fresh.close()


def _frozen_without_network(monkeypatch, tmp_path):
    """A frozen program whose models/smd holds no network (installation built without weights)."""
    monkeypatch.setattr(gui, "is_frozen", lambda: True)
    monkeypatch.setattr(gui, "smd_weights_path", lambda name=None: tmp_path / "smd" / "smd_classifier.onnx")


def test_frozen_window_defaults_to_yolo_when_the_smd_network_is_absent(monkeypatch, tmp_path):
    """@brief Negative: a frozen program WITHOUT the network's file falls back to YOLO."""
    _frozen_without_network(monkeypatch, tmp_path)
    fresh = gui.MainWindow()
    try:
        assert fresh._photo_detector_options() == ("yolo", None)
    finally:
        fresh.close()


def test_frozen_window_keeps_the_smd_route_when_its_onnx_network_is_installed(monkeypatch, tmp_path):
    """@brief Positive (2026-10-28): the installed program ships models/smd/smd_classifier.onnx -> route on and default.

    The rule is the presence of the file, not the packaging: the same
    frozen program answers differently with and without the network.
    """
    network = tmp_path / "smd" / "smd_classifier.onnx"
    network.parent.mkdir()
    network.write_bytes(b"graph")
    monkeypatch.setattr(gui, "is_frozen", lambda: True)
    monkeypatch.setattr(gui, "smd_weights_path", lambda name=None: network)
    assert gui.smd_route_available()
    fresh = gui.MainWindow()
    try:
        assert fresh._photo_detector_options()[0] == "yolo_smd"
        index = fresh.combo_photo_detector.findData("yolo_smd")
        assert fresh.combo_photo_detector.model().item(index).isEnabled()
    finally:
        fresh.close()


def test_smd_mode_selects_the_verified_yolo_and_preserves_manual_override(window, tmp_path):
    window.combo_photo_detector.setCurrentIndex(window.combo_photo_detector.findData('yolo_smd'))
    detector, path = window._photo_detector_options()
    assert detector == 'yolo_smd'
    # The checkpoint when it is there (developer's machine), else its ONNX
    # export (installed program), with neither the checkpoint's name (GitHub
    # runner); test_the_smd_route_falls_back_... checks the rule itself.
    assert path.name == _smd_route_default_name()
    assert path.name in window.field_yolo_model.placeholderText()
    assert window.btn_pick_yolo_model.isEnabled()
    assert 'SMD' in window.combo_photo_detector.currentText()
    custom = tmp_path / 'custom.pt'
    window.field_yolo_model.setText(str(custom))
    assert window._photo_detector_options() == ('yolo_smd', custom)


def test_frozen_build_keeps_saved_smd_mode_visible_but_unavailable_before_ai(
    window, monkeypatch, tmp_path,
):
    _frozen_without_network(monkeypatch, tmp_path)
    index = window.combo_photo_detector.findData("yolo_smd")
    window.combo_photo_detector.setCurrentIndex(index)
    window._refresh_photo_detector_controls()

    assert window.combo_photo_detector.currentData() == "yolo_smd"
    assert not window.combo_photo_detector.model().item(index).isEnabled()
    assert window.combo_photo_detector.itemData(index, gui.Qt.ItemDataRole.ToolTipRole) == \
        window._photo_detector_text("smd_unavailable")
    assert window._photo_detector_options()[0] == "yolo_smd"

    warnings = []
    monkeypatch.setattr(gui.QMessageBox, "warning", lambda *args: warnings.append(args))
    _fake_yolo(monkeypatch, lambda path: pytest.fail("frozen SMD gate ran too late"))
    monkeypatch.setattr(window, "_selected_documents", _unexpected)
    monkeypatch.setattr(window, "_ensure_api_key", _unexpected)
    window._extract_with_ai()
    assert len(warnings) == 1
    assert warnings[0][2] == window._photo_detector_text("smd_unavailable")
    assert window.combo_photo_detector.currentData() == "yolo_smd"


def test_missing_smd_network_stops_before_paid_extraction(window, monkeypatch):
    from kicadgen.services import smd_component_classifier as smd
    events, warnings = [], []
    window.combo_photo_detector.setCurrentIndex(window.combo_photo_detector.findData('yolo_smd'))
    _fake_yolo(monkeypatch, lambda path: events.append('yolo'))
    def missing(path=None):
        events.append('smd')
        raise FileNotFoundError('smd_classifier.pt non trovato')
    monkeypatch.setattr(smd, 'validate_smd_setup', missing)
    monkeypatch.setattr(gui.QMessageBox, 'warning', lambda *args: warnings.append(args))
    monkeypatch.setattr(window, '_selected_documents', _unexpected)
    monkeypatch.setattr(window, '_ensure_api_key', _unexpected)
    window._extract_with_ai()
    assert events == ['yolo', 'smd']
    assert len(warnings) == 1
    assert 'smd_classifier.pt' in warnings[0][2]
    assert window._extract_worker is None


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
    # Back in use after the short pause that lets queued clicks find it
    # disabled (2026-10-02, «Preparazione…»).
    from PyQt6.QtTest import QTest
    QTest.qWait(gui.EXTRACT_REENABLE_DELAY_MS + 100)
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
    window._photo_rows[0].update(name="Edited body", x_mm=5)
    _rebuild_from_boxes(window)
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
    assert window._photo_detector_options()[0] == STARTUP_DETECTOR


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

    window._photo_rows[0]["rotation_deg"] = 90.0
    component, = window._components_from_table()
    assert component.rotation == 90
    assert component.model_rotation_deg == override
    assert component.model_z_offset_mm == z_offset
    _rebuild_from_boxes(window)
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
    _rebuild_from_boxes(window)
    result, = window._current_castellated_params().components
    assert (result.x, result.y) == (x, y)
    assert (result.length, result.width, result.height) == (4, 2, 1)
    assert result.rotation == 37
    assert result.mating == "edge"
    assert result.bbox_basis == "body"
    assert window._photo_detector_options()[0] == STARTUP_DETECTOR


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


@pytest.mark.parametrize("detector, visible", [("yolo_smd", True), ("yolo", False), ("ai", False)])
def test_the_smd_model_row_shows_only_with_the_smd_network(window, detector, visible):
    """@brief «Modello rete SMD» (user, 2026-10-02) appears like «Modello YOLO», only for its mode."""
    window.show()
    window.project_wizard.set_page(1)
    window.group_ai_tools.setExpanded(True)
    window.combo_photo_detector.setCurrentIndex(window.combo_photo_detector.findData(detector))
    window._on_photo_detector_changed()
    assert window.field_smd_model.isVisibleTo(window) is visible
    assert window.btn_pick_smd_model.isVisibleTo(window) is visible
    assert str(gui.smd_weights_path()) in window.field_smd_model.placeholderText()
    assert "smd" in str(gui.smd_weights_path().parent).lower()


def test_the_chosen_smd_network_is_checked_and_forwarded(window, monkeypatch, tmp_path):
    """@brief The session file goes to the check before paying and to the worker.

    @note Negative: an empty field means the default (None is forwarded).
    """
    from kicadgen.services import smd_component_classifier as smd
    checked = []
    monkeypatch.setattr(smd, "validate_smd_setup", lambda path=None: checked.append(path))
    _fake_yolo(monkeypatch, lambda path: None)
    monkeypatch.setattr(window, "_selected_documents", lambda: ([Path("board.png")], None))
    monkeypatch.setattr(window, "_ensure_api_key", lambda: True)
    monkeypatch.setattr(gui.ExtractWorker, "start", lambda self: None)
    window.combo_photo_detector.setCurrentIndex(window.combo_photo_detector.findData("yolo_smd"))
    chosen = tmp_path / "other_network.pt"
    window.field_smd_model.setText(str(chosen))
    window._extract_with_ai()
    assert checked == [chosen]
    assert window._extract_worker.smd_model_path == chosen
    window._on_extract_finished()
    window.field_smd_model.clear()
    checked.clear()
    window._extract_with_ai()
    assert checked == [None] and window._extract_worker.smd_model_path is None
    window._on_extract_finished()


@pytest.mark.parametrize("picked, expected", [("default", ""), ("other", "other")])
def test_the_smd_button_opens_the_model_window_on_the_smd_folder(window, monkeypatch, tmp_path,
                                                                 picked, expected):
    """@brief A window with the model's information (2026-10-02), not a bare file dialog.

    @note Picking the default leaves the field empty (it follows models/smd);
        another file is written in the field.
    """
    from kicadgen.gui import smd_model_dialog
    other = tmp_path / "other.pt"
    seen = {}

    class Stub:
        def __init__(self, current, models_dir, default="", tr=None, parent=None):
            seen.update(current=current, folder=models_dir, default=default)

        def exec(self):
            return gui.QDialog.DialogCode.Accepted

        def selected_path(self):
            return seen["default"] if picked == "default" else str(other)

        def shutdown(self):
            pass

        def deleteLater(self):
            pass
    monkeypatch.setattr(smd_model_dialog, "SmdModelDialog", Stub)
    window._pick_session_smd_model()
    assert seen["folder"] == gui.resource_path(*gui.SMD_MODELS_DIR)
    assert seen["default"] == str(gui.smd_weights_path())
    assert window.field_smd_model.text() == ("" if expected == "" else str(other))


def _smd_route_default_name() -> str:
    """The YOLO weights the SMD route uses on this machine: the checkpoint, else its ONNX export.

    CONTRACT CHANGED 2026-10-29 (review P3): with neither file (the GitHub
    runner) the shared rule app_paths.default_yolo_smd_model() names the
    checkpoint, the file the analysis checks and reports as missing.
    """
    if gui.yolo_weights_path(gui.DEFAULT_YOLO_PT).is_file():
        return gui.DEFAULT_YOLO_PT
    if gui.yolo_weights_path(gui.DEFAULT_YOLO_SMD_ONNX).is_file():
        return gui.DEFAULT_YOLO_SMD_ONNX
    return gui.DEFAULT_YOLO_PT


def test_the_smd_route_falls_back_to_the_onnx_export_of_its_detector(monkeypatch, tmp_path):
    """@brief 2026-10-28: without the .pt (installed program) the route runs the ONNX export of the same model.

    Positive: the checkpoint present -> the checkpoint. Negative: absent ->
    the ONNX export, never the base ONNX detector of the other routes.
    """
    monkeypatch.delenv("KICADGEN_YOLO_MODEL", raising=False)
    monkeypatch.setattr(gui, "yolo_weights_path", lambda name: tmp_path / "yolo" / name)
    # CONTRACT CHANGED 2026-10-29 (review P3): the 'yolo_smd' choice now lives
    # in app_paths.default_yolo_smd_model(), shared with the analysis, so the
    # folder is redirected where that function looks.
    from kicadgen import app_paths
    monkeypatch.setattr(app_paths, "yolo_model_path", lambda name: tmp_path / "yolo" / name)
    (tmp_path / "yolo").mkdir()
    # With neither file the shared rule names the checkpoint, the file the
    # analysis checks and reports as missing (before, the field named the
    # export while the run looked for the checkpoint).
    assert gui.MainWindow._default_yolo_weights("yolo_smd").name == gui.DEFAULT_YOLO_PT
    (tmp_path / "yolo" / gui.DEFAULT_YOLO_SMD_ONNX).write_bytes(b"export")
    assert gui.MainWindow._default_yolo_weights("yolo_smd").name == gui.DEFAULT_YOLO_SMD_ONNX
    assert gui.MainWindow._default_yolo_weights("yolo").name == gui.DEFAULT_YOLO_ONNX
    (tmp_path / "yolo" / gui.DEFAULT_YOLO_PT).write_bytes(b"checkpoint")
    assert gui.MainWindow._default_yolo_weights("yolo_smd").name == gui.DEFAULT_YOLO_PT


@pytest.mark.parametrize("detector, default_name", [
    ("yolo_smd", None),
    ("yolo", Path(gui.DEFAULT_YOLO_ONNX).name),
])
def test_the_yolo_picker_proposes_the_default_of_the_chosen_detector(
        window, monkeypatch, detector, default_name):
    """@brief Review B2 (2026-10-02): the SMD route runs its own weights, not the base ONNX file.

    @note Confirming the default leaves the field empty, so the detector's
        own default keeps applying; the other detector is the negative case.
    """
    from kicadgen.gui import yolo_model_dialog
    default_name = default_name or _smd_route_default_name()
    monkeypatch.delenv("KICADGEN_YOLO_MODEL", raising=False)
    window.combo_photo_detector.setCurrentIndex(window.combo_photo_detector.findData(detector))
    seen = {}

    def init(self, current, folder, default="", **kwargs):
        seen["default"] = default
    monkeypatch.setattr(yolo_model_dialog.YoloModelDialog, "__init__", init)
    monkeypatch.setattr(yolo_model_dialog.YoloModelDialog, "exec",
                        lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(yolo_model_dialog.YoloModelDialog, "selected_path",
                        lambda self: seen["default"])
    monkeypatch.setattr(yolo_model_dialog.YoloModelDialog, "shutdown", lambda self: None)
    monkeypatch.setattr(yolo_model_dialog.YoloModelDialog, "deleteLater", lambda self: None)
    window._pick_session_yolo_model()
    assert Path(seen["default"]).name == default_name
    assert window.field_yolo_model.text() == ""


def test_the_environment_override_applies_to_the_smd_route_too(window, monkeypatch, tmp_path):
    """@brief Review C-L3: KICADGEN_YOLO_MODEL was ignored by 'yolo_smd'."""
    weights = tmp_path / "mine.pt"
    monkeypatch.setenv("KICADGEN_YOLO_MODEL", str(weights))
    window.combo_photo_detector.setCurrentIndex(window.combo_photo_detector.findData("yolo_smd"))
    window.field_yolo_model.clear()
    assert window._photo_detector_options() == ("yolo_smd", weights)
    monkeypatch.delenv("KICADGEN_YOLO_MODEL")
    assert window._photo_detector_options()[1].name == _smd_route_default_name()


def test_a_project_without_the_smd_field_does_not_inherit_the_previous_one(window, tmp_path):
    """@brief Review B3 (2026-10-02): an older project means the default network.

    @note Negative: a project that declares its own file keeps it.
    """
    chosen = str(tmp_path / "mine.pt")
    window.field_smd_model.setText(chosen)
    state = window._project_state()
    window.field_smd_model.setText(str(tmp_path / "other.pt"))
    window._apply_project_state(state)
    assert window.field_smd_model.text() == chosen
    del state["widgets"]["field_smd_model"]
    window._apply_project_state(state)
    assert window.field_smd_model.text() == ""
