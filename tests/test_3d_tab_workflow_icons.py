"""@brief Scheda Modello 3D: i comandi del flusso di lavoro sono icone nella barra in alto.

Nota 6 dell'utente (2026-09-23): una sola barra -- strumenti di vista a
sinistra, un divisore, poi i comandi del flusso di lavoro come icone con
tooltip tradotti -- e niente piu' riga in basso, cosi' il modello guadagna
spazio. Il tipo di contatto perde l'etichetta testuale e diventa un'icona con
tooltip accanto al selettore.

Nota 8 (parte b e c): le quote dell'anteprima 2D e le righe di avanzamento
dell'abbinamento modelli non devono restare in una lingua diversa da quella
dell'interfaccia.

Ogni comportamento ha un caso positivo e uno negativo.
"""
import ast
from pathlib import Path
import re

import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("pyvistaqt")
from PyQt6.QtWidgets import QFrame, QLabel, QPushButton, QToolButton

from kicadgen.gui import main_window as gui
from kicadgen.gui import theme
from kicadgen.gui.analysis_overlay import is_machine_line
from kicadgen.generators.pin_header import PinHeaderParams
from kicadgen.services.i18n import DEFAULT_TRANSLATIONS, LANGUAGES
from tests.test_photo_inventory_gui import window  # noqa: F401 - fixture condivisa


def _workflow(window):
    return [window.btn_regen_3d, window.btn_accept_models, window.btn_reopen_review_3d,
            window.btn_relaunch_inventory_3d, window.btn_edit_component_review_3d]


def _view_tools(window):
    return [window.chk_pin_numbers, window.btn_pinout_3d, window.btn_fit_3d,
            window.btn_open_3dshapes]


def _bar_widgets(window):
    layout = window.model_3d_action_bar.layout()
    return [layout.itemAt(i).widget() for i in range(layout.count())]


# ── Una sola barra: vista, divisore, flusso di lavoro ─────────────────────


def test_workflow_commands_are_icons_after_the_divider(window):  # noqa: F811
    """@brief Positivo: cinque icone senza testo, tutte DOPO il divisore verticale.

    Negativo: gli strumenti di vista restano PRIMA del divisore; le due
    famiglie non si mescolano.
    """
    widgets = _bar_widgets(window)
    divider = window.toolbar_3d_divider
    assert isinstance(divider, QFrame) and divider.frameShape() == QFrame.Shape.VLine
    split = widgets.index(divider)
    for button in _workflow(window):
        assert isinstance(button, QToolButton)
        assert button.text() == ""
        assert not button.icon().isNull()
        assert widgets.index(button) > split
    for button in _view_tools(window):
        assert widgets.index(button) < split


def test_the_bottom_row_is_gone_and_the_bar_sits_above_the_model(window):  # noqa: F811
    """@brief Positivo: la barra sta sopra lo splitter del modello.

    Negativo: attorno al modello -- barra e riquadro dell'anteprima, cioe'
    fuori dal pannello delle impostazioni avanzate, che ha i suoi pulsanti --
    non resta nessun pulsante a testo visibile, e dopo la riga di stato non
    c'e' piu' nulla: la vecchia riga in basso era fatta di QPushButton. Il
    solo QPushButton rimasto e' «Rileva terminali», nascosto per decisione
    dell'utente (2026-09-13) e verificato altrove.
    """
    page = window.model_3d_page.layout()
    widgets = [page.itemAt(i).widget() for i in range(page.count())]
    assert widgets[0] is window.model_3d_action_bar
    assert widgets[1].isAncestorOf(window.preview_3d)
    viewer_pane = window.preview_3d.parentWidget()
    visible_text_buttons = [
        button for root in (window.model_3d_action_bar, viewer_pane)
        for button in root.findChildren(QPushButton) if not button.isHidden()]
    assert visible_text_buttons == []
    pane = viewer_pane.layout()
    assert pane.itemAt(pane.count() - 1).widget() is window.label_3d_status


def test_icon_tooltips_carry_the_name_then_the_explanation(window):  # noqa: F811
    """@brief Positivo: prima riga = nome del comando, poi la spiegazione; nome accessibile = nome.

    Negativo: la spiegazione non finisce nel nome accessibile, che un lettore
    di schermo pronuncia a ogni passaggio.
    """
    for button in (window.btn_regen_3d, window.btn_accept_models):
        title = button.property("workflow_title_source")
        detail = button.property("workflow_detail_source")
        assert button.toolTip() == f"{title}\n{detail}"
        assert button.accessibleName() == title
        assert detail not in button.accessibleName()


