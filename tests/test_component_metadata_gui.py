"""@brief «Metadati del componente» in the KiCad tab (user, 2026-10-10).

Designator, description, keywords, datasheet and footprint filters of the
symbol and footprint; description/keywords mirrored with the project data;
live KLC findings; confirmation before installing with KLC errors; the
metadata in the project file. Positive and negative cases for every rule.
"""
from types import SimpleNamespace

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QMessageBox

from kicadgen.gui.project_state import PROJECT_CONFIG_NAME
from kicadgen.services import klc_checks
from tests.test_kicad_tab_gate import _outputs
from tests.test_package_project_gui import acquire
from tests.test_photo_inventory_gui import gui

KICAD = gui.MainWindow.KICAD_TAB_INDEX


def _finding(code, severity, message):
    return klc_checks.Finding(code, severity, message)


def _ready(window, tmp_path, name="SCHEDA"):
    """Project with footprint and STEP on disk: the KiCad tab and the group are on."""
    window.field_library_root.setText(str(tmp_path))
    _outputs(tmp_path, name)
    window.field_name.setText(name)
    return window._project_dir()


# ── Designator proposal ──────────────────────────────────────────────────

def test_the_proposal_table_is_keyed_by_declared_families():
    """Positive: a declared IC package or a board module gives U. Negative: an
    unknown family gets the generic default, never a guess from a name."""
    assert gui.proposed_reference("aggregate_castellated") == "U"
    assert gui.proposed_reference("single_smd", "soic") == "U"
    assert gui.proposed_reference("", None) == gui.DEFAULT_REFERENCE
    assert gui.proposed_reference("not-a-family", "not-a-package") == gui.DEFAULT_REFERENCE
    assert all(value.isalpha() and value.isupper()
               for value in list(gui.REFERENCE_BY_TYPE_FAMILY.values())
               + list(gui.REFERENCE_BY_PACKAGE_FAMILY.values()))


def test_an_empty_designator_follows_the_proposal_and_a_typed_one_wins(window, tmp_path):  # noqa: F811
    _ready(window, tmp_path)
    assert window.field_reference.text() == ""
    assert window.field_reference.placeholderText() == "U"
    assert window._effective_reference() == "U"
    window.field_reference.setText("K")
    assert window._effective_reference() == "K"
    assert window._metadata_dict()["reference"] == "K"


def test_the_designator_is_letters_only_and_upper_case(window, tmp_path):  # noqa: F811
    _ready(window, tmp_path)
    window.field_reference.clear()
    QTest.keyClicks(window.field_reference, "rv")
    assert window.field_reference.text() == "RV"
    QTest.keyClicks(window.field_reference, "1?")
    assert window.field_reference.text() == "RV"


# ── Footprint filters ────────────────────────────────────────────────────

def test_footprint_filters_follow_the_footprint_name_until_typed(window, tmp_path):  # noqa: F811
    _ready(window, tmp_path, "MODULO")
    assert window.field_fp_filters.placeholderText() == "MODULO*"
    assert window._effective_fp_filters() == "MODULO*"
    window.field_fp_filters.setText("Altro* MODULO?")
    assert window._effective_fp_filters() == "Altro* MODULO?"


# ── Mirroring with the project data ──────────────────────────────────────

def test_description_and_keywords_mirror_the_project_fields_both_ways(window):  # noqa: F811
    notified = []
    window.field_description.on_change(notified.append)
    window.field_description.setText("Modulo radio")
    assert window.field_meta_description.text() == "Modulo radio"
    window.field_meta_keywords.setText("radio modulo")
    assert window.field_tags.text() == "radio modulo"
    # Typing in the editor changes the datum exactly once: no echo loop.
    window.field_meta_description.setText("Modulo radio 2")
    assert window.field_description.text() == "Modulo radio 2"
    assert notified == ["Modulo radio", "Modulo radio 2"]
    assert window._metadata_dict()["description"] == "Modulo radio 2"
    assert window._metadata_dict()["keywords"] == "radio modulo"


# ── KLC list ─────────────────────────────────────────────────────────────

def _items(window):
    return [window.klc_list.item(i).text() for i in range(window.klc_list.count())]


