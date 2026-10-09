# Release Automation Workflows

This document describes the automated release workflows for the OpenHands Software Agent SDK.

## Overview

The release process has been automated with three GitHub Actions workflows:

1. **prepare-release.yml** - Prepares a release PR with version updates
2. **pypi-release.yml** - Automatically publishes packages to PyPI when a release is created
3. **release-binaries.yml** - Builds and smoke-tests the linux x86_64 agent-server binary
   on releases and main pushes; release runs also attach binaries to the release

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

### Step 4b: Release Binaries + Docker Smoke Test (Automated)

In parallel with the PyPI workflow, **release-binaries.yml** also fires on `release: published`.
It also runs on every push to `main` as ongoing smoke coverage. It:

- ✅ Builds the agent-server PyInstaller binary on `ubuntu-24.04` (linux x86_64
  only; macOS, Windows and arm64 builds were removed) and smoke-tests it
- ✅ Exports and validates the deterministic public Agent Server contract as
  `openapi.json`, with `info.version` matching the release version
- ✅ Generates a combined `SHA256SUMS` and attaches the binaries and
  `openapi.json` to the GitHub release on release/manual runs
- ✅ Verifies that the Docker manifest
  `ghcr.io/openhands/agent-server:<image-tag>-<variant>` published by
  `server.yml` covers `linux/amd64` for every variant
  (`python`, `java`, `golang`)
- ✅ Pulls each variant with `--platform=linux/amd64`,
  boots the container, and asserts `/health` responds

On `push` events, `<image-tag>` is the 7-character commit SHA and binaries plus
`openapi.json` remain as workflow artifacts only. On release/manual runs,
`<image-tag>` is the release version and the binaries plus `openapi.json` are
uploaded to the GitHub release.

#### Build time / runner expectations

| Stage | Runtime (typical) | Runners |
|---|---|---|
| Binary build (single linux x86_64 leg) | ~10–15 min | `ubuntu-24.04` |
| `publish-binaries` (download + checksum + upload) | ~1–2 min | `ubuntu-24.04` |
| `docker-smoke-test` (3-way matrix, parallel) | Up to 45 min (mostly polling for the docker images) | `ubuntu-24.04` (amd64) |

#### QEMU / buildx requirements

The smoke test does **not** require QEMU: every job runs on an `ubuntu-24.04`
(x86_64) runner and pulls `--platform=linux/amd64`, so containers run natively. We do still set up Docker Buildx so we can call
`docker buildx imagetools inspect` on the manifest.

The wait window for the manifest is 45 min — long enough to absorb
the full `server.yml` matrix runtime (~25–30 min for `build-and-push-image` +
`merge-manifests`) when this workflow races the corresponding `server.yml` run
for a release tag or main-branch push.

If the matching manifest is already in GHCR, the wait step exits immediately.

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

## Workflow Files

- `.github/workflows/prepare-release.yml` - Automated release preparation
- `.github/workflows/pypi-release.yml` - PyPI package publication
- `.github/workflows/release-binaries.yml` - Linux x86_64 binary publishing and
  docker manifest smoke test on releases and main pushes

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
- **`docker-smoke-test` timed out waiting for the manifest**: `server.yml` did
  not publish images for the matching release tag or commit SHA.
  Check that workflow's corresponding run and re-trigger if needed.
- **`/health` never responded**: open the failing job; the cleanup trap dumps
  the last 100 lines of `docker logs` for the container.
- Release/manual runs can be re-run against an existing tag via
  `workflow_dispatch` with the `release_tag` input (e.g. `v1.20.1`);
  `gh release upload --clobber` makes this safe.

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
