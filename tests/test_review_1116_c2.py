"""@brief Fixes of the KiCad delivery findings of the 2026-11-16 review (R9-1 .. R9-7, R4-1).

@details Every test writes only below pytest's temporary directory; KiCad is
never needed (kicad-cli and its processes are faked, or a plain Python child
stands for a hung command). Hard links are made with ``os.link`` (no
privilege on NTFS); a volume without them skips those tests. Each rule has a
positive and a negative case that differ for a structural reason.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from kicadgen.cad3d import process_log
from kicadgen.generators.symbol import serialize_symbol, symbol_inputs
from kicadgen.services import component_check as check
from kicadgen.services import kicad_library
from kicadgen.services.kicad_library import (
    KiCadLibraryError,
    ROLLBACK_DIR_PREFIX,
    ROLLBACK_JOURNAL,
    install_component_library,
)
from test_kicad_library import _artifacts

REPO = Path(__file__).resolve().parents[1]


def _symbol_text(pin_name='GPIO1'):
    """A component symbol library as the generator writes it, one pin named `pin_name`."""
    pins, body = symbol_inputs([{'pin': '1', 'name': pin_name}])
    return serialize_symbol('Demo', pins, footprint='KiCadGen:Demo', body=body)


def _component_with_symbol(tmp_path, pin_name='GPIO1'):
    """Generated artifacts of «Demo» with their own .kicad_sym."""
    art = _artifacts(tmp_path)
    (art.component_dir / 'Demo.kicad_sym').write_text(_symbol_text(pin_name), encoding='utf-8')
    return art


@pytest.fixture(autouse=True)
def _private_locks(tmp_path, monkeypatch):
    """Install locks in this test's own folder, never the user's temporary folder."""
    folder = tmp_path / 'locks'
    monkeypatch.setattr(kicad_library, '_install_lock_dir', lambda: folder)
    return folder


def _rollback_dirs(root):
    """Rollback folders present in a library root."""
    return sorted(p for p in Path(root).iterdir() if p.name.startswith(ROLLBACK_DIR_PREFIX))


# ── R9-1: a hard-linked shared symbol library cut short half-way ─────────────

def _hard_linked_library(tmp_path):
    """Install «Demo» with its symbol, hard-link the shared library, change the symbol.

    @return (artifacts, project, library, other name, original library bytes).
    """
    art = _component_with_symbol(tmp_path)
    project = tmp_path / 'project'
    project.mkdir()
    install_component_library(art, project)
    library = project / 'KiCadGen.kicad_sym'
    other = tmp_path / 'dotfiles.kicad_sym'
    try:
        os.link(library, other)
    except OSError as exc:
        pytest.skip(f'hard links unavailable here: {exc}')
    original = library.read_bytes()
    # A different symbol: the shared library must be rewritten (in place).
    (art.component_dir / 'Demo.kicad_sym').write_text(_symbol_text('GPIO2'), encoding='utf-8')
    return art, project, library, other, original


def _disk_full_on(monkeypatch, failing_calls):
    """Make the module's in-place copy write half the bytes, then fail like a full disk.

    @param failing_calls 1-based call numbers of ``shutil.copyfileobj`` that fail.
    """
    real_copy = shutil.copyfileobj
    calls = [0]

    def copy(source, target, *args):
        """Half the data, then ENOSPC, on the chosen calls; a normal copy otherwise."""
        calls[0] += 1
        if calls[0] in failing_calls:
            data = source.read()
            target.write(data[:len(data) // 2])
            target.flush()
            raise OSError(28, 'No space left on device')
        return real_copy(source, target, *args)
    monkeypatch.setattr(kicad_library.shutil, 'copyfileobj', copy)
    return calls


def test_a_hard_linked_library_cut_short_half_way_is_restored(tmp_path, monkeypatch):
    """@brief Positive: the in-place write fails half-way -> the library is back under both names."""
    art, project, library, other, original = _hard_linked_library(tmp_path)
    _disk_full_on(monkeypatch, {1})
    with pytest.raises(OSError):
        install_component_library(art, project, overwrite=True)
    assert library.read_bytes() == original
    assert other.read_bytes() == original
    # Complete rollback: its folder is gone, nothing to report.
    assert _rollback_dirs(project) == []


def test_a_library_that_cannot_be_restored_keeps_its_rollback_copy(tmp_path, monkeypatch):
    """@brief Negative: the restore fails too -> ``rollback_incomplete`` and the copy is KEPT."""
    art, project, library, _other, original = _hard_linked_library(tmp_path)
    _disk_full_on(monkeypatch, {1, 2})
    with pytest.raises(KiCadLibraryError) as raised:
        install_component_library(art, project, overwrite=True)
    assert raised.value.code == 'rollback_incomplete'
    kept = Path(raised.value.context['rollback_dir'])
    assert kept.is_dir()
    assert any(copy.read_bytes() == original for copy in kept.iterdir() if copy.is_file())
    assert library.read_bytes() != original  # still cut short: the copy is all there is


def test_an_in_place_open_refused_before_writing_is_a_clean_refusal(tmp_path, monkeypatch):
    """@brief Negative: the in-place open is refused (nothing truncated) -> install_write_refused,
    not a false rollback_incomplete, although the target was registered before the write."""
    art, project, library, other, original = _hard_linked_library(tmp_path)
    real_in_place = kicad_library._write_in_place
    calls = [0]

    def refuse_first(source, target):
        """The first in-place write is refused before opening the target."""
        calls[0] += 1
        if calls[0] == 1:
            raise PermissionError(13, 'Access is denied', str(target))
        return real_in_place(source, target)
    monkeypatch.setattr(kicad_library, '_write_in_place', refuse_first)
    with pytest.raises(KiCadLibraryError) as raised:
        install_component_library(art, project, overwrite=True)
    assert raised.value.code == 'install_write_refused'
    assert calls[0] == 1  # nothing to put back: the file still held its bytes
    assert library.read_bytes() == original == other.read_bytes()


# ── R9-2: two installs into one library at once ───────────────────────────

def _fast_wait(monkeypatch, seconds=0.3):
    """A short lock wait for the tests."""
    monkeypatch.setattr(kicad_library, 'INSTALL_LOCK_WAIT_S', seconds)
    monkeypatch.setattr(kicad_library, '_lock_pause', lambda _s: time.sleep(0.02))


def test_an_install_into_a_root_another_install_holds_is_refused_untouched(tmp_path, monkeypatch):
    """@brief Positive: the root is locked -> ``install_busy`` naming it, and nothing written."""
    _fast_wait(monkeypatch)
    art = _component_with_symbol(tmp_path)
    project = tmp_path / 'project'
    project.mkdir()
    with kicad_library._install_locks([project]):
        with pytest.raises(KiCadLibraryError) as raised:
            install_component_library(art, project)
    assert raised.value.code == 'install_busy'
    assert Path(raised.value.context['path']) == project
    assert list(project.iterdir()) == []


def test_an_install_runs_once_the_other_released_or_on_another_root(tmp_path, monkeypatch):
    """@brief Negative: a released lock, or a lock on an unrelated root, does not block."""
    _fast_wait(monkeypatch)
    art = _component_with_symbol(tmp_path)
    project = tmp_path / 'project'
    elsewhere = tmp_path / 'elsewhere'
    project.mkdir()
    with kicad_library._install_locks([project]):
        pass
    with kicad_library._install_locks([elsewhere]):
        result = install_component_library(art, project)
    assert result.footprint_path.is_file()


def test_a_shared_global_table_is_locked_too(tmp_path, monkeypatch):
    """@brief Positive: two roots registering in ONE table -> the table's lock excludes them."""
    _fast_wait(monkeypatch)
    art = _component_with_symbol(tmp_path)
    config = tmp_path / 'cfg'
    config.mkdir()
    table = config / 'fp-lib-table'
    table.write_text('(fp_lib_table\n  (version 7)\n)\n', encoding='utf-8')
    (config / 'sym-lib-table').write_text('(sym_lib_table\n  (version 7)\n)\n', encoding='utf-8')
    with kicad_library._install_locks([table]):
        with pytest.raises(KiCadLibraryError) as raised:
            install_component_library(art, tmp_path / 'libs', project_library=False,
                                      table_path=table)
    assert raised.value.code == 'install_busy'
    install_component_library(art, tmp_path / 'libs', project_library=False, table_path=table)


