"""@brief Find the real component edges in the photo through edge detection.

Ritrova nella foto i bordi reali dei componenti, via edge detection.

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
   orientamento misurati. L'angolo si tiene come misurato: il generatore
   CAD applica rotazioni qualsiasi (`_rectangle_axes` ha il ramo non
   cardinale). Si riporta alla cardinale solo quando lo scarto e' piu'
   piccolo dell'incertezza -- il residuo di inclinazione che la
   calibrazione ammette sulla scheda, o la risoluzione angolare del
   contorno per quella taglia -- perche' li' un pezzo obliquo e una
   scheda storta danno la stessa misura.

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
from .messages import tx

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
#: Pixel minimi sul lato corto di un passivo 0402 perche' sia distinguibile.
#: Duplicato della soglia usata dall'inventario, esposto qui perche' la
#: GUI possa giudicare una foto senza importare l'intera catena.
MIN_PACKAGE_PIXELS_HINT = 12
#: TETTO di quanto ci si allontana dalla posizione stimata nel cercare i bordi,
#: oltre il semi-ingombro del componente. Non e' la finestra: quella si ferma a
#: meta' strada dal corpo piu' vicino (vedi `search_margin_mm`). Resta come
#: limite superiore per i corpi isolati, dove non c'e' un vicino che la chiuda.
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
#: Incertezza di posizione di un punto di contorno, in pixel. Ripiego
#: dichiarato: e' la quantizzazione del contorno (un pixel), non una misura
#: del rumore di questa fotocamera. Serve a ricavare la risoluzione angolare
#: di un corpo dalla sua dimensione: un corpo corto non puo' dichiarare un
#: angolo fine. Diventerebbe superflua stimando il rumore di bordo dalla foto
#: stessa.
CONTOUR_NOISE_PX = 1.0
#: Di quanto `_edge_mask` gonfia il contorno rispetto al corpo, per lato:
#: l'anello di Canny dopo la sfocatura sta circa un pixel fuori dal bordo e
#: la dilatazione 3x3 ne aggiunge un altro. Va tolta dal lato corto misurato
#: prima di giudicare quanto fine puo' essere l'angolo, altrimenti un corpo
#: piccolo sembra piu' grande di quello che e' e si finisce per attribuirgli
#: una precisione angolare che non ha. Misurato su rettangoli sintetici da 10
#: a 84 px: il contorno risulta piu' largo di 4.4-4.8 px in tutto, quindi 2.0
#: per lato e' il valore che il kernel prevede ed e' anche quello osservato.
MORPH_GROWTH_PX = 2.0


@dataclass
class BoardFrame:
    """@brief Board placement in the photo: centre in pixels and px/mm scale.

    Dove sta la scheda nella foto: centro in px, scala px/mm.
    """

    cx: float
    cy: float
    scale: float
    width_px: float
    height_px: float

    def to_px(self, x_mm: float, y_mm: float) -> tuple[float, float]:
        """@brief Convert board millimetres (origin at centre, Y down) to photo pixels."""
        # Y del footprint verso il basso, come le righe dell'immagine.
        return self.cx + x_mm * self.scale, self.cy + y_mm * self.scale

    def to_mm(self, x_px: float, y_px: float) -> tuple[float, float]:
        """@brief Convert photo pixels to board millimetres (origin at centre, Y down)."""
        return (x_px - self.cx) / self.scale, (y_px - self.cy) / self.scale


@dataclass
class RefineReport:
    """@brief Report terminal-refinement changes and unresolved items."""
    photo: Optional[str] = None
    measured: int = 0
    skipped: list = field(default_factory=list)

    def summary(self) -> str:
        """@brief Return a translated log line summarizing measured and skipped components."""
        if not self.photo:
            return tx("nessuna foto dall'alto utilizzabile per rifinire i componenti (bordi "
                      "misurati dalla vista dall'alto: nessuna trovata)")
        text = (
            tx("bordi rifiniti dalla foto {name}: {measured} componenti corretti sui "
               "bordi reali",
               name=Path(self.photo).name,
               measured=self.measured)
        )
        if self.skipped:
            text += tx(", {n_skipped} lasciati come stimati (bordi non chiari)",
                       n_skipped=len(self.skipped))
        return text


#: Inclinazione massima che si prova a raddrizzare. Oltre, la foto non e'
#: una vista dall'alto storta ma un'inquadratura prospettica, che una
#: rotazione rigida non puo' correggere.
MAX_DESKEW_DEG = 20.0


def _board_mask(image):
    """Maschera del rettangolo scuro della scheda, come la vede find_board."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    _, dark = cv2.threshold(gray, 70, 255, cv2.THRESH_BINARY_INV)
    close_k = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
    open_size = max(9, int(min(image.shape[:2]) * 0.02) | 1)
    open_k = cv2.getStructuringElement(cv2.MORPH_RECT, (open_size, open_size))
    dark = cv2.morphologyEx(dark, cv2.MORPH_CLOSE, close_k)
    return cv2.morphologyEx(dark, cv2.MORPH_OPEN, open_k)


