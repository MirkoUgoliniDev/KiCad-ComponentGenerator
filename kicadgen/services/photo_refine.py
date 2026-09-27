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

1. nella foto dall'alto si trova la scheda per CONTRASTO CON LO SFONDO,
   qualunque ne siano i colori: lo sfondo si stima dalla cornice della foto,
   e una regione e' la scheda solo se non tocca la cornice su due o piu'
   lati, e' circondata dallo sfondo su ogni lato libero e ha le proporzioni
   dichiarate NEL VERSO dichiarato (X = lunghezza). Dalle sue estensioni si
   ricava la scala px/mm; una foto incompatibile (vista laterale, dettaglio,
   scheda ruotata di 90 gradi rispetto alle quote) viene scartata con un
   motivo esplicito, mai ritagliata a caso;
2. per ogni componente stimato dal modello si ritaglia una finestra
   intorno alla posizione dichiarata, si cercano i bordi con Canny (soglie
   dal gradiente della finestra e dal suo rumore, non dalla luminosita':
   vale per schede nere, verdi, blu e bianche), si dilatano quanto basta a
   chiudere i contorni spezzati e si cerca il contorno chiuso piu'
   compatibile per area e distanza dal centro;
3. il rettangolo minimo che lo contiene da' l'orientamento; posizione e
   ingombro si rileggono sui pixel di Canny che il contorno racchiude, lungo
   quell'orientamento, cosi' la crescita della dilatazione non gonfia la
   misura (errore entro un pixel a ogni scala). L'angolo si tiene come misurato: il generatore
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

import functools
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
#: Pixel minimi sul lato corto del corpo piu' piccolo perche' sia
#: distinguibile. Esposto qui (e riusato dall'inventario) perche' la GUI possa
#: giudicare una foto senza importare l'intera catena.
MIN_PACKAGE_PIXELS_HINT = 12
#: Lato corto, in mm, del corpo piu' piccolo che si assume montato sulla
#: scheda quando si giudica se una foto e' abbastanza fine. IPOTESI
#: DICHIARATA, non una misura e non ricavata da una scheda: 0.5 mm e' il lato
#: corto di un passivo 0402 (1.0 x 0.5 mm), il piu' piccolo package comune
#: sulle schede assemblate in serie. Una scheda con 0201 (0.3 mm) richiede
#: foto piu' fini, una con soli 0805 ne tollera di piu' grossolane: chi conosce
#: la scheda puo' passare la propria taglia (photo_inventory._warn_if_too_coarse),
#: e l'avviso dice sempre rispetto a quale taglia e' stato dato.
SMALLEST_EXPECTED_PART_MM = 0.5
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
#: Spessore, in pixel, della fascia di pixel di bordo che descrive UN lato di
#: un corpo in `_edge_extent`. Non e' una soglia tarata: il salto di
#: luminosita' cade fra due pixel adiacenti e Canny ne marca uno dei due, quindi
#: i pixel di un lato stanno in una fascia larga un pixel dal piu' esterno.
#: La media di quella fascia sta entro mezzo pixel dal bordo vero, per lato:
#: l'ingombro misurato resta entro un pixel da quello del corpo a ogni scala,
#: mentre il contorno dilatato lo gonfiava di una quantita' costante in pixel
#: (+0.25 mm a 12 px/mm, +0.38 mm a 8 px/mm).
EDGE_SIDE_BAND_PX = 1.0
#: Spessore dell'anello di cornice da cui si stima lo sfondo, come frazione
#: del lato corto dell'immagine (minimo BACKGROUND_RING_MIN_PX). In una vista
#: dall'alto la cornice e' sfondo per costruzione: la scheda deve stare tutta
#: nell'inquadratura. Ripiego dichiarato sullo spessore: abbastanza sottile da
#: non entrare nella scheda con un margine normale, abbastanza spesso da dare
#: migliaia di campioni anche su una foto piccola.
BACKGROUND_RING_FRACTION = 0.02
BACKGROUND_RING_MIN_PX = 2
#: Quante deviazioni robuste (MAD scalata a sigma) oltre la mediana della
#: cornice un colore deve distare dallo sfondo per non esserlo. Ripiego
#: dichiarato, non tarato su una foto: 4 sigma sarebbero la coda di un rumore
#: gaussiano, ma Delta E e' un modulo con coda a destra e su rumore sintetico
#: lascia fuori soglia ~0.5% dei pixel di sfondo, isolati; li toglie il voto
#: a maggioranza di `_contrast_mask`, non questa costante. Su uno sfondo
#: davvero non uniforme (vignettatura, due superfici) la statistica della
#: cornice non basta: e' un limite dichiarato.
BACKGROUND_SPREAD_SIGMAS = 4.0
#: Distanza di colore minima (Delta E CIE76) sotto cui due superfici non si
#: distinguono: la soglia di percettibilita' (JND) classica. Serve solo
#: quando la cornice e' perfettamente uniforme (disegni, rendering), dove la
#: dispersione misurata e' zero e ogni pixel antialias sembrerebbe "scheda".
MIN_BACKGROUND_DELTA_E = 2.3
#: Frazione della fascia appena FUORI da ogni lato libero della scheda che
#: deve somigliare allo sfondo. La scheda e' circondata dallo sfondo; un
#: dettaglio interno (un integrato, uno schermo, una zona fitta di
#: componenti) e' circondato dalla scheda su almeno un lato. Maggioranza
#: semplice: un cavo, un connettore sporgente o una quota che attraversano il
#: bordo ne coprono una parte minore, un lato interno lo copre quasi tutto.
MIN_OUTSIDE_BACKGROUND_SHARE = 0.5
#: Lati della cornice che un candidato puo' toccare. Una regione che ne tocca
#: due o piu' e' lo sfondo stesso (o una superficie che esce dall'inquadratura)
#: e la sua estensione vera non e' misurabile: non e' mai la scheda.
MAX_FRAME_SIDES_TOUCHED = 1


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
#: Fascia di verifica del contorno della scheda, come frazione del suo lato
#: corto in pixel: `gap` salta la transizione antialias e le piazzole
#: castellate che sporgono di qualche pixel, `band` e' lo spessore esaminato.
#: Ripiego dichiarato sulle proporzioni (1% e 3%): servono solo a campionare
#: una striscia sottile a ridosso del bordo, il verdetto lo da' la
#: maggioranza (BOUNDARY_MAJORITY_SHARE), non questi valori.
BOUNDARY_GAP_FRACTION = 0.01
BOUNDARY_GAP_MIN_PX = 2
BOUNDARY_BAND_FRACTION = 0.03
BOUNDARY_BAND_MIN_PX = 3
#: Maggioranza richiesta su ogni lato libero: fuori deve prevalere lo sfondo,
#: dentro deve prevalere cio' che sfondo non e'.
BOUNDARY_MAJORITY_SHARE = 0.5

