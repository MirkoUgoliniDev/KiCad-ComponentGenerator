"""@brief «Solo YOLO locale»: a local pretest that only shows YOLO's boxes, with no AI call.

User requests (2026-09-25): «se abbiamo solo YOLO si fermi li' e basta, fa
vedere solo i rettangoli individuati», then «se scelgo solo YOLO non deve
partire con AI ... serve a fare un pretest». Each behaviour has a negative
twin on the `yolo` path, which must still reconcile with one paid request.
"""
from pathlib import Path

import pytest
from unittest.mock import Mock

from kicadgen.cad3d.kicad_models import _is_yolo_only_hypothesis
from kicadgen.services import photo_inventory
from kicadgen.services.photo_inventory import YOLO_CLASS_KINDS, yolo_only_components

from test_global_inventory_pipeline import pipeline  # noqa: F401 - fixture
from kicadgen.gui import main_window as gui


def _row(identity, yolo_class, *, weak=False, ambiguous=False):
    return {"_id": identity, "name": f"YOLO candidate {identity}", "kind": "generic",
            "x_mm": 0.0, "y_mm": 0.0, "length_mm": 2.0, "width_mm": 1.0,
            "rotation_deg": 0, "height_mm": 1.0, "_box": (.1, .1, .2, .2),
            "_source": "YOLO local", "_yolo_class": yolo_class,
            "_yolo_class_ambiguous": ambiguous, "_yolo_review_only": weak,
            "_uncertain": True}


def test_kinds_come_from_the_declared_class_table():
    rows = [_row(1, "capacitor"), _row(2, "transistor"), _row(3, "clock"),
            _row(4, "buzzer"), _row(5, "ic", ambiguous=True)]
    kinds = [c["kind"] for c in yolo_only_components(rows, reviewed=True)]
    # Omonimi restano tali (buzzer ha un tipo dedicato dal 27/09), transistor ->
    # ic come la riconciliazione, clock -> crystal; classe ambigua: generic.
    assert kinds == ["capacitor", "ic", "crystal", "buzzer", "generic"]
    assert set(YOLO_CLASS_KINDS.values()) <= set(
        __import__("kicadgen.services.datasheet_extractor", fromlist=["x"]).COMPONENT_KINDS)


def test_weak_proposals_need_a_reviewer():
    rows = [_row(1, "resistor"), _row(2, "resistor", weak=True)]
    # Nessuno le ha guardate: fuori, come avrebbe potuto decidere solo l'AI.
    assert [c["_yolo_class"] for c in yolo_only_components(rows, reviewed=False)] == ["resistor"]
    # Lasciate dall'utente nella revisione: sono una sua decisione, restano.
    kept = yolo_only_components(rows, reviewed=True)
    assert len(kept) == 2 and all(c["_presence_reviewed_by_user"] for c in kept)


def test_rows_are_the_inventory_not_hypotheses_excluded_from_3d():
    rows = [_row(1, "led")]
    # Negativo: la riga grezza del rilevatore resta un'ipotesi esclusa dal 3D.
    assert _is_yolo_only_hypothesis(rows[0])
    [component] = yolo_only_components(rows, reviewed=True)
    assert not _is_yolo_only_hypothesis(component)
    assert component["name"] == "led 1" and component["_uncertain"] is True
    assert "no AI verification" in component["_source"]
    assert rows[0]["name"] == "YOLO candidate 1"  # input untouched


