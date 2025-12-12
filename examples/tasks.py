from fastloop import ExecutorType, FastLoop, RetryPolicy

app = FastLoop(name="task-demo")


@app.task(name="add", retry=RetryPolicy(max_attempts=3))
async def add(a: int, b: int) -> int:
    return a + b


@app.task(name="cpu_work", executor=ExecutorType.PROCESS)
def cpu_work(n: int) -> int:
    total = 0
    for i in range(n):
        total += i * i
    return total


@app.task(name="flaky", retry=RetryPolicy(max_attempts=5, initial_delay=0.5))
async def flaky_task() -> str:
    import random

    if random.random() < 0.5:
        raise Exception("Random failure")
    return "success"


@app.schedule(name="cleanup", cron="0 * * * *")
async def hourly_cleanup() -> dict:
    return {"cleaned": True}


@app.schedule(name="heartbeat", interval=60)
async def heartbeat() -> dict:
    return {"alive": True}


async def demo():
    task_id = await app.invoke("add", a=2, b=3)
    result = await app.invoke("add", a=10, b=20, wait=True)
    schedule_id = await app.schedule_task(
        "add", cron="*/5 * * * *", args={"a": 1, "b": 2}
    )
    print(f"task_id={task_id}, result={result}, schedule_id={schedule_id}")


if __name__ == "__main__":
    app.run(port=8112)
