"""Side/pinout coherence of a side-based module (docs/pinout-coherence-plan.md, item A).

Every rule is checked on TWO naming conventions -- a devkit silkscreen with
bare GPIO numbers against «GPIOn (...)» pinout names, and a board with
«D13»/«A0» labels against «D13 (SCK)» names -- so no rule is tied to one
board, and each positive case has a negative one that differs structurally.
"""
import pytest

from kicadgen.services import pinout_coherence as pc


# --- label_relation ---------------------------------------------------------------------------

@pytest.mark.parametrize("label,name", [
    ("G", "GND"), ("GND", "AGND"), ("VSS", "0V"),          # ground spellings
    ("5V", "5V0"), ("3V3", "3.3V"), ("+5V", "5V"),          # supplies compared as voltages
    ("12", "12"),                                           # two bare numbers
    ("D13", "D13 (SCK)"), ("A0", "A0 (ADC0)"),              # second convention, same prefix
    ("TX", "TX (GPIO43, U0TXD)"), ("aref", "AREF"),        # equal primary names
])
def test_labels_that_name_the_same_signal_are_same(label, name):
    assert pc.label_relation(label, name) == pc.SAME


@pytest.mark.parametrize("label,name", [
    ("3V3", "5V"), ("3", "4"), ("D12", "D11 (MOSI)"),       # same class, other value
    ("G", "3V3"), ("G", "GPIO0"), ("D2", "GND"),            # different named classes
])
def test_labels_that_name_another_signal_are_different(label, name):
    assert pc.label_relation(label, name) == pc.DIFFERENT


@pytest.mark.parametrize("label,name", [
    ("IO4", "GPIO4"), ("A0", "D0"),          # two different prefixes cannot be told
    ("RST", "EN"), ("20", "USB_D+ (GPIO20)"),  # free text on either side
    ("", "GND"), ("GND", None),
    # CONTRACT CHANGED 2026-11-13 (generality audit H3): a bare number may be a
    # signal index or the contact's physical position, so it is compared with
    # another bare number only. Before, «12»/«GPIO12» was SAME and «1»/«GND»,
    # «2»/«PD1 (SDA)» DIFFERENT, which broke a board printing positions.
    ("12", "GPIO12 (ADC2_1)"), ("12", "GPIO13"), ("2", "PD1 (SDA)"),
    ("1", "GND"), ("14", "5V0"), ("0", "A0"),
])
def test_what_the_rule_cannot_tell_is_unknown(label, name):
    assert pc.label_relation(label, name) == pc.UNKNOWN


# --- two conventions, one wrong numbering each ------------------------------------------------

#: Devkit convention: pinout 1..12 around the board, labels met along each
#: edge with the numbers decreasing (reverse), bare GPIO numbers on the silk.
DEVKIT_PINOUT = [{"pin": n, "name": name} for n, name in enumerate(
    ["3V3", "RST", "GPIO4 (ADC1_3)", "GPIO5", "GPIO6", "GND",
     "GND", "TX (GPIO43)", "RX (GPIO44)", "GPIO1", "GPIO2", "5V0"], start=1)]
DEVKIT_TOP = ["G", "6", "5", "4", "RST", "3V3"]        # pins 6..1
DEVKIT_BOTTOM = ["5V", "2", "1", "RX", "TX", "G"]      # pins 12..7

#: Second convention: «D13»/«A0» labels, numbers increasing along the edge.
NANO_PINOUT = [{"pin": str(n), "name": name} for n, name in enumerate(
    ["D13 (SCK)", "3V3", "AREF", "A0 (ADC0)", "A1 (ADC1)", "A2",
     "D12 (MISO)", "D11 (MOSI)", "D10", "D9", "GND", "5V"], start=1)]
NANO_TOP = ["D13", "3.3V", "AREF", "A0", "A1", "A2"]   # pins 1..6
NANO_BOTTOM = ["D12", "D11", "D10", "D9", "GND", "+5V"]  # pins 7..12

CONVENTIONS = {
    "devkit": (DEVKIT_PINOUT, DEVKIT_TOP, DEVKIT_BOTTOM, {"top": (1, True), "bottom": (7, True)}),
    "nano": (NANO_PINOUT, NANO_TOP, NANO_BOTTOM, {"top": (1, False), "bottom": (7, False)}),
}

