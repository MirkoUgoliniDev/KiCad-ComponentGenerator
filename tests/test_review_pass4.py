"""@brief Review pass 4 residuals (2026-10-15): keystroke cost on a big matrix and six gate details.

MEDIUM: a metadata keystroke on a 900-pad pin matrix rebuilt the footprint
    (cache keyed on repr(params), which carries the metadata) and paid the
    O(n²) clearance check: 316-524 ms per character. Now the key holds the
    geometric fields only and the check is O(n): 3-4 ms measured.
a: «Aggiorna il file del simbolo» refuses while work runs.
b: its disabled tooltip names the pad mismatch when that is the cause.
c: a footprint block names the KLC field to fix when «Applica ai file»
   would refuse.
d: the overwrite question lists the .kicad_sym when a draft of other pins
   is displaced and the file rewritten.
e: a project reopened with a kind chosen (pcb thickness, pins choice added
   by the kind) is not a stale STEP.
f: the status label says «busy» first, like the buttons.
"""
import time

from PyQt6.QtWidgets import QMessageBox

from kicadgen.gui import main_window as gui
from tests.test_generated_files_alignment import FakeCad3D, dialogs  # noqa: F401
from tests.test_kicad_gate_remedies import (NAME, _pinout, _ready, _setup_busy, _setup_symbol_text,
                                            _write_step)
from tests.test_photo_inventory_gui import fill_required_metadata
from tests.ui_flow_helpers import header_extraction, prepare, run_extraction


# ── MEDIUM ─────────────────────────────────────────────────────────────────

def _big_matrix(window, monkeypatch, tmp_path):
    monkeypatch.setattr(gui.GenerateWorker, "start", lambda w: (w.run(), w.finished.emit()))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    window.field_library_root.setText(str(tmp_path))
    window.field_name.setText("BIG")
    window.group_3d.setChecked(False)
    for field, value in ((window.field_rows, 30), (window.field_cols, 30),
                         (window.field_pitch, 1.0), (window.field_row_spacing, 1.0),
                         (window.field_pad, 0.7), (window.field_drill, 0.4)):
        field.setValue(value)
    fill_required_metadata(window)
    assert window._generate()
    _write_step(window._project_dir(), "BIG")
    window._refresh_output_actions()
    window._refresh_output_actions()


def test_a_metadata_keystroke_never_rebuilds_a_big_matrix(window, monkeypatch, tmp_path):  # noqa: F811
    """Negative of the review: no footprint built for a keystroke; positive:
    a pitch change still rebuilds once. Bound generous for CI (measured
    3-4 ms on the probe, see `_geometry_key`)."""
    _big_matrix(window, monkeypatch, tmp_path)
    builds = []
    real_build = gui.MainWindow._build_footprint
    monkeypatch.setattr(gui.MainWindow, "_build_footprint",
                        staticmethod(lambda p: builds.append(1) or real_build(p)))
    start = time.perf_counter()
    for index in range(5):
        window.field_description.setText(f"descr {index}")
        window.field_meta_keywords.setText(f"kw {index}")
    elapsed = (time.perf_counter() - start) / 10
    assert builds == []
    assert elapsed < 0.05, elapsed
    window.field_pitch.setValue(1.2)
    window._refresh_output_actions()
    assert builds == [1] and window._kicad_block_code() == "footprint_pads"


def test_the_geometry_key_ignores_the_metadata_only():
    from kicadgen.generators.pin_header import PinHeaderParams
    base = dict(name="A", rows=2, cols=4, pitch=2.54, pad_diameter=1.7, drill=1.0)
    key = gui._geometry_key(PinHeaderParams(**base))
    assert gui._geometry_key(PinHeaderParams(**dict(base, name="B"), description="x", tags="y",
                                             datasheet="z")) == key
    assert gui._geometry_key(PinHeaderParams(**dict(base, pitch=2.0))) != key
    assert gui._geometry_key(PinHeaderParams(**dict(base, pad_overrides={"1": {"size": 2}}))) != key


# ── a ──────────────────────────────────────────────────────────────────────

