"""@brief Every check that can block is declared, with when it applies and what concludes it (review 2026-11-15, round 3, item 2).

@details `REQUIRED_FOR_INSTALL` was hand-picked: checks that emit ERROR (the
document pinout, the NC/NO roles, the 3D import, the applied symbol's read)
were missing, so a run interrupted before them could be «crashed»
(installation allowed). Now `BLOCKING_CHECKS` declares each of them. An AST
scan finds every ERROR this module can emit -- ``add('ERROR', ...)`` and
``Check('ERROR', ...)``, a status held in a variable, a code passed through
a parameter or built as ``f'{prefix}_{kind}'`` -- and requires each code in
the table or in the documented `ERROR_CODES_NOT_REQUIRED`, so a future
check cannot be forgotten. A dynamic test interrupts real runs after every
row and checks the declared conclusions: a check counted as concluded has
already published every ERROR row the complete run gives it. Positive and
negative twins.
"""

import ast
import fnmatch
import subprocess
from pathlib import Path

import pytest

from kicadgen.generators.symbol import serialize_symbol, symbol_inputs
from kicadgen.services import component_check as check
from test_kicad_library import _artifacts

MODULE = Path(check.__file__)


# ── the AST scan ────────────────────────────────────────────────────────

#: Positional index of the `code` argument of `Check(...)` (status, title,
#: detail, category, importance, remedy, action, code, ...).
_CHECK_CODE_INDEX = 7
#: Functions whose `Check(...)` only forwards what their callers pass: their
#: callers are the emission sites (`add` inside `verify_component`).
_WRAPPERS = {'add'}


def _parents(tree):
    """{node: parent} of an AST."""
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _enclosing_function(node, parents):
    """The innermost function definition around a node, or None."""
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    return None


def _may_be_error(status):
    """True when a status expression can be 'ERROR': the literal, or anything not a literal."""
    if isinstance(status, ast.Constant):
        return status.value == 'ERROR'
    constants = [node.value for node in ast.walk(status) if isinstance(node, ast.Constant)]
    if constants and all(isinstance(value, str) for value in constants) and isinstance(
            status, (ast.IfExp, ast.BoolOp)):
        return 'ERROR' in constants
    return True


def _resolve(expr, function, tree, parents, depth=0):
    """The set of codes (or ``prefix*`` families) an expression can hold.

    A literal is itself; ``f'{a}_{b}'`` is the prefix resolved from its first
    part plus ``*``; a parameter of the enclosing function is resolved from
    every call of that function in the module (by position or keyword).
    Anything else fails: an emission site must be resolvable.
    """
    assert depth < 6, ast.dump(expr)
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return {expr.value}
    if isinstance(expr, ast.JoinedStr):
        head = expr.values[0]
        inner = head.value if isinstance(head, ast.FormattedValue) else head
        rest = expr.values[1] if len(expr.values) > 1 else None
        separator = rest.value if isinstance(rest, ast.Constant) else ''
        return {f'{prefix}{separator}*' for prefix in _resolve(inner, function, tree, parents, depth + 1)}
    assert isinstance(expr, ast.Name) and function is not None, (
        f'unresolvable code at line {getattr(expr, "lineno", "?")}: {ast.dump(expr)}')
    params = [arg.arg for arg in function.args.posonlyargs + function.args.args]
    keyword_only = [arg.arg for arg in function.args.kwonlyargs]
    assert expr.id in params + keyword_only, (
        f'code «{expr.id}» at line {expr.lineno} is not a parameter of {function.name}')
    position = params.index(expr.id) if expr.id in params else None
    codes = set()
    for call in ast.walk(tree):
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id == function.name):
            continue
        given = next((kw.value for kw in call.keywords if kw.arg == expr.id), None)
        if given is None and position is not None and position < len(call.args):
            given = call.args[position]
        if given is None:
            default = _default(function, expr.id)
            if default is None:
                continue
            given = default
        codes |= _resolve(given, _enclosing_function(call, parents), tree, parents, depth + 1)
    return codes


def _default(function, name):
    """The default value node of a parameter, or None."""
    args = function.args
    positional = args.posonlyargs + args.args
    for arg, default in zip(positional[len(positional) - len(args.defaults):], args.defaults):
        if arg.arg == name:
            return default
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        if arg.arg == name:
            return default
    return None


