"""@brief «Modifica pad N» of an explicit package: the drill of a holed pad (user, 2026-10-10).

A through-hole pad shows «Foro:» and the new drill reaches the saved package
and the regenerated footprint; an SMD pad has no such field; a drill that
does not fit the pad is refused and the previous value comes back.
"""
from PyQt6.QtWidgets import QDialog, QDoubleSpinBox, QFormLayout, QLabel, QMessageBox

from kicadgen.gui.project_state import PROJECT_CONFIG_NAME
from tests.test_package_project_gui import acquire
from tests.test_photo_inventory_gui import gui


def _drive(monkeypatch, drill=None, seen=None):
    """Accept the pad dialog, recording its labels and setting the drill when asked."""
    def exec_(dialog):
        labels = [w.text() for w in dialog.findChildren(QLabel)]
        spins = dialog.findChildren(QDoubleSpinBox)
        form = dialog.findChild(QFormLayout)
        drill_spin = None
        for spin in spins:
            label = form.labelForField(spin)
            if label is not None and label.text() == "Foro:":
                drill_spin = spin
        if seen is not None:
            seen.update(labels=labels, drill=drill_spin)
        if drill is not None:
            assert drill_spin is not None
            drill_spin.setValue(drill)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(QDialog, "exec", exec_)


def _tht_index(window):
    return next(i for i, pin in enumerate(window.package_geometry.pins) if pin.drill_mm is not None)


def _smd_index(window):
    return next(i for i, pin in enumerate(window.package_geometry.pins) if pin.drill_mm is None)


def test_a_holed_pad_offers_the_drill_and_the_value_reaches_package_and_footprint(
        window, monkeypatch):  # noqa: F811
    acquire(window)
    monkeypatch.setattr(gui.GenerateWorker, "start", lambda worker: worker.run())
    index = _tht_index(window)
    seen = {}
    _drive(monkeypatch, drill=0.5, seen=seen)
    window._edit_package_pad(index)
    assert seen["drill"] is not None and seen["drill"].decimals() == 3
    # CONTRACT CHANGED 2026-11-06: 0 is «da indicare» (no size yet), refused on OK.
    assert seen["drill"].minimum() == 0.0 and seen["drill"].maximum() == 20.0
    assert seen["drill"].isEnabled()
    assert window.package_geometry.pins[index].drill_mm == 0.5
    folder = window._project_dir()
    footprint = folder / f"{folder.name}.pretty" / f"{folder.name}.kicad_mod"
    # CONTRACT CHANGED 2026-10-14 (footprint draft, user): the pad edit is a
    # draft until «Applica», which saves the project and rewrites the file.
    config = folder / PROJECT_CONFIG_NAME
    assert not config.is_file() or '"drill_mm": 0.5' not in config.read_text(encoding="utf-8")
    assert not footprint.is_file() or "(drill 0.5)" not in footprint.read_text(encoding="utf-8")
    assert window.apply_footprint_draft()
    from PyQt6.QtWidgets import QApplication
    QApplication.processEvents()      # the rewrite runs on the next event-loop turn
    assert "(drill 0.5)" in footprint.read_text(encoding="utf-8")
    saved = config.read_text(encoding="utf-8")
    assert '"drill_mm": 0.5' in saved


def test_an_smd_pad_has_its_drill_field_disabled(window, monkeypatch):  # noqa: F811
    """CONTRACT CHANGED 2026-11-06 (pad type editable): the field exists, disabled, and writes nothing."""
    acquire(window)
    monkeypatch.setattr(gui.GenerateWorker, "start", lambda worker: worker.run())
    index = _smd_index(window)
    seen = {}
    _drive(monkeypatch, seen=seen)
    window._edit_package_pad(index)
    assert seen["drill"] is not None and not seen["drill"].isEnabled()
    assert seen["drill"].value() == 0.0 and seen["drill"].specialValueText() == "da indicare"
    assert window.package_geometry.pins[index].drill_mm is None
    assert window.package_geometry.pins[index].pad_type == "smd"