#: Whether the renumbering is RECOMMENDED for each convention. The bare GPIO
#: numbers of the devkit carry no evidence (audit H3): its placement rests on
#: RST/TX/RX and grounds, and a ground-only window competes on each side
#: («contested»), so it is offered unrecommended. The prefixed labels of the
#: second convention single out one window per side.
RECOMMENDED = {"devkit": False, "nano": True}


def _sides(top, bottom, top_first, bottom_first, top_reverse=False, bottom_reverse=False):
    return [{"edge": "top", "count": len(top), "first_pin": top_first, "reverse": top_reverse,
             "pin_names": list(top)},
            {"edge": "bottom", "count": len(bottom), "first_pin": bottom_first,
             "reverse": bottom_reverse, "pin_names": list(bottom)}]


@pytest.mark.parametrize("convention", sorted(CONVENTIONS))
def test_a_shifted_numbering_is_contradicted_and_the_unique_alignment_recommended(convention):
    pinout, top, bottom, expected = CONVENTIONS[convention]
    # The top side numbered after the pinout's end: rule 1 breaks.
    report = pc.side_pinout_report(_sides(top, bottom, 12, 1), pinout)
    assert report["contradicted"]
    assert report["dropped"] and report["unnamed_pads"]
    assert {edge: (p["first_pin"], p["reverse"]) for edge, p in report["alignment"].items()} == expected
    assert report["renumberable"] and not report["already_aligned"]
    assert report["recommended"] is RECOMMENDED[convention]
    assert bool(report["contested_sides"]) is not RECOMMENDED[convention]
    renumbered = pc.renumbered_sides(_sides(top, bottom, 12, 1), report["alignment"])
    fixed = pc.side_pinout_report(renumbered, pinout)
    assert not fixed["contradicted"] and not fixed["contradictions"] and fixed["already_aligned"]


@pytest.mark.parametrize("convention", sorted(CONVENTIONS))
def test_swapped_sides_break_no_count_but_their_labels_differ(convention):
    """Negative of rule 1: numbers cover the pinout, only the name rule sees the error."""
    pinout, top, bottom, expected = CONVENTIONS[convention]
    (top_first, top_rev), (bottom_first, bottom_rev) = expected["top"], expected["bottom"]
    swapped = _sides(top, bottom, bottom_first, top_first, top_rev, bottom_rev)
    report = pc.side_pinout_report(swapped, pinout)
    assert not report["contradicted"]
    assert report["contradictions"]
    assert report["renumberable"]
    assert report["recommended"] is RECOMMENDED[convention]


@pytest.mark.parametrize("convention", sorted(CONVENTIONS))
def test_the_right_numbering_reports_nothing(convention):
    pinout, top, bottom, expected = CONVENTIONS[convention]
    (top_first, top_rev), (bottom_first, bottom_rev) = expected["top"], expected["bottom"]
    report = pc.side_pinout_report(_sides(top, bottom, top_first, bottom_first, top_rev, bottom_rev),
                                   pinout)
    assert not report["contradicted"] and not report["contradictions"]
    assert report["already_aligned"]
    assert pc.document_findings(report) == []


def test_symmetric_labels_are_ambiguous_and_never_recommended():
    """Two windows fit equally (a ground at both ends, the other label free text)."""
    pinout = [{"pin": 1, "name": "GND"}, {"pin": 2, "name": "X"}, {"pin": 3, "name": "Y"},
              {"pin": 4, "name": "GND"}]
    sides = [{"edge": "left", "count": 2, "first_pin": 5, "pin_names": ["G", "Q"]},
             {"edge": "right", "count": 2, "first_pin": 7, "pin_names": ["Z", "W"]}]
    report = pc.side_pinout_report(sides, pinout)
    assert report["contradicted"]
    assert "left" in report["ambiguous"] and "right" in report["unplaced"]
    assert not report["renumberable"] and not report["recommended"]


def test_one_agreeing_label_among_unknown_ones_leaves_two_placements():
    """Fragility made visible: «D11» against «A2» is UNKNOWN (two prefixes), so
    with a free-text last label («VIN») a second window agrees on «D12» alone.
    The rule then refuses to recommend instead of picking one."""
    pinout = [dict(entry) for entry in NANO_PINOUT]
    pinout[-1]["name"] = "VIN"
    bottom = NANO_BOTTOM[:-1] + ["VIN"]
    report = pc.side_pinout_report(_sides(NANO_TOP, bottom, 1, 7), pinout)
    assert report["ambiguous"] == ["bottom"]
    assert not report["renumberable"] and not report["recommended"]


