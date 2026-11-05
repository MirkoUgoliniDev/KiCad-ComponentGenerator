"""@brief The relay symbol in the application: composed after the analysis, toggled in the symbol tab.

The analysis declares a `function` per pinout entry; the main window composes
the symbol only from those declarations, keeps a custom layout the user
made, and the «Simbolo funzionale» toggle goes back to the box or recomposes.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from kicadgen.generators.symbol import PIN_NAMES_HIDDEN_KEY
from kicadgen.generators.symbol_relay import COMPOSED_KEY
from tests.ui_flow_helpers import package_extraction, prepare, run_extraction, soic_geometry

#: Eight SOIC pads named as a two-contact relay would be: the functions decide, not the names.
DPDT = [{"pin": "1", "name": "Bobina", "function": "coil"}, {"pin": "2", "name": "Bobina", "function": "coil"},
        {"pin": "3", "name": "Comune", "function": "common", "contact": "1"},
        {"pin": "4", "name": "NC", "function": "nc", "contact": "1"},
        {"pin": "5", "name": "NO", "function": "no", "contact": "1"},
        {"pin": "6", "name": "Comune", "function": "common", "contact": "2"},
        {"pin": "7", "name": "NC", "function": "nc", "contact": "2"},
        {"pin": "8", "name": "NO", "function": "no", "contact": "2"}]
NAMED_ONLY = [{"pin": row["pin"], "name": row["name"]} for row in DPDT]
#: Pole 2's functions the other way round: a second analysis that contradicts the first.
DPDT_SWAPPED = [dict(row, function={"nc": "no", "no": "nc"}.get(row["function"], row["function"]))
                for row in DPDT]


def _action(window):
    return window.symbol_tab.functional_button.defaultAction()


def test_an_analysis_with_declared_functions_composes_the_relay_symbol(window, monkeypatch, tmp_path):
    prepare(window, monkeypatch, tmp_path, "K1")
    run_extraction(window, package_extraction(soic_geometry(), pinout=DPDT))
    rows = window.pinout
    assert all(row.get(PIN_NAMES_HIDDEN_KEY) for row in rows)
    assert [g["key"] for g in rows[0]["symbol_graphics"]] == [
        "relay_coil", "relay_link", "relay_contact_spdt", "relay_link", "relay_contact_spdt"]
    assert _action(window).isEnabled() and _action(window).isChecked()
    assert any("composto dalle funzioni dichiarate" in str(line) for line in window._log_lines)
    assert all(row.get(COMPOSED_KEY) for row in rows)


def test_a_pinout_naming_fewer_pads_than_the_footprint_keeps_the_box(window, monkeypatch, tmp_path):
    """Negative (generality audit 2026-11-05, MEDIUM-2): pad 8 has no row, hence no declared function;
    a composed symbol could not place it, so the box stays and the log names the pad."""
    prepare(window, monkeypatch, tmp_path, "K5")
    run_extraction(window, package_extraction(soic_geometry(), pinout=DPDT[:-1]))
    assert not any(row.get(COMPOSED_KEY) for row in window.pinout)
    assert not _action(window).isEnabled()
    assert any("senza una funzione dichiarata nel pinout: 8" in str(line) for line in window._log_lines)


def test_a_new_analysis_recomposes_over_a_stale_composition(window, monkeypatch, tmp_path):
    """MEDIUM-3: the composed layout is derived data. The first analysis composes; the user keeps the
    names; the second analysis swaps pole 2's NC/NO: the symbol follows the new functions."""
    prepare(window, monkeypatch, tmp_path, "K6")
    run_extraction(window, package_extraction(soic_geometry(), pinout=DPDT))
    first = {row["pin"]: row["symbol_position"] for row in window.pinout}
    run_extraction(window, package_extraction(soic_geometry(), pinout=DPDT_SWAPPED))
    second = {row["pin"]: row["symbol_position"] for row in window.pinout}
    assert all(row.get(COMPOSED_KEY) for row in window.pinout)
    assert first["7"] == second["8"] and first["8"] == second["7"] and first["3"] == second["3"]


