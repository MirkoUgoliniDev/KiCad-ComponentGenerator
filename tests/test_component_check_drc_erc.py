"""@brief Automatic DRC/ERC and the «Libreria KiCad» row of «Verifica componente» (2026-11-14).

@details User decision: every verification runs KiCad's DRC on the preview
board and ERC on the preview schematic (the APPLIED symbol), through
kicad-cli's JSON reports. Only `type`, `severity` and the item uuids are
read; the uuids are mapped to pad/pin numbers through the files of the same
run. The preview's own artefacts (no nets, generated outline, a symbol
alone on its sheet) are filtered; a KiCad error is a WARN of importance 2, a
warning of importance 3; a failing or missing kicad-cli is a SKIP, never an
ERROR. Offline: `subprocess.run` is faked and writes the JSON; the real-KiCad
test is skipped without kicad-cli. Positive and negative cases.
"""

import json
import subprocess
from pathlib import Path

import pytest

from kicadgen.services import component_check as check
from kicadgen.services.kicad_library import attach_3d_model, name_footprint
from test_kicad_library import _artifacts


def _violation(kind, severity, *uuids, description='KiCad says so'):
    """One violation of a kicad-cli JSON report."""
    return {'type': kind, 'severity': severity, 'description': description,
            'items': [{'uuid': uuid, 'description': 'x', 'pos': {'x': 0, 'y': 0}} for uuid in uuids]}


def _pads(*numbers, uuids=True, xs=None):
    """Board pads as `preview_board_pads` gives them: 1 mm round plated pads, 0.5 mm holes, 3 mm apart."""
    from types import SimpleNamespace
    pads = []
    for index, number in enumerate(numbers):
        x = (xs or [3.0 * i for i in range(len(numbers))])[index]
        pads.append(SimpleNamespace(number=number, pad_type='thru_hole', shape='circle', at=(x, 0.0),
                                    rotation=0.0, size=(1.0, 1.0), layers=['*.Cu'], rect_delta=None,
                                    roundrect_rratio=None, drill_offset=(0.0, 0.0), hole=(0.5, 0.5),
                                    index=index, uuid=f'u{number}' if uuids else None,
                                    board_xy=(100.0 + x, 100.0)))
    return pads


PADS = _pads('1', '2', '3')
PINS = {'u1': '1', 'u2': '2', 'u3': '3'}


# ── DRC rows ─────────────────────────────────────────────────────────────

def test_drc_violations_become_rows_with_their_pads():
    """@brief Positive: an error -> WARN importance 2 with its pads; a warning -> importance 3."""
    report = {'violations': [_violation('annular_width', 'error', 'u1'),
                             _violation('silk_over_copper', 'warning', 'u2', 'edge')]}
    rows = check.drc_checks(report, PADS)
    assert [(r.code, r.importance, r.pins) for r in rows] == [
        ('drc_annular_width', check.IMPORTANCE_BEFORE_PRODUCTION, ('1',)),
        ('drc_silk_over_copper', check.IMPORTANCE_FINAL_REVIEW, ('2',))]
    assert all(r.status == 'WARN' and r.action == check.ACTION_SHOW_IN_KICAD for r in rows)
    assert 'pad 1' in rows[0].detail and 'KiCad says so' in rows[0].detail


def test_preview_artefacts_duplicates_and_known_overlaps_are_not_shown():
    """@brief Negative: edge clearance, unconnected items, a repeated violation and a clearance
    on a pad pair the program already reported give no extra row; an unknown type keeps
    KiCad's description."""
    report = {'violations': [_violation('copper_edge_clearance', 'error', 'u1', 'edge'),
                             _violation('unconnected_items', 'error', 'u1', 'u2'),
                             _violation('clearance', 'error', 'u2', 'u3'),
                             _violation('clearance', 'error', 'u3', 'u2'),
                             _violation('clearance', 'error', 'u1', 'u2'),
                             _violation('clearance', 'error', 'u1', 'u2'),
                             _violation('new_kicad_rule', 'warning', 'u3', description='Regola nuova')]}
    # CONTRACT CHANGED 2026-11-14 (review E/D): overlap pairs are pad INSTANCES
    # (indices); an edge violation is an artefact only on the preview's own outline.
    rows = check.drc_checks(report, PADS, overlap_pairs=[frozenset({1, 2})], outline_uuids={'edge'})
    assert [(r.code, r.pins) for r in rows] == [('drc_clearance', ('1', '2')),
                                               ('drc_new_kicad_rule', ('3',))]
    assert rows[1].detail.startswith('Regola nuova')


