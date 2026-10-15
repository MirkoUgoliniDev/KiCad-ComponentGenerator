"""@brief «Controlli KiCad (KLC)» row of «Metadati del componente» (user, 2026-10-15, option 3).

The user found the old «Regole KLC» row unclear. Decision: the label reads
«Controlli KiCad (KLC):»; an empty list shows one non-alarming row «Nessun
problema trovato» that is not a finding; every finding has a tooltip with its
remedy (what to change and where), written next to the rule in
`klc_checks.FIX_HINTS`. The structural guard enumerates every code the module
can produce, from its source, and fails when one has no remedy.
"""
import ast
import inspect

import pytest
from PyQt6.QtWidgets import QMessageBox

from kicadgen.services import klc_checks
from kicadgen.services.i18n import DEFAULT_TRANSLATIONS
from tests.test_photo_inventory_gui import gui
from tests.test_required_metadata import NAME, _open

ROLE = gui.MainWindow.FINDING_CODE_ROLE


# ── Structural guard: every code has a remedy ───────────────────────────
#
# Review pass 2 (M4): the first scanner SKIPPED the call shapes it did not
# recognise (an f-string code, `code=` as a keyword, a direct `Finding(...)`,
# a code held in a variable), so a new rule written that way slipped through
# with no remedy. This scanner is strict: every call that can create a
# finding must resolve to literal codes or to a known pass-through, and any
# other shape is an ERROR naming the line, never a silent skip.

#: Functions of klc_checks that receive a finding code: name -> position of
#: the code argument when given positionally (it may also come as `code=`).
CODE_SINKS = {'_error': 0, '_warning': 0, 'Finding': 0, '_parse': 1, 'name_findings': 1}
#: The sinks whose codes become findings (the others forward their code).
FINDING_FACTORIES = ('_error', '_warning', 'Finding')


class UnresolvedCode(AssertionError):
    """A call creates a finding whose code the scanner cannot read statically."""


def _code_argument(call: ast.Call, sink: str):
    """The expression passed as `code` to a sink call, or None when absent."""
    for keyword in call.keywords:
        if keyword.arg == 'code':
            return keyword.value
    index = CODE_SINKS[sink]
    return call.args[index] if len(call.args) > index else None


def _enclosing_functions(tree) -> dict:
    """Map every node to its innermost enclosing function definition."""
    owner = {}
    for definition in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        for node in ast.walk(definition):
            owner[node] = definition       # BFS: inner definitions come later and win
    return owner


def scan_codes(source: str) -> set[str]:
    """Every finding code a klc_checks-like module can emit, read from its source.

    @param source Python source text.
    @return The codes of every `_error`/`_warning`/`Finding` call, with the
        pass-throughs resolved: a sink whose own parameter `code` is forwarded
        (`_error(code, …)` inside `_parse`) or extended with a literal suffix
        (`_error(code + '_empty', …)` inside `name_findings`) gets the codes of
        its call sites.
    @throws UnresolvedCode For any sink call whose code is not a literal or one
        of those pass-throughs, and for a sink used as a value (an alias would
        hide its calls from the scan).
    """
    tree = ast.parse(source)
    owner = _enclosing_functions(tree)
    called = set()
    # (sink, ('literal', code) | ('pass', forwarding sink, suffix))
    edges = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in CODE_SINKS):
            continue
        called.add(node.func)
        sink = node.func.id
        expression = _code_argument(node, sink)
        where = f"line {node.lineno}: {ast.unparse(node)[:80]}"
        if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
            edges.append((sink, ('literal', expression.value)))
            continue
        function = owner.get(node)
        forwards = (function is not None and function.name in CODE_SINKS
                    and 'code' in [arg.arg for arg in function.args.args + function.args.kwonlyargs])
        if forwards and isinstance(expression, ast.Name) and expression.id == 'code':
            edges.append((sink, ('pass', function.name, '')))
        elif (forwards and isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.Add)
              and isinstance(expression.left, ast.Name) and expression.left.id == 'code'
              and isinstance(expression.right, ast.Constant) and isinstance(expression.right.value, str)):
            edges.append((sink, ('pass', function.name, expression.right.value)))
        else:
            raise UnresolvedCode(f"finding code not readable statically, {where}")
    for node in ast.walk(tree):
        # `make = _error` then `make(...)`: the calls would escape the scan.
        if (isinstance(node, ast.Name) and node.id in CODE_SINKS and node.id != 'Finding'
                and isinstance(node.ctx, ast.Load) and node not in called):
            raise UnresolvedCode(f"line {node.lineno}: sink {node.id} used as a value")
    codes = {sink: set() for sink in CODE_SINKS}
    changed = True
    while changed:          # fixed point: pass-throughs may chain (_parse -> _error -> Finding)
        changed = False
        for sink, (kind, *rest) in edges:
            new = ({rest[0]} if kind == 'literal'
                   else {code + rest[1] for code in codes[rest[0]]})
            if not new <= codes[sink]:
                codes[sink] |= new
                changed = True
    return set().union(*(codes[sink] for sink in FINDING_FACTORIES))


