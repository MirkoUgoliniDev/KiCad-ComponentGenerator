"""@brief Verify the SQLite-backed internationalization contract.

Oltre alle traduzioni, qui si verifica la COPERTURA del catalogo: ogni testo
che l'interfaccia mostra deve avere la sua voce nelle quattro lingue. Il
controllo si costruisce leggendo il codice della GUI invece di elencare a mano
le stringhe, perche' un elenco scritto a mano invecchia in silenzio: la voce
che manca si scopre solo quando un utente vede una frase italiana dentro
un'interfaccia inglese.
"""

import ast
from pathlib import Path
import re

import pytest

from kicadgen.services.analysis_profiles import ProfileStore
from kicadgen.services.i18n import DEFAULT_TRANSLATIONS, LANGUAGES, I18n


#: Cartella della GUI: e' li' che nascono i testi visibili.
GUI_DIR = Path(__file__).resolve().parents[1] / "kicadgen" / "gui"

#: Costruttori di widget il cui PRIMO argomento e' testo visibile, ritradotto
#: da `I18n.retranslate_widgets` (pulsanti, etichette, gruppi).
_TEXT_CONSTRUCTORS = {"QPushButton", "QLabel", "QCheckBox", "QRadioButton",
                      "QToolButton", "QGroupBox", "QCommandLinkButton", "QAction"}

#: Setter il cui primo argomento e' testo visibile. Sono gli stessi che
#: `retranslate_widgets` rilegge dalle proprieta' `i18n_*_source`.
_TEXT_SETTERS = {"setText", "setToolTip", "setWindowTitle", "setTitle",
                 "setPlaceholderText", "setAccessibleName", "setTabText"}

#: Proprieta' con cui un widget dichiara da se' la propria sorgente i18n.
_SOURCE_PROPERTIES = {"i18n_source", "i18n_tooltip_source", "i18n_window_title_source",
                      "i18n_title_source", "i18n_placeholder_source",
                      "i18n_accessible_name_source"}

#: EURISTICA DICHIARATA (1/2): un valore macchina non e' una frase da tradurre.
#: Sono codici colore (`#c9a227`, scritto in un QLineEdit di dati) e stringhe
#: di soli numeri o separatori. Il criterio e' la FORMA del valore, non il
#: widget che lo riceve: dedurre staticamente il tipo di ogni widget sarebbe
#: piu' preciso ma molto piu' fragile. Diventerebbe superflua se i valori
#: macchina non passassero mai da un setter di testo.
_MACHINE_VALUE = re.compile(r"^(#[0-9a-fA-F]{3,8}|[\d.,:%/+\-]+)$")


def _skipped_targets(tree):
    """@brief Nomi dei widget marcati `i18n_skip`, che non vanno mai tradotti.

    @param tree Albero sintattico di un modulo della GUI.
    @return Insieme di nomi come li scrive il sorgente ('self.label_kicad_step').
    @note Sono i widget che portano dati dell'utente (nomi di file, percorsi):
        `retranslate_widgets` li salta, quindi il catalogo non li deve coprire.
    """
    skipped = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "setProperty" and len(node.args) >= 2
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "i18n_skip"):
            skipped.add(ast.unparse(node.func.value))
    return skipped


