"""@brief Visible terminals, thermal tabs and complete lead counts are not interchangeable."""
import pytest
from kicadgen.cad3d import kicad_models as km
from test_kicad_model_candidates_backend import library, supported


def visible(**changes):
    return supported(**dict(dict(kind='ic', package='SOIC-4', terminal_count=2,
        pin_count=2, pin_count_source='photo_observation',
        terminal_count_basis='visible_including_tabs', confidence='high',
        terminal_layout='two visible leads; the other side is occluded'), **changes))


def test_visible_subset_does_not_veto_more_leads_or_claim_automatic_match(library):
    root, add, measured = library
    path = add('Package_SO', 'SOIC-4')
    report = km.model_candidates(visible(), library=root)
    assert measured == [path]
    assert report['status'] == 'review_required'
    assert report['selected_path'] is None
    assert km._observed_count(visible()) == set()
    assert km._provisional_terminal_count(visible()) is None


def test_complete_leads_reject_a_different_count(library):
    root, add, measured = library
    add('Package_SO', 'SOIC-6')
    report = km.model_candidates(visible(package='SOIC-6', pin_count=4,
        pin_count_source='complete_lead_observation'), library=root)
    assert not measured and report['candidates'] == []


def test_visible_tab_does_not_conflict_with_complete_ordinary_lead_count(library):
    root, add, _ = library
    add('Package_SO', 'SOIC-8_1EP')
    report = km.model_candidates(visible(package='SOIC-8_1EP', terminal_count=9,
        pin_count=8, pin_count_source='complete_lead_observation'), library=root)
    assert report['status'] == 'auto_matched'


@pytest.mark.parametrize('kind,package', [('resistor','R_0603_1608Metric'), ('capacitor','C_0603_1608Metric')])
def test_single_passive_with_two_visible_endcaps_retains_mechanical_estimate(library, kind, package):
    root, add, _ = library
    add('Resistor_SMD' if kind == 'resistor' else 'Capacitor_SMD', package)
    report = km.model_candidates(visible(kind=kind, package=package,
        pin_count=None, terminal_layout='two endcaps, complete single chip body'), library=root)
    assert report['status'] == 'auto_matched'
    # A third visible terminal contradicts a single two-endcap component.
    assert km.model_candidates(visible(kind=kind, package=package, pin_count=None,
        terminal_count=3), library=root)['status'] == 'review_required'


def test_unknown_semiconductor_renderer_alias_does_not_exclude_diode(library, monkeypatch):
    root, add, _ = library
    diode = add('Diode_SMD', 'SOD-323')
    monkeypatch.setattr(km, 'load_index', lambda *args: {km.index_key(diode): {'body':[1.6,.8]}})
    report = km.model_candidates(visible(package='', **{'class':'semiconductor_unknown'}), library=root)
    assert [row['path'] for row in report['candidates']] == [str(diode)]
    assert report['status'] == 'review_required'
    assert not km._compatible_candidates([diode], visible(kind='resistor'), lambda text:None)
