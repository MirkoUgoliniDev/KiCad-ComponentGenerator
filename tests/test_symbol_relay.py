"""@brief A relay's symbol composed from DECLARED pin functions (user, 2026-11-05).

«Un simbolo più adeguato, mettendo la bobina e i contatti» (a more fitting
symbol, with the coil and the contacts), and «deve essere qualcosa di
generale: un relè diverso deve funzionare ugualmente» (it must be general:
a different relay must work the same). The composition reads only the
closed `function` of each pinout row -- coil, common, nc, no -- never a
part name or the free-text pin names; the positives and negatives below
differ in the declared structure, not in a tuned number.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import shutil
import subprocess

import pytest

from kicadgen.generators.symbol import (
    PIN_NAMES_HIDDEN_KEY, layout_symbol, serialize_symbol, symbol_inputs, symbol_pin_names_hidden,
    symbol_property_positions)
from kicadgen.generators.symbol_glyphs import symbol_graphics
from kicadgen.generators.symbol_relay import (
    COMPOSED_KEY, RELAY_BODY_HEIGHT_MM, RELAY_PART_PITCH_MM, RELAY_PIN_FUNCTIONS, RELAY_REASONS,
    is_composed_layout, relay_parts_summary, relay_plan, relay_symbol_rows)


def _rows(*pairs):
    """Pinout rows from (pin, function[, contact label]) tuples, with a free-text name that must NOT matter."""
    rows = []
    for entry in pairs:
        pin, function = entry[0], entry[1]
        row = {"pin": pin, "name": "Bobina" if function == "coil" else "X", "function": function}
        if len(entry) > 2:
            row["contact"] = entry[2]
        rows.append(row)
    return rows


SPDT = _rows(("1", "coil"), ("5", "coil"), ("4", "common"), ("2", "nc"), ("3", "no"))


# ── the plan: structure, never names ────────────────────────────────────

def test_a_coil_and_a_changeover_make_an_spdt_plan():
    plan, reason = relay_plan(SPDT)
    assert reason == () and plan.coil == ("1", "5")
    assert [(c.common, c.nc, c.no, c.glyph_key) for c in plan.contacts] == [("4", "2", "3", "relay_contact_spdt")]
    assert relay_parts_summary(plan) == "coil 1-5; COM 4 NC 2 NO 3"


def test_single_contacts_and_two_contacts_are_planned_from_the_counts():
    no_only, _ = relay_plan(_rows(("A1", "coil"), ("A2", "coil"), ("13", "common"), ("14", "no")))
    assert [c.glyph_key for c in no_only.contacts] == ["relay_contact_no"]
    nc_only, _ = relay_plan(_rows(("A1", "coil"), ("A2", "coil"), ("11", "common"), ("12", "nc")))
    assert [c.glyph_key for c in nc_only.contacts] == ["relay_contact_nc"]
    dpdt, _ = relay_plan(_rows(("1", "coil"), ("2", "coil"), ("3", "common", "1"), ("4", "nc", "1"),
                               ("5", "no", "1"), ("6", "common", "2"), ("7", "no", "2"), ("8", "nc", "2")))
    assert [(c.common, c.nc, c.no) for c in dpdt.contacts] == [("3", "4", "5"), ("6", "8", "7")]


def test_several_contacts_are_grouped_by_the_declared_label_whatever_the_order():
    """Positive (generality audit 2026-11-05, HIGH-1): a table sorted by pin number lists pole 1's
    contacts far from its common; the declared labels pair them, the order does not."""
    sorted_table = _rows(("1", "coil"), ("2", "coil"), ("3", "common", "1"), ("4", "nc", "2"),
                         ("5", "no", "2"), ("6", "common", "2"), ("7", "nc", "1"), ("8", "no", "1"))
    plan, reason = relay_plan(sorted_table)
    assert reason == () and [(c.common, c.nc, c.no) for c in plan.contacts] == [("3", "7", "8"), ("6", "4", "5")]
    # Labels as printed, numeric first, then text: "A"/"B" work the same.
    lettered = _rows(("1", "coil"), ("2", "coil"), ("6", "common", "B"), ("4", "nc", "B"),
                     ("3", "common", "A"), ("8", "no", "A"))
    plan, _ = relay_plan(lettered)
    assert [(c.common, c.nc, c.no) for c in plan.contacts] == [("3", None, "8"), ("6", "4", None)]


def test_several_contacts_without_labels_are_refused_never_paired_by_order():
    """Negative (HIGH-1): the same sorted table without labels would have paired pole 1 with pole
    2's contacts in silence; now it is refused with the pin that lacks its label."""
    unlabelled = _rows(("1", "coil"), ("2", "coil"), ("3", "common"), ("4", "nc"), ("5", "no"),
                       ("6", "common"), ("7", "nc"), ("8", "no"))
    plan, reason = relay_plan(unlabelled)
    assert plan is None and reason == ("contact_group_missing", {"pin": "3"})
    half = _rows(("1", "coil"), ("2", "coil"), ("3", "common", "1"), ("4", "nc", "1"),
                 ("6", "common", "2"), ("7", "nc"))
    assert relay_plan(half)[1] == ("contact_group_missing", {"pin": "7"})
    stray = _rows(("1", "coil"), ("2", "coil"), ("3", "common", "1"), ("4", "nc", "1"),
                  ("6", "common", "2"), ("7", "nc", "3"))
    assert relay_plan(stray)[1] == ("contact_group_unknown", {"pin": "7", "label": "3"})


