"""@brief Capture the README screenshots from the real application.

Usage::

    python tools/make_readme_screenshots.py <project folder> [--lang en|it] [--out docs/images] [--without-drawings]

The window is built in an isolated data folder (a temporary
``KICADGEN_DATA_DIR`` with its own configuration, profile database and
workspace), so the captures never show the developer's recent projects,
paths, keys or balances, and nothing is written to the developer's data. The
given project is **copied** into that workspace and opened there: the original
is never modified. No AI call is made; the 3D tab shows the STEP model
already generated in the project.

@note The window is shown on screen for a few seconds: the VTK 3D view only
    renders into a visible window. The files written are
    ``<out>/<lang>-<screen>.png``.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]

#: Window size of every capture, in logical pixels: wide enough for the
#: tab bar and the side panels, small enough to read in a README.
WINDOW_SIZE = (1440, 900)

#: Milliseconds given to Qt and VTK to lay out and render a tab before grabbing it.
SETTLE_MS = 1500

#: Workspace path shown on the start page: the installed program's default,
#: instead of the temporary folder that names the developer's account.
DISPLAYED_WORKSPACE = r"C:\Users\<user>\Documents\KiCadGen"


def _isolate(data_dir: Path, language: str) -> None:
    """Point every user-data path of the program at ``data_dir``.

    Must run before importing ``kicadgen``: the workspace root is read at
    import time.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "config.json").write_text('{"language": "%s"}' % language, encoding="utf-8")
    shutil.copy2(ROOT / "data" / "analysis_profiles.sqlite3", data_dir / "analysis_profiles.sqlite3")
    os.environ["KICADGEN_DATA_DIR"] = str(data_dir)
    os.environ["KICADGEN_CONFIG_PATH"] = str(data_dir / "config.json")
    os.environ["KICADGEN_PROFILE_DB"] = str(data_dir / "analysis_profiles.sqlite3")
    os.environ["KICADGEN_MATCH_DB"] = str(data_dir / "accepted_components.sqlite3")
    # No AI key from the environment can reach the window.
    for name in ("OPENROUTER_API_KEY", "DEEPSEEK_API_KEY"):
        os.environ.pop(name, None)


def _settle(app, milliseconds: int = SETTLE_MS) -> None:
    """Run the event loop for ``milliseconds`` so pending layout and paint finish."""
    from PyQt6.QtCore import QEventLoop, QTimer

    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()
    app.processEvents()


def _grab(window, path: Path) -> None:
    """Save what the screen shows of ``window`` (VTK included) as a PNG."""
    screen = window.screen()
    pixmap = screen.grabWindow(int(window.winId()))
    if pixmap.isNull():
        pixmap = window.grab()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not pixmap.save(str(path), "PNG"):
        raise OSError(f"cannot write {path}")
    print(f"wrote {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")


def _clear_manual_drawings(project: Path) -> None:
    """Remove the hand-drawn footprint and symbol graphics from a project copy.

    Only the saved editor state is changed (``project_config.json``): the
    previews draw from it. Pins, pads and generated geometry are untouched.
    """
    path = project / "project_config.json"
    if not path.is_file():
        return
    config = json.loads(path.read_text(encoding="utf-8"))
    state = config.get("state", {})
    if isinstance(state, dict):
        state["footprint_drawings"] = []
        for pin in state.get("pinout") or []:
            if isinstance(pin, dict) and "symbol_graphics" in pin:
                pin["symbol_graphics"] = []
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def capture(project: Path, language: str, out_dir: Path, without_drawings: bool = False) -> None:
    """@brief Open a copy of ``project`` and capture the start page and every tab.

    @param project Generated project folder (footprint, symbol, STEP model).
    @param language Interface language, ``en`` or ``it``.
    @param out_dir Folder that receives ``<language>-<screen>.png``.
    @param without_drawings Remove the hand-drawn footprint and symbol graphics
        from the copy before opening it (test scribbles left in a project).
    @throws FileNotFoundError When ``project`` is not a folder.
    """
    if not project.is_dir():
        raise FileNotFoundError(project)
    with tempfile.TemporaryDirectory(prefix="kicadgen-shots-") as temp:
        data_dir = Path(temp)
        _isolate(data_dir, language)
        sys.path.insert(0, str(ROOT))
        from PyQt6.QtGui import QIcon
        from PyQt6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance() or QApplication(sys.argv)
        from kicadgen.gui import main_window as gui

        app.setWindowIcon(QIcon(str(ROOT / "kicadgen" / "gui" / "assets" / "app-icon.svg")))
        app.setStyleSheet(gui.APP_QSS)
        # Any modal box would block an unattended run; none is expected here.
        QMessageBox.exec = lambda self: QMessageBox.StandardButton.Ok

        window = gui.MainWindow()
        window.resize(*WINDOW_SIZE)
        window.show()
        _settle(app)
        # Display only: the real workspace is the temporary folder, whose path
        # names the developer's account; show the installed program's default.
        window.start_page.set_library_root(DISPLAYED_WORKSPACE)
        _settle(app, 300)
        _grab(window, out_dir / f"{language}-start.png")

        workspace = gui.DEFAULT_LIBRARY_ROOT
        workspace.mkdir(parents=True, exist_ok=True)
        copy = workspace / project.name
        shutil.copytree(project, copy)
        if without_drawings:
            _clear_manual_drawings(copy)
        window._open_project_at(copy, save_current=False)
        _settle(app, 3000)

        screens = (
            (gui.TAB_PROJECT, "project"),
            (gui.TAB_PREVIEW, "footprint"),
            (gui.TAB_SYMBOL, "symbol"),
            (gui.TAB_MODEL_3D, "step-model"),
            (gui.TAB_KICAD, "kicad"),
        )
        for index, name in screens:
            window.tabs.setCurrentIndex(index)
            # The 3D viewer is created lazily on first entry and loads the mesh
            # in the background: give it longer.
            _settle(app, 8000 if index == gui.TAB_MODEL_3D else SETTLE_MS)
            _grab(window, out_dir / f"{language}-{name}.png")

        window.close()
        _settle(app, 500)


def main(argv: list[str]) -> int:
    """@brief Command-line entry point.

    @param argv Arguments after the program name.
    @return 0 on success.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0].replace("@brief ", ""))
    parser.add_argument("project", type=Path, help="generated project folder to show")
    parser.add_argument("--lang", default="en", choices=("en", "it"), help="interface language")
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "images", help="output folder")
    parser.add_argument("--without-drawings", action="store_true",
                        help="hide hand-drawn footprint/symbol graphics (in the copy only)")
    args = parser.parse_args(argv)
    capture(args.project.resolve(), args.lang, args.out.resolve(), args.without_drawings)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
