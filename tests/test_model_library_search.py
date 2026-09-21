"""@brief Library index and KiCad-style filter contracts on temporary roots."""

import os
from pathlib import Path

import pytest

from kicadgen.services import model_library_search as search


def _touch(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('fixture')
    return path


@pytest.fixture
def roots(tmp_path):
    """Two roots: a vendor tree with ``*.3dshapes`` folders and a flat user repository."""
    vendor = tmp_path / 'vendor'
    user = tmp_path / 'espressif-repo'
    _touch(vendor / 'Package_TO_SOT_SMD.3dshapes' / 'SOT-23.step')
    _touch(vendor / 'Package_TO_SOT_SMD.3dshapes' / 'SOT-23-5.step')
    _touch(vendor / 'Package_TO_SOT_SMD.3dshapes' / 'SOT-223.STP')
    _touch(vendor / 'Connector_USB.3dshapes' / 'USB_C_Receptacle_GCT.stp')
    _touch(vendor / 'Connector_USB.3dshapes' / 'nested' / 'USB_Micro.STEP')
    _touch(vendor / 'Connector_USB.3dshapes' / 'USB_C_Receptacle_GCT.wrl')  # twin of the .stp
    _touch(vendor / 'Connector_USB.3dshapes' / 'USB_A_mesh_only.wrl')
    _touch(vendor / 'Connector_USB.3dshapes' / 'readme.txt')
    _touch(user / 'ESP32-S3-WROOM-1.step')
    _touch(user / 'espressif.3dshapes' / 'ESP32-C3-MINI-1.step')
    return vendor, user


def test_index_lists_solids_in_any_case_with_library_names(roots):
    vendor, user = roots
    models = search.library_models(roots, refresh=True)
    labels = {model.label for model in models}
    assert labels == {
        'Package_TO_SOT_SMD.3dshapes/SOT-23', 'Package_TO_SOT_SMD.3dshapes/SOT-23-5',
        'Package_TO_SOT_SMD.3dshapes/SOT-223', 'Connector_USB.3dshapes/USB_C_Receptacle_GCT',
        'Connector_USB.3dshapes/USB_Micro', 'Connector_USB.3dshapes/USB_A_mesh_only',
        'espressif-repo/ESP32-S3-WROOM-1', 'espressif.3dshapes/ESP32-C3-MINI-1',
    }
    by_label = {model.label: model for model in models}
    assert by_label['Package_TO_SOT_SMD.3dshapes/SOT-223'].path.suffix == '.STP'
    assert by_label['Connector_USB.3dshapes/USB_Micro'].path == vendor / 'Connector_USB.3dshapes' / 'nested' / 'USB_Micro.STEP'
    assert by_label['espressif-repo/ESP32-S3-WROOM-1'].path.parent == user
    assert models == sorted(models, key=lambda m: (m.library.casefold(), m.stem.casefold(), str(m.path)))


def test_mesh_is_hidden_when_a_solid_twin_exists_and_kept_otherwise(roots):
    models = search.library_models(roots, refresh=True)
    suffixes = {model.stem: model.path.suffix for model in models}
    assert suffixes['USB_C_Receptacle_GCT'] == '.stp'
    assert suffixes['USB_A_mesh_only'] == '.wrl'
    assert 'readme' not in suffixes


def test_index_is_cached_per_roots_and_refresh_invalidates(roots, monkeypatch):
    vendor, user = roots
    first = search.library_models(roots, refresh=True)
    _touch(vendor / 'Package_TO_SOT_SMD.3dshapes' / 'SOT-89.step')
    # Same roots, no refresh: the walk is not repeated, so the new file is absent.
    monkeypatch.setattr(search, '_walk_root', lambda root: pytest.fail('Cached roots were walked again'))
    assert search.library_models(roots) == first
    assert search.library_models([str(vendor), str(user)]) == first
    monkeypatch.undo()
    refreshed = search.library_models(roots, refresh=True)
    assert len(refreshed) == len(first) + 1
    assert any(model.stem == 'SOT-89' for model in refreshed)
    # A different root tuple is a different cache entry, never a stale hit.
    assert {m.stem for m in search.library_models([vendor])} == {m.stem for m in first if m.path.is_relative_to(vendor)} | {'SOT-89'}


def test_default_roots_come_from_kicad_models_discovery(roots, monkeypatch):
    vendor, user = roots
    monkeypatch.setattr(search, '_search_roots', lambda library: [vendor, user])
    assert search.discovered_roots() == [vendor, user]
    models = search.library_models(refresh=True)
    assert {model.library for model in models} == {
        'Package_TO_SOT_SMD.3dshapes', 'Connector_USB.3dshapes', 'espressif-repo', 'espressif.3dshapes'}
    monkeypatch.setattr(search, '_search_roots', lambda library: [])
    assert search.library_models(refresh=True) == []


def test_unreadable_and_missing_roots_are_skipped(roots, tmp_path):
    vendor, _user = roots
    missing = tmp_path / 'absent'
    locked = tmp_path / 'locked'
    _touch(locked / 'Hidden.3dshapes' / 'secret.step')
    if os.name == 'nt' or os.geteuid() == 0:
        pytest.skip('Permission bits are not enforced for this user')
    os.chmod(locked, 0)
    try:
        models = search.library_models([vendor, missing, locked], refresh=True)
    finally:
        os.chmod(locked, 0o700)
    assert models
    assert all(model.path.is_relative_to(vendor) for model in models)
    # A file used as a root is not a directory: it contributes nothing and does not raise.
    file_root = vendor / 'Connector_USB.3dshapes' / 'readme.txt'
    assert search.library_models([file_root], refresh=True) == []


def test_root_failing_during_walk_is_skipped_without_losing_others(roots, monkeypatch):
    vendor, user = roots
    real_walk = os.walk

    def walk(top, *args, **kwargs):
        if Path(top) == user:
            raise OSError('simulated I/O failure')
        return real_walk(top, *args, **kwargs)

    monkeypatch.setattr(search.os, 'walk', walk)
    models = search.library_models([user, vendor], refresh=True)
    assert models
    assert all(model.path.is_relative_to(vendor) for model in models)


def test_filter_requires_every_token_case_insensitively(roots):
    models = search.library_models(roots, refresh=True)
    # Substring semantics, like KiCad: ``23`` also occurs inside ``SOT-223``.
    assert [m.stem for m in search.filter_models(models, 'sot 23')] == ['SOT-23', 'SOT-223', 'SOT-23-5']
    assert [m.stem for m in search.filter_models(models, 'SOT-23')] == ['SOT-23', 'SOT-23-5']
    assert [m.stem for m in search.filter_models(models, 'USB_c recept')] == ['USB_C_Receptacle_GCT']
    assert [m.stem for m in search.filter_models(models, 'sot 23 usb')] == []
    # Fragments crossing the ``library/stem`` separator are matched too.
    assert [m.stem for m in search.filter_models(models, '3dshapes/SOT-223')] == ['SOT-223']


def test_filter_matches_library_folder_and_repository_names(roots):
    models = search.library_models(roots, refresh=True)
    assert {m.stem for m in search.filter_models(models, 'connector_usb')} == {
        'USB_C_Receptacle_GCT', 'USB_Micro', 'USB_A_mesh_only'}
    assert {m.stem for m in search.filter_models(models, 'espressif')} == {
        'ESP32-S3-WROOM-1', 'ESP32-C3-MINI-1'}
    assert [m.stem for m in search.filter_models(models, 'espressif-repo')] == ['ESP32-S3-WROOM-1']


def test_filter_supports_wildcards_inside_tokens(roots):
    models = search.library_models(roots, refresh=True)
    assert [m.stem for m in search.filter_models(models, 'sot-2?3')] == ['SOT-223']
    assert [m.stem for m in search.filter_models(models, 'esp32*mini')] == ['ESP32-C3-MINI-1']
    assert [m.stem for m in search.filter_models(models, 'usb*mesh')] == ['USB_A_mesh_only']
    assert search.filter_models(models, 'esp32*zzz') == []


def test_filter_orders_shorter_stems_first(roots):
    models = search.library_models(roots, refresh=True)
    result = search.filter_models(models, 'sot')
    assert [m.stem for m in result] == ['SOT-23', 'SOT-223', 'SOT-23-5']
    assert result.total == 3
    assert not result.truncated


@pytest.mark.parametrize('query', ['', '   ', '\t\n', None])
def test_empty_query_lists_nothing(roots, query):
    models = search.library_models(roots, refresh=True)
    result = search.filter_models(models, query)
    assert result == []
    assert result.total == 0
    assert not result.truncated


def test_filter_caps_rows_and_reports_truncation(tmp_path):
    root = tmp_path / 'big'
    total = search.MAX_RESULTS + 37
    for index in range(total):
        _touch(root / 'Bulk.3dshapes' / f'R_{index:04d}.step')
    models = search.library_models([root], refresh=True)
    assert len(models) == total
    capped = search.filter_models(models, 'r_')
    assert len(capped) == search.MAX_RESULTS
    assert capped.total == total
    assert capped.truncated
    assert [m.stem for m in capped] == [f'R_{i:04d}' for i in range(search.MAX_RESULTS)]
    narrowed = search.filter_models(models, 'r_00')
    assert len(narrowed) == 100
    assert not narrowed.truncated
