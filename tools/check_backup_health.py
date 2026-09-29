"""One-command backup health check for the Aadhaar dashboard.

Answers the only question that matters after a deploy: is the database actually
being mirrored off Render's ephemeral disk, and when was the last good copy?

    .venv/Scripts/python tools/check_backup_health.py

Needs the bridge token. It is read from AADHAR_SYNC_TOKEN, or passed as the
first argument. Nothing is written or pushed - this only reads.

Exit codes: 0 healthy, 1 unhealthy, 2 could not determine.
"""
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "https://aadhar-backup.aadhar-haryana.workers.dev"
UA = "Mozilla/5.0 (compatible; aadhar-dashboard-healthcheck/1.0)"


def get(url, token, timeout=30, expect_json=True):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token, "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body) if expect_json else body


def main(argv):
    token = argv[1] if len(argv) > 1 else os.environ.get("AADHAR_SYNC_TOKEN", "")
    base = (argv[2] if len(argv) > 2 else DEFAULT_URL).rstrip("/")
    if not token:
        print("No token. Pass it as the first argument or set AADHAR_SYNC_TOKEN.")
        return 2

    # Probe an authenticated route first. /health is deliberately unauthenticated
    # so a human can check staleness without the secret, which means it says
    # nothing about whether the token is right. Reading /db proves the token.
    try:
        get(base + "/db", token, expect_json=False)
        status_note = "accepted"
    except urllib.error.HTTPError as err:
        if err.code == 401:
            print("FAIL  the bridge rejected this token (HTTP 401).")
            print("      AADHAR_SYNC_TOKEN does not match the Worker's BRIDGE_TOKEN secret.")
            print("      This is the exact failure that leaves the app looking healthy")
            print("      while every push and restore is rejected.")
            return 2
        if err.code == 404:
            status_note = "accepted (no snapshot stored yet)"
        else:
            print(f"FAIL  bridge returned HTTP {err.code} on /db")
            return 2
    except Exception as err:
        print(f"FAIL  bridge unreachable: {err}")
        return 2

    try:
        health = get(base + "/health", token)
    except Exception as err:
        print(f"FAIL  bridge unreachable: {err}")
        return 2

    if not health.get("configured"):
        print("FAIL  the Worker has no BRIDGE_TOKEN secret set.")
        return 1

    latest = health.get("latest")
    age = health.get("ageSeconds")
    print(f"  service        : {health.get('service')}")
    print(f"  token          : {status_note}")
    print(f"  newest snapshot: {latest or 'NONE STORED'}")
    if age is not None:
        print(f"  age            : {age // 3600}h {age % 3600 // 60}m")
    print(f"  pushes today   : {health.get('pushesToday')}/{health.get('dailyPushBudget')}")
    print(f"  generations    : {health.get('trackedGenerations')} kept, retain={health.get('retain')}")

    if not latest:
        print()
        print("UNHEALTHY  nothing is backed up. The database exists only on Render's")
        print("          ephemeral disk and will be lost on the next deploy or restart.")
        print("          Log in and upload data - every write triggers a push.")
        print()
        print("Legacy path: dashboardharyana.site/api/backup/aadhaar-db still exists in the")
        print("dash-site Worker. It is stale, not live, and is not where backups go now.")
        return 1

    if age is not None and age > 2 * 3600:
        print()
        print(f"UNHEALTHY  the newest snapshot is {age // 3600}h old, past two push intervals.")
        return 1

    print()
    print("HEALTHY  the database is mirrored off Render's ephemeral disk.")
    print()
    print("Legacy path: dashboardharyana.site/api/backup/aadhaar-db still exists in the")
    print("dash-site Worker and still holds the September snapshot. Nothing writes to or")
    print("reads from it now - the service is pointed at this bridge - so it is a stale")
    print("copy, not a live backup. Retiring it needs a change in the dash-site repo")
    print("(deployed by that project's own account).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