def test_the_klc_list_shows_errors_then_warnings_with_their_marks(window, tmp_path, monkeypatch):  # noqa: F811
    _ready(window, tmp_path)
    seen = {}

    def fake_check_all(metadata, footprint_name, symbol_text=None, footprint_text=None):
        seen.update(metadata=dict(metadata), name=footprint_name,
                    symbol=symbol_text, footprint=footprint_text)
        return [_finding("reference_letters", "error", "Designatore errato"),
                _finding("datasheet_empty", "warning", "Datasheet vuoto")]
    monkeypatch.setattr(klc_checks, "check_all", fake_check_all)
    window.field_datasheet.setText("https://example.org/ds.pdf")
    assert _items(window) == ["⛔ Designatore errato", "⚠ Datasheet vuoto"]
    assert seen["name"] == "SCHEDA" and seen["metadata"]["datasheet"] == "https://example.org/ds.pdf"
    # The footprint written by `_outputs` is read; no symbol exists yet.
    assert seen["footprint"] == "(footprint)" and seen["symbol"] is None


def test_without_findings_the_list_says_so(window, tmp_path, monkeypatch):  # noqa: F811
    _ready(window, tmp_path)
    monkeypatch.setattr(klc_checks, "check_all", lambda *a, **k: [])
    window.field_reference.setText("U")
    assert _items(window) == ["Nessuna osservazione."]


def test_the_real_rules_see_the_window_data(window, tmp_path):  # noqa: F811
    """Positive/negative on the real checker: a bad designator is an error, an
    empty datasheet only a warning."""
    _ready(window, tmp_path)
    window.field_datasheet.setText("")
    window.field_description.setText("Modulo")
    window.field_tags.setText("modulo")
    codes = {f.code: f.severity for f in window._klc_findings()}
    assert codes.get("datasheet_empty") == "warning"
    assert "reference_empty" not in codes  # the proposal counts
    window.field_datasheet.setText("con spazio.pdf")
    codes = {f.code: f.severity for f in window._klc_findings()}
    assert codes.get("datasheet_blanks") == "warning", "a Windows path with blanks is advice, not a refusal"
    window.field_reference.setText("")
    window.field_reference.setPlaceholderText("")


# ── Installation gate ────────────────────────────────────────────────────

def _install(window, tmp_path, monkeypatch):
    installed = []
    monkeypatch.setattr(gui, "install_component_library", lambda *a, **k: installed.append(k) or
                        SimpleNamespace(identifier="KiCadGen:SCHEDA", footprint_path=tmp_path,
                                        table_path=tmp_path))
    monkeypatch.setattr(gui, "record_kicad_export", lambda **k: True)
    artifacts = SimpleNamespace(name="SCHEDA", component_dir=tmp_path / "SCHEDA")
    window._install_kicad_library(tmp_path / "dest", project_library=True, artifacts=artifacts)
    return installed


def test_klc_errors_ask_before_installing_and_no_refuses(window, tmp_path, monkeypatch):  # noqa: F811
    _ready(window, tmp_path)
    monkeypatch.setattr(klc_checks, "check_all",
                        lambda *a, **k: [_finding("x", "error", "Errore grave")])
    asked = []
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: asked.append(a[2]) or QMessageBox.StandardButton.No)
    assert _install(window, tmp_path, monkeypatch) == []
    assert asked and "Errore grave" in asked[0]


def test_klc_errors_confirmed_install_and_warnings_never_ask(window, tmp_path, monkeypatch):  # noqa: F811
    _ready(window, tmp_path)
    monkeypatch.setattr(klc_checks, "check_all",
                        lambda *a, **k: [_finding("x", "error", "Errore grave")])
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    assert len(_install(window, tmp_path, monkeypatch)) == 1
    monkeypatch.setattr(klc_checks, "check_all",
                        lambda *a, **k: [_finding("y", "warning", "Solo avviso")])

    def never(*args, **kwargs):
        raise AssertionError("a warning must not ask")
    monkeypatch.setattr(QMessageBox, "question", never)
    assert len(_install(window, tmp_path, monkeypatch)) == 1


# ── Group gate ───────────────────────────────────────────────────────────

def test_the_group_is_enabled_only_with_a_footprint(window, tmp_path):  # noqa: F811
    window.field_library_root.setText(str(tmp_path))
    window.field_name.setText("VUOTO")
    assert not window.group_metadata.isEnabled()
    _outputs(tmp_path, "VUOTO", step=False)
    window._refresh_output_actions()
    assert window.group_metadata.isEnabled()


