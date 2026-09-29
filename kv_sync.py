"""Durable backup bridge for hosts with an ephemeral filesystem.

Render free web services have no disk: every redeploy or restart wipes the
filesystem, which would take `aadhaar.db` with it. This module mirrors the
database into a dedicated Cloudflare Worker that holds a copy in Workers KV, and
restores from it before the app opens the file.

The bridge lives in a Cloudflare account that hosts nothing else
(`aadhar-backup.aadhar-haryana.workers.dev`), so its write budget and its
stored snapshots cannot be exhausted or deleted by another project.

    AADHAR_SYNC_URL            base URL of the bridge
    AADHAR_SYNC_TOKEN          bearer token accepted by the bridge
    AADHAR_SYNC_INTERVAL_MS    periodic snapshot cadence (default 1 hour)
    AADHAR_SYNC_MIN_INTERVAL_MS  floor between two pushes (default 15 min)
    AADHAR_SYNC_MAX_BYTES      refuse to push a snapshot above this size
                               (default 20 MiB; Workers KV caps a value at 25)

Every function is a no-op when the bridge is not configured, so the app runs
unchanged on a developer laptop or on a host with a real disk.

DESIGN NOTES — why this looks the way it does
--------------------------------------------
* The old bridge never worked. It asked for `/api/backup/aadhaar-db` but the
  dash-site Worker only routed `/db`, `/uploads`, `/meetings` and `/stats`, so
  every restore and every push got a 404. `aadhaar.db` was therefore never
  backed up at all.
* The old client counted writes in a module-level dict, which resets to zero on
  every Render restart, so its "400 writes/day" cap was never actually enforced.
  The Worker is now the authority on the budget; this module only spaces its
  pushes out and reports what the Worker says.
* Failures are recorded, not swallowed. `get_status()` carries the last error
  and a consecutive-failure count, and `app.py` renders it to admins, so a dead
  bridge is visible in the UI instead of only in the log.

Exit codes follow the usual convention: a bridge outage is logged and the app
still starts, so a failure degrades rather than boot-looping.
"""

import atexit
import calendar
import json
import os
import re
import shutil
import signal
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

DATA_DIR = Path(os.environ.get("APP_DATA_DIR") or str(Path(__file__).resolve().parent / "data"))
# Created here, not lazily. Validation writes its temp file into DATA_DIR, and
# on a fresh container the directory does not exist yet because boot.py runs
# restore_data() before app.py is imported. Deferring this to app.py meant the
# restore failed and the service silently booted empty.
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB = DATA_DIR / "aadhaar.db"

BASE = (os.environ.get("AADHAR_SYNC_URL") or "").rstrip("/")
TOKEN = os.environ.get("AADHAR_SYNC_TOKEN") or ""
# A full snapshot is ~5.6 MB and each push costs 3 KV writes, so pushing on every
# write exhausted the daily allowance within hours and then silently stopped.
# The default cadence is now an hour, and MIN_INTERVAL_MS is the floor between
# two pushes no matter how many writes land in between.
INTERVAL_MS = int(float(os.environ.get("AADHAR_SYNC_INTERVAL_MS") or 60 * 60 * 1000))
MIN_INTERVAL_MS = int(float(os.environ.get("AADHAR_SYNC_MIN_INTERVAL_MS") or 15 * 60 * 1000))
MAX_BYTES = int(float(os.environ.get("AADHAR_SYNC_MAX_BYTES") or 20 * 1024 * 1024))
FETCH_TIMEOUT_MS = 60000

# Cloudflare's bot rules reject urllib's default `Python-urllib/x.y` agent with
# 403 on both GET and PUT, which would make every restore fail. The Node service
# already sets an explicit agent for the same reason.
USER_AGENT = "Mozilla/5.0 (compatible; aadhar-dashboard-sync/2.0; +https://dashboardharyana.site)"

# Tables the app itself relies on. `master`/`tx`/`operator_master` are created by
# the first upload, so a legitimate empty database will not have them yet.
REQUIRED_TABLES = ("users", "uploads", "camps")

