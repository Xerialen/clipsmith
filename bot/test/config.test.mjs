import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { loadConfig, parseEnvFile, REQUIRED_KEYS } from '../src/config.mjs';

const SECRET = 'tok-SECRET-VALUE-abc123';

function tempEnvFile(content) {
  const dir = mkdtempSync(join(tmpdir(), 'clipsmith-bot-'));
  const path = join(dir, 'api-keys.env');
  writeFileSync(path, content, 'utf8');
  return { path, cleanup: () => rmSync(dir, { recursive: true, force: true }) };
}

test('parseEnvFile handles comments, quotes, blanks', () => {
  const vars = parseEnvFile([
    '# comment',
    '',
    'PLAIN=value',
    'DQ="double quoted"',
    "SQ='single quoted'",
    'SPACED = padded ',
    'noequals-line',
  ].join('\n'));
  assert.equal(vars.PLAIN, 'value');
  assert.equal(vars.DQ, 'double quoted');
  assert.equal(vars.SQ, 'single quoted');
  assert.equal(vars.SPACED, 'padded');
  assert.equal(Object.keys(vars).length, 4);
});

test('loadConfig resolves all three keys', () => {
  const { path, cleanup } = tempEnvFile([
    `CLIPSMITH_DISCORD_TOKEN=${SECRET}`,
    'CLIPSMITH_DISCORD_GUILD_ID=111',
    'CLIPSMITH_DISCORD_REVIEW_CHANNEL_ID=222',
  ].join('\n'));
  try {
    const cfg = loadConfig({ envPath: path });
    assert.equal(cfg.token, SECRET);
    assert.equal(cfg.guildId, '111');
    assert.equal(cfg.reviewChannelId, '222');
    assert.deepEqual(cfg.missing, []);
  } finally {
    cleanup();
  }
});

test('loadConfig (requireAll) throws naming missing keys, never leaking values', () => {
  const { path, cleanup } = tempEnvFile(
    `CLIPSMITH_DISCORD_TOKEN=${SECRET}\n`, // guild + channel missing
  );
  try {
    assert.throws(
      () => loadConfig({ envPath: path }),
      (err) => {
        assert.match(err.message, /CLIPSMITH_DISCORD_GUILD_ID/);
        assert.match(err.message, /CLIPSMITH_DISCORD_REVIEW_CHANNEL_ID/);
        assert.ok(!err.message.includes(SECRET), 'error must not leak values');
        return true;
      },
    );
  } finally {
    cleanup();
  }
});

test('loadConfig (tolerant) reports missing keys instead of throwing', () => {
  const { path, cleanup } = tempEnvFile('UNRELATED=1\n');
  try {
    const cfg = loadConfig({ envPath: path, requireAll: false });
    assert.deepEqual(cfg.missing, REQUIRED_KEYS);
    assert.equal(cfg.token, null);
  } finally {
    cleanup();
  }
});

test('loadConfig (tolerant) survives a missing env file entirely', () => {
  const cfg = loadConfig({
    envPath: join(tmpdir(), 'does-not-exist', 'api-keys.env'),
    requireAll: false,
  });
  assert.deepEqual(cfg.missing, REQUIRED_KEYS);
});
