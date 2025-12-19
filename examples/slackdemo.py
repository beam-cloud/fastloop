import os
from typing import Any

from fastloop import FastLoop, LoopContext
from fastloop.integrations.slack import (
    SlackAppMentionEvent,
    SlackConfig,
    SlackFileSharedEvent,
    SlackIntegration,
    SlackMessageEvent,
    SlackSetupInput,
)

app = FastLoop(name="slackdemo")


class AppContext(LoopContext):
    client: Any


async def resolve_slack_config(setup_input: SlackSetupInput) -> SlackConfig:
    """
    Resolve the Slack config for this workspace.

    In a real app, you would look up the bot token and signing secret from a database
    based on the team_id that was stored when the workspace installed your Slack app via OAuth.

    Example:
        workspace = await db.get_workspace(setup_input.team_id)
        return SlackConfig(
            bot_token=workspace.bot_token,
            signing_secret=workspace.signing_secret,
            team_id=setup_input.team_id,
        )
    """
    return SlackConfig(
        bot_token=os.getenv("SLACK_BOT_TOKEN") or "",
        signing_secret=os.getenv("SLACK_SIGNING_SECRET") or "",
        team_id=setup_input.team_id,
    )


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
    integrations=[SlackIntegration(setup=resolve_slack_config)],
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
