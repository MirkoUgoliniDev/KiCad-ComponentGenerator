"""Regressions for cross-view matching; no model calls or image fixtures."""
from copy import deepcopy
from itertools import permutations

from kicadgen.services.photo_inventory import merge_components, _associate_view


def boxes(xs, source='photo', **extra):
    return [dict(_box=(x,0,x+.2,.2),name=str(x),kind='generic',_source=source,**extra) for x in xs]


def test_audit_ordering_regression_has_two_matches_for_every_permutation():
    a,b = boxes([0,.08],'full'), boxes([.045,.09],'detail')
    expected = {tuple(c['_box']) for c in b}
    for first in permutations(a):
        for second in permutations(b):
            merged = merge_components([first,second],containment=True)
            assert len(merged)==2
            assert {tuple(c['_box']) for c in merged} == expected


def test_association_does_not_mutate_input_or_merge_within_one_view():
    group = boxes([0,.08])
    before = deepcopy(group)
    assert len(merge_components([group],containment=True))==2
    assert group==before


def test_large_enclosing_box_cannot_absorb_a_small_real_component():
    large = dict(_box=(0,0,1,1),kind='module')
    small = dict(_box=(.1,.1,.15,.15),kind='resistor')
    assert len(merge_components([[large],[small]],containment=True))==2


def test_reassignment_finds_maximum_cardinality_without_changing_geometry():
    old = boxes([0,.08,.16])
    new = boxes([.045,.09,.19])
    for order in permutations(new):
        match = _associate_view(order,old,True)
        assert len(match)==3
        assert len(set(match.values()))==3


def test_matched_metadata_stays_with_its_physical_box():
    old = boxes([0,.08],confidence='high')
    old[0]['marking']='left'
    old[1]['marking']='right'
    new = boxes([.045,.09],confidence='low')
    for order in permutations(new):
        result = merge_components([old,order],containment=True)
        assert [(c['_box'],c['marking']) for c in result] == [(c['_box'],c['marking']) for c in old]
