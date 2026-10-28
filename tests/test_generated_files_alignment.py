"""@brief Files on disk aligned with the parameters that produced them (audit 2026-10-15, H1).

After a «No» to the overwrite question (or a blocked automatic generation)
the fields held new parameters, saved in the project, while the .kicad_mod
and the STEP were the old ones. The KiCad tab still said «pronti», the
installation copied the old footprint, the symbol «Applica» wrote pins for
pads the file does not have and «Rigenera 3D» built new leads over the old
pads. Now:

- the footprint is READ BACK from the .kicad_mod and its pads compared with
  the pads the current (applied) fields build; the STEP records the pads it
  was built over (`step_source_pads`, saved with the project);
- while they differ, the KiCad tab says the files are not up to date, the
  isolated preview and both installations (buttons, «Strumenti» entries,
  direct calls and the common sink) are refused with the reason;
- the symbol «Applica» and export compare the rows with the pads of the
  footprint ON DISK and refuse with a message;
- «Rigenera 3D» refuses to build a STEP over a footprint that does not match;
- an unapplied footprint draft never makes the files look stale; an applied
  pad edit waiting for its rewrite does, until the rewrite has run.

Every rule has a positive case (aligned -> allowed) and a negative one
(misaligned -> refused) differing only in the parameter under test.
"""
import re
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox

from kicadgen.core.package_geometry import PackageGeometry, PinSpec
from kicadgen.generators.package import build_package
from kicadgen.generators.pin_header import PinHeaderParams, build_pin_header
from tests.test_photo_inventory_gui import fill_required_metadata, gui

NAME = "HDR"
KICAD_COMMANDS = gui.MainWindow.KICAD_COMMAND_ATTRS


# ── Pure helpers: read-back and comparison of pad records ──────────────

def _header(**overrides):
    values = dict(name="H", rows=2, cols=4, pitch=2.54, row_spacing=2.54,
                  pad_diameter=1.7, drill=1.0)
    values.update(overrides)
    return build_pin_header(PinHeaderParams(**values))


def _write(tmp_path, footprint, name="F.kicad_mod"):
    path = tmp_path / name
    path.write_text(footprint.render(), encoding="utf-8")
    return path


def test_read_back_equals_the_footprint_built_in_memory(tmp_path):
    """Positive: what the generator writes reads back as the same pads (pin
    header and an explicit package with a rotated pad)."""
    header = _header()
    assert not gui._pad_records_differ(
        gui.read_footprint_pad_records(_write(tmp_path, header)), gui.footprint_pad_records(header))
    package = build_package(PackageGeometry(
        name="P", family="soic", body_length_mm=4, body_width_mm=4, pins=[
            PinSpec(number="1", x_mm=-2.5, y_mm=0.635, pad_length_mm=1.5, pad_width_mm=0.6,
                    pad_type="smd", shape="rect", rotation_deg=90),
            PinSpec(number="2", x_mm=2.5, y_mm=0.635, pad_length_mm=1.5, pad_width_mm=0.6,
                    pad_type="smd", shape="rect")]))
    assert not gui._pad_records_differ(
        gui.read_footprint_pad_records(_write(tmp_path, package, "P.kicad_mod")),
        gui.footprint_pad_records(package))


@pytest.mark.parametrize("change", [dict(cols=5), dict(pitch=2.0), dict(row_spacing=7.62),
                                    dict(pad_diameter=1.8), dict(drill=0.9)])
def test_every_pad_relevant_parameter_is_seen(tmp_path, change):
    """Negative: pin count, pitch, row spacing (distinct from the pitch), pad
    size and drill each make the file differ from the fields."""
    written = gui.read_footprint_pad_records(_write(tmp_path, _header()))
    assert gui._pad_records_differ(written, gui.footprint_pad_records(_header(**change)))


