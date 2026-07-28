#!/usr/bin/env python3
"""Reset and seed the resource-owned DuckDB demo database."""

from __future__ import annotations

import argparse
from pathlib import Path

from mcp_aauth_codex.demo_database import setup_demo_database


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path(".demo-state"))
    args = parser.parse_args()
    for provider_id in ("alice", "bob", "carol"):
        catalog = setup_demo_database(
            args.state_dir.resolve() / provider_id,
            provider_id=provider_id,
        )
        for entry in catalog.values():
            print(f"{entry.resource_uri} -> {entry.database_path}")


if __name__ == "__main__":
    main()
