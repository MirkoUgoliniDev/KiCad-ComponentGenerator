"""@brief Estimated pads in «Verifica componente» (review 2026-11-15, A5).

@details The footprint tab flags pads whose hole, size or shape the program
derived or estimated (`footprint_checks.check_estimated_pads`); the
component check never said so. `verify_component(estimated_pads=...)` now
turns each finding into a WARN «Pad stimati» with the finding's text, its
pads as numbers, code «estimated_pads», category «final» and the footprint
tab's own remedy. None or [] -> no row. Positive and negative twins.
"""

from types import SimpleNamespace

from kicadgen.core.footprint import Footprint, Pad
from kicadgen.services import component_check as check
from kicadgen.services import footprint_checks as fc
from test_kicad_library import _artifacts

ROWS = [{'pin': '1', 'name': 'GPIO1'}]
THT = ['*.Cu', '*.Mask']


def _tht(number, x, sources):
    """A plated through-hole pad with a provenance."""
    return Pad(number=number, pad_type='thru_hole', shape='circle', at=(x, 0), size=(2.2, 2.2),
               layers=THT, drill=0.85, sources=sources)


def _footprint(*pads):
    """A footprint holding the pads, in order."""
    footprint = Footprint(name='T', attr='through_hole')
    for pad in pads:
        footprint.add_pad(pad)
    return footprint


def test_estimated_pads_become_one_warn_with_their_numbers():
    """@brief Positive: pads 1 and 3 derived, pad 2 read -> one WARN naming 1 and 3, the finding's
    text, the footprint tab's remedy, code «estimated_pads», category «final»."""
    derived = {'drill_mm': 'derived', 'pad_size': 'derived', 'shape': 'read'}
    read = {'drill_mm': 'read', 'pad_size': 'read', 'shape': 'read'}
    findings = fc.check_estimated_pads(_footprint(_tht('1', 0, derived), _tht('2', 5, read),
                                                  _tht('3', 10, derived)))
    (row,) = check.estimated_pad_checks(findings, ['1', '2', '3'])
    assert (row.status, row.code, row.category) == ('WARN', 'estimated_pads', 'final')
    assert row.title == 'Pad stimati' and row.pins == ('1', '3')
    assert row.detail == findings[0].message
    assert row.remedy == fc.geometry_hint('estimated_pad') and row.remedy
    assert row.importance == check.IMPORTANCE_BEFORE_PRODUCTION


def test_pads_that_were_read_give_no_row():
    """@brief Negative twin: every pad read -> no finding -> no row; None -> no row."""
    read = {'drill_mm': 'read', 'pad_size': 'read', 'shape': 'read'}
    findings = fc.check_estimated_pads(_footprint(_tht('1', 0, read), _tht('2', 5, read)))
    assert findings == [] and check.estimated_pad_checks(findings, ['1', '2']) == []
    assert check.estimated_pad_checks(None, ['1']) == []


def test_an_index_the_file_does_not_have_names_no_pad():
    """@brief Negative: a finding computed on another footprint than the file (index out of the
    file's pads) -> the row stays, with NO pins: never a guessed pad."""
    finding = SimpleNamespace(code='estimated_pad', message='Pad stimati: 1-4', pads=(0, 3))
    (row,) = check.estimated_pad_checks([finding], ['1', '2'])
    assert row.pins == () and row.detail == 'Pad stimati: 1-4'
    (row,) = check.estimated_pad_checks([finding], None)
    assert row.pins == ()


def test_another_finding_code_is_ignored():
    """@brief Negative: only «estimated_pad» findings are read (by code, never by text)."""
    other = SimpleNamespace(code='pads_overlap', message='Pad stimati', pads=(0,))
    assert check.estimated_pad_checks([other], ['1']) == []


def test_verify_component_reports_the_estimated_pads_it_is_given(tmp_path, monkeypatch):
    """@brief Positive: `estimated_pads=` -> the row is in the check, pad index 0 named «1» through
    the numbers of the footprint the finding was computed on; negative twin: the same check
    without it has no such row.

    CONTRACT CHANGED 2026-11-15 (round 2): the pad is named through
    `estimated_pad_numbers`, no longer through the file's pad order (R6); without
    them the row names no pad (see tests/test_component_check_r2_estimated_numbers.py).
    """
    art = _artifacts(tmp_path)
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: None)
    finding = SimpleNamespace(code='estimated_pad', message='• 1 — foro', pads=(0,))
    results = check.verify_component(art.component_dir, 'Demo', ROWS, estimated_pads=[finding],
                                     estimated_pad_numbers=['1'])
    rows = [r for r in results if r.code == 'estimated_pads']
    assert [(r.status, r.pins, r.detail) for r in rows] == [('WARN', ('1',), '• 1 — foro')]
    unnamed = check.verify_component(art.component_dir, 'Demo', ROWS, estimated_pads=[finding])
    assert [r.pins for r in unnamed if r.code == 'estimated_pads'] == [()]
    plain = check.verify_component(art.component_dir, 'Demo', ROWS)
    assert not any(r.code == 'estimated_pads' for r in plain)
    empty = check.verify_component(art.component_dir, 'Demo', ROWS, estimated_pads=[])
    assert not any(r.code == 'estimated_pads' for r in empty)