def test_own_workflow_texts_are_catalogued_in_four_languages(window):  # noqa: F811
    """@brief Le sorgenti italiane dei tooltip composti hanno la voce completa nel catalogo.

    Il controllo statico di tests/test_i18n.py non vede queste stringhe, che
    passano per un parametro: la copertura si verifica qui, sui valori veri.
    """
    for button in (window.btn_regen_3d, window.btn_accept_models):
        for key in ("workflow_title_source", "workflow_detail_source"):
            entry = DEFAULT_TRANSLATIONS.get("ui:" + button.property(key))
            assert entry is not None, button.property(key)
            assert all(entry.get(language) for language in LANGUAGES)
    busy = DEFAULT_TRANSLATIONS["ui:Generazione in corso (FreeCAD)..."]
    assert all(busy.get(language) for language in LANGUAGES)


def test_icon_tooltips_follow_the_language(window):  # noqa: F811
    """@brief Positivo: in inglese il tooltip e' inglese; negativo: l'italiano non resta."""
    window._on_language_changed(window.language_combo.findData("en"))
    regen = DEFAULT_TRANSLATIONS["ui:Rigenera modello 3D (STEP) con queste annotazioni"]["en"]
    assert window.btn_regen_3d.accessibleName() == regen
    assert window.btn_regen_3d.toolTip().startswith(regen)
    assert "Rigenera" not in window.btn_regen_3d.toolTip()
    accept = DEFAULT_TRANSLATIONS["ui:Accetto, mi piace — ricorda questi modelli…"]["en"]
    assert window.btn_accept_models.accessibleName() == accept
    window._on_language_changed(window.language_combo.findData("it"))
    assert window.btn_regen_3d.accessibleName().startswith("Rigenera")


def test_regenerate_is_the_emphasised_primary_command(window):  # noqa: F811
    """@brief Positivo: «Rigenera» e' il comando principale e il tema lo evidenzia.

    Negativo: nessun altro comando della barra reclama la stessa enfasi.
    """
    assert window.btn_regen_3d.objectName() == "primary"
    assert "QToolButton#primary" in theme.APP_QSS
    others = [button for button in _workflow(window) + _view_tools(window)
              if button is not window.btn_regen_3d]
    assert all(button.objectName() != "primary" for button in others)


def test_the_paid_command_stays_recognisable_as_paid(window):  # noqa: F811
    """@brief Positivo: il rilancio a pagamento lo dichiara nel tooltip e ha un'icona propria.

    Negativo: i comandi gratuiti accanto non si dichiarano a pagamento.
    """
    paid = window.btn_relaunch_inventory_3d
    assert "A PAGAMENTO" in paid.toolTip()
    assert "(a pagamento)" in paid.accessibleName()
    keys = {button.icon().cacheKey() for button in _workflow(window)}
    assert len(keys) == len(_workflow(window)), "ogni comando ha la sua icona"
    for free in (window.btn_reopen_review_3d, window.btn_edit_component_review_3d,
                 window.btn_regen_3d, window.btn_accept_models):
        assert "pagamento" not in free.toolTip().lower()


# ── Tipo di contatto: icona al posto dell'etichetta ───────────────────────


def test_contact_type_label_became_an_icon_with_tooltip(window):  # noqa: F811
    """@brief Positivo: un'icona con tooltip precede il selettore, nella stessa barra.

    Negativo: l'etichetta testuale «Tipo contatto (tutti i lati):» non c'e' piu'.
    """
    icon = window.icon_contact_style_3d
    assert icon.pixmap() is not None and not icon.pixmap().isNull()
    assert icon.text() == ""
    assert icon.toolTip() == "Tipo contatto (tutti i lati)"
    assert window.contact_style_bar_3d.isAncestorOf(icon)
    assert window.model_3d_action_bar.isAncestorOf(window.combo_contact_style_3d)
    texts = [label.text() for label in window.model_3d_page.findChildren(QLabel)]
    assert "Tipo contatto (tutti i lati):" not in texts


