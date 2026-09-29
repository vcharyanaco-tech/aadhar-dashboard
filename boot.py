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
    # The restore result is printed explicitly. It used to be discarded, so a
    # boot that failed to restore looked exactly like a healthy one in the logs,
    # and the app then pushed the empty database it had just created over the
    # only good copy. A restore that does not succeed now says so on stdout.
    result = kv_sync.restore_data()
    if result.get("restored"):
        print(f"[boot] restored aadhaar.db from the bridge "
              f"({result.get('bytes')} bytes, generation {result.get('generation')})", flush=True)
    elif kv_sync.enabled():
        print(f"[boot] WARNING: aadhaar.db was NOT restored ({result.get('reason')}). "
              f"The app will start on an empty database and kv_sync will refuse to "
              f"push it, so the stored backup is safe. Do not enter real data until "
              f"this is fixed.", flush=True)
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
        # Left off, matching .streamlit/config.toml. See the note there: enabling
        # CORS with an origin allowlist was tried and broke the page while
        # /health still returned 200, so this stays as it was.
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
