"""Protect crop coordinates, rotation TTA and the existing KiCad matcher boundary."""
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest

from kicadgen.services import smd_component_classifier as smd


def test_board_preparation_shares_the_full_frame_for_component_margins(monkeypatch):
    from kicadgen.services import photo_inventory, photo_refine
    photo = np.arange(50 * 80 * 3, dtype=np.uint8).reshape(50, 80, 3)
    monkeypatch.setattr(photo_inventory, "read_photo", lambda path: photo)
    monkeypatch.setattr(photo_refine, "find_board", lambda *a: SimpleNamespace(
        cx=40, cy=25, width_px=60, height_px=40))
    frames = []
    board = photo_inventory.prepare_photo(
        "unused.png", 60, 40, frame_callback=lambda frame, box: frames.append((frame, box)))
    assert frames[0][0] is photo
    assert frames[0][1] == (10, 5, 70, 45)
    assert np.array_equal(board, photo[5:45, 10:70])


@pytest.fixture
def network(monkeypatch):
    torch = pytest.importorskip("torch")
    torchvision = pytest.importorskip("torchvision")

    class ControlledNet(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.calls = []
            self.other = False

        def forward(self, images):
            turn = len(self.calls) % 4
            self.calls.append(images.detach().cpu())
            classes = torch.zeros(len(images), 2, device=images.device)
            classes[:, 1 if self.other else 0] = 10
            # A quarter-turn input adds the same turn to the predicted angle.
            rotation = torch.roll(torch.tensor([0., 10., 0., 0.], device=images.device), turn)
            return classes, rotation.repeat(len(images), 1)

    net = ControlledNet()
    checkpoint = {"format": "smdmap-classifier-2", "arch": "resnet18", "img_size": 224,
                  "classes": ["SOT-23", "_other"], "state_dict": {},
                  "orientations": {"SOT-23": "full", "_other": "none"},
                  "suggested_threshold": 0.0}
    monkeypatch.setattr(torch, "load", lambda *a, **k: checkpoint)
    monkeypatch.setattr(smd, "_runtime", lambda: (torch, torchvision.transforms, lambda count: net))
    return net


def test_four_turns_are_returned_to_the_original_rotation(network):
    result = smd.classify_photo_boxes(Image.new("RGB", (40, 40)), [[10, 10, 30, 30]])[0]
    assert len(network.calls) == 4
    assert result["accepted"]
    assert result["rotation_deg"] == 90
    assert result["rotation_confidence"] > .99
    assert result["footprint_status"] == "needs_scale"


def test_board_edge_uses_full_photo_margin_and_bgr_colors(network):
    from torchvision import transforms
    # Gradient makes missing context and swapped red/blue channels detectable.
    rgb = np.arange(40 * 60 * 3, dtype=np.uint8).reshape(40, 60, 3)
    result = smd.classify_photo_boxes(
        rgb[:, :, ::-1].copy(), [[0, .25, .5, .75]],
        box_format="normalized", box_frame_px=[10, 10, 30, 30],
    )[0]
    assert result["box_px"] == [10, 15, 20, 25]
    # 10% per side extends left of the PCB ROI into the full photo.
    expected_crop = Image.fromarray(rgb).crop((9, 14, 21, 26))
    expected = transforms.Normalize(smd._MEAN, smd._STD)(
        transforms.ToTensor()(smd._Letterbox()(expected_crop)))
    assert np.array_equal(network.calls[0][0].numpy(), expected.numpy())


def test_other_is_rejected_before_model_matching(network, monkeypatch):
    network.other = True
    from kicadgen.cad3d import kicad_models
    monkeypatch.setattr(kicad_models, "model_candidates", lambda *a, **k: pytest.fail("unknown matched"))
    result = smd.classify_photo_boxes(
        Image.new("RGB", (40, 40)), [[10, 10, 30, 30]], mm_per_pixel=(.1, .2))[0]
    assert result["class_confidence"] > .99
    assert not result["accepted"]
    assert result["rotation_deg"] is None
    assert result["footprint"] is None
    assert result["step_model"] is None


def test_existing_matcher_receives_unpadded_metric_box_and_links_footprint(network, monkeypatch, tmp_path):
    from kicadgen.cad3d import kicad_models
    step = tmp_path / "3dmodels" / "Package_TO_SOT_SMD.3dshapes" / "SOT-23.step"
    step.parent.mkdir(parents=True)
    step.write_bytes(b"inert test model")
    footprint = tmp_path / "footprints" / "Package_TO_SOT_SMD.pretty" / "SOT-23.kicad_mod"
    seen = []

    def match(component, **kwargs):
        seen.append(component)
        return {"status": "provisional_matched", "selected_path": str(step),
                "candidates": [{"path": str(step), "package": "SOT-23"}]}

    monkeypatch.setattr(kicad_models, "model_candidates", match)
    monkeypatch.setattr(kicad_models, "footprint_sizes", lambda paths, **kwargs: {
        str(step): [{"source": str(footprint), "size": [2.9, 2.4]}]})
    result = smd.classify_photo_boxes(
        Image.new("RGB", (40, 40)), [[10, 10, 30, 20]], mm_per_pixel=(.1, .3))[0]
    assert result["size_mm"] == [2., 3.]
    assert (seen[0]["length_mm"], seen[0]["width_mm"]) == (3., 2.)
    assert seen[0]["bbox_basis"] == "unknown"
    assert result["model_status"] == "provisional_matched"
    assert result["footprint"] == "Package_TO_SOT_SMD:SOT-23"
    assert result["step_model"]["file"] == str(step)


def test_multiple_linked_footprints_remain_alternatives(network, monkeypatch, tmp_path):
    from kicadgen.cad3d import kicad_models
    step = tmp_path / "3dmodels" / "Package_TO_SOT_SMD.3dshapes" / "SOT-23.step"
    monkeypatch.setattr(kicad_models, "model_candidates", lambda *a, **k: {
        "status": "provisional_matched", "selected_path": str(step), "candidates": []})
    monkeypatch.setattr(kicad_models, "footprint_sizes", lambda *a, **k: {
        str(step): [{"source": str(tmp_path / "Package_TO_SOT_SMD.pretty" / f"{name}.kicad_mod"),
                     "size": [2.9, 2.4]} for name in ("SOT-23", "SOT-23_Handsoldering")]})
    result = smd.classify_photo_boxes(
        Image.new("RGB", (40, 40)), [[10, 10, 30, 30]], mm_per_pixel=(.1, .1))[0]
    assert result["footprint"] is None
    assert result["footprint_status"] == "ambiguous"
    assert len(result["footprint_candidates"]) == 2


@pytest.mark.parametrize('other', [False, True])
def test_photo_inventory_classifies_reviewed_boxes_on_full_frame(network, monkeypatch, tmp_path, other):
    from kicadgen.services import photo_inventory as inv, photo_yolo, datasheet_extractor as ext
    from kicadgen.gui import photo_review_dialog as review
    from PIL import Image
    import json
    network.other = other
    rgb = np.arange(40 * 60 * 3, dtype=np.uint8).reshape(40, 60, 3)
    full = rgb[:, :, ::-1].copy()
    phases, seen_models = [], []

    def prepare(path, length, width, calibration, *, frame_callback):
        frame_callback(full, (10, 10, 30, 30))
        return full[10:30, 10:30].copy()

    proposal = dict(_id=1, _box=[.2, .2, .6, .4], _yolo_class='resistor',
                    x_mm=0., y_mm=0., length_mm=5., width_mm=5., height_mm=1.,
                    rotation_deg=0, _height_estimated=True, color_hex='#303030')

    def detect(photo, length, width, **kwargs):
        seen_models.append(kwargs['model_path'].name)
        return [proposal]

    def reviewed(photo_png, rows, length, width):
        return [dict(rows[0], _box=[0, .25, .5, .75])]

    monkeypatch.setattr(inv, 'prepare_photo', prepare)
    monkeypatch.setattr(photo_yolo, 'detect_yolo_components', detect)
    monkeypatch.setattr(inv, 'measure_proposal_edges', lambda photo, rows, *args: rows)
    monkeypatch.setattr(smd, '_attach_kicad_results', lambda *args: None)
    # Solo i corpi che la rete non risolve passano alla lettura AI dei ritagli
    # (utente, 2026-10-02); la chiamata remota vera resta vietata dal fail.
    rechecked = []

    def read_bodies(photo, rows, *args, **kwargs):
        rechecked.extend(rows)
        return SimpleNamespace(warning=None)
    from kicadgen.services import photo_crop_reading
    monkeypatch.setattr(photo_crop_reading, 'read_bodies', read_bodies)
    result = inv.extract_inventory(
        ['top.png'], 'unused-remote-model', 10, 10, [],
        lambda **kw: pytest.fail('Remote photo call'), top_photo_index=0,
        detector='yolo_smd', cache_dir=tmp_path, review_proposals=reviewed,
        phase=lambda *args: phases.append(args))
    row = result.components[0]
    assert row['_smd_prediction']['box_px'] == [10, 15, 20, 25]
    assert row['_smd_prediction']['size_mm'] == [5., 5.]
    assert row['kind'] == ('generic' if other else 'ic')
    assert rechecked == ([row] if other else [])
    assert row.get('_smd_ai_recheck', False) is other
    assert result.calibration['smd_resolved_count'] == (0 if other else 1)
    assert result.calibration['smd_ai_recheck_count'] == (1 if other else 0)
    if other:
        # Nessuna lettura concorde nello stub: il corpo resta generico.
        assert row['class'] == 'generic'
        assert any(phase[0] == 'llm_crops' for phase in phases)
    else:
        assert not any(phase[0] == 'llm_crops' for phase in phases)
    assert row['_smd_rotation_deg'] == (None if other else 90)
    assert row['rotation_deg'] == 0  # Geometric body angle stays independent.
    assert seen_models == ['pcb_component_detector_yolov8m_best.pt']
    assert any(phase[0] == 'smd' for phase in phases)
    assert result.calibration['inventory_strategy'] == 'local-detector-smd'
    assert result.overlay_png.startswith(b'\x89PNG')
    expected = smd._crop(Image.fromarray(rgb), [10, 15, 20, 25], .1)
    from torchvision import transforms
    tensor = transforms.Normalize(smd._MEAN, smd._STD)(
        transforms.ToTensor()(smd._Letterbox()(expected)))
    assert np.array_equal(network.calls[0][0].numpy(), tensor.numpy())
    normalized = ext._components_3d(result.components, trusted=True)[0]
    assert normalized['_smd_prediction'] == row['_smd_prediction']
    sections = review.component_attribute_sections(normalized, lambda text: text)
    assert sections[1][0] == 'Rete SMD locale'
    assert any(label == 'Rotazione KiCad proposta' for label, _ in sections[1][2])
    outcome = dict(sections[1][2])['Esito rete SMD']
    assert outcome == ('non riconosciuto: riverifica AI del ritaglio' if other
                       else 'risolto in locale, nessuna chiamata AI')
    # Evidence must be serializable, without full photo arrays in metadata.
    json.dumps(result.calibration, allow_nan=False)
    json.dumps(result.components, allow_nan=False)


def test_callable_cancellation_stops_classification(network):
    with pytest.raises(RuntimeError, match='annullata'):
        smd.classify_photo_boxes(Image.new('RGB', (40, 40)), [[10, 10, 30, 30]],
                                 cancel=lambda: True)
    assert not network.calls


def test_preflight_accepts_a_structurally_compatible_checkpoint(monkeypatch, tmp_path):
    torch = pytest.importorskip("torch")
    path = tmp_path / "classifier.pt"
    path.write_bytes(b"mock checkpoint")
    factory = lambda count: torch.nn.Linear(1, 1)
    checkpoint = {
        "format": "smdmap-classifier-2", "arch": "resnet18", "img_size": 224,
        "classes": ["chip_R"], "orientations": {"chip_R": "axis"},
        "suggested_threshold": 0.5, "state_dict": factory(1).state_dict(),
    }
    monkeypatch.setattr(smd, "_runtime", lambda: (torch, None, factory))
    monkeypatch.setattr(torch, "load", lambda *args, **kwargs: checkpoint)
    assert smd.validate_smd_setup(path) == path


@pytest.mark.parametrize("mutate,reason", [
    (lambda c: c.pop("classes"), "classes"),
    (lambda c: c.update(orientations={"chip_R": "diagonal"}), "orientations"),
    (lambda c: c.update(orientations={}), "orientations"),
    (lambda c: c.update(suggested_threshold=float("nan")), "suggested_threshold"),
    (lambda c: c.update(suggested_threshold=1.1), "suggested_threshold"),
    (lambda c: c.update(suggested_threshold=True), "suggested_threshold"),
    (lambda c: c.update(state_dict={}), "state_dict keys"),
    (lambda c: c["state_dict"]["weight"].fill_(float("nan")), "non finito"),
    (lambda c: c["state_dict"].update(
        weight=c["state_dict"]["weight"].to_sparse()),
     "state_dict weight"),
])
def test_preflight_rejects_checkpoints_incompatible_with_inference(
    monkeypatch, tmp_path, mutate, reason,
):
    torch = pytest.importorskip("torch")
    path = tmp_path / "malformed.pt"
    path.write_bytes(b"mock checkpoint")
    factory = lambda count: torch.nn.Linear(1, 1)
    checkpoint = {
        "format": "smdmap-classifier-2", "arch": "resnet18", "img_size": 224,
        "classes": ["chip_R"], "orientations": {"chip_R": "axis"},
        "suggested_threshold": 0.5, "state_dict": factory(1).state_dict(),
    }
    mutate(checkpoint)
    monkeypatch.setattr(smd, "_runtime", lambda: (torch, None, factory))
    monkeypatch.setattr(torch, "load", lambda *args, **kwargs: checkpoint)
    with pytest.raises(ValueError, match=reason):
        smd.validate_smd_setup(path)


def test_local_network_fields_are_not_trusted_in_remote_answers():
    from kicadgen.services import datasheet_extractor as ext
    row = dict(name='fake', kind='ic', x_mm=0, y_mm=0, length_mm=2,
               width_mm=1, height_mm=1, _smd_class='SOIC-8', footprint='fake:fake',
               model_match={'status': 'matched'}, footprint_candidates=['fake'])
    normalized = ext._components_3d([row])[0]
    assert '_smd_class' not in normalized
    assert 'footprint' not in normalized
    assert 'model_match' not in normalized


def _smd_inventory(monkeypatch, tmp_path, classify, cancel):
    """Run the real yolo_smd branch with a stubbed detector and classifier."""
    from kicadgen.services import photo_inventory as inv, photo_yolo
    full = np.zeros((40, 60, 3), np.uint8)

    def prepare(path, length, width, calibration, *, frame_callback):
        frame_callback(full, (10, 10, 30, 30))
        return full[10:30, 10:30].copy()
    proposal = dict(_id=1, _box=[.2, .2, .6, .4], _yolo_class='resistor', x_mm=0., y_mm=0.,
                    length_mm=5., width_mm=5., height_mm=1., rotation_deg=0,
                    _height_estimated=True, color_hex='#303030')
    monkeypatch.setattr(inv, 'prepare_photo', prepare)
    monkeypatch.setattr(photo_yolo, 'detect_yolo_components', lambda *a, **k: [proposal])
    monkeypatch.setattr(inv, 'measure_proposal_edges', lambda photo, rows, *args: rows)
    monkeypatch.setattr(smd, 'classify_photo_boxes', classify)
    return inv.extract_inventory(
        ['top.png'], 'unused-remote-model', 10, 10, [],
        lambda **kw: pytest.fail('Remote photo call'), top_photo_index=0,
        detector='yolo_smd', cache_dir=tmp_path, cancel=cancel)


@pytest.mark.parametrize('cancelled', [True, False])
def test_cancelling_the_network_stops_the_run_instead_of_degrading_to_a_failure(
        monkeypatch, tmp_path, cancelled):
    """@brief Annullare durante la rete deve fermare la corsa (audit 2026-10-02).

    @note La rete annulla con eccezioni sue; l'estrattore degrada ogni altra
        eccezione a «inventario fallito» e prosegue con gli ingombri a occhio.
        Caso negativo: un guasto vero, senza annullo, resta un guasto.
    """
    from kicadgen.services.ai_diagnostics import CallCancelled

    def classify(*args, **kwargs):
        raise RuntimeError('Classificazione SMD annullata')
    expected = CallCancelled if cancelled else RuntimeError
    with pytest.raises(expected) as caught:
        _smd_inventory(monkeypatch, tmp_path, classify, lambda: cancelled)
    assert isinstance(caught.value, CallCancelled) is cancelled


def _pending_row(**extra):
    return dict(_id=7, _smd_prediction={'accepted': False}, x_mm=0., y_mm=0., length_mm=3.,
                width_mm=1.5, rotation_deg=0, **extra)


def test_recheck_keeps_the_reasons_the_crop_reading_wrote(monkeypatch):
    """@brief Il motivo aggiunto dalla riverifica va in testa, non al posto (audit A1).

    @note Caso negativo: un corpo che la rete ha risolto non viene toccato.
    """
    from kicadgen.services import photo_inventory as inv, photo_crop_reading

    def read_bodies(photo, rows, *args, **kwargs):
        for row in rows:
            row['class'] = 'ic'
            row['_review_reason'] = 'package lasciato vuoto, da verificare.'
        return SimpleNamespace(warning=None)
    monkeypatch.setattr(photo_crop_reading, 'read_bodies', read_bodies)
    resolved = dict(_id=1, _smd_prediction={'accepted': True}, **{'class': 'resistor'})
    pending = _pending_row()
    report = inv.InventoryResult()
    sent = inv.recheck_unresolved_smd_rows(None, report, [resolved, pending], 10, 10, 'm',
                                           lambda **kw: None, lambda m: None, None, None,
                                           lambda *a: None)
    assert sent == [pending]
    assert pending['_review_reason'].startswith('Rete locale: tipo non riconosciuto')
    assert pending['_review_reason'].endswith('package lasciato vuoto, da verificare.')
    assert pending['name'] == 'ic 7' and pending['_smd_ai_recheck'] is True
    assert '_review_reason' not in resolved and resolved['class'] == 'resistor'


@pytest.mark.parametrize('read_class, final_class, claims_type', [
    ('ic', 'ic', True),
    ('semiconductor_unknown', 'semiconductor_unknown', False),
    ('unknown', 'generic', False),
])
def test_recheck_claims_a_type_only_for_a_specific_class(monkeypatch, read_class, final_class,
                                                        claims_type):
    """@brief Una famiglia non specifica non e' «tipo letto dall'AI» (audit, sospetto 3)."""
    from kicadgen.services import photo_inventory as inv, photo_crop_reading

    def read_bodies(photo, rows, *args, **kwargs):
        rows[0]['class'] = read_class
        return SimpleNamespace(warning=None)
    monkeypatch.setattr(photo_crop_reading, 'read_bodies', read_bodies)
    pending = _pending_row()
    inv.recheck_unresolved_smd_rows(None, inv.InventoryResult(), [pending], 10, 10, 'm',
                                    lambda **kw: None, lambda m: None, None, None,
                                    lambda *a: None)
    assert pending['class'] == final_class
    assert ('Rete locale: tipo non riconosciuto' in (pending.get('_review_reason') or '')) is claims_type


def test_a_box_dragged_past_the_photo_edge_does_not_wipe_the_inventory(monkeypatch, tmp_path):
    """@brief Un riquadro oltre il bordo si ritaglia, non annulla il lotto (audit D2).

    @note Caso negativo: un riquadro senza area dentro la foto non viene
        mandato alla rete e resta da rivedere, senza toccare gli altri.
    """
    from kicadgen.services import photo_inventory as inv
    sent = []

    def classify(photo, boxes, **kwargs):
        sent.extend(boxes)
        assert all(0 <= v <= 1 for box in boxes for v in box)
        return [dict(_outside_like(), accepted=True, **{'class': 'chip_R'}, kind='resistor',
                     package='SMD chip resistor') for _ in boxes]
    monkeypatch.setattr(smd, 'classify_photo_boxes', classify)
    rows = [dict(_id=1, _box=[-0.2, 0.1, 0.4, 0.3]), dict(_id=2, _box=[1.2, 0.1, 1.4, 0.3])]
    predictions = inv.classify_smd_rows(rows, np.zeros((20, 20, 3), np.uint8), None, (1, 1),
                                        lambda m: None, None, lambda *a: None)
    assert sent == [[0.0, 0.1, 0.4, 0.3]]
    assert predictions[0]['accepted'] and rows[0]['kind'] == 'resistor'
    assert not predictions[1]['accepted'] and rows[1]['kind'] == 'generic'


def _outside_like():
    from kicadgen.services.photo_inventory import _outside_prediction
    return _outside_prediction()


@pytest.mark.parametrize('detector, paid', [('yolo_smd', []), ('yolo', ['report_global_inventory'])])
def test_relaunching_a_smd_bundle_does_not_pay_a_global_inventory(monkeypatch, detector, paid):
    """@brief «Rilancia inventario» su un pacchetto SMD rifa' la rete, non l'AI (audit D1).

    @note Caso negativo: un pacchetto YOLO normale paga ancora la sua
        riconciliazione globale.
    """
    from kicadgen.services import photo_inventory as inv
    calls = []

    def call(**kwargs):
        calls.append(kwargs['tool']['function']['name'])
        raise RuntimeError('stop after recording')
    monkeypatch.setattr(smd, 'classify_photo_boxes', lambda photo, boxes, **kw: [
        dict(_outside_like(), accepted=True, **{'class': 'chip_R'}, kind='resistor')
        for _ in boxes])
    proposal = dict(_id=1, _box=[.2, .2, .4, .4], _yolo_class='resistor', x_mm=0., y_mm=0.,
                    length_mm=2., width_mm=1., height_mm=.5, rotation_deg=0,
                    _height_estimated=True, color_hex='#303030')
    photo = np.full((40, 80, 3), 90, np.uint8)
    try:
        report = inv.reconcile_saved_proposals(photo, [proposal], 20, 10, 'offline/m', call,
                                               lambda m: None, None, detector=detector)
    except Exception:
        report = None
    assert calls[:1] == paid
    if detector == 'yolo_smd':
        assert report.calibration['inventory_strategy'] == 'local-detector-smd'
        assert report.components[0]['kind'] == 'resistor'


def test_a_review_bundle_remembers_whether_a_person_reviewed_it(tmp_path):
    """@brief Fallback bundles from the cache are not reviews (audit 2026-10-02, D6).

    @note Negative: a bundle written before the field existed came from the
        review window, so it still reads as reviewed.
    """
    import json
    from kicadgen.services import photo_inventory as inv
    row = dict(_id=1, _box=[.1, .1, .3, .3])
    inv.save_review_bundle(tmp_path, [row], None, 20, 10, detector='yolo_smd', reviewed=False)
    bundle = inv.load_review_bundle(tmp_path)
    assert bundle.detector == 'yolo_smd' and bundle.reviewed is False
    path = bundle.json_path
    document = json.loads(path.read_text(encoding='utf-8'))
    document.pop('reviewed')
    path.write_text(json.dumps(document), encoding='utf-8')
    assert inv.load_review_bundle(tmp_path).reviewed is True


@pytest.mark.parametrize('reviewed', [True, False])
def test_an_unreviewed_relaunch_gives_no_body_a_person_kept(monkeypatch, reviewed):
    """@brief Only a reviewed bundle makes its rows «kept by a person» (audit D6)."""
    from kicadgen.services import photo_inventory as inv
    monkeypatch.setattr(smd, 'classify_photo_boxes', lambda photo, boxes, **kw: [
        dict(_outside_like(), accepted=True, **{'class': 'chip_R'}, kind='resistor')
        for _ in boxes])
    proposal = dict(_id=1, _box=[.2, .2, .4, .4], _yolo_class='resistor', x_mm=0., y_mm=0.,
                    length_mm=2., width_mm=1., height_mm=.5, rotation_deg=0,
                    _height_estimated=True, color_hex='#303030')
    report = inv.reconcile_saved_proposals(np.full((40, 80, 3), 90, np.uint8), [proposal], 20, 10,
                                           'offline/m', lambda **kw: pytest.fail('paid call'),
                                           lambda m: None, None, detector='yolo_smd',
                                           reviewed=reviewed)
    assert report.components[0]['_presence_reviewed_by_user'] is reviewed


@pytest.mark.parametrize('chosen', [None, 'other_network.pt'])
def test_the_chosen_network_reaches_the_classification_and_names_the_phase(monkeypatch, tmp_path,
                                                                           chosen):
    """@brief «Modello rete SMD» (2026-10-02): the file is used and written in the log.

    @note The phase row says WHAT the step does, not which file (user, same
        day): the file name goes to the log only.
    """
    from kicadgen.services import photo_inventory as inv
    from kicadgen.app_paths import smd_model_path
    seen, phases = {}, []

    def classify(photo, boxes, **kwargs):
        seen['model_path'] = kwargs.get('model_path')
        return [dict(_outside_like(), accepted=True, **{'class': 'chip_R'}, kind='resistor')
                for _ in boxes]
    monkeypatch.setattr(smd, 'classify_photo_boxes', classify)
    path = tmp_path / chosen if chosen else None
    logs = []
    inv.classify_smd_rows([dict(_id=1, _box=[.1, .1, .3, .3])], np.zeros((20, 20, 3), np.uint8),
                          None, (1, 1), logs.append, None, lambda *a: phases.append(a),
                          model_path=path)
    expected = path if path is not None else smd_model_path()
    assert seen['model_path'] == expected
    smd_phase = [detail for tool, detail, *rest in phases if tool == 'smd']
    assert smd_phase and 'classifica i corpi' in smd_phase[0]
    assert expected.name not in smd_phase[0]
    assert any(str(expected) in str(line) for line in logs)
    assert expected.parent.name in ('smd', tmp_path.name)


def test_a_box_outside_the_photo_is_not_sent_to_the_ai_recheck(monkeypatch):
    """@brief Review A6 (2026-10-02): no crop, no recheck, not counted as one.

    @note Negative: the unresolved box inside the photo is still sent.
    """
    from kicadgen.services import photo_inventory as inv, photo_crop_reading
    read = []

    def read_bodies(photo, rows, *args, **kwargs):
        read.extend(rows)
        return SimpleNamespace(warning=None)
    monkeypatch.setattr(photo_crop_reading, 'read_bodies', read_bodies)
    outside = dict(_id=3, _smd_prediction=_outside_like(), kind='generic',
                   **{'class': 'generic'})
    pending = _pending_row()
    report = inv.InventoryResult()
    sent = inv.recheck_unresolved_smd_rows(None, report, [outside, pending], 10, 10, 'm',
                                           lambda **kw: None, lambda m: None, None, None,
                                           lambda *a: None)
    assert sent == [pending] and read == [pending]
    assert report.calibration['smd_ai_recheck_count'] == 1
    assert report.calibration['smd_resolved_count'] == 0
    assert '_smd_ai_recheck' not in outside


# ── ONNX export of the network (2026-10-28) ───────────────────────────────
# The installed program has no PyTorch: the network ships as an .onnx graph
# that takes the normalised batch and returns the two heads' logits, with the
# checkpoint's metadata as JSON under ONNX_METADATA_KEY. Verified the same day
# on real boxes of two photos: same classes, same rotations, confidences
# within 1e-5 of the checkpoint's. Here a fake onnxruntime drives the route.
import json
import sys

_ONNX_META = {"format": "smdmap-classifier-2", "arch": "resnet18", "img_size": 224,
              "classes": ["SOT-23", "_other"],
              "orientations": {"SOT-23": "full", "_other": "none"},
              "suggested_threshold": 0.0, "trained": "20261001-162356", "epoch": 40,
              "catalog_sha256": "ab" * 32,
              "metrics": {"val": {"samples": 42, "accuracy": 1.0, "balanced_accuracy": 1.0}}}


def _fake_onnx(monkeypatch, metadata=_ONNX_META, *, in_shape=("batch", 3, 224, 224),
               out_shapes=(("batch", 2), ("batch", 4)), fail_session=False):
    """Install a fake onnxruntime whose session answers like ControlledNet of the torch tests."""
    calls = []

    class Session:
        def __init__(self, path, sess_options, providers):
            if fail_session:
                raise RuntimeError("not a protobuf")
            calls.append(("open", path, providers))

        def get_modelmeta(self):
            return SimpleNamespace(custom_metadata_map={
                smd.ONNX_METADATA_KEY: json.dumps(metadata)} if metadata is not None else {})

        def get_inputs(self):
            return [SimpleNamespace(name="images", type="tensor(float)", shape=list(in_shape))]

        def get_outputs(self):
            return [SimpleNamespace(name=n, type="tensor(float)", shape=list(s))
                    for n, s in zip(("class_logits", "rotation_logits"), out_shapes)]

        def run(self, names, feeds):
            batch = feeds["images"]
            assert batch.dtype == np.float32 and batch.flags.c_contiguous
            turn = len([c for c in calls if c[0] == "run"]) % 4
            calls.append(("run", batch))
            classes = np.zeros((len(batch), 2), np.float32)
            classes[:, 0] = 10
            rotation = np.roll(np.array([0., 10., 0., 0.], np.float32), turn)
            return [classes, np.repeat(rotation[None], len(batch), axis=0)]

    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace(
        InferenceSession=Session, SessionOptions=SimpleNamespace))
    return calls


