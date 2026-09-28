# Haryana Circle Aadhaar Monitoring Dashboard

A Streamlit + SQLite application for tracking Aadhaar enrolment and transaction
targets across Haryana Circle divisions, sub-divisions and camps. It replaces the
earlier React/Vite SPA.

In production it is served by Streamlit on Render and proxied through the
Cloudflare Worker in the `dash-site` repository.

## Features

| Tab | Available to | Purpose |
| --- | --- | --- |
| Dashboard | everyone | Division/sub-division totals against daily targets |
| Operator Analysis | everyone | Per-operator totals, days worked, top-20 chart |
| Report | everyone | Excel/CSV exports built from uploaded data |
| Camps | everyone | Camp entry log and totals; admins can delete entries |
| Upload data | admin | Parse and import master, operator-master and transaction spreadsheets |
| Manage users | admin | Create users, bulk-create division logins, reset passwords, enable/disable, delete |

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

## First run

The first time the app starts with an empty database there are no user accounts,
so it shows a **First-time setup** screen and serves nothing else until an
administrator is created. That account is the only admin, and it is created
through the UI — there are no admin credentials in code or in the repository.

After that, every visit goes through the login screen. A user's first login is
forced through a change-password screen, which is how both newly created users
and bulk-created division logins are onboarded.

## Configuration

All configuration is via environment variables. There are no code changes and no
committed secrets.

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `APP_DATA_DIR` | no | `./data` | Directory holding `aadhaar.db`. Point this at a disk mount if one is attached. |
| `PORT` | on Render | `8501` | Port Streamlit binds to. Render sets this. |
| `AADHAR_SYNC_URL` | to enable the bridge | none | Base URL of the Worker bridge, e.g. `https://dashboardharyana.site/api/backup` |
| `AADHAR_SYNC_TOKEN` | to enable the bridge | none | Bearer token for the bridge. Scoped to this app's single endpoint only. |
| `AADHAR_SYNC_INTERVAL_MS` | no | `600000` | Snapshot cadence (10 minutes) |
| `AADHAR_SYNC_WRITE_BUDGET` | no | `400` | Rolling daily cap on KV writes, so backups degrade rather than exhaust the Workers free tier |
| `AADHAR_SYNC_MAX_BYTES` | no | `20971520` | Refuse to push a snapshot above this size (20 MiB; Workers KV caps a value at 25) |

The bridge is off unless both `AADHAR_SYNC_URL` and `AADHAR_SYNC_TOKEN` are set,
so local development is unaffected.

### Known gaps

These are documented here rather than silently left out, because they are
things a reader of this file would otherwise assume work.

- **The default temporary password is a hardcoded constant.** `DEFAULT_TEMP_PASSWORD`
  in `app.py` is the shared initial password for every login created by *Bulk create
  Division logins*. It lives in version control. Users are forced to change it on
  first login, but the constant should be replaced with a per-batch random password
  before this is relied on for anything sensitive.
- **Restore is a documented-but-unimplemented safety net.** If `aadhaar.db` is lost
  and the bridge has no usable snapshot, there is currently no in-app way to get
  the data back. Copy the file yourself, or re-upload the source spreadsheets.
  The admin backup/restore tooling is not built yet.
- **`render.yaml` still declares `ADMIN_USERNAME` and `ADMIN_PASSWORD`.** No code
  reads them. They are harmless but misleading; the admin is created through the
  first-run screen instead. They can be removed.
- **The upload size limit depends on how the app is started.** `boot.py` passes
  `--server.maxUploadSize=50`, and command-line flags take precedence over
  `.streamlit/config.toml`, so **on Render the limit is 50 MB**. Started directly
  (`run_app.bat`, `streamlit run app.py`) the `config.toml` value applies and the
  limit is 200 MB. The two values disagree; pick one.

## Data

The database is `aadhaar.db` inside `APP_DATA_DIR`. It is gitignored and must
never be committed.

Tables: `users`, `uploads`, `camps` are created at startup by `init_db()`.
`master`, `operator_master` and `tx` appear once the matching sheet is uploaded,
which is why `kv_sync.REQUIRED_TABLES` only insists on the first three.

Uploaded source spreadsheets are parsed in memory, so the database is the only
state the bridge needs to mirror. `parse_master` and `parse_tx` match columns by
name rather than position, so column order in the workbook does not matter.

