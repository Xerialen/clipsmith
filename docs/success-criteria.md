# Success Criteria

last-verified: 2026-06-11
status: active working contract

This file defines the repository-level meaning of success. A specific issue
remains the concrete task contract; this document provides the stable project
bar that those issues must serve.

## Project-Level Success

The north star is the OF THE WEEK pipeline described in
[pipeline-spec.md](pipeline-spec.md): fresh QuakeWorld 4on4 demos produce
weekly highlight candidates, the owner curates them in Discord, selected
candidates render to branded MP4s, and approved clips publish to the One More
QUAD YouTube channel without fragile manual glue.

Long-term success means the pipeline can survive a full month unattended:

- new demos are detected on schedule without skipped or duplicated candidates;
- weekly Discord digests are posted with useful, reviewable candidates;
- owner selections drive render, assembly, upload, review, and publish stages;
- failures are visible and reruns converge safely;
- engagement metrics can answer whether the community-content loop is working.

## Current Stage Success

Current `main` contains the detection/digest spine, Discord review bot, render
orchestrator, branded assembly path, and an experimental COME-BACK detector.
The immediate success criterion is completing publication automation without
weakening the reliability of the implemented stages.

For new work, success is:

- solve a clearly documented user or operational problem;
- implement only the scoped definition of done;
- preserve the pipeline contracts in [pipeline-spec.md](pipeline-spec.md);
- update relevant public documentation;
- produce validation evidence strong enough for the stage touched.

## Phase Gates

These gates summarize the roadmap demos. A phase is not complete until the
demo is proven with evidence in the relevant pull request or issue.

| Phase | Success gate |
|---|---|
| Phase 1 - Detection & candidates | Run the CLI against a real finished week and get a correct, ranked candidate digest JSON. |
| Phase 2 - Discord review loop | A real Monday digest lands in Discord; clicking/selecting flips a candidate to `selected`. |
| Phase 3 - Render & assembly | A selected candidate renders to a finished branded MP4 hands-off. |
| Phase 4 - Publish & feedback | One clip goes candidate -> Discord -> render -> unlisted upload -> approval -> public. |
| Phase 5 - Hardening | The pipeline survives a full month unattended; reruns are idempotent and failures are visible. |

## Evidence Required

Every meaningful change must leave evidence appropriate to its risk:

- relevant automated tests, with actual command output recorded in
  the relevant pull request or issue;
- dry-run output for side-effecting CLIs before live writes;
- store/status readback for changes that move candidate lifecycle state;
- rendered artifact paths plus frame/audio/blackdetect checks for render work;
- Discord/YouTube screenshots or message/video links for live integration work;
- systemd timer, journal, and forced-run evidence for deployment work;
- issue and PR comments that say what remains blocked or deliberately deferred.

## What Does Not Count

- Code that passes a narrow unit test but does not satisfy the issue's
  Definition of Done.
- A generated artifact that was not inspected with the documented verifier.
- A live side effect without a dry-run or rollback story when one is practical.
- "Merge-ready" claims on conflicted PRs or PRs with missing checks.
- Silent documentation drift: changing behavior without updating the public
  contract and operational guidance.
