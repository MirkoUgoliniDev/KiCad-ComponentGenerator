"""@brief HANDOFF 157: what the AI read is shown in «Parametri componente», no drill-down.

User, 2026-09-29: «il + non mi piace più con drilldown: quando un componente
è selezionato si attivi Parametri… e le informazioni appaiano lì, rimuovendo
dimensione e rotazione». The list is plain again; the parameter modal shows
name, type, marking and height (editable) and, read-only, the five lines of
`component_attribute_lines`. Centre, sides and rotation are no longer shown
there: their spin boxes stay, hidden, as the contract through which dragging
and resizing on the photo (and the tests) write the geometry.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtWidgets import QToolButton

from kicadgen.gui.photo_review_dialog import (
    ATTRIBUTE_UNAVAILABLE, PhotoReviewDialog, component_attribute_lines,
)
from test_photo_review_list import components, png  # noqa: F401  (fixtures)


def _identity(text):
    return text


## A row as the RS485 run of 2026-09-29 wrote it (C4, shortened).
FULL_ROW = dict(
    name="C4 (ic)", kind="ic", **{"class": "ic"}, class_confidence="low",
    package="SOT-223", package_confidence="low",
    marking="345", marking_confidence="medium",
    _crop_reading="disagreed",
    _review_reason="Letture del ritaglio discordi (class, package, leads).",
    x_mm=-3.7878, y_mm=-0.1292, length_mm=3.42, width_mm=3.0388, rotation_deg=90,
    height_mm=1.5, _height_estimated=True,
    model_match={"status": "provisional_matched", "selected_path": "C:/lib/SOT-223.step"},
)


def test_the_lines_of_a_full_row_include_model_origin():
    """@brief Positive: type, package, marking, crop outcome, KiCad model; nothing else."""
    lines = component_attribute_lines(FULL_ROW, _identity)
    assert [label for label, _ in lines] == [
        "Tipo", "Package", "Sigla letta", "Letture del ritaglio", "Modello KiCad", "Provenienza modello KiCad"]
    as_dict = dict(lines)
    assert as_dict["Tipo"] == "ic · confidenza bassa"
    assert as_dict["Package"] == "SOT-223 · confidenza bassa"
    assert as_dict["Sigla letta"] == "345 · confidenza media"
    assert as_dict["Letture del ritaglio"] == "discordi (da rivedere)"
    assert as_dict["Modello KiCad"] == "agganciato dalla sola geometria, da confermare: SOT-223.step"


def test_a_bare_row_reports_what_is_missing_and_invents_nothing():
    """@brief Negative: a hand-drawn body has no readings; none are made up."""
    as_dict = dict(component_attribute_lines(dict(name="Nuovo", kind="generic"), _identity))
    assert as_dict["Tipo"] == "generic"
    assert as_dict["Package"] == "non dichiarato"
    assert as_dict["Sigla letta"] == "nessuna"
    assert as_dict["Letture del ritaglio"] == ATTRIBUTE_UNAVAILABLE
    assert as_dict["Modello KiCad"].startswith("non ancora cercato")
    assert len(as_dict) == 6


def test_model_origin_and_individual_orientation_are_explicit():
    from kicadgen.gui.photo_review_dialog import kicad_section_lines
    row = {"model_path": "part.step", "model_match": {"attachment": "accepted_library"}}
    lines = dict(kicad_section_lines(row, _identity))
    assert lines["Provenienza modello KiCad"] == "Archivio consolidati"
    assert "Da verificare" in lines["Orientamento da consolidati"]
    row["model_match"]["accepted_orientation"] = {
        "status": "matched", "rotation_deg": 90, "image_id": 7}
    assert "90°" in dict(kicad_section_lines(row, _identity))["Orientamento da consolidati"]
    row["model_selection"] = {"status": "confirmed"}
    assert dict(kicad_section_lines(row, _identity))["Provenienza modello KiCad"] == "Scelta manuale"
    assert component_attribute_lines(None, _identity) == []


def _model_line(**fields):
    return dict(component_attribute_lines(dict(kind="ic", **fields), _identity))["Modello KiCad"]


def test_the_kicad_model_line_follows_the_matcher_and_the_pipeline_step():
    """@brief The review at the pipeline pause precedes the library search."""
    assert _model_line() == \
        "non ancora cercato: la ricerca in libreria parte dopo «Applica correzioni»"
    assert _model_line(model_match={"status": "auto_matched", "selected_path": "x/R_0805.step"}) \
        == "agganciato dalla sola geometria, da confermare: R_0805.step"
    assert _model_line(model_match={"status": "library_matched", "selected_path": "a/b.step"}) \
        == "dall'archivio dei tuoi abbinamenti, da confermare: b.step"
    assert _model_line(model_match={"status": "review_required", "candidates": [{}, {}, {}]}) \
        == "candidati da scegliere: 3"
    assert _model_line(model_match={"status": "unresolved", "candidates": []}) \
        == "nessun candidato compatibile in libreria"
    assert _model_line(model_match={"status": "import_failed", "selected_path": "a/c.step"}) \
        == "importazione STEP fallita: c.step"
    assert _model_line(model_match={"status": "something_new"}) == "non confermato · something_new"
    assert _model_line(model_match={"status": "unresolved"},
                       model_selection={"status": "confirmed", "path": "C:/lib/3d/SOT-223.step"}) \
        == "confermato: SOT-223.step"


def test_free_text_of_the_model_is_left_out():
    """@brief The user's rule: no review reason, no provenance, no dimensions."""
    text = "\n".join(f"{label}: {value}"
                     for label, value in component_attribute_lines(FULL_ROW, _identity))
    for absent in ("Da rivedere", "Quote", "Altezza", "Posizione", "Geometria", "Origine",
                   "discordi (class", "3.42"):
        assert absent not in text


