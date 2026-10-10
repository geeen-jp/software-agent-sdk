# Claude Haiku 5.5 ACP qualification

- Repository: `geeen-jp/software-agent-sdk`
- Issue: `#69`

**Live verdict: FAIL — not qualified.**

`failed_stage: same_run_acceptance_evidence`

This is an evidence failure, not an observed provider incompatibility. The
Runtime record lists five bounded probe runs, but their per-probe JSON does
not establish every acceptance field for one session and completed turn. In
particular, the same-turn output needed to prove structured-output acceptance
and schema validity is inaccessible. The live tuple therefore cannot pass;
no provider mismatch is inferred from the missing evidence.

## Observed environment and run inventory

- Claude Code host binary checked: **2.1.284**.
- Repository `claude-agent-acp` version pin: **0.81.0**. This is the
  repository pin, not per-probe JSON evidence of the resolved runtime version.
- Runtime record: **five** bounded probe runs, at **01:42, 01:46, 01:50,
  01:53, and 01:57 JST**.
- SDK SHA recorded for the probe candidate:
  `2b7c522c0d399e13899ef011108e6af55477b8c3`.

These observations establish the host binary check, repository pin, run count,
and candidate source identity. They do not fill missing acceptance fields in
the individual probe JSON records.

No credential values are included in this report.

## Target tuple and live acceptance fields

Target tuple: requested model `claude-haiku-5-5`, `effort=high`, read-only,
Claude subscription/OAuth, and native `json_schema` output.

| Field | Verdict | Missing same-run evidence |
| --- | --- | --- |
| Exact served model | FAIL — not proven | Provider-owned assistant model identity `claude-haiku-5-5` from the probed turn. The `haiku` selector alone is insufficient. |
| High effort | FAIL — not proven | Accepted and effective `effort=high` configuration for the same session and turn. |
| Read-only | FAIL — not proven | Advertised and confirmed read-only mode, re-proved after config options, for the probed session. |
| Claude OAuth, no PAYG | FAIL — not proven | Subscription/OAuth authentication and absence of API-key, custom-base-URL, or PAYG configuration in the effective Claude environment. |
| Native structured output | FAIL — not proven | Acceptance of the submitted `json_schema` format and a completed response that validates against that exact schema. |

The qualification therefore fails at `same_run_acceptance_evidence`. A PASS
requires one record linking the exact served model, effective effort,
read-only proof, authentication path, and schema-valid response to the same
session and turn. The five bounded runs do not change that result while the
per-probe JSON lacks this linked evidence.

## Local regression coverage

The SDK tests cover Haiku 5.5 exact-model matching and mismatch, rejection of
alias-only or missing same-turn model evidence, high-effort application and
mismatch, read-only policy, resumed-session proof handling, OAuth isolation
without PAYG API-key preference, and native schema metadata for fresh and
resumed sessions. These tests do not establish that the live provider accepted
the schema or returned a response that validates against it.

## Source changes

No production source changed. The candidate adds regression coverage for the
existing generic Claude ACP path; the available evidence does not justify a
Haiku-specific SDK branch or another provider-mechanics change.

## Local verification

- Focused ACP tests: **526 passed**, 6 warnings.
- `make validate` on the code/test candidate: **PASS**.
  - Sync: passed.
  - SDK: 6,162 passed, 7 skipped, 10 xfailed, 671 warnings.
  - Agent Server: 2,083 passed, 1,260 warnings.
  - Workspace: 191 passed, 2 warnings.
  - Cross: 438 passed, 1 skipped, 1 deselected, 317 warnings.
  - Tools: 926 passed, 39 skipped, 1 warning.
  - Pyright: 0 errors, 4 warnings.
  - Pre-commit, `git diff --check`, and `git diff --exit-code`: passed.

The canonical run validated the code/test tree before this report-only update.
