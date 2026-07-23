// clipsmith-bot config — resolves CLIPSMITH_DISCORD_* from ~/.config/api-keys.env.
//
// Mirrors clipsmith/watcher.py load_creds(): parse KEY=VALUE lines, strip
// quotes, NEVER print or log values. Error messages name missing KEYS only.
//
// Per docs/pipeline-spec.md § "Placeholders" these are placeholder names until
// the owner completes issue #12 (Discord server + bot application); the bot
// must build and dry-run without them.
import { readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

export const REQUIRED_KEYS = [
  'CLIPSMITH_DISCORD_TOKEN',
  'CLIPSMITH_DISCORD_GUILD_ID',
  'CLIPSMITH_DISCORD_REVIEW_CHANNEL_ID',
];

export const DEFAULT_ENV_PATH = join(homedir(), '.config', 'api-keys.env');

/** Parse KEY=VALUE lines; '#' comments and blank lines ignored; quotes stripped. */
export function parseEnvFile(text) {
  const vars = {};
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#') || !line.includes('=')) continue;
    const eq = line.indexOf('=');
    const key = line.slice(0, eq).trim();
    let value = line.slice(eq + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) ||
        (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    if (key) vars[key] = value;
  }
  return vars;
}

/**
 * Load bot config from the env file.
 *
 * @param {object} opts
 * @param {string} opts.envPath   - env file path (tests inject a temp file)
 * @param {boolean} opts.requireAll - throw if any REQUIRED_KEYS are missing
 *                                    (live mode); dry-run passes false and
 *                                    inspects `missing` instead.
 * @returns {{token: string|null, guildId: string|null,
 *            reviewChannelId: string|null, missing: string[]}}
 */
export function loadConfig({ envPath = DEFAULT_ENV_PATH, requireAll = true } = {}) {
  let text;
  try {
    text = readFileSync(envPath, 'utf8');
  } catch (err) {
    if (!requireAll) {
      return { token: null, guildId: null, reviewChannelId: null, missing: [...REQUIRED_KEYS] };
    }
    throw new Error(`cannot read env file ${envPath} (${err.code ?? err.message})`);
  }
  const vars = parseEnvFile(text);
  const missing = REQUIRED_KEYS.filter((k) => !vars[k]);
  if (missing.length && requireAll) {
    throw new Error(
      `missing ${missing.join(', ')} in ${envPath} — ` +
      'placeholders until the owner completes issue #12 (Discord server + bot app)',
    );
  }
  return {
    token: vars.CLIPSMITH_DISCORD_TOKEN ?? null,
    guildId: vars.CLIPSMITH_DISCORD_GUILD_ID ?? null,
    reviewChannelId: vars.CLIPSMITH_DISCORD_REVIEW_CHANNEL_ID ?? null,
    missing,
  };
}
