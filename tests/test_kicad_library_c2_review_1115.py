"""@brief Fixes C2 of the 2026-11-15 review of the symbol installation (kicad_library).

Each rule has a twin case it must tell apart for a structural reason:

- M5/F10: the symbol's ``Footprint`` is rewritten only when it names one of
  this program's footprints (its library is the generator's own nickname or
  the target library, or it has no library at all) under the old or the new
  name; the same footprint NAME in another library is a person's choice.
- F8: the ``Value`` follows a rename only when the generator's convention is
  witnessed (Footprint naming this program's footprint under the old name).
- L3: a UTF-8 BOM, which KiCad 10 does not load in a ``.kicad_sym``,
  ``sym-lib-table`` or ``fp-lib-table``, is dropped and reported.
- L4: a failed install leaves no history file it created and the older
  history byte-identical; a successful one keeps its backups.
- P3: an identical reinstall writes nothing; a changed pad is a replacement.
- The component library's root token is matched exactly.
"""

from pathlib import Path

import pytest

from kicadgen.services import kicad_library
from kicadgen.services.kicad_library import (
    KiCadLibraryError,
    discover_component_artifacts,
    install_component_library,
    plan_component_install,
    update_fp_lib_table,
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
BOM = b"\xef\xbb\xbf"


def _symbol_library(name: str, *, value: str | None = None,
                    footprint: str | None = None) -> str:
    """A one-symbol library as the generator writes it, Value and Footprint overridable."""
    value = name if value is None else value
    footprint = f"KiCadGen:{name}" if footprint is None else footprint
    return (
        f'(kicad_symbol_lib (version 20251024) (generator "kicadgen") (generator_version "10.0.1")\n'
        f'  (symbol "{name}"\n'
        f'    (property "Reference" "U" (at 0 2.54 0) (effects (font (size 1.27 1.27))))\n'
        f'    (property "Value" "{value}" (at 0 0 0) (effects (font (size 1.27 1.27))))\n'
        f'    (property "Footprint" "{footprint}" (at 0 0 0) (hide yes) (effects (font (size 1.27 1.27))))\n'
        f'    (symbol "{name}_0_1"\n'
        f'      (rectangle (start -2.54 2.54) (end 2.54 -2.54)'
        f' (stroke (width 0.254) (type default)) (fill (type background))))\n'
        f'    (symbol "{name}_1_1"\n'
        f'      (pin passive line (at -5.08 0 0) (length 2.54)\n'
        f'        (name "A" (effects (font (size 1.27 1.27))))\n'
        f'        (number "1" (effects (font (size 1.27 1.27))))))\n'
        f'  )\n'
        f')\n'
    )


def _component(tmp_path: Path, name: str = "Demo", *, symbol: str | None = "default") -> Path:
    """Write a generated component folder: footprint, STEP and (optionally) its symbol."""
    component = tmp_path / "components" / name
    (component / f"{name}.pretty").mkdir(parents=True, exist_ok=True)
    (component / f"{name}.pretty" / f"{name}.kicad_mod").write_text(
        FOOTPRINT.format(name=name), encoding="utf-8")
    (component / f"{name}.3dshapes").mkdir(exist_ok=True)
    (component / f"{name}.3dshapes" / f"{name}.step").write_bytes(STEP_BYTES)
    if symbol is not None:
        text = _symbol_library(name) if symbol == "default" else symbol
        (component / f"{name}.kicad_sym").write_text(text, encoding="utf-8")
    return component


def _artifacts(component: Path):
    """The artifacts as the GUI discovers them."""
    return discover_component_artifacts(component, component.name, require_3d=False)


def _properties(block: str) -> dict[str, str]:
    """Key -> value of the direct properties of a symbol block."""
    found = {}
    for start, end in kicad_library._direct_child_ranges(block, "property"):
        match = kicad_library._PROPERTY_HEAD.match(block[start:end])
        found[kicad_library._atom_value(match.group(2))] = kicad_library._atom_value(match.group(4))
    return found


def _named(symbol_name: str, new_name: str, library: str, **overrides) -> dict[str, str]:
    """Properties of the generated symbol `symbol_name` once installed as `new_name` in `library`."""
    source = _symbol_library(symbol_name, **overrides)
    block = kicad_library._library_symbol(source, symbol_name)
    return _properties(kicad_library._named_symbol_block(block, new_name, library))


def _tree(folder: Path) -> dict[str, bytes | None]:
    """Every file (bytes) and folder (None) under `folder`, by relative path."""
    return {path.relative_to(folder).as_posix(): (path.read_bytes() if path.is_file() else None)
            for path in sorted(folder.rglob("*"))}


# ── M5 / F10: the Footprint property ──

def test_own_library_footprint_follows_the_install_library():
    """Positive: the generator's «KiCadGen:<old>» and the target's «MyLib:<old>» become «MyLib:<new>»."""
    assert _named("Old", "New", "MyLib")["Footprint"] == "MyLib:New"
    assert _named("Old", "New", "MyLib", footprint="MyLib:Old")["Footprint"] == "MyLib:New"
    # The same component into another library: the generator's nickname is repointed.
    assert _named("Demo", "Demo", "MyLib")["Footprint"] == "MyLib:Demo"


def test_same_footprint_name_in_another_library_is_kept():
    """Negative twin (M5): the NAME matches, the library is a person's choice -> kept."""
    assert _named("Old", "New", "MyLib", footprint="Vendor:Old")["Footprint"] == "Vendor:Old"
    assert _named("Demo", "Demo", "MyLib", footprint="Vendor:Demo")["Footprint"] == "Vendor:Demo"
    assert (_named("SOT-23", "SOT-23", "MyLib", footprint="Package_TO_SOT_SMD:SOT-23")
            ["Footprint"] == "Package_TO_SOT_SMD:SOT-23")


def test_bare_footprint_naming_the_component_gets_the_library():
    """F10 positive: a bare old or new name (KiCad cannot resolve it) names this component."""
    assert _named("Old", "New", "MyLib", footprint="Old")["Footprint"] == "MyLib:New"
    assert _named("Old", "New", "MyLib", footprint="New")["Footprint"] == "MyLib:New"


def test_bare_footprint_naming_something_else_is_kept():
    """F10 negative: a bare name of ANOTHER footprint, or an empty field, is not ours."""
    assert _named("Old", "New", "MyLib", footprint="SOT-23")["Footprint"] == "SOT-23"
    assert _named("Old", "New", "MyLib", footprint="")["Footprint"] == ""


def test_install_keeps_a_foreign_footprint_of_the_same_name(tmp_path):
    """End to end: «Vendor:Demo» in the component's symbol survives the install verbatim."""
    project = tmp_path / "project"
    project.mkdir()
    symbol = _symbol_library("Demo", footprint="Vendor:Demo")
    install_component_library(_artifacts(_component(tmp_path, symbol=symbol)), project,
                              library_name="MyLib")
    text = (project / "MyLib.kicad_sym").read_text(encoding="utf-8")
    assert '(property "Footprint" "Vendor:Demo"' in text
    assert "MyLib:Demo" not in text


# ── F8: the Value ──

def test_generated_value_follows_the_rename():
    """Positive: Value, symbol and Footprint carry one generated name -> the Value follows."""
    assert _named("Old", "New", "KiCadGen")["Value"] == "New"
    assert _named("Old", "New", "KiCadGen", footprint="Old")["Value"] == "New"


def test_vendor_value_of_a_symbol_renamed_by_hand_is_kept():
    """Negative twin: symbol renamed by hand to «LM358» (Value «LM358», Footprint still the
    component's) -> the Value is a part number and stays; the Footprint still follows."""
    props = _named("LM358", "Demo", "KiCadGen", footprint="KiCadGen:Demo")
    assert props["Value"] == "LM358"
    assert props["Footprint"] == "KiCadGen:Demo"


def test_value_without_generator_witness_is_kept():
    """Negative twin: a foreign Footprint gives no witness of the generator's naming."""
    assert _named("Old", "New", "KiCadGen", footprint="Vendor:Old")["Value"] == "Old"
    assert _named("Old", "New", "KiCadGen", footprint="")["Value"] == "Old"


# ── exact root token of the component's library ──

def test_component_library_root_token_is_matched_exactly(tmp_path):
    """«(kicad_symbol_libX» is another file: unreadable; leading blanks are fine."""
    good = _symbol_library("Demo")
    with pytest.raises(kicad_library._SymbolUnusable) as raised:
        kicad_library._library_symbol(good.replace("(kicad_symbol_lib ", "(kicad_symbol_libX ", 1),
                                      "Demo")
    assert raised.value.args[0] == "symbol_unreadable"
    assert kicad_library._library_symbol("\n  " + good, "Demo").startswith('(symbol "Demo"')
    project = tmp_path / "project"
    project.mkdir()
    bad = good.replace("(kicad_symbol_lib ", "(kicad_symbol_libX ", 1)
    plan = plan_component_install(_artifacts(_component(tmp_path, symbol=bad)), project)
    assert plan.symbol_skipped == "symbol_unreadable"


# ── L3: BOM in the tables ──

TABLE = '(fp_lib_table\r\n  (version 7)\r\n  (lib (name "A")(type "KiCad")(uri "x")(options "")(descr ""))\r\n)\r\n'


def test_table_bom_is_dropped_even_without_a_row_change(tmp_path):
    """A table with a BOM is rewritten without it (KiCad 10 ignores it whole), CRLF kept."""
    table = tmp_path / "fp-lib-table"
    table.write_bytes(BOM + TABLE.encode("utf-8"))
    assert update_fp_lib_table(table, "A", "x", create_missing=False)
    assert table.read_bytes() == TABLE.encode("utf-8")
    backup = table.with_name("fp-lib-table.kicadgen-backup")
    assert backup.read_bytes() == BOM + TABLE.encode("utf-8"), "the original is the backup"


def test_table_without_bom_and_without_change_is_not_rewritten(tmp_path):
    """Negative twin: nothing to add and no BOM -> no write, no backup."""
    table = tmp_path / "fp-lib-table"
    table.write_bytes(TABLE.encode("utf-8"))
    assert not update_fp_lib_table(table, "A", "x", create_missing=False)
    assert not table.with_name("fp-lib-table.kicadgen-backup").exists()


def test_install_reports_the_tables_that_lost_their_bom(tmp_path):
    """Both project tables with a BOM: written without it and listed in bom_removed."""
    project = tmp_path / "project"
    project.mkdir()
    fp_table, sym_table = project / "fp-lib-table", project / "sym-lib-table"
    fp_table.write_bytes(BOM + b"(fp_lib_table\r\n  (version 7)\r\n)\r\n")
    sym_table.write_bytes(BOM + b"(sym_lib_table\r\n  (version 7)\r\n)\r\n")
    plan = plan_component_install(_artifacts(_component(tmp_path)), project)
    assert set(plan.bom_to_remove) == {fp_table, sym_table}
    result = install_component_library(_artifacts(_component(tmp_path)), project)
    assert set(result.bom_removed) == {fp_table, sym_table}
    assert result.symbol_library_bom_removed is False
    for table in (fp_table, sym_table):
        data = table.read_bytes()
        assert not data.startswith(BOM)
        assert b"\n" not in data.replace(b"\r\n", b""), "CRLF kept"


# ── L4: the rollback leaves no history file behind ──

def _tables_with_foreign_rows(project: Path) -> None:
    """Project tables without a KiCadGen row, so the install changes (and backs up) both."""
    project.mkdir(parents=True, exist_ok=True)
    (project / "fp-lib-table").write_text(
        '(fp_lib_table\n  (version 7)\n  (lib (name "Mine")(type "KiCad")'
        '(uri "${KIPRJMOD}/mine.pretty")(options "")(descr ""))\n)\n', encoding="utf-8")
    (project / "sym-lib-table").write_text(
        '(sym_lib_table\n  (version 7)\n  (lib (name "Mine")(type "KiCad")'
        '(uri "${KIPRJMOD}/mine.kicad_sym")(options "")(descr ""))\n)\n', encoding="utf-8")


def _fail_after_symbol_table(monkeypatch) -> None:
    """The symbol table is really written (history rotated), then the install fails."""
    real = kicad_library.update_sym_lib_table

    def write_then_fail(*args, **kwargs):
        real(*args, **kwargs)
        raise OSError("injected failure after the symbol table write")

    monkeypatch.setattr(kicad_library, "update_sym_lib_table", write_then_fail)


def test_failed_install_leaves_the_folder_identical(tmp_path, monkeypatch):
    """No history file of its own, nothing else: the folder is exactly as before."""
    project = tmp_path / "project"
    _tables_with_foreign_rows(project)
    before = _tree(project)
    _fail_after_symbol_table(monkeypatch)
    with pytest.raises(OSError, match="injected"):
        install_component_library(_artifacts(_component(tmp_path)), project)
    assert _tree(project) == before


def test_failed_install_puts_a_full_older_history_back_byte_for_byte(tmp_path, monkeypatch):
    """Three older copies per table (the rotation drops the oldest): all back, unchanged."""
    project = tmp_path / "project"
    _tables_with_foreign_rows(project)
    for table in ("fp-lib-table", "sym-lib-table"):
        for index, suffix in enumerate(("", ".1", ".2")):
            (project / f"{table}.kicadgen-backup{suffix}").write_bytes(
                f"({table} old copy {index})\n".encode())
    before = _tree(project)
    _fail_after_symbol_table(monkeypatch)
    with pytest.raises(OSError, match="injected"):
        install_component_library(_artifacts(_component(tmp_path)), project)
    assert _tree(project) == before


def test_refused_table_rollback_copy_is_a_clean_refusal(tmp_path, monkeypatch):
    """A refused rollback copy of the TABLE is ``install_write_refused`` naming the table, with
    nothing left behind; before the fix the rollback restored from the copy that was never
    written and reported ``rollback_incomplete``."""
    project = tmp_path / "project"
    _tables_with_foreign_rows(project)
    before = _tree(project)
    real_copy = kicad_library._copy_writable

    def refuse_table_copy(source, target):
        if Path(target).name == "table-backup":
            raise PermissionError(13, "simulated", str(target))
        return real_copy(source, target)

    monkeypatch.setattr(kicad_library, "_copy_writable", refuse_table_copy)
    with pytest.raises(KiCadLibraryError) as raised:
        install_component_library(_artifacts(_component(tmp_path)), project)
    assert raised.value.code == "install_write_refused"
    assert Path(raised.value.context["path"]) == project / "fp-lib-table"
    assert _tree(project) == before


def test_refused_footprint_rollback_copy_names_the_footprint(tmp_path, monkeypatch):
    """Twin: the copy refused is a REPLACED footprint's -> the refusal names that footprint."""
    project = tmp_path / "project"
    project.mkdir()
    component = _component(tmp_path)
    first = install_component_library(_artifacts(component), project)
    (component / "Demo.pretty" / "Demo.kicad_mod").write_text(
        FOOTPRINT.format(name="Demo").replace("(size 1 1)", "(size 2 1)"), encoding="utf-8")
    before = _tree(project)
    real_copy = kicad_library._copy_writable

    def refuse_rollback_copies(source, target):
        if Path(target).parent.name.startswith(kicad_library.ROLLBACK_DIR_PREFIX):
            raise PermissionError(13, "simulated", str(target))
        return real_copy(source, target)

    monkeypatch.setattr(kicad_library, "_copy_writable", refuse_rollback_copies)
    with pytest.raises(KiCadLibraryError) as raised:
        install_component_library(_artifacts(component), project, overwrite=True)
    assert raised.value.code == "install_write_refused"
    assert Path(raised.value.context["path"]) == first.footprint_path
    assert _tree(project) == before


def test_successful_install_keeps_its_history_backups(tmp_path):
    """Positive control: without the failure the pre-change copies stay as the safety copy."""
    project = tmp_path / "project"
    _tables_with_foreign_rows(project)
    fp_before = (project / "fp-lib-table").read_bytes()
    sym_before = (project / "sym-lib-table").read_bytes()
    install_component_library(_artifacts(_component(tmp_path)), project)
    assert (project / "fp-lib-table.kicadgen-backup").read_bytes() == fp_before
    assert (project / "sym-lib-table.kicadgen-backup").read_bytes() == sym_before


# ── P3: identical reinstall ──

def _spy_writers(monkeypatch) -> list[Path]:
    """Record every file the install writes, copies or replaces atomically."""
    written: list[Path] = []
    for name in ("_write_durably", "_copy_writable", "_atomic_write_bytes"):
        real = getattr(kicad_library, name)

        def spy(first, second, *rest, _real=real, _name=name, **kwargs):
            # _copy_writable(source, target); the other two (target, payload).
            written.append(Path(second if _name == "_copy_writable" else first))
            return _real(first, second, *rest, **kwargs)

        monkeypatch.setattr(kicad_library, name, spy)
    return written


def test_identical_reinstall_writes_nothing(tmp_path, monkeypatch):
    """Positive: same footprint, model and symbol -> everything unchanged, no write at all."""
    project = tmp_path / "project"
    project.mkdir()
    component = _component(tmp_path)
    first = install_component_library(_artifacts(component), project)
    before = _tree(project)
    identities = {path: path.stat().st_ino for path in project.rglob("*") if path.is_file()}
    plan = plan_component_install(_artifacts(component), project)
    assert plan.replaced == () and plan.removed == ()
    assert set(plan.unchanged) == {first.footprint_path, first.step_path}
    assert plan.symbol_identical is True and plan.symbol_replaced is False
    written = _spy_writers(monkeypatch)
    again = install_component_library(_artifacts(component), project)  # no overwrite needed
    assert written == []
    assert again.unchanged == plan.unchanged and again.symbol_identical is True
    assert again.replaced == () and again.symbol_replaced is False
    assert _tree(project) == before
    assert {path: path.stat().st_ino for path in project.rglob("*") if path.is_file()} == identities


def test_changed_pad_is_a_replacement(tmp_path):
    """Negative twin: a different pad -> the footprint is replaced (asked), the same STEP is not."""
    project = tmp_path / "project"
    project.mkdir()
    component = _component(tmp_path)
    first = install_component_library(_artifacts(component), project)
    footprint = component / "Demo.pretty" / "Demo.kicad_mod"
    footprint.write_text(FOOTPRINT.format(name="Demo").replace("(size 1 1)", "(size 1.5 1)"),
                         encoding="utf-8")
    plan = plan_component_install(_artifacts(component), project)
    assert plan.replaced == (first.footprint_path,)
    assert plan.unchanged == (first.step_path,)
    with pytest.raises(KiCadLibraryError) as raised:
        install_component_library(_artifacts(component), project)
    assert raised.value.code == "files_exist"
    result = install_component_library(_artifacts(component), project, overwrite=True)
    assert "(size 1.5 1)" in result.footprint_path.read_text(encoding="utf-8")


def test_changed_symbol_is_a_replacement_not_identical(tmp_path):
    """Negative twin for the symbol: another pin name -> replaced, not identical."""
    project = tmp_path / "project"
    project.mkdir()
    component = _component(tmp_path)
    install_component_library(_artifacts(component), project)
    (component / "Demo.kicad_sym").write_text(
        _symbol_library("Demo").replace('(name "A"', '(name "B"'), encoding="utf-8")
    plan = plan_component_install(_artifacts(component), project)
    assert plan.symbol_replaced is True and plan.symbol_identical is False


def test_identical_symbol_in_a_library_with_bom_is_rewritten_without_it(tmp_path):
    """The symbol is identical but the file starts with a BOM: not «replaced», yet the file
    is rewritten to drop the BOM (KiCad could not load it), and the result says so."""
    project = tmp_path / "project"
    project.mkdir()
    component = _component(tmp_path)
    install_component_library(_artifacts(component), project)
    library = project / "KiCadGen.kicad_sym"
    clean = library.read_bytes()
    library.write_bytes(BOM + clean)
    plan = plan_component_install(_artifacts(component), project)
    assert plan.symbol_identical is True and plan.symbol_replaced is False
    assert plan.bom_to_remove == (library,)
    result = install_component_library(_artifacts(component), project)
    assert library.read_bytes() == clean
    assert result.symbol_library_bom_removed is True
