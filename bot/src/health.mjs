// Startup health line (issue #16 step 2): bot version, candidate-store
// reachability, counts by status. Posted to the review channel on connect
// and always logged to stdout. Pure function — unit-tested offline.
import { STATUSES } from './store.mjs';

export function buildHealthLine({ version, storeOk, counts = null }) {
  const store = storeOk ? 'ok' : 'UNREACHABLE';
  const countsStr = storeOk && counts
    ? STATUSES.map((s) => `${s}=${counts[s] ?? 0}`).join(' ')
    : 'n/a';
  return `clipsmith-bot v${version} up | store: ${store} | ${countsStr}`;
}
