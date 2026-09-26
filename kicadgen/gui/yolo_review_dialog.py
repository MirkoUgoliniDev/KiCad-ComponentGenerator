"""@brief Revisione delle proposte del rilevatore locale, prima di pagare l'AI.

Il rilevatore propone molto piu' di quanto ci sia: su una scheda da 45
componenti ha prodotto 2265 rilevamenti grezzi su 30 viste, 1238 soppressi
come doppioni, e ne ha consegnati 111 -- 63 sopra la soglia operativa e 48
tenuti apposta sotto soglia, perche' una confidenza bassa non e' prova di
assenza (un regolatore nero su PCB nero e' sopravvissuto a 0,055).

Quelle 111 finivano tutte dentro la chiamata di riconciliazione, che si paga.
Qui si potano prima: costa meno e il modello riceve un problema piu' pulito.
Chi guarda decide, perche' distinguere un corpo da un riflesso e' esattamente
cio' che un occhio fa meglio di una soglia.
"""
import html

from PyQt6.QtCore import QPointF, QRectF, QSignalBlocker, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PyQt6.QtWidgets import (QApplication, QButtonGroup, QComboBox, QDialog,
                             QDialogButtonBox, QHBoxLayout, QLabel, QListWidget,
                             QListWidgetItem, QVBoxLayout, QWidget)

from .review_tools import (ZOOM_STEP, InfoPopup, asset_icon, give_combos_their_keys,
                           set_tool_texts, tool_button, toolbar_divider)
from ..services.photo_yolo import awaiting_user_keep

#: Lato minimo, in mm SULLA SCHEDA, di un riquadro tracciato a mano: sotto
#: non c'e' un corpo montato possibile (il chip piu' piccolo in commercio,
#: 008004, misura 0.25 x 0.125 mm). E' in millimetri e non in frazione della
#: foto: la frazione fissa (0.004) su una scheda da oltre 125 mm impediva di
#: tracciare il lato corto di uno 0402 (0.5 mm). Il tremolio della mano lo
#: scarta gia' la distanza di trascinamento di Qt, in pixel dello schermo.
MIN_NEW_BOX_MM = 0.1

#: Semilato, in pixel della vista, delle maniglie di ridimensionamento e
#: della zona in cui il puntatore le "prende". Piu' piccolo e' impreciso da
#: centrare col mouse; piu' grande copre il bordo del corpo che si sta
#: allineando.
HANDLE_RADIUS_PX = 5

#: Lato minimo, in pixel della vista, a cui si puo' ridurre un riquadro con
#: le maniglie. Esiste per due ragioni: sotto pochi pixel un corpo non e'
#: piu' osservabile (nessuno allinea a occhio un bordo che non vede), e le due
#: maniglie di lati opposti si sovrapporrebbero, rendendo impossibile
#: riallargarlo. Non riusa MIN_PACKAGE_PIXELS_HINT di photo_refine perche'
#: quella soglia e' in pixel DELLA FOTO (risoluzione del ritaglio), mentre qui
#: la scala e' quella dello schermo, che dipende da quanto e' grande la
#: finestra: le due grandezze coincidono solo per caso.
MIN_BOX_PX = 2 * HANDLE_RADIUS_PX + 2

#: Sotto questa larghezza (o altezza), in pixel della vista, un riquadro non
#: ha piu' un interno che le maniglie non coprano: ogni zona di presa entra
#: di HANDLE_RADIUS_PX dentro il bordo, e fra le due opposte deve restare
#: almeno MIN_BOX_PX di interno libero -- lo stesso lato sotto cui un
#: riquadro non e' piu' osservabile. Sotto, un clic dentro il riquadro
#: prenderebbe sempre un angolo, e un componente piccolo sullo schermo
#: (un 0402 a ~19x10 px) non potrebbe essere scelto senza essere alterato.
#: Percio' in un riquadro cosi' l'interno sposta e le maniglie si prendono
#: solo da fuori, dove non c'e' altro da prendere.
SMALL_BOX_PX = 2 * HANDLE_RADIUS_PX + MIN_BOX_PX

#: Sotto questo spostamento, in pixel della vista, un trascinamento e' un
#: clic fermo: il riquadro torna esattamente com'era e non viene marcato come
#: ritoccato. Serve perche' pixel -> normalizzato -> pixel non e' un'identita'
#: in virgola mobile, e un clic non deve cambiare la provenienza di un corpo.
#: Mezzo pixel e' sotto qualunque cosa si possa disegnare o vedere.
STILL_CLICK_PX = 0.5

#: Le otto maniglie come versi (hx, hy) in {-1, 0, 1}: -1 e' il lato sinistro
#: o superiore, +1 il destro o inferiore, 0 il centro del lato. (0, 0) non e'
#: una maniglia: e' l'interno, che sposta il riquadro intero. Gli angoli
#: vengono prima cosi', su un riquadro piccolo, vincono sul lato adiacente.
HANDLES = ((-1, -1), (1, -1), (-1, 1), (1, 1), (0, -1), (0, 1), (-1, 0), (1, 0))
MOVE_HANDLE = (0, 0)

#: Cursore per ogni maniglia: chi guarda deve capire cosa fara' il
#: trascinamento prima di iniziarlo.
HANDLE_CURSORS = {
    (-1, -1): Qt.CursorShape.SizeFDiagCursor, (1, 1): Qt.CursorShape.SizeFDiagCursor,
    (1, -1): Qt.CursorShape.SizeBDiagCursor, (-1, 1): Qt.CursorShape.SizeBDiagCursor,
    (0, -1): Qt.CursorShape.SizeVerCursor, (0, 1): Qt.CursorShape.SizeVerCursor,
    (-1, 0): Qt.CursorShape.SizeHorCursor, (1, 0): Qt.CursorShape.SizeHorCursor,
    MOVE_HANDLE: Qt.CursorShape.SizeAllCursor,
}

#: Provenienza dichiarata di un riquadro che l'utente ha spostato,
#: ridimensionato o tracciato: la stessa stringa per tutti e tre i casi,
#: perche' per chi sta a valle sono la stessa cosa -- l'occhio di chi guarda.
MANUAL_SOURCE = "revisione manuale"

#: Colore delle proposte sopra la soglia operativa e di quelle sotto. La
#: differenza non e' estetica: sulle deboli la decisione di chi guarda conta
#: davvero, sulle altre serve solo a togliere un falso positivo evidente.
STRONG_COLOR = "#a6e3a1"
WEAK_COLOR = "#f9e2af"
ADDED_COLOR = "#89b4fa"
SELECTED_COLOR = "#f38ba8"
#: Proposte sotto la soglia operativa (`_yolo_low_confidence`): mostrate ma
#: NON tenute finche' l'utente non le tiene (photo_yolo.awaiting_user_keep).
#: Tinta propria, oltre al tratteggio, perche' la decisione qui e' opposta a
#: quella delle altre: sulle altre si toglie, su queste si aggiunge.
LOW_CONFIDENCE_COLOR = "#cba6f7"

#: Stati dello sfondo della vista, nell'ordine in cui il pulsante li
#: attraversa: fotografia intera, attenuata, tolta. Stessi identificatori e
#: stesse etichette di `photo_review_dialog`: le due finestre risolvono lo
#: stesso problema e chi le usa non deve incontrare due convenzioni.
PHOTO_FULL = "full"
PHOTO_DIMMED = "dimmed"
PHOTO_HIDDEN = "hidden"
PHOTO_STATES = (PHOTO_FULL, PHOTO_DIMMED, PHOTO_HIDDEN)

#: Per ogni stato: il comando del passo successivo, lo stato attuale e l'icona
#: che lo disegna. Stessi testi di `photo_review_dialog`.
PHOTO_STATE_TEXTS = {
    PHOTO_FULL: ("Attenua foto", "Foto: intera", "photo-full.svg"),
    PHOTO_DIMMED: ("Nascondi foto", "Foto: attenuata", "photo-dimmed.svg"),
    PHOTO_HIDDEN: ("Mostra foto", "Foto: nascosta", "photo-hidden.svg"),
}
#: Seconda riga del tooltip del comando della foto, dopo lo stato attuale.
PHOTO_BUTTON_EXPLANATION = (
    "Su una foto scura e affollata i contorni si leggono meglio quando la fotografia "
    "si fa da parte; il bordo della PCB resta disegnato.")
#: Primo paragrafo del popup «Come si usa».
INFO_INTRO = (
    "I riquadri sono le proposte del rilevatore. Tieni quelli che sono componenti, "
    "elimina gli altri, aggiungi quelli che mancano. I tratteggiati sono le proposte "
    "su cui non si e' fidato: ci sono perche' una confidenza bassa non e' prova di "
    "assenza.")

#: Secondo paragrafo del popup: le proposte a bassa confidenza sono opt-in.
INFO_LOW_CONFIDENCE = (
    "Le proposte puntinate sono sotto la soglia di confidenza del rilevatore: le vedi, "
    "ma restano escluse e non vanno al modello a pagamento finché non le tieni (T). "
    "Spostarne o ridimensionarne una vale come tenerla.")

