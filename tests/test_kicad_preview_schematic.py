"""@brief «Prova in KiCad» schematic, two editors and 3D image (USER DECISION 2026-11-13, option B).

@details The isolated preview also writes a schematic with the component
symbol placed, a project-local copy of the symbol library and a
``sym-lib-table`` naming only that copy; it opens the Schematic Editor beside
the PCB Editor and renders the board with ``kicad-cli pcb render``. Every
check here fakes the editors and kicad-cli, except the read-only checks at
the end, which run the real kicad-cli on temporary files when KiCad is
installed and are skipped otherwise (the GitHub runner has no KiCad).
"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess

import pytest

from kicadgen.services import kicad_library
from kicadgen.services.kicad_library import (
    PREVIEW_LIBRARY_NICKNAME,
    SCHEMATIC_FORMAT_VERSION,
    KiCadLibraryError,
    PreviewProject,
    create_preview_project,
    discover_component_artifacts,
    kicad_lock_file,
    launch_preview_editors,
    preview_reference,
    preview_render_command,
    preview_schematic_text,
    render_preview_image,
)
from kicadgen.services.system_dependencies import find_kicad_cli


FOOTPRINT = """(footprint "Demo"
\t(version 20260206)
\t(generator "kicadgen_tool")
\t(layer "F.Cu")
\t(pad "1" thru_hole circle (at -1.27 0) (size 1.7 1.7) (drill 1) (layers "*.Cu" "*.Mask"))
\t(pad "2" thru_hole circle (at 1.27 0) (size 1.7 1.7) (drill 1) (layers "*.Cu" "*.Mask"))
\t(fp_rect (start -3 -2) (end 3 2) (stroke (width 0.05) (type default)) (fill no) (layer "F.CrtYd"))
)
"""

#: A single-unit symbol written as `kicadgen.generators.symbol` writes it.
SYMBOL_LIBRARY = """(kicad_symbol_lib (version 20251024) (generator "kicadgen") (generator_version "10.0.1")
  (symbol "Demo"
    (exclude_from_sim no) (in_bom yes) (on_board yes) (in_pos_files yes)
    (property "Reference" "K" (at 0 5.08 0) (show_name no) (do_not_autoplace no) (effects (font (size 1.27 1.27))))
    (property "Value" "Demo" (at 0 -5.08 0) (show_name no) (do_not_autoplace no) (effects (font (size 1.27 1.27))))
    (property "Footprint" "KiCadGen:Demo" (at 0 0 0) (show_name no) (do_not_autoplace no) (hide yes) (effects (font (size 1.27 1.27))))
    (property "Datasheet" "" (at 0 0 0) (show_name no) (do_not_autoplace no) (hide yes) (effects (font (size 1.27 1.27))))
    (property "Description" "Demo part" (at 0 0 0) (show_name no) (do_not_autoplace no) (hide yes) (effects (font (size 1.27 1.27))))
    (property "ki_keywords" "demo" (at 0 0 0) (show_name no) (do_not_autoplace no) (hide yes) (effects (font (size 1.27 1.27))))
    (symbol "Demo_0_1"
      (rectangle (start -2.54 2.54) (end 2.54 -2.54) (stroke (width 0.254) (type default)) (fill (type background)))
    )
    (symbol "Demo_1_1"
      (pin passive line (at -5.08 0 0) (length 2.54)
        (name "A" (effects (font (size 1.27 1.27))))
        (number "1" (effects (font (size 1.27 1.27)))))
      (pin passive line (at 5.08 0 180) (length 2.54)
        (name "B" (effects (font (size 1.27 1.27))))
        (number "2" (effects (font (size 1.27 1.27)))))
    )
  )
)
"""

#: Two units sharing a power pin of unit 0, to show every unit is placed.
TWO_UNIT_BLOCK = """(symbol "Dual"
    (property "Reference" "U?" (at 0 5.08 0) (effects (font (size 1.27 1.27))))
    (property "Value" "Dual" (at 0 -5.08 0) (effects (font (size 1.27 1.27))))
    (symbol "Dual_0_1"
      (pin power_in line (at 0 7.62 270) (length 2.54) (name "V+") (number "8"))
    )
    (symbol "Dual_1_1"
      (pin input line (at -5.08 0 0) (length 2.54) (name "IN") (number "3"))
    )
    (symbol "Dual_2_1"
      (pin input line (at -5.08 0 0) (length 2.54) (name "IN") (number "5"))
    )
  )"""

STEP_BYTES = b"ISO-10303-21;\nEND-ISO-10303-21;\n"


def _component(tmp_path: Path, *, step: bool = True, symbol: str | None = SYMBOL_LIBRARY):
    """Component folder with footprint, optional STEP and optional symbol library."""
    component = tmp_path / "Demo"
    (component / "Demo.pretty").mkdir(parents=True)
    (component / "Demo.pretty" / "Demo.kicad_mod").write_text(FOOTPRINT, encoding="utf-8")
    if step:
        (component / "Demo.3dshapes").mkdir()
        (component / "Demo.3dshapes" / "Demo.step").write_bytes(STEP_BYTES)
    if symbol is not None:
        (component / "Demo.kicad_sym").write_text(symbol, encoding="utf-8")
    return discover_component_artifacts(component, require_3d=False)


# --- schematic and project-local symbol table ---------------------------------

def test_preview_writes_schematic_symbol_copy_and_local_table(tmp_path):
    artifacts = _component(tmp_path)
    preview = create_preview_project(artifacts)
    assert preview.schematic_skipped is None
    assert preview.schematic_path == preview.directory / "Demo-preview.kicad_sch"
    # KiCad opens <project>.kicad_sch as the root sheet of <project>.kicad_pro.
    assert preview.schematic_path.stem == preview.project_path.stem
    assert preview.symbol_library_path.read_bytes() == (
        artifacts.component_dir / "Demo.kicad_sym").read_bytes()
    table = (preview.directory / "sym-lib-table").read_text(encoding="utf-8")
    uris = re.findall(r'\(uri "([^"]*)"\)', table)
    # ONE row, naming only the copy inside the temporary project.
    assert uris == ["${KIPRJMOD}/Demo.kicad_sym"]
    assert f'(name "{PREVIEW_LIBRARY_NICKNAME}")' in table
    # USER DECISION 2026-11-13: the footprint field resolves through a
    # footprint table of the temporary project naming only its own copy.
    fp_table = (preview.directory / "fp-lib-table").read_text(encoding="utf-8")
    fp_uris = re.findall(r'\(uri "([^"]*)"\)', fp_table)
    assert fp_uris == [f"${{KIPRJMOD}}/{PREVIEW_LIBRARY_NICKNAME}.pretty"]
    assert f'(name "{PREVIEW_LIBRARY_NICKNAME}")' in fp_table
    copy = preview.directory / f"{PREVIEW_LIBRARY_NICKNAME}.pretty" / "Demo.kicad_mod"
    footprint = copy.read_text(encoding="utf-8")
    assert footprint.startswith('(footprint "Demo"')
    # Same model node as the board, so «Update PCB from schematic» keeps it.
    assert '(model "${KIPRJMOD}/Demo.step"' in footprint


def test_footprint_copy_without_step_has_no_model(tmp_path):
    preview = create_preview_project(_component(tmp_path, step=False))
    copy = preview.directory / f"{PREVIEW_LIBRARY_NICKNAME}.pretty" / "Demo.kicad_mod"
    assert "(model" not in copy.read_text(encoding="utf-8")


def test_schematic_embeds_the_symbol_and_places_it(tmp_path):
    preview = create_preview_project(_component(tmp_path))
    text = preview.schematic_path.read_text(encoding="utf-8")
    assert text.startswith(f"(kicad_sch\n\t(version {SCHEMATIC_FORMAT_VERSION})")
    assert '(paper "A4")' in text
    lib_id = f"{PREVIEW_LIBRARY_NICKNAME}:Demo"
    # Embedded under the library id, unit children keep their own names.
    assert f'(symbol "{lib_id}"' in text
    assert '(symbol "Demo_1_1"' in text
    assert f'(lib_id "{lib_id}")' in text
    assert '(property "Reference" "K1"' in text
    assert '(property "Value" "Demo"' in text
    assert f'(property "Footprint" "{PREVIEW_LIBRARY_NICKNAME}:Demo"' in text
    assert '(reference "K1")' in text
    assert '(project "Demo-preview"' in text
    # Library-only properties are not copied onto the placed symbol.
    placed = text.split("\t(lib_symbols", 1)[1].split("\n\t)\n", 1)[1]
    assert "ki_keywords" not in placed
    assert '(pin "1" (uuid' in placed and '(pin "2" (uuid' in placed


def test_placed_property_positions_follow_the_placement():
    text = preview_schematic_text(
        SYMBOL_LIBRARY.split("\n", 1)[1].rsplit(")", 1)[0].strip(), "Demo",
        footprint_id="X:Demo", project_name="p", sheet_uuid="s")
    centre_x, centre_y = kicad_library._SCHEMATIC_CENTRE
    # Library Y up (Reference at +5.08) -> schematic Y down (above the centre).
    assert f'(property "Reference" "K1" (at {centre_x:g} {round(centre_y - 5.08, 4):g} 0)' in text
    assert f'(property "Value" "Demo" (at {centre_x:g} {round(centre_y + 5.08, 4):g} 0)' in text


def test_every_unit_is_placed_with_its_pins_and_the_common_ones():
    text = preview_schematic_text(TWO_UNIT_BLOCK, "Dual", footprint_id="X:Dual",
                                  project_name="p", sheet_uuid="s")
    placed = re.findall(r"\(lib_id \"[^\"]+\"\)\n\t\t\(at ([\d.]+) [\d.]+ 0\)\n\t\t\(unit (\d+)\)", text)
    assert [unit for _, unit in placed] == ["1", "2"]
    assert placed[0][0] != placed[1][0]
    units = text.split("\t(symbol\n")[1:]
    assert '(pin "3"' in units[0] and '(pin "5"' not in units[0]
    assert '(pin "5"' in units[1] and '(pin "3"' not in units[1]
    assert all('(pin "8"' in unit for unit in units)
    assert text.count('(reference "U1")') == 2


@pytest.mark.parametrize(("prefix", "expected"), [
    ("U", "U1"), ("K", "K1"), ("J?", "J1"), ("SW3", "SW1"), ("", "U1"), ("?", "U1")])
def test_preview_reference_is_the_designator_plus_one(prefix, expected):
    assert preview_reference(prefix) == expected


def test_without_symbol_the_board_is_still_written(tmp_path):
    preview = create_preview_project(_component(tmp_path, symbol=None))
    assert preview.board_path.is_file()
    assert preview.schematic_path is None
    assert preview.schematic_skipped == "symbol_missing"
    assert not (preview.directory / "sym-lib-table").exists()
    assert not (preview.directory / "fp-lib-table").exists()


@pytest.mark.parametrize(("library", "reason"), [
    ("not a library", "symbol_unreadable"),
    ("(kicad_symbol_lib (version 1) (symbol \"A\") (symbol \"B\"))", "symbol_not_found"),
    ("(kicad_symbol_lib (version 1) (symbol \"Demo\" (extends \"Base\")))", "symbol_derived"),
    ("(kicad_symbol_lib (symbol \"Demo\"", "symbol_unreadable"),
])
def test_unusable_symbol_skips_only_the_schematic(tmp_path, library, reason):
    preview = create_preview_project(_component(tmp_path, symbol=library))
    assert preview.board_path.is_file()
    assert preview.schematic_skipped == reason


def test_single_symbol_of_another_name_is_used(tmp_path):
    preview = create_preview_project(
        _component(tmp_path, symbol=SYMBOL_LIBRARY.replace('"Demo', '"Other')))
    assert preview.schematic_skipped is None
    assert f'(lib_id "{PREVIEW_LIBRARY_NICKNAME}:Other")' in preview.schematic_path.read_text(
        encoding="utf-8")


def test_lost_symbol_removes_stale_schematic_in_the_own_folder(tmp_path):
    artifacts = _component(tmp_path)
    first = create_preview_project(artifacts)
    (artifacts.component_dir / "Demo.kicad_sym").unlink()
    second = create_preview_project(artifacts)
    assert second.schematic_skipped == "symbol_missing"
    for stale in (first.schematic_path, first.symbol_library_path,
                  first.directory / "sym-lib-table", first.directory / "fp-lib-table",
                  first.directory / f"{PREVIEW_LIBRARY_NICKNAME}.pretty"):
        assert not stale.exists()


def test_caller_folder_keeps_a_foreign_sym_lib_table(tmp_path):
    artifacts = _component(tmp_path)
    folder = tmp_path / "chosen"
    folder.mkdir()
    foreign = '(sym_lib_table (version 7) (lib (name "Mine")(type "KiCad")(uri "/x.kicad_sym")))\n'
    (folder / "sym-lib-table").write_text(foreign, encoding="utf-8")
    preview = create_preview_project(artifacts, folder)
    assert preview.schematic_skipped == "sym_lib_table_foreign"
    assert (folder / "sym-lib-table").read_text(encoding="utf-8") == foreign
    assert preview.board_path.is_file()
    # A table that is exactly the preview's own is refreshed normally.
    (folder / "sym-lib-table").write_bytes(
        kicad_library.preview_sym_lib_table("Demo.kicad_sym").encode("utf-8"))
    assert create_preview_project(artifacts, folder).schematic_skipped is None


@pytest.mark.parametrize(("relative", "reason"), [
    ("Demo.kicad_sym", "symbol_copy_foreign"),
    (f"{PREVIEW_LIBRARY_NICKNAME}.pretty/Demo.kicad_mod", "footprint_copy_foreign"),
    (f"{PREVIEW_LIBRARY_NICKNAME}.pretty/Other.kicad_mod", "footprint_copy_foreign"),
])
def test_caller_folder_keeps_foreign_library_copies(tmp_path, relative, reason):
    artifacts = _component(tmp_path)
    folder = tmp_path / "chosen"
    foreign = folder / relative
    foreign.parent.mkdir(parents=True)
    foreign.write_bytes(b"user content")
    preview = create_preview_project(artifacts, folder)
    assert preview.schematic_skipped == reason
    assert foreign.read_bytes() == b"user content"
    for name in ("sym-lib-table", "fp-lib-table", "Demo-preview.kicad_sch"):
        assert not (folder / name).exists()
    assert preview.board_path.is_file()


def test_caller_folder_refreshes_its_own_earlier_copies(tmp_path):
    # Negative of the case above: copies written by an earlier preview into
    # the same chosen folder are identical, so they are not foreign.
    artifacts = _component(tmp_path)
    folder = tmp_path / "chosen"
    assert create_preview_project(artifacts, folder).schematic_skipped is None
    assert create_preview_project(artifacts, folder).schematic_skipped is None


def test_a_failed_set_write_leaves_no_new_half_set(tmp_path, monkeypatch):
    artifacts = _component(tmp_path)
    folder = tmp_path / "chosen"
    real_publish = kicad_library._publish_scratch

    def failing_publish(scratch, target):
        """Refuse the schematic, the last file of the set."""
        if target.suffix == ".kicad_sch":
            raise PermissionError(13, "denied", str(target))
        real_publish(scratch, target)
    monkeypatch.setattr(kicad_library, "_publish_scratch", failing_publish)
    with pytest.raises(KiCadLibraryError) as raised:
        create_preview_project(artifacts, folder)
    assert raised.value.code == "preview_write_refused"
    for name in ("Demo.kicad_sym", "sym-lib-table", "fp-lib-table", "Demo-preview.kicad_sch",
                 f"{PREVIEW_LIBRARY_NICKNAME}.pretty"):
        assert not (folder / name).exists()
    # No scratch file left behind either.
    assert not [path for path in folder.rglob("*") if ".tmp" in path.name or path.name.endswith("~")]


def test_caller_folder_keeps_a_foreign_fp_lib_table(tmp_path):
    artifacts = _component(tmp_path)
    folder = tmp_path / "chosen"
    folder.mkdir()
    foreign = b'(fp_lib_table (version 7) (lib (name "Mine")(type "KiCad")(uri "/x.pretty")))\n'
    (folder / "fp-lib-table").write_bytes(foreign)
    preview = create_preview_project(artifacts, folder)
    assert preview.schematic_skipped == "fp_lib_table_foreign"
    assert (folder / "fp-lib-table").read_bytes() == foreign
    # Neither table is written when one of them is foreign.
    assert not (folder / "sym-lib-table").exists()
    assert preview.board_path.is_file()
    (folder / "fp-lib-table").write_bytes(kicad_library.preview_fp_lib_table().encode("utf-8"))
    assert create_preview_project(artifacts, folder).schematic_skipped is None


def test_preview_leaves_user_tables_untouched(tmp_path, monkeypatch):
    config = tmp_path / "cfg" / "10.0"
    config.mkdir(parents=True)
    tables = {name: config / name for name in ("fp-lib-table", "sym-lib-table")}
    for path in tables.values():
        path.write_text('(x (lib (name "Keep")(uri "/keep")))\n', encoding="utf-8")
    before = {name: path.read_bytes() for name, path in tables.items()}
    monkeypatch.setenv("APPDATA", str(tmp_path / "cfg"))
    artifacts = _component(tmp_path)
    preview = create_preview_project(artifacts)
    assert {name: path.read_bytes() for name, path in tables.items()} == before
    # The only tables created are the preview folder's own.
    created = sorted(path for path in tmp_path.rglob("*-lib-table")
                     if path not in tables.values())
    assert created == [preview.directory / "fp-lib-table", preview.directory / "sym-lib-table"]
    assert not (artifacts.component_dir / "sym-lib-table").exists()
    assert not (artifacts.component_dir / "fp-lib-table").exists()


def test_an_open_schematic_is_left_alone_and_the_board_refreshed(tmp_path):
    """CONTRACT CHANGED 2026-11-13 (pre-commit review): the Schematic Editor holding the preview
    schematic no longer stops the whole preview. Positive: the board is rewritten and the reason
    «schematic_locked» returned; negative: the open schematic set is neither rewritten nor removed."""
    artifacts = _component(tmp_path)
    preview = create_preview_project(artifacts)
    kicad_lock_file(preview.schematic_path).write_text("lock", encoding="utf-8")
    before = preview.schematic_path.read_bytes()
    symbol_before = preview.symbol_library_path.read_bytes()
    preview.board_path.write_text("stale board", encoding="utf-8")
    again = create_preview_project(artifacts)
    assert again.schematic_path is None and again.schematic_skipped == "schematic_locked"
    assert again.board_path.read_text(encoding="utf-8") != "stale board"
    assert preview.schematic_path.read_bytes() == before
    assert preview.symbol_library_path.read_bytes() == symbol_before


# --- launching both editors ----------------------------------------------------

def _executables(tmp_path: Path, *names: str) -> dict[str, Path]:
    """Empty files standing for KiCad editors in one bin folder."""
    folder = tmp_path / "bin"
    folder.mkdir(exist_ok=True)
    result = {}
    for name in names:
        path = folder / (name + (".exe" if kicad_library.os.name == "nt" else ""))
        path.write_bytes(b"")
        result[name] = path
    return result


def test_both_editors_are_launched(tmp_path):
    preview = create_preview_project(_component(tmp_path))
    exe = _executables(tmp_path, "pcbnew", "eeschema")
    calls = []
    launch = launch_preview_editors(preview, exe["pcbnew"],
                                    launcher=lambda e, d, cwd: calls.append((e, d)))
    expected = [(exe["pcbnew"], preview.board_path), (exe["eeschema"], preview.schematic_path)]
    assert calls == expected
    assert list(launch.launched) == expected
    assert launch.schematic_skipped is None


def _inside(path: Path, folder: Path) -> bool:
    """Whether `path` is `folder` or lies inside it."""
    path, folder = Path(path).resolve(), Path(folder).resolve()
    return path == folder or folder in path.parents


def test_editors_start_outside_the_project_with_absolute_documents(tmp_path):
    # Review 2026-11-13: a working folder inside the project kept Windows
    # from renaming the project folder while KiCad stayed open.
    artifacts = _component(tmp_path)
    preview = create_preview_project(artifacts)
    exe = _executables(tmp_path, "pcbnew", "eeschema")
    seen = []
    launch_preview_editors(preview, exe["pcbnew"],
                           launcher=lambda e, d, cwd: seen.append((d, cwd)))
    assert len(seen) == 2
    for document, cwd in seen:
        assert Path(document).is_absolute()
        assert not _inside(cwd, artifacts.component_dir)
        assert Path(cwd) == exe["pcbnew"].parent


def test_default_editor_start_uses_the_neutral_folder(tmp_path, monkeypatch):
    preview = create_preview_project(_component(tmp_path))
    exe = _executables(tmp_path, "pcbnew")
    seen = {}

    def fake_popen(command, **kwargs):
        """Record the Popen call instead of starting a program."""
        seen["command"], seen["cwd"] = command, kwargs["cwd"]
    monkeypatch.setattr(kicad_library.subprocess, "Popen", fake_popen)
    kicad_library.launch_preview(exe["pcbnew"], preview.board_path)
    assert not _inside(seen["cwd"], preview.directory.parent)
    assert Path(seen["command"][1]).is_absolute()


def test_neutral_working_dir_falls_back_to_the_temp_folder(tmp_path):
    assert kicad_library.neutral_working_dir(None) == Path(kicad_library.tempfile.gettempdir())
    assert kicad_library.neutral_working_dir(tmp_path / "no" / "such" / "cli.exe") == Path(
        kicad_library.tempfile.gettempdir())


def test_missing_eeschema_still_opens_the_board(tmp_path):
    preview = create_preview_project(_component(tmp_path))
    exe = _executables(tmp_path, "pcbnew")
    calls = []
    launch = launch_preview_editors(preview, exe["pcbnew"],
                                    launcher=lambda e, d, cwd: calls.append((e, d)))
    assert calls == [(exe["pcbnew"], preview.board_path)]
    assert launch.schematic_skipped == "eeschema_missing"


def test_eeschema_that_fails_to_start_does_not_raise(tmp_path):
    preview = create_preview_project(_component(tmp_path))
    exe = _executables(tmp_path, "pcbnew", "eeschema")

    def launcher(executable, document, cwd):
        """Fake launcher: the Schematic Editor cannot start."""
        if executable == exe["eeschema"]:
            raise OSError("denied")
    launch = launch_preview_editors(preview, exe["pcbnew"], launcher=launcher)
    assert launch.schematic_skipped == "eeschema_launch_failed"
    assert launch.schematic_error == "denied"
    assert launch.launched == ((exe["pcbnew"], preview.board_path),)


def test_board_launch_failure_still_raises(tmp_path):
    preview = create_preview_project(_component(tmp_path))
    exe = _executables(tmp_path, "pcbnew", "eeschema")

    def launcher(executable, document, cwd):
        """Fake launcher: nothing can start."""
        raise OSError("denied")
    with pytest.raises(KiCadLibraryError) as raised:
        launch_preview_editors(preview, exe["pcbnew"], launcher=launcher)
    assert raised.value.code == "launch_failed"


def test_skipped_schematic_opens_only_the_board(tmp_path):
    preview = create_preview_project(_component(tmp_path, symbol=None))
    exe = _executables(tmp_path, "pcbnew", "eeschema")
    calls = []
    launch = launch_preview_editors(preview, exe["pcbnew"],
                                    launcher=lambda e, d, cwd: calls.append((e, d)))
    assert calls == [(exe["pcbnew"], preview.board_path)]
    assert launch.schematic_skipped == "symbol_missing"


# --- 3D image ------------------------------------------------------------------

def test_render_command_uses_kicad_cli_options(tmp_path):
    command = preview_render_command("cli", tmp_path / "b.kicad_pcb", tmp_path / "o.png")
    assert command[:3] == ["cli", "pcb", "render"]
    assert command[command.index("--output") + 1] == str(tmp_path / "o.png")
    assert command[command.index("--rotate") + 1] == kicad_library.PREVIEW_RENDER_ROTATION
    for option in ("--width", "--height", "--side", "--background", "--quality", "--zoom"):
        assert option in command
    assert command[-1] == str(tmp_path / "b.kicad_pcb")


def _cli(tmp_path: Path) -> Path:
    """An empty file standing for kicad-cli."""
    return _executables(tmp_path, "kicad-cli")["kicad-cli"]


def test_render_writes_the_image_named_after_the_component(tmp_path):
    preview = create_preview_project(_component(tmp_path))
    seen = {}

    def runner(command, **kwargs):
        """Fake kicad-cli: writes a PNG where --output says."""
        seen["command"], seen["kwargs"] = command, kwargs
        Path(command[command.index("--output") + 1]).write_bytes(b"\x89PNG fake")
        return subprocess.CompletedProcess(command, 0, "", "")
    result = render_preview_image(preview, _cli(tmp_path), has_step=True, runner=runner)
    assert result.reason is None
    assert result.image_path == preview.directory / "Demo-preview-3d.png"
    assert result.image_path.read_bytes() == b"\x89PNG fake"
    assert seen["command"][-1] == str(preview.board_path)
    assert seen["kwargs"]["timeout"] == kicad_library.PREVIEW_RENDER_TIMEOUT_S
    # kicad-cli runs outside the project, on absolute paths.
    assert not _inside(seen["kwargs"]["cwd"], preview.directory.parent)
    assert Path(seen["command"][-1]).is_absolute()
    # No scratch file left behind.
    assert sorted(p.name for p in preview.directory.glob("*.png")) == ["Demo-preview-3d.png"]


@pytest.mark.parametrize(("outcome", "reason"), [
    ("timeout", "render_timeout"),
    ("fail", "render_failed"),
    ("silent", "render_no_image"),
    ("oserror", "render_start_failed"),
])
def test_render_failures_return_a_reason_and_no_stale_image(tmp_path, outcome, reason):
    preview = create_preview_project(_component(tmp_path))
    stale = preview.directory / "Demo-preview-3d.png"
    stale.write_bytes(b"old picture")

    def runner(command, **kwargs):
        """Fake kicad-cli with one kind of failure."""
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        if outcome == "oserror":
            raise OSError("cannot start")
        return subprocess.CompletedProcess(
            command, 1 if outcome == "fail" else 0, "", "Unknown command: render")
    result = render_preview_image(preview, _cli(tmp_path), has_step=True, runner=runner)
    assert result.image_path is None
    assert result.reason == reason
    assert not stale.exists()
    assert not list(preview.directory.glob("*.png"))


def test_render_without_step_or_cli_runs_nothing(tmp_path):
    preview = create_preview_project(_component(tmp_path, step=False))

    def runner(*args, **kwargs):
        """kicad-cli must not be called."""
        raise AssertionError("kicad-cli called")
    assert render_preview_image(preview, _cli(tmp_path), has_step=False,
                                runner=runner).reason == "no_step"
    assert render_preview_image(preview, None, has_step=True,
                                runner=runner).reason == "kicad_cli_missing"
    assert render_preview_image(preview, tmp_path / "absent.exe", has_step=True,
                                runner=runner).reason == "kicad_cli_missing"


# --- real kicad-cli, read-only on temporary files --------------------------------

def _real_cli() -> str:
    """Installed kicad-cli, or skip: the GitHub runner has no KiCad."""
    cli = find_kicad_cli()
    if not cli:
        pytest.skip("KiCad (kicad-cli) is not installed")
    return cli


def test_real_kicad_cli_loads_the_preview_schematic(tmp_path):
    cli = _real_cli()
    preview = create_preview_project(_component(tmp_path))
    report = tmp_path / "erc.rpt"
    proc = subprocess.run(
        [cli, "sch", "erc", "-o", str(report), str(preview.schematic_path)],
        capture_output=True, text=True, errors="replace", timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    text = report.read_text(encoding="utf-8", errors="replace")
    # The symbol was resolved: ERC names its pins under the placed reference.
    assert "Symbol K1 Pin 1" in text and "Symbol K1 Pin 2" in text
    # The footprint field resolves through the preview's own table.
    assert "footprint_link_issues" not in text


def test_real_kicad_cli_renders_the_preview_board(tmp_path):
    cli = _real_cli()
    preview = create_preview_project(_component(tmp_path, step=False))
    # Rendered even without a model: here only the command's acceptance by
    # kicad-cli is checked, so the bare board is enough.
    result = render_preview_image(preview, cli, has_step=True)
    assert result.reason is None, result.detail
    assert result.image_path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_preview_project_dataclass_defaults_keep_old_callers():
    project = PreviewProject(Path("d"), Path("d/b.kicad_pcb"), Path("d/b.kicad_pro"))
    assert project.schematic_path is None and project.schematic_skipped is None


# --- image window ----------------------------------------------------------------

def _png(path: Path, width: int = 400, height: int = 300) -> Path:
    """Write a real PNG of the given size."""
    from PyQt6.QtGui import QColor, QImage
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(30, 90, 60))
    assert image.save(str(path), "PNG")
    return path


def test_image_window_is_non_modal_scaled_and_retranslated(qapp, tmp_path):
    from PyQt6.QtCore import Qt
    from kicadgen.gui.render_preview_window import HINT_SOURCE, RenderPreviewWindow
    language = {"value": "it"}

    def translate(source):
        """Fake catalogue: prefixes the language code."""
        return f"[{language['value']}] {source}"
    window = RenderPreviewWindow(_png(tmp_path / "x.png"), "Demo", translate)
    try:
        assert window.windowModality() == Qt.WindowModality.NonModal
        assert window.isWindow()
        assert window.has_image()
        assert "Demo" in window.windowTitle() and window.windowTitle().startswith("[it]")
        assert window.hint_label.text() == f"[it] {HINT_SOURCE}"
        assert "Alt+3" in HINT_SOURCE
        window.resize(300, 300)
        window._fit_image()
        pixmap = window.image_label.pixmap()
        # Scaled down to the label, proportions kept (400 x 300 -> 4:3).
        assert pixmap.width() <= window.image_label.width()
        assert abs(pixmap.width() / pixmap.height() - 4 / 3) < 0.02
        language["value"] = "en"
        window.retranslate()
        assert window.windowTitle().startswith("[en]")
        assert window.hint_label.text().startswith("[en]")
    finally:
        window.deleteLater()


def test_image_window_with_an_unreadable_file_says_so(qapp, tmp_path):
    from kicadgen.gui.render_preview_window import RenderPreviewWindow
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not a png")
    window = RenderPreviewWindow(broken, "Demo", lambda source: source)
    try:
        assert not window.has_image()
        assert str(broken) in window.image_label.text()
    finally:
        window.deleteLater()
