"""Nonblocking wrappers around the AAuth requests transport."""

from __future__ import annotations

import asyncio

import requests
from aauth_edocs.agent import RequestsTransport


class AsyncRequestsTransport(RequestsTransport):
    """Expose the coordinator's optional async transport interface."""

    async def get_async(self, url: str):
        return await asyncio.to_thread(self.get, url)

    async def request_async(self, method: str, url: str, **kwargs):
        return await asyncio.to_thread(self.request, method, url, **kwargs)


def person_transport(person: str | None, ps_url: str) -> AsyncRequestsTransport:
    """Create the person's PS session.

    ``person`` uses the prototype PS login endpoint and is intentionally
    demo-only. A deployed host should inject a session authenticated by its
    normal person-facing mechanism.
    """
    transport = AsyncRequestsTransport(requests.Session())
    if person:
        response = transport.request(
            "POST",
            f"{ps_url.rstrip('/')}/login",
            json={"person": person},
        )
        if response.status_code != 200:
            raise RuntimeError("Person Server demo login failed")
    return transport
