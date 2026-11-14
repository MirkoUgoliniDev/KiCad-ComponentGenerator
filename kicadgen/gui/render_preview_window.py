"""@brief Non-modal window showing the 3D image of the KiCad preview board.

@details USER DECISION 2026-11-13 (option B): «Prova in KiCad» also renders the
preview board with ``kicad-cli pcb render``
(`kicadgen.services.kicad_library.render_preview_image`) and shows the PNG
here, beside the KiCad editors it opens. The window does not block the main
window: the user can keep working, compare it with the editors and close it
whenever. The image is scaled to the window keeping its proportions; a hint
says that Alt+3 in KiCad's PCB Editor opens the interactive 3D viewer.
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget

#: Italian source of the window title; ``{name}`` is the component name.
TITLE_SOURCE = "Anteprima 3D in KiCad — {name}"

#: Italian source of the hint under the image.
HINT_SOURCE = ("Immagine generata da KiCad. Per ruotare il modello, premi Alt+3 "
               "nel PCB Editor di KiCad.")

#: Italian source of the text shown when the image file cannot be read.
UNREADABLE_SOURCE = "Impossibile leggere l'immagine: {path}"


class RenderPreviewWindow(QWidget):
    """@brief Top-level window with one rendered PNG, scaled to fit.

    @note The texts are Italian sources translated by the ``translate``
        callable (the main window's ``i18n.text``); `retranslate` redraws them
        after a language change. The window property ``i18n_skip`` keeps the
        main window's generic translation pass off the already translated
        texts.
    """

    def __init__(self, image_path: str | Path, component_name: str, translate,
                 parent: QWidget | None = None):
        """@brief Build the window; call show() to display it.

        @param image_path PNG written by ``render_preview_image``.
        @param component_name Name shown in the title.
        @param translate Callable mapping an Italian source text to the
            interface language.
        @param parent Owner window: the image window closes with it and stays
            in front of it, but does not block it.
        """
        super().__init__(parent, Qt.WindowType.Window)
        self.setObjectName("renderPreviewWindow")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setProperty("i18n_skip", True)
        self._translate = translate
        self._name = str(component_name)
        self._path = Path(image_path)
        self._pixmap = QPixmap(str(self._path))
        column = QVBoxLayout(self)
        self.image_label = QLabel()
        self.image_label.setObjectName("renderPreviewImage")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Ignored size policy: the label follows the window instead of
        # forcing it to the pixmap's size, so the window can shrink.
        self.image_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.image_label.setMinimumSize(200, 150)
        column.addWidget(self.image_label, 1)
        self.hint_label = QLabel()
        self.hint_label.setObjectName("renderPreviewHint")
        self.hint_label.setWordWrap(True)
        column.addWidget(self.hint_label)
        self.resize(800, 640)
        self.retranslate()

    @property
    def image_path(self) -> Path:
        """@brief The PNG this window shows.

        @return The path given at construction.
        """
        return self._path

    def has_image(self) -> bool:
        """@brief Whether the PNG could be read.

        @return False when the file is missing or not an image; the window
            then shows a message instead.
        """
        return not self._pixmap.isNull()

    def retranslate(self) -> None:
        """@brief Redraw the title, the hint and a missing-image message in the current language."""
        self.setWindowTitle(self._translate(TITLE_SOURCE).replace("{name}", self._name))
        self.hint_label.setText(self._translate(HINT_SOURCE))
        if not self.has_image():
            self.image_label.setText(
                self._translate(UNREADABLE_SOURCE).replace("{path}", str(self._path)))
        self._fit_image()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        """@brief Qt override: rescale the image to the new window size.

        @param event The QResizeEvent.
        """
        super().resizeEvent(event)
        self._fit_image()

    def _fit_image(self) -> None:
        """Scale the PNG to the label keeping its proportions (smooth, never upscaled past 1:1)."""
        if not self.has_image():
            return
        size = self.image_label.size()
        width = min(size.width(), self._pixmap.width())
        height = min(size.height(), self._pixmap.height())
        if width <= 0 or height <= 0:
            return
        self.image_label.setPixmap(self._pixmap.scaled(
            width, height, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))


def show_render_preview(image_path: str | Path, component_name: str, translate,
                        parent: QWidget | None = None) -> RenderPreviewWindow:
    """@brief Build and show the image window without blocking.

    @param image_path PNG to show.
    @param component_name Name shown in the title.
    @param translate Callable mapping an Italian source to the interface language.
    @param parent Owner window.
    @return The window, already shown; the caller keeps a reference to
        retranslate it, Qt deletes it when it is closed (WA_DeleteOnClose).
    """
    window = RenderPreviewWindow(image_path, component_name, translate, parent)
    window.show()
    window.raise_()
    return window
