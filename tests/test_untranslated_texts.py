"""@brief Revisore 3, difetto 10: testi rimasti in italiano con la lingua inglese.

Il motivo del blocco senza progetto e il tooltip di «Estrai parametri» si
scrivevano in italiano a ogni cambio di nome; il tooltip della lista degli
elementi (una vista a elenco) non passava dalla ritraduzione; tre testi non
avevano voce di catalogo. Negativo: in italiano restano quelli di sempre.
"""
from PyQt6.QtWidgets import QGroupBox, QLabel

from test_photo_inventory_gui import gui


def _switch(window, language):  # noqa: F811
    window.language_combo.setCurrentIndex(window.language_combo.findData(language))


def test_the_gate_reason_follows_the_language(window):  # noqa: F811
    _switch(window, "en")
    window.field_name.setText("")
    assert window.btn_extract_ai.toolTip() == "Create or open a project first (File menu)."
    assert window.docs_list.toolTip() == "Create or open a project first (File menu)."
    window.field_name.setText("Scheda")
    assert window.btn_extract_ai.toolTip().startswith("Start the extraction from the selected")
    # Un secondo cambio di lingua ritraduce dalla sorgente italiana, non dal
    # testo inglese appena mostrato.
    _switch(window, "fr")
    assert window.btn_extract_ai.toolTip().startswith("Lance l’extraction")
    _switch(window, "it")
    window.field_name.setText("")
    assert window.btn_extract_ai.toolTip() == "Crea o apri prima un progetto (menu File)."


def test_list_tooltip_group_tooltip_and_form_labels_are_translated(window):  # noqa: F811
    _switch(window, "en")
    assert window.ai_elements_list.toolTip() == "Items and outlines found during photo analysis"
    # Il gruppo «Modello 3D» e le etichette dei fori stavano nel tab Footprint
    # 2D, tolto il 2026-09-26: nessuna etichetta italiana resta a vista.
    labels = {label.text() for label in window.findChildren(QLabel)}
    assert not any("(0 = auto)" in text and ("bordo" in text) for text in labels)
    _switch(window, "it")
    assert window.ai_elements_list.toolTip() == \
        "Elementi e ingombri individuati durante l'analisi fotografica"
    # Il 3D non e' piu' un gruppo di widget: e' un dato (component_state).
    assert not isinstance(window.group_3d, QGroupBox)


# ── Audit 2026-10-15, medio 12: voci di combo, azioni e testi fuori dal ─
# ── testo del widget restavano in italiano dopo il cambio di lingua ──────
#
# Le classi di fuga che la ripassata generica non vedeva: voci di QComboBox,
# testo/tooltip/statusTip delle QAction, statusTip/whatsThis/tooltip dei
# widget fuori dall'elenco delle classi, testi tradotti una volta sola alla
# costruzione (anche in un'interfaccia partita in inglese) e testi tenuti in
# dizionari (TOOL_HINTS). Ogni regola ha il caso positivo e il negativo.

from PyQt6.QtCore import QEvent
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QApplication, QComboBox, QMenu, QPushButton, QToolButton, QWidget

from kicadgen.services.analysis_profiles import ProfileStore
from kicadgen.services.i18n import (DEFAULT_TRANSLATIONS, I18n, ITEM_SOURCE_ROLE,
                                    set_item_source)

MOUNTING = "Saldatura diretta / piazzole del modulo"
HINT = "Filo: clicca i punti nel simbolo; doppio clic o tasto destro per chiudere"
LINE_HINT = ("Linea: clicca i punti sulla griglia; doppio clic, Invio o tasto destro "
             "per chiudere, Esc per annullare")


def _i18n(tmp_path, language="it"):
    return I18n(ProfileStore(tmp_path / "profiles.sqlite3"), language)


def _rendered(language, source):
    return DEFAULT_TRANSLATIONS["ui:" + source][language]


def test_a_combo_entry_registered_with_its_source_follows_the_language(qapp, tmp_path):
    """@brief Positivo: la voce aggiunta con `add_item` cambia lingua; negativo: la voce dato no."""
    i18n = _i18n(tmp_path)
    root = QWidget()
    combo = QComboBox(root)
    i18n.add_item(combo, MOUNTING, "direct")
    combo.addItem("anthropic/claude-opus-5", "model")   # un dato: nome di modello
    combo.setCurrentIndex(1)
    for language in ("en", "fr", "de"):
        i18n.set_language(language)
        i18n.retranslate_widgets(root)
        assert combo.itemText(0) == _rendered(language, MOUNTING)
        assert combo.itemText(1) == "anthropic/claude-opus-5"
        assert combo.itemData(0) == "direct" and combo.currentIndex() == 1
    # Due cambi di fila ritraducono dalla sorgente, non dal testo mostrato.
    i18n.set_language("it")
    i18n.retranslate_widgets(root)
    assert combo.itemText(0) == MOUNTING


