"""@brief Render the application icon SVG into a multi-size Windows .ico.

Usage: python tools/make_app_icon.py [output.ico]
The SVG in kicadgen/gui/assets/app-icon.svg is the single source; the .ico
used by the executable, the shortcuts and the MSI is generated from it.
"""
import os
import sys
from io import BytesIO
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "kicadgen" / "gui" / "assets" / "app-icon.svg"
SIZES = (16, 24, 32, 48, 64, 128, 256)


def main(argv):
    """@brief Write the .ico; return the process exit status.

    @param argv Command-line arguments; `argv[1]` is the optional target
        path of the .ico.
    @return 0 on success, 1 when the source SVG cannot be rendered.
    """
    from PIL import Image
    from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, Qt
    from PyQt6.QtGui import QImage, QPainter
    from PyQt6.QtSvg import QSvgRenderer
    from PyQt6.QtWidgets import QApplication

    target = Path(argv[1]) if len(argv) > 1 else ROOT / "packaging" / "KiCadGen.ico"
    app = QApplication.instance() or QApplication([])
    renderer = QSvgRenderer(str(SOURCE))
    if not renderer.isValid():
        print(f"Invalid SVG: {SOURCE}", file=sys.stderr)
        return 1
    image = QImage(256, 256, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    base = Image.open(BytesIO(bytes(data))).convert("RGBA")
    target.parent.mkdir(parents=True, exist_ok=True)
    base.save(target, format="ICO", sizes=[(s, s) for s in SIZES])
    print(f"Icon written: {target}")
    del app
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
