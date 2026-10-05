"""Aadhaar Transaction Monitor - Haryana Circle.

Admin: uploads master + transaction sheets, creates/resets/disables user logins.
Users: log in and view the dashboard only.
Run:  streamlit run app.py
"""
import base64
import hashlib
import hmac
import mimetypes
import os
import re
import secrets
import sqlite3
from io import BytesIO
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

import kv_sync

# plotly and openpyxl are imported on first use, not at module scope.
#
# The login page pays for every module-level import on a cold start, and Render's
# free tier runs on a throttled shared CPU where that cost is several times
# higher than on a workstation. Measured locally: streamlit 811 ms, pandas
# 485 ms, openpyxl 166 ms, plotly 87 ms. plotly is only needed for the dashboard
# charts and openpyxl only for the Excel exports, so neither is required to draw
# a login form, and paying for both on every cold start was roughly a fifth of
# the page's load time. pandas stays at module scope: parsers needs it, and
# read_sql_query is on the main path.


def _plotly():
    global _px
    if _px is None:
        import plotly.express as px
        _px = px
    return _px


def _excel_helpers():
    global _excel
    if _excel is None:
        from openpyxl.styles import Font
        from openpyxl.utils import get_column_letter
        _excel = (Font, get_column_letter)
    return _excel


_px = None
_excel = None
import parsers
from parsers import (
    date_from_filename,
    describe_excluded,
    norm_key,
    parse_first_usable as read_file,
    parse_label_date,
    parse_master,
    parse_operator_master,
    parse_tx,
    split_working_days,
    target_table,
)