def test_an_entry_declared_afterwards_with_set_item_source_follows_too(qapp, tmp_path):
    """@brief La strada senza oggetto I18n (scheda Simbolo, `tx`): stessa regola."""
    i18n = _i18n(tmp_path)
    combo = QComboBox()
    combo.addItem(i18n.text("Ingresso"), "input")
    set_item_source(combo, 0, "Ingresso")
    i18n.set_language("de")
    i18n.retranslate_widgets(combo)
    assert combo.itemText(0) == _rendered("de", "Ingresso") != "Ingresso"
    assert combo.itemData(0, ITEM_SOURCE_ROLE) == "Ingresso"


def test_action_text_tooltip_and_status_tip_follow_the_language(qapp, tmp_path):
    """@brief Positivo: QAction di menu e di barra; negativo: azione `i18n_skip` e azione dato."""
    i18n = _i18n(tmp_path)
    root = QWidget()
    menu = QMenu(root)
    action = menu.addAction("Recupera bozza")
    action.setToolTip("Recupera bozza")
    action.setStatusTip("Scarta bozza")
    skipped = QAction("Scarta bozza", root)
    skipped.setProperty("i18n_skip", True)
    path_action = menu.addAction(r"C:\progetti\scheda")
    i18n.set_language("fr")
    i18n.retranslate_widgets(root)
    assert action.text() == _rendered("fr", "Recupera bozza") != "Recupera bozza"
    assert action.toolTip() == _rendered("fr", "Recupera bozza")
    assert action.statusTip() == _rendered("fr", "Scarta bozza")
    assert skipped.text() == "Scarta bozza"
    assert path_action.text() == r"C:\progetti\scheda"
    i18n.set_language("en")
    i18n.retranslate_widgets(root)
    assert action.text() == "Recover draft"


def test_prose_properties_of_any_widget_class_follow_the_language(qapp, tmp_path):
    """@brief Tooltip, statusTip e whatsThis di un QWidget qualunque (la palette e' un QWidget)."""
    i18n = _i18n(tmp_path)
    root = QWidget()
    plain = QWidget(root)
    plain.setToolTip("Palette: clicca un elemento, poi clicca nel simbolo per inserirlo.")
    plain.setStatusTip("Recupera bozza")
    plain.setWhatsThis("Scarta bozza")
    i18n.set_language("de")
    i18n.retranslate_widgets(root)
    assert plain.toolTip() == _rendered(
        "de", "Palette: clicca un elemento, poi clicca nel simbolo per inserirlo.")
    assert plain.statusTip() == _rendered("de", "Recupera bozza")
    assert plain.whatsThis() == _rendered("de", "Scarta bozza")


def test_a_text_rendered_in_english_at_construction_is_still_retranslated(qapp, tmp_path):
    """@brief Interfaccia partita in inglese: il testo catturato e' inglese, la sorgente si risale.

    Negativo: un testo che non e' la resa di nessuna voce resta com'e'.
    """
    i18n = _i18n(tmp_path, "en")
    root = QWidget()
    button = QPushButton(i18n.text("Recupera bozza"), root)
    button.setToolTip(i18n.text(HINT))
    foreign = QPushButton("Just a caption nobody catalogued", root)
    i18n.retranslate_widgets(root)          # primo passaggio, gia' in inglese
    i18n.set_language("fr")
    i18n.retranslate_widgets(root)
    assert button.text() == _rendered("fr", "Recupera bozza")
    assert button.toolTip() == _rendered("fr", HINT)
    assert foreign.text() == "Just a caption nobody catalogued"


def test_the_inventory_pause_question_is_in_the_catalogue_in_four_languages(tmp_path):
    """@brief La chiave usata da main_window esiste e ogni lingua e' una traduzione vera."""
    entry = DEFAULT_TRANSLATIONS["inventory_pause.question"]
    texts = {language: entry[language] for language in ("it", "en", "fr", "de")}
    assert all("{count}" in text for text in texts.values())
    assert len(set(texts.values())) == 4, "una lingua copia un'altra"
    i18n = _i18n(tmp_path, "de")
    assert i18n.tr("inventory_pause.question", count=7).startswith("Das KI-Inventar hat 7")


# ── Review 2026-10-16 (I1, I2, I3, I5) ───────────────────────────────


