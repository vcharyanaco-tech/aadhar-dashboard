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
| `AADHAR_DEFAULT_TEMP_PASSWORD` | no | random per batch | Shared initial password for bulk-created division logins. Unset means a random password is generated for each batch and shown once. |
| `SESSION_TIMEOUT_MINUTES` | no | `30` | Idle logout. `0` disables it. Streamlit reruns on every interaction, so this is a reliable activity signal. |
| `AADHAR_NON_WORKING_WEEKDAYS` | no | `6` | Weekdays excluded from the target, as Python weekday indices (Monday=0 … Sunday=6). Sundays only by default; Indian post offices do not close on the second Saturday. |

The bridge is off unless both `AADHAR_SYNC_URL` and `AADHAR_SYNC_TOKEN` are set,
so local development is unaffected.

### Known gaps

These are documented here rather than silently left out, because they are
things a reader of this file would otherwise assume work.

- **The old default password is still recoverable from git history.** The shared
  default password used to be a hardcoded constant in `app.py` and is present in
  commits up to `7be9c86` — including in this file's own history, since naming it
  here would defeat the point. It now comes from `AADHAR_DEFAULT_TEMP_PASSWORD`,
  so it is no longer added to new commits, but only rewriting history removes it
  from the old ones. Anyone with read access to the repository still has it;
  rotating it or rewriting history is a separate decision.
- **The default password is shared across every division login.** It is generated
  per batch when the env var is unset, but if it is set then every division gets
  the same one. Users are forced to change it on first login.
- **Restore is a manual admin action.** If `aadhaar.db` is lost, the app restores
  the newest bridge snapshot automatically at boot. To go back deliberately, the
  admin **Backup status** tab lists every retained generation and offers a
  rollback, gated behind typing `RESTORE`; the current file is kept as
  `aadhaar.db.pre-rollback` first. There is still no way to upload a replacement
  database from the UI — use `python kv_sync.py restore`, copy the file yourself,
  or re-upload the source spreadsheets.
- **Division logins are not scoped — by design.** Bulk creation makes one login
  per division, but no query filters by the logged-in user's division, so every
  logged-in user sees all eleven divisions in the dashboard, the reports and the
  operator analysis. The per-division credentials identify who is asking and give
  a division its own `must_change` password rotation; they are **not** an
  access-control boundary. `role` decides which *pages* load, never which *rows*
  are returned. `tests/test_screens.py` pins this, so if scoping is ever wanted it
  has to be written deliberately rather than assumed to exist already.
- **There is no row-level access control at all.** Every non-admin sees the same
  figures.
- **The public URL is `https://dashboardharyana.site/aadhar-dashboard/`.** It has
  not changed. The dash-site Worker proxies that prefix to the Render origin
  (`aadhar-dashboard-5i4x.onrender.com`, a `AADHAR_ORIGIN` secret) and forwards
  WebSocket upgrades, redirecting the old `/aadhar.html` and
  `/aadhar-dashboard/index.html` paths to the canonical form. The proxy adds
  `X-Frame-Options: SAMEORIGIN`, `frame-ancestors 'self'`, `nosniff` and a
  `Referrer-Policy`, and strips the origin's `Access-Control-Allow-Origin: *`.
  The origin URL behind it is not something users should be given.
- **The origin itself is unprotected and reachable.** Going straight to
  `aadhar-dashboard-5i4x.onrender.com` returns no security headers at all and
  `Access-Control-Allow-Origin: *`. Render's free plan has no IP allowlist, so
  the proxy is a hardening layer rather than a boundary. `proxy-worker/` adds a
  stricter header set but is redundant while the dash-site proxy is in place.
- **`AADHAR_SYNC_TOKEN` must match the Worker's `BRIDGE_TOKEN`.** If it does not,
  every push and every restore is rejected with 401 while the app itself looks
  perfectly healthy, and nothing is backed up. That is not hypothetical: the
  service ran with a mismatched token and its backup namespace stayed empty. Run
  `tools/check_backup_health.py` after any change to either side — it probes an
  authenticated route, because `/health` is deliberately unauthenticated and will
  happily report healthy while your token is wrong.

## Data

The database is `aadhaar.db` inside `APP_DATA_DIR`. It is gitignored and must
never be committed.

