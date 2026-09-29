"""The empty-database outage, pinned down.

On 2026-09-29 a deploy left the production database empty. The chain was:

  1. A boot failed to restore for some transient reason.
  2. `init_db()` created a fresh schema with no users.
  3. The startup heartbeat pushed that 24 KB database to the bridge, over the
     5.6 MB real snapshot.
  4. The next deploy restored "the latest generation", which was the empty file.

Step 3 is the one that destroyed the data, and the reason it was allowed is that
`REQUIRED_TABLES` is only `users`/`uploads`/`camps` - exactly the table set a
failed boot produces. A database with zero users passed validation.

These tests build the two kinds of file that were involved and assert the client
refuses the empty one.
"""
import os

import pytest
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _make_db(path, users=0, tables=("camps", "uploads", "users"), rows=True):
    """Build a database like the ones in the outage.

    users=0 with only the login tables reproduces the 24 KB file that took the
    production data with it. tables=DATA_TABLES + the login tables reproduces a
    real database.
    """
    con = sqlite3.connect(path)
    for t in tables:
        cols = {
            "users": "username TEXT PRIMARY KEY, salt TEXT, pw_hash TEXT, role TEXT",
            "uploads": "id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT",
            "camps": "id INTEGER PRIMARY KEY AUTOINCREMENT, camp_date TEXT",
            "master": "division TEXT, sub_division TEXT, pincode TEXT",
            "tx": "id INTEGER PRIMARY KEY AUTOINCREMENT, pincode TEXT, amount INTEGER",
            "operator_master": "operator TEXT, division TEXT",
        }[t]
        con.execute(f"CREATE TABLE IF NOT EXISTS {t} ({cols})")
    if rows:
        for i in range(users):
            con.execute("INSERT INTO users VALUES (?,?,?,'admin')", (f"u{i}", "s", "h"))
        if "tx" in tables and users:
            con.execute("INSERT INTO tx (pincode, amount) VALUES ('110001', 100)")
    con.commit()
    con.close()
    return path


@pytest.fixture
def bridge(monkeypatch):
    """Turn the bridge on for the duration of one test.

    Deliberately a fixture and not a line at import time: kv_sync.enabled reads
    the environment once, and mutating it permanently leaks into every later test
    in the session (it made the app try to reach a bridge with no base URL).
    """
    import kv_sync
    monkeypatch.setattr(kv_sync, "enabled", lambda: True)
    return kv_sync

class TestEmptyDatabaseIsRejected:
    def test_rejects_the_exact_file_that_caused_the_outage(self, tmp_path):
        """users/uploads/camps only, no rows: the 24 KB file from the bridge."""
        import kv_sync
        bad = _make_db(tmp_path / "empty.db")
        ok, why = kv_sync._validate_file(bad, require_data=True)
        assert not ok, "a database with no users must never be accepted"
        assert "no users" in why

    def test_rejects_a_database_that_never_held_a_transaction(self, tmp_path):
        import kv_sync
        bad = _make_db(tmp_path / "nousers.db", users=0,
                       tables=("camps", "uploads", "users", "master", "tx", "operator_master"))
        ok, why = kv_sync._validate_file(bad, require_data=True)
        assert not ok
        assert "no users" in why

    def test_accepts_a_real_database(self, tmp_path):
        import kv_sync
        good = _make_db(tmp_path / "good.db", users=3,
                        tables=("camps", "uploads", "users", "master", "tx", "operator_master"))
        ok, why = kv_sync._validate_file(good, require_data=True)
        assert ok, why

    def test_structural_validation_still_allows_a_genuine_first_install(self, tmp_path):
        """A real first install legitimately has users but no data tables yet, and
        restore must not refuse it just because the tables are missing."""
        import kv_sync
        fresh = _make_db(tmp_path / "fresh.db", users=1, tables=("camps", "uploads", "users"))
        ok, why = kv_sync._validate_file(fresh, require_data=False)
        assert ok, why
        # but it is still not something to push over a stored backup
        assert not kv_sync._validate_file(fresh, require_data=True)[0]

    def test_corrupt_file_is_still_rejected(self, tmp_path):
        import kv_sync
        junk = tmp_path / "junk.db"
        junk.write_bytes(b"this is not a sqlite file" * 100)
        assert not kv_sync._validate_file(junk, require_data=True)[0]

    def test_missing_required_table_is_still_rejected(self, tmp_path):
        import kv_sync
        partial = _make_db(tmp_path / "partial.db", users=5, tables=("uploads", "users"))
        ok, why = kv_sync._validate_file(partial, require_data=True)
        assert not ok
        assert "camps" in why


