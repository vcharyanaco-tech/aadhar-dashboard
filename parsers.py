"""Spreadsheet parsing and database persistence for the Aadhaar dashboard.

Split out of app.py so it can be tested without Streamlit: everything here is a
plain function over a pandas DataFrame, or a small database helper with an
explicit connection. Nothing in this module touches `st`, session state or the
screens.

The rules that decide the numbers on the dashboard live here, so they are the
ones worth pinning down in tests (see tests/test_parsers.py):

* `parse_tx` treats the sheet's own total column as the source of truth and
  derives updates as `total - new_enrolments`, clipped at zero. Only when that
  column is absent does it fall back to summing MBU + demographic + non-MBU.
* `parse_master` and `parse_tx` both normalise station numbers through
  `norm_key`, which is what makes the master<->transaction join work when the
  two sheets spell the same station differently.
"""

import difflib
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------- normalisation
def norm(s):
    """Lowercase and strip everything that is not alphanumeric."""
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def norm_key(v):
    """Normalise a station/operator number to a join key.

    Strips a trailing '.0' that spreadsheet software adds to numeric cells,
    lowercases, and drops leading zeros, so '00123', '123' and '123.0' all
    become the same key.
    """
    t = re.sub(r"\.0+$", "", str(v).strip().lower())
    return t.lstrip("0") or ("0" if t else "")


def find_col(cols, keys, exclude=()):
    """First column whose normalised name contains any of `keys` and none of `exclude`."""
    for k in keys:
        for c in cols:
            n = norm(c)
            if k in n and not any(x in n for x in exclude):
                return c
    return None


def exact_col(cols, name):
    return next((c for c in cols if norm(c) == name), None)


def to_num(series):
    """Coerce a column to numbers, treating '1,234' as 1234 and blanks as 0."""
    return pd.to_numeric(series.astype(str).str.replace(",", "", regex=False), errors="coerce").fillna(0)


def fuzzy_hint(cols, keys):
    """A 'did you mean' hint for a header that is close to, but not, one of `keys`.

    Uses difflib rather than a hand-rolled prefix score: a shared-prefix measure
    punishes a header that carries a suffix, so "Divission Name" would never be
    recognised as a near miss for "division" even though it plainly is one.
    Returns the closest header, or None when nothing is close enough.
    """
    best = None
    for k in keys:
        k = norm(k)
        if not k:
            continue
        for candidate in difflib.get_close_matches(k, [norm(c) for c in cols], n=1, cutoff=0.7):
            # get_close_matches works on normalised names, so map back to the
            # original header for the message.
            for original in cols:
                if norm(original) == candidate:
                    best = original
                    break
            if best is not None:
                return best
    return best


def missing_col_error(what, cols, keys):
    """Build a helpful ValueError for a header that could not be found."""
    hint = fuzzy_hint(cols, keys)
    detail = f" Did you mean '{hint}'?" if hint else ""
    return ValueError(f"Column not found: {what}.{detail} "
                      f"Headers in your file: {', '.join(map(str, cols))}")


# ---------------------------------------------------------------- division target
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


