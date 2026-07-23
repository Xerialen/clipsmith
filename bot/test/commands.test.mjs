import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  commandDefinitions,
  formatStatusReply,
  handleInteraction,
} from '../src/commands.mjs';
import { STATUSES } from '../src/store.mjs';

function mockInteraction({ commandName = 'clipsmith', sub = 'status' } = {}) {
  const replies = [];   // immediate reply() calls (errors, unknown subcommand)
  const edits = [];     // editReply() calls after deferReply()
  const calls = [];     // ordered method log, to assert defer-before-work
  return {
    replies,
    edits,
    calls,
    deferred: false,
    isChatInputCommand: () => true,
    commandName,
    options: { getSubcommand: () => sub },
    reply: async (msg) => { calls.push('reply'); replies.push(msg); },
    deferReply: async function (opts) { this.deferred = true; calls.push('deferReply'); this.deferOpts = opts; },
    editReply: async (msg) => { calls.push('editReply'); edits.push(msg); },
  };
}

const seededCounts = () => {
  const counts = Object.fromEntries(STATUSES.map((s) => [s, 0]));
  counts.candidate = 40;
  counts.posted = 2;
  return counts;
};

test('command definition exposes /clipsmith status', () => {
  assert.equal(commandDefinitions[0].name, 'clipsmith');
  assert.equal(commandDefinitions[0].options[0].name, 'status');
  assert.equal(commandDefinitions[0].options[0].type, 1); // SUB_COMMAND
});

test('formatStatusReply lists every status and a correct total', () => {
  const reply = formatStatusReply(seededCounts());
  for (const s of STATUSES) assert.match(reply, new RegExp(`${s}\\s+\\d`));
  assert.match(reply, /candidate\s+40/);
  assert.match(reply, /total\s+42/);
});

test('/clipsmith status defers, then edits with counts from the store', async () => {
  const interaction = mockInteraction();
  const store = {
    countsByStatus: async () => {
      // The interaction must already be acknowledged before the (slow)
      // Python subprocess runs — Discord's window is 3 seconds.
      assert.equal(interaction.deferred, true, 'deferReply must precede store call');
      return seededCounts();
    },
  };
  const handled = await handleInteraction(interaction, { store });
  assert.equal(handled, true);
  assert.deepEqual(interaction.calls, ['deferReply', 'editReply']);
  assert.equal(interaction.edits.length, 1);
  assert.match(interaction.edits[0].content, /candidate\s+40/);
});

test('unknown /clipsmith subcommand gets an ephemeral reply (handled, no ghosting)', async () => {
  const interaction = mockInteraction({ sub: 'frobnicate' });
  const store = { countsByStatus: async () => { throw new Error('must not be called'); } };
  const handled = await handleInteraction(interaction, { store });
  assert.equal(handled, true);
  assert.equal(interaction.deferred, false);
  assert.equal(interaction.replies.length, 1);
  assert.equal(interaction.replies[0].ephemeral, true);
  assert.match(interaction.replies[0].content, /frobnicate/);
  assert.match(interaction.replies[0].content, /unknown subcommand/i);
});

test('unknown commands are not handled', async () => {
  const interaction = mockInteraction({ commandName: 'other' });
  const store = { countsByStatus: async () => { throw new Error('must not be called'); } };
  assert.equal(await handleInteraction(interaction, { store }), false);
  assert.equal(interaction.replies.length, 0);
});

test('non-chat-input interactions are ignored', async () => {
  const interaction = { isChatInputCommand: () => false };
  assert.equal(await handleInteraction(interaction, { store: {} }), false);
});

test('store failure edits the deferred reply with an error, not a crash', async () => {
  const interaction = mockInteraction();
  const store = { countsByStatus: async () => { throw new Error('db gone'); } };
  const handled = await handleInteraction(interaction, { store });
  assert.equal(handled, true);
  assert.deepEqual(interaction.calls, ['deferReply', 'editReply']);
  assert.match(interaction.edits[0].content, /unreachable/i);
});
