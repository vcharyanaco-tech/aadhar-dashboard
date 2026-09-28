# Haryana Circle Aadhaar Monitoring Dashboard

A Streamlit + SQLite application for tracking Aadhaar enrolment and transaction
targets across Haryana Circle divisions, sub-divisions and camps. It replaces the
earlier React/Vite SPA.

The app is single-file (`app.py`) with a local SQLite database. It is served
locally with Streamlit, and in production is proxied through the Cloudflare
Worker in the `dash-site` repository from an origin hosted on Render.

## Features

| Tab | Available to | Purpose |
| --- | --- | --- |
| Dashboard | everyone | Division/sub-division totals against daily targets |
| Report | everyone | Excel exports built from uploaded data |
| Camps | everyone | Camp entry log and totals |
| Upload data | admin | Parse and import master and transaction spreadsheets |
| Manage users | admin | Create users, bulk-create division users, reset passwords, and perform database backup/restore |

## Requirements

- Python 3.13 or newer
- The pinned dependencies in `requirements.txt`

## Running locally

```
pip install -r requirements.txt
python -m streamlit run app.py
```

On Windows you can double-click `run_app.bat`, which does the same thing and
pauses on exit so you can read any error.

The app opens at <http://localhost:8501>. When Streamlit sits behind the
`/aadhar-dashboard` base path (see below), the local URL is
<http://localhost:8501/aadhar-dashboard>.

## Configuration

All configuration is via environment variables. There are no code changes and no
committed secrets.

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `APP_DATA_DIR` | no | `./data` | Directory holding `aadhaar.db`. Point this at a disk mount if one is attached. |
| `ADMIN_USERNAME` | on a fresh database | none | Username for the first administrator |
| `ADMIN_PASSWORD` | on a fresh database | none | Password for the first administrator |
| `AADHAR_SYNC_URL` | to enable the bridge | none | Base URL of the Worker bridge, e.g. `https://dashboardharyana.site/api/backup` |
| `AADHAR_SYNC_TOKEN` | to enable the bridge | none | Bearer token for the bridge. Scoped to this app's single endpoint only. |
| `AADHAR_SYNC_INTERVAL_MS` | no | `600000` | Snapshot cadence (10 minutes) |
| `AADHAR_SYNC_WRITE_BUDGET` | no | `400` | Rolling daily cap on KV writes, so backups degrade rather than exhaust the Workers free tier |
| `AADHAR_SYNC_MAX_BYTES` | no | `20971520` | Refuse to push a snapshot above this size (20 MiB; Workers KV caps a value at 25) |

`ADMIN_USERNAME` and `ADMIN_PASSWORD` are only read when the `users` table has no
administrator. If the database already has an admin account they are ignored.
When they are absent and no admin exists, the app shows a configuration error and
refuses to start rather than falling back to a default password.

Passwords are stored as PBKDF2-SHA256 hashes with a per-user salt. Login locks an
account for 5 minutes after 5 failed attempts.

## Data

The database is `aadhaar.db` inside `APP_DATA_DIR`. It is gitignored and must
never be committed.

On a host with an ephemeral filesystem — Render's free plan has no disk — the
database is mirrored to a Cloudflare Worker by `kv_sync.py`, which follows the
same approach the Node service in `dash-site` uses
(`src/server/data-sync.js`). `boot.py` restores the latest snapshot before
Streamlit opens the database, and the app pushes a fresh snapshot after each
write and on an interval. The Aadhaar service keeps a single KV key,
`backup:aadhaar.sqlite`, so it never collides with the Node service's keys.

The bridge is off unless both `AADHAR_SYNC_URL` and `AADHAR_SYNC_TOKEN` are set,
so local development is unaffected. A snapshot is validated with
`PRAGMA quick_check` and a required-table check before it is written over
anything, and a restore never overwrites a database that already exists. If the
bridge is unreachable the app still starts and logs the failure, degrading to the
previous ephemeral behaviour rather than boot-looping.

Uploaded source spreadsheets are parsed by `parse_master` and `parse_tx`, which
match columns by name rather than position, so column order in the workbook does
not matter. Uploads are parsed in memory, so the database is the only state the
bridge needs to mirror.

To move a database between machines by hand, use the admin **backup / restore**
tools in the Manage users tab rather than copying the file. Restore validates the
upload (50 MB limit, `PRAGMA quick_check`, required tables and columns, and
rejection of views, triggers and virtual tables) before it replaces the live
database.

## Deployment

`render.yaml` defines a Blueprint: a Python web service on the free plan in
`oregon`, matching the existing `dash-site` service.

```
buildCommand: pip install -r requirements.txt
startCommand: python boot.py
```

Apply the Blueprint in Render and supply `ADMIN_USERNAME` and `ADMIN_PASSWORD`
when prompted. Render prompts for them rather than taking values from the
repository, so the credentials are never committed. `AADHAR_SYNC_TOKEN` is
prompted for in the same way.

### Surviving the free plan's lack of a disk

This service has **no persistent disk**, because Render only offers disks on paid
plans. `aadhaar.db` therefore lives on Render's ephemeral filesystem and is
**deleted on every deploy and every restart**. A free instance also spins down
after a period of inactivity.

`kv_sync.py` removes the first problem: the Worker holds a copy in Workers KV, so
a cold start or redeploy restores the database instead of losing it. The second
problem is handled in the Worker — its `*/10 * * * *` cron pings this service's
`_stcore/health` endpoint during roughly 06:00-21:00 IST and deliberately skips
overnight, so the instance stays responsive through the working day and sleeps
outside it.

This needs the bridge to be configured (`AADHAR_SYNC_URL` and
`AADHAR_SYNC_TOKEN`); without it the service behaves exactly as before and loses
its data on every deploy.

The current database is about 5.6 MB. Workers KV caps a single value at 25 MiB, so
`AADHAR_SYNC_MAX_BYTES` refuses to push anything larger rather than failing the
write. Backups are also capped at 400 KV writes per UTC day. If the database ever
approaches the size limit, attach a paid disk and set `APP_DATA_DIR` to it, and
the bridge steps aside automatically.

`.streamlit/config.toml` configures Streamlit for the reverse proxy: base path
`aadhar-dashboard`, XSRF and CORS enabled, the two public origins allowlisted, a
200 MB upload limit, and telemetry disabled.

The Cloudflare Worker in `dash-site` proxies `/aadhar-dashboard/*` to the Render
origin, which it reads from the `AADHAR_ORIGIN` secret. It forwards WebSocket
upgrades, 308-redirects the old `/aadhar.html` and
`/aadhar-dashboard/index.html` paths to the canonical `/aadhar-dashboard/`, and
returns 503 while `AADHAR_ORIGIN` is unset.

## Notes

- The logo is read from `logo.png` (or `.jpg`/`.jpeg`/`.webp`) next to `app.py`.
- `data/`, `.venv/` and Python caches are gitignored.