def test_a_clean_drc_is_one_ok_row():
    """@brief Negative: only artefacts -> one OK row."""
    rows = check.drc_checks({'violations': [_violation('invalid_outline', 'error', 'edge')]}, PADS,
                            outline_uuids={'edge'})
    assert [(r.status, r.code) for r in rows] == [('OK', 'drc')]


def test_an_edge_drawing_of_the_footprint_stays_visible():
    """@brief Positive (review D): a footprint Edge.Cuts drawing too close to copper is shown."""
    rows = check.drc_checks({'violations': [_violation('copper_edge_clearance', 'error', 'u1', 'fp-edge')]},
                            PADS, outline_uuids={'preview-outline'})
    assert [(r.code, r.pins) for r in rows] == [('drc_copper_edge_clearance', ('1',))]


def test_an_item_without_a_known_uuid_is_named_by_its_position_only_for_uuidless_pads():
    """@brief Positive: an unknown uuid at the centre of a pad written WITHOUT a uuid -> that pad;
    negative: elsewhere -> no pad, and at the centre of a pad that HAS a uuid -> no pad (a
    footprint-level item on an exposed pad at the origin, review C)."""
    board = ('(kicad_pcb (footprint "X" (at 100 100 90)\n'
             '  (pad "7" thru_hole circle (at 2 0) (size 1 1) (drill 0.5) (layers "*.Cu"))\n'
             '  (pad "EP" smd rect (at 0 0) (size 3 3) (layers "F.Cu") (uuid "ep-uuid"))))')
    pads, _outline = check.preview_board_pads(board)
    # Footprint turned 90 degrees counter-clockwise: the pad on its right goes up (y down).
    x, y = pads[0].board_xy
    assert pads[0].uuid is None and abs(x - 100.0) < 1e-9 and abs(y - 98.0) < 1e-9
    at_pad = _violation('annular_width', 'error', 'zzz')
    at_pad['items'][0]['pos'] = {'x': 100.0, 'y': 98.0}
    away = _violation('hole_to_hole', 'warning', 'yyy')
    away['items'][0]['pos'] = {'x': 101.0, 'y': 98.0}
    on_ep = _violation('silk_over_copper', 'warning', 'footprint-uuid')
    on_ep['items'][0]['pos'] = {'x': 100.0, 'y': 100.0}
    rows = check.drc_checks({'violations': [at_pad, away, on_ep]}, pads)
    assert [(r.code, r.pins) for r in rows] == [('drc_annular_width', ('7',)), ('drc_hole_to_hole', ()),
                                               ('drc_silk_over_copper', ())]


# ── ERC rows ─────────────────────────────────────────────────────────────

def _erc(*violations):
    """An ERC report with one sheet."""
    return {'sheets': [{'path': '/', 'violations': list(violations)}]}


def test_erc_of_a_symbol_alone_is_ok_with_its_unconnected_count():
    """@brief Negative: unconnected pins and an undriven power pin are artefacts: OK row, power pins returned."""
    rows, power = check.erc_checks(_erc(_violation('pin_not_connected', 'error', 'u1'),
                                        _violation('pin_not_connected', 'error', 'u2'),
                                        _violation('pin_not_connected', 'error', 'u2'),
                                        _violation('power_pin_not_driven', 'error', 'u1')), PINS)
    # Two DISTINCT pins, though pin 2 is reported twice (review E).
    assert [(r.status, r.code) for r in rows] == [('OK', 'erc')] and '2 pin' in rows[0].detail
    assert power == ['1']


