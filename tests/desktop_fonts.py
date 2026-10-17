"""@brief Desktop fonts in tests: registered only in a dedicated child pytest process.

Registering a font file with Qt is not undone by removing it: under the
offscreen platform the application's default font then resolves to the
registered family for the rest of the process (measured 2026-10-16: 12 px
high before, 15-16 px after, still after removal), and a later layout test
measured its card 1 px taller. So the shared test session never registers a
font (the `conftest.no_application_font_in_the_shared_session` guard fails a
test that tries), and the checks that need Segoe UI or Courier New rerun in a
child process started by `run_with_desktop_fonts`.
"""
import os
import subprocess
import sys
from pathlib import Path

#: Environment flag set only in the child process that checks the desktop fonts.
DESKTOP_FONTS_ENV = "KICADGEN_TEST_DESKTOP_FONTS"

#: Folder of the Windows desktop fonts.
FONTS_DIR = Path("C:/Windows/Fonts")


def in_desktop_font_process() -> bool:
    """@brief Whether this process is the desktop-font child.

    @return True only in the child started by `run_with_desktop_fonts`.
    """
    return os.environ.get(DESKTOP_FONTS_ENV) == "1"


def desktop_font(file_name: str, family: str):
    """@brief A Windows desktop font registered with Qt, in the desktop-font child only.

    @param file_name Font file inside `FONTS_DIR`, e.g. ``segoeui.ttf``.
    @param family Family name the file registers.
    @return The QFont, or None where the file is not installed.
    @throws RuntimeError Outside the desktop-font child: the registration
        would leak into every later test of the shared session.
    """
    from PyQt6.QtGui import QFont, QFontDatabase
    if not in_desktop_font_process():
        raise RuntimeError("desktop fonts are registered only in the desktop-font child process")
    path = FONTS_DIR / file_name
    if not path.exists():
        return None
    QFontDatabase.addApplicationFont(str(path))
    return QFont(family)


def run_with_desktop_fonts(tmp_path: Path, target: str) -> None:
    """@brief Rerun `target` in a child pytest process where desktop fonts may be registered.

    @param tmp_path Base temporary folder of the calling test.
    @param target pytest selection: a module path, optionally with ``::test``.
    @throws AssertionError When the child run fails; its output tail is the message.
    @note The caller skips itself inside the child (`in_desktop_font_process`).
    """
    env = dict(os.environ, **{DESKTOP_FONTS_ENV: "1", "QT_QPA_PLATFORM": "offscreen"})
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--basetemp",
         str(tmp_path / "child"), target],
        cwd=str(Path(__file__).resolve().parents[1]), env=env, capture_output=True,
        text=True, timeout=600)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