def test_a_pinout_with_entries_beyond_the_sides_is_renumberable_but_not_recommended():
    """Coverage: an inner-header pin the sides do not carry stays without a pad."""
    pinout = NANO_PINOUT + [{"pin": "13", "name": "D8"}]
    report = pc.side_pinout_report(_sides(NANO_TOP, NANO_BOTTOM, 20, 1), pinout)
    assert report["contradicted"]
    assert report["renumberable"] and not report["covers_pinout"] and not report["recommended"]


def test_overlapping_placements_are_not_renumberable():
    pinout = [{"pin": 1, "name": "GND"}, {"pin": 2, "name": "GPIO2"}]
    sides = [{"edge": "left", "count": 2, "first_pin": 1, "pin_names": ["G", "2"]},
             {"edge": "right", "count": 2, "first_pin": 3, "pin_names": ["GND", "2"]}]
    report = pc.side_pinout_report(sides, pinout)
    assert len(report["alignment"]) == 2 and not report["renumberable"]


def test_no_pinout_or_no_side_is_not_applicable():
    assert not pc.side_pinout_report([], DEVKIT_PINOUT)["applicable"]
    assert not pc.side_pinout_report(_sides(DEVKIT_TOP, DEVKIT_BOTTOM, 1, 7), None)["applicable"]


# --- helpers -----------------------------------------------------------------------------------

def test_side_pad_labels_pairs_labels_with_the_generator_numbering():
    side = {"edge": "top", "count": 3, "first_pin": 4, "reverse": True, "pin_names": ["a", "b", "c"]}
    assert pc.side_pad_labels(side) == [{"pin": 6, "name": "a"}, {"pin": 5, "name": "b"},
                                        {"pin": 4, "name": "c"}]
    assert pc.side_pad_labels({"count": 2, "pin_names": ["x", "y"]}) == [
        {"pin": 1, "name": "x"}, {"pin": 2, "name": "y"}]
    # Audit L3: a list of another length is refused, never truncated.
    assert pc.side_pad_labels({"count": 3, "pin_names": ["x", "y"]}) == []
    assert pc.side_pad_labels({"count": 1, "pin_names": ["x", "y"]}) == []


def test_the_digest_ignores_the_view_and_follows_the_reading():
    sides = _sides(DEVKIT_TOP, DEVKIT_BOTTOM, 1, 7, True, True)
    # The same contacts read on a bottom view: labels reversed, direction flipped.
    mirrored = _sides(DEVKIT_TOP[::-1], DEVKIT_BOTTOM[::-1], 1, 7, False, False)
    assert pc.report_digest(sides, DEVKIT_PINOUT) == pc.report_digest(mirrored, DEVKIT_PINOUT)
    assert pc.report_digest(sides, DEVKIT_PINOUT) != pc.report_digest(sides, DEVKIT_PINOUT[:-1])
    # Audit L2: the same numbers with another name are another situation.
    renamed = [dict(entry) for entry in DEVKIT_PINOUT]
    renamed[2]["name"] = "GPIO40"
    assert pc.report_digest(sides, DEVKIT_PINOUT) != pc.report_digest(sides, renamed)
    assert pc.report_digest(sides, DEVKIT_PINOUT) != pc.report_digest(
        _sides(DEVKIT_TOP, DEVKIT_BOTTOM, 7, 1, True, True), DEVKIT_PINOUT)


def test_pin_ranges_are_compressed_in_natural_order():
    assert pc.pin_ranges_text(["25", 23, "24", "45", "A1", "7"]) == "7, 23-25, 45, A1"
    assert pc.pin_ranges_text([]) == ""


def test_kept_sides_pinout_uses_labels_and_document_names_only_for_unlabelled_sides():
    sides = [{"edge": "top", "count": 2, "first_pin": 1, "pin_names": ["G", "5V"]},
             {"edge": "bottom", "count": 2, "first_pin": 3}]
    pinout = [{"pin": 1, "name": "3V3"}, {"pin": 4, "name": "GPIO4"}, {"pin": 9, "name": "X"}]
    assert pc.kept_sides_pinout(sides, pinout) == [
        {"pin": "1", "name": "G"}, {"pin": "2", "name": "5V"}, {"pin": "4", "name": "GPIO4"}]


