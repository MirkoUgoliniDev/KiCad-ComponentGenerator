"""@brief Detect generic PCB envelopes with a strictly local, optional ONNX model.

@note The caller supplies an already calibrated BGR board crop. This module does
not calibrate, download weights, infer package identity, or measure height. The
frozen experiment recipe uses one overview and native 640/1280-pixel tiles with
25% overlap, confidence 0.25, and class-agnostic IoU NMS 0.50. View count depends
on image dimensions (41 for 4000 x 1848); no components are silently capped.
"""
from __future__ import annotations

import ast
from collections import Counter
import hashlib
import json
import math
import os
from numbers import Real
from pathlib import Path
import tempfile
try:
    from .messages import tx
except ImportError:  # caricato come file a se' (test di isolamento degli import)
    from kicadgen.services.messages import tx


_CLASSES = (
    "battery", "button", "buzzer", "capacitor", "clock", "connector", "diode",
    "display", "fuse", "heatsink", "ic", "inductor", "led", "pads", "pins",
    "potentiometer", "relay", "resistor", "switch", "transformer", "transistor",
)
_CONFIDENCE = 0.25
_AUDIT_CONFIDENCE = 0.05
_NMS_IOU = 0.5
_EXCLUDED = {"pads", "pins"}


def detector_classes() -> tuple:
    """@brief Categorie che il rilevatore locale sa assegnare.

    @return Le classi note, escluse quelle che non sono corpi montati.
    @note Le piazzole nude e le file di pin non sono componenti: restano fuori
        anche dall'elenco che si propone a chi rivede, perche' sceglierle
        sarebbe un errore che nessuno a valle saprebbe correggere.
    """
    return tuple(name for name in _CLASSES if name not in _EXCLUDED)


def resolve_yolo_model(model_path=None) -> Path:
    """@brief Resolve and check a readable, nonempty local ONNX weights file.

    @param model_path Optional local path overriding KICADGEN_YOLO_MODEL.
    @return Absolute Path, defaulting to repository models/pcb_yolov8s.onnx.
    @throws ValueError If the path is invalid, missing, unreadable, or not ONNX.
    @note No downloads, pickle deserialization, or optional imports occur here.
    """
    selected = model_path if model_path is not None else os.environ.get(
        "KICADGEN_YOLO_MODEL", Path(__file__).resolve().parents[2] / "models" / "pcb_yolov8s.onnx"
    )
    try:
        if not os.fspath(selected).strip():
            raise ValueError("empty model path")
        path = Path(selected).expanduser().resolve()
        if path.suffix.lower() != ".onnx":
            raise ValueError("expected a .onnx file; pickle/.pt weights are unsupported")
        with path.open("rb") as handle:
            if not handle.read(1):
                raise ValueError("weights file is empty")
        return path
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        raise ValueError(
            f"Cannot read local YOLO weights {selected!r}: {exc}. Supply model_path "
            "or set KICADGEN_YOLO_MODEL to the existing 21-class PCB .onnx file."
        ) from exc


def _dependencies():
    # Optional packages must never prevent importing the standard application.
    try:
        import numpy as np
        import cv2
        import onnxruntime as ort
    except (ImportError, OSError) as exc:
        raise ValueError(
            "Local YOLO runtime is unavailable. Install requirements-yolo.txt "
            "in the application's Python environment (numpy, opencv-python and "
            f"CPU onnxruntime), then retry. Details: {exc}"
        ) from exc
    return np, cv2, ort