def test_one_contact_needs_no_label_and_any_order():
    """With a single common pin the grouping is unique: no label, contact pins before the common."""
    plan, reason = relay_plan(_rows(("2", "nc"), ("3", "no"), ("1", "coil"), ("4", "common"), ("5", "coil")))
    assert reason == () and [(c.common, c.nc, c.no) for c in plan.contacts] == [("4", "2", "3")]


@pytest.mark.parametrize("rows, code", [
    (_rows(("1", "coil"), ("5", "coil"), ("4", "common"), ("2", "nc"), ("3", "other")), "no_function"),
    ([{"pin": "1", "name": "Coil", "function": "coil"}, {"pin": "5", "name": "Coil"}], "no_function"),
    (_rows(("1", "coil"), ("5", "coil"), ("4", "common"), ("2", "no"), ("3", "no")), "role_twice"),
    (_rows(("1", "coil"), ("5", "coil"), ("4", "common", "1"), ("6", "common", "1"), ("3", "no", "1")),
     "role_twice"),
    (_rows(("1", "coil"), ("5", "coil"), ("6", "coil"), ("4", "common"), ("3", "no")), "coil_pins"),
    (_rows(("1", "coil"), ("5", "coil")), "no_common"),
    (_rows(("1", "coil"), ("5", "coil"), ("4", "common")), "common_without_contact"),
    (_rows(("1", "coil"), ("1", "coil"), ("4", "common"), ("3", "no")), "duplicate_pin"),
])
def test_an_incomplete_or_ambiguous_declaration_is_refused_with_its_reason(rows, code):
    """Negatives: nothing is guessed; every refusal names a structural reason the GUI translates."""
    composed, reason = relay_symbol_rows(rows)
    assert composed is None and reason[0] == code
    assert set(reason[1]) == set(RELAY_REASONS[code])


def test_the_free_text_names_never_decide():
    """A row named «Bobina» without a declared function is not a coil; one named «foo» declared coil is."""
    named = [{"pin": "1", "name": "Bobina"}, {"pin": "5", "name": "Bobina"},
             {"pin": "4", "name": "Comune"}, {"pin": "2", "name": "NC"}, {"pin": "3", "name": "NO"}]
    assert relay_plan(named)[1][0] == "no_function"
    odd = [{"pin": "1", "name": "foo", "function": "coil"}, {"pin": "5", "name": "bar", "function": "coil"},
           {"pin": "4", "name": "baz", "function": "common"}, {"pin": "2", "name": "qux", "function": "no"}]
    assert relay_plan(odd)[0] is not None


# ── the rows: KiCad's Relay_SPDT geometry, editable like a hand-made one ──

def test_the_composed_rows_lay_the_pins_on_the_glyph_terminals_inside_a_kicad_sized_body():
    composed, reason = relay_symbol_rows(SPDT)
    assert reason == ()
    by_pin = {row["pin"]: row for row in composed}
    # KiCad's Relay_SPDT: coil at x = -5.08, changeover at +5.08, body 20.32 x 10.16.
    assert by_pin["1"]["symbol_position"] == [-5.08, 7.62] and by_pin["1"]["symbol_angle"] == 270
    assert by_pin["5"]["symbol_position"] == [-5.08, -7.62] and by_pin["5"]["symbol_angle"] == 90
    assert by_pin["4"]["symbol_position"] == [5.08, -7.62]
    assert by_pin["2"]["symbol_position"] == [2.54, 7.62] and by_pin["3"]["symbol_position"] == [7.62, 7.62]
    assert all(row["symbol_body"] == [2 * RELAY_PART_PITCH_MM, RELAY_BODY_HEIGHT_MM] for row in composed)
    assert all(row[PIN_NAMES_HIDDEN_KEY] is True and row[COMPOSED_KEY] is True for row in composed)
    assert is_composed_layout(composed) and not is_composed_layout(SPDT)
    # Names and functions survive: only the drawing of the names is off.
    assert by_pin["1"]["name"] == "Bobina" and by_pin["1"]["function"] == "coil"
    # One shared graphics list: coil, link, changeover.
    graphics = composed[0]["symbol_graphics"]
    assert all(row["symbol_graphics"] is graphics for row in composed)
    assert [g["key"] for g in graphics] == ["relay_coil", "relay_link", "relay_contact_spdt"]
    # The layout validates as any editor draft does, with every pin on the grid.
    pins, body = symbol_inputs(composed)
    layout = layout_symbol(pins, body)
    assert (layout.width, layout.height) == (20.32, 10.16)
    assert all(abs(p.x / 2.54 - round(p.x / 2.54)) < 1e-9 and abs(p.y / 2.54 - round(p.y / 2.54)) < 1e-9
               for p in layout.pins)