def test_two_threads_installing_at_once_keep_both_symbols(tmp_path, monkeypatch):
    """@brief Positive: two concurrent installs of different symbols into one library keep both.

    Off the GUI thread a caller may wait for the lock: the wait is given here
    (the default is no wait, see the follow-up tests below).
    """
    monkeypatch.setattr(kicad_library, 'INSTALL_LOCK_WAIT_S', 60.0)
    first = _component_with_symbol(tmp_path / 'a')
    second_dir = tmp_path / 'b' / 'Other'
    shutil.copytree(first.component_dir, second_dir)
    # Deepest first, so a renamed folder never invalidates a path still to rename.
    for path in sorted(second_dir.rglob('Demo*'), key=lambda p: len(p.parts), reverse=True):
        path.rename(path.with_name(path.name.replace('Demo', 'Other')))
    symbol = (second_dir / 'Other.kicad_sym').read_text(encoding='utf-8')
    (second_dir / 'Other.kicad_sym').write_text(symbol.replace('"Demo', '"Other'), encoding='utf-8')
    second = kicad_library.discover_component_artifacts(second_dir)
    project = tmp_path / 'project'
    project.mkdir()
    errors = []

    def install(artifacts):
        """One install, its error kept for the assertion."""
        try:
            install_component_library(artifacts, project)
        except Exception as exc:  # noqa: BLE001 - reported by the assertion below
            errors.append(exc)
    threads = [threading.Thread(target=install, args=(each,)) for each in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert errors == []
    text = (project / 'KiCadGen.kicad_sym').read_text(encoding='utf-8')
    assert '(symbol "Demo"' in text and '(symbol "Other"' in text


_HOLDER = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from kicadgen.services import kicad_library as k
k._install_lock_dir = lambda: Path(sys.argv[2])
fd = k._open_lock_file(k._install_lock_key(sys.argv[3]))
assert k._try_lock(fd)
print('locked', flush=True)
if sys.argv[4] == 'die':
    import os
    os._exit(0)
sys.stdin.readline()
"""


def _holder(locks, root, mode):
    """A child process that takes the lock of `root`, then dies at once or waits for stdin."""
    process = subprocess.Popen(
        [sys.executable, '-c', _HOLDER, str(REPO), str(locks), str(root), mode],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, cwd=str(REPO))
    assert process.stdout.readline().strip() == 'locked'
    return process


def test_a_live_process_holding_the_lock_blocks_another_process(tmp_path, monkeypatch, _private_locks):
    """@brief Positive: the lock excludes ANOTHER PROCESS (two program windows)."""
    _fast_wait(monkeypatch)
    project = tmp_path / 'project'
    project.mkdir()
    process = _holder(_private_locks, project, 'wait')
    try:
        with pytest.raises(KiCadLibraryError) as raised:
            with kicad_library._install_locks([project]):
                pass
        assert raised.value.code == 'install_busy'
    finally:
        process.communicate('\n', timeout=30)


def test_a_lock_left_by_a_crashed_process_is_free(tmp_path, monkeypatch, _private_locks):
    """@brief Negative: the holder ended without releasing (a crash) -> the lock is free at once,
    no stale-lock guessing (the operating system released it)."""
    _fast_wait(monkeypatch)
    project = tmp_path / 'project'
    project.mkdir()
    process = _holder(_private_locks, project, 'die')
    process.wait(30)
    with kicad_library._install_locks([project]):
        pass


# ── R9-3 / R4-1: a stop is sticky for the check it stopped ────────────────

class _FakeProcess:
    """Stands for a kicad-cli Popen: finishes at once with exit 0."""

    def __init__(self, command, **_kwargs):
        """Record the command."""
        self.command = command
        self.returncode = 0
        self.pid = 4242

    def communicate(self, timeout=None):
        """No output."""
        return '', ''

    def poll(self):
        """Already finished."""
        return self.returncode

    def wait(self, timeout=None):
        """Already finished."""
        return self.returncode


@pytest.fixture
def fake_cli(monkeypatch):
    """kicad-cli faked: started commands and killed processes are recorded."""
    started, killed = [], []

    def popen(command, **kwargs):
        """A fake process; the hook may act at the very moment of the start."""
        started.append(command)
        hook = popen.hook
        if hook is not None:
            hook()
        return _FakeProcess(command, **kwargs)
    popen.hook = None
    monkeypatch.setattr(check.subprocess, 'Popen', popen)
    monkeypatch.setattr(process_log, 'contain_process', lambda process: None)
    monkeypatch.setattr(process_log, 'release_process', lambda process: None)
    monkeypatch.setattr(process_log, 'reap_killed_child', lambda process: True)
    monkeypatch.setattr(process_log, 'kill_process_tree', lambda process: killed.append(process))
    return started, killed, popen


def test_a_check_stopped_between_two_commands_starts_no_further_command(fake_cli):
    """@brief Positive: a stop while no command runs -> the next command is never started."""
    started, _killed, _popen = fake_cli

    @check._stoppable_run
    def run():
        """One command, a stop, another command."""
        check._run_cli(['kicad-cli', 'first'])
        check.stop_cli_commands()
        check._run_cli(['kicad-cli', 'second'])
    with pytest.raises(check.CliStopped):
        run()
    assert started == [['kicad-cli', 'first']]


def test_a_check_started_after_the_stop_runs_normally(fake_cli):
    """@brief Negative: the stop is for the check that was running, not for the next one."""
    started, _killed, _popen = fake_cli
    check.stop_cli_commands()

    @check._stoppable_run
    def run():
        """Two commands, no stop."""
        return [check._run_cli(['kicad-cli', name]).returncode for name in ('a', 'b')]
    assert run() == [0, 0]
    assert len(started) == 2


def test_a_command_started_at_the_moment_of_the_stop_is_killed(fake_cli):
    """@brief Positive: the stop lands between the check and the registration -> killed, CliStopped."""
    started, killed, popen = fake_cli
    popen.hook = check.stop_cli_commands

    @check._stoppable_run
    def run():
        """One command whose start races with a stop."""
        check._run_cli(['kicad-cli', 'racing'])
    with pytest.raises(check.CliStopped):
        run()
    assert len(started) == 1 and len(killed) == 1


def test_a_command_outside_any_check_is_not_affected_by_a_stop(fake_cli):
    """@brief Negative: no running check recorded in this thread -> nothing sticky."""
    check.stop_cli_commands()
    assert check._run_cli(['kicad-cli', 'version']).returncode == 0


def test_verify_component_is_a_stoppable_run():
    """@brief The check entry point records its stop epoch (the wrapper keeps the docstring)."""
    assert check.verify_component.__wrapped__ is not None
    assert check.verify_component.__doc__.startswith('@brief')


# ── R9-4: the table rows KiCad finds the libraries through ────────────────

def _installed_project(tmp_path):
    """«Demo» with its symbol installed into a project; (artifacts, project, rows())."""
    art = _component_with_symbol(tmp_path)
    project = tmp_path / 'project'
    project.mkdir()
    install_component_library(art, project)
    installs = [{'scope': 'project', 'pretty_dir': str(project / 'KiCadGen.pretty')}]

    def rows():
        """The «Libreria KiCad» rows of this installation."""
        return check.library_checks('Demo', art.footprint_path.read_text(encoding='utf-8'),
                                    installs, art.step_path,
                                    symbol_path=art.component_dir / 'Demo.kicad_sym')
    return art, project, rows


def test_a_row_removed_disabled_or_repointed_is_not_up_to_date(tmp_path):
    """@brief Positive: each way KiCad stops seeing the library is named, never «aggiornata»."""
    _art, project, rows = _installed_project(tmp_path)
    table = project / 'fp-lib-table'
    symbols = project / 'sym-lib-table'
    good_table = table.read_text(encoding='utf-8')
    good_symbols = symbols.read_text(encoding='utf-8')

    def detail():
        """The single row's detail; it must be a WARN."""
        (row,) = rows()
        assert row.status == 'WARN' and row.action == check.ACTION_OPEN_KICAD_TAB
        return row.detail
    table.write_text('(fp_lib_table\n  (version 7)\n)\n', encoding='utf-8')
    assert 'riga KiCadGen mancante in fp-lib-table' in detail()
    table.write_text(good_table.replace('(descr', '(disabled)(descr'), encoding='utf-8')
    assert 'riga KiCadGen disattivata in fp-lib-table' in detail()
    table.write_text(good_table.replace('${KIPRJMOD}/KiCadGen.pretty', '${KIPRJMOD}/Other.pretty'),
                     encoding='utf-8')
    assert 'non punta a questa libreria' in detail()
    table.unlink()
    assert 'tabella fp-lib-table mancante' in detail()
    table.write_text(good_table, encoding='utf-8')
    symbols.write_text('(sym_lib_table\n  (version 7)\n)\n', encoding='utf-8')
    assert 'riga KiCadGen mancante in sym-lib-table' in detail()
    symbols.write_text(good_symbols, encoding='utf-8')
    assert [r.status for r in rows()] == ['OK']


def test_a_row_spelled_differently_for_the_same_place_is_up_to_date(tmp_path):
    """@brief Negative: an absolute URI naming the same folder as ``${KIPRJMOD}`` -> still OK."""
    _art, project, rows = _installed_project(tmp_path)
    table = project / 'fp-lib-table'
    text = table.read_text(encoding='utf-8')
    table.write_text(text.replace('${KIPRJMOD}/KiCadGen.pretty',
                                  (project / 'KiCadGen.pretty').as_posix()), encoding='utf-8')
    assert [r.status for r in rows()] == ['OK']


# ── R9-5: a hung kicad-cli is ended with its whole tree, the wait bounded ──

_HANGING = ('import subprocess, sys, time; '
            'subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"]); '
            'time.sleep(120)')


def test_a_hung_command_with_a_child_holding_its_pipes_ends_at_the_timeout():
    """@brief Positive: a grandchild keeps the pipes open -> TimeoutExpired within a bound.

    ``subprocess.run`` killed only the direct child and then waited for the
    pipes without a bound (here 120 s).
    """
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        kicad_library._run_contained([sys.executable, '-c', _HANGING], timeout=2)
    assert time.monotonic() - started < 30


def test_a_quick_command_returns_its_output():
    """@brief Negative: a command that ends returns its exit code and text."""
    proc = kicad_library._run_contained(
        [sys.executable, '-c', 'import sys; print("10.0.8"); sys.exit(3)'], timeout=30)
    assert proc.returncode == 3 and proc.stdout.strip() == '10.0.8'


def test_the_preview_render_uses_the_tree_killing_runner(tmp_path, monkeypatch):
    """@brief The render and the version probe go through `_run_contained` by default."""
    art = _artifacts(tmp_path)
    preview = kicad_library.create_preview_project(art)
    cli = tmp_path / 'kicad-cli.exe'
    cli.write_bytes(b'x')
    seen = []

    def fake(command, **kwargs):
        """Record the call, report a failed render."""
        seen.append(command)
        return subprocess.CompletedProcess(command, 1, '', 'failed')
    monkeypatch.setattr(kicad_library, '_run_contained', fake)
    monkeypatch.setattr(kicad_library, '_KICAD_CLI_VERSION_CACHE', {})
    result = kicad_library.render_preview_image(preview, cli, has_step=True)
    assert result.reason == 'render_failed' and seen[0][1:3] == ['pcb', 'render']
    assert kicad_library._run_kicad_cli_version(cli) is None
    assert seen[1][1:] == ['version']


# ── R9-6: DRC and ERC judge the footprint bytes the checks read ───────────

def _capture_preview(monkeypatch):
    """Record the footprint bytes the preview project is built from."""
    seen = []
    real = check.create_preview_project

    def capture(artifacts, *args, **kwargs):
        """Keep the footprint the preview reads, then build it."""
        seen.append(Path(artifacts.footprint_path).read_bytes())
        return real(artifacts, *args, **kwargs)
    monkeypatch.setattr(check, 'create_preview_project', capture)
    return seen


def test_the_preview_is_built_from_the_bytes_the_checks_read(tmp_path, monkeypatch):
    """@brief Positive: the file is rewritten right after the read -> the preview has the read bytes."""
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: None)
    art = _artifacts(tmp_path)
    fp = art.footprint_path
    checked = fp.read_bytes()
    real_read = check._read_component_file

    def read_then_rewrite(path):
        """Read the file, then let «the window» rewrite the footprint."""
        data = real_read(path)
        if Path(path) == fp:
            fp.write_bytes(checked.replace(b'(size 1 1)', b'(size 2 2)'))
        return data
    monkeypatch.setattr(check, '_read_component_file', read_then_rewrite)
    seen = _capture_preview(monkeypatch)
    check.verify_component(art.component_dir, 'Demo', [{'pin': '1', 'name': 'VCC'}])
    assert seen == [checked]
    assert fp.read_bytes() != checked


