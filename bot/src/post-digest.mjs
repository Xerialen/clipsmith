#!/usr/bin/env node
// Digest-post entrypoint (issue #17) — posts the Monday 20:00 candidate
// digest to the review channel, one message per category, one embed per
// candidate, then flips posted candidates candidate→posted in the store.
//
//   node src/post-digest.mjs [--digest <path>] [--channel <id>] [--dry-run]
//
//   --digest   digest JSON (#11 shape); default: state/digests/<week>.json
//              for the current digest week on the Europe/Stockholm calendar
//   --channel  channel id override (safe live testing once #12 lands);
//              default: CLIPSMITH_DISCORD_REVIEW_CHANNEL_ID
//   --dry-run  render every message/embed to stdout; no token needed,
//              nothing sent, NO store writes; exits 0
//
// The scheduled invocation (systemd timer, Monday 20:00 Europe/Stockholm)
// is deployed in P2.4/#21 — this file is the postable command it runs.
// discord.js is imported lazily so dry-run works without install/creds
// (the index.mjs convention).
import { pathToFileURL } from 'node:url';
import { loadConfig } from './config.mjs';
import { CandidateStore } from './store.mjs';
import {
  loadDigest, defaultDigestPath, formatStockholm, postDigest,
} from './digest.mjs';

const ts = () => new Date().toISOString();
const log = (...args) => console.log(`[${ts()}]`, ...args);

export function parseArgs(argv) {
  const args = { digest: null, channel: null, dryRun: false };
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === '--dry-run') {
      args.dryRun = true;
    } else if (a === '--digest' || a === '--channel') {
      const value = argv[i + 1];
      if (value === undefined || value.startsWith('--')) {
        throw new Error(`${a} needs a value`);
      }
      args[a === '--digest' ? 'digest' : 'channel'] = value;
      i += 1;
    } else {
      throw new Error(
        `unknown argument: ${a} (usage: post-digest.mjs [--digest <path>] [--channel <id>] [--dry-run])`,
      );
    }
  }
  return args;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));

  // Dry-run tolerates missing keys (placeholders until #12); live mode
  // throws naming the missing KEYS only — never values (config.mjs rule).
  const config = loadConfig({
    envPath: process.env.CLIPSMITH_ENV_PATH ?? undefined,
    requireAll: !args.dryRun,
  });

  const digestPath = args.digest ?? defaultDigestPath();
  const digest = loadDigest(digestPath);
  log(`digest ${digest.week} (${digest.week_start} → ${digest.week_end}) ` +
      `from ${digestPath}, generated ${formatStockholm(digest.generated_at)}`);

  if (args.dryRun) {
    if (config.missing.length) {
      log(`dry-run: missing env keys: ${config.missing.join(', ')} — live post needs issue #12`);
    }
    await postDigest({ digest, dryRun: true, log });
    log('dry-run complete — nothing sent, no store writes.');
    process.exit(0);
  }

  const store = new CandidateStore({ dbPath: process.env.CLIPSMITH_DB ?? null });
  const channelId = args.channel ?? config.reviewChannelId;

  const { Client, GatewayIntentBits } = await import('discord.js');
  const client = new Client({ intents: [GatewayIntentBits.Guilds] });
  try {
    await client.login(config.token);
    const channel = await client.channels.fetch(channelId);
    const summary = await postDigest({ digest, channel, store, log });
    log(`posted ${summary.posted.length}/${summary.categories} category message(s) ` +
        `to channel ${channelId}; flipped ${summary.flipped.length} candidate(s) candidate→posted`);
    if (summary.postFailures.length || summary.flipFailures.length) {
      log(`failures — posts: [${summary.postFailures.join(', ')}] ` +
          `flips: [${summary.flipFailures.join(', ')}]`);
      process.exitCode = 1;
    }
  } finally {
    await client.destroy();
  }
}

// Only run when invoked as the entrypoint — tests import parseArgs.
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((err) => {
    console.error(`[${ts()}] fatal:`, err.message ?? err);
    process.exit(1);
  });
}