def test_two_contacts_widen_the_body_and_stay_centred_on_the_grid():
    rows = _rows(("1", "coil"), ("2", "coil"), ("3", "common", "1"), ("4", "nc", "1"), ("5", "no", "1"),
                 ("6", "common", "2"), ("7", "no", "2"), ("8", "nc", "2"))
    composed, _ = relay_symbol_rows(rows)
    keys = [g["key"] for g in composed[0]["symbol_graphics"]]
    assert keys == ["relay_coil", "relay_link", "relay_contact_spdt", "relay_link", "relay_contact_spdt"]
    xs = [g["x"] for g in composed[0]["symbol_graphics"] if g["key"] != "relay_link"]
    assert xs == [-10.16, 0.0, 10.16]
    pins, body = symbol_inputs(composed)
    layout = layout_symbol(pins, body)
    assert (layout.width, layout.height) == (30.48, 10.16)
    assert len({(p.x, p.y) for p in layout.pins}) == 8


def test_an_old_layout_of_the_rows_is_replaced_by_the_composition():
    stale = [dict(row, symbol_side="right", symbol_offset=2.54, symbol_properties={"Value": [0, 0]})
             for row in SPDT]
    composed, _ = relay_symbol_rows(stale)
    assert all("symbol_offset" not in row and "symbol_properties" not in row for row in composed)


# ── the file: hidden pin names, KiCad's own form ─────────────────────────

def _serialized(rows, name="RELAY"):
    pins, body = symbol_inputs(rows)
    return serialize_symbol(name, pins, body=body, graphics=symbol_graphics(rows, []),
                            properties=symbol_property_positions(rows),
                            pin_names_hidden=symbol_pin_names_hidden(rows))


def test_hidden_pin_names_are_written_as_kicad_writes_its_relays():
    composed, _ = relay_symbol_rows(SPDT)
    text = _serialized(composed)
    assert "(pin_names (offset 0.508) (hide yes))" in text
    assert '(name "Bobina"' in text, "the names stay on the pins, only their display is off"
    assert text.count("(pin passive line") == 5
    plain = _serialized([{"pin": "1", "name": "A"}])
    assert "(pin_names (offset 0.508))" in plain and "(hide yes)" not in plain.split("(property")[0]


def test_symbol_pin_names_hidden_reads_the_shared_key_from_any_row():
    assert symbol_pin_names_hidden([{"pin": "1"}, {"pin": "2", PIN_NAMES_HIDDEN_KEY: True}])
    assert not symbol_pin_names_hidden([{"pin": "1", PIN_NAMES_HIDDEN_KEY: False}, {"pin": "2"}])
    assert not symbol_pin_names_hidden([])


def test_real_kicad_loads_the_composed_relay(tmp_path):
    cli = shutil.which("kicad-cli")
    if not cli:
        candidate = Path("C:/Program Files/KiCad/10.0/bin/kicad-cli.exe")
        cli = str(candidate) if candidate.is_file() else None
    if not cli:
        pytest.skip("KiCad CLI not installed")
    composed, _ = relay_symbol_rows(SPDT)
    path = tmp_path / "relay.kicad_sym"
    path.write_text(_serialized(composed), encoding="utf-8")
    result = subprocess.run([cli, "sym", "export", "svg", "-o", str(tmp_path / "svg"), str(path)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert list((tmp_path / "svg").glob("*.svg"))


# ── the editor: the composed draft is drawn without the names ─────────────

def test_the_symbol_editor_draws_the_composed_relay_without_pin_names(qapp):
    from PyQt6.QtWidgets import QGraphicsSimpleTextItem
    from kicadgen.gui.symbol_tab import SymbolTab
    composed, _ = relay_symbol_rows(SPDT)
    tab = SymbolTab()
    tab.resize(1000, 700)
    try:
        tab.set_data("RELAY", composed, pad_ids=["1", "2", "3", "4", "5"])
        texts = [item.text() for item in tab.view.scene().items() if isinstance(item, QGraphicsSimpleTextItem)]
        assert "Bobina" not in texts and "X" not in texts
        assert {"1", "2", "3", "4", "5"} <= set(texts), "the numbers are drawn"
        assert [g["key"] for g in tab.graphics()] == ["relay_coil", "relay_link", "relay_contact_spdt"]
        # Positive twin: the same rows without the flag draw the names.
        plain = [{k: v for k, v in row.items() if k != PIN_NAMES_HIDDEN_KEY} for row in composed]
        tab.set_data("RELAY2", plain, pad_ids=["1", "2", "3", "4", "5"], force=True)
        texts = [item.text() for item in tab.view.scene().items() if isinstance(item, QGraphicsSimpleTextItem)]
        assert "Bobina" in texts
    finally:
        tab.close()


def test_the_declared_functions_are_the_schema_s():
    from kicadgen.services.datasheet_extractor import PINOUT_FUNCTIONS
    assert tuple(PINOUT_FUNCTIONS) == tuple(RELAY_PIN_FUNCTIONS)
