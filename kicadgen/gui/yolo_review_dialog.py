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
from PyQt6.QtCore import QPoint, QRect, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QHBoxLayout,
                             QLabel, QListWidget, QListWidgetItem, QPushButton,
                             QVBoxLayout, QWidget)

#: Sotto questa frazione del lato il rettangolo tracciato e' un clic andato
#: storto, non un corpo: un componente grande come un pixel non esiste.
MIN_NEW_BOX = 0.004

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

    def __init__(self, pixmap, proposals, parent=None):
        super().__init__(parent)
        self.pixmap = pixmap
        self.proposals = proposals
        self.selected = -1
        self._drag_from = None
        self._drag_to = None
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

    def paintEvent(self, event):
        """@brief Disegna la foto e sopra i riquadri ancora in gioco."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        frame = self._frame()
        if not self.pixmap.isNull():
            painter.drawPixmap(frame, self.pixmap)
        for index, proposal in enumerate(self.proposals):
            painter.setPen(box_pen(proposal, index == self.selected))
            painter.drawRect(self.to_widget(proposal["_box"]))
        if self._drag_from and self._drag_to:
            painter.setPen(QPen(QColor(ADDED_COLOR), 1.6, Qt.PenStyle.DotLine))
            painter.drawRect(QRectF(self._drag_from, self._drag_to).normalized())

    def mousePressEvent(self, event):
        """@brief Un clic sceglie il riquadro sotto il puntatore, o inizia un nuovo."""
        position = event.position()
        for index in range(len(self.proposals) - 1, -1, -1):
            if self.to_widget(self.proposals[index]["_box"]).contains(position):
                self.selected = index
                self.selection_changed.emit(index)
                self.update()
                return
        self._drag_from, self._drag_to = position, position

    def mouseMoveEvent(self, event):
        if self._drag_from is not None:
            self._drag_to = event.position()
            self.update()

    def mouseReleaseEvent(self, event):
        """@brief Il rilascio conferma il rettangolo tracciato, se ha un'area."""
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
            "rilevatore non ha visto. I tratteggiati sono le proposte su cui non "
            "si e' fidato: ci sono perche' una confidenza bassa non e' prova di "
            "assenza."))
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
        origin = proposal.get("_yolo_origin")
        confidence = proposal.get("_yolo_confidence")
        if origin == "user_added":
            state = "aggiunto a mano"
        else:
            state = f"{confidence:.2f}" if isinstance(confidence, (int, float)) else "?"
            if proposal.get("_yolo_review_only"):
                state += " · debole"
        return f"{proposal.get('_yolo_class') or 'generico'} · {state}"

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

    def _add_box(self, box):
        """@brief Aggiunge un corpo che il rilevatore non ha visto.

        @param box Rettangolo normalizzato (x0, y0, x1, y1).
        @note Le quote in millimetri si ricavano dalla calibrazione della
            scheda, esattamente come fa il rilevatore: un riquadro aggiunto a
            mano deve essere indistinguibile dagli altri per chi sta a valle.
        """
        x0, y0, x1, y1 = box
        sx, sy = (x1 - x0) * self.length_mm, (y1 - y0) * self.width_mm
        identifiers = [p.get("_id") for p in self.proposals if isinstance(p.get("_id"), int)]
        identifier = max(identifiers, default=0) + 1
        self.proposals.append({
            "name": f"Corpo aggiunto {identifier}", "kind": "generic",
            "x_mm": ((x0 + x1) / 2 - 0.5) * self.length_mm,
            "y_mm": ((y0 + y1) / 2 - 0.5) * self.width_mm,
            "length_mm": max(sx, sy), "width_mm": min(sx, sy),
            "rotation_deg": 0 if sx >= sy else 90, "height_mm": 1.0,
            "_height_source": "estimated_placeholder_not_measured", "_height_estimated": True,
            "color_hex": "#303030", "marking": "", "confidence": "low",
            "_box": (x0, y0, x1, y1), "_source": "revisione manuale",
            "_uncertain": True, "_id": identifier,
            "_yolo_class": self.combo_class.currentText() or (self.classes[0] if self.classes else ""),
            "_yolo_confidence": None, "_yolo_proposal_id": None,
            "_yolo_class_ambiguous": False,
            "_yolo_origin": "user_added", "_yolo_review_only": False,
        })
        self.view.selected = len(self.proposals) - 1
        self._select_row(self.view.selected)