DATA_DIR = Path(os.environ.get("APP_DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB = DATA_DIR / "aadhaar.db"
PBKDF2_ROUNDS = 200_000
MAX_FAILS, LOCK_MINUTES = 5, 5
# The shared initial password for bulk-created division logins. It lives in the
# environment, not in source: a credential committed to a public repository is
# published even if the file is later edited, and this one is handed to every
# division at once. Set AADHAR_DEFAULT_TEMP_PASSWORD on the host to keep using
# the Circle's standard password; when it is unset a random password is
# generated per batch instead of falling back to a shared default.
DEFAULT_TEMP_PASSWORD = os.environ.get("AADHAR_DEFAULT_TEMP_PASSWORD") or ""
# Idle logout in minutes. 0 disables it. Long enough not to interrupt a user
# mid-report, short enough that an unattended station is not still signed in.
SESSION_TIMEOUT_MINUTES = int(float(os.environ.get("SESSION_TIMEOUT_MINUTES") or 30))
APP_NAME = "HARYANA CIRCLE AADHAR MONITORING DASHBOARD"
# Put the India Post logo in the same folder as app.py, named logo.webp (or
# .png/.jpg). WebP is preferred and logo.webp is the one shipped: logo.png is
# 600x389 and the largest thing this app ever sends, but it is only ever
# displayed at about 170px, so it was roughly 3x oversized. At 400px wide the
# same image is 28 KB instead of 88 KB, and since the login page inlines it the
# saving lands on every single render. logo.png is kept unmodified as the source
# the webp was generated from.
LOGO = next((p for n in ("logo.webp", "logo.png", "logo.jpg", "logo.jpeg")
             if (p := Path(__file__).parent / n).exists()), None)


@st.cache_data(show_spinner=False)
def _encode_logo(path_str, stamp):
    """Encode the logo once. `stamp` is part of the cache key, so replacing
    logo.png invalidates it without needing a manual cache clear."""
    mime = mimetypes.guess_type(path_str)[0] or "image/png"
    encoded = base64.b64encode(Path(path_str).read_bytes()).decode()
    return f"data:{mime};base64,{encoded}"


def logo_data_uri():
    """The logo as a data URI, encoded once per process rather than per render.

    It is ~88 KB on disk, which is ~118 KB once base64-encoded, and the login page
    inlines it. Streamlit re-runs the whole script on every interaction, so
    without this the same 118 KB was re-encoded on every single render.
    """
    if LOGO is None:
        return None
    stat = LOGO.stat()
    return _encode_logo(str(LOGO), f"{stat.st_mtime_ns}-{stat.st_size}")

st.set_page_config(page_title=APP_NAME, layout="wide")


# ---------------------------------------------------------------- database
_READ_ONLY_PREFIXES = ("SELECT", "PRAGMA", "WITH", "EXPLAIN")


def _is_write(sql):
    """True unless the statement is plainly a read.

    `run()` used to commit unconditionally, so every SELECT took a write lock
    and appended a WAL frame. There are dozens of reads per page render, so on a
    throttled free-plan instance that was a meaningful cost for no benefit.
    """
    return not sql.lstrip().upper().startswith(_READ_ONLY_PREFIXES)


def run(sql, args=(), one=False, many=False):
    con = sqlite3.connect(DB)
    try:
        con.row_factory = sqlite3.Row
        cur = con.execute(sql, args)
        out = cur.fetchone() if one else cur.fetchall() if many else None
        if _is_write(sql):
            con.commit()
        return out
    finally:
        con.close()


def connect():
    """A connection for the parsers module, which takes `con` rather than the path.

    Row access by name is kept on because callers rely on sqlite3.Row.
    """
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def read_sql(sql, params=()):
    con = sqlite3.connect(DB)
    try:
        return pd.read_sql_query(sql, con, params=params)
    finally:
        con.close()


def table_exists(name):
    return run("SELECT 1 FROM sqlite_master WHERE name=?", (name,), one=True) is not None


def init_db():
    run("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, salt TEXT, pw_hash TEXT, role TEXT,
        active INTEGER DEFAULT 1, fails INTEGER DEFAULT 0, locked_until TEXT, created_at TEXT,
        must_change INTEGER DEFAULT 0)""")
    run("""CREATE TABLE IF NOT EXISTS uploads(
        id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT, uploaded_by TEXT, uploaded_at TEXT)""")
    run("""CREATE TABLE IF NOT EXISTS camps(
        id INTEGER PRIMARY KEY AUTOINCREMENT, camp_date TEXT, division TEXT, sub_division TEXT,
        location TEXT, transactions INTEGER, remarks TEXT, created_by TEXT, created_at TEXT)""")
    run("""CREATE TABLE IF NOT EXISTS division_targets(
        division TEXT PRIMARY KEY, daily_target INTEGER NOT NULL,
        updated_by TEXT, updated_at TEXT)""")
    run("""CREATE TABLE IF NOT EXISTS audit(
        id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, username TEXT, action TEXT, detail TEXT)""")
    # One row per machine whose station ID changed. The old ID stays in `master`
    # (so history keeps its division); this table says from which date it stops
    # counting and the new ID starts. Full copies of both master rows are kept so a
    # later master upload can put them back.
    run("""CREATE TABLE IF NOT EXISTS station_changes(
        id INTEGER PRIMARY KEY AUTOINCREMENT, old_key TEXT, old_station TEXT,
        new_key TEXT, new_station TEXT, office_id TEXT, division TEXT, sub_division TEXT,
        address TEXT, district TEXT, effective_date TEXT, changed_by TEXT, changed_at TEXT)""")
    # One row per Division / Sub Division transfer of a station. master always holds
    # the CURRENT placement; this table keeps where the station was before, and from
    # which date, so reports for earlier dates can still show the old Division.
    run("""CREATE TABLE IF NOT EXISTS station_transfers(
        id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT, station TEXT, office_id TEXT,
        old_division TEXT, old_sub_division TEXT, new_division TEXT, new_sub_division TEXT,
        effective_date TEXT, changed_by TEXT, changed_at TEXT)""")
    con = connect()
    try:
        parsers.ensure_cols(con, "users", {"must_change": "INTEGER DEFAULT 0", "division": "TEXT"})
        # The report queries filter on tx.upload_id and join on tx.key; without
        # these every page load is a full scan of a table that grows daily.
        parsers.ensure_indexes(con)
        parsers.ensure_audit_index_on(con)
        con.commit()
    finally:
        con.close()
    seed_division_targets()


def seed_division_targets():
    """Copy the built-in targets into the table the first time it is empty.

    Deliberately only on an empty table. Once an admin edits a target it is
    theirs, and a later change to the constants in parsers.py must not silently
    overwrite it - that would make a target look like it had been changed by
    someone who did not change it.
    """
    if run("SELECT 1 FROM division_targets LIMIT 1", one=True):
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    for division, target in parsers.DIVISION_DAILY_TARGETS.items():
        run("INSERT OR IGNORE INTO division_targets(division, daily_target, updated_by, updated_at)"
            " VALUES (?,?,?,?)", (division, target, "built-in", now))


def daily_targets():
    """Division -> daily target, from the database with the built-ins as a fallback.

    Falls back to the constants so a division present in the master sheet but
    absent from the table still gets measured rather than silently dropped from
    the target comparison.
    """
    table = {}
    for r in run("SELECT division, daily_target FROM division_targets", many=True):
        table[r["division"]] = r["daily_target"]
    merged = dict(parsers.DIVISION_DAILY_TARGETS)
    merged.update(table)
    return merged


@st.cache_data(ttl=60, show_spinner=False)
def _target_lookup_cached():
    """Cached so the lookup is not rebuilt from a table read on every render."""
    return target_lookup()


def target_lookup():
    """Name-insensitive division -> daily target, for the target table."""
    return {parsers.norm(k): v for k, v in daily_targets().items()}


def audit(action, detail=""):
    """Record an action in the audit log. Best-effort: never block the action."""
    try:
        user = st.session_state.get("user", {}).get("username", "system")
        run("INSERT INTO audit(at, username, action, detail) VALUES (?,?,?,?)",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user, action, str(detail)[:500]))
    except Exception:
        pass


# ---------------------------------------------------------------- auth
def hash_pw(password, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ROUNDS).hex()
    return salt, h


def check_new_credentials(username, pw1, pw2, need_username=True):
    if need_username:
        if not re.fullmatch(r"[a-z0-9_.-]{3,30}", username):
            return "Username must be 3-30 characters: lowercase letters, digits, dot, dash or underscore."
        if run("SELECT 1 FROM users WHERE username=?", (username,), one=True):
            return "This username already exists."
    if len(pw1) < 8:
        return "Password must be at least 8 characters."
    if pw1 != pw2:
        return "Passwords do not match."
    return None


def add_user(username, password, role="user", must_change=0, division=None):
    salt, h = hash_pw(password)
    run("INSERT INTO users(username, salt, pw_hash, role, created_at, must_change, division)"
        " VALUES (?,?,?,?,?,?,?)",
        (username, salt, h, role, datetime.now().strftime("%Y-%m-%d %H:%M"), must_change, division))
    audit("user.create", f"{username} as {role}" + (f" ({division})" if division else ""))
    kv_sync.request_backup()


def set_password(username, password, must_change=0):
    salt, h = hash_pw(password)
    run("UPDATE users SET salt=?, pw_hash=?, fails=0, locked_until=NULL, must_change=? WHERE username=?",
        (salt, h, must_change, username))
    # Never the password itself: the audit log is backed up off-site.
    audit("user.password_change", username)
    kv_sync.request_backup()


def slugify_username(name):
    s = re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")
    while len(s) < 3:
        s += "x"
    return s[:30]


def generate_temp_password():
    """A readable random password for a batch of new logins.

    12 characters from a pool that avoids visually ambiguous glyphs, so it can
    be read aloud or copied off a screen. Satisfies the 8-character minimum.
    """
    pool = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(pool) for _ in range(12))


def bulk_create_division_users(default_password=None):
    """Create one login per Division found in the master sheet.

    Username = the division's name (slugified). `default_password` defaults to
    the configured shared password; when none is configured a single random
    password is generated for the whole batch instead of a hardcoded fallback,
    so the logins never share a credential that is published in the repository.
    Returns (created, skipped) where each entry is (division, username).
    """
    if not table_exists("master"):
        return [], [], ""
    password = default_password or DEFAULT_TEMP_PASSWORD or generate_temp_password()
    divs = read_sql("SELECT DISTINCT division FROM master WHERE division!='' ORDER BY division")
    existing = {r["username"] for r in run("SELECT username FROM users", many=True)}
    created, skipped = [], []
    for division in divs["division"]:
        uname = slugify_username(division)
        if uname in existing:
            # Logins created before division-linking have no division. Attach it
            # now if the field is still empty, so they are not left locked out of
            # camp entry. Never overwrite a division an admin set by hand.
            run("UPDATE users SET division=? WHERE username=? AND role='user'"
                " AND (division IS NULL OR division='')",
                (division, uname))
            skipped.append((division, uname))
            continue
        add_user(uname, password, "user", must_change=1, division=division)
        existing.add(uname)
        created.append((division, uname))
    if created:
        audit("user.bulk_create", f"{len(created)} division login(s): "
                                  + ", ".join(u for _, u in created))
    return created, skipped, password


def authenticate(username, password):
    username = username.strip().lower()
    u = run("SELECT * FROM users WHERE username=?", (username,), one=True)
    generic = "Invalid username or password."
    if not u:
        hash_pw(password)  # keep timing similar
        return None, generic
    if u["locked_until"] and datetime.fromisoformat(u["locked_until"]) > datetime.now():
        mins = int((datetime.fromisoformat(u["locked_until"]) - datetime.now()).total_seconds() // 60) + 1
        return None, f"Too many failed attempts. Try again in {mins} minute(s)."
    _, h = hash_pw(password, u["salt"])
    if hmac.compare_digest(h, u["pw_hash"]) and u["active"]:
        run("UPDATE users SET fails=0, locked_until=NULL WHERE username=?", (username,))
        return {"username": u["username"], "role": u["role"], "must_change": u["must_change"]}, None
    fails = u["fails"] + 1
    lock = (datetime.now() + timedelta(minutes=LOCK_MINUTES)).isoformat() if fails >= MAX_FAILS else None
    run("UPDATE users SET fails=?, locked_until=? WHERE username=?", (0 if lock else fails, lock, username))
    return None, generic


def is_admin():
    return st.session_state.get("user", {}).get("role") == "admin"


def user_division():
    """The Division linked to the logged-in user, read fresh from the database.

    Read fresh rather than from the session copy so that an admin changing a
    user's division takes effect on their very next request, without needing a
    re-login. Returns None for admins and for users with no division set.
    """
    me = st.session_state.get("user", {})
    row = run("SELECT division FROM users WHERE username=?", (me.get("username", ""),), one=True)
    d = (row["division"] or "").strip() if row else ""
    return d or None


def flash(msg):
    st.session_state["flash"] = msg
    st.rerun()


def _password_dialog_body(username, password, note):
    st.write(note)
    if username:
        st.write(f"Login: **{username}**")
    st.code(password, language=None)
    st.caption("Shown only now. It is not stored in readable form and will not appear again. "
               "The user must set a new password on first login.")
    if st.button("Close", key="pw_dialog_close"):
        st.rerun()


def show_password_popup(title, username, password, note):
    """Show a password once, in a pop-up. Falls back to an inline box on old Streamlit."""
    dialog = getattr(st, "dialog", None) or getattr(st, "experimental_dialog", None)
    if dialog is None:
        st.warning(f"{title}: {note} Login: {username or '-'}  Password: {password}")
        return
    dialog(title)(_password_dialog_body)(username, password, note)


# ---------------------------------------------------------------- file parsing
# The pure parsing work lives in parsers.py so it can be tested without
# Streamlit. Only the UI that chooses which uploads to report on stays here.
def sort_uploads_by_date_desc(uploads):
    """Sort uploads newest-first by their label's calendar date; uploads whose label isn't
    a recognisable date are kept at the end, newest id first."""
    upload_date = {r["id"]: parse_label_date(r["label"]) for r in uploads}
    return (sorted((r for r in uploads if upload_date[r["id"]]),
                   key=lambda r: (upload_date[r["id"]], r["id"]), reverse=True)
           + sorted((r for r in uploads if not upload_date[r["id"]]),
                   key=lambda r: r["id"], reverse=True))


def select_period_ids(uploads, key_prefix):
    """UI to choose one or more uploads, either by a from/to date range or by picking
    specific uploads by name. Returns a list of upload ids, or None if nothing is selected yet."""
    upload_date = {r["id"]: parse_label_date(r["label"]) for r in uploads}
    dated_ids = [r["id"] for r in uploads if upload_date[r["id"]]]

    mode = st.radio("Select period by", ["Date range", "Specific upload(s)"],
                    horizontal=True, disabled=not dated_ids, key=f"{key_prefix}_mode",
                    help=None if dated_ids else "No upload labels look like dates yet, "
                                                 "so pick uploads by name instead.")

    if mode == "Date range" and dated_ids:
        all_dates = [upload_date[i] for i in dated_ids]
        lo, hi = min(all_dates), max(all_dates)
        rng = st.date_input("Date range (from - to)", value=(lo, hi), min_value=lo, max_value=hi,
                            format="DD-MM-YYYY", key=f"{key_prefix}_range")
        if isinstance(rng, tuple) and len(rng) < 2:
            st.info("Select the end date to complete the range.")
            return None
        start, end = rng
        ids = [i for i in dated_ids if start <= upload_date[i] <= end]
        if not ids:
            st.info("No uploads fall in this date range.")
            return None
        st.caption(f"{len(ids)} day(s) included: "
                   f"{', '.join(sorted(upload_date[i].strftime('%d-%m-%Y') for i in ids))}")
        return ids

    opts = {f"{r['label']} (#{r['id']})": r["id"] for r in sort_uploads_by_date_desc(uploads)}
    sel = st.multiselect("Days / uploads to include", list(opts), default=[next(iter(opts))],
                         key=f"{key_prefix}_sel")
    if not sel:
        st.info("Select at least one upload.")
        return None
    return [opts[s] for s in sel]


def _refuse_orphan_replace(summary):
    """Veto hook for save_master: raise if replacing the master would orphan data.

    The master is how every stored transaction row is mapped to a division, so a
    replacement that drops station keys does not just add rows -- it makes
    existing history invisible in the dashboard and the report, without appearing
    in the "not in master" list either. Blocking here turns silent data loss into
    a message the admin has to acknowledge.
    """
    raise ValueError(
        f"This master sheet would leave {summary['orphaned_keys']} station(s) already "
        f"recorded in the transaction history with no master entry. Their data would stop "
        f"appearing in the dashboard and reports. Sample: {', '.join(summary['sample'])}. "
        f"Upload the complete master, or clear the existing transaction uploads first.")


def replace_master(m, confirm_orphans=False):
    """Replace the master table, refusing by default if it would orphan history.

    Returns a summary dict. Set `confirm_orphans` to proceed despite the warning.
    """
    m, carried = carry_over_station_changes(m)
    if carried:
        audit("master.carry_over", f"{carried} station ID(s) from Station ID changes re-added to the new master")
    m, moved = reapply_station_transfers(m)
    if moved:
        audit("master.transfer_reapplied",
              f"{moved} transferred station(s) kept in their new Division / Sub Division")
    con = connect()
    try:
        summary = parsers.save_master(con, m, on_replace=None if confirm_orphans else _refuse_orphan_replace)
        con.commit()
    finally:
        con.close()
    clear_report_cache()
    audit("master.replace", f"{summary['incoming']} stations in, "
                            f"{summary['orphaned']} previously-seen key(s) orphaned"
                            + (", OVERRIDDEN" if confirm_orphans else ""))
    kv_sync.request_backup()
    return summary


def clear_report_cache():
    """Drop the cached report aggregates.

    Only master, tx and operator_master affect them, so the users/camps tables
    deliberately do not clear this. The TTL is a safety net, not the mechanism:
    an admin uploading a sheet should see it immediately, not after five minutes.
    """
    for name in ("build_period", "machine_address_map", "station_operator_map"):
        try:
            globals()[name].clear()
        except Exception:
            pass


def save_operator_master(m):
    con = connect()
    try:
        n = parsers.save_operator_master(con, m)
        con.commit()
    finally:
        con.close()
    clear_report_cache()
    audit("operator_master.replace", f"{n} operators")
    kv_sync.request_backup()
    return n


def operator_names():
    """operator_name indexed by normalised key, deterministically tie-broken."""
    con = connect()
    try:
        return parsers.operator_names(con)
    finally:
        con.close()


def save_tx(t, label, by):
    """Append one day's transactions. Rejects a frame that breaks the column contract."""
    con = connect()
    try:
        upload_id = parsers.save_tx(con, t, label, by)
        con.commit()
    finally:
        con.close()
    clear_report_cache()
    audit("tx.upload", f"{label}: {len(t)} rows")
    kv_sync.request_backup()
    return upload_id


# ---------------------------------------------------------------- station ID changes
def apply_station_changes(agg, missing, period_dates, changes):
    """Adjust the period aggregates for machines whose station ID changed.

    An old ID stays in master forever, so its history keeps its division. What
    changes is whether it is *counted*: from the effective date onward the old ID
    is no longer a station, and before the effective date the new ID is not yet
    one. A retired/not-yet-live ID is dropped from "no data" and "zero
    transactions" only; if it did record transactions in the period it still
    counts as working. A period that straddles the effective date counts both IDs.
    Periods containing an upload whose label is not a date are left unadjusted.
    """
    if changes is None or len(changes) == 0:
        return agg, missing
    agg = agg.copy()
    missing = missing.copy()

    # Backstop: an old ID missing from master still maps to its old division.
    info = changes.drop_duplicates("old_key", keep="last").set_index("old_key")
    stray = (agg["division"] == "Not in master") & agg["key"].isin(info.index)
    if stray.any():
        for col in ("division", "sub_division", "office_id"):
            if col in agg.columns:
                agg.loc[stray, col] = agg.loc[stray, "key"].map(info[col])

    if not period_dates or any(d is None for d in period_dates):
        return agg, missing
    start, end = min(period_dates), max(period_dates)
    eff = pd.to_datetime(changes["effective_date"], errors="coerce").dt.date
    old_gone = set(changes.loc[eff.notna() & (eff <= start), "old_key"])
    new_pending = set(changes.loc[eff.notna() & (eff > end), "new_key"])
    inactive = old_gone | new_pending
    if inactive:
        missing = missing[~missing["key"].isin(inactive)]
        agg = agg[~(agg["key"].isin(inactive) & (agg["total"] <= 0))]
    return agg.reset_index(drop=True), missing.reset_index(drop=True)


def build_period_adj(ids):
    """build_period plus the station-ID-change adjustments (never cached, cheap)."""
    agg, missing, m, ops = build_period(ids)
    try:
        changes = read_sql("SELECT * FROM station_changes")
        transfers = read_sql("SELECT * FROM station_transfers ORDER BY effective_date, id")
        if changes.empty and transfers.empty:
            return agg, missing, m, ops
        marks = ",".join("?" * len(ids))
        labels = run(f"SELECT label FROM uploads WHERE id IN ({marks})", tuple(ids), many=True)
        dates = [parse_label_date(r["label"]) for r in labels]
        agg, missing = apply_station_changes(agg, missing, dates, changes)
        agg, missing = apply_station_transfers(agg, missing, dates, transfers)
    except Exception as e:
        st.warning(f"Station ID changes could not be applied to this report: {e}")
    return agg, missing, m, ops


def carry_over_station_changes(m):
    """Put retired and replacement stations back into a freshly uploaded master.

    A master sheet normally lists only current IDs. Without this, uploading it
    would drop the old IDs (their history would show "Not in master") and the new
    IDs entered through the Station ID change form. Returns (master, rows_added).
    """
    try:
        ch = read_sql("SELECT * FROM station_changes ORDER BY id")
        need = {"key", "station", "office_id", "division", "sub_division"}
        if ch.empty or not need.issubset(m.columns):
            return m, 0
        have = set(m["key"].astype(str))
        rows = []
        for _, r in ch.iterrows():
            for k, name in ((r["old_key"], r["old_station"]), (r["new_key"], r["new_station"])):
                if str(k) in have:
                    continue
                have.add(str(k))
                rows.append({"key": k, "station": name, "office_id": r["office_id"],
                             "division": r["division"], "sub_division": r["sub_division"],
                             "address": r["address"], "district": r["district"]})
        if not rows:
            return m, 0
        extra = pd.DataFrame(rows).reindex(columns=m.columns)
        for c in extra.columns:
            if extra[c].dtype == object:
                extra[c] = extra[c].fillna("")
        return pd.concat([m, extra], ignore_index=True), len(rows)
    except Exception:
        return m, 0


def register_station_change(old_key, new_id, effective, by):
    """Retire `old_key` from `effective` and add `new_id` to master. Returns (ok, message)."""
    new_id = str(new_id).strip()
    if not new_id:
        return False, "Enter the new station ID."
    con = connect()
    try:
        old = con.execute("SELECT * FROM master WHERE key=?", (old_key,)).fetchone()
        if old is None:
            return False, "The selected station is no longer in the master."
        old = dict(old)
        try:
            new_key = norm_key(new_id)
            rule_ok = norm_key(old["station"]) == old["key"]
        except Exception:
            return False, "Could not work out the key for this station ID."
        if not rule_ok:
            return False, ("The master's keys are not built from the station ID alone, so a new key "
                           "cannot be derived safely. Send parsers.py so this can be adapted.")
        if not new_key or new_key == old_key:
            return False, "The new station ID must be different from the old one."
        if con.execute("SELECT 1 FROM master WHERE key=?", (new_key,)).fetchone():
            return False, "This station ID already exists in the master."
        row = dict(old)
        row["key"], row["station"] = new_key, new_id
        cols = list(row)
        con.execute("INSERT INTO master(" + ",".join(f'"{c}"' for c in cols) + ") VALUES ("
                    + ",".join("?" * len(cols)) + ")", [row[c] for c in cols])
        con.execute(
            "INSERT INTO station_changes(old_key, old_station, new_key, new_station, office_id, division,"
            " sub_division, address, district, effective_date, changed_by, changed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (old_key, old.get("station"), new_key, new_id, old.get("office_id"), old.get("division"),
             old.get("sub_division"), old.get("address"), old.get("district"),
             effective.strftime("%Y-%m-%d"), by, datetime.now().strftime("%Y-%m-%d %H:%M")))
        con.commit()
    finally:
        con.close()
    clear_report_cache()
    audit("station.change", f"{old.get('station')} -> {new_id} ({old.get('division')}/"
                            f"{old.get('sub_division')}) effective {effective.strftime('%d-%m-%Y')}")
    kv_sync.request_backup()
    return True, (f"Station ID changed: {old.get('station')} -> {new_id}, effective "
                  f"{effective.strftime('%d-%m-%Y')}.")


def undo_station_change(change_id):
    """Reverse a change made by mistake. Refused once the new ID has transaction data."""
    row = run("SELECT * FROM station_changes WHERE id=?", (change_id,), one=True)
    if row is None:
        return False, "That change no longer exists."
    try:
        used = run("SELECT 1 FROM tx WHERE key=? LIMIT 1", (row["new_key"],), one=True)
    except Exception:
        return False, "Could not check whether the new ID already has data, so nothing was changed."
    if used:
        return False, ("The new station ID already has transaction data, so this change cannot be "
                       "undone. Add another Station ID change instead.")
    run("DELETE FROM master WHERE key=?", (row["new_key"],))
    run("DELETE FROM station_changes WHERE id=?", (change_id,))
    clear_report_cache()
    audit("station.change_undo", f"{row['old_station']} -> {row['new_station']}")
    kv_sync.request_backup()
    return True, "Station ID change undone."


def apply_station_transfers(agg, missing, period_dates, transfers):
    """Show a transferred station under the Division / Sub Division it had on the report dates.

    master holds the current placement. If the whole period ends before a transfer's
    effective date, the station is moved back to its old Division / Sub Division. A
    period that contains or follows the effective date is reported under the new
    placement as a whole. Periods with an upload whose label is not a date are left
    unadjusted.
    """
    if transfers is None or len(transfers) == 0:
        return agg, missing
    if not period_dates or any(d is None for d in period_dates):
        return agg, missing
    end = max(period_dates)
    tr = transfers.copy()
    tr["_eff"] = pd.to_datetime(tr["effective_date"], errors="coerce").dt.date
    tr = tr[tr["_eff"].notna()].sort_values(["_eff", "id"])
    future = tr[tr["_eff"] > end]
    if future.empty:
        return agg, missing
    # The earliest transfer still in the future says where the station was before it.
    first = future.drop_duplicates("key", keep="first").set_index("key")
    agg, missing = agg.copy(), missing.copy()
    for df in (agg, missing):
        if df.empty:
            continue
        hit = df["key"].isin(first.index) & (df["division"] != "Not in master")
        if hit.any():
            df.loc[hit, "division"] = df.loc[hit, "key"].map(first["old_division"])
            df.loc[hit, "sub_division"] = df.loc[hit, "key"].map(first["old_sub_division"])
    return agg, missing


def reapply_station_transfers(m):
    """Keep transferred stations in their new Division / Sub Division after a master upload."""
    try:
        tr = read_sql("SELECT * FROM station_transfers ORDER BY id")
        if tr.empty or not {"key", "division", "sub_division"}.issubset(m.columns):
            return m, 0
        latest = tr.drop_duplicates("key", keep="last").set_index("key")
        m = m.copy()
        moved = 0
        for i in m.index[m["key"].astype(str).isin(latest.index)]:
            r = latest.loc[str(m.at[i, "key"])]
            if (m.at[i, "division"], m.at[i, "sub_division"]) != (r["new_division"], r["new_sub_division"]):
                m.at[i, "division"], m.at[i, "sub_division"] = r["new_division"], r["new_sub_division"]
                moved += 1
        return m, moved
    except Exception:
        return m, 0


def register_station_transfer(key, new_div, new_sub, effective, by):
    """Move one station to another Division / Sub Division. Returns (ok, message)."""
    if not new_div or not new_sub:
        return False, "Select the new Division and Sub Division."
    eff = effective.strftime("%Y-%m-%d")
    con = connect()
    try:
        old = con.execute("SELECT key, station, office_id, division, sub_division FROM master WHERE key=?",
                          (key,)).fetchone()
        if old is None:
            return False, "The selected station is no longer in the master."
        old = dict(old)
        if con.execute("SELECT 1 FROM master WHERE division=? AND sub_division=? LIMIT 1",
                       (new_div, new_sub)).fetchone() is None:
            return False, "This Sub Division does not exist under the selected Division in the master."
        if (old["division"], old["sub_division"]) == (new_div, new_sub):
            return False, "The station is already in this Division and Sub Division."
        last = con.execute("SELECT MAX(effective_date) FROM station_transfers WHERE key=?", (key,)).fetchone()[0]
        if last and eff < last:
            return False, (f"This station already has a transfer effective {last[8:10]}-{last[5:7]}-{last[:4]}. "
                           "The new effective date must be on or after it.")
        con.execute("UPDATE master SET division=?, sub_division=? WHERE key=?", (new_div, new_sub, key))
        con.execute(
            "INSERT INTO station_transfers(key, station, office_id, old_division, old_sub_division,"
            " new_division, new_sub_division, effective_date, changed_by, changed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (key, old["station"], old["office_id"], old["division"], old["sub_division"], new_div, new_sub,
             eff, by, datetime.now().strftime("%Y-%m-%d %H:%M")))
        con.commit()
    finally:
        con.close()
    clear_report_cache()
    audit("station.transfer", f"{old['station']} ({old['office_id']}): {old['division']}/{old['sub_division']}"
                              f" -> {new_div}/{new_sub} effective {effective.strftime('%d-%m-%Y')}")
    kv_sync.request_backup()
    return True, (f"Station {old['station']} moved to {new_div} / {new_sub}, effective "
                  f"{effective.strftime('%d-%m-%Y')}.")