def test_erc_violations_become_rows_and_library_mismatch_is_shown_in_kicad():
    """@brief Positive: a pin conflict -> show in KiCad; a library mismatch -> re-export the symbol."""
    rows, power = check.erc_checks(_erc(_violation('pin_to_pin', 'error', 'u1', 'u2'),
                                        _violation('lib_symbol_mismatch', 'warning', 'sym')), PINS)
    # CONTRACT CHANGED 2026-11-14 (review F): a re-export cannot change the
    # copies the check made, so a library mismatch is SHOWN in KiCad too.
    assert [(r.code, r.importance, r.action) for r in rows] == [
        ('erc_pin_to_pin', check.IMPORTANCE_BEFORE_PRODUCTION, check.ACTION_SHOW_IN_KICAD),
        ('erc_lib_symbol_mismatch', check.IMPORTANCE_FINAL_REVIEW, check.ACTION_SHOW_IN_KICAD)]
    assert power == []


# ── verify_component with a faked kicad-cli ─────────────────────────────

def _fake_cli(monkeypatch, drc=None, erc=None, fail=(), seen=None):
    """kicad-cli faked: every command succeeds, the DRC/ERC write `drc`/`erc` (a callable of the input path)."""
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: 'kicad-cli')
    monkeypatch.setattr(check, 'check_kicad_version',
                        lambda cli: type('V', (), {'supported': True, 'newer_cli': None})())

    def run(command, **_kwargs):
        args = [str(a) for a in command[1:]]
        kind = 'drc' if args[:2] == ['pcb', 'drc'] else 'erc' if args[:2] == ['sch', 'erc'] else None
        if seen is not None:
            seen.append((kind, args))
        if kind in fail:
            return subprocess.CompletedProcess(command, 3, '', 'boom')
        output = Path(args[args.index('-o') + 1])
        if kind is not None:
            source = Path(args[-1])
            maker = drc if kind == 'drc' else erc
            output.write_text(json.dumps(maker(source) if maker else
                                         ({'violations': []} if kind == 'drc' else {'sheets': []})),
                              encoding='utf-8')
        elif args[:2] == ['pcb', 'export']:
            output.write_text('MANIFOLD_SOLID_BREP(', encoding='utf-8')
        else:
            output.mkdir(parents=True, exist_ok=True)
            (output / 'out.svg').write_text('<svg/>', encoding='utf-8')
        return subprocess.CompletedProcess(command, 0, '', '')
    monkeypatch.setattr(check.subprocess, 'run', run)


def test_drc_and_erc_run_on_the_preview_of_the_applied_symbol(tmp_path, monkeypatch):
    """@brief Positive: the DRC report's pad uuid is mapped through the preview board of the same
    run; the ERC reads a schematic whose symbol is the APPLIED one, not the exported file."""
    art = _artifacts(tmp_path)
    (art.component_dir / 'Demo.kicad_sym').write_text('(kicad_symbol_lib (symbol "OLD"))', encoding='utf-8')
    seen, symbols = [], []

    def drc(board):
        # The test footprint's pad has no uuid: KiCad would invent one, and the
        # item is named by its position (the pad centre on the preview board).
        x, y = check.preview_board_pads(board.read_text(encoding='utf-8'))[0][0].board_xy
        violation = _violation('annular_width', 'error', 'kicad-made-uuid')
        violation['items'][0]['pos'] = {'x': x, 'y': y}
        return {'violations': [violation]}

    def erc(schematic):
        symbols.append((schematic.parent / 'Demo.kicad_sym').read_text(encoding='utf-8'))
        return {'sheets': []}
    _fake_cli(monkeypatch, drc=drc, erc=erc, seen=seen)
    results = check.verify_component(art.component_dir, 'Demo', [{'pin': '1', 'name': 'GPIO1'}])
    drc_rows = [r for r in results if r.code == 'drc_annular_width']
    assert len(drc_rows) == 1 and drc_rows[0].pins == ('1',)
    assert symbols and 'GPIO1' in symbols[0] and '"OLD"' not in symbols[0]
    assert [r.status for r in results if r.code == 'erc'] == ['OK']
    drc_args = next(args for kind, args in seen if kind == 'drc')
    assert '--exit-code-violations' not in drc_args and {'--severity-error', '--severity-warning'} <= set(drc_args)