def _setup(model_path):
    path = resolve_yolo_model(model_path)
    np, cv2, ort = _dependencies()
    try:
        # Use the same bytes for the session and audit digest. Loading bytes also
        # requires self-contained ONNX weights, with no external tensor files.
        weights = path.read_bytes()
        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        session = ort.InferenceSession(
            weights, sess_options=options, providers=["CPUExecutionProvider"]
        )
        inputs, outputs = session.get_inputs(), session.get_outputs()
        if (len(inputs) != 1 or inputs[0].type != "tensor(float)"
                or list(inputs[0].shape) != [1, 3, 640, 640]):
            raise ValueError("expected one float32 input [1, 3, 640, 640]")
        if (len(outputs) != 1 or outputs[0].type != "tensor(float)"
                or list(outputs[0].shape) != [1, 25, 8400]):
            raise ValueError("expected one raw YOLOv8 float32 output [1, 25, 8400]")
        metadata = dict(session.get_modelmeta().custom_metadata_map)
        names = ast.literal_eval(metadata["names"])
        if (not isinstance(names, dict) or any(type(k) is not int for k in names)
                or names != dict(enumerate(_CLASSES))):
            raise ValueError("class metadata must match the supported 21 PCB classes in order")
        if metadata.get("task") != "detect":
            raise ValueError("model task metadata must be 'detect'")
        for key, expected in (("imgsz", [640, 640]), ("batch", 1), ("stride", 32)):
            if key in metadata and ast.literal_eval(metadata[key]) != expected:
                raise ValueError(f"incompatible {key} metadata; expected {expected}")
        providers = session.get_providers()
        if "CPUExecutionProvider" not in providers:
            raise ValueError("CPUExecutionProvider is unavailable")
    except Exception as exc:
        raise ValueError(
            f"Cannot load compatible local YOLO model {path}: {exc}. Use a "
            "self-contained 21-class PCB YOLOv8 ONNX export with fixed 640 input "
            "and raw [1, 25, 8400] output; check the CPU onnxruntime installation."
        ) from exc
    info = {
        "model_path": str(path), "model_sha256": hashlib.sha256(weights).hexdigest(),
        "model_metadata": metadata, "classes": list(_CLASSES),
        "input_shape": list(inputs[0].shape), "output_shape": list(outputs[0].shape),
        "providers": providers, "runtime": getattr(ort, "__version__", "unknown"),
    }
    return session, inputs[0].name, np, cv2, info


def validate_yolo_setup(model_path=None) -> dict:
    """@brief Validate local weights, optional CPU runtime, and model contract.

    @param model_path Optional weights path; resolution follows resolve_yolo_model.
    @return Dictionary containing model path/hash, metadata, classes and shapes.
    @throws ValueError For missing weights/runtime or an incompatible ONNX model.
    @note Creates a CPU session but does not run inference or access the network.
    """
    return _setup(model_path)[-1]


#: Frazione di tessera che si ripete nella successiva. Il passo e' il
#: complemento: e' la stessa costante usata da _views, tenuta qui perche' da
#: essa dipende quale corpo una tessera puo' misurare e quale no.
_TILE_OVERLAP_FRACTION = 0.25


def view_overlap_px(view):
    """@brief Quanto una tessera si sovrappone alla successiva, in pixel.

    @param view Nome della vista, come lo genera _views.
    @return Pixel di sovrapposizione, o infinito per la vista d'insieme.
    @note E' la quota che decide che cosa quella vista puo' misurare: un corpo
        piu' grande della sovrapposizione tocca un bordo in OGNI tessera che lo
        contiene, quindi da li' il suo ingombro non e' osservabile, per quanto
        il rilevamento sia sicuro.
    """
    name = str(view or "")
    if not name.startswith("tile"):
        return float("inf")
    try:
        size = int(name.split("-")[0][4:])
    except ValueError:
        return float("inf")
    return size * _TILE_OVERLAP_FRACTION


