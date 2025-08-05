from fastloop import FastLoop, LoopContext
from fastloop.integrations.conversation import (
    ConversationIntegration,
    GenerateResponseEvent,
    StartConversationEvent,
)

app = FastLoop(name="my-app")


# @app.loop(name="start_conversation"])
async def start_conversation(context: LoopContext):
    print("Conversation started")


@app.loop(name="chat", start_event=StartConversationEvent, integrations=[ConversationIntegration()])
async def chat_loop(context: LoopContext):
    # User can trigger something here, for example a call or audio stream
    # await context.wait_for(StartConversationEvent, timeout=5.0)
    await context.wait_for(GenerateResponseEvent)
    print("Conversation started")

    # context.

if __name__ == "__main__":
    app.run(port=8000)
