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

# «reference» 2026-11-06, «Dwgs.User» 2026-11-08 (plan B6); the back layers after their front
# twins and one «zones» row for the keepout zones 2026-11-09 (plan F1, F4).
LAYER_KEYS = ("reference", "pads", "holes", "F.SilkS", "B.SilkS", "F.Fab", "B.Fab", "F.CrtYd", "Cmts.User",
              "Dwgs.User", "zones", "texts", "numbers", "dimensions")


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


def _capture_details(monkeypatch):
    """Replace the modal details window with a recorder of what it was given."""
    shown = []
    monkeypatch.setattr(gui, "show_finding_details",
                        lambda parent, details, translate: shown.append(details) or True)
    return shown


def test_a_thin_annular_ring_is_reported_and_its_row_selects_that_pad(window, monkeypatch):  # noqa: F811
    """A pad edit the draft accepts (drill below the pad side) but the guide discourages."""
    shown = _capture_details(monkeypatch)
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
    # The click also opens the full text and the remedy (2026-11-12).
    severity, message, hint = shown[-1]
    assert severity == "warning" and "anello" in message and hint == item.toolTip()
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


def test_a_polyline_before_the_silk_line_does_not_shift_the_drawing_the_row_selects(window, monkeypatch):  # noqa: F811
    """Review 2026-11-06: a line record yields one graphic per segment; the map is built per record."""
    shown = _capture_details(monkeypatch)
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
    assert len(shown) == 1 and "Serigrafia" in shown[0][1]


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


# ── Start-up state and selection of the side panel (user, 2026-11-13) ──


def _groups(window):
    """The four collapsible groups of the side panel, by title."""
    panel = window.fp_properties_group.parentWidget()
    return {group.baseTitle(): group for group in panel.findChildren(type(window.fp_properties_group))}


def _thin_ring(window):
    """Make the first drilled pad's ring thin: a draft with one warning, and return its index."""
    index = next(i for i, pin in enumerate(window.package_geometry.pins) if pin.drill_mm is not None)
    pin = window.package_geometry.pins[index]
    side = min(pin.pad_length_mm, pin.pad_width_mm)
    assert window._apply_package_pad_dialog(index, pin.shape, pin.pad_length_mm, pin.pad_width_mm,
                                            round(side - 0.1, 3), (0.0, 0.0))
    return index


def test_every_group_of_the_side_panel_starts_collapsed(window):  # noqa: F811
    """Positive: «Layer», «Disegno», «Proprietà» and «Controlli» start closed."""
    groups = _groups(window)
    assert set(groups) == {"Layer", "Disegno", "Proprietà", "Controlli"}
    assert not any(group.isExpanded() for group in groups.values())


def test_checks_open_by_themselves_only_when_findings_appear(window):  # noqa: F811
    """Positive: a draft with a finding opens «Controlli»; negative: a clean
    package and no footprint leave it closed, and so do the other groups."""
    checks = window.fp_checks_group
    acquire(window)
    window._draw_preview()
    assert _checks(window) == ["Nessun problema trovato"]
    assert not checks.isExpanded(), "no finding: the group stays closed"
    window._fp_checks_pending = None
    _checks(window)
    assert not checks.isExpanded()
    acquire(window)
    _thin_ring(window)
    assert any("anello" in row for row in _checks(window))
    assert checks.isExpanded(), "findings appeared: the group opens"
    others = {title: group for title, group in _groups(window).items() if title != "Controlli"}
    assert not any(group.isExpanded() for group in others.values())


def test_checks_closed_by_the_user_stay_closed_while_findings_remain(window):  # noqa: F811
    """Negative: only the passage from no findings to some opens the group, so
    a refresh with the same findings does not undo the user's click; the
    findings gone and back is a new passage and opens it again."""
    checks = window.fp_checks_group
    acquire(window)
    _thin_ring(window)
    _checks(window)
    assert checks.isExpanded()
    checks.setExpanded(False)
    _checks(window)
    assert not checks.isExpanded(), "same findings: the user's choice is kept"
    window._undo_footprint_drawing()
    assert not any("anello" in row for row in _checks(window))
    assert not checks.isExpanded(), "the program never closes it, and it was closed"
    _thin_ring(window)
    _checks(window)
    assert checks.isExpanded(), "findings back after none: a new passage"


