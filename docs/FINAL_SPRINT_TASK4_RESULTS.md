# Final Sprint Task 4 Results

## Implemented

- Fixed candidate CI verification to derive repository identity from the
  validated pull request reference. The previous path referenced an undefined
  `repository` local and returned `unknown` for otherwise valid evidence.
- Candidate CI still requires the exact candidate SHA, all named checks, and a
  fresh pull request head before and after the read. The adapter has no CI write
  capability.
- Tightened the behavioral-verification broker boundary: source/test payloads
  are bounded, changed paths must be safe relative POSIX paths, and a successful
  report must attest that the candidate ran and exited zero under the exact
  requested limits and isolation controls.
- Expanded offline confidence-calibration evidence to require distinct
  prediction IDs, explicit calibration/holdout splits, finding and fix-safety
  label coverage, a signed manifest tied to the exact examples, and held-out
  metrics. Calibration reports can be stored as owner-only signed artifacts.
  The service still never turns model-authored confidence into calibrated
  confidence.
- Added explicit API and UI status for unavailable calibration, candidate CI
  not run, behavioral testing not run, and disabled commit execution.
- Kept the existing human fix approval/export flow. No automatic patch
  application or commit path was enabled.

## Verification

- Offline Python suites: **448 passed**, with one third-party ChromaDB
  deprecation warning.
- Focused Task 4 and related API/GitHub tests: **62 passed** before the final
  non-execution regression test; the final full-suite run includes it.
- Frontend tests: **14 passed**.
- Frontend production build: **passed**.
- No live Groq, other model, or GitHub write requests were made. No model
  weights were loaded and no candidate source was executed.

## Remaining blockers and limits

- There is no separately reviewed isolated execution broker configured. The
  behavioral verifier therefore returns `not_run`; the broker protocol and
  tests do not constitute a production sandbox or behavioral regression test
  runner.
- There is no signed independent calibration dataset. Confidence remains
  unavailable to the autonomous-fix policy.
- No candidate commit exists in a staging branch, so exact-candidate CI cannot
  complete for a proposed patch through the current read-only path.
- GitHub check evidence is matched by candidate SHA and named check, but the
  current verifier does not independently enforce a trusted workflow identity.
- Autonomous commit execution remains hard-disabled in policy and adapter code.
  No prepared change is applied automatically; manual approval/export remains
  the available fix workflow.
- No Task 4 changes were committed or pushed. The existing worktree changes
  were preserved.
