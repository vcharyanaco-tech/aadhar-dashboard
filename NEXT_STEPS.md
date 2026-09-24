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

## Pending (user to decide later)

1. **Post-login dashboard content.** The Tamilnadu dashboard behind the PIN could
   not be copied — no valid PIN available, and we will not bypass its auth. Options
   the user will pick from:
   a. Have the Tamilnadu portal admin export the dashboard HTML/spec.
   b. Build the post-login MIS page from spec (11 reports, 4 access levels,
      Excel/PDF/PNG exports) in the India Post Haryana style — recommended.
2. **`/login` endpoint.** `aadhar.html` POSTs `pin` to `/login`
   (`application/x-www-form-urlencoded`). The TN reference behaves as:
   - invalid PIN → 200 re-render of login page with
     `<div class="err">Incorrect PIN. Please contact Circle Office, Tamilnadu.</div>`
   - valid PIN → (assumed) session cookie + dashboard redirect.
   Decide: single **configurable PIN** (choice made — default value TBD) and where a
   valid PIN should go (dashboard page URL). Wire into the dashboardharyana.site
   Cloudflare Worker / Node backend when decided.
3. Keep `aadhar.html` in sync between this repo and `dash-site` root on updates.