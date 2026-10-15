# Claude ACP ScopeJudge outcome schema qualification

## Verdict

**PASS for two exact corrected schema expressions on the qualified Claude ACP
path.** The previous PASS was superseded because its fixtures omitted three
canonical outcome constraints. The corrected fixtures restore those
constraints, and fresh live turns accepted all three valid outcomes and rejected
each newly added forbidden combination for both exact schema digests below.
The qualification remains limited to these expressions and the recorded
runtime identity; it does not replace Workflow validation.

The field names below are neutral fixtures required by this repository-side
qualification. They are representative stand-ins, not the Workflow canonical
schema. The Workflow must substitute its canonical fields and confirm the
result against its own schema before adopting either expression.

## Run identity

- Repository baseline: `7e2841fb98964e26d74cd60f92504cbd9c481d37`
- SDK source SHA used for corrected-schema live qualification: `73df482ff2ea340ecd676ab2a3c851026228f7b7`
- SDK version: `1.44.1`
- Date: 2026-10-15 (Asia/Tokyo)
- Requested and same-turn verified served model: `claude-haiku-5-5`
- Verified reasoning effort: `high`
- SDK permission policy: `read_only` (ACP selected session mode `default`)
- Authentication: logged-in Claude Code OAuth session; `ANTHROPIC_API_KEY`,
  `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, and
  `CLAUDE_CODE_OAUTH_TOKEN` were unset
- `claude-agent-acp`: `0.81.0` (requested and reported by ACP initialize)
- Claude Code: `2.1.293`
- All 12 corrected-schema turns passed same-turn root-model verification with
  no delegated model reported. The runtime advertised zero tools in each run.

Each live case used a fresh `LocalConversation` and a fresh temporary workspace.
The SDK passed the original schema through native Claude ACP session metadata.
No SDK schema rewriting or prompt-only fallback occurred. When Claude rejected
a structured call, it later returned ordinary text; that text has no structured
result and must be rejected by the caller. No file operation was used.

## Candidate matrix

All five candidates keep `type: object` at the schema root and omit root
`oneOf`, `anyOf`, and `allOf`. The offline tests use the Draft 2020-12 evaluator
and verify three valid outcomes plus seven invalid cases for each candidate,
including the three canonical restrictions missing from the previous round.
Across 69 qualification tests, all candidates passed the offline matrix and
digest checks.

| Candidate | Offline evaluator | Live result | Canonical mapping | Schema SHA-256 |
|---|---|---|---|---|
| `root_if_then` | Three valid and seven invalid fixtures pass | All three valid outcomes accepted; all three newly added forbidden combinations rejected | Identity mapping; no wrapper | `114fd8572b0ad69b683e1409be79c6b52507be404a8fce34d39f7e0bf8b61c8c` |
| `nested_any_of` | Three valid and seven invalid fixtures pass | All three valid outcomes accepted; all three newly added forbidden combinations rejected | Unwrap `result`; lossless when the wrapper is not part of the canonical result | `68ce62139c6dc1b0fb155a534c4a451abd1d5fa73fcba115066849227239d01d` |
| `nested_one_of` | Three valid and seven invalid fixtures pass | Not live-tested | Wrapper and unwrapping required | `4423cfe1e4493efb0d8b1ccdd223e3b314f4cde58350eb946762bedeb6621d15` |
| `nested_if_then` | Three valid and seven invalid fixtures pass | Not live-tested | Wrapper and unwrapping required | `d284d7e3b9e9c5bb236118a8fcc970bf486b361bbd41fbe884d5e7cee342c196` |
| `nested_all_of_conditionals` | Three valid and seven invalid fixtures pass | Not live-tested | Wrapper and unwrapping required | `49444cffba28cdc033d7b3a7fda52381b7ad2de20ea747ccb76edfe5d73de37b` |

The candidate schema builders, pinned schema digests, payload fixtures, and
evaluator tests are in
[`test_claude_scopejudge_schema_qualification.py`](test_claude_scopejudge_schema_qualification.py).
The existing Claude root-combinator rejection remains in place. Codex continues
to reject conditional keywords through its own qualified-subset validator.

## Corrected live behavior and enforcement

Each exact schema digest had six fresh `LocalConversation` turns: one valid
PASS, TRIM_REQUIRED, and NEEDS_HUMAN result, then one intentionally forbidden
payload for each corrected constraint. All six valid cases completed one
`StructuredOutput` call with a schema-valid payload. For each of the six
invalid cases, the ACP emitted two failed `StructuredOutput` calls containing
the requested invalid payload and no completed invalid result:

- PASS with non-empty `scope_findings` was rejected by `maxItems: 0`.
- NEEDS_HUMAN with non-empty `scope_findings` was rejected by `maxItems: 0`.
- TRIM_REQUIRED with non-empty human reasons was rejected by `maxItems: 0`.

The error paths were `scope_findings` and `human_reasons` for the root
conditional digest, and `result/scope_findings` and `result/human_reasons` for
the nested `anyOf` digest. This separates syntax acceptance from enforcement:
valid calls completed, while deliberately invalid calls failed at the ACP
structured-output validation boundary. Explanatory text after a failed call is
not a structured result and must still be rejected by a caller.

Every turn selected session mode `default` under the SDK `read_only` policy,
verified `effort=high`, and passed same-turn root-model verification for
`claude-haiku-5-5`; no delegated model was reported. Claude ACP initialized as
`@agentclientprotocol/claude-agent-acp` version `0.81.0`. Claude Code reported
version `2.1.293`; the run used the first-party OAuth login, with API key and
base-URL variables unset. The runtime advertised zero tools and no file
operation occurred.

Conversation teardown logged the ACP client's `message queue already closed`
receive-loop warning after successful turns. It occurred during explicit
cleanup, after result, status, and model evidence had been captured; no provider
or authentication error occurred during the turns.

## Adoptable expressions and limits

The lossless, shape-preserving expression is a root object with the canonical
properties followed by a chained root conditional:

1. If `outcome` is `PASS`, constrain `scope_findings`, the three remediation
   arrays, and human reasons to `maxItems: 0`.
2. Else if `outcome` is `TRIM_REQUIRED`, constrain `scope_findings` and each
   remediation array to `minItems: 1`, and human reasons to `maxItems: 0`.
3. Else apply the `NEEDS_HUMAN` constraints: human reasons have `minItems: 1`,
   while `scope_findings` and all remediation arrays have `maxItems: 0`.

The schema root remains `type: object`; the conditional is expressed with
`if`/`then`/`else`, without a root composition keyword. The offline fixture
requires `outcome` and enumerates the three values so the final `else` branch
cannot match a missing or unknown outcome.

The live-qualified alternative is a root object with a required `result`
property whose object schema contains a nested `anyOf` with three outcome
branches. Each branch requires its outcome constant and all result fields. A
Workflow projection can unwrap `result` without data loss, but it must account
for that wrapper explicitly.

The fixture names `human_reasons` and `remediation_one` through
`remediation_three` stand in for Workflow's `human_required_reasons` and its
three canonical remediation arrays. Apply the conditional and branch
constraints to those canonical fields when Workflow builds its schema.

The evidence qualifies only the exact two schema digests above, on Claude Code
`2.1.293`, Claude ACP `0.81.0`, Haiku 5.5, high effort, and the first-party
OAuth/read-only path. Offline-only results do not qualify nested `oneOf`,
nested `allOf`, nested conditional alternatives, or other keywords. Do not
infer support for untested schema features, other Claude model identities,
other runtime versions, Codex, or writable sessions. The SDK remains
ScopeJudge-neutral and passes the caller schema unchanged. Workflow remains
responsible for canonical field mapping, evidence binding, result parsing, and
rejecting a turn that has no completed structured result.

## Downstream handoff reconciliation

The existing [Workflow #414 comment](https://github.com/geeen-jp/ai-dev-workflow-go/issues/414#issuecomment-6182678917)
records NOT_SUPPORTED for execution `exec-46d41b351e2a54928701e81d9bb6b99e`
and different schema digests. That comment describes an earlier Candidate and
does not qualify the corrected digests in this record. The current disposition
is PASS only for `root_if_then` and `nested_any_of` above; the other candidates
remain offline-only. The authoritative qualification record is available in
[Candidate commit `f6533f47d8a218e5d7fe91a87602d102679682da`](https://github.com/geeen-jp/software-agent-sdk/commit/f6533f47d8a218e5d7fe91a87602d102679682da).
The downstream issue and its comments remain unchanged in this Builder run
under the read-only issue boundary.

## Verification

- `uv run pytest tests/sdk/settings/test_claude_scopejudge_schema_qualification.py tests/sdk/settings/test_acp_providers.py -q` — 154 passed.
- Corrected-schema live qualification — 12 fresh turns passed across the two live candidates; each accepted three valid outcomes and rejected the three corrected forbidden combinations. No invalid structured result completed.
- Live identity — SDK source SHA `73df482ff2ea340ecd676ab2a3c851026228f7b7`, SDK `1.44.1`, Claude ACP `0.81.0`, Claude Code `2.1.293`, Haiku `claude-haiku-5-5`, high effort, read-only, first-party OAuth.
- `uv run pre-commit run --files tests/sdk/settings/claude_scopejudge_schema_qualification.md` — passed; no configured hooks apply to Markdown.
- `make validate` — not rerun during this remediation round.
