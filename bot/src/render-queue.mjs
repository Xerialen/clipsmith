// Durable Phase-2 -> Phase-3 handoff queue (issue #18).
//
// File format (JSON, atomically rewritten):
// {
//   "version": 1,
//   "updated_at": "2026-06-10T21:30:00.000Z",
//   "entries": [
//     {
//       "candidate_id": "...",
//       "category": "QUAD RUNNER",
//       "demo_sha": "...",
//       "window": {"start_s": 1, "end_s": 2},
//       "pov_player": {"name": "Milton", "userid": 35},
//       "selected_at": "...",
//       "status": "pending"
//     }
//   ]
// }
//
// The render orchestrator can poll entries where status == "pending"; once it
// claims one it should move the candidate through the store lifecycle and mark
// its own queue acknowledgement in a later Phase-3 ticket. Re-selection within
// a category marks any still-pending entry for that category as "superseded"
// and appends the new pending entry, so consumers see one pending per category.
import { mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { REPO_ROOT } from './store.mjs';

export const QUEUE_VERSION = 1;
export const DEFAULT_RENDER_QUEUE = join(REPO_ROOT, 'state', 'render-queue.json');

export function defaultRenderQueuePath(dbPath = null) {
  return dbPath ? join(dirname(dbPath), 'render-queue.json') : DEFAULT_RENDER_QUEUE;
}

export class FileRenderQueue {
  constructor({ path = DEFAULT_RENDER_QUEUE } = {}) {
    this.path = path;
    // Mutations are read-modify-write on one shared file; chain them so two
    // concurrent enqueues (e.g. selections in different categories) cannot
    // interleave and drop each other's entries (review finding P1, PR #42).
    this._mutationChain = Promise.resolve();
  }

  async _withMutationLock(fn) {
    const run = this._mutationChain.then(fn);
    this._mutationChain = run.then(() => {}, () => {});
    return run;
  }

  async read() {
    try {
      const data = JSON.parse(await readFile(this.path, 'utf8'));
      return {
        version: data.version ?? QUEUE_VERSION,
        updated_at: data.updated_at ?? null,
        entries: Array.isArray(data.entries) ? data.entries : [],
      };
    } catch (err) {
      if (err.code === 'ENOENT') {
        return { version: QUEUE_VERSION, updated_at: null, entries: [] };
      }
      throw err;
    }
  }

  // Atomic on its own (temp file + rename) but NOT a transaction with the
  // preceding read — use enqueue()/_withMutationLock for read-modify-write.
  async write(data) {
    await mkdir(dirname(this.path), { recursive: true });
    const tmp = `${this.path}.tmp`;
    await writeFile(tmp, `${JSON.stringify(data, null, 2)}\n`, 'utf8');
    await rename(tmp, this.path);
  }

  async enqueue(candidate, { selectedAt = new Date().toISOString() } = {}) {
    return this._withMutationLock(() => this._enqueueLocked(candidate, selectedAt));
  }

  async _enqueueLocked(candidate, selectedAt) {
    const data = await this.read();
    for (const entry of data.entries) {
      if (entry.category === candidate.category && entry.status === 'pending') {
        entry.status = 'superseded';
        entry.superseded_at = selectedAt;
        entry.superseded_by = candidate.id;
      }
    }
    const entry = {
      candidate_id: candidate.id,
      category: candidate.category,
      demo_sha: candidate.demo_sha,
      window: candidate.window,
      pov_player: candidate.pov_player,
      selected_at: selectedAt,
      status: 'pending',
    };
    data.version = QUEUE_VERSION;
    data.updated_at = selectedAt;
    data.entries.push(entry);
    await this.write(data);
    return entry;
  }

  async pending({ category = null } = {}) {
    const data = await this.read();
    return data.entries.filter(
      (entry) => entry.status === 'pending'
        && (category == null || entry.category === category),
    );
  }
}