def test_a_footprint_that_could_not_be_read_builds_no_preview(tmp_path, monkeypatch):
    """@brief Negative: the footprint is held by another program -> no preview from a second read;
    the DRC row says the footprint could not be read."""
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: None)
    art = _artifacts(tmp_path)
    fp = art.footprint_path
    real_read = check._read_component_file

    def busy(path):
        """The footprint is locked; other files read normally."""
        if Path(path) == fp:
            raise check._ComponentFileBusy(path, PermissionError(13, 'in use'))
        return real_read(path)
    monkeypatch.setattr(check, '_read_component_file', busy)
    seen = _capture_preview(monkeypatch)
    results = check.verify_component(art.component_dir, 'Demo', [{'pin': '1', 'name': 'VCC'}])
    assert seen == []
    drc = [r for r in results if r.code == 'drc']
    assert drc and drc[0].status == 'SKIP' and 'Footprint non leggibile.' in drc[0].detail


# ── R9-7: an install killed half-way is discoverable at the next install ──

def test_an_install_killed_half_way_is_reported_by_the_next_one(tmp_path, monkeypatch):
    """@brief Positive: the rollback folder of a killed install (journal, no marker) is reported."""
    art = _component_with_symbol(tmp_path)
    project = tmp_path / 'project'
    project.mkdir()
    install_component_library(art, project)
    (art.component_dir / 'Demo.kicad_sym').write_text(_symbol_text('GPIO2'), encoding='utf-8')

    class Killed(BaseException):
        """Stands for the process ending right after the first change."""

    real_publish = kicad_library._publish_scratch

    def publish_then_die(scratch, target, **kwargs):
        """Publish one file, then «crash» without any rollback."""
        real_publish(scratch, target, **kwargs)
        raise Killed()
    monkeypatch.setattr(kicad_library, '_publish_scratch', publish_then_die)
    monkeypatch.setattr(kicad_library, '_rollback_install', lambda *a, **k: [])
    monkeypatch.setattr(kicad_library, '_dispose_rollback_dir', lambda folder: folder)
    with pytest.raises(Killed):
        install_component_library(art, project, overwrite=True)
    monkeypatch.undo()
    monkeypatch.setattr(kicad_library, '_install_lock_dir', lambda: tmp_path / 'locks')
    (left,) = _rollback_dirs(project)
    journal = (left / ROLLBACK_JOURNAL).read_text(encoding='utf-8')
    assert str(project / 'KiCadGen.kicad_sym') in journal
    result = install_component_library(art, project, overwrite=True)
    assert result.earlier_rollbacks == (left,)
    assert left.is_dir()  # reported, never removed


