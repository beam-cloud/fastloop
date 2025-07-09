from fastloop import FastLoop, LoopContext, LoopEvent


class MockClient:
    def transcribe(self, message: str):
        return message + " - from the server"


app = FastLoop(name="pr-review-bot")


class AppContext(LoopContext):
    client: MockClient | None = None


async def load_client(context: LoopContext):
    print("Loading client...")
    context.client = MockClient()


@app.event("pr_opened")
class PrOpenedEvent(LoopEvent):
    repo_url: str
    sha1: str


@app.event("pr_closed")
class PrClosedEvent(LoopEvent):
    repo_url: str
    sha1: str


@app.event("pr_merged")
class PrMergedEvent(LoopEvent):
    repo_url: str
    sha1: str


@app.event("changes_approved")
class GitHubChangesApprovedEvent(LoopEvent):
    approved: bool


@app.loop(
    name="pr-review",
    start_event=PrOpenedEvent,
    idle_timeout=60.0,
    on_loop_start=load_client,
)
async def pr_view(context: AppContext):
    print("Starting PR review loop")

    github_event: PrOpenedEvent | None = await context.get("github_event")
    if not github_event:
        github_event = await context.wait_for(PrOpenedEvent)
        await context.set("github_event", github_event)

    print("Waiting for approval: ", github_event.sha1)
    approval_event: GitHubChangesApprovedEvent | None = await context.wait_for(
        GitHubChangesApprovedEvent, timeout=5.0, raise_on_timeout=False
    )
    if not approval_event:
        print("No approval event received, pausing loop")
        context.pause()
    else:
        print("Approval event received, committing changes: ", approval_event.approved)
        context.stop()


if __name__ == "__main__":
    app.run(port=8111)
