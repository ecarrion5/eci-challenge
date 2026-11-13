"""Call our own FastAPI app in-process over ASGI, with no network and no extra dependencies.

The request goes through the real stack: routing, validation, dependencies, the
per-request transaction and the error handlers. So the agent sees exactly what an
HTTP client would. Requires a FastAPI built with ``scope="function"`` sessions
(``app.deps.DbSession``), so each inner call commits before it returns.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ApiResult:
    status_code: int
    body: Any


async def call_api(app: Any, method: str, path: str, body: Any | None = None,
                   headers: dict[str, str] | None = None) -> ApiResult:
    payload = b"" if body is None else json.dumps(body).encode()
    raw_headers = [(b"content-type", b"application/json"),
                   (b"content-length", str(len(payload)).encode())]
    raw_headers += [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    path_only, _, query = path.partition("?")
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method.upper(), "scheme": "http", "path": path_only,
        "raw_path": path_only.encode(), "query_string": query.encode(), "root_path": "",
        "headers": raw_headers, "client": ("agent", 0), "server": ("agent", 80),
    }

    done = asyncio.Event()
    body_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await done.wait()  # a later receive() only checks for a disconnect
        return {"type": "http.disconnect"}

    status = 500
    chunks: list[bytes] = []

    async def send(message: dict[str, Any]) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                done.set()

    try:
        await app(scope, receive, send)
    finally:
        done.set()

    raw = b"".join(chunks)
    try:
        parsed: Any = json.loads(raw) if raw else None
    except ValueError:
        parsed = raw.decode(errors="replace")
    return ApiResult(status, parsed)
