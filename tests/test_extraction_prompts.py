"""@brief Extraction prompts carry no sample data; provenance, contact style, kinds and roles are explicit.

Medium findings 1, 2, 12 and 13 of the audit of 2026-09-26 (HANDOFF points 82
and 85), extraction side. Every answer here is an offline tool-argument
dictionary or a mocked call: no network, no paid call, no .env.

- 1: the prompts must not carry numbers, names or counts taken from the
  sample board, and every key dimension carries a declared provenance so an
  estimated value never passes for a read one;
- 2: a castellated side whose contact style was not observed is unknown
  (None), never the sample's default;
- 13: the body-kind vocabulary is structural and an unknown kind degrades to
  generic with a note;
- 12: one document may carry several roles, and only photographs go to the
  photo inventory.
"""
import json
from unittest.mock import Mock

import pytest


def _tiny_png() -> bytes:
    """@brief A real 4x4 PNG for image fixtures.

    Document images must be decodable: since 2026-10-15 an upload whose
    metadata cannot be removed is refused (fail closed), so fake bytes named
    .png/.jpg no longer reach the fake provider.
    """
    from io import BytesIO
    from PIL import Image
    out = BytesIO()
    Image.new("RGB", (4, 4), "white").save(out, "PNG")
    return out.getvalue()


_TINY_PNG = _tiny_png()

from kicadgen.services import component_types
from kicadgen.services import datasheet_extractor as ext
from kicadgen.services.datasheet_extractor import (
    COMPONENT_KINDS, COMPONENT_KIND_DESCRIPTIONS, DOCUMENT_ROLE_KEYS, ExtractionError,
    UNDECLARED_SOURCE, VALUE_SOURCES, _params_from_tool_arguments, _validate_main_response,
    normalize_document_roles, photo_document_indices)

# ── 1. No sample data in the prompts ─────────────────────────────────────────

#: Fragments that identify the sample boards the prompts were first written
#: on (count anchor, silkscreen and markings of the RS485 module, the DC-DC
#: converter pinout, the TEST4 offsets, the 2.54 mm "standard" drill/pad and
#: the usual DIP row spacings). Each is data of ONE board: in a prompt it
#: anchors the model to that board.
SAMPLE_TOKENS = (
    "30-50", "SP3485", "VPT", "PE, B-", "A+", "-Vin", "+Vout", "Single", "Dual",
    "22.86", "25.4mm", "-1.27", "-12.7", "2.54", "7.62", "10.16", "15.24",
    "drill 1.0", "pad 1.7", "ESP32", "U.FL", "RS485", "TEST4", "0402", "0603", "0805",
    "3.0mm", "-3.0",
)


def _sample_tokens_in(text):
    """The sample-specific fragments found in `text`."""
    return [token for token in SAMPLE_TOKENS if token in text]


def _schema_text():
    return json.dumps(ext._EXTRACTION_TOOL, ensure_ascii=False)


def _run_extraction(monkeypatch, tmp_path, names, roles, answer=None):
    """Run the extraction offline; return (main call mock, photo inventory mock, log)."""
    from kicadgen.services import photo_inventory
    paths = []
    for name in names:
        path = tmp_path / name
        path.write_bytes(b"%PDF-1.4 offline" if name.endswith(".pdf") else _TINY_PNG)
        paths.append(path)
    call = Mock(return_value=answer or dict(
        layout="castellated", confidence="high", body_length_mm=40, body_width_mm=20,
        castellated_sides=[dict(edge="left", count=2, pitch_mm=2.0, contact_style="half_hole")]))
    photo = Mock(return_value=photo_inventory.InventoryResult())
    monkeypatch.setattr(ext, "api_key", lambda: "offline")
    monkeypatch.setattr(ext, "check_credit", lambda **kw: None)
    monkeypatch.setattr(ext, "call_tool", call)
    monkeypatch.setattr(photo_inventory, "extract_inventory", photo)
    log = []
    ext.extract_pin_header_params(paths, document_roles=roles, progress=log.append,
                                  photo_cache_dir=tmp_path / "cache")
    return call, photo, log


def _request_text(call):
    return "\n".join(block["text"] for block in call.call_args.kwargs["content_blocks"]
                     if block["type"] == "text")


def test_the_system_prompt_and_the_schema_carry_no_sample_data():
    assert _sample_tokens_in(ext.DEFAULT_SYSTEM_PROMPT) == []
    assert _sample_tokens_in(_schema_text()) == []


