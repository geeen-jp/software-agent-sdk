# Issue #1 Agent Server boundary PoC evidence

## Verdict

**FAIL** for the Issue #1 acceptance gate at the tested baseline. This is an
acceptance failure, not a reason to add workflow orchestration to OpenHands.
The exact main baseline lacks the requested generic ACP option contract,
durable run identity, and complete provider metadata; its read-only boundary
is not enforceable. Composer live evidence through Cursor is unavailable.

## Baseline

- Repository: geeen-jp/software-agent-sdk
- PoC baseline: cca903c808f7d954f1749a1a91890476d2f0a107
- Baseline ref: main, tested in detached worktree .agent_tmp/issue-1-main
- User worktree: aidw/issue-203-cursor-acp-parameterized-config at
  e2a5cfc497ad52a2628c081ce4460a0c2520533e
- Date: 2026-09-04 (Asia/Tokyo)
- Versions: uv 0.12.3, CPython 3.13.15, SDK v1.44.1
- Issue #1 was retrieved with gh issue view 1 --repo geeen-jp/software-agent-sdk.
- The user worktree was clean before this artifact. Its pre-existing ACP
  changes were not used to redefine the baseline.

## Capability matrix

| Capability | Existing API | Caller adapter only | Generic OpenHands patch | Unsupported | Evidence |
|---|---:|---:|---:|---:|---|
| Composer fast=false |  |  | ✓ |  | Baseline has no generic ACP session-option map. |
| Grok 4.6 medium, fast=false |  |  | ✓ |  | acp_model exists, arbitrary effort/fast does not. |
| Requested/effective config readback | partial |  | ✓ |  | Current model is best-effort; arbitrary option/provider state is not durable/public. |
| Workspace binding | ✓ |  |  |  | StartConversationRequest.workspace, optional worktree, returned in conversation info; conversation path/worktree tests. |
| Read-write execution | ✓ |  |  |  | LocalWorkspace, bash, editor, upload/download. |
| Read-only enforcement/detection |  |  |  | ✓ | No native mode; repository snapshots cannot observe arbitrary external, ignored, symlink, subprocess, or network effects. |
| Async create/start | ✓ |  |  |  | Conversation and /{id}/run endpoints. |
| Status query | ✓ |  |  |  | GET /api/conversations/{id}. |
| Terminal result | ✓ limited |  |  |  | Final response/events exist, but are not run-scoped. |
| Cancel/interrupt + readback | ✓ limited |  |  |  | Existing /interrupt cancels in-flight async arun and transitions to paused; no durable cancelled terminal status. |
| Response-loss lookup |  | ✓ conditional |  |  | Preallocated UUID enables lookup/attach only when unresolved create is not retried; omitted UUID can duplicate. |
| Restart lookup | ✓ |  |  |  | meta.json, state, and events reload; stale running becomes error. |
| Rate-limit classification | ✓ broad |  |  |  | LLMRateLimitError and ConversationErrorEvent.classification. |
| Provider/model unavailable classification | ✓ broad |  |  |  | Existing ErrorClassification exposes broad config/transient/unknown kinds; finer subtypes are unavailable. |
| Auth failure classification | ✓ broad |  |  |  | SDK mapping exists; provider metadata is absent at server boundary. |
| Usage/provider observation | partial |  | ✓ |  | Token/cost and requested/current model exist; effective provider/retry metadata do not. |
| No implicit cross-model fallback |  | ✓ policy | ✓ strictness gap |  | FallbackStrategy is opt-in, but ACP model rejection is tolerated and can run the server default; strict verification is needed. |

The current branch already contains a small, generic ACP parameterized-config
change with focused tests, but it is pre-existing and not claimed as a PoC
change against the baseline.

Key baseline source/test anchors: `request.py:109` for worktree binding,
`conversation_service.py:195-274` for worktree creation,
`conversation_router.py:250-267` and `tests/cross/test_remote_conversation_live_server.py:2362`
for interrupt, `error_classification.py:52-169` and
`tests/sdk/conversation/remote/test_remote_conversation.py:1235` for structured
failure, `state.py:46-77` for terminal status, and
`tests/agent_server/stress/test_lease_contention.py:56-74` for concurrent
start behavior.

## Deterministic validation

From the exact baseline detached worktree:

- make build: passed; dependencies and pre-commit installed.
- Agent Server conversation/event/response/live-server selection:
  `uv run pytest tests/agent_server/test_conversation_service.py
  tests/agent_server/test_conversation_router.py
  tests/agent_server/test_conversation_response.py
  tests/agent_server/test_event_service.py
  tests/cross/test_remote_conversation_live_server.py -q`: **344 passed,
  1 skipped**.
