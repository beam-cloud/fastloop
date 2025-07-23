from typing import Any

from fastloop import FastLoop, LoopContext
from fastloop.integrations.slack import (
    SlackMessageEvent,
    create_slack_integration,
)

app = FastLoop(name="slackdemo")
app.add_integration(
    create_slack_integration(
        bot_token="xoxb-1234567890",
        signing_secret="d9cf6a238fbcf6a3b8b5dcd3ba12c06b",
        app_id="A096VJU1XD1",
        client_id="2018927186421.9233640065443",
        client_secret="f60ba7292e5c07fe32af2f6d51d15cdb",
        verification_token="7OkbmhIrzlCF3NSJUx5kzhiX",
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
