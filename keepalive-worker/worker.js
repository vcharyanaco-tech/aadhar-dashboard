/**
 * aadhar-keepalive — keeps the Render free instance for the Aadhaar dashboard
 * from spinning down.
 *
 * WHY 24/7
 *
 * Render free web services are idled after ~15 minutes without traffic, and a
 * cold start takes 30-60s. A cron pings the service's health endpoint every 10
 * minutes to hold it awake.
 *
 * The cron used to skip 21:00-06:00 IST so the instance slept overnight and
 * banked instance-hours. That put it on Render's wake path every morning, and
 * on 12-13 November 2026 Render's edge refused that wake for more than a day:
 * every request came back 429 with `x-render-routing: hibernate-rate-limited`,
 * so the dashboard was down even though this cron was running.
 *
 * The night skip is gone. This service is in its own Render workspace and now
 * runs 24/7: ~720 instance-hours in a 30-day month and ~744 in a 31-day month,
 * against the workspace's 750-hour free allowance. A warm instance never takes
 * the hibernation wake path, so the failure is avoided rather than retried.
 *
 * DO NOT RETRY 429
 *
 * A 429 from Render's edge is not a transient error. Retrying it in a burst
 * used to amplify the pressure that caused the block and could extend it. This
 * Worker records the 429 on the first response and then backs off (5 minutes,
 * then 10) instead of pinging again. The 10-minute cap is deliberate: a
 * hibernated free instance re-hibernates after 15 minutes, so a 30/60-minute
 * backoff guarantees every probe is another refused wake and turns one 429 into
 * an hour-long outage. Only 5xx and transport errors are retried in a burst,
 * because those can be a cold start or a redeploy.
 *
 * The dash-site Worker has an equivalent cron, but it only pings the Node
 * backend at SERVER_ORIGIN and still sleeps overnight. It intentionally does
 * not ping this service.
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

// Back off for 5 minutes after the first 429, then cap at 10. Keep this below
// the 15-minute idle window: if the backoff outlives the window, the instance
// is already hibernated again when the next probe runs, so every probe is a
// refused wake and the block never gets a chance to clear. One probe per tick
// is still not a retry burst.
const THROTTLE_BASE_MS = 5 * 60 * 1000;
const THROTTLE_MAX_MS = 10 * 60 * 1000;

// 5xx and transport failures get a couple of quick attempts: those can be a
// cold start or a deploy. 429 never retries (see the header comment).
const MAX_ATTEMPTS = 3;

// Cloudflare's bot rules reject default fetch agents, and a bare
// `workers-fetch` UA can be challenged. Be explicit.
const USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36';

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

/** IST calendar date as YYYY-MM-DD, used to reset the daily tick counter. */
function istDateKey(date = new Date()) {
  return new Date(date.getTime() + 5.5 * 3600 * 1000).toISOString().slice(0, 10);
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

  const startedAll = Date.now();

  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
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
      const at = new Date().toISOString();

      // 429 means Render's edge is refusing this client, not that the service
      // is slow. Retrying makes the block worse, so record it and stop.
      if (resp.status === 429) {
        return {
          ok: false,
          status: 429,
          ms: Date.now() - started,
          attempt,
          body,
          routing: resp.headers.get('x-render-routing') || null,
          at,
        };
      }

      if (resp.status >= 500 && attempt < MAX_ATTEMPTS) {
        await new Promise((r) => setTimeout(r, 1000 * attempt));
        continue;
      }
      return {
        ok: resp.ok,
        status: resp.status,
        ms: Date.now() - started,
        totalMs: Date.now() - startedAll,
        attempt,
        body,
        at,
      };
    } catch (err) {
      clearTimeout(timer);
      const reason = err && err.name === 'AbortError' ? `timeout after ${timeoutMs}ms` : String(err);
      if (attempt === MAX_ATTEMPTS) {
        return { ok: false, error: reason, ms: Date.now() - startedAll, at: new Date().toISOString() };
      }
      await new Promise((r) => setTimeout(r, 1000 * attempt));
    }
  }
  return { ok: false, error: 'unreachable', at: new Date().toISOString() };
}

/**
 * Merge one attempt into the stored status. Kept in one place so /ping and the
 * cron cannot drift apart again (the manual ping used to leave old counters
 * behind).
 */