- Failure mapping, retry/fallback, metrics, timeout, conversation response,
  and event-service selection:
  `uv run pytest tests/sdk/llm/test_exception_mapping.py
  tests/sdk/llm/test_exception_classifier.py tests/sdk/llm/test_llm_fallback.py
  tests/sdk/llm/test_api_connection_error_retry.py
  tests/sdk/llm/test_llm_retry_telemetry.py tests/sdk/llm/test_llm_metrics.py
  tests/sdk/llm/test_llm_timeout.py tests/sdk/llm/test_exception.py
  tests/agent_server/test_conversation_response.py
  tests/agent_server/test_event_service.py -q`: **242 passed**.
- Baseline reconnaissance recorded ACP model/settings/fallback checks as
  88 + 70 + 16 + 25 passed; these counts are reported by read-only agents.
- git diff --check: passed.
- No provider quota or rate limit was intentionally consumed.

## Composer live verification

Cursor CLI ran a simple write in disposable workspace
.agent_tmp/issue-1-fixture with exit code 0. The session identified as
Cursor Grok 4.6, not Composer. The CLI exposed no Composer picker or
fast=false control.

- Composer requested/effective configuration: unavailable
- ACP fast=false: no live CLI control/readback; the weekly usage export
  identifies non-fast Cursor Composer records by the absence of `fast` in
  `composer-2.5` (20 records), alongside `composer-2.5-fast` (83 records)
- workspace write and terminal result: passed, exit 0
- fallback: none observed, but model proof is incomplete

## Grok live verification through Cursor CLI

The accepted live check used Cursor CLI, not standalone Grok CLI. The command
completed a write in disposable workspace .agent_tmp/issue-1-fixture with exit 0.

The following usage evidence was obtained from the Cursor Web usage export. It
is an external Cursor-side observation, not telemetry read from the local Agent
or ACP subprocess. It confirms the model selected by Cursor's service-side
usage record, but cannot be used by local code to verify the model actually
executing through ACP during a run.

The one-week Cursor Web usage export covers 215 Included records from
2026-08-29T00:34:02.913Z through 2026-09-04T05:39:19.044Z. For this export,
the model name is the fast/non-fast discriminator; `Max Mode` is not used as a
proxy. The relevant model-name counts are:

| Model name | Records | `fast` in model name |
|---|---:|:---:|
| `cursor-grok-4.6-medium` | 56 | No |
| `cursor-grok-4.6-high-fast` | 15 | Yes |
| `cursor-grok-4.5-high` | 7 | No |
| `composer-2.5` | 20 | No |
| `composer-2.5-fast` | 83 | Yes |

All 215 records have `Max Mode=No`, which is not the discriminant used here.
The two records from 2026-09-04 are `cursor-grok-4.6-medium` (no `fast` in the
model name), matching the requested medium, non-fast Cursor variant. The
weekly export also contains 20 `composer-2.5` records with no `fast` suffix and
83 explicit `composer-2.5-fast` records. This is strong Cursor-side model-name
evidence for the Cursor Web service, but it does not prove that an Agent Server
ACP subprocess returned the same effective state, and no local code path can
currently make that verification.

- requested model: grok-4.6
- requested effort: medium
- effective Cursor model: cursor-grok-4.6-medium
- Cursor fast/non-fast determination: model-name `fast` presence; the current
  Grok records are non-fast
- effective Agent Server ACP model/options: unavailable and not locally
  code-verifiable from this usage export
- workspace write and terminal result: passed, exit 0
- fallback: none observed

## Workspace and access

Explicit workspace binding is represented by StartConversationRequest.workspace
and returned in conversation info. The workspace is writable by design.

There is no native Reviewer/Auditor read-only enforcement. readOnlyHint is
descriptive. Bash accepts shell commands and arbitrary working directories;
file routes validate absolute paths rather than workspace containment. Starting
a fresh workspace may itself run git init. The baseline does provide an
optional conversation worktree, but that is isolation/branching, not read-only
enforcement.

A caller adapter can detect scoped repository mutations by comparing canonical
pre/post state, but it cannot reliably observe all ignored/external/symlink,
subprocess, or network side effects. Therefore this PoC classifies true
read-only execution as unsupported; a repository-only detector must not be
advertised as a complete Reviewer/Auditor guarantee.

## Lifecycle and recovery

- Server identity is a UUID; caller may supply conversation_id.
- Caller can map AttemptID to a preallocated conversation_id.
- Sequential explicit-ID duplicate create returns the existing conversation
  (200) and does not replay the initial message. Concurrent duplicates may
  receive a lease conflict.
- Statuses are idle, running, paused, waiting_for_confirmation, finished,
  error, stuck, deleting; terminal are finished, error, and stuck.
- /run starts a background task and returns Success, with no run ID.
- /pause is cooperative; an in-flight LLM call may complete.
- /interrupt is an existing API that cancels an in-flight async arun task,
  emits InterruptEvent, transitions to paused, and is resumable. Synchronous
  execution falls back to cooperative pause.
- There is no durable cancelled state; DELETE removes the conversation.
- Explicit UUID plus GET/event reconciliation is workable if the caller does
  not retry an unresolved create. The baseline lacks a locked same-service
  recheck/idempotency receipt, so unresolved creates must be reported UNKNOWN;
  server-generated UUID retry is not safe.
