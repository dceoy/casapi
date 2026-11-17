"""Tests for the stateless agent API."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx
import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    Message,
    ResultError,
    ResultMessage,
    ServerToolUseBlock,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from fastapi import Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from casapi.agent import (
    AgentExecutionError,
    AgentRunner,
    SdkAgentRunner,
    block_to_dict,
    build_options,
    collect_result,
    json_safe,
    message_to_dict,
)
from casapi.config import Settings
from casapi.main import _events, app, create_app  # pyright: ignore[reportPrivateUsage]
from casapi.schemas import QueryRequest

if TYPE_CHECKING:
    from collections.abc import (
        AsyncGenerator,
        AsyncIterator,
        Awaitable,
        Callable,
        Coroutine,
    )

    from fastapi.responses import StreamingResponse


def canned() -> list[Message]:
    """Build a canned message transcript.

    Returns:
        SDK messages.
    """
    return [
        SystemMessage(subtype="init", data={"a": 1}),
        AssistantMessage(
            content=[
                ThinkingBlock(thinking="hmm", signature="sig"),
                TextBlock(text="hello"),
                ToolUseBlock(id="t1", name="Bash", input={"command": "ls"}),
            ],
            model="m",
            session_id="s1",
        ),
        UserMessage(
            content=[ToolResultBlock(tool_use_id="t1", content="out", is_error=False)]
        ),
        UserMessage(content="plain"),
        ResultMessage(
            subtype="success",
            duration_ms=10,
            duration_api_ms=5,
            is_error=False,
            num_turns=2,
            session_id="s1",
            total_cost_usd=0.01,
            result="done",
        ),
    ]


class FakeRunner:
    """Runner yielding canned messages, optionally failing or sleeping."""

    def __init__(
        self,
        messages: list[Message] | None = None,
        *,
        fail_after: int | None = None,
        delay: float = 0.0,
    ) -> None:
        """Initialize the fake.

        Args:
            messages: Messages to yield.
            fail_after: Raise after yielding this many messages.
            delay: Seconds to sleep before yielding.
        """
        self.messages = canned() if messages is None else messages
        self.fail_after = fail_after
        self.delay = delay
        self.options: ClaudeAgentOptions | None = None

    async def stream(
        self, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[Message]:
        """Yield canned messages.

        Args:
            prompt: Ignored.
            options: Captured for assertions.

        Yields:
            Canned messages.

        Raises:
            RuntimeError: When configured to fail.
        """
        assert prompt
        self.options = options
        await asyncio.sleep(self.delay)
        for i, message in enumerate(self.messages):
            if self.fail_after is not None and i >= self.fail_after:
                msg = "boom"
                raise RuntimeError(msg)
            yield message


def make_settings(tmp_path: Path, **kwargs: Any) -> Settings:  # ruff: ignore[any-type]
    """Build test settings rooted in tmp_path.

    Args:
        tmp_path: Workspace root.
        **kwargs: Overrides.

    Returns:
        Settings.
    """
    base = Settings(
        api_key="secret", workspace_root=tmp_path.resolve(), max_concurrency=2
    )
    return replace(base, **kwargs)


def client_for(
    settings: Settings, runner: FakeRunner | None = None
) -> tuple[TestClient, FakeRunner]:
    """Create a client with a fake runner.

    Args:
        settings: Settings.
        runner: Optional runner.

    Returns:
        Client and runner.
    """
    runner = runner or FakeRunner()
    return TestClient(create_app(settings, runner)), runner


AUTH = {"Authorization": "Bearer secret"}


def test_health() -> None:
    """Verify the public health endpoint."""
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_query_success(tmp_path: Path) -> None:
    """Verify a successful query and transcript serialization."""
    client, runner = client_for(make_settings(tmp_path))
    response = client.post("/v1/query", json={"prompt": "hi"}, headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["result"] == "done"
    assert body["session_id"] == "s1"
    assert body["total_cost_usd"] == pytest.approx(0.01)
    assert body["is_error"] is False
    types = [m["type"] for m in body["messages"]]
    assert types == ["system", "assistant", "user", "user", "result"]
    blocks = body["messages"][1]["content"]
    assert [b["type"] for b in blocks] == ["thinking", "text", "tool_use"]
    assert blocks[2]["input"] == {"command": "ls"}
    assert body["messages"][2]["content"][0]["type"] == "tool_result"
    assert body["messages"][3]["content"] == "plain"
    assert runner.options is not None
    assert runner.options.cwd == str(tmp_path.resolve())
    assert runner.options.permission_mode == "acceptEdits"


def test_query_agent_error_is_200(tmp_path: Path) -> None:
    """Verify agent-level errors stay HTTP 200."""
    result = ResultMessage(
        subtype="error_max_turns",
        duration_ms=1,
        duration_api_ms=1,
        is_error=True,
        num_turns=1,
        session_id="s",
    )
    client, _ = client_for(make_settings(tmp_path), FakeRunner([result]))
    response = client.post("/v1/query", json={"prompt": "hi"}, headers=AUTH)
    assert response.status_code == 200
    assert response.json()["is_error"] is True


def test_auth_matrix(tmp_path: Path) -> None:
    """Verify the authentication matrix."""
    disabled = make_settings(tmp_path, api_key=None)
    client, _ = client_for(disabled)
    assert client.post("/v1/query", json={"prompt": "x"}).status_code == 503
    assert client.get("/v1/info").status_code == 503

    for blank in ("", "   "):
        settings = replace(make_settings(tmp_path), api_key=blank)
        client, _ = client_for(settings)
        assert client.post("/v1/query", json={"prompt": "x"}).status_code == 503

    open_settings = make_settings(tmp_path, api_key=None, allow_unauthenticated=True)
    client, _ = client_for(open_settings)
    assert client.post("/v1/query", json={"prompt": "x"}).status_code == 200

    client, _ = client_for(make_settings(tmp_path))
    assert client.post("/v1/query", json={"prompt": "x"}).status_code == 401
    wrong = {"Authorization": "Bearer nope"}
    assert (
        client.post("/v1/query", json={"prompt": "x"}, headers=wrong).status_code == 401
    )
    assert (
        client.post("/v1/query", json={"prompt": "x"}, headers=AUTH).status_code == 200
    )


def test_cwd(tmp_path: Path) -> None:
    """Verify working-directory confinement."""
    (tmp_path / "sub").mkdir()
    (tmp_path / "file.txt").write_text("x")
    client, runner = client_for(make_settings(tmp_path))

    def post(cwd: str) -> int:
        return client.post(
            "/v1/query", json={"prompt": "x", "cwd": cwd}, headers=AUTH
        ).status_code

    assert post("/") == 403
    assert post("../outside") == 403
    assert post("missing") == 400
    assert post("file.txt") == 400
    assert post("sub") == 200
    assert runner.options is not None
    assert runner.options.cwd == str((tmp_path / "sub").resolve())
    assert post(str(tmp_path / "sub")) == 200


def test_cwd_symlinks_and_malformed(tmp_path: Path) -> None:
    """Verify symlink confinement and malformed path handling."""
    (tmp_path / "inner").mkdir()
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    (tmp_path / "escape").symlink_to(outside, target_is_directory=True)
    (tmp_path / "alias").symlink_to(tmp_path / "inner", target_is_directory=True)
    (tmp_path / "loop-a").symlink_to(tmp_path / "loop-b")
    (tmp_path / "loop-b").symlink_to(tmp_path / "loop-a")
    client, _ = client_for(make_settings(tmp_path))

    def post(cwd: str) -> int:
        return client.post(
            "/v1/query", json={"prompt": "x", "cwd": cwd}, headers=AUTH
        ).status_code

    assert post("escape") == 403
    assert post("alias") == 200
    assert post("loop-a") == 400
    assert post("bad\0path") == 400


def test_cwd_containment_precedes_directory_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject outside paths before consulting the filesystem for directory status."""
    client, _ = client_for(make_settings(tmp_path))

    def fail_is_dir(_self: Path) -> bool:
        raise OSError

    monkeypatch.setattr(Path, "is_dir", fail_is_dir)
    response = client.post(
        "/v1/query", json={"prompt": "x", "cwd": "../outside"}, headers=AUTH
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "cwd is outside the workspace root"}
    response = client.post("/v1/query", json={"prompt": "x", "cwd": "."}, headers=AUTH)
    assert response.status_code == 400
    assert response.json() == {"detail": "cwd is not a valid path"}


