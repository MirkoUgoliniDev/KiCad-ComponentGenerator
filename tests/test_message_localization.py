"""@brief Log, avanzamento ed errori nella lingua dell'utente (decisione del 2026-09-24).

«Verifica che non ci siano testi in italiano cablati: deve essere tutto
localizzato» -- l'utente ha scelto il perimetro «tutto»: interfaccia, log e
diario, errori. I messaggi nascono lontano dalla GUI, quindi passano da
`messages.tx` con un MODELLO italiano; questi test verificano il meccanismo,
la copertura del catalogo e che nessun punto di uscita scavalchi `tx`.
"""
import ast
import json
import re
import string
from pathlib import Path

import pytest

from test_photo_inventory_gui import gui
from kicadgen.services import messages  # noqa: F401
from kicadgen.services.i18n import DEFAULT_TRANSLATIONS, LANGUAGES
from kicadgen.services.messages import (CACHE_HIT, WARNING, child_warning_text, kind_of, marked,
                                        render_child_line, set_translator, tx, warning)

PACKAGE = Path(__file__).resolve().parents[1] / "kicadgen"


# ── Il meccanismo ────────────────────────────────────────────────────────

def _english(key, default=None):
    """Traduttore finto: solo due voci, il resto ripiega sulla sorgente."""
    return {"ui:Progetto aperto: {folder}": "Project opened: {folder}",
            "ui:Rotto {x}": "Broken {y}"}.get(key, default)


def test_without_translator_the_italian_template_is_formatted():
    assert tx("Progetto aperto: {folder}", folder="C:/P") == "Progetto aperto: C:/P"
    assert tx("Graffe {{letterali}}") == "Graffe {letterali}"


def test_the_translator_is_used_and_values_move_with_the_template():
    """@brief Positivo: voce presente -> inglese. Negativo: voce assente -> italiano."""
    set_translator(_english)
    assert tx("Progetto aperto: {folder}", folder="C:/P") == "Project opened: C:/P"
    assert tx("Nessuna voce {n}", n=3) == "Nessuna voce 3"


def test_a_broken_translation_never_raises():
    """@brief Un segnaposto sbagliato nel catalogo non fa cadere un messaggio: vale l'italiano."""
    set_translator(_english)
    assert tx("Rotto {x}", x=1) == "Rotto 1"
    set_translator(lambda key, default=None: 1 / 0)
    assert tx("Progetto aperto: {folder}", folder="A") == "Progetto aperto: A"


def test_a_placeholder_named_like_the_source_parameter_works():
    """@brief `source` e' solo posizionale: un modello puo' avere un segnaposto «source»."""
    assert tx("{source}: elenco componenti non valido.", source="crop") == \
        "crop: elenco componenti non valido."


def test_the_kind_travels_with_the_text_and_plain_strings_have_none():
    """@brief Positivo: warning()/marked() portano il tipo. Negativo: tx() e un str no."""
    set_translator(_english)
    message = warning("Progetto aperto: {folder}", folder="X")
    assert message == "Project opened: X" and kind_of(message) == WARNING
    assert kind_of(marked(CACHE_HIT, "qualcosa")) == CACHE_HIT
    assert kind_of(tx("qualcosa")) == "" and kind_of("testo") == "" and kind_of(None) == ""


def test_a_freecad_warning_is_translated_by_the_parent():
    """@brief Positivo: JSON {t, v} dal figlio -> testo tradotto. Negativo: testo libero invariato."""
    set_translator(_english)
    payload = json.dumps({"t": "Progetto aperto: {folder}", "v": {"folder": "D"}})
    assert child_warning_text(payload) == "Project opened: D"
    assert render_child_line("FreeCAD: KICADGEN_WARN:" + payload) == \
        "FreeCAD: KICADGEN_WARN:Project opened: D"
    assert child_warning_text(" componente U3: package sconosciuto") == \
        "componente U3: package sconosciuto"
    assert render_child_line("KICADGEN_STEP_OK:C:/x.step") == "KICADGEN_STEP_OK:C:/x.step"


def test_the_empty_source_stays_empty(tmp_path):
    """@brief Regressione: `I18n.text("")` dava «ui:», finito in un tooltip a progetto aperto."""
    from kicadgen.services.analysis_profiles import ProfileStore
    from kicadgen.services.i18n import I18n
    i18n = I18n(ProfileStore(tmp_path / "p.sqlite3"), "en")
    assert i18n.text("") == ""
    assert i18n.text("Esci") == "Exit"
    assert i18n.tr("chiave.che.non.esiste") == "chiave.che.non.esiste"


