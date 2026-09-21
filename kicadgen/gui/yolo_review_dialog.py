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
from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QHBoxLayout,
                             QLabel, QListWidget, QListWidgetItem, QPushButton,
                             QVBoxLayout, QWidget)

#: Sotto questa frazione del lato il rettangolo tracciato e' un clic andato
#: storto, non un corpo: un componente grande come un pixel non esiste.
MIN_NEW_BOX = 0.004

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


def _origin_color(proposal):
    """@brief Colore di un riquadro secondo la sua provenienza."""
    if proposal.get("_yolo_origin") == "user_added":
        return ADDED_COLOR
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
    if proposal.get("_yolo_review_only"):
        pen.setStyle(Qt.PenStyle.DashLine)
    return pen


class BoardView(QWidget):
    """@brief Fotografia della scheda con sopra i riquadri proposti.

    @note Lavora in coordinate normalizzate [0, 1], come `_box` del
        rilevatore: cosi' la vista puo' essere ridimensionata a piacere senza
        che le proposte si spostino di un pixel.
    """
    selection_changed = pyqtSignal(int)
    box_added = pyqtSignal(tuple)
    #: Indice della proposta il cui `_box` e' stato spostato o ridimensionato
    #: a mano. Viene emesso solo al rilascio e solo se la geometria e'
    #: davvero cambiata: un clic fermo su un riquadro non e' una modifica.
    box_edited = pyqtSignal(int)

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
        self.setMinimumSize(480, 320)
        self.setMouseTracking(True)

    def _frame(self):
        """@brief Rettangolo, in pixel, in cui la foto e' disegnata."""
        if self.pixmap.isNull():
            return QRect(0, 0, max(1, self.width()), max(1, self.height()))
        scaled = self.pixmap.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        return QRect(QPoint((self.width() - scaled.width()) // 2,
                            (self.height() - scaled.height()) // 2), scaled)

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
        """
        rect = self._selected_rect()
        if rect is None:
            return None
        for handle in HANDLES:
            center = self._handle_center(rect, handle)
            if (abs(position.x() - center.x()) <= HANDLE_RADIUS_PX
                    and abs(position.y() - center.y()) <= HANDLE_RADIUS_PX):
                return handle
        return MOVE_HANDLE if rect.contains(position) else None

    def _edited_rect(self, origin, handle, delta):
        """@brief Rettangolo che risulta trascinando `handle` di `delta` pixel.

        @param origin Rettangolo di partenza, in pixel.
        @param handle Maniglia di HANDLES o MOVE_HANDLE.
        @param delta Spostamento del puntatore dalla pressione.
        @return Nuovo rettangolo, sempre dentro la foto e mai piu' stretto
            di MIN_BOX_PX: il lato trascinato si ferma contro l'opposto, non
            lo scavalca, cosi' un riquadro non puo' rovesciarsi ne' sparire.
        """
        frame = QRectF(self._frame())
        hx, hy = handle
        rect = QRectF(origin)
        if handle == MOVE_HANDLE:
            dx = min(max(delta.x(), frame.left() - rect.left()), frame.right() - rect.right())
            dy = min(max(delta.y(), frame.top() - rect.top()), frame.bottom() - rect.bottom())
            return rect.translated(dx, dy)
        if hx < 0:
            rect.setLeft(min(max(frame.left(), rect.left() + delta.x()), rect.right() - MIN_BOX_PX))
        elif hx > 0:
            rect.setRight(max(min(frame.right(), rect.right() + delta.x()), rect.left() + MIN_BOX_PX))
        if hy < 0:
            rect.setTop(min(max(frame.top(), rect.top() + delta.y()), rect.bottom() - MIN_BOX_PX))
        elif hy > 0:
            rect.setBottom(max(min(frame.bottom(), rect.bottom() + delta.y()), rect.top() + MIN_BOX_PX))
        return rect

    def paintEvent(self, event):
        """@brief Disegna la foto, i riquadri in gioco e le maniglie di quello scelto."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        frame = self._frame()
        if not self.pixmap.isNull():
            painter.drawPixmap(frame, self.pixmap)
        for index, proposal in enumerate(self.proposals):
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
        """@brief Pressione: prende una maniglia, sceglie un riquadro o ne inizia uno nuovo.

        @note Le maniglie del riquadro scelto vincono sull'interno di un
            altro riquadro che le copra: chi ha scelto un corpo sta lavorando
            su quello. Scegliere un riquadro avvia anche lo spostamento, ma
            un clic fermo non cambia nulla (vedi mouseReleaseEvent).
        """
        if event.button() != Qt.MouseButton.LeftButton:
            return
        position = event.position()
        handle = self.handle_at(position)
        if handle is None:
            for index in range(len(self.proposals) - 1, -1, -1):
                if self.to_widget(self.proposals[index]["_box"]).contains(position):
                    self.selected = index
                    self.selection_changed.emit(index)
                    handle = MOVE_HANDLE
                    break
        if handle is not None:
            self._edit = (self.selected, handle,
                          tuple(self.proposals[self.selected]["_box"]), position)
            self.setCursor(HANDLE_CURSORS[handle])
            self.update()
            return
        self._drag_from, self._drag_to = position, position

    def mouseMoveEvent(self, event):
        """@brief Trascina cio' che e' stato preso, o mostra cosa si prenderebbe."""
        position = event.position()
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
        handle = self.handle_at(position)
        self.setCursor(HANDLE_CURSORS[handle] if handle is not None
                       else Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, event):
        """@brief Il rilascio conferma la modifica, o il rettangolo tracciato se ha un'area."""
        if self._edit is not None:
            index, _handle, origin, _start = self._edit
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
                self.box_edited.emit(index)
            return
        if self._drag_from is None:
            return
        start, end = self._drag_from, event.position()
        self._drag_from = self._drag_to = None
        self.update()
        x0, y0 = self.to_normalized(start)
        x1, y1 = self.to_normalized(end)
        box = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        if box[2] - box[0] >= MIN_NEW_BOX and box[3] - box[1] >= MIN_NEW_BOX:
            self.box_added.emit(box)


class YoloReviewDialog(QDialog):
    """@brief Mostra le proposte del rilevatore e restituisce quelle da tenere.

    @param photo_png Fotografia calibrata, gia' codificata in PNG.
    @param proposals Proposte del rilevatore, con `_box` normalizzato.
    @param length_mm Estensione della scheda lungo X, in mm.
    @param width_mm Estensione lungo Y, in mm.
    @param classes Categorie che il rilevatore conosce.
    @note Annullare significa non procedere: chi chiama non deve pagare la
        chiamata di riconciliazione con proposte che non sono state approvate.
    """

    def __init__(self, photo_png, proposals, length_mm, width_mm, classes, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Corpi rilevati: togli quelli che non sono componenti")
        self.setModal(True)
        self.length_mm, self.width_mm = float(length_mm), float(width_mm)
        self.classes = list(classes)
        # Copia: annullare deve lasciare intatto cio' che ha prodotto il
        # rilevatore, e la lista originale appartiene a chi chiama.
        self.proposals = [dict(p) for p in proposals]
        self._strong = sum(1 for p in self.proposals if not p.get("_yolo_review_only"))
        self._weak = len(self.proposals) - self._strong

        pixmap = QPixmap()
        if photo_png:
            image = QImage()
            image.loadFromData(bytes(photo_png))
            pixmap = QPixmap.fromImage(image)
        self.view = BoardView(pixmap, self.proposals)
        self.view.selection_changed.connect(self._select_row)
        self.view.box_added.connect(self._add_box)
        self.view.box_edited.connect(self._box_edited)

        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._select_from_list)
        self.combo_class = QComboBox()
        self.combo_class.addItems(self.classes)
        self.combo_class.setEnabled(False)
        self.combo_class.currentTextChanged.connect(self._change_class)
        self.btn_delete = QPushButton("Elimina il riquadro scelto")
        self.btn_delete.setEnabled(False)
        self.btn_delete.clicked.connect(self._delete_selected)
        self.summary = QLabel()
        self.summary.setWordWrap(True)

        side = QVBoxLayout()
        side.addWidget(QLabel("Proposte del rilevatore locale"))
        side.addWidget(self.list, 1)
        side.addWidget(QLabel("Categoria del riquadro scelto:"))
        side.addWidget(self.combo_class)
        side.addWidget(self.btn_delete)
        side.addWidget(self.summary)
        side_widget = QWidget()
        side_widget.setLayout(side)
        side_widget.setFixedWidth(300)

        body = QHBoxLayout()
        body.addWidget(self.view, 1)
        body.addWidget(side_widget)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Traccia un rettangolo sulla foto per aggiungere un corpo che il "
            "rilevatore non ha visto; trascina l'interno del riquadro scelto per "
            "spostarlo e le sue maniglie per ridimensionarlo. I tratteggiati sono "
            "le proposte su cui non si e' fidato: ci sono perche' una confidenza "
            "bassa non e' prova di assenza."))
        layout.addLayout(body, 1)
        layout.addWidget(buttons)
        self.resize(1100, 700)
        self._refresh()

    def kept(self):
        """@brief Proposte sopravvissute alla revisione.

        @return Lista di dizionari nello stesso formato del rilevatore.
        """
        return [dict(p) for p in self.proposals]

    def _label(self, proposal):
        """@brief Testo della voce in lista: categoria, stato e quote in mm.

        @note Le quote ci sono perche' chi sposta o ridimensiona un riquadro
            deve vedere subito che cosa ha cambiato, e perche' un corpo
            ritoccato a mano va riconoscibile come tale.
        """
        origin = proposal.get("_yolo_origin")
        confidence = proposal.get("_yolo_confidence")
        if origin == "user_added":
            state = "aggiunto a mano"
        else:
            state = f"{confidence:.2f}" if isinstance(confidence, (int, float)) else "?"
            if proposal.get("_yolo_review_only"):
                state += " · debole"
            if proposal.get("_yolo_manual_box"):
                state += " · ritoccato"
        text = f"{proposal.get('_yolo_class') or 'generico'} · {state}"
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
        added = sum(1 for p in self.proposals if p.get("_yolo_origin") == "user_added")
        self.summary.setText(
            f"Il rilevatore ne ha proposte {self._strong + self._weak} "
            f"({self._strong} sicure, {self._weak} deboli). "
            f"Ne tieni {len(self.proposals) - added}, piu' {added} aggiunte a mano.")
        self.combo_class.setEnabled(0 <= current < len(self.proposals))
        self.btn_delete.setEnabled(0 <= current < len(self.proposals))
        self.view.update()

    def _select_row(self, index):
        self.view.selected = index
        if 0 <= index < len(self.proposals):
            self.combo_class.blockSignals(True)
            self.combo_class.setCurrentText(self.proposals[index].get("_yolo_class") or "")
            self.combo_class.blockSignals(False)
        self._refresh()

    def _select_from_list(self, index):
        self._select_row(index)

    def _delete_selected(self):
        """@brief Toglie la proposta scelta: non e' un componente."""
        index = self.view.selected
        if 0 <= index < len(self.proposals):
            del self.proposals[index]
            self.view.selected = min(index, len(self.proposals) - 1)
            self._select_row(self.view.selected)

    def _change_class(self, name):
        """@brief Corregge la categoria proposta dal rilevatore.

        @note La categoria restringe la famiglia di modelli in cui cercare; il
            package lo decide comunque la riconciliazione successiva.
        """
        index = self.view.selected
        if 0 <= index < len(self.proposals) and name:
            self.proposals[index]["_yolo_class"] = name
            self._refresh()

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
    def mark_manual(proposal):
        """@brief Dichiara che la geometria di `proposal` e' quella vista dall'utente.

        @param proposal Proposta modificata sul posto.
        @note `_yolo_manual_box` ha il prefisso `_yolo_` apposta: e' cio' che
            viene inoltrato nei `detector_metadata` della richiesta al modello,
            che deve sapere che quel riquadro non l'ha misurato una macchina.
            Le marcature che decadono: `_edges_measured` era la misura dei
            bordi con OpenCV, e ora il bordo e' dove lo ha messo l'occhio;
            `_yolo_clipped_sides` e `_yolo_clipped_recovery` dicevano che
            alcuni lati poggiavano sul confine di una tessera del rilevatore
            e non erano misure -- se l'utente ha sistemato il riquadro, quei
            lati sono stati guardati e non sono piu' artefatti. La provenienza
            precedente resta in `_yolo_source_before_manual`, cosi' si sa se
            sotto c'era una proposta forte, debole o ricomposta.
        """
        if not proposal.get("_yolo_manual_box"):
            proposal["_yolo_source_before_manual"] = proposal.get("_source")
        proposal["_yolo_manual_box"] = True
        proposal["_source"] = MANUAL_SOURCE
        proposal.pop("_edges_measured", None)
        proposal["_yolo_clipped_sides"] = []
        proposal["_yolo_clipped_recovery"] = False

    def _box_edited(self, index):
        """@brief Un riquadro esistente e' stato spostato o ridimensionato.

        @param index Posizione della proposta nella lista in revisione.
        """
        if not 0 <= index < len(self.proposals):
            return
        proposal = self.proposals[index]
        proposal.update(self.geometry_mm(proposal["_box"]))
        self.mark_manual(proposal)
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
            "_yolo_class": self.combo_class.currentText() or (self.classes[0] if self.classes else ""),
            "_yolo_confidence": None, "_yolo_proposal_id": None,
            "_yolo_class_ambiguous": False,
            "_yolo_origin": "user_added", "_yolo_review_only": False,
            # Tracciato a mano: per il modello a valle vale quanto un
            # riquadro ritoccato, e lo dichiara con la stessa chiave.
            "_yolo_manual_box": True,
        })
        self.view.selected = len(self.proposals) - 1
        self._select_row(self.view.selected)