def test_the_dedicated_numbering_prompt_and_tool_carry_no_sample_data():
    """Review A, 10: the second call anchors the model as little as the first."""
    import json
    assert _sample_tokens_in(ext.DEFAULT_PIN_NUMBERING_PROMPT) == []
    assert _sample_tokens_in(json.dumps(ext._PIN_NUMBERING_TOOL, ensure_ascii=False)) == []


@pytest.mark.parametrize("component_type", component_types.COMPONENT_TYPES, ids=lambda t: t.key)
def test_no_type_instruction_carries_sample_data(component_type):
    assert _sample_tokens_in(component_type.prompt) == []


def test_the_request_and_the_role_texts_carry_no_sample_data(monkeypatch, tmp_path):
    roles = {"sheet.pdf": ["pinout", "top", "land_pattern", "side_view"], "photo.png": ["hires"]}
    call, _, _ = _run_extraction(monkeypatch, tmp_path, ["sheet.pdf", "photo.png"], roles)
    assert _sample_tokens_in(_request_text(call)) == []


def test_the_checker_catches_the_former_prompt_fragments():
    """Negative: the fragments the audit found are caught by the same check."""
    former = ("stimarle; una lista di 30-50 elementi e' il risultato giusto. "
              "(es. PE, B-, A+, G, R, T, P, V) (es. drill 1.0mm per pad 1.7mm passo 2.54mm) "
              "(es. due file distanti 22.86mm in un corpo largo 25.4mm: -1.27, non -12.7)")
    assert {"30-50", "PE, B-", "drill 1.0", "22.86", "-1.27"} <= set(_sample_tokens_in(former))


def test_the_prompts_say_there_is_no_target_count():
    """The count anchor is replaced by an explicit statement, in both places."""
    components = ext._EXTRACTION_TOOL["input_schema"]["properties"]["components_3d"]
    assert "Non esiste un numero atteso" in components["description"]
    assert "Non esiste un numero atteso" in ext.DEFAULT_SYSTEM_PROMPT


def test_the_former_defaults_migrate_and_the_current_ones_do_not():
    """The texts of commit 8970c7e are known superseded defaults; the new ones are not."""
    assert ("d00743ee29a91d396eae57cd56e40dbc2e03825d571960dd09f88ba36d0f754f"
            in ext.SUPERSEDED_SYSTEM_PROMPT_DIGESTS)
    digests = component_types.SUPERSEDED_INSTRUCTION_DIGESTS
    assert "dfcb4dd38185961f7d5e72fef151657f8b22090d065917434e3ff1cd66de5155" in digests["single_tht"]
    assert ("2a307c67e5df3ff3b3150c69de731dc7828b4781949d47391f9b6fe903263de6"
            in digests["aggregate_castellated"])
    assert component_types.text_digest(ext.DEFAULT_SYSTEM_PROMPT) not in ext.SUPERSEDED_SYSTEM_PROMPT_DIGESTS


# ── 1. Per-value provenance ──────────────────────────────────────────────────

def _matrix(**extra):
    data = dict(layout="matrix", rows=2, cols=4, pitch_mm=2.0, row_spacing_mm=6.0,
                pad_diameter_mm=1.5, drill_mm=0.9, confidence="high",
                drawing_unit="mm", contacts_view="top", body_length_mm=10.0, body_width_mm=8.0)
    data.update(extra)
    return data


def test_the_schema_asks_a_provenance_for_every_key_dimension():
    properties = ext._EXTRACTION_TOOL["input_schema"]["properties"]
    top = properties["value_sources"]["properties"]
    side = properties["castellated_sides"]["items"]["properties"]["value_sources"]["properties"]
    assert set(top) == set(ext._TOP_LENGTH_KEYS) and set(side) == set(ext._SIDE_LENGTH_KEYS)
    assert all(entry["enum"] == list(VALUE_SOURCES) for entry in (*top.values(), *side.values()))
    assert "value_sources" in ext.DEFAULT_SYSTEM_PROMPT


