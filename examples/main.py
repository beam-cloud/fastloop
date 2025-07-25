from fastloop import FastLoop, LoopContext, LoopEvent


class MockClient:
    def transcribe(self, message: str):
        return message + " - from the server"


app = FastLoop(name="basic-chat-demo")


class AppContext(LoopContext):
    client: MockClient | None = None


async def load_client(context: AppContext):
    print("Loading client...")
    context.client = MockClient()


@app.event("user_message")
class UserMessage(LoopEvent):
    msg: str


@app.event("user_approval")
class UserApprovalEvent(LoopEvent):
    approved: bool


@app.event("agent_message")
class AgentMessage(LoopEvent):
    msg: str


async def wait_for_approval(context: AppContext) -> None:
    print("Another function!!! ", context.client)

    user_approval_event: UserApprovalEvent | None = await context.wait_for(
        UserApprovalEvent, timeout=1.0
    )
    if not user_approval_event:
        print("No user approval event")
        return

    if user_approval_event.approved:
        context.switch_to(basic_chat)


@app.loop(
    name="chat",
    start_event=UserMessage,
    on_start=load_client,
)
async def basic_chat(context: AppContext):
    print("Basic chat")
    user_message: UserMessage | None = await context.wait_for(
        UserMessage, timeout=1.0, raise_on_timeout=False
    )
    if not user_message:
        print("No user message")

    context.switch_to(wait_for_approval)


if __name__ == "__main__":
    app.run(port=8111)
