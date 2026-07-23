// Monday-20:00 digest rendering + posting (issue #17).
//
// Consumes the digest JSON built by `python -m clipsmith.digest build`
// (issue #11 — the documented Phase-2 bot input contract, see
// clipsmith/digest.py docstring) and turns each section into one Discord
// message: content line "<CATEGORY> OF THE WEEK — candidates", one embed
// per candidate (description, POV, ranking reasoning, hub deep link).
//
// Pure module: no discord.js import, embeds are plain API JSON (the
// commands.mjs convention) so everything is unit-testable with mocked
// channel/store objects. The CLI wiring lives in post-digest.mjs.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { REPO_ROOT } from './store.mjs';
import { selectionButton } from './selection.mjs';

// Spec: all date/time presentation and "this week" logic (issue #17 task 6).
export const TIMEZONE = 'Europe/Stockholm';

// Discord hard limits (developer docs, "Embed Limits") — exceeding any of
// them rejects the whole message with a 400, so we guard, never trust input.
export const EMBED_TITLE_MAX = 256;
export const EMBED_DESCRIPTION_MAX = 4096;
export const EMBED_FIELD_VALUE_MAX = 1024;
// title+description+fields+footer+author, summed across ALL embeds of one
// message — Discord counts the 6000 per message, not per embed.
export const EMBED_TOTAL_MAX = 6000;

// pipeline-spec "Highlight categories": min 1, max 3 candidates per category.
// digest.py already caps at 3; the bot re-guards so a hand-edited digest
// can never produce an over-long category message.
export const MAX_PER_CATEGORY = 3;

// ---------------------------------------------------------------- week math

/** ISO week id ('2026-W24') of a calendar day given as a UTC-midnight Date. */
function isoWeekId(day) {
  const d = new Date(day.getTime());
  d.setUTCDate(d.getUTCDate() + 3 - ((d.getUTCDay() + 6) % 7)); // its Thursday
  const isoYear = d.getUTCFullYear();
  const firstThursday = new Date(Date.UTC(isoYear, 0, 4));
  firstThursday.setUTCDate(
    firstThursday.getUTCDate() + 3 - ((firstThursday.getUTCDay() + 6) % 7),
  );
  const week = 1 + Math.round((d - firstThursday) / (7 * 86_400_000));
  return `${isoYear}-W${String(week).padStart(2, '0')}`;
}

/** The calendar date of `now` in Europe/Stockholm, as a UTC-midnight Date. */
function stockholmDay(now) {
  const ymd = new Intl.DateTimeFormat('en-CA', {
    timeZone: TIMEZONE, year: 'numeric', month: '2-digit', day: '2-digit',
  }).format(now); // en-CA => YYYY-MM-DD
  const [y, m, d] = ymd.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, d));
}

/**
 * Default digest week — mirrors clipsmith/digest.py default_week(), but on
 * the Europe/Stockholm calendar: the ISO week ending at the upcoming (or
 * current) Monday. Run on a Monday (the scheduled 20:00 post) that is the
 * week that just finished; run mid-week it is the current ISO week.
 */
export function defaultWeek(now = new Date()) {
  const today = stockholmDay(now);
  const daysUntilMonday = (8 - today.getUTCDay()) % 7; // 0 when Monday
  const coveredSunday = new Date(
    today.getTime() + (daysUntilMonday - 1) * 86_400_000,
  );
  return isoWeekId(coveredSunday);
}

/** Where digest.py writes the week's digest: state/digests/<week>.json. */
export function defaultDigestPath(now = new Date()) {
  return join(REPO_ROOT, 'state', 'digests', `${defaultWeek(now)}.json`);
}

/** Human timestamp in Europe/Stockholm for presentation lines. */
export function formatStockholm(iso) {
  const fmt = new Intl.DateTimeFormat('en-GB', {
    timeZone: TIMEZONE, dateStyle: 'medium', timeStyle: 'short',
  });
  return `${fmt.format(new Date(iso))} (${TIMEZONE})`;
}

// ------------------------------------------------------------- digest input

/** Load + shape-check a digest JSON file (#11 contract). Throws with path. */
export function loadDigest(path) {
  let digest;
  try {
    digest = JSON.parse(readFileSync(path, 'utf8'));
  } catch (err) {
    throw new Error(
      `cannot load digest ${path} (${err.message ?? err}) — ` +
      'build it with: python -m clipsmith.digest build',
    );
  }
  if (typeof digest.week !== 'string' || !Array.isArray(digest.sections)) {
    throw new Error(`digest ${path} is not the #11 shape (week + sections[])`);
  }
  for (const section of digest.sections) {
    if (typeof section.category !== 'string' || !Array.isArray(section.candidates)) {
      throw new Error(
        `digest ${path}: section missing category/candidates[] (#11 shape)`,
      );
    }
  }
  return digest;
}

// ---------------------------------------------------------------- rendering

const truncate = (text, max) => {
  const s = String(text ?? '');
  if (max <= 0) return '';
  return s.length <= max ? s : `${s.slice(0, max - 1)}…`;
};