@pytest.mark.parametrize('fail, drc, reason', [
    (('drc',), None, 'codice 3'),
    ((), lambda board: {'nothing': []}, 'rapporto leggibile'),
])
def test_a_failing_kicad_cli_is_a_skip_never_an_error(tmp_path, monkeypatch, fail, drc, reason):
    """@brief Negative: a non-zero exit or a report without `violations` -> SKIP «DRC non eseguito»."""
    art = _artifacts(tmp_path)
    _fake_cli(monkeypatch, drc=drc, fail=fail)
    results = check.verify_component(art.component_dir, 'Demo', [{'pin': '1', 'name': 'GPIO1'}])
    rows = [r for r in results if r.code == 'drc']
    assert [r.status for r in rows] == ['SKIP'] and reason in rows[0].detail
    assert not any(r.status == 'ERROR' and r.code.startswith('drc') for r in results)


def test_without_kicad_cli_drc_and_erc_are_skipped(tmp_path, monkeypatch):
    """@brief Negative: no kicad-cli -> both SKIP «Richiede KiCad CLI.», folded by the dialog."""
    art = _artifacts(tmp_path)
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: None)
    results = check.verify_component(art.component_dir, 'Demo', [{'pin': '1', 'name': 'GPIO1'}])
    assert {r.code: r.status for r in results if r.code in ('drc', 'erc', 'kicad_cli')} == {
        'drc': 'SKIP', 'erc': 'SKIP', 'kicad_cli': 'SKIP'}


def test_an_undriven_power_pin_goes_to_the_manual_row(tmp_path, monkeypatch):
    """@brief Positive: the ERC's power artefact names its pins in the manual row; negative: none, no sentence."""
    art = _artifacts(tmp_path)

    def erc(schematic):
        pins = check._schematic_pin_uuids(schematic.read_text(encoding='utf-8'))
        return _erc(_violation('power_pin_not_driven', 'error', *pins))
    _fake_cli(monkeypatch, erc=erc)
    manual = check.verify_component(art.component_dir, 'Demo', [{'pin': '1', 'name': 'VCC'}])[-1]
    assert manual.code == 'manual_checks' and 'PWR_FLAG' in manual.detail and '1' in manual.detail
    _fake_cli(monkeypatch)
    manual = check.verify_component(art.component_dir, 'Demo', [{'pin': '1', 'name': 'VCC'}])[-1]
    assert 'PWR_FLAG' not in manual.detail


# ── «Libreria KiCad» ─────────────────────────────────────────────────────

def _installed(tmp_path, scope='project'):
    """A component with a STEP and a library installed the way the install writes it now."""
    art = _artifacts(tmp_path)
    source = art.footprint_path.read_text(encoding='utf-8')
    root = tmp_path / ('project' if scope == 'project' else 'global')
    pretty = root / 'KiCadGen.pretty'
    shapes = root / 'KiCadGen.3dshapes'
    pretty.mkdir(parents=True)
    shapes.mkdir()
    step = shapes / 'Demo.step'
    reference = '${KIPRJMOD}/KiCadGen.3dshapes/Demo.step' if scope == 'project' else step.as_posix()
    installed = attach_3d_model(name_footprint(source, 'Demo'), reference, (0.0, 0.0, 0.0))
    (pretty / 'Demo.kicad_mod').write_bytes(installed.replace('\n', '\r\n').encode('utf-8'))
    step.write_bytes(art.step_path.read_bytes())
    return art, source, [{'scope': scope, 'pretty_dir': str(pretty)}], pretty, step


@pytest.mark.parametrize('scope', ['project', 'global'])
def test_an_installed_library_equal_to_what_an_install_writes_now_is_ok(tmp_path, scope):
    """@brief Positive: footprint (CRLF, model node) and STEP as written -> OK, whatever the scope."""
    art, source, installs, _pretty, _step = _installed(tmp_path, scope)
    rows = check.library_checks('Demo', source, installs, art.step_path)
    assert [r.status for r in rows] == ['OK']


