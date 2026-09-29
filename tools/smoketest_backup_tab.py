"""Smoke test: log in as admin and render the Backup status tab.

Uses Streamlit's own AppTest harness so the real script runs, rather than
importing app.py and asserting on nothing. app.py cannot be imported directly -
it executes its whole auth bootstrap at module level - so the password hash is
computed here with the same parameters.

Run from the repo root:

    python tools_smoketest_backup_tab.py
"""
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA = Path(tempfile.mkdtemp(prefix="aadhar-smoke-"))
os.environ["APP_DATA_DIR"] = str(DATA)
# If the caller supplies bridge credentials the configured path is exercised
# against the real bridge; otherwise the "not configured" path is checked.
BRIDGE_URL = os.environ.get("AADHAR_SYNC_URL", "")
BRIDGE_TOKEN = os.environ.get("AADHAR_SYNC_TOKEN", "")
print("bridge:", BRIDGE_URL or "(unconfigured)")

sys.path.insert(0, str(REPO))

PASSWORD = "Sm0keTest!"
SALT = "a1b2c3"
# Must match app.py: PBKDF2-SHA256, 200_000 rounds.
PW_HASH = hashlib.pbkdf2_hmac("sha256", PASSWORD.encode(), bytes.fromhex(SALT), 200_000).hex()

db = DATA / "aadhaar.db"
con = sqlite3.connect(db)
con.execute("CREATE TABLE users(username TEXT PRIMARY KEY, salt TEXT, pw_hash TEXT, role TEXT,"
            " active INTEGER DEFAULT 1, fails INTEGER DEFAULT 0, locked_until TEXT,"
            " created_at TEXT, must_change INTEGER DEFAULT 0)")
con.execute("CREATE TABLE uploads(id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT,"
            " uploaded_by TEXT, uploaded_at TEXT)")
con.execute("CREATE TABLE camps(id INTEGER PRIMARY KEY AUTOINCREMENT, camp_date TEXT, division TEXT,"
            " sub_division TEXT, location TEXT, transactions INTEGER, remarks TEXT,"
            " created_by TEXT, created_at TEXT)")
con.execute("INSERT INTO users VALUES ('admin',?,?,'admin',1,0,NULL,'2026-09-01',0)",
            (SALT, PW_HASH))
con.commit()
con.close()

from streamlit.testing.v1 import AppTest  # noqa: E402


def check(condition, message):
    if not condition:
        print(f"FAIL  {message}")
        raise SystemExit(1)
    print(f"PASS  {message}")


at = AppTest.from_file(str(REPO / "app.py"), default_timeout=90)
at.run()
check(not at.exception, f"app booted to the login screen (exception: {at.exception})")
check(len(at.text_input) >= 2, f"login form has username + password ({len(at.text_input)} inputs)")
check(len(at.button) >= 1, "login form has a submit button")

at.text_input[0].set_value("admin")
at.text_input[1].set_value(PASSWORD)
at.button[0].click().run()
check(not at.exception, f"login succeeded (exception: {at.exception})")

# The sidebar radio's label is "Navigate" (its label_visibility is collapsed);
# the page names live in its options.
check(len(at.sidebar.radio) == 1, "sidebar has one navigation radio")
nav = list(at.sidebar.radio[0].options)
print("      nav:", nav)
check(bool(nav), "sidebar navigation rendered after login")
check("Backup status" in nav, f"admin nav includes Backup status (got {nav})")
check("Upload data" in nav and "Manage users" in nav, f"admin-only pages present (got {nav})")

at.sidebar.radio[0].set_value("Backup status").run()
check(not at.exception, f"Backup status tab rendered (exception: {at.exception})")

messages = ([m.value for m in at.markdown] + [i.value for i in at.info]
            + [s.value for s in at.success] + [e.value for e in at.error])
for text in messages:
    if text:
        print("      |", text.replace("\n", " ")[:120])

metrics = [m.label for m in at.metric]
print("      metrics:", metrics)

if BRIDGE_URL:
    # Configured: the tab must report real health figures from the bridge.
    check(bool(metrics), f"health metrics rendered (got {metrics})")
    check("Newest snapshot" in metrics, f"snapshot-age metric present (got {metrics})")
    check("Consecutive failures" in metrics, f"failure counter present (got {metrics})")
    labels = [b.label for b in at.button]
    check("Back up now" in labels, f"manual backup button present (got {labels})")
    check("Refresh" in labels, f"refresh button present (got {labels})")

    had_snapshot = "The bridge holds no snapshots yet." not in messages
    if had_snapshot:
        check(not any("NOT working" in t for t in messages if t),
              "populated bridge reports healthy")
    else:
        # A bridge with nothing stored must say so rather than imply safety.
        check(any("NOT working" in t for t in messages if t),
              "empty bridge is reported as NOT backed up")

    # Exercise the manual push path, then confirm the state it claims.
    at.button(key="bk_refresh").click().run()
    at.button(key="bk_backup_now").click().run()
    check(not at.exception, f"manual backup raised no exception ({at.exception})")
    after = [s.value for s in at.success] + [e.value for e in at.error]
    for text in after:
        if text:
            print("      >", text.replace("\n", " ")[:120])
    check(any("Pushed" in t for t in after if t),
          f"manual push reported a new generation (got {after})")

    at.button(key="bk_refresh").click().run()
    at.sidebar.radio[0].set_value("Dashboard").run()
    at.sidebar.radio[0].set_value("Backup status").run()
    final = [s.value for s in at.success] + [e.value for e in at.error]
    check(any("Backups are working" in t for t in final if t),
          f"healthy after the push (got {final})")
else:
    # Unconfigured: the tab must say so instead of implying things are fine.
    check(any("not configured" in t for t in messages if t),
          "unconfigured-bridge state is explained in the UI")
    check(not metrics, f"no misleading metrics when unconfigured (got {metrics})")

shutil.rmtree(DATA, ignore_errors=True)
print("\nALL CHECKS PASSED")