def test_document_findings_are_typed():
    report = pc.side_pinout_report(_sides(DEVKIT_TOP, DEVKIT_BOTTOM, 12, 1), DEVKIT_PINOUT)
    kinds = [finding["kind"] for finding in pc.document_findings(report, kept_sides=True)]
    assert kinds[:2] == [pc.FINDING_PINOUT_WITHOUT_PAD, pc.FINDING_PAD_WITHOUT_PINOUT]
    # Round 5: the shifted sides' labels also place the names elsewhere, strongly.
    assert set(kinds[2:]) == {pc.FINDING_LABEL_PLACEMENT, pc.FINDING_LABEL_DIFFERS}
    assert all(finding["kept_sides"] for finding in pc.document_findings(report, kept_sides=True))


# --- generality audit 2026-11-13 ---------------------------------------------------------------

#: A side-based module with a numbered centre pad: four edge contacts 1..4,
#: pinout entry 5 the exposed pad under the module (no side carries it).
EP_PINOUT = [{"pin": n, "name": name} for n, name in
             enumerate(["GND", "D1 (SDA)", "D2 (SCL)", "3V3", "GND (EP)"], start=1)]
#: The same pinout with the centre pad DECLARED off the sides by the answer.
EP_PINOUT_DECLARED = [dict(entry, off_side=True) if entry["pin"] == 5 else dict(entry)
                      for entry in EP_PINOUT]
EP_LABELS = ["G", "D1", "D2", "3V3"]


def test_an_entry_declared_off_every_side_is_reported_not_asked():
    """H2 positive: the sides are right, the answer declares the centre pad off the sides.

    CONTRACT CHANGED 2026-11-13 (round 3, item 2): an entry counts as off the
    sides only when DECLARED (`off_side`); before, any dropped entry of an
    unshifted numbering was classified so silently.
    """
    report = pc.side_pinout_report(
        [{"edge": "left", "count": 4, "first_pin": 1, "pin_names": EP_LABELS}], EP_PINOUT_DECLARED)
    assert report["dropped"] == ["5"] and not report["unnamed_pads"]
    assert not report["contradicted"] and not report["needs_question"]
    assert report["off_side_entries"] == ["5"]
    assert report["already_aligned"]
    kinds = [finding["kind"] for finding in pc.document_findings(report)]
    assert kinds == [pc.FINDING_OFF_SIDE_ENTRY]


def test_an_undeclared_entry_without_a_pad_is_a_doubt_not_a_silent_off_side():
    """Item 2 negative: the same pinout without the declaration -> unexplained, asked."""
    report = pc.side_pinout_report(
        [{"edge": "left", "count": 4, "first_pin": 1, "pin_names": EP_LABELS}], EP_PINOUT)
    assert not report["contradicted"] and not report["off_side_entries"]
    assert report["unexplained_entries"] == ["5"] and report["needs_question"]
    kinds = [finding["kind"] for finding in pc.document_findings(report)]
    assert kinds == [pc.FINDING_UNEXPLAINED_ENTRY]


def test_a_shift_hidden_behind_an_exposed_pad_is_seen():
    """Probe4: six side contacts, entry 7 the exposed pad, the model numbered the sides 2..7."""
    pinout = [{"pin": str(n), "name": name} for n, name in
              enumerate(["GND", "3V3", "EN", "IO4", "IO5", "IO6", "GND"], start=1)]
    sides = [{"edge": "left", "count": 3, "first_pin": 2, "pin_names": ["1", "2", "3"]},
             {"edge": "right", "count": 3, "first_pin": 5, "reverse": True,
              "pin_names": ["4", "5", "6"]}]
    report = pc.side_pinout_report(sides, pinout)
    assert report["unexplained_entries"] == ["1"] and report["needs_question"]
    assert pc.uniform_shifts(sides, pinout) == [-1]
    right = [dict(side, first_pin=side["first_pin"] - 1) for side in sides]
    assert pc.side_pinout_report(right, pinout)["unexplained_entries"] == ["7"]


def test_the_same_module_shifted_is_contradicted_and_keeps_its_centre_pad_reported():
    """H2 negative: pads 6..9 the pinout lacks AND entries 1..4 without pad -> shifted."""
    report = pc.side_pinout_report(
        [{"edge": "left", "count": 4, "first_pin": 6, "pin_names": EP_LABELS}], EP_PINOUT)
    assert report["contradicted"] and not report["off_side_entries"]
    assert report["alignment"]["left"]["first_pin"] == 1
    assert not report["covers_pinout"], "the centre pad stays off the sides"