def check_codes_have_hints(source: str, hints: dict) -> set[str]:
    """Scan `source` and fail on an unreadable code or a code without a hint.

    @return The codes found (all with a hint).
    @throws UnresolvedCode See `scan_codes`.
    @throws AssertionError Naming the codes without a remedy.
    """
    codes = scan_codes(source)
    missing = sorted(codes - set(hints))
    if missing:
        raise AssertionError(f"codes without a remedy in klc_checks.FIX_HINTS: {missing}")
    return codes


def _codes_in_source() -> set[str]:
    """Every finding code klc_checks can emit (strict scan of its own source)."""
    return scan_codes(inspect.getsource(klc_checks))


@pytest.mark.parametrize("snippet", [
    "def rule(kind):\n    return [_warning(f'{kind}_too_big', 'm')]\n",
    "def rule():\n    return [_error(code='pin_new_rule', message='m')]\n",
    "def rule():\n    return [Finding('pin_other_rule', 'error', 'm')]\n",
    "def rule():\n    c = 'pin_var_rule'\n    return [_error(c, 'm')]\n",
    "make = _error\ndef rule():\n    return [make('pin_alias_rule', 'm')]\n",
    "def rule(code):\n    return [_error(code, 'm')]\n",   # forwarding from a non-sink function
])
def test_the_guard_fails_on_every_unrecognised_or_unhinted_shape(snippet):
    """Self-test of the scanner (review pass 2, M4): each of these shapes used
    to slip through; now each one fails, either as unreadable or as a code
    without a remedy."""
    with pytest.raises(AssertionError):
        check_codes_have_hints(snippet, klc_checks.FIX_HINTS)


def test_the_guard_accepts_the_known_shapes():
    """Positive self-test: literal codes and the two pass-through forms resolve."""
    snippet = (
        "def _error(code, message):\n    return Finding(code, 'error', message)\n"
        "def name_findings(name, *, code, label):\n    return [_error(code + '_empty', 'm')]\n"
        "def rule():\n    return [_error('pin_grid', 'm'), *name_findings('n', code='symbol_name', label='l')]\n")
    assert check_codes_have_hints(snippet, klc_checks.FIX_HINTS) == {'pin_grid', 'symbol_name_empty'}


def test_the_source_scan_sees_every_kind_of_code():
    """Negative of the guard: a scan that found nothing would pass vacuously."""
    codes = _codes_in_source()
    assert {'pin_grid', 'datasheet_empty', 'symbol_unreadable', 'footprint_unreadable',
            'footprint_name_illegal', 'symbol_name_spaces', 'metadata_type'} <= codes
    assert len(codes) >= 40


def test_every_code_has_a_remedy_and_every_remedy_a_code():
    codes = _codes_in_source()
    missing = sorted(codes - set(klc_checks.FIX_HINTS))
    assert not missing, f"codes without a remedy in klc_checks.FIX_HINTS: {missing}"
    stale = sorted(set(klc_checks.FIX_HINTS) - codes)
    assert not stale, f"remedies of codes no rule produces: {stale}"


@pytest.mark.parametrize("language", ["it", "en", "fr", "de"])
def test_every_remedy_is_in_the_catalogue_in_four_languages(language):
    for source in set(klc_checks.FIX_HINTS.values()):
        entry = DEFAULT_TRANSLATIONS.get("ui:" + source)
        assert entry and entry.get(language, "").strip(), (language, source)
        assert "{" not in entry[language], "remedies have no placeholders"


def test_the_findings_carry_their_remedy():
    """Runtime side: the checks attach the remedy of the code to each finding."""
    findings = klc_checks.check_all({}, 'A/B', symbol_text='(kicad_symbol_lib)',
                                    footprint_text='(footprint "A/B")')
    assert findings
    for finding in findings:
        assert finding.hint and finding.hint == klc_checks.fix_hint(finding.code), finding.code
    assert klc_checks.fix_hint("not-a-code") == ""


def test_a_remedy_says_where_to_act():
    """Spot checks of the content: the metadata remedies name their field, the
    file remedies name the tab or the command that fixes them."""
    hints = klc_checks.FIX_HINTS
    assert "«Descrizione»" in hints['description_empty'] and "«Metadati del componente»" in hints['description_empty']
    assert "«Datasheet (URL o file)»" in hints['datasheet_empty']
    assert "«Simbolo elettrico»" in hints['pin_grid']
    assert "«Applica ai file»" in hints['footprint_courtyard']
    assert "«Rinomina progetto...»" in hints['footprint_name_illegal']


# ── GUI: label, empty state, tooltips ───────────────────────────────────

def _rows(window):
    return [window.klc_list.item(i) for i in range(window.klc_list.count())]


