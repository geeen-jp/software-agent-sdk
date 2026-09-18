"""Tests for the managed runtime readiness contract."""

import importlib
import json

from fastapi.testclient import TestClient

from openhands.agent_server.api import create_app
from openhands.agent_server.config import Config


runtime_router_module = importlib.import_module("openhands.agent_server.runtime_router")


class _EmptySecrets:
    def get_secret(self, name: str) -> str | None:
        del name
        return None


def _write_provider_sources(tmp_path):
    claude_root = tmp_path / "claude"
    codex_root = tmp_path / "codex"
    claude_root.mkdir()
    codex_root.mkdir()
    (claude_root / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "refreshToken": "refresh-value",
                    "accessToken": "access-value",
                }
            }
        ),
        encoding="utf-8",
    )
    (codex_root / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {"refresh_token": "codex-refresh"},
            }
        ),
        encoding="utf-8",
    )
    return claude_root, codex_root


def test_runtime_readiness_requires_the_session_api_key():
    client = TestClient(create_app(Config(session_api_keys=["runtime-key"])))

    response = client.get("/api/runtime/readiness")

    assert response.status_code == 401


def test_runtime_readiness_reports_missing_sources_without_secret_values(
    monkeypatch,
):
    monkeypatch.setattr(
        runtime_router_module,
        "get_secrets_store",
        lambda config: _EmptySecrets(),
    )
    client = TestClient(create_app(Config(session_api_keys=["runtime-key"])))

    response = client.get(
        "/api/runtime/readiness",
        headers={"X-Session-API-Key": "runtime-key"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "not_ready"
    assert payload["providers"] == {
        "claude-oauth": False,
        "codex-chatgpt": False,
    }
    assert "refresh-value" not in response.text
    assert "codex-refresh" not in response.text


def test_runtime_readiness_uses_explicit_provider_source_roots(
    tmp_path,
    monkeypatch,
):
    claude_root, codex_root = _write_provider_sources(tmp_path)
    monkeypatch.setenv("OPENHANDS_CLAUDE_CREDENTIALS_SOURCE", str(claude_root))
    monkeypatch.setenv("OPENHANDS_CODEX_AUTH_SOURCE", str(codex_root))
    monkeypatch.setattr(
        runtime_router_module,
        "get_secrets_store",
        lambda config: _EmptySecrets(),
    )
    client = TestClient(create_app(Config(session_api_keys=["runtime-key"])))

    response = client.get(
        "/api/runtime/readiness",
        headers={"X-Session-API-Key": "runtime-key"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["agent_server_auth_mode"] == "session-api-key"
    assert payload["providers"] == {
        "claude-oauth": True,
        "codex-chatgpt": True,
    }
