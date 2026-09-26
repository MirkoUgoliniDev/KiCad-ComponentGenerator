"""@brief Overhanging bodies stay where measured unless the overhang is within the measurement uncertainty.

Decisione dell'utente (2026-09-26, punto 84 medio 4): un componente che sporge
dal bordo resta dove e' stato misurato, con una nota nel log; viene riportato
dentro solo se lo sforamento e' minore dell'incertezza di misura.

Positivo e negativo si distinguono per una ragione STRUTTURALE: lo stesso
sforamento di 0.5 mm e' rumore su una foto a 5 px/mm (incertezza 0.8 mm) e
una sporgenza vera su una a 50 px/mm (incertezza 0.08 mm).
"""
import math

import pytest

from kicadgen.cad3d import kicad_models as km
from kicadgen.cad3d import measurement_uncertainty as mu
from kicadgen.cad3d import step_generator as cad
from kicadgen.cad3d.body_params import Body3DParams
from kicadgen.generators.castellated_module import (
    CastellatedModuleParams, CastellatedSide, ComponentBox,
)


# ── Incertezza ──────────────────────────────────────────────────────


def test_the_assumed_uncertainty_is_declared_and_derived_from_the_fallback_scale():
    value, source = mu.edge_uncertainty_mm()
    assert source == mu.SOURCE_ASSUMED
    assert value == pytest.approx(mu.EDGE_UNCERTAINTY_PX / mu.ASSUMED_SCALE_PX_PER_MM)


def test_a_declared_uncertainty_wins_over_the_scale():
    assert mu.edge_uncertainty_mm(declared_mm=0.05, scale_px_per_mm=4.0) == (
        0.05, mu.SOURCE_DECLARED)
    value, source = mu.edge_uncertainty_mm(scale_px_per_mm=40.0)
    assert (value, source) == (pytest.approx(0.05), mu.SOURCE_PHOTO_SCALE)


@pytest.mark.parametrize("bad", [math.nan, math.inf, 0.0, -3.0, True, "12", None])
def test_invalid_scales_are_missing_data_never_a_measurement(bad):
    assert mu.edge_uncertainty_mm(declared_mm=bad, scale_px_per_mm=bad)[1] == mu.SOURCE_ASSUMED


def test_a_component_carries_its_uncertainty_in_the_evidence():
    box = ComponentBox(0, 0, 2, 1, 1, match_evidence={mu.SCALE_KEY: 10.0})
    assert mu.component_edge_uncertainty(box, 50.0) == (pytest.approx(0.2),
                                                        mu.SOURCE_PHOTO_SCALE)
    # Senza dato proprio vale la scala della foto della scheda.
    assert mu.component_edge_uncertainty(ComponentBox(0, 0, 2, 1, 1), 50.0)[0] == \
        pytest.approx(0.04)


# ── Decisione per asse ──────────────────────────────────────────────


def test_a_body_overhanging_by_several_mm_stays_where_measured():
    """Positivo: 3 mm oltre il bordo con tolleranza 0.17 mm -> resta."""
    x, y, kept_x, kept_y = cad.place_component(12.0, 0.0, 2.0, 1.0, 0.0, 20.0, 10.0, 0.17)
    assert (x, y) == (12.0, 0.0)
    assert kept_x == pytest.approx(3.0) and kept_y == 0.0


def test_a_body_overhanging_less_than_the_uncertainty_is_moved_inside():
    """Negativo: 0.1 mm oltre il bordo con tolleranza 0.17 mm -> dentro, con il margine."""
    x, y, kept_x, kept_y = cad.place_component(9.1, 0.0, 2.0, 1.0, 0.0, 20.0, 10.0, 0.17)
    assert x == pytest.approx(10.0 - 1.0 - cad.COMPONENT_EDGE_MARGIN_MM)
    assert (kept_x, kept_y) == (0.0, 0.0)


def test_each_axis_is_decided_on_its_own():
    x, y, kept_x, kept_y = cad.place_component(12.0, 4.6, 2.0, 1.0, 0.0, 20.0, 10.0, 0.17)
    assert x == 12.0 and kept_x == pytest.approx(3.0)
    assert y == pytest.approx(5.0 - 0.5 - cad.COMPONENT_EDGE_MARGIN_MM) and kept_y == 0.0