def test_keeping_the_sides_never_removes_an_entry_off_the_sides():
    sides = [{"edge": "left", "count": 4, "first_pin": 1, "pin_names": EP_LABELS}]
    rows = pc.kept_sides_pinout(sides, EP_PINOUT_DECLARED)
    assert {"pin": "5", "name": "GND (EP)", "off_side": True} in rows
    # Undeclared, it is not kept as an off-side entry: nothing says it is one.
    assert all(row["pin"] != "5" for row in pc.kept_sides_pinout(sides, EP_PINOUT))


def test_physical_position_numbers_on_the_silkscreen_contradict_nothing():
    """H3: labels 1..4 are positions, the pinout names GP0, GP1, GND, GP2."""
    pinout = [{"pin": 1, "name": "GP0"}, {"pin": 2, "name": "GP1"}, {"pin": 3, "name": "GND"},
              {"pin": 4, "name": "GP2"}]
    report = pc.side_pinout_report(
        [{"edge": "left", "count": 4, "first_pin": 1, "pin_names": ["1", "2", "3", "4"]}], pinout)
    assert not report["contradicted"] and not report["contradictions"]


def test_a_one_contact_side_has_one_placement_not_two():
    """M2: forward and reverse of one contact are the same numbering."""
    pinout = [{"pin": 1, "name": "ANT"}, {"pin": 2, "name": "GND"}]
    side = {"edge": "top", "count": 1, "first_pin": 9, "pin_names": ["ANT"]}
    assert len(pc.side_placements(side, pc.document_pinout_entries(pinout))) == 1


def test_a_placement_held_by_repeated_names_only_is_never_recommended():
    """Item 5 positive/negative: one window, agreeing only on names the pinout repeats or not.

    CONTRACT CHANGED 2026-11-13 (round 3, item 5): weakness is the REPETITION
    of the name in the pinout, not its class; a ground is weak because a
    board has several.
    """
    pinout = [{"pin": n, "name": name} for n, name in
              enumerate(["GND", "D1", "3V3", "D2", "D3", "GND", "3V3"], start=1)]
    weak = pc.side_pinout_report(
        [{"edge": "left", "count": 3, "first_pin": 8, "pin_names": ["G", "X", "3V3"]}], pinout)
    strong = pc.side_pinout_report(
        [{"edge": "left", "count": 3, "first_pin": 8, "pin_names": ["G", "D1", "3V3"]}], pinout)
    assert weak["weak_sides"] == ["left"] and not weak["recommended"]
    assert weak["alignment"]["left"]["unique"] == 0 and weak["alignment"]["left"]["repeated"] == 2
    assert not strong["weak_sides"] and strong["alignment"]["left"]["unique"] == 1


def test_a_ground_that_occurs_once_is_strong_support():
    """Item 5: the class does not decide, the repetition does."""
    pinout = [{"pin": 1, "name": "GND"}, {"pin": 2, "name": "D1"}, {"pin": 3, "name": "D2"}]
    report = pc.side_pinout_report(
        [{"edge": "left", "count": 3, "first_pin": 4, "pin_names": ["G", "X", "Y"]}], pinout)
    assert report["alignment"]["left"]["unique"] == 1 and not report["weak_sides"]



# --- round 3, item 1: ground and supply spellings are never an index ----------------------------

@pytest.mark.parametrize("label,name", [
    ("GND", "GND2"), ("G", "GND_1"), ("GND", "VSS1"), ("GND", "PGND1"), ("AGND", "GND"),
    ("3V3", "V3V3"),
])
def test_enumerated_grounds_and_supply_spellings_agree(label, name):
    assert pc.label_relation(label, name) == pc.SAME


@pytest.mark.parametrize("label,name", [
    ("3V3", "VDD33"), ("5V", "VCC5"), ("3V3", "VDD3P3"), ("VIN", "5V"), ("3V", "3V3"),
])
def test_supply_rails_without_a_readable_voltage_are_unknown(label, name):
    assert pc.label_relation(label, name) == pc.UNKNOWN


@pytest.mark.parametrize("name", ["GND2", "PGND1", "VSS1", "VDD33", "VCC5"])
def test_a_ground_or_supply_with_a_suffix_is_never_an_index(name):
    assert pc.agreement_kind(name) in (pc.KIND_GROUND, pc.KIND_SUPPLY)


def test_g_followed_by_digits_stays_an_index():
    """Negative: some boards print GPIOs as G0, G1 -- not a ground."""
    assert pc.agreement_kind("G0") == pc.KIND_INDEX
    assert pc.label_relation("G0", "GPIO0") == pc.UNKNOWN


