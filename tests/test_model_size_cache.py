"""@brief Verify optional size caches preserve data through filesystem failures."""

import copy
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicadgen.cad3d import model_size_cache as cache


def _seed(path, value, modified=1_000_000_000):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    os.utime(path, ns=(modified, modified))
    return path


def test_missing_caches_are_empty_and_do_not_create_directories(tmp_path, caplog):
    paths = [tmp_path / "primary" / "cache.json", tmp_path / "user" / "cache.json"]
    assert cache.load(paths) == {}
    assert not list(tmp_path.iterdir())
    assert not caplog.records


def test_load_merges_keys_and_newer_fallback_wins_entire_record(tmp_path):
    primary = _seed(tmp_path / "primary.json", {
        "a": [1, 2, 3], "shared": {"body": [9, 9, 9], "total": [10, 10, 10]}})
    fallback = _seed(tmp_path / "fallback.json", {
        "b": [2, 3, 4], "shared": {"total": [3, 4, 5]}}, 2_000_000_000)
    assert cache.load([primary, fallback]) == {
        "a": [1, 2, 3], "b": [2, 3, 4], "shared": {"total": [3, 4, 5]}}


def test_equal_timestamps_prefer_primary(tmp_path):
    first = _seed(tmp_path / "first.json", {"a": 1})
    second = _seed(tmp_path / "second.json", {"a": 2, "b": 3})
    assert cache.load([first, second]) == {"a": 1, "b": 3}


def test_timestamp_is_read_from_open_file_not_path(tmp_path, monkeypatch):
    path = _seed(tmp_path / "cache.json", {"a": [1, 2]})
    fstat = os.fstat
    descriptors = []

    def traced(descriptor):
        descriptors.append(descriptor)
        return fstat(descriptor)

    def no_path_stat(*args, **kwargs):
        pytest.fail("The path may refer to a replacement, not the opened file")

    monkeypatch.setattr(cache.os, "fstat", traced)
    monkeypatch.setattr(Path, "stat", no_path_stat)
    assert cache.load([path]) == {"a": [1, 2]}
    assert len(descriptors) == 1


@pytest.mark.parametrize("raw", ["{broken", "[]", "null", "42", '"text"',
                                     '{"a":NaN}', '{"a":Infinity}',
                                     '{"a":1,"a":2}', '{"a":{"x":1,"x":2}}'])
def test_invalid_json_does_not_hide_valid_fallback(tmp_path, raw, caplog):
    first = tmp_path / "first.json"
    first.write_text(raw, encoding="utf-8")
    second = _seed(tmp_path / "second.json", {"valid": [1, 2, 3]})
    assert cache.load([first, second]) == {"valid": [1, 2, 3]}
    assert "read failed" in caplog.text


def test_invalid_utf8_is_nonfatal(tmp_path):
    path = tmp_path / "bad.json"
    path.write_bytes(b"\xff\xfe")
    assert cache.load([path]) == {}


def test_read_permission_failure_is_nonfatal_and_logs_no_private_data(tmp_path, monkeypatch, caplog):
    first = tmp_path / "SECRET_PRIVATE_PATH.json"
    second = _seed(tmp_path / "second.json", {"valid": [1, 2, 3]})
    original = Path.open

    def denied(path, *args, **kwargs):
        if path == first:
            raise PermissionError("SECRET_EXCEPTION")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    assert cache.load([first, second]) == {"valid": [1, 2, 3]}
    assert "path #1" in caplog.text
    assert "SECRET" not in caplog.text
    assert str(tmp_path) not in caplog.text


def test_save_preserves_all_existing_keys_and_does_not_mutate_input(tmp_path):
    first = _seed(tmp_path / "first.json", {"a": [1, 2], "same": {"body": [1, 2]}})
    second = _seed(tmp_path / "second.json", {"b": [3, 4]})
    update = {"c": [5, 6], "same": {"total": [3, 4]}}
    before = copy.deepcopy(update)
    assert cache.save(update, [first, second]) == first
    assert json.loads(first.read_text()) == {
        "a": [1, 2], "b": [3, 4], "c": [5, 6], "same": {"total": [3, 4]}}
    assert update == before
    assert json.loads(second.read_text()) == {"b": [3, 4]}


