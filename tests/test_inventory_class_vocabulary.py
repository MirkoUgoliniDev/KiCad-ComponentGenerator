"""@brief Contract between the global-inventory class vocabulary, the 3D kinds and the height rules.

A body family the 3D generator and photo_heights know must be declarable by
the vision model; otherwise it comes back as 'other' and becomes a 1 mm slab.
"""
import pytest

from kicadgen.services import photo_global_inventory as inventory
from kicadgen.services.datasheet_extractor import COMPONENT_KINDS
from kicadgen.services.photo_heights import (
    FAMILY_HEIGHT_MM, PLAN_SCALED_RATIO, estimate_body_height)
from kicadgen.services.photo_inventory import YOLO_CLASS_KINDS
from kicadgen.services.photo_yolo import _CLASSES as YOLO_CLASSES

#: Schema 6 vocabulary: recorded responses use these names and must still validate.
SCHEMA_6_CLASSES = ('resistor', 'capacitor', 'inductor', 'diode', 'transistor',
                    'regulator', 'semiconductor_unknown', 'ic', 'connector', 'switch',
                    'led', 'crystal', 'rf_module', 'module', 'other', 'unknown')


def _row(cls, bbox, **changes):
    """Complete model row as the schema requires it."""
    row = dict(component_id='c1', grouping_basis='single_body', source_candidate_ids=[],
               **{'class': cls}, bbox=bbox, bbox_basis='body', rotation_deg=0,
               package='', marking='', terminal_count=None, terminal_layout='unresolved',
               lead_count=None, lead_count_complete=False, lead_count_confidence='low',
               height_mm=None, height_source='unknown', confidence='medium',
               evidence='Closed plastic block', presence_confidence='medium',
               class_confidence='medium', asymmetry_landmark=None)
    row.update(changes)
    return row


def _normalized(cls, bbox=(.1, .1, .29, .255)):
    """Normalize one row on a 100 x 100 mm board (default plan 19 x 15.5 mm)."""
    return inventory._normalize(_row(cls, list(bbox)), 1, 100.0, 100.0, [])


def test_every_class_maps_to_a_known_kind():
    for name in inventory.CLASSES:
        assert inventory.class_kind(name) in COMPONENT_KINDS, name


def test_every_kind_is_a_class_kind_or_a_declared_package_variant():
    class_kinds = {inventory.class_kind(name) for name in inventory.CLASSES}
    variants = inventory.KIND_PACKAGE_VARIANTS
    assert not class_kinds & set(variants)
    assert set(COMPONENT_KINDS) == class_kinds | set(variants)
    # A variant refines a kind the vocabulary does name.
    assert set(variants.values()) <= class_kinds


def test_every_height_family_is_reachable_from_a_class():
    reachable = {inventory.CLASS_HEIGHT_FAMILIES.get(name, inventory.class_kind(name))
                 for name in inventory.CLASSES}
    families = (set(FAMILY_HEIGHT_MM) | set(PLAN_SCALED_RATIO) | {'battery'}) - {
        # Synonym of capacitor_electrolytic kept for detector naming.
        'electrolytic_capacitor'}
    missing = families - reachable - set(inventory.KIND_PACKAGE_VARIANTS)
    assert not missing


def test_yolo_classes_map_to_valid_kinds():
    assert set(YOLO_CLASS_KINDS) <= set(YOLO_CLASSES)
    assert set(YOLO_CLASS_KINDS.values()) <= set(COMPONENT_KINDS)
    # Every detector class with a same-named kind uses it instead of generic.
    for name in YOLO_CLASSES:
        if name in COMPONENT_KINDS:
            assert YOLO_CLASS_KINDS.get(name) == name, name


def test_schema_6_class_names_still_validate():
    assert set(SCHEMA_6_CLASSES) <= set(inventory.CLASSES)
    for name in SCHEMA_6_CLASSES:
        assert inventory._component_valid(_row(name, [.1, .1, .3, .3]), set()), name


def test_relay_gets_the_relay_height_rule_not_the_placeholder():
    row = _normalized('relay')
    assert row['kind'] == 'relay'
    assert row['length_mm'] == pytest.approx(19) and row['width_mm'] == pytest.approx(15.5)
    assert row['height_mm'] == pytest.approx(estimate_body_height('relay', 19, 15.5)[0])
    assert row['height_mm'] == pytest.approx(15.5)
    assert row['height_mm'] != inventory.PLACEHOLDER_HEIGHT_MM
    assert row['_height_estimated'] and row['_height_source'].endswith('_not_measured')


@pytest.mark.parametrize('cls', ['other', 'unknown'])
def test_unrecognised_body_keeps_generic_kind_and_placeholder(cls):
    row = _normalized(cls)
    assert row['kind'] == 'generic'
    assert row['height_mm'] == inventory.PLACEHOLDER_HEIGHT_MM
    assert row['_height_source'] == 'estimated_placeholder_not_measured'


def test_fuse_and_heatsink_are_generic_blocks_with_their_own_height():
    fuse = _normalized('fuse', (.1, .1, .132, .116))
    assert fuse['kind'] == 'generic' and fuse['height_mm'] == pytest.approx(FAMILY_HEIGHT_MM['fuse'])
    sink = _normalized('heatsink', (.1, .1, .24, .24))
    assert sink['kind'] == 'generic'
    assert sink['height_mm'] == pytest.approx(14 * PLAN_SCALED_RATIO['heatsink'])


def test_class_enum_in_schema_is_the_vocabulary():
    item = inventory.inventory_tool()['function']['parameters']['properties']['components']['items']
    assert item['properties']['class']['enum'] == list(inventory.CLASSES)
    assert inventory._component_valid(_row('relay', [.1, .1, .3, .3]), set())
    assert not inventory._component_valid(_row('toaster', [.1, .1, .3, .3]), set())