def test_declared_provenance_reaches_the_result_and_estimates_are_flagged():
    params = _params_from_tool_arguments(_matrix(
        body_height_mm=3.0,
        value_sources=dict(pitch_mm="read", row_spacing_mm="derived", pad_diameter_mm="read",
                           drill_mm="estimated", body_length_mm="read", body_width_mm="read",
                           body_height_mm="estimated")))
    assert params.value_sources == dict(
        pitch_mm="read", row_spacing_mm="derived", pad_diameter_mm="read", drill_mm="estimated",
        body_length_mm="read", body_width_mm="read", body_height_mm="estimated")
    assert params.values_to_review() == ["body_height_mm", "drill_mm"]
    assert "Valori STIMATI" in params.notes and "drill_mm" in params.notes


def test_values_read_from_the_documents_raise_no_review():
    """Negative: everything read or derived -> nothing flagged, no note."""
    params = _params_from_tool_arguments(_matrix(value_sources=dict(
        pitch_mm="read", row_spacing_mm="derived", pad_diameter_mm="read", drill_mm="read",
        body_length_mm="read", body_width_mm="read")))
    assert params.values_to_review() == []
    assert "STIMATI" not in params.notes and "provenienza" not in params.notes


@pytest.mark.parametrize("declared", [None, {}, {"drill_mm": "measured"}, {"drill_mm": True}])
def test_a_value_without_a_valid_provenance_is_never_read(declared):
    """An undeclared or unknown provenance is reviewed like an estimate."""
    data = _matrix() if declared is None else _matrix(value_sources=declared)
    params = _params_from_tool_arguments(data)
    assert params.value_sources["drill_mm"] == UNDECLARED_SOURCE
    assert "drill_mm" in params.values_to_review()
    assert "provenienza dichiarata" in params.notes


def test_a_malformed_provenance_object_is_dropped_with_a_note():
    params = _params_from_tool_arguments(_matrix(value_sources="read"))
    assert set(params.value_sources.values()) == {UNDECLARED_SOURCE}
    assert "value_sources scartato" in params.notes


def test_absent_values_have_no_provenance():
    """A declaration for a value the result does not carry is not a value."""
    params = _params_from_tool_arguments(_matrix(value_sources=dict(body_height_mm="read")))
    assert params.body_height_mm is None and "body_height_mm" not in params.value_sources


def test_side_provenance_follows_the_mirrored_edge():
    """A bottom view mirrors left and right: the provenance moves with its side."""
    data = dict(layout="castellated", confidence="high", drawing_unit="mm",
                contacts_view="bottom", body_length_mm=20.0, body_width_mm=10.0,
                castellated_sides=[dict(edge="left", count=3, pitch_mm=2.0, contact_style="half_hole",
                                        half_hole_diameter_mm=1.0,
                                        value_sources=dict(pitch_mm="read",
                                                           half_hole_diameter_mm="estimated"))])
    params = _params_from_tool_arguments(data)
    assert params.castellated_sides[0]["edge"] == "right"
    assert params.value_sources["castellated_sides.right.pitch_mm"] == "read"
    assert params.value_sources["castellated_sides.right.half_hole_diameter_mm"] == "estimated"
    assert not any(key.startswith("castellated_sides.left") for key in params.value_sources)
    # The side dict itself keeps only its geometric keys.
    assert "value_sources" not in params.castellated_sides[0]


def test_side_provenance_survives_the_numbering_repair():
    """The one paid repair rewrites the sides: the declared provenance must stay."""
    data = dict(layout="castellated", confidence="high", drawing_unit="mm", contacts_view="top",
                body_length_mm=20.0, body_width_mm=10.0, castellated_sides=[
                    dict(edge="left", count=3, pitch_mm=2.0, first_pin=1, contact_style="half_hole",
                         value_sources=dict(pitch_mm="read")),
                    dict(edge="right", count=3, pitch_mm=2.0, first_pin=1, contact_style="half_hole",
                         value_sources=dict(pitch_mm="estimated"))])
    repaired = dict(castellated_sides=[dict(edge="left", count=3, pitch_mm=2.0, first_pin=1),
                                       dict(edge="right", count=3, pitch_mm=2.0, first_pin=4)])
    call = Mock(return_value=repaired)
    _validate_main_response(data, [], call, None, None, False, None)
    params = _params_from_tool_arguments(data)
    assert call.call_count == 1
    assert params.value_sources["castellated_sides.left.pitch_mm"] == "read"
    assert params.value_sources["castellated_sides.right.pitch_mm"] == "estimated"


# ── 2. Unobserved contact style: unknown, never the sample default ───────────

