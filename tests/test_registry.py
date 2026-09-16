from typing import Any
from unittest.mock import patch

import pytest

from dangerzone_mcp.registry import ToolDefinition, ToolRegistry
from tests.helpers import BASE, COMPLEX_SOURCE, definition


@pytest.mark.parametrize(
    "patch",
    [
        {"name": "../escape"},
        {"name": "ADD_TOOL"},
        {"description": ""},
        {"input_schema": {"type": "array"}},
        {"input_schema": {"type": "object", "properties": 1}},
        {"input_schema": {"type": "object", "$schema": "invalid"}},
        {"source": "x = 1"},
        {"source": "def main(a, b): return 1"},
        {"source": "@decorator\ndef main(a): return 1"},
        {"source": "def main(a): return 1\ndef main(b): return 2"},
        {"source": "def main(a):\n    nonlocal x"},
    ],
)
def test_invalid_registration(patch: dict[str, Any]) -> None:
    registry = ToolRegistry()
    with pytest.raises(ValueError):
        registry.add(ToolDefinition.model_validate({**BASE, **patch}))
    assert registry.list_tools() == []


def test_registry_snapshots_do_not_allow_mutation() -> None:
    registry = ToolRegistry()
    tool = ToolDefinition.model_validate(BASE)
    registry.add(tool)
    tool.input_schema.clear()
    registry.get("greet").input_schema.clear()
    registry.list_tools()[0].input_schema.clear()
    assert registry.get("greet").input_schema["type"] == "object"


@pytest.mark.parametrize("reference", ["https://example.com/x", "other.json", "#/$defs/missing"])
def test_invalid_refs_rejected(reference: str) -> None:
    registry = ToolRegistry()
    tool = definition().model_copy(
        update={"input_schema": {"type": "object", "properties": {"x": {"$ref": reference}}}}
    )
    with pytest.raises(ValueError, match="reference"):
        registry.add(tool)
    assert not registry.list_tools()


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "#"},
        {"$dynamicAnchor": "root", "$dynamicRef": "#root"},
        {
            "$defs": {"a": {"$ref": "#/$defs/b"}, "b": {"$ref": "#/$defs/a"}},
            "$ref": "#/$defs/a",
        },
        {"properties": {"child": {"$ref": "#"}}},
    ],
)
def test_cyclic_refs_are_rejected(schema: dict[str, Any]) -> None:
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="Cyclic schema references"):
        registry.add(
            ToolDefinition.model_validate({**BASE, "input_schema": {"type": "object", **schema}})
        )
    assert not registry.list_tools()


@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef"])
@pytest.mark.parametrize("reference", ["https://example.com/schema", "#/$defs/missing"])
def test_refs_in_referenced_non_schema_locations_are_checked(keyword: str, reference: str) -> None:
    registry = ToolRegistry()
    schema = {
        "type": "object",
        "examples": [
            {"$ref": "#/examples/1"},
            {"$schema": "urn:unknown-dialect", keyword: reference},
        ],
        "properties": {"x": {"$ref": "#/examples/0"}},
    }
    with pytest.raises(ValueError, match="reference"):
        registry.add(ToolDefinition.model_validate({**BASE, "input_schema": schema}))
    assert not registry.list_tools()


def test_shared_targets_anchors_and_scoped_refs_are_allowed() -> None:
    registry = ToolRegistry()
    schema = {
        "type": "object",
        "$defs": {
            "integer": {"$anchor": "number", "type": "integer"},
            "nested": {
                "$id": "urn:nested",
                "type": "object",
                "$defs": {"text": {"type": "string"}},
                "properties": {"text": {"$ref": "#/$defs/text"}},
            },
        },
        "properties": {
            "a": {"$ref": "#/$defs/integer"},
            "b": {"$ref": "#number"},
            "c": {"$ref": "#/$defs/nested"},
            "d": {"$ref": "#/examples/0"},
        },
        "examples": [{"type": "boolean"}, {"$ref": "data is not a schema"}],
    }
    tool = ToolDefinition.model_validate({**BASE, "input_schema": schema})
    registry.add(tool)
    assert registry.get("greet") == tool


def test_large_expression_is_rejected_without_changing_registry() -> None:
    registry = ToolRegistry()
    tool = ToolDefinition.model_validate(BASE)
    registry.add(tool)
    with pytest.raises(ValueError, match="too complex"):
        registry.edit(tool.model_copy(update={"source": COMPLEX_SOURCE}))
    assert registry.get("greet") == tool


@pytest.mark.parametrize("function", ["ast.parse", "compile"])
@pytest.mark.parametrize("error", [RecursionError, MemoryError])
def test_parser_resource_errors_are_validation_errors(
    function: str, error: type[Exception]
) -> None:
    registry = ToolRegistry()
    with (
        patch(f"dangerzone_mcp.registry.{function}", side_effect=error),
        pytest.raises(ValueError, match="too complex"),
    ):
        registry.add(ToolDefinition.model_validate(BASE))
    assert not registry.list_tools()