def test_a_clean_history_reports_no_earlier_rollback(tmp_path):
    """@brief Negative: installs that ended normally, and a folder marked disposable, are not reported."""
    art = _component_with_symbol(tmp_path)
    project = tmp_path / 'project'
    project.mkdir()
    install_component_library(art, project)
    marked = project / f'{ROLLBACK_DIR_PREFIX}{"a" * 32}'
    marked.mkdir()
    (marked / kicad_library.ROLLBACK_DISPOSABLE_MARKER).write_bytes(b'')
    result = install_component_library(art, project, overwrite=True)
    assert result.earlier_rollbacks == ()


# ── R11-5: the «Solo nome principale» choice is read once per check ────────

@pytest.mark.parametrize('at_start', [True, False])
def test_the_pin_name_mode_is_the_one_of_the_start_of_the_check(tmp_path, monkeypatch, at_start):
    """@brief Positive (True) and negative (False): the window flips the choice while the check runs
    -> the symbol is built with the mode the check STARTED with, whichever it was."""
    from kicadgen.generators import symbol as symbol_module
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: None)
    art = _artifacts(tmp_path)
    previous = symbol_module.primary_pin_names_enabled()
    symbol_module.set_primary_pin_names(at_start)
    seen = []
    real_inputs = check.symbol_inputs

    def record(rows, **kwargs):
        """Keep the mode the check asked for."""
        seen.append(kwargs.get('primary_names'))
        return real_inputs(rows, **kwargs)
    monkeypatch.setattr(check, 'symbol_inputs', record)

    def flip(_result):
        """The user toggles the choice after the first row."""
        symbol_module.set_primary_pin_names(not at_start)
    try:
        check.verify_component(art.component_dir, 'Demo', [{'pin': '1', 'name': 'IO5 (RX, TX)'}],
                               emit=flip)
    finally:
        symbol_module.set_primary_pin_names(previous)
    assert seen == [at_start]