def undo_station_transfer(transfer_id):
    """Reverse a transfer made by mistake. Only the latest transfer of a station can be undone."""
    row = run("SELECT * FROM station_transfers WHERE id=?", (transfer_id,), one=True)
    if row is None:
        return False, "That transfer no longer exists."
    if run("SELECT 1 FROM station_transfers WHERE key=? AND id>?", (row["key"], transfer_id), one=True):
        return False, "This station was transferred again later. Undo the latest transfer first."
    run("UPDATE master SET division=?, sub_division=? WHERE key=?",
        (row["old_division"], row["old_sub_division"], row["key"]))
    run("DELETE FROM station_transfers WHERE id=?", (transfer_id,))
    clear_report_cache()
    audit("station.transfer_undo", f"{row['station']}: back to {row['old_division']}/{row['old_sub_division']}")
    kv_sync.request_backup()
    return True, "Station transfer undone."


def station_transfer_form():
    st.caption("Use this to move a station from one Division to another, or to a different Sub Division. "
               "Choose the new Division first - the Sub Division list then shows only that Division's "
               "Sub Divisions. Reports for dates before the effective date keep showing the station under "
               "its old Division. Every transfer is recorded in the activity log.")
    master = read_sql("SELECT key, station, office_id, division, sub_division FROM master"
                      " ORDER BY division, sub_division, station")
    retired = set(read_sql("SELECT old_key FROM station_changes")["old_key"])
    master = master[~master["key"].isin(retired) & (master["division"] != "")]
    divisions = get_divisions()
    if master.empty or not divisions:
        st.info("No stations available in the master.")
        return

    c1, c2 = st.columns(2)
    from_div = c1.selectbox("From Division", sorted(master["division"].unique()), key="stt_from_div")
    pool = master[master["division"] == from_div]
    opts = {f"{r.station} | {r.office_id} | {r.sub_division or 'Not mapped'}": r.key
            for r in pool.itertuples()}
    pick = c2.selectbox("Station ID (| Office ID | Sub Division)", list(opts), key=f"stt_station_{from_div}")

    c3, c4 = st.columns(2)
    default_idx = next((i for i, d in enumerate(divisions) if d != from_div), 0)
    to_div = c3.selectbox("To Division", divisions, index=default_idx, key=f"stt_to_div_{from_div}")
    subs = get_subdivisions(to_div)
    to_sub = c4.selectbox("To Sub Division", subs if subs else ["(none found in master)"],
                          key=f"stt_to_sub_{from_div}_{to_div}")
    eff = st.date_input("Effective date (first day the station counts in the new Division)",
                        value=datetime.now().date(), format="DD-MM-YYYY", key="stt_eff")
    if st.button("Transfer station", key="stt_go"):
        if not subs:
            st.warning("No Sub Division was found in the master for this Division.")
        else:
            ok, msg = register_station_transfer(opts[pick], to_div, to_sub, eff,
                                                st.session_state["user"]["username"])
            if ok:
                flash(msg)
            else:
                st.error(msg)

    tr = read_sql("SELECT id, station, office_id, old_division, old_sub_division, new_division,"
                  " new_sub_division, effective_date, changed_by, changed_at FROM station_transfers"
                  " ORDER BY id DESC")
    if tr.empty:
        return
    show = tr.assign(effective_date=pd.to_datetime(tr["effective_date"], errors="coerce")
                     .dt.strftime("%d-%m-%Y")).rename(columns={
        "id": "ID", "station": "Station", "office_id": "Office ID", "old_division": "From Division",
        "old_sub_division": "From Sub Division", "new_division": "To Division",
        "new_sub_division": "To Sub Division", "effective_date": "Effective from",
        "changed_by": "Changed by", "changed_at": "Changed at"})
    st.dataframe(show, hide_index=True, use_container_width=True)
    latest_ids = set(read_sql("SELECT MAX(id) AS id FROM station_transfers GROUP BY key")["id"])
    undoable = {f"#{r.id}: {r.station} ({r.old_division}/{r.old_sub_division} -> "
                f"{r.new_division}/{r.new_sub_division})": r.id for r in tr.itertuples() if r.id in latest_ids}
    with st.expander("Undo a station transfer"):
        label = st.selectbox("Transfer to undo", list(undoable), key="stt_undo_pick")
        if st.button("Undo this transfer", key="stt_undo_btn"):
            ok, msg = undo_station_transfer(undoable[label])
            if ok:
                flash(msg)
            else:
                st.error(msg)


def station_change_section():
    st.subheader("Station ID change / transfer")
    if not table_exists("master"):
        st.info("Upload the master sheet first.")
        return
    mode = st.radio("Action", ["Retire / change Station ID (with date)",
                               "Transfer station to another Division / Sub Division"],
                    horizontal=True, key="sc_mode")
    if mode.startswith("Retire"):
        station_id_change_form()
    else:
        station_transfer_form()


