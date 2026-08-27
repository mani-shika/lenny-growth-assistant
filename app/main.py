"""FastAPI application entrypoint.

Startup is written to be **survivable**. A missing database, an empty index or
an unreachable Ollama must not stop the process from booting, because a server
that refuses to start gives an operator nothing to debug with. Instead the app
comes up, logs precisely what is wrong, and reports it on `/api/health` - so
the first thing a client engineer does when something breaks is curl one URL
and read the `checks` array.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.routes import router
from app.core.config import settings
from app.core.errors import AppError, ValidationFailed
from app.core.logging import (
    configure_logging,
    get_logger,
    new_request_id,
    set_request_id,
)
from app.db.session import dispose_engine, get_sessionmaker, init_db
from app.rag.retriever import retriever

configure_logging()
log = get_logger(__name__)

FRONTEND_DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    log.info(
        "app.starting",
        version=__version__,
        provider=settings.llm_provider,
        model=settings.model_for(settings.llm_provider),
        fallback_chain=list(settings.fallback_chain),
    )
    try:
        await init_db()
        async with get_sessionmaker()() as session:
            loaded = await retriever.load(session)
        if loaded == 0:
            log.warning(
                "app.corpus_empty",
                hint="run `python scripts/ingest.py` to build the index",
            )
        else:
            log.info("app.corpus_ready", chunks=loaded)
    except Exception as exc:  # noqa: BLE001 - boot must not be fatal
        log.error(
            "app.startup_degraded",
            error=str(exc)[:400],
            hint="the API is up; see GET /api/health for what is broken",
        )

    yield

    await dispose_engine()
    log.info("app.stopped")


app = FastAPI(
    title="The Lenny Growth Assistant",
    version=__version__,
    description=(
        "A grounded internal assistant over Lenny's Podcast and Newsletter "
        "transcripts: cited answers, Ship 30 for 30 essays, and rendered artifacts."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Tag every request with an id, log one line per request, echo the id back.

    The id appears in every log line emitted while handling the request and in
    any error body, so a user can paste an error into a ticket and an engineer
    can find the exact trace.
    """
    request_id = request.headers.get("x-request-id") or new_request_id()
    set_request_id(request_id)
    started = time.perf_counter()

    response = await call_next(request)

    duration = round((time.perf_counter() - started) * 1000, 2)
    response.headers["x-request-id"] = request_id
    # Health polling every few seconds would drown the log otherwise.
    if not request.url.path.startswith("/api/health"):
        log.info(
            "http.request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=duration,
        )
    return response


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    from app.core.logging import get_request_id

    request_id = get_request_id()
    log.warning(
        "http.app_error",
        code=exc.code,
        path=request.url.path,
        message=exc.message[:300],
        details=exc.details,
    )
    return JSONResponse(status_code=exc.status_code, content=exc.to_payload(request_id))


@app.exception_handler(RequestValidationError)
async def validation_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Turn a validation failure into the standard error envelope.

    Pydantic's raw error dicts embed the originating exception object under
    `ctx`, which is not JSON-serialisable - returning them verbatim turns a
    422 into a 500 and hides the actual problem from the caller. So we project
    each error onto the three fields a client can act on.
    """
    from app.core.logging import get_request_id

    fields = [
        {
            "field": ".".join(str(part) for part in err.get("loc", ())),
            "message": str(err.get("msg", "")),
            "type": str(err.get("type", "")),
        }
        for err in exc.errors()
    ]
    error = ValidationFailed("Request body failed validation.", fields=fields)
    return JSONResponse(status_code=422, content=error.to_payload(get_request_id()))


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last resort. Log the trace, return an id - never leak internals."""
    from app.core.logging import get_request_id

    request_id = get_request_id()
    log.error(
        "http.unhandled",
        path=request.url.path,
        error_type=type(exc).__name__,
        error=str(exc)[:500],
        exc_info=True,
    )
    return JSONResponse(
        status_code=500,
        content=AppError("An unexpected error occurred.").to_payload(request_id),
    )


app.include_router(router)


# The built SPA is served by the API in the container, so the whole product is
# one origin and one port. In development the Vite dev server proxies to /api
# instead, and this block simply does not apply.
if FRONTEND_DIST.is_dir():
    app.mount(
        "/assets",
        StaticFiles(directory=FRONTEND_DIST / "assets"),
        name="assets",
    )

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> FileResponse:
        candidate = FRONTEND_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")

else:

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {
            "name": "The Lenny Growth Assistant",
            "version": __version__,
            "docs": "/docs",
            "health": "/api/health",
            "note": (
                "Frontend bundle not found. Run `npm --prefix frontend run build`, "
                "or use the Vite dev server on :5173."
            ),
        }
