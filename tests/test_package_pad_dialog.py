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
    assert seen["drill"].minimum() == 0.05 and seen["drill"].maximum() == 20.0
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


def test_an_smd_pad_has_no_drill_field(window, monkeypatch):  # noqa: F811
    acquire(window)
    monkeypatch.setattr(gui.GenerateWorker, "start", lambda worker: worker.run())
    index = _smd_index(window)
    seen = {}
    _drive(monkeypatch, seen=seen)
    window._edit_package_pad(index)
    assert seen["drill"] is None and "Foro:" not in seen["labels"]
    assert window.package_geometry.pins[index].drill_mm is None


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