def _prefer_complete_views(rows, complete):
    """@brief Toglie i frammenti che nessuna tessera poteva misurare.

    @param rows Righe da filtrare.
    @param complete Rilevamenti non tagliati, da QUALUNQUE vista e a qualunque
        confidenza: che una vista abbia visto il corpo per intero e' un fatto
        geometrico e non dipende dalla soglia operativa. Restringerlo ai soli
        candidati sopra soglia lasciava passare proprio il caso che conta --
        sulla scheda di prova la vista d'insieme vedeva il modulo intero a
        0,18, sotto soglia, e il confronto non la trovava.
    @return Le righe da tenere.
    @note Regola fisica, non una soglia: un corpo piu' grande della
        sovrapposizione fra tessere tocca un bordo in ogni tessera che lo
        contiene, quindi quella vista non ne conosce l'ingombro. Se un'altra
        vista lo contiene per intero, e' quella a misurarlo, e il frammento non
        aggiunge nulla -- anzi, e' cio' che creava il falso accordo su un bordo
        di tessera. Sotto la sovrapposizione la regola non si applica: un corpo
        piccolo che sfiora una cucitura resta un corpo osservato.
    """
    keep = []
    for row in rows:
        sides = row.get("clipped_sides")
        box = row["box"]
        size = max(box[2] - box[0], box[3] - box[1])
        covering = [other for other in complete if other is not row
                    and _cover(box, other["box"]) >= _FRAGMENT_COVERED]
        if sides and size > view_overlap_px(row.get("view")) and covering:
            # Presenza ed ingombro sono due fatti distinti. Se il frammento che
            # cede il posto era sopra la soglia operativa, la PRESENZA del corpo
            # e' attestata da un rilevamento sicuro: a cedere e' solo la misura
            # dell'ingombro, che passa alla vista che lo contiene. Quella vista
            # va quindi tenuta anche se debole, altrimenti si perderebbe due
            # volte un corpo che era stato visto benissimo.
            if row.get("rejection") is None:
                for other in covering:
                    other["attests_presence"] = True
            continue
        keep.append(row)
    return keep


def _views(width, height):
    yield "overview", (0, 0, width, height)
    for size in (640, 1280):
        def starts(length):
            values = range(0, max(1, length - size + 1),
                           int(size * (1 - _TILE_OVERLAP_FRACTION)))
            return sorted(set(values) | {max(0, length - size)})

        for y in starts(height):
            for x in starts(width):
                yield f"tile{size}-{x}-{y}", (x, y, min(width, x + size), min(height, y + size))


def _letterbox(image, np, cv2):
    height, width = image.shape[:2]
    ratio = min(640 / width, 640 / height)
    nw, nh = max(1, round(width * ratio)), max(1, round(height * ratio))
    left, top = round((640 - nw) / 2 - 0.1), round((640 - nh) / 2 - 0.1)
    canvas = np.full((640, 640, 3), 114, np.uint8)
    canvas[top:top + nh, left:left + nw] = cv2.resize(
        image, (nw, nh), interpolation=cv2.INTER_LINEAR
    )
    tensor = np.ascontiguousarray(canvas[:, :, ::-1].transpose(2, 0, 1)[None], dtype=np.float32)
    return tensor / 255.0, ratio, left, top


def _nms(rows, np, trace=None, stage=None):
    """Suppress alternate boxes regardless of class; do not suppress containment alone."""
    if not rows:
        return []
    boxes = np.array([row["box"] for row in rows], dtype=float)
    areas = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
    order = np.argsort(-np.array([row["confidence"] for row in rows]), kind="stable")
    retained = []
    while order.size:
        index, rest = order[0], order[1:]
        retained.append(rows[index])
        wh = np.maximum(0, np.minimum(boxes[index, 2:], boxes[rest, 2:])
                        - np.maximum(boxes[index, :2], boxes[rest, :2]))
        intersection = wh[:, 0] * wh[:, 1]
        overlap = intersection / (areas[index] + areas[rest] - intersection)
        if trace is not None:
            for suppressed, iou in zip(rest[overlap > _NMS_IOU], overlap[overlap > _NMS_IOU]):
                trace.append({"proposal_id": rows[suppressed]["proposal_id"],
                              "suppressed_by": rows[index]["proposal_id"],
                              "stage": stage, "iou": float(iou)})
        order = rest[overlap <= _NMS_IOU]
    return retained


#: Quanto un frammento deve stare dentro un rilevamento intero perche' sia lo
#: stesso corpo, gia' visto meglio: sotto questa frazione della propria area
#: il frammento e' l'unica prova che quel corpo esista.
_FRAGMENT_COVERED = 0.5


def _cover(first, second):
    """@brief Frazione del primo rettangolo che sta dentro il secondo."""
    wide = max(0.0, min(first[2], second[2]) - max(first[0], second[0]))
    high = max(0.0, min(first[3], second[3]) - max(first[1], second[1]))
    area = (first[2] - first[0]) * (first[3] - first[1])
    return (wide * high / area) if area > 0 else 0.0