def _derived_tooltip(text):
    """Tooltip Qt derives for an action with `text` and no tooltip of its own."""
    return QAction(text).toolTip()


def test_an_action_without_its_own_tooltip_still_has_none_after_switches(qapp, tmp_path):
    """@brief I1 positive: a derived tooltip is never captured; negative: an explicit one is translated.

    An action without a tooltip reports one derived from its text. Freezing
    it as explicit made it stale after the next text change and visible in
    menus with tooltips shown.
    """
    i18n = _i18n(tmp_path)
    root = QWidget()
    menu = QMenu(root)
    menu.setToolTipsVisible(True)
    plain = menu.addAction("&Recupera bozza...")
    explicit = menu.addAction("Recupera bozza")
    explicit.setToolTip("Scarta bozza")
    for language in ("en", "fr", "de", "it", "en"):
        i18n.set_language(language)
        i18n.retranslate_widgets(root)
        assert plain.toolTip() == _derived_tooltip(plain.text())
        assert plain.property("i18n_tooltip_source") is None
        expected = "Scarta bozza" if language == "it" else _rendered(language, "Scarta bozza")
        assert explicit.toolTip() == expected
    # Still derived: a later text change by the program carries the tooltip with it.
    plain.setText("Annulla: sposta pin 3")
    assert plain.toolTip() == "Annulla: sposta pin 3"
    explicit.setText("Annulla: sposta pin 3")
    assert explicit.toolTip() == _rendered("en", "Scarta bozza")


def test_an_ambiguous_rendering_is_not_guessed(qapp, tmp_path):
    """@brief I2: «Fit» is the English of «Adatta» and of «Centra», whose French differ.

    Positive: the source recorded on the widget decides. Negative: with no
    recorded source the text is left unchanged instead of picking the first.
    """
    assert _rendered("en", "Adatta") == _rendered("en", "Centra") == "Fit"
    assert _rendered("fr", "Adatta") != _rendered("fr", "Centra")
    i18n = _i18n(tmp_path, "en")
    root = QWidget()
    unknown = QPushButton("Fit", root)
    recorded = QPushButton("Fit", root)
    recorded.setProperty("i18n_source", "Centra")
    unambiguous = QPushButton(i18n.text("Recupera bozza"), root)
    i18n.retranslate_widgets(root)
    i18n.set_language("fr")
    i18n.retranslate_widgets(root)
    assert unknown.text() == "Fit"
    assert recorded.text() == _rendered("fr", "Centra")
    assert unambiguous.text() == _rendered("fr", "Recupera bozza")
    assert i18n.source_of("Fit") is None


def test_an_english_rendering_equal_to_a_key_is_not_read_as_that_key(qapp, tmp_path):
    """@brief I2: «Apply» renders both «Applica» and the key «Apply», whose German differ.

    Positive: the Italian literal «Applica» shown in Italian is its own
    source and reaches German. Negative: the English «Apply» is ambiguous and
    is not translated as the key «Apply» (nor as «Applica»).
    """
    assert DEFAULT_TRANSLATIONS["ui:Applica"]["de"] != DEFAULT_TRANSLATIONS["ui:Apply"]["de"]
    italian = _i18n(tmp_path / "it")
    root = QWidget()
    literal = QPushButton("Applica", root)
    italian.set_language("de")
    italian.retranslate_widgets(root)
    assert literal.text() == DEFAULT_TRANSLATIONS["ui:Applica"]["de"]

    english = _i18n(tmp_path / "en", "en")
    other = QWidget()
    shown = QPushButton("Apply", other)
    english.retranslate_widgets(other)
    english.set_language("de")
    english.retranslate_widgets(other)
    assert shown.text() == "Apply"


def test_a_text_written_in_english_between_two_switches_is_recovered(qapp, tmp_path):
    """@brief I3: the reverse index covers every language, not only the target one.

    Negative: the same widget keeps following the language on later switches.
    """
    i18n = _i18n(tmp_path)
    root = QWidget()
    button = QPushButton("Scarta bozza", root)
    i18n.set_language("en")
    i18n.retranslate_widgets(root)
    button.setText(i18n.text("Recupera bozza"))
    button.setToolTip(i18n.text("Recupera bozza"))
    i18n.set_language("fr")
    i18n.retranslate_widgets(root)
    assert button.text() == button.toolTip() == _rendered("fr", "Recupera bozza")
    i18n.set_language("it")
    i18n.retranslate_widgets(root)
    assert button.text() == "Recupera bozza"


