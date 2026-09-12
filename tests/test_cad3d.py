"""Test per kicadgen.cad3d: validazione parametri, ricerca freecadcmd,
e un test di integrazione reale (skippato se FreeCAD non e' installato)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from kicadgen.cad3d.body_params import Body3DParams
from kicadgen.cad3d.step_generator import Cad3DError, find_freecadcmd, generate_3d_model
from kicadgen.generators.pin_header import PinHeaderParams


def test_body_params_rejects_non_positive_dimensions():
    body = Body3DParams(length_mm=0, width_mm=10, height_mm=5)
    with pytest.raises(ValueError, match="dimensioni del corpo"):
        body.validate()


def test_body_params_rejects_non_positive_pin_length():
    body = Body3DParams(length_mm=10, width_mm=10, height_mm=5, pin_length_mm=0)
    with pytest.raises(ValueError, match="lunghezza pin"):
        body.validate()


def test_find_freecadcmd_raises_when_not_found(monkeypatch):
    monkeypatch.delenv("FREECAD_CMD_PATH", raising=False)
    monkeypatch.setattr("kicadgen.cad3d.step_generator.glob.glob", lambda pattern: [])
    with pytest.raises(Cad3DError, match="freecadcmd non trovato"):
        find_freecadcmd()


def test_find_freecadcmd_uses_env_override(tmp_path, monkeypatch):
    fake = tmp_path / "freecadcmd.exe"
    fake.write_text("")
    monkeypatch.setenv("FREECAD_CMD_PATH", str(fake))
    assert find_freecadcmd() == str(fake)


def _freecadcmd_available() -> bool:
    try:
        find_freecadcmd()
        return True
    except Cad3DError:
        return False


@pytest.mark.skipif(not _freecadcmd_available(), reason="FreeCAD non installato su questa macchina")
def test_generate_3d_model_real_integration(tmp_path):
    pin_params = PinHeaderParams(
        name="TEST_CAD3D", rows=2, cols=2, pitch=2.54, row_spacing=5.0,
        pad_diameter=1.0, drill=0.5,
    )
    body = Body3DParams(length_mm=10, width_mm=8, height_mm=4, pin_length_mm=2.0)

    result = generate_3d_model(pin_params, body, tmp_path)
    assert result["step_path"].is_file()
    assert result["step_path"].stat().st_size > 0


@pytest.mark.skipif(not _freecadcmd_available(), reason="FreeCAD non installato su questa macchina")
def test_generate_3d_model_with_multiple_fillets(tmp_path):
    """Regressione: con piu' di un fillet il solido acquista spigoli circolari
    chiusi (un solo vertice); classificarli assumendo due vertici faceva fallire
    la generazione con 'list index out of range'."""
    from kicadgen.cad3d.annotations import EdgeFilletAnnotation

    pin_params = PinHeaderParams(
        name="TEST_CAD3D_FILLETS", rows=2, cols=2, pitch=2.54, row_spacing=5.0,
        pad_diameter=1.0, drill=0.5,
    )
    body = Body3DParams(length_mm=10, width_mm=8, height_mm=4, pin_length_mm=2.0)
    fillets = [
        EdgeFilletAnnotation(edge=e, radius_mm=0.5)
        for e in ("top-front", "top-back", "top-left", "top-right")
    ]

    result = generate_3d_model(pin_params, body, tmp_path, fillets=fillets)
    assert result["step_path"].is_file()
    assert result["step_path"].stat().st_size > 0
