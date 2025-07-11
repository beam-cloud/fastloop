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
        final_config = self.default_config.copy()
        if config:
            final_config.update(config)

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            (temp_path / "main.py").write_text(source_code)

            import yaml

            yaml.dump(final_config, (temp_path / "config.yaml").open("w"))

            (temp_path / "entry.py").write_text(self._create_entry_point())

            (temp_path / "requirements.txt").write_text("fastloop\nfastapi\npydantic\n")

            bundle_path = temp_path / "fastloop_bundle.zip"
            with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                for file_path in temp_path.rglob("*"):
                    if file_path.is_file():
                        zipf.write(file_path, file_path.relative_to(temp_path))

            return bundle_path.read_bytes()

    def _create_entry_point(self) -> str:
        return """
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

from main import app

fastapi_app = app._app

async def asgi_app(scope, receive, send):
    await fastapi_app(scope, receive, send)

def handle_request(method, path, headers=None, body=None):
    headers = headers or {}
    body_bytes = body.encode("utf-8") if body else b""

    scope = {
        "type": "http",
        "method": method.upper(),
        "path": path,
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "http_version": "1.1",
        "scheme": "http",
        "query_string": b"",
        "server": ("localhost", 8000),
        "client": ("127.0.0.1", 12345),
    }

    response = {}

    async def receive():
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            response["status"] = message["status"]
            response["headers"] = dict(message["headers"])
        elif message["type"] == "http.response.body":
            response["body"] = message.get("body", b"")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(asgi_app(scope, receive, send))
        return {
            "status": response["status"],
            "headers": {k.decode(): v.decode() for k, v in response["headers"].items()},
            "body": response["body"].decode("utf-8"),
        }
    finally:
        loop.close()

print("🚀 FastLoop app initialized")
"""


compiler = FastLoopCompiler()
