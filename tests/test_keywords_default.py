"""@brief Keywords start empty and follow every extraction (user decision 2026-10-15, option 1).

Round-3 review M-1: the keywords field started as «pin header THT» and an
extraction copied its keywords only when it had some, so a SOIC analysed
without keywords kept «pin header THT» (or the previous analysis's words) in
its footprint tags and symbol ki_keywords, unnoticed because a filled field
passes the required-field gate. Now the field starts empty, every extraction
sets it (empty included), and the gate names the empty field.
"""
import dataclasses
import re

import pytest

from tests.test_package_project_gui import acquire, extracted_package
from tests.test_photo_inventory_gui import gui
from tests.test_required_metadata import INSTALL_ATTRS, _install_enabled


def test_a_new_window_and_a_new_project_have_no_keywords(window):  # noqa: F811
    assert window.field_tags.text() == ""
    assert window.field_meta_keywords.text() == ""
    assert window._general_project_state["widgets"]["field_tags"] == ""
    window._switch_project("NUOVO", window._general_project_state)   # as «Nuovo» does
    assert window.field_tags.text() == ""
    assert window._current_params().tags == "", "a manual pin header gets no invented keywords"


@pytest.mark.parametrize("previous", ["pin header THT", "parole di un'altra analisi"])
def test_an_extraction_without_keywords_clears_the_stale_value(window, previous):  # noqa: F811
    """Negative: before the fix the previous value survived the extraction."""
    window.field_tags.setText(previous)
    acquire(window)                       # extracted_package() has tags=""
    assert window.field_tags.text() == ""
    assert window._metadata_dict()["keywords"] == ""


def test_an_extraction_with_keywords_sets_them_and_a_second_without_clears_them(window):  # noqa: F811
    window._freecad_available = True
    window._on_extracted(dataclasses.replace(extracted_package(), tags="soic ldo regolatore"))
    assert window.field_tags.text() == "soic ldo regolatore"
    window._on_extracted(extracted_package())
    assert window.field_tags.text() == "", "the first analysis's keywords must not stay"


def test_an_extraction_without_keywords_blocks_until_typed_and_applied(window, monkeypatch):  # noqa: F811
    """End to end: empty keywords from the analysis -> install blocked naming
    «Parole chiave», box open; typed + «Applica ai file» -> unblocked, and the
    written footprint tags and symbol ki_keywords carry them."""
    acquire(window)
    monkeypatch.setattr(gui.GenerateWorker, "start", lambda worker: worker.run())
    window.field_meta_description.setText("Regolatore SOIC")
    window.field_datasheet.setText("https://example.org/ds.pdf")
    assert window._generate(footprint_only=True)
    folder = window._project_dir()
    name = folder.name
    step = folder / f"{name}.3dshapes" / f"{name}.step"
    step.parent.mkdir(parents=True, exist_ok=True)
    step.write_bytes(b"ISO-10303-21;\nEND-ISO-10303-21;\n")
    window._refresh_output_actions()
    assert window._missing_required_metadata() == ["keywords"]
    assert not any(_install_enabled(window).values())
    for attr in INSTALL_ATTRS:
        assert "Parole chiave" in getattr(window, attr).toolTip(), attr
    assert window.group_metadata.isExpanded()
    window.field_meta_keywords.setText("soic regolatore")
    window.btn_apply_metadata.click()
    assert all(_install_enabled(window).values())
    footprint = (folder / f"{name}.pretty" / f"{name}.kicad_mod").read_text(encoding="utf-8")
    assert re.search(r'\(tags "soic regolatore"\)', footprint)
    symbol = (folder / f"{name}.kicad_sym").read_text(encoding="utf-8")
    assert '(property "ki_keywords" "soic regolatore"' in symbol
    assert "pin header THT" not in footprint + symbol


def test_a_project_saved_with_the_old_default_keeps_it(window):  # noqa: F811
    """Point 4 of the decision: a saved «pin header THT» cannot be told apart
    from a user's choice, so it comes back as saved."""
    state = window._project_state()
    state["widgets"]["field_tags"] = "pin header THT"
    window._switch_project("VECCHIO", state)
    assert window.field_tags.text() == "pin header THT"
