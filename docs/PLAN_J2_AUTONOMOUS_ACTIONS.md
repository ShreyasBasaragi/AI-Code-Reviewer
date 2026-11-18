# Plan J2 — Risk-Based GitHub Actions and MCP Integration

## Scope and execution gate

J2 extends the J1 webhook worker with a deterministic policy decision, optional
targeted review comments, safe patch preparation, and durable action records.
Manual PR review, candidate generation, verification, approval, rejection, and
export remain available. J2 never creates a commit, applies a patch, approves,
merges, or force-pushes.

Commit execution is hard-disabled in `mcp_server/github_policy.py` by the
constant `AUTONOMOUS_COMMIT_EXECUTION_ENABLED = False`. Repository configuration
cannot override it. `execute_commit` always returns
`AUTONOMOUS_COMMIT_DISABLED`; a prepared commit plan has no commit SHA and does
not issue a write request. J3 must separately implement behavioral/CI checks
and review the execution gate before any commit capability is considered.

## Policy model

`mcp_server/github_policy.py` evaluates validated finding data, provenance,
category/severity, path protection, current and expected head SHA, independently
calibrated confidence, actual changed-line count, branch/fork state, verification
statuses, repository opt-in, and presence of an action credential. It returns
an action, eligibility, status, denial reasons, required checks, and policy
evidence. Missing or ambiguous data denies external action.

Only `style`, `naming`, and `formatting` are in the fixed low-risk allowlist;
the current review schema emits `style` but does not emit independent confidence
or a calibrated confidence source. The webhook coordinator deliberately ignores
confidence-like fields in model output. Consequently, the current live pipeline
cannot mark an autonomous candidate eligible. Security, high-severity, bug,
performance, authentication/authorization, cryptography, business-logic, and
architecture cues cannot enter the automatic-fix path. These cues can still be
eligible for an explicitly enabled human-readable review comment.

The fixed safety bounds are confidence >= 0.80, no more than three changed
lines, exact reviewed source at the expected head SHA, an unprotected path,
syntax and static checks passed, and every configured check passed. Required
checks always include syntax, static, behavioral, and CI; repository settings
cannot remove these checks. Behavioral and CI results are `not_run` in J2, so
they block automatic eligibility.

## Administrator-controlled repository configuration

There is no HTTP or MCP mutation endpoint for policy. The administrator sets
`GITHUB_ACTION_POLICY_PATH` to a local JSON file owned and maintained outside
the repository/PR content. The loader rejects malformed versions, unknown
fields, writable-by-group/world files, invalid values, and policies that remove
required verification checks. Missing or invalid configuration means all
automated writes are disabled. PR code, README files, descriptions, model output,
and webhook fields are never read as policy.

Example:

```json
{
  "version": "github-action-policy-v1",
  "repositories": {
    "acme/demo": {
      "publish_review_comments": false,
      "enable_autonomous_fixes": false,
      "allowed_low_risk_categories": ["style", "naming", "formatting"],
      "confidence_threshold": 0.8,
      "max_changed_lines": 3,
      "protected_paths": ["src/generated/"],
      "permitted_branches": ["feature"],
      "required_checks": ["syntax", "static", "behavioral", "ci"],
      "required_github_checks": ["unit-tests", "lint"],
      "allow_source_sharing": false
    }
  }
}
```

Comment publication and autonomous fixing default to false. The hard-coded
protected paths and sensitive-file exclusions cannot be removed by policy.
The threshold can be raised but never below 0.80; line limit is clamped to at
most three. Branch entries and GitHub CI check names are exact strings. J3 adds
`required_github_checks` (empty by default, so CI cannot pass without trusted
names) and `allow_source_sharing` (false by default); hosted PR review also
requires global `source_sharing_consent` through the authenticated provider
settings API or an administrator environment setting.

## MCP action layer and comments

`mcp_server/github_actions.py::GitHubMCPActionAdapter` is the narrow action
capability used by the existing J1 webhook worker. It has one implemented write
operation: create an inline PR review comment at a fixed GitHub endpoint. It
does not offer generic URLs, arbitrary API methods, paths, or branch writes.
`mcp_server/server.py` adds a read-only MCP tool to inspect sanitized action
records; no model-callable GitHub write tool is registered. The worker is the
only comment dispatcher and calls the adapter only after policy authorization.