# --- round 4: the fixed convention (user decision 2026-11-13) -----------------------------------

def _side(edge, count, first=1, reverse=False, names=None):
    side = {"edge": edge, "count": count, "first_pin": first, "reverse": reverse}
    if names is not None:
        side["pin_names"] = list(names)
    return side


@pytest.mark.parametrize("sides,expected", [
    # one side: down the left side
    ([_side("left", 3)], {"left": [1, 2, 3]}),
    # top side only: the first contact met walking counter-clockwise from the
    # top-left corner is the RIGHT end of the top side
    ([_side("top", 3)], {"top": [3, 2, 1]}),
    # right side only: the walk goes up the right side, pin 1 at its BOTTOM end
    ([_side("right", 3)], {"right": [3, 2, 1]}),
    # two vertical rows: down the left, up the right (DIP)
    ([_side("left", 3), _side("right", 3)], {"left": [1, 2, 3], "right": [6, 5, 4]}),
    # two horizontal rows: pin 1 is bottom-left, then the top row right to left
    ([_side("top", 3), _side("bottom", 3)], {"bottom": [1, 2, 3], "top": [6, 5, 4]}),
    # three sides
    ([_side("left", 3), _side("bottom", 2), _side("right", 3)],
     {"left": [1, 2, 3], "bottom": [4, 5], "right": [8, 7, 6]}),
    # four sides
    ([_side(edge, 2) for edge in ("top", "right", "bottom", "left")],
     {"left": [1, 2], "bottom": [3, 4], "right": [6, 5], "top": [8, 7]}),
])
def test_the_convention_walks_counter_clockwise_from_the_top_left_corner(sides, expected):
    assert pc.convention_numbers(sides) == expected
    numbered = pc.sides_with_numbers(sides, expected)
    assert sorted(n for side in numbered for n in pc.side_pad_numbers(side)) == list(
        range(1, sum(side["count"] for side in sides) + 1))


def test_names_stay_on_the_contacts_the_analysis_gave_them():
    """The analysis' own mapping (its numbers), only renumbered: never by labels."""
    sides = [_side("top", 2, 1, names=["GND", "TX"]), _side("bottom", 2, 3, names=["RX", "3V3"])]
    pinout = [{"pin": n, "name": name, "function": "other"} for n, name in
              enumerate(["GND", "TX", "RX", "3V3"], start=1)]
    numbers = pc.convention_numbers(sides)
    rows, unattached, _clashes = pc.convention_pinout(sides, pinout, numbers)
    # bottom row 1..2 left to right, top row 4..3 left to right
    assert {row["pin"]: row["name"] for row in rows} == {"1": "RX", "2": "3V3", "4": "GND", "3": "TX"}
    assert all(row.get("function") == "other" for row in rows) and not unattached
    assert pc.sides_with_numbers(sides, {"top": [1, 3], "bottom": [2, 4]}) is None


def test_one_agreeing_label_never_moves_a_name():
    """P5: the model numbered contacts and pinout 1..6; one label on the far contact of the
    right side agrees with ANOTHER entry. The name stays where the analysis put it."""
    pinout = [{"pin": str(n), "name": name} for n, name in
              enumerate(["RST", "EN", "IO3", "IO4", "IO5", "TX"], start=1)]
    sides = [_side("left", 3, 1, names=["", "", ""]), _side("right", 3, 4, True, names=["", "", "IO5"])]
    rows, _, _ = pc.convention_pinout(sides, pinout, pc.convention_numbers(sides))
    by_contact = {}
    for side in sides:
        for index, number in enumerate(pc.side_pad_numbers(side)):
            by_contact[(side["edge"], index)] = number
    names = {row["pin"]: row["name"] for row in rows}
    # The right side's last listed contact carries pad 4 for the analysis -> IO4, not IO5.
    new = pc.convention_numbers(sides)["right"][2]
    assert by_contact[("right", 2)] == 4 and names[str(new)] == "IO4"


def test_entries_without_a_contact_are_kept_after_the_contacts():
    """M2: never dropped silently; a declared one keeps its flag, an undeclared one is named."""
    pinout = [{"pin": 1, "name": "A"}, {"pin": 2, "name": "B"},
              {"pin": 9, "name": "GND", "off_side": True}, {"pin": 7, "name": "X"}]
    rows, unattached, _clashes = pc.convention_pinout([_side("left", 2, 1, names=["a", "b"])],
                                                      pinout, {"left": [1, 2]})
    assert [(row["pin"], row["name"], row.get("off_side")) for row in rows] == [
        ("1", "A", None), ("2", "B", None), ("3", "GND", True), ("4", "X", None)]
    assert unattached == ["7"]


