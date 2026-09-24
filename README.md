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
| `aadhar.html` | The Aadhaar MIS Dashboard login page (Haryana Circle). Same layout, styling and behaviour as the Tamilnadu portal, rebranded for Haryana Circle. |

## Behaviour

- The page is a faithful copy of the original portal: a password-style PIN entry that
  submits via `POST /login`.
- The `/login` endpoint is wired to the dashboardharyana.site infrastructure when the
  backend is connected; until then the page renders exactly like the original.

## Deploy

`aadhar.html` is published to GitHub Pages from the `dash-site` repository root so that
`https://dashboardharyana.site/aadhar.html` serves this file. Keep the two copies in sync
when updating this page.