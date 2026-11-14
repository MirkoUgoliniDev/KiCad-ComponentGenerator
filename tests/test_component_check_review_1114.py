"""@brief Review fixes of the automatic DRC/ERC (2026-11-14): contacts, mounting holes, courtyard, errors.

@details Zero-distance defects block the use, computed from the board's own
geometry (never from KiCad's localized text): a short, two drills that
intersect, a drill that cuts another pad's copper; a positive shortfall stays
a KiCad-default-rule warning. A mechanical hole without a number is named
«foro di montaggio»; items without uuid are deduplicated by position; the
footprint FILE is held to the courtyard rule; a failed preview or kicad-cli
gives a translated SKIP with the first stderr line; pin numbers like «²»
never raise. Positive and negative cases.
"""

import subprocess
from types import SimpleNamespace

from kicadgen.services import component_check as check
from kicadgen.services.kicad_library import KiCadLibraryError, _pin_sort_key
from test_kicad_library import _artifacts
from tests.test_component_check_drc_erc import _fake_cli, _pads, _violation


# ── B: contacts block the use ────────────────────────────────────────────

def test_a_short_is_an_error_that_blocks_the_use():
    """@brief Positive: shorting_items -> ERROR importance 1 without the «default rules» note."""
    rows = check.drc_checks({'violations': [_violation('shorting_items', 'error', 'u1', 'u2')]}, _pads('1', '2'))
    assert [(r.status, r.importance) for r in rows] == [('ERROR', check.IMPORTANCE_BLOCKS_USE)]
    assert 'Regole predefinite' not in rows[0].detail


def test_drills_that_intersect_are_an_error_but_close_drills_are_a_warning():
    """@brief Positive: 0.5 mm holes 0.3 mm apart intersect -> ERROR 1; negative: 3 mm apart -> WARN 2."""
    touching = check.drc_checks({'violations': [_violation('hole_to_hole', 'error', 'u1', 'u2')]},
                                _pads('1', '2', xs=[0.0, 0.3]))
    assert [(r.status, r.importance) for r in touching] == [('ERROR', check.IMPORTANCE_BLOCKS_USE)]
    apart = check.drc_checks({'violations': [_violation('hole_to_hole', 'error', 'u1', 'u2')]},
                             _pads('1', '2', xs=[0.0, 3.0]))
    assert [(r.status, r.importance) for r in apart] == [('WARN', check.IMPORTANCE_BEFORE_PRODUCTION)]
    assert 'Regole predefinite' in apart[0].detail


def test_a_drill_cutting_another_pads_copper_is_an_error():
    """@brief Positive: hole of pad 1 (r 0.25) at 0.6 mm from pad 2 (r 0.5) cuts it -> ERROR 1;
    negative: 2 mm apart -> WARN 2."""
    cut = check.drc_checks({'violations': [_violation('hole_clearance', 'error', 'u1', 'u2')]},
                           _pads('1', '2', xs=[0.0, 0.6]))
    assert [r.status for r in cut] == ['ERROR']
    clear = check.drc_checks({'violations': [_violation('hole_clearance', 'error', 'u1', 'u2')]},
                             _pads('1', '2', xs=[0.0, 2.0]))
    assert [r.status for r in clear] == ['WARN']


# ── E: mounting holes, positions, instances ──────────────────────────────

def test_an_unnumbered_hole_is_a_mounting_hole_and_never_a_pin():
    """@brief Positive: an unnumbered np_thru_hole is «foro di montaggio», not «pad 1, »; the row's
    pins hold numbers only. Negative (review L2): an unnumbered PLATED pad is «pad senza numero»."""
    pads = _pads('1', '')
    pads[1].pad_type = 'np_thru_hole'
    rows = check.drc_checks({'violations': [_violation('hole_to_hole', 'warning', 'u1', 'u')]}, pads)
    assert 'pad 1, foro di montaggio' in rows[0].detail and rows[0].pins == ('1',)
    plated = check.drc_checks({'violations': [_violation('hole_to_hole', 'warning', 'u1', 'u')]}, _pads('1', ''))
    assert 'pad 1, pad senza numero' in plated[0].detail and plated[0].pins == ('1',)