- Persistence reloads meta.json, base_state.json, and event files. Persisted
  running conversations become error rather than resuming.
- Without a per-run receipt, retrying /run after response loss is ambiguous.

## Failure semantics

| Failure | Internal representation | Boundary/caller result | Lost or unavailable |
|---|---|---|---|
| Rate limit | LLMRateLimitError, same-model retry | Error event with kind, retryable, user_action, error_id | Retry-after/header/provider metadata |
| Quota/credit | Budget error or provider text | Broad quota classification for known patterns | Dedicated credit semantics/balance |
| Provider unavailable | LLMServiceUnavailableError | Generic error event with transient classification | Provider/status metadata |
| Model unavailable | Generic/config/not-found error | Broad config/unknown classification | Stable model-unavailable subtype |
| Authentication | LLMAuthenticationError or text mapping | Generic error event with auth classification | Provider identity/subtype |
| Attempt timeout | LLMTimeoutError or caller wait timeout | Error event or ambiguous ConversationRunError | Whether server continued |
| Cancellation | Cooperative pause | paused state | Durable cancellation disposition |
| Response/process loss | No receipt/run ID | Reconcile only with known UUID when create is not retried | Exactly-once dispatch proof |

TestLLM injects deterministic exceptions. Existing mapping/retry/event tests
prove the basic paths without consuming provider quota. Same-model retry is
distinct from explicit FallbackStrategy, which calls another model only when
the caller configures it.

## Usage/provider observations

Available: conversation ID, requested LLM/ACP model, best-effort current ACP
model, successful token usage, accumulated cost, and conversation metrics.

Unavailable at the stable server boundary: authoritative effective provider,
authoritative effective model for all ACP servers, provider execution ID,
per-run ID, retry count, Retry-After, rate-limit headers, dedicated quota
metadata, and reliable failure-attempt usage/cost.

## Downstream-neutral contract notes

### AgentExecutionRequest

Keep only neutral semantics:

- logical Attempt identity and caller idempotency identity
- requested model/provider configuration and execution parameters
- explicit workspace/repository binding and access mode
- prompt/input
- timeout and cancellation policy/deadline

An adapter may translate this to a conversation UUID and OpenHands request,
while retaining the original Attempt ID outside OpenHands.

### AgentExecutionResult

Include:

- server execution identity and logical Attempt identity
- terminal disposition: SUCCEEDED, FAILED, CANCELLED, UNKNOWN
- requested and authoritative effective model/provider when available
- output/result
- structured terminal failure: kind, retryable, safe user action, stable ID
- usage/cost and retry/rate-limit metadata when observed
- conversation/run/provider references when available

Fields not exposed by OpenHands must be unavailable, never caller guesses.

## Independent review

Sol was reserved for the final independent audit and was not used for
reconnaissance. Sol's first audit returned NEED_FIX; all findings were
corrected in this artifact. Sol's final re-review returned **PASS**.

## Acceptance criteria

| Criterion | Result |
|---|---|
| Composer fast=false evidence | PARTIAL — Cursor Web usage has 20 `composer-2.5` (no `fast`) records, but the live CLI run identified as Grok; this is not local ACP verification. |
| Grok model/effort/fast evidence | PARTIAL — Cursor Web usage identifies `cursor-grok-4.6-medium` with no `fast`, but the executing Agent/ACP model is not code-verifiable from that external usage data. |
| Workspace binding and RW/RO | PARTIAL — binding/RW/worktree proven; true native RO unsupported. |
| Async start/query/terminal/cancel | PARTIAL — create/query/run/interrupt proven; durable cancelled disposition absent. |
| Response-loss recovery | PARTIAL — caller UUID permits lookup only when unresolved create is not retried; no run receipt. |
| Restart/persistence lookup | PASS for persisted lookup, stale-running-to-error documented. |
| Failure characterization | PARTIAL — structured broad mappings exist; provider/run metadata and finer subtypes are lost. |
| Usage/provider metadata | PARTIAL — usage/cost and requested/current model partial. |
| No implicit cross-model fallback | PARTIAL — fallback is opt-in, but ACP model rejection can silently use the server default. |
| Generic minimal patch | PARTIAL — strict ACP verification, run receipt, and metadata additions were identified but not applied to main. |
| Downstream-neutral contract | PASS as notes with unavailable fields explicit. |

## Remaining limitations

1. No accepted Cursor Composer live execution evidence.
2. No authoritative effective ACP provider/config readback.
3. No durable per-run receipt or exactly-once external-side-effect guarantee;
   unresolved create must be returned as UNKNOWN rather than retried.
4. No durable cancellation disposition; /interrupt is available and resumable.
5. No native read-only sandbox; repository Git detection cannot cover all effects.
6. Provider quota/credit and Retry-After metadata are not preserved.
7. Concurrent same-service create has no locked recheck/idempotency receipt;
   callers must return UNKNOWN when commitment cannot be established.
8. Pre-existing user-branch ACP changes were intentionally not overwritten or
   presented as baseline main.
