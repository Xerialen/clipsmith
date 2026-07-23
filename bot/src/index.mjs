#!/usr/bin/env node
// clipsmith-bot entrypoint (issue #16) — Evola-bot pattern (gateway client,
// env from ~/.config/api-keys.env, timestamped logs, clean shutdown).
//
//   node src/index.mjs            # live: connect, register /clipsmith, post health
//   node src/index.mjs --dry-run  # no token needed: print health line, exit 0
//
// discord.js is imported lazily so --dry-run and the unit tests work before
// `npm install` and without any Discord credentials.
import { readFileSync } from 'node:fs';
import { loadConfig } from './config.mjs';
import { CandidateStore } from './store.mjs';
import { FileRenderQueue, defaultRenderQueuePath } from './render-queue.mjs';
import { buildHealthLine } from './health.mjs';
import { commandDefinitions, handleInteraction } from './commands.mjs';
import { handleSelectionInteraction, handleTextSelectionMessage } from './selection.mjs';

const VERSION = JSON.parse(
  readFileSync(new URL('../package.json', import.meta.url), 'utf8'),
).version;

const ts = () => new Date().toISOString();
const log = (...args) => console.log(`[${ts()}]`, ...args);

async function main() {
  const dryRun = process.argv.includes('--dry-run');

  // Dry-run tolerates missing keys (placeholders until #12); live mode throws
  // with the missing NAMES (never values).
  const config = loadConfig({
    envPath: process.env.CLIPSMITH_ENV_PATH ?? undefined,
    requireAll: !dryRun,
  });

  const dbPath = process.env.CLIPSMITH_DB ?? null;
  const store = new CandidateStore({ dbPath });
  const queue = new FileRenderQueue({
    path: process.env.CLIPSMITH_RENDER_QUEUE ?? defaultRenderQueuePath(dbPath),
  });
  const storeOk = await store.reachable();
  const counts = storeOk ? await store.countsByStatus() : null;
  const health = buildHealthLine({ version: VERSION, storeOk, counts });
  log(health);

  if (dryRun) {
    if (config.missing.length) {
      log(`dry-run: missing env keys: ${config.missing.join(', ')} — live connect needs issue #12`);
    }
    log('dry-run complete.');
    process.exit(0);
  }

  const { Client, GatewayIntentBits, Events } = await import('discord.js');
  const client = new Client({
    intents: [
      GatewayIntentBits.Guilds,
      GatewayIntentBits.GuildMessages,
      GatewayIntentBits.MessageContent,
    ],
  });

  client.once(Events.ClientReady, async (c) => {
    log(`connected as ${c.user.tag} (id ${c.user.id})`);
    try {
      await c.application.commands.set(commandDefinitions, config.guildId);
      log('slash commands registered for guild');
      const channel = await c.channels.fetch(config.reviewChannelId);
      await channel.send(health);
      log('health line posted to review channel');
    } catch (err) {
      log('startup post failed:', err.message ?? err);
    }
  });

  client.on(Events.InteractionCreate, async (interaction) => {
    try {
      if (await handleSelectionInteraction(interaction, { store, queue, log })) return;
      await handleInteraction(interaction, { store, log });
    } catch (err) {
      log('interaction error:', err.message ?? err);
    }
  });

  client.on(Events.MessageCreate, async (message) => {
    try {
      if (message.author?.bot) return;
      await handleTextSelectionMessage(message, {
        store,
        queue,
        log,
        fetchMessage: async (id) => message.channel.messages.fetch(id),
      });
    } catch (err) {
      log('message selection error:', err.message ?? err);
    }
  });

  client.on(Events.Error, (err) => log('client error:', err.message ?? err));

  const shutdown = async (signal) => {
    log(`${signal} — shutting down`);
    await client.destroy();
    process.exit(0);
  };
  process.on('SIGINT', () => shutdown('SIGINT'));
  process.on('SIGTERM', () => shutdown('SIGTERM'));

  await client.login(config.token);
}

main().catch((err) => {
  console.error(`[${ts()}] fatal:`, err.message ?? err);
  process.exit(1);
});