# --- round 4: audit fixes ------------------------------------------------------------------------

def test_an_omitted_pin_and_an_exposed_pad_are_no_shift():
    """H1 negative: pinout omits pad 3 and has an undeclared 9 -> no shift evidence, no retry;
    the entry is unexplained and the question offers it off the sides."""
    names = {1: "GND", 2: "3V3", 4: "IO4", 5: "IO5", 6: "IO6", 7: "IO7", 8: "IO8", 9: "GND"}
    pinout = [{"pin": str(k), "name": v} for k, v in names.items()]
    sides = [_side("left", 4, 1, names=["GND", "3V3", "NC", "IO4"]),
             _side("right", 4, 5, True, names=["IO8", "IO7", "IO6", "IO5"])]
    report = pc.side_pinout_report(sides, pinout)
    assert report["dropped"] == ["9"] and report["unnamed_pads"] == ["3"]
    assert not report["contradicted"] and report["unexplained_entries"] == ["9"]


def test_a_shift_with_evidence_is_contradicted():
    """H1 positive: the same kind of mismatch with labels sitting on another window."""
    report = pc.side_pinout_report([_side("left", 4, 6, names=["G", "D1", "D2", "3V3"])],
                                   [{"pin": n, "name": name} for n, name in
                                    enumerate(["GND", "D1 (SDA)", "D2 (SCL)", "3V3"], start=1)])
    assert report["contradicted"]


def test_enumerated_grounds_are_one_repeated_name():
    """M4: GND1 and GND2 are two grounds -> repeated, as plain GND twice."""
    for first, second in (("GND", "GND"), ("GND1", "GND2")):
        pinout = [{"pin": str(n), "name": name} for n, name in
                  enumerate([first, "RST", "EN", "IO4", second, "IO6"], start=1)]
        report = pc.side_pinout_report([_side("left", 3, 2, names=["GND", "IO7", ""])], pinout)
        placement = report["alignment"]["left"]
        assert placement["unique"] == 0 and placement["repeated"] == 1, first


@pytest.mark.parametrize("name", ["VINP", "VINN", "VBUS_DET", "VCC_EN", "VDDIO"])
def test_a_rail_name_with_a_function_is_not_a_supply(name):
    """L2 negative: an input, a detection or an enable pin."""
    assert pc.agreement_kind(name) != pc.KIND_SUPPLY


@pytest.mark.parametrize("name", ["VDD", "VCC", "VIN", "VDD33", "VCC5", "VDD3P3", "VDD_3V3"])
def test_a_rail_name_with_a_voltage_is_a_supply(name):
    """L2 positive."""
    assert pc.agreement_kind(name) == pc.KIND_SUPPLY


def test_a_shift_never_lands_on_a_declared_off_side_number():
    """L5: pads 2..4, pinout 1..4 with 1 declared off the sides -> no shift of -1."""
    pinout = [{"pin": 1, "name": "EP", "off_side": True}] + [
        {"pin": n, "name": f"IO{n}"} for n in (2, 3, 4)]
    assert pc.uniform_shifts([_side("left", 3, 2)], pinout) == []
    undeclared = [dict(entry, off_side=False) for entry in pinout]
    assert pc.uniform_shifts([_side("left", 3, 2)], undeclared) == [-1]


# --- round 5: strong label contradiction --------------------------------------------------------

_STRONG_PINOUT = [{"pin": n, "name": name} for n, name in
                  enumerate(["RST", "EN", "TX", "RX", "IO5", "IO6"], start=1)]


def test_two_unique_name_labels_on_another_window_are_a_strong_conflict():
    """Positive: the labels RST, EN, TX sit on 1..3, the analysis numbered the side 4..6."""
    side = {"edge": "left", "count": 3, "first_pin": 4, "pin_names": ["RST", "EN", "TX"]}
    (conflict,) = pc.strong_label_conflicts([side], _STRONG_PINOUT)
    assert (conflict["first_pin"], conflict["reverse"], conflict["named"]) == (1, False, 3)
    assert conflict["pins"] == ["4", "5", "6"]
    kinds = [f["kind"] for f in pc.document_findings(pc.side_pinout_report([side], _STRONG_PINOUT))]
    assert pc.FINDING_LABEL_PLACEMENT in kinds


