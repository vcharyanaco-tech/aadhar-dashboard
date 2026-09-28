"""Aadhaar Transaction Monitor - Haryana Circle.

Admin: uploads master + transaction sheets, creates/resets/disables user logins.
Users: log in and view the dashboard only.
Run:  streamlit run app.py
"""
import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
from io import BytesIO
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

import kv_sync

DATA_DIR = Path(os.environ.get("APP_DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB = DATA_DIR / "aadhaar.db"
PBKDF2_ROUNDS = 200_000
MAX_FAILS, LOCK_MINUTES = 5, 5
DEFAULT_TEMP_PASSWORD = "***REMOVED***"
APP_NAME = "HARYANA CIRCLE  AADHAR MONITORING DASHBOARD"
# Put the India Post logo in the same folder as app.py and name it logo.png (or logo.jpg)
LOGO = next((p for n in ("logo.png", "logo.jpg", "logo.jpeg", "logo.webp")
             if (p := Path(__file__).parent / n).exists()), None)

st.set_page_config(page_title=APP_NAME, layout="wide")


# ---------------------------------------------------------------- database
def run(sql, args=(), one=False, many=False):
    con = sqlite3.connect(DB)
    try:
        con.row_factory = sqlite3.Row
        cur = con.execute(sql, args)
        out = cur.fetchone() if one else cur.fetchall() if many else None
        con.commit()
        return out
    finally:
        con.close()


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
    con = sqlite3.connect(DB)
    try:
        ensure_cols(con, "users", {"must_change": "INTEGER DEFAULT 0"})
        con.commit()
    finally:
        con.close()


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


def add_user(username, password, role="user", must_change=0):
    salt, h = hash_pw(password)
    run("INSERT INTO users(username, salt, pw_hash, role, created_at, must_change) VALUES (?,?,?,?,?,?)",
        (username, salt, h, role, datetime.now().strftime("%Y-%m-%d %H:%M"), must_change))
    kv_sync.request_backup()


def set_password(username, password, must_change=0):
    salt, h = hash_pw(password)
    run("UPDATE users SET salt=?, pw_hash=?, fails=0, locked_until=NULL, must_change=? WHERE username=?",
        (salt, h, must_change, username))
    kv_sync.request_backup()


def slugify_username(name):
    s = re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")
    while len(s) < 3:
        s += "x"
    return s[:30]


def bulk_create_division_users(default_password="***REMOVED***"):
    """Create one login per Division found in the master sheet.
    Username = the division's name (slugified)."""
    if not table_exists("master"):
        return [], [], []
    divs = read_sql("SELECT DISTINCT division FROM master WHERE division!='' ORDER BY division")
    existing = {r["username"] for r in run("SELECT username FROM users", many=True)}
    created, renamed, skipped = [], [], []
    for division in divs["division"]:
        uname = slugify_username(division)
        if uname in existing:
            skipped.append((division, uname))
            continue
        add_user(uname, default_password, "user", must_change=1)
        existing.add(uname)
        created.append((division, uname))
    return created, renamed, skipped


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


def flash(msg):
    st.session_state["flash"] = msg
    st.rerun()


# ---------------------------------------------------------------- file parsing
def norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def norm_key(v):
    t = re.sub(r"\.0+$", "", str(v).strip().lower())
    return t.lstrip("0") or ("0" if t else "")


# Daily transaction target per division (Competent Authority approved targets).
DIVISION_DAILY_TARGETS = {
    "Hisar": 640, "Karnal": 880, "Faridabad": 620, "Sonipat": 280, "Bhiwani": 560,
    "Kurukshetra": 520, "Rohtak": 560, "Gurgaon": 840, "Ambala": 1040,
    "HR Division": 20, "D Division": 60,
}
_TARGET_LOOKUP = {norm(k): v for k, v in DIVISION_DAILY_TARGETS.items()}


def daily_target_for(division):
    """Daily target for a division name, matched ignoring case/spacing. None if not configured."""
    return _TARGET_LOOKUP.get(norm(division))


def find_col(cols, keys, exclude=()):
    for k in keys:
        for c in cols:
            n = norm(c)
            if k in n and not any(x in n for x in exclude):
                return c
    return None


def exact_col(cols, name):
    return next((c for c in cols if norm(c) == name), None)


def to_num(series):
    return pd.to_numeric(series.astype(str).str.replace(",", "", regex=False), errors="coerce").fillna(0)


def date_from_filename(name):
    """Look for a date in the uploaded file's name (e.g. '22.09.2026.xlsx' or '2026-09-22.xlsx')
    and return it as 'DD-MM-YYYY', or None if no valid date is found."""
    stem = Path(name).stem
    m = re.search(r"(\d{2})[.\-_](\d{2})[.\-_](\d{4})", stem)  # DD.MM.YYYY / DD-MM-YYYY / DD_MM_YYYY
    if m:
        d, mo, y = m.groups()
        try:
            return datetime(int(y), int(mo), int(d)).strftime("%d-%m-%Y")
        except ValueError:
            pass
    m = re.search(r"(\d{4})[.\-_](\d{2})[.\-_](\d{2})", stem)  # YYYY-MM-DD
    if m:
        y, mo, d = m.groups()
        try:
            return datetime(int(y), int(mo), int(d)).strftime("%d-%m-%Y")
        except ValueError:
            pass
    m = re.search(r"(?<!\d)(\d{2})(\d{2})(\d{4})(?!\d)", stem)  # DDMMYYYY
    if m:
        d, mo, y = m.groups()
        try:
            return datetime(int(y), int(mo), int(d)).strftime("%d-%m-%Y")
        except ValueError:
            pass
    return None


def parse_label_date(label):
    """Try to read an upload's label as a calendar date, for date-range filtering. Returns a date or None."""
    label = (label or "").strip()
    for fmt in ("%d-%m-%Y", "%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(label, fmt).date()
        except ValueError:
            continue
    return None


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


def read_file(f, parser):
    """Return parser(dataframe). For Excel files with several sheets, the first sheet that parses is used."""
    if f.name.lower().endswith(".csv"):
        sheets = {"CSV": pd.read_csv(f, dtype=str, keep_default_na=False)}
    else:
        sheets = pd.read_excel(f, dtype=str, keep_default_na=False, sheet_name=None)
    errors = []
    for name, df in sheets.items():
        df.columns = [str(c).strip() for c in df.columns]
        if df.empty:
            errors.append(f"Sheet '{name}' is empty.")
            continue
        try:
            return parser(df)
        except ValueError as e:
            errors.append(f"Sheet '{name}': {e}")
    raise ValueError(" | ".join(errors))


def pick(df, spec):
    cols = list(df.columns)
    found = {name: find_col(cols, keys) for name, keys in spec.items()}
    missing = [n for n, c in found.items() if c is None]
    if missing:
        raise ValueError(f"Column not found: {', '.join(missing)}. Headers in your file: {', '.join(cols)}")
    return found


def parse_master(df):
    c = pick(df, {"Station Number": ["stationno", "stationnumber", "station"],
                  "Office ID": ["officeid", "office"]})
    cols = list(df.columns)
    # also accepts the misspelt header "Divison"
    dv = next((x for x in cols if ("division" in norm(x) or "divison" in norm(x)) and "sub" not in norm(x)), None)
    sd = next((x for x in cols if "subdiv" in norm(x)), None)
    if dv is None:
        raise ValueError(f"Column not found: Division. Headers in your file: {', '.join(cols)}")
    a, d = find_col(cols, ["address"]), find_col(cols, ["district"])

    def text(col):
        return df[col].str.strip() if col else ""

    m = pd.DataFrame({"station": text(c["Station Number"]), "office_id": text(c["Office ID"]),
                      "division": text(dv), "sub_division": text(sd), "address": text(a), "district": text(d)})
    m["key"] = m["station"].map(norm_key)
    m = m[m["key"] != ""].replace("", pd.NA)
    # a station may appear on several rows: keep the first filled value of every column
    m = m.groupby("key", as_index=False, sort=False).first().fillna("")
    # unify spellings such as "HIsar" and "Hisar" (most common spelling wins)
    for col in ("division", "sub_division"):
        spell = m[m[col] != ""].groupby(m[col].str.lower())[col].agg(lambda s: s.value_counts().idxmax())
        m[col] = m[col].str.lower().map(spell).fillna("")
    return m


def parse_tx(df):
    cols = list(df.columns)
    s = find_col(cols, ["stationno", "stationnumber", "station"])
    e = find_col(cols, ["newenrol", "enrol"]) or exact_col(cols, "countn")
    tot = find_col(cols, ["countuplusnplusz", "totaltransaction", "countuplusn"])
    mb = find_col(cols, ["ismbu", "mbu", "mandatorybiometric"], exclude=("non",))
    nm = find_col(cols, ["nonmbu"])
    dm = find_col(cols, ["demoupdate", "demographic", "demo"])
    up = find_col(cols, ["numberofupdation", "updation"])
    op = find_col(cols, ["sessionoperatorid", "operatorid", "operatorname", "operator"])
    if not s or not e or not (tot or mb or dm or nm or up):
        raise ValueError("Required columns: station_number, Count_N, and Count_U_plus_N_plus_Z "
                         "(or Count_U_plus_N / IS_MBU / NON_MBU / DEMO_UPDATE). "
                         f"Headers in your file: {', '.join(cols)}")
    a, d = find_col(cols, ["address"]), find_col(cols, ["district"])
    dv = next((x for x in cols if "division" in norm(x) and "sub" not in norm(x)), None)
    sd = next((x for x in cols if "subdiv" in norm(x)), None)

    def text(c):
        return df[c].str.strip() if c else ""

    enr = to_num(df[e])
    mbu = to_num(df[mb]) if mb else 0.0
    demo = to_num(df[dm]) if dm else 0.0
    non = to_num(df[nm]) if nm else 0.0
    if tot:  # Total transactions from the sheet is the source of truth; updates = Total - New
        upd = (to_num(df[tot]) - enr).clip(lower=0)
    elif mb or dm or nm:
        upd = mbu + demo + non
    else:
        upd = to_num(df[up])
    t = pd.DataFrame({"station": df[s].str.strip(), "address": text(a), "district": text(d),
                      "t_div": text(dv), "t_sub": text(sd), "operator": text(op), "enr": enr, "mbu": mbu,
                      "demo": demo, "nonmbu": non, "upd": upd})
    t["key"] = t["station"].map(norm_key)
    return t[t["key"] != ""]


def save_master(m):
    con = sqlite3.connect(DB)
    try:
        m.to_sql("master", con, if_exists="replace", index=False)
        con.commit()
    finally:
        con.close()
    kv_sync.request_backup()


def parse_operator_master(df):
    """Operator master: Operator ID + Operator Name (any extra columns are ignored)."""
    cols = list(df.columns)
    oid = find_col(cols, ["sessionoperatorid", "operatorid", "operatorcode", "userid", "employeeid", "empid"],
                   exclude=("name",))
    if oid is None:  # header such as just "Operator" or "ID"
        oid = find_col(cols, ["operator", "id"], exclude=("name",))
    nm = find_col(cols, ["operatorname", "employeename", "username", "name"])
    if oid is None or nm is None or oid == nm:
        raise ValueError("Required columns: Operator ID and Operator Name. "
                         f"Headers in your file: {', '.join(cols)}")
    m = pd.DataFrame({"operator_id": df[oid].astype(str).str.strip(),
                      "operator_name": df[nm].astype(str).str.strip()})
    m["op_key"] = m["operator_id"].map(norm_key)
    m = m[m["op_key"] != ""].replace("", pd.NA)
    # an operator may appear on several rows: keep the first filled name
    m = m.groupby("op_key", as_index=False, sort=False).first().fillna("")
    return m[["op_key", "operator_id", "operator_name"]]


def save_operator_master(m):
    con = sqlite3.connect(DB)
    try:
        m.to_sql("operator_master", con, if_exists="replace", index=False)
        con.commit()
    finally:
        con.close()
    kv_sync.request_backup()


def operator_names():
    """Series of operator_name indexed by normalised operator key (empty if no operator master)."""
    if not table_exists("operator_master"):
        return pd.Series(dtype=str)
    om = read_sql("SELECT op_key, operator_name FROM operator_master")
    return om.drop_duplicates("op_key").set_index("op_key")["operator_name"]


def ensure_cols(con, table, wanted):
    have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    if have:
        for name, typ in wanted.items():
            if name not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")


def save_tx(t, label, by):
    con = sqlite3.connect(DB)
    try:
        cur = con.execute("INSERT INTO uploads(label, uploaded_by, uploaded_at) VALUES (?,?,?)",
                          (label, by, datetime.now().strftime("%Y-%m-%d %H:%M")))
        ensure_cols(con, "tx", {"mbu": "REAL", "demo": "REAL", "nonmbu": "REAL", "t_div": "TEXT", "t_sub": "TEXT",
                                "operator": "TEXT"})
        t.assign(upload_id=cur.lastrowid).to_sql("tx", con, if_exists="append", index=False)
        con.commit()
    finally:
        con.close()
    kv_sync.request_backup()


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
    if LOGO:
        mime = "image/jpeg" if LOGO.suffix.lower() in (".jpg", ".jpeg") else f"image/{LOGO.suffix[1:].lower()}"
        b64 = base64.b64encode(LOGO.read_bytes()).decode()
        plate = f'<div class="hp-plate"><img src="data:{mime};base64,{b64}" alt="India Post"></div><br>'
    st.markdown(f'<div class="hp-band">{plate}<div class="hp-title">Aadhaar MIS Dashboard<br>'
                'Department of Posts, India<br>Haryana Circle</div></div>', unsafe_allow_html=True)
    with st.form("login"):
        u = st.text_input("Username")
        p = st.text_input("Password", type="password")
        if st.form_submit_button("Login"):
            user, err = authenticate(u, p)
            if user:
                st.session_state["user"] = user
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

    agg, missing, m = build_stations(ids)

    f1, f2, f3 = st.columns([1, 1, 2])
    dv = f1.selectbox("Division", ["All"] + sorted(set(agg["division"]) | set(m["division"])))
    pool = agg if dv == "All" else agg[agg["division"] == dv]
    ds = f2.selectbox("District", ["All"] + sorted(d for d in pool["district"].unique() if d))
    q = f3.text_input("Search station, office ID or address").strip().lower()

    v = pool if ds == "All" else pool[pool["district"] == ds]
    if q:
        blob = (v["station"] + " " + v["office_id"] + " " + v["address"]).str.lower()
        v = v[blob.str.contains(q, regex=False)]
    miss = missing if dv == "All" else missing[missing["division"] == dv]

    k = st.columns(6)
    k[0].metric("Total transactions", f"{int(v['total'].sum()):,}")
    k[1].metric("New enrolments", f"{int(v['enr'].sum()):,}")
    k[2].metric("Updates", f"{int(v['upd'].sum()):,}")
    k[3].metric("Stations reporting", f"{int(v['days_reported'].sum()):,}")
    k[4].metric("Zero-transaction stations", f"{int((v['total'] == 0).sum()):,}")
    k[5].metric("Master stations with no data", f"{len(miss):,}")

    show = v.rename(columns={"station": "Station", "office_id": "Office ID", "division": "Division",
                             "sub_division": "Sub Division", "district": "District",
                             "address": "Office address", "machines": "Machines",
                             "enr": "New enrolment", "mbu": "MBU", "demo": "Demographic updates",
                             "nonmbu": "Non-MBU", "upd": "Updates", "total": "Total"})
    cols = ["Station", "Office ID", "Division", "Sub Division", "District", "Office address", "Machines",
            "New enrolment", "MBU", "Demographic updates", "Non-MBU", "Updates", "Total"]
    show = show[cols].sort_values("Total", ascending=False)

    period_days = len(ids)
    tgt = show.groupby("Division")["Total"].sum().reset_index().rename(columns={"Total": "Achievement"})
    tgt["Daily Target"] = tgt["Division"].map(daily_target_for)
    no_target = sorted(tgt.loc[tgt["Daily Target"].isna(), "Division"])
    tgt = tgt.dropna(subset=["Daily Target"])
    if len(tgt):
        tgt["Target"] = (tgt["Daily Target"] * period_days).astype(int)
        tgt["Achievement"] = tgt["Achievement"].astype(int)
        tgt["Shortfall / Surplus"] = tgt["Achievement"] - tgt["Target"]
        tgt["% Achieved"] = (tgt["Achievement"] / tgt["Target"] * 100).round(1)

        st.subheader("Target vs Achievement")
        st.caption(f"Target = Daily Target x {period_days} working day(s) selected above. "
                   "Achievement = actual transactions in the same period, for the divisions currently in view.")
        fig_t = px.bar(tgt.melt("Division", value_vars=["Target", "Achievement"],
                                var_name="Type", value_name="Count"),
                       x="Division", y="Count", color="Type", barmode="group", text="Count",
                       color_discrete_map={"Target": "#B0B0B0", "Achievement": "#7A1F2B"},
                       title="Target vs Achievement (transactions)")
        fig_t.update_traces(texttemplate="%{text:,.0f}", textposition="outside")
        st.plotly_chart(fig_t, use_container_width=True)

        tshow = tgt[["Division", "Target", "Achievement", "Shortfall / Surplus", "% Achieved"]] \
            .sort_values("Division")
        st.dataframe(tshow, hide_index=True, use_container_width=True)
        st.download_button("Download Target vs Achievement CSV", tshow.to_csv(index=False).encode("utf-8-sig"),
                           "target_vs_achievement.csv", "text/csv", key="dl_target_vs_ach")
        if no_target:
            st.caption("No target configured for: " + ", ".join(no_target))
    else:
        st.info("No division in the current view has a configured daily target.")

    st.divider()
    d = show.groupby("Division")[["New enrolment", "Updates"]].sum().reset_index()
    fig_d = px.bar(d.melt("Division", var_name="Type", value_name="Count"), x="Division", y="Count",
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

    ops = build_operators(ids)
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

    top = show.head(20).copy()
    top["Operator"] = top.apply(
        lambda r: f"{r['Operator Name']} ({r['Operator ID']})" if r["Operator Name"] else r["Operator ID"], axis=1)
    fig = px.bar(top, x="Operator", y="Total", color="Division", text="Total",
                title="Top 20 operators by total transactions")
    fig.update_traces(texttemplate="%{text:,.0f}", textposition="outside")
    st.plotly_chart(fig, use_container_width=True)

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
    with st.form("master_form", clear_on_submit=True):
        f = st.file_uploader("Master file", type=["xlsx", "xls", "csv"], key="master_uploader")
        if f is not None:
            st.caption(f"Selected file: **{f.name}** ({f.size / 1024:.1f} KB) - ready to save.")
        if st.form_submit_button("Save master"):
            if not f:
                st.warning("No file detected. Please choose the file again, wait until its name "
                           "appears above (this confirms it finished uploading), then click "
                           "'Save master' again.")
            else:
                try:
                    mm = read_file(f, parse_master)
                    save_master(mm)
                    flash(f"Master saved: {len(mm)} stations.")
                except Exception as e:
                    st.error(str(e))

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
            run("DELETE FROM tx WHERE upload_id=?", (pick_id,))
            run("DELETE FROM uploads WHERE id=?", (pick_id,))
            kv_sync.request_backup()
            flash("Upload deleted.")


def users_tab():
    if not is_admin():  # server-side guard
        st.error("Only the admin can manage users.")
        return
    me = st.session_state["user"]["username"]

    st.subheader("Bulk create Division logins")
    st.caption("Creates one login per Division found in the master sheet. The username is generated "
               f"from the division's name, the default password for all of them is \"{DEFAULT_TEMP_PASSWORD}\", "
               "and the user must set a new password on first login.")
    if not table_exists("master"):
        st.info("Please upload the master sheet from the Upload data tab first.")
    elif st.button("Create logins for all Divisions"):
        created, renamed, skipped = bulk_create_division_users(DEFAULT_TEMP_PASSWORD)
        if created:
            st.success(f"{len(created)} login(s) created. Default password: {DEFAULT_TEMP_PASSWORD} "
                       "(must be changed on first login).")
            st.dataframe(pd.DataFrame(created, columns=["Division", "Username"]),
                        hide_index=True, use_container_width=True)
        if skipped:
            st.info(f"{len(skipped)} division(s) already had a login, so they were skipped.")
            st.dataframe(pd.DataFrame(skipped, columns=["Division", "Username"]),
                        hide_index=True, use_container_width=True)
        if not created and not skipped:
            st.info("No Division found in the master sheet.")

    st.divider()
    st.subheader("Create user login")
    with st.form("new_user", clear_on_submit=True):
        u = st.text_input("Username").strip().lower()
        p = st.text_input("Temporary password", type="password")
        if st.form_submit_button("Create user"):
            err = check_new_credentials(u, p, p)
            if err:
                st.error(err)
            else:
                add_user(u, p, "user", must_change=1)
                flash(f"User '{u}' created. Share the username and password with them - "
                     "they will need to set a new password on first login.")

    users = read_sql("SELECT username, role, active, created_at FROM users ORDER BY role, username")
    st.dataframe(users.assign(active=users["active"].map({1: "Yes", 0: "No"})).rename(columns={
        "username": "Username", "role": "Role", "active": "Active", "created_at": "Created"}),
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
        if st.button(f"Reset to default password ({DEFAULT_TEMP_PASSWORD})"):
            set_password(target, DEFAULT_TEMP_PASSWORD, must_change=1)
            st.success(f"Password reset to {DEFAULT_TEMP_PASSWORD} for {target}. "
                      "They will need to set a new password on first login.")
    row = users[users["username"] == target].iloc[0]
    a, b = st.columns(2)
    if a.button("Disable login" if row["active"] else "Enable login"):
        run("UPDATE users SET active=? WHERE username=?", (0 if row["active"] else 1, target))
        kv_sync.request_backup()
        flash("User updated.")
    with b:
        sure = st.checkbox("Confirm delete")
        if st.button("Delete user") and sure:
            run("DELETE FROM users WHERE username=?", (target,))
            kv_sync.request_backup()
            flash(f"User '{target}' deleted.")


def build_stations(ids):
    """Station-wise totals for the selected upload ids, joined with the master sheet."""
    m = read_sql("SELECT * FROM master")
    for c in ("sub_division", "address", "district"):
        if c not in m.columns:
            m[c] = ""
    t = read_sql(f"SELECT * FROM tx WHERE upload_id IN ({','.join('?' * len(ids))})", tuple(ids))
    for c in ("mbu", "demo", "nonmbu"):
        t[c] = pd.to_numeric(t[c], errors="coerce").fillna(0) if c in t.columns else 0.0
    for c in ("t_div", "t_sub", "address", "district"):
        t[c] = t[c].fillna("").astype(str) if c in t.columns else ""
    agg = t.groupby("key", as_index=False).agg(
        station=("station", "first"), district=("district", "first"), address=("address", "first"),
        t_div=("t_div", "first"), t_sub=("t_sub", "first"), machines=("key", "size"),
        days_reported=("upload_id", "nunique"),
        enr=("enr", "sum"), mbu=("mbu", "sum"), demo=("demo", "sum"), nonmbu=("nonmbu", "sum"),
        upd=("upd", "sum"))
    agg["total"] = agg["enr"] + agg["upd"]
    mm = m[["key", "office_id", "division", "sub_division"]].assign(m_addr=m["address"], m_dist=m["district"])
    agg = agg.merge(mm, on="key", how="left")

    def first_filled(a, b, default):
        a, b = a.fillna("").astype(str).str.strip(), b.fillna("").astype(str).str.strip()
        return a.where(a != "", b.where(b != "", default))

    agg["division"] = first_filled(agg["division"], agg["t_div"], "Not in master")
    agg["sub_division"] = first_filled(agg["sub_division"], agg["t_sub"], "Not mapped")
    agg["office_id"] = agg["office_id"].fillna("")
    agg["address"] = first_filled(agg["address"], agg["m_addr"], "")
    agg["district"] = first_filled(agg["district"], agg["m_dist"], "")
    return agg, m[~m["key"].isin(agg["key"])], m


def build_operators(ids):
    """Operator-wise totals for the selected upload ids. Each operator is mapped to the
    division / sub-division of the stations they reported from (most frequent one)."""
    agg, _, _ = build_stations(ids)
    key_div = agg.set_index("key")[["division", "sub_division"]]

    t = read_sql(f"SELECT * FROM tx WHERE upload_id IN ({','.join('?' * len(ids))})", tuple(ids))
    empty = pd.DataFrame(columns=["operator", "operator_name", "division", "sub_division", "stations", "days_worked",
                                  "enr", "mbu", "demo", "nonmbu", "upd", "total"])
    if "operator" not in t.columns:
        return empty
    t["operator"] = t["operator"].fillna("").astype(str).str.strip()
    t = t[t["operator"] != ""]
    if t.empty:
        return empty
    for c in ("mbu", "demo", "nonmbu"):
        t[c] = pd.to_numeric(t[c], errors="coerce").fillna(0) if c in t.columns else 0.0
    t = t.join(key_div, on="key")
    t["division"] = t["division"].fillna("Not in master")
    t["sub_division"] = t["sub_division"].fillna("Not mapped")

    def mode(s):
        return s.value_counts().idxmax()

    op = t.groupby("operator", as_index=False).agg(
        division=("division", mode), sub_division=("sub_division", mode),
        stations=("key", "nunique"), days_worked=("upload_id", "nunique"),
        enr=("enr", "sum"), mbu=("mbu", "sum"), demo=("demo", "sum"), nonmbu=("nonmbu", "sum"), upd=("upd", "sum"))
    op["total"] = op["enr"] + op["upd"]
    op["operator_name"] = op["operator"].map(norm_key).map(operator_names()).fillna("")
    return op


REPORT_COLS = ["Division Name", "Sub Division Name", "Total Transactions", "New Enrollments", "MBU",
               "Demographic Updates", "Non-MBU Biometric Updates"]


def report_tab():
    from openpyxl.styles import Font
    uploads = run("SELECT * FROM uploads ORDER BY id DESC", many=True)
    if not table_exists("master") or not uploads:
        st.info("Data has not been uploaded yet. Please contact the admin.")
        return

    ids = select_period_ids(uploads, key_prefix="rep")
    if not ids:
        return

    agg_full, missing, _ = build_stations(ids)
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

    rep = (agg.groupby(["division", "sub_division"], as_index=False)[["total", "enr", "mbu", "demo", "nonmbu"]].sum()
           .sort_values(["division", "sub_division"]))
    rep.columns = REPORT_COLS
    rep[REPORT_COLS[2:]] = rep[REPORT_COLS[2:]].round().astype(int)
    grand = pd.DataFrame([["Grand Total", ""] + [int(rep[c].sum()) for c in REPORT_COLS[2:]]], columns=REPORT_COLS)
    out = pd.concat([rep, grand], ignore_index=True)

    if (agg["sub_division"] == "Not mapped").any():
        st.warning("Some stations have no Sub Division. Add a 'Sub Division Name' column to the master sheet "
                   "and upload the master again.")
    st.caption("Total Transactions (Count_U_plus_N_plus_Z) = New Enrollments + MBU + Demographic Updates + "
               "Non-MBU Biometric Updates. When several days are selected, their figures are added together.")
    st.dataframe(out, hide_index=True, use_container_width=True)

    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        out.to_excel(w, index=False, sheet_name="Report")
        ws = w.sheets["Report"]
        for i, c in enumerate(out.columns, 1):
            ws.column_dimensions[chr(64 + i)].width = max(len(c), int(out[c].astype(str).str.len().max())) + 3
        for cell in ws[1] + ws[ws.max_row]:
            cell.font = Font(bold=True)
    a, b = st.columns(2)
    a.download_button("Download Excel", buf.getvalue(), "haryana_circle_report.xlsx",
                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    b.download_button("Download CSV", out.to_csv(index=False).encode("utf-8-sig"), "haryana_circle_report.csv",
                      "text/csv")

    st.divider()
    st.subheader("Division-wise Consolidated Report")
    st.caption("All divisions, summed to a single row each - independent of the Division filter above.")
    cons = (agg_full.groupby("division", as_index=False)[["total", "enr", "mbu", "demo", "nonmbu"]].sum()
            .sort_values("division"))
    cons.columns = ["Division Name", "Total Transactions", "New Enrollments", "MBU",
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
            ws2.column_dimensions[chr(64 + i)].width = max(len(c), int(out2[c].astype(str).str.len().max())) + 3
        for cell in ws2[1] + ws2[ws2.max_row]:
            cell.font = Font(bold=True)
    c1, c2 = st.columns(2)
    c1.download_button("Download Division-wise Excel", buf2.getvalue(), "haryana_circle_division_consolidated.xlsx",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    c2.download_button("Download Division-wise CSV", out2.to_csv(index=False).encode("utf-8-sig"),
                       "haryana_circle_division_consolidated.csv", "text/csv")

    st.divider()
    st.subheader("Operator-wise Consolidated Report")
    st.caption("Each operator is grouped under the division / sub-division they reported from most often "
               "in the selected period(s), independent of the Division filter above.")
    ops = build_operators(ids)
    if ops.empty:
        st.info("No operator data found in the selected period. Make sure the daily upload file has a "
                "Session Operator ID (or Operator ID) column.")
    else:
        orep = ops[["operator", "operator_name", "division", "sub_division", "total", "enr", "mbu", "demo",
                    "nonmbu", "days_worked"]].sort_values(["division", "sub_division", "total"],
                                                ascending=[True, True, False]).copy()
        orep.columns = ["Operator ID", "Operator Name", "Division", "Sub Division", "Total Transactions", "New Enrollments",
                        "MBU", "Demographic Updates", "Non-MBU Biometric Updates", "Days Worked"]
        num_cols = ["Total Transactions", "New Enrollments", "MBU", "Demographic Updates",
                   "Non-MBU Biometric Updates", "Days Worked"]
        orep[num_cols] = orep[num_cols].round().astype(int)
        grand3 = pd.DataFrame([["Grand Total", "", "", ""] + [int(orep[c].sum()) for c in num_cols]],
                              columns=orep.columns)
        out3 = pd.concat([orep, grand3], ignore_index=True)
        st.dataframe(out3, hide_index=True, use_container_width=True)

        buf3 = BytesIO()
        with pd.ExcelWriter(buf3, engine="openpyxl") as w:
            out3.to_excel(w, index=False, sheet_name="Operator Report")
            ws3 = w.sheets["Operator Report"]
            for i, c in enumerate(out3.columns, 1):
                ws3.column_dimensions[chr(64 + i)].width = max(len(c), int(out3[c].astype(str).str.len().max())) + 3
            for cell in ws3[1] + ws3[ws3.max_row]:
                cell.font = Font(bold=True)
        g1, g2 = st.columns(2)
        g1.download_button("Download Operator-wise Excel", buf3.getvalue(), "haryana_circle_operator_report.xlsx",
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           key="op_rep_dl_xlsx")
        g2.download_button("Download Operator-wise CSV", out3.to_csv(index=False).encode("utf-8-sig"),
                           "haryana_circle_operator_report.csv", "text/csv", key="op_rep_dl_csv")


def camp_entry_form():
    st.subheader("Add camp entry")
    divisions = get_divisions()
    if not divisions:
        st.info("The master sheet has not been uploaded yet. The admin must upload the master sheet "
                "before camp entries can be added.")
        return
    c1, c2, c3 = st.columns(3)
    dv = c1.selectbox("Division", divisions, key="camp_dv")
    subs = get_subdivisions(dv)
    sd = c2.selectbox("Sub Division", subs if subs else ["(none found in master)"], key="camp_sd")
    dt = c3.date_input("Camp date", value=datetime.now().date(), format="DD-MM-YYYY", key="camp_dt")
    c4, c5 = st.columns([2, 1])
    loc = c4.text_input("Camp location", key="camp_loc")
    txn = c5.number_input("Number of transactions", min_value=0, step=1, key="camp_txn")
    remarks = st.text_input("Remarks (optional)", key="camp_remarks")
    if st.button("Save camp entry"):
        if not loc.strip():
            st.warning("Please enter the camp location.")
        elif not subs:
            st.warning("No Sub Division was found in the master sheet for this division.")
        else:
            save_camp(dt.strftime("%d-%m-%Y"), dv, sd, loc.strip(), int(txn), remarks.strip(),
                      st.session_state["user"]["username"])
            flash(f"Camp entry saved: {loc.strip()} ({dt.strftime('%d-%m-%Y')}).")


def excel_download(df, sheet_name, filename, label, bold_last_row=True):
    """Build a formatted .xlsx (auto column width, bold header + optional bold last row)
    and render a Streamlit download button for it."""
    from openpyxl.styles import Font
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name=sheet_name)
        ws = w.sheets[sheet_name]
        for i, c in enumerate(df.columns, 1):
            ws.column_dimensions[chr(64 + i)].width = max(len(c), int(df[c].astype(str).str.len().max())) + 3
        rows = ws[1] + ws[ws.max_row] if bold_last_row else ws[1]
        for cell in rows:
            cell.font = Font(bold=True)
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
        else:
            st.info("The bridge holds no snapshots yet.")

    st.caption("The same figures are available from a shell: `python kv_sync.py status` "
               "(or `generations`).")


def _since_text(iso_ts):
    return f" since {iso_ts}" if iso_ts else ""


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
                kv_sync.request_backup()
                flash("Camp entry deleted.")


# ---------------------------------------------------------------- main
init_db()
if "flash" in st.session_state:
    st.success(st.session_state.pop("flash"))

if not run("SELECT 1 FROM users WHERE role='admin'", one=True):
    setup_screen()
    st.stop()
if "user" not in st.session_state:
    login_screen()
    st.stop()
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