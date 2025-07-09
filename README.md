# FastLoop

A Python package for building and deploying stateful loops.

## Installation

```bash
pip install fastloop
```

## Usage

### Basic Example

```python
from fastloop import FastLoop, LoopContext, LoopEvent

app = FastLoop(name="my-app")

@app.event("start")
class StartEvent(LoopEvent):
    user_id: str
    message: str

@app.loop(name="chat", start_event=StartEvent)
async def chat_loop(context: LoopContext):
    # Get the initial event
    start_event = await context.wait_for(StartEvent)
    print(f"User {start_event.user_id} started chat: {start_event.message}")
    
    # Your loop logic here
    context.stop()

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

## License

[Add your license here] 