def test_atomic_write_uses_closed_same_directory_temp_and_preserves_original_until_replace(
        tmp_path, monkeypatch):
    target = _seed(tmp_path / "cache.json", {"old": [1, 2]})
    original = target.read_bytes()
    replace = os.replace
    calls = []

    def inspect(source, destination):
        source = Path(source)
        assert source != target and source.parent == target.parent
        assert target.read_bytes() == original
        assert json.loads(source.read_text()) == {"old": [1, 2], "new": [3, 4]}
        calls.append(source)
        replace(source, destination)

    monkeypatch.setattr(cache.os, "replace", inspect)
    assert cache.save({"new": [3, 4]}, [target]) == target
    assert len(calls) == 1 and not calls[0].exists()
    assert list(tmp_path.iterdir()) == [target]


def test_mkdir_permission_denial_uses_user_fallback_without_os_access(tmp_path, monkeypatch):
    preferred = tmp_path / "system" / "cache.json"
    fallback = tmp_path / "user" / "cache.json"
    mkdir = Path.mkdir

    def denied(path, *args, **kwargs):
        if path == preferred.parent:
            raise PermissionError("denied")
        return mkdir(path, *args, **kwargs)

    def no_access(*args, **kwargs):
        pytest.fail("A permission precheck must not determine writability")

    monkeypatch.setattr(Path, "mkdir", denied)
    monkeypatch.setattr(cache.os, "access", no_access)
    assert cache.save({"a": [1, 2]}, [preferred, fallback]) == fallback
    assert cache.load([preferred, fallback]) == {"a": [1, 2]}


def test_exclusive_open_permission_error_immediately_uses_fallback(tmp_path, monkeypatch):
    primary = _seed(tmp_path / "system" / "cache.json", {"old": [1, 2]})
    fallback = tmp_path / "user" / "cache.json"
    primary_bytes = primary.read_bytes()
    unrelated = primary.parent / ".cache.json.someone-else.tmp"
    unrelated.write_text("preserve this file")
    original_open, original_unlink = Path.open, Path.unlink
    attempts, deletions = [], []

    def denied(path, mode="r", *args, **kwargs):
        if mode == "x":
            attempts.append(path.parent)
            if path.parent == primary.parent:
                raise PermissionError("Windows ACL denies creation")
        return original_open(path, mode, *args, **kwargs)

    def track_delete(path, *args, **kwargs):
        deletions.append(path)
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    monkeypatch.setattr(Path, "unlink", track_delete)
    monkeypatch.setattr(cache.os, "access", lambda *args, **kwargs: True)
    assert cache.save({"new": [3, 4]}, [primary, fallback]) == fallback
    assert attempts == [primary.parent, fallback.parent]
    assert primary.read_bytes() == primary_bytes
    assert unrelated.read_text() == "preserve this file"
    assert deletions == []
    assert list(primary.parent.glob("*.tmp")) == [unrelated]
    assert not list(fallback.parent.glob("*.tmp"))
    assert cache.load([primary, fallback]) == {"old": [1, 2], "new": [3, 4]}


def test_file_exists_collisions_are_bounded_then_try_fallback(tmp_path, monkeypatch):
    primary = _seed(tmp_path / "system" / "cache.json", {"old": [1, 2]})
    fallback = tmp_path / "user" / "cache.json"
    original_bytes = primary.read_bytes()
    collision = primary.parent / ".cache.json.fixed.tmp"
    collision.write_text("another writer owns this")
    attempts = []
    original_open = Path.open

    def colliding(path, mode="r", *args, **kwargs):
        if mode == "x":
            attempts.append(path)
            if path == collision:
                raise FileExistsError("occupied")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(cache.uuid, "uuid4", lambda: SimpleNamespace(hex="fixed"))
    monkeypatch.setattr(Path, "open", colliding)
    assert cache.save({"new": [3, 4]}, [primary, fallback]) == fallback
    assert attempts == [collision] * 3 + [fallback.parent / collision.name]
    assert primary.read_bytes() == original_bytes
    assert collision.read_text() == "another writer owns this"
    assert list(primary.parent.glob("*.tmp")) == [collision]
    assert not list(fallback.parent.glob("*.tmp"))
    assert cache.load([primary, fallback]) == {"old": [1, 2], "new": [3, 4]}


def test_single_uuid_collision_retries_without_deleting_existing_file(tmp_path, monkeypatch):
    target = _seed(tmp_path / "cache.json", {"old": [1, 2]})
    collision = tmp_path / ".cache.json.occupied.tmp"
    collision.write_text("not ours")
    names = iter(["occupied", "available"])
    monkeypatch.setattr(cache.uuid, "uuid4", lambda: SimpleNamespace(hex=next(names)))
    assert cache.save({"new": [3, 4]}, [target]) == target
    assert cache.load([target]) == {"old": [1, 2], "new": [3, 4]}
    assert collision.read_text() == "not ours"
    assert list(tmp_path.glob("*.tmp")) == [collision]