class TestEmptyDatabaseIsNeverPushed:
    def test_backup_refuses_to_push_an_empty_db_over_a_stored_snapshot(self, bridge, tmp_path, monkeypatch):
        """The step that destroyed the data. Simulated end to end against a fake
        bridge that already holds a snapshot."""
        import kv_sync
        data = tmp_path / "data"
        data.mkdir()
        _make_db(data / "aadhaar.db", users=0)          # the failed-boot database
        puts = []

        def fake_get(path):
            if path == "/generations":
                return b'{"latest":"gen-1","generations":[{"generation":"gen-1"}]}', {}
            raise AssertionError(f"unexpected GET {path}")

        def fake_call(method, path, payload=None):
            if method == "PUT":
                puts.append(len(payload))
            return b'{"pushesToday":1,"dailyPushBudget":120,"generation":"gen-2"}', b"", {}

        monkeypatch.setattr(kv_sync, "DB", data / "aadhaar.db")
        monkeypatch.setattr(kv_sync, "_get_json", fake_get)
        monkeypatch.setattr(kv_sync, "_call", fake_call)

        result = kv_sync.backup_data(force=True, reason="test")
        assert puts == [], "an empty database was pushed over a stored snapshot"
        assert result["backed_up"] is False
        assert kv_sync._stats["skipped_empty"] is True

    def test_bridge_state_is_never_guessed(self, bridge, tmp_path, monkeypatch):
        """If the generations listing cannot be read, the push must be blocked.
        Assuming 'empty' here would allow the destructive push the guard exists
        to prevent."""
        import kv_sync
        data = tmp_path / "data"
        data.mkdir()
        _make_db(data / "aadhaar.db", users=0)
        monkeypatch.setattr(kv_sync, "DB", data / "aadhaar.db")
        monkeypatch.setattr(kv_sync, "_get_json", lambda p: (_ for _ in ()).throw(RuntimeError("bridge down")))
        assert kv_sync._bridge_has_snapshot() is True


class TestRestoreIsSelfHealing:
    def test_restore_walks_back_past_an_empty_latest(self, bridge, tmp_path, monkeypatch):
        """If the newest generation is empty, restore must fall back to an older
        good one rather than faithfully restoring the empty file."""
        import kv_sync
        data = tmp_path / "data"
        data.mkdir()
        monkeypatch.setattr(kv_sync, "DB", data / "aadhaar.db")
        monkeypatch.setattr(kv_sync, "DATA_DIR", data)

        real = tmp_path / "empty.db"
        good = tmp_path / "good.db"
        _make_db(real, users=0)
        _make_db(good, users=4, tables=("camps", "uploads", "users", "master", "tx", "operator_master"))

        listing = {"latest": "gen-empty", "generations": [
            {"generation": "gen-empty"}, {"generation": "gen-good"}]}
        blobs = {"gen-empty": real.read_bytes(), "gen-good": good.read_bytes()}

        monkeypatch.setattr(kv_sync, "_get_json", lambda p: listing)
        monkeypatch.setattr(kv_sync, "_get", lambda p: (blobs[p.rsplit("/", 1)[1]], {}))

        result = kv_sync.restore_data()
        assert result["restored"] is True
        assert result["generation"] == "gen-good"
        con = sqlite3.connect(data / "aadhaar.db")
        assert con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 4
        con.close()

    def test_restore_replaces_an_empty_local_db(self, bridge, tmp_path, monkeypatch):
        """The live host was left holding the empty file. With no deploy to wipe
        the filesystem, the restore has to be willing to replace it."""
        import kv_sync
        data = tmp_path / "data"
        data.mkdir()
        local = data / "aadhaar.db"
        _make_db(local, users=0)
        monkeypatch.setattr(kv_sync, "DB", local)
        monkeypatch.setattr(kv_sync, "DATA_DIR", data)

        good = tmp_path / "good.db"
        _make_db(good, users=9, tables=("camps", "uploads", "users", "master", "tx", "operator_master"))
        monkeypatch.setattr(kv_sync, "_get_json", lambda p: {
            "latest": "gen-good", "generations": [{"generation": "gen-good"}]})
        monkeypatch.setattr(kv_sync, "_get", lambda p: (good.read_bytes(), {}))

        result = kv_sync.restore_data()
        assert result["restored"] is True
        con = sqlite3.connect(local)
        assert con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 9
        con.close()
        assert (data / "aadhaar.db.empty-from-failed-restore").exists()

    def test_restore_still_leaves_a_populated_local_db_alone(self, bridge, tmp_path, monkeypatch):
        """A host with a real disk may be mid rolling-deploy; do not clobber it."""
        import kv_sync
        data = tmp_path / "data"
        data.mkdir()
        local = data / "aadhaar.db"
        _make_db(local, users=5, tables=("camps", "uploads", "users", "master", "tx", "operator_master"))
        monkeypatch.setattr(kv_sync, "DB", local)
        monkeypatch.setattr(kv_sync, "_get_json", lambda p: (_ for _ in ()).throw(AssertionError("must not call")))
        result = kv_sync.restore_data()
        assert result["restored"] is False
        assert result["reason"] == "local db exists"


class TestBootIsNotSilent:
    def test_boot_reports_a_failed_restore(self):
        """The restore result used to be discarded, so a failed restore was
        indistinguishable from a healthy boot in the logs."""
        src = (REPO / "boot.py").read_text(encoding="utf-8")
        body = src.split("def main")[1]
        assert "result = kv_sync.restore_data()" in body
        assert "was NOT restored" in body


class TestOutageRegressionViaSubprocess:
    def test_a_failed_restore_cannot_be_pushed(self, tmp_path):
        """Whole-module check in a clean interpreter, since kv_sync reads its
        configuration from the environment at import time."""
        data = tmp_path / "data"
        data.mkdir()
        _make_db(data / "aadhaar.db", users=0)
        code = textwrap.dedent(f"""
            import kv_sync
            kv_sync._get_json = lambda p: {{"latest": "g1", "generations": [{{"generation": "g1"}}]}}
            pushed = []
            kv_sync._call = lambda m, p, payload=None: (pushed.append(len(payload) if payload else 0), (b'{{}}', b'', {{}}))[1]
            kv_sync.backup_data(force=True)
            assert not pushed, "pushed an empty database"
            print("SAFE")
        """)
        env = dict(os.environ, APP_DATA_DIR=str(data), PYTHONPATH=str(REPO))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120)
        assert out.returncode == 0, out.stderr
        assert "SAFE" in out.stdout




