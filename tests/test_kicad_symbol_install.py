"""@brief The installation brings the symbol too (user decision 2026-11-15, «opzione 1»).

Before this decision the project and global installations copied only the
footprint and the STEP model: the symbol had to be exported by hand and added
in KiCad. Now the component's symbol goes into the shared
``<destination>/KiCadGen.kicad_sym`` registered in the ``sym-lib-table`` next
to the footprint table, with the footprint's confirmations, conflicts,
backups and rollback. Every rule has a twin case it must tell apart for a
structural reason (symbol present or absent, same name or another, table
existing or not), never by a number.
"""

from pathlib import Path

import pytest

from kicadgen.services import kicad_library
from kicadgen.services.kicad_library import (
    KiCadLibraryError,
    discover_component_artifacts,
    install_component_library,
    merged_symbol_library_text,
    plan_component_install,
)


FOOTPRINT = """(footprint "{name}"
\t(version 20260206)
\t(generator "kicadgen_tool")
\t(layer "F.Cu")
\t(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask"))
)
"""

#: Smallest STEP accepted by discover_component_artifacts (ISO trailer present).
STEP_BYTES = b"ISO-10303-21;\nEND-ISO-10303-21;\n"


def _symbol_library(name: str, *, version: int = 20251024, pin_name: str = "A") -> str:
    """A one-symbol library as the generator writes it (units named after the symbol)."""
    return (
        f'(kicad_symbol_lib (version {version}) (generator "kicadgen") (generator_version "10.0.1")\n'
        f'  (symbol "{name}"\n'
        f'    (property "Reference" "U" (at 0 2.54 0) (effects (font (size 1.27 1.27))))\n'
        f'    (property "Value" "{name}" (at 0 0 0) (effects (font (size 1.27 1.27))))\n'
        f'    (property "Footprint" "KiCadGen:{name}" (at 0 0 0) (hide yes) (effects (font (size 1.27 1.27))))\n'
        f'    (symbol "{name}_0_1"\n'
        f'      (rectangle (start -2.54 2.54) (end 2.54 -2.54)'
        f' (stroke (width 0.254) (type default)) (fill (type background))))\n'
        f'    (symbol "{name}_1_1"\n'
        f'      (pin passive line (at -5.08 0 0) (length 2.54)\n'
        f'        (name "{pin_name}" (effects (font (size 1.27 1.27))))\n'
        f'        (number "1" (effects (font (size 1.27 1.27))))))\n'
        f'  )\n'
        f')\n'
    )


def _component(tmp_path: Path, name: str = "Demo", *, symbol: str | None = "default",
               step: bool = True) -> Path:
    """Write a generated component folder: footprint, STEP and (optionally) its symbol."""
    component = tmp_path / "components" / name
    (component / f"{name}.pretty").mkdir(parents=True, exist_ok=True)
    (component / f"{name}.pretty" / f"{name}.kicad_mod").write_text(
        FOOTPRINT.format(name=name), encoding="utf-8")
    if step:
        (component / f"{name}.3dshapes").mkdir(exist_ok=True)
        (component / f"{name}.3dshapes" / f"{name}.step").write_bytes(STEP_BYTES)
    if symbol is not None:
        text = _symbol_library(name) if symbol == "default" else symbol
        (component / f"{name}.kicad_sym").write_text(text, encoding="utf-8")
    return component


def _artifacts(component: Path):
    """The artifacts as the GUI discovers them."""
    return discover_component_artifacts(component, component.name, require_3d=False)


def _symbol_names(library: Path) -> list[str]:
    """Names of the top-level symbols of a library file, in file order."""
    text = library.read_text(encoding="utf-8")
    names = []
    for start, end in kicad_library._direct_child_ranges(text, "symbol"):
        head = kicad_library._SYMBOL_HEAD.match(text[start:end])
        names.append(kicad_library._atom_value(head.group(2)))
    return names


