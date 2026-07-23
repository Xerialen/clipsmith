# Testing and Validation

last-verified: 2026-06-11
status: active working contract

Before changing source, state which validation path applies. When a path is
unavailable, record why in the relevant pull request or issue and use the
strongest narrower check without claiming broader proof.

## Baseline Loop

For code changes, use this loop:

1. Read [success-criteria.md](success-criteria.md), the relevant specification,
   and the issue body.
2. Identify the smallest command or scenario that should fail or expose the
   gap before the change, when practical.
3. Implement the minimum change that satisfies the issue contract.
4. Rerun the targeted validation.
5. Run the broader suite for the touched area.
6. Paste the real command output, artifact paths, or live evidence into
   the relevant pull request or issue.
7. Update the issue/PR with what landed, what remains, and any manual evidence
   still needed.

## Docs-Only Changes

Use when only Markdown, prompts, or operational notes change.

Required checks:

- `git diff --check`
- Markdown whitespace scan equivalent to `.github/workflows/markdown-check.yml`
- relative-link scan equivalent to `.github/workflows/markdown-check.yml`

Expected evidence: changed docs listed, checks passed, and any intentional
ambiguity or deferred validation recorded in the pull request or issue.

## Python Store, Watcher, Digest, and Detectors

Use for `clipsmith/`, detector CLIs, store behavior, digest generation, and
Python deployment helpers.

Targeted checks:

- affected test module, for example `python -m pytest tests/test_watcher.py -q`
- regression test for every bug fix
- dry-run command for side-effecting CLIs

Broader check:

- `python -m pytest tests/ -q`

Expected evidence: command output, store fixture or state-dir location, and
before/after status or file checks for side-effect-free claims.

## Discord Bot and Node Scripts

Use for `bot/` and Node render helpers where tests do not require live Discord.

Targeted checks:

- `npm test` from `bot/`
- dry-run command for bot entrypoints, for example
  `node src/post-digest.mjs --dry-run --digest <digest.json>`
- explicit missing-env behavior when credentials are optional or HITL-gated

Expected evidence: test output, dry-run rendered message summary, and proof
that live posting is gated unless the issue requires a live post.

## Deployment and Scheduling

Use for `scripts/deploy/**`, systemd units, timers, and host split changes.

Offline checks:

- targeted deploy tests, for example `python -m pytest tests/test_deploy.py -q`
- static read-through of unit `ExecStart=`, `EnvironmentFile=`, `OnCalendar=`,
  `Persistent=`, lock, timeout, and failure paths

Live checks when the issue requires deployment:

- `systemctl list-timers` on the target host
- forced service run with journal excerpt
- catch-up behavior evidence for `Persistent=true`
- proof that secrets are not printed

Expected evidence: host, unit names, next-elapse times, forced-run result, and
any manual owner/HITL gate still blocking production cutover.

## Render and Branding

Use for ezQuake capture, title cards, overlays, Resolve assembly, ffmpeg, and
frame/audio verification.

Targeted checks:

- relevant Python tests, for example `python -m pytest tests/test_render.py -q`
- relevant branding tests, for example
  `python -m pytest tests/test_titlecard.py -q`
- generated artifact verifier for the feature under change

Artifact checks:

- frame or pixel verifier output
- `ffprobe` duration/frame count where applicable
- `ffmpeg` blackdetect for gameplay sections
- audio onset/silence verification for pulse or counter sync
- rendered output path under the configured `CLIPSMITH_OUTPUT_ROOT`
  destinations, not under the repo

Expected evidence: command output, artifact paths, verifier summaries, and any
manual visual inspection notes required by the issue.

## Publish and Browser Automation

Use for YouTube upload/review/publish and flows that require the owner's
logged-in Chrome profile.

Preconditions:

- Chrome running with remote debugging on `127.0.0.1:9222`
- correct profile logged into `@OneMoreQUAD`
- test playlist or real playlist explicitly identified
- candidate store has the required input status and metadata

Checks:

- fail-fast behavior when Chrome or login is unavailable
- screenshots at each major browser step
- idempotency proof for reruns, especially duplicate upload avoidance
- candidate store readback after status changes

Expected evidence: screenshots or video/message links, store readback, and a
clear note when production credentials or owner approval remain HITL.

## Pull Request Checks

Before merging any pull request:

- GitHub reports the pull request is mergeable and not a draft.
- Required checks are green, or a non-blocking exception has a documented
  reason.
- Blocking review comments have been rechecked against the current head.
- Conflict resolution preserves concurrent documentation and behavior changes.
- The pull request or linked issue contains validation evidence matching the
  touched area.
