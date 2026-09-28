# Haryana Circle Aadhaar Monitoring Dashboard

A Streamlit + SQLite application for tracking Aadhaar enrolment and transaction
targets across Haryana Circle divisions, sub-divisions and camps. It replaces the
earlier React/Vite SPA.

In production it is served by Streamlit on Render and proxied through the
Cloudflare Worker in the `dash-site` repository. `aadhaar.db` is mirrored to a
separate, dedicated Cloudflare Worker (`backup-worker/`) so it survives the
ephemeral filesystem that Render's free plan provides.

## Features

| Tab | Available to | Purpose |
| --- | --- | --- |
| Dashboard | everyone | Division/sub-division totals against daily targets |
| Operator Analysis | everyone | Per-operator totals, days worked, top-20 chart |
| Report | everyone | Excel/CSV exports built from uploaded data |
| Camps | everyone | Camp entry log and totals; admins can delete entries |
| Upload data | admin | Parse and import master, operator-master and transaction spreadsheets |
| Manage users | admin | Create users, bulk-create division logins, reset passwords, enable/disable, delete |
| Backup status | admin | Whether the off-site backup is actually working, plus a manual push |

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
| `AADHAR_SYNC_URL` | to enable the bridge | none | Base URL of the backup bridge. In production this is the dedicated Worker, see below. |
| `AADHAR_SYNC_TOKEN` | to enable the bridge | none | Bearer token for the bridge. Must match the Worker's `BRIDGE_TOKEN` secret. |
| `AADHAR_SYNC_INTERVAL_MS` | no | `3600000` | Periodic snapshot cadence (1 hour) |
| `AADHAR_SYNC_MIN_INTERVAL_MS` | no | `900000` | Floor between two pushes (15 min), so a burst of edits costs one push |
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
- **Restore is a manual admin action.** If `aadhaar.db` is lost, the app restores
  the newest bridge snapshot automatically at boot, but there is no in-app
  "roll back to generation X" button and no way to upload a replacement database
  from the UI. Use `python kv_sync.py restore`, or copy the file yourself, or
  re-upload the source spreadsheets. Full admin backup/restore tooling is not
  built yet.
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

`kv_sync.py` removes the first problem: a dedicated Cloudflare Worker holds a
copy in Workers KV, so a cold start or redeploy restores the database instead of
losing it. The second problem is handled in the dash-site Worker — its
`*/10 * * * *` cron pings this service's `_stcore/health` endpoint during roughly
06:00-21:00 IST and deliberately skips overnight, so the instance stays
responsive through the working day and sleeps outside it.

This needs the bridge to be configured (`AADHAR_SYNC_URL` and
`AADHAR_SYNC_TOKEN`); without it the service loses its data on every deploy. The
admin **Backup status** tab says so explicitly rather than looking healthy.

#### The bridge Worker

`backup-worker/` in this repository, deployed to its own Cloudflare account so
that nothing else can spend its write budget or delete its snapshots:

| | |
| --- | --- |
| URL | `https://aadhar-backup.aadhar-haryana.workers.dev` |
| Worker | `aadhar-backup` |
| KV namespace | `aadhar-dashboard-backups` |
| Secret | `BRIDGE_TOKEN` (never committed) |

Endpoints, all requiring `Authorization: Bearer <BRIDGE_TOKEN>` except `/health`:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Snapshot age, budget, retention. Unauthenticated, returns no database content. |
| `GET` | `/db` | Download the newest snapshot |
| `PUT` | `/db` | Push a new snapshot |
| `GET` | `/db/<generation>` | Download a specific snapshot, for rollback |
| `GET` | `/generations` | List retained snapshots |
| `GET` | `/stats` | Push accounting |
| `POST` | `/reconcile` | Rebuild the retention index from KV (rarely needed) |

Deploy or update it with:

```
cd backup-worker
npx wrangler secret put BRIDGE_TOKEN   # must match AADHAR_SYNC_TOKEN on Render
npx wrangler deploy
```

**How durability works**

- Each push writes a new `aadhar:gen:<timestamp>-<rand>` key and then moves the
  `aadhar:latest` pointer, so an interrupted push can never corrupt the snapshot a
  restore would use. The last `RETAIN` (12) generations are kept, which at ~5.6 MB
  is roughly 67 MB — far inside the 1 GB free-tier allowance.
- The **daily push budget is enforced in the Worker**, not in the app. The old
  client counted writes in a module-level dict that reset on every Render
  restart, so its 400/day cap was never actually enforced. The Worker refuses
  pushes past `DAILY_PUSH_BUDGET` with `429` and an explicit reset time, and
  **restores keep working even when the budget is exhausted** — a spent budget can
  never block recovery.
- A push costs 3 KV writes (generation + pointer + stats). With the 15-minute
  floor that is at most ~96 pushes/day, about 288 writes, comfortably inside the
  1,000/day free allowance.
- Every write in the app calls `kv_sync.request_backup()`, which is debounced, so
  a burst of edits coalesces into a single push. A change made shortly before a
  deploy is captured by a final push on `SIGTERM` and on process exit.
- **KV is eventually consistent.** A cold start within ~60s of a push may restore
  the previous generation rather than the newest. That is the safe direction —
  never a partial write — and the Backup status tab reports the age of the
  newest snapshot, so a stale restore is visible.

**Verifying it by hand**

```
python kv_sync.py status        # health, budget, last error, snapshot age
python kv_sync.py backup        # force a push
python kv_sync.py generations   # what the bridge is holding
python kv_sync.py restore       # pull the newest snapshot into DATA_DIR
```

The same figures are in the admin **Backup status** tab. That tab exists because
the previous bridge failed silently: every restore and every push returned `404`,
the only trace was one line in the boot log, and the app looked healthy while
holding no backups at all.

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
