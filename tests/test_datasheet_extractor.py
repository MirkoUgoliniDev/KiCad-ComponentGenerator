"""Test per la logica di validazione di kicadgen.services.datasheet_extractor.

Non chiamano l'API OpenRouter reale: verificano solo i controlli locali
(chiave mancante, file non supportati, troppo grandi, troppi file) e la
forma dei blocchi di contenuto inviati al modello.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from kicadgen.services.datasheet_extractor import (
    MAX_FILES, ExtractionError, extract_pin_header_params, _file_to_content_block,
    _openai_tool, _params_from_tool_arguments, _merge_photo_components,
)


def test_extract_requires_at_least_one_file():
    with pytest.raises(ExtractionError, match="Nessun file"):
        extract_pin_header_params([])


def test_overlapping_explicit_pad_ranges_are_rejected():
    from kicadgen.services.datasheet_extractor import _castellated_sides
    with pytest.raises(ExtractionError, match="pad duplicato"):
        _castellated_sides([dict(edge="top", count=22, pitch_mm=2.54, first_pin=1),
                           dict(edge="bottom", count=22, pitch_mm=2.54, first_pin=1)])


def test_missing_pad_range_reserves_later_explicit_numbers():
    from kicadgen.services.datasheet_extractor import _castellated_sides
    sides = _castellated_sides([dict(edge="top", count=3, pitch_mm=2.54),
        dict(edge="bottom", count=3, pitch_mm=2.54, first_pin=1, reverse=True)])
    assert [s["first_pin"] for s in sides] == [4, 1]
    assert sides[1]["reverse"] is True


@pytest.mark.parametrize("corrected_start", [3, 1])
def test_duplicate_pin_repair_preserves_geometry_and_labels(tmp_path, monkeypatch, corrected_start):
    from kicadgen.services import datasheet_extractor as ext
    from unittest.mock import Mock
    source = tmp_path / "drawing.pdf"
    source.write_bytes(b"%PDF-1.4 offline")
    sides = [dict(edge="top", count=2, pitch_mm=2.54, first_pin=1, pin_names=["G", "1"]),
             dict(edge="bottom", count=2, pitch_mm=2.54, first_pin=1, pin_names=["G", "TX"])]
    call = Mock(side_effect=[dict(layout="castellated", castellated_sides=sides),
        dict(castellated_sides=[sides[0], dict(sides[1], first_pin=corrected_start, pitch_mm=99)]),
        {"sides": []}])
    monkeypatch.setattr(ext, "api_key", lambda: "offline")
    monkeypatch.setattr(ext, "check_credit", lambda **kw: None)
    monkeypatch.setattr(ext, "call_tool", call)
    if corrected_start == 1:
        with pytest.raises(ExtractionError, match="pad duplicato"):
            ext.extract_pin_header_params([source])
        assert call.call_count == 2
        return
    result = ext.extract_pin_header_params([source])
    assert call.call_count == 3  # Main, numbering repair, missing-style check.
    assert result.castellated_sides[1]["first_pin"] == 3
    assert result.castellated_sides[1]["pitch_mm"] == 2.54
    assert result.castellated_sides[1]["pin_names"] == ["G", "TX"]
    assert "Numerazione contatti corretta" in result.notes


def test_extract_rejects_too_many_files(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key-for-test")
    files = []
    for i in range(MAX_FILES + 1):
        f = tmp_path / f"doc{i}.pdf"
        f.write_bytes(b"%PDF-1.4")
        files.append(f)
    with pytest.raises(ExtractionError, match="Troppi file"):
        extract_pin_header_params(files)


def test_extract_requires_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4")
    with pytest.raises(ExtractionError, match="OPENROUTER_API_KEY"):
        extract_pin_header_params([f])


def test_content_block_rejects_unsupported_extension(tmp_path):
    f = tmp_path / "doc.docx"
    f.write_bytes(b"fake")
    with pytest.raises(ExtractionError, match="non supportato"):
        _file_to_content_block(f)


def test_content_block_rejects_oversized_file(tmp_path, monkeypatch):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(
        "kicadgen.services.datasheet_extractor.MAX_FILE_BYTES", 1
    )
    with pytest.raises(ExtractionError, match="troppo grande"):
        _file_to_content_block(f)


def test_content_block_builds_pdf_file_block(tmp_path):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 fake content")
    block = _file_to_content_block(f)
    assert block["type"] == "file"
    assert block["file"]["filename"] == "doc.pdf"
    assert block["file"]["file_data"].startswith("data:application/pdf;base64,")


def test_content_block_builds_image_block(tmp_path):
    f = tmp_path / "drawing.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    block = _file_to_content_block(f)
    assert block["type"] == "image_url"
    assert block["image_url"]["url"].startswith("data:image/png;base64,")


def test_openai_tool_wraps_schema_for_openrouter():
    """Lo schema deve arrivare nel formato "function" dell'API OpenAI."""
    tool = _openai_tool()
    assert tool["type"] == "function"
    function = tool["function"]
    assert function["name"] == "report_pin_header_params"
    properties = function["parameters"]["properties"]
    for key in ("rows", "cols", "pitch_mm", "row_spacing_mm", "pad_diameter_mm", "drill_mm"):
        assert key in properties


