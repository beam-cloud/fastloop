import os
from typing import Any

from fastloop import FastLoop, LoopContext
from fastloop.integrations.slack import (
    SlackAppMentionEvent,
    SlackFileSharedEvent,
    SlackIntegration,
    SlackMessageEvent,
)
from fastloop.models import LoopEvent

app = FastLoop(name="slackdemo")


class AppContext(LoopContext):
    client: Any


async def resolve_bot_token(context: LoopContext, event: LoopEvent) -> str:
    """
    Resolve the bot token for this workspace.

    In a real app, you would look this up from a database based on the team_id
    that was stored when the workspace installed your Slack app via OAuth.

    Example:
        team_id = getattr(event, 'team', None)
        return await db.get_bot_token(team_id)
    """
    return os.getenv("SLACK_BOT_TOKEN") or ""


async def analyze_file(context: AppContext):
    file_shared: SlackFileSharedEvent | None = await context.wait_for(
        SlackFileSharedEvent, timeout=1
    )
    if not file_shared:
        return

    file_bytes = await file_shared.download_file(context)
    with open("something.png", "wb") as f:
        f.write(file_bytes)


@app.loop(
    "filebot",
    integrations=[
        SlackIntegration(
            signing_secret=os.getenv("SLACK_SIGNING_SECRET"),
            setup=resolve_bot_token,
        )
    ],
)
async def test_slack_bot(context: AppContext):
    mention: SlackAppMentionEvent | None = await context.wait_for(
        SlackAppMentionEvent, timeout=1
    )
    if mention:
        await context.set("initial_mention", mention)
        await context.emit(
            SlackMessageEvent(
                channel=mention.channel,
                user=mention.user,
                text="Upload a file to get started.",
                ts=mention.ts,
                thread_ts=mention.ts,
                team=mention.team,
                event_ts=mention.event_ts,
            )
        )

        context.switch_to(analyze_file)


if __name__ == "__main__":
    app.run(port=8111)
