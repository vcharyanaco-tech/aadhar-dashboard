# aadhar-dashboard

Aadhaar MIS Reporting Dashboard — **Circle Office, Haryana, Department of Posts, India**.

This is a Haryana Circle conversion of the Tamilnadu Circle Aadhaar reporting portal
(formerly at `doptnaadhar.online/login`). It provides PIN-gated access to Aadhaar MIS
reports for authorized Circle Office staff.

## Live URL

The dashboard is served at:

**https://dashboardharyana.site/aadhar.html**

It is deployed alongside the India Post Haryana dashboard static site (see the
`dash-site` repository for the surrounding landing page and backend).

## Files

| Path | What it is |
| --- | --- |
| `aadhar.html` | The Aadhaar MIS Dashboard (Haryana Circle): PIN login view + post-login MIS dashboard in one page. Same layout and look as the Tamilnadu portal, rebranded for Haryana Circle. |

## Behaviour

- Faithful copy of the original portal: a password-style PIN entry on a
  `POST /login` form.
- A single configurable PIN (`var ACCESS_PIN` at the bottom of the file, set to
  the portal PIN) unlocks the post-login MIS dashboard (11 reports, 4 access
  levels, Excel/PDF/PNG exports). Wrong PIN shows the same
  *"Incorrect PIN. Please contact Circle Office, Haryana."* error as the original.
- The login is currently gated client-side; the PIN is therefore visible in the
  page source. Wire a server-side `/login` endpoint into the
  dashboardharyana.site backend before exposing real data.

## Deploy

`aadhar.html` is published to GitHub Pages from the `dash-site` repository root so that
`https://dashboardharyana.site/aadhar.html` serves this file. Keep the two copies in sync
when updating this page.