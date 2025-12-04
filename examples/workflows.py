"""
Workflow Example - Durable multi-step execution.

Control flow:
  ctx.next(payload)  → advance to next block
  ctx.repeat()       → re-run current block
  normal return      → workflow completes
"""

from fastloop import FastLoop, LoopContext, LoopEvent, WorkflowBlock

app = FastLoop(name="workflow-demo")


# --- Events ---


@app.event("start_workflow")
class StartWorkflow(LoopEvent):
    pass


@app.event("user_input")
class UserInput(LoopEvent):
    value: str


# --- Callbacks (optional) ---


def on_block_done(ctx: LoopContext, block: WorkflowBlock, payload: dict | None):
    print(f"  ✓ Block complete: {block.type}")


def on_error(ctx: LoopContext, block: WorkflowBlock, error: Exception):
    """
    Called when a block raises an exception.
    - Normal return → workflow stops
    - Raise ctx.repeat() → retry the block
    """
    retries = getattr(ctx, "_retries", 0)
    if retries < 3:
        ctx._retries = retries + 1
        print(f"  ⟳ Retrying block {block.type} (attempt {retries + 1})")
        ctx.repeat()  # Raises WorkflowRepeatError → retry
    print(f"  ✗ Block failed: {block.type} - {error}")


# --- Workflow ---


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
    """
    Multi-step onboarding. State survives restarts.

    ctx.block_index      - current position (0-indexed)
    ctx.block_count      - total blocks
    ctx.previous_payload - data from previous ctx.next(payload)
    """
    print(f"[{ctx.block_index + 1}/{ctx.block_count}] {current_block.type}")

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
            print(f"Done: {name} <{email}>")


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
# 2. Send event to workflow (use workflow_id from response):
#    curl -X POST http://localhost:8111/onboarding/<workflow_id>/event \
#      -H "Content-Type: application/json" \
#      -d '{"type": "user_input", "value": "John Doe", "workflow_id": "<id>"}'
#
# 3. Check workflow status:
#    curl http://localhost:8111/onboarding/<workflow_id>
#
# 4. If service restarts, workflow resumes from current block automatically
