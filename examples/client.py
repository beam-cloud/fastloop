import asyncio

from fastloop.client import LoopClient


async def handle_events(event):
    print("Event received: ", event)


async def main():
    client = LoopClient()
    async with client.with_loop(
        url="http://localhost:8111/pr-review",
        event_callback=handle_events,
    ) as loop:
        await loop.send("pr_opened", {"repo_url": "ok then", "sha1": "testmeout"})


if __name__ == "__main__":
    asyncio.run(main())


"""
something like this?

let {send, stop, pause} = useLoop(url="http://localhost:8111/pr-review",
        event_callback=handle_events,)
"""
