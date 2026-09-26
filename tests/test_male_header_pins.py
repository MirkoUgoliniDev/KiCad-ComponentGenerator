"""@brief Pins of a populated board are a male header, and can be switched off.

User requests (2026-09-26): «se scegli fori passanti non devi mettere i pin,
oppure un'opzione»; «se voglio i pin per collegare devi usare questi» (photo
of a board with male strips soldered underneath).
"""
from unittest.mock import Mock

import pytest

from kicadgen.cad3d import step_generator as step
from kicadgen.cad3d.body_params import Body3DParams
from kicadgen.cad3d.header_geometry import (LONG_TAIL_MM, MIN_TOP_PROTRUSION_MM, PIN_SIDE_MM,
                                            SHORT_TAIL_MM, SPACER_HEIGHT_MM, header_pitch,
                                            male_header_boxes)
from kicadgen.generators.pin_header import PinHeaderParams


def _row(count, pitch=2.54, y=0.0):
    return [(n + 1, n * pitch, y) for n in range(count)]


def test_a_standard_strip_has_square_pins_through_a_spacer_under_the_board():
    boxes = male_header_boxes(_row(3), board_top_z=1.6)
    spacers = [b for b in boxes if b["role"] == "spacer"]
    pins = [b for b in boxes if b["role"] == "pin"]
    # Non «Pin<n>»: quel nome e' del contatto del foro, e l'anteprima numera i
    # «Pin» + cifre. Con lo stesso nome i numeri comparivano due volte.
    assert [b["name"] for b in pins] == ["HeaderPin1", "HeaderPin2", "HeaderPin3"]
    for spacer in spacers:
        assert spacer["z"] == pytest.approx(-SPACER_HEIGHT_MM)
        assert spacer["z"] + spacer["dz"] == pytest.approx(0.0), "a contatto con il lato inferiore"
        assert spacer["dx"] == pytest.approx(2.54)
    # Celle vicine si toccano: si leggono come la barra continua dello strip.
    assert spacers[0]["x"] + spacers[0]["dx"] == pytest.approx(spacers[1]["x"])
    for pin in pins:
        assert pin["dx"] == pin["dy"] == pytest.approx(PIN_SIDE_MM), "sezione quadrata"
        assert pin["z"] == pytest.approx(-SPACER_HEIGHT_MM - LONG_TAIL_MM)
        assert pin["z"] + pin["dz"] == pytest.approx(SHORT_TAIL_MM), "sporge sopra la scheda"


def test_a_thick_board_still_shows_the_pin_on_top():
    """@brief Negativo della coda corta: con una scheda piu' spessa della coda non sparisce."""
    pin = [b for b in male_header_boxes(_row(1), board_top_z=4.0) if b["role"] == "pin"][0]
    assert pin["z"] + pin["dz"] == pytest.approx(4.0 + MIN_TOP_PROTRUSION_MM)


def test_other_pitches_scale_the_strip():
    assert header_pitch(_row(4, pitch=2.0)) == pytest.approx(2.0)
    spacer = male_header_boxes(_row(2, pitch=2.0), board_top_z=1.6)[0]
    assert spacer["dx"] == pytest.approx(2.0)
    assert spacer["dz"] == pytest.approx(SPACER_HEIGHT_MM * 2.0 / 2.54)
    assert header_pitch([(1, 0, 0)]) == pytest.approx(2.54), "un pin solo: passo di riferimento"


def _payload(monkeypatch, tmp_path, **body_extra):
    run = Mock(return_value={})
    monkeypatch.setattr(step, "_run_freecad", run)
    body = Body3DParams(length_mm=40, width_mm=20, height_mm=5, populated_board=True,
                        components=[], **body_extra)
    step.generate_3d_model(PinHeaderParams(), body, tmp_path)
    return run


def test_a_populated_board_asks_for_a_male_header(monkeypatch, tmp_path):
    run = _payload(monkeypatch, tmp_path)
    assert run.call_args.kwargs["extra_payload"]["pin_style"] == "male_header"
    assert run.call_args.args[2].show_pins_3d is True


def test_pins_can_be_switched_off(monkeypatch, tmp_path):
    run = _payload(monkeypatch, tmp_path, show_pins_3d=False)
    assert run.call_args.args[2].show_pins_3d is False


def test_a_single_component_keeps_its_cylindrical_leads(monkeypatch, tmp_path):
    """@brief Negativo: un componente singolo a fori passanti non riceve uno strip."""
    run = Mock(return_value={})
    monkeypatch.setattr(step, "_run_freecad", run)
    step.generate_3d_model(PinHeaderParams(), Body3DParams(length_mm=10, width_mm=5, height_mm=4),
                           tmp_path)
    assert "pin_style" not in (run.call_args.kwargs.get("extra_payload") or {})