def _global_tables(tmp_path: Path, *, symbols: bool = True) -> Path:
    """Fake KiCad's global tables (never the real ones); return the fp-lib-table."""
    config = tmp_path / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "fp-lib-table").write_text("(fp_lib_table\n  (version 7)\n)\n", encoding="utf-8")
    if symbols:
        (config / "sym-lib-table").write_text(
            '(sym_lib_table\n  (version 7)\n'
            '  (lib (name "power")(type "KiCad")(uri "C:/kicad/power.kicad_sym")'
            '(options "")(descr "Keep me"))\n)\n', encoding="utf-8")
    return config / "fp-lib-table"


# ── project installation ──

def test_project_install_writes_symbol_library_and_table(tmp_path):
    """Positive: with a symbol, the project gets KiCadGen.kicad_sym and a portable sym-lib-table row."""
    project = tmp_path / "project"
    project.mkdir()
    result = install_component_library(_artifacts(_component(tmp_path)), project)
    library = project / "KiCadGen.kicad_sym"
    assert result.symbol_library_path == library
    assert result.symbol_table_path == project / "sym-lib-table"
    assert result.symbol_skipped is None and result.symbol_replaced is False
    assert _symbol_names(library) == ["Demo"]
    assert '"KiCadGen:Demo"' in library.read_text(encoding="utf-8")
    entry = kicad_library.find_library_entry(project / "sym-lib-table", "KiCadGen",
                                             root=kicad_library.SYM_TABLE_ROOT)
    assert entry.uri == "${KIPRJMOD}/KiCadGen.kicad_sym" and entry.library_type == "KiCad"


def test_project_install_without_symbol_installs_footprint_only(tmp_path):
    """Negative twin: no <name>.kicad_sym -> footprint installed, no symbol library, reason given."""
    project = tmp_path / "project"
    project.mkdir()
    result = install_component_library(_artifacts(_component(tmp_path, symbol=None)), project)
    assert result.footprint_path.is_file()
    assert result.symbol_library_path is None and result.symbol_table_path is None
    assert result.symbol_skipped == "symbol_missing"
    assert not (project / "KiCadGen.kicad_sym").exists()
    assert not (project / "sym-lib-table").exists()


def test_second_component_is_appended_and_first_kept_byte_for_byte(tmp_path):
    """Positive: another component joins the shared library; the first symbol is untouched."""
    project = tmp_path / "project"
    project.mkdir()
    install_component_library(_artifacts(_component(tmp_path, "Demo")), project)
    library = project / "KiCadGen.kicad_sym"
    before = library.read_text(encoding="utf-8")
    first = before[before.index('(symbol "Demo"'):before.rindex(")\n", 0, len(before) - 2)]
    install_component_library(_artifacts(_component(tmp_path, "Other")), project)
    after = library.read_text(encoding="utf-8")
    assert _symbol_names(library) == ["Demo", "Other"]
    assert first in after


def test_reinstall_asks_then_replaces_in_place(tmp_path):
    """Positive: the same name is a replace (files_exist names it), never a second copy."""
    project = tmp_path / "project"
    project.mkdir()
    component = _component(tmp_path, "Demo")
    install_component_library(_artifacts(component), project)
    install_component_library(_artifacts(_component(tmp_path, "Other")), project)
    (component / "Demo.kicad_sym").write_text(_symbol_library("Demo", pin_name="NEW"),
                                              encoding="utf-8")
    plan = plan_component_install(_artifacts(component), project)
    assert plan.symbol_replaced is True
    with pytest.raises(KiCadLibraryError) as raised:
        install_component_library(_artifacts(component), project)
    assert raised.value.code == "files_exist"
    assert "KiCadGen.kicad_sym (Demo)" in raised.value.context["files"]
    result = install_component_library(_artifacts(component), project, overwrite=True)
    assert result.symbol_replaced is True
    library = project / "KiCadGen.kicad_sym"
    assert _symbol_names(library) == ["Demo", "Other"]
    text = library.read_text(encoding="utf-8")
    assert '(name "NEW"' in text and '(name "A"' in text  # Other keeps its pin «A»


def test_new_component_is_not_a_replace(tmp_path):
    """Negative twin: a library holding only OTHER symbols does not count as a replace."""
    project = tmp_path / "project"
    project.mkdir()
    install_component_library(_artifacts(_component(tmp_path, "Other")), project)
    plan = plan_component_install(_artifacts(_component(tmp_path, "Demo")), project)
    assert plan.symbol_replaced is False and plan.replaced == ()