# ── Copertura del catalogo ───────────────────────────────────────────────

_CALLS = {"tx": 0, "warning": 0, "_warn": 0, "marked": 1}


def message_templates():
    """@brief Ogni modello scritto come letterale in tx/warning/marked/_warn del pacchetto.

    @return {modello: {"file.py:riga", ...}}.
    @note `_warn` e' la funzione del processo FreeCAD: i suoi modelli li
        traduce il padre, quindi stanno nello stesso catalogo.
    """
    found = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            index = _CALLS.get(name)
            if index is None or len(node.args) <= index:
                continue
            arg = node.args[index]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.setdefault(arg.value, set()).add(f"{path.name}:{node.lineno}")
    return found


def _fields(text):
    return sorted((name, conversion or "", spec or "") for _, name, spec, conversion
                  in string.Formatter().parse(text) if name is not None)


def test_every_message_template_has_four_languages_with_the_same_placeholders():
    """@brief Positivo: ogni modello e' nel catalogo, e ogni traduzione ha gli stessi segnaposto.

    Un segnaposto rinominato in una traduzione non rompe nulla (tx ripiega
    sull'italiano) ma rimette l'italiano davanti a chi ha scelto un'altra
    lingua: e' il difetto che questo test esiste per vedere.
    """
    problems = []
    for template, places in sorted(message_templates().items()):
        entry = DEFAULT_TRANSLATIONS.get("ui:" + template)
        where = ", ".join(sorted(places)[:2])
        if not entry:
            problems.append(f"manca {template!r} ({where})")
            continue
        for language in LANGUAGES:
            text = entry.get(language)
            if not text:
                problems.append(f"{language} vuota: {template!r}")
            elif _fields(text) != _fields(template):
                problems.append(f"{language} segnaposto diversi: {template!r}")
    assert not problems, "\n".join(problems)


def test_the_template_scan_sees_the_three_ways():
    """@brief Negativo del test sopra: un raccoglitore rotto lo farebbe passare a vuoto."""
    templates = message_templates()
    assert len(templates) > 400
    assert any(p.startswith("main_window.py") for p in templates["Nuovo progetto: {project_dir}"])
    assert any(p.startswith("photo_grounding.py")
               for p in templates["{label}: fallita ({exc}); ingombri conservati come dubbi."])
    assert any(p.startswith("freecad_parts.py")
               for p in templates["connector mating sconosciuto; ingombro generico"])


# ── Nessun punto di uscita scavalca tx ───────────────────────────────────

#: EURISTICA DICHIARATA: riconosce una frase italiana da parole funzionali e
#: da apostrofi/accenti tipici. Serve solo a non segnalare le stringhe
#: tecniche inglesi dei punti di uscita (errori di programmazione, marcatori);
#: una frase italiana senza nessuna di queste parole sfuggirebbe. Diventerebbe
#: superflua se ogni punto di uscita accettasse solo `tx(...)`.
#: Una parola seguita da un trattino non conta («Non-finite» e' inglese) e
#: l'apostrofo conta solo dopo una vocale, come nell'elisione italiana
#: («puo'», «e'»), non dopo una consonante che chiude una citazione.
_ITALIAN = re.compile(
    r"\b(il|lo|la|gli|le|di|del|della|dei|delle|per|non|con|che|nel|nella|dal|dalla|sono|una|"
    r"questo|questa|anche|senza|nessun|nessuna|manca|deve|oppure|ogni|gia|piu|puo)\b(?!-)"
    r"|[aeiou]'(\s|$)|[àèéìòù]", re.I)

#: Punti di uscita verso l'utente: log e diario, avanzamento, errori.
_SINKS = {"_append_log", "_on_ai_progress", "log", "progress", "warn", "note", "report",
          "insertPlainText", "_on_extract_error", "_on_error", "_set_3d_status"}

#: Moduli che NON passano da tx di proposito: girano dentro FreeCAD (usano
#: `_warn`), o SONO il meccanismo.
_EXEMPT_FILES = {"freecad_build_body.py", "freecad_parts.py", "messages.py", "i18n.py"}