def test_bypass_permissions(tmp_path: Path) -> None:
    """Verify bypassPermissions is opt-in."""
    payload = {"prompt": "x", "permission_mode": "bypassPermissions"}
    client, _ = client_for(make_settings(tmp_path))
    assert client.post("/v1/query", json=payload, headers=AUTH).status_code == 403
    client, _ = client_for(make_settings(tmp_path, allow_bypass_permissions=True))
    assert client.post("/v1/query", json=payload, headers=AUTH).status_code == 200


def test_validation(tmp_path: Path) -> None:
    """Verify request validation."""
    client, _ = client_for(make_settings(tmp_path, max_timeout_seconds=10))
    assert (
        client.post("/v1/query", json={"prompt": ""}, headers=AUTH).status_code == 422
    )
    stateful = {"prompt": "x", "resume": "abc"}
    assert client.post("/v1/query", json=stateful, headers=AUTH).status_code == 422
    too_long = {"prompt": "x", "timeout_seconds": 11}
    assert client.post("/v1/query", json=too_long, headers=AUTH).status_code == 400


def test_runner_failures(tmp_path: Path) -> None:
    """Verify 502 and 504 mappings."""
    settings = make_settings(tmp_path)
    client, _ = client_for(settings, FakeRunner(fail_after=1))
    response = client.post("/v1/query", json={"prompt": "x"}, headers=AUTH)
    assert response.status_code == 502
    assert response.json() == {"detail": "agent execution failed"}

    no_result = FakeRunner(canned()[:2])
    client, _ = client_for(settings, no_result)
    assert (
        client.post("/v1/query", json={"prompt": "x"}, headers=AUTH).status_code == 502
    )

    client, _ = client_for(settings, FakeRunner(delay=1.0))
    slow = {"prompt": "x", "timeout_seconds": 0.05}
    assert client.post("/v1/query", json=slow, headers=AUTH).status_code == 504