def station_id_change_form():
    st.markdown("**Retire / change Station ID**")
    st.caption("Use this when a machine's station ID has changed. The old ID stays in the master so old "
               "reports keep working, but it is not counted from the effective date onward. The new ID is "
               "added to the master with the same office, division and sub division and is counted from "
               "the effective date. Every change is recorded in the activity log.")
    if not table_exists("master"):
        st.info("Upload the master sheet first.")
        return
    master = read_sql("SELECT key, station, office_id, division, sub_division FROM master"
                      " ORDER BY division, station")
    done = set(read_sql("SELECT old_key FROM station_changes")["old_key"])
    opts = {f"{r.station} | {r.office_id} | {r.division}": r.key
            for r in master.itertuples() if r.key not in done}
    if opts:
        with st.form("station_change_form", clear_on_submit=True):
            pick = st.selectbox("Old station ID", list(opts))
            new_id = st.text_input("New station ID")
            eff = st.date_input("Effective date (first day the new ID is used)",
                                value=datetime.now().date(), format="DD-MM-YYYY")
            if st.form_submit_button("Save station ID change"):
                ok, msg = register_station_change(opts[pick], new_id, eff,
                                                  st.session_state["user"]["username"])
                if ok:
                    flash(msg)
                else:
                    st.error(msg)
    ch = read_sql("SELECT id, old_station, new_station, division, sub_division, effective_date,"
                  " changed_by, changed_at FROM station_changes ORDER BY id DESC")
    if ch.empty:
        return
    show = ch.assign(effective_date=pd.to_datetime(ch["effective_date"], errors="coerce")
                     .dt.strftime("%d-%m-%Y")).rename(columns={
        "id": "ID", "old_station": "Old station ID", "new_station": "New station ID",
        "division": "Division", "sub_division": "Sub Division", "effective_date": "Effective from",
        "changed_by": "Changed by", "changed_at": "Changed at"})
    st.dataframe(show, hide_index=True, use_container_width=True)
    with st.expander("Undo a station ID change"):
        labels = {f"#{r.id}: {r.old_station} -> {r.new_station}": r.id for r in ch.itertuples()}
        pick_u = st.selectbox("Change to undo", list(labels), key="sc_undo_pick")
        if st.button("Undo this change", key="sc_undo_btn"):
            ok, msg = undo_station_change(labels[pick_u])
            if ok:
                flash(msg)
            else:
                st.error(msg)


# ---------------------------------------------------------------- camps
def get_divisions():
    if not table_exists("master"):
        return []
    d = read_sql("SELECT DISTINCT division FROM master WHERE division!='' ORDER BY division")
    return list(d["division"])


def get_subdivisions(division):
    if not table_exists("master"):
        return []
    d = read_sql("SELECT DISTINCT sub_division FROM master WHERE division=? AND sub_division!='' ORDER BY sub_division",
                 (division,))
    return list(d["sub_division"])


def save_camp(camp_date, division, sub_division, location, transactions, remarks, by):
    run("""INSERT INTO camps(camp_date, division, sub_division, location, transactions, remarks, created_by, created_at)
        VALUES (?,?,?,?,?,?,?,?)""",
        (camp_date, division, sub_division, location, transactions, remarks, by,
         datetime.now().strftime("%Y-%m-%d %H:%M")))
    audit("camp.create", f"{camp_date} {division}/{sub_division} {location} ({transactions})")
    kv_sync.request_backup()


# ---------------------------------------------------------------- screens
def setup_screen():
    st.title("First-time setup")
    st.write("Create the administrator account. Only the admin can upload data and create logins for other users.")
    with st.form("setup"):
        u = st.text_input("Admin username").strip().lower()
        p1 = st.text_input("Password", type="password")
        p2 = st.text_input("Confirm password", type="password")
        if st.form_submit_button("Create admin"):
            err = check_new_credentials(u, p1, p2)
            if err:
                st.error(err)
            else:
                add_user(u, p1, "admin")
                flash("Admin created. Please log in.")


LOGIN_CSS = """<style>
[data-testid="stHeader"],[data-testid="stToolbar"],footer{display:none}
.stApp{background:#F4EFE9}
[data-testid="stMainBlockContainer"],.block-container{max-width:460px!important;margin:6vh auto 24px;padding:0 0 34px!important;background:#fff;border-radius:20px;box-shadow:0 24px 60px rgba(74,16,23,.18);overflow:hidden}
.hp-band{background:linear-gradient(120deg,#7A1F2B,#4A1017);padding:34px 36px 46px;text-align:center;position:relative;margin-bottom:8px}
.hp-band:after{content:"";position:absolute;left:0;right:0;bottom:-1px;height:36px;background:#fff;border-radius:50% 50% 0 0/100% 100% 0 0}
.hp-plate{display:inline-block;background:#fff;border-radius:12px;padding:10px 16px;margin:0 auto 16px;box-shadow:0 8px 18px rgba(0,0,0,.22)}
.hp-plate img{display:block;width:150px;height:auto}
.hp-title{color:#fff;font-size:18px;font-family:Cambria,Georgia,serif;line-height:1.35}
[data-testid="stForm"]{border:none;padding:8px 36px 0}
[data-testid="stWidgetLabel"] p{font-size:11px;font-weight:700;color:#4A1017;text-transform:uppercase;letter-spacing:.6px}
.stTextInput input{border:2px solid #EADEDF;border-radius:9px;background:#FCF8F8;color:#222;padding:13px 14px}
.stTextInput input:focus{border-color:#962538;background:#fff}
[data-testid="stFormSubmitButton"] button{width:100%;background:#C97B1E;color:#fff;border:none;border-radius:9px;padding:14px;font-weight:700;box-shadow:0 6px 16px rgba(201,123,30,.3)}
[data-testid="stFormSubmitButton"] button:hover{background:#B26C19;color:#fff}
.hp-pills{display:flex;gap:8px;padding:22px 36px 0}
.hp-pill{flex:1;background:#FBF6F0;border:1px solid #F2E4D8;border-radius:10px;padding:12px 8px;text-align:center;color:#7A1F2B;font-size:9.5px;font-weight:700;text-transform:uppercase;letter-spacing:.3px}
.hp-note{padding:16px 36px 0;text-align:center;font-size:11.5px;color:#8A6B6E;line-height:1.7}
</style>"""


def login_screen():
    st.markdown(LOGIN_CSS, unsafe_allow_html=True)
    plate = ""
    uri = logo_data_uri()
    if uri:
        plate = f'<div class="hp-plate"><img src="{uri}" alt="India Post"></div><br>'
    st.markdown(f'<div class="hp-band">{plate}<div class="hp-title">Aadhaar MIS Dashboard<br>'
                'Department of Posts, India<br>Haryana Circle</div></div>', unsafe_allow_html=True)
    with st.form("login"):
        u = st.text_input("Username")
        p = st.text_input("Password", type="password")
        if st.form_submit_button("Login"):
            user, err = authenticate(u, p)
            if user:
                st.session_state["user"] = user
                st.session_state["last_seen"] = datetime.now()
                st.rerun()
            st.error(err)
    st.markdown('<div class="hp-pills"><div class="hp-pill">Live Dashboard</div>'
                '<div class="hp-pill">Division Reports</div><div class="hp-pill">Excel / CSV</div></div>'
                '<div class="hp-note">Username and password are provided by the Circle Office</div>',
                unsafe_allow_html=True)


def force_change_password_screen():
    st.title("Set a new password")
    st.info("This is your first login, or the admin has reset your password. Please set a new "
            "password before continuing.")
    with st.form("force_chpw"):
        n1 = st.text_input("New password", type="password")
        n2 = st.text_input("Confirm new password", type="password")
        if st.form_submit_button("Set password"):
            err = check_new_credentials("", n1, n2, need_username=False)
            if err:
                st.error(err)
            else:
                set_password(st.session_state["user"]["username"], n1)
                st.session_state["user"]["must_change"] = 0
                flash("Password set. You can now use the dashboard.")
    if st.button("Log out"):
        st.session_state.clear()
        st.rerun()


def sidebar(page_names):
    me = st.session_state["user"]
    if LOGO:
        st.sidebar.image(str(LOGO), width=170)
    st.sidebar.write(f"Logged in as **{me['username']}** ({me['role']})")
    st.sidebar.divider()
    page = st.sidebar.radio("Navigate", page_names, key="nav_page", label_visibility="collapsed")
    st.sidebar.divider()
    if st.sidebar.button("Log out"):
        st.session_state.clear()
        st.rerun()
    with st.sidebar.expander("Change my password"):
        with st.form("chpw", clear_on_submit=True):
            old = st.text_input("Current password", type="password")
            n1 = st.text_input("New password", type="password")
            n2 = st.text_input("Confirm new password", type="password")
            if st.form_submit_button("Change password"):
                u = run("SELECT * FROM users WHERE username=?", (me["username"],), one=True)
                if not hmac.compare_digest(hash_pw(old, u["salt"])[1], u["pw_hash"]):
                    st.error("Current password is wrong.")
                elif (err := check_new_credentials("", n1, n2, need_username=False)):
                    st.error(err)
                else:
                    set_password(me["username"], n1)
                    st.success("Password changed.")
    return page


def dashboard():
    uploads = run("SELECT * FROM uploads ORDER BY id DESC", many=True)
    if not table_exists("master") or not uploads:
        st.info("Data has not been uploaded yet. Please contact the admin.")
        return

    ids = select_period_ids(uploads, key_prefix="dash")
    if not ids:
        return

    agg, missing, m, ops = build_period_adj(ids)
    agg = agg.assign(machine_address=agg["key"].map(machine_address_map(tuple(ids))).fillna(""))
    # Operator name(s) per station ID. Falls back to the operator ID when the
    # operator master has no name for it.
    _name_by_op = {str(o).strip().upper(): n for o, n in zip(ops["operator"], ops["operator_name"]) if n}
    _ops_by_key = station_operator_map(tuple(ids))
    agg = agg.assign(operator_name=agg["key"].map(
        lambda k: ", ".join(_name_by_op.get(o.upper(), o) for o in _ops_by_key.get(k, []))))

    f1, f2, f3 = st.columns([1, 1, 2])
    dv = f1.selectbox("Division", ["All"] + sorted(set(agg["division"]) | set(m["division"])))
    pool = agg if dv == "All" else agg[agg["division"] == dv]
    ds = f2.selectbox("District", ["All"] + sorted(d for d in pool["district"].unique() if d))
    q = f3.text_input("Search station, office ID or address (office / machine)").strip().lower()

    v = pool if ds == "All" else pool[pool["district"] == ds]
    if q:
        blob = (v["station"] + " " + v["office_id"] + " " + v["address"] + " " + v["machine_address"]).str.lower()
        v = v[blob.str.contains(q, regex=False)]
    miss = missing if dv == "All" else missing[missing["division"] == dv]

    k = st.columns(5)
    k[0].metric("Total transactions", f"{int(v['total'].sum()):,}")
    k[1].metric("New enrolments", f"{int(v['enr'].sum()):,}")
    k[2].metric("Updates", f"{int(v['upd'].sum()):,}")
    k[3].metric("Zero-transaction stations", f"{int((v['total'] == 0).sum()):,}")
    k[4].metric("Master stations with no data", f"{len(miss):,}")

    show = v.rename(columns={"station": "Station", "office_id": "Office ID", "division": "Division",
                             "sub_division": "Sub Division", "district": "District",
                             "address": "Office address", "operator_name": "Operator Name",
                             "machines": "Machines",
                             "enr": "New enrolment", "mbu": "MBU", "demo": "Demographic updates",
                             "nonmbu": "Non-MBU", "upd": "Updates", "total": "Total"})
    cols = ["Station", "Office ID", "Operator Name", "Division", "Sub Division", "District", "Office address", "Machines",
            "New enrolment", "MBU", "Demographic updates", "Non-MBU", "Updates", "Total"]
    show = show[cols].sort_values("Total", ascending=False)

    # The target is Daily Target x the number of *working* days selected, not
    # the number of uploads. Divisional offices are closed on Sundays, so
    # counting a Sunday as a full day of opportunity understates achievement -
    # by 12 points for Hisar across 18-24 September 2026. Sunday transactions
    # that did happen (RMS and delivery branches) still count as achievement.
    label_by_id = {r["id"]: r["label"] for r in uploads}
    period_days, excluded, unknown_dates = split_working_days(
        [parse_label_date(label_by_id.get(i)) for i in ids])
    achievement = show.groupby("Division")["Total"].sum().reset_index()
    tgt, no_target = target_table(achievement, period_days, _target_lookup_cached())
    if len(tgt) and period_days > 0:
        if period_days <= 0:
            # Every selected day is a non-working day. A target of zero would
            # make the percentage meaningless, so say so rather than divide.
            st.subheader("Target vs Achievement")
            st.warning(
                "Every day in the selected period is a non-working day, so there is no "
                "target to measure against. Transactions from those days are still "
                "shown in the station details below. "
                + describe_excluded(excluded))
        else:
            st.subheader("Target vs Achievement")
            st.caption(
                f"Target = Daily Target x {period_days} working day(s) selected above "
                f"({len(ids)} upload(s) in total). "
                "Achievement = actual transactions in the same period, for the divisions "
                "currently in view."
                + ((" " + describe_excluded(excluded)) if excluded else "")
                + (f" {unknown_dates} upload label(s) could not be read as a date and "
                   "were counted as working days." if unknown_dates else ""))
            fig_t = _plotly().bar(tgt.melt("Division", value_vars=["Target", "Achievement"],
                                    var_name="Type", value_name="Count"),
                           x="Division", y="Count", color="Type", barmode="group", text="Count",
                           color_discrete_map={"Target": "#B0B0B0", "Achievement": "#7A1F2B"},
                           title="Target vs Achievement (transactions)")
            fig_t.update_traces(texttemplate="%{text:,.0f}", textposition="outside")
            st.plotly_chart(fig_t, use_container_width=True)

            tshow = tgt[["Division", "Target", "Achievement", "Shortfall / Surplus", "% Achieved"]] \
                .sort_values("Division")
            st.dataframe(tshow, hide_index=True, use_container_width=True)
            st.download_button("Download Target vs Achievement CSV",
                               tshow.to_csv(index=False).encode("utf-8-sig"),
                               "target_vs_achievement.csv", "text/csv", key="dl_target_vs_ach")
        if no_target:
            st.caption("No target configured for: " + ", ".join(no_target))
    else:
        st.info("No division in the current view has a configured daily target.")

    st.divider()
    d = show.groupby("Division")[["New enrolment", "Updates"]].sum().reset_index()
    fig_d = _plotly().bar(d.melt("Division", var_name="Type", value_name="Count"), x="Division", y="Count",
                   color="Type", barmode="stack", text="Count", title="Division-wise transactions")
    fig_d.update_traces(texttemplate="%{text:,.0f}", textposition="inside")
    st.plotly_chart(fig_d, use_container_width=True)

    if len(miss):
        with st.expander(f"{len(miss)} master stations have no transaction data"):
            st.dataframe(miss[["station", "office_id", "division"]].rename(
                columns={"station": "Station", "office_id": "Office ID", "division": "Division"}),
                hide_index=True, use_container_width=True)
    st.subheader("Station-wise details")
    st.dataframe(show, hide_index=True, use_container_width=True)
    st.download_button("Download CSV", show.to_csv(index=False).encode("utf-8-sig"),
                       "haryana_aadhaar_transactions.csv", "text/csv")


