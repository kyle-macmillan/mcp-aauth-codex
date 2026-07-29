"""Function deployment and execution interfaces for the demo resource."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import duckdb
from aauth_edocs import FunctionDescriptor
from mcp_edocs_provider import LoadedFunction, LocalFunctionLoader

from .demo_database import CatalogEntry

MAX_RESULT_ROWS = 100
IDENTITY_FUNCTION_ID = "identity@1"
DEMO_FUNCTION_SQL = {
    "department_counts@1": (
        "SELECT department, count(*) AS employee_count "
        "FROM document GROUP BY department ORDER BY department"
    ),
    "average_salary_by_department@1": (
        "SELECT department, round(avg(salary), 2) AS average_salary "
        "FROM document GROUP BY department ORDER BY department"
    ),
    "employee_count@1": "SELECT count(*) AS employee_count FROM document",
}
DEMO_FUNCTION_DESCRIPTIONS = {
    "department_counts@1": "Count employees in each department",
    "average_salary_by_department@1": (
        "Calculate average salary for each department"
    ),
    "employee_count@1": "Count all employees in the document",
}


def query_table(
    document: CatalogEntry,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """Execute resource-supplied SQL against the selected eDoc's DuckDB."""
    statement = arguments.get("statement")
    parameters = arguments.get("parameters")
    if not isinstance(statement, str) or not statement:
        raise ValueError("query_table@1 requires a non-empty statement")
    if not isinstance(parameters, (list, dict)):
        raise ValueError(
            "query_table@1 parameters must be a JSON array or object"
        )

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


@contextmanager
def temporary_document_from_output(output: dict[str, Any]) -> Iterator[Any]:
    """Yield a document object usable by identity and SQL demo functions."""
    columns = output.get("columns")
    rows = output.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        yield SimpleNamespace(storage=output)
        return
    if any(not isinstance(column, str) or not column for column in columns):
        raise ValueError("derived output columns must be non-empty strings")
    if len(set(columns)) != len(columns):
        raise ValueError("derived output columns must be unique")
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("derived output rows must be objects")
    with tempfile.TemporaryDirectory(prefix="edocs-transform-") as temp_dir:
        database_path = Path(temp_dir) / "document.duckdb"
        connection = duckdb.connect(str(database_path))
        try:
            if rows:
                json_path = Path(temp_dir) / "rows.json"
                ordered_rows = [
                    {column: row.get(column) for column in columns}
                    for row in rows
                ]
                json_path.write_text(
                    json.dumps(ordered_rows),
                    encoding="utf-8",
                )
                connection.execute(
                    "CREATE TABLE document AS SELECT * FROM read_json_auto(?)",
                    [str(json_path)],
                )
            else:
                column_sql = ", ".join(
                    f'"{column}" VARCHAR' for column in columns
                )
                connection.execute(f"CREATE TABLE document ({column_sql})")
        finally:
            connection.close()
        yield SimpleNamespace(
            storage=output,
            database_path=str(database_path),
            edoc_id="derived_transform",
        )


def execute_on_derived_output(
    registration: LoadedFunction,
    output: dict[str, Any],
    arguments: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one registered function against a derived eDoc JSON payload."""
    args = {} if arguments is None else dict(arguments)
    with temporary_document_from_output(output) as document:
        return registration.implementation(document, args)


def _identity_implementation(
    document: Any,
    _arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a published payload object, or a stub for non-payload storage."""
    storage = getattr(document, "storage", document)
    if isinstance(storage, dict):
        return dict(storage)
    return {"edoc_id": getattr(document, "edoc_id", str(document))}


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


def local_demo_function_registrations() -> dict[str, LoadedFunction]:
    """Return all executable SQL functions seeded in the demo providers."""
    registrations = {
        "query_table@1": local_query_table_registration(),
    }
    identity_source = b"return the governed eDoc payload unchanged"
    registrations[IDENTITY_FUNCTION_ID] = LoadedFunction(
        FunctionDescriptor(
            id=IDENTITY_FUNCTION_ID,
            description="Forward a governed derived eDoc without transforming it",
            implementation_uri="demo-identity://identity@1",
            digest=(
                f"sha256:{hashlib.sha256(identity_source).hexdigest()}"
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),
        _identity_implementation,
    )
    for function_id, statement in DEMO_FUNCTION_SQL.items():
        digest = f"sha256:{hashlib.sha256(statement.encode()).hexdigest()}"
        descriptor = FunctionDescriptor(
            id=function_id,
            description=DEMO_FUNCTION_DESCRIPTIONS[function_id],
            implementation_uri=f"demo-sql://{function_id}",
            digest=digest,
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        )

        def execute(
            document: CatalogEntry,
            _arguments: Mapping[str, Any],
            *,
            sql: str = statement,
        ) -> dict[str, Any]:
            return query_table(
                document,
                {"statement": sql, "parameters": []},
            )

        registrations[function_id] = LoadedFunction(descriptor, execute)
    return registrations


def sql_function_registration(
    *,
    function_id: str,
    description: str,
    sql: str,
    input_schema: dict[str, Any],
) -> LoadedFunction:
    """Validate and compile a demo SQL artifact into a loaded function."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+@[A-Za-z0-9_.-]+", function_id):
        raise ValueError("function_id must be a versioned identifier")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("description must be a non-empty string")
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("SQL must be a non-empty string")
    normalized = sql.strip()
    first_word = normalized.split(None, 1)[0].upper()
    if first_word not in {"SELECT", "WITH"}:
        raise ValueError("demo SQL functions must be read-only SELECT queries")
    if ";" in normalized.rstrip(";"):
        raise ValueError("demo SQL functions must contain one statement")
    if (
        not isinstance(input_schema, dict)
        or input_schema.get("type") != "object"
        or not isinstance(input_schema.get("properties", {}), dict)
    ):
        raise ValueError("input_schema must describe a JSON object")
    digest = f"sha256:{hashlib.sha256(normalized.encode()).hexdigest()}"
    descriptor = FunctionDescriptor(
        id=function_id,
        description=description.strip(),
        implementation_uri=f"demo-sql://{function_id}",
        digest=digest,
        input_schema=input_schema,
    )

    def execute(
        document: CatalogEntry,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        return query_table(
            document,
            {"statement": normalized, "parameters": dict(arguments)},
        )

    return LoadedFunction(descriptor, execute)