def _emitted_error_codes(source):
    """{code or family: [line, ...]} of every ERROR the source can emit."""
    tree = ast.parse(source)
    parents = _parents(tree)
    found = {}
    for call in ast.walk(tree):
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id in ('add', 'Check') and call.args):
            continue
        function = _enclosing_function(call, parents)
        if function is not None and function.name in _WRAPPERS:
            continue
        if not _may_be_error(call.args[0]):
            continue
        code = next((kw.value for kw in call.keywords if kw.arg == 'code'), None)
        if code is None and call.func.id == 'Check' and len(call.args) > _CHECK_CODE_INDEX:
            code = call.args[_CHECK_CODE_INDEX]
        codes = {''} if code is None else _resolve(code, function, tree, parents)
        for value in codes:
            found.setdefault(value, []).append(call.lineno)
    return found


def _declared(code):
    """True when an ERROR code is in a `BlockingCheck.errors` (exact or family) or excluded."""
    errors = set().union(*(entry.errors for entry in check.BLOCKING_CHECKS.values()))
    return code in errors or code in check.ERROR_CODES_NOT_REQUIRED


def test_every_error_the_module_can_emit_is_declared():
    """@brief Positive: each ERROR code found by the scan is in the table or the documented
    exclusions; the scan finds the sites the table was built from (a sanity floor)."""
    found = _emitted_error_codes(MODULE.read_text(encoding='utf-8'))
    undeclared = {code: lines for code, lines in found.items() if not _declared(code)}
    assert not undeclared, f'ERROR codes in neither BLOCKING_CHECKS nor ERROR_CODES_NOT_REQUIRED: {undeclared}'
    for code in ('document_dropped', 'document_unnamed', 'pin_role_contradiction', 'kicad_3d_import',
                 'kicad_symbol_read', 'drc_*', 'model_link', 'kicad_version', 'component_name'):
        assert code in found, code


def test_a_forgotten_error_site_is_caught():
    """@brief Negative twin: a new check emitting an undeclared ERROR, through a literal, a
    parameter or a prefix, is found and reported by the same scan."""
    source = MODULE.read_text(encoding='utf-8') + '''

def _new_check(code_name):
    return [Check('ERROR', 't', 'd', 'technical', 1, '', None, code_name)]


def _caller():
    _new_check('brand_new_blocker')


def verify_like():
    def add(status, title, detail, *, code=''):
        pass
    add('ERROR', 't', 'd', code='another_new_blocker')
    add('WARN' if True else 'ERROR', 't', 'd', code='third_new_blocker')
'''
    found = _emitted_error_codes(source)
    for code in ('brand_new_blocker', 'another_new_blocker', 'third_new_blocker'):
        assert code in found and not _declared(code), code


def test_the_exclusions_are_real_and_reasoned():
    """@brief Negative: every exclusion carries a reason, and no code is both required and
    excluded; the ERC really never says ERROR (the reason of «erc_*»)."""
    assert all(reason.strip() for reason in check.ERROR_CODES_NOT_REQUIRED.values())
    errors = set().union(*(entry.errors for entry in check.BLOCKING_CHECKS.values()))
    assert not errors & set(check.ERROR_CODES_NOT_REQUIRED)
    contact = {'type': 'pin_to_pin', 'severity': 'error',
               'items': [{'uuid': 'a'}, {'uuid': 'b'}], 'description': 'x'}
    rows, _power = check.erc_checks({'sheets': [{'violations': [contact]}]}, {'a': '1', 'b': '2'})
    assert rows and all(row.status != 'ERROR' for row in rows)


#: The checks the run's last row does NOT conclude (round 4, item 7): their own
#: row is published whenever they are made, and its SKIP or absence means they
#: were not made (a footprint held by another program, no readable footprint,
#: the saved symbol held).
NOT_CONCLUDED_BY_THE_RUN_END = {'footprint_file', 'pin_pad', 'kicad_symbol_file_read'}


