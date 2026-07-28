"""Directory entries for downstream eDocs MCP providers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderEndpoint:
    """Public identity and routing information for one provider."""

    provider_id: str
    display_name: str
    description: str
    mcp_url: str

    def __post_init__(self) -> None:
        if (
            not self.provider_id
            or not self.provider_id.isascii()
            or not all(
                character.isalnum() or character in "_-"
                for character in self.provider_id
            )
        ):
            raise ValueError(
                "provider_id must contain only ASCII letters, digits, '_' or '-'"
            )
        for name in ("display_name", "mcp_url"):
            if not getattr(self, name):
                raise ValueError(f"{name} is required")

    def public_dict(self) -> dict[str, str]:
        return {
            "provider_id": self.provider_id,
            "display_name": self.display_name,
            "description": self.description,
        }