def _recover_clipped(retained, fragments):
    """@brief Ricompone i corpi che nessuna vista ha visto per intero.

    @param retained Rilevamenti interni sopravvissuti alla NMS globale.
    @param fragments Righe scartate perche' toccavano il bordo di una tessera.
    @return Righe ricomposte, con la confidenza migliore del gruppo.
    @note Il taglio di una tessera non e' prova di assenza. La regola che
        scarta i frammenti serve a togliere i doppioni di cio' che un'altra
        vista vede per intero: quando NESSUNA vista lo vede intero, scartare
        significa perdere il corpo. Succede a chi e' piu' grande della
        sovrapposizione fra tessere -- 160 px su tessere da 640 -- e allora il
        corpo tocca un bordo in ogni tessera che lo contiene. Le sovrapposizioni
        si potrebbero allargare, ma nessuna larghezza fissa e' garantita:
        l'ingombro dei corpi non si conosce prima di averli visti.
    @note L'unione dei frammenti resta un ingombro TRONCATO quando il taglio e'
        su un bordo che nessuna tessera supera: e' un'ipotesi da rivedere, non
        una misura, e chi la riceve la marca come tale.
    @note Separazione dichiaratamente fragile. Il raggruppamento lega ogni
        frammento al capostipite, non al precedente, ma sulla scheda di prova
        l'unione piu' grande cresce comunque a quattro volte l'area del
        capostipite (31 frammenti, da 6,4x11,3 a 19,7x14,7 mm). Va bene per
        dire «qui c'e' un corpo», non per dire quanto e' grande: chi misura e'
        la riconciliazione, che riceve queste righe come ipotesi incerte.
    """
    orphans = [row for row in fragments
               if not any(_cover(row["box"], kept["box"]) >= _FRAGMENT_COVERED
                          for kept in retained)]
    groups = []
    for row in sorted(orphans, key=lambda row: -row["confidence"]):
        for group in groups:
            # Ogni frammento deve coprire il CAPOSTIPITE del gruppo, non un
            # membro qualsiasi: con il legame a catena l'unione puo' camminare
            # per tutta la scheda, un frammento alla volta. Legata al
            # capostipite, ogni membro sta per meta' dentro di lui, quindi
            # l'unione resta nell'ordine del doppio in ogni direzione.
            # ATTENZIONE, misurato sulla scheda vera: non basta a rendere
            # l'unione stretta. Il gruppo piu' grande resta di 31 frammenti e
            # cresce fino a QUATTRO VOLTE l'area del capostipite (6,4x11,3 mm
            # -> 19,7x14,7 mm). L'ingombro di un corpo ricomposto e' percio'
            # un'ipotesi grossolana, non una misura: per questo esce marcato e
            # la riconciliazione lo rimisura.
            # La porzione condivisa, non la IoU: due meta' dello stesso corpo
            # si coprono a vicenda ma hanno IoU bassa, ed e' proprio per
            # questo che la NMS non le aveva unite.
            if max(_cover(row["box"], group[0]["box"]),
                   _cover(group[0]["box"], row["box"])) >= _FRAGMENT_COVERED:
                group.append(row)
                break
        else:
            groups.append([row])
    recovered = []
    for group in groups:
        best = max(group, key=lambda row: row["confidence"])
        box = [min(row["box"][0] for row in group), min(row["box"][1] for row in group),
               max(row["box"][2] for row in group), max(row["box"][3] for row in group)]
        # Dopo l'unione un lato resta artificiale solo se TUTTI i frammenti che
        # lo toccano erano tagliati li': se un altro pezzo vede oltre, quel
        # bordo e' stato misurato davvero.
        outer = {'left': box[0], 'top': box[1], 'right': box[2], 'bottom': box[3]}
        index_of = {'left': 0, 'top': 1, 'right': 2, 'bottom': 3}
        still_clipped = [side for side in outer
                         if all(side in row.get('clipped_sides', ())
                                for row in group
                                if row['box'][index_of[side]] == outer[side])]
        recovered.append(dict(best, box=box, clipped_by_tile=True,
                              clipped_sides=still_clipped,
                              recovered_from=[row["proposal_id"] for row in group],
                              rejection=(None if best["confidence"] >= _CONFIDENCE
                                         else "below_confidence")))
    return recovered