#: Codici del motivo per cui `locate_board` non ha trovato la scheda.
BOARD_FOUND = "found"
BOARD_NOT_FOUND = "not_found"
BOARD_ROTATED_90 = "rotated_90"
#: Nessuna scheda con le proporzioni dichiarate e PIU' di un oggetto separato
#: dallo sfondo con proporzioni diverse: quale sia la scheda non si decide.
BOARD_AMBIGUOUS = "ambiguous"
#: Pixel di quantizzazione per lato del riquadro (la stessa ipotesi della
#: tolleranza di scala dei test: un pixel di bordo per lato): sotto 2 px di
#: differenza fra le due estensioni il verso di un contorno quasi quadrato
#: non e' misurato, e' rumore di bordo.
ORIENTATION_QUANTIZATION_PX = 2.0


@dataclass(frozen=True)
class BackgroundModel:
    """@brief Colour of the photo background, estimated from the image frame.

    @note Colours are CIE L*a*b* with L in 0..100, so distances are Delta E
        CIE76 and mean the same for dark, light and saturated surfaces: a
        black board on a black mat and a green board on a white sheet are
        judged by the same rule.
    """

    #: Median background colour, (L, a, b).
    lab: tuple
    #: Median background colour in the photo's BGR, used to fill new canvas.
    bgr: tuple
    #: Delta E under which a pixel is indistinguishable from the background.
    tolerance: float


def _lab(image):
    """Immagine BGR a 8 bit in L*a*b* float (L 0..100): distanze in Delta E."""
    return cv2.cvtColor(image.astype(np.float32) / 255.0, cv2.COLOR_BGR2Lab)


def estimate_background(image) -> Optional[BackgroundModel]:
    """@brief Estimate the background colour and its spread from a ring along the image frame.

    @param image BGR photo (three channels).
    @return BackgroundModel, or None when OpenCV is missing, the image is not
        a three-channel picture or it is too small to hold a ring.
    @note A top view frames the whole board, so the frame is background by
        construction. Median and MAD are robust: dimension lines, a cable or
        one board side grazing the frame are a minority of the ring and do
        not move the estimate.
    @note Limit: one colour for the whole background. A strongly vignetted
        or two-surface background is only partly "background" under this
        model; the regions that differ touch the frame and are rejected as
        candidates, so the failure is a declared one, not a wrong crop.
    """
    if cv2 is None or image is None or getattr(image, "ndim", 0) != 3 or image.shape[2] != 3:
        return None
    height, width = image.shape[:2]
    ring = max(BACKGROUND_RING_MIN_PX,
               int(round(BACKGROUND_RING_FRACTION * min(height, width))))
    if min(height, width) <= 2 * ring:
        return None

    def frame_pixels(array):
        return np.concatenate([array[:ring].reshape(-1, 3), array[-ring:].reshape(-1, 3),
                               array[ring:-ring, :ring].reshape(-1, 3),
                               array[ring:-ring, -ring:].reshape(-1, 3)])

    samples = frame_pixels(_lab(image))
    centre = np.median(samples, axis=0)
    distance = np.linalg.norm(samples - centre, axis=1)
    median = float(np.median(distance))
    # MAD scalata a sigma (1.4826): stessa unita' di una deviazione standard.
    spread = 1.4826 * float(np.median(np.abs(distance - median)))
    tolerance = max(MIN_BACKGROUND_DELTA_E, median + BACKGROUND_SPREAD_SIGMAS * spread)
    bgr = tuple(int(v) for v in np.median(frame_pixels(image), axis=0))
    return BackgroundModel(lab=tuple(float(v) for v in centre), bgr=bgr,
                           tolerance=float(tolerance))


def _background_distance(image, background: BackgroundModel):
    """Delta E di ogni pixel dal colore dello sfondo."""
    return np.linalg.norm(_lab(image) - np.array(background.lab, dtype=np.float32), axis=2)


def _clean_mask(mask, shape):
    """Chiusura (ricuce) e apertura (toglie linee sottili: quote, piste)."""
    close_k = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
    open_size = max(9, int(min(shape[:2]) * 0.02) | 1)
    open_k = cv2.getStructuringElement(cv2.MORPH_RECT, (open_size, open_size))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, close_k)
    return cv2.morphologyEx(mask, cv2.MORPH_OPEN, open_k)


def _board_mask(image):
    """Maschera storica dei pixel molto scuri (grigio < 70).

    Non e' piu' il criterio che decide cos'e' la scheda: e' una delle
    segmentazioni provate, e ogni suo candidato deve superare le stesse
    verifiche di contrasto con lo sfondo (`_board_candidates`). Resta la prima
    per compatibilita': sulle foto scure-su-chiaro in cui era gia' corretta
    riproduce al pixel il ritaglio di prima, e con esso le risposte in cache.
    La soglia 70 e' ereditata, non derivata: su uno sfondo scuro la maschera
    prende lo sfondo, che tocca la cornice e viene scartato.
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    _, dark = cv2.threshold(gray, 70, 255, cv2.THRESH_BINARY_INV)
    return _clean_mask(dark, image.shape)


def _contrast_mask(distance, background: BackgroundModel, shape):
    """Maschera di cio' che si distingue dallo sfondo, qualunque colore abbia.

    La distanza Delta E e' un modulo, con coda a destra: anche oltre la
    tolleranza resta qualche pixel di rumore isolato (misurato ~0.5% su
    rumore gaussiano sintetico). La chiusura 15x15 li salderebbe alla scheda
    in escrescenze che ne allargano il riquadro. Prima si vota a maggioranza
    su 5x5 (filtro mediano): la scheda e' una regione, un pixel isolato no. Un
    bordo rettilineo resta dov'e', uno spigolo si arrotonda di un pixel senza
    cambiare il riquadro.
    """
    mask = np.where(distance > background.tolerance, 255, 0).astype("uint8")
    return _clean_mask(cv2.medianBlur(mask, 5), shape)


def _texture_mask(image):
    """Maschera della scheda per tessitura, non per colore.

    Su una fotografia la scheda e' fitta di componenti, serigrafie e piste,
    mentre uno sfondo uniforme e' liscio. L'energia del gradiente separa le
    due cose anche quando i colori si sovrappongono: uno schermo metallico
    chiaro quasi quanto il piano d'appoggio e' pieno di bordi, il piano no.

    La chiusura morfologica con kernel largo ricuce l'interno della scheda,
    compreso lo schermo: quello che conta e' la sagoma esterna.
    Limite: uno sfondo a sua volta tessuto (legno, tessuto) non si separa
    cosi'; i suoi blob toccano la cornice e vengono scartati.
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