function recordAttempt(env, previous, result, extra = {}) {
  const at = result.at || new Date().toISOString();
  const next = {
    ...previous,
    service: 'aadhar-keepalive',
    origin: env.AADHAR_ORIGIN || previous.origin || null,
    everPinged: true,
    lastAttemptAt: at,
    lastOk: Boolean(result.ok),
    lastError: result.error || null,
    lastStatus: result.status ?? null,
    lastMs: result.ms ?? null,
    lastSuccessAt: result.ok ? at : previous.lastSuccessAt || null,
    consecutiveFailures: result.ok ? 0 : (previous.consecutiveFailures || 0) + 1,
    ...extra,
  };
  if (!result.ok) {
    next.lastFailureAt = at;
    next.lastFailureStatus = result.status ?? null;
    next.lastFailureError = result.error || (result.status ? `HTTP ${result.status}` : 'unknown');
  }
  return next;
}

/** Compute the throttle deadline for this attempt, if it was a 429. */
function throttleState(previous, result) {
  if (result.status !== 429) {
    return { throttleCount: 0, throttledUntil: null };
  }
  const count = (previous.throttleCount || 0) + 1;
  const backoffMs = Math.min(THROTTLE_BASE_MS * 2 ** (count - 1), THROTTLE_MAX_MS);
  return {
    throttleCount: count,
    throttledUntil: new Date(Date.parse(result.at) + backoffMs).toISOString(),
    lastThrottledAt: result.at,
    lastThrottleRouting: result.routing || null,
  };
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // Unauthenticated. Exposes only keepalive state, never application data.
    if (url.pathname === '/status') {
      const status = await readStatus(env);
      const last = status.lastAttemptAt ? Date.parse(status.lastAttemptAt) : null;
      const stale = last ? Date.now() - last > MAX_STATUS_AGE_MS : true;
      const until = status.throttledUntil ? Date.parse(status.throttledUntil) : null;
      const throttled = Boolean(until && until > Date.now());
      return json({
        ...status,
        healthy: Boolean(status.everPinged && status.lastOk && !stale && !throttled),
        stale,
        throttled,
        note: !env.AADHAR_ORIGIN
          ? 'Set AADHAR_ORIGIN before the cron can do anything.'
          : undefined,
      });
    }

    // Manual trigger, for verifying the origin without waiting for a tick. It
    // deliberately ignores an active throttle: a human asking for a probe is
    // an explicit override, and the result still updates the stored state.
    if (url.pathname === '/ping') {
      const result = await ping(env);
      const previous = await readStatus(env);
      const today = istDateKey();
      const next = recordAttempt(env, previous, result, {
        ...throttleState(previous, result),
        ticksDate: previous.ticksDate || today,
      });
      await env.STATUS.put(STATUS_KEY, JSON.stringify(next));
      return json(next, result.ok ? 200 : 502);
    }

    return json({ ok: false, error: 'not_found' }, 404);
  },

  async scheduled(event, env, ctx) {
    const previous = await readStatus(env);
    const now = Date.now();
    const until = previous.throttledUntil ? Date.parse(previous.throttledUntil) : 0;

    // Render already refused a ping recently. Do not retry: each request adds
    // pressure and can extend the block. Record the skip so /status explains
    // why lastAttemptAt is older than the cron interval.
    if (until > now) {
      await env.STATUS.put(
        STATUS_KEY,
        JSON.stringify({
          ...previous,
          service: 'aadhar-keepalive',
          origin: env.AADHAR_ORIGIN || previous.origin || null,
          lastSkippedThrottleAt: new Date(now).toISOString(),
        }),
      );
      return;
    }

    const result = await ping(env);
    const { hour, minute } = istNow();
    const today = istDateKey();
    const ticks = previous.ticksDate === today ? (previous.ticksToday || 0) : 0;
    const next = recordAttempt(env, previous, result, {
      ...throttleState(previous, result),
      ticksDate: today,
      ticksToday: ticks + 1,
    });
    await env.STATUS.put(STATUS_KEY, JSON.stringify(next));

    if (!result.ok) {
      // Surface it: a keepalive that has been failing for a while is a problem
      // in its own right, and silence is what made the old one untrustworthy.
      console.error(
        `keepalive FAILED origin=${env.AADHAR_ORIGIN} error=${result.error || result.status} ` +
          `failures=${next.consecutiveFailures} throttledUntil=${next.throttledUntil || 'none'} ` +
          `ist=${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`,
      );
    }
  },
};
