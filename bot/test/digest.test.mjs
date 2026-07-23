import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import {
  TIMEZONE,
  EMBED_TITLE_MAX,
  EMBED_DESCRIPTION_MAX,
  EMBED_FIELD_VALUE_MAX,
  EMBED_TOTAL_MAX,
  MAX_PER_CATEGORY,
  defaultWeek,
  defaultDigestPath,
  loadDigest,
  formatReasoning,
  embedCharCount,
  buildCandidateEmbed,
  buildCategoryMessages,
  renderMessagePreview,
  postDigest,
} from '../src/digest.mjs';
import { CandidateStore, REPO_ROOT } from '../src/store.mjs';
import { parseArgs } from '../src/post-digest.mjs';

// ------------------------------------------------------------ fixtures

let nextId = 0;
const cand = (over = {}) => ({
  candidate_id: `cand-${(nextId += 1)}`,
  rank_in_category: 1,
  score: 10.75,
  pov_player: { name: 'Milton', userid: 35 },
  map: 'e1m2',
  matchdate: '2026-06-09',
  teams: [{ name: 'red', score: 227 }, { name: 'blue', score: 180 }],
  window: { start_s: 561.8, end_s: 596.8 },
  description: 'Milton grabs quad at 9:26 on e1m2 and tears through 9 frags.',
  hub_url: 'https://hub.quakeworld.nu/games?gameId=221160&from=561&track=35',
  hub_url_missing: false,
  metrics_summary: { frags_in_run: 9, rl_kills: 7, rank: 1, crosscheck: { ok: true } },
  ...over,
});

// Issue #17 measurement: a full 3-candidate category, a 1-candidate
// category, an empty category — plus an over-cap (4) category to prove the
// bot's own max-3 guard against hand-edited digests.
const fixtureDigest = () => ({
  week: '2026-W24',
  week_start: '2026-06-08',
  week_end: '2026-06-14',
  generated_at: '2026-06-10T13:13:18+00:00',
  sections: [
    {
      category: 'QUAD RUNNER',
      label: 'QUAD RUNNER OF THE WEEK — candidates',
      note: null,
      candidates: [
        cand({ candidate_id: 'q1', rank_in_category: 1 }),
        cand({ candidate_id: 'q2', rank_in_category: 2, score: 10.25, pov_player: { name: 'BLooD_DoG(D_P)', userid: 76 } }),
        cand({ candidate_id: 'q3', rank_in_category: 3, score: 9.75 }),
      ],
    },
    {
      category: 'TEAM KILLER',
      label: 'TEAM KILLER OF THE WEEK — candidates',
      note: null,
      candidates: [
        cand({
          candidate_id: 't1',
          pov_player: { name: 'niomic', userid: 28 },
          description: 'niomic takes out 2 teammates on dm3 in 2s.',
          hub_url: 'https://hub.quakeworld.nu/games?gameId=221149&from=889&track=28',
          metrics_summary: { tk_count: 2, span_s: 1.568 },
          score: 2.23,
        }),
      ],
    },
    {
      category: 'PENT GRIND',
      label: 'PENT GRIND OF THE WEEK — candidates',
      note: 'no qualifying PENT GRIND moment this week',
      candidates: [],
    },
    {
      category: 'PENT RUNNER',
      label: 'PENT RUNNER OF THE WEEK — candidates',
      note: null,
      candidates: [
        cand({ candidate_id: 'p1' }), cand({ candidate_id: 'p2' }),
        cand({ candidate_id: 'p3' }), cand({ candidate_id: 'p4' }),
      ],
    },
  ],
});

const mockChannel = ({ failFor = [] } = {}) => {
  const sent = [];
  return {
    sent,
    send: async (payload) => {
      if (failFor.some((cat) => payload.content.includes(cat))) {
        throw new Error('Discord 50013: Missing Permissions');
      }
      sent.push(payload);
      return { id: `msg-${sent.length}` };
    },
  };
};

const mockStore = ({ failFor = [] } = {}) => {
  const flips = [];
  return {
    flips,
    updateStatus: async (id, status) => {
      if (failFor.includes(id)) throw new Error(`illegal transition for ${id}`);
      flips.push([id, status]);
    },
  };
};

// --------------------------------------------------- week math (Stockholm)

