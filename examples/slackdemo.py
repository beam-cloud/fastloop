import os
from typing import Any

from fastloop import FastLoop, LoopContext
from fastloop.integrations.slack import (
    SlackAppMentionEvent,
    SlackIntegration,
    SlackMessageEvent,
)

app = FastLoop(name="slackdemo")


class BotContext(LoopContext):
    client: Any


@app.loop(
    "somebot",
    start_event=SlackAppMentionEvent,
    integrations=[
        SlackIntegration(
            app_id=os.getenv("SLACK_APP_ID") or "",
            bot_token=os.getenv("SLACK_BOT_TOKEN") or "",
            signing_secret=os.getenv("SLACK_SIGNING_SECRET") or "",
            client_id=os.getenv("SLACK_CLIENT_ID") or "",
            client_secret=os.getenv("SLACK_CLIENT_SECRET") or "",
            verification_token=os.getenv("SLACK_VERIFICATION_TOKEN") or "",
        )
    ],
)
async def my_bot(context: BotContext):
    msg: SlackAppMentionEvent | None = await context.wait_for(
        SlackAppMentionEvent, timeout=1, raise_on_timeout=True
    )
    if msg:
        print(context.loop_id, msg.channel, msg.user, msg.text)

        await context.emit(
            SlackMessageEvent(
                loop_id=context.loop_id,
                channel=msg.channel,
                user=msg.user,
                text="Hello, world!",
                ts=msg.ts,
                thread_ts=msg.thread_ts,
                team=msg.team,
                event_ts=msg.event_ts,
            )
        )


if __name__ == "__main__":
    app.run(port=8111)