def _texture_mask(image):
    """Maschera della scheda per tessitura, non per luminosita'.

    Su una fotografia la scheda e' fitta di componenti, serigrafie e piste,
    mentre gli sfondi -- sia il piano chiaro sia l'ombra scura accanto --
    sono lisci. L'energia del gradiente separa le due cose anche quando i
    livelli di grigio si sovrappongono: su questo modulo lo schermo metallico
    sta a 201 e lo sfondo a 204, indistinguibili per soglia, ma il primo e'
    pieno di bordi e il secondo no.

    La chiusura morfologica con kernel largo ricuce l'interno della scheda,
    compreso lo schermo: quello che conta e' la sagoma esterna.
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    gradient = cv2.magnitude(cv2.Sobel(blurred, cv2.CV_32F, 1, 0, ksize=3),
                             cv2.Sobel(blurred, cv2.CV_32F, 0, 1, ksize=3))
    window = max(9, int(min(image.shape[:2]) * 0.012) | 1)
    energy = cv2.boxFilter(gradient, cv2.CV_32F, (window, window))
    energy = cv2.normalize(energy, None, 0, 255, cv2.NORM_MINMAX).astype("uint8")
    _, mask = cv2.threshold(energy, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    close = cv2.getStructuringElement(cv2.MORPH_RECT, (window * 3 | 1, window * 3 | 1))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, close)
    open_k = cv2.getStructuringElement(cv2.MORPH_RECT, (window, window))
    return cv2.morphologyEx(mask, cv2.MORPH_OPEN, open_k)


def deskew_board(image, body_length: float, body_width: float):
    """@brief Rotate a tilted board photo upright, or return None when not needed or not possible.

    Raddrizza la foto quando la scheda e' inquadrata storta.

    `find_board` non rettifica la rotazione, per non falsare scala e
    coordinate: tollera percio' meno di un grado. Su una foto scattata a mano
    libera bastano due o tre gradi per far rifiutare l'immagine, ed e' il
    motivo per cui una foto vera ad alta risoluzione veniva scartata in favore
    di un disegno a bassa risoluzione.

    Qui si misura l'inclinazione del rettangolo scuro piu' grande compatibile
    con le proporzioni del modulo e si ruota l'intera immagine. E' una
    trasformazione rigida: lunghezze e proporzioni restano quelle vere, quindi
    la scala ricavata dopo il raddrizzamento e' altrettanto valida. La tela
    viene allargata, cosi' la rotazione non taglia gli angoli.

    Ritorna l'immagine raddrizzata, oppure None se non c'e' niente da
    raddrizzare o l'inclinazione e' troppo grande per essere una vista
    dall'alto.
    """
    if cv2 is None or not all(math.isfinite(v) and v > 0 for v in (body_length, body_width)):
        return None
    expected = body_length / body_width
    contours = []
    for mask in (_board_mask(image), _texture_mask(image)):
        found, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contours.extend(found)
    best = None
    for contour in contours:
        (cx, cy), (rw, rh), angle = cv2.minAreaRect(contour)
        if min(rw, rh) < 20:
            continue
        # Sul rettangolo ruotato il rapporto e' quello vero della scheda: il
        # bounding box assiale di un rettangolo storto e' sempre piu' tozzo.
        found = max(rw, rh) / min(rw, rh)
        if abs(found - expected) / expected > MAX_BOARD_ASPECT_ERROR:
            continue
        area = rw * rh
        if best is None or area > best[0]:
            best = (area, cx, cy, rw, rh, angle)
    if best is None:
        return None
    _, cx, cy, rw, rh, angle = best
    # minAreaRect riporta l'angolo del lato, non della scheda: si porta la
    # correzione nel quarto piu' vicino allo zero.
    tilt = angle % 90
    if tilt > 45:
        tilt -= 90
    if abs(tilt) <= MAX_BOARD_TILT_DEG or abs(tilt) > MAX_DESKEW_DEG:
        return None
    matrix = cv2.getRotationMatrix2D((cx, cy), tilt, 1.0)
    radians = math.radians(abs(tilt))
    height, width = image.shape[:2]
    new_w = int(width * math.cos(radians) + height * math.sin(radians)) + 2
    new_h = int(width * math.sin(radians) + height * math.cos(radians)) + 2
    matrix[0, 2] += new_w / 2 - cx
    matrix[1, 2] += new_h / 2 - cy
    return cv2.warpAffine(image, matrix, (new_w, new_h), flags=cv2.INTER_CUBIC,
                          borderValue=(255, 255, 255))


def find_board(image, body_length: float, body_width: float) -> Optional[BoardFrame]:
    """@brief Locate the board rectangle in the photo; None when it is not a top view.

    Trova il rettangolo della scheda nella foto; None se non e' una vista dall'alto.
    """
    if not all(math.isfinite(v) and v > 0 for v in (body_length, body_width)):
        return None
    expected = body_length / body_width
    # Due segmentazioni, provate in quest'ordine. La soglia sul buio e' esatta
    # su disegni e rendering, dove il PCB e' nero uniforme. Su una fotografia
    # reale fallisce: uno schermo metallico e' chiaro quanto lo sfondo e
    # un'ombra sul tavolo e' scura quanto la scheda, cosi' il "rettangolo
    # scuro piu' grande" diventa un blob senza significato. La tessitura non
    # ha quel problema, ma su un disegno pulito e' meno precisa: resta il
    # ripiego, non il criterio principale.
    for mask in (_board_mask(image), _texture_mask(image)):
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if contours:
            candidates = _board_candidates(contours, image, expected, body_length)
            if candidates:
                break
    else:
        return None
    if not candidates:
        return None
    return _frame_from(candidates, body_length, body_width)


def _board_candidates(contours, image, expected, body_length):
    """Rettangoli compatibili col modulo, filtrati come da contratto."""
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
    return candidates


def _frame_from(candidates, body_length, body_width):
    """Il candidato piu' grande diventa il riferimento della scheda."""
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


