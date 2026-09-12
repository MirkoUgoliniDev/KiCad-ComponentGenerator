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


# ── Componenti letti dalla foto (modello 3D) ────────────────────────

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
