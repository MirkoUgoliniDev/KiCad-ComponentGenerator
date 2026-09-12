"""Test del generatore di moduli con piazzole a mezzo foro (castellated).

Il caso di riferimento e' un convertitore RS485/TTL isolato da
42.8 x 15.2 mm: 3 contatti a passo 5.08mm sul lato sinistro (PE, B-, A+)
e 5 a passo 2.54mm sul lato destro. E' proprio il componente che il
generatore a matrice non puo' rappresentare, perche' i due lati hanno
passi diversi.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from kicadgen.generators.castellated_module import (
    CastellatedModuleParams, CastellatedSide, build_castellated_module,
    pad_positions,
)


def rs485_module() -> CastellatedModuleParams:
    return CastellatedModuleParams(
        name="RS485_TTL_Isolated_42x15",
        body_length=42.8,
        body_width=15.2,
        pad_length=1.8,
        pad_width=1.2,
        pad_overhang=0.5,
        sides=[
            CastellatedSide(edge="left", count=3, pitch=5.08, first_pin=1),
            CastellatedSide(edge="right", count=5, pitch=2.54, first_pin=4),
        ],
    )


def test_two_sides_keep_their_own_pitch():
    """Il punto di tutto il generatore: passi diversi sui due lati."""
    placements = {p.number: p for p in pad_positions(rs485_module())}

    assert len(placements) == 8
    # Lato sinistro: 3 pad a 5.08mm, centrati sul bordo.
    assert placements["1"].y == pytest.approx(-5.08)
    assert placements["2"].y == pytest.approx(0.0)
    assert placements["3"].y == pytest.approx(5.08)
    # Lato destro: 5 pad a 2.54mm, indipendenti dal passo di sinistra.
    assert placements["4"].y == pytest.approx(-5.08)
    assert placements["5"].y == pytest.approx(-2.54)
    assert placements["8"].y == pytest.approx(5.08)


def test_pads_sit_on_the_body_edges_and_overhang_outward():
    params = rs485_module()
    placements = {p.number: p for p in pad_positions(params)}

    # Bordo esterno del pad = bordo del corpo + overhang, sui due lati.
    left = placements["1"]
    assert left.x - left.width / 2 == pytest.approx(-42.8 / 2 - 0.5)
    right = placements["4"]
    assert right.x + right.width / 2 == pytest.approx(42.8 / 2 + 0.5)


def test_pads_are_oriented_along_their_edge():
    params = CastellatedModuleParams(
        body_length=20, body_width=10, pad_length=1.8, pad_width=1.0,
        sides=[
            CastellatedSide(edge="left", count=2, pitch=2.54, first_pin=1),
            CastellatedSide(edge="top", count=2, pitch=2.54, first_pin=3),
        ],
    )
    placements = {p.number: p for p in pad_positions(params)}
    # Lato verticale: il lato lungo del pad e' orizzontale (perpendicolare al bordo).
    assert (placements["1"].width, placements["1"].height) == (1.8, 1.0)
    # Lato orizzontale: le dimensioni si scambiano.
    assert (placements["3"].width, placements["3"].height) == (1.0, 1.8)
    assert placements["3"].y < 0  # "top" e' a Y negativo, come in KiCad


def test_reverse_numbering_along_the_edge():
    side = CastellatedSide(edge="right", count=3, pitch=2.54, first_pin=4, reverse=True)
    assert side.numbers() == ["6", "5", "4"]


def test_explicit_pin_numbers_win():
    side = CastellatedSide(
        edge="left", count=3, pitch=5.08, pin_numbers=["PE", "B-", "A+"]
    )
    assert side.numbers() == ["PE", "B-", "A+"]


def test_offset_shifts_the_group_along_the_edge():
    params = CastellatedModuleParams(
        body_length=20, body_width=10,
        sides=[CastellatedSide(edge="left", count=2, pitch=2.54, offset=1.0)],
    )
    ys = [p.y for p in pad_positions(params)]
    assert ys == pytest.approx([-1.27 + 1.0, 1.27 + 1.0])


# ── Validazione ─────────────────────────────────────────────────────

def test_overlapping_pads_are_rejected():
    params = CastellatedModuleParams(
        body_length=20, body_width=10, pad_width=2.0,
        sides=[CastellatedSide(edge="left", count=2, pitch=1.0)],
    )
    with pytest.raises(ValueError, match="sovrapporrebbero"):
        params.validate()


def test_pads_longer_than_the_edge_are_rejected():
    params = CastellatedModuleParams(
        body_length=20, body_width=10,
        sides=[CastellatedSide(edge="left", count=10, pitch=2.54)],
    )
    with pytest.raises(ValueError, match="bordo e' lungo"):
        params.validate()


def test_duplicate_pin_numbers_across_sides_are_rejected():
    params = CastellatedModuleParams(
        body_length=20, body_width=10,
        sides=[
            CastellatedSide(edge="left", count=2, pitch=2.54, first_pin=1),
            CastellatedSide(edge="right", count=2, pitch=2.54, first_pin=2),
        ],
    )
    with pytest.raises(ValueError, match="compare sia sul lato"):
        params.validate()


def test_unknown_edge_is_rejected():
    params = CastellatedModuleParams(
        body_length=20, body_width=10,
        sides=[CastellatedSide(edge="diagonale", count=2, pitch=2.54)],
    )
    with pytest.raises(ValueError, match="Lato non valido"):
        params.validate()


# ── Footprint ───────────────────────────────────────────────────────

def test_footprint_has_smd_pads_without_drill():
    fp = build_castellated_module(rs485_module())
    assert fp.attr == "smd"
    assert len(fp.pads) == 8
    for pad in fp.pads:
        assert pad.pad_type == "smd"
        assert pad.drill is None
        assert list(pad.layers) == ["F.Cu", "F.Paste", "F.Mask"]


def test_footprint_writes_valid_file(tmp_path):
    fp = build_castellated_module(rs485_module())
    target = tmp_path / "mod.pretty" / "RS485.kicad_mod"
    fp.save(str(target))
    content = target.read_text(encoding="utf-8")
    assert content.startswith("(footprint")
    assert "(attr smd)" in content
    assert "F.CrtYd" in content
    assert "thru_hole" not in content


# ── Componenti sul modulo (solo modello 3D) ─────────────────────────

def test_component_box_requires_a_real_volume():
    from kicadgen.generators.castellated_module import ComponentBox
    assert ComponentBox(x=0, y=0, length=2, width=2, height=1).is_usable()
    assert not ComponentBox(x=0, y=0, length=2, width=0, height=1).is_usable()


def test_components_do_not_affect_the_footprint():
    """I componenti servono al 3D: il footprint 2D non deve cambiare."""
    from kicadgen.generators.castellated_module import ComponentBox
    plain = build_castellated_module(rs485_module())
    with_components = rs485_module()
    with_components.components = [
        ComponentBox(x=0, y=0, length=9, width=6, height=4.5, name="trasformatore")
    ]
    populated = build_castellated_module(with_components)
    assert len(plain.pads) == len(populated.pads)
    assert [(p.number, p.at) for p in plain.pads] == [(p.number, p.at) for p in populated.pads]