def _body_rect_mm(component) -> tuple:
    """Riquadro allineato agli assi del corpo stimato, in millimetri."""
    length, width = float(component["length_mm"]), float(component["width_mm"])
    rotation = math.radians(float(component.get("rotation_deg") or 0.0))
    span_x = length * abs(math.cos(rotation)) + width * abs(math.sin(rotation))
    span_y = length * abs(math.sin(rotation)) + width * abs(math.cos(rotation))
    x, y = float(component["x_mm"]), float(component["y_mm"])
    return (x - span_x / 2, y - span_y / 2, x + span_x / 2, y + span_y / 2)


def _rect_gap_mm(first, second) -> float:
    """Distanza fra due riquadri, zero quando si toccano o si sovrappongono."""
    dx = max(second[0] - first[2], first[0] - second[2], 0.0)
    dy = max(second[1] - first[3], first[1] - second[3], 0.0)
    return math.hypot(dx, dy)


def search_margin_mm(gap_mm: float, scale_px_per_mm: float) -> float:
    """@brief Quanto guardare oltre il corpo stimato, cercandone i bordi.

    @param gap_mm Distanza dal corpo stimato piu' vicino, zero se si toccano.
    @param scale_px_per_mm Scala della fotografia calibrata.
    @return Millimetri oltre il semi-ingombro, per il lato della finestra.
    @note La zona di competenza di un corpo finisce dove comincia quella di un
        altro: si guarda al piu' fino a meta' strada dal vicino. Non e' una
        soglia scelta a mano -- la decide la densita' della scheda, quindi su
        un modulo fitto la finestra e' minuscola e su una scheda sparsa si
        apre fino al tetto di SEARCH_MARGIN_MM.
    @note Il minimo non e' arbitrario: e' di quanto `_edge_mask` gonfia il
        contorno rispetto al corpo. Con una finestra piu' stretta si
        taglierebbe il proprio stesso contorno e non si misurerebbe piu' nulla.
    @note Perche' serve: con una finestra fissa di 2 mm, su una foto a 55 px/mm
        si guardano 110 pixel di vicinato per parte. Misurato sulle 123
        proposte del modulo di prova, dove la distanza mediana fra proposte e'
        zero: la chiusura morfologica fondeva il corpo con cio' che ha intorno
        (contorno mediano 9 volte l'area della proposta) e delle 12 misure che
        passavano i filtri lo spostamento mediano era 1.44 mm, cioe' il
        rilevamento agganciava il vicino invece di rifinire il corpo. Con
        questa regola le misure diventano 71 e lo spostamento mediano 0.08 mm.
    """
    floor = MORPH_GROWTH_PX / max(float(scale_px_per_mm), 1e-6)
    return max(floor, min(SEARCH_MARGIN_MM, float(gap_mm) / 2))