# ── Follow-ups of the review of these fixes ──────────────────────────────

def test_on_start_sees_the_contained_process_and_a_refusal_kills_it():
    """@brief Follow-up 1: the hook gets the process before the wait; raising from it ends the
    tree at once (positive); a hook that accepts lets the command finish (negative)."""
    seen = []
    proc = kicad_library._run_contained([sys.executable, '-c', 'print("ok")'], timeout=30,
                                        on_start=seen.append)
    assert proc.stdout.strip() == 'ok' and len(seen) == 1 and seen[0].pid

    def refuse(_process):
        """A stop was asked before the start."""
        raise subprocess.SubprocessError('stopped')
    started = time.monotonic()
    with pytest.raises(subprocess.SubprocessError):
        kicad_library._run_contained([sys.executable, '-c', 'import time; time.sleep(60)'],
                                     timeout=60, on_start=refuse)
    assert time.monotonic() - started < 30


def test_the_render_worker_runs_through_the_contained_runner(qapp, monkeypatch):
    """@brief Follow-up 1: the GUI render path keeps the Job Object and the bounded reap."""
    from kicadgen.gui import quit_wait
    from kicadgen.gui.workers import PreviewRenderWorker
    calls = []

    def contained(command, **kwargs):
        """Record the hook the worker registers its process with."""
        calls.append(kwargs)
        kwargs['on_start'](_FakeProcess(command))
        return subprocess.CompletedProcess(command, 0, '', '')
    monkeypatch.setattr(kicad_library, '_run_contained', contained)
    worker = PreviewRenderWorker(None, None, has_step=True)
    try:
        worker._run(['kicad-cli', 'pcb', 'render'], capture_output=True, timeout=5, cwd='.')
        assert calls and callable(calls[0]['on_start']) and calls[0]['timeout'] == 5
        assert worker._process is None
    finally:
        quit_wait.disconnect_quit_wait(worker)
        worker.deleteLater()