def test_an_onnx_network_gives_the_same_answer_as_the_checkpoint_route(network, monkeypatch, tmp_path):
    """@brief Positive: the .onnx route returns the same rows as the torch route, from the same preprocessing."""
    calls = _fake_onnx(monkeypatch)
    onnx_path = tmp_path / "smd_classifier.onnx"
    onnx_path.write_bytes(b"graph")
    image = Image.fromarray(np.arange(40 * 60 * 3, dtype=np.uint8).reshape(40, 60, 3))
    (tmp_path / "x.pt").write_bytes(b"mock checkpoint")   # torch.load is faked by `network`
    expected = smd.classify_photo_boxes(image, [[10, 10, 30, 30]], model_path=tmp_path / "x.pt")[0]
    result = smd.classify_photo_boxes(image, [[10, 10, 30, 30]], model_path=onnx_path)[0]
    assert calls[0] == ("open", str(onnx_path), ["CPUExecutionProvider"])
    assert result == expected and result["rotation_deg"] == 90 and result["accepted"]
    # Same batches as the torch route, bit for bit, for the four views.
    runs = [c[1] for c in calls if c[0] == "run"]
    assert len(runs) == 4 and all(np.array_equal(r, t.numpy()) for r, t in zip(runs, network.calls))


def test_describe_and_validate_read_the_onnx_metadata(monkeypatch, tmp_path):
    """@brief Positive: the model window's description comes from the JSON metadata, no PyTorch involved."""
    _fake_onnx(monkeypatch)
    monkeypatch.setattr(smd, "_runtime", lambda: pytest.fail("PyTorch must not be loaded for .onnx"))
    onnx_path = tmp_path / "smd_classifier.onnx"
    onnx_path.write_bytes(b"graph")
    assert smd.validate_smd_setup(onnx_path) == onnx_path
    info = smd.describe_smd_checkpoint(onnx_path)
    assert info["classes"] == ["SOT-23", "_other"] and info["epochs"] == 40
    assert info["validation"] == {"samples": 42, "accuracy": 1.0, "balanced_accuracy": 1.0}
    assert info["threshold"] == 0.5 and info["catalog_sha256"] == "ab" * 32


