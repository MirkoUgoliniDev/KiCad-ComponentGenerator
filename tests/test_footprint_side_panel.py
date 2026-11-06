"""@brief The side panel of the footprint tab (2026-11-06): «Layer», «Controlli», «Disegno».

Layer boxes hide the tagged items of the preview without a redraw and are
remembered as a preference; the courtyard and fabrication graphics of the
built footprint are drawn and tagged; the «Controlli» list follows the draft
(a pad moved onto another, a silkscreen line over a pad) and a click on a
row selects the elements concerned. Positive and negative cases for each.
"""
from PyQt6.QtWidgets import QCheckBox, QGraphicsSimpleTextItem

from kicadgen.gui.footprint_view import DRAWING_KEY_ROLE, HOLE_ROLE, LAYER_ROLE, PAD_KEY_ROLE
from tests.test_package_project_gui import acquire
from tests.test_photo_inventory_gui import gui  # noqa: F401 - window fixture

LAYER_KEYS = ("pads", "holes", "F.SilkS", "F.Fab", "F.CrtYd", "Cmts.User", "texts", "numbers", "dimensions")


def _tagged(window, tag):
    return [item for item in window.preview_scene.items() if item.data(LAYER_ROLE) == tag]


def _checks(window):
    """Run the pending checks now and return the rows' texts."""
    window._refresh_footprint_checks_now()
    lst = window.fp_checks_list
    return [lst.item(i).text() for i in range(lst.count())]


def test_the_panel_has_one_box_per_layer_all_on_by_default(window):  # noqa: F811
    assert tuple(window.fp_layer_boxes) == LAYER_KEYS
    assert all(isinstance(box, QCheckBox) and box.isChecked() for box in window.fp_layer_boxes.values())
    assert tuple(key for key, _label, _colour in window.FOOTPRINT_VIEW_LAYERS) == LAYER_KEYS


def test_every_drawn_element_of_a_package_carries_its_layer(window):  # noqa: F811
    acquire(window)
    window._draw_preview()
    pads = [item for item in window.preview_scene.items()
            if str(item.data(PAD_KEY_ROLE) or "").startswith("package:")]
    assert pads and all(item.data(LAYER_ROLE) == "pads" for item in pads)
    holes = [item for item in window.preview_scene.items() if item.data(HOLE_ROLE)]
    assert holes and all(item.data(LAYER_ROLE) == "holes" for item in holes)
    numbers = [item for item in _tagged(window, "numbers") if isinstance(item, QGraphicsSimpleTextItem)]
    assert numbers
    # The courtyard and the fabrication outline of the BUILT footprint are drawn now.
    assert _tagged(window, "F.CrtYd") and _tagged(window, "F.Fab")
    assert _tagged(window, "texts") and _tagged(window, "dimensions")


def test_unticking_a_layer_hides_its_items_without_a_redraw_and_is_remembered(window):  # noqa: F811
    acquire(window)
    window._draw_preview()
    pads = _tagged(window, "pads")
    assert pads and all(item.isVisible() for item in pads)
    before = set(map(id, window.preview_scene.items()))
    window.fp_layer_boxes["pads"].setChecked(False)
    assert set(map(id, window.preview_scene.items())) == before, "no redraw"
    assert all(not item.isVisible() for item in pads)
    assert all(item.isVisible() for item in _tagged(window, "holes")), "other layers untouched"
    assert window._config["preview_layers"] == {"pads": False}
    # A redraw keeps the preference.
    window._draw_preview()
    assert all(not item.isVisible() for item in _tagged(window, "pads"))
    window.fp_layer_boxes["pads"].setChecked(True)
    assert all(item.isVisible() for item in _tagged(window, "pads"))
    assert window._config["preview_layers"] == {"pads": True}


def test_user_drawings_follow_their_own_layer_box(window):  # noqa: F811
    acquire(window)
    window.footprint_drawings = [{"kind": "line", "layer": "Cmts.User", "width": 0.12,
                                  "points": [(-20.0, -20.0), (-19.0, -20.0)]}]
    window._draw_preview()
    drawn = [item for item in window.preview_scene.items() if item.data(DRAWING_KEY_ROLE) == 0]
    assert drawn and all(item.data(LAYER_ROLE) == "Cmts.User" for item in drawn)
    window.fp_layer_boxes["Cmts.User"].setChecked(False)
    assert all(not item.isVisible() for item in drawn)
    window.fp_layer_boxes["Cmts.User"].setChecked(True)


def test_a_clean_package_reports_no_problem_and_no_footprint_says_so(window):  # noqa: F811
    acquire(window)
    window._draw_preview()
    assert _checks(window) == ["Nessun problema trovato"]
    window._fp_checks_pending = None
    assert _checks(window) == ["Nessun footprint da controllare."]


