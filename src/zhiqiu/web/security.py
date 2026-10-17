"""Checks every request passes before a route runs, defined once for the whole app.

- Every request: the Host must be this machine (no DNS rebinding), the method GET or, on the
  workbench only, POST; every response carries no-store, nosniff and the page's CSP.
- Workbench reads: the token embedded in the served page, sent as a header.
- Workbench writes: the token, the page's own Origin, the expected Content-Type and a size limit,
  checked against both the declared Content-Length and the bytes actually read. Refusals leave a
  GuardrailTriggered event, as in 7.2.
- Every JSON body goes through value-based redaction.
"""
from __future__ import annotations

import hmac
import json
from http import HTTPStatus
from typing import Any

from fastapi import Request
from fastapi.responses import Response

from ..redact import redact_obj

TOKEN_HEADER = "X-Zhiqiu-Token"
# 'self' scripts: only the fixed /voice-worklet.js, which the microphone recorder needs as a module.
CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; "
       "connect-src 'self'; img-src 'self' data:")
HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Content-Security-Policy": CSP}


def json_response(status: int, data: Any) -> Response:
    """The only way a route answers with JSON: redacted, UTF-8, never cached."""
    body = json.dumps(redact_obj(data), ensure_ascii=False, default=str).encode("utf-8")
    return Response(body, status_code=int(status), media_type="application/json; charset=utf-8")


def error(status: int, reason: str, **extra: Any) -> Response:
    return json_response(status, {"error": reason, **extra})


def host_ok(request: Request, port: int) -> bool:
    return request.headers.get("host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")


def token_ok(request: Request, token: str | None) -> bool:
    sent = request.headers.get(TOKEN_HEADER, "")
    return bool(token) and hmac.compare_digest(sent.encode("utf-8"), token.encode("utf-8"))


def origin_ok(request: Request, port: int) -> bool:
    # Browsers send Origin on POST; a page on another site cannot fake ours. Tools like curl send none.
    origin = request.headers.get("origin")
    return origin is None or origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")


def content_type(request: Request) -> str:
    # As email.message.get_content_type(): parameters dropped, text/plain when missing.
    value = request.headers.get("content-type", "").split(";")[0].strip().lower()
    return value or "text/plain"


class Guard:
    """Host and method for every request; the shared headers on every response.

    Plain ASGI rather than BaseHTTPMiddleware, so the SSE stream passes through untouched."""

    def __init__(self, app: Any, server: Any):
        self.app = app
        self.server = server

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        refusal: Response | None = None
        if not host_ok(request, self.server.port):
            if request.method != "GET" and self.server.workbench is not None:
                await self.server.refuse(request, "只接受来自本机地址的请求")
            refusal = error(HTTPStatus.FORBIDDEN, "只接受来自本机地址的请求")
        elif request.method not in ("GET", "HEAD") and (request.method != "POST" or self.server.workbench is None):
            refusal = error(HTTPStatus.METHOD_NOT_ALLOWED, "看板只读，只接受 GET")

        async def with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                names = {name.lower().encode("latin-1") for name in HEADERS}
                message["headers"] = [(key, value) for key, value in message.get("headers", [])
                                      if key.lower() not in names] + [
                    (name.encode("latin-1"), value.encode("latin-1")) for name, value in HEADERS.items()]
            await send(message)
        if refusal is not None:
            await refusal(scope, receive, with_headers)
        else:
            await self.app(scope, receive, with_headers)


async def read_checked(request: Request, server: Any, expected: str, limit: int) -> bytes | Response:
    """The request body after every workbench write check; a refusal Response otherwise."""
    if not token_ok(request, server.token):
        reason, status = "缺少或错误的工作台令牌", HTTPStatus.FORBIDDEN
    elif not origin_ok(request, server.port):
        reason, status = "请求来自其他网站", HTTPStatus.FORBIDDEN
    elif content_type(request) != expected:
        reason, status = f"只接受 {expected}", HTTPStatus.UNSUPPORTED_MEDIA_TYPE
    else:
        too_large = f"请求体超过 {limit} 字节或长度无效"
        declared = request.headers.get("content-length")
        try:
            length = int(declared) if declared is not None else None
        except ValueError:
            length = -1
        if length is not None and not 0 <= length <= limit:
            await server.refuse(request, too_large)
            return error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, too_large)
        # Counted as it arrives, so a body without a length (chunked) cannot exceed the limit either.
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > limit:
                await server.refuse(request, too_large)
                return error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, too_large)
        return bytes(body)
    await server.refuse(request, reason)
    return error(status, reason)


async def json_body(request: Request, server: Any, limit: int) -> dict[str, Any] | Response:
    raw = await read_checked(request, server, "application/json", limit)
    if isinstance(raw, Response):
        return raw
    try:
        data = json.loads(raw or b"{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        data = None
    if isinstance(data, dict):
        return data
    return error(HTTPStatus.BAD_REQUEST, "请求体必须是 JSON 对象")