# ── Disposizione dei contatti (layout) ──────────────────────────────

def _matrix_payload(**extra):
    data = {
        "layout": "matrix", "rows": 2, "cols": 4, "pitch_mm": 2.54,
        "row_spacing_mm": 2.54, "pad_diameter_mm": 1.7, "drill_mm": 1.0,
        "confidence": "high",
    }
    data.update(extra)
    return data


def test_matrix_layout_requires_the_grid_fields():
    data = _matrix_payload()
    del data["row_spacing_mm"]
    with pytest.raises(ExtractionError, match="row_spacing_mm"):
        _params_from_tool_arguments(data)


def test_matrix_layout_is_the_default_when_missing():
    data = _matrix_payload()
    del data["layout"]
    params = _params_from_tool_arguments(data)
    assert params.layout == "matrix"
    assert not params.is_castellated()


def test_castellated_layout_keeps_one_pitch_per_side():
    """Il caso che la matrice non sa rappresentare: due lati, due passi."""
    params = _params_from_tool_arguments({
        "layout": "castellated",
        "name": "RS485_TTL",
        "confidence": "medium",
        "body_length_mm": 42.8, "body_width_mm": 15.2, "body_height_mm": 4.75,
        "castellated_sides": [
            {"edge": "left", "count": 3, "pitch_mm": 5.08,
             "pin_names": ["PE", "B-", "A+"]},
            {"edge": "right", "count": 5, "pitch_mm": 2.54},
        ],
    })
    assert params.is_castellated()
    assert params.rows is None and params.pitch_mm is None
    left, right = params.castellated_sides
    assert (left["edge"], left["count"], left["pitch_mm"]) == ("left", 3, 5.08)
    assert (right["edge"], right["count"], right["pitch_mm"]) == ("right", 5, 2.54)
    assert left["pin_names"] == ["PE", "B-", "A+"]


def test_castellated_sides_get_progressive_numbering_when_missing():
    params = _params_from_tool_arguments({
        "layout": "castellated", "confidence": "low",
        "castellated_sides": [
            {"edge": "left", "count": 3, "pitch_mm": 5.08},
            {"edge": "right", "count": 5, "pitch_mm": 2.54},
        ],
    })
    assert params.castellated_sides[0]["first_pin"] == 1
    assert params.castellated_sides[1]["first_pin"] == 4


def test_castellated_without_sides_is_reported():
    with pytest.raises(ExtractionError, match="non ha descritto alcun lato"):
        _params_from_tool_arguments({"layout": "castellated", "confidence": "low"})


def test_unusable_sides_are_discarded():
    params = _params_from_tool_arguments({
        "layout": "castellated", "confidence": "low",
        "castellated_sides": [
            {"edge": "left", "count": 3, "pitch_mm": 5.08},
            {"edge": "diagonale", "count": 2, "pitch_mm": 2.54},
            {"edge": "right", "count": 4, "pitch_mm": 0},
        ],
    })
    assert [s["edge"] for s in params.castellated_sides] == ["left"]


@pytest.mark.parametrize("bad", [None, "side", 42, [], {},
    {"edge": "right", "count": "bad", "pitch_mm": 2},
    {"edge": "right", "count": True, "pitch_mm": 2},
    {"edge": "right", "count": 1.5, "pitch_mm": 2},
    {"edge": "right", "count": float("inf"), "pitch_mm": 2},
    *[{"edge": "right", "count": 2, "pitch_mm": value}
      for value in (True, "bad", float("nan"), float("inf"), -1)],
])
def test_malformed_castellated_side_does_not_kill_valid_sibling(bad):
    from kicadgen.services.datasheet_extractor import _castellated_sides
    good = dict(edge="left", count=2, pitch_mm=2.54)
    # contact_style None: not observed, never a default (user decision 26/09).
    assert _castellated_sides([bad, good]) == [dict(good, first_pin=1, contact_style=None)]
    with pytest.raises(ExtractionError):
        _castellated_sides([bad])