#: Opacita' della fotografia attenuata. La scheda resta riconoscibile -- e
#: capire su QUALE corpo poggia un riquadro e' esattamente cio' che si sta
#: rivedendo -- mentre quasi tutto il contrasto torna ai contorni. Bassa
#: abbastanza perche' il risultato composto su BOARD_BACKDROP_COLOR resti
#: molto piu' vicino allo sfondo neutro che alla fotografia; la garanzia
#: piena sul contrasto la da' solo PHOTO_HIDDEN.
PHOTO_DIMMED_OPACITY = .25

#: Riempimento neutro disegnato al posto della fotografia quando questa e'
#: attenuata o tolta. Scelto per misura, non a occhio: la sua luminanza
#: relativa (~0.038) tiene il rapporto di contrasto WCAG sopra 3:1 contro il
#: PIU' SCURO dei tratti che questa finestra usa (SELECTED_COLOR, ~5.2:1) e
#: quindi anche contro gli altri tre, che sono piu' chiari. I colori di stato
#: portano significato -- il testo della finestra spiega i tratteggiati --
#: percio' si sceglie lo sfondo attorno a loro, mai il contrario. E' anche
#: piu' chiaro dello sfondo dell'applicazione (BASE del tema, #1e1e2e), che e'
#: cio' che rende l'area della scheda ancora leggibile come regione.
BOARD_BACKDROP_COLOR = "#32373e"

#: Bordo della scheda disegnato con quello sfondo. Tolta la fotografia e'
#: l'unica traccia rimasta di dove finisce la PCB, quindi un corpo che sta
#: fuori dal bordo resta giudicabile. Grigio apposta: non deve essere scambiato
#: per il tratto colorato di una proposta ne' per le maniglie.
BOARD_BORDER_COLOR = "#8a93a0"
BOARD_BORDER_WIDTH_PX = 2

#: Strumenti della vista (scelta dell'utente del 2026-09-24, dopo il mockup):
#: con Seleziona un clic sceglie un riquadro, un clic nel vuoto toglie la
#: scelta e un trascinamento nel vuoto sposta la vista; con Disegna un
#: trascinamento traccia un riquadro nuovo. Prima un trascinamento nel vuoto
#: disegnava sempre e un clic nel vuoto non faceva nulla: la scelta non si
#: poteva togliere.
TOOL_SELECT = "select"
TOOL_DRAW = "draw"
TOOLS = (TOOL_SELECT, TOOL_DRAW)

#: Ingrandimento rispetto alla foto adattata alla vista. Sotto 1 la foto si
#: rimpicciolirebbe dentro una vista vuota senza mostrare nulla di piu'. Il
#: massimo e' una scelta: a 16x un 0402, che adattato misura pochi pixel,
#: diventa largo decine di pixel, e oltre si ingrandiscono solo i pixel della
#: foto stessa.
MIN_ZOOM = 1.0
MAX_ZOOM = 16.0
#: Passo di uno scatto della rotella e di un clic su zoom +/-: lo stesso
#: della finestra «Revisione componenti», vedi `review_tools.ZOOM_STEP`.
WHEEL_ZOOM_STEP = BUTTON_ZOOM_STEP = ZOOM_STEP


def _origin_color(proposal):
    """@brief Colore di un riquadro secondo la sua provenienza."""
    if proposal.get("_yolo_origin") == "user_added":
        return ADDED_COLOR
    if proposal.get("_yolo_low_confidence"):
        return LOW_CONFIDENCE_COLOR
    return WEAK_COLOR if proposal.get("_yolo_review_only") else STRONG_COLOR


def box_pen(proposal, chosen=False):
    """@brief Tratto con cui si disegna una proposta.

    @param proposal Proposta del rilevatore, o aggiunta a mano.
    @param chosen Se e' quella scelta in questo momento.
    @return Penna da usare per il suo rettangolo.
    @note Il colore da solo non basta: chi non distingue due tinte deve
        comunque vedere dove la macchina non si e' fidata, percio' le proposte
        sotto soglia sono tratteggiate.
    """
    pen = QPen(QColor(SELECTED_COLOR if chosen else _origin_color(proposal)),
               2.5 if chosen else 1.4)
    if awaiting_user_keep(proposal):
        # Non tenuta: puntinata, piu' rada del tratteggio delle deboli, cosi'
        # si distingue anche senza distinguere i colori.
        pen.setStyle(Qt.PenStyle.DotLine)
    elif proposal.get("_yolo_review_only"):
        pen.setStyle(Qt.PenStyle.DashLine)
    return pen


