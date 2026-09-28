/**
 * aadhar-backup — durable backup bridge for the Haryana Circle Aadhaar
 * dashboard (aadhar-dashboard repo).
 *
 * WHY THIS EXISTS
 *
 * Render's free plan has no persistent disk, so `aadhaar.db` is destroyed on
 * every deploy, every restart and every spin-down. This Worker holds the only
 * durable copy, in a Cloudflare account that hosts nothing else, so the backup
 * path cannot be broken or exhausted by an unrelated project.
 *
 * (The previous bridge lived in the dash-site Worker and never worked: the app
 * requested /api/backup/aadhaar-db but that handler only routes /db, /uploads,
 * /meetings and /stats, so every restore and every push returned 404. See the
 * repository README.)
 *
 * DESIGN
 *
 * - Versioned generations. Each push writes a new key `aadhar:gen:<ts>-<rand>`
 *   and then moves the `aadhar:latest` pointer. An interrupted push can never
 *   corrupt the generation a restore would use, and older generations stay
 *   available for manual rollback.
 * - Budget enforced HERE, not in the client. The client used to count writes in
 *   a module-level dict, which reset to zero on every Render restart, so the cap
 *   was never actually enforced. This Worker is the authority.
 * - Retention driven by a tracked index, not by KV list(). KV list is
 *   eventually consistent, so on the hot path it cannot see keys written seconds
 *   ago, and pruning from a stale listing risks deleting a current generation.
 *   Instead the authoritative generation list lives in the stats record and
 *   pruning deletes exact keys. `POST /reconcile` rebuilds the index from list()
 *   when drift is suspected.
 * - Fails closed: if the BRIDGE_TOKEN secret is missing, every authenticated
 *   route returns 401 rather than serving data to anyone.
 *
 * The database contains user accounts and password hashes. Treat the token as a
 * credential and never commit it; it is set with `wrangler secret put`.
 */

const KEY_LATEST = 'aadhar:latest';
const KEY_GEN_PREFIX = 'aadhar:gen:';
const KEY_STATS = 'aadhar:stats';

// A SQLite database file starts with this exact 16-byte header. Rejecting
// anything else means a truncated or mis-posted upload can never become the
// generation a later restore would hand to the app.
const SQLITE_MAGIC = 'SQLite format 3\u0000';

const DEFAULTS = { retain: 12, dailyPushBudget: 150, maxBytes: 20971520 };

function num(v, fallback) {
  const n = Number(v);
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : fallback;
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data, null, 2), {
    status,
    headers: {
      'Content-Type': 'application/json; charset=utf-8',
      // The restore path must never read a cached copy of a snapshot. A stale
      // response here would hand the app a database that is hours old.
      'Cache-Control': 'no-store',
      'X-Content-Type-Options': 'nosniff',
      'X-Robots-Tag': 'noindex, nofollow',
    },
  });
}

/** Constant-time token comparison via SHA-256, so neither length nor content leaks. */
async function tokenMatches(provided, expected) {
  if (!expected || !provided) return false; // fail closed if the secret is unset
  const enc = new TextEncoder();
  const [a, b] = await Promise.all([
    crypto.subtle.digest('SHA-256', enc.encode(provided)),
    crypto.subtle.digest('SHA-256', enc.encode(expected)),
  ]);
  const A = new Uint8Array(a);
  const B = new Uint8Array(b);
  let diff = 0;
  for (let i = 0; i < A.length; i++) diff |= A[i] ^ B[i];
  return diff === 0;
}

function bearerOf(request) {
  const raw = request.headers.get('Authorization') || '';
  const m = raw.match(/^Bearer\s+(.+)$/i);
  return m ? m[1].trim() : null;
}

function today() {
  return new Date().toISOString().slice(0, 10);
}

/**
 * Push accounting. Resets on a UTC day boundary and carries `lastPushAt` across
 * the reset so a health check can still tell how stale the newest snapshot is.
 * `gens` is the authoritative generation index used for retention; it is
 * carried across day rollovers so pruning survives midnight UTC.
 */
