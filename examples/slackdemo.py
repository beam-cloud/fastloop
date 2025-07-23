import os
from typing import Any

from fastloop import FastLoop, LoopContext
from fastloop.integrations.slack import (
    SlackMessageEvent,
    create_slack_integration,
)

app = FastLoop(name="slackdemo")
app.add_integration(
    create_slack_integration(
        bot_token=os.getenv("SLACK_BOT_TOKEN") or "",
        signing_secret=os.getenv("SLACK_SIGNING_SECRET") or "",
        app_id=os.getenv("SLACK_APP_ID") or "",
        client_id=os.getenv("SLACK_CLIENT_ID") or "",
        client_secret=os.getenv("SLACK_CLIENT_SECRET") or "",
        verification_token=os.getenv("SLACK_VERIFICATION_TOKEN") or "",
    )
)


class BotContext(LoopContext):
    client: Any


@app.loop("somebot", start_event=SlackMessageEvent)
async def my_bot(context: BotContext):
    msg: Any = await context.wait_for(
        SlackMessageEvent, timeout=10, raise_on_timeout=True
    )
    print(msg)


if __name__ == "__main__":
    app.run(port=8111)