def test_atomic_replace_failure_keeps_primary_and_merges_into_fallback(tmp_path, monkeypatch):
    primary = _seed(tmp_path / "primary.json", {"a": [1, 2], "shared": [2, 3]})
    fallback = _seed(tmp_path / "fallback.json", {"b": [3, 4]})
    original = primary.read_bytes()
    replace = os.replace

    def denied(source, destination):
        if destination == primary:
            raise PermissionError("read-only primary")
        replace(source, destination)

    monkeypatch.setattr(cache.os, "replace", denied)
    assert cache.save({"shared": [4, 5]}, [primary, fallback]) == fallback
    assert primary.read_bytes() == original
    assert cache.load([primary, fallback]) == {"a": [1, 2], "b": [3, 4], "shared": [4, 5]}
    assert not list(tmp_path.glob("*.tmp"))


def test_primary_recovery_reads_new_save_instead_of_stale_fallback(tmp_path, monkeypatch):
    primary = _seed(tmp_path / "primary.json", {"primary_only": [1, 2]})
    fallback = _seed(tmp_path / "fallback.json", {"fallback_only": [3, 4]})
    replace = os.replace
    primary_writable = False

    def controlled_replace(source, destination):
        if destination == primary and not primary_writable:
            raise PermissionError("primary is read-only")
        replace(source, destination)
        # Fixed times verify ordering without clock-resolution or sleep races.
        modified = 3_000_000_000 if destination == primary else 2_000_000_000
        os.utime(destination, ns=(modified, modified))

    monkeypatch.setattr(cache.os, "replace", controlled_replace)
    paths = [primary, fallback]
    value_a, value_b = {"total": [1, 2, 3]}, {"total": [4, 5, 6]}
    assert cache.save({"shared": value_a}, paths) == fallback
    assert cache.load(paths)["shared"] == value_a
    original_fallback = fallback.read_bytes()

    primary_writable = True
    assert cache.save({"shared": value_b}, paths) == primary
    assert fallback.read_bytes() == original_fallback
    assert cache.load(paths) == {
        "primary_only": [1, 2], "fallback_only": [3, 4], "shared": value_b}
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("exception", [PermissionError("PRIVATE"), ValueError("PRIVATE")])
def test_all_write_failures_preserve_existing_files_and_only_remove_owned_temp(
        tmp_path, monkeypatch, caplog, exception):
    paths = [_seed(tmp_path / "first.json", {"a": [1, 2]}),
             _seed(tmp_path / "second.json", {"b": [2, 3]})]
    original = {path: path.read_bytes() for path in paths}
    unrelated = tmp_path / ".first.json.someone-else.tmp"
    unrelated.write_text("do not delete")

    def failed(*args, **kwargs):
        raise exception

    monkeypatch.setattr(cache.os, "replace", failed)
    assert cache.save({"new": [3, 4]}, paths) is None
    assert {path: path.read_bytes() for path in paths} == original
    assert list(tmp_path.glob("*.tmp")) == [unrelated]
    assert unrelated.read_text() == "do not delete"
    assert "no writable destination" in caplog.text
    assert "PRIVATE" not in caplog.text
    assert all(record.levelno == logging.WARNING for record in caplog.records)


@pytest.mark.parametrize("stage", ["create", "fsync"])
def test_temp_creation_or_flush_failure_is_nonfatal(tmp_path, monkeypatch, stage):
    target = _seed(tmp_path / "cache.json", {"old": [1, 2]})
    original = target.read_bytes()

    def failed(*args, **kwargs):
        raise OSError("failure")

    if stage == "create":
        original_open = Path.open

        def denied_create(path, mode="r", *args, **kwargs):
            if mode == "x":
                raise OSError("creation failed")
            return original_open(path, mode, *args, **kwargs)

        monkeypatch.setattr(Path, "open", denied_create)
    else:
        monkeypatch.setattr(cache.os, "fsync", failed)
    assert cache.save({"new": [3, 4]}, [target]) is None
    assert target.read_bytes() == original
    assert list(tmp_path.iterdir()) == [target]


def test_cleanup_failure_does_not_mask_fallback_success(tmp_path, monkeypatch, caplog):
    primary, fallback = tmp_path / "a" / "cache.json", tmp_path / "b" / "cache.json"
    replace, unlink = os.replace, Path.unlink

    def denied_replace(source, destination):
        if destination == primary:
            raise PermissionError("denied")
        replace(source, destination)

    def denied_cleanup(path, *args, **kwargs):
        if path.parent == primary.parent:
            raise PermissionError("denied")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(cache.os, "replace", denied_replace)
    monkeypatch.setattr(Path, "unlink", denied_cleanup)
    assert cache.save({"a": [1, 2]}, [primary, fallback]) == fallback
    assert "cleanup failed" in caplog.text
    assert json.loads(fallback.read_text()) == {"a": [1, 2]}


