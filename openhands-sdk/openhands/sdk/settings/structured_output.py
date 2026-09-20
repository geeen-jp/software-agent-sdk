"""Provider-neutral structured-output settings."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


StructuredOutputMode = Literal["json_schema"]
"""Structured-output modes supported by the SDK boundary."""


def _validate_nested_object_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ValueError(
                    "structured_output.schema nested object keys must be strings"
                )
            _validate_nested_object_keys(nested)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _validate_nested_object_keys(item)


class StructuredOutputConfig(BaseModel):
    """Bounded structured-output configuration for an ACP agent.

    The caller owns the schema and its semantic interpretation.  The SDK only
    validates that it is a JSON-serializable object and transports it through a
    qualified provider adapter.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        serialize_by_alias=True,
    )

    mode: StructuredOutputMode
    json_schema: dict[str, Any] = Field(
        alias="schema",
        description="Caller-supplied JSON Schema object.",
    )

    @property
    def schema(self) -> dict[str, Any]:  # pyright: ignore[reportIncompatibleMethodOverride]
        """Caller-supplied JSON Schema, exposed under the API name."""
        return self.json_schema

    @field_validator("json_schema", mode="before")
    @classmethod
    def _validate_schema_object(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError("structured_output.schema must be a JSON object")

        # The caller owns the schema's semantics. Keep the SDK's validated
        # copy independent from both the caller's nested mappings and any
        # provider projection/copy performed later in the lifecycle.
        schema = copy.deepcopy(dict(value))
        if any(not isinstance(key, str) for key in schema):
            raise ValueError("structured_output.schema keys must be strings")
        try:
            json.dumps(schema, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "structured_output.schema must be JSON-serializable"
            ) from exc
        _validate_nested_object_keys(schema)
        return schema