def test_yolo_only_inventory_makes_no_call(pipeline, tmp_path):  # noqa: F811
    """@brief The yolo_only branch of extract_inventory never calls a model."""
    def no_ai(**_kwargs):
        pytest.fail("yolo_only must not call any AI model")
    phases = []
    photo = tmp_path / "top.png"
    import cv2, numpy as np
    assert cv2.imwrite(str(photo), np.zeros((100, 200, 3), np.uint8))
    report = photo_inventory.extract_inventory(
        [photo], "", 40, 20, (), no_ai, top_photo_index=0, detector="yolo_only",
        yolo_model_path=tmp_path / "fixture.onnx", phase=lambda t, d: phases.append(t))
    pipeline["global_review"].assert_not_called()
    pipeline["detection"].assert_called_once()
    assert [c["_id"] for c in report.components] == [1, 2]
    assert report.overlay_png
    # Nessuna fase a pagamento; la fase del rilevatore viene chiusa.
    assert not {"llm", "llm_inventory"} & set(phases) and phases[-1] == ""


def test_the_ai_extraction_does_not_accept_the_pretest(pipeline):  # noqa: F811
    """@brief The pretest is not an extraction: the AI entry point refuses it."""
    from kicadgen.services.datasheet_extractor import ExtractionError
    with pytest.raises(ExtractionError):
        pipeline["execute"](photo_detector="yolo_only")
    assert pipeline["requests"] == []


def test_yolo_with_ai_verification_still_reconciles(pipeline):  # noqa: F811
    pipeline["execute"](photo_detector="yolo")
    assert len(pipeline["requests"]) == 2
    pipeline["global_review"].assert_called_once()


def test_unknown_detector_is_still_rejected():
    with pytest.raises(ValueError):
        photo_inventory.extract_inventory([], "m", 10, 10, (), None, detector="yolo-only")


def test_detector_row_comes_first_and_the_pretest_hides_both_ai_models(window):  # noqa: F811
    combo = window.combo_photo_detector
    form = window._models_form
    # Le scelte esistenti restano nello stesso ordine e la modalità SMD si
    # aggiunge in coda, così i progetti salvati mantengono i propri valori.
    assert [combo.itemData(i) for i in range(combo.count())] == [
        "ai", "yolo", "vlm", "yolo_only", "yolo_smd"]
    # «Rilevamento foto» e' la prima riga di «Strumenti AI», prima di «Modello AI».
    assert form.itemAt(0, form.ItemRole.FieldRole).widget() is combo
    assert window._row_ai_model == 1
    combo.setCurrentIndex(combo.findData("yolo_only"))
    window.field_yolo_model.setText("new.pt")
    assert window._photo_detector_options() == ("yolo_only", Path("new.pt"))
    assert window.field_yolo_model.isEnabled()
    assert form.isRowVisible(window._row_yolo_model)
    assert not form.isRowVisible(window._row_ai_model)
    assert not form.isRowVisible(window._row_vlm_model)
    assert not form.isRowVisible(window._row_ai_account)  # niente credito: non si spende
    # La verifica AI riattiva il modello AI; la riga VLM compare solo quando
    # è configurato un override distinto dal modello AI.
    combo.setCurrentIndex(combo.findData("yolo"))
    assert form.isRowVisible(window._row_ai_model)
    assert not form.isRowVisible(window._row_vlm_model)
    previous_vlm = window._vlm_model
    window._vlm_model = "different/vision-model"
    window._on_photo_detector_changed()
    assert form.isRowVisible(window._row_vlm_model)
    window._vlm_model = previous_vlm
    window._on_photo_detector_changed()
    assert form.isRowVisible(window._row_ai_account)


def _fake_validation(monkeypatch):
    import sys
    from types import ModuleType
    validated = []
    module = ModuleType("kicadgen.services.photo_yolo")
    module.validate_yolo_setup = validated.append
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return validated


