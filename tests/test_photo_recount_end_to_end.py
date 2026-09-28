"""@brief The photo recount from end to end, and the phases it announces (HANDOFF point 139).

The audit (point 138, D1) showed that the recount hook could be disconnected,
or handed a copy of the declared sides, with every test green: no test ran
extraction -> inventory -> recount -> repeated comparison. Here the whole
chain runs offline through `extract_pin_header_params`: the main answer
declares 3 contacts on an edge where the synthetic photo shows 4, the recount
reply says 4, and the comparison repeated AFTER the recount must confirm the
UPDATED side. Disconnecting the hook, passing a copy of the answer as
`declared`, or comparing a list read before the recount leaves the old count
in force and the warning in the log: each of those breaks these tests.

The phase contract of the modal is checked on the same runs: every announced
phase is the tool actually working -- the paid recount is a documents call
('llm'), the calibration and the comparison are OpenCV, the vision call is
announced when the photo leaves, not before the calibration (C1).
"""
import copy
from unittest.mock import Mock

import cv2
import pytest

from kicadgen.services import datasheet_extractor as extractor
from kicadgen.services import photo_global_inventory, photo_grounding, photo_inventory
from kicadgen.services.messages import WARNING, kind_of
from test_edge_notches import LENGTH, ROW_4, WIDTH, board_photo

NAMES = ["GND", "A", "B", "VCC"]


def _payload(right_count):
    """Main answer: one castellated side on the right edge, centred row at 2.54 mm."""
    return dict(layout="castellated", body_length_mm=LENGTH, body_width_mm=WIDTH,
                top_photo_index=0, drawing_unit="mm", contacts_view="top",
                castellated_sides=[dict(edge="right", count=right_count, pitch_mm=2.54,
                                        first_pin=1, contact_style="half_hole",
                                        pin_names=NAMES[:right_count])])


class _PaidPhotoCall(Exception):
    """Raised by the fake model when the vision inventory is asked for."""


@pytest.fixture
def run(monkeypatch, tmp_path):
    """@brief Run the extraction offline with a photo showing 4 half-holes on the right.

    @return Callable(recount_count, detector, rows, recount_error) -> (phases,
        logs, params, requests): the main answer declares 3 contacts; the
        photo shows half-holes at `rows` (mm from the top of the right edge);
        the recount reply declares `recount_count`, or the recount call raises
        `recount_error`; the first vision call stops the inventory.
    """
    photo = tmp_path / "top.png"
    monkeypatch.setattr(extractor, "api_key", lambda: "offline")
    monkeypatch.setattr(extractor, "check_credit", lambda **kwargs: None)

    def execute(recount_count, detector="vlm", rows=ROW_4, recount_error=None):
        cv2.imwrite(str(photo), board_photo(right=rows))
        requests = []

        def call_tool(**request):
            requests.append(request)
            text = request["content_blocks"][-1].get("text", "")
            if len(requests) == 1:
                return _payload(3)
            if text.startswith("Confronto con la fotografia"):
                if recount_error is not None:
                    raise recount_error
                return _payload(recount_count)
            raise _PaidPhotoCall("vision inventory")

        monkeypatch.setattr(extractor, "call_tool", call_tool)
        phases, logs = [], []
        params = extractor.extract_pin_header_params(
            [photo], photo_detector=detector, photo_cache_dir=tmp_path / "cache",
            progress=logs.append, phase=lambda tool, detail: phases.append((tool, detail)))
        return phases, logs, params, requests

    return execute


def _recount_requests(requests):
    return [r for r in requests
            if r["content_blocks"][-1].get("text", "").startswith("Confronto con la fotografia")]


def test_the_comparison_after_the_recount_reads_the_updated_side(run):
    phases, logs, params, requests = run(4)
    assert len(_recount_requests(requests)) == 1
    # Il confronto ripetuto vede il lato AGGIORNATO: conferma, nessun avviso.
    assert any("Lato destro: sulla foto si contano 4 semi-fori, come i contatti dichiarati."
               in line for line in logs)
    assert not [line for line in logs
                if "semi-fori" in line and kind_of(line) == WARNING]
    [right] = [side for side in params.castellated_sides if side["edge"] == "right"]
    assert right["count"] == 4 and right["pin_names"] == NAMES
    assert "Contatti ricontati dopo il confronto con la foto" in (params.notes or "")


def test_a_recount_that_confirms_the_answer_leaves_the_warning(run):
    """Negative: the reply keeps 3, so the repeated comparison still disagrees."""
    _phases, logs, params, requests = run(3)
    assert len(_recount_requests(requests)) == 1
    [warning] = [line for line in logs if "semi-fori" in line and kind_of(line) == WARNING]
    assert "4 semi-fori" in warning and "3 contatti" in warning
    [right] = [side for side in params.castellated_sides if side["edge"] == "right"]
    assert right["count"] == 3


def test_the_paid_recount_is_announced_as_a_documents_call(run):
    """C1: the recount ran under «OpenCV»; the vision call was announced before calibrating."""
    phases, _logs, _params, _requests = run(4)
    photo_phases = phases[phases.index(("opencv", "ricerca e calibrazione della scheda")):]
    assert photo_phases[:4] == [("opencv", "ricerca e calibrazione della scheda"),
                                ("llm", "ricontrollo dei contatti"),
                                ("opencv", "confronto dei contatti con la foto"),
                                ("llm_inventory", photo_phases[3][1])]
    # Before the calibration no vision call is claimed: it has not started.
    before = phases[:phases.index(("opencv", "ricerca e calibrazione della scheda"))]
    assert not [tool for tool, _ in before if tool == "llm_inventory"]