def _proposals(raw, bounds, image_size, transform, view, np):
    """Keep auditable pre-NMS proposals; reject malformed or cropped body boxes."""
    x, y, x1, y1 = bounds
    width, height = image_size
    ratio, left, top = transform
    predictions = raw[0].T
    scores = predictions[:, 4:]
    body_classes = np.array([i for i, name in enumerate(_CLASSES) if name not in _EXCLUDED])
    valid = (np.isfinite(predictions).all(axis=1)
             & (scores >= 0).all(axis=1) & (scores <= 1).all(axis=1))
    classes = scores.argmax(axis=1)
    confidences = scores[np.arange(len(scores)), classes]
    rows, invalid_count = [], int((~valid).sum())
    for index in np.flatnonzero(valid & (confidences >= _AUDIT_CONFIDENCE)):
        prediction = predictions[index]
        class_id = int(classes[index])
        confidence = float(confidences[index])
        body_id = int(body_classes[scores[index, body_classes].argmax()])
        body_confidence = float(scores[index, body_id])
        # Argmax alone is not evidence of absence. Preserve a competing body
        # hypothesis at the SAME operating threshold, as uncertain review input.
        # This is a recall heuristic, not validation that a pad is a mounted body.
        ambiguous_body = _CLASSES[class_id] in _EXCLUDED and body_confidence >= _CONFIDENCE
        selected_id = body_id if ambiguous_body else class_id
        selected_confidence = body_confidence if ambiguous_body else confidence
        cx, cy, bw, bh = map(float, prediction[:4])
        local = [(cx - bw / 2 - left) / ratio, (cy - bh / 2 - top) / ratio,
                 (cx + bw / 2 - left) / ratio, (cy + bh / 2 - top) / ratio]
        # Quali lati poggiano sul CONFINE DELLA TESSERA e non sul corpo: sono
        # artefatti del ritaglio, non bordi misurati, e chi li riceve deve
        # saperlo. Un bordo interno della tessera resta escluso dal conteggio
        # quando coincide con il bordo dell'immagine, che invece e' reale.
        clipped_sides = [name for name, touching in (
            ('left', x > 0 and local[0] <= 1),
            ('top', y > 0 and local[1] <= 1),
            ('right', x1 < width and local[2] >= x1 - x - 1),
            ('bottom', y1 < height and local[3] >= y1 - y - 1)) if touching]
        cut = bool(clipped_sides)
        # Clip letterbox padding to this view's actual image, then to the board.
        # Interior boundary fragments remain excluded even after clipping.
        box = [max(x, min(x1, local[0] + x)), max(y, min(y1, local[1] + y)),
               max(x, min(x1, local[2] + x)), max(y, min(y1, local[3] + y))]
        reason = None
        if bw <= 0 or bh <= 0 or box[2] <= box[0] or box[3] <= box[1]:
            reason = "empty_or_inverted_box"
        elif _CLASSES[class_id] in _EXCLUDED and not ambiguous_body:
            reason = "excluded_class"
        elif cut:
            reason = "clipped_by_tile"
        elif confidence < _CONFIDENCE:
            reason = "below_confidence"
        rows.append({"proposal_id": f"{view}:{int(index)}",
                     "view": view, "prediction_index": int(index), "box": box,
                     "raw_xywh": [cx, cy, bw, bh], "class": _CLASSES[selected_id],
                     "confidence": selected_confidence,
                     "argmax_class": _CLASSES[class_id], "argmax_confidence": confidence,
                     "best_body_class": _CLASSES[body_id], "best_body_confidence": body_confidence,
                     "class_scores": [float(score) for score in scores[index]],
                     "class_ambiguous": ambiguous_body, "clipped_by_tile": bool(cut),
                     "clipped_sides": clipped_sides,
                     "rejection": reason})
    return rows, invalid_count


def _dimension(value, name):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite positive number in mm (not a boolean).")
    try:
        number = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite positive number in mm.") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be a finite positive number in mm.")
    return number


def _write_audit(audit_dir, document):
    try:
        directory = Path(audit_dir)
        directory.mkdir(parents=True, exist_ok=True)
        # A unique filename preserves previous runs in a shared audit directory.
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix="yolo-", suffix=".json",
            dir=directory, delete=False,
        ) as handle:
            json.dump(document, handle, indent=2, allow_nan=False)
            return handle.name
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError(f"Cannot write YOLO audit under {audit_dir!r}: {exc}") from exc