# Tables the real data lives in. A database that has none of these has never
# held a single transaction, and is what a botched boot leaves behind.
DATA_TABLES = ("master", "tx", "operator_master")

_stats = {
    "last_backup_at": None,
    "last_error": None,
    "consecutive_failures": 0,
    "db_bytes": 0,
    "restored": False,
    "restored_from": None,
    "last_pushed_generation": None,
    "skipped_budget": False,
    "skipped_size": False,
    "skipped_min_interval": False,
    "skipped_empty": False,
}
_lock = threading.Lock()
_thread_started = False

# Push scheduling. `request_backup()` marks the database dirty and a background
# timer does the pushing, so a burst of edits costs one push, not one per edit.
_dirty = False
_dirty_since = None
_last_push_at = None
_timer = None
_timer_lock = threading.Lock()
_push_running = threading.Lock()
_shutdown = False
_lifecycle_installed = False


def _log(msg):
    print(f"[kv-sync] {msg}", flush=True)


def _now():
    return time.time()


def _iso(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else _now()))


def enabled():
    return bool(BASE and TOKEN)


def _auth_headers():
    return {
        "Authorization": "Bearer " + TOKEN,
        "Content-Type": "application/octet-stream",
        "User-Agent": USER_AGENT,
    }


class BridgeError(Exception):
    """A bridge call failed. `status` is the HTTP code when there was one."""

    def __init__(self, message, status=None, payload=None):
        super().__init__(message)
        self.status = status
        self.payload = payload or {}