def test_every_condition_is_one_the_caller_declares():
    """@brief Negative: no entry depends on a condition `required_for_install` cannot set, or
    needs a check that is not declared.
    CONTRACT CHANGED 2026-11-15 (round 4, item 7): the run's last row concludes every check
    except those whose own row must be an outcome (`NOT_CONCLUDED_BY_THE_RUN_END`); those
    declare their own row and refuse its SKIP, or are reads that refuse a «file in uso» SKIP."""
    for code, entry in check.BLOCKING_CHECKS.items():
        assert set(entry.applies) <= set(check.CONDITIONS), code
        assert entry.needs <= set(check.BLOCKING_CHECKS), code
        assert entry.own <= entry.concluded_by, code
        if code in NOT_CONCLUDED_BY_THE_RUN_END:
            assert 'manual_checks' not in entry.concluded_by and entry.own == {code}, code
        else:
            assert 'manual_checks' in entry.concluded_by, code


# ── the conditions ──────────────────────────────────────────────────────

def test_document_condition_follows_what_document_checks_judges():
    """@brief Positive: a side-numbered pinout applies; negatives: no document, another layout,
    an empty pinout -- exactly when `document_checks` says nothing."""
    pinout = {'pinout': [{'pin': '1', 'name': 'A'}]}
    assert check.document_check_applies(pinout)
    for document in (None, {}, dict(pinout, layout='grid'), {'pinout': []}):
        assert not check.document_check_applies(document)
        assert check.document_checks(document, [{'pin': '1', 'name': 'A'}], {'1'}) == []


def test_contacts_condition_follows_the_declared_functions():
    """@brief Positive: a row declaring NC applies; negative: an IC pin NAMED «NC» without a
    switch function does not (it is «not connected»)."""
    assert check.declares_contacts([{'pin': '1', 'name': 'NC', 'function': 'nc'}])
    assert not check.declares_contacts([{'pin': '1', 'name': 'NC'}])
    assert not check.declares_contacts(None)


def test_the_new_flags_add_only_their_checks():
    """@brief Positive: each flag adds its own codes; negative: the defaults keep the set a
    caller of round 2 obtained plus the always-applicable blockers."""
    base = dict(cli_available=True, has_step=True, has_symbol_file=True)
    plain = check.required_for_install(**base)
    assert not plain & {'document_dropped', 'document_unnamed', 'pin_role_contradiction', 'lead_hole'}
    assert check.required_for_install(**base, has_document=True) - plain == {
        'document_dropped', 'document_unnamed'}
    assert check.required_for_install(**base, has_contacts=True) - plain == {'pin_role_contradiction'}
    assert check.required_for_install(**base, has_pad_leads=True) - plain == {'lead_hole'}
    assert check.required_reads(cli_available=True, has_symbol_file=True) == check.INSTALLED_FILE_READS
    assert check.required_reads(cli_available=False, has_symbol_file=True) == frozenset()


# ── the declared conclusions, on interrupted real runs ──────────────────

ROWS = [{'pin': '1', 'name': ''}, {'pin': '2', 'name': 'NC', 'function': 'no'},
        {'pin': '3', 'name': 'COM', 'function': 'common'}]

#: A THT footprint whose pad 1 hole is too small for its declared terminal
#: and whose pads 2 and 3 overlap: several ERRORs in the footprint stage.
FOOTPRINT = '''(footprint "Demo"
\t(version 20260206)
\t(generator "kicadgen_tool")
\t(layer "F.Cu")
\t(pad "1" thru_hole circle (at 0 0) (size 1.6 1.6) (drill 0.5) (layers "*.Cu" "*.Mask"))
\t(pad "2" smd rect (at 5 0) (size 2 2) (layers "F.Cu" "F.Paste" "F.Mask"))
\t(pad "3" smd rect (at 6 0) (size 2 2) (layers "F.Cu" "F.Paste" "F.Mask"))
)
'''


def _fake_cli(monkeypatch, codes):
    """kicad-cli faked: per kind of read an exit code for EVERY call (controls included)."""
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: 'kicad-cli')
    monkeypatch.setattr(check, 'check_kicad_version',
                        lambda cli: type('V', (), {'supported': True, 'newer_cli': None})())

    def run(command, **_kwargs):
        args = [str(a) for a in command[1:]]
        output = Path(args[args.index('-o') + 1])
        control = any(part.startswith('control-') for arg in args for part in Path(arg).parts)
        kind = {('fp', 'export'): 'fp', ('sym', 'export'): 'sym',
                ('pcb', 'export'): 'step'}.get(tuple(args[:2]), args[1])
        code = 0 if control else codes.get(kind, 0)
        if args[:2] == ['pcb', 'drc']:
            output.write_text('{"violations": []}', encoding='utf-8')
        elif args[:2] == ['sch', 'erc']:
            output.write_text('{"sheets": []}', encoding='utf-8')
        elif code == 0 and kind == 'step':
            output.write_text('MANIFOLD_SOLID_BREP(', encoding='utf-8')
        elif code == 0:
            (output / 'out.svg').write_text('<svg/>', encoding='utf-8')
        return subprocess.CompletedProcess(command, code, '', '')
    monkeypatch.setattr(check, '_run_cli', run)


