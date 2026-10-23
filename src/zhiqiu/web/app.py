"""The FastAPI app behind `zhiqiu dashboard` and `zhiqiu web`.

Paths, status codes and JSON shapes are those of the 7.2 standard-library server. Read routes
open the database read-only per request; workbench routes exist only when a workbench is given,
and every one of them passes the checks in `security`.
"""
from __future__ import annotations

import sqlite3
from http import HTTPStatus
from importlib import resources
from typing import Any, Callable
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import dashboard as data
from . import sse
from .security import Guard, error, json_body, json_response, read_checked, token_ok

# ----- request bodies --------------------------------------------------------------------------


class _Body(BaseModel):
    # Unknown fields are ignored, as the 7.2 server did; values are checked by hand-written rules
    # so a refusal says what is wrong in the page's language.
    model_config = ConfigDict(extra="ignore", validate_default=True)


class Source(_Body):
    """How a text was entered: typed, or a voice transcript the user read and sent."""
    input_source: Any = "text"
    voice_id: Any = None

    @field_validator("input_source")
    @classmethod
    def _known(cls, value: Any) -> Any:
        if value not in data.INPUT_SOURCES:
            raise ValueError("input_source 只能是 text 或 voice")
        return value


class ChatInput(Source):
    text: Any = None

    @field_validator("text")
    @classmethod
    def _text(cls, value: Any) -> Any:
        if not isinstance(value, str) or not value.strip() or len(value) > data.MAX_BODY:
            raise ValueError("text 必须是非空字符串")
        return value


class Respond(Source):
    request_id: Any = ""
    value: Any = None
    origin: Any = None


class ConversationControl(_Body):
    conversation_id: Any = None
    title: Any = None


class RootChange(_Body):
    kind: Any = None
    path: Any = None

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: Any) -> Any:
        if value not in ("read", "write"):
            raise ValueError("kind 只能是 read 或 write")
        return value


def _parse(model: type[BaseModel], body: dict[str, Any]) -> BaseModel | Response:
    try:
        return model.model_validate(body)
    except ValidationError as exc:
        message = str(exc.errors()[0].get("msg", "请求参数无效")).removeprefix("Value error, ")
        return error(HTTPStatus.BAD_REQUEST, message)


# ----- the app ---------------------------------------------------------------------------------


