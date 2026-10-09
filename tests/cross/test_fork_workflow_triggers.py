from pathlib import Path
from typing import Any

import pytest
import yaml


WORKFLOWS_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows"

REMOVED_UPSTREAM_WORKFLOWS = [
    "deploy-docs.yml",
    "cancel-eval.yml",
    "prepare-release.yml",
    "version-bump-prs.yml",
    "stale.yml",
    "issue-duplicate-checker.yml",
    "remove-duplicate-candidate-label.yml",
    "todo-management.yml",
    "create-release.yml",
    "pr-artifacts.yml",
    "issue-readiness-check.yml",
]

PUBLISH_WORKFLOW_CONFIRMATIONS = {
    "pypi-release.yml": "publish-pypi",
    "typescript-client-npm-publish.yml": "publish-npm",
    "typescript-client-github-packages-publish.yml": "publish-github-packages",
}

# release-binaries.yml attaches linux binaries to a GitHub release; it is not a
# registry publish workflow and is intentionally left on `release`.
RELEASE_TRIGGER_ALLOWED = {"release-binaries.yml"}
UPSTREAM_ONLY_EVENTS = {"release", "schedule", "issues", "issue_comment"}
LABEL_GATED_PULL_REQUEST_TARGET = {"run-eval.yml", "integration-runner.yml"}


def _load(path: Path) -> dict[Any, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _on(document: dict[Any, Any]) -> Any:
    # PyYAML parses the bare `on:` key as boolean True.
    return document[True] if True in document else document["on"]


def _triggers(path: Path) -> set[str]:
    on = _on(_load(path))
    return {on} if isinstance(on, str) else set(on)


@pytest.mark.parametrize("name", REMOVED_UPSTREAM_WORKFLOWS)
def test_upstream_only_workflows_are_removed(name: str) -> None:
    assert not (WORKFLOWS_DIR / name).exists()


def test_no_workflow_listens_to_release_issue_or_schedule_events() -> None:
    offenders = {
        path.name: sorted(_triggers(path) & UPSTREAM_ONLY_EVENTS)
        for path in WORKFLOWS_DIR.glob("*.yml")
        if path.name not in RELEASE_TRIGGER_ALLOWED
        and _triggers(path) & UPSTREAM_ONLY_EVENTS
    }
    assert offenders == {}


def test_pull_request_target_only_in_label_gated_workflows() -> None:
    offenders = [
        path.name
        for path in WORKFLOWS_DIR.glob("*.yml")
        if "pull_request_target" in _triggers(path)
        and path.name not in LABEL_GATED_PULL_REQUEST_TARGET
    ]
    assert offenders == []


@pytest.mark.parametrize("name,confirmation", PUBLISH_WORKFLOW_CONFIRMATIONS.items())
def test_publish_workflows_are_dispatch_only_with_confirmation(
    name: str, confirmation: str
) -> None:
    path = WORKFLOWS_DIR / name
    assert _triggers(path) == {"workflow_dispatch"}

    document = _load(path)
    on = _on(document)
    assert on["workflow_dispatch"]["inputs"]["confirm"]["required"] is True
    conditions = [job.get("if") for job in document["jobs"].values()]
    assert conditions == [f"inputs.confirm == '{confirmation}'"]