def angular_resolution_deg(short_side_px: float) -> float:
    """@brief Angolo piu' fine che un contorno di questa taglia puo' dichiarare.

    @param short_side_px Lato corto del rettangolo minimo, in pixel, gia'
        misurato sul contorno gonfiato dalla chiusura morfologica.
    @return Gradi: sotto questo valore l'inclinazione non e' distinguibile
        dall'incertezza di posizione dei punti di contorno.
    @note Un chip 0402 alto 10 px non puo' dire che e' ruotato di due gradi:
        basta che un punto di bordo sia fuori di un pixel. Un modulo lungo 800
        px si'. La quota nasce dalla geometria del contorno, non da una soglia
        scelta perche' un caso ci ricada dentro.
    """
    body_px = float(short_side_px) - 2 * MORPH_GROWTH_PX
    return math.degrees(math.atan2(CONTOUR_NOISE_PX, max(body_px, 1.0)))


def _axis_rotation(angle: float, rw: float, rh: float, short_side_px: float) -> float:
    """@brief Orientamento del lato lungo, in gradi, da un minAreaRect.

    @param angle Angolo restituito da cv2.minAreaRect, riferito a un lato
        qualsiasi del rettangolo.
    @param rw Larghezza del rettangolo minimo, in pixel.
    @param rh Altezza del rettangolo minimo, in pixel.
    @param short_side_px Lato corto, per sapere quanto fine puo' essere l'angolo.
    @return Rotazione in [0, 180), gia' riportata al lato lungo.
    @note minAreaRect e' ambiguo di 90 gradi perche' non dice quale lato ha
        preso come riferimento: si toglie l'ambiguita' prendendo sempre il lato
        lungo. Resta indeterminata la POLARITA' (quale estremo e' il pin 1, da
        che parte guarda l'innesto): quella non si ricava dall'ingombro e va
        chiesta al modello, non dedotta qui.
    @note Un'inclinazione piu' piccola dell'incertezza -- il residuo che la
        calibrazione stessa ammette sulla scheda, oppure la risoluzione
        angolare del contorno, quale delle due e' maggiore -- viene riportata
        alla cardinale. Non e' un arrotondamento di comodo: sotto quella soglia
        un corpo davvero obliquo e una scheda leggermente storta danno la
        stessa misura, e dichiarare un angolo sarebbe inventare precisione.
    """
    axis = (angle if rw >= rh else angle + 90) % 180
    cardinal = 0.0 if (axis < 45 or axis > 135) else 90.0
    reference = 0.0 if axis < 45 else 180.0 if axis > 135 else 90.0
    deviation = axis - reference
    if abs(deviation) <= max(MAX_BOARD_TILT_DEG, angular_resolution_deg(short_side_px)):
        return cardinal
    return round(axis, 1)


