"""eDocs agent bridge process entry point."""

import anyio

from .config import AgentRuntimeConfig
from .mcp_adapter import build_server


def main() -> None:
    server = build_server(AgentRuntimeConfig.from_env())
    anyio.run(server.run_stdio_async)


if __name__ == "__main__":
    main()