@pytest.mark.parametrize("bad", [True, "bad", float("nan"), float("inf"), [], {}])
def test_invalid_optional_side_numerics_are_omitted(bad):
    from kicadgen.services.datasheet_extractor import _castellated_sides
    result = _castellated_sides([dict(edge="left", count=1, pitch_mm=0,
        first_pin=bad, offset_mm=bad, pad_length_mm=bad, pad_width_mm=bad),
        dict(edge="right", count=1, offset_mm=-2)])
    assert result[0] == dict(edge="left", count=1, pitch_mm=0, first_pin=1, contact_style=None)
    assert result[1]["first_pin"] == 2 and result[1]["offset_mm"] == -2


def test_nonpositive_optional_pad_dimensions_and_fractional_first_pin_are_omitted():
    from kicadgen.services.datasheet_extractor import _castellated_sides
    assert _castellated_sides([dict(edge="left", count=1, first_pin=1.5,
        pad_length_mm=0, pad_width_mm=-1)]) == [dict(edge="left", count=1, pitch_mm=0, first_pin=1, contact_style=None)]


@pytest.mark.parametrize("bad", [123, "false", {"pin": "A"}, True])
def test_malformed_optional_side_names_and_reverse_are_ignored(bad):
    from kicadgen.services.datasheet_extractor import _castellated_sides
    side = dict(edge="left", count=1, pin_names=bad, reverse="false")
    assert _castellated_sides([side]) == [dict(edge="left", count=1, pitch_mm=0, first_pin=1, contact_style=None)]


@pytest.mark.parametrize("names", [["A", 2], ("A", 2)])
def test_valid_optional_side_names_and_boolean_reverse_are_preserved(names):
    from kicadgen.services.datasheet_extractor import _castellated_sides
    side = _castellated_sides([dict(edge="left", count=2, pitch_mm=2, pin_names=names, reverse=True)])[0]
    assert side["pin_names"] == ["A", "2"] and side["reverse"] is True


@pytest.mark.parametrize("key", ["body_length_mm", "body_width_mm", "body_height_mm"])
def test_discarded_body_dimension_is_reported_in_notes_and_progress(monkeypatch, tmp_path, key):
    from kicadgen.services import datasheet_extractor as ext
    path = tmp_path / "drawing.png"
    path.write_bytes(b"offline fixture")
    monkeypatch.setattr(ext, "api_key", lambda: "offline")
    monkeypatch.setattr(ext, "call_tool", lambda **kw: _matrix_payload(**{key: True}, notes="Original note"))
    messages = []
    result = ext.extract_pin_header_params([path], progress=messages.append)
    assert getattr(result, key) is None
    assert "Original note" in result.notes and f"{key} scartato" in result.notes
    assert any(f"{key} scartato" in message for message in messages)


@pytest.mark.parametrize("key", ["body_length_mm", "body_width_mm", "body_height_mm"])
@pytest.mark.parametrize("bad", [True, 0, -1, "bad", float("nan"), float("inf"), {}])
def test_invalid_optional_body_dimensions_are_discarded(key, bad):
    params = _params_from_tool_arguments(_matrix_payload(**{key: bad},
        body_offset_x_mm=-3, body_offset_y_mm=-4))
    assert getattr(params, key) is None
    assert (params.body_offset_x_mm, params.body_offset_y_mm) == (-3, -4)
    assert (params.rows, params.cols, params.pitch_mm) == (2, 4, 2.54)


