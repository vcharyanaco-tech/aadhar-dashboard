/**
 * aadhar-keepalive — stops the Render free instance for the Aadhaar dashboard
 * from spinning down during the working day.
 *
 * WHY
 *
 * Render free web services are idled after ~15 minutes without traffic, and a
 * cold start takes 30-60s. A cron pings the service's health endpoint every 10
 * minutes to hold it awake.
 *
 * The dash-site Worker has an equivalent cron, but it only ever pings the Node
 * backend at SERVER_ORIGIN. It has no knowledge of this service, so the
 * Aadhaar dashboard was never actually being kept warm.
 *
 * NIGHT SKIP
 *
 * Between 21:00 and 06:00 IST the ping is skipped so the instance idles out and
 * banks instance-hours against Render's monthly free cap. The first user of the
 * day pays one cold start; that is an accepted trade, and the database survives
 * regardless because it is mirrored to the aadhar-backup Worker.
 *
 * WHY THIS RECORDS STATUS
 *
 * A keepalive that silently fails looks exactly like a keepalive that is not
 * needed. The previous cron had no observable state, so there was no way to tell
 * "the cron is working" from "the cron is pointed at the wrong URL". Every tick
 * here records its outcome in KV and exposes it on /status, so a broken
 * configuration is visible in seconds rather than after a cold start complaint.
 */

const STATUS_KEY = 'keepalive:status';
const MAX_STATUS_AGE_MS = 30 * 60 * 1000; // two missed 10-minute ticks

const DEFAULTS = { timeoutMs: 20000, path: '/aadhar-dashboard/_stcore/health' };

// Cloudflare's bot rules reject default fetch agents, and a bare
// `workers-fetch` UA can be challenged. Be explicit.
const USER_AGENT = 'Mozilla/5.0 (compatible; aadhar-keepalive/1.0; +https://dashboardharyana.site)';

function json(data, status = 200) {
  return new Response(JSON.stringify(data, null, 2), {
    status,
    headers: {
      'Content-Type': 'application/json; charset=utf-8',
      'Cache-Control': 'no-store',
      'X-Content-Type-Options': 'nosniff',
      'X-Robots-Tag': 'noindex, nofollow',
    },
  });
}

/** Current hour and minute in IST (UTC+05:30). */
function istNow() {
  const shifted = new Date(Date.now() + 5.5 * 3600 * 1000);
  return { hour: shifted.getUTCHours(), minute: shifted.getUTCMinutes() };
}

async function readStatus(env) {
  const raw = await env.STATUS.get(STATUS_KEY, 'text');
  const configured = Boolean(env.AADHAR_ORIGIN);
  if (!raw) {
    return {
      ok: true,
      service: 'aadhar-keepalive',
      configured,
      origin: env.AADHAR_ORIGIN || null,
      everPinged: false,
    };
  }
  let parsed = null;
  try {
    parsed = JSON.parse(raw);
  } catch {
    parsed = null;
  }
  if (!parsed) {
    return { ok: true, service: 'aadhar-keepalive', configured, everPinged: false, corrupt: true };
  }
  // Recompute from the environment rather than trusting the stored value. A
  // record written before AADHAR_ORIGIN was set would otherwise keep reporting
  // configured:false forever, telling an operator to set a variable they had
  // already set - the same silent-misconfiguration trap as before.
  return { ...parsed, configured, origin: env.AADHAR_ORIGIN || parsed.origin || null };
}

