import json

import numpy as np
import pytest

from kicadgen.services.crop_observation_images import prepare_observation_views


def component(**updates):
    row = dict(x_mm=0, y_mm=0, length_mm=4, width_mm=2, rotation_deg=0)
    row.update(updates)
    return row


def test_clean_detail_has_no_frame_and_context_retains_frame():
    photo = np.zeros((200, 200, 3), dtype=np.uint8)
    result = prepare_observation_views(photo, component(), 20, 20, 200)
    assert not result['detail_bgr'].any()
    assert np.any(np.all(result['context_bgr'] == (255, 0, 255), axis=2))
    assert result['detail_box_px'] == [64, 81, 136, 119]
    assert result['context_box_px'] == [56, 78, 144, 122]
    assert result['native_target_size_px'] == [40, 20]
    assert not photo.any()  # never annotates the source photograph
    json.dumps({k: v for k, v in result.items() if not k.endswith('_bgr')})


def test_anisotropy_corrected_without_changing_axes():
    photo = np.zeros((200, 400, 3), dtype=np.uint8)
    result = prepare_observation_views(photo, component(), 20, 20, 200)
    assert result['native_target_size_px'] == [80, 20]
    assert result['context_bgr'].shape[:2] == (100, 200)
    rotated = prepare_observation_views(photo, component(rotation_deg=90), 20, 20, 200)
    assert rotated['context_bgr'].shape[:2] == (200, 100)


def test_contacts_outside_frame_preserved_and_not_double_rendered():
    photo = np.zeros((200, 200, 3), dtype=np.uint8)
    # Target bottom y=110; a contact extending one third of its 20px height.
    photo[110:117, 99:102] = (255, 255, 255)
    result = prepare_observation_views(photo, component(), 20, 20, 200)
    assert result['detail_box_px'][3] >= 118
    assert result['detail_bgr'].max() == 255
    assert result['context_bgr'].max() == 255


def test_boundary_clipping_reported():
    result = prepare_observation_views(np.zeros((200, 200, 3), np.uint8),
                                       component(x_mm=-9, y_mm=-9), 20, 20)
    assert result['clipped_edges']['context'] == ['left', 'top']
    assert result['clipped_edges']['detail'] == ['left', 'top']
    assert result['context_box_px'][:2] == [0, 0]


def test_legacy_crop_uses_same_context_and_geometry_text():
    from kicadgen.services.photo_crop_reading import body_crop, crop_envelope_text
    photo = np.zeros((200, 400, 3), np.uint8)
    row = component(rotation_deg=90, bbox_basis='body')
    context, text = body_crop(photo, row, 20, 20, 200)
    views = prepare_observation_views(photo, row, 20, 20, 200)
    assert np.array_equal(context, views['context_bgr'])
    assert text == crop_envelope_text(row)
    assert '2.00 x 4.00 mm' in text
    assert 'body estimate; leads may extend' in text


def test_geometry_text_rejects_invalid_and_describes_oblique_axes():
    from kicadgen.services.photo_crop_reading import crop_envelope_text
    assert crop_envelope_text(component(length_mm=float('nan'))) is None
    assert crop_envelope_text({}) is None
    text = crop_envelope_text(component(rotation_deg=45))
    assert '4.24 x 4.24 mm' in text
    assert 'measurement extent uncertain' in text
    assert '45.0 degrees clockwise' in text


@pytest.mark.parametrize('row,board_l,board_w', [
    (component(length_mm=0), 20, 20), (component(x_mm=float('nan')), 20, 20),
    (component(x_mm=50), 20, 20), (component(), 0, 20),
    (component(), 20, float('inf')), ({}, 20, 20)])
def test_invalid_geometry_returns_none(row, board_l, board_w):
    assert prepare_observation_views(np.zeros((200, 200, 3), np.uint8),
                                     row, board_l, board_w) is None
