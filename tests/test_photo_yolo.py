"""@brief Exercise local detector contracts with fake ONNX sessions, never real inference."""
import builtins
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from kicadgen.services import photo_yolo as yolo


_NAMES = (
    "battery", "button", "buzzer", "capacitor", "clock", "connector", "diode",
    "display", "fuse", "heatsink", "ic", "inductor", "led", "pads", "pins",
    "potentiometer", "relay", "resistor", "switch", "transformer", "transistor",
)


def _raw(*entries):
    result = np.zeros((1, 25, 8400), dtype=np.float32)
    for index, (xywh, class_id, confidence) in enumerate(entries):
        result[0, :4, index] = xywh
        result[0, 4 + class_id, index] = confidence
    return result


@pytest.fixture
def setup(monkeypatch, tmp_path):
    """@brief Supply a fake CPU runtime and tiny inert weights for every inference test."""
    path = tmp_path / "fake.onnx"
    path.write_bytes(b"inert ONNX test weights; never execute")
    session = SimpleNamespace(
        inputs=[SimpleNamespace(name="custom_images", type="tensor(float)", shape=[1, 3, 640, 640])],
        outputs=[SimpleNamespace(name="output0", type="tensor(float)", shape=[1, 25, 8400])],
        metadata={"task": "detect", "imgsz": "[640, 640]", "batch": "1", "stride": "32",
                  "names": repr(dict(enumerate(_NAMES)))},
        raw=_raw(), calls=[], creations=[],
    )
    session.get_inputs = lambda: session.inputs
    session.get_outputs = lambda: session.outputs
    session.get_modelmeta = lambda: SimpleNamespace(custom_metadata_map=session.metadata)
    session.get_providers = lambda: ["CPUExecutionProvider"]

    def run(output_names, feeds):
        assert output_names is None
        assert set(feeds) == {"custom_images"}
        tensor = feeds["custom_images"]
        assert tensor.dtype == np.float32 and tensor.shape == (1, 3, 640, 640)
        assert tensor.flags.c_contiguous
        call_index = len(session.calls)
        session.calls.append(tensor)
        return [session.raw(call_index) if callable(session.raw) else session.raw]

    def create(weights, *, sess_options, providers):
        assert isinstance(weights, bytes)
        assert providers == ["CPUExecutionProvider"]
        assert sess_options.intra_op_num_threads == 4
        session.creations.append(weights)
        return session

    session.run = run
    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace(
        InferenceSession=create, SessionOptions=SimpleNamespace, __version__="fake-cpu",
    ))
    monkeypatch.setenv("KICADGEN_YOLO_MODEL", str(path))
    return session, path


def test_public_import_does_not_load_optional_packages(monkeypatch):
    """@brief Import the detector while all optional runtime imports are forbidden."""
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name.split(".")[0] in {"numpy", "cv2", "onnxruntime"}:
            pytest.fail(f"Eager optional import: {name}")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded)
    spec = importlib.util.spec_from_file_location("isolated_photo_yolo", yolo.__file__)
    spec.loader.exec_module(importlib.util.module_from_spec(spec))


def test_model_resolution_priority_and_default(setup, monkeypatch, tmp_path):
    """@brief Resolve explicit, environment, then repository-default weights in order."""
    _, path = setup
    explicit = tmp_path / "explicit.onnx"
    explicit.write_bytes(b"other")
    assert yolo.resolve_yolo_model(explicit) == explicit.resolve()
    assert yolo.resolve_yolo_model() == path.resolve()
    monkeypatch.delenv("KICADGEN_YOLO_MODEL")
    # Contratto invariato, meccanismo cambiato (2026-09-25): il default e' la
    # risorsa del programma (`app_paths.resource_path`), che da sorgente e' il
    # repository e nell'eseguibile e' il pacchetto installato.
    # Dal 2026-10-02 il predefinito sta in models/yolo (app_paths.yolo_model_path).
    monkeypatch.setattr(yolo, "yolo_model_path",
                        lambda name: tmp_path.joinpath("models", "yolo", name))
    default = tmp_path / "models" / "yolo" / "pcb_yolov8s.onnx"
    default.parent.mkdir(parents=True)
    default.write_bytes(b"default")
    assert yolo.resolve_yolo_model() == default.resolve()


# Contratto cambiato di proposito (utente, 2026-09-25): i .pt si caricano con
# PyTorch (tests/test_photo_yolo_pt.py). Resta rifiutato un formato che non e'
# ne' .onnx ne' .pt.
@pytest.mark.parametrize("filename,content", [("missing.onnx", None), ("empty.onnx", b""),
                                              ("empty.pt", b""), ("weights.bin", b"data")])
def test_missing_empty_or_non_onnx_weights(tmp_path, filename, content):
    """@brief Reject missing, empty and unsupported-format weights with actionable guidance."""
    path = tmp_path / filename
    if content is not None:
        path.write_bytes(content)
    with pytest.raises(ValueError, match="KICADGEN_YOLO_MODEL"):
        yolo.resolve_yolo_model(path)


def test_unreadable_weights(setup, monkeypatch):
    """@brief Convert filesystem permission failures to a model-path error."""
    _, path = setup
    def denied(*args, **kwargs):
        raise PermissionError("test access denied")
    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(ValueError, match="Cannot read local YOLO weights.*access denied"):
        yolo.resolve_yolo_model(path)


@pytest.mark.parametrize("package", ["numpy", "cv2", "onnxruntime"])
def test_missing_optional_runtime_is_actionable(setup, monkeypatch, package):
    """@brief Explain how to install a missing optional runtime without doing so."""
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name == package:
            raise ImportError(f"missing {package}")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded)
    with pytest.raises(ValueError, match="requirements/yolo.txt"):
        yolo.validate_yolo_setup()