@pytest.mark.parametrize("index", [None, [], 12, {1: [1, 2]}, {"a": object()},
                                       {"a": float("nan")}, {"a": float("inf")}])
def test_invalid_input_never_damages_existing_cache(tmp_path, index):
    target = _seed(tmp_path / "cache.json", {"old": [1, 2]})
    original = target.read_bytes()
    assert cache.save(index, [target]) is None
    assert target.read_bytes() == original
    assert list(tmp_path.iterdir()) == [target]


def test_circular_input_is_nonfatal(tmp_path):
    value = {}
    value["recursive"] = value
    assert cache.save(value, [tmp_path / "cache.json"]) is None
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("paths", [[], None, [None], ["nul\0path"]])
def test_absent_or_invalid_destinations_are_nonfatal(paths):
    assert cache.load(paths) == {}
    assert cache.save({"a": [1, 2]}, paths) is None


def test_duplicate_paths_and_generator_are_supported(tmp_path, monkeypatch):
    path = tmp_path / "cache.json"
    calls = []
    replace = os.replace

    def traced(*args):
        calls.append(args)
        replace(*args)

    monkeypatch.setattr(cache.os, "replace", traced)
    assert cache.save({"a": [1, 2]}, (p for p in [path, str(path)])) == path
    assert len(calls) == 1
    assert cache.load(path) == {"a": [1, 2]}


def test_partial_cache_does_not_invent_measurements_or_completeness(tmp_path):
    path = tmp_path / "cache.json"
    record = {"body": None, "total": [1, 2, 3], "method": "CAD envelope"}
    assert cache.save({"one": record}, [path]) == path
    assert cache.load([path]) == {"one": record}
    assert "missing" not in cache.load([path])


def test_corrupt_primary_replaced_with_fallback_keys_and_new_measurements(tmp_path):
    primary = tmp_path / "primary.json"
    primary.write_text("{broken", encoding="utf-8")
    fallback = _seed(tmp_path / "fallback.json", {"existing": [1, 2]})
    assert cache.save({"new": [3, 4]}, [primary, fallback]) == primary
    assert cache.load([primary, fallback]) == {"existing": [1, 2], "new": [3, 4]}


# --------------------------------------------------------------------------
# L'identita' del file: cio' che rende una misura replicabile e invalidabile.
# --------------------------------------------------------------------------

def test_the_identity_changes_when_the_file_changes(tmp_path):
    """@brief Positivo e negativo sulla condizione che la cache deve garantire.

    Stesso file, stessa chiave: e' cio' che permette di replicare una misura.
    File riscritto, chiave diversa: e' cio' che impedisce a una misura vecchia
    di passare per una misura del file nuovo.
    """
    path = tmp_path / "model.step"
    path.write_bytes(b"ISO-10303-21;")
    first = cache.file_identity(path)
    assert first and cache.file_identity(path) == first
    assert cache.file_identity(str(path)) == first

    path.write_bytes(b"ISO-10303-21; PIU' LUNGO")
    assert cache.file_identity(path) != first

    # Stessa dimensione ma data diversa: la sola lunghezza non basterebbe.
    same_size = cache.file_identity(path)
    os.utime(path, (1_000_000, 1_000_000))
    assert cache.file_identity(path) != same_size


def test_a_file_that_cannot_be_inspected_has_no_identity(tmp_path):
    """@brief Negativo: senza dimensione e data non si puo' invalidare nulla.

    Ripiegare sul solo percorso darebbe una chiave che non cambia mai piu':
    una misura sbagliata resterebbe valida per sempre. Meglio rimisurare.
    """
    assert cache.file_identity(tmp_path / "non-esiste.step") is None
    assert cache.file_identity(None) is None
    assert cache.file_identity("nul\0path") is None


def test_the_identity_recipe_matches_the_size_index_key(tmp_path):
    """@brief Le due ricette della stessa chiave non devono divergere.

    `kicad_models.index_key` calcola oggi la stessa stringa per l'indice degli
    ingombri. Finche' esistono due copie della ricetta, questa prova le tiene
    allineate: chi ne cambia una deve cambiare l'altra.
    """
    from kicadgen.cad3d.kicad_models import index_key

    path = tmp_path / "model.step"
    path.write_bytes(b"ISO-10303-21;")
    assert cache.file_identity(path) == index_key(path)