def test_the_reverse_index_is_cached_until_the_catalogue_changes(qapp, tmp_path):
    """@brief Positive: two passes share one index; negative: an edited translation rebuilds it."""
    i18n = _i18n(tmp_path)
    first = i18n._rendered_sources()
    assert i18n._rendered_sources() is first
    i18n.store.save_translation("ui:Recupera bozza", "en", "Bring the draft back")
    rebuilt = i18n._rendered_sources()
    assert rebuilt is not first
    assert "Recupera bozza" in rebuilt["Bring the draft back"]["en"]


def _component_review_button_source() -> str:
    """Italian source the «Revisione componenti…» button is built with, read from main_window."""
    import re
    from pathlib import Path
    code = (Path(__file__).resolve().parents[1] / "kicadgen" / "gui" / "main_window.py").read_text(
        encoding="utf-8")
    match = re.search(r'self\.btn_component_review = QPushButton\("([^"]+)"\)', code)
    assert match, "btn_component_review is no longer built with a literal label"
    return match.group(1)


def test_texts_naming_the_component_review_use_its_label_in_each_language():
    """@brief I5/R2I1: texts sending the user to the component review quote the REAL button's label.

    The label is derived from the source `btn_component_review` is built
    with, so renaming the button makes this fail instead of passing on a
    label no control carries. Negative: no text keeps the former wording
    «Modifica la revisione dei componenti…» or its renderings, which named a
    control that does not exist, and that label left the catalogue.
    """
    source = _component_review_button_source()
    label = DEFAULT_TRANSLATIONS["ui:" + source]
    keys = ["inventory_pause.question"] + [
        key for key in DEFAULT_TRANSLATIONS
        if key.startswith("ui:") and f"«{source}»" in key]
    assert len(keys) >= 2
    former = ("Modifica la revisione dei componenti", "Edit the component review",
              "Modifier la révision des composants", "Komponentenprüfung bearbeiten",
              "Bauteilprüfung bearbeiten")
    for key in keys:
        for language in ("it", "en", "fr", "de"):
            text = DEFAULT_TRANSLATIONS[key][language]
            quoted = (f"« {label[language]} »" if language == "fr"
                      else f"«{label[language]}»")
            assert quoted in text, (key, language)
            assert not any(old in text for old in former), (key, language)
    assert "ui:Modifica la revisione dei componenti…" not in DEFAULT_TRANSLATIONS


def test_a_database_seeded_with_the_former_review_wording_follows_the_real_button(tmp_path):
    """@brief R2I1: an existing database moves to the new pause question; a user edit stays.

    Positive: every former default of `inventory_pause.question` stored
    verbatim is replaced on opening. Negative: a text the user changed is left
    alone.
    """
    import sqlite3
    from kicadgen.services.analysis_profiles import ProfileStore
    from kicadgen.services.i18n import SUPERSEDED_DEFAULTS
    path = tmp_path / "profiles.sqlite3"
    ProfileStore(path)
    olds = SUPERSEDED_DEFAULTS["inventory_pause.question"]
    assert set(olds) == {"it", "en", "fr", "de"}
    with sqlite3.connect(path) as db:
        for language in ("it", "en", "fr"):
            db.execute("UPDATE translations SET text=? WHERE key=? AND language=?",
                       (olds[language][-1], "inventory_pause.question", language))
        db.execute("UPDATE translations SET text=? WHERE key=? AND language=?",
                   ("Testo scritto da me", "inventory_pause.question", "de"))
    db.close()
    ProfileStore(path)
    with sqlite3.connect(path) as db:
        stored = dict(db.execute(
            "SELECT language, text FROM translations WHERE key='inventory_pause.question'"))
    db.close()
    for language in ("it", "en", "fr"):
        assert stored[language] == DEFAULT_TRANSLATIONS["inventory_pause.question"][language]
    assert stored["de"] == "Testo scritto da me"


# ── Sulla finestra vera ──────────────────────────────────────────────


def _settle():
    """Consegna le cancellazioni differite: la palette ricostruita lascia
    i vecchi pulsanti in coda, e un rilievo li scambierebbe per fughe."""
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()


def _skipped(obj):
    return obj.property("i18n_skip") is True