def test_names_alone_compose_nothing_and_the_toggle_stays_off(window, monkeypatch, tmp_path):
    """Negative: the same names without declared functions keep the box; no log, the toggle disabled."""
    prepare(window, monkeypatch, tmp_path, "K2")
    run_extraction(window, package_extraction(soic_geometry(), pinout=NAMED_ONLY))
    assert not any(key.startswith("symbol_") for row in window.pinout for key in row)
    assert not _action(window).isEnabled()
    assert not any("Simbolo funzionale" in str(line) for line in window._log_lines)


def test_the_toggle_goes_back_to_the_box_and_recomposes(window, monkeypatch, tmp_path):
    prepare(window, monkeypatch, tmp_path, "K3")
    run_extraction(window, package_extraction(soic_geometry(), pinout=DPDT))
    _action(window).setChecked(False)
    assert not any(key.startswith("symbol_") for row in window.pinout for key in row)
    assert all(row.get("function") for row in window.pinout), "the declared functions are kept"
    assert window._functional_symbol() is False and not _action(window).isChecked()
    assert any("disegni e i fili del simbolo composto" in str(line) for line in window._log_lines)
    _action(window).setChecked(True)
    assert all(row.get(PIN_NAMES_HIDDEN_KEY) for row in window.pinout)
    assert window._functional_symbol() is True and _action(window).isChecked()


def test_the_toggle_reflects_the_rows_not_the_option(window, monkeypatch, tmp_path):
    """LOW-5: with a layout the user made the composition is skipped, so the button is NOT checked
    although the option is on; LOW-6: a refused recomposition leaves the option off."""
    prepare(window, monkeypatch, tmp_path, "K7")
    run_extraction(window, package_extraction(soic_geometry(), pinout=DPDT))
    custom = [{k: v for k, v in row.items() if k not in ("symbol_graphics", "symbol_body", COMPOSED_KEY)}
              for row in window.pinout]
    custom[0]["symbol_position"] = [-12.7, 7.62]
    window.pinout = custom
    window._refresh_symbol_tab(force=True)
    assert _action(window).isEnabled() and not _action(window).isChecked()
    assert window._functional_symbol() is True
    # A toggle on that cannot compose (a function removed) goes back off with the option.
    window.pinout = [dict(row, function="other") if row["pin"] == "8" else dict(row) for row in custom]
    window._toggle_functional_symbol(True)
    assert window._functional_symbol() is False and not _action(window).isChecked()
    assert window.pinout[0]["symbol_position"] == [-12.7, 7.62], "the symbol is unchanged"


def test_every_relay_reason_template_is_a_catalogue_key():
    """The GUI holds the only Italian copy of each reason and the catalogue translates it."""
    from kicadgen.gui.main_window import MainWindow
    from kicadgen.generators.symbol_relay import RELAY_REASONS
    from kicadgen.services.i18n import DEFAULT_TRANSLATIONS
    texts = MainWindow._RELAY_REASON_TEXTS
    assert set(texts) == set(RELAY_REASONS)
    for code, template in texts.items():
        entry = DEFAULT_TRANSLATIONS["ui:" + template]
        assert set(entry) == {"it", "en", "fr", "de"}
        for placeholder in RELAY_REASONS[code]:
            assert "{%s}" % placeholder in template


def test_a_custom_layout_of_the_user_is_not_replaced_unless_asked(window, monkeypatch, tmp_path):
    """The silent composition (after an analysis) leaves a custom layout alone; the toggle replaces it."""
    prepare(window, monkeypatch, tmp_path, "K4")
    run_extraction(window, package_extraction(soic_geometry(), pinout=DPDT))
    custom = [{k: v for k, v in row.items()
               if k not in ("symbol_graphics", "symbol_body", PIN_NAMES_HIDDEN_KEY, COMPOSED_KEY)}
              for row in window.pinout]
    custom[0]["symbol_position"] = [-12.7, 7.62]
    window.pinout = custom
    assert window._compose_functional_symbol() is False
    assert window.pinout[0]["symbol_position"] == [-12.7, 7.62]
    assert window._compose_functional_symbol(replace_layout=True) is True
    assert window.pinout[0]["symbol_position"] == [-10.16, 7.62]
