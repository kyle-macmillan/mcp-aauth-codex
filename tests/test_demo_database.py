import json
from pathlib import Path

from mcp_aauth_codex.demo_database import (
    DEMO_EDOC_ID,
    DEMO_RESOURCE_URI,
    load_demo_catalog,
    setup_demo_database,
)
from mcp_aauth_codex.functions import (
    LocalFunctionLoader,
    local_query_table_registration,
    query_table,
)


def test_setup_creates_opaque_resource_owned_duckdb(tmp_path):
    catalog = setup_demo_database(tmp_path)
    entry = catalog[DEMO_EDOC_ID]

    assert entry.resource_uri == DEMO_RESOURCE_URI
    assert entry.edoc_id not in entry.original_filename
    assert Path(entry.database_path).is_file()
    assert load_demo_catalog(tmp_path) == catalog
    manifest = json.loads((tmp_path / "catalog.json").read_text())
    assert manifest[DEMO_EDOC_ID]["original_filename"] == "employees.csv"


def test_setup_resets_resource_database(tmp_path):
    first = setup_demo_database(tmp_path)[DEMO_EDOC_ID]
    Path(first.database_path).write_bytes(b"not a database")

    second = setup_demo_database(tmp_path)[DEMO_EDOC_ID]

    result = query_table(
        second,
        {"statement": "SELECT count(*) AS count FROM document", "parameters": []},
    )
    assert result["rows"] == [{"count": 4}]


def test_query_table_executes_parameters_against_selected_document(tmp_path):
    entry = setup_demo_database(tmp_path)[DEMO_EDOC_ID]

    result = query_table(
        entry,
        {
            "statement": (
                "SELECT name, department FROM document "
                "WHERE department = ? ORDER BY name"
            ),
            "parameters": ["engineering"],
        },
    )

    assert result == {
        "columns": ["name", "department"],
        "rows": [
            {"name": "Avery", "department": "engineering"},
            {"name": "Casey", "department": "engineering"},
        ],
        "truncated": False,
    }


def test_local_loader_requires_matching_immutable_descriptor():
    registration = local_query_table_registration()
    loader = LocalFunctionLoader({registration.descriptor.id: registration})

    assert loader.load(registration.descriptor) is registration.implementation