def test_a_busy_library_is_refused_at_once_by_default(tmp_path):
    """@brief Follow-up 2 positive: no wait on the GUI thread -> install_busy immediately."""
    art = _component_with_symbol(tmp_path)
    project = tmp_path / 'project'
    project.mkdir()
    started = time.monotonic()
    with kicad_library._install_locks([project]):
        with pytest.raises(KiCadLibraryError) as raised:
            install_component_library(art, project)
    assert raised.value.code == 'install_busy'
    assert time.monotonic() - started < 2


def test_a_caller_off_the_gui_thread_may_wait_for_the_lock(tmp_path):
    """@brief Follow-up 2 negative: an explicit wait gets the lock once the holder releases it."""
    project = tmp_path / 'project'
    project.mkdir()
    holder = kicad_library._install_locks([project])
    holder.__enter__()
    threading.Timer(0.3, holder.__exit__, (None, None, None)).start()
    with kicad_library._install_locks([project], wait_s=20):
        pass


def test_a_stop_after_the_worker_was_created_stops_its_check(fake_cli):
    """@brief Follow-up 3 positive: the epoch captured at creation -> a stop before the run
    began still refuses every command of that run; negative: captured after the stop."""
    started, _killed, _popen = fake_cli
    epoch = check.current_stop_epoch()
    check.stop_cli_commands()

    @check._stoppable_run
    def run(stop_epoch=None):
        """One command."""
        check._run_cli(['kicad-cli', 'x'])
    with pytest.raises(check.CliStopped):
        run(stop_epoch=epoch)
    assert started == []
    run(stop_epoch=check.current_stop_epoch())
    assert len(started) == 1