def test_the_button_starts_the_local_pretest_without_ai(window, monkeypatch):  # noqa: F811
    validated = _fake_validation(monkeypatch)
    monkeypatch.setattr(window, "_selected_documents", lambda: ([Path("board.png")], {"board.png": "top"}))
    monkeypatch.setattr(gui.QThread, "start", lambda self: None)
    monkeypatch.setattr(window.profile_store, "snapshot",
                        lambda *a: pytest.fail("the pretest must not read the AI profile"))
    lines = []
    monkeypatch.setattr(window, "_on_ai_progress", lines.append)
    # Matrice: quote dai campi del corpo della matrice.
    monkeypatch.setattr(window, "_is_castellated", lambda: False)
    window.field_body_length.setValue(25.5)
    window.field_body_width.setValue(18.0)
    window.field_cast_body_length.setValue(51.0)
    window.field_cast_body_width.setValue(28.0)
    window.combo_photo_detector.setCurrentIndex(window.combo_photo_detector.findData("yolo_only"))
    window.field_yolo_model.setText("")
    window._extract_with_ai()
    worker = window._extract_worker
    assert validated == [None]
    assert isinstance(worker, gui.YoloPretestWorker)
    assert (worker.length_mm, worker.width_mm) == (25.5, 18.0)
    assert worker._top_views() == [0]
    assert any("nessuna chiamata AI" in line and "25.50 x 18.00" in line for line in lines), lines
    window._hide_analysis_overlay()
    window.btn_extract_ai.setEnabled(True)


def test_a_castellated_pretest_uses_the_castellated_body_fields(window, monkeypatch):  # noqa: F811
    """@brief Utente, 25/09: la card diceva 61 px/mm, il pretest «non calibrabile».

    Le quote vengono dalla stessa fonte delle card (`_body_size`): con una
    tipologia castellated sono i campi castellated, non quelli della matrice
    (qui lasciati a valori che non c'entrano). Il caso matrice e' nel test sopra.
    """
    _fake_validation(monkeypatch)
    monkeypatch.setattr(window, "_selected_documents", lambda: ([Path("hires.jpg")], {"hires.jpg": "hires"}))
    monkeypatch.setattr(gui.QThread, "start", lambda self: None)
    monkeypatch.setattr(window, "_is_castellated", lambda: True)
    window.field_body_length.setValue(10.0)
    window.field_body_width.setValue(10.0)
    window.field_cast_body_length.setValue(51.0)
    window.field_cast_body_width.setValue(28.0)
    window.combo_photo_detector.setCurrentIndex(window.combo_photo_detector.findData("yolo_only"))
    window._extract_with_ai()
    worker = window._extract_worker
    assert (worker.length_mm, worker.width_mm) == (51.0, 28.0)
    assert (worker.length_mm, worker.width_mm) == window._body_size()
    window._hide_analysis_overlay()
    window.btn_extract_ai.setEnabled(True)


def test_without_body_dimensions_the_pretest_does_not_start(window, monkeypatch):  # noqa: F811
    _fake_validation(monkeypatch)
    monkeypatch.setattr(window, "_selected_documents", lambda: ([Path("hires.jpg")], None))
    monkeypatch.setattr(gui.QThread, "start", lambda self: pytest.fail("must not start"))
    monkeypatch.setattr(window, "_body_size", lambda: None)
    # Dal 26/09 le quote ignote si chiedono (prima un messaggio rimandava a
    # campi che non esistono piu'); chi annulla non avvia il pretest.
    asked = []
    monkeypatch.setattr(window, "_ask_board_size", lambda: asked.append(True) or None)
    window.combo_photo_detector.setCurrentIndex(window.combo_photo_detector.findData("yolo_only"))
    window._extract_with_ai()
    assert asked == [True]
    # Back in use after the short pause that lets queued clicks find it
    # disabled (2026-10-02, «Preparazione…»).
    from PyQt6.QtTest import QTest
    QTest.qWait(gui.EXTRACT_REENABLE_DELAY_MS + 100)
    assert window.btn_extract_ai.isEnabled()


def test_the_yolo_choice_still_starts_the_ai_extraction(window, monkeypatch):  # noqa: F811
    _fake_validation(monkeypatch)
    monkeypatch.setattr(window, "_selected_documents", lambda: ([Path("board.png")], None))
    monkeypatch.setattr(gui.ExtractWorker, "start", lambda self: None)
    window.combo_photo_detector.setCurrentIndex(window.combo_photo_detector.findData("yolo"))
    window._extract_with_ai()
    worker = window._extract_worker
    assert type(worker) is gui.ExtractWorker and worker.photo_detector == "yolo"
    window._on_extract_finished()