def test_a_drill_that_does_not_fit_the_pad_is_refused_and_restored(window, monkeypatch):  # noqa: F811
    acquire(window)
    index = _tht_index(window)
    pin = window.package_geometry.pins[index]
    before = pin.drill_mm
    too_big = min(pin.pad_length_mm, pin.pad_width_mm)  # equal to the smaller side: refused
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[2]))
    regenerated = []
    monkeypatch.setattr(window, "_generate", lambda **kwargs: regenerated.append(kwargs))
    _drive(monkeypatch, drill=too_big)
    window._edit_package_pad(index)
    assert warnings and "minore" in warnings[0]
    assert window.package_geometry.pins[index].drill_mm == before
    assert window.package_geometry.pins[index] is pin
    assert regenerated == []


# ── Pad type, rotation and slot (2026-11-06) ─────────────────────────────

def _drive_fields(monkeypatch, *, pad_type=None, rotation=None, drill=None, slot=None, seen=None,
                  answers=None):
    """Accept the dialog after setting the named fields; `answers` lists the result of each exec."""
    from PyQt6.QtWidgets import QComboBox
    results = list(answers or [QDialog.DialogCode.Accepted])

    def exec_(dialog):
        form = dialog.findChild(QFormLayout)
        by_label = {}
        for widget in dialog.findChildren(QDoubleSpinBox) + dialog.findChildren(QComboBox):
            label = form.labelForField(widget)
            if label is not None:
                by_label[label.text()] = widget
        if seen is not None:
            seen.update(by_label)
        if pad_type is not None:
            combo = by_label["Tipo di pad:"]
            combo.setCurrentIndex(combo.findData(pad_type))
        if rotation is not None:
            by_label["Rotazione:"].setValue(rotation)
        if drill is not None:
            by_label["Foro:"].setValue(drill)
        if slot is not None:
            by_label["Asola, secondo lato:"].setValue(slot)
        return results.pop(0) if len(results) > 1 else results[0]
    monkeypatch.setattr(QDialog, "exec", exec_)


def test_a_holed_pad_made_mechanical_is_written_without_number_and_leaves_the_symbol(window, monkeypatch):  # noqa: F811
    """The model keeps the number (generality audit 2026-11-06: reversible, undoable); the file drops it."""
    acquire(window)
    monkeypatch.setattr(gui.GenerateWorker, "start", lambda worker: worker.run())
    index = _tht_index(window)
    before = window.package_geometry.pins[index]
    number = before.number
    assert number in window._symbol_pad_ids()
    _drive_fields(monkeypatch, pad_type="np_thru_hole")
    window._edit_package_pad(index)
    pin = window.package_geometry.pins[index]
    assert pin.pad_type == "np_thru_hole" and pin.number == number and pin.drill_mm == before.drill_mm
    assert number not in window._symbol_pad_ids()
    # Undo brings the plated pad back; redo the mechanical one.
    window._undo_footprint_drawing()
    assert window.package_geometry.pins[index] == before
    window._redo_footprint_drawing()
    assert window.package_geometry.pins[index].pad_type == "np_thru_hole"
    assert window.apply_footprint_draft()
    from PyQt6.QtWidgets import QApplication
    QApplication.processEvents()
    folder = window._project_dir()
    text = (folder / f"{folder.name}.pretty" / f"{folder.name}.kicad_mod").read_text(encoding="utf-8")
    assert '(pad "" np_thru_hole' in text and f'(pad "{number}"' not in text
    # The way back: plated again, with its number and name.
    _drive_fields(monkeypatch, pad_type="thru_hole")
    window._edit_package_pad(index)
    pin = window.package_geometry.pins[index]
    assert pin.pad_type == "thru_hole" and pin.number == number and pin.name == before.name
    assert number in window._symbol_pad_ids()


