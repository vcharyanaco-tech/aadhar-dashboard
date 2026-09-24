# Next steps (handoff note)

Snapshot of the `aadhar-dashboard` project as of 2026-09-24.

## Done and live

- **https://github.com/vcharyanaco-tech/aadhar-dashboard** — source repo for the
  Haryana Circle Aadhaar MIS dashboard (converted from the Tamilnadu portal that
  lives at `doptnaadhar.online/login`).
- `aadhar.html` is the Haryana Circle login page: same layout/CSS/POST `/login`
  behaviour as the Tamilnadu original, rebranded to **Haryana Circle**, with the
  India Post logo embedded.
- The same `aadhar.html` is mirrored into the **dash-site** repo root so GitHub
  Pages serves it at **https://dashboardharyana.site/aadhar.html** (Pages + Worker
  deploys verified green).
- The landing site at `dashboardharyana.site` links to it: hero button, top nav,
  footer link, and an "Aadhaar Reporting — Haryana Circle" section (`dash-site`
  commit `c3ec5b5`, v1.3.0).

## Done — post-login dashboard (2026-09-24)

- The post-login MIS dashboard was recovered via the Anima Playground capture of
  `doptnaadhar.online` (`saved_resource.html` iframe in the downloaded Anima file)
  and converted to the **Haryana Circle** version. It is now embedded in
  `aadhar.html` and revealed after a valid PIN.
- Dashboard contents (same as captured TN structure): sidebar with the 11 reports
  (Division Wise Summary, Sub Division, Head PO, Sub PO, Branch PO, Operator
  Performance, Daily Transactions, Monthly Progress, Quarterly Analysis, Rejection
  Analysis, Pending Applications), 4 access levels (Circle Admin / Regional Admin /
  Division Admin / Operator), Excel/PDF/PNG export buttons, 4 KPI cards, and a
  Quick Access grid.

## How auth works today (client-side gate)

`aadhar.html` keeps the faithful login card (`POST /login`, PIN field, pills). On
submit a small inline script checks the PIN against a single configurable constant:

```js
var ACCESS_PIN = '0000';   // <-- change this before going live
```

- Correct PIN → login view is hidden, the MIS dashboard is shown, and the session
  is remembered in `sessionStorage` (refresh keeps you in, mirroring the TN
  server-session behaviour).
- Incorrect PIN → same error pattern as TN:
  `<div class="err">Incorrect PIN. Please contact Circle Office, Haryana.</div>`
- Logout returns to the login view.

## Pending (user to decide later)

1. **Secure the `/login` endpoint backend.** The client-side gate is a placeholder
   until the dashboardharyana.site Cloudflare Worker / Node backend is wired to
   accept `POST /login` and validate the PIN server-side. When that happens, remove
   `ACCESS_PIN` from `aadhar.html` and POST as the original does.
2. **Set the real PIN** (replace the `0000` default) and decide whether a single
   PIN covers all 4 access levels or each role gets its own PIN.
3. Keep `aadhar.html` in sync between this repo and `dash-site` root on updates.