def test_concurrency_limit(tmp_path: Path) -> None:
    """Verify 429 when no capacity is available."""
    client, _ = client_for(make_settings(tmp_path, max_concurrency=0))
    assert (
        client.post("/v1/query", json={"prompt": "x"}, headers=AUTH).status_code == 429
    )
    assert (
        client.post("/v1/query/stream", json={"prompt": "x"}, headers=AUTH).status_code
        == 429
    )


def test_semaphore_released(tmp_path: Path) -> None:
    """Verify slots are released after success and failure."""
    client, _ = client_for(make_settings(tmp_path, max_concurrency=1))
    for _ in range(2):
        assert (
            client.post("/v1/query", json={"prompt": "x"}, headers=AUTH).status_code
            == 200
        )
    client, _ = client_for(
        make_settings(tmp_path, max_concurrency=1), FakeRunner(fail_after=0)
    )
    for _ in range(2):
        assert (
            client.post("/v1/query", json={"prompt": "x"}, headers=AUTH).status_code
            == 502
        )


def parse_sse(text: str) -> list[dict[str, Any]]:
    """Parse SSE data lines.

    Args:
        text: Response body.

    Returns:
        Decoded events.
    """
    return [
        json.loads(line.removeprefix("data: "))
        for line in text.splitlines()
        if line.startswith("data: ")
    ]


def test_stream_success(tmp_path: Path) -> None:
    """Verify SSE output."""
    client, _ = client_for(make_settings(tmp_path))
    response = client.post("/v1/query/stream", json={"prompt": "x"}, headers=AUTH)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    events = parse_sse(response.text)
    assert [e["type"] for e in events] == [
        "system",
        "assistant",
        "user",
        "user",
        "result",
    ]


def test_stream_errors(tmp_path: Path) -> None:
    """Verify stream errors and auth."""
    settings = make_settings(tmp_path)
    client, _ = client_for(settings, FakeRunner(fail_after=2))
    response = client.post("/v1/query/stream", json={"prompt": "x"}, headers=AUTH)
    events = parse_sse(response.text)
    assert [e["type"] for e in events] == ["system", "assistant", "error"]
    assert events[-1]["detail"] == "agent execution failed"

    client, _ = client_for(settings, FakeRunner(delay=1.0))
    slow = {"prompt": "x", "timeout_seconds": 0.05}
    events = parse_sse(client.post("/v1/query/stream", json=slow, headers=AUTH).text)
    assert events == [{"type": "error", "detail": "agent run timed out"}]

    assert client.post("/v1/query/stream", json={"prompt": "x"}).status_code == 401


