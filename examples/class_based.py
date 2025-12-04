"""
Example of class-based loop definition.

This demonstrates the class-based alternative to the function decorator approach.
Both styles use the same @app.loop decorator and have identical functionality.
"""

from fastloop import FastLoop, Loop, LoopContext, LoopEvent

app = FastLoop(name="class-based-demo")


@app.event("chat.message")
class ChatMessage(LoopEvent):
    content: str


@app.event("chat.response")
class ChatResponse(LoopEvent):
    content: str


@app.loop("chat", start_event=ChatMessage)
class ChatLoop(Loop):
    """A chat loop implemented as a class."""

    messages_processed: int = 0

    async def on_start(self, ctx: LoopContext) -> None:
        print(f"[{ctx.loop_id}] Chat loop started")
        self.messages_processed = 0

    async def on_stop(self, ctx: LoopContext) -> None:
        print(
            f"[{ctx.loop_id}] Chat loop stopped, processed {self.messages_processed} messages"
        )

    async def on_app_start(self, ctx: LoopContext) -> bool:
        print(f"[{ctx.loop_id}] App started, checking if loop should resume...")
        pending = await ctx.get("pending_work", default=False)
        return pending

    async def on_event(self, ctx: LoopContext, event: LoopEvent) -> None:
        print(f"[{ctx.loop_id}] Received event: {event.type}")

    async def loop(self, ctx: LoopContext) -> None:
        message = await ctx.wait_for(ChatMessage, timeout=5, raise_on_timeout=False)

        if message:
            self.messages_processed += 1
            response = f"Echo: {message.content}"
            await ctx.emit(ChatResponse(content=response))
            print(f"[{ctx.loop_id}] Processed message #{self.messages_processed}")


if __name__ == "__main__":
    app.run(port=8112)