# ---------------------------------------------------------------- date labels
def date_from_filename(name):
    """Read a date out of an uploaded file's name as 'DD-MM-YYYY', or None.

    Accepts '22.09.2026.xlsx', '2026-09-22.xlsx' and '22092026.xlsx'.
    """
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
    """Read an upload's label as a calendar date, for date-range filtering. None if it is not one."""
    label = (label or "").strip()
    for fmt in ("%d-%m-%Y", "%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(label, fmt).date()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------- sheet readers
def read_sheets(f):
    """Load an uploaded Excel/CSV into {sheet_name: DataFrame}, all as strings.

    Everything is read as text so the parsers control numeric coercion; letting
    pandas guess turns station numbers like '00123' into the integer 123 and
    loses the leading zeros the master sheet may rely on.
    """
    if str(getattr(f, "name", "")).lower().endswith(".csv"):
        return {"CSV": pd.read_csv(f, dtype=str, keep_default_na=False)}
    return pd.read_excel(f, dtype=str, keep_default_na=False, sheet_name=None)


def parse_first_usable(f, parser):
    """Run `parser` over each sheet in turn, returning the first one that parses.

    Reports every failure at once so a multi-sheet workbook does not need one
    upload attempt per sheet to diagnose.
    """
    errors = []
    for name, df in read_sheets(f).items():
        df = df.copy()
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
    """Locate each required column by name. Raises ValueError listing what is missing."""
    cols = list(df.columns)
    found = {}
    missing = []
    for name, keys in spec.items():
        col = find_col(cols, keys)
        if col is None:
            missing.append((name, keys))
        else:
            found[name] = col
    if missing:
        parts = []
        for name, keys in missing:
            hint = fuzzy_hint(cols, keys)
            parts.append(f"{name}" + (f" (did you mean '{hint}'?)" if hint else ""))
        raise ValueError(f"Column not found: {', '.join(parts)}. "
                         f"Headers in your file: {', '.join(map(str, cols))}")
    return found


def _division_col(cols):
    """Find the division column, tolerating the common misspelling 'Divison'."""
    return next((x for x in cols
                 if ("division" in norm(x) or "divison" in norm(x)) and "sub" not in norm(x)), None)


def _subdivision_col(cols):
    return next((x for x in cols if "subdiv" in norm(x)), None)


# ---------------------------------------------------------------- parsers
MASTER_COLS = ["key", "station", "office_id", "division", "sub_division", "address", "district"]
OPERATOR_COLS = ["op_key", "operator_id", "operator_name"]
# The contract for the tx table. parse_tx always produces exactly these columns,
# and save_tx refuses to append a frame that does not match, so the table's
# shape cannot drift as parsers are changed.
TX_COLS = ["station", "address", "district", "t_div", "t_sub", "operator",
           "enr", "mbu", "demo", "nonmbu", "upd", "key"]


def parse_master(df):
    """Master sheet -> one row per station with division/sub-division/address."""
    c = pick(df, {"Station Number": ["stationno", "stationnumber", "station"],
                  "Office ID": ["officeid", "office"]})
    cols = list(df.columns)
    dv = _division_col(cols)
    if dv is None:
        raise missing_col_error("Division", cols, ["division", "divison"])
    sd = _subdivision_col(cols)
    a, d = find_col(cols, ["address"]), find_col(cols, ["district"])

    def text(col):
        return df[col].str.strip() if col else ""

    m = pd.DataFrame({"station": text(c["Station Number"]), "office_id": text(c["Office ID"]),
                      "division": text(dv), "sub_division": text(sd), "address": text(a), "district": text(d)})
    m["key"] = m["station"].map(norm_key)
    m = m[m["key"] != ""].replace("", pd.NA)
    # A station may appear on several rows: keep the first filled value of every column.
    m = m.groupby("key", as_index=False, sort=False).first().fillna("")
    # Unify spellings such as "HIsar" and "Hisar" (most common spelling wins).
    for col in ("division", "sub_division"):
        filled = m[m[col] != ""]
        if filled.empty:
            m[col] = ""
            continue
        spell = filled.groupby(filled[col].str.lower())[col].agg(lambda s: s.value_counts().idxmax())
        m[col] = m[col].str.lower().map(spell).fillna("")
    return m[MASTER_COLS]


def parse_tx(df):
    """Daily transaction sheet -> per-station counts, with `upd` derived.

    The sheet's own total column is the source of truth when present, and updates
    are `total - new`, clipped at zero so a total smaller than the new-enrolment
    count cannot produce a negative update figure. Otherwise updates are the sum
    of the biometric/demographic columns, and failing that the updation column.
    """
    cols = list(df.columns)
    s = find_col(cols, ["stationno", "stationnumber", "station"])
    e = find_col(cols, ["newenrol", "enrol"]) or exact_col(cols, "countn")
    tot = find_col(cols, ["countuplusnplusz", "totaltransaction", "countuplusn"])
    mb = find_col(cols, ["ismbu", "mbu", "mandatorybiometric"], exclude=("non",))
    nm = find_col(cols, ["nonmbu"])
    dm = find_col(cols, ["demoupdate", "demographic", "demo"])
    up = find_col(cols, ["numberofupdation", "updation"])
    op = find_col(cols, ["sessionoperatorid", "operatorid", "operatorname", "operator"])
    if not s:
        raise missing_col_error("station_number", cols, ["stationno", "stationnumber", "station"])
    if not e:
        raise missing_col_error("Count_N", cols, ["newenrol", "enrol", "countn"])
    if not (tot or mb or dm or nm or up):
        raise ValueError("Required columns: station_number, Count_N, and Count_U_plus_N_plus_Z "
                         "(or Count_U_plus_N / IS_MBU / NON_MBU / DEMO_UPDATE). "
                         f"Headers in your file: {', '.join(map(str, cols))}")
    a, d = find_col(cols, ["address"]), find_col(cols, ["district"])
    dv = _division_col(cols)
    sd = _subdivision_col(cols)

    def text(c):
        return df[c].str.strip() if c else ""

    enr = to_num(df[e])
    mbu = to_num(df[mb]) if mb else 0.0
    demo = to_num(df[dm]) if dm else 0.0
    non = to_num(df[nm]) if nm else 0.0
    if tot:  # The sheet's total is authoritative; updates = total - new enrolments.
        upd = (to_num(df[tot]) - enr).clip(lower=0)
    elif mb or dm or nm:
        upd = mbu + demo + non
    else:
        upd = to_num(df[up])
    t = pd.DataFrame({"station": df[s].str.strip(), "address": text(a), "district": text(d),
                      "t_div": text(dv), "t_sub": text(sd), "operator": text(op), "enr": enr, "mbu": mbu,
                      "demo": demo, "nonmbu": non, "upd": upd})
    t["key"] = t["station"].map(norm_key)
    t = t[t["key"] != ""]
    return t[TX_COLS]


def parse_operator_master(df):
    """Operator master -> operator id + name, one row per normalised id."""
    cols = list(df.columns)
    oid = find_col(cols, ["sessionoperatorid", "operatorid", "operatorcode", "userid", "employeeid", "empid"],
                   exclude=("name",))
    if oid is None:  # header such as just "Operator" or "ID"
        oid = find_col(cols, ["operator", "id"], exclude=("name",))
    nm = find_col(cols, ["operatorname", "employeename", "username", "name"])
    if oid is None or nm is None or oid == nm:
        raise ValueError("Required columns: Operator ID and Operator Name. "
                         f"Headers in your file: {', '.join(map(str, cols))}")
    m = pd.DataFrame({"operator_id": df[oid].astype(str).str.strip(),
                      "operator_name": df[nm].astype(str).str.strip()})
    m["op_key"] = m["operator_id"].map(norm_key)
    m = m[m["op_key"] != ""].replace("", pd.NA)
    # An operator may appear on several rows. Sort by id first so the row that
    # survives .first() is the lowest id for a given key, not whatever order
    # pandas happened to produce.
    m = m.sort_values("operator_id", kind="stable")
    m = m.groupby("op_key", as_index=False, sort=False).first().fillna("")
    return m[OPERATOR_COLS]


# ---------------------------------------------------------------- persistence
# Identifiers that reach SQL as text. `quote_ident` accepts a value only if it
# matches, so nothing caller-supplied can ever be interpolated into a statement.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def quote_ident(name):
    """Validate and quote a SQL identifier, or raise ValueError.

    Callers pass literals from this module, but validating here means a future
    caller that forwards user input fails loudly instead of building injectable
    SQL.
    """
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise ValueError(f"refusing to use {name!r} as a SQL identifier")
    return '"' + name + '"'


# Column types for the tx table. Anything the parser can produce must be listed.
TX_COLUMN_TYPES = {
    "station": "TEXT", "address": "TEXT", "district": "TEXT",
    "t_div": "TEXT", "t_sub": "TEXT", "operator": "TEXT",
    "enr": "REAL", "mbu": "REAL", "demo": "REAL", "nonmbu": "REAL", "upd": "REAL",
    "key": "TEXT", "upload_id": "INTEGER",
}


def ensure_cols(con, table, wanted):
    """Add any missing columns to `table`. Returns the names added."""
    t = quote_ident(table)
    have = {r[1] for r in con.execute(f"PRAGMA table_info({t})")}
    if not have:
        # Table does not exist yet; the CREATE TABLE path will make it.
        return []
    added = []
    for name, typ in wanted.items():
        if name in have:
            continue
        # `typ` is a type name from this module's own constants, not user input,
        # but it is validated as an identifier too so it cannot carry a clause.
        con.execute(f"ALTER TABLE {t} ADD COLUMN {quote_ident(name)} {quote_ident(typ)}")
        added.append(name)
    return added


def create_tx_table(con):
    cols = ", ".join(f"{quote_ident(c)} {quote_ident(TX_COLUMN_TYPES[c])}" for c in TX_COLS)
    con.execute(f"CREATE TABLE IF NOT EXISTS {quote_ident('tx')} ({cols})")


def save_master(con, m, on_replace=None):
    """Replace the master table with `m`.

    The master is the mapping every historical transaction row is joined
    through, so replacing it can orphan rows that are already stored. If
    `on_replace` is given it is called with a summary dict and may raise to veto
    the write; the caller is expected to have confirmed with the operator first.
    """
    existing = table_keys(con, "master")
    incoming = set(m["key"].astype(str))
    orphans = existing - incoming
    if orphans and on_replace is not None:
        on_replace({
            "incoming": len(incoming),
            "existing": len(existing),
            "orphaned_keys": len(orphans),
            "sample": sorted(orphans)[:10],
        })
    m.to_sql("master", con, if_exists="replace", index=False)
    return {"replaced": len(existing), "incoming": len(incoming), "orphaned": len(orphans)}


def save_operator_master(con, m):
    m.to_sql("operator_master", con, if_exists="replace", index=False)
    return len(m)


def table_keys(con, table):
    """Distinct station keys currently in a table that has a `key` column."""
    t = quote_ident(table)
    have = {r[1] for r in con.execute(f"PRAGMA table_info({t})")}
    if "key" not in have:
        return set()
    return {r[0] for r in con.execute(f"SELECT DISTINCT {quote_ident('key')} FROM {t}")}


def save_tx(con, t, label, by, uploaded_at=None):
    """Append one day's transactions, creating the uploads row for it.

    The frame is checked against TX_COLS first: appending a frame with a renamed
    or missing column would otherwise either fail deep inside pandas or, worse,
    leave the table's shape drifting as parsers change over time.
    """
    got = list(t.columns)
    if got != TX_COLS:
        raise ValueError(
            "Transaction frame does not match the expected columns.\n"
            f"  expected: {', '.join(TX_COLS)}\n"
            f"  got:      {', '.join(map(str, got))}")
    stamp = uploaded_at or datetime.now().strftime("%Y-%m-%d %H:%M")
    cur = con.execute("INSERT INTO uploads(label, uploaded_by, uploaded_at) VALUES (?,?,?)",
                      (label, by, stamp))
    create_tx_table(con)
    ensure_cols(con, "tx", {k: v for k, v in TX_COLUMN_TYPES.items() if k not in TX_COLS})
    t.assign(upload_id=cur.lastrowid).to_sql("tx", con, if_exists="append", index=False)
    return cur.lastrowid


def ensure_indexes(con):
    """Create the indexes the report queries depend on.

    Every read of the transaction table filters on `upload_id` and joins on
    `key`; without these, each page load is a full scan of a table that grows by
    a full day of sheets every day.
    """
    t = quote_ident("tx")
    have = {r[1] for r in con.execute(f"PRAGMA table_info({t})")}
    if not have:
        return []
    con.execute(f"CREATE INDEX IF NOT EXISTS {quote_ident('idx_tx_upload_id')} "
                f"ON {t} ({quote_ident('upload_id')})")
    con.execute(f"CREATE INDEX IF NOT EXISTS {quote_ident('idx_tx_key')} "
                f"ON {t} ({quote_ident('key')})")
    # Composite covering the group-by key order used by build_stations.
    con.execute(f"CREATE INDEX IF NOT EXISTS {quote_ident('idx_tx_key_upload')} "
                f"ON {t} ({quote_ident('key')}, {quote_ident('upload_id')})")
    return ["idx_tx_upload_id", "idx_tx_key", "idx_tx_key_upload"]


def operator_names(con, table="operator_master"):
    """operator_name indexed by normalised operator key, or an empty Series.

    Ties on `op_key` are broken by the operator id and then the name, both
    ascending, so the result does not depend on row order. Previously it used a
    bare drop_duplicates, which meant a database holding two spellings of the
    same id could show either name depending on insertion order.
    """
    t = quote_ident(table)
    have = {r[1] for r in con.execute(f"PRAGMA table_info({t})")}
    if not have:
        return pd.Series(dtype=str)
    # A legacy operator_master may predate operator_id, so only include it in
    # the tie-break when the column is actually present.
    tiebreak = [c for c in ("operator_id", "operator_name") if c in have]
    order = ", ".join(quote_ident(c) for c in ["op_key"] + tiebreak)
    rows = con.execute(
        f"SELECT {quote_ident('op_key')}, {quote_ident('operator_name')} "
        f"FROM {t} WHERE {quote_ident('op_key')} != '' ORDER BY {order}"
    ).fetchall()
    frame = pd.DataFrame(rows, columns=["op_key", "operator_name"])
    if frame.empty:
        return pd.Series(dtype=str)
    return frame.drop_duplicates("op_key").set_index("op_key")["operator_name"]