def test_stream_without_result(tmp_path: Path) -> None:
    """Verify a stream ending without a result emits an error event."""
    client, _ = client_for(make_settings(tmp_path), FakeRunner(canned()[:1]))
    response = client.post("/v1/query/stream", json={"prompt": "x"}, headers=AUTH)
    events = parse_sse(response.text)
    assert [e["type"] for e in events] == ["system", "error"]
    assert "without a result" in events[-1]["detail"]


def test_stream_releases_slot(tmp_path: Path) -> None:
    """Verify a finished stream releases its concurrency slot."""
    client, _ = client_for(make_settings(tmp_path, max_concurrency=1))
    for _ in range(2):
        response = client.post("/v1/query/stream", json={"prompt": "x"}, headers=AUTH)
        assert response.status_code == 200


class BlockingRunner:
    """Runner that blocks mid-stream until released."""

    def __init__(self) -> None:
        """Initialize the events and capture slot."""
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.options: ClaudeAgentOptions | None = None

    async def stream(
        self, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[Message]:
        """Yield one message, block, then yield the result.

        Args:
            prompt: Ignored beyond a non-empty check.
            options: Captured for completeness.

        Yields:
            The first and last canned messages.
        """
        assert prompt
        self.options = options
        messages = canned()
        yield messages[0]
        self.started.set()
        await self.release.wait()
        yield messages[-1]


@pytest.mark.asyncio
async def test_concurrency_in_flight(tmp_path: Path) -> None:
    """Verify a second request gets 429 while a run holds the only slot."""
    runner = BlockingRunner()
    test_app = create_app(make_settings(tmp_path, max_concurrency=1), runner)
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = asyncio.create_task(
            client.post("/v1/query", json={"prompt": "x"}, headers=AUTH)
        )
        await runner.started.wait()
        second = await client.post("/v1/query", json={"prompt": "x"}, headers=AUTH)
        assert second.status_code == 429
        runner.release.set()
        assert (await first).status_code == 200


def test_info(tmp_path: Path) -> None:
    """Verify the info endpoint."""
    client, _ = client_for(make_settings(tmp_path, default_model="m1"))
    body = client.get("/v1/info", headers=AUTH).json()
    assert body == {
        "version": "0.1.0",
        "stateless": True,
        "default_model": "m1",
        "workspace_root": str(tmp_path.resolve()),
        "default_permission_mode": "acceptEdits",
        "default_allowed_tools": ["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        "max_concurrency": 2,
        "auth_required": True,
    }


def test_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify default settings."""
    for name in (
        "CASAPI_API_KEY",
        "CASAPI_ALLOW_UNAUTHENTICATED",
        "CASAPI_WORKSPACE_ROOT",
        "CASAPI_DEFAULT_MODEL",
        "CASAPI_PERMISSION_MODE",
        "CASAPI_ALLOWED_TOOLS",
        "CASAPI_ALLOW_BYPASS_PERMISSIONS",
        "CASAPI_MAX_CONCURRENCY",
        "CASAPI_MAX_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings.from_env()
    assert settings.api_key is None
    assert settings.allow_unauthenticated is False
    assert settings.workspace_root == Path.cwd().resolve()
    assert settings.default_model is None
    assert settings.permission_mode == "acceptEdits"
    assert settings.allowed_tools == ("Read", "Write", "Edit", "Bash", "Glob", "Grep")
    assert settings.allow_bypass_permissions is False
    assert settings.max_concurrency == 4
    assert settings.max_timeout_seconds == pytest.approx(3600.0)
    assert Settings().workspace_root == Path.cwd().resolve()


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Verify explicit environment parsing."""
    monkeypatch.setenv("CASAPI_API_KEY", "k")
    monkeypatch.setenv("CASAPI_ALLOW_UNAUTHENTICATED", "TRUE")
    monkeypatch.setenv("CASAPI_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("CASAPI_DEFAULT_MODEL", "mm")
    monkeypatch.setenv("CASAPI_PERMISSION_MODE", "plan")
    monkeypatch.setenv("CASAPI_ALLOWED_TOOLS", " Read , Bash,,")
    monkeypatch.setenv("CASAPI_ALLOW_BYPASS_PERMISSIONS", "off")
    monkeypatch.setenv("CASAPI_MAX_CONCURRENCY", "7")
    monkeypatch.setenv("CASAPI_MAX_TIMEOUT_SECONDS", "12.5")
    settings = Settings.from_env()
    assert settings.api_key == "k"
    assert settings.allow_unauthenticated is True
    assert settings.workspace_root == tmp_path.resolve()
    assert settings.default_model == "mm"
    assert settings.permission_mode == "plan"
    assert settings.allowed_tools == ("Read", "Bash")
    assert settings.allow_bypass_permissions is False
    assert settings.max_concurrency == 7
    assert settings.max_timeout_seconds == pytest.approx(12.5)


def test_settings_api_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify an empty or blank CASAPI_API_KEY counts as unconfigured."""
    monkeypatch.setenv("CASAPI_API_KEY", "")
    assert Settings.from_env().api_key is None
    monkeypatch.setenv("CASAPI_API_KEY", "   ")
    assert Settings.from_env().api_key is None
    monkeypatch.setenv("CASAPI_API_KEY", "  secret  ")
    assert Settings.from_env().api_key == "secret"
    monkeypatch.delenv("CASAPI_API_KEY")
    assert Settings.from_env().api_key is None


def test_build_options(tmp_path: Path) -> None:
    """Verify defaults versus overrides."""
    settings = make_settings(tmp_path, default_model="dm")
    options = build_options(QueryRequest(prompt="x"), settings, tmp_path)
    assert options.model == "dm"
    assert options.permission_mode == "acceptEdits"
    assert options.allowed_tools == ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
    assert options.cwd == str(tmp_path)
    assert options.system_prompt == {"type": "preset", "preset": "claude_code"}
    assert options.setting_sources == []
    assert options.extra_args == {"no-session-persistence": None}
    assert options.env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert options.strict_mcp_config is True
    assert options.max_turns is None
    assert options.resume is None
    assert options.continue_conversation is False

    request = QueryRequest(
        prompt="x",
        model="o",
        system_prompt="sp",
        allowed_tools=["Read"],
        disallowed_tools=["Bash"],
        permission_mode="plan",
        max_turns=3,
        max_budget_usd=1.5,
        effort="low",
    )
    options = build_options(request, settings, tmp_path)
    assert options.model == "o"
    assert options.system_prompt == "sp"
    assert options.allowed_tools == ["Read"]
    assert options.disallowed_tools == ["Bash"]
    assert options.permission_mode == "plan"
    assert options.max_turns == 3
    assert options.max_budget_usd == pytest.approx(1.5)
    assert options.effort == "low"


@dataclass
class Inner:
    """Nested dataclass."""

    values: set[int]


class Weird:
    """Object with a custom string form."""

    def __str__(self) -> str:
        """Return a fixed string.

        Returns:
            The string.
        """
        return "weird"


def test_json_safe() -> None:
    """Verify recursive conversion and fallback."""
    value = {"a": (1, 2), "b": Inner({3}), "c": Weird(), 4: None, "d": [1.5, True]}
    assert json_safe(value) == {
        "a": [1, 2],
        "b": {"values": [3]},
        "c": "weird",
        "4": None,
        "d": [1.5, True],
    }
    assert json_safe(Inner) == str(Inner)


def test_generic_serialization() -> None:
    """Verify fallback serialization for unknown message and block types."""
    event = StreamEvent(uuid="u", session_id="s", event={"x": 1})
    assert message_to_dict(event) == {
        "type": "stream_event",
        "data": {
            "uuid": "u",
            "session_id": "s",
            "event": {"x": 1},
            "parent_tool_use_id": None,
        },
    }
    block = ServerToolUseBlock(id="i", name="web_search", input={"q": "z"})
    assert block_to_dict(block)["type"] == "server_tool_use_block"


def test_protocol_stub_is_noop() -> None:
    """Verify the AgentRunner protocol body is only a placeholder."""
    runner = cast("AgentRunner", object())
    result = AgentRunner.stream(runner, "p", ClaudeAgentOptions())  # pyright: ignore[reportAbstractUsage]
    assert result is None  # pyright: ignore[reportUnnecessaryComparison]


@pytest.mark.asyncio
async def test_collect_result_missing() -> None:
    """Verify missing ResultMessage raises."""
    with pytest.raises(AgentExecutionError):
        await collect_result(FakeRunner([]).stream("x", ClaudeAgentOptions()))


def test_sdk_runner_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the SDK runner calls claude_agent_sdk.query."""
    calls: list[tuple[str, ClaudeAgentOptions]] = []

    def fake_query(*, prompt: str, options: ClaudeAgentOptions) -> str:
        calls.append((prompt, options))
        return "stream"

    monkeypatch.setattr("casapi.agent.query", fake_query)
    options = ClaudeAgentOptions()
    assert SdkAgentRunner().stream("p", options) == "stream"  # pyright: ignore[reportUnnecessaryComparison]
    assert calls == [("p", options)]


class ResultErrorRunner(FakeRunner):
    """Runner that raises the SDK terminal error after its messages."""

    async def stream(
        self, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[Message]:
        """Yield configured messages and then fail as the SDK does."""
        async for message in super().stream(prompt, options):
            yield message
        msg = "terminal result error"
        raise ResultError(msg)


def test_result_error_responses(tmp_path: Path) -> None:
    """Preserve a terminal error result, but reject an absent result."""
    result = replace(
        cast("ResultMessage", canned()[-1]), is_error=True, subtype="error_max_turns"
    )
    messages = [canned()[0], result]
    client, _ = client_for(make_settings(tmp_path), ResultErrorRunner(messages))
    response = client.post("/v1/query", json={"prompt": "x"}, headers=AUTH)
    assert response.status_code == 200
    assert response.json()["is_error"] is True
    assert [m["type"] for m in response.json()["messages"]] == ["system", "result"]
    events = parse_sse(
        client.post("/v1/query/stream", json={"prompt": "x"}, headers=AUTH).text
    )
    assert [e["type"] for e in events] == ["system", "result"]
    assert events[-1]["is_error"] is True
    client, _ = client_for(make_settings(tmp_path), ResultErrorRunner([]))
    assert (
        client.post("/v1/query", json={"prompt": "x"}, headers=AUTH).status_code == 502
    )
    events = parse_sse(
        client.post("/v1/query/stream", json={"prompt": "x"}, headers=AUTH).text
    )
    assert events == [{"type": "error", "detail": "agent execution failed"}]


class CleanupRunner(BlockingRunner):
    """Record stream cleanup after cancellation or early closure."""

    def __init__(self) -> None:
        """Initialize cleanup tracking."""
        super().__init__()
        self.closed = asyncio.Event()

    async def stream(
        self, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[Message]:
        """Yield then wait until cancelled, recording cleanup."""
        try:
            async for message in super().stream(prompt, options):
                yield message
        finally:
            self.closed.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("early_close", [False, True])
async def test_stream_cleanup_with_stalled_consumer(
    tmp_path: Path, early_close: bool
) -> None:
    """Close the runner before releasing capacity on timeout or early close."""
    runner = CleanupRunner()
    test_app = create_app(make_settings(tmp_path, max_concurrency=1), runner)
    route = next(
        r
        for r in test_app.routes
        if isinstance(r, APIRoute) and r.path == "/v1/query/stream"
    )
    request = Request({"type": "http", "app": test_app})
    endpoint = cast("Callable[..., Awaitable[StreamingResponse]]", route.endpoint)
    response = await endpoint(QueryRequest(prompt="x", timeout_seconds=0.05), request)
    stream = cast("AsyncGenerator[str]", response.body_iterator)
    assert parse_sse(await anext(stream))[0]["type"] == "system"
    await asyncio.wait_for(runner.started.wait(), 1)
    assert test_app.state.semaphore.locked()
    if early_close:
        await stream.aclose()
    else:
        await asyncio.wait_for(runner.closed.wait(), 1)
        assert not test_app.state.semaphore.locked()
        assert parse_sse(await anext(stream)) == [
            {"type": "error", "detail": "agent run timed out"}
        ]
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    assert runner.closed.is_set()
    assert not test_app.state.semaphore.locked()


@pytest.mark.asyncio
async def test_stream_cancelled_before_pump_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Release capacity if the response is cancelled before the pump runs."""
    semaphore = asyncio.Semaphore(1)
    await semaphore.acquire()
    stream = _events(BlockingRunner(), "x", ClaudeAgentOptions(), 1, semaphore)
    create_task = asyncio.create_task

    def cancel_before_start(coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = create_task(coro)
        task.cancel()
        asyncio.get_running_loop().call_soon(
            cast("asyncio.Task[object]", asyncio.current_task()).cancel
        )
        return task

    monkeypatch.setattr(asyncio, "create_task", cancel_before_start)
    with pytest.raises(asyncio.CancelledError):
        await anext(stream)
    assert not semaphore.locked()