def ui_source_literals():
    """@brief Ogni testo visibile scritto come letterale nella GUI.

    @return Dizionario {testo: {"file.py:riga", ...}}.
    @note Si raccolgono le due strade con cui un testo raggiunge il catalogo:
        la traduzione esplicita (`self._tr("…")`, `i18n.text("…")`,
        `setProperty("i18n_source", "…")`) e il testo scritto direttamente nel
        widget, che `I18n.retranslate_widgets` cattura al primo passaggio e da
        quel momento cerca nel catalogo come `ui:<testo>`. Fermarsi alla prima
        strada lascerebbe fuori proprio i casi visti sul campo: un pulsante
        nuovo e il suo tooltip, scritti nel costruttore.
    """
    literals = {}

    def remember(value, path, line):
        literals.setdefault(value, set()).add(f"{path.name}:{line}")

    def constant(node):
        return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None

    for path in sorted(GUI_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skipped = _skipped_targets(tree)
        assigned = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                for target in node.targets:
                    if isinstance(target, (ast.Name, ast.Attribute)):
                        assigned[id(node.value)] = ast.unparse(target)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            name = (function.attr if isinstance(function, ast.Attribute)
                    else function.id if isinstance(function, ast.Name) else None)
            owner = (ast.unparse(function.value) if isinstance(function, ast.Attribute)
                     else None)
            if name in ("_tr", "text") and (value := constant(node.args[0] if node.args else None)):
                # `text` esiste su mezza libreria Qt: vale solo sull'oggetto i18n.
                if name == "text" and owner not in ("i18n", "self.i18n"):
                    continue
                remember(value, path, node.lineno)
            elif name in _TEXT_CONSTRUCTORS and (value := constant(node.args[0] if node.args else None)):
                if assigned.get(id(node)) not in skipped:
                    remember(value, path, node.lineno)
            elif name in _TEXT_SETTERS and owner not in skipped:
                if value := constant(node.args[0] if node.args else None):
                    remember(value, path, node.lineno)
            elif name in ("setHorizontalHeaderLabels", "setHeaderLabels"):
                if node.args and isinstance(node.args[0], (ast.List, ast.Tuple)):
                    for element in node.args[0].elts:
                        if value := constant(element):
                            remember(value, path, node.lineno)
            elif name == "addTab" and len(node.args) >= 2 and (value := constant(node.args[1])):
                remember(value, path, node.lineno)
            elif name in ("tool_button", "set_tool_texts"):
                # Pulsanti a icona delle finestre (review_tools): titolo e
                # spiegazione sono sorgenti del catalogo; il tasto (terzo
                # argomento in entrambe) e' una sigla e non si traduce.
                offset = 1
                for position, argument in enumerate(node.args[offset:], start=offset):
                    shortcut = position == offset + 1
                    if not shortcut and (value := constant(argument)):
                        remember(value, path, node.lineno)
            elif name in ("_workflow_tool_button", "_view_tool_button", "_set_workflow_detail"):
                # Comandi a icona: nome e spiegazione sono sorgenti del
                # catalogo, tradotte quando si compone il tooltip.
                for argument in node.args[1:]:
                    if value := constant(argument):
                        remember(value, path, node.lineno)
            elif name == "addRow" and len(node.args) >= 2 and (value := constant(node.args[0])):
                # Etichetta di una riga di QFormLayout: diventa una QLabel che
                # `retranslate_widgets` ritraduce. Mancava al raccoglitore, e
                # «Modello AI:» restava italiano in inglese (utente, 24/09).
                remember(value, path, node.lineno)
            elif (name == "setProperty" and len(node.args) >= 2
                  and isinstance(node.args[0], ast.Constant)
                  and node.args[0].value in _SOURCE_PROPERTIES
                  and (value := constant(node.args[1]))):
                remember(value, path, node.lineno)
    return literals


def translatable(literal):
    """@brief Dice se quel letterale e' una frase da tradurre.

    @param literal Testo trovato nel codice della GUI.
    @return False per cio' che resta identico in ogni lingua.
    @note EURISTICA DICHIARATA (2/2): senza nemmeno una lettera non c'e' nulla
        da tradurre (i puntini di sospensione, un trattino di segnaposto), e un
        valore macchina (`#c9a227`) non e' prosa. Tutto il resto va nel
        catalogo, anche i nomi propri: una voce che ripete lo stesso testo
        nelle quattro lingue dichiara che quel testo non si traduce, mentre
        l'assenza della voce non distingue «non si traduce» da «dimenticata».
    """
    if not any(character.isalpha() for character in literal):
        return False
    return not _MACHINE_VALUE.match(literal.strip())


def missing_ui_translations():
    """@brief I testi della GUI privi di una voce completa nel catalogo.

    @return {testo: (motivo, {posizioni})}, vuoto quando il catalogo copre tutto.
    """
    missing = {}
    for literal, places in ui_source_literals().items():
        if not literal.strip() or not translatable(literal):
            continue
        entry = DEFAULT_TRANSLATIONS.get("ui:" + literal)
        if entry is None:
            missing[literal] = ("nessuna voce nel catalogo", places)
            continue
        absent = sorted(language for language in LANGUAGES if not entry.get(language))
        if absent:
            missing[literal] = ("lingue mancanti: " + ", ".join(absent), places)
    return missing


def test_translation_catalog_seeds_all_supported_languages(tmp_path):
    """@brief Ensure a new profile database contains the navigation catalog."""
    store = ProfileStore(tmp_path / "profiles.sqlite3")
    i18n = I18n(store, "fr")
    # Titoli senza emoji dal 2026-09-24: l'icona della scheda e' un SVG.
    assert i18n.tr("tab.project.name") == "Projet"
    assert i18n.tr("tab.model3d.name") == "Modèle 3D"
    assert i18n.tr("tab.kicad.name") == "KiCad"


def test_emoji_free_tab_titles_reach_a_database_created_before_them(tmp_path):
    """@brief Un catalogo gia' creato riceve i titoli nuovi senza perdere le modifiche.

    Il catalogo SQLite aggiunge le chiavi mancanti a ogni apertura ma non
    riscrive quelle esistenti. Per questo i titoli senza emoji hanno chiavi
    nuove: positivo, un database con le vecchie voci «📁 Progetto» mostra il
    titolo nuovo; negativo, una modifica dell'utente a una chiave esistente
    sopravvive alla riapertura.
    """
    path = tmp_path / "profiles.sqlite3"
    store = ProfileStore(path)
    store.save_translation("tab.project", "it", "📁 Progetto")
    store.save_translation("tab.project.name", "it", "Il mio progetto")
    reopened = ProfileStore(path)
    assert I18n(reopened, "it").tr("tab.project.name") == "Il mio progetto"
    fresh = tmp_path / "fresh.sqlite3"
    import sqlite3
    ProfileStore(fresh)
    with sqlite3.connect(fresh) as db:
        db.execute("DELETE FROM translations WHERE key LIKE 'tab.%.name'")
    assert I18n(ProfileStore(fresh), "it").tr("tab.model3d.name") == "Modello 3D"


def test_translation_falls_back_to_english_and_persists_edits(tmp_path):
    """@brief Ensure missing locales fall back and edited text survives reopen."""
    store = ProfileStore(tmp_path / "profiles.sqlite3")
    store.save_translation("custom.help", "de", "Benutzerdefinierte Hilfe")
    assert I18n(store, "de").tr("custom.help") == "Benutzerdefinierte Hilfe"
    assert I18n(store, "fr").tr("app.file") == "Fichier"
    assert I18n(store, "fr").tr("custom.help") == "custom.help"
    assert any(row["key"] == "custom.help" for row in store.list_translations("de"))


def test_existing_database_receives_new_bundled_translation_keys(tmp_path):
    """@brief Seed new UI keys without overwriting a user's existing edit."""
    database = tmp_path / "profiles.sqlite3"
    store = ProfileStore(database)
    store.save_translation("ui:Non disponibile", "fr", "Indisponible personnalisé")
    with store._connect() as connection:
        connection.execute(
            "DELETE FROM translations WHERE key=? AND language=?",
            ("ui:Vista superiore", "fr"),
        )

    reopened = ProfileStore(database)

    assert reopened.translation("ui:Vista superiore", "fr") == "Vue de dessus"
    assert reopened.translation("ui:Non disponibile", "fr") == "Indisponible personnalisé"


@pytest.mark.parametrize(
    ("language", "title", "compatible", "header", "no_limit", "selected", "cancel"),
    (
        ("it", "Modello solo per questa sessione", "Solo modelli compatibili",
         "Modello (id)", "nessun limite", "Modello scelto: anthropic/claude-sonnet-5", "Annulla"),
        ("en", "Model for this session only", "Compatible models only",
         "Model (ID)", "no limit", "Selected model: anthropic/claude-sonnet-5", "Cancel"),
        ("fr", "Modèle pour cette session uniquement", "Modèles compatibles",
         "Modèle (ID)", "aucune limite", "Modèle sélectionné : anthropic/claude-sonnet-5", "Annuler"),
        ("de", "Modell nur für diese Sitzung", "Nur kompatible Modelle",
         "Modell (ID)", "keine Begrenzung", "Ausgewähltes Modell: anthropic/claude-sonnet-5", "Abbrechen"),
    ),
)
def test_model_picker_uses_parent_language_without_touching_model_data(qapp,
        tmp_path, monkeypatch, language, title, compatible, header, no_limit,
        selected, cancel):
    """@brief Translate the nested model picker that executes inside its helper."""
    from PyQt6.QtWidgets import QDialogButtonBox, QWidget
    from kicadgen.gui.model_dialog import ModelPickerDialog

    app = qapp
    # Qt's own button texts follow the Qt translator the application installs
    # for its language (MainWindow does it at start and on every change): the
    # test installs it too, instead of relying on whatever an earlier test
    # left installed (it failed at random depending on the order, 131).
    from kicadgen.gui import qt_translations
    qt_translations.install_qt_translations(language)
    request_cleanup = lambda: qt_translations.install_qt_translations("none")  # noqa: E731
    parent = QWidget()
    parent.i18n = I18n(ProfileStore(tmp_path / "models.sqlite3"), language)
    monkeypatch.setattr(ModelPickerDialog, "_load_models", lambda self, force_refresh=False: None)

    dialog = ModelPickerDialog("anthropic/claude-sonnet-5", parent,
                               title="Modello solo per questa sessione")

    assert dialog.windowTitle() == title
    assert dialog.check_compatible.text().startswith(compatible)
    assert dialog.table.horizontalHeaderItem(0).text() == header
    assert dialog.spin_max_cost.specialValueText() == no_limit
    assert dialog.label_selected.text() == selected
    try:
        assert dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel).text() == cancel
    finally:
        dialog.close()
        parent.close()
        app.processEvents()
        request_cleanup()