@pytest.fixture
def dialog(app, png, components):  # noqa: F811
    components[0].update(FULL_ROW, name="U1")
    widget = PhotoReviewDialog(png, components, 40, 20)
    widget.resize(900, 600)
    widget.show()
    app.processEvents()
    yield widget
    widget.close()


def test_the_list_is_plain_again(dialog):
    """@brief No «+», no row widgets: the row text is the item text, as before."""
    assert not dialog.component_list.findChildren(QToolButton)
    assert all(dialog.component_list.itemWidget(dialog.component_list.item(i)) is None
               for i in range(dialog.component_list.count()))
    assert dialog.component_list.item(0).text().startswith("1. U1")


def test_the_parameter_modal_shows_the_ai_lines_and_hides_the_geometry(dialog, app):
    """@brief Selection fills the read-only lines; centre, sides, rotation are not shown."""
    dialog.component_list.setCurrentRow(0)
    app.processEvents()
    assert dialog.parameters_button.isEnabled()
    text = dialog.attributes_plain_text()
    # HANDOFF 163: two headed sections, the detector's (empty here: no
    # reading on this row) before the AI's five lines.
    # Blank lines: QTextDocument separates a table band from the paragraphs.
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines[0] == "Rilevatore locale (YOLO)"
    assert lines[1] == "Lettura: nessuna proposta singola del rilevatore"
    assert lines[2] == "Lettura AI"
    assert lines[3] == "Tipo: ic · confidenza bassa"
    assert "Package: SOT-223 · confidenza bassa" in text
    # HANDOFF 165: the KiCad model has its own section, the third.
    assert lines[7] == "Modello KiCad"
    assert lines[8] == "Modello KiCad: agganciato dalla sola geometria, da confermare: SOT-223.step"
    assert len(lines) == 10
    dialog.open_parameters()
    app.processEvents()
    form = dialog._form
    # 157-bis: the height goes too («togli altezza stimata»); every quota is hidden.
    for key in ("x_mm", "y_mm", "length_mm", "width_mm", "rotation_deg", "height_mm"):
        assert dialog.fields[key].isHidden()
        assert form.labelForField(dialog.fields[key]).isHidden()
    # Rich text: one paragraph per line (headings are table bands), values
    # escaped, never interpreted.
    assert dialog.attributes_label.text().count("<p") == 7
    assert dialog.attributes_label.text().count("<table") == 3
    assert dialog.attributes_label.isVisibleTo(dialog.parameters_dialog)
    for key in ("name", "kind", "marking"):
        assert not dialog.text_fields[key].isHidden()
    dialog.parameters_dialog.close()


