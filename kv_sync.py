"""Persistent-storage bridge for hosts with an ephemeral filesystem.

Render free web services have no disk: every redeploy or restart wipes the
filesystem, which would take `aadhaar.db` with it. This module mirrors the
approach already used by the Node service in the `dash-site` repo
(`src/server/data-sync.js`): the Cloudflare Worker holds a copy of the database
in Workers KV, this module restores from it before the app opens the file, and
pushes a fresh snapshot after each write and on an interval.

Unlike the Node service there is nothing to mirror except the database itself.
Uploaded spreadsheets are parsed in memory and only the resulting rows are
stored, so there are no file blobs to sync.

Configured via environment variables:
    AADHAR_SYNC_URL            base of the bridge, e.g.
                              https://dashboardharyana.site/api/backup
    AADHAR_SYNC_TOKEN          bearer token accepted by the Worker
    AADHAR_SYNC_INTERVAL_MS    snapshot cadence (default 10 min)
    AADHAR_SYNC_WRITE_BUDGET   rolling daily KV write cap (default 400)
    AADHAR_SYNC_MAX_BYTES      refuse to push a snapshot above this size
                              (default 20 MiB; Workers KV caps a value at 25)

Every function is a no-op when the bridge is not configured, so the app runs
unchanged on a developer laptop or on a host with a real disk.
"""

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

DATA_DIR = Path(os.environ.get("APP_DATA_DIR", str(Path(__file__).resolve().parent / "data")))
# Created here, not lazily. Validation writes its temp file into DATA_DIR, and
# on a fresh container the directory does not exist yet because boot.py runs
# restore_data() before app.py is imported. Deferring this to app.py meant the
# restore failed and the service silently booted empty.
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB = DATA_DIR / "aadhaar.db"

BASE = (os.environ.get("AADHAR_SYNC_URL") or "").rstrip("/")
TOKEN = os.environ.get("AADHAR_SYNC_TOKEN") or ""
INTERVAL_MS = int(float(os.environ.get("AADHAR_SYNC_INTERVAL_MS") or 10 * 60 * 1000))
WRITE_BUDGET = int(float(os.environ.get("AADHAR_SYNC_WRITE_BUDGET") or 400))
MAX_BYTES = int(float(os.environ.get("AADHAR_SYNC_MAX_BYTES") or 20 * 1024 * 1024))
FETCH_TIMEOUT_MS = 20000

# Cloudflare's bot rules reject urllib's default `Python-urllib/x.y` agent with
# 403 on both GET and PUT, which would make every restore fail. The Node service
# already sets an explicit agent for the same reason.
USER_AGENT = "Mozilla/5.0 (compatible; aadhar-dashboard-sync/1.0; +https://dashboardharyana.site)"

# Tables the app itself relies on. `master`/`tx` are created by the first
# upload, so a legitimate empty database will not have them yet.
REQUIRED_TABLES = ("users", "uploads", "camps")

_stats = {
    "day_key": "",
    "writes_today": 0,
    "last_backup_at": None,
    "db_bytes": 0,
    "restored": False,
    "error": "",
    "skipped_budget": False,
    "skipped_size": False,
}
_lock = threading.Lock()
_thread_started = False


def _log(msg):
    print(f"[kv-sync] {msg}", flush=True)


def enabled():
    return bool(BASE and TOKEN)


def _auth_headers():
    return {
        "Authorization": "Bearer " + TOKEN,
        "Content-Type": "application/octet-stream",
        "User-Agent": USER_AGENT,
    }


def _get(path):
    """GET a bridge path. Returns bytes, or None when there is no snapshot yet."""
    req = urllib.request.Request(BASE + path, headers=_auth_headers())
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_MS) as resp:
            return resp.read()
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return None
        raise


def _put(path, payload):
    req = urllib.request.Request(BASE + path, data=payload, headers=_auth_headers(), method="PUT")
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_MS) as resp:
        return resp.status


def _roll_budget():
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if _stats["day_key"] != today:
        _stats["day_key"] = today
        _stats["writes_today"] = 0


def _budget_left():
    _roll_budget()
    return max(0, WRITE_BUDGET - _stats["writes_today"])


def _put_counted(path, payload):
    if _budget_left() <= 0:
        _stats["skipped_budget"] = True
        return False
    _put(path, payload)
    _stats["writes_today"] += 1
    return True


def _validate(raw):
    """Confirm the downloaded bytes are a usable copy of the app's database.

    A truncated or corrupt download must never be written over a live database,
    so this checks SQLite integrity and that the tables the app opens at boot
    are present before the caller is allowed to touch the filesystem.
    """
    tmp = Path(tempfile.mkstemp(prefix="kv-sync-validate-", suffix=".db", dir=str(DATA_DIR))[1])
    try:
        tmp.write_bytes(raw)
        con = sqlite3.connect(str(tmp))
        try:
            integrity = con.execute("PRAGMA quick_check").fetchone()[0]
            if integrity != "ok":
                return False, f"integrity check returned {integrity!r}"
            present = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing = [t for t in REQUIRED_TABLES if t not in present]
            if missing:
                return False, "missing tables: " + ", ".join(missing)
            return True, "ok"
        finally:
            con.close()
    except sqlite3.DatabaseError as err:
        return False, f"not a valid sqlite database ({err})"
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def restore_data():
    """Download the latest snapshot into DATA_DIR.

    Must run before the app opens the database. Only acts when the local file is
    ABSENT: never overwrite an existing database, because a host that does have
    a real disk is rolling a deploy and the previous instance may still hold the
    file open.
    """
    if not enabled():
        return {"restored": False, "reason": "disabled"}
    if DB.exists():
        return {"restored": False, "reason": "local db exists"}
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        raw = _get("/aadhaar-db")
        if not raw:
            return {"restored": False, "reason": "no snapshot in bridge"}
        ok, detail = _validate(raw)
        if not ok:
            _stats["error"] = f"rejected snapshot: {detail}"
            _log(f"snapshot rejected, not written ({detail})")
            return {"restored": False, "reason": detail}
        DB.write_bytes(raw)
        # A restored copy must not be replayed against WAL side files left by an
        # earlier boot, so drop them and let SQLite open the snapshot clean.
        for suffix in ("-wal", "-shm"):
            try:
                Path(str(DB) + suffix).unlink()
            except OSError:
                pass
        _stats["restored"] = True
        _stats["db_bytes"] = len(raw)
        _log(f"restored aadhaar.db from the bridge ({len(raw)} bytes)")
        return {"restored": True, "bytes": len(raw)}
    except Exception as err:  # never block startup on the bridge
        _stats["error"] = str(err)
        _log(f"restore failed: {err}")
        return {"restored": False, "reason": str(err)}


