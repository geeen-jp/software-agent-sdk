# Release and CI Workflows (geeen-jp fork)

This document describes which GitHub Actions workflows run in the independent
geeen-jp fork of the OpenHands Software Agent SDK, which are stopped, and how a
person starts the manual ones. sdk#84 removed or disabled every workflow that
operates the upstream `OpenHands/software-agent-sdk` project (Cloud automation,
upstream docs, upstream downstream-release bumps, registry publishing).

## Fork workflow inventory (sdk#84)

"Auto" means a `push`, `pull_request`, `issues`, `issue_comment`, `schedule` or
`release` event starts the workflow. No workflow in the "removed" or "manual"
groups starts automatically.

### Removed (upstream-only; the file is deleted)

| Workflow | Former trigger | Why removed |
|---|---|---|
| `deploy-docs.yml` | push to main (agent-server paths), dispatch | deploys upstream docs |
| `cancel-eval.yml` | dispatch | cancels upstream OpenHands/evaluation runs |
| `prepare-release.yml` | dispatch | opens `rel-X.Y.Z` PRs for the upstream release train |
| `version-bump-prs.yml` | dispatch (from `pypi-release.yml`) | opens version-bump PRs in OpenHands-CLI, automation and this repository |
| `create-release.yml` | merged `rel-*` PR (`contents: write`, `actions: write`) | creates the upstream GitHub release and dispatches publish workflows |
| `stale.yml` | daily cron | closes upstream stale issues/PRs |
| `issue-duplicate-checker.yml` | `issues: opened`, daily cron, dispatch | OpenHands Cloud duplicate check, auto-closes issues |
| `remove-duplicate-candidate-label.yml` | `issue_comment` | upstream label hygiene |
| `todo-management.yml` | dispatch, PR label `automatic-todo` | OpenHands Cloud TODO automation |
| `issue-readiness-check.yml` | `issues` events (`issues: write`, `actions: write`) | auto-labels `ready-for-dev`; no label policy is established for this fork, so unknown automatic issue writes fail closed |
| `pr-artifacts.yml` | `pull_request_target`, `pull_request_review` (`pull-requests: write`, `contents: write`) | auto-commits removal of `.pr/` and comments on PRs; the `.pr/` directory is now removed by hand before merge |

### Manual only (dispatch with a confirmation input)

| Workflow | How to run | Guard |
|---|---|---|
| `pypi-release.yml` | Actions > "Publish all OpenHands packages (uv)" > Run workflow, set `confirm` to `publish-pypi` | job `if: inputs.confirm == 'publish-pypi'`; fails when secret `PYPI_TOKEN_OPENHANDS` is missing; no token fallback; `contents: read` only |
| `typescript-client-npm-publish.yml` | Run workflow with `version` and `confirm` = `publish-npm` | job `if: inputs.confirm == 'publish-npm'`; npm trusted publishing (OIDC) only |
| `typescript-client-github-packages-publish.yml` | Run workflow with `version` and `confirm` = `publish-github-packages` | job `if: inputs.confirm == 'publish-github-packages'`; uses `GITHUB_TOKEN` |
| `run-eval.yml` | Run workflow with `reason` and `eval_limit`; or add `run-eval-1/50/200/500` to a same-repository PR | same-repository PRs only; no `release` trigger; needs secret `OPENHANDS_BOT_GITHUB_PAT_EVAL_DISPATCH` (fails when missing); it dispatches `OpenHands/evaluation`, so only run it when that is intended; permissions `contents: read`, `pull-requests: write` |
| `integration-runner.yml` | Run workflow, or add `integration-test` / `behavior-test` to a same-repository PR | fork PRs never run; `id-token` removed |
| `run-examples.yml` | Run workflow, or add `test-examples` to a PR (unchanged) | label or dispatch only |
| `security-scan.yml` | Run workflow, or add `security-scan` to a PR, or any label on a `rel-*` PR (unchanged) | plain `pull_request` (read-only token on forks) |

### Kept (automatic, local rules)

