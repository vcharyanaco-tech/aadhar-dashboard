"""One-off data-health report for the recovered production database.

Not part of the test suite: it inspects a real snapshot rather than a fixture.
Usage:

    .venv/Scripts/python tools/report_data_health.py <path-to-aadhaar.db>
"""
import sqlite3
import sys
from pathlib import Path

BLANK = "''"


def main(argv):
    path = argv[1] if len(argv) > 1 else "data/aadhaar.db"
    db = Path(path)
    if not db.exists():
        print(f"No such database: {db}")
        return 2
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    q = lambda sql: con.execute(sql).fetchone()[0]

    print(f"database: {db}  ({db.stat().st_size:,} bytes)")
    print(f"integrity: {q('PRAGMA quick_check')}")
    print()

    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    print("tables and row counts")
    for t in ("users", "uploads", "master", "tx", "camps", "operator_master"):
        if t in tables:
            print(f"  {t:18} {q(f'SELECT COUNT(*) FROM {t}'):>8,}")
    print()

    # -- referential integrity: the failure mode that hides history silently
    if {"tx", "master"} <= tables:
        orphan = q("SELECT COUNT(*) FROM tx WHERE key NOT IN (SELECT key FROM master)")
        no_tx = q("SELECT COUNT(*) FROM master WHERE key NOT IN (SELECT key FROM tx)")
        print("referential integrity")
        print(f"  tx rows with no master match : {orphan:,}  <- invisible in dashboard/report")
        print(f"  master stations with no tx   : {no_tx:,}")
        dupes = q("SELECT COUNT(*) FROM (SELECT key FROM master GROUP BY key HAVING COUNT(*)>1)")
        print(f"  duplicate master keys        : {dupes:,}")
        print()

    if "uploads" in tables:
        print("per-upload coverage")
        for r in con.execute(
            "SELECT u.id, u.label, COUNT(t.rowid) rows, COUNT(DISTINCT t.key) stations "
            "FROM uploads u LEFT JOIN tx t ON t.upload_id=u.id GROUP BY u.id ORDER BY u.id"
        ):
            print(f"  {r['label'] or '(no label)':14} rows={r['rows']:>5} stations={r['stations']:>4}")
        orphan_uploads = q("SELECT COUNT(*) FROM uploads u WHERE NOT EXISTS "
                           "(SELECT 1 FROM tx t WHERE t.upload_id=u.id)")
        print(f"  uploads with no transaction rows: {orphan_uploads:,}")
        print()

    if "tx" in tables:
        print("value sanity (negative or null counts)")
        for col in ("enr", "upd", "mbu", "demo", "nonmbu"):
            try:
                neg = q(f"SELECT COUNT(*) FROM tx WHERE {col} < 0")
                nul = q(f"SELECT COUNT(*) FROM tx WHERE {col} IS NULL")
                print(f"  {col:8} negative={neg:>5}  null={nul:>5}")
            except sqlite3.Error:
                print(f"  {col:8} (column not present)")
        print()
        # The upd = total - new derivation can clamp to zero; count how often,
        # because a clamped row means the sheet's total disagreed with its own
        # new-enrolment count and the figures are worth a look.
        print("  rows where upd was clamped to 0 despite enr > 0: "
              f"{q('SELECT COUNT(*) FROM tx WHERE upd = 0 AND enr > 0'):,}")
        print()

    if "master" in tables:
        print("master sheet quality")
        print(f"  distinct divisions     : {q(f'SELECT COUNT(DISTINCT division) FROM master WHERE division <> {BLANK}')}")
        print(f"  blank division         : {q(f'SELECT COUNT(*) FROM master WHERE division IS NULL OR division = {BLANK}')}")
        print(f"  blank sub_division     : {q(f'SELECT COUNT(*) FROM master WHERE sub_division IS NULL OR sub_division = {BLANK}')}")
        print(f"  blank station key      : {q(f'SELECT COUNT(*) FROM master WHERE key IS NULL OR key = {BLANK}')}")
        print()

    if "users" in tables:
        print("users")
        for r in con.execute("SELECT username, role, active, must_change, created_at, fails, locked_until "
                             "FROM users ORDER BY role, username"):
            flags = []
            if not r["active"]:
                flags.append("DISABLED")
            if r["must_change"]:
                flags.append("must-change-password")
            if r["fails"]:
                flags.append(f"fails={r['fails']}")
            if r["locked_until"]:
                flags.append(f"locked_until={r['locked_until']}")
            print(f"  {r['username']:16} {r['role']:6} created={str(r['created_at'])[:10]:10} "
                  f"{' '.join(flags)}")
        print()

    if "operator_master" in tables:
        print("operator master")
        print(f"  operators                : {q('SELECT COUNT(*) FROM operator_master'):,}")
        print(f"  blank names              : {q(f'SELECT COUNT(*) FROM operator_master WHERE operator_name IS NULL OR TRIM(operator_name) = {BLANK}')}")
        if "tx" in tables:
            unlinked = q("SELECT COUNT(DISTINCT t.operator) FROM tx t WHERE t.operator IS NOT NULL "
                         f"AND t.operator <> {BLANK} AND norm IS NOT NULL") if False else None
            # operators present in tx but absent from the operator master
            missing = con.execute(
                "SELECT COUNT(*) FROM (SELECT DISTINCT t.operator FROM tx t "
                "WHERE t.operator IS NOT NULL AND TRIM(t.operator) <> '' "
                "AND NOT EXISTS (SELECT 1 FROM operator_master o "
                "WHERE LTRIM(CAST(o.operator_id AS TEXT), '0') = "
                "LTRIM(CAST(t.operator AS TEXT), '0')))").fetchone()[0]
            print(f"  operators in tx not in master: {missing:,}  <- shown with a blank name")
        print()

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
