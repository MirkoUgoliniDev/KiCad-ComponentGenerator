"""@brief Category proposals and observed terminals remain independent."""
from test_photo_crop_reading import _row, _obs, _read, Script
from kicadgen.services.photo_global_inventory import retain_detector_category
from kicadgen.gui.photo_review_dialog import PhotoReviewDialog


def test_semiconductor_function_unknown_does_not_discard_six_observed_leads():
    row = _row(length_mm=3.15, width_mm=2.79, package='SOT-23-6', lead_count=6)
    obs = _obs(0, 0, 3, 3, contacts_complete=True,
               visual_class='semiconductor_unknown', visual_class_confidence='medium',
               visual_class_evidence='Six visible leads; electrical function unknown.')
    _read([row], Script(obs, obs))
    assert row['pin_count'] == 6 and row['lead_count_complete']
    assert row['class'] == 'ic'
    assert row['_crop_class_status'] != 'conflict'
    assert row['_terminal_status'] == 'supported'
    assert not row.get('_crop_terminal_review_required')


def test_detector_proposal_keeps_its_provenance_without_inventing_ai_confidence():
    row = dict(kind='generic', _yolo_class='resistor', _yolo_confidence=.83)
    retain_detector_category(row)
    assert row['class'] == row['kind'] == 'resistor'
    assert row['class_confidence'] == 'low'
    assert row['_yolo_confidence'] == .83
    assert row['_category_source'] == 'yolo_proposal'
    assert PhotoReviewDialog.classification_state(row) == 'proposed'


def test_global_ai_disagreement_keeps_detector_and_records_both():
    row = dict(kind='capacitor', **{'class': 'capacitor'}, class_confidence='high',
               _detector_evidence={'class': 'resistor', 'confidence': .9})
    retain_detector_category(row)
    assert row['class'] == 'resistor' and row['_ai_class_proposal'] == 'capacitor'
    assert PhotoReviewDialog.classification_state(row) == 'conflict'


def test_weak_detector_does_not_override_inventory():
    row = dict(kind='capacitor', **{'class': 'capacitor'}, _yolo_class='resistor', _yolo_low_confidence=True)
    retain_detector_category(row)
    assert row['class'] == 'capacitor'


def test_known_category_with_low_confidence_is_not_unknown():
    assert PhotoReviewDialog.classification_state({'class':'resistor', 'class_confidence':'low'}) == 'proposed'
    assert PhotoReviewDialog.classification_state({'class':'unknown'}) == 'unknown'
    assert PhotoReviewDialog.classification_state({'class':'ic', '_crop_class_status':'conflict'}) == 'conflict'


def test_malformed_category_values_are_missing_evidence_not_support():
    for value in (None, True, 0, ['diode'], {'category': 'diode'}):
        assert PhotoReviewDialog.classification_state({'class': value, 'kind': value}) == 'unknown'
    assert PhotoReviewDialog.classification_state({'class': 'resistor', 'kind': None}) == 'supported'


def test_disagreeing_terminal_counts_are_not_promoted():
    row = _row(length_mm=3.15, width_mm=2.79, package='SOT-23-6')
    a = _obs(0, 0, 3, 3, contacts_complete=True)
    b = _obs(0, 0, 3, 2, contacts_complete=True)
    _read([row], Script(a, b))
    assert row.get('_terminal_status') != 'supported'
    assert row.get('_crop_terminal_review_required')
    assert row.get('pin_count') is None


def test_failed_crop_call_cannot_erase_detector_proposal():
    from kicadgen.services.openrouter import OpenRouterError
    row = _row(**{'class': 'resistor', 'kind': 'resistor'}, _yolo_class='resistor', class_confidence='low')
    retain_detector_category(row)
    _read([row], Script(OpenRouterError('offline'), OpenRouterError('offline'), OpenRouterError('offline')))
    assert row['class'] == 'resistor'
    assert row['_category_source'] == 'yolo_proposal'


def test_raw_yolo_category_is_proposed_even_when_cad_kind_is_generic():
    row = dict(kind='generic', _yolo_class='resistor', _yolo_confidence=.83)
    assert PhotoReviewDialog.classification_state(row) == 'proposed'
    assert row['kind'] == 'generic'  # display correction does not alter detector geometry
    assert PhotoReviewDialog.classification_state(dict(kind='generic', _yolo_class=None)) == 'unknown'


def test_extractor_preserves_category_and_terminal_evidence_through_real_normalization():
    from kicadgen.services.datasheet_extractor import _components_3d
    evidence = dict(_category_source='yolo_proposal', _category_status='conflict',
                    _ai_class_proposal='diode', _ai_class_confidence='low',
                    _terminal_status='supported', _crop_terminal_review_required=True,
                    _crop_class_status='conflict', _crop_class_decided=False)
    row = _row(height_mm=1.2, **evidence)
    result = _components_3d([row], trusted=True)
    assert result and all(result[0].get(k) == v for k,v in evidence.items())
    raw = _components_3d([row], trusted=False)
    assert raw and not any(k in raw[0] for k in evidence)

