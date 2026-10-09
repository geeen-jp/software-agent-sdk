# Release Automation Workflows

This document describes the automated release workflows for the OpenHands Software Agent SDK.

## Overview

The release process has been automated with three GitHub Actions workflows:

1. **prepare-release.yml** - Prepares a release PR with version updates
2. **pypi-release.yml** - Automatically publishes packages to PyPI when a release is created
3. **release-binaries.yml** - Builds and smoke-tests the linux x86_64 agent-server binary
   on releases and main pushes; release runs also attach binaries to the release
   (no Docker; see Step 4c)

## How to Create a New Release

### Step 1: Trigger the Prepare Release Workflow

1. Go to the [Actions tab](https://github.com/OpenHands/software-agent-sdk/actions)
2. Select **"Prepare Release"** workflow from the left sidebar
3. Click **"Run workflow"** button
4. Enter the version number (e.g., `1.2.3`) - must be in format `X.Y.Z`
5. Click **"Run workflow"**

The workflow will automatically:
- ✅ Create a new branch named `rel-X.Y.Z`
- ✅ Update all package versions using `make set-package-version`
- ✅ Commit the changes
- ✅ Push the branch
- ✅ Create a PR with labels `integration-tests` and `test-examples`

### Step 2: Review the PR

The created PR will include a checklist. Complete the following:

- [ ] Fix any deprecation deadlines if they exist
- [ ] Verify integration tests pass (triggered by `integration-tests` label)
- [ ] Verify example checks pass (triggered by `test-examples` label)
- [ ] Confirm any merged `release-note-required` PRs are accurately called out in the final release notes
- [ ] Review and approve the PR

### Step 3: Create the GitHub Release

1. Go to [Releases](https://github.com/OpenHands/software-agent-sdk/releases/new)
2. Click **"Draft a new release"**
3. Configure the release:
   - **Tag**: `vX.Y.Z` (must match the version)
   - **Branch**: `rel-X.Y.Z` (the branch created by the workflow)
   - **Previous tag**: Select the previous release version
4. Click **"Generate release notes"** to auto-generate the changelog
5. Review and edit the release notes as needed
6. Click **"Publish release"**

### Step 4: PyPI Publication (Automated)

Once the release is published, the **pypi-release.yml** workflow will automatically:
- ✅ Build all packages (openhands-sdk, openhands-tools, openhands-workspace, openhands-agent-server)
- ✅ Publish them to PyPI

You can monitor the progress in the [Actions tab](https://github.com/OpenHands/software-agent-sdk/actions/workflows/pypi-release.yml).

### Step 4b: Release Binaries (Automated)

In parallel with the PyPI workflow, **release-binaries.yml** also fires on `release: published`.
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
- Not changed: `version-bump-prs.yml` `bump-typescript-client` still waits up to
  45 minutes for `ghcr.io/openhands/agent-server:<X.Y.Z>-python`; it runs only
  in `OpenHands/software-agent-sdk` (never in this fork), so it is out of scope.
  A release there needs a `server.yml` dispatch with `publish=true` first.

#### Build time / runner expectations

| Stage | Runtime (typical) | Runners |
|---|---|---|
| Binary build (single linux x86_64 leg) | ~10–15 min | `ubuntu-24.04` |
| `publish-binaries` (download + checksum + upload) | ~1–2 min | `ubuntu-24.04` |

#### QEMU / buildx requirements

No QEMU is needed: every job runs on an `ubuntu-24.04` (x86_64) runner and runs
`linux/amd64` images natively. The Docker jobs in `server.yml` set up Docker
Buildx for `docker/build-push-action` and `docker buildx imagetools`.

### Step 5: Version Bump PRs (Automated)

After successful PyPI publication, the workflow will automatically create PRs to update SDK versions in downstream repositories:

- **[OpenHands-CLI](https://github.com/OpenHands/openhands-cli)** - Updates `openhands-sdk` and `openhands-tools` versions
- **[automation](https://github.com/OpenHands/automation)** - Updates `openhands-sdk` and `openhands-workspace` versions. Opened with a `fix:` title so the repo's release-please cuts a patch release, publishing an `openhands-automation` build pinned to this SDK (which the agent-canvas `sdk-version-sync` check requires).
- **TypeScript client (`clients/typescript`)** - Opens a PR in this repository after both the exact GHCR image and release `openapi.json` are available, updates `config.agentServerImage`, regenerates the checked-in transport types, and includes an API-change summary.

These PRs will:
- Be created automatically with branch name `bump-sdk-X.Y.Z` (`bump-agent-server-X.Y.Z` for typescript-client)
- Include links back to the SDK release
- Include generated Agent Server contract changes for the exact released
  version rather than only changing the image tag
- Need to be reviewed and merged by maintainers

### Step 6: Post-Release Tasks

- [ ] Merge the release PR to main
- [ ] Review and merge the auto-created version bump PRs in OpenHands-CLI, automation, and the TypeScript client (merging the automation PR triggers its release-please release PR; merge that too to publish the pinned `openhands-automation`)
- [ ] Announce the release

## Manual PyPI Release (If Needed)

If you need to manually trigger the PyPI release workflow:

1. Go to the [Actions tab](https://github.com/OpenHands/software-agent-sdk/actions)
2. Select **"Publish all OpenHands packages (uv)"** workflow
3. Click **"Run workflow"**
4. Select the branch/tag you want to publish from
5. Click **"Run workflow"**

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

- `.github/workflows/prepare-release.yml` - Automated release preparation
- `.github/workflows/pypi-release.yml` - PyPI package publication
- `.github/workflows/release-binaries.yml` - Linux x86_64 binary and OpenAPI
  publishing on releases and main pushes
- `.github/workflows/server.yml` - binary/OpenAPI checks on push and PR; on
  demand (`workflow_dispatch`) Docker build, smoke test and optional publish

## Troubleshooting

### Version Format Error

If you get a version format error, ensure you're using the format `X.Y.Z` (e.g., `1.2.3`), not `vX.Y.Z`.

### PR Creation Failed

If the PR creation fails, check:
- The branch doesn't already exist
- You have proper permissions
- The `GITHUB_TOKEN` has sufficient permissions

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

## Previous Manual Process

For reference, the previous manual release checklist was:

- [ ] Checkout SDK repo, use `make set-package-version version=x.x.x` to set the version
- [ ] Push to a branch like `rel-x.x.x` and start a PR
- [ ] Fix any "deprecation deadlines" if they exist
- [ ] Tag "integration-tests" and make sure integration test all pass
- [ ] Tag "test-examples" and make sure example checks all pass
- [ ] Draft a new release
- [ ] Use workflow to publish to PyPI on tag `v1.X.X`

Most of these steps are now automated!
