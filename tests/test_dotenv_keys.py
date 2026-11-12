"""@brief Reading one key from a .env file and removing only its line (2026-11-12)."""
import pytest

from kicadgen.services import dotenv_keys


def test_only_the_lines_of_that_name_go_and_the_rest_stays_byte_for_byte(tmp_path):
    path = tmp_path / ".env"
    path.write_bytes(b"# keys\r\nDEEPSEEK_API_KEY=sk-1\r\nDEEPSEEK_API_KEY_OLD=sk-0\r\n"
                     b"export DEEPSEEK_API_KEY = 'sk-2'\r\nOTHER=x\r\n# DEEPSEEK_API_KEY=commented\r\n")
    assert dotenv_keys.read_key(path, "DEEPSEEK_API_KEY") == "sk-2"
    assert dotenv_keys.remove_key(path, "DEEPSEEK_API_KEY") is True
    assert path.read_bytes() == (b"# keys\r\nDEEPSEEK_API_KEY_OLD=sk-0\r\nOTHER=x\r\n"
                                 b"# DEEPSEEK_API_KEY=commented\r\n")


def test_a_file_without_the_name_is_left_untouched(tmp_path):
    path = tmp_path / ".env"
    path.write_bytes(b"OTHER=x\n")
    assert dotenv_keys.remove_key(path, "OPENROUTER_API_KEY") is False
    assert path.read_bytes() == b"OTHER=x\n"
    assert dotenv_keys.read_key(path, "OPENROUTER_API_KEY") == ""
    assert dotenv_keys.read_key(tmp_path / "missing.env", "OPENROUTER_API_KEY") == ""


def test_an_unwritable_file_raises_and_stays_as_it_was(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_bytes(b"OPENROUTER_API_KEY=sk\nOTHER=x\n")
    import os
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(PermissionError("locked")))
    with pytest.raises(OSError):
        dotenv_keys.remove_key(path, "OPENROUTER_API_KEY")
    assert path.read_bytes() == b"OPENROUTER_API_KEY=sk\nOTHER=x\n"
    assert [p.name for p in tmp_path.iterdir()] == [".env"], "no temporary file left behind"


def test_bom_quoted_name_and_multiline_values_are_handled_by_the_parser(tmp_path):
    """@brief Review 2026-11-12: a BOM, a quoted name and a multi-line value never fool the removal."""
    path = tmp_path / ".env"
    path.write_bytes("﻿OPENROUTER_API_KEY=sk-1\r\n'OPENROUTER_API_KEY'=sk-2\r\n"
                     "FOO=\"a\nOPENROUTER_API_KEY=notakey\"\nOTHER=x\n".encode("utf-8"))
    assert dotenv_keys.remove_key_result(path, "OPENROUTER_API_KEY") == dotenv_keys.REMOVED
    assert path.read_bytes() == "﻿FOO=\"a\nOPENROUTER_API_KEY=notakey\"\nOTHER=x\n".encode("utf-8")


def test_a_reference_to_another_variable_is_not_removed(tmp_path):
    """@brief Negative: «KEY=${REAL}» keeps the secret in REAL: nothing is removed and the caller is told."""
    path = tmp_path / ".env"
    path.write_bytes(b"REAL=sk-4\nOPENROUTER_API_KEY=${REAL}\n")
    assert dotenv_keys.read_key(path, "OPENROUTER_API_KEY") == "sk-4"
    assert dotenv_keys.remove_key_result(path, "OPENROUTER_API_KEY") == dotenv_keys.INDIRECT
    assert path.read_bytes() == b"REAL=sk-4\nOPENROUTER_API_KEY=${REAL}\n"


def test_a_file_that_is_not_utf8_raises_oserror_and_stays(tmp_path):
    path = tmp_path / ".env"
    path.write_bytes(b"# caff\xe8\nOPENROUTER_API_KEY=sk\n")
    with pytest.raises(OSError):
        dotenv_keys.remove_key_result(path, "OPENROUTER_API_KEY")
    assert path.read_bytes() == b"# caff\xe8\nOPENROUTER_API_KEY=sk\n"


def test_other_files_that_still_set_the_key_are_named(tmp_path, monkeypatch):
    first, second = tmp_path / "a" / ".env", tmp_path / "b" / ".env"
    for path in (first, second):
        path.parent.mkdir()
        path.write_text("OPENROUTER_API_KEY=sk\n", encoding="utf-8")
    monkeypatch.setattr(dotenv_keys, "env_files", lambda: [first, second])
    assert dotenv_keys.other_files_setting("OPENROUTER_API_KEY", first) == [str(second)]
    second.write_text("OTHER=x\n", encoding="utf-8")
    assert dotenv_keys.other_files_setting("OPENROUTER_API_KEY", first) == []
