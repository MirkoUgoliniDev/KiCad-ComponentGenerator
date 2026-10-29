"""@brief Read cache of ProfileStore: fewer connections, never a stale value.

The GUI asks for the selected type's family on every refresh; each call used
to open a new SQLite connection. The getters are now cached against the
database file signature and dropped by every write of the store.
"""
import pytest

from kicadgen.services import analysis_profiles
from kicadgen.services.analysis_profiles import ProfileError, ProfileStore


def _counting(store, monkeypatch):
    """Count the connections the store opens from now on."""
    calls = []
    original = store._connect

    def counted():
        calls.append(1)
        return original()

    monkeypatch.setattr(store, "_connect", counted)
    return calls


def _first_key(store):
    return store.list_types()[0]["key"]


def test_repeated_reads_open_one_connection_each(tmp_path, monkeypatch):
    store = ProfileStore(tmp_path / "p.sqlite3")
    key = _first_key(store)
    store._invalidate_cache()
    calls = _counting(store, monkeypatch)
    for _ in range(25):
        store.get_type(key)
        store.list_types()
        store.list_types(True)
        store.overrides(key)
        store.defaults()
        store.resolve(key)
        store.snapshot(key)
        store.ignored_model_overrides(key)
    # get_type, list_types x2, overrides, defaults, resolve: one load each.
    assert len(calls) == 6


def test_cached_values_equal_uncached_reads(tmp_path):
    store = ProfileStore(tmp_path / "p.sqlite3")
    key = _first_key(store)
    fresh = ProfileStore(tmp_path / "p.sqlite3")
    for _ in range(2):  # second round is served by the cache
        assert store.get_type(key) == fresh.get_type(key) is not None
        assert store.resolve(key) == fresh.resolve(key)
        assert store.list_types(True) == fresh.list_types(True)
        fresh._invalidate_cache()


def test_a_mutated_result_does_not_poison_the_cache(tmp_path):
    store = ProfileStore(tmp_path / "p.sqlite3")
    key = _first_key(store)
    store.get_type(key)["label"] = "changed by a caller"
    store.resolve(key)["model"] = "changed by a caller"
    store.list_types()[0]["label"] = "changed by a caller"
    store.defaults()["model"] = "changed by a caller"
    assert store.get_type(key)["label"] != "changed by a caller"
    assert store.resolve(key)["model"] != "changed by a caller"
    assert store.list_types()[0]["label"] != "changed by a caller"
    assert store.defaults()["model"] != "changed by a caller"


def test_own_writes_are_visible_at_once(tmp_path):
    """Negative case: a cached read must never hide an edit of the profile dialog."""
    store = ProfileStore(tmp_path / "p.sqlite3")
    key = _first_key(store)
    row = store.get_type(key)
    store.resolve(key)
    family = next(f for f in analysis_profiles.FAMILIES if f != row["family"])
    store.save_type(key, label="Renamed", family=family, instructions=row["instructions"],
                    active=True, overrides={"system_prompt": "edited prompt"})
    assert store.get_type(key)["label"] == "Renamed"
    assert store.get_type(key)["family"] == family
    assert store.resolve(key)["system_prompt"] == "edited prompt"
    store.save_defaults({"image_instructions": "global note"})
    assert store.defaults()["image_instructions"] == "global note"
    new_key = store.duplicate(key)
    assert any(t["key"] == new_key for t in store.list_types())
    store.delete_type(new_key)
    assert store.get_type(new_key) is None
    assert all(t["key"] != new_key for t in store.list_types(True))


def test_a_write_by_another_store_is_seen(tmp_path):
    store = ProfileStore(tmp_path / "p.sqlite3")
    other = ProfileStore(tmp_path / "p.sqlite3")
    key = _first_key(store)
    before = store.get_type(key)
    other.save_type(key, label=before["label"] + " (other)", family=before["family"],
                    instructions=before["instructions"], active=True)
    assert store.get_type(key)["label"] == before["label"] + " (other)"


def test_a_missing_type_is_not_cached_as_an_error(tmp_path):
    store = ProfileStore(tmp_path / "p.sqlite3")
    with pytest.raises(ProfileError):
        store.resolve("no_such_type")
    assert store.get_type("no_such_type") is None
    key = store.save_type(None, label="New", family=next(iter(analysis_profiles.FAMILIES)))
    assert store.resolve(key)["model"] == store.defaults()["model"]
    assert store.get_type(key)["label"] == "New"


def test_a_read_straddling_a_write_is_not_stored(tmp_path):
    """A worker that read the old row while the GUI wrote must not cache it."""
    store = ProfileStore(tmp_path / "p.sqlite3")
    calls = []

    def stale_loader():
        calls.append(1)
        if len(calls) == 1:
            store._invalidate_cache()  # a write commits while this read runs
            return "old"
        return "new"

    assert store._cached("probe", None, stale_loader) == "old"
    assert store._cached("probe", None, stale_loader) == "new"
    assert store._cached("probe", None, stale_loader) == "new"
    assert len(calls) == 2


def test_translation_catalog_identity_is_kept_until_a_change(tmp_path):
    """I18n keys its reverse index on the catalogue dict identity."""
    store = ProfileStore(tmp_path / "p.sqlite3")
    first = store._language_catalog("de")
    assert store._language_catalog("de") is first
    store.save_translation("tab.project", "de", "Projekt X")
    second = store._language_catalog("de")
    assert second is not first and second["tab.project"] == "Projekt X"


def test_an_outside_write_in_the_same_tick_with_the_same_size_is_seen(tmp_path):
    """@brief Precise invalidation (2026-10-17): SQLite's file change counter enters the key.

    Another process rewrites one label to a text of the same length and the
    file keeps its size; the modification time is then put back, as when
    both writes land in the same tick of a coarse clock. With modification
    time and size alone the store kept serving the old label.
    """
    import os
    import sqlite3
    path = tmp_path / "p.sqlite3"
    store = ProfileStore(path)
    key = _first_key(store)
    label = store.get_type(key)["label"]
    stamp = path.stat()
    replacement = ("X" if label[0] != "X" else "Y") + label[1:]
    with sqlite3.connect(path) as outside:
        outside.execute("UPDATE component_types SET label=? WHERE key=?", (replacement, key))
    outside.close()
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert path.stat().st_size == stamp.st_size and path.stat().st_mtime_ns == stamp.st_mtime_ns
    # CONTRACT CHANGED 2026-10-29 (review E2): a write by a connection that is
    # not a ProfileStore of this process is noticed by the first lookup after
    # the bound _EXTERNAL_CHANGE_CHECK_S, no longer by every lookup (each one
    # used to stat and open the file). The counter still decides, as before.
    import time
    time.sleep(analysis_profiles._EXTERNAL_CHANGE_CHECK_S * 1.5)
    assert store.get_type(key)["label"] == replacement


def test_an_unchanged_file_keeps_serving_the_cache(tmp_path, monkeypatch):
    """@brief Negative: reading the header counter does not turn every read into a query."""
    store = ProfileStore(tmp_path / "p.sqlite3")
    key = _first_key(store)
    store.get_type(key)
    calls = _counting(store, monkeypatch)
    for _ in range(5):
        store.get_type(key)
    assert calls == []