// Presentation rounding for float noise (2.2304000000000004 → 2.23): score
// to 2 decimals (matches the describer's wording), metrics to 3.
const fmtNum = (n, places) => {
  const value = Number(n);
  if (!Number.isFinite(value)) return String(n);
  return Number.isInteger(value)
    ? String(value)
    : String(Number(value.toFixed(places)));
};

/** Why-it-ranked line: score + the scalar ranking metrics from the digest. */
export function formatReasoning(cand) {
  const parts = [
    `score ${fmtNum(cand.score, 2)}`,
    `rank #${cand.rank_in_category}`,
  ];
  for (const [key, value] of Object.entries(cand.metrics_summary ?? {})) {
    if (typeof value === 'number') {
      parts.push(`${key}=${fmtNum(value, 3)}`);
    } else if (typeof value === 'string' || typeof value === 'boolean') {
      parts.push(`${key}=${value}`);
    }
  }
  return truncate(parts.join(' · '), EMBED_FIELD_VALUE_MAX);
}

/** Sum of the embed text lengths Discord counts against the 6000-char cap. */
export function embedCharCount(embed) {
  let n = (embed.title ?? '').length
    + (embed.description ?? '').length
    + (embed.footer?.text ?? '').length
    + (embed.author?.name ?? '').length;
  for (const f of embed.fields ?? []) {
    n += String(f.name).length + String(f.value).length;
  }
  return n;
}

/** One candidate -> one plain-JSON embed, all Discord limits guarded. */
export function buildCandidateEmbed(cand) {
  const pov = cand.pov_player ?? {};
  const teams = (cand.teams ?? [])
    .map((t) => `${t.name} ${t.score}`).join(' vs ');
  const embed = {
    title: truncate(
      `#${cand.rank_in_category} ${pov.name ?? '?'} — ${cand.map ?? '?'}, ${cand.matchdate || 'date unknown'}`,
      EMBED_TITLE_MAX,
    ),
    description: truncate(cand.description ?? '', EMBED_DESCRIPTION_MAX),
    fields: [
      {
        name: 'POV',
        value: truncate(
          `${pov.name ?? '?'}${pov.userid != null ? ` (userid ${pov.userid})` : ''}${teams ? ` — ${teams}` : ''}`,
          EMBED_FIELD_VALUE_MAX,
        ),
        inline: true,
      },
      { name: 'Why it ranked', value: formatReasoning(cand) },
      {
        name: 'Watch',
        // hub_url_missing: the #11 contract marks candidates whose hub link
        // could not be resolved; surface that instead of an empty field.
        value: cand.hub_url
          ? truncate(cand.hub_url, EMBED_FIELD_VALUE_MAX)
          : '_hub link missing — re-run clipsmith.hub_links resolution_',
      },
    ],
  };
  // Per-embed guard: shrink the description until this embed alone fits.
  // (Necessary but not sufficient — the 6000 cap is per MESSAGE, so
  // fitEmbedsToBudget re-checks the aggregate across a message's embeds.)
  const over = embedCharCount(embed) - EMBED_TOTAL_MAX;
  if (over > 0) {
    embed.description = truncate(
      embed.description, Math.max(0, embed.description.length - over),
    );
  }
  return fitEmbedsToBudget([embed])[0];
}

const totalEmbedChars = (embeds) =>
  embeds.reduce((n, e) => n + embedCharCount(e), 0);

function trimLongestField(embeds, fieldName, budget) {
  while (totalEmbedChars(embeds) > budget) {
    let longest = null;
    for (const embed of embeds) {
      const field = embed.fields?.find((f) => f.name === fieldName);
      if (field && String(field.value).length > 1
          && (!longest || String(field.value).length > String(longest.value).length)) {
        longest = field;
      }
    }
    if (!longest) return;
    const over = totalEmbedChars(embeds) - budget;
    longest.value = truncate(longest.value, Math.max(1, String(longest.value).length - over));
  }
}

function trimLongestTitle(embeds, budget) {
  while (totalEmbedChars(embeds) > budget) {
    let longest = null;
    for (const embed of embeds) {
      if (String(embed.title ?? '').length > 1
          && (!longest || String(embed.title).length > String(longest.title).length)) {
        longest = embed;
      }
    }
    if (!longest) return;
    const over = totalEmbedChars(embeds) - budget;
    longest.title = truncate(longest.title, Math.max(1, String(longest.title).length - over));
  }
}

/**
 * Aggregate guard for one message: Discord's 6000-char limit is the TOTAL
 * across all embeds in a single message, so three individually-valid embeds
 * can still get the whole channel.send rejected with a 400. Lower a common
 * cap on description length (water-filling: only the longest descriptions
 * are cut, each keeps as much as the budget allows) until the message fits.
 * If pathological field text still overflows after descriptions are empty,
 * trim lower-priority fields in this order: Why-it-ranked, POV, Watch. The
 * hub URL is the last field touched, but the message must never be sent over
 * Discord's hard limit.
 */
