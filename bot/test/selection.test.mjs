import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { buildCategoryMessages } from '../src/digest.mjs';
import { FileRenderQueue } from '../src/render-queue.mjs';
import {
  handleSelectionInteraction,
  handleTextSelectionMessage,
  markSelectedComponents,
  selectionsFromMessage,
} from '../src/selection.mjs';
import { CandidateStore, REPO_ROOT } from '../src/store.mjs';

const pythonBin = process.env.CLIPSMITH_PYTHON ?? 'python';

const candidate = (over = {}) => ({
  category: 'QUAD RUNNER',
  demo_sha: 'a'.repeat(64),
  map: 'dm3',
  matchdate: '2026-06-10',
  teams: [{ name: 'red', score: 200 }, { name: 'blue', score: 150 }],
  pov_player: { name: 'Milton', userid: 35 },
  window: { start_s: 100, end_s: 135 },
  score: 9.5,
  metrics: { frags: 9 },
  description: 'Milton does a thing.',
  hub_url: 'https://hub.quakeworld.nu/games?gameId=1&from=100&track=35',
  status: 'candidate',
  ...over,
});

function seedCandidate(dbPath, cand, status = 'posted') {
  const id = execFileSync(
    pythonBin,
    ['-m', 'clipsmith.store', '--db', dbPath, 'add'],
    { cwd: REPO_ROOT, input: JSON.stringify(cand), windowsHide: true },
  ).toString().trim();
  if (status === 'posted' || status === 'selected') {
    execFileSync(
      pythonBin,
      ['-m', 'clipsmith.store', '--db', dbPath, 'update-status', id, 'posted'],
      { cwd: REPO_ROOT, windowsHide: true },
    );
  }
  if (status === 'selected') {
    execFileSync(
      pythonBin,
      ['-m', 'clipsmith.store', '--db', dbPath, 'update-status', id, 'selected'],
      { cwd: REPO_ROOT, windowsHide: true },
    );
  }
  return id;
}

function makeDigestMessage(ids) {
  const digest = {
    week: '2026-W24',
    week_start: '2026-06-08',
    week_end: '2026-06-14',
    sections: [{
      category: 'QUAD RUNNER',
      label: 'QUAD RUNNER OF THE WEEK — candidates',
      note: null,
      candidates: ids.map((id, i) => ({
        candidate_id: id,
        rank_in_category: i + 1,
        score: 10 - i,
        pov_player: { name: `player-${i + 1}`, userid: 30 + i },
        map: 'dm3',
        matchdate: '2026-06-10',
        teams: [{ name: 'red', score: 200 }, { name: 'blue', score: 150 }],
        description: `candidate ${i + 1}`,
        hub_url: `https://hub.quakeworld.nu/games?gameId=${i + 1}`,
        metrics_summary: { frags: 9 - i },
      })),
    }],
  };
  const [msg] = buildCategoryMessages(digest);
  return { ...msg, id: 'digest-message-1' };
}

// Mirrors the live discord.js button-interaction surface the handler uses:
// deferUpdate() to ack, editReply() for the component edit, followUp() for
// ephemeral failures. `events` records call order for the ack-first test.
function makeInteraction(customId, message) {
  const events = [];
  const edits = [];
  const replies = [];
  const followUps = [];
  return {
    customId,
    message,
    events,
    edits,
    replies,
    followUps,
    isButton: () => true,
    deferUpdate: async () => events.push('deferUpdate'),
    editReply: async (payload) => {
      events.push('editReply');
      edits.push(payload);
    },
    reply: async (payload) => replies.push(payload),
    followUp: async (payload) => followUps.push(payload),
  };
}