class BoardView(QWidget):
    """@brief Fotografia della scheda con sopra i riquadri proposti.

    @note Lavora in coordinate normalizzate [0, 1], come `_box` del
        rilevatore: cosi' la vista puo' essere ridimensionata, ingrandita o
        spostata a piacere senza che le proposte cambino. Le maniglie e le
        soglie in pixel (HANDLE_RADIUS_PX, MIN_BOX_PX) restano pixel dello
        SCHERMO a ogni ingrandimento.
    """
    selection_changed = pyqtSignal(int)
    box_added = pyqtSignal(tuple)
    #: Indice della proposta il cui `_box` e' stato spostato o ridimensionato
    #: a mano, e la maniglia (di HANDLES o MOVE_HANDLE) con cui: chi riceve
    #: deve sapere QUALI lati sono stati guardati. Viene emesso solo al
    #: rilascio e solo se la geometria e' davvero cambiata: un clic fermo su
    #: un riquadro non e' una modifica.
    box_edited = pyqtSignal(int, tuple)
    #: Ingrandimento o spostamento cambiati: la barra aggiorna la percentuale.
    view_changed = pyqtSignal()
    #: Punto della scheda sotto il puntatore, normalizzato (x, y), o None fuori
    #: dalla foto: la barra di stato lo mostra in millimetri.
    pointer_moved = pyqtSignal(object)

    def __init__(self, pixmap, proposals, parent=None):
        super().__init__(parent)
        self.pixmap = pixmap
        self.proposals = proposals
        self.selected = -1
        self._drag_from = None
        self._drag_to = None
        # Modifica in corso di un riquadro esistente: (indice, maniglia,
        # `_box` di partenza, punto di pressione). None quando non si sta
        # trascinando nulla di esistente.
        self._edit = None
        # Stato dello sfondo: appartiene alla vista perche' e' la vista a
        # dipingere. Non tocca nulla di cio' che la finestra restituisce.
        self._photo_state = PHOTO_FULL
        # Vista: strumento attivo, ingrandimento rispetto alla foto adattata e
        # spostamento in pixel. Come la foto, non toccano le proposte.
        self.tool = TOOL_SELECT
        self._zoom = MIN_ZOOM
        self._pan = QPointF()
        # Pressione nel vuoto in corso: (punto, spostamento di partenza,
        # pulsante, mosso). Un rilascio senza movimento col sinistro
        # deseleziona; un trascinamento sposta la vista.
        self._pan_press = None
        # Indici delle proposte nascoste dal filtro per categoria (utente,
        # 2026-09-24: il filtro filtra anche la foto). Non si disegnano e non
        # si scelgono; restano nei dati, che il filtro non tocca mai.
        self.hidden = set()
        # Quote della scheda (lungo X, lungo Y) in mm, per il lato minimo di
        # un riquadro tracciato (MIN_NEW_BOX_MM). None: nessun minimo in mm,
        # resta solo la distanza di trascinamento.
        self.board_mm = None
        self.setMinimumSize(480, 320)
        self.setMouseTracking(True)

    def photo_state(self):
        """@brief Quanto della fotografia la vista sta mostrando.

        @return Uno fra PHOTO_FULL, PHOTO_DIMMED e PHOTO_HIDDEN.
        @note E' solo un modo di guardare: non entra in `_box` ne' in nessun
            altro campo delle proposte, quindi non raggiunge chi paga la
            chiamata a valle.
        """
        return self._photo_state

    def set_photo_state(self, state):
        """@brief Mostra la fotografia intera, la attenua, o la toglie.

        @param state Uno fra PHOTO_FULL, PHOTO_DIMMED, PHOTO_HIDDEN.
        @throws ValueError per qualunque altro valore: uno stato ignorato in
            silenzio lascerebbe la vista e il pulsante in disaccordo su cosa
            si sta guardando, che e' esattamente cio' che questo comando
            esiste per rendere inequivocabile.
        @note Cambia solo cio' che si vede. Scelta, spostamento, le otto
            maniglie, il rettangolo tracciato e le coordinate normalizzate
            restano quelli di prima: `_frame()` non dipende dallo stato.
        """
        if state not in PHOTO_STATES:
            raise ValueError(f"Unknown photo state: {state!r}")
        self._photo_state = state
        self.update()

    def cycle_photo_state(self):
        """@brief Passa allo stato successivo: intera, attenuata, nascosta.

        @note Monotono apposta, un passo per clic sempre piu' lontano dalla
            fotografia: un ciclo che saltasse dritto a "nascosta" sarebbe piu'
            rapido per il caso che l'utente ha chiesto, al prezzo di un ordine
            che dal pulsante non si puo' prevedere.
        """
        index = (PHOTO_STATES.index(self._photo_state) + 1) % len(PHOTO_STATES)
        self.set_photo_state(PHOTO_STATES[index])

    def set_tool(self, tool):
        """@brief Sceglie lo strumento: TOOL_SELECT o TOOL_DRAW.

        @param tool Uno di TOOLS.
        @throws ValueError per qualunque altro valore.
        @note Cambiare strumento abbandona un rettangolo in corso di disegno.
        """
        if tool not in TOOLS:
            raise ValueError(f"Unknown tool: {tool!r}")
        self.tool = tool
        self._drag_from = self._drag_to = None
        self.setCursor(Qt.CursorShape.CrossCursor if tool == TOOL_DRAW
                       else Qt.CursorShape.ArrowCursor)
        self.update()

    def cancel_drawing(self):
        """@brief Abbandona il rettangolo che si sta tracciando.

        @return True se ce n'era uno.
        """
        if self._drag_from is None:
            return False
        self._drag_from = self._drag_to = None
        self.update()
        return True

    def cancel_gesture(self):
        """@brief Abbandona qualunque gesto del mouse in corso, riportando le cose com'erano.

        @return True se c'era un gesto da abbandonare.
        @note Serve a Canc premuto DURANTE un trascinamento (revisore 1,
            difetto 3): `_edit` tiene l'indice della proposta presa, che dopo
            la cancellazione punta alla successiva, e il rilascio la spostava
            alle coordinate di quella cancellata marcandola manuale. Il
            riquadro trascinato torna dov'era: la cancellazione decide, non il
            movimento a meta'.
        """
        active = self._edit is not None or self._drag_from is not None or self._pan_press is not None
        if self._edit is not None:
            index, _handle, origin, _start = self._edit
            if 0 <= index < len(self.proposals):
                self.proposals[index]["_box"] = origin
        self._edit = None
        self._drag_from = self._drag_to = None
        self._pan_press = None
        if active:
            self.setCursor(Qt.CursorShape.CrossCursor if self.tool == TOOL_DRAW
                           else Qt.CursorShape.ArrowCursor)
            self.update()
        return active

    def zoom(self):
        """@brief Ingrandimento attuale rispetto alla foto adattata alla vista (1 = adattata)."""
        return self._zoom

    def zoom_at(self, factor, anchor=None):
        """@brief Ingrandisce di `factor` tenendo fermo sotto `anchor` il punto della scheda.

        @param factor Moltiplicatore; >1 avvicina.
        @param anchor Punto della vista che resta fermo (il puntatore per la
            rotella); il centro della vista se None.
        @note L'ingrandimento resta fra MIN_ZOOM e MAX_ZOOM.
        """
        anchor = QPointF(anchor) if anchor is not None else QPointF(self.width() / 2, self.height() / 2)
        target = min(MAX_ZOOM, max(MIN_ZOOM, self._zoom * factor))
        if target == self._zoom:
            return
        before = self._frame()
        nx = (anchor.x() - before.x()) / max(1e-9, before.width())
        ny = (anchor.y() - before.y()) / max(1e-9, before.height())
        self._zoom = target
        self._pan = QPointF()
        after = self._frame()
        self._pan = QPointF(anchor.x() - (after.x() + nx * after.width()),
                            anchor.y() - (after.y() + ny * after.height()))
        self._clamp_pan()
        self.update()
        self.view_changed.emit()

    def fit_view(self):
        """@brief Torna alla foto intera, adattata alla vista."""
        self._zoom = MIN_ZOOM
        self._pan = QPointF()
        self.update()
        self.view_changed.emit()

    def _clamp_pan(self):
        """Tiene la foto ingrandita a coprire la vista: niente vuoto oltre i bordi.

        Su un asse in cui la foto e' piu' piccola della vista resta centrata.
        """
        fitted = self._fitted_frame()
        width = fitted.width() * self._zoom
        height = fitted.height() * self._zoom
        cx, cy = fitted.center().x(), fitted.center().y()

        def clamp(value, extent, center, size):
            if extent <= size:
                return 0.0
            return min(extent / 2 - center, max(size - center - extent / 2, value))

        self._pan = QPointF(clamp(self._pan.x(), width, cx, self.width()),
                            clamp(self._pan.y(), height, cy, self.height()))

    def _fitted_frame(self):
        """Rettangolo della foto intera adattata alla vista, senza ingrandimento."""
        if self.pixmap.isNull():
            return QRectF(0, 0, max(1, self.width()), max(1, self.height()))
        scaled = self.pixmap.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        return QRectF((self.width() - scaled.width()) // 2,
                      (self.height() - scaled.height()) // 2,
                      scaled.width(), scaled.height())

    def _frame(self):
        """@brief Rettangolo, in pixel della vista, in cui la foto e' disegnata.

        @note Con l'ingrandimento puo' essere piu' grande della vista: tutte le
            conversioni passano da qui, quindi riquadri, maniglie e disegno
            restano allineati alla foto a ogni ingrandimento.
        """
        fitted = self._fitted_frame()
        width = fitted.width() * self._zoom
        height = fitted.height() * self._zoom
        return QRectF(fitted.center().x() - width / 2 + self._pan.x(),
                      fitted.center().y() - height / 2 + self._pan.y(), width, height)

    def resizeEvent(self, event):  # noqa: N802 - Qt override
        """@brief Tiene lo spostamento valido quando la vista cambia misura."""
        super().resizeEvent(event)
        self._clamp_pan()

    def wheelEvent(self, event):  # noqa: N802 - Qt override
        """@brief Rotella: ingrandisce attorno al puntatore."""
        steps = event.angleDelta().y() / 120
        if steps:
            self.zoom_at(WHEEL_ZOOM_STEP ** steps, event.position())
        event.accept()

    def leaveEvent(self, event):  # noqa: N802 - Qt override
        """@brief Fuori dalla vista non c'e' un punto della scheda da mostrare."""
        self.pointer_moved.emit(None)
        super().leaveEvent(event)

    def _report_pointer(self, position):
        frame = self._frame()
        x = (position.x() - frame.x()) / max(1e-9, frame.width())
        y = (position.y() - frame.y()) / max(1e-9, frame.height())
        self.pointer_moved.emit((x, y) if 0 <= x <= 1 and 0 <= y <= 1 else None)

    def to_widget(self, box):
        """@brief Da coordinate normalizzate a pixel della vista."""
        frame = self._frame()
        x0, y0, x1, y1 = box
        return QRectF(frame.x() + x0 * frame.width(), frame.y() + y0 * frame.height(),
                      (x1 - x0) * frame.width(), (y1 - y0) * frame.height())

    def to_normalized(self, point):
        """@brief Da pixel della vista a coordinate normalizzate, dentro [0, 1]."""
        frame = self._frame()
        x = (point.x() - frame.x()) / max(1, frame.width())
        y = (point.y() - frame.y()) / max(1, frame.height())
        return min(1.0, max(0.0, x)), min(1.0, max(0.0, y))

    def _selected_rect(self):
        """@brief Rettangolo in pixel del riquadro scelto, o None se non c'e'."""
        if 0 <= self.selected < len(self.proposals):
            return self.to_widget(self.proposals[self.selected]["_box"])
        return None

    @staticmethod
    def _handle_center(rect, handle):
        """@brief Punto, in pixel, in cui sta la maniglia `handle` di `rect`."""
        hx, hy = handle
        x = {-1: rect.left(), 0: rect.center().x(), 1: rect.right()}[hx]
        y = {-1: rect.top(), 0: rect.center().y(), 1: rect.bottom()}[hy]
        return QPointF(x, y)

    def handle_at(self, position):
        """@brief Che cosa prenderebbe un trascinamento iniziato in `position`.

        @param position Punto in pixel della vista.
        @return Una maniglia di HANDLES, MOVE_HANDLE per l'interno del
            riquadro scelto, None se il punto non tocca il riquadro scelto.
        @note Solo il riquadro scelto ha maniglie: su una scheda fitta, otto
            maniglie per ogni proposta coprirebbero i corpi vicini.
        @note Se il riquadro e' piu' stretto di SMALL_BOX_PX su un asse, il
            suo interno (bordo compreso) e' sempre MOVE_HANDLE e le maniglie
            si prendono solo da fuori: altrimenti un clic per sceglierlo
            cadrebbe per forza su un angolo e lo ridimensionerebbe.
        """
        rect = self._selected_rect()
        if rect is None:
            return None
        inside = rect.contains(position)
        if inside and (rect.width() < SMALL_BOX_PX or rect.height() < SMALL_BOX_PX):
            return MOVE_HANDLE
        for handle in HANDLES:
            center = self._handle_center(rect, handle)
            if (abs(position.x() - center.x()) <= HANDLE_RADIUS_PX
                    and abs(position.y() - center.y()) <= HANDLE_RADIUS_PX):
                return handle
        return MOVE_HANDLE if inside else None

    def _edited_rect(self, origin, handle, delta):
        """@brief Rettangolo che risulta trascinando `handle` di `delta` pixel.

        @param origin Rettangolo di partenza, in pixel.
        @param handle Maniglia di HANDLES o MOVE_HANDLE.
        @param delta Spostamento del puntatore dalla pressione.
        @return Nuovo rettangolo, sempre dentro la foto: il lato trascinato
            si ferma contro l'opposto, non lo scavalca, cosi' un riquadro non
            puo' rovesciarsi ne' sparire.
        @note Il lato minimo e' MIN_BOX_PX, ma mai piu' del lato che il
            riquadro ha gia': un riquadro piu' piccolo del minimo (un 0402
            visto da lontano) puo' essere allargato, non ristretto, e con
            delta nullo resta esattamente com'e'. Se il minimo fosse
            assoluto, il solo prendere la maniglia lo farebbe crescere da
            solo, e una misura della macchina diventerebbe "manuale" senza
            che nessuno abbia deciso nulla.
        """
        frame = QRectF(self._frame())
        hx, hy = handle
        rect = QRectF(origin)
        if handle == MOVE_HANDLE:
            dx = min(max(delta.x(), frame.left() - rect.left()), frame.right() - rect.right())
            dy = min(max(delta.y(), frame.top() - rect.top()), frame.bottom() - rect.bottom())
            return rect.translated(dx, dy)
        min_w = min(MIN_BOX_PX, rect.width())
        min_h = min(MIN_BOX_PX, rect.height())
        if hx < 0:
            rect.setLeft(min(max(frame.left(), rect.left() + delta.x()), rect.right() - min_w))
        elif hx > 0:
            rect.setRight(max(min(frame.right(), rect.right() + delta.x()), rect.left() + min_w))
        if hy < 0:
            rect.setTop(min(max(frame.top(), rect.top() + delta.y()), rect.bottom() - min_h))
        elif hy > 0:
            rect.setBottom(max(min(frame.bottom(), rect.bottom() + delta.y()), rect.top() + min_h))
        return rect

    def paintEvent(self, event):
        """@brief Disegna lo sfondo, i riquadri in gioco e le maniglie di quello scelto.

        @note Lo sfondo neutro va sotto la fotografia, non al posto suo:
            attenuare deve sfumare la foto dentro una base nota, altrimenti il
            contrasto dei tratti dipenderebbe dal colore della scheda sotto.
        @note Il bordo della scheda si disegna DOPO la fotografia: nello stato
            attenuato un bordo sotto verrebbe sbiadito proprio quando serve,
            perche' e' l'unico riferimento di dove finisce la PCB.
        """
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        frame = self._frame()
        backdrop = self._photo_state != PHOTO_FULL
        if backdrop:
            painter.fillRect(frame, QColor(BOARD_BACKDROP_COLOR))
        if not self.pixmap.isNull() and self._photo_state != PHOTO_HIDDEN:
            if self._photo_state == PHOTO_DIMMED:
                painter.setOpacity(PHOTO_DIMMED_OPACITY)
            painter.drawPixmap(frame, self.pixmap, QRectF(self.pixmap.rect()))
            painter.setOpacity(1.0)
        if backdrop:
            painter.setPen(QPen(QColor(BOARD_BORDER_COLOR), BOARD_BORDER_WIDTH_PX))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            # Mezzo tratto dentro il fotogramma: una penna centrata sul bordo
            # esatto verrebbe tagliata a meta' quando la foto riempie la vista.
            half = BOARD_BORDER_WIDTH_PX / 2
            painter.drawRect(QRectF(frame).adjusted(half, half, -half, -half))
        for index, proposal in enumerate(self.proposals):
            if index in self.hidden and index != self.selected:
                continue
            painter.setPen(box_pen(proposal, index == self.selected))
            painter.drawRect(self.to_widget(proposal["_box"]))
        rect = self._selected_rect()
        if rect is not None:
            painter.setPen(QPen(QColor(SELECTED_COLOR), 1.0))
            painter.setBrush(QColor(SELECTED_COLOR))
            side = 2 * HANDLE_RADIUS_PX
            for handle in HANDLES:
                center = self._handle_center(rect, handle)
                painter.drawRect(QRectF(center.x() - HANDLE_RADIUS_PX,
                                        center.y() - HANDLE_RADIUS_PX, side, side))
            painter.setBrush(Qt.BrushStyle.NoBrush)
        if self._drag_from and self._drag_to:
            painter.setPen(QPen(QColor(ADDED_COLOR), 1.6, Qt.PenStyle.DotLine))
            painter.drawRect(QRectF(self._drag_from, self._drag_to).normalized())

    def mousePressEvent(self, event):
        """@brief Pressione: prende una maniglia, sceglie un riquadro, sposta la vista o disegna.

        @note Le maniglie del riquadro scelto vincono sull'interno di un
            altro riquadro che le copra: chi ha scelto un corpo sta lavorando
            su quello. Il suo interno invece no: un riquadro piu' piccolo che
            ci sta dentro si sceglie (`box_at`). Scegliere un riquadro avvia
            anche lo spostamento, ma un clic fermo non cambia nulla (vedi
            mouseReleaseEvent).
        @note Con Seleziona, una pressione nel vuoto sposta la vista se si
            trascina e toglie la scelta se si rilascia fermi. Con Disegna, le
            maniglie del riquadro scelto restano prendibili (lo si aggiusta
            subito dopo averlo tracciato), un trascinamento traccia e un clic
            fermo sceglie il riquadro sotto il puntatore (vedi
            mouseReleaseEvent). Il tasto centrale sposta la vista con entrambi
            gli strumenti. Fra riquadri sovrapposti vince il piu' piccolo
            (`box_at`).
        """
        position = event.position()
        if event.button() == Qt.MouseButton.MiddleButton:
            self._pan_press = (position, QPointF(self._pan), event.button(), False)
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        handle = self.handle_at(position)
        if self.tool == TOOL_DRAW and handle == MOVE_HANDLE:
            handle = None
        if handle == MOVE_HANDLE and self.box_at(position) not in (None, self.selected):
            # L'interno del riquadro scelto non copre i riquadri piu' piccoli
            # che gli stanno dentro: si sceglie quello (revisore 1, difetto
            # 2), con la stessa regola del clic su un riquadro non scelto.
            # Le maniglie invece restano del riquadro scelto.
            handle = None
        if handle is None and self.tool == TOOL_SELECT:
            index = self.box_at(position)
            if index is not None:
                self.selected = index
                self.selection_changed.emit(index)
                handle = MOVE_HANDLE
        if handle is not None:
            self._edit = (self.selected, handle,
                          tuple(self.proposals[self.selected]["_box"]), position)
            self.setCursor(HANDLE_CURSORS[handle])
            self.update()
            return
        if self.tool == TOOL_DRAW:
            self._drag_from, self._drag_to = position, position
            return
        self._pan_press = (position, QPointF(self._pan), event.button(), False)

    def large_enough(self, box):
        """@brief Tell whether a drawn box can be a mounted body, in millimetres on the board.

        @param box Normalized (x0, y0, x1, y1).
        @return True when both sides reach MIN_NEW_BOX_MM on the board (or
            when the board size is unknown, board_mm None).
        """
        if self.board_mm is None:
            return box[2] > box[0] and box[3] > box[1]
        length, width = self.board_mm
        return ((box[2] - box[0]) * length >= MIN_NEW_BOX_MM
                and (box[3] - box[1]) * width >= MIN_NEW_BOX_MM)

    def box_at(self, position):
        """@brief Il riquadro sotto il punto: fra quelli che lo contengono, il piu' piccolo.

        @param position Punto in pixel della vista.
        @return Indice della proposta, o None se il punto non tocca nessun riquadro.
        @note Il piu' piccolo e non l'ultimo dell'elenco: una proposta spuria
            piccola dentro o sopra un corpo grande (sul bordo del modulo, dentro
            un regolatore) altrimenti non si poteva prendere, perche' il clic
            finiva al grande se questo veniva dopo nell'elenco (utente,
            2026-09-24). A parita' di area vince l'ultimo, che e' disegnato sopra.
        """
        found, best = None, None
        for index, proposal in enumerate(self.proposals):
            if index in self.hidden:
                continue
            rect = self.to_widget(proposal["_box"])
            if not rect.contains(position):
                continue
            area = rect.width() * rect.height()
            if best is None or area <= best:
                found, best = index, area
        return found

    def mouseMoveEvent(self, event):
        """@brief Trascina cio' che e' stato preso, o mostra cosa si prenderebbe."""
        position = event.position()
        self._report_pointer(position)
        if self._edit is not None:
            index, handle, origin, start = self._edit
            rect = self._edited_rect(self.to_widget(origin), handle, position - start)
            x0, y0 = self.to_normalized(rect.topLeft())
            x1, y1 = self.to_normalized(rect.bottomRight())
            self.proposals[index]["_box"] = (x0, y0, x1, y1)
            self.update()
            return
        if self._drag_from is not None:
            self._drag_to = position
            self.update()
            return
        if self._pan_press is not None:
            start, origin, button, moved = self._pan_press
            delta = position - start
            if not moved and delta.manhattanLength() < QApplication.startDragDistance():
                return
            self._pan_press = (start, origin, button, True)
            self._pan = origin + delta
            self._clamp_pan()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            self.update()
            return
        handle = self.handle_at(position)
        if self.tool == TOOL_DRAW and handle in (None, MOVE_HANDLE):
            self.setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.setCursor(HANDLE_CURSORS[handle] if handle is not None
                           else Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, event):
        """@brief Il rilascio conferma la modifica, il rettangolo tracciato o la deselezione."""
        if self._edit is not None:
            index, handle, origin, _start = self._edit
            self._edit = None
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.update()
            if index >= len(self.proposals):
                return
            before, after = self.to_widget(origin), self.to_widget(self.proposals[index]["_box"])
            moved = max(abs(after.left() - before.left()), abs(after.top() - before.top()),
                        abs(after.right() - before.right()), abs(after.bottom() - before.bottom()))
            if moved < STILL_CLICK_PX:
                self.proposals[index]["_box"] = origin
            else:
                self.box_edited.emit(index, tuple(handle))
            return
        if self._pan_press is not None:
            _start, _origin, button, moved = self._pan_press
            self._pan_press = None
            self.setCursor(Qt.CursorShape.ArrowCursor)
            # Clic fermo nel vuoto col sinistro: toglie la scelta. Era il
            # difetto segnalato dall'utente: la scelta non si poteva togliere.
            if not moved and button == Qt.MouseButton.LeftButton and self.selected != -1:
                self.selected = -1
                self.selection_changed.emit(-1)
                self.update()
            return
        if self._drag_from is None:
            return
        start, end = self._drag_from, event.position()
        self._drag_from = self._drag_to = None
        self.update()
        x0, y0 = self.to_normalized(start)
        x1, y1 = self.to_normalized(end)
        box = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        # Un tremolio della mano non e' un trascinamento: sotto la distanza
        # che Qt usa per iniziare un trascinamento (la stessa dello
        # spostamento della vista) e' un clic, a qualunque ingrandimento.
        # Prima contava solo un minimo relativo alla foto: al 100% un
        # tremolio di pochi pixel creava un riquadro invece di scegliere.
        dragged = (end - start).manhattanLength() >= QApplication.startDragDistance()
        if dragged and self.large_enough(box):
            self.box_added.emit(box)
            return
        # Con Disegna un clic fermo SCEGLIE (o toglie la scelta nel vuoto):
        # solo un trascinamento traccia. Prima un clic su un riquadro con
        # Disegna attivo non faceva nulla, e le proposte spurie non si
        # potevano scegliere per eliminarle (utente, 2026-09-24).
        index = self.box_at(start)
        target = -1 if index is None else index
        if target != self.selected:
            self.selected = target
            self.selection_changed.emit(target)
            self.update()


