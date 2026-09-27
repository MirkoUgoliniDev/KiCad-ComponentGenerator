"""@brief Crash reports: written on unhandled exceptions, rotated, private, never raising."""
import sys
import threading
from pathlib import Path

import pytest

from kicadgen import crash_report as cr


@pytest.fixture
def hooks(monkeypatch):
    """Record the hooks that crash_report must chain to, and restore everything."""
    seen = {"sys": [], "threading": []}
    monkeypatch.setattr(sys, "excepthook", lambda *args: seen["sys"].append(args))
    monkeypatch.setattr(threading, "excepthook", lambda args: seen["threading"].append(args))
    yield seen
    cr.uninstall()


def _raise(exc):
    try:
        raise exc
    except BaseException as caught:  # noqa: BLE001 - test helper
        return type(caught), caught, caught.__traceback__


def test_unhandled_exception_writes_a_report_and_chains(tmp_path, hooks):
    """@brief Positive: one report with version, platform and traceback; previous hook called."""
    cr.install(lambda: tmp_path)
    exc_info = _raise(ValueError("broken geometry"))
    sys.excepthook(*exc_info)
    reports = cr.latest_reports()
    assert len(reports) == 1 and reports[0].parent == tmp_path / cr.REPORTS_DIRNAME
    text = reports[0].read_text(encoding="utf-8")
    import kicadgen
    assert f"KiCadGen: {kicadgen.__version__}" in text
    assert "Python: " in text and "OS: " in text and "Qt: " in text
    assert "ValueError: broken geometry" in text and "Traceback" in text
    assert hooks["sys"] == [exc_info]


def test_thread_exception_writes_a_report_and_chains(tmp_path, hooks):
    """@brief Positive: an exception in a worker thread is reported with the thread name."""
    cr.install(lambda: tmp_path)

    def work():
        raise RuntimeError("worker failed")

    thread = threading.Thread(target=work, name="extract-worker")
    thread.start()
    thread.join()
    reports = cr.latest_reports()
    assert len(reports) == 1
    text = reports[0].read_text(encoding="utf-8")
    assert "Thread: extract-worker" in text and "RuntimeError: worker failed" in text
    assert len(hooks["threading"]) == 1


def test_keyboard_interrupt_is_not_a_crash(tmp_path, hooks):
    """@brief Negative: Ctrl+C and SystemExit write nothing but still chain."""
    cr.install(lambda: tmp_path)
    sys.excepthook(*_raise(KeyboardInterrupt()))
    sys.excepthook(*_raise(SystemExit(2)))
    assert cr.latest_reports() == [] and len(hooks["sys"]) == 2


def test_rotation_keeps_only_the_newest(tmp_path, hooks):
    """@brief At most max_reports files remain, the most recent ones."""
    cr.install(lambda: tmp_path, max_reports=3)
    for index in range(5):
        sys.excepthook(*_raise(ValueError(f"crash {index}")))
    reports = cr.latest_reports()
    assert len(reports) == 3
    assert "crash 4" in reports[0].read_text(encoding="utf-8")
    assert "crash 2" in reports[-1].read_text(encoding="utf-8")
    assert cr.latest_reports(limit=1) == reports[:1]


def test_rotation_leaves_other_files_alone(tmp_path, hooks):
    """@brief Negative: only files named like reports are ever deleted."""
    folder = cr.reports_dir(tmp_path)
    folder.mkdir(parents=True)
    keep = folder / "notes.txt"
    keep.write_text("mine", encoding="utf-8")
    cr.install(lambda: tmp_path, max_reports=1)
    for index in range(3):
        sys.excepthook(*_raise(ValueError(f"crash {index}")))
    assert keep.read_text(encoding="utf-8") == "mine" and len(cr.latest_reports()) == 1


def test_the_hook_never_raises(tmp_path, hooks, monkeypatch):
    """@brief A failing provider, an unwritable folder or a broken previous hook are swallowed."""
    def broken_provider():
        raise OSError("disk gone")
    cr.install(broken_provider)
    sys.excepthook(*_raise(ValueError("x")))          # provider fails
    blocker = tmp_path / "file"
    blocker.write_text("", encoding="utf-8")
    cr.install(lambda: blocker)                        # folder cannot be created
    sys.excepthook(*_raise(ValueError("y")))
    cr.uninstall()

    def broken_previous(*args):
        raise RuntimeError("previous hook broken")
    monkeypatch.setattr(sys, "excepthook", broken_previous)
    cr.install(lambda: tmp_path)
    sys.excepthook(*_raise(ValueError("z")))           # previous hook raises
    assert len(cr.latest_reports()) == 1
    assert cr.write_report(*_raise(ValueError("w")), base=blocker) is None


def test_no_environment_dump_and_personal_data_scrubbed(tmp_path, hooks, monkeypatch):
    """@brief The report carries no environment variables, keys, user or home paths."""
    monkeypatch.setenv("KICADGEN_SECRET_PROBE", "probe-value-123")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-0123456789abcdef")
    cr.install(lambda: tmp_path)
    home = str(Path.home())
    message = (f"cannot open {home}\\secret\\file.pdf with key sk-or-v1-0123456789abcdef "
               "and C:\\Users\\someone\\Desktop\\x")
    sys.excepthook(*_raise(OSError(message)))
    text = cr.latest_reports()[0].read_text(encoding="utf-8")
    assert "probe-value-123" not in text and "KICADGEN_SECRET_PROBE" not in text
    assert "sk-or-v1-0123456789abcdef" not in text and "<redacted>" in text
    assert home.lower() not in text.lower() and "~\\secret\\file.pdf" in text
    assert "someone" not in text and "C:\\Users\\<user>\\Desktop" in text


def test_scrub_keeps_ordinary_text():
    """@brief Negative: text without personal data is left unchanged."""
    text = "ValueError: pitch 2.54 mm and row spacing 7.62 mm in C:\\Program Files\\KiCad"
    assert cr.scrub(text) == text


def test_install_is_idempotent_and_uninstall_restores(tmp_path, hooks):
    """@brief A second install keeps the original chain; uninstall restores it."""
    original = sys.excepthook
    cr.install(lambda: tmp_path)
    cr.install(lambda: tmp_path / "other")
    sys.excepthook(*_raise(ValueError("once")))
    assert len(hooks["sys"]) == 1
    assert cr.latest_reports(base=tmp_path / "other")
    cr.uninstall()
    assert sys.excepthook is original


def test_latest_reports_without_folder_or_provider(tmp_path):
    """@brief Listing never fails: no provider or no folder gives an empty list."""
    assert cr.latest_reports() == []
    assert cr.latest_reports(base=tmp_path / "missing") == []


@pytest.mark.parametrize("name", ["OPENROUTER_API_KEY", "DEEPSEEK_API_KEY"])
def test_every_provider_key_is_masked_by_value_whatever_its_shape(monkeypatch, name):
    """@brief S8: a key without the ``sk-`` shape is still masked by its value.

    Positive: the value of each provider variable disappears. Negative: an
    unrelated text of the same length is left as it is.
    """
    secret = "zz" + "Q7" * 12
    monkeypatch.setenv(name, secret)
    assert cr.scrub(f"header Bearer {secret} end") == "header Bearer <redacted> end"
    assert cr.scrub("zz" + "A1" * 12) == "zz" + "A1" * 12
