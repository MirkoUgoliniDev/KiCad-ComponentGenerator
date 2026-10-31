"""@brief Unconfirmed pad positions block; the drawn holes may pick the one right reading (user, 2026-10-30).

A relay footprint was generated from a reading nobody had confirmed and
came out wrong. Since then a single part whose explicit pads no second
reading confirms is BLOCKED, with one way out: the holes drawn in the
documents (drawing_holes, a local check independent of both models)
confirm exactly one of the readings at hand and none of the others.

The hole check is faked here by its contract: it sees GEOMETRY only, the
set of positions up to a translation, never the pad numbers. The drawing is
invented (a 20 x 12 mm body, five holes), not the relay.
"""
import pytest

from kicadgen.services.datasheet_extractor import apply_dedicated_pads, pads_from_chains

#: x: outline, 1, {2, 5}, {3, 4}, outline; y: outline, {1, 3}, 2, {4, 5}, outline.
CHAINS = [
    {"axis": "x", "stops": ["edge", "1", "2,5", "3,4", "edge"], "values": [2.0, 5.0, 9.0, 4.0]},
    {"axis": "y", "stops": ["edge", "1,3", "2", "4,5", "edge"], "values": [2.0, 3.0, 4.0, 3.0]},
]
#: The same holes from the outline's top-left corner (page: X right, Y down).
TRUE = {"1": (2.0, 2.0), "2": (7.0, 5.0), "3": (16.0, 2.0), "4": (16.0, 9.0), "5": (7.0, 9.0)}
#: A wrong reading: hole 2 one row too high (a quote attributed to the wrong stop).
SHIFTED = dict(TRUE, **{"2": (7.0, 2.0)})
#: The right positions with two pad numbers exchanged: the holes cannot tell.
SWAPPED = dict(TRUE, **{"1": TRUE["3"], "3": TRUE["1"]})


class FakeHoles:
    """Confirms a layout whose positions, up to a translation AND a uniform scale, are the drawn ones.

    Scale-free like the real check (drawing_holes fits the px/mm scale), so
    the tests cannot pass on a strictness the real check does not have.
    """

    def __init__(self, drawn, status_when_not="not_confirmed", reason="no_match", also=()):
        self.drawn, self.status_when_not, self.reason, self.calls = drawn, status_when_not, reason, 0
        #: Further layouts drawn on the same page (another view).
        self.also = list(also)

    def __call__(self, pads, body):
        self.calls += 1

        def shape(points):
            points = list(points)
            x0 = min(x for x, _y in points)
            y0 = min(y for _x, y in points)
            size = max(max(x - x0, y - y0) for x, y in points) or 1.0
            return sorted((round((x - x0) / size, 3), round((y - y0) / size, 3)) for x, y in points)

        for drawn in [self.drawn] + self.also:
            if len(pads) == len(drawn) and shape(pads.values()) == shape(drawn.values()):
                return _Verdict("confirmed", "confirmed")
        return _Verdict(self.status_when_not, self.reason)


class _Verdict:
    """The fields of drawing_holes.LayoutCheck the extractor reads."""

    def __init__(self, status, reason):
        self.status, self.reason, self.image = status, reason, "drawing.png"
        self.max_error_mm, self.tolerance_mm, self.circles = 0.05, 0.2, 5


def _main(pads, view="bottom", length=20.0, width=12.0):
    return {"drawing_unit": "mm", "contacts_view": view, "package_geometry": {
        "origin": "custom", "body_length_mm": length, "body_width_mm": width,
        "pins": [{"number": n, "x_mm": x, "y_mm": y} for n, (x, y) in pads.items()]}}


def _reading(chains=CHAINS, view="bottom"):
    return dict(pads_from_chains(chains), view=view, notes="")


def _offsets(data):
    pins = {p["number"]: (p["x_mm"], p["y_mm"]) for p in data["package_geometry"]["pins"]}
    ox, oy = pins["1"]
    return {n: (round(x - ox, 3), round(y - oy, 3)) for n, (x, y) in pins.items()}


def _true_offsets():
    ox, oy = TRUE["1"]
    return {n: (round(x - ox, 3), round(y - oy, 3)) for n, (x, y) in TRUE.items()}


# ── No second reading ────────────────────────────────────────────────────

@pytest.mark.parametrize("reading", [
    None, {"view": "unknown", "pads": {}, "notes": "", "failed": "HTTP 500"}])
def test_an_unconfirmed_main_answer_blocks(reading):
    """Negative: no reading and no hole check -> blocked, with the remedy."""
    data = _main(TRUE)
    apply_dedicated_pads(data, reading)
    assert "NON confermate" in data["_pin_map_conflict"]
    assert "Rigenera footprint" in data["_pin_map_conflict"]


def test_the_drawn_holes_alone_confirm_the_main_answer():
    """Positive: same failed reading, the holes match the main layout -> it stands."""
    data = _main(TRUE)
    apply_dedicated_pads(data, None, check_layout=FakeHoles(TRUE))
    assert "_pin_map_conflict" not in data
    assert "non i numeri" in data["_numbering_note"]
    assert _offsets(data) == _true_offsets()


