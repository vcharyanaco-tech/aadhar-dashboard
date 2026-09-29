"""Measure what a user waits for per interaction, using Streamlit's own runtime.

Local numbers understate the live experience because Render's free tier runs on a
throttled shared CPU. This reports both the local figure and the work that
actually happens per rerun, so the two can be compared.

    .venv/Scripts/python tools/measure_page_renders.py <path-to-aadhaar.db>
"""
import hashlib
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

DATA = Path(tempfile.mkdtemp(prefix="render-timing-"))
os_env = __import__("os").environ
os_env["APP_DATA_DIR"] = str(DATA)
for var in ("AADHAR_SYNC_URL", "AADHAR_SYNC_TOKEN"):
    os_env.pop(var, None)

import pandas as pd  # noqa: E402
import parsers  # noqa: E402

src = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "data" / "aadhaar.db"
db = DATA / "aadhaar.db"
shutil.copy2(src, db)

PW = hashlib.pbkdf2_hmac("sha256", b"adminpass", bytes.fromhex("a1b2c3"), 200_000).hex()
con = sqlite3.connect(db)
con.execute("CREATE TABLE IF NOT EXISTS users(username TEXT PRIMARY KEY, salt TEXT, pw_hash TEXT,"
            " role TEXT, active INTEGER DEFAULT 1, fails INTEGER DEFAULT 0, locked_until TEXT,"
            " created_at TEXT, must_change INTEGER DEFAULT 0)")
con.execute("INSERT OR REPLACE INTO users VALUES ('admin','a1b2c3',?,'admin',1,0,NULL,'2026-09-01',0)", (PW,))
con.commit()
con.close()

from streamlit.testing.v1 import AppTest  # noqa: E402


def timed(at, label, action=None, repeats=3):
    if action:
        action()
    else:
        at.run()
    samples = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        if action:
            action()
        else:
            at.run()
        samples.append((time.perf_counter() - t0) * 1000)
    samples.sort()
    print(f"  {label:<44} {samples[len(samples) // 2]:8.0f} ms   (min {samples[0]:.0f})")
    return samples[len(samples) // 2]


print(f"database: {src.name} ({src.stat().st_size / 1024 / 1024:.2f} MB)")
print(f"python:   {sys.version.split()[0]}")
print()
print("median of 3 full script runs, after a warm-up:")
print()

at = AppTest.from_file(str(REPO / "app.py"), default_timeout=120)
timed(at, "first load (login screen)")

at.text_input[0].set_value("admin")
at.text_input[1].set_value("adminpass")
login = timed(at, "login (form submit + rerun)", lambda: at.button[0].click().run())

timed(at, "Dashboard (default page after login)")

print("  after Dashboard render:")
print(f"    sidebar radio present : {len(at.sidebar.radio)}")
print(f"    sidebar buttons       : {[b.label for b in at.sidebar.button]}")
print(f"    sidebar expander      : {len(at.sidebar.expander)}")
print(f"    page subheaders       : {[s.value for s in at.subheader][:6]}")
print(f"    errors                : {[e.value[:60] for e in at.error]}")
# What the login page pushes over the websocket every time it renders
logo = REPO / "logo.png"
if logo.exists():
    import base64
    size = len(base64.b64encode(logo.read_bytes()))
    print()
    print(f"  login page also ships {size / 1024:.0f} KB of inlined base64 logo per render")
print()
print("Note: these are LOCAL numbers on an unthrottled CPU. The live service is a")
print("Render free instance, which runs on a shared and throttled CPU - the same")
print("work is typically several times slower there.")
shutil.rmtree(DATA, ignore_errors=True)