### Re-uploading the master is destructive

`save_master` uses `if_exists="replace"`, so a new master sheet **overwrites the
whole master table**. Nothing checks that the new master still contains the
station keys already referenced by `tx`. If the replacement uses different station
IDs, every historical transaction row silently stops matching: the rows remain in
the database but disappear from the dashboard and the report, and they will not
appear in the "not in master" list either, because that list only covers the
selected period. Back up the database before replacing the master.

The same `replace` behaviour applies to the operator master. Transaction sheets
are **appended**, never replaced, and are keyed by `upload_id`; deleting an upload
in the Upload data tab removes its rows from `tx` as well.

### No indexes

`tx` has no index on `upload_id` or `key`, which are the two columns every
report query filters and joins on. `init_db()` should add them; as `tx` grows
this becomes a full table scan on each page load.

## Deployment

`render.yaml` defines a Blueprint: a Python web service on the free plan in
`oregon`, matching the existing `dash-site` service.

```
buildCommand: pip install -r requirements.txt
startCommand: python boot.py
```

Apply the Blueprint in Render and supply `AADHAR_SYNC_TOKEN` when prompted. Render
prompts for it rather than taking the value from the repository, so it is never
committed. Create the admin account through the first-run screen after the first
deploy.

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

The bridge restores only when the local file is **absent**, and a snapshot is
validated with `PRAGMA quick_check` plus a required-table check before it is
written over anything, so a corrupt download never replaces a live database. If
the bridge is unreachable the app still starts and logs the failure, degrading to
the previous ephemeral behaviour rather than boot-looping.

**Backups can fail silently.** The daily write budget is cumulative, resets at
midnight UTC, and is shared with the Node service in `dash-site` because they use
the same Worker. When the budget is exhausted `backup_data()` returns without
pushing and without raising; the next restart then restores whatever the last
successful snapshot was, so recent uploads can appear to have vanished. The
current status is readable with:

```
python kv_sync.py status
```

and `kv_sync.get_status()` exposes the same values. There is no admin UI for this
yet. Given the database is roughly 5.6 MB and the instance is a single service,
pushing a full snapshot after every write against a 400/day budget is harder than
it needs to be; a slower cadence would be safer.

`.streamlit/config.toml` configures Streamlit for the reverse proxy: base path
`aadhar-dashboard`, XSRF and CORS enabled, the two public origins allowlisted, and
telemetry disabled. Note that `boot.py` overrides the CORS, XSRF and upload-size
settings on the command line, so the effective values in production come from
`boot.py`, not from this file.

The Cloudflare Worker in `dash-site` proxies `/aadhar-dashboard/*` to the Render
origin, which it reads from the `AADHAR_ORIGIN` secret. It forwards WebSocket
upgrades, 308-redirects the old `/aadhar.html` and
`/aadhar-dashboard/index.html` paths to the canonical `/aadhar-dashboard/`, and
returns 503 while `AADHAR_ORIGIN` is unset.

## Security notes

- Passwords are stored as PBKDF2-SHA256 hashes with a per-user salt, 200,000
  rounds, compared with `hmac.compare_digest`.
- Login locks an account for 5 minutes after 5 failed attempts.
- Usernames are restricted to 3-30 characters of lowercase letters, digits, dot,
  dash or underscore, so they are safe to use in URLs and filenames.
- Uploaded spreadsheets are parsed with pandas; a file that fails to parse is
  rejected with the list of headers it actually contained, which is the main
  defence against a mis-mapped upload silently corrupting the numbers.
- `ensure_cols()` in `app.py` builds `PRAGMA table_info` and `ALTER TABLE` with
  f-string interpolation. It is only ever called with hardcoded table and column
  names today, so it is not reachable, but it should not stay in that shape
  alongside an otherwise parameterised codebase.

## Notes

- The logo is read from `logo.png` (or `.jpg`/`.jpeg`/`.webp`) next to `app.py`.
- `data/`, `.venv/` and Python caches are gitignored.
- `app.py` is the whole application: database helpers, authentication, the three
  spreadsheet parsers and all six screens. The parsers are pure functions over
  DataFrames and are the obvious thing to split out and cover with tests; there
  are no tests in the repository today.
