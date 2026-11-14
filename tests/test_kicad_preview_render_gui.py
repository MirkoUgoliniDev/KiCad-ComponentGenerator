"""@brief «Prova in KiCad» in the window: both editors, the 3D image off the GUI thread (2026-11-13, point 8).

@details The window opens the preview through `launch_preview_editors` and
logs what was opened (or why the schematic was not), then renders the 3D image
in `PreviewRenderWorker` and shows it in the non-modal image window. A result
of an older start or of another project is ignored; without a STEP the reason
is logged and no thread starts. KiCad, kicad-cli and the image window are
faked: the GitHub runner has none of them. Positive and negative cases each.
"""

import threading
import time
from pathlib import Path
from types import SimpleNamespace

from PyQt6.QtWidgets import QApplication

from kicadgen.gui import main_window as gui
from kicadgen.services import kicad_library
from kicadgen.services.kicad_library import KiCadLibraryError, PreviewLaunch, PreviewRender


def _ready_for_preview(window, monkeypatch, tmp_path, launch_result):
    """Make `_test_in_kicad` reach the launch with fakes; return the launches and renders recorded."""
    launched, renders = [], []
    monkeypatch.setattr(window, "_kicad_output_complete", lambda: True)
    monkeypatch.setattr(window, "_refuse_kicad_command", lambda: False)
    monkeypatch.setattr(window, "_confirm_without_step", lambda artifacts: True)
    monkeypatch.setattr(window, "_current_kicad_artifacts",
                        lambda: SimpleNamespace(has_step=True, name="PART"))
    monkeypatch.setattr(gui, "detect_external_applications", lambda: SimpleNamespace(
        pcbnew="pcbnew.exe", kicad_cli="kicad-cli.exe"))
    board = tmp_path / "PART-preview.kicad_pcb"
    monkeypatch.setattr(gui, "create_preview_project",
                        lambda artifacts: SimpleNamespace(board_path=board))

    def fake_launch(preview, pcbnew, *a, **k):
        launched.append((preview.board_path, pcbnew))
        return launch_result
    monkeypatch.setattr(gui, "launch_preview_editors", fake_launch)
    monkeypatch.setattr(window, "_start_preview_render",
                        lambda preview, cli, artifacts: renders.append((preview.board_path, cli)))
    return launched, renders, board


def test_both_editors_are_opened_and_said(window, monkeypatch, tmp_path):
    """@brief Positive: both opened -> one line says so and the render starts with kicad-cli."""
    both = PreviewLaunch(((Path("pcbnew.exe"), Path("b")), (Path("eeschema.exe"), Path("s"))))
    launched, renders, board = _ready_for_preview(window, monkeypatch, tmp_path, both)
    window._test_in_kicad()
    assert launched == [(board, "pcbnew.exe")] and renders == [(board, "kicad-cli.exe")]
    assert "Aperti lo schema e la scheda di anteprima in KiCad." in window._log_plain_text()


def test_a_skipped_schematic_is_logged_with_its_reason(window, monkeypatch, tmp_path):
    """@brief Negative: the schematic not opened -> its reason (by code) and the OS error are logged."""
    skipped = PreviewLaunch(((Path("pcbnew.exe"), Path("b")),), "eeschema_launch_failed", "denied")
    _ready_for_preview(window, monkeypatch, tmp_path, skipped)
    window._test_in_kicad()
    log = window._log_plain_text()
    assert "Impossibile avviare l'Editor di schemi: aperta solo la scheda. (denied)" in log
    assert "Aperti lo schema e la scheda" not in log