def test_no_recount_phase_when_the_photo_agrees(monkeypatch, tmp_path):
    """Negative: nothing to recount, no 'llm' phase between calibration and the vision call."""
    photo = tmp_path / "top.png"
    cv2.imwrite(str(photo), board_photo(right=ROW_4))
    phases = []
    monkeypatch.setattr(photo_global_inventory, "review_global_inventory",
                        Mock(side_effect=_PaidPhotoCall("stop")))
    declared = copy.deepcopy(_payload(4))
    with pytest.raises(_PaidPhotoCall):
        photo_inventory.extract_inventory(
            [photo], "vision/m", LENGTH, WIDTH, ["generic"], Mock(), top_photo_index=0,
            detector="vlm", declared=declared, recount_contacts=Mock(),
            phase=lambda tool, detail: phases.append((tool, detail)))
    assert phases == [("opencv", "ricerca e calibrazione della scheda"),
                      ("llm_inventory", "vision/m")]


def test_the_ai_detector_announces_the_vision_call_after_the_calibration(monkeypatch, tmp_path):
    """C1 for 'ai': the paid grounding calls are announced when they start, after OpenCV."""
    photo = tmp_path / "top.png"
    cv2.imwrite(str(photo), board_photo(right=ROW_4))
    order = []
    monkeypatch.setattr(photo_grounding, "grounded_components",
                        Mock(side_effect=lambda *a, **k: order.append("paid") or
                             (_ for _ in ()).throw(_PaidPhotoCall("stop"))))
    with pytest.raises(_PaidPhotoCall):
        photo_inventory.extract_inventory(
            [photo], "vision/m", LENGTH, WIDTH, ["generic"], Mock(), top_photo_index=0,
            detector="ai", phase=lambda tool, detail: order.append((tool, detail)))
    assert order == [("opencv", "ricerca e calibrazione della scheda"),
                     ("llm_inventory", "vision/m"), "paid"]


@pytest.mark.parametrize("detector", ["ai", "vlm"])
def test_the_extractor_does_not_claim_the_vision_call_before_the_inventory(
        monkeypatch, tmp_path, detector):
    """C1 on the extractor side: with the inventory replaced, no 'llm_inventory' is claimed.

    Before point 139 the extractor announced it ahead of the calibration and
    OpenCV covered it at once: an empty «✓» phase in the modal.
    """
    photo = tmp_path / "top.png"
    photo.write_bytes(b"offline")
    monkeypatch.setattr(extractor, "api_key", lambda: "offline")
    monkeypatch.setattr(extractor, "check_credit", lambda **kwargs: None)
    monkeypatch.setattr(extractor, "call_tool", Mock(return_value=_payload(4)))
    monkeypatch.setattr(photo_inventory, "extract_inventory",
                        Mock(return_value=photo_inventory.InventoryResult()))
    phases = []
    extractor.extract_pin_header_params(
        [photo], photo_detector=detector, photo_cache_dir=tmp_path / "cache",
        phase=lambda tool, detail: phases.append((tool, detail)))
    assert photo_inventory.extract_inventory.called
    assert not [tool for tool, _ in phases if tool == "llm_inventory"], phases


# ── The recount judged on a real calibration (R-G2) and its failures ────


def _shifted_row(fraction):
    """ROW_4 moved along the edge by `fraction` of the calibration bound of this photo."""
    from kicadgen.services.edge_notches import row_shift_bound_mm
    from test_edge_notches import SCALE
    bound = row_shift_bound_mm((int(WIDTH * SCALE), int(LENGTH * SCALE)), SCALE)
    return tuple(round(p + fraction * bound, 3) for p in ROW_4)


def test_a_row_off_centre_within_the_calibration_bound_is_adopted(run):
    """The answer says «centred» (no offset); the photo shows the row a little off.

    The shift is an arbitrary share of the bound, not the offset of a sample.
    """
    _phases, logs, params, _requests = run(4, rows=_shifted_row(0.6))
    [right] = [side for side in params.castellated_sides if side["edge"] == "right"]
    assert right["count"] == 4
    assert not [line for line in logs if "semi-fori" in line and kind_of(line) == WARNING]


def test_a_row_off_centre_beyond_the_calibration_bound_is_not_adopted(run):
    """Negative twin: the same row further off than any calibration error explains."""
    _phases, logs, params, _requests = run(4, rows=_shifted_row(1.6))
    [right] = [side for side in params.castellated_sides if side["edge"] == "right"]
    assert right["count"] == 3
    assert any("spostata" in line for line in logs)
    [warning] = [line for line in logs if "semi-fori" in line and kind_of(line) == WARNING]
    assert "4 semi-fori" in warning


@pytest.mark.parametrize("error", [KeyError("castellated_sides"), ValueError("x")])
def test_a_failing_recount_does_not_lose_the_photo_inventory(run, error):
    """A non-OpenRouter error of the recount used to escape and drop the whole inventory.

    Contained, the photo stage goes on to its vision call.
    """
    _phases, logs, params, requests = run(4, recount_error=error)
    assert len(_recount_requests(requests)) == 1
    assert len(requests) == 3          # main answer, recount, then the vision inventory
    assert any("non riuscito" in line for line in logs)
    [right] = [side for side in params.castellated_sides if side["edge"] == "right"]
    assert right["count"] == 3
