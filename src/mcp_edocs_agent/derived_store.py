"""Shared on-disk store for materialized derived eDoc payloads."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from aauth_edocs import DerivedEdoc, serialize_dataflow


class DerivedPayloadStore:
    """Persist derived eDoc metadata and output bodies under ``state_dir``."""

    def __init__(self, state_dir: Path) -> None:
        self.root = state_dir / "derived"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def path_for(self, edoc_id: str) -> Path:
        if (
            not edoc_id
            or "/" in edoc_id
            or edoc_id in {".", ".."}
            or not edoc_id.isascii()
        ):
            raise ValueError("invalid derived eDoc id")
        return self.root / f"{edoc_id}.json"

    def write(self, derived: DerivedEdoc, output: dict[str, Any]) -> Path:
        path = self.path_for(derived.edoc_id)
        payload = {
            "edoc_id": derived.edoc_id,
            "resource_uri": derived.resource_uri,
            "producer": serialize_dataflow(derived.producer),
            "producer_fingerprint": derived.producer_fingerprint,
            "output_digest": derived.output_digest,
            "custodian": derived.custodian,
            "controllers": list(derived.controllers),
            "output": output,
            "published": False,
        }
        path.write_text(json.dumps(payload, sort_keys=True, indent=2))
        os.chmod(path, 0o600)
        return path

    def read(self, edoc_id: str) -> dict[str, Any]:
        path = self.path_for(edoc_id)
        try:
            value = json.loads(path.read_text())
        except FileNotFoundError as error:
            raise LookupError(f"unknown derived eDoc: {edoc_id}") from error
        except (json.JSONDecodeError, OSError) as error:
            raise RuntimeError(f"derived eDoc store is unreadable: {edoc_id}") from error
        if not isinstance(value, dict):
            raise RuntimeError(f"derived eDoc store is invalid: {edoc_id}")
        return value

    def mark_published(self, edoc_id: str) -> dict[str, Any]:
        value = self.read(edoc_id)
        value["published"] = True
        path = self.path_for(edoc_id)
        path.write_text(json.dumps(value, sort_keys=True, indent=2))
        os.chmod(path, 0o600)
        return value