# ── Copertura del catalogo: nessuna lingua mescolata nell'interfaccia ─


def test_every_ui_string_has_a_four_language_entry():
    """@brief Positivo: ogni testo visibile della GUI ha la sua voce completa.

    `I18n.text` ripiega sulla sorgente italiana quando la chiave manca, quindi
    una voce dimenticata non rompe nulla: produce una frase italiana dentro
    un'interfaccia inglese, francese o tedesca, accanto a vicini tradotti. Il
    difetto si vede solo guardando l'applicazione nella lingua giusta, e non si
    scopre da soli: e' esattamente il caso in cui serve un controllo
    automatico.

    Il controllo legge il codice della GUI invece di elencare le stringhe a
    mano: un elenco scritto a mano non conosce il pulsante aggiunto domani.
    """
    missing = missing_ui_translations()
    assert not missing, "voci di traduzione mancanti:\n" + "\n".join(
        f"  {literal!r} ({reason}) in {', '.join(sorted(places))}"
        for literal, (reason, places) in sorted(missing.items()))


def test_the_scan_sees_the_strings_the_user_actually_reads():
    """@brief Il controllo sopra vale quanto cio' che riesce a vedere.

    Negativo del test precedente: senza questa prova un raccoglitore rotto --
    che non trova piu' nessun letterale -- lo farebbe passare a vuoto per
    sempre. Si ancora quindi il raccoglitore ai casi che l'audit ha trovato
    davvero non tradotti, uno per ciascuna delle strade con cui un testo
    raggiunge il catalogo: la traduzione esplicita con `_tr`, il testo scritto
    nel costruttore del widget e il tooltip impostato con un setter.
    """
    literals = ui_source_literals()
    assert len(literals) > 100, "il raccoglitore non vede piu' la GUI"
    assert "Componenti: {count} di {total}" in literals, "manca la strada di `self._tr`"
    assert "Modifica la revisione dei componenti…" in literals, \
        "manca il testo scritto nel costruttore del widget"
    # Testo del tooltip del filtro aggiornato il 2026-09-24 (il filtro ora
    # filtra anche la foto): stessa strada, setToolTip su un letterale.
    assert any(literal.startswith("Filtra per categoria la lista") for literal in literals), \
        "manca il tooltip impostato con setToolTip"