def test_foreign_rows_of_the_symbol_table_are_kept(tmp_path):
    """Unrelated rows of an existing project sym-lib-table survive verbatim."""
    project = tmp_path / "project"
    project.mkdir()
    foreign = ('  (lib (name "Mine")(type "KiCad")(uri "${KIPRJMOD}/mine.kicad_sym")'
               '(options "")(descr "Keep me"))\n')
    (project / "sym-lib-table").write_text(
        "(sym_lib_table\n  (version 7)\n" + foreign + ")\n", encoding="utf-8")
    install_component_library(_artifacts(_component(tmp_path)), project)
    text = (project / "sym-lib-table").read_text(encoding="utf-8")
    assert foreign in text and '"KiCadGen"' in text


def test_symbol_row_conflict_needs_its_own_confirmation(tmp_path):
    """A KiCadGen symbol row pointing elsewhere is a conflict of kind «symbol»; confirming
    the FOOTPRINT row does not repoint it, its own flag does."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "sym-lib-table").write_text(
        '(sym_lib_table\n  (version 7)\n'
        '  (lib (name "KiCadGen")(type "KiCad")(uri "C:/elsewhere/KiCadGen.kicad_sym")'
        '(options "")(descr ""))\n)\n', encoding="utf-8")
    artifacts = _artifacts(_component(tmp_path))
    with pytest.raises(KiCadLibraryError) as raised:
        install_component_library(artifacts, project, replace_library_entry=True)
    assert raised.value.code == "library_entry_conflict"
    assert raised.value.context["kind"] == "symbol"
    # Refused before the first write: nothing of the component is there.
    assert not (project / "KiCadGen.pretty").exists()
    assert not (project / "KiCadGen.kicad_sym").exists()
    install_component_library(artifacts, project, replace_symbol_library_entry=True)
    entry = kicad_library.find_library_entry(project / "sym-lib-table", "KiCadGen",
                                             root=kicad_library.SYM_TABLE_ROOT)
    assert entry.uri == "${KIPRJMOD}/KiCadGen.kicad_sym"


def test_footprint_row_conflict_says_footprint(tmp_path):
    """Negative twin: the same conflict in the fp-lib-table carries kind «footprint»."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "fp-lib-table").write_text(
        '(fp_lib_table\n  (version 7)\n'
        '  (lib (name "KiCadGen")(type "KiCad")(uri "C:/elsewhere/KiCadGen.pretty")'
        '(options "")(descr ""))\n)\n', encoding="utf-8")
    with pytest.raises(KiCadLibraryError) as raised:
        install_component_library(_artifacts(_component(tmp_path)), project)
    assert raised.value.code == "library_entry_conflict"
    assert raised.value.context["kind"] == "footprint"


# ── refusals before any write ──

