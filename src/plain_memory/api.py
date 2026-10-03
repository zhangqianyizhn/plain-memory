"""AML 外部契约；成功仅在完整写入后返回，错误不回显私密输入。"""

import secrets
import sqlite3
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr
from starlette.responses import JSONResponse

from plain_memory import __version__
from plain_memory.config import Settings
from plain_memory.embedding import EmbeddingError, HTTPEmbedder, normalize_vectors
from plain_memory.retrieval import retrieve
from plain_memory.store import RequestConflict, Store, StoreMismatch, payload_hash
from plain_memory.text import chunks, lexical_terms

Identifier = Annotated[StrictStr, Field(min_length=1)]


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: StrictStr
    timestamp: Annotated[StrictInt, Field(ge=-62135596800000, le=253402300799999)] | None = None


class AddRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: Identifier
    user_id: Identifier
    session_id: Identifier
    messages: Annotated[list[Message], Field(min_length=1)]


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: Annotated[StrictStr, Field(min_length=1)]
    user_id: Identifier
    top_k: Annotated[StrictInt, Field(ge=1)]
    options: list[StrictStr] | None = None


class BodyLimit:
    def __init__(self, app, maximum: int):
        self.app, self.maximum = app, maximum

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        parts, size = [], 0
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            body = event.get("body", b"")
            size += len(body)
            if size > self.maximum:
                return await JSONResponse(
                    {"detail": "Request exceeds the declared body limit."}, 413
                )(scope, receive, send)
            parts.append(body)
            if not event.get("more_body", False):
                break
        consumed = False

        async def buffered_receive():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": b"".join(parts), "more_body": False}
            return await receive()

        await self.app(scope, buffered_receive, send)


@contextmanager
def capacity(semaphore):
    if not semaphore.acquire(blocking=False):
        raise HTTPException(429, "Service concurrency limit reached.", headers={"Retry-After": "1"})
    try:
        yield
    finally:
        semaphore.release()


def create_app(settings: Settings, *, embedder=None) -> FastAPI:
    token = settings.api_token.get_secret_value()
    if len(token.encode()) < 32 or token == "GENERATE_LOCALLY":
        raise ValueError("Set a random api_token (at least 32 bytes) in the private local config.")
    encoder = embedder if embedder is not None else HTTPEmbedder(settings.embedding)
    try:
        store = Store(settings)
    except Exception:
        encoder.close()
        raise
    add_slots = threading.BoundedSemaphore(settings.add_concurrency)
    search_slots = threading.BoundedSemaphore(settings.search_concurrency)

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            encoder.close()

    app = FastAPI(
        title="PlainMemory",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.store = store
    app.add_middleware(BodyLimit, maximum=settings.max_body_bytes)

    def authorized(authorization: str | None = Header(default=None)):
        expected = f"Bearer {token}".encode()
        if authorization is None or not secrets.compare_digest(authorization.encode(), expected):
            raise HTTPException(
                401, "Invalid or missing Bearer credential.", headers={"WWW-Authenticate": "Bearer"}
            )

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        # 默认 FastAPI 错误含 content 等输入；只输出字段位置与错误类型。
        return JSONResponse(
            {"detail": [{"loc": error["loc"], "type": error["type"]} for error in exc.errors()]},
            status_code=422,
        )

    @app.exception_handler(EmbeddingError)
    async def embedding_failure(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.exception_handler(RequestConflict)
    async def conflict(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(StoreMismatch)
    async def incompatible(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.exception_handler(sqlite3.Error)
    async def storage_failure(request, exc):
        return JSONResponse({"detail": "Storage temporarily unavailable."}, status_code=503)

    def health():
        store.health()
        return {"status": "ok", "version": __version__}

    for path in ("/", "/health", "/add", "/search"):
        app.add_api_route(path, health, methods=["GET"])

    @app.post("/add", dependencies=[Depends(authorized)])
    def add(request: AddRequest):
        payload = request.model_dump()
        digest = payload_hash(payload)
        response = {
            "success": True,
            "request_id": request.request_id,
            "user_id": request.user_id,
            "session_id": request.session_id,
        }
        # 已提交的重试不需要占用 embedding 名额。
        if store.completed(payload, digest):
            return response
        with capacity(add_slots):
            deadline = time.monotonic() + settings.add_timeout_seconds
            if store.completed(payload, digest):
                return response
            prepared = []
            for message_index, message in enumerate(request.messages):
                for chunk_index, text in enumerate(
                    chunks(message.content, settings.chunk_tokens, settings.overlap_tokens)
                ):
                    prepared.append(
                        {
                            "message_index": message_index,
                            "chunk_index": chunk_index,
                            "role": message.role,
                            "timestamp": message.timestamp,
                            "content": text,
                            "terms": lexical_terms(text),
                        }
                    )
            vectors = None
            if prepared:
                vectors = normalize_vectors(
                    encoder.embed([chunk["content"] for chunk in prepared], deadline),
                    len(prepared),
                    settings.embedding.dimensions,
                )
            if time.monotonic() >= deadline:
                raise EmbeddingError("Add exceeded the service deadline; nothing was written.")
            store.commit(payload, digest, prepared, vectors)
        return response

    @app.post("/search", dependencies=[Depends(authorized)])
    def search(request: SearchRequest):
        if not request.query.strip():
            raise HTTPException(422, "query must contain non-whitespace text.")
        with capacity(search_slots):
            deadline = time.monotonic() + settings.search_timeout_seconds
            rows = store.snapshot(request.user_id)
            if not rows:
                return {"data": []}
            query = request.query
            if request.options:
                query += "\nOptions:\n" + "\n".join(request.options)
            vector = normalize_vectors(
                encoder.embed([query], deadline), 1, settings.embedding.dimensions
            )[0]
            result = retrieve(rows, query, vector, request.top_k)
            if time.monotonic() >= deadline:
                raise EmbeddingError("Search exceeded the service deadline.")
            return {"data": result}

    return app