def test_a_slot_and_a_rotation_reach_the_package_the_file_and_the_preview(window, monkeypatch):  # noqa: F811
    acquire(window)
    monkeypatch.setattr(gui.GenerateWorker, "start", lambda worker: worker.run())
    index = _tht_index(window)
    pin = window.package_geometry.pins[index]
    slot = round(pin.pad_width_mm * 0.75, 3)
    drill = 0.4 if slot != 0.4 else 0.3
    _drive_fields(monkeypatch, drill=drill, slot=slot, rotation=90.0)
    window._edit_package_pad(index)
    pin = window.package_geometry.pins[index]
    assert (pin.drill_mm, pin.drill_length_mm) == (drill, slot)
    # The model holds the source-frame angle: 90 seen from the top, minus
    # the package orientation (the fixture is not reflected).
    assert pin.rotation_deg == (90.0 - window.package_geometry.orientation_deg) % 360
    # The preview draws a stadium outline for the slot, turned with the pad.
    from PyQt6.QtWidgets import QGraphicsPathItem
    from kicadgen.gui.footprint_view import HOLE_ROLE
    slots = [item for item in window.preview_scene.items()
             if isinstance(item, QGraphicsPathItem) and item.data(HOLE_ROLE)]
    # The dialog edits the angle SEEN FROM THE TOP (the one KiCad writes),
    # whatever the package orientation: the preview turns the slot by it.
    seen = window.package_geometry.pins_seen_from_top()[index].rotation_deg
    assert seen == 90.0
    assert len(slots) == 1 and slots[0].rotation() == -90.0
    assert window.apply_footprint_draft()
    from PyQt6.QtWidgets import QApplication
    QApplication.processEvents()
    folder = window._project_dir()
    text = (folder / f"{folder.name}.pretty" / f"{folder.name}.kicad_mod").read_text(encoding="utf-8")
    assert f"(drill oval {drill:g} {slot:g})" in text


def test_the_rotation_typed_is_the_top_view_angle_under_a_reflection_and_an_orientation(window, monkeypatch):  # noqa: F811
    """A bottom-view package turned by 90: typing 30 writes 30, the source angle is derived."""
    from dataclasses import replace
    acquire(window)
    index = _tht_index(window)
    package = window.package_geometry
    window._rebind_package(source_view="bottom", orientation_deg=90.0)
    assert window.package_geometry is not package
    _drive_fields(monkeypatch, rotation=30.0)
    window._edit_package_pad(index)
    seen = window.package_geometry.pins_seen_from_top()[index]
    assert seen.rotation_deg == 30.0
    # Source frame: seen = (-source if reflected else source) + orientation.
    reflected = (int(window.package_geometry.source_view == "bottom")
                 + int(window.package_geometry.mirrored)) % 2 == 1
    expected = (90.0 - 30.0) % 360 if reflected else (30.0 - 90.0) % 360
    assert window.package_geometry.pins[index].rotation_deg == expected
    window._rebind_package(source_view=package.source_view, orientation_deg=package.orientation_deg,
                           pins=list(replace(package, pins=package.pins).pins))


def test_a_slot_equal_to_the_drill_is_a_round_hole(window, monkeypatch):  # noqa: F811
    acquire(window)
    index = _tht_index(window)
    _drive_fields(monkeypatch, drill=0.5, slot=0.5)
    window._edit_package_pad(index)
    pin = window.package_geometry.pins[index]
    assert pin.drill_mm == 0.5 and pin.drill_length_mm is None


def test_a_slot_side_at_the_pad_side_is_refused_and_restored(window, monkeypatch):  # noqa: F811
    acquire(window)
    index = _tht_index(window)
    pin = window.package_geometry.pins[index]
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[2]))
    _drive_fields(monkeypatch, slot=pin.pad_width_mm)
    window._edit_package_pad(index)
    assert warnings and "asola" in warnings[0]
    assert window.package_geometry.pins[index] is pin


def test_an_smd_pad_made_holed_without_a_size_is_refused_not_invented(window, monkeypatch):  # noqa: F811
    """No hole size is guessed: OK with «da indicare» warns and keeps the dialog; Cancel changes nothing."""
    acquire(window)
    index = _smd_index(window)
    pin = window.package_geometry.pins[index]
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[2]))
    _drive_fields(monkeypatch, pad_type="thru_hole",
                  answers=[QDialog.DialogCode.Accepted, QDialog.DialogCode.Rejected])
    window._edit_package_pad(index)
    assert warnings and "diametro" in warnings[0]
    assert window.package_geometry.pins[index] is pin


def test_an_smd_pad_made_holed_with_a_size_gets_the_hole(window, monkeypatch):  # noqa: F811
    acquire(window)
    index = _smd_index(window)
    pin = window.package_geometry.pins[index]
    drill = round(min(pin.pad_length_mm, pin.pad_width_mm) * 0.5, 3)
    _drive_fields(monkeypatch, pad_type="thru_hole", drill=drill)
    window._edit_package_pad(index)
    pin = window.package_geometry.pins[index]
    assert pin.pad_type == "thru_hole" and pin.drill_mm == drill and pin.number
