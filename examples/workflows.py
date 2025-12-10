"""
Workflow examples - function-based and class-based styles.
"""

from typing import Any

from fastloop import (
    BlockPlan,
    FastLoop,
    LoopContext,
    LoopEvent,
    ScheduleType,
    Workflow,
    WorkflowBlock,
)

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


def on_block_done(_ctx: LoopContext, block: WorkflowBlock, _payload: dict | None):
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
    _blocks: list[WorkflowBlock],
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
        self, _ctx: LoopContext, block: WorkflowBlock, _payload: dict | None
    ) -> None:
        print(f"  ✓ Survey step complete: {block.type}")

    async def on_error(
        self, _ctx: LoopContext, _block: WorkflowBlock, error: Exception
    ) -> None:
        print(f"  ✗ Survey error: {error}")

    async def execute(
        self,
        ctx: LoopContext,
        _blocks: list[WorkflowBlock],
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


@app.event("start_email_monitor")
class StartEmailMonitor(LoopEvent):
    pass


async def email_monitor_plan(
    _ctx: LoopContext,
    _blocks: list[WorkflowBlock],
    current_block: WorkflowBlock,
    block_output: Any,
) -> BlockPlan:
    """Plan function decides next block and scheduling based on block output."""
    match current_block.type:
        case "search_emails":
            if block_output and block_output.get("found_emails"):
                return BlockPlan(schedule_type=ScheduleType.IMMEDIATE)
            return BlockPlan(
                next_block_index=0,
                schedule_type=ScheduleType.DELAY,
                delay_seconds=600,
                reason="No emails found, retry in 10 minutes",
            )
        case "notify":
            return BlockPlan(schedule_type=ScheduleType.STOP)
        case _:
            return BlockPlan(schedule_type=ScheduleType.IMMEDIATE)


@app.workflow(
    name="email_monitor",
    start_event=StartEmailMonitor,
    plan=email_monitor_plan,
)
async def email_monitor_workflow(
    ctx: LoopContext,
    _blocks: list[WorkflowBlock],
    current_block: WorkflowBlock,
) -> dict[str, Any]:
    """Workflow that returns output for the plan function to decide next steps."""
    print(f"[{ctx.block_index + 1}/{ctx.block_count}] {current_block.type}")

    match current_block.type:
        case "search_emails":
            emails = []  # simulate search
            return {"found_emails": len(emails) > 0, "emails": emails}
        case "summarize":
            return {"summary": "Email summary"}
        case "notify":
            return {"notified": True}
        case _:
            return {}


@app.event("start_retry_demo")
class StartRetryDemo(LoopEvent):
    pass


@app.workflow(name="retry_demo", start_event=StartRetryDemo)
class RetryDemoWorkflow(Workflow):
    """Class-based workflow with plan method for rate limit handling."""

    async def plan(
        self,
        ctx: LoopContext,
        _blocks: list[WorkflowBlock],
        _current_block: WorkflowBlock,
        block_output: Any,
    ) -> BlockPlan | None:
        if block_output and block_output.get("rate_limited"):
            return BlockPlan(
                next_block_index=ctx.block_index,
                schedule_type=ScheduleType.DELAY,
                delay_seconds=block_output.get("retry_after", 60),
            )
        if ctx.block_index >= ctx.block_count - 1:
            return BlockPlan(schedule_type=ScheduleType.STOP)
        return None

    async def execute(
        self,
        ctx: LoopContext,
        _blocks: list[WorkflowBlock],
        current_block: WorkflowBlock,
    ) -> dict[str, Any]:
        print(f"[{ctx.block_index + 1}/{ctx.block_count}] {current_block.type}")
        return {"success": True}


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
# 2. Subscribe to SSE events (use workflow_run_id from step 1):
#    curl -N http://localhost:8111/events/<workflow_run_id>/sse
#
# 3. Send event to workflow:
#    curl -X POST http://localhost:8111/onboarding/<workflow_run_id>/event \
#      -H "Content-Type: application/json" \
#      -d '{"type": "user_input", "value": "John Doe", "workflow_run_id": "<id>"}'
#
# 4. Check workflow status:
#    curl http://localhost:8111/onboarding/<workflow_run_id>
#
# 5. Cancel a running workflow:
#    curl -X POST http://localhost:8111/onboarding/<workflow_run_id>/cancel
#
# 6. If service restarts, workflow resumes from current block automatically
#
# 7. Start email monitor workflow with plan function:
#    curl -X POST http://localhost:8111/email_monitor \
#      -H "Content-Type: application/json" \
#      -d '{
#        "type": "start_email_monitor",
#        "blocks": [
#          {"type": "search_emails", "text": "Search for customer emails"},
#          {"type": "summarize", "text": "Summarize found emails"},
#          {"type": "notify", "text": "Send notification"}
#        ]
#      }'
#
#    The plan function will:
#    - Retry the search block with a 10-minute delay if no emails found
#    - Proceed immediately when emails are found
#    - Stop the workflow after notification
#    - Delays use Redis scheduling, so the workflow survives restarts