| Workflow | Trigger | Permissions |
|---|---|---|
| `validate.yml` (job `validate`) | `pull_request` | read |
| `tests.yml`, `precommit.yml`, `check-docstrings.yml`, `deprecation-check.yml`, `check-documented-examples.yml`, `check-duplicate-examples.yml` | push / pull_request | read |
| `api-breakage.yml`, `agent-server-rest-api-breakage.yml`, `persisted-settings-compat.yml` | push / pull_request | unchanged |
| `typescript-client-ci.yml`, `typescript-client-integration-tests.yml`, `typescript-client-endpoint-audit.yml` | push / pull_request | unchanged |
| `server.yml` | binary and OpenAPI jobs on push/PR; Docker jobs on dispatch only (sdk#83) | unchanged |
| `release-binaries.yml` | push to main, `release: published`, dispatch | unchanged (linux binaries and `openapi.json`, not a registry publish) |
| `version-bump-guard.yml` | `pull_request` to main | `contents: read` |
| `review-thread-gate.yml` | `pull_request` to main | `contents: read`, `pull-requests: read` |
| `pr-description-check.yml` | `pull_request` | `contents: read`, `pull-requests: read` (was `pull_request_target`; `issues: read` and the `GITHUB_TOKEN` env were dropped) |

`oh-update-documentation.yml.back` is not a workflow (GitHub ignores the
`.back` suffix) and is left as is.

### Acceptance decisions (sdk#84)

- Removed upstream-only workflows leave no `push`, `pull_request`, `issues`,
  `issue_comment`, `schedule` or `release` trigger behind; `release-binaries.yml`
  keeps its `release` trigger because it only builds and attaches linux binaries
  and is covered by sdk#83. `tests/cross/test_fork_workflow_triggers.py` fails if
  a removed workflow file returns or if any other workflow gains a `release`,
  `schedule`, `issues` or `issue_comment` trigger.
- Publishing is not adopted by default. The three publish workflows keep their
  steps (a future publish is not forbidden) but only start from
  `workflow_dispatch`, and the job is skipped unless the `confirm` input equals
  the exact word listed above. There is no fallback publish path when a secret
  is missing, and fork PRs have no way to start them.
- `pypi-release.yml` no longer dispatches `version-bump-prs.yml` after
  publishing, and its `actions: write` permission was removed.
- The `pr-description-check.yml` linked-issue `ready-for-dev` label lookup is
  not enforced: the workflow passes no `GITHUB_TOKEN`, so
  `check_pr_description.py` still requires a linked issue reference and the PR
  template sections but skips the label lookup. This is because
  `issue-readiness-check.yml` was removed and nothing applies the label.
  `.github/scripts/check_issue_readiness.py` and `post-readiness-comment.mjs`
  are left in place unused (non-goal: no source changes).
- `pr-artifacts.yml` was removed instead of reduced to a read-only check: its
  only valuable part was the automatic `.pr/` cleanup, which needs write
  access. Delete `.pr/` yourself before merging (see AGENTS.md "PR_ARTIFACTS").
- `run-eval.yml`, `integration-runner.yml`, `run-examples.yml` and
  `security-scan.yml` stay as the evaluation path. They start only from
  dispatch, a label on a same-repository PR, or (security-scan) a `rel-*` PR;
  an unlabeled PR starts none of them. Unchanged and still correct:
  `run-examples.yml` and `security-scan.yml`.
- Out of scope and kept as is: `validate`, Python/REST/OpenAPI compatibility
  checks, and the TypeScript CI / integration / endpoint-audit workflows.

### Required checks 移行 (sdk#84)

Main branch protection is unconfirmed (403 with the available token, rulesets
API returns `[]`). The operator must check Settings > Branches in the GitHub UI
and remove any of the following that is required; nothing was changed in
repository settings.

| Check / job | Workflow | Change |
|---|---|---|
| `dispatch` | `deploy-docs.yml` | deleted |
| `cancel-eval` | `cancel-eval.yml` | deleted |
| `prepare-release` | `prepare-release.yml` | deleted |
| `create-version-bump-prs`, `bump-typescript-client` | `version-bump-prs.yml` | deleted |
| `create-release` | `create-release.yml` | deleted |
| `stale` | `stale.yml` | deleted |
| `smoke-clone`, `issue-duplicate-check`, `auto-close-duplicates` | `issue-duplicate-checker.yml` | deleted |
| `remove-duplicate-candidate` | `remove-duplicate-candidate-label.yml` | deleted |
| `scan-todos`, `process-todos`, `summary` | `todo-management.yml` | deleted |
| `check`, `Refresh PR gates for linked issues` | `issue-readiness-check.yml` | deleted |
| `cleanup-on-approval`, `check-pr-artifacts`, `cleanup-after-merge` | `pr-artifacts.yml` | deleted |
| `publish` | `pypi-release.yml`, `typescript-client-*-publish.yml` | skipped unless dispatched with `confirm` |
| `print-parameters`, `build-and-evaluate` | `run-eval.yml` | skipped on fork PRs and on release |
| `Validate PR description` | `pr-description-check.yml` | kept, trigger is now `pull_request` |

Unchanged and required to stay: `validate`, `build-binary-and-test (ubuntu-latest)`,
`check-openapi-schema`, `Check package versions`, the review-thread gate and the
TypeScript client checks.

### Re-syncing with upstream and restoring a workflow

When merging upstream `OpenHands/software-agent-sdk`, expect modify/delete
conflicts for every removed file and content conflicts in the manual workflows.
Do not take the upstream side blindly: upstream re-adds `release`, `schedule`
and `issues` triggers, the `OpenHands/software-agent-sdk` repository gate and
the write permissions removed here. After a merge run
`pytest tests/cross/test_fork_workflow_triggers.py`; it names any workflow that
came back.

To restore a deleted workflow deliberately, take it from the last commit that
had it (`git show c62f7a493:.github/workflows/<name>.yml > .github/workflows/<name>.yml`),
replace the `OpenHands/software-agent-sdk` repository gate with this
repository, remove triggers and write permissions you do not need, update
`tests/cross/test_fork_workflow_triggers.py` and this document, then confirm
the required checks in the GitHub UI.

## Release binaries and Agent Server images

`release-binaries.yml` and `server.yml` are kept (sdk#82, sdk#83); the sections
below describe them.

### Step 4b: Release Binaries (Automated)

**release-binaries.yml** fires on `release: published` (a release created by hand).
It also runs on every push to `main` as ongoing smoke coverage. It:

- ✅ Builds the agent-server PyInstaller binary on `ubuntu-24.04` (linux x86_64
  only; macOS, Windows and arm64 builds were removed) and smoke-tests it
- ✅ Exports and validates the deterministic public Agent Server contract as
  `openapi.json`, with `info.version` matching the release version
- ✅ Generates a combined `SHA256SUMS` and attaches the binaries and
  `openapi.json` to the GitHub release on release/manual runs

It does **not** build, push, pull or smoke-test Docker images. On `push` events
the binaries plus `openapi.json` remain as workflow artifacts only. On
release/manual runs they are uploaded to the GitHub release.

### Step 4c: Agent Server Docker images (on demand)

Docker images are never built by `push`, tag, `pull_request` or `release`
events (sdk#83). Run **Agent Server** (`server.yml`) via "Run workflow" on the
branch or tag you want. One run chains, with `needs` and artifacts and no wait
on any other workflow:

1. `Build & Push (<variant>-amd64)` builds `linux/amd64` for `python`, `java`
   and `golang` and pushes only `ghcr.io/<owner>/agent-server:verify-<run_id>-<run_attempt>-<variant>`.
   The job fails if no image digest is produced.
2. `Docker smoke (<variant>-amd64)` downloads the digest artifact, pulls
   `<image>@<digest>`, checks that the verify tag still resolves to that digest
   and that the image is `linux/amd64`, starts it and asserts `/health`
   (about 2 minutes). A missing image fails at the first `docker pull`.
3. `Publish GHCR tags (<variant>)` runs only if the dispatch sets `publish=true`
   and all three smoke jobs passed. It re-tags the verified digest with
   `docker buildx imagetools create` as `<sha7>-<variant>`, `<long-sha>-<variant>`,
   `<branch>-<variant>`, `<sha7>-<base-slug>`, `<X.Y.Z>-<variant>` (when run on a
   tag ref such as `v1.2.3`) and `latest-<variant>` (when run on `main`). This
   single-arch manifest step is kept because existing consumers pull the
   arch-less tags.

**Verification image vs published image.** The `verify-...` image is only
evidence for that run: unique per run and attempt, never overwritten, and not a
supported tag for consumers. The published image is the same digest under the
tags in step 3, and exists only after an explicit `publish=true` dispatch.

**Retention.** GHCR package versions are not deleted automatically; remove
`verify-...` versions in the GHCR package UI when they are no longer needed. The
`image-ref-<variant>` workflow artifact (digest and tag list) is kept for 1 day.

**Impact on existing tag users.** Tags already in GHCR (`main-*`, `latest-*`,
`<sha7>-*`, `<X.Y.Z>-*`, `*-amd64`) are not removed and keep pointing at the
images they point at now. They no longer move automatically: `latest-<variant>`
and `<branch>-<variant>` change only when someone dispatches with
`publish=true`, `*-amd64` tags are no longer pushed, and PR descriptions no
longer get an "Agent Server images for this PR" section. To test a PR build,
dispatch `server.yml` on the PR branch and use the `verify-...` digest from the
run summary or logs. Pinning consumers (for example
`clients/typescript/package.json` `config.agentServerImage`) must use a tag
that was published with `publish=true`.

**Acceptance decisions (sdk#83).**
- Ordinary PRs and main pushes build and push 0 Docker images; fork PRs never
  write to the registry because every registry-writing job is
  `workflow_dispatch`-only and holds `packages: write` only at job level.
- GHCR is the only registry. Only `linux/amd64` runs; no arm64, QEMU, Windows
  or macOS job exists. The `python`, `java` and `golang` variants are kept.
- The 45-minute manifest poll is removed; the smoke test sits in the same run as
  the build. No retry loop longer than the 2 minute `/health` wait remains in
  the Docker path.
- Concurrent dispatches cannot collide: the verification tag contains the run
  id and attempt, and the smoke test uses the digest. Published tags are
  mutable aliases by design and are serialized per ref and variant.
- Credentials: only `secrets.GITHUB_TOKEN` through `docker/login-action`; it is
  never echoed. Auth failures, tag conflicts, pull failures, an unreachable
  `/health` and cancellations show as failed or cancelled jobs.
- `version-bump-prs.yml` (which waited for `ghcr.io/openhands/agent-server:<X.Y.Z>-python`)
  was removed in sdk#84.

#### Build time / runner expectations

| Stage | Runtime (typical) | Runners |
|---|---|---|
| Binary build (single linux x86_64 leg) | ~10–15 min | `ubuntu-24.04` |
| `publish-binaries` (download + checksum + upload) | ~1–2 min | `ubuntu-24.04` |

#### QEMU / buildx requirements

No QEMU is needed: every job runs on an `ubuntu-24.04` (x86_64) runner and runs
`linux/amd64` images natively. The Docker jobs in `server.yml` set up Docker
Buildx for `docker/build-push-action` and `docker buildx imagetools`.

## Manual PyPI Release (If Needed)

Publishing is not adopted by default (sdk#84). To publish deliberately:

1. Go to the Actions tab of this repository.
2. Select **"Publish all OpenHands packages (uv)"** and click **"Run workflow"**.
3. Select the branch/tag to publish from and type `publish-pypi` in `confirm`.
4. Click **"Run workflow"**. Any other `confirm` value skips the job.

The secret `PYPI_TOKEN_OPENHANDS` must exist; the job fails when it is missing.
The npm and GitHub Packages workflows work the same way with `publish-npm` and
`publish-github-packages` plus a `version` input.

## CI scope and required checks

CI targets Ubuntu linux/amd64 only (sdk#82). Removed checks: `windows-tests` (`tests.yml`); `build-binary-and-test (macos-latest)` and `(windows-latest)`, `Build & Push (<variant>-arm64)` (`server.yml`); `Build (linux-arm64)`, `Build (macos-x86_64)`, `Build (macos-arm64)`, `Build (windows-x86_64)`, `Docker (<variant>-arm64)` (`release-binaries.yml`). Remaining check names are unchanged. Main branch protection could not be read with the available token (403; rulesets API returns `[]`), so an operator must confirm in the GitHub UI that no removed check is a required status check and drop it if so; this was not changed from the repository.

### Required checks 移行

sdk#83 stops Docker work on ordinary PRs and pushes. Main branch protection is
unconfirmed (403 with the available token, rulesets API returns `[]`): the
operator must check the GitHub UI and remove any of the following that is
required, and must not require an image smoke check on PRs that do not run
Docker. Nothing was changed in repository settings.

| Check / job | Workflow | Change |
|---|---|---|
| `Build & Push (<variant>-amd64)` | `server.yml` | kept, now `workflow_dispatch` only (skipped on push/PR) |
| `Merge Multi-Arch Manifests` | `server.yml` | deleted (single-arch; tags applied by `Publish GHCR tags`) |
| `Consolidate Build Information` | `server.yml` | deleted (build-info aggregation) |
| `Update PR description with agent server image` | `server.yml` | deleted (PR image blurb) |
| `Docker (<variant>-amd64)` | `release-binaries.yml` | deleted (45 min poll) |
| `Docker smoke (<variant>-amd64)` | `server.yml` | new, `workflow_dispatch` only |
| `Publish GHCR tags (<variant>)` | `server.yml` | new, `workflow_dispatch` with `publish=true` only |
| `Dispatch Agent Server image build` step | `create-release.yml` | deleted (no implicit release image build) |

Unchanged and still run on ordinary PRs: `validate`, `build-binary-and-test (ubuntu-latest)`,
`check-openapi-schema` (`Check OpenAPI Schema`), the TypeScript client and API
checks. In `release-binaries.yml`, `resolve-tag`, `build-binary`,
`build-openapi` and `publish-binaries` are unchanged.

## Workflow Files

- `.github/workflows/pypi-release.yml` - PyPI package publication (manual, confirmation required)
- `.github/workflows/release-binaries.yml` - Linux x86_64 binary and OpenAPI
  publishing on releases and main pushes
- `.github/workflows/server.yml` - binary/OpenAPI checks on push and PR; on
  demand (`workflow_dispatch`) Docker build, smoke test and optional publish

## Troubleshooting

### PyPI Publication Failed

If PyPI publication fails:
- Check that the `PYPI_TOKEN_OPENHANDS` secret is properly configured
- Verify the version doesn't already exist on PyPI
- Check the workflow logs for specific error messages

### Release Binaries Failed

If `release-binaries.yml` fails:
- **Binary build failure**: re-run the failed matrix job; PyInstaller flakes are
  rare but possible. If it persists, the issue is likely in `agent-server.spec`.
- Release/manual runs can be re-run against an existing tag via
  `workflow_dispatch` with the `release_tag` input (e.g. `v1.20.1`);
  `gh release upload --clobber` makes this safe.

### Agent Server Docker Failed

If a `server.yml` dispatch fails:
- **Login or push failed** (`Build & Push`): check the dispatching actor's
  `packages: write` permission; the job fails at once.
- **`Docker smoke` pull failed**: the digest in the `image-ref-<variant>`
  artifact is not in GHCR; the job fails at the first `docker pull`, so re-run
  the dispatch.
- **`/health` never responded**: open the failing job; the cleanup trap dumps
  the last 100 lines of `docker logs` for the container.