@pytest.mark.parametrize("photo_state", ["absent", "failed", "bad-length", "bad-width", "missing-length"])
def test_castellated_fallback_is_uncertain_generic_without_markings(monkeypatch, tmp_path, photo_state):
    from unittest.mock import Mock
    from kicadgen.services import datasheet_extractor as ext, photo_inventory as inv
    path = tmp_path / "drawing.png"
    path.write_bytes(b"offline fixture")
    payload = dict(layout="castellated", body_length_mm=40, body_width_mm=20,
        castellated_sides=[dict(edge="left", count=2, pitch_mm=2.54)],
        components_3d=[dict(name="R1", kind="resistor", marking="102", confidence="high",
            _uncertain=False, _height_source="measured", x_mm=-2, y_mm=1,
            length_mm=2, width_mm=1, height_mm=.5)])
    if photo_state == "bad-length":
        payload["body_length_mm"] = "bad"
    elif photo_state == "bad-width":
        payload["body_width_mm"] = True
    elif photo_state == "missing-length":
        del payload["body_length_mm"]
    monkeypatch.setattr(ext, "api_key", lambda: "offline")
    call = Mock(return_value=payload)
    monkeypatch.setattr(ext, "call_tool", call)
    report = Mock(components=[], warnings=[])
    photo = Mock(return_value=report)
    if photo_state == "failed":
        photo.side_effect = ext.OpenRouterError("offline failure")
    monkeypatch.setattr(inv, "extract_inventory", photo)
    result = ext.extract_pin_header_params([path], photo_cache_dir=tmp_path / "cache")
    assert call.call_count == 2  # Main extraction plus missing-style check.
    assert photo.call_count == (1 if photo_state in ("absent", "failed") else 0)
    c = result.components_3d[0]
    assert (c["name"], c["kind"], c["marking"]) == ("Ingombro 1", "generic", "")
    assert c["_uncertain"] and c["confidence"] == "low"
    assert c["_height_source"] == "global_visual_estimate_not_measured"
    assert (c["x_mm"], c["height_mm"]) == (-2, .5)
    assert "non misure certificate" in result.notes
    assert result.photo_overlay_png is None


@pytest.mark.parametrize("raw", [None, [], "invalid", {"layout": "castellated", "castellated_sides": [None]}])
def test_invalid_main_response_raises_application_error_before_photo(monkeypatch, tmp_path, raw):
    from unittest.mock import Mock
    from kicadgen.services import datasheet_extractor as ext, photo_inventory as inv
    path = tmp_path / "drawing.png"
    path.write_bytes(b"offline fixture")
    monkeypatch.setattr(ext, "api_key", lambda: "offline")
    monkeypatch.setattr(ext, "call_tool", lambda **kw: raw)
    photo = Mock(side_effect=AssertionError("Photo must not run"))
    monkeypatch.setattr(inv, "extract_inventory", photo)
    with pytest.raises(ExtractionError):
        ext.extract_pin_header_params([path])
    photo.assert_not_called()


@pytest.mark.parametrize('verification_model', [None, 'vendor/verify'])
def test_three_stage_models_and_custom_prompts_reach_services(monkeypatch, tmp_path, verification_model):
    from unittest.mock import Mock
    from kicadgen.services import datasheet_extractor as ext, photo_inventory as inv, photo_terminals
    path = tmp_path / 'drawing.png'
    path.write_bytes(b'offline fixture')
    payload = dict(layout='castellated', body_length_mm=40, body_width_mm=20,
                   castellated_sides=[dict(edge='left', count=2, pitch_mm=2.54)])
    component = dict(name='body', kind='generic', x_mm=0, y_mm=0, length_mm=2, width_mm=1, height_mm=.5)
    report = inv.InventoryResult(components=[component], photo_png=b'photo', photo='fixture')
    main_call, photo, terminals = Mock(return_value=payload), Mock(return_value=report), Mock(return_value=[component])
    monkeypatch.setattr(ext, 'api_key', lambda: 'offline')
    monkeypatch.setattr(ext, 'call_tool', main_call)
    monkeypatch.setattr(inv, 'extract_inventory', photo)
    monkeypatch.setattr(photo_terminals, 'observe_terminals', terminals)
    ext.extract_pin_header_params([path], model='vendor/data', vision_model='vendor/photo',
        verification_model=verification_model, system_prompt='datasheet prompt', grounding_prompt='ground prompt',
        verification_prompt='verify prompt', terminal_prompt='terminal prompt', photo_cache_dir=tmp_path / 'cache')
    assert main_call.call_args.kwargs['model'] == 'vendor/data'
    assert main_call.call_args_list[0].kwargs['system_prompt'] == 'datasheet prompt'
    assert photo.call_args.args[1] == 'vendor/photo'
    assert photo.call_args.kwargs['classifier_model'] == (verification_model or 'vendor/data')
    assert photo.call_args.kwargs['grounding_prompt'] == 'ground prompt'
    assert photo.call_args.kwargs['verification_prompt'] == 'verify prompt'
    assert terminals.call_args.args[2] == 'vendor/photo'
    assert terminals.call_args.kwargs['system_prompt'] == 'terminal prompt'