def test_items_without_uuid_are_deduplicated_by_position():
    """@brief Positive: the same violation again at the same positions is dropped (its text never
    shows); negative: one elsewhere is kept."""
    def at(x, text):
        violation = _violation('silk_overlap', 'warning', description=text)
        violation['items'] = [{'pos': {'x': x, 'y': 0.0}}]
        return violation
    rows = check.drc_checks({'violations': [at(1.0, 'first'), at(1.0, 'again'), at(2.0, 'other')]},
                            _pads('1'))
    assert len(rows) == 1
    assert 'first' in rows[0].detail and 'other' in rows[0].detail and 'again' not in rows[0].detail


def test_overlap_dedupe_is_by_pad_instance_not_by_number():
    """@brief Two pads numbered «2» overlap a third: only the reported INSTANCE pair is dropped."""
    pads = _pads('2', '2', '3')
    report = {'violations': [_violation('clearance', 'error', 'u2', 'u3')]}
    # Pads 0 and 2 (the first «2» and «3») were reported; uuids «u2» name pad 0 here.
    assert check.drc_checks(report, pads, overlap_pairs=[frozenset({0, 2})])[0].status == 'OK'
    assert check.drc_checks(report, pads, overlap_pairs=[frozenset({1, 2})])[0].code == 'drc_clearance'


def test_pads_with_the_same_number_are_never_a_contact():
    """@brief Review M2: two pieces of one pin whose holes intersect keep KiCad's severity (WARN 2);
    negative: the same geometry with two numbers is a contact (ERROR 1)."""
    report = {'violations': [_violation('hole_to_hole', 'error', 'u7', 'u8')]}
    same = _pads('7', '8', xs=[0.0, 0.3])
    same[1].number = '7'
    # Two pieces of one pin differ in size; same-size copies are a double reading (third review).
    same[1].size = (2.0, 1.0)
    assert [r.status for r in check.drc_checks(report, same)] == ['WARN']
    assert [r.status for r in check.drc_checks(report, _pads('7', '8', xs=[0.0, 0.3]))] == ['ERROR']


def test_a_zero_clearance_between_two_pads_is_a_contact():
    """@brief Review L1: 1 mm pads 1 mm apart touch -> clearance ERROR 1; 1.5 mm apart -> WARN 2."""
    report = {'violations': [_violation('clearance', 'error', 'u1', 'u2')]}
    assert [r.status for r in check.drc_checks(report, _pads('1', '2', xs=[0.0, 1.0]))] == ['ERROR']
    assert [r.status for r in check.drc_checks(report, _pads('1', '2', xs=[0.0, 1.5]))] == ['WARN']


def test_library_findings_carry_no_manufacturer_note():
    """@brief Review L7: a grid finding has no «regole del produttore» note; a clearance has it."""
    report = {'violations': [_violation('endpoint_off_grid', 'warning', 'u1')]}
    rows, _power = check.erc_checks({'sheets': [{'violations': report['violations']}]}, {'u1': '1'})
    assert 'Regole predefinite' not in rows[0].detail


def test_relays_analysed_before_the_witness_get_a_nc_no_row():
    """@brief Practical test (3): switch functions without a witness -> «single» WARN 2 with the
    Pinout table; negative: no switch function -> no row."""
    rows = [{'pin': '1', 'name': 'NC', 'function': 'nc'}, {'pin': '2', 'name': 'COM', 'function': 'common'}]
    (row,) = check.contact_function_checks(None, rows)
    assert row.importance == check.IMPORTANCE_BEFORE_PRODUCTION and row.action == check.ACTION_EDIT_PINOUT
    assert check.contact_function_checks(None, [{'pin': '1', 'name': 'VCC'}]) == []