def test_each_stale_part_of_an_installed_library_is_named(tmp_path):
    """@brief Negative (review A): another footprint, another model link, another STEP, a footprint
    gone -> WARN importance 2 naming WHAT differs, with the KiCad tab and the overwrite warning."""
    art, source, installs, pretty, step = _installed(tmp_path)
    file = pretty / 'Demo.kicad_mod'
    good = file.read_text(encoding='utf-8')

    def what():
        (row,) = check.library_checks('Demo', source, installs, art.step_path)
        assert row.status == 'WARN' and row.importance == check.IMPORTANCE_BEFORE_PRODUCTION
        assert row.action == check.ACTION_OPEN_KICAD_TAB and 'sovrascrive' in row.remedy
        return row.detail
    file.write_text(good.replace('(at ', '(at 1 ', 1), encoding='utf-8')
    assert '(footprint)' in what()
    file.write_text(good.replace('KiCadGen.3dshapes/Demo.step', 'Other.3dshapes/Demo.step'), encoding='utf-8')
    assert 'collegamento al modello STEP' in what() and '(footprint' not in what()
    file.write_text(good, encoding='utf-8')
    step.write_bytes(b'another model')
    assert 'modello STEP' in what()
    file.unlink()
    assert 'footprint mancante' in what()


def test_layout_is_ignored_but_quoted_text_is_compared():
    """@brief The token comparison: blanks between atoms do not count, inside quotes they do."""
    assert check.sexpr_tokens('(a  "x"\n\t(b 1))') == check.sexpr_tokens('(a "x" (b 1))')
    assert check.sexpr_tokens('(p "Relay (5V)  coil")') != check.sexpr_tokens('(p "Relay(5V) coil")')


def test_no_installation_and_no_recording():
    """@brief Never installed -> WARN 3; None -> no row."""
    never = check.library_checks('Demo', '(footprint "Demo")', [])
    assert [(r.status, r.importance) for r in never] == [('WARN', check.IMPORTANCE_FINAL_REVIEW)]
    assert check.library_checks('Demo', '(footprint "Demo")', None) == []


# ── Editor issues deduplicated by kind ───────────────────────────────────

def test_editor_issues_are_dropped_by_kind_whatever_their_language(tmp_path, monkeypatch):
    """@brief Positive: an English «Electrical type to check: 1» is not shown twice; negative:
    an issue of another kind stays."""
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: None)
    issues = [('electrical_type', 'Electrical type to check: 1'), ('text_overlap', 'Overlapping texts: 1, 2')]
    results = check.verify_component(tmp_path, 'Demo', [{'pin': '1', 'name': 'A'}], issues=issues)
    review = [r.detail for r in results if r.title == 'Revisione simbolo']
    assert 'Electrical type to check: 1' not in review and 'Overlapping texts: 1, 2' in review


# ── Real KiCad (skipped without kicad-cli) ───────────────────────────────

def test_real_kicad_drc_flags_a_thin_annular_ring_and_passes_a_clean_footprint(tmp_path):
    """@brief Positive: a 0.025 mm annular ring -> drc_annular_width on pad 1; negative: a clean pad -> DRC OK."""
    if not check.find_kicad_cli():
        pytest.skip('KiCad CLI not installed')

    def component(name, size):
        folder = tmp_path / name
        (folder / f'{name}.pretty').mkdir(parents=True)
        (folder / f'{name}.pretty' / f'{name}.kicad_mod').write_text(
            f'(footprint "{name}" (version 20241229) (generator "t") (layer "F.Cu") (attr through_hole)\n'
            f'  (pad "1" thru_hole circle (at 0 0) (size {size} {size}) (drill 1.0) (layers "*.Cu" "*.Mask")\n'
            '    (uuid "11111111-1111-1111-1111-111111111111")))\n', encoding='utf-8')
        return folder
    rows = [{'pin': '1', 'name': 'A'}]
    thin = check.verify_component(component('THIN', 1.05), 'THIN', rows)
    assert [r.pins for r in thin if r.code == 'drc_annular_width'] == [('1',)]
    clean = check.verify_component(component('CLEAN', 1.8), 'CLEAN', rows)
    assert [r.status for r in clean if r.code == 'drc'] == ['OK']
    assert [r.status for r in clean if r.code == 'erc'] == ['OK']
