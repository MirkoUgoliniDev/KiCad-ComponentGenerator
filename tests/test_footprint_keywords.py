"""@brief The project keywords reach the footprint `tags` on every generation path (review 2026-10-15, R2-M2).

Before the fix `build_package` always wrote `tags=geometry.family`, and the
worker passed it only the datasheet and the description: on an explicit
package and on a module mounted with headers (whose host footprint is a
PackageGeometry, family «module») the project keywords never reached the
installed footprint, while the applied-metadata record and the help said they
did. Each path is checked by reading the tags back from the written
.kicad_mod; the empty-keywords case keeps the family and a byte-identical file.
"""
import re

import pytest

from kicadgen.generators.host_mounting import header_footprint_params
from kicadgen.generators.package import build_package
from kicadgen.generators.pin_header import PinHeaderParams
from kicadgen.gui.workers import GenerateWorker
from tests.test_footprint_review_fixes import _board
from tests.test_host_mounting import module

KEYWORDS = "radio modulo castellated"


def _tags(text: str):
    """The `(tags "...")` value of a footprint text, or None when absent."""
    match = re.search(r'\(tags "([^"]*)"\)', text)
    return match.group(1) if match else None


def _without_uuids(text: str) -> str:
    """KiCad item UUIDs are random per render; everything else must match byte for byte."""
    return re.sub(r'\(uuid "[^"]+"\)', '(uuid)', text)


def _run(params, folder, *, host=None, tags=""):
    """Run one footprint-only generation synchronously; return the written text."""
    worker = GenerateWorker(params, folder, None)
    worker.footprint_params = host
    worker.tags = tags
    errors = []
    worker.error.connect(errors.append)
    worker.run()
    assert not errors, errors
    return (folder / f"{params.name}.pretty" / f"{params.name}.kicad_mod").read_text(encoding="utf-8")


# ── Generator ───────────────────────────────────────────────────────────

def test_build_package_writes_the_keywords_and_falls_back_to_the_family():
    geometry = _board((0, 0), (2.54, 0))
    assert _tags(build_package(geometry, tags=KEYWORDS).render()) == KEYWORDS
    assert _tags(build_package(geometry, tags="  " + KEYWORDS + " ").render()) == KEYWORDS
    for empty in ("", "   "):
        assert _tags(build_package(geometry, tags=empty).render()) == "module"


def test_without_keywords_the_package_file_is_unchanged():
    """Projects without keywords: the same file as before the parameter existed."""
    geometry = _board((0, 0), (2.54, 0))
    before = _without_uuids(build_package(geometry).render())
    assert _without_uuids(build_package(geometry, tags="").render()) == before
    assert _without_uuids(build_package(geometry, tags=" \t").render()) == before


# ── Every worker path, read back from the written file ──────────────────

def test_explicit_package_path(qapp, tmp_path):
    geometry = _board((0, 0), (2.54, 0))
    assert _tags(_run(geometry, tmp_path, tags=KEYWORDS)) == KEYWORDS
    assert _tags(_run(geometry, tmp_path, tags="")) == "module", "no keywords: the family, as before"


def test_module_mounted_with_headers_path(qapp, tmp_path):
    """The host footprint is a PackageGeometry built from the castellated module."""
    physical = module()
    host = header_footprint_params(physical, 1.6, .9)
    assert _tags(_run(physical, tmp_path, host=host, tags=KEYWORDS)) == KEYWORDS
    assert _tags(_run(physical, tmp_path, host=host, tags="")) == host.family


def test_pin_header_and_castellated_paths_use_their_params(qapp, tmp_path):
    """Those generators already read `params.tags`; the worker's `tags` must not override them."""
    header = PinHeaderParams(name="HDR", rows=1, cols=3, tags=KEYWORDS)
    assert _tags(_run(header, tmp_path, tags="altro")) == KEYWORDS
    board = module()
    board.tags = KEYWORDS
    assert _tags(_run(board, tmp_path, tags="altro")) == KEYWORDS


# ── Window: what `_generate` hands over is what the snapshot records ─────

@pytest.mark.parametrize("keywords", [KEYWORDS, ""])
def test_the_window_writes_the_recorded_keywords_on_a_package(window, monkeypatch, keywords):  # noqa: F811
    from tests.test_package_project_gui import acquire
    from tests.test_photo_inventory_gui import gui
    acquire(window)
    monkeypatch.setattr(gui.GenerateWorker, "start", lambda worker: worker.run())
    window.field_meta_keywords.setText(keywords)
    assert window._generate(footprint_only=True)
    folder = window._project_dir()
    text = (folder / f"{folder.name}.pretty" / f"{folder.name}.kicad_mod").read_text(encoding="utf-8")
    family = window.package_geometry.family
    assert _tags(text) == (keywords or family)
    assert window._applied_metadata["keywords"] == keywords
