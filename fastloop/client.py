import asyncio
from collections.abc import Callable
from typing import Any

import aiohttp


class LoopClient:
    def __init__(self):
        self.url: str | None = None
        self.name: str | None = None
        self.loop_id: str | None = None
        self._session: aiohttp.ClientSession | None = None

    def with_loop(
        self,
        *,
        url: str,
        name: str,
        event_callback: Callable,
        loop_id: str | None = None,
    ) -> "LoopClient":
        self.url = url
        self.name = name
        self.loop_id = loop_id
        self.event_callback = event_callback
        return self

    async def __aenter__(self):
        self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._session:
            await self._session.close()

    async def send(self, type: str, data: dict[str, Any]):
        """Send an event to the loop"""

        if not self._session:
            raise RuntimeError("Loop context manager not ente red")

        if not self.url or not self.name:
            raise RuntimeError("Loop not configured - call with_loop first")

        event_data = {"type": type, **data}

        # Add loop_id if we have one
        if self.loop_id:
            event_data["loop_id"] = self.loop_id

        endpoint_url = f"{self.url.rstrip('/')}/{self.name}"

        async with self._session.post(endpoint_url, json=event_data) as response:
            if response.status >= 400:
                error_text = await response.text()
                raise Exception(f"HTTP {response.status}: {error_text}")
            return await response.json()


async def handle_events(event):
    print("Event received: ", event)


async def main():
    client = LoopClient()

    async with client.with_loop(
        url="http://localhost:8111",
        name="pr-review",
        event_callback=handle_events,
    ) as loop:
        await loop.send("pr_opened", {"repo_url": "ok then", "sha1": "testmeout"})


if __name__ == "__main__":
    asyncio.run(main())