def test_the_pretest_worker_runs_detection_and_review_offline(pipeline, monkeypatch, tmp_path):  # noqa: F811
    """@brief The worker's run: detector, review hand-off, report; no network (fixture)."""
    import cv2, numpy as np
    photo = tmp_path / "top.png"
    assert cv2.imwrite(str(photo), np.zeros((100, 200, 3), np.uint8))
    worker = gui.YoloPretestWorker([photo], {"top.png": "top"}, 40, 20,
                                   tmp_path / "fixture.onnx", tmp_path / "cache")
    reviewed, results, errors = [], [], []
    worker.review_requested.connect(
        lambda png, proposals, *_: (reviewed.append(len(proposals)), worker.deliver_review(proposals[:1])))
    worker.done.connect(results.append)
    worker.error.connect(errors.append)
    worker.run()
    assert errors == [] and reviewed == [2]
    [report] = results
    assert len(report.components) == 1
    pipeline["global_review"].assert_not_called()
    assert pipeline["requests"] == []


def test_interrupting_the_pretest_does_not_speak_of_a_charge(window, monkeypatch, tmp_path):  # noqa: F811
    """@brief «Interrompi» sul pretest: abbandono senza costi; sull'estrazione resta 'ai'."""
    monkeypatch.setattr(gui.QThread, "isRunning", lambda self: True)
    window._extract_worker = gui.YoloPretestWorker([], None, 10, 10, None, tmp_path)
    kind, worker, _label = window._abortable_operation()
    assert kind == "local" and worker is window._extract_worker
    asked = []
    monkeypatch.setattr(gui.QMessageBox, "question",
                        lambda _parent, _title, text, *a: asked.append(text) or gui.QMessageBox.StandardButton.No)
    window._confirm_abort(kind, "Pretest")
    assert "addebitata" not in asked[0] and "nessun costo" in asked[0]
    # Negativo: l'estrazione vera resta una corsa a pagamento.
    window._extract_worker = gui.ExtractWorker([])
    assert window._abortable_operation()[0] == "ai"
    window._extract_worker = None


def test_the_pretest_modal_does_not_claim_an_ai_analysis(window, monkeypatch):  # noqa: F811
    _fake_validation(monkeypatch)
    monkeypatch.setattr(window, "_selected_documents", lambda: ([Path("board.png")], None))
    monkeypatch.setattr(gui.QThread, "start", lambda self: None)
    window.combo_photo_detector.setCurrentIndex(window.combo_photo_detector.findData("yolo_only"))
    window._extract_with_ai()
    assert "YOLO" in window.analysis_overlay.title.text()
    assert "AI" not in window.analysis_overlay.title.text()
    window._hide_analysis_overlay()
    window.btn_extract_ai.setEnabled(True)


def test_the_chosen_threshold_reaches_the_pretest(window, monkeypatch):  # noqa: F811
    """@brief La soglia si sceglie nella modale «Parametri YOLO» e arriva al pretest.
    Contratto cambiato (utente, 25/09, «solo nella finestra»): niente piu' campo
    «Soglia YOLO»; la riga «Parametri YOLO» mostra il riepilogo."""
    _fake_validation(monkeypatch)
    monkeypatch.setattr(window, "_selected_documents", lambda: ([Path("hires.jpg")], {"hires.jpg": "hires"}))
    monkeypatch.setattr(gui.QThread, "start", lambda self: None)
    form = window._models_form
    window.combo_photo_detector.setCurrentIndex(window.combo_photo_detector.findData("ai"))
    # Negativo: senza rilevatore locale i parametri non c'entrano e non si vedono.
    assert not form.isRowVisible(window._row_yolo_params)
    window.combo_photo_detector.setCurrentIndex(window.combo_photo_detector.findData("yolo_only"))
    assert form.isRowVisible(window._row_yolo_params)
    assert "0.25" in window.label_yolo_summary.text()  # predefinito invariato
    window._yolo_settings = dict(window._chosen_yolo_settings(), confidence=0.46)
    window._extract_with_ai()
    assert window._extract_worker.yolo_settings["confidence"] == pytest.approx(0.46)
    assert "field_yolo_confidence" not in gui.PROJECT_WIDGETS
    window._hide_analysis_overlay()
    window.btn_extract_ai.setEnabled(True)


