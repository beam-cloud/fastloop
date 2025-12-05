"""
Workflow examples - function-based and class-based styles.
"""

from fastloop import FastLoop, LoopContext, LoopEvent, Workflow, WorkflowBlock

app = FastLoop(name="workflow-demo")


@app.event("start_workflow")
class StartWorkflow(LoopEvent):
    pass


@app.event("user_input")
class UserInput(LoopEvent):
    value: str


@app.event("progress")
class Progress(LoopEvent):
    message: str
    step: int


def on_block_done(ctx: LoopContext, block: WorkflowBlock, payload: dict | None):
    print(f"  ✓ Block complete: {block.type}")


def on_error(ctx: LoopContext, block: WorkflowBlock, error: Exception):
    retries = getattr(ctx, "_retries", 0)
    if retries < 3:
        ctx._retries = retries + 1
        print(f"  ⟳ Retrying block {block.type} (attempt {retries + 1})")
        ctx.repeat()
    print(f"  ✗ Block failed: {block.type} - {error}")


@app.workflow(
    name="onboarding",
    start_event=StartWorkflow,
    on_block_complete=on_block_done,
    on_error=on_error,
)
async def onboarding_workflow(
    ctx: LoopContext,
    blocks: list[WorkflowBlock],
    current_block: WorkflowBlock,
):
    await ctx.emit(Progress(message=current_block.text, step=ctx.block_index + 1))

    match current_block.type:
        case "collect_name":
            response = await ctx.wait_for(
                UserInput, timeout=300.0, raise_on_timeout=False
            )
            if response:
                await ctx.set("user_name", response.value)
                ctx.next()
            else:
                ctx.repeat()

        case "collect_email":
            response = await ctx.wait_for(
                UserInput, timeout=300.0, raise_on_timeout=False
            )
            if response:
                await ctx.set("user_email", response.value)
                ctx.next()
            else:
                ctx.repeat()

        case "confirm":
            name = await ctx.get("user_name")
            email = await ctx.get("user_email")
            await ctx.emit(
                Progress(message=f"Complete: {name} <{email}>", step=ctx.block_count)
            )


@app.event("start_survey")
class StartSurvey(LoopEvent):
    pass


@app.workflow(name="survey", start_event=StartSurvey)
class SurveyWorkflow(Workflow):
    async def on_start(self, ctx: LoopContext) -> None:
        print(f"[{ctx.loop_id}] Survey started")

    async def on_stop(self, ctx: LoopContext) -> None:
        print(f"[{ctx.loop_id}] Survey completed")

    async def on_block_complete(
        self, ctx: LoopContext, block: WorkflowBlock, payload: dict | None
    ) -> None:
        print(f"  ✓ Survey step complete: {block.type}")

    async def on_error(
        self, ctx: LoopContext, block: WorkflowBlock, error: Exception
    ) -> None:
        print(f"  ✗ Survey error: {error}")

    async def execute(
        self,
        ctx: LoopContext,
        blocks: list[WorkflowBlock],
        current_block: WorkflowBlock,
    ) -> None:
        print(f"[{ctx.block_index + 1}/{ctx.block_count}] {current_block.type}")

        match current_block.type:
            case "question":
                response = await ctx.wait_for(
                    UserInput, timeout=60.0, raise_on_timeout=False
                )
                if response:
                    await ctx.set(f"answer_{ctx.block_index}", response.value)
                    ctx.next()
                else:
                    ctx.abort()

            case "summary":
                answers = [
                    await ctx.get(f"answer_{i}") for i in range(ctx.block_count - 1)
                ]
                print(f"Survey results: {answers}")


if __name__ == "__main__":
    app.run(port=8111)

# Example usage:
#
# 1. Start workflow:
#    curl -X POST http://localhost:8111/onboarding \
#      -H "Content-Type: application/json" \
#      -d '{
#        "type": "start_workflow",
#        "blocks": [
#          {"type": "collect_name", "text": "Please enter your name"},
#          {"type": "collect_email", "text": "Please enter your email"},
#          {"type": "confirm", "text": "Confirm your details"}
#        ]
#      }'
#
# 2. Subscribe to SSE events (use workflow_id from step 1):
#    curl -N http://localhost:8111/events/<workflow_id>/sse
#
# 3. Send event to workflow:
#    curl -X POST http://localhost:8111/onboarding/<workflow_id>/event \
#      -H "Content-Type: application/json" \
#      -d '{"type": "user_input", "value": "John Doe", "workflow_id": "<id>"}'
#
# 4. Check workflow status:
#    curl http://localhost:8111/onboarding/<workflow_id>
#
# 5. If service restarts, workflow resumes from current block automatically
