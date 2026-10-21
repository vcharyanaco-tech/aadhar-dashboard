import test from 'node:test';
import assert from 'node:assert/strict';
import { gzipSync } from 'node:zlib';

import worker, { gunzipLimited, isGzip } from './worker.js';

function makeKv() {
  const values = new Map();
  return {
    async get(key, type) {
      const value = values.get(key);
      if (value === undefined) return null;
      if (type === 'arrayBuffer') {
        return value instanceof ArrayBuffer
          ? value
          : new TextEncoder().encode(String(value)).buffer;
      }
      if (type === 'text') {
        return typeof value === 'string' ? value : new TextDecoder().decode(value);
      }
      return value;
    },
    async put(key, value) {
      values.set(key, value);
    },
    async delete(key) {
      values.delete(key);
    },
    async list({ prefix } = {}) {
      return {
        keys: [...values.keys()]
          .filter((name) => !prefix || name.startsWith(prefix))
          .map((name) => ({ name })),
      };
    },
  };
}

function testEnv() {
  return {
    BRIDGE_TOKEN: 'test-token',
    BACKUPS: makeKv(),
    DAILY_PUSH_BUDGET: '120',
    MAX_BYTES: String(20 * 1024 * 1024),
    RETAIN: '12',
  };
}

const AUTH = { Authorization: 'Bearer test-token' };

test('gzip bodies are recognised and round-trip to the original SQLite bytes', async () => {
  const raw = Buffer.concat([Buffer.from('SQLite format 3\u0000'), Buffer.alloc(8192, 7)]);
  const gz = gzipSync(raw, { level: 6 });

  assert.equal(isGzip(gz), true);
  assert.equal(isGzip(raw), false);

  const restored = Buffer.from(await gunzipLimited(gz, 1024 * 1024));
  assert.deepEqual(restored, raw);
});

test('decompression refuses to materialise more than the configured cap', async () => {
  const gz = gzipSync(Buffer.alloc(4096, 1));

  await assert.rejects(() => gunzipLimited(gz, 1024), /too_large/);
});

test('a non-gzip body is rejected rather than stored', async () => {
  await assert.rejects(() => gunzipLimited(Buffer.from('not a gzip stream'), 1024));
});

test('a gzipped PUT is decompressed, validated and served back as raw SQLite', async () => {
  const env = testEnv();
  const raw = Buffer.concat([Buffer.from('SQLite format 3\u0000'), Buffer.alloc(16384, 3)]);
  const gz = gzipSync(raw, { level: 6 });

  const put = await worker.fetch(
    new Request('https://backup.test/db', {
      method: 'PUT',
      headers: { ...AUTH, 'X-Backup-Encoding': 'gzip' },
      body: gz,
    }),
    env,
  );
  assert.equal(put.status, 200);
  const info = await put.json();
  assert.equal(info.encoding, 'gzip');
  assert.equal(info.bytes, raw.length);
  assert.equal(info.receivedBytes, gz.length);

  const got = await worker.fetch(
    new Request('https://backup.test/db', { headers: AUTH }),
    env,
  );
  assert.equal(got.status, 200);
  assert.deepEqual(Buffer.from(await got.arrayBuffer()), raw);
});

test('a raw PUT still works, so old clients survive the Worker deploy', async () => {
  const env = testEnv();
  const raw = Buffer.concat([Buffer.from('SQLite format 3\u0000'), Buffer.alloc(2048, 9)]);

  const put = await worker.fetch(
    new Request('https://backup.test/db', { method: 'PUT', headers: AUTH, body: raw }),
    env,
  );
  assert.equal(put.status, 200);
  const info = await put.json();
  assert.equal(info.encoding, 'raw');
  assert.equal(info.bytes, raw.length);

  const got = await worker.fetch(
    new Request('https://backup.test/db', { headers: AUTH }),
    env,
  );
  assert.deepEqual(Buffer.from(await got.arrayBuffer()), raw);
});

test('a body advertised as gzip but not gzip is refused before any write', async () => {
  const env = testEnv();
  const put = await worker.fetch(
    new Request('https://backup.test/db', {
      method: 'PUT',
      headers: { ...AUTH, 'X-Backup-Encoding': 'gzip' },
      body: Buffer.from('not a gzip stream'),
    }),
    env,
  );
  assert.equal(put.status, 400);
  const info = await put.json();
  assert.equal(info.error, 'bad_gzip');
});
