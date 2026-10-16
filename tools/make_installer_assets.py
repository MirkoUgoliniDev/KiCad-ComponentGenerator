"""@brief Generate the MSI images and the license RTF from the project sources.

Usage: python tools/make_installer_assets.py <output folder>
Writes banner.bmp (493x58), dialog.bmp (493x312) and license.rtf. Sizes are
the WixUI standard ones: the text of the pages is drawn by Windows Installer
in black, on the right of the dialog image and on the left of the banner, so
those areas stay light.
"""
import os
import sys
from io import BytesIO
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
ICON_SVG = ROOT / "kicadgen" / "gui" / "assets" / "app-icon.svg"
DARK = (30, 30, 46)
ACCENT = (137, 180, 250)


def _icon(size):
    """The application icon as a PIL image of `size` pixels (rendered by Qt)."""
    from PIL import Image
    from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, Qt
    from PyQt6.QtGui import QImage, QPainter
    from PyQt6.QtSvg import QSvgRenderer
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    QSvgRenderer(str(ICON_SVG)).render(painter)
    painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return Image.open(BytesIO(bytes(data))).convert("RGBA")


def _rtf(text: str) -> str:
    """Plain text as a minimal RTF document (the license page needs RTF)."""
    escaped = (text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
               .replace("\r\n", "\n").replace("\n", "\\par\n"))
    return "{\\rtf1\\ansi\\deff0{\\fonttbl{\\f0 Segoe UI;}}\\f0\\fs18\n" + escaped + "\n}"


def main(argv):
    """@brief Write the three files; return the process exit status.

    @param argv Command-line arguments; `argv[1]` is the optional target
        folder.
    @return 0 on success.
    """
    from PIL import Image, ImageDraw
    from PyQt6.QtWidgets import QApplication
    target = Path(argv[1]) if len(argv) > 1 else ROOT / "build" / "msi"
    target.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])

    dialog = Image.new("RGB", (493, 312), "white")
    ImageDraw.Draw(dialog).rectangle((0, 0, 163, 311), fill=DARK)
    ImageDraw.Draw(dialog).rectangle((164, 0, 167, 311), fill=ACCENT)
    big = _icon(112)
    dialog.paste(big, (26, 40), big)
    dialog.save(target / "dialog.bmp")

    banner = Image.new("RGB", (493, 58), "white")
    ImageDraw.Draw(banner).rectangle((0, 56, 492, 57), fill=ACCENT)
    small = _icon(46)
    banner.paste(small, (493 - 46 - 8, 5), small)
    banner.save(target / "banner.bmp")

    (target / "license.rtf").write_text(_rtf((ROOT / "LICENSE").read_text(encoding="utf-8")),
                                        encoding="ascii", errors="replace")
    print(f"Installer assets written in {target}")
    del app
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