def _segmentations(image, background: BackgroundModel, distance):
    """Maschere in ordine di prova, ciascuna con l'indicazione se il riquadro
    va riportato a meta' del salto di contrasto (`_mid_step_box`).

    1. pixel molto scuri (storica, vedi `_board_mask`): riquadro invariato,
       per riprodurre al pixel i ritagli di prima;
    2. contrasto con lo sfondo stimato dalla cornice: il criterio generale,
       che vale per scheda verde su nero, bianca su scuro, rossa su grigio;
    3. tessitura: ripiego per le parti della scheda dello stesso colore dello
       sfondo (schermi metallici), dove nessuna maschera a colore vede il bordo.
    """
    # Generatore: una maschera si calcola solo se le precedenti non bastano.
    yield _board_mask(image), False
    yield _contrast_mask(distance, background, image.shape), True
    yield _texture_mask(image), False


def _mid_step_box(distance, background: BackgroundModel, bx, by, bw, bh):
    """Riquadro del candidato con il bordo a meta' del salto sfondo -> scheda.

    La maschera di contrasto usa la tolleranza del rumore, cioe' il piede del
    gradino: su un bordo sfocato (ottica, ricampionamento) prende tutta la
    sfumatura e allarga il riquadro del raggio di sfocatura per lato, un
    errore di scala sistematico. Il punto di un gradino sfocato che non si
    sposta con la sfocatura e' quello a meta' ampiezza: qui si ri-soglia la
    finestra del candidato a meta' del contrasto mediano della scheda (mai
    sotto la tolleranza del rumore) e se ne prende il riquadro.
    """
    height, width = distance.shape[:2]
    _, gap = _side_bands(bx, by, bw, bh, width, height)
    interior = distance[by + gap:by + bh - gap, bx + gap:bx + bw - gap]
    if interior.size == 0:
        return bx, by, bw, bh
    level = max(background.tolerance, 0.5 * float(np.median(interior)))
    x0, y0 = max(0, bx - gap), max(0, by - gap)
    x1, y1 = min(width, bx + bw + gap), min(height, by + bh + gap)
    window = np.where(distance[y0:y1, x0:x1] > level, 255, 0).astype("uint8")
    # Cornice di zeri piu' larga del kernel di chiusura: senza, la chiusura
    # (erosione con bordo "infinito") non erode dal lato della finestra e la
    # regione si allarga fino al bordo della finestra stessa.
    pad = 16
    window = cv2.copyMakeBorder(cv2.medianBlur(window, 5), pad, pad, pad, pad,
                                cv2.BORDER_CONSTANT, value=0)
    window = _clean_mask(window, distance.shape)
    contours, _ = cv2.findContours(window, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return bx, by, bw, bh
    rx, ry, rw, rh = cv2.boundingRect(max(contours, key=cv2.contourArea))
    return x0 + rx - pad, y0 + ry - pad, rw, rh


def _frame_sides_touched(bx, by, bw, bh, width, height) -> int:
    """Quanti lati della cornice il riquadro raggiunge."""
    return sum((bx <= 0, by <= 0, bx + bw >= width, by + bh >= height))


def _side_bands(bx, by, bw, bh, width, height):
    """Coppie (fascia esterna, fascia interna) per ogni lato non sulla cornice."""
    short = min(bw, bh)
    gap = max(BOUNDARY_GAP_MIN_PX, int(round(BOUNDARY_GAP_FRACTION * short)))
    band = max(BOUNDARY_BAND_MIN_PX, int(round(BOUNDARY_BAND_FRACTION * short)))
    x1, y1 = bx + bw, by + bh
    rows, cols = slice(by, y1), slice(bx, x1)
    sides = []
    left = (max(0, bx - gap - band), max(0, bx - gap))
    if left[1] > left[0]:
        sides.append(((rows, slice(*left)), (rows, slice(bx + gap, min(x1, bx + gap + band)))))
    right = (min(width, x1 + gap), min(width, x1 + gap + band))
    if right[1] > right[0]:
        sides.append(((rows, slice(*right)), (rows, slice(max(bx, x1 - gap - band), x1 - gap))))
    top = (max(0, by - gap - band), max(0, by - gap))
    if top[1] > top[0]:
        sides.append(((slice(*top), cols), (slice(by + gap, min(y1, by + gap + band)), cols)))
    bottom = (min(height, y1 + gap), min(height, y1 + gap + band))
    if bottom[1] > bottom[0]:
        sides.append(((slice(*bottom), cols), (slice(max(by, y1 - gap - band), y1 - gap), cols)))
    return sides, gap


def _surrounded_by_background(distance, background: BackgroundModel, bx, by, bw, bh) -> bool:
    """Il riquadro separa, lato per lato, cio' che sfondo non e' dallo sfondo.

    Fuori: un pixel conta come sfondo se e' piu' vicino allo sfondo che alla
    scheda (meta' del salto di contrasto, il criterio del bordo a gradino), o
    comunque indistinguibile dallo sfondo. Cosi' un alone chiaro, un'ombra
    tenue o una piazzola che sporge di un pixel restano "fuori", mentre la
    scheda attorno a un dettaglio interno no.

    Dentro: la fascia deve essere in maggioranza distinguibile dallo sfondo.
    Una maschera che mette un lato dove dentro c'e' sfondo ha incollato alla
    scheda qualcosa di esterno (il testo di una quota, un connettore
    sporgente): il riquadro e' sbagliato. Vale per tutte le segmentazioni:
    sulla foto quotata di riferimento la tessitura incollava le quote alla
    scheda e dava un riquadro con il 7% di scarto d'aspetto, dentro la
    tolleranza, che senza questa verifica passava in silenzio. Limite
    dichiarato: una scheda il cui lato e' occupato per piu' di meta' da una
    superficie indistinguibile dallo sfondo (schermo metallico fino al bordo
    su un piano dello stesso colore) viene rifiutata, non misurata.

    Un lato che giace sulla cornice non si puo' verificare e si salta; se non
    resta nessun lato verificabile la scheda non e' dimostrabilmente
    circondata dallo sfondo e il candidato si scarta.
    """
    height, width = distance.shape[:2]
    sides, gap = _side_bands(bx, by, bw, bh, width, height)
    if not sides:
        return False
    interior = distance[by + gap:by + bh - gap, bx + gap:bx + bw - gap]
    if interior.size == 0:
        interior = distance[by:by + bh, bx:bx + bw]
    outside_tolerance = max(background.tolerance, 0.5 * float(np.median(interior)))
    for outer, inner in sides:
        outside = distance[outer]
        if outside.size and np.mean(outside <= outside_tolerance) < BOUNDARY_MAJORITY_SHARE:
            return False
        inside = distance[inner]
        if inside.size and np.mean(inside <= background.tolerance) >= BOUNDARY_MAJORITY_SHARE:
            return False
    return True


def _aspect_error(width_px, height_px, body_length, body_width) -> float:
    """Scarto relativo del rapporto X/Y trovato da quello dichiarato, nel verso dichiarato.

    Il confronto si fa sul rapporto lato lungo / lato corto DELLE QUOTE: con
    L >= W la scheda deve essere orizzontale (w/h contro L/W, come sempre), con
    L < W verticale (h/w contro W/L). Una scheda con il verso sbagliato da' un
    rapporto < 1 contro uno >= 1 e viene scartata, senza scambiare gli assi.
    """
    if body_length >= body_width:
        found, expected = width_px / height_px, body_length / body_width
    else:
        found, expected = height_px / width_px, body_width / body_length
    return abs(found - expected) / expected


def deskew_board(image, body_length: float, body_width: float):
    """@brief Rotate a tilted board photo upright, or return None when not needed or not possible.

    Raddrizza la foto quando la scheda e' inquadrata storta.

    `find_board` non rettifica la rotazione, per non falsare scala e
    coordinate: tollera percio' meno di un grado. Su una foto scattata a mano
    libera bastano due o tre gradi per far rifiutare l'immagine, ed e' il
    motivo per cui una foto vera ad alta risoluzione veniva scartata in favore
    di un disegno a bassa risoluzione.

    Qui si misura l'inclinazione della regione piu' grande compatibile con le
    proporzioni del modulo (in qualunque verso: il verso lo giudica poi
    `find_board`) e che non tocca la cornice su due o piu' lati, e si ruota
    l'intera immagine. E' una trasformazione rigida: lunghezze e proporzioni
    restano quelle vere, quindi la scala ricavata dopo il raddrizzamento e'
    altrettanto valida. La tela viene allargata, cosi' la rotazione non taglia
    gli angoli, e riempita con il colore dello sfondo stimato: un riempimento
    bianco fisso creerebbe su uno sfondo scuro un bordo che non esiste.

    @param image BGR photo.
    @param body_length Declared board extent along X, in mm.
    @param body_width Declared board extent along Y, in mm.
    @return The straightened image, or None when there is nothing to
        straighten or the tilt is too large for a top view.
    """
    if cv2 is None or not all(math.isfinite(v) and v > 0 for v in (body_length, body_width)):
        return None
    background = estimate_background(image)
    if background is None:
        return None
    expected = max(body_length, body_width) / min(body_length, body_width)
    height, width = image.shape[:2]
    distance = _background_distance(image, background)
    contours = []
    for mask, _ in _segmentations(image, background, distance):
        found, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contours.extend(found)
    best = None
    for contour in contours:
        (cx, cy), (rw, rh), angle = cv2.minAreaRect(contour)
        if min(rw, rh) < 20:
            continue
        if _frame_sides_touched(*cv2.boundingRect(contour), width, height) > MAX_FRAME_SIDES_TOUCHED:
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
    new_w = int(width * math.cos(radians) + height * math.sin(radians)) + 2
    new_h = int(width * math.sin(radians) + height * math.cos(radians)) + 2
    matrix[0, 2] += new_w / 2 - cx
    matrix[1, 2] += new_h / 2 - cy
    return cv2.warpAffine(image, matrix, (new_w, new_h), flags=cv2.INTER_CUBIC,
                          borderValue=background.bgr)


@dataclass(frozen=True)
class BoardSearch:
    """@brief Outcome of the board search: the frame, or why there is none."""

    #: Board reference in the photo, None when the board was not found.
    frame: Optional[BoardFrame]
    #: BOARD_FOUND, BOARD_NOT_FOUND or BOARD_ROTATED_90.
    reason: str

    def message(self, body_length: float, body_width: float) -> str:
        """@brief Translated explanation for the user; empty when the board was found.

        @param body_length Declared board extent along X, in mm.
        @param body_width Declared board extent along Y, in mm.
        @return Log line in the active language.
        """
        if self.reason == BOARD_FOUND:
            return ""
        if self.reason == BOARD_AMBIGUOUS:
            return tx("Scheda non riconosciuta nella foto: nessun oggetto ha le proporzioni "
                      "{length:.2f} x {width:.2f} mm e ce n'e' piu' di uno separato dallo "
                      "sfondo. Ritagliare la foto lasciando solo la scheda.",
                      length=body_length, width=body_width)
        if self.reason == BOARD_ROTATED_90:
            return tx("La scheda nella foto e' ruotata di 90 gradi rispetto alle quote "
                      "dichiarate: la lunghezza ({length:.2f} mm) corre lungo X, cioe' in "
                      "orizzontale nella foto, la larghezza ({width:.2f} mm) in verticale. "
                      "Ruotare la foto di 90 gradi, oppure verificare che lunghezza e "
                      "larghezza non siano state inserite al contrario.",
                      length=body_length, width=body_width)
        return tx("Scheda non riconosciuta nella foto: nessuna regione distinta dallo "
                  "sfondo, interamente inquadrata e con le proporzioni "
                  "{length:.2f} x {width:.2f} mm.",
                  length=body_length, width=body_width)


def _search(image, body_length, body_width, background, distance) -> Optional[BoardFrame]:
    """Prima segmentazione che da' candidati validi; il piu' grande vince."""
    for mask, mid_step in _segmentations(image, background, distance):
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        candidates = _board_candidates(contours, image, body_length, body_width,
                                       distance, background, mid_step)
        if candidates:
            return _frame_from(candidates, body_length, body_width)
    return None


def _orientation_mismatch(width_px, height_px, body_length, body_width) -> bool:
    """Il lato lungo misurato sta sull'asse opposto a quello dichiarato.

    Solo se entrambi i versi sono determinati: quote uguali non ne dichiarano
    uno, e un contorno le cui estensioni differiscono di non piu' di
    ORIENTATION_QUANTIZATION_PX non ne misura uno.
    """
    if body_length == body_width or abs(width_px - height_px) <= ORIENTATION_QUANTIZATION_PX:
        return False
    return (width_px > height_px) != (body_length > body_width)


def _search_any_aspect(image, body_length, body_width, background, distance):
    """Seconda passata: la scheda con proporzioni diverse da quelle dichiarate.

    Una vista dall'alto ridimensionata in modo non proporzionale (o fotografata
    con pixel non quadrati) resta una vista dall'alto: la scala si misura per
    asse, X sulla lunghezza e Y sulla larghezza, e i consumatori leggono gia'
    le coordinate asse per asse (`isotropic_board` ricampiona dove serve una
    scala unica). Qui il rapporto d'aspetto smette di identificare la scheda;
    la identificano i criteri strutturali, tutti ancora applicati da
    `_board_candidates` (circondata dallo sfondo, al piu' un lato di cornice,
    non inclinata, scala e area minime), piu' l'unicita': senza il rapporto
    nulla distingue due oggetti sul tavolo, quindi se ce n'e' piu' di uno la
    risposta e' BOARD_AMBIGUOUS, non il piu' grande.

    @return (reason, frame or None) with reason one of the BOARD_* codes.
    @note La deformazione non e' limitata: ogni soglia sarebbe tarata sulle
        foto viste finora. Viene dichiarata nel log (`aspect_deformation`),
        perche' o la foto e' deformata o le quote dichiarate sono sbagliate, e
        dalla sola foto le due cause non si distinguono.
    """
    for mask, mid_step in _segmentations(image, background, distance):
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        candidates = _board_candidates(contours, image, body_length, body_width,
                                       distance, background, mid_step, check_aspect=False)
        if not candidates:
            continue
        if len(candidates) > 1:
            return BOARD_AMBIGUOUS, None
        _, _, _, _, w, h = candidates[0]
        if _orientation_mismatch(w, h, body_length, body_width):
            return BOARD_ROTATED_90, None
        return BOARD_FOUND, _frame_from(candidates, body_length, body_width)
    return BOARD_NOT_FOUND, None


def aspect_deformation(frame: BoardFrame, body_length: float, body_width: float) -> float:
    """@brief Relative gap between the X and Y scales of a board frame.

    @param frame Board found by `locate_board`.
    @param body_length Declared board extent along X, in mm.
    @param body_width Declared board extent along Y, in mm.
    @return (px/mm on X) / (px/mm on Y) - 1: 0 for an undistorted view,
        negative when the photo is squeezed along X.
    """
    return (frame.width_px / body_length) / (frame.height_px / body_width) - 1.0


def locate_board(image, body_length: float, body_width: float) -> BoardSearch:
    """@brief Locate the board by contrast with the background and say why it failed, if it did.

    @param image BGR photo.
    @param body_length Declared board extent along X (horizontal in the photo), in mm.
    @param body_width Declared board extent along Y (vertical in the photo), in mm.
    @return BoardSearch with the frame, or with the reason it is missing.
    @note A board is a region that differs from the background estimated on
        the image frame, touches at most one side of the frame, is surrounded
        by background on every free side, is axis-aligned within
        MAX_BOARD_TILT_DEG, has the declared X/Y proportions IN THE DECLARED
        ORIENTATION within MAX_BOARD_ASPECT_ERROR, at least
        MIN_BOARD_SCALE_PX_PER_MM and MIN_BOARD_IMAGE_FRACTION of the photo.
        When nothing qualifies there is no frame: a wrong rectangle is never
        returned in its place.
    @note No image is rotated to guess the orientation. When the board only
        fits with the axes swapped the reason is BOARD_ROTATED_90, so the user
        can rotate the photo or correct the dimensions.
    """
    if cv2 is None or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                              and math.isfinite(v) and v > 0
                              for v in (body_length, body_width)):
        return BoardSearch(None, BOARD_NOT_FOUND)
    background = estimate_background(image)
    if background is None:
        return BoardSearch(None, BOARD_NOT_FOUND)
    distance = _background_distance(image, background)
    frame = _search(image, body_length, body_width, background, distance)
    if frame is not None:
        return BoardSearch(frame, BOARD_FOUND)
    if body_length != body_width and _search(image, body_width, body_length,
                                             background, distance) is not None:
        return BoardSearch(None, BOARD_ROTATED_90)
    reason, frame = _search_any_aspect(image, body_length, body_width, background, distance)
    return BoardSearch(frame, reason)