def test_pad_order_and_shared_numbers_do_not_matter():
    """Positive: KiCad may save pads in another order; pads sharing a number
    are paired by position, not by list index. Negative: one moved by
    0.01 mm (the smallest spin step) is a difference."""
    records = gui.footprint_pad_records(_header())
    shared = [records[0], records[1][:0] + (records[0][0],) + records[1][1:]]
    assert not gui._pad_records_differ(list(reversed(records)), records)
    assert not gui._pad_records_differ(list(reversed(shared)), shared)
    moved = list(records)
    moved[0] = moved[0][:2] + (moved[0][2] + 0.01,) + moved[0][3:]
    assert gui._pad_records_differ(moved, records)


def test_a_file_without_pads_or_unreadable_says_nothing(tmp_path):
    """No pads or no s-expression: None («cannot tell»), never «aligned»."""
    empty = tmp_path / "e.kicad_mod"
    empty.write_text("(footprint)", encoding="utf-8")
    broken = tmp_path / "b.kicad_mod"
    broken.write_text("(footprint (pad \"1\"", encoding="utf-8")
    assert gui.read_footprint_pad_records(empty) is None
    assert gui.read_footprint_pad_records(broken) is None
    assert gui.read_footprint_pad_records(tmp_path / "missing.kicad_mod") is None


# ── GUI: a pin-header project generated, then its parameters changed ───

class FakeCad3D(QThread):
    """Stands for Cad3DWorker: writes the STEP synchronously and reports it."""
    done = pyqtSignal(str)
    error = pyqtSignal(str)
    progress = pyqtSignal(str)
    warning = pyqtSignal(str)
    cancellable = pyqtSignal(bool)
    phase = pyqtSignal(str, str, int, int)
    created = []

    def __init__(self, params, body, project_dir, fillets, face_texts, single_component=False):
        super().__init__()
        self.params = params
        self.project_dir = project_dir
        FakeCad3D.created.append(self)

    def start(self, *_args):  # noqa: D401 - QThread override in the fake
        step = self.project_dir / f"{self.params.name}.3dshapes" / f"{self.params.name}.step"
        step.parent.mkdir(parents=True, exist_ok=True)
        step.write_bytes(b"ISO-10303-21;\nEND-ISO-10303-21;\n")
        self.done.emit(str(step))
        self.finished.emit()


@pytest.fixture
def dialogs(monkeypatch):
    """Record every dialog and side effect a KiCad command could produce."""
    seen = {"folder": [], "question": [], "warning": [], "info": [], "installed": [],
            "preview": []}
    monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                        lambda *a, **k: seen["folder"].append(a) or "")
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: seen["question"].append(a) or QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: seen["warning"].append(a))
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: seen["info"].append(a))
    monkeypatch.setattr(gui, "install_component_library",
                        lambda *a, **k: seen["installed"].append(k) or SimpleNamespace(
                            identifier="KiCadGen:" + NAME, footprint_path="fp", table_path="t"))
    monkeypatch.setattr(gui, "create_preview_project",
                        lambda *a, **k: seen["preview"].append(a) or SimpleNamespace(board_path="b"))
    monkeypatch.setattr(gui, "launch_preview", lambda *a, **k: None)
    monkeypatch.setattr(gui, "detect_external_applications", lambda: SimpleNamespace(
        pcbnew="pcbnew.exe", kicad_cli="kicad-cli.exe"))
    # CONTRACT CHANGED 2026-10-27 (review K): the global installation resolves
    # the table WITH its KiCad version, to name both before writing.
    monkeypatch.setattr(gui, "resolve_global_fp_lib_table", lambda *_: SimpleNamespace(
        path=None, version="10.0", version_source="kicad-cli"))
    monkeypatch.setattr(gui, "record_kicad_export", lambda **k: True)
    return seen


