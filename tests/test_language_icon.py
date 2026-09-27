"""@brief Un globo al posto della scritta «Lingua:» (utente, 2026-09-24)."""
from test_photo_inventory_gui import gui


def test_the_language_is_an_icon_named_in_its_tooltip(window):  # noqa: F811
    """@brief Positivo: icona, nessun testo, nome nel tooltip e nella tendina."""
    label = window.language_label
    assert label.text() == "" and not label.pixmap().isNull()
    name = window.i18n.tr("language.name")
    assert label.toolTip() == name and window.language_combo.accessibleName() == name


def test_the_language_name_follows_the_language(window, monkeypatch):  # noqa: F811
    """@brief Negativo: il nome non resta nella lingua di prima."""
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    before = window.language_label.toolTip()
    window.language_combo.setCurrentIndex(window.language_combo.findData(
        "de" if window.i18n.language != "de" else "en"))
    after = window.language_label.toolTip()
    assert after != before and after == window.i18n.tr("language.name")
    assert window.language_combo.toolTip() == after
