"""FastAPI application for the stateless Claude Agent API."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import aclosing, suppress
from typing import TYPE_CHECKING, cast

import anyio
from claude_agent_sdk import ResultError, ResultMessage
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import Response, StreamingResponse

from casapi.agent import (
    AgentExecutionError,
    AgentRunner,
    SdkAgentRunner,
    build_options,
    collect_result,
    message_to_dict,
)
from casapi.config import Settings
from casapi.sandbox import SandboxSetupError, service_home
from casapi.schemas import InfoResponse, QueryRequest, QueryResponse
from casapi.security import (
    auth_dependency,
    check_permission_mode,
    check_sandbox_cwd,
    check_sandbox_request,
    resolve_cwd,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator

    from claude_agent_sdk import ClaudeAgentOptions, Message
    from starlette.types import Receive, Scope, Send

logger = logging.getLogger(__name__)

VERSION = "0.3.0"
DISCONNECT_POLL_INTERVAL = 0.1
HEARTBEAT_INTERVAL = 15.0

# Keep detached cleanup alive if the response task is externally cancelled.
_pump_tasks: set[asyncio.Task[None]] = set()

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
    check_sandbox_request(settings, body.sandbox, body.permission_mode)
    home = None
    if body.sandbox:
        try:
            home = service_home()
        except SandboxSetupError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        check_sandbox_cwd(cwd, home)
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
    semaphore: asyncio.Semaphore = request.app.state.semaphore
    if semaphore.locked():
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "agent concurrency limit reached"
        )
    await semaphore.acquire()  # immediate: not locked, so no suspension
    try:
        options = build_options(body, settings, cwd, home)
    except BaseException as exc:
        semaphore.release()
        if isinstance(exc, SandboxSetupError):
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        raise
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


class _SlotLease:
    """Release an acquired semaphore slot at most once."""

    def __init__(self, semaphore: asyncio.Semaphore) -> None:
        self.semaphore = semaphore
        self.released = False
        self.claimed = False

    def release(self) -> None:
        """Return the slot if it is still held."""
        if not self.released:
            self.released = True
            self.semaphore.release()


async def _events(  # ruff: ignore[complex-structure]  # pump and consumer share lifecycle
    agent_runner: AgentRunner,
    prompt: str,
    options: ClaudeAgentOptions,
    run_timeout: float,
    lease: _SlotLease,
) -> AsyncIterator[str]:
    """Pump events with a deadline independent of response consumption.

    Yields:
        Serialized SSE events.
    """
    lease.claimed = True
    queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=1)
    started = False
    closing = False

    async def pump() -> None:
        nonlocal closing, started
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
            lease.release()
        if closing:
            return
        if terminal is not None:
            await queue.put(terminal)
        await queue.put(None)

    task = asyncio.create_task(pump())
    _pump_tasks.add(task)
    task.add_done_callback(_pump_tasks.discard)
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), HEARTBEAT_INTERVAL)
            except TimeoutError:
                yield ": heartbeat\n\n"
                continue
            if event is None:
                break
            yield event
    finally:
        closing = True
        task.cancel()
        with anyio.CancelScope(shield=True), suppress(asyncio.CancelledError):
            await asyncio.shield(task)
        if not started:
            lease.release()


class _ClosingStreamingResponse(StreamingResponse):
    """Close the event generator even when sending the response is cancelled."""

    def __init__(self, content: AsyncIterator[str], *, lease: _SlotLease) -> None:
        super().__init__(
            content,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
        self.lease = lease

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Run the response and await generator cleanup on every exit."""
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                with anyio.CancelScope(shield=True):
                    await cast("AsyncGenerator[str]", self.body_iterator).aclose()
            finally:
                if not self.lease.claimed:
                    self.lease.release()


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
            auth_required=bool(settings.api_key and settings.api_key.strip()),
            sandbox_mode=settings.sandbox_mode,
            sandbox_allowed_domains=list(settings.sandbox_allowed_domains),
        )

    _ = (health, info)


def _add_query_routes(app: FastAPI) -> None:
    """Register the agent execution routes."""

    @app.post("/v1/query", dependencies=[Auth], response_model=QueryResponse)
    async def run_query(  # pyright: ignore[reportUnusedFunction]
        body: QueryRequest, request: Request
    ) -> QueryResponse | Response:
        """Run one ephemeral agent query and return the transcript.

        Args:
            body: Query request.
            request: Incoming request.

        Returns:
            Final result and message transcript.

        Raises:
            HTTPException: 502 on agent failure, 504 on timeout.
            asyncio.CancelledError: If cancellation is not solely due to disconnect.
        """
        options, timeout, semaphore = await _prepare(request, body)
        agent_runner: AgentRunner = request.app.state.runner
        task = cast("asyncio.Task[object]", asyncio.current_task())

        client_gone = False

        async def watch_disconnect() -> None:
            nonlocal client_gone
            while not await request.is_disconnected():  # ruff: ignore[async-busy-wait]  # ASGI disconnect polling
                await asyncio.sleep(DISCONNECT_POLL_INTERVAL)
            logger.info("client disconnected; cancelling agent run")
            client_gone = True
            task.cancel()

        watcher = asyncio.create_task(watch_disconnect())
        try:
            async with asyncio.timeout(timeout):
                stream = agent_runner.stream(body.prompt, options)
                async with aclosing(
                    cast("AsyncGenerator[Message]", stream)
                ) as messages:
                    return await collect_result(
                        messages, include_messages=body.include_messages
                    )
        except asyncio.CancelledError:
            if client_gone and task.uncancel() == 0:
                return Response(status_code=499)
            raise
        except TimeoutError as exc:
            raise HTTPException(
                status.HTTP_504_GATEWAY_TIMEOUT, "agent run timed out"
            ) from exc
        except AgentExecutionError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
        except Exception as exc:
            logger.exception("agent execution failed")
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, "agent execution failed"
            ) from exc
        finally:
            watcher.cancel()
            try:
                with suppress(asyncio.CancelledError):
                    await watcher
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

        lease = _SlotLease(semaphore)
        return _ClosingStreamingResponse(
            _events(agent_runner, body.prompt, options, timeout, lease),
            lease=lease,
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