@pytest.mark.parametrize("literal,expected", [
    ("Applica", True),
    ("Componenti: {count} di {total}", True),
    ("KiCadGen-Tool", True),
    ("…", False),
    ("—", False),
    ("#c9a227", False),
    ("12,5", False),
])
def test_only_machine_values_are_exempt_from_the_catalog(literal, expected):
    """@brief L'esenzione e' stretta: solo cio' che e' identico in ogni lingua.

    Positivo e negativo insieme. Un nome proprio come «KiCadGen-Tool» NON e'
    esente: entra nel catalogo con lo stesso testo nelle quattro lingue, che
    dichiara «non si traduce». L'assenza della voce non lo direbbe: non
    distinguerebbe una scelta da una dimenticanza.
    """
    assert translatable(literal) is expected


def test_the_removed_entries_are_not_referenced_by_any_source():
    """@brief Negativo: le voci tolte erano davvero morte.

    Due voci nominavano un pulsante che non esiste piu' e una terza conservava
    la vecchia formulazione del paragrafo di istruzioni, che il dialogo aveva
    gia' riscritto: la voce non combaciava piu' e il paragrafo restava non
    tradotto. Tenerle avrebbe fatto credere che quei testi fossero coperti.
    """
    removed = (
        "ui:Rivedi proposte e rilancia l'inventario…",
        "ui:Mostra gli ingombri rilevati sulla foto superiore e, quando disponibile, "
        "permette di correggerli. Non avvia la generazione del footprint o del modello 3D.",
    )
    for key in removed:
        assert key not in DEFAULT_TRANSLATIONS
        assert key[3:] not in ui_source_literals()
    stale = [key for key in DEFAULT_TRANSLATIONS
             if key.startswith("ui:Seleziona un rettangolo")
             and "Modifica le quote per ridimensionare" in key]
    assert not stale, "la vecchia formulazione del paragrafo non deve restare in catalogo"