class _Stop(Exception):
    """An interruption of the run after a given number of rows."""


def _run(art, stop_after=None):
    """The rows of one run of the blocking scenario, interrupted after `stop_after` rows."""
    emitted = []

    def emit(result):
        emitted.append(result)
        if stop_after is not None and len(emitted) >= stop_after:
            raise _Stop()
    kwargs = dict(
        emit=emit, pad_leads={'1': {'lead': {'shape': 'round', 'diameter_mm': 0.8}}},
        document={'pinout': [{'pin': '1', 'name': 'VCC'}, {'pin': '9', 'name': 'X'}],
                  'shifted': True},
        contact_witness='single')
    try:
        check.verify_component(art.component_dir, 'Demo', ROWS, **kwargs)
    except _Stop:
        pass
    return emitted


FLAGS = dict(cli_available=True, has_step=True, has_symbol_file=True, has_document=True,
             has_contacts=True, has_pad_leads=True)


def _blocking_component(tmp_path):
    """The test component with the blocking footprint and a saved symbol."""
    art = _artifacts(tmp_path)
    art.footprint_path.write_text(FOOTPRINT, encoding='utf-8')
    pins, body = symbol_inputs([dict(row, name=row['name'] or 'P') for row in ROWS])
    (art.component_dir / 'Demo.kicad_sym').write_text(
        serialize_symbol('Demo', pins, footprint='KiCadGen:Demo', body=body), encoding='utf-8')
    return art


@pytest.mark.parametrize('codes', [{}, {'fp': 2, 'sym': 3, 'step': 2}])
def test_a_concluded_check_has_published_all_its_errors(tmp_path, monkeypatch, codes):
    """@brief Positive: a complete run concludes every requirement, and its ERRORs cover many
    checks; for EVERY interruption point, a check counted as concluded has already published
    every ERROR row the complete run gives it (the declared stage closers hold)."""
    art = _blocking_component(tmp_path)
    _fake_cli(monkeypatch, codes)
    full = _run(art)
    assert check.missing_required(full, **FLAGS) == frozenset()
    errors = {r.code for r in full if r.status == 'ERROR'}
    assert {'document_dropped', 'document_unnamed', 'pin_role_contradiction', 'lead_hole',
            'footprint_pads_overlap'} <= errors, errors
    if codes:
        assert {'kicad_footprint_read', 'kicad_symbol_read', 'kicad_3d_import'} <= errors
    for stop in range(1, len(full)):
        prefix = _run(art, stop)
        assert [(r.code, r.status) for r in prefix] == [(r.code, r.status) for r in full[:stop]]
        for requirement in check.concluded_required(prefix, **FLAGS):
            owned = check.BLOCKING_CHECKS[requirement].errors
            later = [r.code for r in full[stop:] if r.status == 'ERROR'
                     and any(fnmatch.fnmatchcase(r.code, pattern) for pattern in owned)]
            assert not later, (stop, requirement, later)


def test_an_interrupted_run_misses_the_checks_it_did_not_reach(tmp_path, monkeypatch):
    """@brief Negative twin: interrupted right after the footprint stage, the document, contact,
    symbol and STEP checks are missing (the run cannot be called «crashed» on them)."""
    art = _blocking_component(tmp_path)
    _fake_cli(monkeypatch, {})
    full = _run(art)
    stop = next(index for index, row in enumerate(full) if row.code == 'kicad_footprint_read') + 1
    missing = check.missing_required(_run(art, stop), **FLAGS)
    assert {'document_dropped', 'document_unnamed', 'pin_role_contradiction', 'pin_pad',
            'kicad_symbol_read', 'step_integrity', 'kicad_3d_import', 'drc'} <= missing
    assert not missing & {'footprint_file', 'footprint_pads_overlap', 'lead_hole', 'model_link',
                          'kicad_footprint_read'}