def test_a_thin_annular_ring_is_reported_and_its_row_selects_that_pad(window):  # noqa: F811
    """A pad edit the draft accepts (drill below the pad side) but the guide discourages."""
    acquire(window)
    index = next(i for i, pin in enumerate(window.package_geometry.pins) if pin.drill_mm is not None)
    pin = window.package_geometry.pins[index]
    side = min(pin.pad_length_mm, pin.pad_width_mm)
    assert window._apply_package_pad_dialog(index, pin.shape, pin.pad_length_mm, pin.pad_width_mm,
                                            round(side - 0.1, 3), (0.0, 0.0))
    rows = _checks(window)
    ring_rows = [i for i in range(window.fp_checks_list.count())
                 if "anello" in window.fp_checks_list.item(i).text()]
    assert ring_rows and rows[ring_rows[0]].startswith("⚠")
    item = window.fp_checks_list.item(ring_rows[0])
    assert item.data(window._FP_CHECK_TARGETS_ROLE) == ((index,), ())
    assert item.toolTip()
    window._on_footprint_check_clicked(item)
    assert window._fp_selected_pads() == [index]
    # Undo restores the ring and the row goes away.
    window._undo_footprint_drawing()
    assert not any("anello" in row for row in _checks(window))


def test_a_silkscreen_line_over_a_pad_is_a_warning_and_its_row_selects_the_drawing(window):  # noqa: F811
    acquire(window)
    pin = window.package_geometry.pins_seen_from_top()[0]
    window.footprint_drawings = [{"kind": "line", "layer": "F.SilkS", "width": 0.12,
                                  "points": [(pin.x_mm - 1.0, pin.y_mm), (pin.x_mm + 1.0, pin.y_mm)]}]
    window._draw_preview()
    rows = _checks(window)
    assert any(row.startswith("⚠") and "Serigrafia" in row for row in rows)
    item = next(window.fp_checks_list.item(i) for i in range(window.fp_checks_list.count())
                if "Serigrafia" in window.fp_checks_list.item(i).text())
    pads, drawings = item.data(window._FP_CHECK_TARGETS_ROLE)
    assert drawings == (0,) and pads == (0,)
    # The same line on the fabrication layer is no finding.
    window.footprint_drawings[0]["layer"] = "F.Fab"
    window._draw_preview()
    assert not any("Serigrafia" in row for row in _checks(window))


def test_a_polyline_before_the_silk_line_does_not_shift_the_drawing_the_row_selects(window):  # noqa: F811
    """Review 2026-11-06: a line record yields one graphic per segment; the map is built per record."""
    acquire(window)
    pin = window.package_geometry.pins_seen_from_top()[0]
    window.footprint_drawings = [
        {"kind": "line", "layer": "Cmts.User", "width": 0.12,
         "points": [(-20.0, -20.0), (-19.0, -20.0), (-19.0, -19.0), (-18.0, -19.0)]},
        {"kind": "line", "layer": "F.SilkS", "width": 0.12,
         "points": [(pin.x_mm - 1.0, pin.y_mm), (pin.x_mm + 1.0, pin.y_mm)]}]
    window._draw_preview()
    _checks(window)
    item = next(window.fp_checks_list.item(i) for i in range(window.fp_checks_list.count())
                if "Serigrafia" in window.fp_checks_list.item(i).text())
    assert item.data(window._FP_CHECK_TARGETS_ROLE) == ((0,), (1,))
    window._on_footprint_check_clicked(item)
    assert window._fp_selected_indices() == [1] and window._fp_selected_pads() == []


def test_the_rubber_band_ignores_hidden_layers(window):  # noqa: F811
    """Review 2026-11-06: what the panel hides must not be band-selected (and then deleted unseen)."""
    acquire(window)
    window.footprint_drawings = [{"kind": "line", "layer": "Cmts.User", "width": 0.12,
                                  "points": [(-20.0, -20.0), (-19.0, -20.0)]}]
    window._draw_preview()
    whole = window.preview_scene.itemsBoundingRect().adjusted(-50, -50, 50, 50)
    window._select_footprint_drawings_in(whole, False)
    assert window._fp_selected_pads(), "visible pads are band-selected"
    window.fp_layer_boxes["pads"].setChecked(False)
    window._select_footprint_drawings_in(whole, False)
    assert window._fp_selected_pads() == [] and window._fp_selected_indices() == [0]
    window.fp_layer_boxes["Cmts.User"].setChecked(False)
    window._select_footprint_drawings_in(whole, False)
    assert window._fp_selected_indices() == []
    window.fp_layer_boxes["pads"].setChecked(True)
    window.fp_layer_boxes["Cmts.User"].setChecked(True)


def test_the_pin_header_drawer_tags_its_layers_and_feeds_the_checks(window, monkeypatch):  # noqa: F811
    """The pin-header path of `_draw_preview` (PinHeaderParams, not host lands) tags and checks too."""
    from kicadgen.generators.pin_header import PinHeaderParams
    params = PinHeaderParams(rows=2, cols=3)
    monkeypatch.setattr(window, "_preview_params", lambda: params)
    window._draw_preview()
    # No F.Fab here: the pin header generator writes only the fabrication
    # reference TEXT on F.Fab, and texts are not drawn by the layer drawer.
    for tag in ("pads", "holes", "F.SilkS", "F.CrtYd", "numbers", "texts", "dimensions"):
        assert _tagged(window, tag), tag
    assert not _tagged(window, "F.Fab")
    assert _checks(window) == ["Nessun problema trovato"]


def test_the_checks_are_deferred_until_the_preview_rests(window):  # noqa: F811
    acquire(window)
    window._draw_preview()
    assert window._fp_checks_timer.isActive() and window._fp_checks_timer.isSingleShot()
    assert window._fp_checks_timer.interval() == window._FP_CHECKS_DELAY_MS
    window._refresh_footprint_checks_now()
    assert not window._fp_checks_timer.isActive()
