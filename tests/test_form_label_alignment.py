"""@brief Form label columns are as wide as their longest label (user, 2026-10-12).

A fixed 132 px column cut «Designatore di riferimento:» in «Metadati del
componente» and would cut any longer translation. The column keeps the
shared minimum when every label fits (fields of the panel stay aligned) and
grows to the longest label otherwise; a language change re-measures it.
"""
from PyQt6.QtWidgets import QFormLayout, QLabel, QLineEdit, QWidget

from kicadgen.gui import main_window as mw
from tests.test_photo_inventory_gui import gui  # noqa: F401 - qapp/window fixtures


def _form(*texts):
    host = QWidget()
    form = QFormLayout(host)
    for text in texts:
        form.addRow(text, QLineEdit())
    return host, form


def _needed(label: QLabel) -> int:
    return label.fontMetrics().horizontalAdvance(label.text())


def test_short_labels_keep_the_shared_minimum_width(qapp):
    host, form = _form("Nome:", "Pin:")
    width = mw._align_form_labels(form)
    assert width == mw.FORM_LABEL_MINIMUM_WIDTH
    assert all(label.width() == width for label in mw._form_labels(form))


def test_a_long_label_widens_the_whole_column(qapp):
    long_text = "Una etichetta decisamente più lunga della colonna minima:"
    host, form = _form("Nome:", long_text)
    width = mw._align_form_labels(form)
    labels = mw._form_labels(form)
    assert width > mw.FORM_LABEL_MINIMUM_WIDTH
    assert width >= _needed(labels[1]) + mw.FORM_LABEL_PADDING
    # Same column for the short label: the fields stay on one line.
    assert {label.width() for label in labels} == {width}


def test_metadata_labels_are_never_cut_in_any_language(window):
    """Driven through the window's own language change, so that dropping
    the re-measure from `_on_language_changed` is caught."""
    labels = mw._form_labels(window.metadata_body.layout())
    assert labels, "the metadata form has labelled rows"
    for language in ("fr", "de", "en", "it"):
        window._on_language_changed(window.language_combo.findData(language))
        assert window.i18n.language == language
        widths = {label.width() for label in labels}
        assert len(widths) == 1, f"{language}: one column for every label"
        for label in labels:
            assert label.width() >= _needed(label) + mw.FORM_LABEL_PADDING, (language, label.text())


def test_the_forms_of_one_panel_share_the_widest_column(window):
    """Artifacts and metadata forms sit in the KiCad tab: their fields line up."""
    panels = {}
    for panel, form in window._aligned_forms:
        panels.setdefault(panel, set()).update(label.width() for label in mw._form_labels(form))
    assert set(panels) == {"project", "kicad"}
    for panel, widths in panels.items():
        assert len(widths) == 1, f"{panel}: one column across its forms"
    kicad_width = next(iter(panels["kicad"]))
    longest = max(_needed(label) for _p, form in window._aligned_forms if _p == "kicad"
                  for label in mw._form_labels(form))
    assert kicad_width >= longest + mw.FORM_LABEL_PADDING


def test_the_column_is_measured_with_the_application_style_sheet(qapp):
    """The style sheet sets 10 pt: measured before it applied, the column
    came out short (user, 2026-10-12)."""
    from kicadgen.gui.theme import APP_QSS
    long_text = "Una etichetta decisamente più lunga della colonna minima:"
    host, form = _form(long_text)
    plain = mw._align_form_labels(form)
    sheet = qapp.styleSheet()
    try:
        qapp.setStyleSheet(APP_QSS)
        host2, form2 = _form(long_text)
        styled = mw._align_form_labels(form2)
    finally:
        qapp.setStyleSheet(sheet)
    assert styled > plain, "the measure follows the style sheet font"