def test_the_check_worker_captures_the_epoch_when_created(qapp):
    """@brief Follow-up 3: the panel's worker records the epoch in its constructor."""
    from kicadgen.gui import quit_wait
    from kicadgen.gui.component_check_dialog import _CheckWorker
    before = check.current_stop_epoch()
    worker = _CheckWorker({})
    try:
        check.stop_cli_commands()
        assert worker.stop_epoch == before != check.current_stop_epoch()
    finally:
        quit_wait.disconnect_quit_wait(worker)
        worker.deleteLater()


def test_the_journal_names_a_table_the_install_creates(tmp_path, monkeypatch):
    """@brief Follow-up 4 positive: a fresh project install creates its tables -> listed as
    created; negative: tables that already exist are not."""
    art = _component_with_symbol(tmp_path)
    project = tmp_path / 'project'
    project.mkdir()
    journals = []
    real = kicad_library._write_rollback_journal

    def keep(folder, aliases, created):
        """Keep the created list, write the journal."""
        journals.append(list(created))
        return real(folder, aliases, created)
    monkeypatch.setattr(kicad_library, '_write_rollback_journal', keep)
    install_component_library(art, project)
    assert project / 'fp-lib-table' in journals[0] and project / 'sym-lib-table' in journals[0]
    (art.component_dir / 'Demo.kicad_sym').write_text(_symbol_text('GPIO2'), encoding='utf-8')
    install_component_library(art, project, overwrite=True)
    assert len(journals) == 2
    assert project / 'fp-lib-table' not in journals[1]
    assert project / 'sym-lib-table' not in journals[1]


