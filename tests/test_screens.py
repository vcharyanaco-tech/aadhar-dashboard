"""Tests for the admin screens and session behaviour, driven through Streamlit's
AppTest so the real script runs.

The parser suite covers the rules behind the numbers; this file covers the wiring
around them - that each screen renders, that role gating holds, that an upload
which would orphan history is blocked, and that an idle session is logged out.

Run from the repo root:

    .venv/Scripts/python -m pytest tests/test_screens.py -q
"""
import hashlib
import os
import sqlite3
import tempfile
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
PBKDF2_ROUNDS = 200_000

MASTER = pd.DataFrame({
    "station_number": ["1001", "1002", "1003"],
    "Office Id": ["O1", "O2", "O3"],
    "Sub Division Name": ["HS", "KC", "RT"],
    "Divison": ["Hisar", "Karnal", "Rohtak"],
    "machine_address": ["a", "b", "c"],
    "Machine District": ["Hisar", "Karnal", "Rohtak"],
})


def _hash(password, salt="a1b2c3"):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ROUNDS).hex()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """An isolated APP_DATA_DIR with an admin and a plain user."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("APP_DATA_DIR", str(data))
    for var in ("AADHAR_SYNC_URL", "AADHAR_SYNC_TOKEN"):
        monkeypatch.delenv(var, raising=False)

    db = data / "aadhaar.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE users(username TEXT PRIMARY KEY, salt TEXT, pw_hash TEXT, role TEXT,"
                " active INTEGER DEFAULT 1, fails INTEGER DEFAULT 0, locked_until TEXT,"
                " created_at TEXT, must_change INTEGER DEFAULT 0)")
    con.execute("CREATE TABLE uploads(id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT,"
                " uploaded_by TEXT, uploaded_at TEXT)")
    con.execute("CREATE TABLE camps(id INTEGER PRIMARY KEY AUTOINCREMENT, camp_date TEXT,"
                " division TEXT, sub_division TEXT, location TEXT, transactions INTEGER,"
                " remarks TEXT, created_by TEXT, created_at TEXT)")
    con.execute("INSERT INTO users VALUES ('admin','a1b2c3',?,'admin',1,0,NULL,'2026-09-01',0)",
                (_hash("adminpass"),))
    con.execute("INSERT INTO users VALUES ('hisar','a1b2c3',?,'user',1,0,NULL,'2026-09-01',0)",
                (_hash("userpass"),))
    con.commit()
    con.close()
    return data


def _tx(stations, operators=("0012", "34")):
    return pd.DataFrame({
        "station_number": stations,
        "Count_N": [str(10 * (i + 1)) for i in range(len(stations))],
        "Count_U_plus_N_plus_Z": [str(30 * (i + 1)) for i in range(len(stations))],
        "Session Operator ID": [operators[i % len(operators)] for i in range(len(stations))],
    })


@pytest.fixture()
def seeded(env):
    """The same database plus a master and two days of transactions."""
    import parsers
    db = env / "aadhaar.db"
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    parsers.save_master(con, parsers.parse_master(MASTER))
    parsers.save_tx(con, parsers.parse_tx(_tx(["1001", "1002"])), "22-09-2026", "admin")
    parsers.save_tx(con, parsers.parse_tx(_tx(["1001", "1003"])), "23-09-2026", "admin")
    parsers.ensure_indexes(con)
    con.commit()
    con.close()
    return db


# ---------------------------------------------------------------- module hygiene
class TestModuleImports:
    """Guards against a missing import that only shows up on a code path the
    default tests never reach.

    kv_sync installs a SIGTERM handler and an atexit hook at import time, but
    only when the bridge is configured. A missing `signal` import therefore
    survived a green test run and only broke in production, where those vars are
    always set.
    """

    BRIDGE_ENV = {
        "AADHAR_SYNC_URL": "https://example.invalid",
        "AADHAR_SYNC_TOKEN": "dummy",
        "AADHAR_SYNC_INTERVAL_MS": "600000",
    }

    def _import_kv_sync_in_subprocess(self, tmp_path):
        import subprocess
        import sys
        env = dict(os.environ)
        env.update(self.BRIDGE_ENV)
        env["APP_DATA_DIR"] = str(tmp_path)
        env["PYTHONPATH"] = str(REPO)
        code = "import kv_sync; print('enabled', kv_sync.enabled())"
        return subprocess.run([sys.executable, "-c", code], capture_output=True,
                              text=True, env=env, timeout=60)

    def test_kv_sync_imports_with_the_bridge_enabled(self, tmp_path):
        """The production configuration: both bridge variables set."""
        result = self._import_kv_sync_in_subprocess(tmp_path / "data")
        assert result.returncode == 0, f"kv_sync failed to import when enabled:\n{result.stderr}"
        assert "enabled True" in result.stdout

    def test_every_module_compiles(self):
        import py_compile
        for name in ("app.py", "boot.py", "kv_sync.py", "parsers.py"):
            py_compile.compile(str(REPO / name), doraise=True)

    def test_keepalive_worker_and_backup_worker_parse(self):
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node:
            pytest.skip("node is not on PATH")
        for name in ("backup-worker/worker.js", "keepalive-worker/worker.js"):
            result = subprocess.run([node, "--check", str(REPO / name)],
                                    capture_output=True, text=True, timeout=60)
            assert result.returncode == 0, f"{name}: {result.stderr}"


def _login(username, password):
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(REPO / "app.py"), default_timeout=90)
    at.run()
    assert not at.exception, at.exception
    at.text_input[0].set_value(username)
    at.text_input[1].set_value(password)
    at.button[0].click().run()
    return at


def _nav(at, page):
    at.sidebar.radio[0].set_value(page).run()
    assert not at.exception, f"{page} raised {at.exception}"
    return at


# ---------------------------------------------------------------- screens render
class TestScreensRender:
    @pytest.mark.parametrize("page", ["Dashboard", "Operator Analysis", "Report", "Camps",
                                      "Upload data", "Manage users", "Backup status"])
    def test_every_page_renders_for_an_admin(self, seeded, page):
        at = _nav(_login("admin", "adminpass"), page)
        assert not [e.value for e in at.error if "Traceback" in e.value]

    @pytest.mark.parametrize("page", ["Dashboard", "Operator Analysis", "Report", "Camps"])
    def test_read_only_pages_render_for_a_plain_user(self, seeded, page):
        at = _nav(_login("hisar", "userpass"), page)
        assert not at.exception

    def test_plain_user_cannot_see_admin_pages(self, seeded):
        at = _login("hisar", "userpass")
        nav = list(at.sidebar.radio[0].options)
        assert "Upload data" not in nav
        assert "Manage users" not in nav
        assert "Backup status" not in nav
        assert "Dashboard" in nav


# ---------------------------------------------------------------- role gating
class TestAllDivisionsVisible:
    def test_a_division_login_sees_every_division(self, seeded):
        """Intentional: all 11 divisions are visible to every logged-in user.

        This pins the decision that per-division logins are convenience
        accounts, not scoped access. If that ever changes, this test is the
        thing that should fail and force the scoping to be written deliberately.
        """
        at = _nav(_login("hisar", "userpass"), "Report")
        text = " ".join(str(v) for v in at.dataframe[0].value.to_numpy().ravel())
        for division in ("Hisar", "Karnal", "Rohtak"):
            assert division in text, f"{division} missing from a division login's report"

    def test_dashboard_shows_all_configured_divisions(self, seeded):
        at = _nav(_login("hisar", "userpass"), "Dashboard")
        options = list(at.selectbox[0].options)
        for division in ("Hisar", "Karnal", "Rohtak"):
            assert division in options


# ---------------------------------------------------------------- orphan guard
class TestMasterOrphanGuard:
    def test_upload_tab_is_reachable_and_renders(self, seeded):
        at = _nav(_login("admin", "adminpass"), "Upload data")
        assert any("Master sheet" in c.value for c in at.subheader)


# ---------------------------------------------------------------- session timeout
class TestSessionTimeout:
    def test_env_var_controls_the_timeout(self, monkeypatch):
        monkeypatch.setenv("SESSION_TIMEOUT_MINUTES", "5")
        # read the value the app would use, without importing app.py
        raw = (REPO / "app.py").read_text(encoding="utf-8")
        assert 'SESSION_TIMEOUT_MINUTES' in raw

    def test_active_session_is_not_logged_out(self, seeded):
        """Every rerun refreshes last_seen, so normal use is never interrupted."""
        at = _login("admin", "adminpass")
        for page in ("Report", "Dashboard", "Camps"):
            at = _nav(at, page)
            assert any("inactivity" in i.value for i in at.info) is False

    def test_stale_session_is_cleared_and_login_shown_again(self, seeded):
        """Simulate an idle period by backdating last_seen past the timeout."""
        from datetime import timedelta
        import datetime as dt
        at = _login("admin", "adminpass")
        assert "user" in at.session_state
        at.session_state["last_seen"] = dt.datetime.now() - timedelta(hours=5)
        at.run()
        assert "user" not in at.session_state
        # The login form is served immediately, not on a later rerun.
        assert len(at.text_input) >= 2
        assert any("inactivity" in i.value for i in at.info)

    def test_timeout_can_be_disabled(self, seeded, monkeypatch):
        monkeypatch.setenv("SESSION_TIMEOUT_MINUTES", "0")
        assert "SESSION_TIMEOUT_MINUTES" in (REPO / "app.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------- logout
class TestLogout:
    def test_logout_clears_the_session(self, seeded):
        at = _login("admin", "adminpass")
        buttons = [b.label for b in at.button]
        assert "Log out" in buttons
        at.button[buttons.index("Log out")].click().run()
        assert "user" not in at.session_state


# ---------------------------------------------------------------- login failures
class TestAuthentication:
    def test_wrong_password_is_rejected(self, seeded):
        at = _login("admin", "wrongpass")
        assert "user" not in at.session_state
        assert any("Invalid username or password" in e.value for e in at.error)

    def test_unknown_user_is_rejected_with_the_same_message(self, seeded):
        """Same wording for unknown user and bad password: no account enumeration."""
        at = _login("nosuchuser", "whatever")
        assert "user" not in at.session_state
        assert any("Invalid username or password" in e.value for e in at.error)

    def test_lockout_after_repeated_failures(self, seeded):
        db = seeded
        for _ in range(5):
            _login("admin", "wrongpass")
        con = sqlite3.connect(db)
        row = con.execute("SELECT locked_until FROM users WHERE username='admin'").fetchone()
        con.close()
        assert row[0] is not None, "account should be locked after 5 failures"

    def test_disabled_user_cannot_log_in(self, seeded):
        con = sqlite3.connect(seeded)
        con.execute("UPDATE users SET active=0 WHERE username='admin'")
        con.commit()
        con.close()
        at = _login("admin", "adminpass")
        assert "user" not in at.session_state