async function budgetState(env) {
  let stored = null;
  try {
    const raw = await env.BACKUPS.get(KEY_STATS, 'text');
    if (raw) stored = JSON.parse(raw);
  } catch {
    stored = null;
  }
  const day = today();
  if (!stored || stored.day !== day) {
    return {
      day,
      pushes: 0,
      lastPushAt: (stored && stored.lastPushAt) || null,
      lastBytes: (stored && stored.lastBytes) || null,
      total: (stored && stored.total) || 0,
      gens: (stored && stored.gens) || [],
    };
  }
  return { ...stored, day, gens: (stored.gens || []).filter(isValidGen) };
}

/** Generation ids sort lexicographically in chronological order (epoch-ms prefix). */
function isValidGen(gen) {
  return typeof gen === 'string' && /^\d{13}-[0-9a-f]{6}$/.test(gen);
}

function genEpoch(gen) {
  return isValidGen(gen) ? Number(gen.slice(0, 13)) : null;
}

async function latestGen(env) {
  const v = await env.BACKUPS.get(KEY_LATEST, 'text');
  return isValidGen(v) ? v : null;
}

async function readStats(env) {
  const budget = await budgetState(env);
  const budgetCap = num(env.DAILY_PUSH_BUDGET, DEFAULTS.dailyPushBudget);
  const retain = num(env.RETAIN, DEFAULTS.retain);
  const latest = await latestGen(env);
  const epoch = genEpoch(latest);
  return {
    ok: true,
    service: 'aadhar-backup',
    configured: Boolean(env.BRIDGE_TOKEN),
    latest,
    latestAt: epoch ? new Date(epoch).toISOString() : null,
    ageSeconds: epoch ? Math.max(0, Math.round((Date.now() - epoch) / 1000)) : null,
    day: budget.day,
    pushesToday: budget.pushes,
    dailyPushBudget: budgetCap,
    pushesLeft: Math.max(0, budgetCap - budget.pushes),
    lastPushAt: budget.lastPushAt,
    lastBytes: budget.lastBytes,
    totalPushes: budget.total,
    trackedGenerations: budget.gens.length,
    retain,
  };
}

/**
 * Enforce retention from the tracked index, deleting exact keys.
 *
 * The index is authoritative for *which* generations we believe exist; the
 * deletes are strongly consistent per key, so a generation is only ever removed
 * when the index says it is surplus. Keys absent from the index are left alone.
 */
async function pruneTracked(env, gens, retain, keepGen) {
  const excess = gens.length - retain;
  if (excess <= 0) return { pruned: 0, kept: gens.length };

  const doomed = gens.slice(0, excess).filter((g) => g !== keepGen);
  for (const g of doomed) {
    await env.BACKUPS.delete(KEY_GEN_PREFIX + g);
  }
  return { pruned: doomed.length, kept: gens.length - doomed.length };
}

/** Full rebuild of the index from KV list(). Manual, via POST /reconcile. */
async function reconcile(env) {
  const listed = await env.BACKUPS.list({ prefix: KEY_GEN_PREFIX, limit: 1000 });
  const seen = listed.keys
    .map((k) => k.name.slice(KEY_GEN_PREFIX.length))
    .filter(isValidGen)
    .sort();

  const budget = await budgetState(env);
  const tracked = new Set(budget.gens);
  const present = new Set(seen);
  const added = seen.filter((g) => !tracked.has(g));
  const removed = budget.gens.filter((g) => !present.has(g));

  const gens = seen;
  await env.BACKUPS.put(KEY_STATS, JSON.stringify({ ...budget, gens }));

  const latest = await latestGen(env);
  if (latest && !present.has(latest)) {
    // The pointer's target is gone. Move it back to the newest generation that
    // actually exists so a restore cannot resolve to a missing key.
    const newest = gens[gens.length - 1];
    if (newest) await env.BACKUPS.put(KEY_LATEST, newest);
  }

  return { ok: true, found: seen.length, added: added.length, droppedFromIndex: removed.length, gens };
}

