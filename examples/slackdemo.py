import os
from typing import Any

from fastloop import FastLoop, LoopContext
from fastloop.integrations.slack import (
    SlackAppMentionEvent,
    SlackFileSharedEvent,
    SlackIntegration,
    SlackMessageEvent,
)

app = FastLoop(name="slackdemo")


class AppContext(LoopContext):
    client: Any


async def analyze_file(context: AppContext):
    file_shared: SlackFileSharedEvent | None = await context.wait_for(
        SlackFileSharedEvent, timeout=1
    )
    if not file_shared:
        return

    file_bytes = await file_shared.download_file()
    with open("something.png", "wb") as f:
        f.write(file_bytes)


@app.loop(
    "filebot",
    # start_event=SlackAppMentionEvent,
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