@pytest.mark.parametrize("kwargs,reason", [
    (dict(metadata=None), "non compatibile"),
    (dict(metadata={**_ONNX_META, "img_size": 256}), "non compatibile"),
    (dict(metadata={**_ONNX_META, "orientations": {"SOT-23": "full"}}), "orientations"),
    (dict(in_shape=(1, 3, 256, 256)), "ONNX input"),
    (dict(in_shape=("batch", 224, 224, 3)), "ONNX input"),
    (dict(out_shapes=(("batch", 3), ("batch", 4))), "ONNX outputs"),
    (dict(out_shapes=(("batch", 2),)), "ONNX outputs"),
    (dict(fail_session=True), "non compatibile"),
])
def test_an_onnx_file_that_does_not_match_the_network_is_refused(monkeypatch, tmp_path, kwargs, reason):
    """@brief Negative: missing/wrong metadata, other input size or layout, other head sizes, unreadable graph."""
    _fake_onnx(monkeypatch, **kwargs)
    onnx_path = tmp_path / "smd_classifier.onnx"
    onnx_path.write_bytes(b"graph")
    with pytest.raises(ValueError, match=reason):
        smd.validate_smd_setup(onnx_path)


def test_a_missing_onnx_runtime_is_actionable(monkeypatch, tmp_path):
    """@brief Negative: without onnxruntime the error names the requirements file."""
    import builtins
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == "onnxruntime":
            raise ImportError("missing")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded)
    onnx_path = tmp_path / "smd_classifier.onnx"
    onnx_path.write_bytes(b"graph")
    with pytest.raises(RuntimeError, match="requirements/yolo.txt"):
        smd.validate_smd_setup(onnx_path)


def test_the_default_weights_fall_back_to_the_onnx_export(monkeypatch, tmp_path):
    """@brief app_paths.smd_model_path(): the checkpoint when present, else the ONNX export, else the checkpoint's name."""
    from kicadgen import app_paths
    monkeypatch.setattr(app_paths, "resource_path", lambda *parts: tmp_path.joinpath(*parts))
    folder = tmp_path / "models" / "smd"
    folder.mkdir(parents=True)
    assert app_paths.smd_model_path() == folder / "smd_classifier.pt"
    (folder / "smd_classifier.onnx").write_bytes(b"x")
    assert app_paths.smd_model_path() == folder / "smd_classifier.onnx"
    (folder / "smd_classifier.pt").write_bytes(b"x")
    assert app_paths.smd_model_path() == folder / "smd_classifier.pt"
    assert app_paths.smd_model_path("other.onnx") == folder / "other.onnx"
