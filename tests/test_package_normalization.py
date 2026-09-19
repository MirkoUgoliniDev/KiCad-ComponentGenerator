"""@brief Package prose must preserve evidence and yield only declared candidates."""

import pytest

from kicadgen.cad3d import kicad_models as km
from kicadgen.services.datasheet_extractor import _components_3d


@pytest.fixture
def library(tmp_path, monkeypatch):
    def add(folder, name, size):
        path = tmp_path / (folder + '.3dshapes') / (name + '.step')
        path.parent.mkdir(exist_ok=True)
        path.touch()
        sizes[str(path)] = {'body': size, 'total': size}
        return path
    sizes = {}
    paths = {
        'r': add('Resistor_SMD', 'R_0603_1608Metric', [1.6, .8, .4]),
        'c': add('Capacitor_SMD', 'C_0603_1608Metric', [1.6, .8, .4]),
        'metric': add('Resistor_SMD', 'R_0201_0603Metric', [.6, .3, .2]),
        'id': add('Custom', 'Vendor_ABC0603', [1.6, .8, .4]),
        'sot': add('Package_TO_SOT_SMD', 'SOT-23', [3, 1.3, 1]),
        'sot230': add('Package_TO_SOT_SMD', 'SOT-230', [3, 1.3, 1]),
    }
    monkeypatch.setattr(km, 'model_sizes', lambda paths, library=None: {str(p): sizes[str(p)] for p in paths})
    monkeypatch.setattr(km, 'footprint_sizes', lambda *a, **k: {})
    return tmp_path, paths


@pytest.mark.parametrize('text', [
    'chip passive, ~1.6x0.85 mm (0603-class)',
    '0603-class',
    'resistor package: 0603, body approximately 1.6 x 0.8 mm',
    'small rectangular component with uncertain visual details ' * 3 + '(0603-class)',
])
def test_prose_survives_extraction_and_matches_with_family_and_size_gates(library, text):
    root, paths = library
    source = dict(package=text, kind='resistor', x_mm=0, y_mm=0,
                  length_mm=1.6, width_mm=.85, height_mm=.4)
    component = _components_3d([source])[0]
    assert component['package'] == text
    report = km.model_candidates(component, library=root)
    assert [c['path'] for c in report['candidates']] == [str(paths['r'])]
    assert report['selected_path'] is None
    assert source['package'] == text
    component.update(length_mm=10, width_mm=10)
    assert km.model_candidates(component, library=root)['candidates'] == []


@pytest.mark.parametrize('text', ['', None, 'chip passive, 1.6 x 0.8 mm',
    'product ID 0603', 'resistor part number 0603', 'ABC0603 component',
    'package 06030', '0603-class or 0805-class', '123456'])
def test_no_invented_or_ambiguous_numeric_package(library, text):
    root, _ = library
    assert km.candidates(text, root) == []


@pytest.mark.parametrize('text', ['0603 metric', 'metric 0603', '0603Metric', '0603 (metric)'])
def test_metric_namespace_never_matches_imperial(library, text):
    root, paths = library
    assert km.candidates(text, root) == [paths['metric']]


def test_imperial_code_never_matches_metric_suffix_or_product_id(library):
    root, paths = library
    assert set(km.candidates('0603', root)) == {paths['r'], paths['c']}


def test_named_prose_ignores_dimensions_and_preserves_numeric_boundary(library):
    root, paths = library
    assert km.candidates('three terminal SOT-23 body 3 x 1.3 mm', root) == [paths['sot']]


@pytest.mark.parametrize('suffix', ['', '.step'])
def test_exact_identifier_is_not_broadened(library, suffix):
    root, paths = library
    assert km.candidates('R_0603_1608Metric' + suffix, root) == [paths['r']]
    assert km.candidates('Vendor_ABC0603' + suffix, root) == [paths['id']]


def test_missing_package_is_preserved_as_missing():
    component = _components_3d([dict(x_mm=0, y_mm=0, length_mm=1.6,
                                   width_mm=.8, height_mm=.4)])[0]
    assert 'package' not in component


