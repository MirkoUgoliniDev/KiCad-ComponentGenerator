"""@brief Qt's own texts (standard buttons, context menus) in the application language.

The application translates its own strings through the SQLite catalogue, but
the texts Qt draws by itself -- the Yes/No/Cancel/Save buttons of every
QMessageBox and QDialogButtonBox, the Copy/Paste entries of a line edit's
context menu -- come from Qt's translation catalogues. Without a translator
they stayed in English whatever the chosen language (user, 2026-09-26: an
Italian question answered with «Yes / No»).
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QCoreApplication, QLibraryInfo, QTranslator
from PyQt6.QtWidgets import QApplication, QTabBar, QToolButton

#: The translator currently installed on the application, replaced on every
#: language change so that two catalogues never compete.
_installed: Optional[QTranslator] = None

#: Texts Qt writes ONCE, when the widget is built, and never rewrites on a
#: language change: (owner class, child object name, source text in Qt's
#: catalogue context). Measured on PyQt6 6.x: a QTabBar built in Italian kept
#: «Scorri a sinistra» after a switch to German, while a bar built after the
#: switch read «Nach links scrollen».
_BUILD_TIME_TEXTS = (
    (QTabBar, "ScrollLeftButton", "QTabBar", "Scroll Left"),
    (QTabBar, "ScrollRightButton", "QTabBar", "Scroll Right"),
)


def install_qt_translations(language: str) -> bool:
    """@brief Make Qt's built-in texts follow the application language.

    @param language Application language code (it, en, fr, de).
    @return True when Qt's catalogue for that language was loaded and installed.
    @note The previous translator is always removed first: after a change to a
        language without a catalogue Qt falls back to its English texts rather
        than keeping those of the previous language.
    @note The catalogues ship with PyQt6 (qtbase_<language>.qm in Qt's
        translations directory); none is added to the repository.
    """
    global _installed
    app = QCoreApplication.instance()
    if app is None:
        return False
    if _installed is not None:
        app.removeTranslator(_installed)
        _installed = None
    translator = QTranslator(app)
    directory = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    if not translator.load(f"qtbase_{language}", directory):
        retranslate_build_time_texts()
        return False
    app.installTranslator(translator)
    _installed = translator
    retranslate_build_time_texts()
    return True


def retranslate_build_time_texts() -> int:
    """@brief Rewrite in the current language the Qt texts that Qt only writes at construction.

    @return Number of texts rewritten (for tests and diagnostics).
    @note Qt's LanguageChange event does not reach these texts (see
        `_BUILD_TIME_TEXTS`): without this pass, the tab bars built before a
        language switch kept the start-up language. Every live widget of the
        application is visited, so dialogs that stay open follow too.
    """
    if not isinstance(QCoreApplication.instance(), QApplication):
        return 0
    rewritten = 0
    for widget in QApplication.allWidgets():
        for owner, name, context, source in _BUILD_TIME_TEXTS:
            if not isinstance(widget, owner):
                continue
            button = widget.findChild(QToolButton, name)
            if button is not None:
                button.setAccessibleName(QCoreApplication.translate(context, source))
                rewritten += 1
    return rewritten
