"""Plugin process entry point."""

import anyio

from .config import ProxyConfig
from .proxy import build_server


def main() -> None:
    server = build_server(ProxyConfig.from_env())
    anyio.run(server.run_stdio_async)


if __name__ == "__main__":
    main()