def find_board(image, body_length: float, body_width: float) -> Optional[BoardFrame]:
    """@brief Locate the board rectangle in the photo; None when it is not a usable top view.

    Trova il rettangolo della scheda nella foto; None se non e' una vista
    dall'alto utilizzabile. Il motivo del rifiuto lo da' `locate_board`.

    @param image BGR photo.
    @param body_length Declared board extent along X, in mm.
    @param body_width Declared board extent along Y, in mm.
    @return BoardFrame whose width_px/height_px are the X/Y extents, or None.
    """
    return locate_board(image, body_length, body_width).frame


def _board_candidates(contours, image, body_length, body_width, distance, background,
                      mid_step=False, check_aspect=True):
    """Rettangoli compatibili col modulo, filtrati come da contratto.

    Con `check_aspect` False il rapporto d'aspetto non filtra (seconda
    passata, `_search_any_aspect`): tutti gli altri criteri restano.
    """
    # Le foto dei moduli sono spesso corredate da quote: linee e frecce possono
    # formare il contorno più grande, ma con un rapporto molto diverso da quello
    # reale della scheda. Scegliamo quindi il candidato più grande tra quelli
    # geometricamente compatibili, invece di usare sempre max(contourArea).
    candidates = []
    height, width = image.shape[:2]
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
        if mid_step:
            bx, by, bw, bh = _mid_step_box(distance, background, bx, by, bw, bh)
        w, h = float(bw), float(bh)
        # I consumatori usano X = lunghezza dichiarata. Il verso si confronta
        # con quello delle quote: nessuno scambio implicito di assi.
        error = _aspect_error(w, h, body_length, body_width)
        if check_aspect and error > MAX_BOARD_ASPECT_ERROR:
            continue
        # Il solo rapporto d'aspetto non identifica la scheda: su una foto
        # reale un connettore o un'etichetta scura possono avere lo stesso
        # rapporto. Senza questi filtri il candidato sbagliato veniva
        # accettato in silenzio e calibrava tutto l'inventario su un ritaglio
        # sbagliato -- un errore muto, peggiore di un rifiuto.
        if w / body_length < MIN_BOARD_SCALE_PX_PER_MM:
            continue
        if (w * h) < MIN_BOARD_IMAGE_FRACTION * height * width:
            continue
        # Una regione che raggiunge due o piu' lati della cornice e' lo sfondo
        # (o esce dall'inquadratura): la sua estensione non e' misurabile.
        if _frame_sides_touched(bx, by, bw, bh, width, height) > MAX_FRAME_SIDES_TOUCHED:
            continue
        # La scheda e' circondata dallo sfondo; un dettaglio interno con le
        # stesse proporzioni no.
        if not _surrounded_by_background(distance, background, bx, by, bw, bh):
            continue
        candidates.append((cv2.contourArea(contour), error, bx, by, w, h))
    return candidates