def test_with_the_library_row_the_model_link_row_is_not_made(tmp_path, monkeypatch):
    """@brief Practical test (4): one «install the library» row; negative: legacy callers keep it."""
    art = _artifacts(tmp_path)
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: None)
    rows = [{'pin': '1', 'name': 'A'}]
    with_installs = check.verify_component(art.component_dir, 'Demo', rows, installs=[])
    assert not [r for r in with_installs if r.code == 'model_link_missing']
    assert [r for r in with_installs if r.code == 'kicad_library']
    legacy = check.verify_component(art.component_dir, 'Demo', rows)
    assert [r for r in legacy if r.code == 'model_link_missing']


# ── D: the courtyard rule on the file ────────────────────────────────────

_COURTYARD = '''(footprint "C" (layer "F.Cu")
  (fp_rect (start -1 -1) (end 1 1) (layer "F.CrtYd") (width 0.05))
  (fp_line (start {x0} 0) (end {x1} 0) (layer "F.Fab") (width 0.1))
  (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu")))
'''


def test_a_body_drawing_outside_the_courtyard_is_reported():
    """@brief Positive: an F.Fab line beyond the courtyard -> WARN 2; negative: inside -> nothing."""
    out = check.courtyard_checks(_COURTYARD.format(x0=-0.5, x1=3.0))
    assert [(r.status, r.importance, r.code) for r in out] == [
        ('WARN', check.IMPORTANCE_BEFORE_PRODUCTION, 'footprint_courtyard')]
    assert check.courtyard_checks(_COURTYARD.format(x0=-0.5, x1=0.5)) == []


# ── I: translated, informative SKIPs ─────────────────────────────────────

def test_a_failed_preview_is_described_by_the_callers_translation(tmp_path, monkeypatch):
    """@brief Positive: a KiCadLibraryError of the preview goes through `describe_error`."""
    art = _artifacts(tmp_path)
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: None)

    def refuse(*_args, **_kwargs):
        raise KiCadLibraryError('raw English', code='preview_locked')
    monkeypatch.setattr(check, 'create_preview_project', refuse)
    results = check.verify_component(art.component_dir, 'Demo', [{'pin': '1', 'name': 'A'}],
                                     describe_error=lambda exc: f'TRADOTTO {exc.code}')
    drc = next(r for r in results if r.code == 'drc')
    assert drc.status == 'SKIP' and 'TRADOTTO preview_locked' in drc.detail and not drc.needs_cli


def test_a_non_zero_exit_keeps_the_first_stderr_line_and_runs_outside_the_project(tmp_path, monkeypatch):
    """@brief Positive: the SKIP says kicad-cli's first error line; every call runs in a neutral cwd."""
    art = _artifacts(tmp_path)
    cwds = []
    _fake_cli(monkeypatch, fail=('drc',))
    fake = check.subprocess.run

    def run(command, **kwargs):
        cwds.append(kwargs.get('cwd'))
        result = fake(command, **kwargs)
        if result.returncode:
            return subprocess.CompletedProcess(command, 3, '', '\nBoard not loaded: version too new\nmore')
        return result
    monkeypatch.setattr(check.subprocess, 'run', run)
    results = check.verify_component(art.component_dir, 'Demo', [{'pin': '1', 'name': 'A'}])
    drc = next(r for r in results if r.code == 'drc')
    assert 'Board not loaded: version too new' in drc.detail and 'more' not in drc.detail
    assert cwds and all(cwd and str(art.component_dir) not in cwd for cwd in cwds)


# ── M: pin numbers that look like digits ────────────────────────────────

def test_superscript_digits_never_raise_in_the_pin_sorts():
    """@brief Positive: «²» sorts as text; canonical numbers still sort by value."""
    assert sorted(['10', '²', '2'], key=check._pin_sort) == ['2', '10', '²']
    assert sorted(['A10', 'A2', '²'], key=_pin_sort_key) == ['A2', 'A10', '²']