def _literal(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
    return None


def untranslated_exits(source):
    """@brief Letterali italiani passati direttamente a un punto di uscita, senza tx.

    @param source Codice di un modulo.
    @return Elenco di (riga, funzione, testo).
    """
    out = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
        exception = name.endswith("Error") or name in ("ReviewCancelled", "Exception")
        if name not in _SINKS and not exception:
            continue
        for arg in node.args:
            parts = [arg.body, arg.orelse] if isinstance(arg, ast.IfExp) else [arg]
            for part in parts:
                text = _literal(part)
                if text and _ITALIAN.search(text):
                    out.append((node.lineno, name, text[:80]))
    return out


def test_the_exit_scan_tells_italian_literals_from_translated_ones():
    """@brief Il rivelatore distingue per struttura: letterale diretto no, tx(...) si'."""
    wrong = 'self._append_log(f"Progetto aperto: {folder} con le impostazioni")\n' \
            'raise ValueError("Il nome non puo\' essere vuoto")\n'
    right = 'self._append_log(tx("Progetto aperto: {folder}", folder=folder))\n' \
            'raise ValueError(tx("Il nome non puo\' essere vuoto"))\n' \
            'raise ValueError(f"Unknown photo state: {state!r}")\n' \
            'raise ValueError("Non-finite JSON constant")\n' \
            'raise ValueError("model task metadata must be \'detect\'")\n'
    assert len(untranslated_exits(wrong)) == 2
    assert untranslated_exits(right) == []


def test_no_exit_point_receives_an_italian_literal():
    """@brief Positivo: in tutto il pacchetto nessun log/errore italiano scavalca tx."""
    found = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if path.name in _EXEMPT_FILES:
            continue
        found += [f"{path.name}:{line} {name}: {text!r}"
                  for line, name, text in untranslated_exits(path.read_text(encoding="utf-8"))]
    assert not found, "messaggi italiani senza tx:\n" + "\n".join(found)


# ── Nella finestra vera, in inglese ──────────────────────────────────────

def test_the_log_and_an_error_come_out_in_english(tmp_path):
    """@brief Positivo: in inglese la riga di log e l'errore di un servizio sono inglesi.

    Negativo: in italiano le stesse frasi restano italiane.
    """
    from kicadgen.generators.pin_header import PinHeaderParams
    from kicadgen.services.analysis_profiles import ProfileStore
    from kicadgen.services.i18n import I18n

    i18n = I18n(ProfileStore(tmp_path / "p.sqlite3"), "en")
    set_translator(i18n.raw)
    with pytest.raises(ValueError, match="The pitch must be > 0."):
        PinHeaderParams(name="X", rows=1, cols=2, pitch=0).validate()
    assert tx("Nuovo progetto: {project_dir}", project_dir="D") == "New project: D"
    i18n.set_language("it")
    assert tx("Nuovo progetto: {project_dir}", project_dir="D") == "Nuovo progetto: D"
    with pytest.raises(ValueError, match=r"Il passo \(pitch\) deve essere > 0\."):
        PinHeaderParams(name="X", rows=1, cols=2, pitch=0).validate()


@pytest.fixture
def english_window(request):
    """Finestra dei test di GUI, portata in inglese."""
    window = request.getfixturevalue("window")  # noqa: F811
    window.language_combo.setCurrentIndex(window.language_combo.findData("en"))
    return window


def test_a_new_project_is_logged_in_english(english_window, monkeypatch):
    """@brief Positivo: «New project: …» nel log. Negativo: nessun «Nuovo progetto:»."""
    from PyQt6.QtWidgets import QDialog
    from kicadgen.gui.project_dialogs import ProjectNameDialog
    monkeypatch.setattr(gui, "save_config", lambda config: None)

    def accept(self):
        self.field.setText("Englisch")
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(ProjectNameDialog, "exec", accept)
    english_window._new_project()
    log = english_window.ai_progress_text.toPlainText()
    assert "New project: " in log and "Nuovo progetto: " not in log


def test_the_documents_list_has_no_key_as_tooltip(english_window):
    """@brief Regressione del «ui:»: a progetto aperto il tooltip della lista e' vuoto."""
    english_window.field_name.setText("")
    assert english_window.docs_list.toolTip() == "Create or open a project first (File menu)."
    english_window.field_name.setText("Aperto")
    assert english_window.docs_list.toolTip() == ""