def test_preflight_reads_metadata_and_hash_without_inference(setup):
    """@brief Validate the fixed CPU model contract without executing a prediction."""
    session, path = setup
    info = yolo.validate_yolo_setup()
    assert info["model_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert info["classes"] == list(_NAMES)
    assert info["input_shape"] == [1, 3, 640, 640]
    assert info["providers"] == ["CPUExecutionProvider"]
    assert not session.calls


@pytest.mark.parametrize("field,value", [
    ("input_shape", ["batch", 3, 640, 640]), ("input_shape", [1, 3, 320, 320]),
    ("input_type", "tensor(float16)"), ("output_shape", [1, 8400, 25]),
    ("output_shape", [1, 84, 8400]), ("output_type", "tensor(int64)"),
    ("task", "segment"), ("imgsz", "[1280, 1280]"), ("batch", "2"),
    ("stride", "64"), ("names", "{0: 'ic'}"),
    ("names", "__import__('os').system('echo unsafe')"),
])
def test_reject_incompatible_model_contract(setup, field, value):
    """@brief Reject dynamic/wrong tensors, tasks, or unsafe class metadata."""
    session, _ = setup
    if field.startswith("input_"):
        setattr(session.inputs[0], field[6:], value)
    elif field.startswith("output_"):
        setattr(session.outputs[0], field[7:], value)
    else:
        session.metadata[field] = value
    with pytest.raises(ValueError, match="compatible local YOLO model"):
        yolo.validate_yolo_setup()
    assert not session.calls


def test_reordered_classes_are_read_by_name(setup):
    """@brief Contratto cambiato (utente, 2026-09-25): una tabella in un altro ordine
    si legge per NOME, non si rifiuta. Prima si rifiutava perche' i punteggi
    si leggevano per posizione; ora vengono riordinati nell'ordine del rilevatore.
    """
    session, _ = setup
    names = dict(enumerate(_NAMES))
    names[13], names[10] = names[10], names[13]
    session.metadata["names"] = repr(names)
    raw = np.zeros((1, 25, 8400), np.float32)
    raw[0, 4 + 10, 0] = 0.8  # colonna 10 del MODELLO = names[10] = la classe interna 13
    session.raw = raw
    info = yolo.validate_yolo_setup()
    assert info["class_order"] == "remapped by name" and info["output_shape"] == [1, 25, 8400]
    model_session, input_name, *_ = yolo._setup(None)
    out = model_session.run(None, {input_name: np.zeros((1, 3, 640, 640), np.float32)})[0]
    assert out[0, 4 + 13, 0] == pytest.approx(0.8) and out[0, 4 + 10, 0] == 0


def test_a_model_with_fewer_classes_keeps_the_absent_ones_at_zero(setup):
    """@brief Positivo: 16 classi, maiuscole diverse, ordine diverso (YOLO26 «_l»)."""
    session, _ = setup
    declared = ["IC", "LED", "battery", "buzzer", "capacitor", "clock", "connector", "diode",
                "display", "fuse", "inductor", "potentiometer", "relay", "resistor",
                "switch", "transistor"]
    session.metadata["names"] = repr(dict(enumerate(declared)))
    session.outputs[0].shape = [1, 4 + len(declared), 8400]
    raw = np.zeros((1, 4 + len(declared), 8400), np.float32)
    raw[0, 4 + declared.index("IC"), 0] = 0.7
    session.raw = raw
    info = yolo.validate_yolo_setup()
    assert set(info["missing_classes"]) == {"button", "heatsink", "pads", "pins", "transformer"}
    model_session, input_name, *_ = yolo._setup(None)
    out = model_session.run(None, {input_name: np.zeros((1, 3, 640, 640), np.float32)})[0]
    assert out.shape == (1, 25, 8400)
    assert out[0, 4 + _NAMES.index("ic"), 0] == pytest.approx(0.7)
    assert out[0, 4:, 0].sum() == pytest.approx(0.7)


@pytest.mark.parametrize("names", [
    {0: "ic", 1: "gearbox"},                 # classe sconosciuta: non si sa cosa sia
    {0: "ic", 1: "IC"},                      # la stessa classe due volte
    {1: "ic", 2: "led"},                     # indici non da 0
])
def test_unknown_or_ambiguous_class_tables_are_refused(setup, names):
    """@brief Negativo: rimappare richiede di sapere cosa sia ogni classe."""
    session, _ = setup
    session.metadata["names"] = repr(names)
    session.outputs[0].shape = [1, 4 + len(names), 8400]
    with pytest.raises(ValueError, match="class"):
        yolo.validate_yolo_setup()


def test_the_detector_order_keeps_the_plain_session(setup):
    """@brief Negativo del riordino: con la tabella del rilevatore nulla cambia."""
    session, _ = setup
    model_session, *_rest, info = yolo._setup(None)
    assert model_session is session and info["class_order"] == "detector order"
    assert info["missing_classes"] == []


def test_corrupt_model_load_is_actionable(setup, monkeypatch):
    """@brief Convert ONNX parser errors into a useful setup error."""
    def fail(*args, **kwargs):
        raise RuntimeError("invalid protobuf")
    monkeypatch.setattr(sys.modules["onnxruntime"], "InferenceSession", fail)
    with pytest.raises(ValueError, match="invalid protobuf.*self-contained"):
        yolo.validate_yolo_setup()


@pytest.mark.parametrize("value", [0, -1, True, float("nan"), float("inf"), "20", None])
@pytest.mark.parametrize("axis", [0, 1])
def test_invalid_board_dimensions(setup, value, axis):
    """@brief Reject nonnumeric, nonfinite, boolean and nonpositive calibration."""
    session, _ = setup
    dimensions = [20, 10]
    dimensions[axis] = value
    with pytest.raises(ValueError, match="finite positive number"):
        yolo.detect_yolo_components(np.zeros((10, 20, 3), np.uint8), *dimensions)
    assert not session.creations


@pytest.mark.parametrize("photo", [
    None, [], np.zeros((0, 10, 3), np.uint8), np.zeros((10, 0, 3), np.uint8),
    np.zeros((10, 20), np.uint8), np.zeros((10, 20, 4), np.uint8),
    np.full((10, 20, 3), np.nan), np.zeros((10, 20, 3), np.float32),
])
def test_invalid_photo(setup, photo):
    """@brief Reject empty, NaN, grayscale and non-BGR-array photos before inference."""
    session, _ = setup
    with pytest.raises(ValueError, match="nonempty uint8 BGR"):
        yolo.detect_yolo_components(photo, 20, 10)
    assert not session.creations


def test_letterbox_rgb_normalization_and_geometry(setup):
    """@brief Verify padding, BGR-to-RGB conversion and inverse letterbox coordinates."""
    session, _ = setup
    photo = np.empty((100, 200, 3), np.uint8)
    photo[:] = (0, 128, 255)
    # Original rectangle (20,10)-(80,30), scale 3.2, top padding 160.
    session.raw = _raw(([160, 224, 192, 64], 17, 0.75))
    components = yolo.detect_yolo_components(photo, 40, 20)
    assert len(components) == 1
    c = components[0]
    assert c["_box"] == pytest.approx((0.1, 0.1, 0.4, 0.3))
    assert (c["x_mm"], c["y_mm"], c["length_mm"], c["width_mm"]) == pytest.approx((-10, -6, 12, 4))
    assert c["rotation_deg"] == 0
    assert c["kind"] == "generic" and c["name"] == "YOLO candidate 1"
    assert c["_id"] == 1 and c["_source"] == "YOLO local" and c["_uncertain"] is True
    # Contratto aggiornato (26/09): altezza stimata dalla classe e dalla pianta
    # (photo_heights), non piu' 1.0 per tutti; resta una stima dichiarata.
    assert c["height_mm"] == 0.6 and c["_height_estimated"] is True
    assert c["_height_source"].startswith("detector_class_")
    assert c["_height_source"].endswith("_not_measured")
    assert c["_yolo_class"] == "resistor" and c["_yolo_confidence"] == 0.75
    assert c["color_hex"] == "#303030" and c["marking"] == ""
    assert "package" not in c and "_detected_kind" not in c
    tensor = session.calls[0]
    assert tensor[0, :, 0, 0] == pytest.approx([114 / 255] * 3)
    assert tensor[0, :, 160, 0] == pytest.approx([1, 128 / 255, 0])


def test_rotation_uses_physical_long_axis(setup):
    """@brief Choose the long axis in calibrated millimeters, not source pixels."""
    session, _ = setup
    session.raw = _raw(([320, 320, 200, 100], 10, 0.8))
    c = yolo.detect_yolo_components(np.zeros((640, 640, 3), np.uint8), 32, 128)[0]
    assert c["length_mm"] == 20 and c["width_mm"] == 10 and c["rotation_deg"] == 90


def test_recipe_has_41_views_and_complete_edge_coverage():
    """@brief Freeze the experiment recipe and cover irregular image edges."""
    views = list(yolo._views(4000, 1848))
    assert len(views) == 41
    assert views[0] == ("overview", (0, 0, 4000, 1848))
    assert sum(name.startswith("tile640") for name, _ in views) == 32
    assert sum(name.startswith("tile1280") for name, _ in views) == 8
    for tile_size in (640, 1280):
        covered = np.zeros((1300, 1700), dtype=bool)
        for name, (x0, y0, x1, y1) in yolo._views(1700, 1300):
            if name.startswith(f"tile{tile_size}"):
                covered[y0:y1, x0:x1] = True
        assert covered.all()


def test_a_fragment_is_dropped_only_when_another_view_saw_the_body_whole(setup):
    """@brief Il taglio di una tessera non e' prova di assenza.

    La regola sui frammenti serve a togliere i doppioni di cio' che un'altra
    vista vede per intero: qui la vista 2 lo vede intero, quindi il frammento
    della vista 1 e' un doppione e sparisce, e sopravvive l'ingombro NON
    troncato. Il caso opposto e' nel test seguente.
    """
    session, _ = setup
    # Stesso corpo: intero in tile640-360-0 (x 600..680), tagliato dal bordo
    # destro di tile640-0-0 (x 600..640).
    outputs = [_raw(), _raw(([630, 50, 60, 40], 10, 0.9)),
               _raw(([280, 50, 80, 60], 10, 0.8)), _raw()]
    session.raw = lambda index: outputs[index]
    results = yolo.detect_yolo_components(np.zeros((640, 1000, 3), np.uint8), 100, 64)
    assert len(results) == 1
    assert results[0]["_box"] == pytest.approx((0.6, 20 / 640, 0.68, 80 / 640))
    assert not results[0]["_yolo_clipped_recovery"]


def test_a_body_no_view_saw_whole_is_rebuilt_instead_of_lost(setup):
    """@brief Il caso del regolatore: piu' grande della sovrapposizione fra tessere.

    Su dati reali il rilevatore lo aveva visto quattro volte (la migliore a
    0,165) e ogni volta il riquadro toccava un bordo, perche' i suoi ~350 px
    non stanno nei 160 px di sovrapposizione delle tessere da 640. Scartando
    ogni frammento il corpo spariva del tutto. Qui due meta' viste da tessere
    diverse tornano un corpo solo, marcato come ricomposto perche' il suo
    ingombro resta troncato.
    """
    session, _ = setup
    # Un corpo da 350 a 650 px: la tessera 0-640 ne vede 350..640 (tagliato a
    # destra), la tessera 360-1000 ne vede 360..650 (tagliato a sinistra).
    # Dove entrambe vedono, 360..640, i due pezzi coincidono. (Prima la
    # seconda meta' finiva a 400 px con un bordo VISTO: non poteva essere lo
    # stesso corpo, e la si univa solo perche' stava dentro la prima.)
    outputs = [_raw(), _raw(([500, 50, 300, 40], 10, 0.9)),
               _raw(([145, 50, 290, 40], 10, 0.7)), _raw()]
    session.raw = lambda index: outputs[index]
    results = yolo.detect_yolo_components(np.zeros((640, 1000, 3), np.uint8), 100, 64)
    assert len(results) == 1
    row = results[0]
    assert row["_yolo_clipped_recovery"] and "cucitura" in row["_source"]
    assert not row["_yolo_review_only"]
    # Unione dei due frammenti: 350..640 dalla prima tessera, 360..650 dalla
    # seconda. Restano i bordi esterni, cioe' l'ingombro piu' ampio visto.
    assert row["_box"] == pytest.approx((0.35, 30 / 640, 0.65, 70 / 640))
    assert row["_yolo_confidence"] == pytest.approx(0.9)


def test_a_seam_fragment_is_not_fused_with_a_weak_box_that_only_contains_it(setup):
    """@brief Caso negativo: contenere un frammento non ne fa la meta' dello stesso corpo.

    Un frammento piccolo e sicuro tagliato da una tessera sta per intero dentro
    un riquadro debole e grande tagliato dalla tessera accanto. Dove entrambe
    vedono i due riquadri non coincidono: sono corpi diversi, e l'unione non
    deve nascere ne' ereditare la confidenza del piccolo (ESP32-S3, 08/10: una
    striscia 7.3x23 mm a 0,10 diventata un corpo a 0,56).
    """
    session, _ = setup
    outputs = [_raw(), _raw(([615, 50, 50, 40], 10, 0.9)),
               _raw(([270, 305, 540, 590], 10, 0.1)), _raw()]
    session.raw = lambda index: outputs[index]
    results = yolo.detect_yolo_components(np.zeros((640, 1000, 3), np.uint8), 100, 64)
    assert len(results) == 1, "solo il frammento sicuro e' un corpo"
    x0, _, x1, _ = results[0]["_box"]
    assert (x0, x1) == pytest.approx((0.59, 0.64)), 'il suo ingombro, non quello del riquadro grande'


@pytest.mark.parametrize("xywh", [[0, 320, 20, 40], [640, 320, 20, 40],
                                   [320, 0, 40, 20], [320, 640, 40, 20]])
def test_board_edges_are_clipped_and_retained(setup, xywh):
    """@brief Preserve genuine detections touching any outer board edge."""
    session, _ = setup
    session.raw = _raw((xywh, 10, 0.9))
    c = yolo.detect_yolo_components(np.zeros((640, 640, 3), np.uint8), 64, 64)[0]
    x0, y0, x1, y1 = c["_box"]
    assert 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1


@pytest.mark.parametrize("xywh", [[0, 320, 20, 40], [640, 320, 20, 40],
                                   [320, 0, 40, 20], [320, 640, 40, 20]])
def test_all_interior_tile_edges_rejected(xywh):
    """@brief Treat all four internal crop boundaries as possible body cuts."""
    rows, invalid = yolo._proposals(_raw((xywh, 10, 0.9)), (100, 100, 740, 740),
                                    (1000, 1000), (1, 0, 0), "tile", np)
    assert invalid == 0 and rows[0]["rejection"] == "clipped_by_tile"


def test_the_tile_edge_margin_is_the_detector_uncertainty_scaled_to_the_view():
    """@brief Il margine di taglio viene dall'incertezza del rilevatore, non da 1 px.

    Regola: 8 px nello spazio del modello (passo piu' fine della testa, P3),
    riportati ai pixel della foto con la scala della vista, piu' 1 px di
    arrotondamento del letterbox. Tessera 640 (scala 1): 9 px; tessera 1280
    (scala 0.5): 17 px. Una scala piu' fine (foto piu' piccola della tessera,
    scala 2) stringe il margine a 5 px.
    """
    assert yolo._BOX_UNCERTAINTY_INPUT_PX == 8
    assert yolo._tile_edge_margin_px(1.0) == 9
    assert yolo._tile_edge_margin_px(0.5) == 17
    assert yolo._tile_edge_margin_px(2.0) == 5


@pytest.mark.parametrize("ratio,xywh,sides", [
    # Scala 1 (tessera 640): un lato a 5 px dal bordo interno e' un taglio...
    (1.0, [605, 320, 60, 40], ["right"]),
    (1.0, [35, 320, 60, 40], ["left"]),
    (1.0, [320, 605, 40, 60], ["bottom"]),
    (1.0, [320, 35, 40, 60], ["top"]),
    # ...e un lato a 12 px dal bordo non lo e': oltre l'incertezza il bordo
    # e' misurato (caso negativo, invariato rispetto a prima).
    (1.0, [598, 320, 60, 40], []),
    (1.0, [42, 320, 60, 40], []),
    # Scala 0.5 (tessera 1280 letterboxata a 640): il margine vale 17 px
    # della foto = 8.5 px del modello. Un lato a 7 px del modello dal bordo
    # (14 px foto) e' un taglio; a 10 px del modello (20 px foto) non lo e'.
    (0.5, [603, 320, 60, 40], ["right"]),
    (0.5, [600, 320, 60, 40], []),
])
def test_a_side_within_the_detector_uncertainty_of_a_tile_edge_is_a_cut(ratio, xywh, sides):
    """@brief Entro l'incertezza del rilevatore un lato sul bordo interno e' tagliato.

    E' il difetto del lamierino dell'ESP32-S3 (08/10): la tessera 0..640 lo
    tagliava e i riquadri finivano a 636-638 px, dentro la tolleranza di 1 px
    no, ma dentro l'incertezza del bordo si'. Passavano per interi e un pezzo
    del lamierino usciva come corpo completo («inductor 11.6x11.3 @0.75»).
    """
    # Tessera interna (640/ratio px di lato): nessun bordo coincide con il
    # bordo dell'immagine.
    size = int(640 / ratio)
    rows, invalid = yolo._proposals(_raw((xywh, 10, 0.9)), (100, 100, 100 + size, 100 + size),
                                    (3000, 3000), (ratio, 0, 0), "tile", np)
    assert invalid == 0 and len(rows) == 1
    assert rows[0]["clipped_sides"] == sides
    assert rows[0]["rejection"] == ("clipped_by_tile" if sides else None)


@pytest.mark.parametrize("xywh", [[35, 320, 60, 40], [605, 320, 60, 40],
                                   [320, 35, 40, 60], [320, 605, 40, 60]])
def test_a_side_within_the_margin_of_an_image_border_is_a_real_edge(xywh):
    """@brief Caso negativo: il bordo dell'immagine non taglia, anche dentro il margine.

    Lo stesso riquadro a 5 px dal bordo, ma la vista e' l'immagine intera:
    il suo bordo e' un bordo reale della scheda, non una cucitura.
    """
    rows, invalid = yolo._proposals(_raw((xywh, 10, 0.9)), (0, 0, 640, 640),
                                    (640, 640), (1.0, 0, 0), "overview", np)
    assert invalid == 0 and rows[0]["clipped_sides"] == [] and rows[0]["rejection"] is None


def test_a_body_near_a_seam_is_measured_by_the_tile_that_holds_it_clear_of_the_seam(setup):
    """@brief Caso positivo end-to-end: chi sfiora la cucitura non misura, il vicino si'.

    Il corpo finisce 5 px prima del bordo destro della tessera 0..640 (dentro
    il margine di 9 px) e la tessera accanto lo contiene con 65 px di aria.
    Prima la prima tessera lo dava per intero e, piu' sicura, vinceva la NMS
    col SUO riquadro; ora quel riquadro e' un frammento, coperto dal corpo
    intero della tessera accanto, e ne resta UNO con l'ingombro misurato da
    chi lo contiene. Nessun corpo si perde, nessuno e' ricomposto.
    """
    session, _ = setup
    # Tessera 0..640: x 570..635, conf 0.9 (lato destro a 5 px dal taglio).
    # Tessera 360..1000: x 575..636 nella foto, conf 0.8, lontano dai bordi.
    outputs = [_raw(), _raw(([602.5, 320, 65, 40], 10, 0.9)),
               _raw(([245.5, 320, 61, 40], 10, 0.8)), _raw()]
    session.raw = lambda index: outputs[index]
    rows = yolo.detect_yolo_components(np.zeros((640, 1000, 3), np.uint8), 100, 64,
                                       include_review_candidates=True)
    assert len(rows) == 1
    row = rows[0]
    assert row["_box"][0] == pytest.approx(0.575) and row["_box"][2] == pytest.approx(0.636)
    assert row["_yolo_confidence"] == pytest.approx(0.8)
    assert not row["_yolo_clipped_recovery"] and row["_yolo_clipped_sides"] == []
    assert row["_yolo_origin"] == "operating_threshold" and not yolo.awaiting_user_keep(row)


def test_a_body_clear_of_every_seam_is_unchanged_by_the_margin(setup):
    """@brief Caso negativo: lontano dai bordi la regola non interviene.

    Stesso corpo visto da due tessere, entrambe con piu' di 9 px di aria dal
    bordo: entrambe lo misurano, la NMS tiene la piu' sicura, come prima.
    """
    session, _ = setup
    # Tessera 0..640: x 560..620 (20 px dal taglio), conf 0.9.
    # Tessera 360..1000: x 561..621 nella foto, conf 0.8.
    outputs = [_raw(), _raw(([590, 320, 60, 40], 10, 0.9)),
               _raw(([231, 320, 60, 40], 10, 0.8)), _raw()]
    session.raw = lambda index: outputs[index]
    rows = yolo.detect_yolo_components(np.zeros((640, 1000, 3), np.uint8), 100, 64,
                                       include_review_candidates=True)
    assert len(rows) == 1
    assert rows[0]["_box"][0] == pytest.approx(0.56) and rows[0]["_box"][2] == pytest.approx(0.62)
    assert rows[0]["_yolo_confidence"] == pytest.approx(0.9)
    assert rows[0]["_yolo_clipped_sides"] == []


def test_the_audit_recipe_reports_the_tile_edge_margin_per_view(setup, tmp_path):
    """@brief L'audit dichiara la regola del margine e il suo valore per ogni vista."""
    session, _ = setup
    session.raw = _raw(([320, 320, 60, 40], 10, 0.9))
    yolo.detect_yolo_components(np.zeros((640, 1000, 3), np.uint8), 100, 64,
                                audit_dir=tmp_path)
    audit = json.loads(next(tmp_path.glob("yolo-*.json")).read_text())
    margin = audit["recipe"]["tile_edge_margin_px"]
    assert margin["box_uncertainty_input_px"] == 8
    assert "view_scale" in margin["rule"]
    views = {view["name"]: view for view in audit["views"]}
    assert set(margin["per_view"]) == set(views)
    for name, view in views.items():
        assert margin["per_view"][name] == view["tile_edge_margin_px"] == 8 / view["scale"] + 1
    assert views["tile640-0-0"]["tile_edge_margin_px"] == 9
    assert views["overview"]["tile_edge_margin_px"] == pytest.approx(8 / 0.64 + 1)


def test_full_view_keeps_large_module_and_small_contained_body(setup):
    """@brief Preserve a large overview envelope and distinct small contained detection."""
    session, _ = setup
    overview = _raw(([320, 320, 580, 300], 10, 0.85), ([320, 320, 20, 20], 3, 0.9))
    session.raw = lambda index: overview if index == 0 else _raw()
    results = yolo.detect_yolo_components(np.zeros((1280, 2560, 3), np.uint8), 80, 40)
    assert len(results) == 2
    assert max(c["length_mm"] for c in results) == pytest.approx(72.5)


def test_nms_class_agnostic_excludes_pads_pins_before_suppression(setup):
    """@brief Suppress duplicates across classes without letting pads/pins erase bodies."""
    session, _ = setup
    session.raw = _raw(
        ([200, 200, 100, 100], 13, 0.99), ([200, 200, 100, 100], 14, 0.98),
        ([200, 200, 100, 100], 3, 0.85), ([202, 202, 100, 100], 17, 0.75),
        ([400, 400, 50, 50], 10, 0.25), ([500, 500, 50, 50], 10, 0.24),
    )
    results = yolo.detect_yolo_components(np.zeros((640, 640, 3), np.uint8), 64, 64)
    assert [c["_yolo_class"] for c in results] == ["capacitor", "ic"]
    assert [c["_id"] for c in results] == [1, 2]


def test_empty_inference_is_empty_inventory_with_audit(setup, tmp_path):
    """@brief Report zero detections as success and preserve the empty audit."""
    results = yolo.detect_yolo_components(np.zeros((20, 40, 3), np.uint8), 40, 20,
                                          audit_dir=tmp_path / "audit")
    assert results == []
    document = json.loads(next((tmp_path / "audit").glob("yolo-*.json")).read_text())
    assert document["raw_proposals"] == document["retained_detector_results"] == []


def test_invalid_predictions_do_not_poison_good_results_or_json(setup, tmp_path):
    """@brief Reject NaN, infinite, invalid-score and inverted-box predictions independently."""
    session, _ = setup
    session.raw = _raw(
        ([100, 100, 20, 20], 10, 0.75), ([float("nan"), 0, 20, 20], 10, 0.9),
        ([0, 0, 20, 20], 10, float("nan")), ([float("inf"), 0, 20, 20], 10, 0.9),
        ([100, 100, -20, 20], 10, 0.9), ([100, 100, 20, 20], 10, 1.5),
    )
    results = yolo.detect_yolo_components(np.zeros((640, 640, 3), np.uint8), 64, 64,
                                          audit_dir=tmp_path / "audit")
    assert len(results) == 1
    contents = next((tmp_path / "audit").glob("yolo-*.json")).read_text()
    assert "NaN" not in contents and "Infinity" not in contents
    document = json.loads(contents)
    assert all(view["invalid_rows"] == 4 for view in document["views"])


def test_audit_contains_hash_recipe_raw_and_retained_without_overwrite(setup, tmp_path):
    """@brief Preserve enough evidence to replay filtering and distinguish repeated runs."""
    session, path = setup
    session.raw = _raw(([200, 200, 100, 100], 10, 0.8), ([400, 400, 20, 20], 13, 0.9))
    logs = []
    for _ in range(2):
        results = yolo.detect_yolo_components(np.zeros((640, 640, 3), np.uint8), 64, 64,
                                              log=logs.append, audit_dir=tmp_path / "audit")
    paths = list((tmp_path / "audit").glob("yolo-*.json"))
    assert len(paths) == 2
    audit = json.loads(paths[0].read_text())
    assert audit["model_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert audit["recipe"]["tile_sizes"] == [640, 1280]
    assert audit["recipe"]["confidence"] == 0.25
    # Contratto aggiornato (fix A10): su una foto 640 x 640 le tessere da 640
    # e da 1280 hanno gli stessi limiti della vista d'insieme e non si
    # ripetono piu': una sola vista, quindi 2 proposte grezze invece di 6.
    assert [view["name"] for view in audit["views"]] == ["overview"]
    assert len(audit["raw_proposals"]) == 2 and len(audit["retained_detector_results"]) == 1
    assert any(row["rejection"] == "excluded_class" for row in audit["raw_proposals"])
    assert audit["components"] == json.loads(json.dumps(results))
    assert any("audit:" in message for message in logs)


def test_no_silent_component_limit(setup):
    """@brief Keep hundreds of disjoint boxes rather than applying a hidden top-k limit."""
    session, _ = setup
    session.raw = _raw(*[([x, y, 8, 8], 10, 0.8)
                         for y in range(10, 630, 30) for x in range(10, 630, 30)])
    results = yolo.detect_yolo_components(np.zeros((640, 640, 3), np.uint8), 64, 64)
    assert len(results) == 441
    assert [c["_id"] for c in results] == list(range(1, 442))


@pytest.mark.parametrize("output", [np.zeros((1, 25, 1), np.float32),
                                     np.zeros((1, 25, 8400), np.float64), None])
def test_invalid_runtime_output_fails_explicitly(setup, output):
    """@brief Refuse unexpected output tensors instead of claiming a complete inventory."""
    session, _ = setup
    session.raw = output
    with pytest.raises(ValueError, match="inference failed"):
        yolo.detect_yolo_components(np.zeros((20, 40, 3), np.uint8), 40, 20)


def test_inference_failure_does_not_return_partial_inventory(setup):
    """@brief Fail the entire detection when a later tile cannot be inferred."""
    session, _ = setup
    def output(index):
        if index:
            raise RuntimeError("simulated CPU failure")
        return _raw(([320, 320, 100, 100], 10, 0.9))
    session.raw = output
    # Foto piu' larga di una tessera (fix A10): una 640 x 640 ha ormai una
    # sola vista, e il guasto della seconda inferenza non avverrebbe mai.
    with pytest.raises(ValueError, match="simulated CPU failure"):
        yolo.detect_yolo_components(np.zeros((640, 1000, 3), np.uint8), 100, 64)


def test_unwritable_audit_is_reported(setup, tmp_path):
    """@brief Do not silently omit requested audit evidence when the destination fails."""
    blocker = tmp_path / "file-not-directory"
    blocker.write_text("occupied")
    with pytest.raises(ValueError, match="Cannot write YOLO audit"):
        yolo.detect_yolo_components(np.zeros((20, 40, 3), np.uint8), 40, 20, audit_dir=blocker)


def test_audit_accounts_for_every_proposal_and_links_components(setup, tmp_path):
    """@brief Distinguish confidence/class filtering, both NMS stages and retained bodies."""
    session, _ = setup
    session.raw = _raw(([200, 200, 100, 100], 10, 0.9),
                       ([201, 200, 100, 100], 3, 0.8),
                       ([400, 400, 20, 20], 17, 0.2),
                       ([450, 450, 20, 20], 13, 0.9))
    photo = np.zeros((640, 640, 3), np.uint8)
    components = yolo.detect_yolo_components(photo, 64, 64, audit_dir=tmp_path)
    audit = json.loads(next(tmp_path.glob('yolo-*.json')).read_text())
    assert audit['audit_schema_version'] == 2
    assert audit['image_sha256'] == hashlib.sha256(photo.tobytes()).hexdigest()
    proposals = {row['proposal_id']: row for row in audit['raw_proposals']}
    rejected = {key for key, row in proposals.items() if row['rejection']}
    suppressed = {row['proposal_id'] for row in audit['nms_suppressions']}
    retained = {row['proposal_id'] for row in audit['retained_detector_results']}
    # Contratto aggiornato (fix A10): la foto 640 x 640 ha una sola vista, le
    # tessere identiche non si ripetono. Prima 12 proposte e 5 soppressioni
    # erano per meta' copie della stessa inferenza; la NMS globale fra viste
    # diverse resta coperta da test_nms_trace_* e dal test che segue.
    assert len(proposals) == len(audit['raw_proposals']) == 4
    assert not (rejected & suppressed or rejected & retained or suppressed & retained)
    assert rejected | suppressed | retained == proposals.keys()
    assert len(audit['nms_suppressions']) == len(suppressed) == 1
    assert {row['stage'] for row in audit['nms_suppressions']} == {'eligible_per_view'}
    # Tutte le passate di NMS dichiarate (fix A8), anche quella della revisione.
    assert audit['recipe']['nms_stages'] == ['eligible_per_view', 'global',
                                             'global_with_recovered', 'presence_attested',
                                             'review_pool']
    assert isinstance(audit['review_nms_suppressions'], list)
    for row in audit['nms_suppressions']:
        assert row['suppressed_by'] in proposals
        assert row['iou'] > audit['recipe']['nms_iou']
    assert {row['_yolo_proposal_id'] for row in components} == retained
    for view in audit['views']:
        assert view['invalid_rows'] + view['valid_rows_below_audit_floor'] + view['raw_proposals'] == 8400
        assert view['filter_rejections'] == {'below_confidence': 1, 'excluded_class': 1}
        assert view['eligible_before_nms'] == 2
    assert 'below audit floor' in audit['audit_scope']


def test_nms_trace_preserves_small_contained_and_adjacent_bodies():
    """@brief Trace true overlap suppression without absorbing contained or adjacent bodies."""
    rows = [dict(proposal_id=str(i), box=box, confidence=score) for i, (box, score) in enumerate([
        ([0, 0, 100, 100], .9), ([10, 10, 20, 20], .8),
        ([21, 10, 31, 20], .7), ([10, 10, 20, 20], .6),
    ])]
    trace = []
    result = yolo._nms(rows, np, trace, 'global')
    assert result == yolo._nms(rows, np) == rows[:3]
    assert trace == [dict(proposal_id='3', suppressed_by='1', stage='global', iou=1.0)]


def test_image_hash_uses_pixels_and_changes_with_input(setup, tmp_path):
    """@brief Hash noncontiguous BGR input consistently and distinguish changed photo pixels."""
    photo = np.zeros((20, 40, 3), np.uint8)[:, ::2]
    assert not photo.flags.c_contiguous
    hashes = []
    for index in range(2):
        photo[0, 0, 0] = index
        directory = tmp_path / str(index)
        yolo.detect_yolo_components(photo, 40, 20, audit_dir=directory)
        audit = json.loads(next(directory.glob('yolo-*.json')).read_text())
        hashes.append(audit['image_sha256'])
        assert hashes[-1] == hashlib.sha256(photo.tobytes()).hexdigest()
        assert audit['nms_suppressions'] == []
    assert hashes[0] != hashes[1]


@pytest.mark.parametrize('excluded_id', [13, 14])
def test_competing_body_evidence_survives_excluded_argmax(setup, tmp_path, excluded_id):
    """@brief Keep uncertain bodies with sufficient alternate evidence, not bare pads/pins."""
    session, _ = setup
    session.raw = _raw(([200, 200, 20, 20], excluded_id, .8),
                       ([300, 300, 20, 20], excluded_id, .8))
    session.raw[0, 4 + 3, 0] = .4
    session.raw[0, 4 + 3, 1] = .1
    result = yolo.detect_yolo_components(np.zeros((640, 640, 3), np.uint8), 64, 64,
                                         audit_dir=tmp_path)
    assert len(result) == 1
    assert result[0]['kind'] == 'generic' and result[0]['_uncertain']
    assert result[0]['_yolo_class_ambiguous']
    assert result[0]['_yolo_confidence'] == pytest.approx(.4)
    audit = json.loads(next(tmp_path.glob('yolo-*.json')).read_text())
    kept = audit['retained_detector_results'][0]
    assert kept['argmax_class'] == _NAMES[excluded_id]
    assert kept['argmax_confidence'] == pytest.approx(.8)
    assert kept['best_body_class'] == 'capacitor'
    assert all(row['rejection'] == 'excluded_class' for row in audit['raw_proposals']
               if row['prediction_index'] == 1)


def test_review_queue_preserves_weak_bodies_without_promoting_pads_or_duplicates(setup, tmp_path):
    """@brief Surface uncertain body hypotheses separately from accepted detector results."""
    session, _ = setup
    session.raw = _raw(([100, 100, 20, 20], 10, .8),
                       ([100, 100, 20, 20], 3, .2),
                       ([300, 300, 20, 20], 3, .2),
                       ([400, 400, 20, 20], 13, .2),
                       ([500, 500, 20, 20], 17, .01))
    results = yolo.detect_yolo_components(np.zeros((640, 640, 3), np.uint8), 64, 64,
                                          audit_dir=tmp_path)
    audit = json.loads(next(tmp_path.glob('yolo-*.json')).read_text())
    assert len(results) == 1
    queue = audit['low_confidence_review_proposals']
    assert len(queue) == 1 and queue[0]['prediction_index'] == 2
    assert queue[0]['rejection'] == 'below_confidence'
    assert queue[0]['proposal_id'] not in {r['_yolo_proposal_id'] for r in results}


@pytest.mark.parametrize('audit_enabled', [False, True])
def test_opt_in_review_candidates_are_deduplicated_and_audit_independent(setup, tmp_path, audit_enabled):
    """@brief Le proposte deboli escono marcate «bassa confidenza», deduplicate.

    Contratto aggiornato (utente, 26/09): prima restavano solo nell'audit; ora
    tornano VISIBILI nella revisione, marcate `_yolo_low_confidence` e da tenere
    a mano (awaiting_user_keep), mai inviate al modello per default. Restano
    fuori i doppioni di un corpo tenuto, le classi escluse (pads/pins) e cio'
    che sta sotto la soglia d'audit.
    """
    session, _ = setup
    session.raw = _raw(([100, 100, 20, 20], 10, .8),
                       ([100, 100, 20, 20], 3, .2),
                       ([300, 300, 20, 20], 3, .2),
                       ([400, 400, 20, 20], 13, .2),
                       ([500, 500, 20, 20], 14, .2),
                       ([550, 550, 20, 20], 17, .01))
    photo = np.zeros((640, 640, 3), np.uint8)
    baseline = yolo.detect_yolo_components(photo, 64, 64)
    result = yolo.detect_yolo_components(photo, 64, 64, include_review_candidates=True,
                                         audit_dir=tmp_path if audit_enabled else None)
    # Senza l'adesione esplicita nessuna debole; con l'adesione UNA sola: la
    # (300, 300). La (100, 100) e' un doppione del corpo tenuto, pads/pins
    # sono esclusi, 0.01 sta sotto la soglia d'audit.
    assert len(baseline) == 1 and not baseline[0]['_yolo_low_confidence']
    assert len(result) == 2
    weak = [c for c in result if c['_yolo_low_confidence']]
    assert len(weak) == 1 and weak[0]['_yolo_review_only'] is True
    assert weak[0]['_yolo_confidence'] == pytest.approx(.2)
    assert weak[0]['x_mm'] == pytest.approx(-2)
    assert yolo.awaiting_user_keep(weak[0])
    assert not yolo.awaiting_user_keep(dict(weak[0], _yolo_user_kept=True))
    if audit_enabled:
        audit = json.loads(next(tmp_path.glob('yolo-*.json')).read_text())
        assert audit['recipe']['include_review_candidates'] is True
        assert len(audit['retained_detector_results']) == 1
        # L'audit le conserva tutte: togliere il rumore dalla revisione non
        # vuol dire nasconderlo a chi vuole controllare.
        weak = audit['low_confidence_review_proposals']
        assert weak and all(row['rejection'] == 'below_confidence' for row in weak)
        assert audit['components'] == json.loads(json.dumps(result))
    else:
        assert not list(tmp_path.glob('yolo-*.json'))


def test_a_weak_fragment_comes_back_as_a_weak_hypothesis_not_as_nothing(setup):
    """@brief Sotto soglia e tagliato restano due cose diverse.

    Il frammento ricomposto rientra dalla porta che gli spetta -- quella delle
    ipotesi deboli, da rivedere -- e non da quella dei candidati sicuri. E' il
    caso reale: il regolatore era a 0,165, sotto la soglia operativa.
    """
    session, _ = setup
    outputs = [_raw(), _raw(([630, 50, 60, 40], 3, .2)), _raw(), _raw()] * 2
    session.raw = lambda index: outputs[index]
    result = yolo.detect_yolo_components(np.zeros((640, 1000, 3), np.uint8), 100, 64,
                                         include_review_candidates=True)
    assert len(result) == 1
    assert result[0]['_yolo_review_only'] and result[0]['_yolo_clipped_recovery']
    assert result[0]['_yolo_confidence'] == pytest.approx(.2)
    # Senza l'adesione esplicita alla revisione, un'ipotesi debole non esce.
    assert yolo.detect_yolo_components(np.zeros((640, 1000, 3), np.uint8), 100, 64) == []


@pytest.mark.parametrize('value', [None, 'false', 0, 1])
def test_review_candidate_opt_in_rejects_non_boolean(setup, value):
    """@brief Require explicit boolean semantics before running local inference."""
    session, _ = setup
    with pytest.raises(ValueError, match='include_review_candidates must be a boolean'):
        yolo.detect_yolo_components(np.zeros((10, 10, 3), np.uint8), 10, 10,
                                    include_review_candidates=value)
    assert not session.calls and not session.creations


@pytest.mark.parametrize('audit_enabled', [False, True])
def test_production_inventory_sends_weak_bodies_to_global_review(setup, monkeypatch, tmp_path, audit_enabled):
    """@brief La catena di produzione non manda al modello le proposte deboli.

    Il modello riceve i corpi sopra la soglia operativa. Le deboli restano nel
    rilevatore: erano il grosso del rumore nella richiesta a pagamento, e su
    una scheda popolata cadono quasi tutte dove non c'e' niente montato.
    """
    from kicadgen.services import photo_inventory
    session, _ = setup
    session.raw = _raw(([100, 100, 20, 20], 10, .8),
                       ([300, 300, 20, 20], 3, .2),
                       ([400, 400, 20, 20], 13, .2))
    monkeypatch.setattr(photo_inventory, 'prepare_photo',
                        lambda *args: np.zeros((640, 640, 3), np.uint8))
    requests = []
    def review(**kwargs):
        if kwargs['tool']['function']['name'] == 'observe_body':
            # Contratto aggiornato (HANDOFF 144, 148): i corpi tenuti si rileggono
            # dal loro ritaglio dopo l'inventario; qui conta solo l'inventario.
            # v5 contract (review D3): every required field, class triple included.
            return {'visual_class': 'unknown', 'visual_class_confidence': 'low', 'visual_class_evidence': '', 'contacts_complete': True, 
                    'bodies_in_frame': 1, 'metal_contacts_total': 2,
                    'two_end_terminals_only': True, 'body_shape': 'other',
                    'top_translucent': False, 'something_visible_inside': False,
                    'coloured_patch_at_end': False, 'band_at_end': False,
                    'printed_text': 'none', 'marking': '', 'tab': False, 'size_family': '',
                    'leads_by_side': dict.fromkeys(('left', 'right', 'top', 'bottom'))}
        requests.append(kwargs)
        payload = json.loads(kwargs['content_blocks'][4]['text'])
        assert [c['id'] for c in payload['candidates']] == [1]
        assert payload['candidates'][0]['yolo_confidence'] == pytest.approx(.8)
        assert not payload['candidates'][0]['detector_metadata'].get('_yolo_review_only')
        return dict(components=[], candidate_decisions=[
            dict(candidate_id=c['id'], status='uncertain', component_id=None,
                 rejection_category=None, reason='Physical presence unresolved; preserve for review')
            for c in payload['candidates']], uncertainties=['Unresolved body evidence'])
    report = photo_inventory.extract_inventory(
        [tmp_path / 'top.png'], 'offline/model', 64, 64, [], review,
        detector='yolo', top_photo_index=0,
        cache_dir=tmp_path / 'cache' if audit_enabled else None)
    assert len(requests) == 1 and len(report.components) == 1
    # Il rilevatore consegna anche la debole (visibile in revisione); senza
    # nessuno che la tenga, la richiesta ne porta una sola (vedi `review`).
    assert report.calibration['detector_proposal_count'] == 2
    assert report.photo_png and report.overlay_png
    assert not any(c.get('_yolo_review_only') for c in report.components)


def test_a_clipped_edge_is_named_so_that_nobody_mistakes_it_for_a_measure(setup):
    """@brief Un bordo sul confine della tessera non e' un bordo del corpo.

    E' l'errore che ha rimpicciolito del 15% il modulo radio della scheda di
    prova: tre frammenti tagliati sulla stessa cucitura «concordavano» su un
    bordo che era solo il taglio della tessera, e chi li ha fusi lo ha preso
    per buono. Chi riceve le proposte deve sapere quali lati sono artefatti.
    """
    session, _ = setup
    # Un corpo tagliato dal bordo DESTRO della prima tessera (x=640).
    outputs = [_raw(), _raw(([620, 320, 80, 60], 10, .9)), _raw(), _raw()]
    session.raw = lambda index: outputs[index]
    row = yolo.detect_yolo_components(np.zeros((640, 1000, 3), np.uint8), 100, 64,
                                      include_review_candidates=True)[0]
    assert row['_yolo_clipped_recovery'] and row['_yolo_clipped_sides'] == ['right']


def test_an_edge_another_view_saw_beyond_is_not_clipped_any_more(setup):
    """@brief Caso negativo: la ricomposizione non deve marcare cio' che e' misurato.

    Se un secondo frammento vede oltre quella cucitura, quel bordo e' stato
    osservato davvero e non va dichiarato artefatto: dichiararlo tale
    inviterebbe chi sta a valle a ignorare una misura buona.
    """
    session, _ = setup
    # Due frammenti dello stesso corpo: il primo tagliato dal bordo destro
    # della sua tessera (x=640), il secondo tagliato a sinistra ma che il
    # bordo destro lo VEDE, a x=700.
    outputs = [_raw(), _raw(([570, 320, 140, 60], 10, .9)),
               _raw(([170.5, 320, 339, 60], 10, .8)), _raw()]
    session.raw = lambda index: outputs[index]
    rows = yolo.detect_yolo_components(np.zeros((640, 1000, 3), np.uint8), 100, 64,
                                       include_review_candidates=True)
    merged = [row for row in rows if row['_yolo_clipped_recovery']]
    assert len(merged) == 1, 'i due frammenti dovevano essere ricomposti in uno'
    sides = merged[0]['_yolo_clipped_sides']
    assert 'right' not in sides, 'quel bordo lo ha misurato il secondo frammento'
    assert 'left' in sides, 'quello sinistro resta un taglio di tessera'
    assert merged[0]['_box'][2] == pytest.approx(.7)


def test_a_body_bigger_than_the_overlap_is_measured_by_the_view_that_contains_it(setup):
    """@brief Regola fisica: una tessera non misura cio' che non contiene.

    Un corpo piu' grande della sovrapposizione fra tessere tocca un bordo in
    OGNI tessera che lo contiene, quindi da li' il suo ingombro non e'
    osservabile. Se un'altra vista lo contiene per intero, il frammento non
    aggiunge nulla -- ed era proprio quello a creare il falso accordo su un
    bordo di tessera che ha rimpicciolito del 15% il modulo radio.
    """
    session, _ = setup
    # Vista d'insieme: il corpo intero, ma con poca confidenza. Una tessera lo
    # taglia e lo vede meglio: e' il caso reale, all'incontrario di come si
    # potrebbe pensare. Vista d'insieme -> [187.5, 164, 812.5, 477] px; la
    # tessera 0-640 ne vede [190, 166, 640, 474], tagliato a destra: i lati
    # NON tagliati coincidono con quelli della vista intera, come per uno
    # stesso corpo (prima la prova usava un frammento col bordo sinistro a
    # 480 px, che con quel corpo non poteva essere: vedi il caso negativo).
    outputs = [_raw(([320, 320, 400, 200], 10, .18)),
               _raw(([415, 320, 450, 308], 10, .80)), _raw(), _raw()]
    session.raw = lambda index: outputs[index]
    rows = yolo.detect_yolo_components(np.zeros((640, 1000, 3), np.uint8), 100, 64,
                                       include_review_candidates=True)
    tagliati = [row for row in rows if row['_yolo_clipped_sides']]
    assert not tagliati, 'il frammento non poteva misurare quel corpo'
    assert any(row['_yolo_confidence'] == pytest.approx(.18) for row in rows), \
        'la vista che lo contiene resta, anche se meno sicura'


def test_a_view_that_only_contains_a_fragment_does_not_measure_it(setup):
    """@brief Caso negativo: contenere un frammento non vuol dire essere lo stesso corpo.

    Stesso frammento sicuro del caso positivo, ma la vista d'insieme vede un
    riquadro debole grande quasi quanto la foto. Lo contiene per intero, pero'
    i bordi che il frammento ha visto davvero (sinistro, alto, basso) non sono
    i suoi: non e' la misura di quel corpo, e non va promosso a corpo vero
    grazie alla confidenza del frammento (ESP32-S3-DEV-KIT, 08/10: un riquadro
    61x15 mm a 0,055 era diventato un corpo a soglia operativa).
    """
    session, _ = setup
    outputs = [_raw(([320, 320, 634, 403], 10, .06)),
               _raw(([415, 320, 450, 308], 10, .80)), _raw(), _raw()]
    session.raw = lambda index: outputs[index]
    rows = yolo.detect_yolo_components(np.zeros((640, 1000, 3), np.uint8), 100, 64,
                                       include_review_candidates=True)
    weak = [row for row in rows if row['_yolo_confidence'] == pytest.approx(.06)]
    assert all(row['_yolo_origin'] != 'operating_threshold' for row in weak),         'un riquadro che contiene soltanto non eredita la presenza del frammento'
    assert all(not row.get('_yolo_presence_attested_by') for row in rows)
    assert any(row['_yolo_confidence'] == pytest.approx(.80) and row['_yolo_clipped_sides']
               for row in rows), 'il frammento sicuro resta, marcato come troncato'


def test_a_small_body_touching_a_seam_is_still_an_observed_body(setup):
    """@brief Caso negativo: sotto la sovrapposizione la regola non si applica.

    Un corpo piu' piccolo della sovrapposizione che sfiora una cucitura e'
    comunque contenuto per intero da una tessera vicina: toglierlo perche'
    tocca un bordo perderebbe un componente vero.
    """
    session, _ = setup
    # Stessa struttura del caso positivo -- una vista d'insieme poco sicura che
    # contiene, una tessera che taglia -- ma il corpo e' 100 px contro i 160 di
    # sovrapposizione: quella tessera lo misura, e quindi resta.
    outputs = [_raw(([320, 320, 700, 300], 10, .18)),
               _raw(([600, 320, 100, 80], 3, .70)), _raw(), _raw()]
    session.raw = lambda index: outputs[index]
    rows = yolo.detect_yolo_components(np.zeros((640, 1000, 3), np.uint8), 100, 64,
                                       include_review_candidates=True)
    assert any(row['_yolo_clipped_sides'] for row in rows), \
        'un corpo piccolo sulla cucitura resta, marcato ma presente'


def test_the_overlap_of_a_view_comes_from_its_own_tile_size():
    """@brief La quota che decide viene dalla ricetta, non da un numero a parte."""
    assert yolo.view_overlap_px('tile640-2400-480') == 160
    assert yolo.view_overlap_px('tile1280-960-0') == 320
    assert yolo.view_overlap_px('overview') == float('inf')
    assert yolo.view_overlap_px(None) == float('inf')


def test_weak_bodies_rebuilt_from_seams_reach_the_review_as_opt_in(setup):
    """@brief Ricomposta o isolata, una proposta sotto soglia e' visibile ma da tenere a mano.

    Contratto cambiato il 2026-10-08 (utente, ESP32-S3-DEV-KIT): prima la
    ricomposta sotto soglia restava tenuta per default, perche' la debolezza
    era attribuita al taglio delle tessere. Ma la confidenza di una ricomposta
    e' quella del suo pezzo MIGLIORE: nessun pezzo ha raggiunto la soglia, e il
    taglio non spiega un punteggio basso su ogni pezzo. Sulla scheda reale una
    striscia di serigrafia a 0,10 entrava cosi' fra i corpi. Il corpo non si
    perde: arriva alla revisione, marcato, e lo si tiene con un clic.
    """
    session, _ = setup
    # Tessera 2 e tessera 3 vedono due pezzi dello stesso corpo a cavallo della
    # cucitura x=640, entrambi sotto soglia; altrove un corpo debole isolato.
    outputs = [_raw(([100, 100, 30, 30], 10, .20)),
               _raw(([600, 320, 80, 60], 10, .20)),
               _raw(([120, 320, 80, 60], 10, .20)), _raw()]
    session.raw = lambda index: outputs[index]
    rows = yolo.detect_yolo_components(np.zeros((640, 1000, 3), np.uint8), 100, 64,
                                       include_review_candidates=True)
    rebuilt = [row for row in rows if row['_yolo_clipped_recovery']]
    assert rebuilt, 'il corpo ricomposto dalla cucitura deve arrivare alla revisione'
    isolated = [row for row in rows if not row['_yolo_clipped_recovery']]
    assert isolated
    assert all(row['_yolo_low_confidence'] and yolo.awaiting_user_keep(row)
               for row in rebuilt + isolated),         'sotto soglia, ricomposta o no, esce solo come proposta da tenere a mano'


def test_a_rebuilt_body_with_a_confident_piece_is_kept_by_default(setup):
    """@brief Caso positivo: se un pezzo ha raggiunto la soglia, il corpo ricomposto e' tenuto."""
    session, _ = setup
    outputs = [_raw(), _raw(([500, 50, 300, 40], 10, 0.9)),
               _raw(([145, 50, 290, 40], 10, 0.2)), _raw()]
    session.raw = lambda index: outputs[index]
    rows = yolo.detect_yolo_components(np.zeros((640, 1000, 3), np.uint8), 100, 64,
                                       include_review_candidates=True)
    rebuilt = [row for row in rows if row['_yolo_clipped_recovery']]
    assert len(rebuilt) == 1 and not yolo.awaiting_user_keep(rebuilt[0])


def test_a_chosen_threshold_turns_a_weak_proposal_into_audit_only(setup):
    """@brief Soglia scelta (utente, 2026-09-25): la stessa proposta a 0.40 conta con la
    soglia predefinita (0.25) e non conta con 0.46, il valore usato su Roboflow."""
    session, _ = setup
    raw = np.zeros((1, 25, 8400), np.float32)
    raw[0, :4, 0] = [320, 320, 60, 40]
    raw[0, 4 + _NAMES.index("capacitor"), 0] = 0.40
    session.raw = raw
    photo = np.zeros((640, 640, 3), np.uint8)
    default = yolo.detect_yolo_components(photo, 20, 20)
    chosen = yolo.detect_yolo_components(photo, 20, 20, confidence=0.46)
    assert len(default) >= 1 and chosen == []


@pytest.mark.parametrize("value", [0.05, 0.0, 1.0, 1.5, float("nan"), True, "0.4"])
def test_an_out_of_range_threshold_is_refused(value):
    with pytest.raises(ValueError, match="threshold"):
        yolo.operating_confidence(value)


def test_no_threshold_keeps_the_default():
    assert yolo.operating_confidence(None) == yolo._CONFIDENCE == 0.25
    assert yolo.operating_confidence(0.46) == 0.46


def test_settings_default_to_the_values_used_so_far():
    """@brief «Parametri YOLO» (utente, 2026-09-25): nessuna scelta = comportamento di prima."""
    assert yolo.yolo_settings() == {"confidence": 0.25, "nms_iou": 0.5,
                                    "tile_sizes": [640, 1280], "tile_overlap": 0.25}
    assert [name for name, _ in yolo._views(2593, 1042)] == [
        name for name, _ in yolo._views(2593, 1042, [640, 1280], 0.25)]


@pytest.mark.parametrize("values", [
    {"nms_iou": 0.05}, {"nms_iou": True}, {"tile_overlap": 0.9},
    {"tile_sizes": [512]}, {"tile_sizes": [640, 640]}, {"tile_sizes": "640"},
    {"confidence": 2}, {"stride": 32},
])
def test_invalid_settings_are_refused(values):
    with pytest.raises(ValueError):
        yolo.yolo_settings(values)


def test_without_tiles_only_the_whole_photo_is_seen(setup):
    """@brief Positivo: senza tessere una sola passata, come Roboflow. Negativo: con i
    predefiniti le viste sono piu' di una."""
    session, _ = setup
    photo = np.zeros((1042, 2593, 3), np.uint8)
    yolo.detect_yolo_components(photo, 60, 24, settings={"tile_sizes": []})
    assert len(session.calls) == 1
    session.calls.clear()
    yolo.detect_yolo_components(photo, 60, 24)
    assert len(session.calls) > 1


def test_a_stricter_overlap_merges_boxes_the_default_keeps_apart(setup):
    """@brief Due riquadri con IoU ~0.43: distinti a 0.50 (predefinito), uno solo a 0.40."""
    session, _ = setup
    raw = np.zeros((1, 25, 8400), np.float32)
    cap = 4 + _NAMES.index("capacitor")
    raw[0, :4, 0] = [300, 320, 100, 60]
    raw[0, :4, 1] = [340, 320, 100, 60]
    raw[0, cap, 0], raw[0, cap, 1] = 0.9, 0.8
    session.raw = raw
    photo = np.zeros((640, 640, 3), np.uint8)
    apart = yolo.detect_yolo_components(photo, 20, 20, settings={"tile_sizes": []})
    merged = yolo.detect_yolo_components(photo, 20, 20, settings={"tile_sizes": [], "nms_iou": 0.4})
    assert len(apart) == 2 and len(merged) == 1


def test_a_box_on_the_surface_of_a_confident_body_is_demoted_not_deleted(setup):
    """@brief Positivo: cio' che sta sopra un corpo gia' trovato non e' un altro corpo.

    Vista d'insieme: un modulo intero e sicuro, un riquadro piu' piccolo (il
    lamierino) col centro sopra di lui, e un corpo vicino col centro FUORI.
    Il lamierino resta visibile ma da tenere a mano; il vicino resta un corpo.
    """
    session, _ = setup
    outputs = [_raw(([320, 320, 400, 200], 10, .9), ([330, 320, 100, 100], 11, .8),
                    ([560, 320, 40, 40], 3, .7)), _raw(), _raw(), _raw()]
    session.raw = lambda index: outputs[index]
    rows = yolo.detect_yolo_components(np.zeros((640, 1000, 3), np.uint8), 100, 64,
                                       include_review_candidates=True)
    by_conf = {round(row['_yolo_confidence'], 2): row for row in rows}
    module, can, neighbour = by_conf[.9], by_conf[.8], by_conf[.7]
    assert not module['_yolo_low_confidence'] and not neighbour['_yolo_low_confidence']
    assert can['_yolo_on_body_of'] == module['_yolo_proposal_id']
    assert can['_yolo_low_confidence'] and yolo.awaiting_user_keep(can)
    from kicadgen.gui import photo_review_dialog as prd
    assert 'sopra un altro corpo' in prd.detector_row_label(can, lambda text: text)


@pytest.mark.parametrize("container_confidence, operating", [(.2, .15), (.08, .06)])
def test_a_weak_container_demotes_nothing_even_with_a_lowered_threshold(
        setup, container_confidence, operating):
    """@brief Negativo: un riquadro grande poco sicuro non fa da contenitore.

    Con la soglia operativa abbassata a mano il riquadro grande e' «tenuto»,
    ma la sua confidenza resta sotto quella di fabbrica: potrebbe essere un
    riquadro spurio grande come la scheda, e i corpi dentro restano corpi.
    """
    session, _ = setup
    outputs = [_raw(([320, 320, 400, 200], 10, container_confidence),
                    ([330, 320, 100, 100], 11, .8)), _raw(), _raw(), _raw()]
    session.raw = lambda index: outputs[index]
    rows = yolo.detect_yolo_components(np.zeros((640, 1000, 3), np.uint8), 100, 64,
                                       include_review_candidates=True, confidence=operating)
    assert not any(row.get('_yolo_on_body_of') for row in rows)
    inner = [row for row in rows if row['_yolo_confidence'] == pytest.approx(.8)]
    assert inner and not inner[0]['_yolo_low_confidence']


def test_a_body_cut_by_two_seams_is_rebuilt_as_one(setup):
    """@brief Positivo: tre tessere vedono tre pezzi dello stesso corpo, nessuna intero.

    Corpo da 300 a 1300 px: la tessera 0-640 lo vede tagliato a destra, la
    480-1120 tagliato su entrambi i lati, la 960-1600 tagliato a sinistra. Il
    primo e l'ultimo pezzo non hanno strisce comuni: si legano attraverso quello
    centrale, che coincide con ciascuno dove le tessere vedono insieme.
    """
    session, _ = setup
    outputs = [_raw(), _raw(([470, 320, 340, 100], 10, .9)),
               _raw(([320, 320, 640, 100], 10, .8)),
               _raw(([170, 320, 340, 100], 10, .7)), _raw(), _raw()]
    session.raw = lambda index: outputs[index]
    rows = yolo.detect_yolo_components(np.zeros((640, 1600, 3), np.uint8), 160, 64)
    assert len(rows) == 1
    assert rows[0]['_box'] == pytest.approx((300 / 1600, 270 / 640, 1300 / 1600, 370 / 640))


def test_two_offset_pieces_in_a_seam_strip_stay_two_bodies(setup):
    """@brief Negativo: due pezzi sfalsati nella striscia comune non sono lo stesso corpo.

    La tessera 0-640 vede un pezzo a y 270-370 tagliato a destra, la 360-1000
    uno a y 330-430 tagliato a sinistra. Nella striscia 360-640, vista da
    entrambe, i due riquadri si sovrappongono solo per 40 px su 160 di altezza:
    corpi diversi, che non vanno uniti in uno solo.
    """
    session, _ = setup
    outputs = [_raw(), _raw(([470, 320, 340, 100], 10, .9)),
               _raw(([220, 380, 440, 100], 10, .8)), _raw()]
    session.raw = lambda index: outputs[index]
    rows = yolo.detect_yolo_components(np.zeros((640, 1000, 3), np.uint8), 100, 64)
    assert len(rows) == 2


def test_a_box_overlapping_the_edge_of_a_confident_body_is_its_pad(setup):
    """@brief Positivo: centro fuori ma riquadro sovrapposto al corpo = piazzola di quel corpo.

    Due corpi distinti tengono un gap di courtyard fra loro: i riquadri non
    si sovrappongono. Un riquadro piccolo a cavallo del bordo di un corpo
    sicuro e' un suo contatto, retrocesso e mai cancellato.
    """
    session, _ = setup
    # Modulo 120..520 x 220..420 (input px); piazzola 100..130 con centro a
    # 115 (fuori) ma 10 px dentro il bordo sinistro; passivo a 60..100,
    # separato da 20 px di gap: resta un corpo.
    outputs = [_raw(([320, 320, 400, 200], 10, .9), ([115, 320, 30, 20], 3, .8),
                    ([80, 320, 40, 20], 3, .7)), _raw(), _raw(), _raw()]
    session.raw = lambda index: outputs[index]
    rows = yolo.detect_yolo_components(np.zeros((640, 1000, 3), np.uint8), 100, 64,
                                       include_review_candidates=True)
    by_conf = {round(row['_yolo_confidence'], 2): row for row in rows}
    pad, neighbour = by_conf[.8], by_conf[.7]
    assert pad['_yolo_on_body_of'] == by_conf[.9]['_yolo_proposal_id'] and pad['_yolo_abuts_body']
    assert yolo.awaiting_user_keep(pad)
    assert not neighbour['_yolo_low_confidence'] and not neighbour.get('_yolo_on_body_of')


def test_a_box_merely_touching_a_body_box_is_not_its_pad(setup):
    """@brief Negativo: contatto a zero sovrapposizione non basta: serve un'area in comune."""
    session, _ = setup
    outputs = [_raw(([320, 320, 400, 200], 10, .9), ([100, 320, 40, 20], 3, .8)),
               _raw(), _raw(), _raw()]
    session.raw = lambda index: outputs[index]
    rows = yolo.detect_yolo_components(np.zeros((640, 1000, 3), np.uint8), 100, 64,
                                       include_review_candidates=True)
    small = [row for row in rows if row['_yolo_confidence'] == pytest.approx(.8)][0]
    assert not small.get('_yolo_on_body_of') and not small['_yolo_low_confidence']


# An end-to-end ONNX export (YOLO26 with `nms=False`) returns the one-to-one
# detections [1, K, 6]. Since 2026-10-28 the ONNX path reads them with the
# same conversion as the .pt path, so the installed program (ONNX only) finds
# the bodies the checkpoint finds. Verified the same day on two real photos:
# the export matches the PyTorch one-to-one output row for row (3e-4).
def _end_to_end_session(session, names=_NAMES, rows=300):
    session.outputs[0].shape = [1, rows, 6]
    session.metadata["names"] = repr(dict(enumerate(names)))
    session.metadata["end2end"] = "True"
    raw = np.zeros((1, rows, 6), np.float32)
    # Row 0: one box, class 10 ("ic"), 0.9. Row 1: the SAME anchor again with
    # class 13 ("pads"), as the one-to-one head ranks anchors x classes.
    # Row 2: another box with a class id that is not integral -> no class.
    raw[0, 0] = [10, 20, 50, 60, 0.9, 10]
    raw[0, 1] = [10, 20, 50, 60, 0.3, 13]
    raw[0, 2] = [100, 100, 140, 120, 0.8, 2.7]
    session.raw = raw
    return raw


def test_end_to_end_onnx_export_is_read_like_the_checkpoint(setup):
    """@brief Positive: [1, K, 6] output -> rows [1, 25, K], the .pt conversion."""
    session, _ = setup
    raw = _end_to_end_session(session)
    info = yolo.validate_yolo_setup()
    assert info["output_shape"] == [1, 25, 300] and info["model_output_shape"] == [1, 300, 6]
    assert info["raw_head"].startswith("one-to-one")
    assert not session.calls
    model_session, input_name, np_module, *_ = yolo._setup(None)
    out = model_session.run(None, {input_name: np.zeros((1, 3, 640, 640), np.float32)})[0]
    assert out.shape == (1, 25, 300) and out.dtype == np.float32
    assert np.array_equal(out, yolo._one_to_one_rows(raw, list(range(25 - 4)), np_module))
    # Centre/size from the corners, both classes of the same anchor merged in
    # the first row, the second row emptied, the non-integral id dropped.
    assert out[0, :4, 0].tolist() == [30, 40, 40, 40]
    assert out[0, 4 + 10, 0] == pytest.approx(0.9) and out[0, 4 + 13, 0] == pytest.approx(0.3)
    assert not out[0, :, 1].any() and not out[0, 4:, 2].any()


def test_end_to_end_onnx_export_with_other_classes_is_remapped_by_name(setup):
    """@brief Positive: a 16-class end-to-end export (YOLO26 «_l») keeps its names."""
    session, _ = setup
    declared = ["IC", "LED", "battery", "buzzer", "capacitor", "clock", "connector", "diode",
                "display", "fuse", "inductor", "potentiometer", "relay", "resistor",
                "switch", "transistor"]
    _end_to_end_session(session, names=declared, rows=100)
    session.raw[0, 0, 5] = 0      # model class 0 = "IC" -> detector class 10
    session.raw[0, 1, 5] = 1      # model class 1 = "LED" -> detector class 12
    info = yolo.validate_yolo_setup()
    assert info["class_order"] == "remapped by name" and info["output_shape"] == [1, 25, 100]
    assert "pads" in info["missing_classes"]
    model_session, input_name, *_ = yolo._setup(None)
    out = model_session.run(None, {input_name: np.zeros((1, 3, 640, 640), np.float32)})[0]
    assert out[0, 4 + 10, 0] == pytest.approx(0.9) and out[0, 4 + 12, 0] == pytest.approx(0.3)
    assert not out[0, 4 + 13].any()


@pytest.mark.parametrize("shape", [[1, 300, 5], [1, 300, 7], [1, "rows", 6], [1, 0, 6],
                                   [2, 300, 6], [300, 6], [1, 300, 6, 1]])
def test_an_output_that_is_neither_grid_nor_end_to_end_is_rejected(setup, shape):
    """@brief Negative: only [1, 4 + classes, 8400] or [1, K, 6] with a fixed K>=1."""
    session, _ = setup
    session.outputs[0].shape = shape
    session.metadata["end2end"] = "True"   # the metadata alone must not open the door
    with pytest.raises(ValueError, match="compatible local YOLO model"):
        yolo.validate_yolo_setup()
    assert not session.calls