def test_holes_that_do_not_match_keep_the_block():
    data = _main(SHIFTED)
    apply_dedicated_pads(data, None, check_layout=FakeHoles(TRUE))
    assert "non coincidono con queste posizioni" in data["_pin_map_conflict"]


def test_a_photo_cannot_confirm_anything():
    """An unverifiable check (no line drawing) is never a confirmation."""
    data = _main(TRUE)
    apply_dedicated_pads(data, None, check_layout=FakeHoles({}, "unverifiable", "not_a_drawing"))
    assert "disegno a linee" in data["_pin_map_conflict"]


def test_a_check_that_cannot_run_blocks_and_does_not_raise():
    def broken(pads, body):
        raise OSError("disk")

    data = _main(TRUE)
    apply_dedicated_pads(data, None, check_layout=broken)
    assert "non riuscito (OSError)" in data["_pin_map_conflict"]


# ── Two readings that disagree ───────────────────────────────────────────

def test_the_reading_the_holes_confirm_is_written():
    """Positive: main wrong, dedicated right, only the dedicated matches the holes."""
    data = _main(SHIFTED)
    apply_dedicated_pads(data, _reading(), check_layout=FakeHoles(TRUE))
    assert "_pin_map_conflict" not in data
    assert "lettura dedicata" in data["_numbering_note"]
    assert _offsets(data) == _true_offsets()
    # Closed on the body on both axes: written with the body centre as origin.
    geometry = data["package_geometry"]
    assert geometry["origin"] == "body_center"
    assert geometry["pins"][0]["x_mm"] == pytest.approx(TRUE["1"][0] - 10.0)


def test_readings_differing_only_by_numbers_are_never_picked_by_the_holes():
    """Negative: both layouts match the holes (two numbers exchanged) -> blocked."""
    data = _main(SWAPPED)
    apply_dedicated_pads(data, _reading(), check_layout=FakeHoles(TRUE))
    assert "non coincidono" in data["_pin_map_conflict"]
    assert _offsets(data) != _true_offsets()


def test_neither_reading_matching_the_holes_keeps_the_block():
    wrong_drawing = dict(TRUE, **{"4": (18.0, 9.0)})
    data = _main(SHIFTED)
    apply_dedicated_pads(data, _reading(), check_layout=FakeHoles(wrong_drawing))
    assert "Nessuna delle due" in data["_pin_map_conflict"]


def test_the_adopted_reading_brings_its_view_to_the_pads():
    """The view the footprint mirrors by is package_geometry.source_view, not contacts_view."""
    data = _main(SHIFTED, view="unknown")
    data["package_geometry"]["source_view"] = "top"     # what single_answer_to_data writes
    apply_dedicated_pads(data, _reading(view="bottom"), check_layout=FakeHoles(TRUE))
    assert data["package_geometry"]["source_view"] == "bottom"
    assert "(bottom)" in data["_numbering_note"]


def test_two_agreeing_readings_take_the_only_declared_view():
    data = _main(TRUE, view="unknown")
    data["package_geometry"]["source_view"] = "top"
    apply_dedicated_pads(data, _reading(view="bottom"))
    assert "_pin_map_conflict" not in data
    assert data["package_geometry"]["source_view"] == "bottom"


@pytest.mark.parametrize("main", [TRUE, SHIFTED])
def test_different_declared_views_are_never_settled_by_the_holes(main):
    """The holes say where, not from which side: even when they match the main layout."""
    data = _main(main, view="top")
    apply_dedicated_pads(data, _reading(view="bottom"), check_layout=FakeHoles(TRUE))
    assert "viste diverse" in data["_pin_map_conflict"]


def test_a_reading_of_the_same_shape_at_another_scale_blocks():
    """Wrong unit in the main answer (pads AND body scaled): the holes cannot see the scale."""
    scaled = {n: (x * 0.9, y * 0.9) for n, (x, y) in TRUE.items()}
    data = _main(scaled, length=18.0, width=10.8)
    apply_dedicated_pads(data, _reading(), check_layout=FakeHoles(TRUE))
    assert "in scala 1.111" in data["_pin_map_conflict"]


def test_contradicting_chains_of_another_shape_do_not_stop_the_holes():
    """Positive twin: the discarded reading differs in shape, not in scale -> the main stands."""
    bad = [dict(CHAINS[0], values=[2.0, 5.0, 9.0, 6.0]), CHAINS[1]]
    data = _main(TRUE)
    apply_dedicated_pads(data, _reading(bad), check_layout=FakeHoles(TRUE))
    assert "_pin_map_conflict" not in data


def test_pads_sharing_a_number_are_never_settled_by_the_holes():
    pads = dict(SHIFTED)
    data = _main(pads)
    data["package_geometry"]["pins"].append({"number": "1", "x_mm": 1.0, "y_mm": 1.0})
    apply_dedicated_pads(data, _reading(), check_layout=FakeHoles(TRUE))
    assert "_pin_map_conflict" in data


