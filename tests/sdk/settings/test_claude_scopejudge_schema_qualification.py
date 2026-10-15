"""Offline qualification fixtures for outcome-dependent Claude schemas.

These representative fields exercise the downstream ScopeJudge constraints;
they are not the Workflow's canonical schema. Bounded live qualification used
Claude Code 2.1.293, Claude ACP 0.81.0, ``claude-haiku-5-5`` at high effort,
and the first-party OAuth/read-only path. The durable qualification record
contains the run identity, observed enforcement, and limits. The candidate
schema digests below are pinned to prevent the evidence from drifting away
from the expressions that were evaluated live.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from openhands.sdk.settings.acp_providers import (
    ACPStructuredOutputCompatibilityError,
    validate_claude_structured_output_schema,
    validate_codex_structured_output_schema,
)


_OUTCOMES = ("PASS", "TRIM_REQUIRED", "NEEDS_HUMAN")
_REMEDIATION_FIELDS = (
    "remediation_one",
    "remediation_two",
    "remediation_three",
)
_FIELDS = ("outcome", "scope_findings", *_REMEDIATION_FIELDS, "human_reasons")


def _array_schema(*, minimum: int | None = None, maximum: int | None = None):
    schema: dict[str, Any] = {"type": "array", "items": {"type": "string"}}
    if minimum is not None:
        schema["minItems"] = minimum
    if maximum is not None:
        schema["maxItems"] = maximum
    return schema


def _base_properties() -> dict[str, Any]:
    return {
        "outcome": {"type": "string", "enum": list(_OUTCOMES)},
        "scope_findings": _array_schema(),
        **{name: _array_schema() for name in _REMEDIATION_FIELDS},
        "human_reasons": _array_schema(),
    }


def _condition(outcome: str, constraints: dict[str, Any]) -> dict[str, Any]:
    return {
        "if": {
            "properties": {"outcome": {"const": outcome}},
            "required": ["outcome"],
        },
        "then": {"properties": constraints},
    }


def _constraints_for(outcome: str) -> dict[str, Any]:
    if outcome == "PASS":
        return {
            "scope_findings": _array_schema(maximum=0),
            **{name: _array_schema(maximum=0) for name in _REMEDIATION_FIELDS},
            "human_reasons": _array_schema(maximum=0),
        }
    if outcome == "TRIM_REQUIRED":
        return {
            "scope_findings": _array_schema(minimum=1),
            **{name: _array_schema(minimum=1) for name in _REMEDIATION_FIELDS},
            "human_reasons": _array_schema(maximum=0),
        }
    return {
        "scope_findings": _array_schema(maximum=0),
        **{name: _array_schema(maximum=0) for name in _REMEDIATION_FIELDS},
        "human_reasons": _array_schema(minimum=1),
    }


def _base_object_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": _base_properties(),
        "required": list(_FIELDS),
        "additionalProperties": False,
    }


def _conditional_schema() -> dict[str, Any]:
    pass_if = _condition("PASS", _constraints_for("PASS"))
    trim_if = _condition("TRIM_REQUIRED", _constraints_for("TRIM_REQUIRED"))
    return {
        **_base_object_schema(),
        "if": pass_if["if"],
        "then": pass_if["then"],
        "else": {
            "if": trim_if["if"],
            "then": trim_if["then"],
            "else": {"properties": _constraints_for("NEEDS_HUMAN")},
        },
    }


def _branch_schema(outcome: str) -> dict[str, Any]:
    properties = _base_properties()
    properties["outcome"] = {"type": "string", "const": outcome}
    properties.update(_constraints_for(outcome))
    return {
        "type": "object",
        "properties": properties,
        "required": list(_FIELDS),
        "additionalProperties": False,
    }


def _candidate_schema(name: str) -> dict[str, Any]:
    if name == "root_if_then":
        return _conditional_schema()

    result_schema = _base_object_schema()
    if name in {"nested_any_of", "nested_one_of"}:
        keyword = "anyOf" if name == "nested_any_of" else "oneOf"
        result_schema[keyword] = [_branch_schema(outcome) for outcome in _OUTCOMES]
    elif name == "nested_if_then":
        nested = _conditional_schema()
        result_schema.update(
            {
                key: value
                for key, value in nested.items()
                if key in {"if", "then", "else"}
            }
        )
    elif name == "nested_all_of_conditionals":
        result_schema["allOf"] = [
            _condition(outcome, _constraints_for(outcome)) for outcome in _OUTCOMES
        ]
    else:
        raise AssertionError(f"unknown candidate: {name}")

    return {
        "type": "object",
        "properties": {"result": result_schema},
        "required": ["result"],
        "additionalProperties": False,
    }


_CANDIDATE_NAMES = (
    "root_if_then",
    "nested_any_of",
    "nested_one_of",
    "nested_if_then",
    "nested_all_of_conditionals",
)
_CANDIDATE_SCHEMAS = {name: _candidate_schema(name) for name in _CANDIDATE_NAMES}
_CANDIDATE_SCHEMA_SHA256 = {
    "root_if_then": "114fd8572b0ad69b683e1409be79c6b52507be404a8fce34d39f7e0bf8b61c8c",
    "nested_any_of": "68ce62139c6dc1b0fb155a534c4a451abd1d5fa73fcba115066849227239d01d",
    "nested_one_of": "4423cfe1e4493efb0d8b1ccdd223e3b314f4cde58350eb946762bedeb6621d15",
    "nested_if_then": (
        "d284d7e3b9e9c5bb236118a8fcc970bf486b361bbd41fbe884d5e7cee342c196"
    ),
    "nested_all_of_conditionals": (
        "49444cffba28cdc033d7b3a7fda52381b7ad2de20ea747ccb76edfe5d73de37b"
    ),
}
_QUALIFICATION_RECORD = Path(__file__).with_name(
    "claude_scopejudge_schema_qualification.md"
)

_VALID_RESULTS = {
    "PASS": {
        "outcome": "PASS",
        "scope_findings": [],
        "remediation_one": [],
        "remediation_two": [],
        "remediation_three": [],
        "human_reasons": [],
    },
    "TRIM_REQUIRED": {
        "outcome": "TRIM_REQUIRED",
        "scope_findings": ["Remove the unrelated deployment request."],
        "remediation_one": ["deployment"],
        "remediation_two": ["remove deployment request"],
        "remediation_three": ["The deployment request is unrelated."],
        "human_reasons": [],
    },
    "NEEDS_HUMAN": {
        "outcome": "NEEDS_HUMAN",
        "scope_findings": [],
        "remediation_one": [],
        "remediation_two": [],
        "remediation_three": [],
        "human_reasons": ["The approved scope is ambiguous."],
    },
}

_INVALID_RESULTS = {
    "PASS_with_remediation": {
        **_VALID_RESULTS["PASS"],
        "remediation_one": ["should be empty"],
    },
    "NEEDS_HUMAN_with_remediation": {
        **_VALID_RESULTS["NEEDS_HUMAN"],
        "remediation_three": ["should be empty"],
    },
    "TRIM_REQUIRED_without_scope_findings": {
        **_VALID_RESULTS["TRIM_REQUIRED"],
        "scope_findings": [],
    },
    "TRIM_REQUIRED_without_remediation": {
        **_VALID_RESULTS["TRIM_REQUIRED"],
        "remediation_two": [],
    },
    "PASS_with_scope_findings": {
        **_VALID_RESULTS["PASS"],
        "scope_findings": ["PASS must not report scope findings."],
    },
    "NEEDS_HUMAN_with_scope_findings": {
        **_VALID_RESULTS["NEEDS_HUMAN"],
        "scope_findings": ["NEEDS_HUMAN must not report scope findings."],
    },
    "TRIM_REQUIRED_with_human_reasons": {
        **_VALID_RESULTS["TRIM_REQUIRED"],
        "human_reasons": ["TRIM_REQUIRED must not report human reasons."],
    },
}


def _candidate_payload(name: str, result: dict[str, Any]) -> dict[str, Any]:
    return result if name == "root_if_then" else {"result": result}


@pytest.mark.parametrize("candidate", _CANDIDATE_NAMES)
def test_scopejudge_candidate_schema_digest_is_pinned(candidate: str) -> None:
    schema_json = json.dumps(
        _CANDIDATE_SCHEMAS[candidate], sort_keys=True, separators=(",", ":")
    ).encode("utf-8")

    assert (
        hashlib.sha256(schema_json).hexdigest() == _CANDIDATE_SCHEMA_SHA256[candidate]
    )


def test_live_qualification_record_keeps_identity_and_schema_digests() -> None:
    record = _QUALIFICATION_RECORD.read_text()

    for identity in (
        "claude-haiku-5-5",
        "high",
        "read_only",
        "Claude Code",
        "2.1.293",
        "Claude ACP",
        "0.81.0",
    ):
        assert identity in record
    for digest in _CANDIDATE_SCHEMA_SHA256.values():
        assert f"`{digest}`" in record


@pytest.mark.parametrize("candidate", _CANDIDATE_NAMES)
def test_scopejudge_candidate_schema_is_valid_draft_2020_12(candidate: str) -> None:
    Draft202012Validator.check_schema(_CANDIDATE_SCHEMAS[candidate])


@pytest.mark.parametrize("candidate", _CANDIDATE_NAMES)
@pytest.mark.parametrize("outcome", _OUTCOMES)
def test_scopejudge_candidate_accepts_representative_valid_outcomes(
    candidate: str, outcome: str
) -> None:
    validator = Draft202012Validator(_CANDIDATE_SCHEMAS[candidate])

    assert validator.is_valid(_candidate_payload(candidate, _VALID_RESULTS[outcome]))


@pytest.mark.parametrize("candidate", _CANDIDATE_NAMES)
@pytest.mark.parametrize("invalid_case", _INVALID_RESULTS)
def test_scopejudge_candidate_rejects_outcome_constraint_violations(
    candidate: str, invalid_case: str
) -> None:
    validator = Draft202012Validator(_CANDIDATE_SCHEMAS[candidate])

    assert not validator.is_valid(
        _candidate_payload(candidate, _INVALID_RESULTS[invalid_case])
    )


@pytest.mark.parametrize("candidate", _CANDIDATE_NAMES)
def test_scopejudge_candidate_keeps_claude_root_boundary(candidate: str) -> None:
    schema = _CANDIDATE_SCHEMAS[candidate]

    assert schema.get("type") == "object"
    assert not {"oneOf", "anyOf", "allOf"}.intersection(schema)
    validate_claude_structured_output_schema(schema)


def test_wrapper_candidates_map_losslessly_to_canonical_result_shape() -> None:
    canonical_result = deepcopy(_VALID_RESULTS["TRIM_REQUIRED"])
    wrapped = _candidate_payload("nested_any_of", canonical_result)

    assert wrapped["result"] == canonical_result
    assert wrapped["result"] == _VALID_RESULTS["TRIM_REQUIRED"]


def test_codex_validator_still_rejects_conditional_keywords() -> None:
    with pytest.raises(
        ACPStructuredOutputCompatibilityError,
        match="unqualified keyword",
    ):
        validate_codex_structured_output_schema(_CANDIDATE_SCHEMAS["root_if_then"])


def test_claude_root_combinators_still_fail_closed() -> None:
    for keyword in ("oneOf", "anyOf", "allOf"):
        with pytest.raises(ACPStructuredOutputCompatibilityError, match="top-level"):
            validate_claude_structured_output_schema(
                {"type": "object", keyword: [{"type": "object"}]}
            )