async function ping(env) {
  const origin = (env.AADHAR_ORIGIN || '').replace(/\/+$/, '');
  const path = env.AADHAR_HEALTH_PATH || DEFAULTS.path;
  const timeoutMs = Number(env.KEEPALIVE_TIMEOUT_MS) > 0
    ? Number(env.KEEPALIVE_TIMEOUT_MS)
    : DEFAULTS.timeoutMs;

  if (!origin) {
    return {
      ok: false,
      error: 'AADHAR_ORIGIN is not set on this Worker',
      at: new Date().toISOString(),
    };
  }

  // A cold start can outlast a single attempt, so give it one quick retry.
  for (let attempt = 1; attempt <= 2; attempt++) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const started = Date.now();
      const resp = await fetch(origin + path, {
        method: 'GET',
        headers: { 'User-Agent': USER_AGENT, Accept: 'text/html,application/json' },
        signal: controller.signal,
      });
      clearTimeout(timer);
      const body = (await resp.text()).slice(0, 200);
      return {
        ok: resp.ok,
        status: resp.status,
        ms: Date.now() - started,
        attempt,
        body,
        at: new Date().toISOString(),
      };
    } catch (err) {
      clearTimeout(timer);
      const reason = err && err.name === 'AbortError' ? `timeout after ${timeoutMs}ms` : String(err);
      if (attempt === 2) {
        return { ok: false, error: reason, at: new Date().toISOString() };
      }
    }
  }
  return { ok: false, error: 'unreachable', at: new Date().toISOString() };
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // Unauthenticated. Exposes only keepalive state, never application data.
    if (url.pathname === '/status') {
      const status = await readStatus(env);
      const last = status.lastAttemptAt ? Date.parse(status.lastAttemptAt) : null;
      const stale = last ? Date.now() - last > MAX_STATUS_AGE_MS : true;
      return json({
        ...status,
        healthy: Boolean(status.everPinged && status.lastOk && !stale),
        stale,
        note: !env.AADHAR_ORIGIN
          ? 'Set AADHAR_ORIGIN before the cron can do anything.'
          : undefined,
      });
    }

    // Manual trigger, for verifying the origin without waiting for a tick.
    if (url.pathname === '/ping') {
      const result = await ping(env);
      const previous = await readStatus(env);
      const next = {
        ...previous,
        service: 'aadhar-keepalive',
        origin: (env.AADHAR_ORIGIN || null),
        everPinged: true,
        lastAttemptAt: result.at,
        lastOk: result.ok,
        lastError: result.error || null,
        lastStatus: result.status ?? null,
        lastMs: result.ms ?? null,
        consecutiveFailures: result.ok ? 0 : (previous.consecutiveFailures || 0) + 1,
      };
      await env.STATUS.put(STATUS_KEY, JSON.stringify(next));
      return json(next, result.ok ? 200 : 502);
    }

    return json({ ok: false, error: 'not_found' }, 404);
  },

  async scheduled(event, env, ctx) {
    const { hour, minute } = istNow();

    // Deliberate overnight idle: 21:00-06:00 IST.
    if (hour >= 21 || hour < 6) {
      const previous = await readStatus(env);
      await env.STATUS.put(
        STATUS_KEY,
        JSON.stringify({
          ...previous,
          service: 'aadhar-keepalive',
          origin: env.AADHAR_ORIGIN || null,
          lastSkippedNightAt: new Date().toISOString(),
        }),
      );
      return;
    }

    const result = await ping(env);
    const previous = await readStatus(env);
    const next = {
      ...previous,
      service: 'aadhar-keepalive',
      origin: (env.AADHAR_ORIGIN || null),
      everPinged: true,
      lastAttemptAt: result.at,
      lastOk: result.ok,
      lastError: result.error || null,
      lastStatus: result.status ?? null,
      lastMs: result.ms ?? null,
      lastSuccessAt: result.ok ? result.at : previous.lastSuccessAt || null,
      consecutiveFailures: result.ok ? 0 : (previous.consecutiveFailures || 0) + 1,
      ticksToday: (previous.ticksToday || 0) + 1,
    };
    await env.STATUS.put(STATUS_KEY, JSON.stringify(next));

    if (!result.ok) {
      // Surface it: a keepalive that has been failing for a while is a problem
      // in its own right, and silence is what made the old one untrustworthy.
      console.error(
        `keepalive FAILED origin=${env.AADHAR_ORIGIN} error=${result.error || result.status} ` +
          `failures=${next.consecutiveFailures} ist=${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`,
      );
    }
  },
};