# ── Componenti letti dalla foto (modello 3D) ────────────────────────


@pytest.mark.parametrize('with_top', [True, False])
def test_explicit_document_roles_override_ai_top_selection(monkeypatch, tmp_path, with_top):
    from unittest.mock import Mock
    from kicadgen.services import datasheet_extractor as ext, photo_inventory as inv
    paths = [tmp_path / 'contacts.png']
    roles = {'contacts.png': 'pinout'}
    if with_top:
        paths.append(tmp_path / 'random-photo.png')
        roles['random-photo.png'] = 'top'
    for path in paths:
        path.write_bytes(b'offline fixture')
    payload = dict(layout='castellated', body_length_mm=40, body_width_mm=20,
                   top_photo_index=0, castellated_sides=[dict(edge='left', count=2, pitch_mm=2.54)])
    call = Mock(return_value=payload)
    photo = Mock(return_value=inv.InventoryResult())
    monkeypatch.setattr(ext, 'api_key', lambda: 'offline')
    monkeypatch.setattr(ext, 'call_tool', call)
    monkeypatch.setattr(inv, 'extract_inventory', photo)
    ext.extract_pin_header_params(paths, document_roles=roles, photo_cache_dir=tmp_path / 'cache')
    # Ora si passa l'elenco delle viste dall'alto assegnate: l'inventario
    # sceglie la piu' dettagliata fra quelle che riesce a calibrare.
    assert photo.call_args.kwargs['top_photo_index'] == ([1] if with_top else [-1])
    assert photo.call_args.kwargs['top_photo_source'] == 'utente'
    blocks = call.call_args_list[0].kwargs['content_blocks']
    assert 'Pinout / disposizione contatti' in blocks[0]['text']
    if with_top:
        assert 'Immagine con quote' in blocks[2]['text']
    else:
        assert 'Viste non fornite: Immagine con quote' in blocks[-1]['text']


def test_ambiguous_roles_fail_before_network(monkeypatch, tmp_path):
    from unittest.mock import Mock
    from kicadgen.services import datasheet_extractor as ext
    network = Mock()
    monkeypatch.setattr(ext, 'api_key', network)
    with pytest.raises(ext.ExtractionError, match='Ruoli documenti'):
        ext.extract_pin_header_params([tmp_path / 'a.png', tmp_path / 'b.png'],
                                      document_roles={'a.png': 'top', 'b.png': 'top'})
    network.assert_not_called()

def test_photo_components_are_kept_with_their_box_and_color():
    params = _params_from_tool_arguments({
        "layout": "castellated", "confidence": "high",
        "castellated_sides": [{"edge": "left", "count": 2, "pitch_mm": 2.54}],
        "components_3d": [
            {"name": "trasformatore", "x_mm": 2.0, "y_mm": 0.0, "length_mm": 9.0,
             "width_mm": 6.0, "height_mm": 4.5, "color_hex": "#0a0a0a"},
        ],
    })
    component = params.components_3d[0]
    assert component["name"] == "trasformatore"
    assert (component["length_mm"], component["width_mm"], component["height_mm"]) == (9.0, 6.0, 4.5)
    assert component["color_hex"] == "#0a0a0a"


def test_components_without_a_real_volume_are_discarded():
    """Un blocco con una dimensione nulla non e' un componente: e' rumore."""
    params = _params_from_tool_arguments({
        "layout": "castellated", "confidence": "low",
        "castellated_sides": [{"edge": "left", "count": 2, "pitch_mm": 2.54}],
        "components_3d": [
            {"x_mm": 0, "y_mm": 0, "length_mm": 5, "width_mm": 0, "height_mm": 1},
            {"x_mm": 1, "y_mm": 1, "length_mm": 2, "width_mm": 2, "height_mm": 1},
        ],
    })
    assert len(params.components_3d) == 1
    assert params.components_3d[0]["x_mm"] == 1