def test_update_symbol_file_refuses_while_busy(window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    folder = _ready(window, monkeypatch, tmp_path)
    _setup_symbol_text(window, monkeypatch, folder)
    _setup_busy(window, monkeypatch, folder)
    before = (folder / f"{NAME}.kicad_sym").read_text(encoding="utf-8")
    infos = []                      # `prepare` silenced the information boxes
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: infos.append(a[2]))
    window._update_symbol_file()
    assert (folder / f"{NAME}.kicad_sym").read_text(encoding="utf-8") == before
    assert infos and "lavoro in corso" in infos[-1]


# ── b ──────────────────────────────────────────────────────────────────────

def test_the_disabled_update_button_names_a_pad_mismatch(window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    folder = _ready(window, monkeypatch, tmp_path)
    _setup_symbol_text(window, monkeypatch, folder)
    monkeypatch.setattr(window, "_symbol_pad_mismatch", lambda: "righe diverse")
    window._refresh_output_actions()
    assert not window.btn_update_symbol_file.isEnabled()
    assert "non corrispondono ai pad" in window.btn_update_symbol_file.toolTip()
    assert "corrisponde al simbolo applicato" not in window.btn_update_symbol_file.toolTip()


# ── c ──────────────────────────────────────────────────────────────────────

def test_a_footprint_block_names_the_field_applica_would_refuse(window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    _ready(window, monkeypatch, tmp_path, with_body=False)
    window.field_cols.setValue(2)
    window._refresh_output_actions()
    assert "rifiuterebbe" not in window._kicad_command_block_reason()       # positive
    window.field_reference.setText("U1")                                     # digits: refused
    window._refresh_output_actions()
    errors = window._write_blocking_klc_errors()
    assert errors
    reason = window._kicad_command_block_reason()
    assert "rifiuterebbe" in reason and errors[0].message in reason


# ── d ──────────────────────────────────────────────────────────────────────

def test_the_overwrite_question_lists_the_symbol_a_displaced_draft_lets_be_rewritten(window, monkeypatch, tmp_path):  # noqa: F811
    asked = prepare(window, monkeypatch, tmp_path, NAME)
    run_extraction(window, header_extraction(cols=4))
    folder = window._project_dir()
    window.tabs.setCurrentIndex(gui.TAB_SYMBOL)
    window.symbol_tab.table.item(0, 1).setText("DRAFT")
    window.tabs.setCurrentIndex(0)
    asked.clear()
    run_extraction(window, header_extraction(cols=2))                        # other pins
    assert any(f"{NAME}.kicad_sym" in text for text in asked)
    assert not window.symbol_tab.has_pending_edits()                         # displaced
    assert '(number "8"' not in (folder / f"{NAME}.kicad_sym").read_text(encoding="utf-8")


# ── e ──────────────────────────────────────────────────────────────────────

def test_a_reopened_project_with_a_kind_chosen_is_not_a_stale_step(window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    folder = _ready(window, monkeypatch, tmp_path)
    assert window._step_source_body["body"]["pcb_thickness_mm"] == 1.6
    assert "show_pins_3d" not in window._step_source_body["body"]
    window._flush_project_config()
    window._switch_project("Other", window._general_project_state)
    window._open_project_at(folder)
    window._refresh_output_actions()
    assert window._kicad_block_code() == "", window._kicad_command_block_reason()
    # A record written before the normalisation compares the same way.
    window._step_source_body["body"]["pcb_thickness_mm"] = None
    window._step_source_body["body"]["show_pins_3d"] = True
    window._refresh_output_actions()
    assert window._kicad_block_code() == ""
    # Negative: a real body change is still seen.
    window.field_body_height.setValue(window.field_body_height.value() + 1.0)
    window._refresh_output_actions()
    assert window._kicad_block_code() == "step_body"


# ── f ──────────────────────────────────────────────────────────────────────

def test_the_status_label_says_busy_first(window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    folder = _ready(window, monkeypatch, tmp_path)
    _setup_symbol_text(window, monkeypatch, folder)
    _setup_busy(window, monkeypatch, folder)
    window._refresh_output_actions()
    assert window.label_kicad_status.text().startswith("Comando non disponibile mentre")
    monkeypatch.setattr(window, "_terminal_worker", None)
    window._refresh_output_actions()
    assert window.label_kicad_status.text().startswith("Simbolo non aggiornato")