async function handlePut(request, env) {
  const budget = await budgetState(env);
  const cap = num(env.DAILY_PUSH_BUDGET, DEFAULTS.dailyPushBudget);
  const maxBytes = num(env.MAX_BYTES, DEFAULTS.maxBytes);

  if (budget.pushes >= cap) {
    // 429 with an explicit reset time: the client logs this and stops trying
    // rather than silently pretending the backup succeeded.
    const resetAt = new Date(Date.parse(budget.day + 'T00:00:00Z') + 86400000).toISOString();
    return json(
      {
        ok: false,
        error: 'daily_push_budget_exhausted',
        message: `Daily push budget of ${cap} is exhausted. Pushes resume after ${resetAt}.`,
        pushesToday: budget.pushes,
        dailyPushBudget: cap,
        resetsAt: resetAt,
      },
      429,
    );
  }

  const buf = await request.arrayBuffer();
  const bytes = buf.byteLength;

  if (bytes === 0) return json({ ok: false, error: 'empty_body' }, 400);
  if (bytes > maxBytes) {
    return json(
      { ok: false, error: 'too_large', bytes, maxBytes, message: 'Snapshot exceeds the configured cap.' },
      413,
    );
  }

  const head = new Uint8Array(buf, 0, Math.min(SQLITE_MAGIC.length, bytes));
  const magic = new TextDecoder().decode(head);
  if (!magic.startsWith(SQLITE_MAGIC.slice(0, magic.length)) || bytes < SQLITE_MAGIC.length) {
    return json({ ok: false, error: 'not_sqlite', message: 'Body is not a SQLite database.' }, 400);
  }

  const gen = `${Date.now()}-${crypto.randomUUID().slice(0, 6)}`;
  await env.BACKUPS.put(KEY_GEN_PREFIX + gen, buf);
  // Pointer last: until it moves, a restore still sees the previous good
  // generation, so a crash between the two writes loses nothing.
  await env.BACKUPS.put(KEY_LATEST, gen);

  // Append to the tracked index. If the index read was stale and already ends
  // with this exact id, do not duplicate it.
  const gens = budget.gens.includes(gen) ? budget.gens : [...budget.gens, gen];

  const next = {
    day: budget.day,
    pushes: budget.pushes + 1,
    lastPushAt: new Date().toISOString(),
    lastBytes: bytes,
    total: budget.total + 1,
    gens,
  };

  const retain = num(env.RETAIN, DEFAULTS.retain);
  const { pruned, kept } = await pruneTracked(env, gens, retain, gen);
  if (pruned > 0) next.gens = gens.slice(pruned);
  await env.BACKUPS.put(KEY_STATS, JSON.stringify(next));

  // No re-assert write here on purpose. KV is eventually consistent, so a cold
  // start within ~60s of a push may still see the previous generation. That is
  // the safe direction (restore to something older, never a partial write) and
  // the client reports which generation it actually restored from. Re-writing
  // the pointer and stats would double the KV writes per push for no gain.
  return json({
    ok: true,
    generation: gen,
    bytes,
    pushesToday: next.pushes,
    dailyPushBudget: cap,
    pushesLeft: Math.max(0, cap - next.pushes),
    retained: kept,
    pruned,
  });
}

