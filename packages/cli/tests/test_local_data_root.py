"""Live vault data stays local even when encrypted packet sync is enabled."""

import pytest

from latticeshadow import config


def _paths(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    local = tmp_path / ".latticeshadow"
    cloud = tmp_path / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "LatticeShadow"
    monkeypatch.setattr(config, "LOG_DIR", str(local))
    monkeypatch.setattr(config, "CONFIG_PATH", str(local / "config.toml"))
    return local, cloud


def test_icloud_sync_keeps_live_data_local_and_packet_directory_available(tmp_path, monkeypatch):
    local, cloud = _paths(tmp_path, monkeypatch)
    config.set("sync.icloud_sync", "true")
    (cloud / "sync_packets").mkdir(parents=True)
    (cloud / "handshake.json").write_text("legacy pairing metadata")

    assert config.get_data_dir() == str(local)
    assert config.get("sync.icloud_sync") is True
    assert not (cloud / "shadow.sqlite").exists()


@pytest.mark.parametrize("legacy_name", ["shadow.sqlite", ".key", "holographic_today.bin", "snapshots"])
def test_legacy_icloud_live_data_blocks_new_local_vault(tmp_path, monkeypatch, legacy_name):
    local, cloud = _paths(tmp_path, monkeypatch)
    config.set("sync.icloud_sync", "true")
    cloud.mkdir(parents=True)
    if legacy_name == "snapshots":
        (cloud / legacy_name).mkdir()
    else:
        (cloud / legacy_name).write_text("legacy data")

    with pytest.raises(RuntimeError, match="Legacy live LatticeShadow data"):
        config.get_data_dir()
    assert not (local / "shadow.sqlite").exists()


def test_disabled_sync_ignores_unrelated_icloud_folder(tmp_path, monkeypatch):
    local, cloud = _paths(tmp_path, monkeypatch)
    cloud.mkdir(parents=True)
    (cloud / "shadow.sqlite").write_text("legacy data")

    assert config.get_data_dir() == str(local)
