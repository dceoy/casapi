"""FastAPI application for the stateless Claude Agent API."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import aclosing, suppress
from typing import TYPE_CHECKING, cast

from claude_agent_sdk import ResultError, ResultMessage
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from casapi.agent import (
    AgentRunner,
    SdkAgentRunner,
    build_options,
    collect_result,
    message_to_dict,
)
from casapi.config import Settings
from casapi.schemas import InfoResponse, QueryRequest, QueryResponse
from casapi.security import auth_dependency, check_permission_mode, resolve_cwd

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator

    from claude_agent_sdk import ClaudeAgentOptions, Message

logger = logging.getLogger(__name__)

VERSION = "0.1.0"

Auth = Depends(auth_dependency)


async def _prepare(
    request: Request, body: QueryRequest
) -> tuple[ClaudeAgentOptions, float, asyncio.Semaphore]:
    """Validate a request and acquire a concurrency slot.

    Returns:
        SDK options, effective timeout, and the semaphore (already acquired).

    Raises:
        HTTPException: On validation, permission, or capacity errors.
    """
    settings: Settings = request.app.state.settings
    cwd = resolve_cwd(settings, body.cwd)
    check_permission_mode(settings, body.permission_mode)
    timeout = (
        settings.max_timeout_seconds
        if body.timeout_seconds is None
        else body.timeout_seconds
    )
    if timeout > settings.max_timeout_seconds:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"timeout_seconds exceeds server maximum {settings.max_timeout_seconds}",
        )
    options = build_options(body, settings, cwd)
    semaphore: asyncio.Semaphore = request.app.state.semaphore
    if semaphore.locked():
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "agent concurrency limit reached"
        )
    await semaphore.acquire()  # immediate: not locked, so no suspension
    return options, timeout, semaphore


def _error_event(detail: str) -> str:
    return f"data: {json.dumps({'type': 'error', 'detail': detail})}\n\n"


async def _stream_events(stream: AsyncIterator[Message]) -> AsyncGenerator[str]:
    """Serialize SDK messages and close the stream.

    Yields:
        Serialized SSE events.

    Raises:
        ResultError: If no result was received.
    """
    seen_result = False
    async with aclosing(cast("AsyncGenerator[Message]", stream)) as messages:
        try:
            async for message in messages:
                if isinstance(message, ResultMessage):
                    seen_result = True
                yield f"data: {json.dumps(message_to_dict(message))}\n\n"  # ruff: ignore[yield-in-context-manager-in-async-generator]  # closed by the pump
        except ResultError:
            if not seen_result:
                raise
    if not seen_result:
        yield _error_event("agent stream ended without a result message")


async def _events(
    agent_runner: AgentRunner,
    prompt: str,
    options: ClaudeAgentOptions,
    run_timeout: float,
    semaphore: asyncio.Semaphore,
) -> AsyncIterator[str]:
    """Pump events with a deadline independent of response consumption.

    Yields:
        Serialized SSE events.
    """
    queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=1)
    started = False

    async def pump() -> None:
        nonlocal started
        started = True
        terminal: str | None = None
        try:
            async with asyncio.timeout(run_timeout):
                stream = agent_runner.stream(prompt, options)
                async with aclosing(_stream_events(stream)) as serialized:
                    async for event in serialized:
                        await queue.put(event)
        except TimeoutError:
            terminal = _error_event("agent run timed out")
        except Exception:
            logger.exception("agent execution failed")
            terminal = _error_event("agent execution failed")
        finally:
            semaphore.release()
        if terminal is not None:
            await queue.put(terminal)
        await queue.put(None)

    task = asyncio.create_task(pump())
    try:
        while (event := await queue.get()) is not None:
            yield event
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        if not started:
            semaphore.release()


def _add_meta_routes(app: FastAPI, settings: Settings) -> None:
    """Register the health and info routes."""

    @app.get("/health")
    async def health() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        """Report API process readiness.

        Returns:
            Status for the running HTTP server.
        """
        return {"status": "ok"}

    @app.get("/v1/info", dependencies=[Auth])
    async def info() -> InfoResponse:  # pyright: ignore[reportUnusedFunction]
        """Describe server defaults.

        Returns:
            Server capabilities.
        """
        return InfoResponse(
            version=VERSION,
            stateless=True,
            default_model=settings.default_model,
            workspace_root=str(settings.workspace_root),
            default_permission_mode=settings.permission_mode,
            default_allowed_tools=list(settings.allowed_tools),
            max_concurrency=settings.max_concurrency,
            auth_required=settings.api_key is not None,
        )

    _ = (health, info)


def _add_query_routes(app: FastAPI) -> None:
    """Register the agent execution routes."""

    @app.post("/v1/query", dependencies=[Auth])
    async def run_query(  # pyright: ignore[reportUnusedFunction]
        body: QueryRequest, request: Request
    ) -> QueryResponse:
        """Run one ephemeral agent query and return the transcript.

        Args:
            body: Query request.
            request: Incoming request.

        Returns:
            Final result and message transcript.

        Raises:
            HTTPException: 502 on agent failure, 504 on timeout.
        """
        options, timeout, semaphore = await _prepare(request, body)
        agent_runner: AgentRunner = request.app.state.runner
        try:
            async with asyncio.timeout(timeout):
                return await collect_result(agent_runner.stream(body.prompt, options))
        except TimeoutError as exc:
            raise HTTPException(
                status.HTTP_504_GATEWAY_TIMEOUT, "agent run timed out"
            ) from exc
        except Exception as exc:
            logger.exception("agent execution failed")
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, "agent execution failed"
            ) from exc
        finally:
            semaphore.release()

    @app.post("/v1/query/stream", dependencies=[Auth])
    async def run_query_stream(  # pyright: ignore[reportUnusedFunction]
        body: QueryRequest, request: Request
    ) -> StreamingResponse:
        """Run one ephemeral agent query and stream messages as SSE.

        Args:
            body: Query request.
            request: Incoming request.

        Returns:
            Server-sent events, one per SDK message.
        """
        options, timeout, semaphore = await _prepare(request, body)
        agent_runner: AgentRunner = request.app.state.runner

        return StreamingResponse(
            _events(agent_runner, body.prompt, options, timeout, semaphore),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    _ = (run_query, run_query_stream)


def create_app(
    settings: Settings | None = None, runner: AgentRunner | None = None
) -> FastAPI:
    """Create the application.

    Args:
        settings: Server settings; read from the environment if omitted.
        runner: Agent runner; defaults to the real SDK runner.

    Returns:
        Configured FastAPI app.
    """
    settings = settings or Settings.from_env()
    app = FastAPI(
        title="casapi",
        description="Stateless HTTP API for remote Claude Code execution.",
        version=VERSION,
    )
    app.state.settings = settings
    app.state.runner = runner or SdkAgentRunner()
    app.state.semaphore = asyncio.Semaphore(settings.max_concurrency)
    _add_meta_routes(app, settings)
    _add_query_routes(app)
    return app


app = create_app()
