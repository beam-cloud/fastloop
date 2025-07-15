# FastLoop

A Python package for building and deploying stateful loops. We use this at [beam.cloud](https://www.beam.cloud) to deploy agents.

## Installation

```bash
pip install fastloop
```

## Usage

### Basic Example

```python
from fastloop import FastLoop, LoopContext, LoopEvent

app = FastLoop(name="my-app")

@app.event("user_message")
class UserMessage(LoopEvent):
    user_id: str
    message: str

@app.loop(name="chat", start_event=UserMessage)
async def chat_loop(context: LoopContext):
    user_msg = await context.wait_for(UserMessage, timeout=5.0)
    print(f"User {user_msg.user_id} sent a message: {user_msg.message}")
    
    # Your loop logic here

    # If you want to stop the loop
    context.stop()

    # If you want to pause the loop
    context.pause()

    # By default, we just run it again

if __name__ == "__main__":
    app.run(port=8000)
```

## Development

This project uses `uv` for dependency management.

```bash
# Install dependencies
uv sync

# Run tests
uv run pytest

# Build package
uv build
```