def test_components_that_are_not_objects_are_skipped_not_fatal():
    """Su liste lunghe (30-50 elementi richiesti) il modello a volte infila
    una voce fuori schema (una stringa invece di un oggetto): non deve far
    fallire l'intera estrazione, solo quella voce."""
    params = _params_from_tool_arguments({
        "layout": "castellated", "confidence": "low",
        "castellated_sides": [{"edge": "left", "count": 2, "pitch_mm": 2.54}],
        "components_3d": [
            "SP3485E",
            {"x_mm": 1, "y_mm": 1, "length_mm": 2, "width_mm": 2, "height_mm": 1},
            None,
            42,
        ],
    })
    assert len(params.components_3d) == 1
    assert params.components_3d[0]["x_mm"] == 1


def test_mechanical_detected_kind_is_preserved_for_3d_renderer():
    # Mechanical envelopes are built by the program (trusted): their internal
    # `_detected_kind` must reach the 3D renderer.
    from kicadgen.services.datasheet_extractor import _components_3d
    result = _components_3d([{
        "name": "Ingombro 1", "kind": "generic", "_detected_kind": "ic_soic",
        "x_mm": 0, "y_mm": 0, "length_mm": 5, "width_mm": 3,
        "height_mm": 1.5,
    }], trusted=True)
    assert result[0]["kind"] == "generic"
    assert result[0]["_detected_kind"] == "ic_soic"


def test_photo_inventory_discards_pads_and_vias():
    params = _params_from_tool_arguments({
        "layout": "castellated", "confidence": "low",
        "castellated_sides": [{"edge": "left", "count": 2, "pitch_mm": 2.54}],
        "components_3d": [
            {"name": "via rotonda", "x_mm": 0, "y_mm": 0,
             "length_mm": 1, "width_mm": 1, "height_mm": 0.1},
            {"name": "R1", "kind": "resistor", "x_mm": 1, "y_mm": 1,
             "length_mm": 1.6, "width_mm": 0.8, "height_mm": 0.5},
        ],
    })
    assert len(params.components_3d) == 1
    assert params.components_3d[0]["name"] == "R1"


def test_tiled_photo_components_are_deduplicated_and_enriched():
    groups = [
        [{"name": "R1", "kind": "resistor", "x_mm": 1, "y_mm": 2,
          "length_mm": 1.6, "width_mm": 0.8, "height_mm": 0.5}],
        [{"name": "resistore", "kind": "generic", "x_mm": 1.08, "y_mm": 2.04,
          "length_mm": 1.5, "width_mm": 0.8, "height_mm": 0.5,
          "marking": "102"}],
    ]
    merged = _merge_photo_components(groups)
    assert len(merged) == 1
    assert merged[0]["kind"] == "resistor"
    assert merged[0]["marking"] == "102"


def test_tiled_merge_does_not_collapse_adjacent_different_components():
    groups = [[
        {"name": "R1", "kind": "resistor", "x_mm": 1, "y_mm": 2,
         "length_mm": 1.6, "width_mm": 0.8, "height_mm": 0.5},
        {"name": "C1", "kind": "capacitor", "x_mm": 1.32, "y_mm": 2.02,
         "length_mm": 1.6, "width_mm": 0.8, "height_mm": 0.5},
    ]]
    assert len(_merge_photo_components(groups)) == 2


# ── S2: internal flags only from the program, never from a raw AI answer ────

def _injected_row(**extra):
    """A raw AI row that tries to claim internal provenance."""
    row = {"name": "pad 1", "kind": "resistor", "package": "R_0603", "mating": "none",
           "x_mm": 1, "y_mm": 1, "length_mm": 1.6, "width_mm": 0.8, "height_mm": 0.5,
           "_height_estimated": False, "_height_source": "measured",
           "_yolo_manual_box": True, "_package_verified": True,
           "_orientation_verified": True,
           "_global_inventory_status": "validated_hypotheses",
           "presence_confidence": "high", "object_type": "component"}
    row.update(extra)
    return row


def test_raw_ai_rows_lose_internal_flags_and_their_height_is_an_estimate():
    """@brief Negative: a raw answer cannot declare a height measured nor a box drawn by hand.

    The row named «pad 1» is also dropped: the injected
    `_global_inventory_status`/`object_type` no longer let a PCB feature
    through the name filter.
    """
    from kicadgen.gui.component_state import height_is_estimated
    from kicadgen.services.datasheet_extractor import _components_3d, AI_ANSWER_HEIGHT_SOURCE
    assert _components_3d([_injected_row()]) is None
    result = _components_3d([_injected_row(name="resistor")])
    component = result[0]
    assert not [key for key in component
                if key.startswith("_") and key not in ("_height_estimated", "_height_source")]
    assert component["_height_estimated"] is True
    assert component["_height_source"] == AI_ANSWER_HEIGHT_SOURCE
    assert height_is_estimated(component)
    assert "presence_confidence" not in component and "object_type" not in component
    # Descriptive fields still pass.
    assert component["package"] == "R_0603" and component["mating"] == "none"


