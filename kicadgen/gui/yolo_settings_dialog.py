"""@brief «Parametri YOLO»: the local detector's settings, chosen by the user.

User request (2026-09-25): a button next to «Soglia YOLO» opening a modal
with the possible values, filled with plausible defaults. The settings are
the ones Roboflow exposes (confidence, overlap) plus the magnified tiles that
make this detector differ from a single Roboflow pass. They apply to both
«YOLO locale + verifica AI» and «Solo YOLO locale».
"""
from __future__ import annotations

from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QLabel,
    QPushButton, QVBoxLayout, QWidget,
)

from ..services.photo_yolo import YOLO_SETTING_DEFAULTS, yolo_settings

#: Decimals of the three fields: one more than the 0.05 steps, so a value
#: chosen elsewhere (a profile, a pretest) between two steps is shown.
_DECIMALS = 3
#: Smallest change the fields can show (one unit of the last decimal).
_RESOLUTION = 10.0 ** -_DECIMALS
#: Ranges of the fields: exactly what `photo_yolo.yolo_settings` accepts. The
#: confidence interval is OPEN on both ends (`operating_confidence`: above the
#: audit floor 0.05 and below 1), so its field stops one unit inside it.
_CONFIDENCE_RANGE = (0.05 + _RESOLUTION, 1.0 - _RESOLUTION)
_NMS_RANGE = (0.10, 0.95)
_TILE_OVERLAP_RANGE = (0.10, 0.50)


