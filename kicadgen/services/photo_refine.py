"""Ritrova nella foto i bordi reali dei componenti, via edge detection.

Il modello linguistico stima bene *quali* componenti ci sono ma sbaglia
spesso *dove* e *come* sono orientati: due stime indipendenti possono
finire sovrapposte, o un componente quasi nero su un PCB quasi nero
(caso comune: case plastico scuro) viene piazzato a occhio senza che il
modello riesca davvero a leggerne i confini.

Un contorno di soglia sul colore fallisce proprio in quel caso (nero su
nero non ha alcun salto di colore da segmentare). Il gradiente di
luminosita' invece c'e' quasi sempre, anche tra due neri leggermente
diversi (lucido vs. opaco, plastica vs. soldermask): per questo qui si usa
**Canny** sul contrasto locale, non una soglia globale.

Procedimento, deterministico e indipendente dal modulo:

1. nella foto dall'alto si trova la scheda (il rettangolo scuro piu'
   grande) e dal suo lato lungo si ricava la scala px/mm; una foto con un
   rapporto lunghezza/larghezza incompatibile con il modulo (vista
   laterale, dettaglio) viene scartata;
2. per ogni componente stimato dal modello si ritaglia una finestra
   intorno alla posizione dichiarata, si cercano i bordi con Canny, si
   dilatano quanto basta a chiudere i contorni spezzati e si cerca il
   contorno chiuso piu' compatibile per area e distanza dal centro;
3. il rettangolo minimo che lo contiene da' posizione, ingombro e
   orientamento (arrotondato al multiplo di 90 gradi piu' vicino, unica
   rotazione che il resto della catena sa applicare) misurati.

Quando la misura non e' affidabile -- nessun contorno utile, area troppo
diversa da quella stimata, spostamento eccessivo rispetto alla stima -- si
tiene la stima del modello: quando NON misura, il risultato non peggiora
mai rispetto a non misurare.

Limite noto, verificato su foto reali (non solo sui casi sintetici dei
test): quando misura, non e' infallibile. Su una scheda densa un piccolo
foro metallizzato (via) o un punto di saldatura isolato puo' avere bordi
abbastanza netti da superare i filtri e passare per un componente,
spostando la stima su una posizione sbagliata invece di lasciarla
inalterata. E' la stessa categoria di errore delle stime del modello
linguistico (a volte sbagliano), non un difetto risolvibile con altra
soglia: va quindi usato come un secondo parere da rivedere nella tabella
componenti, non come una correzione automatica di cui fidarsi alla cieca.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

try:
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover - dipendenza opzionale (opencv-python)
    cv2 = None
    np = None

#: Scarto massimo ammesso tra il rapporto lunghezza/larghezza della scheda
#: trovata e quello del modulo: oltre, la foto non e' una vista dall'alto.
MAX_BOARD_ASPECT_ERROR = 0.12
#: Deriva massima del bordo dovuta a rasterizzazione/contorno irregolare.
#: Il limite angolare evita di accettare vere rotazioni su immagini piccole.
#:
#: La deriva cresce con la lunghezza del contorno: un bordo seghettato dai pad
#: castellati fa leggere a minAreaRect qualche decimo di grado, che su un lato
#: lungo 770 px vale gia' 5.6 px. Con una soglia in pixel *assoluta* la stessa
#: scheda, perfettamente allineata agli assi, passava a bassa risoluzione e
#: veniva scartata ad alta: il criterio diventava la dimensione dell'immagine,
#: non la geometria. L'allowance e' quindi la maggiore tra un minimo in pixel
#: (immagini piccole, dove pochi pixel di rasterizzazione pesano molto) e una
#: frazione del lato lungo. Il limite angolare resta e continua a dominare:
#: 1 deg vale 1.75% di deriva, piu' del rapporto qui sotto.
MAX_BOARD_EDGE_DRIFT_PX = 5.0
MAX_BOARD_EDGE_DRIFT_RATIO = 0.01
MAX_BOARD_TILT_DEG = 1.0
#: Risoluzione minima del ritaglio: sotto, misurare i package non ha senso.
#: A 1.9 px/mm un passivo da 1 mm occupa 2 px, e l'inventario misurerebbe
#: rumore. Serve anche a smascherare i falsi positivi: un rettangolino scuro
#: con il rapporto d'aspetto giusto supera ogni altro filtro, ma non questo.
MIN_BOARD_SCALE_PX_PER_MM = 4.0
#: Frazione minima dell'immagine occupata dalla scheda. In una vista dall'alto
#: la scheda e' il soggetto: un candidato che ne copre una porzione irrisoria
#: e' un dettaglio interno (un connettore, un'etichetta), non il modulo.
MIN_BOARD_IMAGE_FRACTION = 0.02
#: Quanto ci si allontana dalla posizione stimata nel cercare i bordi,
#: oltre il semi-ingombro del componente.
SEARCH_MARGIN_MM = 2.0
#: Area del contorno trovato rispetto a quella stimata: fuori da questo
#: intervallo non e' lo stesso componente (troppo piccolo, o troppo grande
#: perche' fuso con un vicino dalla chiusura morfologica).
AREA_RATIO_RANGE = (0.4, 1.8)
#: Spostamento massimo accettato tra centro stimato e centro misurato.
MAX_SHIFT_MM = 2.0
#: Frazione minima del rettangolo minimo (minAreaRect) effettivamente
#: riempita dal contorno: un componente pieno la riempie quasi tutta, un
#: blob risultato dalla fusione di piu' pezzi vicini (sagoma a "manubrio"
#: o a "L") ne riempie una frazione molto minore.
MIN_FILL_RATIO = 0.45
#: Quanto l'allungamento (lato lungo / lato corto) del contorno puo'
#: discostarsi da quello stimato: un blob fuso e' quasi sempre molto piu'
#: allungato o molto meno del componente che dovrebbe rappresentare.
MAX_ELONGATION_RATIO = 1.5


@dataclass
class BoardFrame:
    """Dove sta la scheda nella foto: centro in px, scala px/mm."""

    cx: float
    cy: float
    scale: float
    width_px: float
    height_px: float

    def to_px(self, x_mm: float, y_mm: float) -> tuple[float, float]:
        """@brief Provide the to px operation for this workflow."""
        # Y del footprint verso il basso, come le righe dell'immagine.
        return self.cx + x_mm * self.scale, self.cy + y_mm * self.scale

    def to_mm(self, x_px: float, y_px: float) -> tuple[float, float]:
        """@brief Provide the to mm operation for this workflow."""
        return (x_px - self.cx) / self.scale, (y_px - self.cy) / self.scale


@dataclass
class RefineReport:
    """@brief Report terminal-refinement changes and unresolved items."""
    photo: Optional[str] = None
    measured: int = 0
    skipped: list = field(default_factory=list)

    def summary(self) -> str:
        """@brief Provide the summary operation for this workflow."""
        if not self.photo:
            return "nessuna foto dall'alto utilizzabile per rifinire i componenti (bordi misurati dalla vista dall'alto: nessuna trovata)"
        text = (
            f"bordi rifiniti dalla foto {Path(self.photo).name}: "
            f"{self.measured} componenti corretti sui bordi reali"
        )
        if self.skipped:
            text += f", {len(self.skipped)} lasciati come stimati (bordi non chiari)"
        return text


def find_board(image, body_length: float, body_width: float) -> Optional[BoardFrame]:
    """Trova il rettangolo della scheda nella foto; None se non e' una vista dall'alto."""
    if not all(math.isfinite(v) and v > 0 for v in (body_length, body_width)):
        return None
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    _, dark = cv2.threshold(gray, 70, 255, cv2.THRESH_BINARY_INV)
    # CLOSE riempie i buchi chiari (componenti, serigrafia) dentro la scheda;
    # OPEN con un kernel proporzionale al lato corto toglie le linee sottili
    # attaccate al contorno (frecce di quota, ombre) che altrimenti
    # allargherebbero il rettangolo e falserebbero la scala.
    close_k = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
    open_size = max(9, int(min(image.shape[:2]) * 0.02) | 1)
    open_k = cv2.getStructuringElement(cv2.MORPH_RECT, (open_size, open_size))
    dark = cv2.morphologyEx(dark, cv2.MORPH_CLOSE, close_k)
    dark = cv2.morphologyEx(dark, cv2.MORPH_OPEN, open_k)
    contours, _ = cv2.findContours(dark, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    expected = body_length / body_width
    # Le foto dei moduli sono spesso corredate da quote: linee e frecce possono
    # formare il contorno più grande, ma con un rapporto molto diverso da quello
    # reale della scheda. Scegliamo quindi il candidato più grande tra quelli
    # geometricamente compatibili, invece di usare sempre max(contourArea).
    candidates = []
    for contour in contours:
        (_, _), (rw, rh), angle = cv2.minAreaRect(contour)
        if min(rw, rh) < 20:
            continue
        tilt = min(abs(angle) % 90, 90 - abs(angle) % 90)
        # BoardFrame non rettifica la rotazione: tollerare solo una deriva
        # marginale sul lato lungo, non inclinazioni che falsano scala e XY.
        # L'allowance scala con il contorno, altrimenti il filtro misurerebbe
        # la risoluzione dell'immagine invece dell'inclinazione della scheda.
        edge_drift = max(rw, rh) * math.sin(math.radians(tilt))
        drift_allowance = max(MAX_BOARD_EDGE_DRIFT_PX,
                              MAX_BOARD_EDGE_DRIFT_RATIO * max(rw, rh))
        if tilt > MAX_BOARD_TILT_DEG or edge_drift > drift_allowance:
            continue
        bx, by, bw, bh = cv2.boundingRect(contour)
        w, h = float(bw), float(bh)
        # I consumatori usano X orizzontale. Non fingere che una scheda
        # verticale sia orizzontale scambiando solo width_px e height_px.
        if w < h:
            continue
        long_px, short_px = max(w, h), min(w, h)
        found = long_px / short_px
        error = abs(found - expected) / expected
        if error > MAX_BOARD_ASPECT_ERROR:
            continue
        # Il solo rapporto d'aspetto non identifica la scheda: su una foto
        # reale un connettore o un'etichetta scura possono avere lo stesso
        # rapporto. Senza questi due filtri il candidato sbagliato veniva
        # accettato in silenzio e calibrava tutto l'inventario su un ritaglio
        # di pochi pixel -- un errore muto, peggiore di un rifiuto.
        if long_px / body_length < MIN_BOARD_SCALE_PX_PER_MM:
            continue
        if (w * h) < MIN_BOARD_IMAGE_FRACTION * image.shape[0] * image.shape[1]:
            continue
        candidates.append((cv2.contourArea(contour), error, bx, by, w, h))
    if not candidates:
        return None
    _, _, bx, by, w, h = max(candidates, key=lambda item: item[0])
    cx, cy = bx + w / 2, by + h / 2
    long_px, short_px = max(w, h), min(w, h)
    scale = (long_px / body_length + short_px / body_width) / 2
    return BoardFrame(cx=cx, cy=cy, scale=scale, width_px=long_px, height_px=short_px)


def _edge_mask(window) -> "np.ndarray":
    """Contorni chiusi e utilizzabili nella finestra: Canny + chiusura morfologica.

    Canny lavora sul gradiente locale, non su una soglia assoluta: separa
    un componente nero da un PCB nero se c'e' anche una minima differenza
    di riflettanza tra le due superfici, cosa che una soglia di colore non
    puo' fare.
    """
    gray = cv2.cvtColor(window, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    median = float(np.median(blurred))
    low = int(max(10, 0.5 * median))
    high = int(min(255, 1.5 * median + 30))
    edges = cv2.Canny(blurred, low, high)
    # Un solo giro, kernel minimo: su una scheda densa (componenti vicini,
    # ognuno con la propria cornice serigrafica) una chiusura piu' aggressiva
    # salda i contorni di pezzi ADIACENTI in un unico blob, che poi
    # sostituirebbe la stima con la sagoma sbagliata (il gruppo, non il pezzo).
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    edges = cv2.dilate(edges, kernel, iterations=1)
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=1)
    return edges


def _forbidden_mask(image, frame: BoardFrame, exclude_rects_mm) -> "np.ndarray":
    """Pixel da ignorare: fuori dal contorno della scheda e sopra le
    piazzole dei contatti (bordi metallici lucidi, facili da confondere
    con quelli di un componente)."""
    forbidden = np.ones(image.shape[:2], dtype="uint8")
    margin = 0.15 * frame.scale
    x0 = int(frame.cx - frame.width_px / 2 + margin)
    x1 = int(frame.cx + frame.width_px / 2 - margin)
    y0 = int(frame.cy - frame.height_px / 2 + margin)
    y1 = int(frame.cy + frame.height_px / 2 - margin)
    forbidden[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = 0
    for rx0, ry0, rx1, ry1 in exclude_rects_mm or ():
        px0, py0 = frame.to_px(rx0, ry0)
        px1, py1 = frame.to_px(rx1, ry1)
        forbidden[int(max(0, py0)):int(max(0, py1)), int(max(0, px0)):int(max(0, px1))] = 1
    return forbidden


def refine_component_edges(image, frame: BoardFrame, component: dict, forbidden=None) -> Optional[dict]:
    """Cerca i bordi reali di un componente nella foto.

    Ritorna {x_mm, y_mm, length_mm, width_mm, rotation_deg} misurati, o
    None se nella finestra non c'e' un contorno abbastanza chiaro e
    compatibile con la stima di partenza.
    """
    length = float(component["length_mm"])
    width = float(component["width_mm"])
    rotation = float(component.get("rotation_deg") or 0.0)
    # Ingombro nel sistema dell'immagine (X/Y), non nel sistema del pezzo.
    span_x = width if rotation % 180 == 90 else length
    span_y = length if rotation % 180 == 90 else width

    px, py = frame.to_px(float(component["x_mm"]), float(component["y_mm"]))
    half_x = (span_x / 2 + SEARCH_MARGIN_MM) * frame.scale
    half_y = (span_y / 2 + SEARCH_MARGIN_MM) * frame.scale
    x0, y0 = int(max(0, px - half_x)), int(max(0, py - half_y))
    x1 = int(min(image.shape[1], px + half_x))
    y1 = int(min(image.shape[0], py + half_y))
    if x1 - x0 < 6 or y1 - y0 < 6:
        return None

    window = image[y0:y1, x0:x1]
    edges = _edge_mask(window)
    if forbidden is not None:
        edges[forbidden[y0:y1, x0:x1] == 1] = 0
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    expected_area = span_x * span_y * frame.scale ** 2
    expected_elongation = max(span_x, span_y) / max(1e-6, min(span_x, span_y))
    wcx, wcy = px - x0, py - y0
    best, best_score = None, None
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < 10:
            continue
        ratio = area / expected_area
        if not (AREA_RATIO_RANGE[0] <= ratio <= AREA_RATIO_RANGE[1]):
            continue
        (rcx, rcy), (rw, rh), _ = cv2.minAreaRect(contour)
        rect_area = rw * rh
        if rect_area <= 0 or area / rect_area < MIN_FILL_RATIO:
            # Riempie solo una piccola frazione del suo stesso rettangolo
            # minimo: e' la sagoma a "manubrio"/"L" di piu' pezzi vicini
            # saldati insieme, non un componente pieno.
            continue
        elongation = max(rw, rh) / max(1e-6, min(rw, rh))
        if elongation / expected_elongation > MAX_ELONGATION_RATIO or \
           expected_elongation / elongation > MAX_ELONGATION_RATIO:
            continue
        distance = math.hypot(rcx - wcx, rcy - wcy) / (SEARCH_MARGIN_MM * frame.scale + 1e-6)
        score = distance + abs(math.log(ratio)) * 0.6
        if best_score is None or score < best_score:
            best, best_score = contour, score
    if best is None:
        return None

    (rcx, rcy), (rw, rh), angle = cv2.minAreaRect(best)
    if rw <= 0 or rh <= 0:
        return None
    x_mm, y_mm = frame.to_mm(x0 + rcx, y0 + rcy)
    if math.hypot(x_mm - float(component["x_mm"]), y_mm - float(component["y_mm"])) > MAX_SHIFT_MM:
        return None

    # L'angolo di minAreaRect e' arbitrario (dipende da quale lato prende
    # come riferimento): si arrotonda al multiplo di 90 piu' vicino, l'unica
    # rotazione che il resto della catena applica.
    long_side, short_side = max(rw, rh), min(rw, rh)
    axis_angle = (angle if rw >= rh else angle + 90) % 180
    horizontal = axis_angle < 45 or axis_angle > 135
    new_rotation = 0.0 if horizontal else 90.0
    measured_length = long_side / frame.scale
    measured_width = short_side / frame.scale

    return {
        "x_mm": round(x_mm, 2),
        "y_mm": round(y_mm, 2),
        "length_mm": round(measured_length, 2),
        "width_mm": round(measured_width, 2),
        "rotation_deg": new_rotation,
    }


def refine_components(
    photo_paths: Sequence[Path],
    body_length: float,
    body_width: float,
    components: Sequence[dict],
    exclude_rects_mm: Sequence[tuple] = (),
) -> tuple[list[dict], RefineReport]:
    """Rifinisce posizione, ingombro e orientamento di ogni componente
    misurandone i bordi nella prima foto dall'alto utilizzabile.

    Non tocca nome, tipo, colore, marcatura: solo la geometria, e solo
    quando la misura e' abbastanza affidabile da fidarsene (vedi
    `refine_component_edges`). Ritorna la lista (copia) corretta e un
    resoconto testuale per il log.
    """
    report = RefineReport()
    refined = [dict(c) for c in components]
    if cv2 is None or not components or body_length <= 0 or body_width <= 0:
        return refined, report

    image, frame = None, None
    for path in photo_paths:
        if Path(path).suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
            continue
        candidate = cv2.imread(str(path))
        if candidate is None:
            continue
        found = find_board(candidate, body_length, body_width)
        if found is not None and (frame is None or found.width_px > frame.width_px):
            image, frame, report.photo = candidate, found, str(path)
    if frame is None:
        return refined, report

    forbidden = _forbidden_mask(image, frame, exclude_rects_mm)
    for component in refined:
        try:
            measured = refine_component_edges(image, frame, component, forbidden)
        except Exception:  # noqa: BLE001 - una misura fallita non ferma le altre
            measured = None
        if measured is None:
            report.skipped.append(component.get("name", "?"))
            continue
        report.measured += 1
        component.update(measured)
    return refined, report