def test_foreign_library_file_is_refused_before_writing(tmp_path):
    """A KiCadGen.kicad_sym that is not a symbol library: refused, nothing written."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "KiCadGen.kicad_sym").write_text("not a library", encoding="utf-8")
    with pytest.raises(KiCadLibraryError) as raised:
        plan_component_install(_artifacts(_component(tmp_path)), project)
    assert raised.value.code == "invalid_symbol_library"
    with pytest.raises(KiCadLibraryError):
        install_component_library(_artifacts(_component(tmp_path)), project)
    assert not (project / "KiCadGen.pretty").exists()
    assert (project / "KiCadGen.kicad_sym").read_text(encoding="utf-8") == "not a library"


def test_duplicate_symbol_in_library_is_refused(tmp_path):
    """A library holding the name twice is ambiguous: refused with duplicate_symbol."""
    project = tmp_path / "project"
    project.mkdir()
    one = _symbol_library("Demo")
    block = one[one.index('(symbol "Demo"'):one.rindex(")")].rstrip()
    (project / "KiCadGen.kicad_sym").write_text(
        one[:one.rindex(")")] + "  " + block + "\n)\n", encoding="utf-8")
    with pytest.raises(KiCadLibraryError) as raised:
        install_component_library(_artifacts(_component(tmp_path)), project)
    assert raised.value.code == "duplicate_symbol"
    assert not (project / "KiCadGen.pretty").exists()


# ── global installation ──

def test_global_install_registers_absolute_symbol_path(tmp_path):
    """Positive: the global sym-lib-table next to the fp-lib-table gets an absolute row."""
    table = _global_tables(tmp_path)
    libraries = tmp_path / "libraries"
    result = install_component_library(_artifacts(_component(tmp_path)), libraries,
                                       project_library=False, table_path=table)
    symbol_table = table.with_name("sym-lib-table")
    assert result.symbol_table_path == symbol_table
    entry = kicad_library.find_library_entry(symbol_table, "KiCadGen",
                                             root=kicad_library.SYM_TABLE_ROOT)
    assert entry.uri == (libraries / "KiCadGen.kicad_sym").as_posix()
    assert '"power"' in symbol_table.read_text(encoding="utf-8")


def test_global_install_without_symbol_table_is_refused(tmp_path):
    """Negative twin: KiCad never created the global sym-lib-table -> refused, nothing written."""
    table = _global_tables(tmp_path, symbols=False)
    libraries = tmp_path / "libraries"
    with pytest.raises(KiCadLibraryError) as raised:
        install_component_library(_artifacts(_component(tmp_path)), libraries,
                                  project_library=False, table_path=table)
    assert raised.value.code == "global_table_missing"
    assert raised.value.context["kind"] == "symbol"
    assert not (libraries / "KiCadGen.pretty").exists()
    assert table.read_text(encoding="utf-8") == "(fp_lib_table\n  (version 7)\n)\n"


def test_global_install_without_symbol_does_not_need_symbol_table(tmp_path):
    """Without a component symbol the missing global sym-lib-table is not needed."""
    table = _global_tables(tmp_path, symbols=False)
    result = install_component_library(_artifacts(_component(tmp_path, symbol=None)),
                                       tmp_path / "libraries", project_library=False,
                                       table_path=table)
    assert result.symbol_skipped == "symbol_missing"
    assert not table.with_name("sym-lib-table").exists()


# ── rollback ──

def test_failed_symbol_table_update_rolls_everything_back(tmp_path, monkeypatch):
    """The symbol table is the last write: its failure undoes footprint, library and fp table."""
    project = tmp_path / "project"
    project.mkdir()
    install_component_library(_artifacts(_component(tmp_path, "Other")), project)
    library = project / "KiCadGen.kicad_sym"
    library_before = library.read_bytes()
    fp_before = (project / "fp-lib-table").read_bytes()

    def broken(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(kicad_library, "update_sym_lib_table", broken)
    with pytest.raises(OSError):
        install_component_library(_artifacts(_component(tmp_path, "Demo")), project)
    assert library.read_bytes() == library_before
    assert (project / "fp-lib-table").read_bytes() == fp_before
    assert not (project / "KiCadGen.pretty" / "Demo.kicad_mod").exists()
    assert not any(path.name.startswith(kicad_library.ROLLBACK_DIR_PREFIX)
                   for path in project.iterdir())


def test_failed_first_install_removes_created_library(tmp_path, monkeypatch):
    """Negative twin: a library this install CREATED is removed, not left half-registered."""
    project = tmp_path / "project"
    project.mkdir()

    def broken(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(kicad_library, "update_sym_lib_table", broken)
    with pytest.raises(OSError):
        install_component_library(_artifacts(_component(tmp_path)), project)
    assert not (project / "KiCadGen.kicad_sym").exists()
    assert not (project / "fp-lib-table").exists()


# ── the library text ──

def test_older_library_version_is_raised_newer_kept():
    """The root version follows the newest format among the library and the new symbol."""
    source = _symbol_library("Demo", version=20251024)
    block = kicad_library._library_symbol(source, "Demo")
    old = _symbol_library("Other", version=20231120)
    merged, _had = merged_symbol_library_text(old, block, "Demo", source)
    assert merged.startswith("(kicad_symbol_lib (version 20251024)")
    newer = _symbol_library("Other", version=20260101)
    merged, _had = merged_symbol_library_text(newer, block, "Demo", source)
    assert merged.startswith("(kicad_symbol_lib (version 20260101)")


def test_crlf_library_keeps_crlf():
    """A library saved with CRLF stays CRLF: the added block takes its line ending."""
    source = _symbol_library("Demo")
    block = kicad_library._library_symbol(source, "Demo")
    existing = _symbol_library("Other").replace("\n", "\r\n")
    merged, _had = merged_symbol_library_text(existing, block, "Demo", source)
    assert "\n" not in merged.replace("\r\n", "")


def test_symbol_renamed_by_hand_is_installed_under_component_name(tmp_path):
    """The one symbol of a library renamed by hand installs under the footprint's name:
    units, Value and the Footprint field all follow (review 2026-11-15, finding 2)."""
    project = tmp_path / "project"
    project.mkdir()
    component = _component(tmp_path, "Demo", symbol=_symbol_library("OldName"))
    install_component_library(_artifacts(component), project)
    text = (project / "KiCadGen.kicad_sym").read_text(encoding="utf-8")
    assert _symbol_names(project / "KiCadGen.kicad_sym") == ["Demo"]
    assert '(symbol "Demo_0_1"' in text and '(symbol "Demo_1_1"' in text
    assert '(property "Footprint" "KiCadGen:Demo"' in text
    assert '(property "Value" "Demo"' in text
    assert "OldName" not in text


def test_renamed_symbol_follows_the_automatic_footprint_filter(tmp_path):
    """The automatic ki_fp_filters token follows the name (review 2026-11-15, L2);
    a filter typed by the user next to it stays."""
    project = tmp_path / "project"
    project.mkdir()
    symbol = _symbol_library("Old Name").replace(
        '    (symbol "Old Name_0_1"',
        '    (property "ki_fp_filters" "Old?Name* SOIC*" (at 0 0 0) (hide yes)'
        ' (effects (font (size 1.27 1.27))))\n    (symbol "Old Name_0_1"')
    install_component_library(_artifacts(_component(tmp_path, "New Name", symbol=symbol)), project)
    text = (project / "KiCadGen.kicad_sym").read_text(encoding="utf-8")
    assert '"New?Name* SOIC*"' in text


def test_footprint_field_naming_another_footprint_is_kept(tmp_path):
    """Negative twin: a Footprint the user pointed at ANOTHER footprint is not rewritten."""
    project = tmp_path / "project"
    project.mkdir()
    symbol = _symbol_library("Demo").replace('"KiCadGen:Demo"', '"Package_DIP:DIP-8_W7.62mm"')
    install_component_library(_artifacts(_component(tmp_path, "Demo", symbol=symbol)), project)
    text = (project / "KiCadGen.kicad_sym").read_text(encoding="utf-8")
    assert '"Package_DIP:DIP-8_W7.62mm"' in text


def test_unit_with_a_stale_prefix_is_renamed_from_its_suffix(tmp_path):
    """A unit named after another symbol would make KiCad refuse the WHOLE shared
    library (review 2026-11-15, finding 1): it is renamed <name>_<unit>_<style>."""
    project = tmp_path / "project"
    project.mkdir()
    install_component_library(_artifacts(_component(tmp_path, "Other")), project)
    bad = _symbol_library("Bad").replace('(symbol "Bad_1_1"', '(symbol "Wrong_1_1"')
    result = install_component_library(_artifacts(_component(tmp_path, "Bad", symbol=bad)), project)
    text = (project / "KiCadGen.kicad_sym").read_text(encoding="utf-8")
    assert result.symbol_skipped is None
    assert '(symbol "Bad_1_1"' in text and "Wrong_" not in text


def test_unit_without_unit_suffix_is_not_installed(tmp_path):
    """Negative twin: a sub-symbol KiCad cannot read as a unit -> no symbol, library untouched."""
    project = tmp_path / "project"
    project.mkdir()
    install_component_library(_artifacts(_component(tmp_path, "Other")), project)
    library = project / "KiCadGen.kicad_sym"
    before = library.read_bytes()
    bad = _symbol_library("Bad").replace('(symbol "Bad_1_1"', '(symbol "Bad_units"')
    result = install_component_library(_artifacts(_component(tmp_path, "Bad", symbol=bad)), project)
    assert result.symbol_skipped == "symbol_unreadable"
    assert result.footprint_path.is_file()
    assert library.read_bytes() == before


def test_nameless_symbol_is_unreadable_not_a_crash(tmp_path):
    """A one-symbol file whose symbol has no name: reason code, never AttributeError (finding 4)."""
    project = tmp_path / "project"
    project.mkdir()
    nameless = ('(kicad_symbol_lib (version 20251024)\n  (symbol\n'
                '    (property "Reference" "U" (at 0 0 0)))\n)\n')
    plan = plan_component_install(_artifacts(_component(tmp_path, symbol=nameless)), project)
    assert plan.symbol_skipped == "symbol_unreadable" and not plan.with_symbol


def test_component_file_without_version_is_unreadable(tmp_path):
    """A component file without a root version cannot start a library KiCad loads."""
    project = tmp_path / "project"
    project.mkdir()
    text = _symbol_library("Demo").replace(" (version 20251024)", "")
    plan = plan_component_install(_artifacts(_component(tmp_path, symbol=text)), project)
    assert plan.symbol_skipped == "symbol_unreadable"


def test_install_drops_bom_keeps_crlf_of_the_shared_library(tmp_path):
    """The real install path keeps a library's CRLF and other symbols byte for
    byte (finding 3: the pure-function test did not cover the install's read),
    and drops its BOM.

    CONTRACT CHANGED 2026-11-15 (review L3): the BOM used to be written back.
    KiCad 10 refuses a .kicad_sym starting with one (kicad-cli 10.0.8
    `sym export svg`: «Impossibile caricare la libreria», "cannot load the
    library"), so the install kept the whole library unusable while
    reporting success; it now writes it without the BOM and says so.
    """
    project = tmp_path / "project"
    project.mkdir()
    library = project / "KiCadGen.kicad_sym"
    other = _symbol_library("Other").replace("\n", "\r\n")
    library.write_bytes(b"\xef\xbb\xbf" + other.encode("utf-8"))
    other_block = other[other.index('(symbol "Other"'):other.rindex(")")].rstrip()
    plan = plan_component_install(_artifacts(_component(tmp_path, "Demo")), project)
    assert plan.bom_to_remove == (library,)
    result = install_component_library(_artifacts(_component(tmp_path, "Demo")), project)
    data = library.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    text = data.decode("utf-8")
    assert "\n" not in text.replace("\r\n", "")
    assert other_block in text
    assert _symbol_names(library) == ["Other", "Demo"]
    assert result.symbol_library_bom_removed is True
    assert result.bom_removed == (library,)


def test_install_keeps_lf_library_lf(tmp_path):
    """Negative twin: an LF library without BOM stays LF without BOM, and no BOM is reported."""
    project = tmp_path / "project"
    project.mkdir()
    library = project / "KiCadGen.kicad_sym"
    library.write_text(_symbol_library("Other"), encoding="utf-8", newline="\n")
    result = install_component_library(_artifacts(_component(tmp_path, "Demo")), project)
    data = library.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf") and b"\r\n" not in data
    assert result.symbol_library_bom_removed is False and result.bom_removed == ()


def test_folder_in_place_of_symbol_table_is_refused_before_writing(tmp_path):
    """A folder named sym-lib-table: refused up front, no debris (finding 5)."""
    project = tmp_path / "project"
    (project / "sym-lib-table").mkdir(parents=True)
    with pytest.raises(KiCadLibraryError) as raised:
        install_component_library(_artifacts(_component(tmp_path)), project)
    assert raised.value.code == "install_target_is_folder"
    assert sorted(path.name for path in project.iterdir()) == ["sym-lib-table"]


def test_folder_at_library_path_does_not_block_footprint_only_install(tmp_path):
    """Negative twin: without a component symbol the library path is never touched (finding 8)."""
    project = tmp_path / "project"
    (project / "KiCadGen.kicad_sym").mkdir(parents=True)
    result = install_component_library(_artifacts(_component(tmp_path, symbol=None)), project)
    assert result.footprint_path.is_file() and result.symbol_skipped == "symbol_missing"


def test_broken_shared_library_names_the_file(tmp_path):
    """A truncated library is refused as invalid_symbol_library WITH its path (finding 6)."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "KiCadGen.kicad_sym").write_text(_symbol_library("Other")[:-3], encoding="utf-8")
    with pytest.raises(KiCadLibraryError) as raised:
        install_component_library(_artifacts(_component(tmp_path)), project)
    assert raised.value.code == "invalid_symbol_library"
    assert raised.value.context["path"] == project / "KiCadGen.kicad_sym"


def test_root_token_is_matched_exactly_and_missing_version_added():
    """«(kicad_symbol_libX» is another file; a root without version gets the component's (finding 7)."""
    source = _symbol_library("Demo")
    block = kicad_library._library_symbol(source, "Demo")
    with pytest.raises(KiCadLibraryError) as raised:
        merged_symbol_library_text("(kicad_symbol_libX (version 1))\n", block, "Demo", source)
    assert raised.value.code == "invalid_symbol_library"
    merged, _had = merged_symbol_library_text("(kicad_symbol_lib)\n", block, "Demo", source)
    assert merged.startswith("(kicad_symbol_lib (version 20251024)")


def test_skipped_symbol_reports_an_older_one_left(tmp_path):
    """No symbol shipped, yet the library holds this name: said (finding 9); otherwise not."""
    project = tmp_path / "project"
    project.mkdir()
    install_component_library(_artifacts(_component(tmp_path, "Demo")), project)
    install_component_library(_artifacts(_component(tmp_path, "Other")), project)
    component = _component(tmp_path, "Demo", symbol=None)
    (component / "Demo.kicad_sym").unlink()  # the same folder held the file before
    stale = install_component_library(_artifacts(component), project, overwrite=True)
    assert stale.symbol_skipped == "symbol_missing" and stale.symbol_left_stale is True
    fresh = install_component_library(_artifacts(_component(tmp_path, "Third", symbol=None)),
                                      project)
    assert fresh.symbol_left_stale is False


def test_unchanged_reinstall_does_not_rewrite_library(tmp_path, monkeypatch):
    """Re-installing the same symbol leaves the shared library file untouched.

    Observed through the writer and the file identity, never the mtime: an
    immediate write + replace kept the same st_mtime_ns in 13 of 200 tries on
    a Windows machine, so a timestamp could not tell a rewrite apart.
    """
    project = tmp_path / "project"
    project.mkdir()
    component = _component(tmp_path)
    install_component_library(_artifacts(component), project)
    library = project / "KiCadGen.kicad_sym"
    before = library.read_bytes()
    identity = library.stat().st_ino
    written: list[Path] = []
    real_write = kicad_library._write_durably

    def spy(path, payload):
        written.append(Path(path))
        return real_write(path, payload)

    monkeypatch.setattr(kicad_library, "_write_durably", spy)
    result = install_component_library(_artifacts(component), project, overwrite=True)
    assert written == []
    assert result.symbol_identical is True and result.symbol_replaced is False
    assert library.read_bytes() == before and library.stat().st_ino == identity


def test_symbol_shadowing_of_global_row_is_reported(tmp_path):
    """A global KiCadGen SYMBOL row elsewhere is reported for a project install."""
    table = _global_tables(tmp_path)
    symbol_table = table.with_name("sym-lib-table")
    symbol_table.write_text(
        '(sym_lib_table\n  (version 7)\n'
        '  (lib (name "KiCadGen")(type "KiCad")(uri "C:/global/KiCadGen.kicad_sym")'
        '(options "")(descr ""))\n)\n', encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    plan = plan_component_install(_artifacts(_component(tmp_path)), project,
                                  other_table_path=table)
    assert plan.symbol_shadowing is not None
    assert plan.symbol_shadowing.uri == "C:/global/KiCadGen.kicad_sym"
    assert plan.shadowing is None  # the footprint row has no clash
