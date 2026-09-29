"""Time the Streamlit script run the way a user experiences it.

Streamlit re-executes the whole script on every interaction, so this measures
what the user waits for on each click, not just first load.

    .venv/Scripts/python tools/measure_renders.py <path-to-aadhaar.db>
"""
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import parsers  # noqa: E402


def time_it(label, fn, repeats=5):
    fn()  # warm
    samples = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000)
    samples.sort()
    mid = samples[len(samples) // 2]
    print(f"  {label:<46} {mid:8.1f} ms   (min {samples[0]:.1f}, max {samples[-1]:.1f})")
    return mid


def main(argv):
    db = Path(argv[1]) if len(argv) > 1 else REPO / "data" / "aadhaar.db"
    if not db.exists():
        print(f"No database at {db}")
        return 2
    print(f"database: {db} ({db.stat().st_size / 1024 / 1024:.2f} MB)")
    print()
    print("median of 5 runs, after a warm-up:")
    print()

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    ids = tuple(r[0] for r in con.execute("SELECT id FROM uploads"))
    labels = [r[0] for r in con.execute("SELECT label FROM uploads")]

    # --- what the app does on EVERY script run, before any page renders
    def init_db_work():
        c = sqlite3.connect(db)
        try:
            c.execute("CREATE TABLE IF NOT EXISTS users(username TEXT PRIMARY KEY, salt TEXT,"
                      " pw_hash TEXT, role TEXT, active INTEGER, fails INTEGER,"
                      " locked_until TEXT, created_at TEXT, must_change INTEGER)")
            c.execute("CREATE TABLE IF NOT EXISTS uploads(id INTEGER PRIMARY KEY AUTOINCREMENT,"
                      " label TEXT, uploaded_by TEXT, uploaded_at TEXT)")
            c.execute("CREATE TABLE IF NOT EXISTS camps(id INTEGER PRIMARY KEY AUTOINCREMENT,"
                      " camp_date TEXT, division TEXT, sub_division TEXT, location TEXT,"
                      " transactions INTEGER, remarks TEXT, created_by TEXT, created_at TEXT)")
            c.execute("CREATE TABLE IF NOT EXISTS division_targets(division TEXT PRIMARY KEY,"
                      " daily_target INTEGER NOT NULL, updated_by TEXT, updated_at TEXT)")
            c.execute("CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT,"
                      " at TEXT, username TEXT, action TEXT, detail TEXT)")
            c.execute("PRAGMA table_info('users')").fetchall()
            for idx in ("idx_tx_upload_id", "idx_tx_key", "idx_tx_key_upload", "idx_audit_at"):
                c.execute(f"CREATE INDEX IF NOT EXISTS {idx} ON tx (key)")
            c.execute("SELECT 1 FROM division_targets LIMIT 1").fetchone()
            c.commit()
        finally:
            c.close()

    time_it("init_db() equivalent (every rerun)", init_db_work)

    # --- the work behind each page
    time_it("build_period() - all 7 uploads", lambda: parsers.aggregate_period(con, ids))
    time_it("build_period() - 1 upload", lambda: parsers.aggregate_period(con, ids[:1]))
    time_it("operator_names() - 1,355 rows", lambda: parsers.operator_names(con))

    def target_work():
        agg, _, _, _ = parsers.aggregate_period(con, ids)
        frame = agg.groupby('division')['total'].sum().reset_index().rename(columns={'division':'Division','total':'Total'})
        parsers.target_table(frame, 6, {parsers.norm(k): v
                                        for k, v in parsers.DIVISION_DAILY_TARGETS.items()})

    time_it("dashboard target-vs-achievement", target_work)

    # --- auth cost
    import hashlib
    salt = bytes.fromhex("a1b2c3")
    time_it("PBKDF2 200k rounds (one login attempt)",
            lambda: hashlib.pbkdf2_hmac("sha256", b"password", salt, 200_000), repeats=3)

    # --- the logo, inlined into every login render
    logo = REPO / "logo.png"
    if logo.exists():
        import base64
        raw = logo.read_bytes()

        def inline_logo():
            base64.b64encode(raw).decode()

        time_it("logo base64-encode (every login render)", inline_logo)
        print(f"       ...and pushes {len(raw) * 4 / 3 / 1024:.0f} KB of it over the websocket each time")

    # --- a full report render
    def report_render():
        agg, _, _, ops = parsers.aggregate_period(con, ids)
        for frame, cols in ((agg, None), (ops, None)):
            if frame.empty:
                continue
            for c in frame.columns:
                if c.endswith("total") or c in ("enr", "upd", "mbu", "demo", "nonmbu"):
                    frame.groupby("division")[c].sum()
        if not ops.empty:
            ops.groupby(["division", "sub_division"])["total"].sum()

    time_it("report: aggregate + group for exports", report_render)

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