def test_contact_type_icon_is_still_only_for_castellated_modules(window):  # noqa: F811
    """@brief Negativo: senza modulo castellated niente icona; positivo: con, c'e'."""
    assert window.contact_style_bar_3d.isHidden()
    window.group_castellated.setChecked(True)
    assert not window.contact_style_bar_3d.isHidden()
    assert window.icon_contact_style_3d.isVisibleTo(window.model_3d_action_bar)


# ── Nota 8b: quote dell'anteprima 2D tradotte ──────────────────────────────


DIM_KEYS = ("dim.body", "dim.total", "dim.pitch", "dim.row_spacing")


@pytest.mark.parametrize("key", DIM_KEYS)
def test_dimension_label_keys_exist_in_four_languages(key):
    """@brief Le voci delle quote esistono in tutte e quattro le lingue, con il segnaposto."""
    entry = DEFAULT_TRANSLATIONS.get(key)
    assert entry is not None, f"{key} manca nel catalogo"
    for language in LANGUAGES:
        assert "{value:.2f}" in entry.get(language, ""), (key, language)


def _scene_texts(window):
    return [item.text() for item in window.preview_scene.items() if hasattr(item, "text")]


def test_dimension_labels_follow_the_language(window):  # noqa: F811
    """@brief Positivo: in inglese le quote usano le parole inglesi, con due decimali.

    Negativo: le parole italiane «totale»/«passo» non restano nel disegno.
    """
    window._last_params = PinHeaderParams(name="H", rows=2, cols=4, pitch=2.54,
                                          row_spacing=2.54, pad_diameter=1.7, drill=1.0)
    window._draw_preview()
    italian = _scene_texts(window)
    assert any(text.startswith("totale ") for text in italian), "premessa: disegno italiano"
    window._on_language_changed(window.language_combo.findData("en"))
    english = _scene_texts(window)
    expected_total = window.i18n.tr("dim.total", value=3 * 2.54 + 2.0)
    assert expected_total in english
    assert expected_total == DEFAULT_TRANSLATIONS["dim.total"]["en"].format(value=3 * 2.54 + 2.0)
    assert not any(text.startswith(("totale ", "passo ", "righe ", "corpo "))
                   for text in english)


# ── Nota 8c: righe di avanzamento dell'abbinamento modelli in italiano ────


#: EURISTICA DICHIARATA: parole funzione inglesi che non compaiono in una
#: frase italiana. Serve solo a riconoscere una frase inglese in un sorgente;
#: la prova negativa qui sotto verifica che il riconoscitore scatti davvero.
_ENGLISH_FUNCTION_WORDS = re.compile(
    r"\b(the|of|with|without|is|are|for|first|checking|restricting|withheld|omitted|"
    r"rejected|proposed|disabled)\b", re.IGNORECASE)


def _rendered_notes(path):
    """@brief Le righe `note(...)` di un modulo, con i campi variabili resi come «X»."""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    lines = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "note" and node.args):
            continue
        parts = []
        for piece in ast.walk(node.args[0]):
            if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                parts.append(piece.value)
            elif isinstance(piece, ast.FormattedValue):
                parts.append("X")
        lines.append((node.lineno, "".join(parts)))
    return lines


def _reaches_the_modal(line):
    return not (is_machine_line(line) or gui.per_item_detail(line))


def test_matching_progress_lines_that_reach_the_modal_are_italian():
    """@brief Positivo: ogni riga dell'abbinamento che arriva in modale e' scritta in italiano.

    Le righe per-elemento con campi macchina («…; review_required; …») restano
    fuori dalla modale per costruzione (`per_item_detail`) e non sono prese in
    esame: la loro forma e' letta da altro codice e non va toccata.
    """
    import kicadgen.cad3d.kicad_models as km

    english = [(line_no, line) for line_no, line in _rendered_notes(km.__file__)
               if _reaches_the_modal(line) and _ENGLISH_FUNCTION_WORDS.search(line)]
    assert english == []


def test_the_english_detector_is_not_blind():
    """@brief Negativo del precedente: la vecchia riga inglese viene riconosciuta come tale."""
    old = ("Estimated chip package: checking all standard endcap sizes and variants; "
           "nominated size is not an exact constraint.")
    assert _reaches_the_modal(old)
    assert _ENGLISH_FUNCTION_WORDS.search(old)
    assert not _ENGLISH_FUNCTION_WORDS.search(
        "Package chip stimato: si provano tutte le misure e varianti standard.")