def test_the_group_starts_collapsed_and_its_title_opens_it(window, tmp_path):  # noqa: F811
    """@brief User, 2026-10-11: collapsed by default; the title on the frame,
    like the other blocks, carries the arrow and a click on it toggles."""
    from PyQt6.QtCore import QPoint
    from kicadgen.gui.collapsible_group import ARROW_COLLAPSED, ARROW_EXPANDED
    _ready(window, tmp_path)
    group = window.group_metadata
    assert window.metadata_body.isHidden() and not group.isExpanded()
    assert group.title().startswith(ARROW_COLLAPSED)
    assert group.baseTitle() == window.i18n.text("Metadati del componente")
    group.setExpanded(True)
    assert not window.metadata_body.isHidden()
    assert group.title().startswith(ARROW_EXPANDED)
    # A click on the title toggles; one on the body does not.
    group.show()
    title_centre = group._title_rect().center()
    QTest.mouseClick(group, Qt.MouseButton.LeftButton, pos=title_centre)
    assert not group.isExpanded()
    QTest.mouseClick(group, Qt.MouseButton.LeftButton, pos=QPoint(group.width() // 2, group.height() - 2))
    assert not group.isExpanded()
    # The hidden fields keep mirroring the project data.
    window.field_meta_description.setText("Nascosto ma vivo")
    assert window.field_description.text() == "Nascosto ma vivo"


def test_the_translated_title_keeps_the_arrow(window, tmp_path):  # noqa: F811
    """@brief The i18n pass rewrites the title: the arrow must survive and the
    source must stay the bare string (a decorated title must never be re-read as source)."""
    from kicadgen.gui.collapsible_group import ARROW_COLLAPSED
    _ready(window, tmp_path)
    group = window.group_metadata
    window.i18n.retranslate_widgets(window)
    assert group.title() == f"{ARROW_COLLAPSED} " + window.i18n.text("Metadati del componente")
    assert group.property("i18n_title_source") == "Metadati del componente"


# ── Project file ─────────────────────────────────────────────────────────

def test_the_metadata_are_saved_typed_only_and_come_back(window, tmp_path):  # noqa: F811
    _ready(window, tmp_path)
    window.field_reference.setText("J")
    window.field_datasheet.setText("https://example.org/ds.pdf")
    window.field_fp_filters.setText("")
    state = window._project_state()
    assert state["metadata"] == {"reference": "J", "datasheet": "https://example.org/ds.pdf",
                                 "fp_filters": ""}
    window._switch_project("ALTRO", window._general_project_state)
    assert window.field_reference.text() == "" and window.field_datasheet.text() == ""
    window._switch_project("SCHEDA", state)
    assert window.field_reference.text() == "J"
    assert window.field_datasheet.text() == "https://example.org/ds.pdf"
    assert window.field_fp_filters.text() == "" and window._effective_fp_filters() == "SCHEDA*"


def test_an_old_project_without_the_key_opens_clean_and_is_not_dirty(window, tmp_path):  # noqa: F811
    _ready(window, tmp_path)
    window.field_reference.setText("K")
    old = window._project_state()
    del old["metadata"]
    window._switch_project("SCHEDA", old)
    assert window.field_reference.text() == "" and window._effective_reference() == "U"
    assert window._save_project_config() is False  # nothing changed since the open


def test_a_corrupt_metadata_value_is_ignored(window, tmp_path):  # noqa: F811
    _ready(window, tmp_path)
    state = window._project_state()
    state["metadata"] = {"reference": 12, "datasheet": None, "fp_filters": ["x"]}
    window._switch_project("SCHEDA", state)
    assert window._metadata_state() == {"reference": "", "datasheet": "", "fp_filters": ""}
    state["metadata"] = "junk"
    window._switch_project("SCHEDA", state)
    assert window._metadata_state() == {"reference": "", "datasheet": "", "fp_filters": ""}
    state["metadata"] = {"reference": "U1", "datasheet": "", "fp_filters": ""}
    window._switch_project("SCHEDA", state)
    assert window._metadata_state()["reference"] == "", "a hand-edited designator with digits is dropped"
    state["metadata"] = {"reference": "K", "datasheet": "", "fp_filters": ""}
    window._switch_project("SCHEDA", state)
    assert window._metadata_state()["reference"] == "K"


# ── Apply to files ───────────────────────────────────────────────────────

def test_apply_regenerates_footprint_and_symbol_with_the_datasheet(window, monkeypatch):  # noqa: F811
    acquire(window)
    monkeypatch.setattr(gui.GenerateWorker, "start", lambda worker: worker.run())
    folder = window._project_dir()
    footprint = folder / f"{folder.name}.pretty" / f"{folder.name}.kicad_mod"
    symbol = folder / f"{folder.name}.kicad_sym"
    window._generate(footprint_only=True)
    assert footprint.is_file() and symbol.is_file()
    assert "example.org" not in footprint.read_text(encoding="utf-8")
    assert "example.org" not in symbol.read_text(encoding="utf-8")
    window.field_datasheet.setText("https://example.org/ds.pdf")
    window.field_reference.setText("Q")
    window.btn_apply_metadata.click()
    # An explicit package writes its own generic descr (generator contract);
    # the datasheet is the project's and must reach both files.
    assert '(property "Datasheet" "https://example.org/ds.pdf"' in footprint.read_text(encoding="utf-8")
    text = symbol.read_text(encoding="utf-8")
    assert '"https://example.org/ds.pdf"' in text and '(property "Reference" "Q"' in text
    saved = (folder / PROJECT_CONFIG_NAME).read_text(encoding="utf-8")
    assert "example.org/ds.pdf" in saved


def test_apply_without_a_generated_footprint_writes_nothing(window, monkeypatch):  # noqa: F811
    """Negative: the group is a gate on the footprint; a refused generation leaves no file."""
    acquire(window)
    window._acquisition_completed = False
    started = []
    monkeypatch.setattr(gui.GenerateWorker, "start", lambda worker: started.append(worker))
    folder = window._project_dir()
    window.btn_apply_metadata.click()
    assert started == []
    assert not (folder / f"{folder.name}.kicad_sym").exists()


def test_the_symbol_export_carries_the_metadata(window, monkeypatch):  # noqa: F811
    from kicadgen.generators import symbol as symbol_module
    acquire(window)
    window.field_reference.setText("Q")
    window.field_datasheet.setText("https://example.org/ds.pdf")
    window.field_description.setText("Transistor")
    window.field_tags.setText("npn")
    calls = []
    monkeypatch.setattr(symbol_module, "export_symbol",
                        lambda path, name, pins, **kwargs: calls.append(kwargs) or path)
    window._refresh_symbol_tab()  # rows in step with the pads, as after a generation
    assert window._save_applied_electrical_symbol(quiet=True) is not None
    metadata = calls[0]["metadata"]
    assert isinstance(metadata, symbol_module.SymbolMetadata)
    assert (metadata.reference, metadata.datasheet, metadata.description, metadata.keywords,
            metadata.fp_filters) == ("Q", "https://example.org/ds.pdf", "Transistor", "npn",
                                     f"{window._component_name()}*")


def test_the_footprint_params_carry_description_keywords_and_datasheet(window):  # noqa: F811
    """The matrix and castellated params read the same fields the KiCad tab edits."""
    window.field_meta_description.setText("Modulo radio")
    window.field_meta_keywords.setText("radio")
    window.field_datasheet.setText("https://example.org/ds.pdf")
    params = window._current_params()
    assert (params.description, params.tags, params.datasheet) == (
        "Modulo radio", "radio", "https://example.org/ds.pdf")
    castellated = window._current_castellated_params()
    assert (castellated.description, castellated.tags, castellated.datasheet) == (
        "Modulo radio", "radio", "https://example.org/ds.pdf")


def test_apply_is_refused_while_work_runs_and_with_klc_errors(window, monkeypatch):  # noqa: F811
    """Review 2026-10-10 (4): no «in rigenerazione» log when nothing ran; errors stop the write."""
    acquire(window)
    monkeypatch.setattr(gui.GenerateWorker, "start", lambda worker: worker.run())
    window._generate(footprint_only=True)   # the group is a gate on the footprint
    started = []
    monkeypatch.setattr(gui.GenerateWorker, "start", lambda worker: started.append(worker))
    shown = []
    monkeypatch.setattr(gui.QMessageBox, "information", lambda *a: shown.append(("info", a[2])))
    monkeypatch.setattr(gui.QMessageBox, "warning", lambda *a: shown.append(("warn", a[2])))
    monkeypatch.setattr(window, "_background_work_running", lambda: True)
    window.btn_apply_metadata.click()
    assert started == [] and shown and shown[-1][0] == "info"
    monkeypatch.setattr(window, "_background_work_running", lambda: False)
    window.field_reference.setText("")
    window.field_reference.setPlaceholderText("")
    monkeypatch.setattr(window, "_effective_reference", lambda: "")
    window.btn_apply_metadata.click()
    assert started == [] and shown[-1][0] == "warn"


def test_a_datasheet_path_with_blanks_reaches_both_files(window, monkeypatch):  # noqa: F811
    """Review 2026-10-10 (1): a Windows path with blanks must not leave the symbol stale."""
    acquire(window)
    monkeypatch.setattr(gui.GenerateWorker, "start", lambda worker: worker.run())
    window._generate(footprint_only=True)
    window.field_datasheet.setText("C:/Documenti del progetto/ds.pdf")
    window.field_reference.setText("K")
    window.btn_apply_metadata.click()
    folder = window._project_dir()
    name = folder.name
    footprint = (folder / f"{name}.pretty" / f"{name}.kicad_mod").read_text(encoding="utf-8")
    assert "C:/Documenti del progetto/ds.pdf" in footprint
    symbol = (folder / f"{name}.kicad_sym").read_text(encoding="utf-8")
    assert "C:/Documenti del progetto/ds.pdf" in symbol
