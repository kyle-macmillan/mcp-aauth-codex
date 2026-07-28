"""Function deployment and execution interfaces for the demo resource."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import duckdb
from aauth_edocs import FunctionDescriptor

from .demo_database import CatalogEntry

MAX_RESULT_ROWS = 100


class FunctionImplementation(Protocol):
    """Executable function loaded into a resource deployment."""

    def __call__(
        self,
        document: CatalogEntry,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]: ...


class FunctionLoader(Protocol):
    """Resolve an immutable descriptor to executable resource code."""

    def load(self, descriptor: FunctionDescriptor) -> FunctionImplementation: ...


@dataclass(frozen=True)
class LoadedFunction:
    """A descriptor paired with the implementation deployed at a resource."""

    descriptor: FunctionDescriptor
    implementation: FunctionImplementation


class LocalFunctionLoader:
    """Demo loader for locally packaged implementations.

    A production loader may retrieve the artifact from any trusted source, as
    long as it verifies and returns the implementation named by the descriptor.
    """

    def __init__(self, registrations: Mapping[str, LoadedFunction]) -> None:
        self.registrations = dict(registrations)

    def load(self, descriptor: FunctionDescriptor) -> FunctionImplementation:
        registration = self.registrations.get(descriptor.id)
        if registration is None or registration.descriptor.digest != descriptor.digest:
            raise LookupError(f"function implementation unavailable: {descriptor.id}")
        return registration.implementation


def query_table(
    document: CatalogEntry,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """Execute resource-supplied SQL against the selected eDoc's DuckDB."""
    statement = arguments.get("statement")
    parameters = arguments.get("parameters")
    if not isinstance(statement, str) or not statement:
        raise ValueError("query_table@1 requires a non-empty statement")
    if not isinstance(parameters, list):
        raise ValueError("query_table@1 parameters must be a JSON array")

    connection = duckdb.connect(
        document.database_path,
        read_only=True,
        config={"enable_external_access": "false"},
    )
    try:
        cursor = connection.execute(statement, parameters)
        description = cursor.description
        if description is None:
            return {"columns": [], "rows": [], "truncated": False}
        columns = [item[0] for item in description]
        rows = cursor.fetchmany(MAX_RESULT_ROWS + 1)
    finally:
        connection.close()

    truncated = len(rows) > MAX_RESULT_ROWS
    return {
        "columns": columns,
        "rows": [
            {column: value for column, value in zip(columns, row, strict=True)}
            for row in rows[:MAX_RESULT_ROWS]
        ],
        "truncated": truncated,
    }


def local_query_table_registration() -> LoadedFunction:
    """Return the descriptor and local implementation used by the demo."""
    artifact = Path(__file__).read_bytes()
    digest = f"sha256:{hashlib.sha256(artifact).hexdigest()}"
    descriptor = FunctionDescriptor(
        id="query_table@1",
        description="Execute a parameterized DuckDB query against one eDoc",
        implementation_uri=Path(__file__).resolve().as_uri(),
        digest=digest,
        input_schema={
            "type": "object",
            "properties": {
                "statement": {"type": "string"},
                "parameters": {"type": "array"},
            },
            "required": ["statement", "parameters"],
            "additionalProperties": False,
        },
    )
    return LoadedFunction(descriptor, query_table)