def test_the_button_says_what_it_does_for_each_detector(window):  # noqa: F811
    """@brief Utente, 25/09: con «Solo YOLO» pulsante e suggerimento erano quelli dell'analisi
    a pagamento. Positivo: pretest gratuito. Negativo: con la verifica AI resta a pagamento."""
    combo, button = window.combo_photo_detector, window.btn_extract_ai
    combo.setCurrentIndex(combo.findData("yolo_only"))
    assert button.text() == "Rileva componenti con YOLO"
    assert "GRATUITO" in button.toolTip() and "PAGAMENTO" not in button.toolTip()
    combo.setCurrentIndex(combo.findData("yolo"))
    assert button.text() == "Estrai parametri con AI"
    assert "PAGAMENTO" in button.toolTip()


def test_the_pretest_button_follows_the_language(window):  # noqa: F811
    combo = window.combo_photo_detector
    combo.setCurrentIndex(combo.findData("yolo_only"))
    window.language_combo.setCurrentIndex(window.language_combo.findData("en"))
    assert window.btn_extract_ai.text() == "Detect components with YOLO"
    assert "FREE" in window.btn_extract_ai.toolTip()
    window.language_combo.setCurrentIndex(window.language_combo.findData("it"))
    assert window.btn_extract_ai.text() == "Rileva componenti con YOLO"


def test_yolo_only_leaves_the_detector_boxes_untouched(pipeline, monkeypatch, tmp_path):  # noqa: F811
    """@brief Positivo (utente 2026-10-08: «solo YOLO deve farmi solo YOLO»).

    Con «Solo YOLO» ne' il filtro sui contatti dichiarati ne' la misura dei
    bordi con OpenCV toccano i riquadri: escono come li ha proposti il
    rilevatore. Entrambi i passi sono sostituiti da sentinelle che fallirebbero.
    """
    def must_not_run(*_args, **_kwargs):
        pytest.fail("yolo_only must run nothing but the detector on the boxes")
    monkeypatch.setattr(photo_inventory, "measure_proposal_edges", must_not_run)
    from kicadgen.services import contact_zones
    monkeypatch.setattr(contact_zones, "contact_proposals", must_not_run)
    photo = tmp_path / "top.png"
    import cv2, numpy as np
    assert cv2.imwrite(str(photo), np.zeros((100, 200, 3), np.uint8))
    report = photo_inventory.extract_inventory(
        [photo], "", 40, 20, (), lambda **_k: pytest.fail("no AI"), top_photo_index=0,
        detector="yolo_only", yolo_model_path=tmp_path / "fixture.onnx")
    proposed = pipeline["detection"].return_value
    assert [tuple(c["_box"]) for c in report.components] == [tuple(p["_box"]) for p in proposed]


def test_yolo_with_ai_still_measures_the_edges(pipeline, monkeypatch, tmp_path):  # noqa: F811
    """@brief Negativo: con il rilevatore YOLO + AI la misura dei bordi gira come prima."""
    measured = Mock(side_effect=lambda photo, proposals, *a, **k: proposals)
    monkeypatch.setattr(photo_inventory, "measure_proposal_edges", measured)
    pipeline["execute"](photo_detector="yolo")
    measured.assert_called_once()