def test_the_label_is_renamed_everywhere(window):  # noqa: F811
    assert window.label_klc.text() == "Controlli KiCad (KLC):"
    assert window.METADATA_HELP_TITLES["klc"] == "Controlli KiCad (KLC)"
    assert "Controlli KiCad (KLC)" in window._metadata_help_buttons["klc"].toolTip()
    assert "Passa il mouse su una riga" in window.label_klc.toolTip()
    assert "controlli KiCad (KLC)" in window.group_metadata.toolTip()
    assert "Regole KLC:" not in window.label_klc.text()


def test_without_findings_one_neutral_row_that_counts_for_nothing(window, tmp_path, monkeypatch):  # noqa: F811
    _open(window, tmp_path)
    monkeypatch.setattr(klc_checks, "check_all", lambda *a, **k: [])
    window._refresh_klc_findings()
    (row,) = _rows(window)
    assert row.text() == "Nessun problema trovato"
    assert row.data(ROLE) is None, "the empty-state row is not a finding"
    assert not row.text().startswith(("⛔", "⚠"))
    # Not counted anywhere: no finding, no missing field, installation open,
    # no KLC question.
    assert window._klc_findings() == [] and window._missing_required_metadata() == []
    assert window.btn_export.isEnabled() and window.btn_install_global.isEnabled()

    def never(*args, **kwargs):
        raise AssertionError("an empty list must not ask anything")
    monkeypatch.setattr(QMessageBox, "question", never)
    assert window._confirm_klc_errors() is True


def test_with_findings_the_neutral_row_disappears(window, tmp_path):  # noqa: F811
    """Negative of the empty state: one real finding, no «Nessun problema trovato»."""
    _open(window, tmp_path)
    window.field_meta_description.setText("")
    texts = [row.text() for row in _rows(window)]
    assert "Nessun problema trovato" not in texts
    assert all(row.data(ROLE) for row in _rows(window))


def test_every_finding_row_has_its_remedy_as_tooltip(window, tmp_path):  # noqa: F811
    """Real checker: metadata findings and file findings (a broken symbol)."""
    _open(window, tmp_path, filled=False)
    (tmp_path / NAME / f"{NAME}.kicad_sym").write_text("(kicad_symbol_lib (version 1))", encoding="utf-8")
    window._refresh_klc_findings()
    rows = _rows(window)
    codes = [row.data(ROLE) for row in rows]
    assert {"description_empty", "keywords_empty", "datasheet_empty", "symbol_version"} <= set(codes)
    for row in rows:
        code = row.data(ROLE)
        assert row.toolTip() == klc_checks.fix_hint(code) and row.toolTip(), code
        assert row.toolTip() != row.text(), "the tooltip explains, it does not repeat the message"


def test_a_finding_without_a_hint_falls_back_to_the_table(window, tmp_path, monkeypatch):  # noqa: F811
    _open(window, tmp_path)
    monkeypatch.setattr(klc_checks, "check_all", lambda *a, **k: [
        klc_checks.Finding("pin_grid", "error", "Pin fuori griglia")])
    window._refresh_klc_findings()
    (row,) = _rows(window)
    assert row.toolTip() == klc_checks.fix_hint("pin_grid")


def test_the_language_change_retranslates_label_rows_and_tooltips(window, tmp_path, monkeypatch):  # noqa: F811
    _open(window, tmp_path)
    window.field_meta_keywords.setText("")
    window._on_language_changed(window.language_combo.findData("en"))
    try:
        assert window.label_klc.text() == "KiCad checks (KLC):"
        (row,) = [row for row in _rows(window) if row.data(ROLE) == "keywords_empty"]
        assert row.text().startswith("⛔ Empty keywords"), row.text()
        assert row.toolTip().startswith("Write a few space-separated words"), row.toolTip()
        assert "«Keywords»" in row.toolTip()
        monkeypatch.setattr(klc_checks, "check_all", lambda *a, **k: [])
        window._refresh_klc_findings()
        (row,) = _rows(window)
        assert row.text() == "No problems found" and row.data(ROLE) is None
    finally:
        window._on_language_changed(window.language_combo.findData("it"))
    assert window.label_klc.text() == "Controlli KiCad (KLC):"
    assert _rows(window)[0].text() == "Nessun problema trovato"


def test_the_codes_that_stop_applica_are_real_and_never_sent_to_applica():
    """Review 2026-10-15, R2-M1: `WRITE_BLOCKING_CODES` names only codes the
    module emits, and none of their remedies is «Applica ai file» itself —
    otherwise the remedy would be the command that refuses it."""
    codes = _codes_in_source()
    assert klc_checks.WRITE_BLOCKING_CODES <= codes
    for code in klc_checks.WRITE_BLOCKING_CODES:
        assert "«Applica ai file»" not in klc_checks.FIX_HINTS[code], code
    # Negative: the file-level codes repaired by the regeneration are not in it.
    assert not {'footprint_version', 'footprint_format', 'footprint_unreadable',
                'pin_grid', 'symbol_version'} & klc_checks.WRITE_BLOCKING_CODES