@pytest.mark.parametrize("scale,kept", [(5.0, False), (50.0, True)])
def test_the_same_overhang_is_noise_or_geometry_depending_on_the_photo_scale(scale, kept):
    """0.5 mm di sforamento: rumore a 5 px/mm (tolleranza 0.8), vero a 50 px/mm (0.08)."""
    edge, _ = mu.edge_uncertainty_mm(scale_px_per_mm=scale)
    x, _, kept_x, _ = cad.place_component(9.5, 0.0, 2.0, 1.0, 0.0, 20.0, 10.0,
                                          mu.side_uncertainty_mm(edge))
    assert (x == 9.5) is kept and (kept_x > 0) is kept


def test_rotated_overhang_uses_the_rotated_outline():
    # 8 x 2 ruotato di 90 gradi: sulla X occupa 2 mm, sulla Y 8 mm.
    assert cad.component_overhang(0.0, 3.0, 8.0, 2.0, 90.0, 20.0, 10.0) == (
        0.0, pytest.approx(2.0))
    with pytest.raises(ValueError):
        cad.component_overhang(math.nan, 0.0, 8.0, 2.0, 0.0, 20.0, 10.0)
    with pytest.raises(ValueError):
        cad.place_component(0.0, 0.0, 8.0, 2.0, 0.0, 20.0, 10.0, -1.0)


# ── Orchestratore ───────────────────────────────────────────────────


def _castellated(monkeypatch, tmp_path, components, **options):
    captured = {}

    def fake_run(name, pins, body, output_dir, **kw):
        captured["components"] = kw["extra_payload"]["components"]
        return {"step_path": output_dir / "x.step", "warnings": [], "notes": [],
                "mesh_path": None}
    monkeypatch.setattr(cad, "_run_freecad", fake_run)
    monkeypatch.setattr(km, "attach_models", lambda components, **kw: None)
    params = CastellatedModuleParams(name="Overhang", body_length=20, body_width=10,
                                     sides=[CastellatedSide(edge="left", count=2, pitch=2.54)],
                                     components=components)
    result = cad.generate_castellated_3d_model(params, Body3DParams(20, 10, 3.0), tmp_path,
                                               **options)
    return result, {c["name"]: c for c in captured["components"]}


def test_the_overhang_is_kept_in_3d_and_noted_in_the_log(monkeypatch, tmp_path):
    result, built = _castellated(monkeypatch, tmp_path, [
        ComponentBox(12.0, 0, 2, 1, 1.0, name="Antenna"),
        ComponentBox(9.1, 0, 2, 1, 1.0, name="Rumore")])
    assert built["Antenna"]["x"] == 12.0
    assert built["Rumore"]["x"] == pytest.approx(8.8)
    antenna = [n for n in result["notes"] if "Antenna" in n]
    assert len(antenna) == 1 and "sporge" in antenna[0] and "3.00" in antenna[0]
    # Senza scala nota il ripiego e' dichiarato nella nota stessa.
    assert "stimata" in antenna[0]
    noise = [n for n in result["notes"] if "Rumore" in n]
    assert len(noise) == 1 and "spostato" in noise[0]


def test_the_board_photo_scale_decides_the_tolerance(monkeypatch, tmp_path):
    """Lo stesso sforamento di 0.5 mm: spostato a 5 px/mm, lasciato a 50 px/mm."""
    row = [ComponentBox(9.5, 0, 2, 1, 1.0, name="Bordo")]
    _, coarse = _castellated(monkeypatch, tmp_path, row, photo_scale_px_per_mm=5.0)
    result, fine = _castellated(monkeypatch, tmp_path, row, photo_scale_px_per_mm=50.0)
    assert coarse["Bordo"]["x"] < 9.5
    assert fine["Bordo"]["x"] == 9.5
    note = next(n for n in result["notes"] if "Bordo" in n)
    assert "scala della foto" in note and "stimata" not in note


def test_an_edge_mating_connector_is_never_moved(monkeypatch, tmp_path):
    result, built = _castellated(monkeypatch, tmp_path, [
        ComponentBox(9.1, 0, 2, 1, 1.0, name="USB", mating="edge")])
    assert built["USB"]["x"] == 9.1
    assert not [n for n in result["notes"] if "USB" in n]