def _frame_from(candidates, body_length, body_width):
    """Il candidato piu' grande diventa il riferimento della scheda.

    width_px e height_px sono le estensioni su X e su Y dell'immagine, nel
    verso dichiarato (X = lunghezza): per una scheda dichiarata verticale
    width_px e' il lato corto.
    """
    _, _, bx, by, w, h = max(candidates, key=lambda item: item[0])
    cx, cy = bx + w / 2, by + h / 2
    scale = (w / body_length + h / body_width) / 2
    return BoardFrame(cx=cx, cy=cy, scale=scale, width_px=w, height_px=h)


#: Quante deviazioni standard del rumore deve superare il gradiente perche'
#: Canny lo prenda anche solo come bordo debole (soglia bassa). Ripiego
#: dichiarato, non tarato su una foto: per un gradiente di puro rumore
#: gaussiano il modulo supera 3 sigma con probabilita' exp(-4.5) ~ 1% per
#: pixel, e la soppressione dei non massimi ne lascia una frazione; su
#: rumore sintetico (sigma 1-6, grigio 22-225) nessuna misura falsa. Lo
#: renderebbe superfluo un modello del rumore della fotocamera.
EDGE_NOISE_SIGMAS = 3.0


@functools.lru_cache(maxsize=1)
def _gradient_gain() -> float:
    """Deviazione standard del gradiente Sobel (ksize 3) di un rumore bianco
    unitario dopo la sfocatura gaussiana 3x3 usata da `_canny_edges`.

    Si misura applicando gli stessi due filtri a un impulso: la norma L2 della
    risposta e' il guadagno esatto della catena, nessun numero ricopiato.
    """
    impulse = np.zeros((11, 11), np.float32)
    impulse[5, 5] = 1.0
    response = cv2.Sobel(cv2.GaussianBlur(impulse, (3, 3), 0), cv2.CV_32F, 1, 0, ksize=3)
    return float(np.sqrt(np.sum(response ** 2)))