def test_unobserved_styles_are_none_and_listed_for_the_user():
    params = _params_from_tool_arguments(dict(
        layout="castellated", confidence="high", drawing_unit="mm", contacts_view="top",
        castellated_sides=[dict(edge="left", count=2, pitch_mm=2.0),
                           dict(edge="right", count=2, pitch_mm=2.0, contact_style="through_hole")]))
    styles = {side["edge"]: side["contact_style"] for side in params.castellated_sides}
    assert styles == {"left": None, "right": "through_hole"}
    assert params.unobserved_contact_style_edges() == ["left"]
    assert "Nessun tipo assunto" in params.notes


def test_observed_styles_need_no_question():
    """Negative: every side observed -> nothing to ask, no note."""
    params = _params_from_tool_arguments(dict(
        layout="castellated", confidence="high", drawing_unit="mm", contacts_view="top",
        castellated_sides=[dict(edge="left", count=2, pitch_mm=2.0, contact_style="half_hole")]))
    assert params.unobserved_contact_style_edges() == []
    assert "non osservato" not in params.notes


def test_a_matrix_result_has_no_contact_style_question():
    assert _params_from_tool_arguments(_matrix()).unobserved_contact_style_edges() == []


# ── 13. Body kinds: structural vocabulary, unknown kinds degrade ─────────────

def test_the_kind_vocabulary_is_structural_and_offered_whole():
    kind = (ext._EXTRACTION_TOOL["input_schema"]["properties"]["components_3d"]["items"]
            ["properties"]["kind"])
    assert kind["enum"] == list(COMPONENT_KINDS) == list(COMPONENT_KIND_DESCRIPTIONS)
    assert COMPONENT_KINDS[0] == "generic"
    for name in COMPONENT_KINDS:
        assert f"{name} = " in kind["description"]
    # Shapes that decide height and outline and were missing before.
    for name in ("ic_sot", "ic_qfp", "power_tab", "capacitor_electrolytic", "inductor_power",
                 "crystal_can", "pin_header", "battery", "display"):
        assert name in COMPONENT_KINDS


def _component(kind):
    return dict(name="Corpo", x_mm=0.0, y_mm=0.0, length_mm=3.0, width_mm=2.0, height_mm=1.0,
                kind=kind)


def test_an_unknown_kind_degrades_to_generic_with_a_note():
    params = _params_from_tool_arguments(_matrix(components_3d=[_component("vacuum_tube")]))
    assert params.components_3d[0]["kind"] == "generic"
    assert "'vacuum_tube' non previsto" in params.notes


@pytest.mark.parametrize("kind", ["capacitor_electrolytic", "ic_qfp", "generic"])
def test_a_known_kind_is_kept_without_a_note(kind):
    """Negative: a kind of the vocabulary passes unchanged and silently."""
    params = _params_from_tool_arguments(_matrix(components_3d=[_component(kind)]))
    assert params.components_3d[0]["kind"] == kind
    assert "non previsto" not in params.notes


# ── 12. Several roles per document; only photographs go to the inventory ────

def test_a_file_with_several_roles_is_described_for_each(monkeypatch, tmp_path):
    roles = {"sheet.pdf": ["pinout", "top", "land_pattern"], "photo.png": ["hires"]}
    call, photo, _ = _run_extraction(monkeypatch, tmp_path, ["sheet.pdf", "photo.png"], roles)
    blocks = call.call_args.kwargs["content_blocks"]
    sheet = blocks[0]["text"]
    assert "RUOLI DICHIARATI" in sheet
    for name in ("Pinout / disposizione contatti", "Immagine con quote", "Piazzole consigliate"):
        assert name in sheet
    # The same file IS the dimensioned top view: it must not be excluded from it.
    assert "non usarlo per gli ingombri" not in sheet
    assert "Viste non fornite: Vista laterale" in _request_text(call)
    assert photo.call_args.kwargs["top_photo_index"] == [1]


def test_a_pinout_only_file_is_kept_out_of_the_body_envelope(monkeypatch, tmp_path):
    """Negative of the previous: without 'top' on the same file the restriction stays."""
    roles = {"pins.pdf": ["pinout"], "drawing.png": ["top"], "photo.png": ["hires"]}
    call, _, _ = _run_extraction(monkeypatch, tmp_path, ["pins.pdf", "drawing.png", "photo.png"],
                                 roles)
    blocks = call.call_args.kwargs["content_blocks"]
    assert "non usarlo per gli ingombri" in blocks[0]["text"]
    assert "non usarlo per gli ingombri" not in blocks[2]["text"]