def _snapshot_bytes():
    """A consistent standalone copy of the live database.

    Uses SQLite's backup API, which is safe while the app is serving traffic.
    """
    with tempfile.TemporaryDirectory(prefix="kv-sync-snap-", dir=str(DATA_DIR)) as tmpdir:
        out = Path(tmpdir) / "snapshot.db"
        src = sqlite3.connect(str(DB), timeout=30)
        dst = sqlite3.connect(str(out))
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
            src.close()
        ok, detail = _validate(out.read_bytes())
        if not ok:
            return None, f"snapshot failed validation: {detail}"
        return out.read_bytes(), "ok"


def backup_data():
    """Push a fresh snapshot of the database to the bridge."""
    if not enabled():
        return {"backed_up": False, "reason": "disabled"}
    if not DB.exists():
        return {"backed_up": False, "reason": "no local db"}
    try:
        payload, detail = _snapshot_bytes()
        if payload is None:
            _stats["error"] = detail
            _log(f"not pushed: {detail}")
            return {"backed_up": False, "error": detail}
        if len(payload) > MAX_BYTES:
            _stats["skipped_size"] = True
            _stats["error"] = f"snapshot {len(payload)}B exceeds AADHAR_SYNC_MAX_BYTES"
            _log(f"not pushed: snapshot is {len(payload)} bytes, over the {MAX_BYTES} byte cap")
            return {"backed_up": False, "error": _stats["error"]}
        if not _put_counted("/aadhaar-db", payload):
            _stats["error"] = "daily KV write budget exhausted (backups paused for today)"
            _log("not pushed: daily write budget exhausted")
            return {"backed_up": False, "error": _stats["error"]}
        _stats["last_backup_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _stats["db_bytes"] = len(payload)
        _stats["error"] = ""
        _log(
            f"pushed aadhaar.db ({len(payload)} bytes), "
            f"writes today {_stats['writes_today']}/{WRITE_BUDGET}"
        )
        return {"backed_up": True, "bytes": len(payload)}
    except Exception as err:
        _stats["error"] = str(err)
        _log(f"backup failed: {err}")
        return {"backed_up": False, "error": str(err)}


_backup_timer = None
_backup_timer_lock = threading.Lock()
_backup_running = threading.Lock()
_backup_queued = False


def _run_backup():
    """Run a snapshot, collapsing concurrent callers into at most one extra run.

    A write that lands mid-backup must not be lost, so it sets a flag and one
    more pass runs once the current one finishes. Without that, the final edit
    before a restart would have no snapshot on the bridge.
    """
    global _backup_queued
    if not _backup_running.acquire(blocking=False):
        _backup_queued = True
        return
    try:
        while True:
            _backup_queued = False
            backup_data()
            if not _backup_queued:
                return
    finally:
        _backup_running.release()


def request_backup():
    """Schedule a snapshot shortly after a write.

    Waiting out the full interval after a change means a restart in that window
    silently reverts the change, so mutations call this instead. Debounced so a
    burst of edits coalesces into a single push.
    """
    global _backup_timer
    if not enabled():
        return
    with _backup_timer_lock:
        if _backup_timer is not None:
            _backup_timer.cancel()
        timer = threading.Timer(1.0, _run_backup)
        timer.daemon = True
        _backup_timer = timer
        timer.start()


def start_interval():
    """Start the periodic snapshot thread once per process."""
    global _thread_started
    if not enabled() or _thread_started:
        return
    _thread_started = True

    def _loop():
        while True:
            time.sleep(INTERVAL_MS / 1000.0)
            try:
                backup_data()
            except Exception as err:  # a bad tick must not kill the thread
                _log(f"interval backup error: {err}")

    threading.Thread(target=_loop, daemon=True).start()
    _log(
        f"bridge active -> {BASE} every {INTERVAL_MS // 1000}s, "
        f"budget {WRITE_BUDGET}/day, cap {MAX_BYTES} bytes"
    )


def get_status():
    _roll_budget()
    return {
        "enabled": enabled(),
        "restored": _stats["restored"],
        "last_backup_at": _stats["last_backup_at"],
        "db_bytes": _stats["db_bytes"],
        "writes_today": _stats["writes_today"],
        "budget": WRITE_BUDGET,
        "budget_left": _budget_left(),
        "skipped_budget": _stats["skipped_budget"],
        "skipped_size": _stats["skipped_size"],
        "max_bytes": MAX_BYTES,
        "error": _stats["error"] or None,
    }


if __name__ == "__main__":
    # Manual entry point: `python kv_sync.py restore|backup|status`
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    if action == "restore":
        print(json.dumps(restore_data(), indent=2))
    elif action == "backup":
        print(json.dumps(backup_data(), indent=2))
    else:
        print(json.dumps(get_status(), indent=2))
