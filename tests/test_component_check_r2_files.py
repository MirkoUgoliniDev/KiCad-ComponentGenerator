"""@brief Component problems behind a kicad-cli exit 0, and the BOM of the copies (review 2026-11-15, round 2).

@details R10: kicad-cli 10 `sym export svg` exits 0 on a library holding no
symbol (no output) or another symbol (another drawing); the check's own
parser (`symbol_library_problem`) says what the component lacks instead of
«not run» or OK. R11: a footprint (and a saved symbol) starting with a UTF-8
BOM is copied for kicad-cli WITHOUT it -- kicad-cli refuses such a file
(exit 2, measured) and the installation drops it, so an ERROR would block a
file the install repairs. Everything runs with a faked kicad-cli. Positive
and negative twins.
"""

import subprocess
from pathlib import Path

from kicadgen.generators.symbol import serialize_symbol, symbol_inputs
from kicadgen.services import component_check as check
from kicadgen.services.kicad_library import UTF8_BOM
from test_kicad_library import _artifacts

ROWS = [{'pin': '1', 'name': 'GPIO1'}]
EMPTY_LIBRARY = '(kicad_symbol_lib (version 20241209) (generator "x"))\n'


def _symbol_text(name='Demo'):
    """A symbol library holding one symbol as the generator writes it."""
    pins, body = symbol_inputs(ROWS)
    return serialize_symbol(name, pins, footprint=f'KiCadGen:{name}', body=body)


def _fake_cli(monkeypatch, *, produce, inputs=None):
    """kicad-cli faked: every command exits 0; `produce(kind)` says whether the read writes an output.

    @param inputs Receives {kind: [bytes of the input file handed over]}.
    """
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: 'kicad-cli')
    monkeypatch.setattr(check, 'check_kicad_version',
                        lambda cli: type('V', (), {'supported': True, 'newer_cli': None})())

    def run(command, **_kwargs):
        args = [str(a) for a in command[1:]]
        kind = args[0] if args[1] == 'export' else args[1]
        output = Path(args[args.index('-o') + 1])
        if inputs is not None and args[1] == 'export':
            source = Path(args[-1])
            source = source / 'Demo.kicad_mod' if source.is_dir() else source
            inputs.setdefault(kind, []).append(source.read_bytes())
        if kind == 'drc':
            output.write_text('{"violations": []}', encoding='utf-8')
        elif kind == 'erc':
            output.write_text('{"sheets": []}', encoding='utf-8')
        elif kind == 'pcb':
            output.write_text('MANIFOLD_SOLID_BREP(', encoding='utf-8')
        elif produce(kind):
            (output / 'out.svg').write_text('<svg/>', encoding='utf-8')
        return subprocess.CompletedProcess(command, 0, '', '')
    monkeypatch.setattr(check, '_run_cli', run)


def _saved_read(tmp_path, monkeypatch, saved_text, produce):
    """The «Simbolo su disco» row for a saved .kicad_sym holding `saved_text`."""
    art = _artifacts(tmp_path)
    (art.component_dir / 'Demo.kicad_sym').write_text(saved_text, encoding='utf-8')
    _fake_cli(monkeypatch, produce=produce)
    (row,) = [r for r in check.verify_component(art.component_dir, 'Demo', ROWS)
              if r.code == 'kicad_symbol_file_read']
    return row


# ── R10: the parser of the check ────────────────────────────────────────

def test_the_parser_names_what_the_library_lacks():
    """@brief Positive: no symbol -> a detail; another name -> a detail naming it."""
    assert check.symbol_library_problem(EMPTY_LIBRARY, 'Demo')
    other = check.symbol_library_problem(_symbol_text('Other'), 'Demo')
    assert other and 'Other' in other and 'Demo' in other


def test_the_parser_is_silent_on_the_components_own_symbol():
    """@brief Negative twins: the component's symbol -> None; a text it cannot walk -> None
    (kicad-cli's own verdict then stands)."""
    assert check.symbol_library_problem(_symbol_text('Demo'), 'Demo') is None
    assert check.symbol_library_problem('(kicad_symbol_lib (symbol "Demo"', 'Demo') is None


# ── R10: exit 0 on a library that lacks the symbol ──────────────────────

def test_an_empty_saved_library_is_the_components_problem_not_not_run(tmp_path, monkeypatch):
    """@brief Positive: exit 0, no output, library holding no symbol -> WARN 2 with the re-export
    action (the installation ships this file), never SKIP «not run» with «ripeti»."""
    row = _saved_read(tmp_path, monkeypatch, EMPTY_LIBRARY, produce=lambda kind: kind != 'sym')
    assert row.status == 'WARN' and row.importance == check.IMPORTANCE_BEFORE_PRODUCTION
    assert row.action == check.ACTION_REEXPORT_SYMBOL