export function fitEmbedsToBudget(embeds, budget = EMBED_TOTAL_MAX) {
  const over = totalEmbedChars(embeds) - budget;
  if (over <= 0) return embeds;
  const lens = embeds.map((e) => (e.description ?? '').length);
  const savings = (cap) => lens.reduce((n, l) => n + Math.max(0, l - cap), 0);
  // Largest cap whose savings cover the overflow. When even cap 0 cannot
  // absorb it (pathological field bloat) descriptions go empty and the
  // fields stay — the fields are the contract, the prose is the luxury.
  let lo = 0;
  let hi = savings(0) < over ? 0 : Math.max(0, ...lens);
  while (lo < hi) {
    const mid = lo + Math.ceil((hi - lo) / 2);
    if (savings(mid) >= over) lo = mid; else hi = mid - 1;
  }
  for (const embed of embeds) {
    if ((embed.description ?? '').length > lo) {
      embed.description = lo === 0 ? '' : truncate(embed.description, lo);
    }
  }
  // Review follow-up: descriptions are the first text to go, but field values
  // alone can still exceed the per-message aggregate budget. Keep every field
  // non-empty, trim recommendation prose before POV, and touch Watch last.
  for (const fieldName of ['Why it ranked', 'POV', 'Watch']) {
    trimLongestField(embeds, fieldName, budget);
  }
  trimLongestTitle(embeds, budget);
  return embeds;
}

/**
 * Digest -> one message per category (fixed digest order):
 * { category, content, embeds, components, candidateIds, truncated }.
 * Empty categories get the explicit "no qualifying moment" content
 * (the digest's own note when present — same sentence digest.py writes)
 * and zero embeds; populated ones get max 3 embeds, extras dropped.
 */
export function buildCategoryMessages(digest) {
  const weekLine =
    `Week ${digest.week} (${digest.week_start} → ${digest.week_end}, ${TIMEZONE})`;
  return digest.sections.map((section) => {
    const label = section.label ?? `${section.category} OF THE WEEK — candidates`;
    const header = `**${label}**\n${weekLine}`;
    const shown = section.candidates.slice(0, MAX_PER_CATEGORY);
    if (shown.length === 0) {
      const note = section.note
        ?? `no qualifying ${section.category} moment this week`;
      return {
        category: section.category,
        content: `${header}\n${note}`,
        embeds: [],
        components: [],
        candidateIds: [],
        truncated: 0,
      };
    }
    const components = [{
      type: 1,
      components: shown.map((candidate, i) => selectionButton({
        category: section.category,
        candidateId: candidate.candidate_id,
        index: i + 1,
      })),
    }];
    return {
      category: section.category,
      content: header,
      embeds: fitEmbedsToBudget(shown.map(buildCandidateEmbed)),
      components,
      candidateIds: shown.map((c) => c.candidate_id),
      truncated: section.candidates.length - shown.length,
    };
  });
}

/** Human-readable dump of one category message (dry-run output). */
export function renderMessagePreview(msg) {
  const lines = [`--- message: ${msg.category} ---`, msg.content];
  msg.embeds.forEach((embed, i) => {
    lines.push(`  embed ${i + 1}: ${embed.title}`);
    lines.push(`    ${embed.description}`);
    for (const f of embed.fields ?? []) lines.push(`    ${f.name}: ${f.value}`);
  });
  if (msg.embeds.length === 0) lines.push('  (no embeds — empty category)');
  return lines.join('\n');
}

// ------------------------------------------------------------------ posting

/**
 * Post the digest: one channel.send per category, then flip that message's
 * candidates candidate→posted via the store. Flips happen strictly AFTER a
 * successful send and per category message — a failed post never flips its
 * candidates (issue #17 task 5). A failed flip is logged and counted, never
 * fatal (mirrors digest.py mark-posted: skip with notice).
 *
 * dryRun renders every message to `log` and touches neither channel nor
 * store. `channel` only needs send(); `store` only needs updateStatus() —
 * tests pass plain objects.
 */
export async function postDigest({
  digest, channel = null, store = null, dryRun = false, log = () => {},
}) {
  const messages = buildCategoryMessages(digest);
  const summary = {
    categories: messages.length,
    posted: [], postFailures: [], flipped: [], flipFailures: [],
  };
  for (const msg of messages) {
    if (msg.truncated > 0) {
      log(`${msg.category}: dropped ${msg.truncated} candidate(s) over the ` +
          'max-3 cap (pipeline-spec: max 3 per category)');
    }
    if (dryRun) {
      log(renderMessagePreview(msg));
      continue;
    }
    try {
      await channel.send({
        content: msg.content,
        embeds: msg.embeds,
        components: msg.components,
      });
      summary.posted.push(msg.category);
    } catch (err) {
      summary.postFailures.push(msg.category);
      log(`${msg.category}: post FAILED (${err.message ?? err}) — ` +
          `NOT flipping its ${msg.candidateIds.length} candidate(s)`);
      continue;
    }
    for (const id of msg.candidateIds) {
      try {
        await store.updateStatus(id, 'posted');
        summary.flipped.push(id);
      } catch (err) {
        summary.flipFailures.push(id);
        log(`${msg.category}: candidate→posted flip failed for ${id} ` +
            `(${err.message ?? err})`);
      }
    }
  }
  return summary;
}
