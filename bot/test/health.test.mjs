import { test } from 'node:test';
import assert from 'node:assert/strict';

import { buildHealthLine } from '../src/health.mjs';
import { STATUSES } from '../src/store.mjs';

test('health line carries version, store ok, and every status count', () => {
  const counts = Object.fromEntries(STATUSES.map((s) => [s, 0]));
  counts.candidate = 3;
  counts.posted = 1;
  const line = buildHealthLine({ version: '0.1.0', storeOk: true, counts });
  assert.match(line, /clipsmith-bot v0\.1\.0 up/);
  assert.match(line, /store: ok/);
  assert.match(line, /candidate=3/);
  assert.match(line, /posted=1/);
  for (const s of STATUSES) assert.match(line, new RegExp(`${s}=\\d`));
});

test('health line marks an unreachable store', () => {
  const line = buildHealthLine({ version: '0.1.0', storeOk: false });
  assert.match(line, /store: UNREACHABLE/);
  assert.match(line, /n\/a/);
});