def test_land_pattern_and_side_view_are_explained(monkeypatch, tmp_path):
    roles = {"land.png": ["land_pattern"], "side.png": ["side_view"]}
    call, photo, _ = _run_extraction(monkeypatch, tmp_path, ["land.png", "side.png"], roles)
    blocks = call.call_args.kwargs["content_blocks"]
    assert "piazzole" in blocks[0]["text"] and "body_*" in blocks[0]["text"]
    assert "body_height_mm" in blocks[2]["text"] and "pcb_thickness_mm" in blocks[2]["text"]
    # No photograph declared: nothing goes to the inventory in its place.
    assert photo.call_args.kwargs["top_photo_index"] == [-1]


def test_a_drawing_is_not_a_photograph_in_the_new_form(monkeypatch, tmp_path):
    """A version 2 mapping is taken as declared: 'top' alone is not sent to the inventory."""
    _, photo, log = _run_extraction(monkeypatch, tmp_path, ["drawing.png"], {"drawing.png": ["top"]})
    assert photo.call_args.kwargs["top_photo_index"] == [-1]
    assert any("disponibili: nessuna" in line for line in log)


def test_the_log_line_lists_every_role_of_a_file(monkeypatch, tmp_path):
    roles = {"sheet.pdf": ["pinout", "top"]}
    _, _, log = _run_extraction(monkeypatch, tmp_path, ["sheet.pdf"], roles)
    (line,) = [entry for entry in log if entry.startswith("Invio a AI")]
    assert "sheet.pdf — Pinout / disposizione contatti + Immagine con quote" in line


@pytest.mark.parametrize("roles, expected", [
    # Legacy form: a lone dimensioned view stood in for the photo (version 1).
    ({"a.png": "top"}, [0]),
    ({"a.png": "pinout", "b.png": "top"}, [1]),
    # Legacy form with a photograph: the drawing is no longer a photo.
    ({"a.png": "hires", "b.png": "top"}, [0]),
    # Version 2: exactly what is declared.
    ({"a.png": ["top"], "b.png": ["pinout"]}, [-1]),
    ({"a.png": ["top", "hires"], "b.png": ["hires"]}, [0, 1]),
])
def test_photo_indices_follow_the_declared_photographs(roles, expected):
    paths = [f"dir/{name}" for name in sorted(roles)]
    assert photo_document_indices(paths, roles) == expected


def test_no_declared_roles_leave_the_choice_to_the_model():
    assert photo_document_indices(["a.png"], None) is None
    assert normalize_document_roles(None, ["a.png"]) is None


def test_normalized_roles_are_ordered_tuples_without_repetitions():
    roles = normalize_document_roles({"a.pdf": ["top", "pinout", "top"]}, ["a.pdf"])
    assert roles == {"a.pdf": ("pinout", "top")}
    assert set(roles["a.pdf"]) <= set(DOCUMENT_ROLE_KEYS)


@pytest.mark.parametrize("names, roles", [
    (["a.png", "b.png"], {"a.png": "top", "b.png": "top"}),     # legacy: one file per role
    (["a.png"], {"a.png": ["photo"]}),                          # unknown role
    (["a.png"], {"a.png": []}),                                 # no role
    (["a.png", "b.png"], {"a.png": "top", "c.png": "hires"}),   # a file not sent
    (["a.png"], {"a.png": 3}),                                  # not a role
])
def test_invalid_roles_fail_before_any_call(monkeypatch, tmp_path, names, roles):
    network = Mock()
    monkeypatch.setattr(ext, "api_key", network)
    paths = [tmp_path / name for name in names]
    with pytest.raises(ExtractionError, match="Ruoli documenti"):
        ext.extract_pin_header_params(paths, document_roles=roles)
    network.assert_not_called()


def test_one_role_on_several_files_is_valid_in_the_new_form():
    """Two photographs (or two pinout pages) are legitimate once a file is not a role slot."""
    roles = normalize_document_roles({"a.png": ["hires"], "b.png": ["hires"]}, ["a.png", "b.png"])
    assert roles == {"a.png": ("hires",), "b.png": ("hires",)}


def test_the_role_names_are_the_labels_the_user_sees():
    """Model and user read the same words for every role both modules know."""
    from kicadgen.services.document_roles import ROLES
    for key in DOCUMENT_ROLE_KEYS:
        if key in ROLES:
            assert ext._ROLE_PROMPTS[key][0] == ROLES[key], key
