"""@brief «Pad stimati» names its pads through the footprint the findings were computed on (review 2026-11-15, round 2, R6).

@details The findings of `footprint_checks.check_estimated_pads` carry pad
INDICES into `Footprint.pads`. They used to be named through the pad order of
the FILE on disk, which KiCad's footprint editor may change (the window's
alignment gate compares pads regardless of order): the wrong pad was then
highlighted. `verify_component(estimated_pad_numbers=...)` now takes the
numbers of that same footprint; without them no pad is named, never a guess
from the file. Positive and negative twins.
"""

from kicadgen.core.footprint import Footprint, Pad
from kicadgen.services import component_check as check
from kicadgen.services import footprint_checks as fc
from test_kicad_library import _artifacts

ROWS = [{'pin': '1', 'name': 'A'}, {'pin': '2', 'name': 'B'}, {'pin': '3', 'name': 'C'}]
DERIVED = {'drill_mm': 'derived', 'pad_size': 'read', 'shape': 'read'}
READ = {'drill_mm': 'read', 'pad_size': 'read', 'shape': 'read'}


def _pad(number, x, sources):
    """A plated through-hole pad with a provenance."""
    return Pad(number=number, pad_type='thru_hole', shape='circle', at=(x, 0), size=(2.0, 2.0),
               layers=['*.Cu', '*.Mask'], drill=0.8, sources=sources)


def _write_reordered(art):
    """The footprint on disk with its pads in the order 3, 2, 1 (as KiCad's editor may save it)."""
    pads = ''.join(
        f'\n  (pad "{n}" thru_hole circle (at {x} 0) (size 2 2) (drill 0.8) (layers "*.Cu" "*.Mask"))'
        for n, x in (('3', 10), ('2', 5), ('1', 0)))
    art.footprint_path.write_text(f'(footprint "Demo"\n  (version 20241229){pads}\n)\n',
                                  encoding='utf-8')


def _findings_and_numbers():
    """Findings of a footprint whose pad «1» (index 0) is derived, and its pad numbers."""
    footprint = Footprint(name='Demo', attr='through_hole')
    for pad in (_pad('1', 0, DERIVED), _pad('2', 5, READ), _pad('3', 10, READ)):
        footprint.add_pad(pad)
    return fc.check_estimated_pads(footprint), [pad.number for pad in footprint.pads]


def test_a_reordered_file_still_names_the_estimated_pad(tmp_path, monkeypatch):
    """@brief Positive: the file lists pad 3 first; the finding means pad «1» (index 0 of the
    footprint) -> the row names «1», not the file's first pad «3»."""
    art = _artifacts(tmp_path)
    _write_reordered(art)
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: None)
    findings, numbers = _findings_and_numbers()
    results = check.verify_component(art.component_dir, 'Demo', ROWS, estimated_pads=findings,
                                     estimated_pad_numbers=numbers)
    assert [r.pins for r in results if r.code == 'estimated_pads'] == [('1',)]


def test_without_the_numbers_no_pad_is_named(tmp_path, monkeypatch):
    """@brief Negative twin: the same findings without `estimated_pad_numbers` -> the row stays,
    naming no pad (never the file's i-th pad)."""
    art = _artifacts(tmp_path)
    _write_reordered(art)
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: None)
    findings, _numbers = _findings_and_numbers()
    results = check.verify_component(art.component_dir, 'Demo', ROWS, estimated_pads=findings)
    assert [r.pins for r in results if r.code == 'estimated_pads'] == [()]


def test_numbers_are_named_as_text_and_blank_ones_are_left_out():
    """@brief Positive: integer numbers become text; an unnumbered pad (a mechanical hole) names
    no pin; negative: an index the numbers do not have names none at all."""
    finding = type('F', (), {'code': 'estimated_pad', 'message': 'm', 'pads': (0, 1, 2)})()
    (row,) = check.estimated_pad_checks([finding], [1, '', 'A1'])
    assert row.pins == ('1', 'A1')
    (row,) = check.estimated_pad_checks([finding], ['1', '2'])
    assert row.pins == ()