class YoloSettingsDialog(QDialog):
    """@brief Modal editor of the YOLO detector settings.

    The dialog never applies anything by itself: the caller reads `values()`
    after an accepted exec().
    @note OK without touching a field returns that field's value exactly as
        it came in (super review 2026-11-16, R4-3): the fields once had a
        narrower range and fewer decimals than the service accepts, so a
        valid 0.97 or 0.333 came back clamped or rounded and changed the
        detector behind the user's back.
    """

    def __init__(self, values: dict | None = None, parent: QWidget | None = None):
        """@brief Build the form, filled with the current values.

        @param values Current settings (keys of YOLO_SETTING_DEFAULTS); missing
            keys take the defaults, invalid ones raise ValueError.
        @param parent Parent window; its `i18n` translates the texts.
        @throws ValueError When `values` are not valid settings.
        """
        super().__init__(parent)
        self.i18n = getattr(parent, "i18n", None)
        tr = self._tr
        current = yolo_settings(values)
        self.setWindowTitle(tr("Parametri YOLO"))
        self.setModal(True)
        layout = QVBoxLayout(self)
        intro = QLabel(tr("Valgono per «YOLO locale + verifica AI» e per «Solo YOLO locale». "
                          "I valori predefiniti sono quelli usati finora. Il pretest serve a "
                          "sceglierli guardando i riquadri."))
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()

        self.spin_confidence = self._spin(*_CONFIDENCE_RANGE, 0.05)
        self._add(form, tr("Soglia di confidenza:"), self.spin_confidence, tr(
            "Sotto questo valore un riquadro non conta come corpo. Alzala se compaiono "
            "riquadri su scritte, fori o piazzole; abbassala se mancano componenti. "
            "Su Roboflow è «Confidence Threshold»."))
        self.spin_nms = self._spin(*_NMS_RANGE, 0.05)
        self._add(form, tr("Sovrapposizione per i doppioni:"), self.spin_nms, tr(
            "Due riquadri che si sovrappongono almeno così sono lo stesso corpo: resta il più "
            "sicuro. Più basso toglie più doppioni, ma può fondere due componenti vicini. "
            "Su Roboflow è «Overlap Threshold»."))
        self.check_640 = QCheckBox(tr("Tessere da 640 px (ingrandimento forte)"))
        self.check_1280 = QCheckBox(tr("Tessere da 1280 px (ingrandimento medio)"))
        tiles = QWidget()
        tiles_layout = QVBoxLayout(tiles)
        tiles_layout.setContentsMargins(0, 0, 0, 0)
        tiles_layout.addWidget(self.check_640)
        tiles_layout.addWidget(self.check_1280)
        self._add(form, tr("Viste ingrandite:"), tiles, tr(
            "Oltre alla foto intera, il modello guarda riquadri ingranditi per trovare i "
            "passivi piccoli. Senza nessuna delle due fa una sola passata sulla foto intera, "
            "come Roboflow: meno falsi positivi, ma può perdere i componenti piccoli."))
        self.spin_overlap = self._spin(*_TILE_OVERLAP_RANGE, 0.05)
        self._add(form, tr("Sovrapposizione fra tessere:"), self.spin_overlap, tr(
            "Quanto una tessera ripete la precedente: un corpo sul bordo di una tessera deve "
            "stare intero nella successiva. Più alta costa più tempo."))
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        # Qt's standard buttons stay in English without Qt's translations:
        # they are written with the catalogue (user, 2026-09-25: «Cancel»).
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("OK"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("Annulla"))
        self.btn_defaults = QPushButton(tr("Ripristina predefiniti"))
        buttons.addButton(self.btn_defaults, QDialogButtonBox.ButtonRole.ResetRole)
        self.btn_defaults.clicked.connect(lambda: self._show(YOLO_SETTING_DEFAULTS))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        for check in (self.check_640, self.check_1280):
            check.toggled.connect(self._refresh_overlap)
        self._show(current)

    def _tr(self, source):
        """@brief Text in the interface language, or the Italian source."""
        return self.i18n.text(source) if self.i18n is not None else source

    @staticmethod
    def _spin(low, high, step):
        """One decimal field over [low, high] with `_DECIMALS` decimals."""
        spin = QDoubleSpinBox()
        spin.setDecimals(_DECIMALS)
        spin.setRange(low, high)
        spin.setSingleStep(step)
        return spin

    @staticmethod
    def _add(form, label, widget, help_text):
        """One row: the control, then its explanation in small print below it."""
        widget.setToolTip(help_text)
        hint = QLabel(help_text)
        hint.setWordWrap(True)
        hint.setStyleSheet("color: palette(placeholder-text); font-size: 11px;")
        form.addRow(label, widget)
        form.addRow("", hint)

    def _spins(self):
        """The decimal fields with the settings key each one edits."""
        return (("confidence", self.spin_confidence), ("nms_iou", self.spin_nms),
                ("tile_overlap", self.spin_overlap))

    def _show(self, values):
        """Put a complete settings dict into the controls and remember what was shown.

        `values()` gives back the exact incoming value of every field still
        showing what was put here: the field may round it for display.
        """
        for key, spin in self._spins():
            spin.setValue(values[key])
        self.check_640.setChecked(640 in values["tile_sizes"])
        self.check_1280.setChecked(1280 in values["tile_sizes"])
        self._refresh_overlap()
        #: {key: (exact value given, value the field shows for it)}.
        self._shown = {key: (float(values[key]), spin.value()) for key, spin in self._spins()}

    def _refresh_overlap(self, *_):
        """The overlap between tiles means nothing without tiles."""
        self.spin_overlap.setEnabled(self.check_640.isChecked() or self.check_1280.isChecked())

    def values(self) -> dict:
        """@brief The settings shown in the dialog, validated.

        @return A complete dict as returned by photo_yolo.yolo_settings.
        """
        sizes = [size for size, check in ((640, self.check_640), (1280, self.check_1280))
                 if check.isChecked()]
        chosen = {"tile_sizes": sizes}
        for key, spin in self._spins():
            exact, shown = self._shown[key]
            # Untouched field: the incoming value, not its rounded display.
            chosen[key] = exact if spin.value() == shown else round(spin.value(), _DECIMALS)
        return yolo_settings(chosen)