def _visible_texts(window):
    """Ogni testo che l'utente puo' leggere, con la sua provenienza."""
    texts = []
    for widget in [window] + window.findChildren(QWidget):
        if _skipped(widget):
            continue
        where = type(widget).__name__
        for name, value in (("tooltip", widget.toolTip()), ("statusTip", widget.statusTip()),
                            ("whatsThis", widget.whatsThis())):
            texts.append((f"{where}.{name}", value))
        if isinstance(widget, QComboBox):
            for index in range(widget.count()):
                if widget.itemData(index, ITEM_SOURCE_ROLE):
                    texts.append((f"{where}.item[{index}]", widget.itemText(index)))
        elif hasattr(widget, "text") and not isinstance(widget, QToolButton):
            try:
                texts.append((f"{where}.text", widget.text()))
            except TypeError:
                pass
    for action in window.findChildren(QAction):
        if _skipped(action):
            continue
        for name, value in (("text", action.text()), ("tooltip", action.toolTip()),
                            ("statusTip", action.statusTip())):
            texts.append((f"QAction.{name}", value))
    return texts


def _leaks(window, language):
    """Testi mostrati in `language` che sono la sorgente italiana, o la resa
    inglese, di una voce la cui traduzione in `language` e' diversa."""
    foreign = {}
    for key, entry in DEFAULT_TRANSLATIONS.items():
        if not key.startswith("ui:") or not entry.get(language):
            continue
        for other in ("it", "en"):
            if other != language and entry.get(other) and entry[other] != entry[language]:
                foreign.setdefault(entry[other], key)
    return sorted({(where, text) for where, text in _visible_texts(window)
                   if text in foreign})


def test_no_italian_or_english_text_survives_a_switch_to_french_or_german(window):  # noqa: F811
    """@brief Rilievo su tutta la finestra: voci di combo, azioni, tooltip e statusTip compresi.

    Positivo: prima del correttivo le voci del montaggio, il layer del
    disegno, le azioni della bozza e i tooltip della palette restavano
    italiani. Il passaggio per l'inglese prova che il secondo cambio
    ritraduce dalla sorgente e non dal testo mostrato.
    """
    _switch(window, "en")
    _settle()
    for language in ("fr", "de"):
        _switch(window, language)
        _settle()
        assert _leaks(window, language) == []


def test_the_italian_interface_keeps_its_italian_texts(window):  # noqa: F811
    """@brief Negativo del rilievo: tornando all'italiano i testi sono le sorgenti."""
    _switch(window, "de")
    _switch(window, "it")
    _settle()
    assert window.combo_mounting.itemText(0) == MOUNTING
    assert window.combo_preview_mounting.itemText(0) == MOUNTING
    assert window.symbol_tab.discard_draft_action.text() == "Scarta bozza"
    assert window._fp_draw_buttons["line"].toolTip() == LINE_HINT


def test_the_mounting_combo_and_the_draw_layer_follow_each_switch(window):  # noqa: F811
    """@brief Le voci cambiano testo, non dato ne' indice."""
    window.combo_mounting.setCurrentIndex(1)
    for language in ("en", "fr", "de"):
        _switch(window, language)
        assert window.combo_mounting.itemText(0) == _rendered(language, MOUNTING)
        assert window.combo_preview_mounting.itemText(0) == _rendered(language, MOUNTING)
        assert window.combo_fp_draw_layer.itemText(0) == _rendered(language, "Serigrafia (stampata)")
        assert window.combo_mounting.currentData() == "headers"
        assert window.combo_mounting.currentIndex() == 1
        assert window.combo_fp_draw_layer.itemData(0) == "F.SilkS"
        # Testo tenuto in un dizionario (TOOL_HINTS), risolto dalla chiave.
        assert window._fp_draw_buttons["line"].toolTip() == _rendered(language, LINE_HINT)


def test_the_mounting_combo_is_english_when_the_app_starts_in_english(qapp, monkeypatch, tmp_path):
    """@brief Stessa causa: le voci erano scritte in italiano anche all'avvio in inglese."""
    from PyQt6.QtWidgets import QMessageBox
    from qt_fixtures import PreviewStub, shutdown_main_window
    monkeypatch.setattr(gui, "Preview3DVtk", PreviewStub)
    monkeypatch.setattr(gui, "load_config",
                        lambda: {"component_type": "", "system_prompt": "", "language": "en"})
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    monkeypatch.setattr(gui, "DEFAULT_LIBRARY_ROOT", tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-fixture")
    started = gui.MainWindow()
    try:
        assert started.i18n.language == "en"
        assert started.combo_mounting.itemText(0) == _rendered("en", MOUNTING)
        assert started.combo_preview_mounting.itemText(2) == _rendered(
            "en", "Strip header / pad quadrati con foro")
        _settle()
        assert _leaks(started, "en") == []
        # E da li' un cambio verso il tedesco ritraduce dalla sorgente.
        _switch(started, "de")
        _settle()
        assert started.combo_mounting.itemText(0) == _rendered("de", MOUNTING)
        assert _leaks(started, "de") == []
    finally:
        shutdown_main_window(started, qapp)