def refine_component_edges(image, frame: BoardFrame, component: dict, forbidden=None,
                          margin_mm: Optional[float] = None) -> Optional[dict]:
    """@brief Measure the real edges of one component in the photo, or return None when unclear.

    Cerca i bordi reali di un componente nella foto.

    Ritorna {x_mm, y_mm, length_mm, width_mm, rotation_deg} misurati, o
    None se nella finestra non c'e' un contorno abbastanza chiaro e
    compatibile con la stima di partenza.
    """
    length = float(component["length_mm"])
    width = float(component["width_mm"])
    rotation = float(component.get("rotation_deg") or 0.0)
    # Ingombro nel sistema dell'immagine (X/Y), non nel sistema del pezzo.
    # Vale per una rotazione qualsiasi; sulle cardinali si riduce esattamente
    # a scambiare lunghezza e larghezza.
    cosine = abs(math.cos(math.radians(rotation)))
    sine = abs(math.sin(math.radians(rotation)))
    span_x = length * cosine + width * sine
    span_y = length * sine + width * cosine

    # Finestra di ricerca: il tetto vale solo per un corpo isolato, altrimenti
    # ci si ferma prima, dove comincia la zona di competenza del vicino.
    margin = SEARCH_MARGIN_MM if margin_mm is None else max(float(margin_mm), 0.0)
    px, py = frame.to_px(float(component["x_mm"]), float(component["y_mm"]))
    half_x = (span_x / 2 + margin) * frame.scale
    half_y = (span_y / 2 + margin) * frame.scale
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

    # Area e allungamento sono proprieta' del pezzo, non della sua posa: si
    # ricavano dall'ingombro dichiarato e non dal rettangolo che lo racchiude
    # nell'immagine, che per una posa obliqua e' piu' grande del corpo.
    expected_area = length * width * frame.scale ** 2
    expected_elongation = max(length, width) / max(1e-6, min(length, width))
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
        distance = math.hypot(rcx - wcx, rcy - wcy) / (margin * frame.scale + 1e-6)
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

    long_side, short_side = max(rw, rh), min(rw, rh)
    new_rotation = _axis_rotation(angle, rw, rh, short_side)
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
    """@brief Refine position, size and orientation of each component from the first usable top photo.

    Rifinisce posizione, ingombro e orientamento di ogni componente
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

    # Stessa scala su X e Y prima di misurare: find_board accetta un ritaglio
    # il cui rapporto differisce da quello dichiarato fino a
    # MAX_BOARD_ASPECT_ERROR, e una scala unica sposterebbe le misure su Y.
    image, frame = isotropic_board(image, frame, body_length, body_width)
    measured_components, loop_report = refine_on_frame(image, frame, refined, exclude_rects_mm)
    loop_report.photo = report.photo
    return measured_components, loop_report


def crop_frame(image, body_length: float, body_width: float) -> Optional[BoardFrame]:
    """@brief Riferimento di una foto GIA' calibrata e ritagliata sulla scheda.

    @param image Ritaglio in cui la scheda occupa esattamente l'immagine.
    @param body_length Lunghezza dichiarata della scheda, in millimetri.
    @param body_width Larghezza dichiarata, in millimetri.
    @return BoardFrame con origine al centro, o None se le quote non reggono.
    @note `prepare_photo` ritaglia la scheda sul suo contorno: ricercarla con
        `find_board` dentro il proprio ritaglio e' sia inutile sia rischioso,
        perche' li' il rettangolo scuro piu' grande puo' essere un componente.
        Qui il riferimento si costruisce dalle quote dichiarate e dai pixel del
        ritaglio, senza nessuna ricerca.
    @note La scala si prende dal lato lungo: e' il lato su cui l'errore
        relativo di ritaglio pesa meno. Vale per ENTRAMBI gli assi, quindi
        e' corretta solo se il ritaglio ha lo stesso rapporto delle quote
        dichiarate. `find_board` ne accetta uno diverso fino a
        MAX_BOARD_ASPECT_ERROR: chi misura su un ritaglio qualsiasi passa
        prima da `isotropic_crop_frame`, che ricampiona l'immagine.
    """
    if image is None or body_length <= 0 or body_width <= 0:
        return None
    height_px, width_px = image.shape[:2]
    if width_px < 2 or height_px < 2:
        return None
    return BoardFrame(cx=width_px / 2, cy=height_px / 2,
                      scale=width_px / body_length,
                      width_px=float(width_px), height_px=float(height_px))


def isotropic_board(image, frame: BoardFrame, body_length: float, body_width: float):
    """@brief Resample a photo so the board has the same px/mm on both axes.

    @param image BGR photo containing the board.
    @param frame Board reference in that photo (width_px along X, height_px
        along Y, both measured on the photo).
    @param body_length Declared board extent along X, in mm.
    @param body_width Declared board extent along Y, in mm.
    @return (image, frame): the inputs themselves when the two axis scales
        already agree, otherwise a resampled copy of the image and a
        BoardFrame whose single `scale` holds on X and on Y.
    @note The detected board rectangle may differ from the declared aspect
        ratio (find_board accepts up to MAX_BOARD_ASPECT_ERROR). Every other
        consumer maps pixels to mm per axis (x / length, y / width); the edge
        measurement works in pixels with ONE scale, so without this step a
        crop 10% taller than declared would shift and stretch every measured
        Y. Only the lower-resolution axis is upsampled: no pixel of the photo
        is thrown away.
    """
    if cv2 is None or image is None or frame is None or body_length <= 0 or body_width <= 0:
        return image, frame
    extents = (getattr(frame, "width_px", None), getattr(frame, "height_px", None))
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
               for value in extents):
        # Riferimento senza estensioni numeriche: non c'e' nulla da cui
        # ricavare le due scale, quindi non si ricampiona.
        return image, frame
    scale_x = extents[0] / body_length
    scale_y = extents[1] / body_width
    if not (math.isfinite(scale_x) and math.isfinite(scale_y)) or scale_x <= 0 or scale_y <= 0:
        return image, frame
    if abs(scale_x - scale_y) <= 1e-9 * max(scale_x, scale_y):
        return image, frame
    target = max(scale_x, scale_y)
    height, width = image.shape[:2]
    new_width = max(1, round(width * target / scale_x))
    new_height = max(1, round(height * target / scale_y))
    # Fattori effettivi, dopo l'arrotondamento a pixel interi.
    factor_x, factor_y = new_width / width, new_height / height
    resized = cv2.resize(image, (new_width, new_height), interpolation=cv2.INTER_LINEAR)
    board_width, board_height = frame.width_px * factor_x, frame.height_px * factor_y
    return resized, BoardFrame(cx=frame.cx * factor_x, cy=frame.cy * factor_y,
                               scale=(board_width / body_length + board_height / body_width) / 2,
                               width_px=board_width, height_px=board_height)


def isotropic_crop_frame(image, body_length: float, body_width: float):
    """@brief Reference of a calibrated board crop, resampled to square pixels if needed.

    @param image Crop in which the board fills the whole image.
    @param body_length Declared board extent along X, in mm.
    @param body_width Declared board extent along Y, in mm.
    @return (image, frame), where frame.scale holds on both axes of the
        returned image, or (image, None) when the dimensions do not hold.
    @note Use this, not crop_frame alone, whenever mm are read back from
        pixels: the calibrated crop keeps the aspect of the detected board,
        which may differ from the declared one (see isotropic_board).
    """
    frame = crop_frame(image, body_length, body_width)
    if frame is None:
        return image, None
    return isotropic_board(image, frame, body_length, body_width)


def refine_on_frame(image, frame: BoardFrame, components: Sequence[dict],
                    exclude_rects_mm: Sequence[tuple] = ()) -> tuple[list[dict], RefineReport]:
    """@brief Misura i bordi dei componenti dati, su una foto e un riferimento noti.

    @param image Foto in cui cercare i bordi.
    @param frame Riferimento della scheda nella foto.
    @param components Ipotesi con x_mm, y_mm, length_mm, width_mm, rotation_deg.
    @param exclude_rects_mm Rettangoli (x0,y0,x1,y1) in mm dove i contorni non
        vanno guardati: tipicamente le strisce dei contatti dichiarati, i cui
        bordi metallici lucidi si confondono con quelli di un componente.
    @return Copia della lista con la geometria misurata dove e' stato possibile,
        e il resoconto di quanti sono stati misurati e quanti lasciati stimati.
    @note Non tocca nome, tipo, colore, marcatura: solo la geometria, e solo
        quando la misura e' abbastanza affidabile (vedi `refine_component_edges`).
        Quando non misura tiene la stima, quindi non puo' fare peggio che non
        misurare affatto.
    """
    report = RefineReport()
    refined = [dict(c) for c in components]
    if cv2 is None or frame is None or not refined:
        return refined, report
    forbidden = _forbidden_mask(image, frame, exclude_rects_mm)
    # La finestra di ricerca di ciascuno si ferma a meta' strada dal corpo piu'
    # vicino: si calcola una volta sola sulle stime di partenza, prima che le
    # misure le cambino, altrimenti l'ordine della lista deciderebbe le zone.
    rects = [_body_rect_mm(c) for c in refined]
    margins = [search_margin_mm(
        min((_rect_gap_mm(rect, other) for index, other in enumerate(rects) if index != position),
            default=SEARCH_MARGIN_MM), frame.scale)
        for position, rect in enumerate(rects)]
    for component, margin in zip(refined, margins):
        try:
            measured = refine_component_edges(image, frame, component, forbidden, margin)
        except Exception:  # noqa: BLE001 - una misura fallita non ferma le altre
            measured = None
        if measured is None:
            report.skipped.append(component.get("name", "?"))
            continue
        report.measured += 1
        # Edge snapping does not establish whether the new contour follows body,
        # attached terminals or solder pads; old box evidence no longer applies.
        if any(component.get(key) != value for key, value in measured.items()):
            if "bbox_basis" in component:
                component["bbox_basis"] = "unknown"
            component.pop("package_evidence", None)
        component.update(measured)
    return refined, report
