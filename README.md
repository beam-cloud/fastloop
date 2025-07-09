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

### Server-Sent Events (SSE)

FastLoop provides SSE endpoints for real-time event streaming:

#### Endpoints

- `GET /events/{loop_id}/{event_type}` - Stream events as SSE
- `GET /events/{loop_id}/{event_type}/single` - Get a single event (non-streaming)
- `GET /events/{loop_id}/history` - Get event history for a loop

#### JavaScript Client

```javascript
const eventSource = new EventSource('/events/my-loop-123/pr_opened');

eventSource.onmessage = function(event) {
    const data = JSON.parse(event.data);
    console.log('Received event:', data);
};

eventSource.onerror = function(event) {
    console.error('SSE error:', event);
};
```

#### Python Client

```python
import asyncio
import aiohttp

async def stream_events(loop_id: str, event_type: str):
    url = f"http://localhost:8000/events/{loop_id}/{event_type}"
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            async for line in response.content:
                line = line.decode('utf-8').strip()
                if line.startswith('data: '):
                    data = json.loads(line[6:])
                    print(f"Event: {data}")

# Usage
asyncio.run(stream_events("my-loop-123", "pr_opened"))
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