async function withStore(t, fn) {
  const dir = mkdtempSync(join(tmpdir(), 'clipsmith-selection-'));
  const dbPath = join(dir, 'candidates.db');
  const queuePath = join(dir, 'render-queue.json');
  try {
    await fn({
      dbPath,
      store: new CandidateStore({ dbPath, pythonBin }),
      queue: new FileRenderQueue({ path: queuePath }),
    });
  } catch (err) {
    if (err.code === 'ENOENT') t.skip(`python unavailable: ${err.message}`);
    else throw err;
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

test('button selection flips posted candidate to selected and queues render', async (t) => {
  await withStore(t, async ({ dbPath, store, queue }) => {
    const a = seedCandidate(dbPath, candidate({ pov_player: { name: 'A', userid: 1 } }));
    const b = seedCandidate(dbPath, candidate({ pov_player: { name: 'B', userid: 2 } }));
    const message = makeDigestMessage([a, b]);
    const interaction = makeInteraction(message.components[0].components[0].custom_id, message);

    assert.equal(await handleSelectionInteraction(interaction, { store, queue }), true);

    const byId = Object.fromEntries((await store.list()).map((c) => [c.id, c.status]));
    assert.equal(byId[a], 'selected');
    assert.equal(byId[b], 'posted');
    const pending = await queue.pending();
    assert.equal(pending.length, 1);
    assert.equal(pending[0].candidate_id, a);
    assert.equal(pending[0].category, 'QUAD RUNNER');
    assert.equal(interaction.edits.length, 1);
    assert.equal(interaction.edits[0].content, undefined);
    const [selectedButton, otherButton] = interaction.edits[0].components[0].components;
    assert.equal(selectedButton.disabled, true);
    assert.equal(selectedButton.style, 3);
    assert.equal(selectedButton.label, 'Selected as QUAD RUNNER OF THE WEEK');
    assert.equal(otherButton.disabled, false);
    assert.equal(otherButton.style, 2);
  });
});

test('re-selection reverts previous selected candidate and supersedes queue entry', async (t) => {
  await withStore(t, async ({ dbPath, store, queue }) => {
    const a = seedCandidate(dbPath, candidate({ pov_player: { name: 'A', userid: 1 } }));
    const b = seedCandidate(dbPath, candidate({ pov_player: { name: 'B', userid: 2 } }));
    const message = makeDigestMessage([a, b]);

    await handleSelectionInteraction(
      makeInteraction(message.components[0].components[0].custom_id, message),
      { store, queue },
    );
    await handleSelectionInteraction(
      makeInteraction(message.components[0].components[1].custom_id, message),
      { store, queue },
    );

    const byId = Object.fromEntries((await store.list()).map((c) => [c.id, c.status]));
    assert.equal(byId[a], 'posted');
    assert.equal(byId[b], 'selected');
    const pending = await queue.pending({ category: 'QUAD RUNNER' });
    assert.deepEqual(pending.map((e) => e.candidate_id), [b]);
  });
});

test('text fallback select 2 maps to the second candidate in the digest message', async (t) => {
  await withStore(t, async ({ dbPath, store, queue }) => {
    const a = seedCandidate(dbPath, candidate({ pov_player: { name: 'A', userid: 1 } }));
    const b = seedCandidate(dbPath, candidate({ pov_player: { name: 'B', userid: 2 } }));
    const digestMessage = makeDigestMessage([a, b]);
    const replies = [];
    const message = {
      content: 'select 2',
      reference: { messageId: digestMessage.id },
      reply: async (payload) => replies.push(payload),
    };

    assert.equal(await handleTextSelectionMessage(message, {
      store,
      queue,
      fetchMessage: async () => digestMessage,
    }), true);

    const byId = Object.fromEntries((await store.list()).map((c) => [c.id, c.status]));
    assert.equal(byId[a], 'posted');
    assert.equal(byId[b], 'selected');
    assert.deepEqual((await queue.pending()).map((e) => e.candidate_id), [b]);
    assert.match(replies[0].content, /Selected B as QUAD RUNNER OF THE WEEK/);
  });
});

test('text fallback rejects out-of-range selection with a helpful reply', async (t) => {
  await withStore(t, async ({ dbPath, store, queue }) => {
    const a = seedCandidate(dbPath, candidate({ pov_player: { name: 'A', userid: 1 } }));
    const digestMessage = makeDigestMessage([a]);
    const replies = [];
    const message = {
      content: 'select 2',
      reference: { messageId: digestMessage.id },
      reply: async (payload) => replies.push(payload),
    };

    assert.equal(await handleTextSelectionMessage(message, {
      store,
      queue,
      fetchMessage: async () => digestMessage,
    }), true);

    assert.equal((await store.list())[0].status, 'posted');
    assert.deepEqual(await queue.pending(), []);
    assert.match(replies[0].content, /Choose a number from 1 to 1/);
  });
});

// --- PR #42 review findings ---

test('interaction is acknowledged via deferUpdate before any store/queue work (P1: 3s ack window)', async () => {
  const events = [];
  const id = 'cand-1';
  const store = {
    list: async () => {
      events.push('store.list');
      return [candidate({ id, status: 'posted' })];
    },
    updateStatus: async () => events.push('store.updateStatus'),
  };
  const queue = {
    enqueue: async (cand) => {
      events.push('queue.enqueue');
      return { candidate_id: cand.id, status: 'pending' };
    },
    pending: async () => [],
  };
  const message = makeDigestMessage([id]);
  const interaction = makeInteraction(message.components[0].components[0].custom_id, message);
  interaction.deferUpdate = async () => interaction.events.push('deferUpdate') && events.push('deferUpdate');

  assert.equal(await handleSelectionInteraction(interaction, { store, queue }), true);

  assert.equal(events[0], 'deferUpdate', `ack must come first, got: ${events.join(' -> ')}`);
  assert.ok(events.indexOf('store.list') > events.indexOf('deferUpdate'));
  assert.deepEqual(interaction.events.at(-1), 'editReply');
  assert.equal(interaction.edits.length, 1);
});

test('selection failure after deferUpdate surfaces as ephemeral followUp', async () => {
  const store = {
    list: async () => [],
    updateStatus: async () => {},
  };
  const queue = { enqueue: async () => ({}), pending: async () => [] };
  const message = makeDigestMessage(['cand-x']);
  const interaction = makeInteraction(message.components[0].components[0].custom_id, message);

  assert.equal(await handleSelectionInteraction(interaction, { store, queue }), true);

  assert.equal(interaction.edits.length, 0);
  assert.equal(interaction.followUps.length, 1);
  assert.match(interaction.followUps[0].content, /Selection failed/);
  assert.equal(interaction.followUps[0].ephemeral, true);
});

test('concurrent selections in one category settle to exactly one selected (P1: non-atomic write race)', async (t) => {
  await withStore(t, async ({ dbPath, store, queue }) => {
    const a = seedCandidate(dbPath, candidate({ pov_player: { name: 'A', userid: 1 } }));
    const b = seedCandidate(dbPath, candidate({ pov_player: { name: 'B', userid: 2 } }));
    const message = makeDigestMessage([a, b]);

    // Fire both button handlers simultaneously — without the per-category
    // lock both read "no current selection" and both reach `selected`.
    await Promise.all([
      handleSelectionInteraction(
        makeInteraction(message.components[0].components[0].custom_id, message),
        { store, queue },
      ),
      handleSelectionInteraction(
        makeInteraction(message.components[0].components[1].custom_id, message),
        { store, queue },
      ),
    ]);

    const statuses = (await store.list()).map((c) => c.status).sort();
    assert.deepEqual(statuses, ['posted', 'selected']);
    const pending = await queue.pending({ category: 'QUAD RUNNER' });
    assert.equal(pending.length, 1);
    const selected = (await store.list()).find((c) => c.status === 'selected');
    assert.equal(pending[0].candidate_id, selected.id);
  });
});

test('concurrent enqueues across categories both persist (queue mutation lock)', async (t) => {
  await withStore(t, async ({ queue }) => {
    await Promise.all([
      queue.enqueue(candidate({ id: 'c1', category: 'QUAD RUNNER' })),
      queue.enqueue(candidate({ id: 'c2', category: 'TEAM KILLER' })),
    ]);
    const pending = await queue.pending();
    assert.deepEqual(pending.map((e) => e.candidate_id).sort(), ['c1', 'c2']);
  });
});

test('re-clicking the already-selected candidate is idempotent (no duplicate queue entries)', async (t) => {
  await withStore(t, async ({ dbPath, store, queue }) => {
    const a = seedCandidate(dbPath, candidate({ pov_player: { name: 'A', userid: 1 } }));
    const message = makeDigestMessage([a]);
    const buttonId = message.components[0].components[0].custom_id;

    await handleSelectionInteraction(makeInteraction(buttonId, message), { store, queue });
    await handleSelectionInteraction(makeInteraction(buttonId, message), { store, queue });

    assert.equal((await store.list())[0].status, 'selected');
    const all = await queue.read();
    assert.equal(all.entries.length, 1, 'second click must not append or supersede');
    assert.equal(all.entries[0].status, 'pending');
  });
});

test('discord.js component instances normalize to API JSON (P1: instance serialization)', () => {
  const cid = 'clipsmith:select:QUAD%20RUNNER:1:cand-1';
  // Builder/instance-shaped: fields under .data, toJSON() like discord.js.
  const buttonInstance = {
    data: { type: 2, style: 1, custom_id: cid, label: 'Select as QUAD RUNNER OF THE WEEK' },
    toJSON() { return { ...this.data }; },
  };
  const rowInstance = {
    components: [buttonInstance],
    toJSON() { return { type: 1, components: [buttonInstance.toJSON()] }; },
  };

  assert.deepEqual(
    selectionsFromMessage({ components: [rowInstance] }),
    [{ category: 'QUAD RUNNER', index: 1, candidateId: 'cand-1' }],
  );

  const [row] = markSelectedComponents([rowInstance], 'cand-1');
  assert.equal(row.type, 1, 'row must keep a top-level type');
  const [button] = row.components;
  assert.equal(button.type, 2, 'button must keep a top-level type');
  assert.equal(button.custom_id, cid);
  assert.equal(button.style, 3);
  assert.equal(button.disabled, true);
  assert.equal(button.data, undefined, 'no nested {data: ...} wrapper in the payload');
});

test('component instances without toJSON fall back to .data and get a type', () => {
  const cid = 'clipsmith:select:QUAD%20RUNNER:1:cand-1';
  const row = {
    components: [{ data: { style: 1, custom_id: cid, label: 'Select as QUAD RUNNER OF THE WEEK' } }],
  };

  const [marked] = markSelectedComponents([row], 'other-cand');
  assert.equal(marked.type, 1);
  assert.equal(marked.components[0].type, 2);
  assert.equal(marked.components[0].custom_id, cid);
  assert.equal(marked.components[0].style, 2);
  assert.equal(marked.components[0].disabled, false);
});

test('text fallback on a message without selection buttons explains itself (P2: "1 to 0")', async () => {
  const replies = [];
  const message = {
    content: 'select 1',
    reference: { messageId: 'empty-category-message' },
    reply: async (payload) => replies.push(payload),
  };

  assert.equal(await handleTextSelectionMessage(message, {
    store: {},
    queue: {},
    fetchMessage: async () => ({ content: 'No qualifying moment this week.', components: [] }),
  }), true);

  assert.equal(replies.length, 1);
  assert.match(replies[0].content, /no selectable candidates/);
  assert.doesNotMatch(replies[0].content, /1 to 0/);
});