def test_a_crashing_hole_check_never_stops_the_analysis():
    def broken(pads, body):
        raise RuntimeError("cv2.error")

    data = _main(TRUE)
    apply_dedicated_pads(data, None, check_layout=broken)
    assert "RuntimeError" in data["_pin_map_conflict"]


def test_the_remedy_follows_the_pad_type():
    """SMD pads have no manual editor: the remedy points to a better input instead."""
    data = _main(TRUE)
    for pin in data["package_geometry"]["pins"]:
        pin["pad_type"] = "smd"
    apply_dedicated_pads(data, None)
    assert "Piazzole del componente THT" not in data["_pin_map_conflict"]
    assert "come immagine" in data["_pin_map_conflict"]
    data = _main(TRUE)
    apply_dedicated_pads(data, None)
    assert "Piazzole del componente THT" in data["_pin_map_conflict"]


# ── Agreement, partial readings, contradictions ──────────────────────────

def test_two_agreeing_readings_stand_with_a_warning_when_the_holes_disagree():
    data = _main(TRUE)
    apply_dedicated_pads(data, _reading(), check_layout=FakeHoles(SHIFTED))
    assert "_pin_map_conflict" not in data and "Attenzione" in data["_numbering_note"]


def test_a_partial_reading_blocks_unless_the_holes_confirm_the_main_answer():
    partial = [CHAINS[0], {"axis": "y", "stops": ["edge", "1,3", "2", "edge"],
                           "values": [2.0, 3.0, 7.0]}]
    data = _main(TRUE)
    apply_dedicated_pads(data, _reading(partial))
    assert "solo 3 pad su 5" in data["_pin_map_conflict"]
    data = _main(TRUE)
    apply_dedicated_pads(data, _reading(partial), check_layout=FakeHoles(TRUE))
    assert "_pin_map_conflict" not in data


def test_contradicting_chains_leave_the_main_answer_to_the_holes():
    """The chains do not close on the body: the dedicated reading is out either way."""
    bad = [dict(CHAINS[0], values=[2.0, 5.0, 9.0, 6.0]), CHAINS[1]]
    data = _main(TRUE)
    apply_dedicated_pads(data, _reading(bad))
    assert "non tornano" in data["_pin_map_conflict"]
    data = _main(TRUE)
    apply_dedicated_pads(data, _reading(bad), check_layout=FakeHoles(TRUE))
    assert "_pin_map_conflict" not in data


def test_the_hole_check_runs_once_per_layout():
    """Main, dedicated and the dedicated layout's mirror: three distinct layouts, three checks."""
    holes = FakeHoles(TRUE)
    data = _main(SHIFTED)
    apply_dedicated_pads(data, _reading(), check_layout=holes)
    assert holes.calls == 3


def test_a_competitor_failing_only_as_distorted_never_lets_another_reading_win():
    """A drawing not to scale: the right reading may fail as «distorted»; no choice then."""
    data = _main(SHIFTED)
    apply_dedicated_pads(data, _reading(), check_layout=FakeHoles(TRUE, reason="distorted"))
    assert "_pin_map_conflict" in data


def test_a_page_where_the_mirror_also_fits_cannot_choose():
    """Top and bottom views on one page: the mirrored layout fits too, the side is unknown."""
    mirrored = {n: (-x, y) for n, (x, y) in TRUE.items()}
    data = _main(SHIFTED)
    apply_dedicated_pads(data, _reading(), check_layout=FakeHoles(TRUE, also=[mirrored]))
    assert "_pin_map_conflict" in data
    data = _main(SHIFTED)
    apply_dedicated_pads(data, _reading(), check_layout=FakeHoles(TRUE))
    assert "_pin_map_conflict" not in data, "positive twin: one view only"


# ── The main answer's own chains ─────────────────────────────────────────

def test_the_holes_settle_the_main_answer_against_its_own_chains():
    """Coordinates and own chains disagree, no dedicated reading: the holes pick the chains."""
    data = _main(SHIFTED)
    data["_chain_candidate"] = {"pads": pads_from_chains(CHAINS)["pads"],
                                "spans": {"x": 20.0, "y": 12.0}, "centred": True}
    data["_pin_map_conflict"] = "own conflict."
    apply_dedicated_pads(data, None, check_layout=FakeHoles(TRUE))
    assert "_pin_map_conflict" not in data and "_chain_candidate" not in data
    assert "catene di quote" in data["_numbering_note"]
    assert _offsets(data) == _true_offsets()
    # Negative: the holes match neither -> the own conflict stays, extended.
    data = _main(SHIFTED)
    data["_chain_candidate"] = {"pads": dict(SHIFTED), "spans": {"x": 20.0, "y": 12.0},
                                "centred": True}
    data["_pin_map_conflict"] = "own conflict."
    apply_dedicated_pads(data, None, check_layout=FakeHoles(TRUE))
    assert data["_pin_map_conflict"].startswith("own conflict.")