def test_trusted_photo_components_keep_their_internal_flags():
    """@brief Positive: components built by the program keep every internal flag."""
    from kicadgen.gui.component_state import height_is_estimated
    from kicadgen.services.datasheet_extractor import _components_3d
    result = _components_3d([_injected_row()], trusted=True)
    component = result[0]
    assert component["_yolo_manual_box"] is True
    assert component["_package_verified"] is True
    assert component["_height_estimated"] is False
    assert component["_global_inventory_status"] == "validated_hypotheses"
    assert not height_is_estimated(component)


def test_params_are_untrusted_by_default_and_trusted_only_on_request():
    """@brief The flag of _params_from_tool_arguments reaches the decoder, default untrusted."""
    data = {"layout": "castellated", "confidence": "low", "body_length_mm": 20,
            "body_width_mm": 10,
            "castellated_sides": [{"edge": "left", "count": 2, "pitch_mm": 2.54}],
            "components_3d": [_injected_row(name="resistor")]}
    raw = _params_from_tool_arguments(data).components_3d[0]
    trusted = _params_from_tool_arguments(data, trusted_components=True).components_3d[0]
    assert "_yolo_manual_box" not in raw and raw["_height_estimated"] is True
    assert trusted["_yolo_manual_box"] is True and trusted["_height_estimated"] is False


def test_the_components_schema_forbids_undeclared_fields():
    """@brief The model is told it may not add fields to a component."""
    items = _openai_tool()["function"]["parameters"]["properties"]["components_3d"]["items"]
    assert items["additionalProperties"] is False
    assert not [name for name in items["properties"] if name.startswith("_")]


# ── S5: pre-flight on the EFFECTIVE models ─────────────────────────────────

class _PreflightDone(Exception):
    """Stops the extraction right after the provider pre-flight."""


def _preflight(tmp_path, monkeypatch, **models):
    """Run extraction until the pre-flight; return which providers were checked."""
    from kicadgen.services import datasheet_extractor as ext, providers
    source = tmp_path / "board.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\n offline")
    checked = []
    monkeypatch.setattr(ext, "api_key", lambda: checked.append("openrouter-key") or "k")
    monkeypatch.setattr(ext, "check_credit", lambda **kw: checked.append("openrouter"))
    monkeypatch.setattr(providers, "deepseek_api_key", lambda: "sk-offline")

    def balance():
        checked.append("deepseek")
        raise _PreflightDone()
    monkeypatch.setattr(providers, "deepseek_balance", balance)
    monkeypatch.setattr(ext, "call_tool", lambda **kw: (_ for _ in ()).throw(_PreflightDone()))
    with pytest.raises(_PreflightDone):
        ext.extract_pin_header_params([source], **models)
    return checked


def test_a_deepseek_run_with_the_default_vision_model_checks_openrouter_too(tmp_path, monkeypatch):
    """@brief Positive: vision_model None means the default (OpenRouter) vision model is used."""
    checked = _preflight(tmp_path, monkeypatch, model="deepseek-api/deepseek-flash")
    assert "openrouter" in checked and "deepseek" in checked


def test_an_all_deepseek_run_does_not_ask_for_openrouter(tmp_path, monkeypatch):
    """@brief Negative: every effective model is DeepSeek, OpenRouter is not touched."""
    deepseek = "deepseek-api/deepseek-flash"
    checked = _preflight(tmp_path, monkeypatch, model=deepseek, vision_model=deepseek,
                         verification_model=deepseek)
    assert checked == ["deepseek"]


def test_an_openrouter_run_checks_only_openrouter(tmp_path, monkeypatch):
    """@brief Negative: no DeepSeek model, no DeepSeek balance read (single path, no duplicate branch)."""
    checked = _preflight(tmp_path, monkeypatch, model="vendor/model", vision_model="vendor/vision")
    assert checked == ["openrouter-key", "openrouter"]