Tables: `users`, `uploads`, `camps` are created at startup by `init_db()`.
`master`, `operator_master` and `tx` appear once the matching sheet is uploaded,
which is why `kv_sync.REQUIRED_TABLES` only insists on the first three.

Uploaded source spreadsheets are parsed in memory, so the database is the only
state the bridge needs to mirror. `parse_master` and `parse_tx` match columns by
name rather than position, so column order in the workbook does not matter.

### Re-uploading the master is guarded, not silent

`save_master` still uses `if_exists="replace"`, so a new master sheet **replaces
the whole master table** — but the write is now blocked by default if it would
leave station keys that already appear in the transaction history unmatched. The
admin is shown how many stations would be orphaned, a sample of their keys, and
has to tick a box to proceed anyway.

This mattered because the old behaviour lost data invisibly: rows that stopped
matching stayed in the database but vanished from the dashboard and the report,
and did not appear in the "not in master" list either, since that only covers the
selected period.

The same `replace` behaviour applies to the operator master. Transaction sheets
are **appended**, never replaced, and are keyed by `upload_id`; deleting an upload
in the Upload data tab removes its rows from `tx` as well. Appending validates the
frame against `parsers.TX_COLS` first, so a parser change that alters the shape
fails loudly instead of drifting the table.

### Indexes

`init_db()` creates `idx_tx_upload_id`, `idx_tx_key` and `idx_tx_key_upload` on
`tx`, which are the columns every report query filters and joins on. A test
asserts the query plan uses the index, so a future schema change cannot quietly
drop back to a full scan.

The two report builders are cached with `st.cache_data` and keyed on the selected
upload ids. `clear_report_cache()` is called on every write that changes master or
transaction data, so an upload shows up immediately; the five-minute TTL is a
safety net rather than the mechanism.

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
losing it. The keep-alive that holds the instance awake during the working day is the
`aadhar-keepalive` Worker in this same repository (see below). The dash-site
cron only knows about the Node backend, so it does not cover this service.

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
`aadhar-dashboard`, XSRF enabled, telemetry disabled, and a 15 MB upload cap.
`boot.py` overrides CORS and the upload size on the command line, and the flag
wins, so the effective production values come from `boot.py`.

There is no Cloudflare proxy in front of this service today. The dash-site Worker
has no `/aadhar-dashboard/*` route and no `AADHAR_ORIGIN` binding, so the Render
URL is the public entry point.

#### The keep-alive Worker

`keepalive-worker/`, deployed to the same dedicated account. A Render free
instance idles after ~15 minutes without traffic and a cold start takes 30-60s,
so a cron pings this service's health endpoint every 10 minutes.

| | |
| --- | --- |
| URL | `https://aadhar-keepalive.aadhar-haryana.workers.dev` |
| Status | `GET /status` — last attempt, last success, failure count, staleness |
| Manual ping | `GET /ping` — pings once, on demand, and reports the result |
| Secret | `AADHAR_ORIGIN` (the Render URL; must be set, or every tick no-ops) |
| KV namespace | `aadhar-keepalive-status` |

It skips 21:00-06:00 IST so the instance sleeps overnight and banks instance-hours
against Render's monthly free cap; the first user of the day pays a cold start.

Unlike the dash-site cron, this one **records its outcome**. The old cron had no
observable state, so "the cron is working" and "the cron is pointed at the wrong
URL" looked identical. `/status` reports `configured: false` and
`healthy: false` when the origin is unset, so a misconfiguration is visible in
seconds rather than after someone complains about a slow morning.

Configure it with:

```
cd keepalive-worker
npx wrangler secret put AADHAR_ORIGIN   # the service's https://<name>.onrender.com
npx wrangler deploy
curl https://aadhar-keepalive.aadhar-haryana.workers.dev/status
```

## Targets and achievement

**The target is Daily Target × *working* days, not × uploads.** Divisional post
offices are closed on Sundays, so a Sunday is not a day of opportunity. The old
arithmetic counted every upload, and on the real 18–24 September 2026 data that
understated Hisar by **12.3 percentage points** and the eleven-division mean by
6.3 — the Circle would have reported against a target it could never meet.

| | As shown before | Correct |
|---|---|---|
| Hisar | 73.7% | 86.0% |
| Karnal | 55.8% | 65.1% |
| Faridabad | 49.9% | 58.2% |
| Mean, 11 divisions | 37.7% | 44.0% |

Two deliberate choices:

