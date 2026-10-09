"""@brief Host mounting changes lands, never the physical module or its 3D payload."""

from copy import deepcopy

import pytest

from kicadgen.generators.castellated_module import (
    CastellatedModuleParams, CastellatedSide, CONTACT_HALF_AND_THROUGH,
    CONTACT_HALF_HOLE, pad_positions, build_castellated_module,
)
from kicadgen.generators.host_mounting import header_footprint_params
from kicadgen.generators.package import build_package
from kicadgen.generators.pin_header import PinHeaderParams, pin_positions


def module():
    """@brief A board with unequal side counts and reversed numbering."""
    return CastellatedModuleParams(
        name="Board", body_length=30, body_width=20,
        sides=[CastellatedSide(edge="top", count=4, pitch=2.54, first_pin=1,
                               reverse=True, contact_style=CONTACT_HALF_AND_THROUGH),
               CastellatedSide(edge="bottom", count=3, pitch=2.54, first_pin=5,
                               contact_style=CONTACT_HALF_AND_THROUGH)])


def test_round_host_pads_preserve_hole_centres_numbers_and_physical_copper():
    physical = module()
    before = deepcopy(physical)
    original = build_castellated_module(physical).pads
    host = header_footprint_params(physical, 1.6, .9)
    footprint = build_package(host)
    assert footprint.attr == "through_hole"
    for pad, placement in zip(footprint.pads, pad_positions(physical)):
        assert pad.number == placement.number
        assert pad.at == physical.thru_hole_centre(placement)
        assert pad.shape == "circle"
        assert pad.size == (1.6, 1.6)
        assert pad.drill == .9
        assert "F.Paste" not in pad.layers
    assert physical == before
    rebuilt = build_castellated_module(physical).pads
    assert [(p.at, p.size, p.drill, p.shape) for p in rebuilt] == [
        (p.at, p.size, p.drill, p.shape) for p in original]


@pytest.mark.parametrize("pad,drill", [(1, 1), (1, 1.2), (0, 1), (float('nan'), 1), (3, 1)])
def test_invalid_or_overlapping_host_pads_are_rejected(pad, drill):
    with pytest.raises(ValueError):
        header_footprint_params(module(), pad, drill)


def test_half_holes_cannot_silently_become_header_holes():
    physical = module()
    physical.sides[0].contact_style = CONTACT_HALF_HOLE
    with pytest.raises(ValueError, match="foro passante"):
        header_footprint_params(physical)


def test_sparse_matrix_keeps_pin_positions_and_original_pad_sizes():
    physical = PinHeaderParams(rows=2, cols=3, populated_pins={1, 3, 6},
                               row_spacing=10, pad_diameter=2, drill=1.2)
    host = header_footprint_params(physical, 1.5, .8)
    assert list(pin_positions(host)) == list(pin_positions(physical))
    assert physical.pad_diameter == 2 and physical.drill == 1.2
    assert host.pad_diameter == 1.5 and host.drill == .8


def test_worker_exports_host_lands_but_sends_original_module_to_3d(qapp, monkeypatch, tmp_path):
    from kicadgen.gui import main_window as gui
    from kicadgen.cad3d.body_params import Body3DParams
    physical = module()
    before = deepcopy(physical)
    worker = gui.GenerateWorker(physical, tmp_path, Body3DParams(30, 20, 3))
    worker.footprint_params = header_footprint_params(physical)
    captured = []
    monkeypatch.setattr(worker, "_generate_model", lambda: (
        captured.append(deepcopy(worker.params)) or {"step_path": tmp_path / "Board.step"}))
    errors = []
    worker.error.connect(errors.append)
    worker.cad3d_error.connect(errors.append)
    worker.run()
    assert not errors
    assert captured == [before]
    output = (tmp_path / 'Board.pretty' / 'Board.kicad_mod').read_text()
    assert 'thru_hole circle' in output
    assert 'thru_hole oval' not in output
    assert physical == before