def _project(window, monkeypatch, tmp_path, cols=4):
    """Pin-header project whose footprint the real generator wrote, plus a STEP.

    Generation runs synchronously (the worker's `run` in the GUI thread);
    the 3D group is off, so the STEP is a placeholder with no record
    («unknown» origin) unless a test regenerates it through FakeCad3D.
    """
    monkeypatch.setattr(gui.GenerateWorker, "start", lambda w: (w.run(), w.finished.emit()))
    monkeypatch.setattr(gui, "Cad3DWorker", FakeCad3D)
    monkeypatch.setattr(window, "_show_generated_mesh", lambda *_: None)
    FakeCad3D.created = []
    window.field_library_root.setText(str(tmp_path))
    window.field_name.setText(NAME)
    window.group_3d.setChecked(False)
    window.field_rows.setValue(2)
    window.field_cols.setValue(cols)
    window.field_pitch.setValue(2.54)
    window.field_row_spacing.setValue(2.54)
    fill_required_metadata(window)
    assert window._generate()
    folder = window._project_dir()
    step = folder / f"{NAME}.3dshapes" / f"{NAME}.step"
    step.parent.mkdir(parents=True, exist_ok=True)
    step.write_bytes(b"ISO-10303-21;\nEND-ISO-10303-21;\n")
    window._refresh_output_actions()
    return folder


def _enabled(window):
    return {attr: getattr(window, attr).isEnabled() for attr in KICAD_COMMANDS}


def _pads_on_disk(folder):
    text = (folder / f"{NAME}.pretty" / f"{NAME}.kicad_mod").read_text(encoding="utf-8")
    return re.findall(r'\(pad "([^"]*)"', text)


