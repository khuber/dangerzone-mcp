"""Validated custom tool definitions with optional JSON persistence."""

import ast
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

from dangerzone_mcp.persistence import JsonStore

PROTECTED_NAMES = frozenset({"add_tool", "edit_tool", "remove_tool"})


class ToolDefinition(BaseModel):
    """Python source and the public schema for a custom tool."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,128}$")
    description: str = Field(min_length=1, max_length=10000)
    input_schema: dict[str, Any]
    source: str = Field(min_length=1, max_length=100000)


class RemoveToolInput(BaseModel):
    """The exact name of a custom tool to remove."""

    model_config = ConfigDict(extra="forbid")
    name: str


class ToolCatalog(BaseModel):
    """Versioned on-disk format, containing custom tools only."""

    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    tools: list[ToolDefinition]


class ToolRegistry:
    """Keep custom definitions separate from the permanent built-in tools."""

    def __init__(self, storage_path: Path | None = None) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._store = JsonStore(storage_path) if storage_path is not None else None
        # Validate an existing catalog up front; a missing one is created on first write.
        if self._store is not None and self._store.path.exists():
            with self._store.locked():
                self._load()

    def _load(self) -> dict[str, ToolDefinition]:
        assert self._store is not None
        try:
            data = self._store.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            catalog = ToolCatalog.model_validate_json(data)
            tools: dict[str, ToolDefinition] = {}
            for tool in catalog.tools:
                self._validate(tool)
                if tool.name in tools:
                    raise ValueError(f"Duplicate tool: {tool.name}")
                tools[tool.name] = tool
            return tools
        except ValueError as exc:
            raise ValueError(f"Invalid tool catalog {self._store.path}: {exc}") from exc

    def _save(self, tools: dict[str, ToolDefinition]) -> None:
        assert self._store is not None
        catalog = ToolCatalog(tools=[tools[name] for name in sorted(tools)])
        self._store.write(catalog.model_dump_json(indent=2) + "\n")

    @contextmanager
    def _access(self, *, write: bool = False) -> Iterator[dict[str, ToolDefinition]]:
        if self._store is None:
            tools = self._tools.copy()
            yield tools
            if write:
                self._tools = tools
        else:
            with self._store.locked():
                tools = self._load()
                yield tools
                if write:
                    self._save(tools)

    def list_tools(self) -> list[ToolDefinition]:
        """Return a sorted snapshot, without exposing mutable registry state."""
        with self._access() as tools:
            return [tools[name].model_copy(deep=True) for name in sorted(tools)]

    def get(self, name: str) -> ToolDefinition:
        """Return one custom definition or reject an unknown tool."""
        with self._access() as tools:
            if name not in tools:
                raise KeyError(name)
            return tools[name].model_copy(deep=True)

    def add(self, request: ToolDefinition) -> None:
        """Validate then atomically add a new custom definition."""
        self._put(request, replace=False)

    def edit(self, request: ToolDefinition) -> None:
        """Validate then atomically replace an existing custom definition."""
        self._put(request, replace=True)

    def remove(self, name: str) -> None:
        """Remove a custom tool, rejecting permanent and unknown names."""
        self._check_name(name)
        with self._access(write=True) as tools:
            if name not in tools:
                raise ValueError(f"Unknown tool: {name}")
            del tools[name]

    def _put(self, request: ToolDefinition, *, replace: bool) -> None:
        self._validate(request)
        with self._access(write=True) as tools:
            if replace and request.name not in tools:
                raise ValueError(f"Unknown tool: {request.name}")
            if not replace and request.name in tools:
                raise ValueError(f"Tool already exists: {request.name}; use edit_tool")
            tools[request.name] = request.model_copy(deep=True)

    def _validate(self, request: ToolDefinition) -> None:
        """Reject any definition the server could not list or run, raising ValueError."""
        self._check_name(request.name)
        if request.input_schema.get("type") != "object":
            raise ValueError('input_schema must have type "object"')
        dialect = request.input_schema.get("$schema")
        if dialect not in (None, "https://json-schema.org/draft/2020-12/schema"):
            raise ValueError("input_schema must use JSON Schema 2020-12")
        try:
            Draft202012Validator.check_schema(request.input_schema)
            self._validate_references(request.input_schema)
            tree = ast.parse(request.source, filename=f"<tool:{request.name}>")
            compile(tree, f"<tool:{request.name}>", "exec")
        except SchemaError as exc:
            raise ValueError(f"Invalid input_schema: {exc.message}") from exc
        except SyntaxError as exc:
            raise ValueError(f"Invalid source: {exc}") from exc
        except (RecursionError, MemoryError) as exc:
            raise ValueError("Tool definition is too complex to validate") from exc
        entries = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == "main"
        ]
        if len(entries) != 1:
            raise ValueError("source must define one top-level main(arguments) function")
        args = entries[0].args
        if (
            len(args.posonlyargs + args.args) != 1
            or args.vararg
            or args.kwarg
            or args.kwonlyargs
            or entries[0].decorator_list
        ):
            raise ValueError("main must accept one argument and have no decorators")

    def _validate_references(self, schema: dict[str, Any]) -> None:
        resource = Resource.from_contents(schema, default_specification=DRAFT202012)
        resolver = (
            Registry()
            .with_resource("urn:dangerzone:tool", resource)
            .resolver("urn:dangerzone:tool")
        )
        pending = [(resource, resolver.in_subresource(resource), False)]
        active: set[int] = set()
        visited: set[int] = set()
        while pending:
            current, scoped, leaving = pending.pop()
            identity = id(current.contents)
            if leaving:
                active.remove(identity)
                visited.add(identity)
                continue
            if identity in active:
                raise ValueError("Cyclic schema references are not supported")
            if identity in visited:
                continue
            active.add(identity)
            pending.append((current, scoped, True))
            if isinstance(current.contents, dict):
                for keyword in ("$ref", "$dynamicRef"):
                    if keyword not in current.contents:
                        continue
                    reference = current.contents[keyword]
                    if not reference.startswith("#"):
                        raise ValueError(f"Only local schema references are supported: {reference}")
                    try:
                        resolved = scoped.lookup(reference)
                    except Unresolvable as exc:
                        raise ValueError(f"Unresolvable schema reference: {reference}") from exc
                    Draft202012Validator.check_schema(resolved.contents)
                    target = DRAFT202012.create_resource(resolved.contents)
                    pending.append((target, resolved.resolver, False))
            pending.extend(
                (child, scoped.in_subresource(child), False) for child in current.subresources()
            )

    def _check_name(self, name: str) -> None:
        if name.casefold() in PROTECTED_NAMES:
            raise ValueError(f"Protected built-in tool cannot be modified: {name}")
