import os
from typing import Any

from fastloop import FastLoop, LoopContext
from fastloop.integrations.slack import (
    SlackAppMentionEvent,
    SlackFileSharedEvent,  # noqa
    SlackIntegration,
    SlackMessageEvent,
)

app = FastLoop(name="slackdemo")


class AppContext(LoopContext):
    client: Any


async def some_other_function(context: AppContext):
    initial_mention: SlackAppMentionEvent | None = await context.get("initial_mention")
    if not initial_mention:
        return

    await context.emit(
        SlackMessageEvent(
            channel=initial_mention.channel,
            user=initial_mention.user,
            text="something else",
            ts=initial_mention.ts,
            thread_ts=initial_mention.ts,
            team=initial_mention.team,
            event_ts=initial_mention.event_ts,
        )
    )

    await context.sleep_for("1 hour")


@app.loop(
    "dumbbot",
    start_event=SlackAppMentionEvent,  # SlackFileSharedEvent,
    integrations=[
        SlackIntegration(
            app_id=os.getenv("SLACK_APP_ID") or "",
            bot_token=os.getenv("SLACK_BOT_TOKEN") or "",
            signing_secret=os.getenv("SLACK_SIGNING_SECRET") or "",
            client_id=os.getenv("SLACK_CLIENT_ID") or "",
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
                text="I am ready to do stuff.",
                ts=mention.ts,
                thread_ts=mention.ts,
                team=mention.team,
                event_ts=mention.event_ts,
            )
        )


if __name__ == "__main__":
    app.run(port=8111)