def test_aligned_files_leave_every_kicad_command_on(window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    """Positive: freshly generated files, untouched fields."""
    _project(window, monkeypatch, tmp_path)
    assert all(_enabled(window).values()), _enabled(window)
    assert window._kicad_files_alignment_reason() == ""
    assert "pronti" in window.label_kicad_status.text()


def test_changed_parameters_block_preview_and_installs_until_regenerated(
        window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    """Negative: 4 columns on disk, 6 in the fields. Every entry point refuses
    without opening a folder dialog, installing or creating a preview; one
    «Applica ai file» realigns and re-enables (positive)."""
    folder = _project(window, monkeypatch, tmp_path)
    window.field_cols.setValue(6)
    window._refresh_output_actions()
    assert not any(_enabled(window).values()), _enabled(window)
    for attr in KICAD_COMMANDS:
        assert "File non aggiornati" in getattr(window, attr).toolTip(), attr
    status = window.label_kicad_status.text()
    assert "pronti" not in status and "(8 pad)" in status and "(12 pad)" in status
    for command in (window._test_in_kicad, window._install_project_library,
                    window._install_global_library):
        command()
    window._install_kicad_library(tmp_path / "dest", project_library=True)
    assert dialogs["folder"] == [] and dialogs["installed"] == [] and dialogs["preview"] == []
    assert len(dialogs["warning"]) == 4
    assert all("File non aggiornati" in args[2] for args in dialogs["warning"])
    # The remedy named in the message: regenerate the footprint.
    window._apply_metadata_to_files()
    assert len(_pads_on_disk(folder)) == 12
    window._refresh_output_actions()
    assert all(_enabled(window).values()), _enabled(window)


def test_the_menu_entries_are_recomputed_when_the_menu_opens(window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    """A field changed on another tab: the «Strumenti» entries follow at the
    next opening of the menu, without any other refresh."""
    _project(window, monkeypatch, tmp_path)
    assert window.action_export.isEnabled()
    window.field_pitch.setValue(2.0)
    window.menu_tools.aboutToShow.emit()
    assert not window.action_export.isEnabled()
    assert not window.action_test_kicad.isEnabled()
    window.field_pitch.setValue(2.54)
    window.menu_tools.aboutToShow.emit()
    assert window.action_export.isEnabled() and window.action_test_kicad.isEnabled()


def test_a_declined_overwrite_reaches_the_gate(window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    """The real path of the audit: a new analysis whose automatic generation
    the user declines («No» to the overwrite question)."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QListWidgetItem
    from kicadgen.services.datasheet_extractor import ExtractedParams

    def acquired(cols):
        return ExtractedParams(name=NAME, description="Header test", tags="hdr",
                               confidence="high", notes="n", layout="matrix", rows=2, cols=cols,
                               pitch_mm=2.54, row_spacing_mm=7.62, pad_diameter_mm=1.7,
                               drill_mm=1.0, body_length_mm=20, body_width_mm=10,
                               body_height_mm=3)

    def run_extraction(params):
        window.combo_photo_detector.setCurrentIndex(window.combo_photo_detector.findData("ai"))
        if not window.docs_list.count():
            row = QListWidgetItem("photo.png")
            row.setCheckState(Qt.CheckState.Checked)
            window.docs_list.addItem(row)
        window._extract_with_ai()
        window._freecad_available = False
        window._on_extracted(params)
        window._on_extract_finished()

    answers = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: (
        answers.pop(0) if answers else QMessageBox.StandardButton.Yes))
    monkeypatch.setattr(gui.ExtractWorker, "start", lambda self: None)
    monkeypatch.setattr(gui.GenerateWorker, "start", lambda w: (w.run(), w.finished.emit()))
    window.field_name.setText("")
    window.field_library_root.setText(str(tmp_path))
    window._switch_project(NAME, window._general_project_state)
    window.field_datasheet.setText("https://example.org/x.pdf")
    run_extraction(acquired(4))
    folder = window._project_dir()
    (folder / f"{NAME}.3dshapes").mkdir(exist_ok=True)
    (folder / f"{NAME}.3dshapes" / f"{NAME}.step").write_bytes(b"ISO-10303-21;\nEND-ISO-10303-21;\n")
    window._refresh_output_actions()
    assert window._kicad_files_alignment_reason() == ""            # positive
    answers.append(QMessageBox.StandardButton.No)
    run_extraction(acquired(6))
    assert window.field_cols.value() == 6 and len(_pads_on_disk(folder)) == 8
    window._refresh_output_actions()
    assert "File non aggiornati" in window.label_kicad_status.text()  # negative
    assert not window.btn_export.isEnabled() and not window.btn_test_kicad.isEnabled()


# ── Symbol against the footprint on disk ───────────────────────────────

def test_symbol_apply_is_refused_against_the_footprint_on_disk(
        window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    """Negative: 12 rows (fields) against 8 pads (file): «Applica» keeps the
    draft, says why, and no .kicad_sym with 12 pins appears. Positive: after
    the footprint is regenerated, the same edit applies."""
    folder = _project(window, monkeypatch, tmp_path)
    window.field_cols.setValue(6)
    window.tabs.setCurrentIndex(gui.TAB_SYMBOL)
    tab = window.symbol_tab
    tab.table.item(0, 1).setText("VCC")
    tab.apply()
    assert tab.has_pending_edits()
    assert "footprint su disco" in tab.status.text()
    symbol = folder / f"{NAME}.kicad_sym"
    assert not symbol.exists() or len(re.findall(r'\(number "', symbol.read_text(
        encoding="utf-8"))) != 12
    assert "footprint su disco" in window._symbol_pad_mismatch()
    window._apply_metadata_to_files()
    assert window._symbol_pad_mismatch() == ""
    tab.apply()
    assert not tab.has_pending_edits()
    assert len(re.findall(r'\(number "', symbol.read_text(encoding="utf-8"))) == 12


# ── STEP: «Rigenera 3D» and the recorded source pads ───────────────────

def _enable_3d(window):
    window.group_3d.setChecked(True)
    window.field_body_length.setValue(10.0)
    window.field_body_width.setValue(5.0)
    window.field_body_height.setValue(3.0)
    # Documented body sizes (audit item 4: the placeholder never builds a solid).
    window._body_size_declared = True
    # The body now has an outline (F.Fab, courtyard): the footprint written
    # without it is rewritten, as a generation with this body would write it
    # (review pass 2, L1 compares the outline too).
    assert window._generate(footprint_only=True)


def test_regenerate_3d_is_refused_over_a_stale_footprint(window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    """Negative: a STEP from the fields would put 12 leads over 8 pads: no
    worker is created and the user is told. Positive: aligned, it runs and
    records the pads it was built over."""
    _project(window, monkeypatch, tmp_path)
    _enable_3d(window)
    window.field_cols.setValue(6)
    window._regenerate_3d()
    assert FakeCad3D.created == []
    assert dialogs["info"] and "Rigenera prima il footprint" in dialogs["info"][-1][2]
    window.field_cols.setValue(4)
    window._regenerate_3d()
    assert len(FakeCad3D.created) == 1
    assert window._step_source_pads is not None and len(window._step_source_pads) == 8


def test_a_step_built_over_other_pads_blocks_until_the_3d_is_regenerated(
        window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    """«Applica ai file» rewrites the footprint only: with a STEP recorded
    over the old pads the commands stay off with the STEP reason (negative),
    and a «Rigenera 3D» realigns them (positive)."""
    _project(window, monkeypatch, tmp_path)
    _enable_3d(window)
    window._regenerate_3d()
    window._refresh_output_actions()
    assert all(_enabled(window).values())
    window.field_cols.setValue(6)
    window._apply_metadata_to_files()
    window._refresh_output_actions()
    assert window._footprint_alignment_reason() == ""
    assert not any(_enabled(window).values())
    assert "Modello STEP non aggiornato" in window.btn_export.toolTip()
    window._regenerate_3d()
    window._refresh_output_actions()
    assert all(_enabled(window).values()), _enabled(window)


def test_the_step_record_is_saved_and_an_unknown_one_never_blocks(
        window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    """The record survives the project file; a project without it (saved
    before the record existed) or with a damaged one reads «unknown», which
    does not block (no FreeCAD run forced on old projects)."""
    _project(window, monkeypatch, tmp_path)
    _enable_3d(window)
    window._regenerate_3d()
    state = window._project_state()
    assert len(state["step_source_pads"]) == 8
    assert gui._valid_pad_records(state["step_source_pads"]) == window._step_source_pads
    for damaged in (None, [], [["1"]], "x", [[1, "smd", 0, 0, 0, 1, 1, [], [0, 0]]]):
        assert gui._valid_pad_records(damaged) is None
    window.field_cols.setValue(6)
    window._apply_metadata_to_files()
    window._step_source_pads = None                       # an old project
    window._refresh_output_actions()
    assert all(_enabled(window).values()), _enabled(window)


def test_an_old_project_is_judged_on_the_file_it_really_holds(window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    """No record is needed for the footprint: reopening a project whose file
    has 8 pads and whose saved fields say 12 is blocked (negative); the same
    project with matching fields is not (positive)."""
    folder = _project(window, monkeypatch, tmp_path)
    window.field_cols.setValue(6)
    assert window._save_project_config(force=True)
    window._switch_project("Another", window._general_project_state)
    window._open_project_at(folder)
    window._refresh_output_actions()
    assert window.field_cols.value() == 6
    assert not window.btn_export.isEnabled()
    window.field_cols.setValue(4)
    window._refresh_output_actions()
    assert window.btn_export.isEnabled()


# ── Footprint drafts keep working ──────────────────────────────────────

def test_a_pending_footprint_draft_never_makes_the_files_stale(window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    """Positive: an unapplied pad edit is not in the files and is not
    compared (the KiCad tab keeps its own «modifiche non applicate» note).
    Negative: once applied and before its rewrite runs the file lags and the
    commands wait; the rewrite realigns them."""
    _project(window, monkeypatch, tmp_path)
    window._edit_footprint_draft({"pad_overrides": {"1": {"shape": "rect", "size": 2.0}}})
    assert window.has_pending_footprint_edits()
    window._refresh_output_actions()
    assert window._kicad_files_alignment_reason() == ""
    assert window.btn_export.isEnabled()
    monkeypatch.setattr(window, "_retry_footprint_rewrite", lambda: None)
    window.apply_footprint_draft()
    window._refresh_output_actions()
    assert "File non aggiornati" in window.btn_export.toolTip()
    window._flush_footprint_rewrite()
    QApplication.processEvents()
    window._refresh_output_actions()
    assert window.btn_export.isEnabled(), window.btn_export.toolTip()
