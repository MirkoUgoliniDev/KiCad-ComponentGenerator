"""@brief Start-up splash: the picture, the animated splash process and its parent-side handle.

The GUI thread of the application is busy for seconds while the main window
is built, so a splash drawn by that same process cannot move: a Qt timer
never gets a tick. The animated splash therefore runs in a second, tiny
process (`run_splash`, started by the launcher with ``--splash``) that only
paints and quits when its standard input closes. The parent side
(`ProcessSplash`) owns the pipe: closing it ends the splash, and so does the
operating system when the parent dies, whatever the reason.

@note This module is imported by the launcher before the heavy packages:
    PyQt6 is imported inside the functions only, and nothing from
    ``kicadgen.gui`` or ``kicadgen.services`` is imported here.
"""
import os
import subprocess
import sys
import threading
from pathlib import Path

#: Splash text per interface language (the launcher must not import the
#: translation catalogue: the splash exists to appear before the heavy imports).
SPLASH_MESSAGES = {"it": "Avvio in corso…", "en": "Starting…", "fr": "Démarrage…", "de": "Wird gestartet…"}
#: Logical size of the splash, pixels: the artwork (`SPLASH_IMAGE`) is
#: scaled to this width; the drawn fallback is painted at this size.
SPLASH_WIDTH = 800
#: Optional artwork, 16:9, no text (the name and the message are painted
#: over it): drop a PNG here to replace the drawn splash (user, 2026-10-12).
SPLASH_IMAGE = ("kicadgen", "gui", "assets", "splash.png")
#: Side of the icon in the drawn splash and its top edge as a fraction of
#: the height: the ring of the animation is drawn around this square.
SPLASH_ICON_SIDE = 180
SPLASH_ICON_TOP = 0.22
#: Seconds after which the splash process quits on its own, pipe or not: a
#: parent stuck before its window must never leave a splash on the desktop.
SPLASH_MAX_SECONDS = 120
#: Seconds the parent waits for the splash process to exit after closing the
#: pipe, before killing it.
SPLASH_EXIT_TIMEOUT_S = 1.0
#: Milliseconds between animation frames (about 60 per second).
SPLASH_FRAME_MS = 16
#: Milliseconds for one turn of the ring and for one step of the dots.
SPLASH_RING_TURN_MS = 1500
SPLASH_DOTS_STEP_MS = 400
#: Milliseconds of the fade-in when the splash appears.
SPLASH_FADE_IN_MS = 220
#: Command-line flag that turns a process into the splash.
SPLASH_FLAG = "--splash"


def splash_pixmap(resource_path, device_pixel_ratio=1.0):
    """@brief The splash picture: the artwork when present, else icon and name on the theme colour.

    @param resource_path Function mapping path parts to a Path (the
        application's `kicadgen.app_paths.resource_path`).
    @param device_pixel_ratio Screen scale; the pixmap is rendered at
        `SPLASH_WIDTH` × ratio so it stays sharp on high-density screens.
    @return A QPixmap with its device pixel ratio set. An unreadable
        artwork falls back to the drawn picture: the splash never fails.
    """
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
    ratio = max(1.0, float(device_pixel_ratio))
    width = int(SPLASH_WIDTH * ratio)
    artwork = resource_path(*SPLASH_IMAGE)
    pixmap = QPixmap(str(artwork)) if artwork.is_file() else QPixmap()
    if not pixmap.isNull():
        pixmap = pixmap.scaledToWidth(width, Qt.TransformationMode.SmoothTransformation)
    else:
        pixmap = QPixmap(width, int(width * 9 / 16))
        pixmap.fill(QColor("#1e1e2e"))
        painter = QPainter(pixmap)
        side = int(SPLASH_ICON_SIDE * ratio)
        icon = QIcon(str(resource_path("kicadgen", "gui", "assets", "app-icon.svg"))).pixmap(side, side)
        painter.drawPixmap((pixmap.width() - icon.width()) // 2, int(pixmap.height() * SPLASH_ICON_TOP), icon)
        painter.end()
    painter = QPainter(pixmap)
    font = QFont(painter.font())
    font.setPixelSize(int(30 * ratio))
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QColor("#cdd6f4"))
    band = pixmap.rect().adjusted(0, int(pixmap.height() * 0.66), 0, -int(pixmap.height() * 0.2))
    painter.drawText(band, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, "KiCadGen")
    painter.end()
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


