"""@brief Run the test suite in two separate pytest processes.

Usage::

    python tools/run_test_halves.py first|second|both [--list] [--timeout MINUTES] [-- <pytest args>]

The full suite in a single process can die at random with a native Qt/VTK
fail-fast (HANDOFF point 113): no traceback, the whole run is lost. Until that
is fixed at the root, CI and local verification run the suite in two halves,
each in its own interpreter. The split is by file name: the files that sort
before ``tests/test_photo_detector_ui.py`` form the first half, that file and
every later one the second. The partition is computed from the file system at
run time, so a new test file always lands in exactly one half.

@note The script works on every OS (plain ``pathlib`` and ``subprocess``), so
    the same command runs on the Windows CI runner and on a developer machine.
@note ``--timeout MINUTES`` bounds each half: a half still running then has
    its whole process tree killed and counts as failed (exit HALF_TIMEOUT_EXIT).
    Without it a half runs as long as it takes; the per-test watchdog of
    tests/conftest.py already stops a single hung test (review 2026-11-16,
    R12-10).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: First file of the second half (HANDOFF point 113: the crash happens at the
#: passage from test_photo_detection_generality.py to this file).
SPLIT_AT = "test_photo_detector_ui.py"
#: Arguments always given to pytest, as in the documented command line.
BASE_ARGS = ("-q", "-p", "no:cacheprovider")
#: Exit status of a half stopped by --timeout (pytest itself uses 0-5).
HALF_TIMEOUT_EXIT = 124


class SplitError(RuntimeError):
    """@brief The suite cannot be split as documented."""


def split_test_files(tests_dir: Path, split_at: str = SPLIT_AT) -> tuple[list[str], list[str]]:
    """@brief Partition the test modules of a directory into two halves.

    @param tests_dir Directory that holds the ``test_*.py`` modules.
    @param split_at File name of the first module of the second half.
    @return Two sorted lists of POSIX paths relative to the repository root
        (or absolute when ``tests_dir`` is outside it): before and from
        ``split_at``. Ordering is by code point, independent of the locale.
    @throws SplitError When ``split_at`` does not exist or a half is empty:
        running an empty half would report success without testing anything.
    """
    names = sorted(path.name for path in tests_dir.glob("test_*.py") if path.is_file())
    if split_at not in names:
        raise SplitError(f"{split_at} not found in {tests_dir}; update SPLIT_AT.")
    first = [name for name in names if name < split_at]
    second = [name for name in names if name >= split_at]
    if not first or not second:
        raise SplitError("One half of the suite would be empty.")
    try:
        base = tests_dir.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        base = tests_dir.resolve().as_posix()
    return [f"{base}/{name}" for name in first], [f"{base}/{name}" for name in second]


def kill_process_tree(process: subprocess.Popen) -> None:
    """@brief Stop a process and every process it started.

    @param process A process started by `run_half` (in its own process group
        on POSIX).
    @note Windows: ``taskkill /T /F`` ends the tree by parent links. POSIX:
        the whole process group is killed. Then the process is reaped.
    """
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, timeout=60)
        else:
            import signal
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        process.kill()
    except OSError:
        pass
    process.wait()


def run_half(command: list[str], timeout_s=None) -> int:
    """@brief Run one half and return its exit status, killing it after `timeout_s`.

    @param command The pytest command line.
    @param timeout_s Seconds before the half is stopped, or None for no bound.
    @return The process exit status, or HALF_TIMEOUT_EXIT when it was stopped.
    @throws BaseException Whatever interrupts the wait (Ctrl-C included) is
        re-raised after the half's process tree has been killed.
    """
    kwargs = {"cwd": ROOT}
    if os.name != "nt":
        kwargs["start_new_session"] = True
    process = subprocess.Popen(command, **kwargs)
    try:
        return process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        kill_process_tree(process)
        print(f"error: the half did not finish within {timeout_s / 60:g} minutes; "
              "its process tree was killed.", file=sys.stderr, flush=True)
        return HALF_TIMEOUT_EXIT
    except BaseException:
        # Ctrl-C or any error here: never leave a pytest tree behind.
        kill_process_tree(process)
        raise


def _parse_timeout(argv: list[str]) -> tuple[list[str], float | None]:
    """Remove ``--timeout MINUTES`` from argv; return the rest and the bound in seconds.

    Raises ValueError for a missing, non-numeric or non-positive value.
    """
    if "--timeout" not in argv:
        return argv, None
    index = argv.index("--timeout")
    if index + 1 >= len(argv):
        raise ValueError("--timeout needs a number of minutes")
    minutes = float(argv[index + 1])
    # "not > 0" also refuses NaN; an infinite bound would overflow the wait.
    if not minutes > 0 or minutes == float("inf"):
        raise ValueError("--timeout must be a positive, finite number of minutes")
    return argv[:index] + argv[index + 2:], minutes * 60


def main(argv: list[str]) -> int:
    """@brief Command-line entry point.

    @param argv Arguments after the program name.
    @return The first non-zero pytest exit code (HALF_TIMEOUT_EXIT for a
        half stopped by ``--timeout``), 0 when every half passed, 2 on a
        usage error.
    @note Both halves run even when the first fails, so a single CI run shows
        the state of the whole suite; the exit code still reports the failure.
    """
    extra: list[str] = []
    if "--" in argv:
        index = argv.index("--")
        argv, extra = argv[:index], argv[index + 1:]
    listing = "--list" in argv
    argv = [arg for arg in argv if arg != "--list"]
    try:
        argv, timeout_s = _parse_timeout(argv)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if len(argv) != 1 or argv[0] not in ("first", "second", "both"):
        print(__doc__, file=sys.stderr)
        return 2
    try:
        first, second = split_test_files(ROOT / "tests")
    except SplitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    halves = {"first": [first], "second": [second], "both": [first, second]}[argv[0]]
    status = 0
    for files in halves:
        if listing:
            print("\n".join(files))
            continue
        print(f"== pytest on {len(files)} files: {files[0]} .. {files[-1]}", flush=True)
        code = run_half([sys.executable, "-m", "pytest", *BASE_ARGS, *extra, *files], timeout_s)
        if code != 0 and status == 0:
            status = code
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
