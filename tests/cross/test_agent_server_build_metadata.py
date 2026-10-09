from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
SERVER_WORKFLOW = WORKFLOWS_DIR / "server.yml"
RELEASE_BINARIES_WORKFLOW = WORKFLOWS_DIR / "release-binaries.yml"
AGENT_SERVER_SPEC = (
    REPO_ROOT
    / "openhands-agent-server"
    / "openhands"
    / "agent_server"
    / "agent-server.spec"
)


def test_server_workflow_passes_git_metadata_build_args() -> None:
    """The published agent-server images should embed git metadata."""
    workflow_text = SERVER_WORKFLOW.read_text(encoding="utf-8")

    assert "OPENHANDS_BUILD_GIT_SHA=${{ env.SDK_SHA }}" in workflow_text
    assert "OPENHANDS_BUILD_GIT_REF=${{ env.SDK_REF }}" in workflow_text


def test_server_workflow_contains_install_acp_providers_expression() -> None:
    """Regression guard for the exact wording of the INSTALL_ACP_PROVIDERS env
    line. This only proves the known-good string is present, not that it
    evaluates correctly in GitHub Actions — see
    test_and_or_shape_preserves_falsy_last_operand for the semantic proof.
    """
    workflow_text = SERVER_WORKFLOW.read_text(encoding="utf-8")

    assert (
        "INSTALL_ACP_PROVIDERS: ${{ github.event_name != 'workflow_dispatch' "
        "&& 'claude-code,codex,gemini-cli' || inputs.install_acp_providers }}"
    ) in workflow_text


def test_server_workflow_contains_install_capabilities_expression() -> None:
    """Regression guard for the exact wording of the INSTALL_CAPABILITIES env
    line. This only proves the known-good string is present, not that it
    evaluates correctly in GitHub Actions — see
    test_and_or_shape_preserves_falsy_last_operand for the semantic proof
    (the shape is identical to INSTALL_ACP_PROVIDERS, just a different
    default/input pair).
    """
    workflow_text = SERVER_WORKFLOW.read_text(encoding="utf-8")

    assert (
        "INSTALL_CAPABILITIES: ${{ github.event_name != 'workflow_dispatch' "
        "&& 'vscode,browser,docker' || inputs.install_capabilities }}"
    ) in workflow_text


def test_and_or_shape_preserves_falsy_last_operand() -> None:
    """GitHub Actions' `&&`/`||` share Python's `and`/`or` short-circuit
    value-return semantics (return an operand, not a coerced bool), so this
    exercises the exact `A && B || C` shape the workflow expression uses.

    A prior version put the maybe-empty dispatch value in B's position:
    `event_name == 'workflow_dispatch' && inputs.value || default`. That
    collapses to `default` whenever `inputs.value` is falsy (e.g. an
    intentional ""), because the trailing `|| default` fires again. Putting
    the maybe-empty value last, gated by the negated condition, avoids the
    second collapse since there is nothing after it to fall through to.
    """

    def resolve(is_dispatch: bool, dispatch_value: str) -> str:
        default = "claude-code,codex,gemini-cli"
        return (not is_dispatch and default) or dispatch_value

    assert (
        resolve(is_dispatch=False, dispatch_value="") == "claude-code,codex,gemini-cli"
    )
    assert resolve(is_dispatch=True, dispatch_value="") == ""
    assert resolve(is_dispatch=True, dispatch_value="codex") == "codex"
    assert (
        resolve(is_dispatch=True, dispatch_value="claude-code,codex,gemini-cli")
        == "claude-code,codex,gemini-cli"
    )


def test_agent_server_binary_copies_openhands_distribution_metadata() -> None:
    """The frozen binary should preserve OpenHands package metadata."""
    spec_text = AGENT_SERVER_SPEC.read_text(encoding="utf-8")

    for distribution in (
        "openhands-agent-server",
        "openhands-sdk",
        "openhands-tools",
        "openhands-workspace",
    ):
        assert f'*copy_metadata("{distribution}")' in spec_text


def _load_jobs(path: Path) -> dict[str, dict[str, Any]]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]


def _needs(job: dict[str, Any]) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def _uses_docker(job: dict[str, Any]) -> bool:
    return any(
        str(step.get("uses", "")).startswith("docker/") for step in job.get("steps", [])
    )