def test_a_clicked_check_row_is_not_left_highlighted(window, monkeypatch):  # noqa: F811
    """Positive: the list has no selection (no theme highlight after a click),
    as the KLC list; negative: the click still selects the pad and opens the
    details, and the row stays reachable from the keyboard (focus kept)."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QListWidget
    shown = _capture_details(monkeypatch)
    lst = window.fp_checks_list
    assert lst.selectionMode() == QListWidget.SelectionMode.NoSelection
    assert lst.focusPolicy() != Qt.FocusPolicy.NoFocus
    acquire(window)
    index = _thin_ring(window)
    _checks(window)
    item = next(lst.item(i) for i in range(lst.count()) if "anello" in lst.item(i).text())
    lst.setCurrentItem(item)
    assert lst.selectedItems() == []
    window._on_footprint_check_clicked(item)
    assert window._fp_selected_pads() == [index] and shown


def _show_footprint_tab(window, qapp, monkeypatch):
    """Show the window on the footprint tab, so the side panel has its real width.

    The deferred checks refresh is muted: fired by `processEvents`, it would
    rewrite the list these tests fill by hand.
    """
    monkeypatch.setattr(window._fp_checks_timer, "start", lambda *args: None)
    window._fp_checks_timer.stop()
    window._acquisition_completed = True
    window._set_acquisition_tabs_enabled(True)
    window.resize(1400, 900)
    window.show()
    window.tabs.setCurrentIndex(gui.TAB_PREVIEW)
    qapp.processEvents()


LONG_FINDING = "⚠ " + "Pad con valori stimati o derivati dal programma, non letti dal disegno: " * 3


def _rows_fully_visible(lst):
    """Whether every row, as the view itself wraps and lays it out, lies inside the viewport."""
    lst.doItemsLayout()
    return all(lst.visualItemRect(lst.item(i)).bottom() <= lst.viewport().height()
               for i in range(lst.count()))


def test_a_long_check_row_shows_its_whole_wrapped_text(window, qapp, monkeypatch):  # noqa: F811
    """Positive (user, 2026-11-13: «espandilo se contiene testo»): with the group
    open, a long finding wraps on several lines and the list is tall enough for
    the whole row as the view lays it out; negative: a short finding keeps one line.
    The measure is the view's own layout (`visualItemRect`), not a copy of the
    formula under test: before the fix the list was one line per row and the
    long row's bottom fell outside the viewport."""
    lst = window.fp_checks_list
    _show_footprint_tab(window, qapp, monkeypatch)
    window.fp_checks_group.setExpanded(True)
    qapp.processEvents()
    assert lst.isVisible(), "precondition: the list is on screen, at its real width"
    lst.clear()
    lst.addItem("⚠ Corto")
    lst.addItem(LONG_FINDING)
    window._fit_footprint_checks_height()
    qapp.processEvents()
    line = lst.fontMetrics().height()
    lst.doItemsLayout()
    short_rect, long_rect = lst.visualItemRect(lst.item(0)), lst.visualItemRect(lst.item(1))
    assert long_rect.height() >= 3 * line, "precondition: the long finding wraps on several lines"
    assert short_rect.height() < 2 * line, "a short finding stays one line"
    assert _rows_fully_visible(lst), "the whole long row is visible, no scrolling"
    window.hide()


def test_rows_fitted_while_collapsed_are_whole_once_the_group_opens(window, qapp, monkeypatch):  # noqa: F811
    """Positive (review 2026-11-13, D2): findings arriving while the user keeps
    «Controlli» closed are measured without a real width; opening the group
    refits them. Negative precondition: closed, the list is not visible, so
    no real-width layout could have happened before the opening."""
    lst = window.fp_checks_list
    _show_footprint_tab(window, qapp, monkeypatch)
    window.fp_checks_group.setExpanded(False)
    qapp.processEvents()
    lst.clear()
    lst.addItem(LONG_FINDING)
    window._fit_footprint_checks_height()
    assert not lst.isVisible(), "precondition: fitted while hidden"
    window.fp_checks_group.setExpanded(True)
    qapp.processEvents()
    qapp.processEvents()
    assert lst.visualItemRect(lst.item(0)).height() >= 3 * lst.fontMetrics().height()
    assert _rows_fully_visible(lst)
    window.hide()


def test_a_footprint_that_did_not_build_does_not_reopen_a_closed_group(window):  # noqa: F811
    """Negative (review 2026-11-13, D1): a redraw that does not build (None)
    between two identical sets of findings is not a passage from none to some,
    so the group the user closed stays closed; positive counterpart: «Nessun
    problema trovato» between them IS such a passage (the test above)."""
    checks = window.fp_checks_group
    acquire(window)
    _thin_ring(window)
    _checks(window)
    assert checks.isExpanded()
    checks.setExpanded(False)
    built = window._fp_checks_pending
    window._fp_checks_pending = None
    assert _checks(window) == ["Nessun footprint da controllare."]
    window._fp_checks_pending = built
    assert any("anello" in row for row in _checks(window))
    assert not checks.isExpanded(), "the same findings after a failed build: the user's choice is kept"


def test_the_tilde_action_writes_no_datasheet_and_can_be_undone(window):  # noqa: F811
    """Positive (user, 2026-11-13): the «~» action inside the Datasheet field
    writes KiCad's «no datasheet», which clears the empty-datasheet KLC error
    shown just before, and switches itself off; negative: a URL replaced by
    mistake comes back with undo and the action is on again."""
    field, action = window.field_datasheet, window.action_datasheet_none

    def klc_rows():
        return [window.klc_list.item(i).text() for i in range(window.klc_list.count())]

    assert action in field.actions()
    field.setText("")
    assert any("Datasheet vuoto" in row for row in klc_rows()), "precondition: the empty field is reported"
    assert action.isEnabled() and action.toolTip()
    action.trigger()
    assert field.text() == "~"
    assert not action.isEnabled()
    assert not any("Datasheet vuoto" in row for row in klc_rows())
    field.setText("https://example.org/ds.pdf")
    assert action.isEnabled()
    action.trigger()
    assert field.text() == "~"
    field.undo()
    assert field.text() == "https://example.org/ds.pdf"
    assert action.isEnabled()
