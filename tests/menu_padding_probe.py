"""@brief Child-process probe: right gap (px) of menu text under APP_QSS and under the old sheet.

Run by `test_theme_menu_padding.py` in its own process: the defect shows only
with the native Windows 11 style and the sheet on the APPLICATION, and
swapping either inside the shared test session crashed Qt natively. Prints
``new=<px> old=<px>``, or ``unavailable`` when the style is missing.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtGui import QColor  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMenu, QStyleFactory  # noqa: E402

from kicadgen.gui.theme import APP_QSS  # noqa: E402

ENTRIES = ("Duplica (Ctrl+D)", "Modifica testo…", "Elimina disegno")


def right_gap(app, qss):
    """@brief Smallest gap between any entry's lit text pixels and the menu's right edge."""
    app.setStyleSheet(qss)
    menu = QMenu()
    style = QStyleFactory.create("windows11")
    menu.setStyle(style)
    for text in ENTRIES:
        menu.addAction(text)
    menu.adjustSize()
    menu.show()
    app.processEvents()
    image = menu.grab().toImage()
    gaps = []
    for action in menu.actions():
        rect = menu.actionGeometry(action)
        right = -1
        for y in range(rect.top() + 2, rect.bottom() - 2):
            for x in range(image.width() - 2, 0, -1):
                if QColor(image.pixel(x, y)).lightness() > 120:
                    right = max(right, x)
                    break
        gaps.append(image.width() - 1 - right)
    menu.hide()
    return min(gaps)


def main():
    """@brief Print the two gaps, the theme's and the old sheet's without item padding."""
    app = QApplication(sys.argv[:1])
    if QStyleFactory.create("windows11") is None:
        print("unavailable")
        return
    old = re.sub(r"QMenu::item\s*\{[^}]*\}", "", APP_QSS)
    old = re.sub(r"(QMenu\s*\{[^}]*?)padding:[^;]*;", r"\1", old)
    print(f"new={right_gap(app, APP_QSS)} old={right_gap(app, old)}")


if __name__ == "__main__":
    main()
