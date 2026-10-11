# Claude ACP persisted-session restore diagnostic (#106)

## Finding

The reported #69 failure is localized to ACP session startup, before a prompt.
The Issue record says `session/load` returned `Internal error`, the restore
fell back to `session/new`, and that call returned `RequestError`. The available
repository artifacts do not include the sanitized per-attempt trace or a
session-state snapshot, so they do not establish why either ACP call failed or
which process boundary was restarted. There is no LLM response to assess and
no model-specific failure verdict.

The sequence above comes from the #106 Issue record. The checked-in
[Haiku 5.5 qualification report](claude-haiku-5-5-acp-qualification.md)
identifies #69 and records five bounded probe runs, but it does not document
this restore attempt. The Issue record describes fresh and repeated turns
passing exact served-model, high-effort, isolated-OAuth, shared read-only, and
current bounded ScopeJudge native-schema checks. The checked-in report records
an overall `same_run_acceptance_evidence` failure because its per-probe records
do not link all acceptance fields to one completed turn. Both are preserved
within their recorded scopes; neither establishes the cause of the
persisted-session errors.

## Reconstructed stages

| Stage | What the record establishes | What remains unknown |
| --- | --- | --- |
| Restore requested | #69 attempted a persisted-session restore. Its reported `session/load` implies a prior ID was supplied; the inspected SDK path reads it from `ConversationState.agent_state` and starts an ACP server during initialization. | The state snapshot, ID value/provenance/validity, and whether the ACP child, SDK Agent, or a larger runtime was restarted. |
| `session/load` | The Issue record reports `Internal error`, before any prompt. | Raw request/response, JSON-RPC error code and data, exception type seen by the SDK, session-store contents, and the effective cwd/MCP inputs. |
| `session/new` fallback | The Issue record reports the fallback call and a `RequestError`. This is still session startup. | Raw response and error code, and whether credentials, session storage, cwd, MCP configuration, or other effective environment changed since the successful turns. |
| Prompt/model invocation | The Issue record places both errors before a prompt. | No prompt payload or model response is available; none should be inferred. |

The historical restart boundary is therefore **unknown**. The checked-in
qualification report describes isolated OAuth for its qualification tuple, but
does not connect an effective credential-source fingerprint or credential
change to this restore attempt. The restored session's state and ACP storage
are likewise unavailable. No session IDs or credential values are included in
this report. The Issue summary names `session/load` and `session/new`; without
a wire trace, other handshake/auth/config calls and their request envelopes
cannot be enumerated or verified.

## SDK path and attribution

At the #69 candidate source SHA
[`2b7c522c0d399e13899ef011108e6af55477b8c3`](https://github.com/geeen-jp/software-agent-sdk/tree/2b7c522c0d399e13899ef011108e6af55477b8c3),
`ACPAgent.init_state()` reads the prior session ID from agent state and starts
the ACP server subprocess ([state lookup](https://github.com/geeen-jp/software-agent-sdk/blob/2b7c522c0d399e13899ef011108e6af55477b8c3/openhands-sdk/openhands/sdk/agent/acp_agent.py#L3461),
[subprocess spawn](https://github.com/geeen-jp/software-agent-sdk/blob/2b7c522c0d399e13899ef011108e6af55477b8c3/openhands-sdk/openhands/sdk/agent/acp_agent.py#L3850)).
When a prior ID exists, it sends `session/load` with that ID, cwd, MCP
configuration, and session metadata. It catches `ACPRequestError` from that
call and then sends `session/new`; other load failures propagate without that
fallback ([load and fallback](https://github.com/geeen-jp/software-agent-sdk/blob/2b7c522c0d399e13899ef011108e6af55477b8c3/openhands-sdk/openhands/sdk/agent/acp_agent.py#L4024)).
The prompt call is a later operation ([prompt](https://github.com/geeen-jp/software-agent-sdk/blob/2b7c522c0d399e13899ef011108e6af55477b8c3/openhands-sdk/openhands/sdk/agent/acp_agent.py#L4557)).

This locates the reported failure at ACP startup. If the historical calls
followed this source path, the SDK received an `ACPRequestError` from
`session/load` before it issued `session/new`; the missing raw trace prevents
confirming that exception mapping or the exact wire messages. The second error
is reported on `session/new`, which also precedes prompt dispatch. The evidence
does not distinguish an SDK request/handling problem from adapter behavior or
persisted-state/environment conditions. It provides no evidence of model
output behavior.

The source review also finds that the restore handler catches any
`ACPRequestError`, although its comment describes recovery for a forgotten
session. The Issue record points to [SDK #108](https://github.com/geeen-jp/software-agent-sdk/issues/108)
as the separate follow-up for this narrow fallback-classification behavior.
The minimal code-level reproduction is to provide `ACPAgent.init_state()` a
prior `acp_session_id`, have `session/load` raise an `ACPRequestError` for a
non-missing-session failure (for example, an internal error), and observe the
SDK issue `session/new`. The owning layer is the SDK restore fallback in
`ACPAgent`; SDK #108 already tracks it. This separate behavior does not
establish that the fallback caused the #69 adapter response sequence, and no
additional implementation issue is indicated by this investigation.

[`resume_transcript.py`](../openhands-sdk/openhands/sdk/event/resume_transcript.py)
describes a separate bootstrap-prompt approach for when ACP session storage is
unavailable. No available #69 evidence shows that this transcript was rendered
or sent.

## Related evidence and probe decision

- #70 records a resume without native schema; it does not explain #69.
- #58/#59 record a successful same-session `restart_for_updated_credentials`
  load and, separately, a different Agent instance receiving `Resource not
  found` before starting a new session. These paths and errors do not establish
  a shared cause with #69.
- #60/#61 cover fresh-turn schema evidence, and #45–47 cover their recorded
  Opus scopes. They do not qualify persisted ScopeJudge-schema resume.
- #41 shared read-only evidence remains contextual; it does not provide the
  missing #69 process boundary or ACP trace.

**Optional Sonnet 5.5 Medium probe: skipped.** The unresolved question is which
historical process/session or persisted-state condition produced the #69
pre-prompt ACP errors. A new probe with the same schema would exercise a new
lifecycle and cannot identify that historical condition or turn either error
into model-output evidence. The probe therefore cannot change this diagnosis.

No SDK source, tests, package pins, or runtime configuration were changed for
this diagnostic.
