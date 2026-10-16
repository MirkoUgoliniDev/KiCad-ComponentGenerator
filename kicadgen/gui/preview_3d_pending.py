"""@brief Stand-in for the 3D viewer until its libraries are imported.

The VTK viewer (`preview_3d_vtk.Preview3DVtk`) drags pyvista, VTK and
matplotlib into the process: about six seconds of the program's start-up on
the user's machine, paid before any window appeared (user, 2026-10-12).
The main window now starts with this placeholder in the 3D tab and builds
the real viewer on the first entry into that tab, or earlier from a
background preload (`MainWindow._ensure_preview_3d`).

@note The placeholder accepts every call the window makes on the viewer and
    does nothing: the window keeps the state the viewer would show (last
    mesh path, parameters, pinout, pin-number switch) and redraws it from
    that state whenever the 3D tab is entered, so nothing sent here is lost.
    Attributes the window probes with `hasattr`/`getattr` (`is_showing`,
    `prepare_mesh`, `mesh_prepared`, `set_view_cube_labels`,
    `highlight_format_rows`, ...) are deliberately absent: the window then
    takes its synchronous or no-op path, as it does with older viewers.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget


class Preview3DPending(QWidget):
    """@brief Empty 3D pane with the viewer's write-only API, shown until the viewer loads."""

    def __init__(self, parent: QWidget | None = None):
        """@brief Build the pane: a dim centred note, nothing else.
        @param parent Optional Qt parent.
        """
        super().__init__(parent)
        # The VTK viewer that will take this place is a native window; a
        # native child makes every ancestor native, and when that happens
        # to a window already on screen Qt re-creates the ancestors' windows:
        # the main window blinked off and on at the swap (user, 2026-10-12).
        # Native from the start, the placeholder makes the ancestor chain
        # native before the window is shown, so the swap re-creates nothing.
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        #: Mirrors `Preview3DVtk.show_pin_numbers`; copied to the real viewer.
        self.show_pin_numbers = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.note = QLabel("Visualizzatore 3D non ancora caricato.")
        self.note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.note.setEnabled(False)
        layout.addWidget(self.note)

    def clear_preview(self) -> None:
        """@brief No scene to clear."""

    def render_params(self, *args) -> None:
        """@brief Nothing drawn: the window redraws from its parameters once the viewer exists.

        @param args Whatever the window passes to the real viewer; ignored.
        """

    def render_mesh(self, path) -> None:
        """@brief Nothing drawn: the window keeps `_last_mesh_path` and redraws later.

        @param path Mesh file the real viewer would load; ignored.
        """

    def set_pin_numbers(self, enabled: bool) -> None:
        """@brief Remember the switch; the real viewer receives it on installation.

        @param enabled True to show pin numbers once the viewer exists.
        """
        self.show_pin_numbers = bool(enabled)

    def set_pin_functions(self, pinout) -> None:
        """@brief Nothing to label: the window re-sends the pinout before each draw.

        @param pinout Pin rows the real viewer would label; ignored.
        """

    def fit_view(self) -> None:
        """@brief No camera yet."""
