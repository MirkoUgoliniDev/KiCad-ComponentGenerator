"""@brief The Italian user guide ships as a self-contained, well-formed HTML page."""
from html.parser import HTMLParser
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "kicadgen" / "gui" / "assets" / "help" / "guida.html"
REPOSITORY = "https://github.com/MirkoUgoliniDev/KiCad-ComponentGenerator"

#: Elements that never have a closing tag in HTML.
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "source", "track", "wbr"}


class _Checker(HTMLParser):
    """Collect nesting errors, ids, anchors and every URL-bearing attribute."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.errors, self.ids, self.anchors, self.urls = [], [], set(), [], []
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        attributes = dict(attrs)
        if "id" in attributes:
            if attributes["id"] in self.ids:
                self.errors.append(f"duplicate id {attributes['id']}")
            self.ids.add(attributes["id"])
        for name in ("href", "src", "action", "data", "poster"):
            if attributes.get(name):
                self.urls.append((tag, name, attributes[name]))
        if tag == "a" and attributes.get("href", "").startswith("#"):
            self.anchors.append(attributes["href"][1:])
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.stack.pop()

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unexpected </{tag}> (open: {self.stack[-3:]})")
            return
        self.stack.pop()


def _parse(text):
    checker = _Checker()
    checker.feed(text)
    checker.close()
    return checker


@pytest.fixture(scope="module")
def guide_text():
    assert GUIDE.is_file(), "guida.html is missing"
    return GUIDE.read_text(encoding="utf-8")


def test_guide_is_well_formed_html(guide_text):
    """@brief Balanced tags, unique ids, every internal anchor resolves."""
    checker = _parse(guide_text)
    assert not checker.errors, checker.errors
    assert not checker.stack, f"unclosed tags: {checker.stack}"
    assert guide_text.lstrip().lower().startswith("<!doctype html>")
    assert '<html lang="it">' in guide_text and '<meta charset="utf-8">' in guide_text
    missing = [anchor for anchor in checker.anchors if anchor not in checker.ids]
    assert not missing, f"broken anchors: {missing}"


def test_checker_detects_broken_markup():
    """@brief Negative: the checker is not blind to a misnested page."""
    assert _parse("<html><body><p><b>x</p></b></body></html>").errors
    assert _parse("<div><p>x</p>").stack == ["div"]


def test_guide_has_no_external_resources(guide_text):
    """@brief Offline and private: the only external link is the project repository."""
    checker = _parse(guide_text)
    for tag, attribute, url in checker.urls:
        if url.startswith("#"):
            continue
        assert tag == "a" and attribute == "href", f"external resource: <{tag} {attribute}={url}>"
        assert url.startswith(REPOSITORY), f"unexpected link: {url}"
    assert not {"script", "img", "link", "iframe", "object", "embed"} & set(checker.tags)
    # CSS may fetch too: no url(...) and no @import in the inline style.
    assert not re.search(r"url\s*\(|@import", guide_text, re.IGNORECASE)
    assert not re.findall(r"https?://(?!github\.com/MirkoUgoliniDev/KiCad-ComponentGenerator)"
                          r"[^\s\"'<>)]+", guide_text)


def test_guide_follows_light_and_dark_theme(guide_text):
    """@brief Readable in both schemes: explicit colours for light and a dark override."""
    assert "prefers-color-scheme: dark" in guide_text and "--bg" in guide_text


@pytest.mark.parametrize("label", [
    # Dal 26/09 i documenti hanno piu' ruoli per file: le caselle usano le
    # etichette brevi dei ruoli (document_roles.ROLE_SHORT).
    "Nuovo progetto...", "Pinout", "Quote", "Foto alta risoluzione", "Piazzole", "Vista laterale",
    "Nuovo documento", "Aggiungi…", "Scheda con componenti · connettori passanti",
    "Scheda con componenti · connettori castellated (mezzo foro)",
    "Estrai parametri con AI", "Credito AI", "Footprint 2D (KiCadGen)",
    "Modelli 3D dalla libreria KiCad", "Solido 3D con FreeCAD", "Chiudi",
    "Modifica componenti…", "Scelta del modello STEP", "Colori",
    "Pin quadrati per il collegamento alla scheda principale (solo modello 3D)",
    "Prova in KiCad...", "Installa nel progetto KiCad...",
    "Installa come libreria globale...", "analisi-ai.log", "freecad.log",
])
def test_guide_names_the_real_commands(guide_text, label):
    """@brief The guide uses the Italian labels the program actually shows.

    @note Whitespace is collapsed first, as a browser does: a label may wrap
        across source lines.
    """
    assert label in re.sub(r"\s+", " ", guide_text)


@pytest.mark.parametrize("label,source", [
    ("Nuovo progetto...", "kicadgen/gui/main_window.py"),
    ("Footprint 2D (KiCadGen)", "kicadgen/gui/main_window.py"),
    ("Modifica componenti…", "kicadgen/gui/main_window.py"),
    ("Pin quadrati per il collegamento alla scheda principale (solo modello 3D)",
     "kicadgen/gui/main_window.py"),
    ("Prova in KiCad...", "kicadgen/gui/main_window.py"),
    ("Scelta del modello STEP", "kicadgen/gui/model_candidates_dialog.py"),
    ("Colori", "kicadgen/gui/model_candidates_dialog.py"),
    ("'top': 'Quote'", "kicadgen/services/document_roles.py"),
    ("'side_view': 'Vista laterale'", "kicadgen/services/document_roles.py"),
    ("Nuovo documento", "kicadgen/gui/guided_documents.py"),
])
def test_quoted_labels_still_exist_in_the_code(label, source):
    """@brief When a command is renamed in the code, this fails and the guide is updated."""
    assert label in (ROOT / source).read_text(encoding="utf-8")


def test_guide_ships_in_the_package():
    """@brief The PyInstaller spec copies the whole assets folder, help/ included."""
    spec = (ROOT / "packaging" / "KiCadGen.spec").read_text(encoding="utf-8")
    assert '(str(ROOT / "kicadgen" / "gui" / "assets"), "kicadgen/gui/assets")' in spec