def test_an_acknowledged_rollback_folder_is_no_longer_reported(tmp_path):
    """@brief Follow-up 4 positive: acknowledged -> marked and not reported; negative: a folder
    not named like ours is never marked."""
    art = _component_with_symbol(tmp_path)
    project = tmp_path / 'project'
    project.mkdir()
    left = project / f'{ROLLBACK_DIR_PREFIX}{"b" * 32}'
    left.mkdir()
    assert install_component_library(art, project).earlier_rollbacks == (left,)
    assert kicad_library.acknowledge_rollback_dir(left)
    assert install_component_library(art, project, overwrite=True).earlier_rollbacks == ()
    assert left.is_dir()  # kept until the sweep's age limit
    foreign = project / 'my-backups'
    foreign.mkdir()
    assert not kicad_library.acknowledge_rollback_dir(foreign)
    assert not (foreign / kicad_library.ROLLBACK_DISPOSABLE_MARKER).exists()


def test_a_global_table_gone_points_to_kicad_not_to_a_reinstall(tmp_path):
    """@brief Follow-up 5: a GLOBAL table missing -> KiCad's table setup (positive); a row missing
    in an existing global table -> the reinstall (negative)."""
    art = _component_with_symbol(tmp_path)
    config = tmp_path / 'cfg'
    config.mkdir()
    table = config / 'fp-lib-table'
    empty = '(fp_lib_table\n  (version 7)\n)\n'
    table.write_text(empty, encoding='utf-8')
    (config / 'sym-lib-table').write_text('(sym_lib_table\n  (version 7)\n)\n', encoding='utf-8')
    libs = tmp_path / 'libs'
    install_component_library(art, libs, project_library=False, table_path=table)
    record = [{'scope': 'global', 'pretty_dir': str(libs / 'KiCadGen.pretty'),
               'table_path': str(table)}]

    def remedy():
        """The remedy of the single WARN row."""
        (row,) = check.library_checks('Demo', art.footprint_path.read_text(encoding='utf-8'),
                                      record, art.step_path,
                                      symbol_path=art.component_dir / 'Demo.kicad_sym')
        assert row.status == 'WARN'
        return row.remedy
    table.unlink()
    assert 'Preferenze' in remedy() and 'Reinstallala' not in remedy()
    table.write_text(empty, encoding='utf-8')
    assert 'Reinstallala' in remedy() and 'Preferenze' not in remedy()


# ── pinout_coherence.FINDING_COUNT_UNUSABLE in the component check ────────

def _document(**extra):
    """A side-numbered document with one pinout entry that has its pad."""
    return dict({'pinout': [{'pin': '1', 'name': 'VCC'}], 'layout': 'sides'}, **extra)


@pytest.mark.parametrize('total,shown', [(500, '500'), (None, '?')])
def test_an_unusable_side_count_is_a_blocking_row(total, shown):
    """@brief Positive: the report's count_unusable -> one ERROR naming total and limit (an
    infinite count shown «?»), declared as a blocking check."""
    rows = check.document_checks(_document(count_unusable={'total': total, 'limit': 400}),
                                 [{'pin': '1', 'name': 'VCC'}], {'1'})
    (row,) = [r for r in rows if r.code == 'document_count_unusable']
    assert row.status == 'ERROR' and row.importance == check.IMPORTANCE_BLOCKS_USE
    assert f'I lati dichiarano {shown} contatti, oltre il limite di 400' in row.detail
    assert row.remedy
    assert 'document_count_unusable' in check.BLOCKING_CHECKS


def test_a_usable_count_gives_no_such_row_and_kept_sides_lowers_it():
    """@brief Negative: no count_unusable -> no row; the user kept the sides -> WARN importance 2,
    like the other side-numbering contradictions."""
    clean = check.document_checks(_document(count_unusable=None), [{'pin': '1', 'name': 'VCC'}],
                                  {'1'})
    assert not [r for r in clean if r.code == 'document_count_unusable']
    kept = check.document_checks(
        _document(count_unusable={'total': 500, 'limit': 400}, decision='keep_sides'),
        [{'pin': '1', 'name': 'VCC'}], {'1'})
    (row,) = [r for r in kept if r.code == 'document_count_unusable']
    assert row.status == 'WARN' and row.importance == check.IMPORTANCE_BEFORE_PRODUCTION