def test_npth_label_helper_and_ranges():
    """@brief The labels of the rows: numbers as ranges, then «foro di montaggio» once."""
    assert check._pins_text(['1', '2', '3', check.tx('foro di montaggio')]) == '1–3, foro di montaggio'
    assert check._pad_label(SimpleNamespace(number='', pad_type='np_thru_hole')) == 'foro di montaggio'
    assert check._pad_label(SimpleNamespace(number='', pad_type='smd')) == 'pad senza numero'


# ── M3: copper sides; L6: an unbuildable symbol ──────────────────────────

def _smd(number, x, layers):
    """A 1 mm square SMD pad of the file at (x, 0) on `layers`."""
    return f'  (pad "{number}" smd rect (at {x} 0) (size 1 1) (layers {layers}))\n'


def test_pads_on_opposite_sides_or_without_copper_never_overlap():
    """@brief Review M3: front vs back copper, or a paste-only aperture, overlap no copper;
    negative: two front pads overlapping still do."""
    def overlaps(*pads):
        return [r for r in check.footprint_geometry_checks('(footprint "X"\n' + ''.join(pads) + ')\n')
                if r.status == 'ERROR']
    assert overlaps(_smd('1', 0, '"F.Cu" "F.Mask"'), _smd('2', 0.5, '"B.Cu" "B.Mask"')) == []
    assert overlaps(_smd('1', 0, '"F.Cu" "F.Mask"'), _smd('2', 0.5, '"F.Paste"')) == []
    assert len(overlaps(_smd('1', 0, '"F.Cu" "F.Mask"'), _smd('2', 0.5, '"F.Cu" "F.Mask"'))) == 1


def test_the_erc_says_why_when_the_applied_symbol_cannot_be_built(tmp_path, monkeypatch):
    """@brief Review L6: the ERC SKIP points to the «Simbolo» row instead of a vague reason."""
    art = _artifacts(tmp_path)
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: None)

    def broken(*_args, **_kwargs):
        raise ValueError('pin senza numero')
    monkeypatch.setattr(check, 'serialize_symbol', broken)
    results = check.verify_component(art.component_dir, 'Demo', [{'pin': '1', 'name': 'A'}])
    erc = next(r for r in results if r.code == 'erc')
    assert erc.status == 'SKIP' and '«Simbolo»' in erc.detail


# ── Third review (2026-11-14) ─────────────────────────────────────────────

def test_a_pad_read_twice_is_reported_but_a_split_pin_is_not():
    """@brief M2: two pads «1» of the same size 0.1 mm apart are copies -> reported (geometry
    row and contact); negative: two pieces of pin «1» of different sizes may overlap."""
    copies = ('(footprint "X"\n' + _smd('1', 0, '"F.Cu"') + _smd('1', 0.1, '"F.Cu"') + ')\n')
    rows = [r for r in check.footprint_geometry_checks(copies) if r.status == 'ERROR']
    assert len(rows) == 1 and 'letto due volte' in rows[0].detail
    split = ('(footprint "X"\n' + _smd('1', 0, '"F.Cu"')
             + '  (pad "1" smd rect (at 0.5 0) (size 2 1) (layers "F.Cu"))\n)\n')
    assert [r.status for r in check.footprint_geometry_checks(split)] == ['OK']
    report = {'violations': [_violation('clearance', 'error', 'u1', 'u2')]}
    pads = _pads('1', '2', xs=[0.0, 0.1])
    pads[1].number = '1'
    pads[1].uuid = 'u2'
    assert [r.status for r in check.drc_checks(report, pads)] == ['ERROR']


def test_an_installation_that_cannot_be_compared_is_said():
    """@brief L9: a footprint text that cannot be renamed -> SKIP «non confrontabile», never silence;
    negative: no recorded installation is a plain «not installed» WARN."""
    rows = check.library_checks('Demo', 'not a footprint', [{'scope': 'global', 'pretty_dir': 'C:/x'}])
    assert [(r.status, r.code) for r in rows] == [('SKIP', 'kicad_library')]
    assert 'non confrontabile' in rows[0].detail
    assert [r.status for r in check.library_checks('Demo', 'not a footprint', [])] == ['WARN']