test('defaultWeek mid-week is the current ISO week', () => {
  // Wed 2026-06-10 (the real W24 digest week).
  assert.equal(defaultWeek(new Date('2026-06-10T12:00:00Z')), '2026-W24');
});

test('defaultWeek on Monday is the week that just finished', () => {
  // Mon 2026-06-15 10:00 Stockholm — the Monday-20:00 run covers W24.
  assert.equal(defaultWeek(new Date('2026-06-15T08:00:00Z')), '2026-W24');
});

test('defaultWeek uses the Europe/Stockholm calendar, not UTC', () => {
  // 23:00 UTC Monday = 01:00 Tuesday in Stockholm (CEST): a UTC computation
  // would still see Monday and answer W24; Stockholm says Tuesday → W25.
  assert.equal(defaultWeek(new Date('2026-06-15T23:00:00Z')), '2026-W25');
});

test('defaultDigestPath points at state/digests/<week>.json', () => {
  const path = defaultDigestPath(new Date('2026-06-10T12:00:00Z'));
  assert.equal(path, join(REPO_ROOT, 'state', 'digests', '2026-W24.json'));
});

// -------------------------------------------------------------- digest I/O

test('loadDigest parses a #11-shaped file and rejects malformed ones', () => {
  const dir = mkdtempSync(join(tmpdir(), 'clipsmith-digest-'));
  try {
    const good = join(dir, 'good.json');
    writeFileSync(good, JSON.stringify(fixtureDigest()));
    assert.equal(loadDigest(good).week, '2026-W24');

    const bad = join(dir, 'bad.json');
    writeFileSync(bad, '{"week": "2026-W24"}'); // no sections[]
    assert.throws(() => loadDigest(bad), /#11 shape/);

    const broken = join(dir, 'broken.json');
    writeFileSync(broken, '{nope');
    assert.throws(() => loadDigest(broken), /cannot load digest/);

    assert.throws(() => loadDigest(join(dir, 'absent.json')),
      /clipsmith\.digest build/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// --------------------------------------------------------------- rendering

test('one message per category with the spec title', () => {
  const messages = buildCategoryMessages(fixtureDigest());
  assert.equal(messages.length, 4);
  for (const [i, category] of
    ['QUAD RUNNER', 'TEAM KILLER', 'PENT GRIND', 'PENT RUNNER'].entries()) {
    assert.equal(messages[i].category, category);
    assert.match(messages[i].content,
      new RegExp(`\\*\\*${category} OF THE WEEK — candidates\\*\\*`));
    assert.match(messages[i].content, new RegExp(TIMEZONE.replace('/', '\\/')));
  }
});

test('full category renders one embed per candidate with the contract fields', () => {
  const [quad] = buildCategoryMessages(fixtureDigest());
  assert.equal(quad.embeds.length, 3);
  assert.deepEqual(quad.candidateIds, ['q1', 'q2', 'q3']);
  assert.equal(quad.components.length, 1);
  assert.equal(quad.components[0].components.length, 3);
  assert.deepEqual(
    quad.components[0].components.map((b) => b.label),
    Array(3).fill('Select as QUAD RUNNER OF THE WEEK'),
  );
  assert.ok(quad.components[0].components.every(
    (b) => b.custom_id.startsWith('clipsmith:select:')),
  );
  const e = quad.embeds[0];
  assert.match(e.title, /^#1 Milton — e1m2, 2026-06-09$/);
  assert.match(e.description, /Milton grabs quad at 9:26/);
  const field = (name) => e.fields.find((f) => f.name === name)?.value;
  assert.match(field('POV'), /Milton \(userid 35\)/);
  assert.match(field('POV'), /red 227 vs blue 180/);
  assert.match(field('Why it ranked'), /score 10\.75/);
  assert.match(field('Why it ranked'), /frags_in_run=9/);
  assert.match(field('Why it ranked'), /rl_kills=7/);
  assert.equal(field('Watch'),
    'https://hub.quakeworld.nu/games?gameId=221160&from=561&track=35');
});

test('1-candidate category renders exactly one embed (min-1 honored)', () => {
  const tk = buildCategoryMessages(fixtureDigest())[1];
  assert.equal(tk.embeds.length, 1);
  assert.match(tk.embeds[0].fields.find((f) => f.name === 'Why it ranked').value,
    /tk_count=2/);
});

test('empty category posts the explicit no-qualifying-moment message, no embeds', () => {
  const grind = buildCategoryMessages(fixtureDigest())[2];
  assert.equal(grind.embeds.length, 0);
  assert.match(grind.content, /no qualifying PENT GRIND moment this week/);
});

test('empty category without a digest note still gets the explicit sentence', () => {
  const digest = fixtureDigest();
  digest.sections[2].note = null;
  const grind = buildCategoryMessages(digest)[2];
  assert.match(grind.content, /no qualifying PENT GRIND moment this week/);
});

test('no category ever exceeds 3 embeds (max-3 guard on over-full input)', () => {
  const messages = buildCategoryMessages(fixtureDigest());
  for (const msg of messages) {
    assert.ok(msg.embeds.length <= MAX_PER_CATEGORY,
      `${msg.category} has ${msg.embeds.length} embeds`);
  }
  const pent = messages[3];
  assert.equal(pent.embeds.length, 3);
  assert.equal(pent.truncated, 1);
  assert.deepEqual(pent.candidateIds, ['p1', 'p2', 'p3']); // p4 dropped
});

test('missing hub_url renders the explicit missing-link note', () => {
  const e = buildCandidateEmbed(cand({ hub_url: '', hub_url_missing: true }));
  assert.match(e.fields.find((f) => f.name === 'Watch').value,
    /hub link missing/);
});

test('embed limits guarded: title 256, description 4096, total 6000', () => {
  const monster = cand({
    description: 'x'.repeat(10_000),
    pov_player: { name: 'n'.repeat(400), userid: 1 },
    metrics_summary: Object.fromEntries(
      Array.from({ length: 80 }, (_, i) => [`metric_${i}`, 'v'.repeat(30)])),
  });
  const e = buildCandidateEmbed(monster);
  assert.ok(e.title.length <= EMBED_TITLE_MAX, `title ${e.title.length}`);
  assert.ok(e.description.length <= EMBED_DESCRIPTION_MAX);
  for (const f of e.fields) assert.ok(f.value.length <= 1024);
  assert.ok(embedCharCount(e) <= EMBED_TOTAL_MAX,
    `total ${embedCharCount(e)} > ${EMBED_TOTAL_MAX}`);
});

test('aggregate 6000-char budget holds across all embeds of one message', () => {
  // Regression: three candidates whose embeds each pass the per-embed guard
  // but together exceed Discord's per-MESSAGE 6000-char total.
  const digest = fixtureDigest();
  digest.sections = [{
    category: 'QUAD RUNNER',
    label: 'QUAD RUNNER OF THE WEEK — candidates',
    note: null,
    candidates: [1, 2, 3].map((i) => cand({
      candidate_id: `big-${i}`,
      rank_in_category: i,
      description: `big-${i} ${'x'.repeat(10_000)}`,
    })),
  }];
  const [msg] = buildCategoryMessages(digest);
  assert.equal(msg.embeds.length, 3);
  const aggregate = msg.embeds.reduce((n, e) => n + embedCharCount(e), 0);
  assert.ok(aggregate <= EMBED_TOTAL_MAX,
    `aggregate ${aggregate} > ${EMBED_TOTAL_MAX}`);
  for (const [i, e] of msg.embeds.entries()) {
    // The cut hits only the prose: every embed keeps a (truncated)
    // description plus its full POV / Why-it-ranked / Watch fields.
    assert.ok(e.description.length > 0, `embed ${i + 1} lost its description`);
    assert.ok(e.description.endsWith('…'),
      `embed ${i + 1} description not truncated`);
    assert.match(e.description, new RegExp(`^big-${i + 1} x`));
    const field = (name) => e.fields.find((f) => f.name === name)?.value;
    assert.match(field('POV'), /Milton \(userid 35\)/);
    assert.match(field('Why it ranked'), /score /);
    assert.equal(field('Watch'),
      'https://hub.quakeworld.nu/games?gameId=221160&from=561&track=35');
  }
});

test('aggregate budget still holds when fields alone overflow', () => {
  // Review follow-up: descriptions are the first thing trimmed, but a
  // pathological digest can still exceed Discord's per-message 6000-char
  // aggregate budget through preserved field values alone.
  const digest = fixtureDigest();
  digest.sections = [{
    category: 'QUAD RUNNER',
    label: 'QUAD RUNNER OF THE WEEK — candidates',
    note: null,
    candidates: [1, 2, 3].map((i) => cand({
      candidate_id: `field-heavy-${i}`,
      rank_in_category: i,
      description: '',
      pov_player: { name: `player-${i}-${'p'.repeat(2_000)}`, userid: i },
      teams: [
        { name: `red-${'r'.repeat(1_000)}`, score: 1 },
        { name: `blue-${'b'.repeat(1_000)}`, score: 2 },
      ],
      hub_url: `https://hub.quakeworld.nu/games?gameId=${i}&from=1&track=${i}&${'u'.repeat(2_000)}`,
      metrics_summary: Object.fromEntries(
        Array.from({ length: 80 }, (_, j) => [`metric_${j}`, 'm'.repeat(40)])),
    })),
  }];
  const [msg] = buildCategoryMessages(digest);
  const aggregate = msg.embeds.reduce((n, e) => n + embedCharCount(e), 0);
  assert.ok(aggregate <= EMBED_TOTAL_MAX,
    `aggregate ${aggregate} > ${EMBED_TOTAL_MAX}`);
  for (const embed of msg.embeds) {
    for (const field of embed.fields) {
      assert.ok(field.value.length >= 1, `${field.name} became empty`);
      assert.ok(field.value.length <= EMBED_FIELD_VALUE_MAX,
        `${field.name} exceeds per-field cap`);
    }
  }
});

test('formatReasoning keeps scalars, skips nested objects', () => {
  const line = formatReasoning(cand());
  assert.match(line, /score 10\.75 · rank #1/);
  assert.match(line, /frags_in_run=9/);
  assert.doesNotMatch(line, /crosscheck/); // nested object skipped
});

test('formatReasoning rounds float noise for presentation', () => {
  const line = formatReasoning(cand({
    score: 2.2304000000000004,
    metrics_summary: { first_tk_s: 904.2090000000001, span_s: 1.568 },
  }));
  assert.match(line, /score 2\.23 · rank #1/);
  assert.match(line, /first_tk_s=904\.209 /);
  assert.match(line, /span_s=1\.568/);
});

test('renderMessagePreview shows content, embeds and the empty marker', () => {
  const [quad, , grind] = buildCategoryMessages(fixtureDigest());
  const full = renderMessagePreview(quad);
  assert.match(full, /QUAD RUNNER OF THE WEEK — candidates/);
  assert.match(full, /embed 1: #1 Milton/);
  assert.match(full, /Watch: https:\/\/hub\.quakeworld\.nu/);
  assert.match(renderMessagePreview(grind), /no embeds — empty category/);
});

// ----------------------------------------------------------------- posting

test('postDigest sends one message per category and flips after success', async () => {
  const channel = mockChannel();
  const store = mockStore();
  const summary = await postDigest({ digest: fixtureDigest(), channel, store });
  assert.equal(channel.sent.length, 4);
  assert.deepEqual(summary.posted,
    ['QUAD RUNNER', 'TEAM KILLER', 'PENT GRIND', 'PENT RUNNER']);
  // q1-3, t1, p1-3 flipped to posted; the empty category flips nothing;
  // the over-cap p4 was never posted so it must not flip.
  assert.deepEqual(store.flips.map(([id]) => id),
    ['q1', 'q2', 'q3', 't1', 'p1', 'p2', 'p3']);
  assert.ok(store.flips.every(([, status]) => status === 'posted'));
  assert.deepEqual(summary.postFailures, []);
  assert.deepEqual(summary.flipFailures, []);
});

test('a failed category post does NOT flip its candidates; others still flip', async () => {
  const channel = mockChannel({ failFor: ['TEAM KILLER'] });
  const store = mockStore();
  const summary = await postDigest({ digest: fixtureDigest(), channel, store });
  assert.deepEqual(summary.postFailures, ['TEAM KILLER']);
  const flipped = store.flips.map(([id]) => id);
  assert.ok(!flipped.includes('t1'), 't1 flipped despite failed post');
  assert.deepEqual(flipped, ['q1', 'q2', 'q3', 'p1', 'p2', 'p3']);
});

test('a failed flip is recorded, never fatal, and does not stop the rest', async () => {
  const channel = mockChannel();
  const store = mockStore({ failFor: ['q2'] });
  const summary = await postDigest({ digest: fixtureDigest(), channel, store });
  assert.deepEqual(summary.flipFailures, ['q2']);
  assert.ok(summary.flipped.includes('q3'), 'q3 must still flip after q2 fails');
});

test('dry-run renders all messages, sends nothing, writes nothing', async () => {
  const channel = mockChannel();
  const store = mockStore();
  const logged = [];
  const summary = await postDigest({
    digest: fixtureDigest(), channel, store,
    dryRun: true, log: (line) => logged.push(line),
  });
  assert.equal(channel.sent.length, 0);
  assert.equal(store.flips.length, 0);
  assert.deepEqual(summary.posted, []);
  const text = logged.join('\n');
  assert.match(text, /QUAD RUNNER OF THE WEEK — candidates/);
  assert.match(text, /no qualifying PENT GRIND moment this week/);
});

// ------------------------------------------------------------- CLI parsing

test('parseArgs handles --digest/--channel/--dry-run and rejects junk', () => {
  assert.deepEqual(parseArgs([]), { digest: null, channel: null, dryRun: false });
  assert.deepEqual(
    parseArgs(['--digest', 'd.json', '--channel', '123', '--dry-run']),
    { digest: 'd.json', channel: '123', dryRun: true });
  assert.throws(() => parseArgs(['--digest']), /--digest needs a value/);
  assert.throws(() => parseArgs(['--digest', '--dry-run']), /needs a value/);
  assert.throws(() => parseArgs(['--frobnicate']), /unknown argument/);
});

// ------------------------------------------------- store layer (unit)

test('store.updateStatus issues update-status with --db before the subcommand', async () => {
  const calls = [];
  const store = new CandidateStore({
    dbPath: 'X.db', exec: async (args) => { calls.push(args); return ''; },
  });
  await store.updateStatus('abc', 'posted');
  assert.deepEqual(calls, [
    ['-m', 'clipsmith.store', '--db', 'X.db', 'update-status', 'abc', 'posted'],
  ]);
});

// -------------------------------------------- integration (seeded store)

// Real python CLI against a seeded temp store (--db): proves the
// candidate→posted flip end-to-end including the lifecycle rules, without
// ever touching state/candidates.db. Skipped when python is absent.
test('integration: postDigest flips only successfully posted candidates in a temp store', async (t) => {
  const pythonBin = process.env.CLIPSMITH_PYTHON ?? 'python';
  const dir = mkdtempSync(join(tmpdir(), 'clipsmith-digest-db-'));
  const dbPath = join(dir, 'candidates.db');
  const seed = (category, name) => {
    const c = {
      category, demo_sha: 'a'.repeat(64), map: 'dm3', matchdate: '2026-06-09',
      teams: [{ name: 'red', score: 200 }, { name: 'blue', score: 150 }],
      pov_player: { name, userid: 42 },
      window: { start_s: 100, end_s: 135 },
      score: 5.0, metrics: { frags: 5 },
      description: `${name} does a thing`, hub_url: '', status: 'candidate',
    };
    return execFileSync(pythonBin,
      ['-m', 'clipsmith.store', '--db', dbPath, 'add'],
      { cwd: REPO_ROOT, input: JSON.stringify(c), windowsHide: true },
    ).toString().trim(); // add prints the id
  };
  try {
    let quadId;
    try {
      quadId = seed('QUAD RUNNER', 'kippo');
    } catch (err) {
      t.skip(`python CLI unavailable: ${err.message}`);
      return;
    }
    const tkId = seed('TEAM KILLER', 'niomic');     // its post will fail
    const untouchedId = seed('PENT RUNNER', 'bps'); // not in the digest

    const digest = fixtureDigest();
    digest.sections = [
      { ...digest.sections[0], candidates: [cand({ candidate_id: quadId })] },
      { ...digest.sections[1], candidates: [cand({ candidate_id: tkId })] },
    ];
    const store = new CandidateStore({ dbPath, pythonBin });
    const channel = mockChannel({ failFor: ['TEAM KILLER'] });
    const summary = await postDigest({ digest, channel, store });

    assert.deepEqual(summary.flipped, [quadId]);
    assert.deepEqual(summary.postFailures, ['TEAM KILLER']);

    const byId = Object.fromEntries(
      (await store.list()).map((c) => [c.id, c.status]));
    assert.equal(byId[quadId], 'posted', 'posted candidate must flip');
    assert.equal(byId[tkId], 'candidate', 'failed post must not flip');
    assert.equal(byId[untouchedId], 'candidate', 'untouched candidate unchanged');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