def detect_yolo_components(photo, length_mm, width_mm, model_path=None, log=None,
                           audit_dir=None, include_review_candidates=False) -> list[dict]:
    """@brief Detect generic mechanical candidates on an already calibrated photo.

    @param photo Nonempty H x W x 3 uint8 NumPy image in OpenCV BGR order.
    @param length_mm Finite positive board extent along image X, in mm.
    @param width_mm Finite positive board extent along image Y, in mm.
    @param model_path Optional local ONNX weights path; see resolve_yolo_model.
    @param log Optional callable receiving progress and audit-path strings.
    @param audit_dir Optional directory receiving a unique yolo-*.json audit.
    @param include_review_candidates Include the subthreshold bodies REBUILT
        from tile seams as generic uncertain review-only candidates, even
        without an audit directory. Defaults to False for standalone detector
        compatibility. The other weak proposals are never returned: they stay
        in the audit only, because on a populated board they are mostly noise
        over areas where nothing is mounted, and they make the single paid call
        noisier and dearer. Reuses the same inference views; stores weak
        proposals in memory and runs extra NMS (quadratic worst case).
    @return photo_grounding-compatible dictionaries ordered top-to-bottom then
        left-to-right, with normalized _box and board-centered mm coordinates.
    @throws ValueError For invalid photo/dimensions/model/runtime, failed inference
        or an unwritable audit directory. Partial inference is never returned.
    @note X points right and Y down. Long/short dimensions determine rotation
        0/90; height_mm=1.0 is explicitly an estimated, unmeasured placeholder.
        Classes/confidence remain detector evidence only; all kinds are generic.
        Pads/pins winners with competing body evidence at the normal confidence
        threshold survive as uncertain generic candidates, never verified bodies.
        Audit includes model hash, recipe, raw proposals at confidence >=0.05
        before filtering/NMS, invalid-row counts, and retained detector results.
        Schema 2 adds decoded BGR pixel hash, NMS suppressor links and detector
        proposal IDs on components. Low-confidence body proposals are separately
        deduplicated for review; of those, only the ones rebuilt from tile
        seams are returned, and only with include_review_candidates.
        _yolo_origin and _yolo_review_only distinguish operating-threshold output
        from weak review hypotheses. IDs are unique within each returned run.
        No evidence below the audit floor is stored;
        absence in this audit does not prove absence in the model output.
    """
    length_mm = _dimension(length_mm, "length_mm")
    width_mm = _dimension(width_mm, "width_mm")
    if type(include_review_candidates) is not bool:
        raise ValueError("include_review_candidates must be a boolean.")
    if log is not None and not callable(log):
        raise ValueError("log must be callable or None.")
    emit = log if log is not None else lambda message: None
    np, cv2, _ort = _dependencies()
    if (not isinstance(photo, np.ndarray) or photo.ndim != 3
            or photo.shape[2] != 3 or min(photo.shape[:2]) <= 0
            or photo.dtype != np.uint8):
        raise ValueError("photo must be a nonempty uint8 BGR NumPy image of shape H x W x 3.")
    session, input_name, np, cv2, info = _setup(model_path)
    height, width = photo.shape[:2]
    views = list(_views(width, height))
    emit(f"YOLO local: {len(views)} views; confidence {_CONFIDENCE}; "
         f"audit floor {_AUDIT_CONFIDENCE}; class-agnostic NMS IoU {_NMS_IOU}; CPU inference.")
    proposals, candidates, view_records, fragments, whole = [], [], [], [], []
    nms_trace = [] if audit_dir is not None else None
    for view, bounds in views:
        x, y, x1, y1 = bounds
        tensor, ratio, left, top = _letterbox(photo[y:y1, x:x1], np, cv2)
        try:
            result = session.run(None, {input_name: tensor})
            if (len(result) != 1 or not isinstance(result[0], np.ndarray)
                    or result[0].shape != (1, 25, 8400) or result[0].dtype != np.float32):
                raise ValueError("expected float32 output [1, 25, 8400]")
        except Exception as exc:
            raise ValueError(f"Local YOLO inference failed in {view}: {exc}") from exc
        rows, invalid = _proposals(result[0], bounds, (width, height),
                                   (ratio, left, top), view, np)
        eligible = [row for row in rows if row["rejection"] is None]
        selected = _nms(eligible, np, nms_trace, "eligible_per_view")
        candidates.extend(selected)
        # Raccolti sempre, non solo per l'audit: senza di loro un corpo piu'
        # grande della sovrapposizione fra tessere non arriva da nessuna parte.
        fragments.extend(row for row in rows if row["rejection"] == "clipped_by_tile")
        # Chi ha visto un corpo per intero conta come misura anche sotto la
        # soglia operativa: e' un fatto geometrico, non un giudizio.
        whole.extend(row for row in rows if not row["clipped_sides"]
                     and row["rejection"] in (None, "below_confidence"))
        if audit_dir is not None or include_review_candidates:
            proposals.extend(rows)
        view_records.append({"name": view, "bounds": list(bounds), "scale": ratio,
                             "padding_left_top": [left, top], "invalid_rows": invalid,
                             "valid_rows_below_audit_floor": 8400 - invalid - len(rows),
                             "filter_rejections": dict(Counter(
                                 row["rejection"] for row in rows if row["rejection"])),
                             "eligible_before_nms": len(eligible),
                             "raw_proposals": len(rows), "retained_before_global_nms": len(selected)})
        emit(f"YOLO local {view}: {len(selected)} candidates; {invalid} invalid rows rejected.")
    retained = _nms(candidates, np, nms_trace, "global")
    # Cio' che nessuna vista ha visto intero torna in gioco: sopra soglia come
    # candidato, sotto soglia come ipotesi da rivedere, come qualunque altro.
    recovered = _recover_clipped(retained, fragments)
    confident = [row for row in recovered if row["rejection"] is None]
    if confident:
        retained = _nms(retained + confident, np, nms_trace, "global")
    retained.sort(key=lambda row: (row["box"][1], row["box"][0]))
    # Chi ha visto un corpo per intero vale come misura anche se non era
    # abbastanza sicuro da diventare un candidato.
    before_preference = len(retained)
    retained = _prefer_complete_views(retained, whole)
    if len(retained) < before_preference:
        emit(f"YOLO local: {before_preference - len(retained)} clipped detections dropped; "
             "they are larger than the tile overlap, so that view could not measure them, "
             "and a view containing them whole is available.")
    if recovered:
        emit(f"YOLO local: {len(recovered)} bodies rebuilt from tile seams "
             f"({len(confident)} at the operating threshold); no view saw them whole, "
             "so their extent is truncated and must be reviewed.")
    # Retained detections outrank weak proposals. Excluded classes cannot enter
    # through this route, regardless of audit storage.
    review_proposals = []
    if audit_dir is not None or include_review_candidates:
        review_pool = retained + [row for row in proposals if row["rejection"] == "below_confidence"]
        review_pool += [row for row in recovered if row["rejection"] == "below_confidence"]
        review_proposals = _prefer_complete_views(
            [row for row in _nms(review_pool, np) if row["rejection"] == "below_confidence"],
            whole)
    # Le proposte deboli non arrivano piu' alla revisione ne' al modello: sulla
    # scheda di prova erano 35 su 82, quasi tutte su zone dove non c'e' nulla
    # montato, e rendevano piu' rumorosa (e piu' cara) l'unica chiamata a
    # pagamento. Restano nell'audit, dove si possono sempre guardare.
    # Fanno eccezione i corpi RICOMPOSTI dalle cuciture fra tessere: quelli
    # stanno sotto soglia per un motivo geometrico -- nessuna vista ne ha visto
    # piu' di un pezzo -- non perche' il rilevatore dubiti che il corpo ci sia.
    # Scartarli vorrebbe dire perdere proprio i corpi che nessuna tessera
    # contiene per intero, che sono quelli piu' facili da non vedere a occhio.
    rebuilt = [row for row in review_proposals
               if row.get("recovered_from") or row.get("attests_presence")]
    returned = retained + (rebuilt if include_review_candidates else [])
    returned.sort(key=lambda row: (row["box"][1], row["box"][0]))
    components = []
    for identifier, row in enumerate(returned, 1):
        x0, y0, x1, y1 = [v / dim for v, dim in zip(row["box"], (width, height, width, height))]
        sx, sy = (x1 - x0) * length_mm, (y1 - y0) * width_mm
        components.append({
            "name": f"YOLO candidate {identifier}", "kind": "generic",
            "x_mm": ((x0 + x1) / 2 - 0.5) * length_mm,
            "y_mm": ((y0 + y1) / 2 - 0.5) * width_mm,
            "length_mm": max(sx, sy), "width_mm": min(sx, sy),
            "rotation_deg": 0 if sx >= sy else 90, "height_mm": 1.0,
            "_height_source": "estimated_placeholder_not_measured", "_height_estimated": True,
            "color_hex": "#303030", "marking": "", "confidence": "low",
            "_box": (x0, y0, x1, y1),
            "_source": ("YOLO local, ricomposto da cucitura" if row.get("recovered_from")
                        else "YOLO local"),
            "_uncertain": True, "_id": identifier,
            "_yolo_class": row["class"], "_yolo_confidence": row["confidence"],
            "_yolo_proposal_id": row["proposal_id"],
            "_yolo_class_ambiguous": row["class_ambiguous"],
            # L'ingombro di un corpo ricomposto e' troncato dal bordo che lo ha
            # tagliato: e' un'ipotesi da rivedere, non una misura.
            "_yolo_clipped_recovery": bool(row.get("recovered_from")),
            # Lati che poggiano sul confine di una tessera: non sono misure.
            "_yolo_clipped_sides": list(row.get("clipped_sides") or ()),
            "_yolo_origin": ("low_confidence_review" if row["rejection"] == "below_confidence"
                             else "operating_threshold"),
            "_yolo_review_only": row["rejection"] == "below_confidence",
        })
    if audit_dir is not None:
        audit = dict(info, audit_schema_version=2,
                     image_sha256=hashlib.sha256(np.ascontiguousarray(photo).tobytes()).hexdigest(),
                     image_hash_basis="decoded uint8 BGR pixels, C order; shape in image_size",
                     image_size=[width, height], board_size_mm=[length_mm, width_mm],
                     audit_scope="Detector only; proposals below audit floor are not recorded; "
                                 "downstream AI decisions require their own audit.",
                     recipe={"overview": True, "input_size": 640, "tile_sizes": [640, 1280],
                             "overlap_fraction": 0.25, "confidence": _CONFIDENCE,
                             "raw_proposal_min_confidence": _AUDIT_CONFIDENCE,
                             "include_review_candidates": include_review_candidates,
                             "nms_iou": _NMS_IOU, "class_agnostic": True,
                             "nms_stages": ["eligible_per_view", "global"],
                             "tile_edge_margin_px": 1, "excluded_classes": sorted(_EXCLUDED),
                             "excluded_class_policy": "retain uncertain competing body class "
                                                      "only at ordinary confidence threshold",
                             "component_limit": None},
                     views=view_records, raw_proposals=proposals,
                     nms_suppressions=nms_trace, recovered_clipped_fragments=recovered,
                     low_confidence_review_proposals=review_proposals,
                     retained_detector_results=retained, components=components)
        emit(f"YOLO local audit: {_write_audit(audit_dir, audit)}")
    if audit_dir is not None or include_review_candidates:
        if include_review_candidates:
            emit(tx("YOLO local: of {n_review_proposals} low-confidence body hypotheses, "
                    "{n_rebuilt} rebuilt from tile seams are included as uncertain "
                    "review-only candidates; the other {value} are weak detections over "
                    "areas the detector was unsure about and stay in the audit only, so "
                    "they neither show in the review nor reach the model.",
                    n_review_proposals=len(review_proposals),
                    n_rebuilt=len(rebuilt),
                    value=len(review_proposals) - len(rebuilt)))
        else:
            emit(tx("YOLO local: {n_review_proposals} additional low-confidence body "
                    "hypotheses saved for review in audit only; not returned.",
                    n_review_proposals=len(review_proposals)))
    emit(f"YOLO local: {len(retained)} operating-threshold candidates; "
         f"returned {len(components)} generic hypotheses; heights are placeholders.")
    return components