def _noise_sigma(gray) -> float:
    """Rumore dei livelli di grigio della finestra (stimatore di Immerkaer, 1996).

    Il nucleo [[1,-2,1],[-2,4,-2],[1,-2,1]] e' il prodotto di due derivate
    seconde: su un bordo rettilineo risponde zero, quindi il bordo di un corpo
    non passa per rumore. La mediana (invece della media dello stimatore
    originale) toglie anche il peso degli spigoli e delle scritte. Il minimo e'
    il rumore di quantizzazione a 8 bit (1/sqrt(12) livelli): un'immagine
    sintetica senza rumore non ha soglia zero.
    """
    kernel = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], np.float32)
    response = cv2.filter2D(gray.astype(np.float32), -1, kernel,
                            borderType=cv2.BORDER_REFLECT)[1:-1, 1:-1]
    # 0.6745 = mediana di |N(0,1)|; 6 = norma L2 del nucleo.
    sigma = float(np.median(np.abs(response))) / (0.6745 * 6.0) if response.size else 0.0
    return max(sigma, 1.0 / math.sqrt(12.0))


def _otsu_level(values) -> float:
    """Soglia di Otsu su valori float (256 classi fra minimo e massimo)."""
    if values.size == 0:
        return 0.0
    hist, bounds = np.histogram(values, bins=256)
    centres = (bounds[:-1] + bounds[1:]) / 2
    below = np.cumsum(hist).astype(np.float64)
    moment = np.cumsum(hist * centres)
    total, mean_total = below[-1], moment[-1]
    above = total - below
    valid = (below > 0) & (above > 0)
    if not np.any(valid):
        return float(centres[-1])
    between = np.zeros_like(centres, dtype=np.float64)
    between[valid] = ((mean_total * below[valid] - moment[valid] * total) ** 2
                      / (below[valid] * above[valid]))
    return float(centres[int(np.argmax(between))])


