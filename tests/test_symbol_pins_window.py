"""@brief The window applies «symbol pins = footprint pads» when an analysis is settled (user, 2026-10-31).

T7N relay: the AI's pinout numbered the pins 1..5 by invention («Coil_A1»,
«Common_11»...) while the footprint pads are 11, 12, 14, A1, A2; the symbol
came out with ten pins. The rule itself is tested in
tests/test_symbol_pins_from_pads.py; this checks the window uses it.
"""

T7N_PADS = ["11", "12", "14", "A1", "A2"]
INVENTED = [{"pin": 1, "name": "Coil_A1"}, {"pin": 2, "name": "Coil_A2"},
            {"pin": 3, "name": "Common_11"}, {"pin": 4, "name": "NC_14"},
            {"pin": 5, "name": "NO_12"}]


def test_an_invented_pinout_becomes_the_names_of_the_footprint_pads(window, monkeypatch):
    monkeypatch.setattr(window, "_symbol_pad_ids", lambda: list(T7N_PADS))
    window._user_pin_names = {}
    window._settle_extracted_pinout([dict(row) for row in INVENTED])
    pins = {str(row["pin"]): row["name"] for row in window.pinout}
    assert sorted(pins) == sorted(T7N_PADS), "one pin per pad, no pin 1..5"
    assert pins == {"11": "Common_11", "12": "NO_12", "14": "NC_14",
                    "A1": "Coil_A1", "A2": "Coil_A2"}


def test_unknown_pads_leave_the_pinout_as_the_analysis_gave_it(window, monkeypatch):
    """Negative: without a known footprint nothing is re-mapped."""
    def unknown():
        raise ValueError("no footprint")
    monkeypatch.setattr(window, "_symbol_pad_ids", unknown)
    window._user_pin_names = {}
    window._settle_extracted_pinout([dict(row) for row in INVENTED])
    assert [row["pin"] for row in window.pinout] == [1, 2, 3, 4, 5]