def _call(method, path, payload=None, timeout=FETCH_TIMEOUT_MS):
    """One bridge request. Raises BridgeError on any non-2xx or transport failure."""
    req = urllib.request.Request(BASE + path, data=payload, headers=_auth_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return resp.status, body, dict(resp.headers)
    except urllib.error.HTTPError as err:
        raw = b""
        try:
            raw = err.read()
        except Exception:
            pass
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:
            parsed = {"error": raw[:200].decode("utf-8", "replace")}
        raise BridgeError(parsed.get("message") or parsed.get("error") or f"HTTP {err.code}",
                          status=err.code, payload=parsed) from None
    except urllib.error.URLError as err:
        raise BridgeError(f"bridge unreachable: {err.reason}") from None
    except TimeoutError:
        raise BridgeError("bridge timed out") from None


def _get(path, timeout=FETCH_TIMEOUT_MS):
    """GET a bridge path. Returns (body, headers), or (None, {}) on 404."""
    try:
        _, body, headers = _call("GET", path, timeout=timeout)
        return body, headers
    except BridgeError as err:
        if err.status == 404:
            return None, {}
        raise


def _get_json(path):
    body, _ = _get(path)
    if body is None:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        raise BridgeError("bridge returned malformed JSON") from None


def _rowcount(con, table):
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.DatabaseError:
        return 0


def _validate_file(path, require_data=False):
    """Check that a file on disk is a usable copy of the app's database.

    Validates in place. `_validate` (below) is the bytes variant for downloads;
    anything we produced locally is already a file, so rewriting it into a second
    temp file just to check it doubled the I/O of every single backup.

    `require_data` additionally demands that the file actually contains the
    app's data. This is the check that was missing, and its absence destroyed the
    production database: `REQUIRED_TABLES` is only `users`/`uploads`/`camps`,
    which is *precisely* the table set a failed boot leaves behind, so a 24 KB
    database with zero users passed validation, was pushed over a 5.6 MB real
    snapshot, and was then restored in its place on the next deploy.
    """
    try:
        con = sqlite3.connect(str(path))
        try:
            integrity = con.execute("PRAGMA quick_check").fetchone()[0]
            if integrity != "ok":
                return False, f"integrity check returned {integrity!r}"
            present = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing = [t for t in REQUIRED_TABLES if t not in present]
            if missing:
                return False, "missing tables: " + ", ".join(missing)
            if require_data:
                # No users means nobody can log in, so this is a botched boot,
                # never a database worth pushing or restoring.
                if _rowcount(con, "users") == 0:
                    return False, "no users: this is an empty database, not a real one"
                if not any(t in present for t in DATA_TABLES):
                    return False, ("no data tables ("
                                   + "/".join(DATA_TABLES) + "): this database has never held a transaction")
            return True, "ok"
        finally:
            con.close()
    except sqlite3.DatabaseError as err:
        return False, f"not a valid sqlite database ({err})"


def _validate(raw, require_data=False):
    """Confirm downloaded bytes are a usable copy of the app's database.

    A truncated or corrupt download must never be written over a live database,
    so this checks SQLite integrity and that the tables the app opens at boot
    are present before the caller is allowed to touch the filesystem. Pass
    `require_data` to also reject a structurally valid but empty database.
    """
    tmp = Path(tempfile.mkstemp(prefix="kv-sync-validate-", suffix=".db", dir=str(DATA_DIR))[1])
    try:
        tmp.write_bytes(raw)
        return _validate_file(tmp, require_data=require_data)
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
        # An existing database is normally left alone, because a host with a real
        # disk is rolling a deploy and the previous instance may still hold the
        # file open. An *empty* one is different: it is the residue of a boot
        # whose restore failed, it cannot be logged into, and it is what the
        # outage above left on disk. There is nothing to protect, so re-restore.
        if _locally_populated():
            return {"restored": False, "reason": "local db exists"}
        try:
            DB.replace(DB.with_suffix(".db.empty-from-failed-restore"))
            _log("local aadhaar.db has no users; moved aside and re-restoring")
        except OSError as err:
            _log(f"local aadhaar.db is empty but could not be moved aside ({err})")
            return {"restored": False, "reason": "local db exists"}
    try:
        candidates = _restore_candidates()
        if not candidates:
            _log("restore skipped: the bridge holds no snapshot")
            return {"restored": False, "reason": "no snapshot in bridge"}

        for generation, path in candidates:
            try:
                raw, _ = _get(f"/db/{generation}") if path else _get("/db")
            except BridgeError as err:
                _log(f"generation {generation} could not be fetched ({err}); trying the next one")
                continue
            if not raw:
                continue
            ok, detail = _validate(raw, require_data=True)
            if not ok:
                _stats["consecutive_failures"] += 1
                _log(f"generation {generation} rejected ({detail}); trying the next one")
                continue
            DB.write_bytes(raw)
            # A restored copy must not be replayed against WAL side files left by an
            # earlier boot, so drop them and let SQLite open the snapshot clean.
            for suffix in ("-wal", "-shm"):
                try:
                    Path(str(DB) + suffix).unlink()
                except OSError:
                    pass
            _stats["restored"] = True
            _stats["restored_from"] = generation
            _stats["db_bytes"] = len(raw)
            _stats["last_error"] = None
            if generation == candidates[0][0] and len(candidates) > 1:
                _log(f"restored generation {generation} ({len(raw)} bytes) after rejecting newer ones")
            else:
                _log(f"restored aadhaar.db from the bridge ({len(raw)} bytes, generation {generation})")
            return {"restored": True, "bytes": len(raw), "generation": generation}

        _stats["last_error"] = "every stored generation failed validation"
        _log(_stats["last_error"])
        return {"restored": False, "reason": "no stored generation passed validation"}
    except BridgeError as err:
        _stats["last_error"] = str(err)
        _stats["consecutive_failures"] += 1
        _log(f"restore failed: {err}")
        return {"restored": False, "reason": str(err)}
    except Exception as err:  # never block startup on the bridge
        _stats["last_error"] = str(err)
        _stats["consecutive_failures"] += 1
        _log(f"restore failed: {err}")
        return {"restored": False, "reason": str(err)}


def _restore_candidates(limit=8):
    """The snapshots worth trying, newest first.

    The latest generation is not automatically the right one. A boot whose
    restore failed used to create an empty database and push it, so "latest" was
    an empty file; the restore then faithfully restored that empty file. Walking
    back through older generations makes the restore recover from exactly that
    situation instead of compounding it.
    """
    try:
        listing = _get_json("/generations")
    except BridgeError as err:
        _log(f"could not list generations ({err}); will try the latest snapshot only")
        return [(None, None)]
    if not listing:
        return [(None, None)]
    out = []
    for item in (listing.get("generations") or [])[:limit]:
        gen = item.get("generation")
        if gen:
            out.append((gen, True))
    return out or [(None, None)]


def restore_generation(generation, keep_backup=True):
    """Replace the live database with a specific stored generation.

    Unlike restore_data() this deliberately overwrites an existing file, which is
    what a rollback means. It is therefore guarded three ways: the download is
    validated before anything is touched, the current file is kept as
    `aadhaar.db.pre-rollback`, and the caller is expected to have confirmed with a
    human first.
    """
    if not enabled():
        return {"restored": False, "reason": "disabled"}
    if not is_valid_gen(generation):
        return {"restored": False, "reason": "bad_generation"}
    try:
        raw, _ = _get(f"/db/{generation}")
        if not raw:
            return {"restored": False, "reason": "not_found", "generation": generation}
        ok, detail = _validate(raw)
        if not ok:
            _record_failure(f"rejected generation {generation}: {detail}")
            _log(f"rollback refused, snapshot not written ({detail})")
            return {"restored": False, "reason": detail, "generation": generation}

        backup_path = None
        if keep_backup and DB.exists():
            backup_path = DB.with_suffix(DB.suffix + ".pre-rollback")
            shutil.copy2(DB, backup_path)

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        DB.write_bytes(raw)
        for suffix in ("-wal", "-shm"):
            try:
                Path(str(DB) + suffix).unlink()
            except OSError:
                pass

        _stats["restored"] = True
        _stats["restored_from"] = generation
        _stats["db_bytes"] = len(raw)
        _stats["last_error"] = None
        _stats["consecutive_failures"] = 0
        _log(f"rolled back to generation {generation} ({len(raw)} bytes)"
             + (f"; previous file kept at {backup_path.name}" if backup_path else ""))
        return {"restored": True, "bytes": len(raw), "generation": generation,
                "backup": str(backup_path) if backup_path else None}
    except BridgeError as err:
        _record_failure(str(err))
        _log(f"rollback failed: {err}")
        return {"restored": False, "reason": str(err), "status": err.status}
    except Exception as err:
        _record_failure(str(err))
        _log(f"rollback failed: {err}")
        return {"restored": False, "reason": str(err)}


def is_valid_gen(generation):
    """Match the Worker's generation ids, so a bad value never reaches a URL."""
    return bool(generation) and bool(re.match(r"^\d{13}-[0-9a-f]{6}$", str(generation)))


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
        raw = out.read_bytes()
        # Validate the file we just produced, in place. Rewriting 5.6 MB into a
        # second temp file purely to integrity-check it doubled the disk I/O of
        # every backup, and backups fire after every write.
        ok, detail = _validate_file(out)
        if not ok:
            return None, f"snapshot failed validation: {detail}"
        return raw, "ok"


def _record_success(info):
    _stats["last_backup_at"] = _iso()
    _stats["db_bytes"] = info.get("bytes") or 0
    _stats["last_pushed_generation"] = info.get("generation")
    _stats["consecutive_failures"] = 0
    _stats["last_error"] = None
    _stats["skipped_budget"] = False
    _stats["skipped_size"] = False
    _stats["skipped_min_interval"] = False


def _record_failure(message, kind=None):
    _stats["last_error"] = message
    _stats["consecutive_failures"] += 1
    if kind:
        _stats[kind] = True


def backup_data(force=False, reason="manual"):
    """Push a fresh snapshot of the database to the bridge.

    `force` bypasses the minimum-interval floor but never the Worker's daily
    budget, so an admin "Back up now" button can be spammed without breaking the
    allowance.
    """
    if not enabled():
        return {"backed_up": False, "reason": "disabled"}
    if not DB.exists():
        return {"backed_up": False, "reason": "no local db"}

    global _last_push_at, _dirty, _dirty_since

    if not force and _last_push_at is not None:
        since = (_now() - _last_push_at) * 1000.0
        if since < MIN_INTERVAL_MS:
            _stats["skipped_min_interval"] = True
            wait = int((MIN_INTERVAL_MS - since) / 1000)
            _log(f"push skipped ({reason}): {wait}s left of the {MIN_INTERVAL_MS // 60000}min floor")
            return {"backed_up": False, "reason": "min_interval",
                    "retry_in_seconds": wait}

    try:
        payload, detail = _snapshot_bytes()
        if payload is None:
            _record_failure(detail)
            _log(f"not pushed: {detail}")
            return {"backed_up": False, "error": detail}
        if len(payload) > MAX_BYTES:
            _record_failure(f"snapshot {len(payload)}B exceeds AADHAR_SYNC_MAX_BYTES", "skipped_size")
            _log(f"not pushed: snapshot is {len(payload)} bytes, over the {MAX_BYTES} byte cap")
            return {"backed_up": False, "error": _stats["last_error"]}

        # The guard that would have stopped the data loss. A database with no
        # users cannot be logged into, so it is what a boot that failed to
        # restore leaves behind. Pushing one over a good snapshot destroys the
        # only copy, and the next deploy then restores the empty file in its
        # place. So an empty database may only be pushed when the bridge is
        # genuinely empty - a real first install with nothing to lose.
        if not _locally_populated():
            if _bridge_has_snapshot():
                _record_failure(
                    "refusing to push an empty database over a stored snapshot", "skipped_empty")
                _log("NOT PUSHED: this database has no users. A restore most "
                     "likely failed, and pushing now would destroy the stored "
                     "backup. Restoring is the fix; pushing is not.")
                return {"backed_up": False, "error": _stats["last_error"]}
            _log("bridge is empty, so the empty first-install database is being pushed")

        # The PUT response carries the Worker's authoritative accounting, so
        # there is no need for a second round trip to learn the budget state.
        _, body, _ = _call("PUT", "/db", payload=payload)
        try:
            info = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            info = {}

        _last_push_at = _now()
        _dirty = False
        _dirty_since = None
        _record_success(info)
        _log(f"pushed aadhaar.db ({len(payload)} bytes) as generation "
             f"{_stats['last_pushed_generation']}, writes today "
             f"{info.get('pushesToday', '?')}/{info.get('dailyPushBudget', '?')}")
        return {"backed_up": True, "bytes": len(payload),
                "generation": _stats["last_pushed_generation"],
                "pushes_today": info.get("pushesToday"),
                "pushes_left": info.get("pushesLeft")}
    except BridgeError as err:
        kind = "skipped_budget" if err.status == 429 else None
        _record_failure(str(err), kind)
        _log(f"backup failed: {err}")
        return {"backed_up": False, "error": str(err), "status": err.status}
    except Exception as err:
        _record_failure(str(err))
        _log(f"backup failed: {err}")
        return {"backed_up": False, "error": str(err)}


def _locally_populated():
    """Does the local database hold a real account, i.e. is it worth backing up?"""
    if not DB.exists():
        return False
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    except sqlite3.DatabaseError:
        return False
    try:
        if _rowcount(con, "users") == 0:
            return False
        present = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return any(t in present for t in DATA_TABLES)
    except sqlite3.DatabaseError:
        return False
    finally:
        con.close()


def _bridge_has_snapshot():
    """Does the bridge already hold something? Never raises: this only gates a push.

    Anything unexpected - a network error, a malformed body, a shape this version
    does not recognise - is treated as "yes, there is a snapshot", because the
    opposite assumption would let the destructive push through.
    """
    try:
        listing = _get_json("/generations")
        if not isinstance(listing, dict):
            return True
        return bool(listing.get("latest"))
    except Exception:
        return True


def _flush():
    """Push once, collapsing concurrent callers into at most one extra run.

    A write that lands mid-push must not be lost, so it sets a flag and one
    more pass runs once the current one finishes.
    """
    global _dirty, _dirty_since
    if not _push_running.acquire(blocking=False):
        _dirty = True
        return
    try:
        while True:
            with _lock:
                _dirty = False
                _dirty_since = None
            backup_data(force=True, reason="scheduled")
            with _lock:
                if not _dirty:
                    return
    finally:
        _push_running.release()


def request_backup():
    """Mark the database as needing a snapshot.

    Debounced and floored: a burst of edits coalesces into one push, and the
    push cannot happen sooner than MIN_INTERVAL_MS after the last one. The old
    code waited only 1 second and pushed the whole database on every write,
    which burned the daily allowance within hours.
    """
    global _timer, _dirty, _dirty_since
    if not enabled() or _shutdown:
        return
    with _lock:
        _dirty = True
        if _dirty_since is None:
            _dirty_since = _now()
    with _timer_lock:
        if _timer is not None:
            _timer.cancel()
        # Wake up either when the floor allows a push, or soon enough that a
        # change made just after a push still goes out promptly.
        delay = 1.0
        if _last_push_at is not None:
            delay = max(1.0, (MIN_INTERVAL_MS - (_now() - _last_push_at) * 1000.0) / 1000.0)
        timer = threading.Timer(delay, _flush)
        timer.daemon = True
        _timer = timer
        timer.start()


def _human(ms):
    """Format a duration for a log line without rounding a sub-minute value to 0."""
    if ms < 60000:
        return f"{int(ms / 1000)}s"
    return f"{int(ms / 60000)}min"


def _install_lifecycle():
    """Register the final-push hook exactly once.

    Deliberately independent of start_interval(): a process may call
    request_backup() without ever starting the scheduler, and it should still
    get a last-chance push on the way out. The old code only wired this up
    inside start_interval(), so such a process silently lost its final change.
    """
    global _lifecycle_installed
    if _lifecycle_installed or not enabled():
        return
    _lifecycle_installed = True
    atexit.register(_on_exit)
    _install_signal_handlers()


def start_interval():
    """Start the periodic snapshot thread once per process."""
    global _thread_started
    if not enabled() or _thread_started:
        return
    _thread_started = True
    _install_lifecycle()

    def _loop():
        # Push once immediately, then on the interval. The loop used to sleep
        # first, which meant a freshly booted instance - including one that had
        # just restored a snapshot - left the bridge unconfirmed for a full hour.
        # If the restore produced something the bridge did not already hold, this
        # is what publishes it.
        try:
            _flush()
        except Exception as err:
            _log(f"startup backup error: {err}")
        while not _shutdown:
            time.sleep(INTERVAL_MS / 1000.0)
            if _shutdown:
                break
            try:
                _flush()
            except Exception as err:  # a bad tick must not kill the thread
                _log(f"interval backup error: {err}")

    threading.Thread(target=_loop, daemon=True, name="kv-sync-interval").start()
    _log(f"bridge active -> {BASE} every {_human(INTERVAL_MS)} "
         f"(floor {_human(MIN_INTERVAL_MS)}), cap {MAX_BYTES} bytes")


def _on_exit():
    """Last chance to push anything written since the final scheduled backup.

    Without this, an edit made in the window before a deploy or restart would
    only exist on the ephemeral disk, and the next boot would restore a snapshot
    from before it.
    """
    global _shutdown
    _shutdown = True
    if not enabled():
        return
    try:
        with _lock:
            pending = _dirty
        if pending:
            _log("exiting with an unpushed change; pushing a final snapshot")
            backup_data(force=True, reason="shutdown")
    except Exception as err:
        _log(f"final push failed: {err}")


def _install_signal_handlers():
    """Push before the process actually dies, while there is still time.

    Render sends SIGTERM and then waits a grace period; that window is the only
    chance to capture a change made moments before a deploy.
    """
    if threading.current_thread() is not threading.main_thread():
        return
    try:
        previous = signal.getsignal(signal.SIGTERM)

        def _handler(signum, frame):
            _on_exit()
            if callable(previous) and previous not in (signal.SIG_DFL, signal.SIG_IGN):
                previous(signum, frame)
            else:
                raise SystemExit(0)

        signal.signal(signal.SIGTERM, _handler)
    except (ValueError, OSError, AttributeError):
        # Not the main thread, or the platform disallows it. atexit still runs.
        pass


def get_status():
    """Everything an admin needs to know whether backups are actually working.

    `bridge` is the Worker's own view; it is `None` when the bridge could not be
    reached, which is itself the signal worth showing.
    """
    bridge = None
    bridge_error = None
    if enabled():
        try:
            bridge = _get_json("/health")
        except BridgeError as err:
            bridge_error = str(err)

    with _lock:
        dirty, dirty_since = _dirty, _dirty_since

    # Age of the newest snapshot as the WORKER sees it. This is the number that
    # actually predicts data loss: if the bridge holds something recent, a
    # restart is safe regardless of whether this process has pushed yet.
    remote_age = (bridge or {}).get("ageSeconds")

    return {
        "enabled": enabled(),
        "url": BASE or None,
        "reachable": bridge_error is None and (bridge is not None or not enabled()),
        "bridge_error": bridge_error,
        "bridge": bridge,
        "restored": _stats["restored"],
        "restored_from": _stats["restored_from"],
        "last_backup_at": _stats["last_backup_at"],
        "last_backup_age_seconds": _age(_stats["last_backup_at"]),
        "newest_snapshot_age_seconds": remote_age,
        "db_bytes": _stats["db_bytes"],
        "last_pushed_generation": _stats["last_pushed_generation"],
        "pending_changes": dirty,
        "pending_since": _iso(dirty_since) if dirty_since else None,
        "consecutive_failures": _stats["consecutive_failures"],
        "last_error": _stats["last_error"],
        "skipped_budget": _stats["skipped_budget"],
        "skipped_size": _stats["skipped_size"],
        "skipped_min_interval": _stats["skipped_min_interval"],
        "writes_today": (bridge or {}).get("pushesToday"),
        "budget": (bridge or {}).get("dailyPushBudget"),
        "budget_left": (bridge or {}).get("pushesLeft"),
        "max_bytes": MAX_BYTES,
        "interval_minutes": INTERVAL_MS // 60000,
        "min_interval_minutes": MIN_INTERVAL_MS // 60000,
        "healthy": _is_healthy(bridge, bridge_error),
    }


def _is_healthy(bridge, bridge_error):
    """Worst-wins, so one broken thing is enough to report unhealthy.

    Deliberately based on the bridge's own view of snapshot age rather than this
    process's push history: a process that has just cold-started and restored has
    perfectly good data even though it has not pushed yet, and flagging that as
    unhealthy would cry wolf on every deploy.
    """
    if not enabled():
        return None
    if bridge_error or not bridge:
        return False
    if _stats["consecutive_failures"] > 0:
        return False
    if not bridge.get("latest"):
        return False  # nothing durable exists yet
    age = bridge.get("ageSeconds")
    if age is None:
        return False
    # Older than two intervals means the scheduler is not keeping up.
    return age <= (INTERVAL_MS / 1000.0) * 2


def _age(iso_ts):
    """Seconds since a UTC 'YYYY-MM-DDTHH:MM:SSZ' timestamp, or None.

    Uses calendar.timegm on an explicitly UTC struct. The obvious-looking
    alternative - time.mktime(time.strptime(...)) - interprets the struct as
    *local* time and then needs time.timezone subtracted, and those two disagree
    across a DST transition, which would make every "age ago" figure in the admin
    UI wrong by an hour twice a year.
    """
    if not iso_ts:
        return None
    try:
        parsed = time.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return None
    return max(0, int(_now() - calendar.timegm(parsed)))


def generations():
    """List the generations the bridge holds, newest first. For admin display."""
    if not enabled():
        return []
    try:
        data = _get_json("/generations")
    except BridgeError as err:
        return [{"error": str(err)}]
    return (data or {}).get("generations", [])


# Arm the final-push hook on import, so a process that only ever calls
# request_backup() still gets a last-chance push on the way out. Must come after
# the definitions above. The signal part is a no-op off the main thread.
_install_lifecycle()


if __name__ == "__main__":
    # Manual entry point: `python kv_sync.py restore|backup|status|generations`
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    if action == "restore":
        print(json.dumps(restore_data(), indent=2))
    elif action == "backup":
        print(json.dumps(backup_data(force="--force" in sys.argv), indent=2))
    elif action == "generations":
        print(json.dumps(generations(), indent=2))
    else:
        print(json.dumps(get_status(), indent=2))