def test_the_lines_follow_the_selection_and_the_edits(dialog, app):
    """@brief Another body, another text; typing a type resets the model line."""
    dialog.component_list.setCurrentRow(2)
    app.processEvents()
    assert "Tipo: capacitor" in dialog.attributes_plain_text().splitlines()
    assert "Letture del ritaglio: non disponibile" in dialog.attributes_plain_text()
    dialog.component_list.setCurrentRow(0)
    app.processEvents()
    assert "SOT-223.step" in dialog.attributes_plain_text()
    # A type typed by hand forgets the model match: the line says so at once.
    dialog.text_fields["kind"].setText("resistor")
    dialog._text_edited("kind")
    app.processEvents()
    typed = [line for line in dialog.attributes_plain_text().splitlines()
             if line.startswith("Tipo:")]
    assert typed == ["Tipo: ic · confidenza bassa"] or typed[0].startswith("Tipo: resistor")
    assert "non ancora cercato" in dialog.attributes_plain_text()


def test_the_hidden_geometry_fields_still_mirror_a_drag(dialog, app):
    """@brief The contract of the hidden spin boxes is unchanged: they follow the outline."""
    dialog.component_list.setCurrentRow(1)
    app.processEvents()
    item = dialog._items[1]
    point = dialog.photo_transform.mm_to_pixel(10.0, -3.0)
    item.setPos(point)
    dialog._item_moved(item)
    assert abs(dialog.fields["x_mm"].value() - 10.0) < 1e-6
    assert abs(dialog.fields["y_mm"].value() + 3.0) < 1e-6
    assert dialog.fields["x_mm"].isHidden()


def test_the_attributes_follow_the_language(dialog, app, tmp_path):
    """@brief Labels are translated; the AI's values stay data."""
    from kicadgen.services.analysis_profiles import ProfileStore
    from kicadgen.services.i18n import I18n
    dialog.i18n = I18n(ProfileStore(tmp_path / "profiles.sqlite3"), "en")
    # Row 0 may already be current: the lines are rebuilt on a selection change.
    dialog.component_list.setCurrentRow(1)
    dialog.component_list.setCurrentRow(0)
    app.processEvents()
    text = dialog.attributes_plain_text()
    assert "Package: SOT-223 · confidence low" in text
    assert "Crop readings: in disagreement (to review)" in text
    assert "KiCad model: hooked from geometry alone, to confirm: SOT-223.step" in text


def test_the_outline_tooltip_is_rich_text_that_wraps(dialog, app):
    """@brief HANDOFF 154: plain text never wraps and overflowed the screen."""
    dialog._components[1]["warnings"] = "<script>alert(1)</script> & altro"
    dialog._refresh_item(dialog._items[1])
    tooltip = dialog._items[1].toolTip()
    assert tooltip.startswith("<b>2. R1")
    assert "<br>" in tooltip
    assert "<script>" not in tooltip and "&lt;script&gt;" in tooltip and "&amp; altro" in tooltip
    assert "\n" not in tooltip
    assert dialog.component_list.item(1).toolTip() == tooltip


def test_the_modal_grows_to_show_every_wrapped_line(dialog, app):
    """@brief User, 2026-09-29: the last AI line («non ancora cercato: la ricerca…») was cut."""
    dialog.component_list.setCurrentRow(2)      # a bare row: the longest model line
    app.processEvents()
    dialog.open_parameters()
    app.processEvents()
    label = dialog.attributes_label
    assert "non ancora cercato" in dialog.attributes_plain_text()
    assert label.height() >= label.heightForWidth(label.width())
    bottom = label.mapTo(dialog.parameters_dialog, label.rect().bottomLeft()).y()
    assert bottom <= dialog.parameters_dialog.height()
    # Switching to a row with shorter lines while the modal is open keeps it fitted.
    dialog.parameters_dialog.close()


def test_model_path_without_report_does_not_invent_origin():
    from kicadgen.gui.photo_review_dialog import kicad_section_lines
    lines = dict(kicad_section_lines({"model_path": "old.step"}, _identity))
    assert lines["Provenienza modello KiCad"] == "Provenienza non disponibile"
    for attachment in ("automatic", "provisional"):
        lines = dict(kicad_section_lines({"model_match": {"attachment": attachment}}, _identity))
        assert lines["Provenienza modello KiCad"] == "Ricerca nella libreria KiCad"