- **Sunday *activity* still counts as achievement.** A few branches (RMS,
  delivery offices) operate on Sundays and those transactions are real. They are
  counted in the numerator and excluded only from the target multiplier.
- **A day is classified by its date, never by how much it reported.** A Sunday
  with no uploads is still a Sunday. Deciding by activity would let a missing
  upload quietly shrink the target and flatter the result.

The caption under the chart names the excluded days, so a figure that differs
from last week is explainable rather than mysterious. An upload whose label
cannot be read as a date is counted as a working day — a target is a ceiling, and
silently lowering it would flatter achievement — and the count is shown.

Change the pattern with `AADHAR_NON_WORKING_WEEKDAYS` (no redeploy needed).

## Security notes

- Passwords are stored as PBKDF2-SHA256 hashes with a per-user salt, 200,000
  rounds, compared with `hmac.compare_digest`.
- Login locks an account for 5 minutes after 5 failed attempts.
- Usernames are restricted to 3-30 characters of lowercase letters, digits, dot,
  dash or underscore, so they are safe to use in URLs and filenames.
- Uploaded spreadsheets are parsed with pandas; a file that fails to parse is
  rejected with the list of headers it actually contained, which is the main
  defence against a mis-mapped upload silently corrupting the numbers.
- `ensure_cols()` and every other identifier that reaches SQL goes through
  `parsers.quote_ident`, which rejects anything that is not a bare identifier, so
  a future caller forwarding user input fails loudly instead of building
  injectable SQL.

## Layout

| File | Purpose |
| --- | --- |
| `app.py` | Database helpers, authentication, the six screens, bootstrap |
| `parsers.py` | Spreadsheet parsing and database persistence — pure functions over DataFrames, no Streamlit |
| `kv_sync.py` | The off-site backup bridge client |
| `backup-worker/` | The Cloudflare Worker that stores the snapshots |
| `keepalive-worker/` | The Cloudflare cron that pings Render to stop the instance idling |
| `tests/test_parsers.py` | 77 tests covering the parsing and persistence rules |
| `tests/test_screens.py` | 24 tests driving the real script through Streamlit's AppTest |
| `tools/smoketest_backup_tab.py` | Interactive check of the admin login path and backup tab |
| `tools/check_backup_health.py` | One-command check that the database is actually mirrored |

## Tests

```
.venv/Scripts/python -m pytest tests/ -q
```

The suite exists to pin the rules that decide the reported numbers, which are
easy to change by accident and would otherwise fail silently:

- `parse_tx` treats the sheet's total column as authoritative and derives
  `upd = total - new`, clipped at zero. Only without that column does it fall back
  to summing MBU + demographic + non-MBU.
- `norm_key` makes `00123`, `123` and `123.0` the same key, which is what the
  master-to-transaction join depends on.
- Replacing the master refuses to orphan history unless explicitly confirmed.
- `save_tx` rejects a frame that does not match `TX_COLS`.
- `quote_ident` rejects injected identifiers.
- The report queries use the index, asserted via `EXPLAIN QUERY PLAN`.

`test_screens.py` drives the real script through AppTest and covers what the
parser suite cannot: that all seven pages render for an admin, that the four
read-only pages render for a plain user, that admin pages are absent from a
non-admin's navigation, that a division login sees all eleven divisions, that
active sessions are not logged out while an idle one is, and that a wrong
password, an unknown user and a disabled account are all rejected with the same
wording (no account enumeration).

## Notes

- The logo is read from `logo.png` (or `.jpg`/`.jpeg`/`.webp`) next to `app.py`,
  and its MIME type comes from `mimetypes.guess_type`.
- `data/`, `.venv/` and Python caches are gitignored.
- The upload cap is 15 MB, set in both `boot.py` (`--server.maxUploadSize`) and
  `.streamlit/config.toml`. Streamlit gives the command-line flag precedence, so
  the two must agree or the effective limit depends on how the app was started.
- `run()` commits only for statements that are not `SELECT`/`PRAGMA`/`WITH`, so
  reads no longer take a write lock.
- `build_period` computes the station and operator aggregates from a single read
  of `master` and `tx`. They are two aggregations of the same rows, and the
  Report page needs both, so reading `tx` twice per render was pure waste.
- The `use_container_width` deprecation notice in Streamlit 1.64 is expected; see
  the note below.
