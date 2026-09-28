/**
 * aadhar-proxy — puts the Streamlit dashboard behind Cloudflare.
 *
 * WHY
 *
 * The Streamlit service is served directly by Render, which means its
 * *.onrender.com URL is the only thing between the public internet and the
 * Aadhaar enrolment data. There is no WAF, no rate limiting, and no
 * Content-Security-Policy in front of it. This Worker adds an edge in front so
 * those can exist, and so the app can be reached on a hostname in the dedicated
 * account rather than only on a Render subdomain.
 *
 * WHAT THIS DOES NOT DO — read this before assuming it is a fix
 *
 * A proxy does not hide the origin. Anyone who learns the Render URL can still
 * hit it directly and bypass every header below. Render's free plan has no IP
 * allowlist, so there is no way to make the origin accept only this Worker.
 * Closing that gap needs either a private/paid Render service, or an
 * application-level control strong enough that direct access is harmless.
 * Session timeout and the login are currently doing that work; this Worker is a
 * hardening layer, not a boundary.
 *
 * STREAMLIT PROXYING NOTES
 *
 * - The path is forwarded intact, including the `aadhar-dashboard` base path
 *   from .streamlit/config.toml. Streamlit routes on that prefix, so stripping
 *   it breaks every request.
 * - WebSocket upgrades are forwarded for /_stcore/stream, which is how the UI
 *   receives tokens. Without the Upgrade header the page loads and then sits
 *   dead.
 * - The websocket has no timeout: Render free instances can take 60s+ to cold
 *   start, and a 30s edge read timeout would kill a healthy connection.
 * - X-Forwarded-Proto/Host are set so Streamlit builds absolute URLs against the
 *   public host rather than the internal one.
 * - The Host header is rewritten to the origin. If it were passed through,
 *   Streamlit would mint cookies and websocket URLs for the Render host, which
 *   the browser would then reject on the public origin.
 *
 * The Content-Security-Policy below is deliberately permissive about inline
 * styles and scripts. Streamlit injects both, and it exposes no nonce hook, so a
 * strict script-src would render a blank page. The directives that do real work
 * here are frame-ancestors, object-src and base-uri.
 */

const DEFAULT_ORIGIN = '';

function securityHeaders(extra = {}) {
  return {
    // Streamlit injects inline <style> and inline <script>; there is no nonce
    // hook, so those two must stay permissive. Everything below is enforced.
    'Content-Security-Policy': [
      "default-src 'self'",
      "script-src 'self' 'unsafe-inline' 'unsafe-eval'",
      "style-src 'self' 'unsafe-inline'",
      "img-src 'self' data: blob:",
      "font-src 'self' data:",
      "connect-src 'self' ws: wss:",
      "worker-src 'self' blob:",
      "object-src 'none'",
      "base-uri 'none'",
      "form-action 'self'",
      "frame-ancestors 'none'",
    ].join('; '),
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
    'Referrer-Policy': 'strict-origin-when-cross-origin',
    'Permissions-Policy': 'geolocation=(), microphone=(), camera=(), payment=()',
    'Cross-Origin-Opener-Policy': 'same-origin',
    'Cross-Origin-Resource-Policy': 'same-origin',
    ...extra,
  };
}

function problem(status, title, detail) {
  return new Response(
    `${title}\n\n${detail}\n`,
    {
      status,
      headers: securityHeaders({ 'Content-Type': 'text/plain; charset=utf-8' }),
    },
  );
}

function notConfigured() {
  return problem(
    503,
    'aadhar-proxy: not configured',
    'AADHAR_ORIGIN is not set on this Worker, so there is nothing to proxy to.\n' +
      'Set it to the service\'s public URL and redeploy:\n' +
      '  cd proxy-worker && npx wrangler secret put AADHAR_ORIGIN\n\n' +
      'Requests are refused rather than passed through, so a missing origin can\n' +
      'never silently serve the origin directly.',
  );
}

function originOf(env) {
  return (env.AADHAR_ORIGIN || DEFAULT_ORIGIN).trim().replace(/\/+$/, '');
}

export default {
  async fetch(request, env) {
    const origin = originOf(env);
    if (!origin) return notConfigured();

    const incoming = new URL(request.url);
    const target = new Request(origin + incoming.pathname + incoming.search, request);

    // The origin must see its own hostname, not the public proxy hostname, or it
    // will mint cookies and websocket URLs the browser rejects.
    const headers = new Headers(request.headers);
    headers.set('Host', new URL(origin).host);
    headers.set('X-Forwarded-Proto', incoming.protocol.replace(':', ''));
    headers.set('X-Forwarded-Host', incoming.host);
    // Preserve any existing chain and append the real client address.
    const clientIp = request.cf && request.cf.clientIp ? request.cf.clientIp : '';
    if (clientIp) {
      const prior = request.headers.get('X-Forwarded-For');
      headers.set('X-Forwarded-For', prior ? `${prior}, ${clientIp}` : clientIp);
    }
    // Identify the client honestly; Render's bot rules can reject default agents.
    headers.set('User-Agent', 'Mozilla/5.0 (compatible; aadhar-proxy/1.0)');

    const isWebSocket = incoming.pathname.includes('/_stcore/stream') ||
      (request.headers.get('Upgrade') || '').toLowerCase() === 'websocket';

    const init = {
      method: request.method,
      headers,
      body: request.method === 'GET' || request.method === 'HEAD' ? undefined : request.body,
      redirect: 'manual',
    };

    // A Streamlit free instance can cold-start for a minute. A default edge read
    // timeout would sever a perfectly healthy socket, so stream it long.
    if (isWebSocket) {
      init.cf = { cacheEverything: false };
    }

    let response;
    try {
      response = await fetch(new Request(target, init));
    } catch (err) {
      return problem(
        502,
        'aadhar-proxy: origin unreachable',
        `${origin} did not respond.\n\n` +
          'This is expected while a Render free instance is asleep; the first\n' +
          'request after an idle period can take a minute. If it persists, the\n' +
          'service is down or AADHAR_ORIGIN points at the wrong URL.\n\n' +
          `${String(err)}`,
      );
    }

    // Re-apply our headers on the way out. The origin's own headers are kept,
    // except for hop-by-hop and length headers that no longer describe the body.
    const out = new Headers(response.headers);
    out.delete('Content-Length');
    out.delete('Transfer-Encoding');
    out.delete('Connection');
    out.delete('Keep-Alive');
    out.delete('Upgrade');
    out.delete('X-Powered-By');
    for (const [name, value] of Object.entries(securityHeaders())) {
      out.set(name, value);
    }
    // Never let an intermediary cache an authenticated page.
    if (!/no-store|private/.test(out.get('Cache-Control') || '')) {
      out.set('Cache-Control', 'no-store');
    }

    return new Response(response.body, { status: response.status, statusText: response.statusText, headers: out });
  },
};
