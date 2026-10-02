"""@brief Keep photographic matching evidence across the stored photo components."""


def test_matching_evidence_roundtrip(window):
    evidence = dict(presence_confidence='high', class_confidence='low',
                    confidence='medium', _yolo_review_only=True,
                    _yolo_origin='low_confidence_review', terminal_count=3,
                    terminal_layout='two plus one', terminal_count_basis='visible_including_tabs',
                    _global_inventory_status='partial',
                    _crop_class_status='conflict',
                    _crop_terminal_review_required=True,
                    **{'class': 'semiconductor_unknown'})
    window._set_photo_components([dict(name='unknown body', x_mm=0, y_mm=0,
        length_mm=3, width_mm=2, height_mm=1, **evidence)])
    box = window._components_from_table()[0]
    assert box.match_evidence == evidence
    from kicadgen.cad3d.step_generator import _match_evidence
    assert _match_evidence(box)['_crop_class_status'] == 'conflict'
    assert _match_evidence(box)['_crop_terminal_review_required'] is True
    # Il giro completo: dai dati salvati di nuovo ai componenti.
    window._set_photo_components(window._photo_components_from_table())
    assert window._components_from_table()[0].match_evidence == evidence