def test_one_unique_name_label_is_never_strong():
    """Negative (audit H2/L3): ONE agreeing label never moves anything."""
    side = {"edge": "left", "count": 3, "first_pin": 4, "pin_names": ["RST", "", ""]}
    assert pc.strong_label_conflicts([side], _STRONG_PINOUT) == []


def test_bare_numbers_and_repeated_names_are_not_strong_evidence():
    """Negative: bare-number labels and grounds do not count as unique names."""
    pinout = [{"pin": n, "name": name} for n, name in
              enumerate(["GND", "1", "2", "GND", "IO5", "IO6"], start=1)]
    side = {"edge": "left", "count": 3, "first_pin": 4, "pin_names": ["G", "1", "2"]}
    assert pc.strong_label_conflicts([side], pinout) == []


def test_the_analysis_mapping_that_matches_the_labels_is_no_conflict():
    side = {"edge": "left", "count": 3, "first_pin": 1, "pin_names": ["RST", "EN", "TX"]}
    assert pc.strong_label_conflicts([side], _STRONG_PINOUT) == []



# --- round 6: final audit -------------------------------------------------------------------------

def test_a_label_printed_on_two_contacts_is_no_strong_evidence():
    """M4 negative (p3): TX/RX printed on both UARTs; the numbering is right -> no conflict."""
    pinout = [{"pin": str(n), "name": name} for n, name in
              enumerate(["TX", "RX", "EN", "TXD1", "RXD1", "SCL"], start=1)]
    sides = [_side("left", 3, 1, names=["TX", "RX", "EN"]),
             _side("right", 3, 4, names=["TX", "RX", "SCL"])]
    assert pc.board_label_counts(sides)["TX"] == 2
    assert pc.strong_label_conflicts(sides, pinout) == []


def test_a_window_held_by_another_side_is_no_evidence():
    """M4: a placement over numbers another side holds with its own strong agreements is
    discarded; the same placement with the other side unlabelled is a conflict."""
    pinout = [{"pin": str(n), "name": name} for n, name in
              enumerate(["3V3", "D1", "EN", "IO4", "IO5", "IO6"], start=1)]
    right = _side("right", 3, 4, names=["3.3V", "D01", "X"])
    held = [_side("left", 3, 1, names=["3V3", "D1", "EN"]), right]
    free = [_side("left", 3, 1, names=["", "", ""]), right]
    assert pc.strong_label_conflicts(held, pinout) == []
    (conflict,) = pc.strong_label_conflicts(free, pinout)
    assert conflict["edge"] == "right" and conflict["first_pin"] == 1


def test_a_repeated_pin_number_is_kept_not_dropped():
    """L5: the first entry names the contact, the second is kept after the contacts."""
    pinout = [{"pin": "1", "name": "A"}, {"pin": "1", "name": "A_ALT"}, {"pin": "2", "name": "B"}]
    rows, unattached, clashes = pc.convention_pinout([_side("left", 2, 1, names=["A", "B"])],
                                                     pinout, {"left": [1, 2]})
    assert [(row["pin"], row["name"]) for row in rows] == [("1", "A"), ("2", "B"), ("3", "A_ALT")]
    assert unattached == ["1"] and clashes == []


def test_a_declared_off_side_entry_on_a_contact_number_stays_off_the_sides():
    """L6: entry 2 is declared an exposed pad; contact 2 keeps its label, the pad stays apart."""
    pinout = [{"pin": "1", "name": "A"}, {"pin": "2", "name": "EP_GND", "off_side": True}]
    rows, unattached, clashes = pc.convention_pinout([_side("left", 2, 1, names=["A", "B"])],
                                                     pinout, {"left": [1, 2]})
    assert ("2", "B") in [(row["pin"], row["name"]) for row in rows]
    assert {"pin": "3", "name": "EP_GND", "off_side": True} in rows
    assert clashes == ["2"] and unattached == []


def test_conflicts_follow_their_contacts_into_a_new_numbering():
    """M3: the pins of a conflict are translated contact by contact."""
    side = _side("left", 3, 4, names=["RST", "EN", "TX"])
    conflicts = pc.strong_label_conflicts([side], [{"pin": n, "name": nm} for n, nm in enumerate(
        ["RST", "EN", "TX", "RX", "IO5", "IO6"], start=1)])
    moved = pc.translated_conflicts(conflicts, [side], {"left": [7, 8, 9]})
    assert conflicts[0]["pins"] == ["4", "5", "6"] and moved[0]["pins"] == ["7", "8", "9"]
