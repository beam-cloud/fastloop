import tempfile
import zipfile
from pathlib import Path
from typing import Any


class FastLoopCompiler:
    def __init__(self):
        self.default_config = {
            "debug_mode": False,
            "log_level": "INFO",
            "state": {"type": "memory"},
        }

    def compile_to_pyodide_bundle(
        self, source_code: str, config: dict[str, Any] | None = None
    ) -> bytes:
        """
        Package FastLoop source code into a Pyodide-compatible zip bundle.
        """
        final_config = self.default_config.copy()
        if config:
            final_config.update(config)

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # Write your FastLoop app
            (temp_path / "main.py").write_text(source_code)

            # Write config file
            import yaml

            yaml.dump(final_config, (temp_path / "config.yaml").open("w"))

            # Write entry point for Pyodide
            (temp_path / "entry.py").write_text(self._create_entry_point())

            # Write requirements.txt - use Pyodide-compatible versions
            (temp_path / "requirements.txt").write_text(
                "fastloop==0.1.5\nfastapi\npydantic>=2.0,<2.5\nPyYAML\ncloudpickle\n"
            )

            # Create zip bundle in a separate temporary location to avoid nesting
            with tempfile.NamedTemporaryFile(
                suffix=".zip", delete=False
            ) as bundle_file:
                bundle_path = Path(bundle_file.name)
                with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                    for file_path in temp_path.rglob("*"):
                        if file_path.is_file():
                            zipf.write(file_path, file_path.relative_to(temp_path))

                # Read the bundle bytes
                bundle_bytes = bundle_path.read_bytes()

            # Clean up the temporary zip file
            bundle_path.unlink()

            return bundle_bytes

    def _create_entry_point(self) -> str:
        return """
import asyncio
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

print("Loading FastLoop app...")

# Force memory state for Pyodide
def is_pyodide() -> bool:
    try:
        import sys
        return "pyodide" in sys.modules
    except ImportError:
        return False

if is_pyodide():
    print("Pyodide detected - forcing memory state")
    # Override the config to use memory state
    import yaml
    config_content = '''
state:
  type: memory
  config:
    max_size: 1000
'''
    with open('config.yaml', 'w') as f:
        f.write(config_content)
    print("✅ Config updated to use memory state")

try:
    from main import app
    print("✅ FastLoop app imported successfully")
except Exception as e:
    print(f"❌ Error importing FastLoop app: {e}")
    raise

# Get the ASGI app
asgi_app = app._app
print("✅ ASGI app created")

# Create a simple request handler that actually calls your FastLoop app
async def handle_request_async(method, path, headers=None, body=None):
    from starlette.requests import Request
    from starlette.responses import Response

    # Convert headers to proper format
    if headers is None:
        headers = {}

    # Convert string headers to bytes for ASGI
    header_list = [(k.encode(), v.encode()) for k, v in headers.items()]

    # Create scope for the request
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": header_list,
        "query_string": b"",
        "client": ("127.0.0.1", 8000),
        "server": ("127.0.0.1", 8000),
    }

    print(f"🔍 Handling request: {method} {path}")
    print(f"🔍 Headers: {headers}")
    print(f" Body: {body}")
    print(f"🔍 Scope: {scope}")

    # Create a simple ASGI application callable
    async def receive():
        return {"type": "http.request", "body": body.encode() if body else b""}

    response_body = []
    response_status = None
    response_headers = {}

    async def send(message):
        nonlocal response_status, response_headers
        if message["type"] == "http.response.start":
            response_status = message["status"]
            # Convert bytes headers to strings for JSON serialization
            response_headers = {k.decode(): v.decode() for k, v in message["headers"]}
            print(f"🔍 ASGI message: {message}")
        elif message["type"] == "http.response.body":
            response_body.append(message["body"])
            print(f"🔍 ASGI message: {message}")

    print("🔍 Calling ASGI app...")
    await asgi_app(scope, receive, send)
    print("✅ ASGI app completed")

    # Combine response body
    full_body = b"".join(response_body).decode()

    # Return a JSON-serializable result
    result = {
        "status": response_status,
        "headers": response_headers,
        "body": full_body
    }

    print(f"🔍 Returning result: {result}")
    return result

print("✅ FastLoop server ready!")
"""
