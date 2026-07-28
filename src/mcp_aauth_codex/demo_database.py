"""Ephemeral DuckDB catalog owned by the demo eDocs resource."""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

import duckdb

DEMO_EDOC_ID = "doc_01JDEMO7F3A"
DEMO_RESOURCE_URI = f"edoc://demo/{DEMO_EDOC_ID}"


@dataclass(frozen=True)
class CatalogEntry:
    """Resource-private location and public metadata for one eDoc."""

    edoc_id: str
    resource_uri: str
    title: str
    description: str
    media_type: str
    original_filename: str
    database_path: str


def setup_demo_database(state_dir: Path) -> dict[str, CatalogEntry]:
    """Reset and seed the resource's DuckDB-backed demo catalog."""
    resources_dir = state_dir / "resources"
    if resources_dir.exists():
        shutil.rmtree(resources_dir)
    resources_dir.mkdir(parents=True, mode=0o700)

    database_path = resources_dir / f"{DEMO_EDOC_ID}.duckdb"
    connection = duckdb.connect(str(database_path))
    try:
        connection.execute(
            """
            CREATE TABLE document (
                name VARCHAR NOT NULL,
                department VARCHAR NOT NULL,
                salary INTEGER NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO document VALUES (?, ?, ?)",
            [
                ("Avery", "engineering", 125000),
                ("Blair", "finance", 110000),
                ("Casey", "engineering", 118000),
                ("Devon", "legal", 132000),
            ],
        )
    finally:
        connection.close()

    entry = CatalogEntry(
        edoc_id=DEMO_EDOC_ID,
        resource_uri=DEMO_RESOURCE_URI,
        title="Employee directory",
        description="Representative tabular data for the eDocs authorization demo",
        media_type="text/csv",
        original_filename="employees.csv",
        database_path=str(database_path),
    )
    catalog = {entry.edoc_id: entry}
    (state_dir / "catalog.json").write_text(
        json.dumps(
            {edoc_id: asdict(value) for edoc_id, value in catalog.items()},
            indent=2,
            sort_keys=True,
        )
    )
    return catalog


def load_demo_catalog(state_dir: Path) -> dict[str, CatalogEntry]:
    """Load catalog metadata written by :func:`setup_demo_database`."""
    values = json.loads((state_dir / "catalog.json").read_text())
    return {edoc_id: CatalogEntry(**value) for edoc_id, value in values.items()}
