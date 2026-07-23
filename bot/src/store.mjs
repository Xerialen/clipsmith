// Candidate-store access for the bot (issue #16 step 4: injectable; #17
// adds the single write path, update-status).
//
// Deliberately shells out to the canonical Python CLI
// (`python -m clipsmith.store list`) instead of opening state/candidates.db
// directly: clipsmith/store.py stays the single source of truth for schema
// and status rules, and the bot needs no native SQLite dependency. The exec
// function is injectable so tests can run against canned output or a seeded
// temp store.
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const execFileAsync = promisify(execFile);

// Subprocess guard rails for the default exec (constructor-injectable).
// `store list` dumps the full candidate JSONL; the node default maxBuffer
// (1 MiB) silently truncates large stores, and no timeout means a hung
// Python child wedges the bot forever.
export const DEFAULT_EXEC_TIMEOUT_MS = 30_000;
export const DEFAULT_EXEC_MAX_BUFFER = 32 * 1024 * 1024;

/** Typed failure for "the store CLI did not answer" (spawn/timeout/non-zero exit). */
export class StoreUnreachableError extends Error {
  constructor(message, { cause } = {}) {
    super(message, { cause });
    this.name = 'StoreUnreachableError';
  }
}

// Status vocabulary from clipsmith/store.py TRANSITIONS (pipeline-spec lifecycle).
export const STATUSES = [
  'candidate', 'posted', 'selected', 'rendered',
  'uploaded', 'approved', 'published', 'rejected', 'revision_requested',
];

// bot/src/ -> repo root (where `python -m clipsmith.store` resolves).
export const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');

export class CandidateStore {
  /**
   * @param {object} opts
   * @param {string|null} opts.dbPath  - --db override (default: store default / $CLIPSMITH_DB)
   * @param {string} opts.pythonBin    - python executable (CLIPSMITH_PYTHON env overrides)
   * @param {string} opts.repoRoot     - cwd for the CLI
   * @param {function(string[]): Promise<string>} opts.exec - injectable runner
   * @param {number} opts.timeoutMs  - kill the CLI after this long (default 30 s)
   * @param {number} opts.maxBuffer  - stdout cap in bytes (default 32 MiB)
   */
  constructor({
    dbPath = null, pythonBin = null, repoRoot = REPO_ROOT, exec = null,
    timeoutMs = DEFAULT_EXEC_TIMEOUT_MS, maxBuffer = DEFAULT_EXEC_MAX_BUFFER,
  } = {}) {
    this.dbPath = dbPath;
    this.pythonBin = pythonBin ?? process.env.CLIPSMITH_PYTHON ?? 'python';
    this.repoRoot = repoRoot;
    this.timeoutMs = timeoutMs;
    this.maxBuffer = maxBuffer;
    this.exec = exec ?? (async (args) => {
      try {
        const { stdout } = await execFileAsync(this.pythonBin, args, {
          cwd: this.repoRoot,
          windowsHide: true,
          timeout: this.timeoutMs,
          maxBuffer: this.maxBuffer,
        });
        return stdout;
      } catch (err) {
        throw new StoreUnreachableError(
          `store CLI failed: ${err.message ?? err}`,
          { cause: err },
        );
      }
    });
  }

  cliArgs(subcommand, extra = []) {
    const args = ['-m', 'clipsmith.store'];
    if (this.dbPath) args.push('--db', this.dbPath); // global flag, before subcommand
    return [...args, subcommand, ...extra];
  }

  /** List candidates as objects (optionally filtered), via `store list` JSON lines. */
  async list({ category = null, status = null } = {}) {
    const extra = [];
    if (category) extra.push('--category', category);
    if (status) extra.push('--status', status);
    const out = await this.exec(this.cliArgs('list', extra));
    return out.split(/\r?\n/).filter((l) => l.trim()).map((l) => JSON.parse(l));
  }

  /** Counts grouped by status; every known status present (zero-filled). */
  async countsByStatus() {
    const counts = Object.fromEntries(STATUSES.map((s) => [s, 0]));
    for (const cand of await this.list()) {
      counts[cand.status] = (counts[cand.status] ?? 0) + 1;
    }
    return counts;
  }

  /**
   * Move a candidate to a new status via `store update-status` (issue #17:
   * digest post flips candidate→posted). The Python CLI stays the single
   * authority on legal lifecycle transitions; an illegal transition or
   * unknown id exits non-zero, which the exec layer surfaces as
   * StoreUnreachableError carrying the CLI's stderr message.
   */
  async updateStatus(id, newStatus) {
    await this.exec(this.cliArgs('update-status', [id, newStatus]));
  }

  /** True when the store CLI answers a list call. */
  async reachable() {
    try {
      await this.list();
      return true;
    } catch {
      return false;
    }
  }
}
