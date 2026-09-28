"""Entry point for hosts with an ephemeral filesystem.

Restores `aadhaar.db` from the Cloudflare Worker bridge when the local file is
missing, then hands off to Streamlit. Restore has to happen before the app
opens the database, which rules out doing it inside `app.py`.

Exit codes follow the usual convention: any failure to reach the bridge is
logged and the app still starts, so a bridge outage degrades to the previous
ephemeral behaviour instead of a boot loop.
"""

import os
import subprocess
import sys

import kv_sync

PORT = os.environ.get("PORT", "8501")


def main():
    kv_sync.restore_data()
    kv_sync.start_interval()
    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        "app.py",
        "--server.address=0.0.0.0",
        f"--server.port={PORT}",
        "--server.headless=true",
        "--server.enableCORS=false",
        "--server.enableXsrfProtection=true",
        "--server.maxUploadSize=15",
        "--browser.gatherUsageStats=false",
    ]
    print(f"[boot] starting: {' '.join(cmd)}", flush=True)
    # Forward signals so Render's SIGTERM reaches Streamlit and the container
    # stops promptly instead of waiting out the grace period.
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