def test_exit_0_without_output_on_a_sound_library_stays_not_run(tmp_path, monkeypatch):
    """@brief Negative twin: the library holds the symbol, kicad-cli still writes nothing -> a
    failure of the tool: SKIP «not run» with its remedy."""
    row = _saved_read(tmp_path, monkeypatch, _symbol_text('Demo'), produce=lambda kind: kind != 'sym')
    assert row.status == 'SKIP' and row.remedy


def test_a_saved_library_holding_another_symbol_is_not_ok(tmp_path, monkeypatch):
    """@brief Positive: kicad-cli draws the other symbol (exit 0 with an output) -> WARN 2, not OK."""
    row = _saved_read(tmp_path, monkeypatch, _symbol_text('Other'), produce=lambda kind: True)
    assert row.status == 'WARN' and row.action == check.ACTION_REEXPORT_SYMBOL


def test_a_saved_library_holding_the_symbol_is_ok(tmp_path, monkeypatch):
    """@brief Negative twin: the component's own symbol, read -> OK."""
    row = _saved_read(tmp_path, monkeypatch, _symbol_text('Demo'), produce=lambda kind: True)
    assert row.status == 'OK'


def test_an_applied_symbol_without_the_component_is_an_error(tmp_path, monkeypatch):
    """@brief Positive: the applied symbol (what the preview and the export use) holds another
    name -> ERROR importance 1 on «Simbolo applicato»; its twin with the right name -> OK."""
    art = _artifacts(tmp_path)
    _fake_cli(monkeypatch, produce=lambda kind: True)

    def applied_row(text):
        (row,) = [r for r in check.verify_component(art.component_dir, 'Demo', ROWS,
                                                    applied_symbol=text)
                  if r.code == 'kicad_symbol_read']
        return row
    wrong = applied_row(_symbol_text('Other'))
    assert wrong.status == 'ERROR' and wrong.importance == check.IMPORTANCE_BLOCKS_USE
    assert applied_row(_symbol_text('Demo')).status == 'OK'


# ── R11: the BOM is not handed to kicad-cli ─────────────────────────────

def test_a_footprint_with_a_bom_is_copied_without_it(tmp_path, monkeypatch):
    """@brief Positive: the file starts with EF BB BF -> the copy kicad-cli reads is the same bytes
    without the BOM (newlines kept), the read is OK, and the on-disk WARN names the BOM."""
    art = _artifacts(tmp_path)
    original = art.footprint_path.read_bytes().replace(b'\n', b'\r\n')
    art.footprint_path.write_bytes(UTF8_BOM + original)
    inputs = {}
    _fake_cli(monkeypatch, produce=lambda kind: True, inputs=inputs)
    results = check.verify_component(art.component_dir, 'Demo', ROWS)
    assert inputs['fp'] == [original]
    assert [r.status for r in results if r.code == 'kicad_footprint_read'] == ['OK']
    assert [r.status for r in results if r.code == 'footprint_bom'] == ['WARN']


def test_a_footprint_without_a_bom_is_copied_as_it_is(tmp_path, monkeypatch):
    """@brief Negative twin: no BOM -> the copy holds exactly the file's bytes, no BOM row."""
    art = _artifacts(tmp_path)
    original = art.footprint_path.read_bytes()
    inputs = {}
    _fake_cli(monkeypatch, produce=lambda kind: True, inputs=inputs)
    results = check.verify_component(art.component_dir, 'Demo', ROWS)
    assert inputs['fp'] == [original]
    assert not any(r.code == 'footprint_bom' for r in results)


def test_a_saved_symbol_with_a_bom_is_copied_without_it_and_not_stale(tmp_path, monkeypatch):
    """@brief Positive (same class, R11): the saved .kicad_sym starts with a BOM -> kicad-cli gets
    it without the BOM, and a BOM alone is not «differs from the applied symbol» (the
    installation drops it); negative twin: a real difference stays «stale»."""
    art = _artifacts(tmp_path)
    saved = art.component_dir / 'Demo.kicad_sym'
    text = _symbol_text('Demo')
    saved.write_bytes(UTF8_BOM + text.encode('utf-8'))
    inputs = {}
    _fake_cli(monkeypatch, produce=lambda kind: True, inputs=inputs)
    results = check.verify_component(art.component_dir, 'Demo', ROWS, applied_symbol=text)
    assert text.encode('utf-8') in inputs['sym'] and not any(
        data.startswith(UTF8_BOM) for data in inputs['sym'])
    assert not any(r.code == 'symbol_export_stale' for r in results)
    saved.write_bytes(UTF8_BOM + text.replace('"GPIO1"', '"GPIO9"').encode('utf-8'))
    results = check.verify_component(art.component_dir, 'Demo', ROWS, applied_symbol=text)
    assert any(r.code == 'symbol_export_stale' for r in results)