def splash_has_artwork(resource_path):
    """@brief Tell whether the artwork PNG is used, in which case the icon's place is unknown.

    @param resource_path Function mapping path parts to a Path.
    @return True when `SPLASH_IMAGE` exists and loads as a picture.
    """
    from PyQt6.QtGui import QPixmap
    artwork = resource_path(*SPLASH_IMAGE)
    return artwork.is_file() and not QPixmap(str(artwork)).isNull()


def splash_message(language):
    """@brief The «Starting…» text for a language, English for an unknown one.

    @param language Interface language code.
    @return The message with its trailing ellipsis.
    """
    return SPLASH_MESSAGES.get(language, SPLASH_MESSAGES["en"])


def dots_message(message, step):
    """@brief The message with its trailing ellipsis replaced by 0 to 3 dots.

    @param message Text ending with «…» (or with dots; both are stripped).
    @param step Animation step; the dots are `step % 4`.
    @return The text to paint for this step.
    """
    base = message.rstrip("….")
    return base + "." * (step % 4)


def splash_command(language):
    """@brief The command line that starts the splash process.

    @param language Interface language passed to the child.
    @return A list for Popen: the running executable (the packaged
        KiCadGen.exe, or this interpreter with the launcher script) plus
        ``--splash <language>``.
    @note From source `sys.executable` is already the project interpreter:
        the launcher relaunches itself into the ``.venv`` before the splash,
        so the child never pays the relaunch.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, SPLASH_FLAG, language]
    launcher = Path(__file__).resolve().parent.parent / "main.py"
    return [sys.executable, str(launcher), SPLASH_FLAG, language]


class ProcessSplash:
    """@brief Parent-side handle of the splash process: same calls the GUI makes on a QSplashScreen.

    @note `finish` and `close` are the two calls `kicadgen.gui.main_window.main`
        makes on the splash it receives; both end the process. The pipe is the
        only signal: closing it is enough for the child, and a child that does
        not exit in `SPLASH_EXIT_TIMEOUT_S` is killed so the parent never
        waits on it.
    """

    def __init__(self, process):
        """@brief Wrap an already started splash process.

        @param process The started Popen with a writable `stdin` pipe.
        """
        self.process = process
        self._ended = False

    @classmethod
    def start(cls, language, popen=subprocess.Popen):
        """@brief Start the splash process for a language.

        @param language Interface language code.
        @param popen Process factory (subprocess.Popen; replaceable in tests).
        @return A ProcessSplash.
        @throws OSError When the process cannot be started.
        @note Output goes nowhere: the child has nothing to say, and an
            inherited console (VS Code's task terminal) must not receive Qt
            warnings of a cosmetic process. No console window is created
            for it on Windows.
        """
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        process = popen(splash_command(language), stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=flags, close_fds=True)
        return cls(process)

    def finish(self, window=None):
        """@brief End the splash once the main window is on screen (QSplashScreen's `finish`).

        @param window Ignored; kept for the QSplashScreen signature.
        """
        self.close()

    def close(self):
        """@brief End the splash process: close the pipe, wait briefly, kill if still there.

        @note Safe to call twice and after the child died on its own.
        """
        if self._ended:
            return
        self._ended = True
        process = self.process
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=SPLASH_EXIT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=SPLASH_EXIT_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                pass
        except OSError:
            pass


def _build_splash_widget():
    """Define the splash window class; PyQt6 is imported here, not at module load."""
    from PyQt6.QtCore import Qt, QTimer, QRectF, QElapsedTimer
    from PyQt6.QtGui import QColor, QFont, QPainter, QPen
    from PyQt6.QtWidgets import QWidget

    class SplashWindow(QWidget):
        """@brief Frameless window painting the splash picture with a turning ring and cycling dots."""

        def __init__(self, pixmap, message, ring):
            """@param pixmap Picture (device pixel ratio set).
            @param message «Starting…» text in the user's language.
            @param ring True to draw the ring around the icon (drawn splash only).
            """
            super().__init__(None, Qt.WindowType.SplashScreen | Qt.WindowType.FramelessWindowHint)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
            self._pixmap = pixmap
            self._message = message
            self._ring = ring
            ratio = pixmap.devicePixelRatio() or 1.0
            self._size = (int(pixmap.width() / ratio), int(pixmap.height() / ratio))
            self.setFixedSize(*self._size)
            self._clock = QElapsedTimer()
            self._clock.start()
            self._timer = QTimer(self)
            self._timer.setInterval(SPLASH_FRAME_MS)
            self._timer.timeout.connect(self.update)
            self._timer.start()
            self.setWindowOpacity(0.0)

        def paintEvent(self, event):
            """@brief Paint picture, ring, message; opacity follows the fade-in."""
            elapsed = self._clock.elapsed()
            self.setWindowOpacity(min(1.0, elapsed / SPLASH_FADE_IN_MS))
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.drawPixmap(0, 0, self._pixmap)
            width, height = self._size
            if self._ring:
                centre_x = width / 2
                centre_y = height * SPLASH_ICON_TOP + SPLASH_ICON_SIDE / 2
                radius = SPLASH_ICON_SIDE / 2 + 22
                rect = QRectF(centre_x - radius, centre_y - radius, 2 * radius, 2 * radius)
                track = QPen(QColor(166, 173, 200, 50), 4)
                track.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(track)
                painter.drawEllipse(rect)
                arc = QPen(QColor("#f9b44c"), 4)
                arc.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(arc)
                start = -int((elapsed % SPLASH_RING_TURN_MS) / SPLASH_RING_TURN_MS * 360 * 16) + 90 * 16
                painter.drawArc(rect, start, -100 * 16)
            font = QFont(painter.font())
            font.setPointSize(11)
            painter.setFont(font)
            painter.setPen(QColor("#a6adc8"))
            band = self.rect().adjusted(0, 0, 0, -int(height * 0.06))
            text = dots_message(self._message, int(elapsed / SPLASH_DOTS_STEP_MS))
            painter.drawText(band, Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter, text)
            painter.end()

    return SplashWindow


def run_splash(language, resource_path=None, stdin=None, max_seconds=SPLASH_MAX_SECONDS):
    """@brief Entry point of the splash process: show the animated splash until stdin closes.

    @param language Interface language code.
    @param resource_path Function mapping path parts to a Path; default the
        application's `kicadgen.app_paths.resource_path`.
    @param stdin Binary stream to watch for end-of-file; default the
        process's own standard input.
    @param max_seconds Safety limit after which the splash quits anyway.
    @return 0 always: the splash is a courtesy and has nothing to report.
    @note The pipe is watched by a thread blocked on a read; it ends the
        application through a queued call, never touching widgets itself.
    """
    from PyQt6.QtCore import QMetaObject, Qt, QTimer
    from PyQt6.QtWidgets import QApplication
    if resource_path is None:
        from kicadgen.app_paths import resource_path
    app = QApplication.instance() or QApplication([sys.argv[0]])
    screen = app.primaryScreen()
    ratio = screen.devicePixelRatio() if screen is not None else 1.0
    pixmap = splash_pixmap(resource_path, ratio)
    window = _build_splash_widget()(pixmap, splash_message(language), not splash_has_artwork(resource_path))
    if screen is not None:
        area = screen.availableGeometry()
        window.move(area.center().x() - window.width() // 2, area.center().y() - window.height() // 2)
    window.show()
    QTimer.singleShot(int(max_seconds * 1000), app.quit)

    stream = stdin if stdin is not None else getattr(sys.stdin, "buffer", sys.stdin)

    def watch_pipe():
        try:
            while stream.read(1):
                pass
        except (OSError, ValueError):
            pass
        # A queued call reaches the GUI thread from here; a timer would not
        # (this thread runs no event loop).
        QMetaObject.invokeMethod(app, "quit", Qt.ConnectionType.QueuedConnection)

    threading.Thread(target=watch_pipe, name="splash-pipe", daemon=True).start()
    app.exec()
    return 0