def _runs_only_on_dispatch(jobs: dict[str, dict[str, Any]], name: str) -> bool:
    job = jobs[name]
    if "workflow_dispatch" in str(job.get("if", "")):
        return True
    return any(_runs_only_on_dispatch(jobs, need) for need in _needs(job))


def test_server_workflow_docker_jobs_run_only_on_workflow_dispatch() -> None:
    jobs = _load_jobs(SERVER_WORKFLOW)

    docker_jobs = {name for name, job in jobs.items() if _uses_docker(job)}
    assert docker_jobs == {
        "build-and-push-image",
        "docker-smoke-test",
        "publish-images",
    }
    for name in docker_jobs:
        assert _runs_only_on_dispatch(jobs, name), name
    assert "pull_request" not in str(jobs["build-and-push-image"]["if"])
    assert jobs["build-and-push-image"]["permissions"]["packages"] == "write"
    assert jobs["docker-smoke-test"]["permissions"]["packages"] == "read"


def test_server_workflow_code_checks_stay_on_push_and_pull_request() -> None:
    workflow = yaml.safe_load(SERVER_WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]

    assert {"push", "pull_request", "workflow_dispatch"} <= set(workflow[True])
    for name in ("build-binary-and-test", "check-openapi-schema"):
        assert "if" not in jobs[name]
        assert not _uses_docker(jobs[name])
    assert workflow["permissions"].get("packages") == "read"


def test_server_workflow_removed_image_publication_jobs() -> None:
    jobs = _load_jobs(SERVER_WORKFLOW)

    for removed in (
        "merge-manifests",
        "consolidate-build-info",
        "update-pr-description",
    ):
        assert removed not in jobs
    assert _needs(jobs["docker-smoke-test"]) == ["build-and-push-image"]
    assert set(_needs(jobs["publish-images"])) == {
        "build-and-push-image",
        "docker-smoke-test",
    }


def test_workflow_needs_reference_existing_jobs() -> None:
    for path in (SERVER_WORKFLOW, RELEASE_BINARIES_WORKFLOW):
        jobs = _load_jobs(path)
        for name, job in jobs.items():
            for need in _needs(job):
                assert need in jobs, f"{path.name}:{name} needs missing job {need}"


def test_release_binaries_workflow_has_no_docker_or_polling() -> None:
    jobs = _load_jobs(RELEASE_BINARIES_WORKFLOW)

    assert set(jobs) == {
        "resolve-tag",
        "build-binary",
        "build-openapi",
        "publish-binaries",
    }
    assert not any(_uses_docker(job) for job in jobs.values())
    text = RELEASE_BINARIES_WORKFLOW.read_text(encoding="utf-8")
    assert "DEADLINE" not in text
    assert "imagetools" not in text


def test_server_workflow_verification_image_is_run_scoped_and_digest_pinned() -> None:
    workflow_text = SERVER_WORKFLOW.read_text(encoding="utf-8")

    assert (
        "VERIFY_TAG: verify-${{ github.run_id }}-${{ github.run_attempt }}-"
        "${{ matrix.variant }}"
    ) in workflow_text
    assert "tags: ${{ env.IMAGE }}:${{ env.VERIFY_TAG }}" in workflow_text
    assert "docker pull --platform" in workflow_text
    assert '"${IMAGE}@${DIGEST}"' in workflow_text


def test_server_workflow_is_ubuntu_amd64_only() -> None:
    jobs = _load_jobs(SERVER_WORKFLOW)

    for job in jobs.values():
        runs_on = str(job["runs-on"]).lower()
        assert "windows" not in runs_on and "macos" not in runs_on
        for entry in job.get("strategy", {}).get("matrix", {}).get("include", []):
            assert entry.get("arch", "amd64") == "amd64"
            assert entry.get("platform", "linux/amd64") == "linux/amd64"


def test_server_workflow_default_dispatch_keeps_each_variant_base_image() -> None:
    workflow = yaml.safe_load(SERVER_WORKFLOW.read_text(encoding="utf-8"))
    dispatch_inputs = workflow[True]["workflow_dispatch"]["inputs"]
    assert dispatch_inputs["base_image"]["default"] == ""

    job = workflow["jobs"]["build-and-push-image"]
    legs = job["strategy"]["matrix"]["include"]
    bases = {leg["variant"]: leg["base_image"] for leg in legs}
    assert len(set(bases.values())) == len(bases) == 3
    assert "matrix.base_image" in job["env"]["BASE_IMAGE"]