#: Filter value of the proposals without a detector class (drawn by hand).
NO_CLASS = "<senza categoria>"


def _filter_class(proposal) -> str:
    """Class a proposal is filtered by; NO_CLASS when it has none."""
    return proposal.get("_yolo_class") or NO_CLASS


class YoloReviewDialog(QDialog):
    """@brief Mostra le proposte del rilevatore e restituisce quelle da tenere.

    @param photo_png Fotografia calibrata, gia' codificata in PNG.
    @param proposals Proposte del rilevatore, con `_box` normalizzato.
    @param length_mm Estensione della scheda lungo X, in mm.
    @param width_mm Estensione lungo Y, in mm.
    @param classes Categorie che il rilevatore conosce. Dal 2026-09-24 la
        finestra non ha piu' un menu per correggerle: restano solo nella
        firma, per chi gia' le passa.
    @note Annullare significa non procedere: chi chiama non deve pagare la
        chiamata di riconciliazione con proposte che non sono state approvate.
    @note Proposte a bassa confidenza (`_yolo_low_confidence`, utente 26/09):
        disegnate puntinate con LOW_CONFIDENCE_COLOR ed etichettate «bassa
        confidenza», partono NON tenute e `kept()` le esclude finche' l'utente
        non le tiene (Tieni, T) o non le ritocca (mark_manual).
    @note La fotografia puo' farsi da parte (`photo_state()`,
        `set_photo_state()`, `cycle_photo_state()`): su una scheda affollata i
        corpi piccoli sono illeggibili sopra la foto, e qui la leggibilita'
        decide cosa viene mandato e pagato. E' un comando di vista e basta:
        `kept()` e le proposte non cambiano di un byte, e ogni finestra nuova
        riparte da PHOTO_FULL, perche' la fotografia e' il riferimento da cui
        la revisione comincia.
    @note Barra di icone in alto (scelta dell'utente del 2026-09-24): strumenti
        Seleziona (V) e Disegna (R), Elimina (Canc), Tieni (T, solo per le
        proposte a bassa confidenza), zoom (+, -, F, rotella),
        foto (P) e le istruzioni nel popup dell'icona «i». Con Seleziona un
        clic nel vuoto toglie la scelta e un trascinamento nel vuoto sposta la
        vista; Esc annulla il gesto, poi lo strumento, poi la scelta, e non
        chiude mai la finestra. Zoom e spostamento sono solo vista: `kept()`
        non cambia.
    """

    def __init__(self, photo_png, proposals, length_mm, width_mm, classes, parent=None):
        super().__init__(parent)
        # Traduzione dei testi dalla finestra che la apre, come la revisione
        # fotografica: una finestra aperta da sola resta in italiano.
        self.i18n = getattr(parent, "i18n", None)
        self.setWindowTitle(self._tr("Corpi rilevati: togli quelli che non sono componenti"))
        self.setModal(True)
        self.length_mm, self.width_mm = float(length_mm), float(width_mm)
        self.classes = list(classes)
        # Copia: annullare deve lasciare intatto cio' che ha prodotto il
        # rilevatore, e la lista originale appartiene a chi chiama.
        self.proposals = [dict(p) for p in proposals]
        self._strong = sum(1 for p in self.proposals if not p.get("_yolo_review_only"))
        self._weak = len(self.proposals) - self._strong
        # Proposte sotto soglia da tenere a mano (photo_yolo.awaiting_user_keep):
        # contate a parte, perche' qui l'azione di chi guarda e' tenerle.
        self._low = sum(1 for p in self.proposals if p.get("_yolo_low_confidence"))

        pixmap = QPixmap()
        if photo_png:
            image = QImage()
            image.loadFromData(bytes(photo_png))
            pixmap = QPixmap.fromImage(image)
        self.view = BoardView(pixmap, self.proposals)
        self.view.board_mm = (self.length_mm, self.width_mm)
        self.view.selection_changed.connect(self._select_row)
        self.view.box_added.connect(self._add_box)
        self.view.box_edited.connect(self._box_edited)

        tr = self._tr

        # ── Barra di icone in alto (utente, 2026-09-24): strumenti, zoom,
        # fotografia, e a destra le istruzioni sotto l'icona «i». I nomi e le
        # scorciatoie stanno nei tooltip, tradotti.
        self.btn_select = tool_button(
            "tool-select.svg", "Seleziona e sposta", "V",
            "Clic su un riquadro per sceglierlo, trascinalo per spostarlo. Clic nel "
            "vuoto: toglie la scelta. Trascinamento nel vuoto: sposta la vista.",
            checkable=True, tr=tr)
        self.btn_draw = tool_button(
            "tool-draw.svg", "Disegna un riquadro nuovo", "R",
            "Trascina sulla foto per aggiungere un corpo che il rilevatore non ha visto.",
            checkable=True, tr=tr)
        self.tool_group = QButtonGroup(self)
        self.tool_group.setExclusive(True)
        self.tool_group.addButton(self.btn_select)
        self.tool_group.addButton(self.btn_draw)
        self.btn_select.setChecked(True)
        self.btn_select.clicked.connect(lambda: self.set_tool(TOOL_SELECT))
        self.btn_draw.clicked.connect(lambda: self.set_tool(TOOL_DRAW))
        self.btn_delete = tool_button(
            "delete.svg", "Elimina il riquadro scelto", "Canc",
            "Toglie la proposta scelta: non e' un componente.", tr=tr)
        self.btn_delete.setEnabled(False)
        self.btn_delete.clicked.connect(self._delete_selected)
        # Tieni: solo per le proposte a bassa confidenza, che partono NON
        # tenute (utente, 26/09). Stessa icona di Seleziona: e' una scelta,
        # non un disegno. Riusa un'icona esistente per non aggiungere asset.
        self.btn_keep = tool_button(
            "tool-select.svg", "Tieni o lascia la proposta a bassa confidenza scelta", "T",
            "Le proposte a bassa confidenza (puntinate) partono escluse: tenute, vanno al "
            "modello come le altre.", checkable=True, tr=tr)
        self.btn_keep.setEnabled(False)
        self.btn_keep.clicked.connect(lambda _checked=False: self.toggle_keep())
        self.btn_zoom_out = tool_button("zoom-out.svg", "Zoom indietro", "-", tr=tr)
        self.btn_zoom_out.clicked.connect(lambda: self.view.zoom_at(1 / BUTTON_ZOOM_STEP))
        self.zoom_label = QLabel()
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.zoom_label.setMinimumWidth(self.zoom_label.fontMetrics().horizontalAdvance("1600%") + 8)
        self.zoom_label.setToolTip(tr("Ingrandimento rispetto alla foto intera"))
        self.btn_zoom_in = tool_button(
            "zoom-in.svg", "Zoom avanti", "+",
            "Anche con la rotella del mouse, attorno al puntatore.", tr=tr)
        self.btn_zoom_in.clicked.connect(lambda: self.view.zoom_at(BUTTON_ZOOM_STEP))
        self.btn_fit = tool_button("view-fit.svg", "Adatta la foto alla finestra", "F", tr=tr)
        self.btn_fit.clicked.connect(lambda: self.view.fit_view())
        # Comando di vista: cambia cio' che si vede, mai cio' che si tiene. Un
        # solo pulsante per tre stati; l'icona disegna lo stato attuale e il
        # tooltip dice il passo successivo (vedi `_apply_photo_state`).
        self.photo_button = tool_button("photo-full.svg", "", checkable=True, tr=tr)
        self.photo_button.clicked.connect(self._photo_button_clicked)
        self.btn_info = tool_button(
            "info.svg", "Come si usa", "",
            "Istruzioni e scorciatoie da tastiera di questa finestra.", tr=tr)
        self.info_popup = InfoPopup(self._info_html(), self)
        self.btn_info.clicked.connect(lambda: self.info_popup.toggle_under(self.btn_info))

        toolbar = QHBoxLayout()
        toolbar.setSpacing(4)
        for widget in (self.btn_select, self.btn_draw, self.btn_delete, self.btn_keep,
                       toolbar_divider(),
                       self.btn_zoom_out, self.zoom_label, self.btn_zoom_in, self.btn_fit,
                       toolbar_divider(), self.photo_button):
            toolbar.addWidget(widget)
        toolbar.addStretch()
        toolbar.addWidget(self.btn_info)

        # ── Colonna a destra: filtro, lista, categoria del riquadro scelto.
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._select_from_list)
        # Filtro per categoria sopra la lista (utente, 2026-09-24): su decine
        # di proposte, cercare «i condensatori» non deve voler dire leggere
        # ogni riga. Nasconde righe della lista E riquadri della foto, e non
        # cambia cio' che la revisione restituisce.
        self.category_filter = QComboBox()
        self.category_filter.setToolTip(
            tr("Filtra per categoria la lista e i riquadri sulla foto."))
        self.category_filter.currentIndexChanged.connect(self._category_filter_changed)
        # Niente piu' menu «Categoria del riquadro scelto» (utente,
        # 2026-09-24): la categoria del rilevatore arriva alla chiamata AI solo
        # come indizio, e il modello rilegge comunque la foto; correggerla qui
        # cambiava poco e il menu, lontano dalla foto, mostrava la categoria
        # dell'ultimo riquadro anche senza una scelta. Il filtro resta.
        side = QVBoxLayout()
        side.addWidget(QLabel(tr("Proposte del rilevatore locale")))
        side.addWidget(self.category_filter)
        side.addWidget(self.list, 1)
        side_widget = QWidget()
        side_widget.setLayout(side)
        side_widget.setFixedWidth(300)

        body = QHBoxLayout()
        body.addWidget(self.view, 1)
        body.addWidget(side_widget)

        # ── Barra di stato: punto sotto il puntatore in mm, conteggi, OK/Annulla.
        self.pointer_label = QLabel()
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        status = QHBoxLayout()
        status.addWidget(self.pointer_label)
        status.addSpacing(18)
        status.addWidget(self.summary, 1)
        status.addWidget(buttons)

        layout = QVBoxLayout(self)
        layout.addLayout(toolbar)
        layout.addLayout(body, 1)
        layout.addLayout(status)

        self.view.view_changed.connect(self._update_zoom_label)
        self.view.pointer_moved.connect(self._update_pointer_label)
        self._bind_shortcuts()
        # Un combo con il fuoco tiene le sue lettere (revisore 1, difetto 4).
        self._combo_keys = give_combos_their_keys(self)
        self.resize(1100, 700)
        self._update_zoom_label()
        self._update_pointer_label(None)
        self._apply_photo_state()
        self._refresh()

    def photo_state(self):
        """@brief Quanto della fotografia la vista sta mostrando.

        @return Uno fra PHOTO_FULL, PHOTO_DIMMED e PHOTO_HIDDEN.
        """
        return self.view.photo_state()

    def set_photo_state(self, state):
        """@brief Mostra la fotografia intera, la attenua, o la toglie.

        @param state Uno fra PHOTO_FULL, PHOTO_DIMMED, PHOTO_HIDDEN.
        @throws ValueError per qualunque altro valore.
        @note Non cambia nient'altro: scelta, spostamento, maniglie, disegno
            di un nuovo riquadro, categoria, lista e `kept()` restano quelli
            di prima.
        """
        self.view.set_photo_state(state)
        self._apply_photo_state()

    def cycle_photo_state(self):
        """@brief Passa allo stato successivo: intera, attenuata, nascosta."""
        self.view.cycle_photo_state()
        self._apply_photo_state()

    def _photo_button_clicked(self, _checked):
        """Cicla al clic; la spunta e' una lettura, non il comando.

        Un pulsante spuntabile si spunta da solo prima che arrivi `clicked`,
        quindi lo stato che riporta non significa nulla per un comando a tre
        stati e viene riscritto da `_apply_photo_state`.
        """
        self.cycle_photo_state()

    def _apply_photo_state(self):
        """Riporta lo stato sull'icona, sul tooltip, sul nome accessibile e sulla spunta.

        L'icona disegna lo stato in cui la vista si trova (foto intera,
        attenuata, nascosta: tre disegni diversi); il tooltip porta come nome
        l'imperativo del passo SUCCESSIVO, che rende prevedibile il ciclo, e
        sotto lo stato attuale. Resta la lezione trovata sul campo: un comando
        che enuncia solo uno stato non si legge come comando. Il nome
        accessibile porta entrambi, perche' un lettore di schermo annuncia il
        pulsante da solo.
        """
        state = self.view.photo_state()
        action, current, asset = PHOTO_STATE_TEXTS[state]
        with QSignalBlocker(self.photo_button):
            self.photo_button.setChecked(state != PHOTO_FULL)
        self.photo_button.setIcon(asset_icon(asset))
        self.photo_button.setProperty("photo_asset", asset)
        detail = self._tr(current) + ". " + self._tr(PHOTO_BUTTON_EXPLANATION)
        set_tool_texts(self.photo_button, self._tr(action), "P", detail)
        self.photo_button.setAccessibleName(
            self._tr("{action} (stato attuale: {state})").format(
                action=self._tr(action), state=self._tr(current)))

    def _tr(self, source):
        """@brief Testo nella lingua dell'interfaccia, o la sorgente italiana."""
        i18n = getattr(self, "i18n", None)
        return i18n.text(source) if i18n is not None else source

    def _info_html(self):
        """@brief Istruzioni del popup «Come si usa», tradotte, in rich text."""
        tr = self._tr
        rows = [
            ("V", tr("Seleziona: clic su un riquadro per sceglierlo, trascinalo per "
                     "spostarlo, maniglie per ridimensionarlo")),
            ("R", tr("Disegna: trascina sulla foto per aggiungere un corpo")),
            (tr("Canc"), tr("Elimina il riquadro scelto")),
            ("T", tr("Tieni o lascia la proposta a bassa confidenza scelta")),
            ("Esc", tr("Toglie la scelta (non chiude la finestra)")),
            (tr("Clic nel vuoto"), tr("Toglie la scelta")),
            (tr("Trascina nel vuoto"), tr("Sposta la vista")),
            (tr("Rotella"), tr("Zoom attorno al puntatore")),
            ("+  -  F", tr("Zoom avanti, indietro, foto intera")),
            ("P", tr("Foto intera, attenuata, nascosta")),
        ]
        table = "".join(
            "<tr><td style='padding:2px 12px 2px 0'><b>" + html.escape(key) + "</b></td>"
            "<td style='padding:2px 0'>" + html.escape(text) + "</td></tr>"
            for key, text in rows)
        intro = html.escape(tr(INFO_INTRO))
        low = html.escape(tr(INFO_LOW_CONFIDENCE))
        return ("<p style='margin:0 0 8px 0'><b>" + html.escape(tr("Come si usa")) + "</b></p>"
                "<p style='margin:0 0 8px 0'>" + intro + "</p>"
                "<p style='margin:0 0 8px 0'>" + low + "</p><table>" + table + "</table>")

    def set_tool(self, tool):
        """@brief Sceglie lo strumento della vista e lo mostra premuto nella barra.

        @param tool TOOL_SELECT o TOOL_DRAW.
        @throws ValueError per qualunque altro valore.
        """
        self.view.set_tool(tool)
        (self.btn_draw if tool == TOOL_DRAW else self.btn_select).setChecked(True)

    def _bind_shortcuts(self):
        """Scorciatoie della finestra: le stesse scritte nei tooltip e nel popup."""
        bindings = (
            ("V", lambda: self.set_tool(TOOL_SELECT)),
            ("R", lambda: self.set_tool(TOOL_DRAW)),
            (QKeySequence(Qt.Key.Key_Delete), self._delete_selected),
            ("T", self.toggle_keep),
            ("F", self.view.fit_view),
            ("+", lambda: self.view.zoom_at(BUTTON_ZOOM_STEP)),
            ("=", lambda: self.view.zoom_at(BUTTON_ZOOM_STEP)),
            ("-", lambda: self.view.zoom_at(1 / BUTTON_ZOOM_STEP)),
            ("P", self.cycle_photo_state),
        )
        self._shortcuts = []
        for key, slot in bindings:
            sequence = key if isinstance(key, QKeySequence) else QKeySequence(key)
            shortcut = QShortcut(sequence, self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(slot)
            self._shortcuts.append(shortcut)

    def escape(self):
        """@brief Esc: annulla il tracciamento, poi lo strumento Disegna, poi la scelta.

        @note Non chiude MAI la finestra: in un QDialog Esc vale Annulla, e chi
            lo premeva per togliere la scelta perdeva tutta la revisione. Si
            chiude con «Annulla» o con la croce della finestra.
        """
        if self.view.cancel_drawing():
            return
        if self.view.tool == TOOL_DRAW:
            self.set_tool(TOOL_SELECT)
            return
        if self.view.selected != -1:
            self._select_row(-1)

    def keyPressEvent(self, event):  # noqa: N802 - Qt override
        """@brief Esc non arriva mai a QDialog, che lo tratterebbe come Annulla."""
        if event.key() == Qt.Key.Key_Escape:
            self.escape()
            event.accept()
            return
        super().keyPressEvent(event)

    def _update_zoom_label(self):
        self.zoom_label.setText(f"{round(self.view.zoom() * 100)}%")

    def _update_pointer_label(self, point):
        """Punto della scheda sotto il puntatore, in mm dal centro (X a destra, Y in basso)."""
        if point is None:
            self.pointer_label.setText("X — · Y —")
            return
        x = (point[0] - 0.5) * self.length_mm
        y = (point[1] - 0.5) * self.width_mm
        self.pointer_label.setText(f"X {x:.2f} mm · Y {y:.2f} mm")

    def _category_filter_changed(self, _index):
        """Nuovo filtro: una proposta scelta fuori categoria perde la scelta.

        Il filtro nasconde anche i riquadri (utente, 2026-09-24): una scelta
        fuori categoria resterebbe l'unica riga e l'unico riquadro estranei.
        """
        wanted = self.category_filter.currentData()
        index = self.view.selected
        if wanted and 0 <= index < len(self.proposals) and                 _filter_class(self.proposals[index]) != wanted:
            self._select_row(-1)
        self._apply_filter()

    def _apply_filter(self):
        """Nasconde righe della lista e riquadri della foto fuori dalla categoria del filtro.

        La proposta scelta resta visibile anche fuori categoria: capita solo
        quando le si cambia la categoria mentre il filtro e' attivo, e ci si
        sta lavorando sopra.
        """
        wanted = self.category_filter.currentData()
        hidden = set()
        for row, proposal in enumerate(self.proposals):
            outside = bool(wanted) and _filter_class(proposal) != wanted
            if outside and row != self.view.selected:
                hidden.add(row)
            self.list.setRowHidden(row, row in hidden)
        self.view.hidden = hidden
        self.view.update()

    def _refresh_filter_choices(self):
        """Le voci del filtro sono le categorie presenti: nessuna tassonomia fissa."""
        classes = {_filter_class(p) for p in self.proposals}
        # I riquadri disegnati a mano non hanno categoria: senza una voce loro,
        # con un filtro attivo sparivano appena deselezionati (audit 25/09).
        present = sorted(classes - {NO_CLASS}) + ([NO_CLASS] if NO_CLASS in classes else [])
        current = self.category_filter.currentData()
        wanted = [None] + present
        existing = [self.category_filter.itemData(i) for i in range(self.category_filter.count())]
        if existing == wanted:
            return
        with QSignalBlocker(self.category_filter):
            self.category_filter.clear()
            self.category_filter.addItem(self._tr("Tutte le categorie"), None)
            for name in present:
                label = self._tr("Senza categoria (aggiunti a mano)") if name == NO_CLASS else name
                self.category_filter.addItem(label, name)
            index = self.category_filter.findData(current) if current in present else 0
            self.category_filter.setCurrentIndex(max(0, index))

    def kept(self):
        """@brief Proposte sopravvissute alla revisione.

        @return Lista di dizionari nello stesso formato del rilevatore.
        @note Le proposte a bassa confidenza che nessuno ha tenuto
            (photo_yolo.awaiting_user_keep) NON sono tenute: si vedono, ma
            sono un'aggiunta da scegliere, non una tara da togliere.
        """
        return [dict(p) for p in self.proposals if not awaiting_user_keep(p)]

    def toggle_keep(self, index=None):
        """@brief Tiene, o lascia di nuovo, una proposta a bassa confidenza.

        @param index Posizione della proposta; None per quella scelta.
        @return True se la proposta e' ora tenuta, False se lasciata, None se
            non e' una proposta a bassa confidenza (nulla cambia).
        @note Tenerla e' la decisione di presenza di chi guarda, come
            ritoccarla (mark_manual): `_yolo_user_kept` diventa True e
            `_yolo_review_only` False. Lasciarla riporta entrambi com'erano.
        """
        index = self.view.selected if index is None else index
        if not 0 <= index < len(self.proposals):
            return None
        proposal = self.proposals[index]
        if not proposal.get("_yolo_low_confidence"):
            return None
        keep = awaiting_user_keep(proposal)
        proposal["_yolo_user_kept"] = keep
        proposal["_yolo_review_only"] = not keep
        self._refresh()
        return keep

    def _label(self, proposal):
        """@brief Testo della voce in lista: categoria, stato e quote in mm.

        @note Le quote ci sono perche' chi sposta o ridimensiona un riquadro
            deve vedere subito che cosa ha cambiato, e perche' un corpo
            ritoccato a mano va riconoscibile come tale.
        """
        origin = proposal.get("_yolo_origin")
        confidence = proposal.get("_yolo_confidence")
        if origin == "user_added":
            state = self._tr("aggiunto a mano")
        else:
            state = f"{confidence:.2f}" if isinstance(confidence, (int, float)) else "?"
            if proposal.get("_yolo_low_confidence"):
                state += " · " + self._tr("bassa confidenza") + " · " + (
                    self._tr("non tenuta") if awaiting_user_keep(proposal)
                    else self._tr("tenuta"))
            elif proposal.get("_yolo_review_only"):
                state += " · " + self._tr("debole")
            if proposal.get("_yolo_manual_box"):
                state += " · " + self._tr("ritoccato")
        text = f"{proposal.get('_yolo_class') or self._tr('generico')} · {state}"
        length, width = proposal.get("length_mm"), proposal.get("width_mm")
        if isinstance(length, (int, float)) and isinstance(width, (int, float)):
            text += f" · {length:.1f}×{width:.1f} mm"
        return text

    def _refresh(self):
        current = self.view.selected
        self.list.blockSignals(True)
        self.list.clear()
        for proposal in self.proposals:
            item = QListWidgetItem(self._label(proposal))
            item.setForeground(QColor(_origin_color(proposal)))
            self.list.addItem(item)
        if 0 <= current < len(self.proposals):
            self.list.setCurrentRow(current)
        self.list.blockSignals(False)
        self._refresh_filter_choices()
        self._apply_filter()
        added = sum(1 for p in self.proposals if p.get("_yolo_origin") == "user_added")
        # Una frase intera con i suoi segnaposto: montata da f-string restava
        # italiana in ogni lingua (utente, 2026-09-24).
        self.summary.setText(self._tr(
            "Il rilevatore ne ha proposte {total} ({strong} sicure, {weak} deboli). "
            "Ne tieni {kept}, più {added} aggiunte a mano.").format(
                total=self._strong + self._weak, strong=self._strong, weak=self._weak,
                kept=len(self.kept()) - added, added=added))
        if self._low:
            waiting = sum(1 for p in self.proposals if awaiting_user_keep(p))
            self.summary.setText(self.summary.text() + " " + self._tr(
                "Di {low} a bassa confidenza ne tieni {low_kept}: le altre non vanno "
                "al modello.").format(
                    low=sum(1 for p in self.proposals if p.get("_yolo_low_confidence")),
                    low_kept=sum(1 for p in self.proposals if p.get("_yolo_low_confidence"))
                    - waiting))
        self.btn_delete.setEnabled(0 <= current < len(self.proposals))
        chosen = self.proposals[current] if 0 <= current < len(self.proposals) else {}
        with QSignalBlocker(self.btn_keep):
            self.btn_keep.setEnabled(bool(chosen.get("_yolo_low_confidence")))
            self.btn_keep.setChecked(bool(chosen.get("_yolo_low_confidence"))
                                     and not awaiting_user_keep(chosen))
        self.view.update()

    def _select_row(self, index):
        self.view.selected = index
        self._refresh()

    def _select_from_list(self, index):
        self._select_row(index)

    def _delete_selected(self):
        """@brief Toglie la proposta scelta: non e' un componente.

        @note Un gesto in corso si abbandona prima (vedi
            `BoardView.cancel_gesture`). La scelta passa al vicino VISIBILE
            nel filtro, o a nessuno: prima passava all'indice successivo
            anche se il filtro lo nascondeva, lo rimostrava, e un secondo Canc
            cancellava un corpo che non si vedeva (revisore 1, difetto 1).
        """
        self.view.cancel_gesture()
        index = self.view.selected
        if 0 <= index < len(self.proposals):
            del self.proposals[index]
            self._select_row(self._nearest_shown(index))

    def _shown_by_filter(self, proposal):
        """Vero quando la proposta rientra nella categoria del filtro (o non c'e' filtro)."""
        wanted = self.category_filter.currentData()
        return not wanted or (proposal.get("_yolo_class") or "") == wanted

    def _nearest_shown(self, index):
        """Indice della proposta visibile piu' vicina a `index`, prima in avanti; -1 se nessuna."""
        for distance in range(len(self.proposals)):
            for candidate in (index + distance, index - distance - 1):
                if 0 <= candidate < len(self.proposals) and \
                        self._shown_by_filter(self.proposals[candidate]):
                    return candidate
        return -1

    def geometry_mm(self, box):
        """@brief Quote in millimetri di un riquadro normalizzato.

        @param box Rettangolo normalizzato (x0, y0, x1, y1) sul ritaglio.
        @return Dizionario con x_mm, y_mm, length_mm, width_mm, rotation_deg.
        @note Stessa convenzione del rilevatore: origine al centro della
            scheda, X a destra, Y in basso, lunghezza = lato lungo, rotazione
            90 quando il lato lungo sta lungo Y. E' l'unica conversione della
            finestra, cosi' un riquadro aggiunto e uno ritoccato non possono
            finire su scale diverse.
        """
        x0, y0, x1, y1 = box
        sx, sy = (x1 - x0) * self.length_mm, (y1 - y0) * self.width_mm
        return {
            "x_mm": ((x0 + x1) / 2 - 0.5) * self.length_mm,
            "y_mm": ((y0 + y1) / 2 - 0.5) * self.width_mm,
            "length_mm": max(sx, sy), "width_mm": min(sx, sy),
            "rotation_deg": 0 if sx >= sy else 90,
        }

    @staticmethod
    def mark_manual(proposal, handle=MOVE_HANDLE):
        """@brief Dichiara che la geometria di `proposal` e' quella vista dall'utente.

        @param proposal Proposta modificata sul posto.
        @param handle Maniglia con cui e' stata modificata: una di HANDLES per
            un ridimensionamento, MOVE_HANDLE (predefinito) per uno
            spostamento. Decide quali lati sono stati guardati.
        @note `_yolo_manual_box` ha il prefisso `_yolo_` apposta: e' cio' che
            viene inoltrato nei `detector_metadata` della richiesta al modello,
            che deve sapere che quel riquadro non l'ha misurato una macchina.
            Le marcature che decadono: `_edges_measured` era la misura dei
            bordi con OpenCV, e ora il bordo e' dove lo ha messo l'occhio.
            `_yolo_clipped_sides` e `_yolo_clipped_recovery` dicevano che
            alcuni lati poggiavano sul confine di una tessera del rilevatore
            e non erano misure: decade SOLO il lato che la maniglia ha
            trascinato, perche' quello e' stato guardato; uno spostamento
            non cambia l'estensione e lascia la lista com'e', e cosi' il
            modello continua a sapere che quel bordo e' un artefatto. La
            ricomposizione (`_yolo_clipped_recovery`) resta dichiarata
            finche' resta almeno un lato tagliato. La provenienza precedente
            resta in `_yolo_source_before_manual`, cosi' si sa se sotto
            c'era una proposta forte, debole o ricomposta.
        @note Decisione del revisore: la mano di chi guarda E' la decisione
            di presenza. Una proposta debole (`_yolo_review_only`) che
            l'utente ha spostato o ridimensionato non e' piu' un'ipotesi a
            bassa confidenza da verificare -- l'ha verificata l'occhio --
            percio' `_yolo_review_only` diventa False, e a valle nessuno la
            tratta piu' da dubbio. L'origine di prima resta in
            `_yolo_origin_before_manual` (solo se c'era), per la
            tracciabilita'.
        """
        if not proposal.get("_yolo_manual_box"):
            proposal["_yolo_source_before_manual"] = proposal.get("_source")
        proposal["_yolo_manual_box"] = True
        proposal["_source"] = MANUAL_SOURCE
        proposal.pop("_edges_measured", None)
        hx, hy = handle
        looked_at = set()
        if hx < 0:
            looked_at.add("left")
        elif hx > 0:
            looked_at.add("right")
        if hy < 0:
            looked_at.add("top")
        elif hy > 0:
            looked_at.add("bottom")
        clipped = [side for side in (proposal.get("_yolo_clipped_sides") or ())
                   if side not in looked_at]
        proposal["_yolo_clipped_sides"] = clipped
        if not clipped:
            proposal["_yolo_clipped_recovery"] = False
        if proposal.get("_yolo_review_only"):
            if "_yolo_origin" in proposal and "_yolo_origin_before_manual" not in proposal:
                proposal["_yolo_origin_before_manual"] = proposal["_yolo_origin"]
            proposal["_yolo_review_only"] = False
        # Stessa regola per le proposte a bassa confidenza: ritoccarla e'
        # guardarla, quindi e' tenuta (photo_yolo.awaiting_user_keep).
        if proposal.get("_yolo_low_confidence"):
            proposal["_yolo_user_kept"] = True

    def _box_edited(self, index, handle):
        """@brief Un riquadro esistente e' stato spostato o ridimensionato.

        @param index Posizione della proposta nella lista in revisione.
        @param handle Maniglia usata, di HANDLES o MOVE_HANDLE.
        """
        if not 0 <= index < len(self.proposals):
            return
        proposal = self.proposals[index]
        proposal.update(self.geometry_mm(proposal["_box"]))
        self.mark_manual(proposal, tuple(handle))
        self._refresh()

    def _add_box(self, box):
        """@brief Aggiunge un corpo che il rilevatore non ha visto.

        @param box Rettangolo normalizzato (x0, y0, x1, y1).
        @note Le quote in millimetri si ricavano dalla calibrazione della
            scheda, esattamente come fa il rilevatore: un riquadro aggiunto a
            mano deve essere indistinguibile dagli altri per chi sta a valle.
        """
        x0, y0, x1, y1 = box
        identifiers = [p.get("_id") for p in self.proposals if isinstance(p.get("_id"), int)]
        identifier = max(identifiers, default=0) + 1
        self.proposals.append({
            "name": f"Corpo aggiunto {identifier}", "kind": "generic",
            **self.geometry_mm(box), "height_mm": 1.0,
            "_height_source": "estimated_placeholder_not_measured", "_height_estimated": True,
            "color_hex": "#303030", "marking": "", "confidence": "low",
            "_box": (x0, y0, x1, y1), "_source": MANUAL_SOURCE,
            "_uncertain": True, "_id": identifier,
            # Nessuna categoria: la decide il modello guardando la foto. Prima
            # si ereditava quella dell'ultimo riquadro scelto nel menu tolto.
            "_yolo_class": "",
            "_yolo_confidence": None, "_yolo_proposal_id": None,
            "_yolo_class_ambiguous": False,
            "_yolo_origin": "user_added", "_yolo_review_only": False,
            # Tracciato a mano: per il modello a valle vale quanto un
            # riquadro ritoccato, e lo dichiara con la stessa chiave.
            "_yolo_manual_box": True,
        })
        self.view.selected = len(self.proposals) - 1
        self._select_row(self.view.selected)
