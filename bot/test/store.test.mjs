import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import {
  CandidateStore,
  StoreUnreachableError,
  STATUSES,
  REPO_ROOT,
  DEFAULT_EXEC_TIMEOUT_MS,
  DEFAULT_EXEC_MAX_BUFFER,
} from '../src/store.mjs';

const fakeCandidate = (status) => JSON.stringify({ id: 'x', status });

test('list parses JSON lines from injected exec', async () => {
  const store = new CandidateStore({
    exec: async () => `${fakeCandidate('candidate')}\n${fakeCandidate('posted')}\n`,
  });
  const cands = await store.list();
  assert.equal(cands.length, 2);
  assert.equal(cands[1].status, 'posted');
});

test('countsByStatus zero-fills every known status', async () => {
  const store = new CandidateStore({
    exec: async () => `${fakeCandidate('candidate')}\n${fakeCandidate('candidate')}\n${fakeCandidate('published')}\n`,
  });
  const counts = await store.countsByStatus();
  for (const s of STATUSES) assert.ok(s in counts, `missing ${s}`);
  assert.equal(counts.candidate, 2);
  assert.equal(counts.published, 1);
  assert.equal(counts.rejected, 0);
});

test('reachable() is false when the CLI fails', async () => {
  const store = new CandidateStore({
    exec: async () => { throw new Error('boom'); },
  });
  assert.equal(await store.reachable(), false);
});

test('cliArgs places --db before the subcommand (store.py global flag)', () => {
  const store = new CandidateStore({ dbPath: 'X.db', exec: async () => '' });
  assert.deepEqual(
    store.cliArgs('list', ['--status', 'posted']),
    ['-m', 'clipsmith.store', '--db', 'X.db', 'list', '--status', 'posted'],
  );
});

test('exec guard rails default sanely and are constructor-injectable', () => {
  const def = new CandidateStore({ exec: async () => '' });
  assert.equal(def.timeoutMs, DEFAULT_EXEC_TIMEOUT_MS);
  assert.equal(def.maxBuffer, DEFAULT_EXEC_MAX_BUFFER);
  assert.ok(DEFAULT_EXEC_MAX_BUFFER > 1024 * 1024, 'must exceed node default 1 MiB');
  const tuned = new CandidateStore({ exec: async () => '', timeoutMs: 5, maxBuffer: 99 });
  assert.equal(tuned.timeoutMs, 5);
  assert.equal(tuned.maxBuffer, 99);
});

test('default exec surfaces spawn failure as StoreUnreachableError', async () => {
  const store = new CandidateStore({ pythonBin: 'definitely-not-a-real-binary-xyz' });
  await assert.rejects(store.list(), StoreUnreachableError);
  await assert.rejects(store.list(), /store CLI failed/);
});

test('default exec enforces the timeout (hung child is killed, typed error)', async () => {
  // Use node as the "python" binary so the test needs no Python; the args
  // are passed straight through, so call exec directly with a sleeper.
  const store = new CandidateStore({ pythonBin: process.execPath, timeoutMs: 200 });
  await assert.rejects(
    store.exec(['-e', 'setTimeout(() => {}, 60000)']),
    StoreUnreachableError,
  );
});

// Integration: real python CLI against a seeded temp store. Proves the
// Node→Python seam (issue #16 measurement). Skipped when python is absent.
test('integration: counts from a python-seeded temp store', async (t) => {
  const pythonBin = process.env.CLIPSMITH_PYTHON ?? 'python';
  const dir = mkdtempSync(join(tmpdir(), 'clipsmith-store-'));
  const dbPath = join(dir, 'candidates.db');
  const seed = (status) => {
    const cand = {
      category: 'QUAD RUNNER', demo_sha: 'a'.repeat(64), map: 'dm3',
      matchdate: '2026-06-08',
      teams: [{ name: 'fu', score: 200 }, { name: 'mix', score: 150 }],
      pov_player: { name: 'kippo', userid: 42 },
      window: { start_s: 100, end_s: 135 },
      score: 5.0, metrics: { frags: 5 },
      description: 'test', hub_url: '', status,
    };
    execFileSync(pythonBin, ['-m', 'clipsmith.store', '--db', dbPath, 'add'], {
      cwd: REPO_ROOT, input: JSON.stringify(cand), windowsHide: true,
    });
  };
  try {
    try {
      seed('candidate');
    } catch (err) {
      t.skip(`python CLI unavailable: ${err.message}`);
      return;
    }
    seed('candidate');
    seed('posted');

    const store = new CandidateStore({ dbPath, pythonBin });
    assert.equal(await store.reachable(), true);
    const counts = await store.countsByStatus();
    assert.equal(counts.candidate, 2);
    assert.equal(counts.posted, 1);
    assert.equal(counts.published, 0);

    const posted = await store.list({ status: 'posted' });
    assert.equal(posted.length, 1);
    assert.equal(posted[0].pov_player.userid, 42);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
