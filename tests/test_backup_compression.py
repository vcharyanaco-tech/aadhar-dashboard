"""Bandwidth guards for the dedicated backup bridge.

The full database used to be uploaded every interval, even when nothing had
changed. These tests pin the three behaviours that keep Render egress inside
the monthly allowance:

  1. changed snapshots are gzipped on the wire;
  2. an unchanged snapshot spends no PUT at all;
  3. restore accepts both gzipped and legacy raw snapshots, and seeds the hash
     so the first interval tick after a restart does not re-upload.

There is also a compatibility test for rolling deploys: an older Worker rejects
gzip before writing anything, and the client must retry the same snapshot raw.
"""

import gzip
import hashlib
import json
import sqlite3
import time
from pathlib import Path

import pytest

import kv_sync


def _make_db(path, users=2):
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY, salt TEXT, pw_hash TEXT, role TEXT
        );
        CREATE TABLE IF NOT EXISTS uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT
        );
        CREATE TABLE IF NOT EXISTS camps (
            id INTEGER PRIMARY KEY AUTOINCREMENT, camp_date TEXT
        );
        CREATE TABLE IF NOT EXISTS master (
            division TEXT, sub_division TEXT, pincode TEXT
        );
        CREATE TABLE IF NOT EXISTS tx (
            id INTEGER PRIMARY KEY AUTOINCREMENT, pincode TEXT, amount INTEGER
        );
        CREATE TABLE IF NOT EXISTS operator_master (operator TEXT, division TEXT);
        """
    )
    for i in range(users):
        con.execute(
            "INSERT INTO users VALUES (?,?,?,?)", (f"u{i}", "s", "h", "admin")
        )
    con.commit()
    con.close()
    return path


@pytest.fixture
def bridge(monkeypatch, tmp_path):
    """A configured client with a real, populated local database."""
    data = tmp_path / "data"
    data.mkdir()
    local = data / "aadhaar.db"
    _make_db(local)

    monkeypatch.setattr(kv_sync, "DB", local)
    monkeypatch.setattr(kv_sync, "DATA_DIR", data)
    monkeypatch.setattr(kv_sync, "enabled", lambda: True)
    monkeypatch.setattr(kv_sync, "_locally_populated", lambda: True)
    monkeypatch.setattr(kv_sync, "_bridge_has_snapshot", lambda: False)
    monkeypatch.setattr(kv_sync, "_last_hash", None)
    monkeypatch.setattr(kv_sync, "_gzip_enabled", True)
    monkeypatch.setattr(kv_sync, "_gzip_retry_at", 0.0)
    monkeypatch.setattr(kv_sync, "_last_push_at", None)
    monkeypatch.setattr(kv_sync, "_dirty", False)
    monkeypatch.setattr(kv_sync, "_dirty_since", None)

    for key, value in {
        "last_backup_at": None,
        "last_upload_at": None,
        "last_checked_at": None,
        "last_error": None,
        "consecutive_failures": 0,
        "db_bytes": 0,
        "stored_bytes": 0,
        "last_pushed_generation": None,
        "last_encoding": None,
        "skipped_unchanged": 0,
        "skipped_budget": False,
        "skipped_size": False,
        "skipped_min_interval": False,
        "skipped_empty": False,
    }.items():
        monkeypatch.setitem(kv_sync._stats, key, value)
    return kv_sync


def test_changed_snapshot_is_gzipped_then_unchanged_is_skipped(bridge, monkeypatch):
    raw = bridge.DB.read_bytes()
    calls = []

    def fake_call(method, path, payload=None):
        calls.append((method, path, payload))
        return (
            200,
            json.dumps(
                {
                    "generation": "1729000000000-abcdef",
                    "bytes": len(raw),
                    "pushesToday": 1,
                    "dailyPushBudget": 150,
                }
            ).encode(),
            {},
        )

    monkeypatch.setattr(bridge, "_snapshot_bytes", lambda: (raw, "ok"))
    monkeypatch.setattr(bridge, "_call", fake_call)

    first = bridge.backup_data(force=True, reason="test")
    assert first["backed_up"] is True
    assert first["encoding"] == "gzip"
    assert first["stored_bytes"] < first["bytes"] or first["stored_bytes"] > 0
    assert len(calls) == 1
    assert calls[0][0] == "PUT"
    assert calls[0][1] == "/db"
    assert calls[0][2][:2] == b"\x1f\x8b"
    assert gzip.decompress(calls[0][2]).startswith(b"SQLite format 3")
    assert bridge._last_hash == hashlib.sha256(raw).hexdigest()

    second = bridge.backup_data(force=True, reason="test")
    assert second["backed_up"] is True
    assert second["skipped"] == "unchanged"
    assert len(calls) == 1, "an unchanged database must not spend a PUT"
    assert bridge._stats["skipped_unchanged"] == 1
    assert bridge._stats["last_upload_at"] is not None


def test_unchanged_snapshot_is_reuploaded_when_the_checkpoint_is_due(bridge, monkeypatch):
    raw = bridge.DB.read_bytes()
    calls = []

    monkeypatch.setattr(bridge, "CHECKPOINT_MS", 1)
    monkeypatch.setattr(bridge, "_snapshot_bytes", lambda: (raw, "ok"))
    monkeypatch.setattr(
        bridge,
        "_call",
        lambda method, path, payload=None: (
            calls.append(payload) or (200, b'{"generation":"g1"}', {})
        ),
    )

    assert bridge.backup_data(force=True)["backed_up"] is True
    assert len(calls) == 1
    monkeypatch.setattr(bridge, "_last_push_at", time.time() - 10)
    out = bridge.backup_data(force=True, reason="checkpoint")
    assert out["backed_up"] is True
    assert out.get("skipped") is None
    assert len(calls) == 2, "a due checkpoint must refresh the generation"


def test_a_new_snapshot_is_pushed_again(bridge, monkeypatch):
    raw = bridge.DB.read_bytes()
    calls = []

    def fake_call(method, path, payload=None):
        calls.append(payload)
        return 200, b'{"generation":"g1","bytes":1}', {}

    monkeypatch.setattr(bridge, "_call", fake_call)
    monkeypatch.setattr(bridge, "_snapshot_bytes", lambda: (raw, "ok"))
    assert bridge.backup_data(force=True)["backed_up"] is True
    assert len(calls) == 1

    changed = raw + b"a real edit changes the snapshot hash"
    monkeypatch.setattr(bridge, "_snapshot_bytes", lambda: (changed, "ok"))
    out = bridge.backup_data(force=True)
    assert out["backed_up"] is True
    assert out.get("skipped") is None
    assert len(calls) == 2
    assert gzip.decompress(calls[1]).startswith(b"SQLite format 3")


def test_old_worker_gzip_rejection_falls_back_to_raw(bridge, monkeypatch):
    raw = bridge.DB.read_bytes()
    calls = []

    def fake_call(method, path, payload=None):
        calls.append(payload)
        if payload[:2] == b"\x1f\x8b":
            raise bridge.BridgeError(
                "Body is not a SQLite database.",
                status=400,
                payload={"error": "not_sqlite"},
            )
        return 200, b'{"generation":"g1","bytes":123}', {}

    monkeypatch.setattr(bridge, "_snapshot_bytes", lambda: (raw, "ok"))
    monkeypatch.setattr(bridge, "_call", fake_call)

    out = bridge.backup_data(force=True, reason="test")
    assert out["backed_up"] is True
    assert out["encoding"] == "raw"
    assert bridge._gzip_enabled is False
    assert len(calls) == 2
    assert calls[1] == raw


def test_restore_past_an_invalid_latest_republishes_the_good_snapshot(
        bridge, monkeypatch, tmp_path):
    good = bridge.DB.read_bytes()
    empty = _make_db(tmp_path / "empty.db", users=0).read_bytes()
    bridge.DB.unlink()

    monkeypatch.setattr(
        bridge,
        "_get_json",
        lambda path: {
            "latest": "1729000000000-badbad",
            "generations": [
                {"generation": "1729000000000-badbad"},
                {"generation": "1728000000000-good00"},
            ],
        },
    )
    monkeypatch.setattr(
        bridge,
        "_get",
        lambda path: (
            (empty, {}) if path.endswith("badbad") else (good, {})
        ),
    )

    result = bridge.restore_data()
    assert result["restored"] is True
    assert result["generation"] == "1728000000000-good00"
    assert bridge._last_hash is None, (
        "falling back past an invalid latest must republish, not dedupe the repair away"
    )

    calls = []
    monkeypatch.setattr(bridge, "_snapshot_bytes", lambda: (good, "ok"))
    monkeypatch.setattr(
        bridge,
        "_call",
        lambda method, path, payload=None: (
            calls.append(payload) or (200, b'{"generation":"repaired"}', {})
        ),
    )
    out = bridge.backup_data(force=True, reason="repair")
    assert out["backed_up"] is True
    assert len(calls) == 1
    assert calls[0][:2] == b"\x1f\x8b"


@pytest.mark.parametrize("encoding", ["gzip", "raw"])
def test_restore_accepts_gzip_and_legacy_and_seeds_dedupe(bridge, monkeypatch, encoding):
    raw = bridge.DB.read_bytes()
    bridge.DB.unlink()

    generation = f"{int(time.time() * 1000)}-abcdef"
    stored = gzip.compress(raw, mtime=0) if encoding == "gzip" else raw
    monkeypatch.setattr(
        bridge,
        "_get_json",
        lambda path: {"latest": generation, "generations": [{"generation": generation}]},
    )
    monkeypatch.setattr(bridge, "_get", lambda path: (stored, {}))

    result = bridge.restore_data()
    assert result["restored"] is True
    assert result["encoding"] == ("gzip" if encoding == "gzip" else "raw")
    assert bridge.DB.read_bytes() == raw
    assert bridge._last_hash == hashlib.sha256(raw).hexdigest()

    # The first scheduled check after a restore must not re-upload the snapshot
    # it just downloaded.
    calls = []
    monkeypatch.setattr(bridge, "_snapshot_bytes", lambda: (raw, "ok"))
    monkeypatch.setattr(
        bridge,
        "_call",
        lambda method, path, payload=None: (
            calls.append(payload) or (200, b'{"generation":"g1"}', {})
        ),
    )
    out = bridge.backup_data(force=True, reason="startup")
    assert out["skipped"] == "unchanged"
    assert calls == []