Inline comments require a validated finding, validated cited evidence (or a
static rule identifier), a changed file, one unambiguous added-line location,
and a fresh open PR whose base repository and head SHA match. Comments use the
GitHub new-side `RIGHT` line semantics and event-pinned `commit_id`. Unmapped,
deleted, stale, or ambiguous locations are skipped and recorded; J2 does not
emit an unrelated top-level comment fallback. Body text is bounded, control
characters/HTML/URLs are sanitized, and only evidence IDs—not private evidence
text—are included. GitHub permission/rate-limit failures are sanitized.

Set `GITHUB_ACTIONS_TOKEN` separately from the read-only `GITHUB_TOKEN`, using a
fine-grained GitHub credential with only Pull requests: write for explicitly
configured repositories. No credential is returned in API/MCP results. Keep
publication off until the administrator has reviewed that credential and policy.

## Fix preparation

`GitHubMCPActionAdapter.prepare_change` accepts only an exact changed Python
path, re-fetches PR state and the source file from the immutable head revision,
and requires byte-for-byte equality with the reviewed original. It checks
syntax, deterministic static-analysis deltas, and the actual unified-diff
changed-line count (maximum three). It returns a bounded diff with syntax/static
passed and behavioral/CI `not_run`, status `PENDING_REQUIRED_VERIFICATION`, and
commit execution `AUTONOMOUS_COMMIT_DISABLED`. It never executes or imports
candidate code. This is a proposal interface, not patch application.

`prepare_fix_candidate` delegates to the existing `verify_fix_candidate` and
unified-diff functions before the pinned-source proposal checks. Behavioral and
CI checks remain not run.

The existing manual candidate flow remains separate and unchanged. The webhook
path will not ask the provider to generate a fix without an independently
calibrated confidence source and policy eligibility; the current schema supplies
neither, so model-authored confidence cannot enable this path.

## Commit preparation (disabled)

`prepare_commit` re-reads the current pull, verifies base repository, branch,
head SHA and fork identity, and returns a proposed AI-attributed commit message
for audit/planning. `commit_sha` remains null. Protected branches, stale heads,
forks, malformed changes, and unavailable identities are denied. `execute_commit`
is unconditional disabled and has no transport call. No force-push or default
branch update can occur in J2.

## Durable action records and idempotency

J1's SQLite file now also has a bounded `github_action_records` table. A unique
key over `(delivery_id, finding_id, action_type)` reserves each decision. The
record stores repository/PR/delivery/revisions, policy decision and denial
reasons, candidate ID, verification status, publication state, sanitized error,
GitHub comment ID, future commit SHA, and timestamps. It stores no credential,
source body, comment body, or retrieved evidence text. Terminal rows follow J1's
retention and row limits. On restart, an interrupted write marked `running` is
changed to `unknown`; it is never blindly retried. Network timeouts are also
recorded as `unknown` and suppress another attempt for that delivery/finding.

The authenticated webhook status endpoint returns these records. The existing
MCP status tool returns only sanitized action records. The browser receives a
policy-mode summary for manual PR reviews. An administrator can enter the
webhook status token on demand to query the action-only endpoint; the frontend
does not persist the token and clears it after each request. That endpoint omits
the full review result.

## Offline validation

Run with existing project dependencies:

```sh
./.venv/bin/python -m pytest -q tests/test_github_policy.py tests/test_github_actions.py tests/test_github_webhook.py tests/test_repository_context.py tests/test_github_pr.py tests/test_phase_d1_http_api.py tests/test_phase_d3_integration.py tests/test_phase_c1_fix_generation.py tests/test_phase_c2_fix_verification.py tests/test_phase_c3_fix_approval_export.py tests/test_plan_i_end_to_end.py tests/test_plan_i_validation.py tests/test_static_analysis.py
npm --prefix frontend test -- --run
npm --prefix frontend run build
```

Tests use temporary SQLite files and injected read clients/transports. No tests
make live GitHub, Groq, or Qwen requests, load model weights, or execute a
candidate. UI build uses the existing frontend dependencies.

## Limits and J3 dependencies

- The PR finding schema has no independent confidence calibration. Autonomous
  fix eligibility therefore fails closed in the running review pipeline.
- J2 does not execute unit tests, behavioral checks, project test commands, or
  CI. Those statuses remain `not_run` and prevent commit eligibility.
- Commit execution is hard-disabled in code, regardless of repository opt-in.
- The webhook worker is still single-process; lifecycle handles remain local.
- There is no summary-comment fallback, action retry queue, deployment monitor,
  or alerting integration.
- J3 must design isolated behavioral/CI verification, threat-model commit
  execution, preserve fresh-revision and branch checks, and add operational
  safeguards before reconsidering the disabled gate.