def operator_analysis_tab():
    uploads = run("SELECT * FROM uploads ORDER BY id DESC", many=True)
    if not table_exists("master") or not uploads:
        st.info("Data has not been uploaded yet. Please contact the admin.")
        return

    ids = select_period_ids(uploads, key_prefix="opan")
    if not ids:
        return

    _, _, _, ops = build_period(ids)
    if ops.empty:
        st.info("No operator data found for the selected period. Make sure the daily upload file has "
                "a Session Operator ID (or Operator ID) column.")
        return

    f1, f2 = st.columns([1, 2])
    dv = f1.selectbox("Division", ["All"] + sorted(ops["division"].unique()), key="opan_div")
    pool = ops if dv == "All" else ops[ops["division"] == dv]
    q = f2.text_input("Search operator ID or name", key="opan_q").strip().lower()
    if q:
        blob = pool["operator"].str.lower() + " " + pool["operator_name"].str.lower()
        pool = pool[blob.str.contains(q, regex=False)]
    if not table_exists("operator_master"):
        st.caption("Operator names are not available yet - the admin can upload an Operator master "
                   "from the Upload data tab.")
    else:
        n_unnamed = int((ops["operator_name"] == "").sum())
        if n_unnamed:
            st.caption(f"{n_unnamed} operator(s) in this period are not in the Operator master "
                       "(their name is shown blank).")

    n_ops = pool["operator"].nunique()
    k = st.columns(5)
    k[0].metric("Operators", f"{n_ops:,}")
    k[1].metric("Total transactions", f"{int(pool['total'].sum()):,}")
    k[2].metric("New enrolments", f"{int(pool['enr'].sum()):,}")
    k[3].metric("Updates", f"{int(pool['upd'].sum()):,}")
    k[4].metric("Avg. transactions / operator", f"{(pool['total'].sum() / max(n_ops, 1)):,.1f}")

    show = pool.rename(columns={"operator": "Operator ID", "operator_name": "Operator Name",
                                "division": "Division", "sub_division": "Sub Division",
                                "stations": "Stations Worked", "days_worked": "Days Worked",
                                "enr": "New Enrolment", "mbu": "MBU", "demo": "Demographic Updates",
                                "nonmbu": "Non-MBU", "upd": "Updates", "total": "Total"})
    cols = ["Operator ID", "Operator Name", "Division", "Sub Division", "Stations Worked", "Days Worked",
            "New Enrolment",
            "MBU", "Demographic Updates", "Non-MBU", "Updates", "Total"]
    show = show[cols].sort_values("Total", ascending=False)

    # Top 2 and bottom 2 operators of every division. A division with 4 or fewer
    # operators shows fewer bars: the top 2 are taken first, so nobody is drawn twice.
    parts = []
    for div_name, g in show.groupby("Division"):
        g = g.sort_values("Total", ascending=False)
        parts.append(g.head(2).assign(Rank="Top 2"))
        parts.append(g.iloc[2:].tail(2).assign(Rank="Bottom 2"))
    tb = pd.concat(parts, ignore_index=True) if parts else show.iloc[0:0].assign(Rank="")
    if not tb.empty:
        tb["Operator"] = tb.apply(
            lambda r: f"{r['Operator Name']} ({r['Operator ID']})" if r["Operator Name"] else r["Operator ID"], axis=1)
        n_div = tb["Division"].nunique()
        wrap = min(n_div, 4)
        fig = _plotly().bar(
            tb, x="Operator", y="Total", color="Rank", text="Total", facet_col="Division",
            facet_col_wrap=wrap, facet_col_spacing=0.04, facet_row_spacing=0.18,
            color_discrete_map={"Top 2": "#2e7d32", "Bottom 2": "#c62828"},
            category_orders={"Rank": ["Top 2", "Bottom 2"], "Operator": tb["Operator"].tolist(),
                             "Division": sorted(tb["Division"].unique())},
            title="Top 2 and bottom 2 operators of each division (by total transactions)",
            height=340 * ((n_div + wrap - 1) // wrap) + 80)
        fig.update_traces(texttemplate="%{text:,.0f}", textposition="outside", cliponaxis=False)
        fig.update_xaxes(matches=None, showticklabels=True, tickangle=-40, title_text="")
        fig.for_each_annotation(lambda a: a.update(text=a.text.split("=")[-1]))
        st.plotly_chart(fig, use_container_width=True)
        st.caption("Green = 2 highest, red = 2 lowest operators in that division for the selected period(s). "
                   "Divisions with 4 or fewer operators show fewer bars.")

    st.subheader("Operator-wise details")
    st.dataframe(show, hide_index=True, use_container_width=True)
    d1, d2 = st.columns(2)
    d1.download_button("Download CSV", show.to_csv(index=False).encode("utf-8-sig"),
                       "operator_analysis.csv", "text/csv", key="opan_dl_csv")
    with d2:
        excel_download(show, "Operator Analysis", "operator_analysis.xlsx", "Download Excel",
                       bold_last_row=False)


def upload_tab():
    if not is_admin():  # server-side guard
        st.error("Only the admin can upload data.")
        return
    st.subheader("Master sheet")
    st.caption("Master headers: station_number, Office Id, Sub Division Name, Divison (or Division). "
               "machine_address and Machine District are optional. A new upload replaces the existing master.")
    # Set when a parsed master is rejected for orphaning history; the operator
    # then re-submits with the acknowledgement checkbox.
    force_master = st.session_state.get("force_master_replace", False)
    with st.form("master_form", clear_on_submit=True):
        f = st.file_uploader("Master file", type=["xlsx", "xls", "csv"], key="master_uploader")
        if f is not None:
            st.caption(f"Selected file: **{f.name}** ({f.size / 1024:.1f} KB) - ready to save.")
        if st.session_state.get("master_orphan_warning"):
            st.warning(st.session_state["master_orphan_warning"])
            force_master = st.checkbox(
                "I understand: replace the master anyway and orphan the listed history",
                key="force_master_replace",
                value=False)
        if st.form_submit_button("Save master"):
            if not f:
                st.warning("No file detected. Please choose the file again, wait until its name "
                           "appears above (this confirms it finished uploading), then click "
                           "'Save master' again.")
            else:
                try:
                    mm = read_file(f, parse_master)
                    replace_master(mm, confirm_orphans=force_master)
                    st.session_state.pop("master_orphan_warning", None)
                    st.session_state["force_master_replace"] = False
                    flash(f"Master saved: {len(mm)} stations.")
                except ValueError as e:
                    # Distinguish "this file is unparseable" from "this file would
                    # destroy history"; only the latter can be acknowledged.
                    msg = str(e)
                    if "no master entry" in msg:
                        st.session_state["master_orphan_warning"] = msg
                        st.session_state["force_master_replace"] = False
                        st.rerun()
                    st.error(msg)
                except Exception as e:
                    st.error(str(e))

    station_change_section()

    st.subheader("Operator master")
    st.caption("Headers: Operator ID, Operator Name (other columns are ignored). The Operator ID must match the "
               "Session Operator ID in the daily transaction sheet. A new upload replaces the existing "
               "operator master, so upload the complete list each time.")
    if table_exists("operator_master"):
        n_om = run("SELECT COUNT(*) AS n FROM operator_master", one=True)["n"]
        st.caption(f"Currently saved: **{n_om}** operators.")
    with st.form("opmaster_form", clear_on_submit=True):
        fo = st.file_uploader("Operator master file", type=["xlsx", "xls", "csv"], key="opmaster_uploader")
        if fo is not None:
            st.caption(f"Selected file: **{fo.name}** ({fo.size / 1024:.1f} KB) - ready to save.")
        if st.form_submit_button("Save operator master"):
            if not fo:
                st.warning("No file detected. Please choose the file again, wait until its name "
                           "appears above (this confirms it finished uploading), then click "
                           "'Save operator master' again.")
            else:
                try:
                    om = read_file(fo, parse_operator_master)
                    save_operator_master(om)
                    flash(f"Operator master saved: {len(om)} operators.")
                except Exception as e:
                    st.error(str(e))

    st.subheader("Transaction sheet")
    st.caption("Daily sheet headers: station_number, machine_address, machine_district, Count_U_plus_N_plus_Z, "
               "Count_N, DEMO_UPDATE, NON_MBU, IS_MBU. Other columns are ignored. Upload one sheet per day, "
               "with the date as the label.")
    st.caption("Tip: if the file name itself contains the date (e.g. '22.09.2026.xlsx' or '22-09-2026.xlsx'), "
               "that date is picked up automatically and used as the label below - no need to type it. "
               "If no date is found in the file name, the label you type is used instead.")
    with st.form("tx_form", clear_on_submit=True):
        f = st.file_uploader("Transaction file", type=["xlsx", "xls", "csv"], key="tx_uploader")
        if f is not None:
            st.caption(f"Selected file: **{f.name}** ({f.size / 1024:.1f} KB) - ready to save.")
        label = st.text_input("Date / period label (used only if the file name has no date)",
                              value=datetime.now().strftime("%d-%m-%Y"))
        if st.form_submit_button("Save transactions"):
            if not f and not label.strip():
                st.warning("Enter a period label and choose a file.")
            elif not f:
                st.warning("No file detected. Please choose the file again, wait until its name "
                           "appears above (this confirms it finished uploading), then click "
                           "'Save transactions' again.")
            elif not label.strip():
                st.warning("Enter a period label.")
            else:
                auto_date = date_from_filename(f.name)
                final_label = auto_date or label.strip()
                if run("SELECT 1 FROM uploads WHERE label=?", (final_label,), one=True):
                    st.error(f"An upload with this label already exists ('{final_label}'). Delete it below "
                             "or rename the file / change the label.")
                else:
                    try:
                        tt = read_file(f, parse_tx)
                        save_tx(tt, final_label, st.session_state["user"]["username"])
                        note = " (date detected from file name)" if auto_date else ""
                        flash(f"Transactions saved for '{final_label}'{note}: {len(tt)} rows.")
                    except Exception as e:
                        st.error(str(e))

    ups = sort_uploads_by_date_desc(run("SELECT * FROM uploads ORDER BY id DESC", many=True))
    if ups:
        st.subheader("Existing uploads")
        st.dataframe(pd.DataFrame([dict(r) for r in ups]).rename(columns={
            "id": "ID", "label": "Period", "uploaded_by": "Uploaded by", "uploaded_at": "Uploaded at"}),
            hide_index=True, use_container_width=True)
        pick_id = st.selectbox("Delete an upload", [r["id"] for r in ups],
                               format_func=lambda i: next(r["label"] for r in ups if r["id"] == i))
        if st.button("Delete selected upload"):
            label = next((r["label"] for r in ups if r["id"] == pick_id), str(pick_id))
            n_rows = run("SELECT COUNT(*) n FROM tx WHERE upload_id=?", (pick_id,), one=True)["n"]
            run("DELETE FROM tx WHERE upload_id=?", (pick_id,))
            run("DELETE FROM uploads WHERE id=?", (pick_id,))
            audit("tx.delete", f"upload '{label}' (#{pick_id}) and {n_rows} transaction row(s)")
            clear_report_cache()
            kv_sync.request_backup()
            flash("Upload deleted.")


def users_tab():
    if not is_admin():  # server-side guard
        st.error("Only the admin can manage users.")
        return
    me = st.session_state["user"]["username"]

    st.subheader("Bulk create Division logins")
    if DEFAULT_TEMP_PASSWORD:
        st.caption("Creates one login per Division found in the master sheet. The username is generated "
                   "from the division's name, all of them get the configured default password, and "
                   "the user must set a new password on first login.")
    else:
        st.caption("Creates one login per Division found in the master sheet. The username is generated "
                   "from the division's name. No shared default password is configured, so a random "
                   "password is generated for the batch and shown once below. Every user must set a "
                   "new password on first login.")
    if not table_exists("master"):
        st.info("Please upload the master sheet from the Upload data tab first.")
    elif st.button("Create logins for all Divisions"):
        created, skipped, password = bulk_create_division_users()
        if created:
            st.success(f"{len(created)} login(s) created. Every user must set a new password on "
                       "first login.")
            st.dataframe(pd.DataFrame(created, columns=["Division", "Username"]),
                         hide_index=True, use_container_width=True)
            if not DEFAULT_TEMP_PASSWORD:
                # A random batch password exists nowhere else, so the admin has to
                # be shown it once or the new logins could never be used.
                show_password_popup("Temporary password for this batch", "", password,
                                    f"Give this to the {len(created)} new division login(s).")
        if skipped:
            st.info(f"{len(skipped)} division(s) already had a login, so they were skipped.")
            st.dataframe(pd.DataFrame(skipped, columns=["Division", "Username"]),
                         hide_index=True, use_container_width=True)
        if not created and not skipped:
            st.info("No Division found in the master sheet.")

    st.divider()
    st.subheader("Daily targets")
    st.caption("Competent Authority approved daily transaction target per division. "
               "These decide the Target column on the dashboard. Editing one takes "
               "effect immediately; it is recorded in the activity log below.")
    known = daily_targets()
    if table_exists("master"):
        for d in get_divisions():
            known.setdefault(d, parsers.DIVISION_DAILY_TARGETS.get(d))
    with st.form("targets_form"):
        st.caption(f"Configured: {len(parsers.DIVISION_DAILY_TARGETS)} built-in. "
                   "Leave blank for a division with no approved target.")
        cols = st.columns(4)
        new_targets = {}
        for i, division in enumerate(sorted(known)):
            with cols[i % 4]:
                current = known.get(division)
                new_targets[division] = st.number_input(
                    division, min_value=0, value=int(current) if current else 0,
                    step=10, key=f"tgt_{division}", format="%d")
        if st.form_submit_button("Save targets"):
            changed = []
            for division, value in new_targets.items():
                value = int(value)
                before = known.get(division)
                if before != value:
                    run("INSERT INTO division_targets(division, daily_target, updated_by, updated_at)"
                        " VALUES (?,?,?,?) ON CONFLICT(division) DO UPDATE SET"
                        " daily_target=excluded.daily_target, updated_by=excluded.updated_by,"
                        " updated_at=excluded.updated_at",
                        (division, value, st.session_state["user"]["username"],
                         datetime.now().strftime("%Y-%m-%d %H:%M")))
                    changed.append(f"{division}: {before if before is not None else 'none'} -> {value}")
            if changed:
                audit("targets.update", "; ".join(changed))
                kv_sync.request_backup()
                flash(f"Saved {len(changed)} target change(s).")
            else:
                st.info("No changes.")

    st.divider()
    st.subheader("Activity log")
    st.caption("Every upload, deletion, user change and rollback, with who did it. "
               "Kept permanently and backed up with the rest of the database.")
    limit = st.number_input("Show latest", min_value=20, max_value=1000, value=100, step=20,
                            key="audit_limit")
    logs = read_sql("SELECT at, username, action, detail FROM audit ORDER BY id DESC LIMIT ?",
                    (int(limit),))
    if logs.empty:
        st.info("Nothing recorded yet. Actions taken from now on will appear here.")
    else:
        show_log = logs.rename(columns={"at": "When", "username": "Who",
                                        "action": "Action", "detail": "Detail"})
        st.dataframe(show_log, hide_index=True, use_container_width=True)
        st.download_button("Download activity log CSV",
                           show_log.to_csv(index=False).encode("utf-8-sig"),
                           "aadhar_activity_log.csv", "text/csv", key="audit_dl")

    st.divider()
    st.subheader("Create user login")
    with st.form("new_user", clear_on_submit=True):
        u = st.text_input("Username").strip().lower()
        p = st.text_input("Temporary password", type="password")
        dv_new = st.selectbox("Division (for camp entry rights)", ["(none)"] + get_divisions())
        if st.form_submit_button("Create user"):
            err = check_new_credentials(u, p, p)
            if err:
                st.error(err)
            else:
                add_user(u, p, "user", must_change=1,
                         division=None if dv_new == "(none)" else dv_new)
                flash(f"User '{u}' created. Share the username and password with them - "
                     "they will need to set a new password on first login.")

    users = read_sql("SELECT username, role, division, active, created_at FROM users"
                     " ORDER BY role, username")
    st.dataframe(users.assign(active=users["active"].map({1: "Yes", 0: "No"}),
                              division=users["division"].fillna("")).rename(columns={
        "username": "Username", "role": "Role", "division": "Division", "active": "Active",
        "created_at": "Created"}),
        hide_index=True, use_container_width=True)

    others = [x for x in users["username"] if x != me]
    if not others:
        return
    target = st.selectbox("Select a user", others)
    rc1, rc2 = st.columns(2)
    with rc1:
        with st.form("reset_pw", clear_on_submit=True):
            n = st.text_input("New password for selected user", type="password")
            if st.form_submit_button("Reset password"):
                err = check_new_credentials("", n, n, need_username=False)
                if err:
                    st.error(err)
                else:
                    set_password(target, n, must_change=1)
                    st.success(f"Password reset for {target}. They will need to set a new password on first login.")
    with rc2:
        st.write("")
        st.write("")
        # Only offered when a shared default is actually configured; otherwise
        # there is nothing to reset to, and offering it would lock the user out.
        if DEFAULT_TEMP_PASSWORD:
            if st.button("Reset to default password"):
                set_password(target, DEFAULT_TEMP_PASSWORD, must_change=1)
                show_password_popup("Password reset", target, DEFAULT_TEMP_PASSWORD,
                                    f"Password for {target} has been reset to the default.")
    row = users[users["username"] == target].iloc[0]
    divs = get_divisions()
    cur_div = row["division"] if isinstance(row["division"], str) else ""
    opts_div = ["(none)"] + divs
    d1, d2 = st.columns([2, 1])
    new_div = d1.selectbox("Division of selected user (camp entry rights)", opts_div,
                           index=opts_div.index(cur_div) if cur_div in opts_div else 0,
                           key="assign_div")
    d2.write("")
    d2.write("")
    if d2.button("Save division"):
        run("UPDATE users SET division=? WHERE username=?",
            (None if new_div == "(none)" else new_div, target))
        audit("user.set_division", f"{target} -> {new_div}")
        kv_sync.request_backup()
        flash(f"Division updated for {target}.")
    a, b = st.columns(2)
    if a.button("Disable login" if row["active"] else "Enable login"):
        run("UPDATE users SET active=? WHERE username=?", (0 if row["active"] else 1, target))
        audit("user.disable" if row["active"] else "user.enable", target)
        kv_sync.request_backup()
        flash("User updated.")
    with b:
        sure = st.checkbox("Confirm delete")
        if st.button("Delete user") and sure:
            run("DELETE FROM users WHERE username=?", (target,))
            audit("user.delete", target)
            kv_sync.request_backup()
            flash(f"User '{target}' deleted.")


# Columns the report builders need. Selecting them explicitly rather than `*`
# keeps a schema change from silently widening every report query, and lets the
# covering index on (key, upload_id) do the work.
_STATION_TX_COLS = ["key", "station", "district", "address", "t_div", "t_sub", "upload_id",
                    "enr", "mbu", "demo", "nonmbu", "upd"]
_MASTER_COLS = ["key", "station", "office_id", "division", "sub_division", "address", "district"]


OPERATOR_EMPTY_COLS = ["operator", "operator_name", "division", "sub_division", "stations",
                       "days_worked", "enr", "mbu", "demo", "nonmbu", "upd", "total"]


@st.cache_data(ttl=300, show_spinner=False)
def build_period(ids):
    """Cached wrapper around parsers.aggregate_period.

    The aggregation itself lives in parsers.py so it can be tested without a
    Streamlit runtime. This only adds the cache, which is keyed on the selected
    upload ids; `clear_report_cache()` runs on the writes that change master or
    transaction data, and the TTL is a backstop rather than the mechanism.
    """
    con = connect()
    try:
        return parsers.aggregate_period(con, tuple(ids))
    finally:
        con.close()


@st.cache_data(ttl=300, show_spinner=False)
def machine_address_map(ids):
    """key -> machine address(es) seen in the daily sheets of the selected uploads.

    A station can have several machines at different addresses, so distinct
    addresses are joined with " | ". Returns {} if the tx table has no address.
    """
    try:
        marks = ",".join("?" * len(ids))
        df = read_sql(f"SELECT DISTINCT key, address FROM tx WHERE upload_id IN ({marks})"
                      " AND address IS NOT NULL AND TRIM(address) <> ''", tuple(ids))
        if df.empty:
            return {}
        df["address"] = df["address"].astype(str).str.strip()
        return df.groupby("key")["address"].agg(lambda s: " | ".join(sorted(set(s)))).to_dict()
    except Exception:
        return {}


@st.cache_data(ttl=300, show_spinner=False)
def station_operator_map(ids):
    """key -> list of operator IDs who worked that station in the selected uploads.

    The operator column in `tx` is looked up by name because it depends on how
    parsers.save_tx stored it. Returns {} if no such column exists.
    """
    try:
        cols = {r["name"] for r in run("PRAGMA table_info(tx)", many=True)}
        col = next((c for c in ("operator", "operator_id", "session_operator_id") if c in cols), None)
        if not col:
            return {}
        marks = ",".join("?" * len(ids))
        df = read_sql(f"SELECT DISTINCT key, {col} AS op FROM tx WHERE upload_id IN ({marks})"
                      f" AND {col} IS NOT NULL AND TRIM({col}) <> ''", tuple(ids))
        if df.empty:
            return {}
        df["op"] = df["op"].astype(str).str.strip()
        return df.groupby("key")["op"].agg(lambda x: sorted(set(x))).to_dict()
    except Exception:
        return {}


REPORT_COLS = ["Division Name", "Sub Division Name", "Total Transactions", "New Enrollments", "MBU",
               "Demographic Updates", "Non-MBU Biometric Updates"]

STATION_COLS = ["Total Station IDs", "Working Stations", "Not Working Stations"]


def station_status_counts(agg, missing, by):
    """Station-ID counts per group (`by` is a list of column names).

    Total Station IDs = stations that reported in the period + master stations
    with no data at all. Working = stations with at least one transaction in the
    period. Not Working = everything else (zero-transaction stations and master
    stations that never appeared in the uploaded files).
    """
    a = agg.assign(_w=(agg["total"] > 0).astype(int))
    a = a.groupby(by).agg(_seen=("key", "size"), _work=("_w", "sum"))
    m = missing.copy()
    if "sub_division" in by:
        m["sub_division"] = m["sub_division"].fillna("Not mapped").replace("", "Not mapped")
    if m.empty:
        c = a.assign(_miss=0)
    else:
        c = a.join(m.groupby(by).size().rename("_miss"), how="outer")
    c = c.fillna(0).astype(int).reset_index()
    c["Total Station IDs"] = c["_seen"] + c["_miss"]
    c["Working Stations"] = c["_work"]
    c["Not Working Stations"] = c["Total Station IDs"] - c["Working Stations"]
    return c[by + STATION_COLS]


def report_tab():
    uploads = run("SELECT * FROM uploads ORDER BY id DESC", many=True)
    if not table_exists("master") or not uploads:
        st.info("Data has not been uploaded yet. Please contact the admin.")
        return

    ids = select_period_ids(uploads, key_prefix="rep")
    if not ids:
        return

    # One call gives both the station and operator aggregates from a single
    # read of master and tx.
    agg_full, missing, _, _ = build_period_adj(ids)
    not_in_master = agg_full[agg_full["division"] == "Not in master"]
    st.caption(f"Stations in daily data: {len(agg_full)} | not found in master: "
               f"{len(not_in_master)} | master stations with no data: {len(missing)}")

    if len(not_in_master) or len(missing):
        e1, e2 = st.columns(2)
        with e1:
            if len(not_in_master):
                with st.expander(f"{len(not_in_master)} station(s) in daily data - not found in master"):
                    st.caption("These stations reported transactions but their Station/Office ID does not "
                               "match anything in the master sheet. Add them to the master sheet (or fix "
                               "the ID in the daily file) so they get correctly mapped to a Division.")
                    nim = not_in_master[["key", "station", "t_div", "t_sub", "address", "total"]].rename(columns={
                        "key": "Station Key", "station": "Station", "t_div": "Division (as in daily file)",
                        "t_sub": "Sub Division (as in daily file)", "address": "Address", "total": "Total"})
                    st.dataframe(nim.sort_values("Total", ascending=False), hide_index=True,
                                use_container_width=True)
                    st.download_button("Download CSV", nim.to_csv(index=False).encode("utf-8-sig"),
                                       "stations_not_in_master.csv", "text/csv", key="dl_not_in_master")
        with e2:
            if len(missing):
                with st.expander(f"{len(missing)} master station(s) with no data in this period"):
                    st.caption("These stations are listed in the master sheet but did not report any "
                               "transactions for the selected period(s) - check if they uploaded / "
                               "were operational.")
                    ms = missing[["key", "station", "office_id", "division", "sub_division"]].rename(columns={
                        "key": "Station Key", "station": "Station", "office_id": "Office ID",
                        "division": "Division", "sub_division": "Sub Division"})
                    st.dataframe(ms.sort_values(["Division", "Station"]), hide_index=True,
                                use_container_width=True)
                    st.download_button("Download CSV", ms.to_csv(index=False).encode("utf-8-sig"),
                                       "master_stations_no_data.csv", "text/csv", key="dl_missing_master")
        st.divider()

    dv = st.selectbox("Division", ["All"] + sorted(agg_full["division"].unique()), key="rep_div")
    agg = agg_full if dv == "All" else agg_full[agg_full["division"] == dv]

    miss_sel = missing if dv == "All" else missing[missing["division"] == dv]
    by2 = ["division", "sub_division"]
    tx_cols = ["total", "enr", "mbu", "demo", "nonmbu"]
    rep = (station_status_counts(agg, miss_sel, by2)
           .merge(agg.groupby(by2, as_index=False)[tx_cols].sum(), on=by2, how="left")
           .fillna(0).sort_values(by2))
    rep_cols = REPORT_COLS[:2] + STATION_COLS + REPORT_COLS[2:]
    rep.columns = rep_cols
    num_cols_rep = rep_cols[2:]
    rep[num_cols_rep] = rep[num_cols_rep].round().astype(int)
    grand = pd.DataFrame([["Grand Total", ""] + [int(rep[c].sum()) for c in num_cols_rep]], columns=rep_cols)
    out = pd.concat([rep, grand], ignore_index=True)

    if (agg["sub_division"] == "Not mapped").any():
        st.warning("Some stations have no Sub Division. Add a 'Sub Division Name' column to the master sheet "
                   "and upload the master again.")
    st.caption("Total Transactions (Count_U_plus_N_plus_Z) = New Enrollments + MBU + Demographic Updates + "
               "Non-MBU Biometric Updates. When several days are selected, their figures are added together.")
    st.caption("Working Stations = station IDs with at least one transaction in the selected period(s). "
               "Not Working Stations = station IDs with no transactions (including master stations that "
               "never appeared in the uploaded data). Total Station IDs = Working + Not Working.")
    st.dataframe(out, hide_index=True, use_container_width=True)

    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        out.to_excel(w, index=False, sheet_name="Report")
        ws = w.sheets["Report"]
        for i, c in enumerate(out.columns, 1):
            ws.column_dimensions[_excel_helpers()[1](i)].width = max(len(c), int(out[c].astype(str).str.len().max())) + 3
        for cell in ws[1] + ws[ws.max_row]:
            cell.font = _excel_helpers()[0](bold=True)
    a, b = st.columns(2)
    a.download_button("Download Excel", buf.getvalue(), "haryana_circle_report.xlsx",
                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    b.download_button("Download CSV", out.to_csv(index=False).encode("utf-8-sig"), "haryana_circle_report.csv",
                      "text/csv")

    st.divider()
    st.subheader("Division-wise Consolidated Report")
    st.caption("All divisions, summed to a single row each - independent of the Division filter above. "
               "Station counts show how many station IDs each division has, how many are working "
               "(at least one transaction in the selected period) and how many are not.")
    cons = (station_status_counts(agg_full, missing, ["division"])
            .merge(agg_full.groupby("division", as_index=False)[tx_cols].sum(), on="division", how="left")
            .fillna(0).sort_values("division"))
    cons.columns = ["Division Name"] + STATION_COLS + ["Total Transactions", "New Enrollments", "MBU",
                                                       "Demographic Updates", "Non-MBU Biometric Updates"]
    cons[cons.columns[1:]] = cons[cons.columns[1:]].round().astype(int)
    grand2 = pd.DataFrame([["Grand Total"] + [int(cons[c].sum()) for c in cons.columns[1:]]], columns=cons.columns)
    out2 = pd.concat([cons, grand2], ignore_index=True)
    st.dataframe(out2, hide_index=True, use_container_width=True)

    buf2 = BytesIO()
    with pd.ExcelWriter(buf2, engine="openpyxl") as w:
        out2.to_excel(w, index=False, sheet_name="Division Consolidated")
        ws2 = w.sheets["Division Consolidated"]
        for i, c in enumerate(out2.columns, 1):
            ws2.column_dimensions[_excel_helpers()[1](i)].width = max(len(c), int(out2[c].astype(str).str.len().max())) + 3
        for cell in ws2[1] + ws2[ws2.max_row]:
            cell.font = _excel_helpers()[0](bold=True)
    c1, c2 = st.columns(2)
    c1.download_button("Download Division-wise Excel", buf2.getvalue(), "haryana_circle_division_consolidated.xlsx",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    c2.download_button("Download Division-wise CSV", out2.to_csv(index=False).encode("utf-8-sig"),
                       "haryana_circle_division_consolidated.csv", "text/csv")

    st.divider()
    st.subheader("Not Working Stations - Division-wise List")
    st.caption("Station IDs with no transactions in the selected period(s). 'No data in period' means the "
               "station is in the master sheet but never appeared in the uploaded files; 'No transactions in "
               "period' means it appeared but with zero transactions. Independent of the Division filter above.")
    nw_cols = ["division", "sub_division", "station", "office_id"]
    nw = pd.concat([
        agg_full[agg_full["total"] <= 0][nw_cols].assign(Status="No transactions in period"),
        missing[nw_cols].assign(Status="No data in period"),
    ], ignore_index=True)
    nw["sub_division"] = nw["sub_division"].fillna("Not mapped").replace("", "Not mapped")
    nw = nw.rename(columns={"division": "Division", "sub_division": "Sub Division",
                            "station": "Station", "office_id": "Office ID"})
    nw = nw.sort_values(["Division", "Sub Division", "Station"])
    if nw.empty:
        st.success("All station IDs have reported transactions in the selected period.")
    else:
        totals = station_status_counts(agg_full, missing, ["division"]).set_index("division")["Total Station IDs"]
        for div_name, g in nw.groupby("Division"):
            total_ids = int(totals.get(div_name, len(g)))
            with st.expander(f"{div_name} - {len(g)} not working out of {total_ids} station IDs"):
                st.dataframe(g[["Sub Division", "Station", "Office ID", "Status"]],
                             hide_index=True, use_container_width=True)
        st.download_button("Download Not Working Stations CSV", nw.to_csv(index=False).encode("utf-8-sig"),
                           "haryana_circle_not_working_stations.csv", "text/csv", key="dl_not_working")


def camp_entry_form():
    st.subheader("Add camp entry")
    all_divisions = get_divisions()
    if not all_divisions:
        st.info("The master sheet has not been uploaded yet. The admin must upload the master sheet "
                "before camp entries can be added.")
        return

    if is_admin():
        divisions = all_divisions
    else:
        # Non-admins may only file camps for the division their login is linked
        # to. Narrowing the list is convenience only; the save path re-checks.
        my_div = user_division()
        if not my_div:
            st.warning("Your login is not linked to any Division, so you cannot add camp entries. "
                       "Please contact the admin.")
            return
        divisions = [d for d in all_divisions if parsers.norm(d) == parsers.norm(my_div)]
        if not divisions:
            st.warning(f"Your Division ('{my_div}') was not found in the master sheet. "
                       "Please contact the admin.")
            return

    c1, c2, c3 = st.columns(3)
    dv = c1.selectbox("Division", divisions, key="camp_dv",
                      disabled=len(divisions) == 1 and not is_admin())
    subs = get_subdivisions(dv)
    sd = c2.selectbox("Sub Division", subs if subs else ["(none found in master)"], key="camp_sd")
    dt = c3.date_input("Camp date", value=datetime.now().date(), format="DD-MM-YYYY", key="camp_dt")
    c4, c5 = st.columns([2, 1])
    loc = c4.text_input("Camp location", key="camp_loc")
    txn = c5.number_input("Number of transactions", min_value=0, step=1, key="camp_txn")
    remarks = st.text_input("Remarks (optional)", key="camp_remarks")
    if st.button("Save camp entry"):
        # Server-side guard: the selectbox is not the authority. A non-admin may
        # only save for the Division linked to their own login, whatever the
        # widget state or a crafted request says.
        if not is_admin() and parsers.norm(dv) != parsers.norm(user_division() or ""):
            st.error("You can only add camp entries for your own Division.")
        elif not loc.strip():
            st.warning("Please enter the camp location.")
        elif not subs or sd not in subs:
            st.warning("No valid Sub Division was found in the master sheet for this division.")
        else:
            save_camp(dt.strftime("%d-%m-%Y"), dv, sd, loc.strip(), int(txn), remarks.strip(),
                      st.session_state["user"]["username"])
            flash(f"Camp entry saved: {loc.strip()} ({dt.strftime('%d-%m-%Y')}).")


def excel_download(df, sheet_name, filename, label, bold_last_row=True):
    """Build a formatted .xlsx (auto column width, bold header + optional bold last row)
    and render a Streamlit download button for it."""
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name=sheet_name)
        ws = w.sheets[sheet_name]
        for i, c in enumerate(df.columns, 1):
            ws.column_dimensions[_excel_helpers()[1](i)].width = max(len(c), int(df[c].astype(str).str.len().max())) + 3
        rows = ws[1] + ws[ws.max_row] if bold_last_row else ws[1]
        for cell in rows:
            cell.font = _excel_helpers()[0](bold=True)
    st.download_button(label, buf.getvalue(), filename,
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@st.cache_data(ttl=30, show_spinner=False)
def _backup_status_cached():
    return kv_sync.get_status()


def _age_text(seconds):
    if seconds is None:
        return "unknown"
    if seconds < 60:
        return f"{seconds} second(s) ago"
    if seconds < 3600:
        return f"{seconds // 60} minute(s) ago"
    if seconds < 86400:
        return f"{seconds // 3600} hour(s) ago"
    return f"{seconds // 86400} day(s) ago"


def _since_text(iso_ts):
    return f" since {iso_ts}" if iso_ts else ""


def _rollback_controls(gens):
    """Restore the live database from an earlier stored generation.

    This overwrites the database the app is currently serving, so it is behind a
    typed confirmation rather than a single click. The current file is kept as
    `aadhaar.db.pre-rollback` before anything is replaced, so a rollback taken in
    error is itself recoverable.
    """
    st.divider()
    st.markdown("**Roll back to an earlier snapshot**")
    st.caption("Replaces the database this instance is serving with an older stored "
               "snapshot. Everything uploaded since that snapshot will be gone from "
               "this instance. The current file is kept as `aadhaar.db.pre-rollback`, "
               "and the upload that caused the problem is usually re-uploadable.")
    older = [g for g in gens if not g.get("isLatest")]
    if not older:
        st.info("There is only one snapshot, so there is nothing to roll back to.")
        return
    options = {f"{g['generation']}  ({_age_text(g['ageSeconds'])})": g["generation"] for g in older}
    choice = st.selectbox("Snapshot to restore", list(options), key="bk_rollback_pick")
    typed = st.text_input("Type RESTORE to enable the button", key="bk_rollback_confirm")
    if st.button("Restore this snapshot", key="bk_rollback_now",
                 disabled=typed.strip().upper() != "RESTORE"):
        gen = options[choice]
        with st.spinner(f"Restoring {gen}..."):
            result = kv_sync.restore_generation(gen)
        if result.get("restored"):
            audit("backup.rollback", f"generation {gen} ({result['bytes']:,} bytes)")
            clear_report_cache()
            st.success(f"Restored {result['bytes']:,} bytes from generation {gen}.")
            st.rerun()
        else:
            st.error(f"Rollback failed: {result.get('reason')}")


def backup_tab():
    """Admin view of whether the off-site backup is actually working.

    This exists because the old bridge failed silently: every restore and every
    push returned 404, the only trace was one line in the boot log, and the app
    looked perfectly healthy while holding no backups at all.
    """
    if not is_admin():  # server-side guard
        st.error("Only the admin can view backup status.")
        return

    c1, c2 = st.columns([3, 1])
    if c2.button("Refresh", key="bk_refresh", use_container_width=True):
        _backup_status_cached.clear()

    s = _backup_status_cached()
    if not s["enabled"]:
        st.info("The backup bridge is not configured on this host, so `aadhaar.db` is only "
                "as durable as the disk it sits on. This is expected in local development. "
                "On Render the database lives on an ephemeral filesystem and is destroyed on "
                "every deploy, so AADHAR_SYNC_URL and AADHAR_SYNC_TOKEN must both be set.")
        return

    healthy = s["healthy"]
    if healthy is True:
        st.success("Backups are working.")
    else:
        st.error("Backups are NOT working. The database on this host is not backed up anywhere.")

    if not s["reachable"]:
        st.error(f"The bridge could not be reached: {s['bridge_error']}")

    m = st.columns(4)
    m[0].metric("Newest snapshot", _age_text(s["newest_snapshot_age_seconds"]))
    m[1].metric("Last push from this instance", _age_text(s["last_backup_age_seconds"]))
    m[2].metric("Snapshot size", f"{s['db_bytes'] / 1024 / 1024:.2f} MB" if s["db_bytes"] else "unknown")
    m[3].metric("Consecutive failures", s["consecutive_failures"])

    b = s.get("bridge") or {}
    if b:
        st.caption(f"Bridge: `{s['url']}` | snapshot age {_age_text(b.get('ageSeconds'))} | "
                   f"pushes today {b.get('pushesToday')}/{b.get('dailyPushBudget')} "
                   f"({b.get('pushesLeft')} left) | {b.get('trackedGenerations')} generation(s) kept, "
                   f"oldest trimmed past {b.get('retain')}")
    st.caption(f"Automatic push every {s['interval_minutes']} minute(s), and never sooner than "
               f"{s['min_interval_minutes']} minute(s) after the last one. Writes that land inside that "
               "window are pushed by the final push on shutdown.")

    if s["last_error"]:
        st.error(f"Last error: {s['last_error']}")
    if s["skipped_budget"]:
        st.warning("The bridge's daily push budget is exhausted, so pushes are paused until it resets. "
                   "Restores still work. Raise DAILY_PUSH_BUDGET on the Worker, or lower "
                   "AADHAR_SYNC_INTERVAL_MS so fewer pushes are needed.")
    if s["skipped_size"]:
        st.warning(f"The database is larger than AADHAR_SYNC_MAX_BYTES ({s['max_bytes']} bytes), so it is "
                   "not being uploaded. Attach a persistent disk and stop using the bridge.")
    if s["skipped_min_interval"]:
        st.info("The most recent push was held back by the minimum interval, not lost. It goes out on the "
                "next scheduled push or on shutdown.")
    if s["pending_changes"]:
        st.info(f"Changes are waiting to be pushed{_since_text(s['pending_since'])}.")
    if s["restored"] and s["restored_from"]:
        st.caption(f"This instance booted by restoring generation {s['restored_from']} from the bridge.")

    a, b2 = st.columns(2)
    if a.button("Back up now", type="primary", use_container_width=True, key="bk_backup_now"):
        with st.spinner("Pushing a snapshot to the bridge..."):
            result = kv_sync.backup_data(force=True, reason="admin")
        _backup_status_cached.clear()
        if result.get("backed_up"):
            st.success(f"Pushed {result['bytes']:,} bytes as generation {result.get('generation')}.")
        else:
            st.error(f"Backup failed: {result.get('error') or result.get('reason')}")

    with b2.expander("Kept snapshots", expanded=False):
        gens = kv_sync.generations()
        if gens and "error" in gens[0]:
            st.error(gens[0]["error"])
        elif gens:
            st.dataframe(pd.DataFrame([{
                "Generation": g["generation"],
                "Pushed": g["at"],
                "Age": _age_text(g["ageSeconds"]),
                "Latest": "yes" if g["isLatest"] else "",
            } for g in gens]), hide_index=True, use_container_width=True)
            _rollback_controls(gens)
        else:
            st.info("The bridge holds no snapshots yet.")

    st.caption("The same figures are available from a shell: `python kv_sync.py status` "
               "(or `generations`).")


def camps_tab():
    camp_entry_form()
    st.divider()
    st.subheader("Camp reports")
    if not table_exists("camps") or not run("SELECT 1 FROM camps", one=True):
        st.info("No camp entry has been made yet.")
        return
    camps = read_sql("SELECT * FROM camps ORDER BY id DESC")
    camps["_d"] = pd.to_datetime(camps["camp_date"], format="%d-%m-%Y", errors="coerce")

    f1, f2, f3 = st.columns([1, 1, 2])
    dv = f1.selectbox("Division", ["All"] + sorted(camps["division"].unique()), key="camp_rep_dv")
    pool = camps if dv == "All" else camps[camps["division"] == dv]
    valid = pool["_d"].dropna()
    if len(valid):
        lo, hi = valid.min().date(), valid.max().date()
        rng = f2.date_input("Date range", value=(lo, hi), min_value=lo, max_value=hi,
                            format="DD-MM-YYYY", key="camp_rep_rng")
        if isinstance(rng, tuple) and len(rng) == 2:
            start, end = rng
            pool = pool[(pool["_d"].dt.date >= start) & (pool["_d"].dt.date <= end)]
        else:
            st.info("Select the end date to complete the range.")
    q = f3.text_input("Search location / sub division", key="camp_rep_q").strip().lower()
    if q:
        blob = (pool["location"] + " " + pool["sub_division"]).str.lower()
        pool = pool[blob.str.contains(q, regex=False)]

    k1, k2, k3 = st.columns(3)
    k1.metric("Total camps", f"{len(pool):,}")
    k2.metric("Total transactions", f"{int(pool['transactions'].sum()):,}")
    k3.metric("Divisions covered", f"{pool['division'].nunique():,}")

    show = pool[["camp_date", "division", "sub_division", "location", "transactions", "remarks",
                "created_by"]].rename(columns={
        "camp_date": "Date", "division": "Division", "sub_division": "Sub Division",
        "location": "Camp Location", "transactions": "Transactions", "remarks": "Remarks",
        "created_by": "Entered by"})
    st.dataframe(show, hide_index=True, use_container_width=True)
    d1, d2 = st.columns(2)
    d1.download_button("Download CSV", show.to_csv(index=False).encode("utf-8-sig"),
                       "aadhaar_camp_report.csv", "text/csv", key="camp_dl_csv")
    with d2:
        excel_download(show, "Camps", "aadhaar_camp_report.xlsx", "Download Excel",
                       bold_last_row=False)

    st.divider()
    st.subheader("Division-wise camp summary")
    summ = (pool.groupby(["division", "sub_division"], as_index=False)
            .agg(camps=("id", "size"), transactions=("transactions", "sum"))
            .sort_values(["division", "sub_division"]))
    summ.columns = ["Division", "Sub Division", "No. of Camps", "Total Transactions"]
    grand = pd.DataFrame([["Grand Total", "", summ["No. of Camps"].sum(), summ["Total Transactions"].sum()]],
                         columns=summ.columns)
    summ_out = pd.concat([summ, grand], ignore_index=True)
    st.dataframe(summ_out, hide_index=True, use_container_width=True)
    e1, e2 = st.columns(2)
    e1.download_button("Download CSV", summ_out.to_csv(index=False).encode("utf-8-sig"),
                       "aadhaar_camp_division_summary.csv", "text/csv", key="camp_summ_dl_csv")
    with e2:
        excel_download(summ_out, "Camp Summary", "aadhaar_camp_division_summary.xlsx", "Download Excel")

    if is_admin() and len(pool):
        with st.expander("Delete a camp entry"):
            opts = {f"{r['camp_date']} - {r['location']} ({r['division']}/{r['sub_division']}) #{r['id']}": r["id"]
                    for _, r in pool.iterrows()}
            pick = st.selectbox("Select entry", list(opts), key="camp_del_pick")
            if st.button("Delete this entry"):
                run("DELETE FROM camps WHERE id=?", (opts[pick],))
                audit("camp.delete", opts[pick])
                kv_sync.request_backup()
                flash("Camp entry deleted.")


# ---------------------------------------------------------------- main
init_db()
if "flash" in st.session_state:
    st.success(st.session_state.pop("flash"))

if not run("SELECT 1 FROM users WHERE role='admin'", one=True):
    setup_screen()
    st.stop()

# Idle timeout. Streamlit reruns the whole script on every interaction, so this
# is a reliable activity signal: an unattended browser stops touching widgets and
# gets logged out. The point is that these are shared logins handed to divisions,
# so a station left open stays usable by whoever walks up to it.
_timed_out = False
if "user" in st.session_state:
    last_seen = st.session_state.get("last_seen")
    if SESSION_TIMEOUT_MINUTES and last_seen and \
            (datetime.now() - last_seen).total_seconds() > SESSION_TIMEOUT_MINUTES * 60:
        st.session_state.clear()
        _timed_out = True

if "user" not in st.session_state:
    if _timed_out:
        st.info(f"You were signed out after {SESSION_TIMEOUT_MINUTES} minutes of inactivity. "
                "Please log in again.")
    login_screen()
    st.stop()
st.session_state["last_seen"] = datetime.now()
if st.session_state["user"].get("must_change"):
    force_change_password_screen()
    st.stop()

PAGES = {"Dashboard": dashboard, "Operator Analysis": operator_analysis_tab, "Report": report_tab,
        "Camps": camps_tab}
if is_admin():
    PAGES["Upload data"] = upload_tab
    PAGES["Manage users"] = users_tab
    PAGES["Backup status"] = backup_tab

page = sidebar(list(PAGES))
st.title(APP_NAME)
PAGES[page]()