def test_the_render_runs_off_the_gui_thread_and_shows_the_window(window, monkeypatch, tmp_path):
    """@brief Positive: the faked kicad-cli render runs in another thread; its image is shown."""
    calls = []

    def fake_render(preview, kicad_cli, *, has_step, **kwargs):
        calls.append((threading.get_ident(), kicad_cli, has_step))
        return PreviewRender(tmp_path / "PART-preview-3d.png")
    monkeypatch.setattr(kicad_library, "render_preview_image", fake_render)
    shown = []
    from kicadgen.gui import render_preview_window

    class WindowStub:
        """Stand-in of the image window."""
        def __init__(self):
            self.destroyed = SimpleNamespace(connect=lambda slot: None)

        def retranslate(self):
            shown.append("retranslated")

        def close(self):
            pass
    monkeypatch.setattr(render_preview_window, "show_render_preview",
                        lambda image, name, translate, parent: shown.append((image, name)) or WindowStub())
    window._start_preview_render(SimpleNamespace(board_path=tmp_path / "b"), "kicad-cli.exe",
                                 SimpleNamespace(has_step=True, name="PART"))
    deadline = time.monotonic() + 10
    while not shown and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)
    assert calls and calls[0][0] != threading.get_ident() and calls[0][1:] == ("kicad-cli.exe", True)
    assert shown[0] == (tmp_path / "PART-preview-3d.png", "PART")
    assert "Creazione dell'immagine 3D…" in window._log_plain_text()
    # A language change retranslates the open image window.
    window.language_combo.setCurrentIndex(window.language_combo.findData("en"))
    window.language_combo.setCurrentIndex(window.language_combo.findData("it"))
    assert "retranslated" in shown


def test_a_stale_result_is_ignored(window, monkeypatch, tmp_path):
    """@brief Negative: an older start or another project never shows its image or logs it."""
    shown = []
    from kicadgen.gui import render_preview_window
    monkeypatch.setattr(render_preview_window, "show_render_preview",
                        lambda *a, **k: shown.append(a))
    window._preview_render_serial = 5
    key = window._preview_render_key()
    before = window._log_plain_text()
    window._on_preview_rendered(PreviewRender(tmp_path / "x.png"), (4, key, "PART"))
    window._on_preview_rendered(PreviewRender(tmp_path / "x.png"), (5, ("other", "B"), "PART"))
    window._on_preview_rendered(PreviewRender(None, "render_failed", "boom"), (4, key, "PART"))
    assert shown == [] and window._log_plain_text() == before


def test_a_current_failure_is_logged_with_its_reason(window):
    """@brief Positive: the current start's failure names its reason (by code) and kicad-cli's detail."""
    window._preview_render_serial = 2
    token = (2, window._preview_render_key(), "PART")
    window._on_preview_rendered(PreviewRender(None, "render_timeout", "60 s"), token)
    assert "Immagine 3D non creata: kicad-cli non ha risposto in tempo. 60 s" in window._log_plain_text()


def test_without_step_the_reason_is_logged_and_no_thread_starts(window, monkeypatch):
    """@brief Negative: no STEP -> «manca il modello STEP» and the worker is never built."""
    def no_worker(*a, **k):
        raise AssertionError("no render thread without a STEP")
    monkeypatch.setattr(gui, "PreviewRenderWorker", no_worker)
    window._start_preview_render(SimpleNamespace(board_path=Path("b")), "kicad-cli.exe",
                                 SimpleNamespace(has_step=False, name="PART"))
    assert "Immagine 3D non creata: manca il modello STEP." in window._log_plain_text()


def test_a_locked_preview_schematic_is_explained(window):
    """@brief Positive: the catalog text names both paths; negative: another code keeps its own text."""
    error = KiCadLibraryError("locked", code="preview_schematic_locked",
                              schematic="C:/p/PART.kicad_sch", path="C:/p/~PART.kicad_sch.lck")
    text = window._kicad_error_text(error)
    assert "C:/p/PART.kicad_sch" in text and "~PART.kicad_sch.lck" in text
    assert "schema di anteprima" in text
    other = window._kicad_error_text(KiCadLibraryError("x", code="launch_failed", error="e"))
    assert "schema di anteprima" not in other