def create_app(server: Any) -> FastAPI:
    """server: the DashboardServer, which holds the port, the token, the workbench and the voice state."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(Guard, server=server)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code == HTTPStatus.NOT_FOUND:
            return error(HTTPStatus.NOT_FOUND, "没有这个接口")
        if exc.status_code == HTTPStatus.METHOD_NOT_ALLOWED:
            return error(HTTPStatus.NOT_FOUND, "没有这个接口")
        return error(exc.status_code, str(exc.detail))

    @app.get("/")
    @app.get("/index.html")
    def index() -> Response:
        return Response(data.page(server.token), media_type="text/html; charset=utf-8")

    def read(handler: Callable[[sqlite3.Connection, Any], dict[str, Any]], arg: Callable[[Request], Any]) -> Callable:
        def route(request: Request) -> Response:
            try:
                db = server.connect()
            except (ValueError, sqlite3.Error) as exc:
                return error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
            try:
                return json_response(HTTPStatus.OK, handler(db, arg(request)))
            except LookupError as exc:
                return error(HTTPStatus.NOT_FOUND, str(exc))
            except data.BadRequest as exc:
                return error(HTTPStatus.BAD_REQUEST, str(exc))
            except sqlite3.OperationalError as exc:
                # e.g. the terminal holds the write lock a moment longer than busy_timeout; the page retries.
                return error(HTTPStatus.SERVICE_UNAVAILABLE, f"数据库暂时繁忙：{exc}")
            finally:
                db.close()
        return route

    def query(request: Request) -> dict[str, list[str]]:
        return parse_qs(request.url.query)

    for path, handler in data.ROUTES.items():
        app.add_api_route(path, read(handler, query), methods=["GET"])
    for prefix, detail in data.DETAIL_ROUTES.items():
        app.add_api_route(prefix + "{ident:path}", read(detail, lambda request: request.path_params["ident"]),
                          methods=["GET"])
    if server.workbench is not None:
        _workbench_routes(app, server, query)
    return app


def _workbench_routes(app: FastAPI, server: Any, query: Callable[[Request], dict[str, list[str]]]) -> None:
    bench = server.workbench

    @app.get("/voice-worklet.js")
    def worklet() -> Response:
        return Response(resources.files("zhiqiu").joinpath("voice-worklet.js").read_bytes(),
                        media_type="text/javascript; charset=utf-8")

    def chat_get(handler: Callable[[Any, dict[str, list[str]]], dict[str, Any]]) -> Callable:
        def route(request: Request) -> Response:
            # Chat reads are conversation content: only our page, which holds the token, may read them.
            if not token_ok(request, server.token):
                return error(HTTPStatus.FORBIDDEN, "缺少或错误的工作台令牌")
            try:
                return json_response(HTTPStatus.OK, handler(server, query(request)))
            except LookupError as exc:
                return error(HTTPStatus.NOT_FOUND, str(exc))
            except (ValueError, sqlite3.Error) as exc:
                return error(HTTPStatus.SERVICE_UNAVAILABLE, f"数据库暂时繁忙：{exc}")
        return route

    for path, handler in data.CHAT_GETS.items():
        app.add_api_route(path, chat_get(handler), methods=["GET"])

    @app.get("/api/chat/stream")
    def stream(request: Request) -> Response:
        if not token_ok(request, server.token):
            return error(HTTPStatus.FORBIDDEN, "缺少或错误的工作台令牌")
        after = sse.start_after(request.headers.get("last-event-id"), request.query_params.get("after"))
        return StreamingResponse(sse.events(server, after), media_type="text/event-stream; charset=utf-8",
                                 headers={"X-Accel-Buffering": "no"})

    @app.post("/api/chat/transcribe")
    async def transcribe(request: Request) -> Response:
        from .. import voice
        audio = await read_checked(request, server, "audio/wav", voice.MAX_WAV_BYTES)
        if isinstance(audio, Response):
            return audio
        return json_response(*await run_in_threadpool(data.transcribe, server, audio))

    async def body_of(request: Request, model: type[BaseModel]) -> BaseModel | Response:
        body = await json_body(request, server, data.MAX_BODY)
        return body if isinstance(body, Response) else _parse(model, body)

    @app.post("/api/voice/install")
    async def voice_install(request: Request) -> Response:
        body = await json_body(request, server, data.MAX_BODY)
        if isinstance(body, Response):
            return body
        return json_response(*await run_in_threadpool(data.voice_install, server))

    @app.post("/api/chat/input")
    async def chat_input(request: Request) -> Response:
        item = await body_of(request, ChatInput)
        if isinstance(item, Response):
            return item
        voice = server.voice_meta(item.voice_id) if item.input_source == "voice" else None
        if bench.submit(item.text, item.input_source, voice):
            return json_response(HTTPStatus.ACCEPTED, {"accepted": True, "next": bench.io.last_seq})
        return error(HTTPStatus.CONFLICT, "上一轮还没结束")

    @app.post("/api/chat/respond")
    async def respond(request: Request) -> Response:
        item = await body_of(request, Respond)
        if isinstance(item, Response):
            return item
        voice = server.voice_meta(item.voice_id) if item.input_source == "voice" else None
        try:
            bench.io.respond(str(item.request_id or ""), item.value, str(item.origin or "chat"),
                             item.input_source, voice)
        except LookupError as exc:
            return error(HTTPStatus.CONFLICT, str(exc))
        except ValueError as exc:
            return error(HTTPStatus.BAD_REQUEST, str(exc))
        return json_response(HTTPStatus.OK, {"answered": True})

    @app.post("/api/chat/stop")
    async def stop(request: Request) -> Response:
        body = await json_body(request, server, data.MAX_BODY)
        if isinstance(body, Response):
            return body
        return json_response(HTTPStatus.OK, {"stopped": bench.io.stop()})

    for action in ("switch", "new", "rename", "compact"):
        def control(action: str = action) -> Callable:
            async def route(request: Request) -> Response:
                item = await body_of(request, ConversationControl)
                if isinstance(item, Response):
                    return item
                return json_response(*await run_in_threadpool(data.conversation_control, server, action,
                                                              item.model_dump()))
            return route
        app.add_api_route(f"/api/chat/{action}", control(), methods=["POST"])

    for action in ("preview", "add", "remove"):
        def roots(action: str = action) -> Callable:
            async def route(request: Request) -> Response:
                item = await body_of(request, RootChange)
                if isinstance(item, Response):
                    return item
                return json_response(*await run_in_threadpool(data.roots_change, server, action, item.model_dump()))
            return route
        app.add_api_route(f"/api/roots/{action}", roots(), methods=["POST"])
