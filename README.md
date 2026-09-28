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

`ADMIN_USERNAME` and `ADMIN_PASSWORD` are only read when the `users` table has no
administrator. If the database already has an admin account they are ignored.
When they are absent and no admin exists, the app shows a configuration error and
refuses to start rather than falling back to a default password.

Passwords are stored as PBKDF2-SHA256 hashes with a per-user salt. Login locks an
account for 5 minutes after 5 failed attempts.

## Data

The database is `aadhaar.db` inside `APP_DATA_DIR`. It is gitignored and must
never be committed.

To move a database between machines, use the admin **backup / restore** tools in
the Manage users tab rather than copying the file. Restore validates the upload
(50 MB limit, `PRAGMA quick_check`, required tables and columns, and rejection of
views, triggers and virtual tables) before it replaces the live database.

Uploaded source spreadsheets are parsed by `parse_master` and `parse_tx`, which
match columns by name rather than position, so column order in the workbook does
not matter.

## Deployment

`render.yaml` defines a Blueprint: a Python web service on the free plan in
`oregon`, matching the existing `dash-site` service.

```
buildCommand: pip install -r requirements.txt
startCommand: streamlit run app.py --server.address 0.0.0.0 --server.port $PORT
```

Apply the Blueprint in Render and supply `ADMIN_USERNAME` and `ADMIN_PASSWORD`
when prompted. Render prompts for them rather than taking values from the
repository, so the credentials are never committed.

### The database does not persist on the free plan

This service has **no persistent disk**, because Render only offers disks on paid
plans. `aadhaar.db` therefore lives on Render's ephemeral filesystem and is
**deleted on every deploy and every restart**. A free instance also spins down
after a period of inactivity.

Consequences to be aware of:

- Any uploaded master, transaction, camp and user data is lost on each deploy.
- To rebuild state, re-upload the source spreadsheets after each deploy.
- The admin backup/restore tab can export a copy of the database, but a copy is
  only useful if you download it somewhere durable.

If the data needs to survive, either attach a disk (requires a paid plan) or
switch `APP_DATA_DIR` to an external location.

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