@pytest.mark.parametrize('text,expected', [
    ('passive package 0402, approximately 1 x 0.5 mm', '0402'),
    ('chip, roughly 2 x 1.25 mm (0805-class)', '0805'),
    ('capacitor case: 1206', '1206'),
    ('IC in SOIC-8 package, 3.9 x 4.9 mm', 'soic8'),
    ('leadless QFN-32, 5 x 5 mm', 'qfn32'),
    ('transistor in SOT-23-5 package', 'sot235'),
    ('SMD crystal 3225', 'crystalsmd3225'),
    ('SMD_Crystal_3225', 'crystalsmd3225'),
])
def test_normalization_generalizes_across_packages(text, expected):
    assert km._package_name(text) == expected


@pytest.mark.parametrize('review_only,ambiguous', [(True, True), (False, False)])
def test_extractor_preserves_detector_metadata_and_independent_confidence(review_only, ambiguous):
    metadata = dict(presence_confidence='high', class_confidence='low',
                    _yolo_proposal_id='tile-1:7', _yolo_class_ambiguous=ambiguous,
                    _yolo_review_only=review_only,
                    _yolo_origin='low_confidence_review' if review_only else 'operating_threshold')
    source = dict(x_mm=0, y_mm=0, length_mm=1.6, width_mm=.8,
                  height_mm=.4, confidence='medium', **metadata)
    component = _components_3d([source])[0]
    assert {key: component[key] for key in metadata} == metadata
    assert component['confidence'] == 'medium'
    assert 'package' not in component


def test_absent_detector_metadata_is_not_invented():
    component = _components_3d([dict(x_mm=0, y_mm=0, length_mm=1.6,
                                   width_mm=.8, height_mm=.4)])[0]
    assert not {'presence_confidence', 'class_confidence', '_yolo_proposal_id',
                '_yolo_class_ambiguous', '_yolo_review_only', '_yolo_origin'}.intersection(component)


def test_mechanical_metadata_copy_preserves_long_package_through_extractor():
    from kicadgen.services.photo_inventory import copy_mechanical_metadata

    package = 'small rectangular component with uncertain visual details ' * 3 + '(0603-class)'
    target = dict(x_mm=0, y_mm=0, length_mm=1.6, width_mm=.8, height_mm=.4)
    copy_mechanical_metadata(target, dict(package=package, package_evidence='x' * 600))
    component = _components_3d([target])[0]
    assert component['package'] == package
    assert km._package_name(component['package']) == '0603'
    assert component['package_evidence'] == 'x' * 500
    copy_mechanical_metadata(target, {'package': '0805'}, overwrite=False)
    assert target['package'] == package


@pytest.mark.parametrize('provenance', [
    {'_global_inventory_status': 'validated_hypotheses'},
    {'_global_inventory_status': 'partial'},
    {'_yolo_proposal_id': 'tile-1:7', '_yolo_origin': 'low_confidence_review'},
    {'_yolo_proposal_id': 'overview:2', '_yolo_origin': 'operating_threshold'},
])
@pytest.mark.parametrize('name', ['pad (unknown)', 'via (unknown)', 'terminal pad (unknown)'])
def test_physical_hypotheses_do_not_depend_on_display_names(provenance, name):
    source = dict(name=name, component_id='pad', kind='generic', x_mm=0, y_mm=0,
                  length_mm=1, width_mm=.5, height_mm=.3, **provenance)
    assert _components_3d([source])[0]['name'] == name
    assert _components_3d([dict(source, object_type='pcb_feature')]) is None
    assert _components_3d([dict(source, width_mm=0)]) is None


@pytest.mark.parametrize('provenance', [{}, {'_global_inventory_status': 'invalid'},
    {'_yolo_proposal_id': 'tile-1:7'}, {'_yolo_origin': 'low_confidence_review'}])
def test_legacy_pcb_feature_name_filter_still_applies(provenance):
    source = dict(name='pad (unknown)', x_mm=0, y_mm=0, length_mm=1,
                  width_mm=.5, height_mm=.3, **provenance)
    assert _components_3d([source]) is None


@pytest.mark.parametrize('text', ['ABC0603-class', 'Vendor-0603-class', '06030-class'])
def test_class_suffix_does_not_extract_embedded_product_codes(library, text):
    assert km.candidates(text, library[0]) == []