def canny_thresholds(gray, blurred=None) -> tuple:
    """@brief Canny hysteresis thresholds from the gradient statistics of a window.

    @param gray Grey window (8 bit), as it comes from the photo: its noise is
        measured here, before any smoothing.
    @param blurred The same window smoothed as Canny will see it (3x3
        Gaussian); computed from `gray` when omitted.
    @return (low, high) in the units of Canny's L1 Sobel magnitude.
    @note Canny compares GRADIENT magnitudes, so its thresholds must come from
        gradients, not from brightness. The former rule (low/high from the
        median intensity) set high = 255 on a white board: a body had to differ
        by ~85 grey levels to count as an edge there, while on a black board 20
        were enough -- fine measurement switched off on light boards.
    @note high = Otsu split of the window's gradient magnitudes (flat surfaces
        versus edges: the two populations are there whatever the colours), low =
        high / 2 (Canny's own 2:1 hysteresis ratio). Both are kept above the
        gradient that pure noise produces, EDGE_NOISE_SIGMAS standard deviations
        of the measured noise (low) and twice that (high): a window with nothing
        in it gives no edges instead of an Otsu split of its noise.
    """
    if blurred is None:
        blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    gx = cv2.Sobel(blurred, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(blurred, cv2.CV_32F, 0, 1, ksize=3)
    magnitude = np.abs(gx) + np.abs(gy)          # stessa norma L1 di cv2.Canny
    # Modulo L1 di due componenti con deviazione s: sta sotto sqrt(2) volte il
    # modulo euclideo, la cui coda oltre k*s e' exp(-k^2/2) (Rayleigh).
    # Il guadagno comprende gia' la sfocatura: il rumore si misura prima.
    floor = EDGE_NOISE_SIGMAS * math.sqrt(2.0) * _noise_sigma(gray) * _gradient_gain()
    split = _otsu_level(magnitude.ravel())
    return max(split / 2.0, floor), max(split, 2.0 * floor)


def _canny_edges(window) -> "np.ndarray":
    """Bordi di Canny della finestra, con soglie dal gradiente (vedi canny_thresholds)."""
    gray = cv2.cvtColor(window, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    low, high = canny_thresholds(gray, blurred)
    return cv2.Canny(blurred, low, high)


def _closed_edges(edges) -> "np.ndarray":
    """Chiusura minima dei bordi: dilatazione e chiusura con nucleo 3x3.

    Un solo giro, kernel minimo: su una scheda densa (componenti vicini,
    ognuno con la propria cornice serigrafica) una chiusura piu' aggressiva
    salda i contorni di pezzi ADIACENTI in un unico blob, che poi
    sostituirebbe la stima con la sagoma sbagliata (il gruppo, non il pezzo).
    """
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    edges = cv2.dilate(edges, kernel, iterations=1)
    return cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=1)


def _edge_mask(window) -> "np.ndarray":
    """Contorni chiusi e utilizzabili nella finestra: Canny + chiusura morfologica.

    Canny lavora sul gradiente locale, non su una soglia assoluta: separa
    un componente nero da un PCB nero se c'e' anche una minima differenza
    di riflettanza tra le due superfici, cosa che una soglia di colore non
    puo' fare. Le sue soglie vengono dal gradiente della finestra
    (`canny_thresholds`), non dalla luminosita'.
    """
    return _closed_edges(_canny_edges(window))


def _edge_extent(edges, contour, rotation_deg):
    """Centro ed estensione dei bordi VERI (non dilatati) racchiusi dal contorno scelto.

    Il contorno viene dalla maschera dilatata e chiusa, quindi e' piu' largo
    del corpo di quanto la morfologia l'ha gonfiato (circa MORPH_GROWTH_PX per
    lato, ma dipende dal nucleo e dalla posa). Invece di togliere una costante
    si rilegge il corpo sui pixel di Canny che il contorno racchiude,
    proiettati sull'asse lungo dichiarato (`rotation_deg`, lo stesso che la
    misura riporta) e sul suo perpendicolare: la crescita sparisce lungo
    ciascun asse, qualunque sia il nucleo, e resta l'errore di quantizzazione
    del bordo. Proiettare sugli assi RIPORTATI, non su quelli grezzi di
    minAreaRect, tiene coerenti ingombro e orientamento: su un corpo piccolo il
    rettangolo grezzo puo' essere storto di qualche grado che la misura poi
    riporta alla cardinale (vedi _axis_rotation).

    Ritorna (cx, cy, lunghezza, larghezza) in pixel della finestra, o None se
    il contorno non racchiude abbastanza pixel di bordo.
    """
    filled = np.zeros(edges.shape[:2], dtype="uint8")
    cv2.drawContours(filled, [contour], -1, 255, thickness=-1)
    ys, xs = np.nonzero((edges > 0) & (filled > 0))
    if xs.size < 4:
        return None
    points = np.stack([xs, ys], axis=1).astype(np.float64)
    # Righe dell'immagine verso il basso: e' lo stesso verso degli angoli di
    # minAreaRect da cui viene rotation_deg.
    angle = math.radians(float(rotation_deg))
    along = np.array([math.cos(angle), math.sin(angle)])
    across = np.array([-math.sin(angle), math.cos(angle)])
    projections = [points @ along, points @ across]
    spans, middle = [], np.zeros(2)
    for index, axis in enumerate((along, across)):
        projection, other = projections[index], projections[1 - index]
        # Il salto di luminosita' cade FRA due pixel e Canny ne marca uno, a
        # volte quello interno e a volte quello esterno: il bordo di un lato e'
        # la media dei pixel entro EDGE_SIDE_BAND_PX dal suo estremo, non
        # l'estremo stesso (che su un lato lungo prende sempre il pixel piu'
        # esterno, su uno corto no: l'errore dipenderebbe dalla taglia). Gli
        # angoli si lasciano fuori: i pixel dei due lati perpendicolari che ci
        # cadono sono gia' dentro il corpo e sul lato corto di un passivo
        # piccolo ne sposterebbero la media verso l'interno.
        core = ((other > other.min() + EDGE_SIDE_BAND_PX + 1e-9)
                & (other < other.max() - EDGE_SIDE_BAND_PX - 1e-9))
        extreme_low, extreme_high = float(projection.min()), float(projection.max())
        sides = []
        for band in (projection <= extreme_low + EDGE_SIDE_BAND_PX + 1e-9,
                     projection >= extreme_high - EDGE_SIDE_BAND_PX - 1e-9):
            chosen = band & core if np.any(band & core) else band
            sides.append(float(projection[chosen].mean()))
        low, high = sides
        spans.append(max(high - low, 1.0))
        middle += axis * (low + high) / 2
    return float(middle[0]), float(middle[1]), spans[0], spans[1]


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

    @param image BGR photo.
    @param frame Board reference in the photo.
    @param component Estimate with x_mm, y_mm, length_mm, width_mm, rotation_deg.
    @param forbidden Optional mask (1 = ignore) of the whole photo.
    @param margin_mm Search margin beyond the half extent; None = SEARCH_MARGIN_MM.
    @return The measured geometry, or None.
    @note The contour is chosen on the closed (dilated) edge mask, and the
        orientation read on its minimum rectangle; centre and size are then
        read on the undilated Canny pixels it encloses, along that orientation
        (_edge_extent). Measured sizes are therefore not inflated by the
        morphological growth, which is constant in pixels and so worth more
        millimetres the coarser the photo.
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
    raw_edges = _canny_edges(window)
    edges = _closed_edges(raw_edges)
    if forbidden is not None:
        blocked = forbidden[y0:y1, x0:x1] == 1
        edges[blocked] = 0
        raw_edges[blocked] = 0
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
    # L'orientamento si legge sul contorno chiuso (angular_resolution_deg ne
    # conosce la crescita); centro e ingombro sui bordi veri che racchiude,
    # lungo l'asse riportato, cosi' la crescita morfologica non entra nella
    # misura (vedi _edge_extent). Senza pixel di bordo utili resta il
    # rettangolo del contorno, meno la crescita dichiarata per lato.
    new_rotation = _axis_rotation(angle, rw, rh, min(rw, rh))
    extent = _edge_extent(raw_edges, best, new_rotation)
    if extent is not None:
        rcx, rcy, long_span, short_span = extent
    else:
        long_span = max(max(rw, rh) - 2 * MORPH_GROWTH_PX, 1.0)
        short_span = max(min(rw, rh) - 2 * MORPH_GROWTH_PX, 1.0)
    x_mm, y_mm = frame.to_mm(x0 + rcx, y0 + rcy)
    if math.hypot(x_mm - float(component["x_mm"]), y_mm - float(component["y_mm"])) > MAX_SHIFT_MM:
        return None
    measured_length = long_span / frame.scale
    measured_width = short_span / frame.scale

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


#: Lato delle classi di colore (Delta E CIE76) dell'istogramma con cui
#: `sample_board_color` cerca il colore piu' esteso della scheda. Ripiego
#: dichiarato: circa due soglie di percettibilita' (MIN_BACKGROUND_DELTA_E),
#: abbastanza stretto da separare il soldermask dai corpi e dalle piazzole,
#: abbastanza largo da tenere insieme il rumore di una superficie. Non decide
#: da solo: il conteggio si fa sulla classe e le sue vicine (3x3x3), quindi il
#: risultato non dipende da dove cadono i confini delle classi.
BOARD_COLOR_BIN_DELTA_E = 5.0
#: Lato lungo, in pixel, a cui si riduce il ritaglio prima di contare i
#: colori: solo velocita'. La riduzione per area media i dettagli fini
#: (serigrafie, piste) ma non sposta il colore di una superficie estesa.
BOARD_COLOR_MAX_SIDE_PX = 512


def sample_board_color(crop) -> Optional[str]:
    """@brief Solder-mask colour of a calibrated board crop, sampled from the photo.

    @param crop BGR image in which the board fills the whole frame (the
        calibrated crop that prepare_photo returns).
    @return "#rrggbb" (lower case), or None when OpenCV is missing or the
        crop is not a non-empty three-channel image.
    @note The mask is the surface that covers the largest area of the board:
        components, pads, silkscreen and holes are many small patches of other
        colours. The crop is binned in CIE L*a*b* (BOARD_COLOR_BIN_DELTA_E
        per class), each class counted together with its 26 neighbours, and
        the result is the per-channel median of the pixels of the winning
        neighbourhood -- so noise and gentle shading of the mask stay inside
        it, and the median ignores the rest.
    @note Declared limit: when one other colour covers more of the board than
        the visible mask (a metal shield or a module over most of it), that
        colour is returned. The caller should present it as sampled, not
        measured, and let the user correct it.
    """
    if cv2 is None or crop is None or getattr(crop, "ndim", 0) != 3 or crop.shape[2] < 3:
        return None
    height, width = crop.shape[:2]
    if height < 1 or width < 1:
        return None
    image = np.ascontiguousarray(crop[:, :, :3])
    longest = max(height, width)
    if longest > BOARD_COLOR_MAX_SIDE_PX:
        factor = BOARD_COLOR_MAX_SIDE_PX / longest
        image = cv2.resize(image, (max(1, round(width * factor)), max(1, round(height * factor))),
                           interpolation=cv2.INTER_AREA)
    pixels = image.reshape(-1, 3)
    lab = _lab(image).reshape(-1, 3)
    # Classi: L in [0, 100], a e b in [-128, 128]; indici non negativi.
    step = BOARD_COLOR_BIN_DELTA_E
    offsets = np.array([0.0, 128.0, 128.0], dtype=np.float32)
    index = np.floor((lab + offsets) / step).astype(np.int64)
    shape = tuple(int(v) for v in np.ceil(np.array([100.0, 256.0, 256.0]) / step) + 1)
    index = np.clip(index, 0, np.array(shape) - 1)
    counts = np.zeros(shape, dtype=np.int64)
    np.add.at(counts, (index[:, 0], index[:, 1], index[:, 2]), 1)
    # Somma sulla classe e le sue vicine: un colore a cavallo di due classi
    # conta per intero, come uno che cade nel mezzo di una.
    padded = np.pad(counts, 1)
    smoothed = np.zeros_like(counts)
    for dl in range(3):
        for da in range(3):
            for db in range(3):
                smoothed += padded[dl:dl + shape[0], da:da + shape[1], db:db + shape[2]]
    mode = np.array(np.unravel_index(int(np.argmax(smoothed)), shape))
    member = np.all(np.abs(index - mode) <= 1, axis=1)
    if not np.any(member):
        member = np.ones(len(pixels), dtype=bool)
    b, g, r = (int(v) for v in np.median(pixels[member], axis=0))
    return f"#{r:02x}{g:02x}{b:02x}"


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