async function handleGenerations(env) {
  const latest = await latestGen(env);
  const budget = await budgetState(env);

  // The tracked index is authoritative. list() is merged in for visibility, but
  // it lags by up to 60s and can omit very recent keys, so anything it reports
  // is marked as unconfirmed rather than treated as the source of truth.
  let listed = [];
  let listError = null;
  try {
    const res = await env.BACKUPS.list({ prefix: KEY_GEN_PREFIX, limit: 1000 });
    listed = res.keys.map((k) => k.name.slice(KEY_GEN_PREFIX.length)).filter(isValidGen);
  } catch (err) {
    listError = String(err && err.message ? err.message : err);
  }

  const confirmed = new Set(listed);
  const all = [...new Set([...budget.gens, ...listed])].sort().reverse();

  const generations = all.map((gen) => {
    const epoch = genEpoch(gen);
    return {
      generation: gen,
      at: epoch ? new Date(epoch).toISOString() : null,
      ageSeconds: epoch ? Math.max(0, Math.round((Date.now() - epoch) / 1000)) : null,
      isLatest: gen === latest,
      tracked: budget.gens.includes(gen),
      // false means list() has not caught up yet, not that the key is missing.
      confirmedByList: confirmed.has(gen),
    };
  });

  return json({
    ok: true,
    latest,
    count: generations.length,
    trackedCount: budget.gens.length,
    listLagged: listError === null && listed.length < budget.gens.length,
    listError,
    generations,
  });
}

async function handleGet(env, gen) {
  if (gen && !isValidGen(gen)) return json({ ok: false, error: 'bad_generation' }, 400);
  const target = gen || (await latestGen(env));
  if (!target) return json({ ok: false, error: 'no_backup' }, 404);

  const value = await env.BACKUPS.get(KEY_GEN_PREFIX + target, 'arrayBuffer');
  if (value === null) return json({ ok: false, error: 'not_found', generation: target }, 404);

  return new Response(value, {
    status: 200,
    headers: {
      'Content-Type': 'application/octet-stream',
      'Content-Length': String(value.byteLength),
      'Cache-Control': 'no-store',
      'X-Content-Type-Options': 'nosniff',
      'X-Backup-Generation': target,
      'X-Robots-Tag': 'noindex, nofollow',
    },
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // Unauthenticated, and deliberately reveals no database content — only
    // whether the bridge is configured and how stale the newest snapshot is.
    if (url.pathname === '/health') {
      return json(await readStats(env));
    }

    if (url.pathname === '/db' || url.pathname.startsWith('/db/')) {
      const authorized = await tokenMatches(bearerOf(request), env.BRIDGE_TOKEN);
      if (!authorized) {
        return json({ ok: false, error: 'unauthorized' }, 401);
      }

      if (url.pathname === '/db') {
        if (request.method === 'GET' || request.method === 'HEAD') {
          return handleGet(env, null);
        }
        if (request.method === 'PUT') {
          return handlePut(request, env);
        }
        return json({ ok: false, error: 'method_not_allowed' }, 405);
      }

      if (request.method === 'GET' || request.method === 'HEAD') {
        return handleGet(env, url.pathname.slice('/db/'.length));
      }
      return json({ ok: false, error: 'method_not_allowed' }, 405);
    }

    if (url.pathname === '/generations' || url.pathname === '/stats') {
      const authorized = await tokenMatches(bearerOf(request), env.BRIDGE_TOKEN);
      if (!authorized) {
        return json({ ok: false, error: 'unauthorized' }, 401);
      }
      if (url.pathname === '/stats') {
        return json(await readStats(env));
      }
      return request.method === 'GET'
        ? handleGenerations(env)
        : json({ ok: false, error: 'method_not_allowed' }, 405);
    }

    // Rebuild the tracked generation index from list(). Only needed if the index
    // has drifted (for example after manually deleting keys in the KV console).
    if (url.pathname === '/reconcile') {
      const authorized = await tokenMatches(bearerOf(request), env.BRIDGE_TOKEN);
      if (!authorized) {
        return json({ ok: false, error: 'unauthorized' }, 401);
      }
      if (request.method !== 'POST') {
        return json({ ok: false, error: 'method_not_allowed' }, 405);
      }
      return json(await reconcile(env));
    }

    return json({ ok: false, error: 'not_found' }, 404);
  },
};
