"""Tests for the stateless agent API."""

from __future__ import annotations

import asyncio
import gc
import json
import os
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import anyio
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
from fastapi import HTTPException, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

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
from casapi.config import DEFAULT_SANDBOX_ALLOWED_DOMAINS, Settings
from casapi.main import _events, _SlotLease, app, create_app  # pyright: ignore[reportPrivateUsage]
from casapi.sandbox import (
    SandboxSetupError,
    build_sandbox_settings,
    bundled_cli_path,
    prepare_sandbox_cache,
    sandbox_cache_dir,
    sandbox_env,
    service_home,
)
from casapi.schemas import QueryRequest
from casapi.security import check_sandbox_request

if TYPE_CHECKING:
    from collections.abc import (
        AsyncGenerator,
        AsyncIterator,
        Awaitable,
        Callable,
        Coroutine,
        Iterator,
        MutableMapping,
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
        self.stream_calls = 0

    def stream(
        self, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[Message]:
        """Count the call itself, then return the message generator.

        Args:
            prompt: Forwarded.
            options: Forwarded.

        Returns:
            Async iterator of canned messages.
        """
        self.stream_calls += 1
        return self._generate(prompt, options)

    async def _generate(
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
    assert body["model"] == "m"
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


def test_query_without_messages(tmp_path: Path) -> None:
    """Result-only queries omit the transcript."""
    client, _ = client_for(make_settings(tmp_path))
    response = client.post(
        "/v1/query", json={"prompt": "hi", "include_messages": False}, headers=AUTH
    )
    assert response.status_code == 200
    body = response.json()
    assert body["result"] == "done"
    assert body["session_id"] == "s1"
    assert body["model"] == "m"
    assert body["messages"] == []


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
    bad_sandbox = {"prompt": "x", "sandbox": "maybe"}
    assert client.post("/v1/query", json=bad_sandbox, headers=AUTH).status_code == 422
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
    response = client.post("/v1/query", json={"prompt": "x"}, headers=AUTH)
    assert response.status_code == 502
    assert response.json() == {"detail": "agent stream ended without a result message"}

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
        "version": "0.3.0",
        "stateless": True,
        "default_model": "m1",
        "workspace_root": str(tmp_path.resolve()),
        "default_permission_mode": "acceptEdits",
        "default_allowed_tools": ["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        "max_concurrency": 2,
        "auth_required": True,
        "sandbox_mode": "off",
        "sandbox_allowed_domains": list(DEFAULT_SANDBOX_ALLOWED_DOMAINS),
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
        "CASAPI_SANDBOX_MODE",
        "CASAPI_SANDBOX_ALLOWED_DOMAINS",
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
    assert settings.sandbox_mode == "off"
    assert settings.sandbox_allowed_domains == DEFAULT_SANDBOX_ALLOWED_DOMAINS
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
    monkeypatch.setenv("CASAPI_SANDBOX_MODE", "opt-in")
    monkeypatch.setenv(
        "CASAPI_SANDBOX_ALLOWED_DOMAINS", " GitHub.com , *.example.org,,"
    )
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
    assert settings.sandbox_mode == "opt-in"
    assert settings.sandbox_allowed_domains == ("github.com", "*.example.org")


@pytest.mark.parametrize(
    "value", ["required", "bogus", "", "OFF", "Opt-In", " opt-in ", "off ", "OPT-IN"]
)
def test_settings_sandbox_mode_invalid(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Verify only the exact literals off/opt-in are accepted."""
    monkeypatch.setenv("CASAPI_SANDBOX_MODE", value)
    with pytest.raises(ValueError, match="CASAPI_SANDBOX_MODE"):
        Settings.from_env()


def test_settings_sandbox_mode_literals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify unset defaults to off and the exact literals parse."""
    monkeypatch.delenv("CASAPI_SANDBOX_MODE", raising=False)
    assert Settings.from_env().sandbox_mode == "off"
    monkeypatch.setenv("CASAPI_SANDBOX_MODE", "off")
    assert Settings.from_env().sandbox_mode == "off"
    monkeypatch.setenv("CASAPI_SANDBOX_MODE", "opt-in")
    assert Settings.from_env().sandbox_mode == "opt-in"


@pytest.mark.parametrize("value", ["", " , ", " ,, "])
def test_settings_sandbox_domains_empty(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Verify an empty domain list means no egress."""
    monkeypatch.setenv("CASAPI_SANDBOX_ALLOWED_DOMAINS", value)
    assert Settings.from_env().sandbox_allowed_domains == ()


def test_settings_sandbox_domains_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify lowercasing, whitespace trimming, empty entries, and punycode."""
    monkeypatch.setenv(
        "CASAPI_SANDBOX_ALLOWED_DOMAINS",
        " GitHub.com ,, *.Example.org,xn--bcher-kva.example,",
    )
    assert Settings.from_env().sandbox_allowed_domains == (
        "github.com",
        "*.example.org",
        "xn--bcher-kva.example",
    )
    label63 = "a" * 63
    host253 = ".".join(["a" * 63, "a" * 63, "a" * 63, "a" * 61])
    assert len(host253) == 253
    monkeypatch.setenv("CASAPI_SANDBOX_ALLOWED_DOMAINS", f"{label63}.com,{host253}")
    assert Settings.from_env().sandbox_allowed_domains == (f"{label63}.com", host253)


@pytest.mark.parametrize(
    "value",
    [
        "https://github.com",
        "github.com/x",
        "localhost",
        "*.com.*",  # embedded wildcard
        "a.*.com",  # embedded wildcard
        "*.*.example.com",  # repeated wildcard
        "*example.com",  # wildcard not a full label
        "a..com",  # empty DNS label
        ".a.com",  # leading empty label
        "a.com.",  # trailing dot
        "*.",  # wildcard only
        "-a.com",
        "a-.com",
        "bücher.example",  # raw Unicode (needs punycode)
        "\u212a.example",  # Kelvin sign U+212A would casefold to ASCII "k"
        "\u017f.example",  # long s U+017F would casefold to ASCII "s"
        "\u0130.example",  # dotted capital I
        "a" * 64 + ".com",  # label too long
        ".".join(["a" * 63, "a" * 63, "a" * 63, "a" * 62]),  # 254 chars
    ],
)
def test_settings_sandbox_domains_invalid(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Verify malformed domains fail at startup."""
    monkeypatch.setenv("CASAPI_SANDBOX_ALLOWED_DOMAINS", value)
    with pytest.raises(ValueError, match="invalid domain"):
        Settings.from_env()


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


@pytest.mark.asyncio
async def test_collect_result_without_assistant() -> None:
    """A result without an assistant message has no model."""
    response = await collect_result(
        FakeRunner([canned()[-1]]).stream("x", ClaudeAgentOptions()),
        include_messages=False,
    )
    assert response.model is None
    assert response.messages == []


@pytest.mark.asyncio
async def test_collect_result_last_assistant_model() -> None:
    """The last assistant message determines the response model."""
    messages = [AssistantMessage(content=[], model="first"), *canned()]
    response = await collect_result(
        FakeRunner(messages).stream("x", ClaudeAgentOptions())
    )
    assert response.model == "m"


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
async def test_slot_lease_release_is_idempotent() -> None:
    """Repeated release cannot inflate concurrency capacity."""
    semaphore = asyncio.Semaphore(1)
    await semaphore.acquire()
    lease = _SlotLease(semaphore)
    lease.release()
    lease.release()
    await semaphore.acquire()
    assert semaphore.locked()
    semaphore.release()


@pytest.mark.asyncio
async def test_stream_cancelled_before_pump_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Release capacity if the response is cancelled before the pump runs."""
    semaphore = asyncio.Semaphore(1)
    await semaphore.acquire()
    stream = _events(
        BlockingRunner(), "x", ClaudeAgentOptions(), 1, _SlotLease(semaphore)
    )
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


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_asgi_disconnect_waits_for_cleanup(
    tmp_path: Path, cleanup_fails: bool
) -> None:
    """Finish awaited cleanup on an ASGI 2.3 disconnect, even if it fails."""

    class DisconnectRunner(FakeRunner):
        closed = False

        async def stream(
            self, prompt: str, options: ClaudeAgentOptions
        ) -> AsyncIterator[Message]:
            assert prompt
            assert options
            try:
                yield canned()[0]
                await asyncio.Event().wait()
            finally:
                with anyio.CancelScope(shield=True):
                    await asyncio.sleep(0)
                    self.closed = True
                if cleanup_fails:
                    msg = "cleanup failed"
                    raise RuntimeError(msg)

    runner = DisconnectRunner()
    test_app = create_app(make_settings(tmp_path, max_concurrency=1), runner)
    first_event = asyncio.Event()
    request_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {
                "type": "http.request",
                "body": b'{"prompt":"x"}',
                "more_body": False,
            }
        await first_event.wait()
        return {"type": "http.disconnect"}

    async def send(message: MutableMapping[str, Any]) -> None:  # ruff: ignore[unused-async]
        if message["type"] == "http.response.body" and message.get("body"):
            first_event.set()

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/query/stream",
        "raw_path": b"/v1/query/stream",
        "query_string": b"",
        "headers": [
            (b"authorization", b"Bearer secret"),
            (b"content-type", b"application/json"),
        ],
        "server": ("testserver", 80),
        "client": ("testclient", 1234),
    }
    await asyncio.wait_for(test_app(scope, receive, send), 1)
    assert first_event.is_set()
    assert runner.closed
    assert not test_app.state.semaphore.locked()
    test_app.state.runner = FakeRunner()
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/query/stream", json={"prompt": "x"}, headers=AUTH
        )
    assert response.status_code == 200
    assert parse_sse(response.text)[-1]["type"] == "result"
    await test_app.state.semaphore.acquire()
    assert test_app.state.semaphore.locked()
    test_app.state.semaphore.release()


@pytest.mark.asyncio
async def test_asgi_external_cancel_holds_slot_until_cleanup(
    tmp_path: Path,
) -> None:
    """Keep capacity occupied when shutdown interrupts disconnect cleanup."""
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    class SlowCleanupRunner(FakeRunner):
        async def stream(
            self, prompt: str, options: ClaudeAgentOptions
        ) -> AsyncIterator[Message]:
            assert prompt
            assert options
            try:
                yield canned()[0]
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await allow_cleanup.wait()
                cleanup_finished.set()

    test_app = create_app(
        make_settings(tmp_path, max_concurrency=1), SlowCleanupRunner()
    )
    first_event = asyncio.Event()
    request_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {
                "type": "http.request",
                "body": b'{"prompt":"x"}',
                "more_body": False,
            }
        await first_event.wait()
        return {"type": "http.disconnect"}

    async def send(message: MutableMapping[str, Any]) -> None:  # ruff: ignore[unused-async]
        if message["type"] == "http.response.body" and message.get("body"):
            first_event.set()
            raise OSError

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/query/stream",
        "raw_path": b"/v1/query/stream",
        "query_string": b"",
        "headers": [
            (b"authorization", b"Bearer secret"),
            (b"content-type", b"application/json"),
        ],
        "server": ("testserver", 80),
        "client": ("testclient", 1234),
    }
    task = asyncio.create_task(test_app(scope, receive, send))
    try:
        await asyncio.wait_for(cleanup_started.wait(), 1)
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
        assert not cleanup_finished.is_set()
        assert test_app.state.semaphore.locked()
    finally:
        allow_cleanup.set()
    await asyncio.wait_for(cleanup_finished.wait(), 1)
    # The pump releases after the runner's cleanup returns.
    await asyncio.sleep(0)
    assert not test_app.state.semaphore.locked()
    await test_app.state.semaphore.acquire()
    assert test_app.state.semaphore.locked()
    test_app.state.semaphore.release()


@pytest.mark.asyncio
async def test_asgi_cancel_inside_send_cleans_up(tmp_path: Path) -> None:
    """Close an abandoned response and release its capacity promptly."""
    runner = CleanupRunner()
    test_app = create_app(make_settings(tmp_path, max_concurrency=1), runner)
    request_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {
                "type": "http.request",
                "body": b'{"prompt":"x"}',
                "more_body": False,
            }
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            cast("asyncio.Task[object]", asyncio.current_task()).cancel()
            await asyncio.sleep(0)

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/query/stream",
        "raw_path": b"/v1/query/stream",
        "query_string": b"",
        "headers": [
            (b"authorization", b"Bearer secret"),
            (b"content-type", b"application/json"),
        ],
        "server": ("testserver", 80),
        "client": ("testclient", 1234),
    }
    task = asyncio.create_task(test_app(scope, receive, send))
    with suppress(asyncio.CancelledError):
        await task
    assert runner.closed.is_set()
    del task
    gc.collect()
    await asyncio.wait_for(runner.closed.wait(), 2)
    assert not test_app.state.semaphore.locked()


def test_info_blank_key(tmp_path: Path) -> None:
    """A directly constructed blank key does not require authentication."""
    settings = Settings(
        api_key="   ", allow_unauthenticated=True, workspace_root=tmp_path
    )
    client, _ = client_for(settings)
    response = client.get("/v1/info")
    assert response.status_code == 200
    assert response.json()["auth_required"] is False


def test_stream_heartbeat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A silent run sends comments before its first data event."""
    monkeypatch.setattr("casapi.main.HEARTBEAT_INTERVAL", 0.01)
    client, _ = client_for(make_settings(tmp_path), FakeRunner(delay=0.05))
    response = client.post("/v1/query/stream", json={"prompt": "x"}, headers=AUTH)
    assert response.text.startswith(": heartbeat\n\n")
    assert parse_sse(response.text)[-1]["type"] == "result"


@pytest.mark.asyncio
async def test_query_disconnect_cancels_and_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mid-run disconnect closes the runner and allows another query."""
    monkeypatch.setattr("casapi.main.DISCONNECT_POLL_INTERVAL", 0.01)
    runner = CleanupRunner()
    test_app = create_app(make_settings(tmp_path, max_concurrency=1), runner)
    disconnected = False

    async def receive() -> dict[str, Any]:
        if disconnected:
            return {"type": "http.disconnect"}
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    route = next(
        r for r in test_app.routes if isinstance(r, APIRoute) and r.path == "/v1/query"
    )
    endpoint = cast("Callable[..., Coroutine[Any, Any, Any]]", route.endpoint)
    request = Request({"type": "http", "app": test_app}, receive)
    first = asyncio.create_task(endpoint(QueryRequest(prompt="x"), request))
    await asyncio.wait_for(runner.started.wait(), 1)
    disconnected = True
    result = await asyncio.wait_for(first, 1)
    assert result.status_code == 499
    assert runner.closed.is_set()
    runner.release.set()
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/v1/query", json={"prompt": "x"}, headers=AUTH)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_query_asgi_disconnect_returns_499(tmp_path: Path) -> None:
    """A disconnect completes the whole ASGI stack after runner cleanup."""
    runner = CleanupRunner()
    test_app = create_app(make_settings(tmp_path, max_concurrency=1), runner)
    request_sent = False
    sent: list[MutableMapping[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {
                "type": "http.request",
                "body": b'{"prompt":"x"}',
                "more_body": False,
            }
        await runner.started.wait()
        return {"type": "http.disconnect"}

    async def send(message: MutableMapping[str, Any]) -> None:  # ruff: ignore[unused-async]
        sent.append(message)

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/query",
        "raw_path": b"/v1/query",
        "query_string": b"",
        "headers": [
            (b"authorization", b"Bearer secret"),
            (b"content-type", b"application/json"),
        ],
        "server": ("testserver", 80),
        "client": ("testclient", 1234),
    }
    await asyncio.wait_for(test_app(scope, receive, send), 1)
    assert any(m["type"] == "http.response.start" and m["status"] == 499 for m in sent)
    assert runner.closed.is_set()
    assert not test_app.state.semaphore.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("disconnect", [False, True])
async def test_query_external_cancel_propagates(
    tmp_path: Path, disconnect: bool
) -> None:
    """External cancellation propagates, including alongside a disconnect."""

    class CancelDuringCleanup(CleanupRunner):
        async def stream(
            self, prompt: str, options: ClaudeAgentOptions
        ) -> AsyncIterator[Message]:
            try:
                async for message in super().stream(prompt, options):
                    yield message
            finally:
                if disconnect:
                    task = asyncio.current_task()
                    assert task is not None
                    task.cancel()

    runner = CancelDuringCleanup()
    test_app = create_app(make_settings(tmp_path, max_concurrency=1), runner)

    async def receive() -> dict[str, Any]:
        if disconnect:
            await runner.started.wait()
            return {"type": "http.disconnect"}
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    route = next(
        r for r in test_app.routes if isinstance(r, APIRoute) and r.path == "/v1/query"
    )
    endpoint = cast("Callable[..., Coroutine[Any, Any, Any]]", route.endpoint)
    request = Request({"type": "http", "app": test_app}, receive)
    task = asyncio.create_task(endpoint(QueryRequest(prompt="x"), request))
    await asyncio.wait_for(runner.started.wait(), 1)
    if not disconnect:
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert runner.closed.is_set()
    assert not test_app.state.semaphore.locked()


@pytest.mark.asyncio
async def test_query_disconnect_coalesced_external_cancel(tmp_path: Path) -> None:
    """An external cancel coalesced with the disconnect cancel still propagates."""
    runner = CleanupRunner()
    test_app = create_app(make_settings(tmp_path, max_concurrency=1), runner)
    gate = asyncio.Event()

    async def receive() -> dict[str, Any]:
        await gate.wait()
        return {"type": "http.disconnect"}

    route = next(
        r for r in test_app.routes if isinstance(r, APIRoute) and r.path == "/v1/query"
    )
    endpoint = cast("Callable[..., Coroutine[Any, Any, Any]]", route.endpoint)
    task = asyncio.create_task(
        endpoint(
            QueryRequest(prompt="x"),
            Request({"type": "http", "app": test_app}, receive),
        )
    )
    await asyncio.wait_for(runner.started.wait(), 1)
    gate.set()
    for _ in range(50):
        await asyncio.sleep(0)
        if task.cancelling():
            break
    assert task.cancelling() == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert runner.closed.is_set()
    assert not test_app.state.semaphore.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
async def test_stream_header_send_failure_releases_slot(
    tmp_path: Path, spec_version: str
) -> None:
    """A disconnect while sending headers releases the unstarted stream slot."""
    runner = FakeRunner()
    test_app = create_app(make_settings(tmp_path, max_concurrency=1), runner)
    request_sent = False
    statuses: list[int] = []

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {
                "type": "http.request",
                "body": b'{"prompt":"x"}',
                "more_body": False,
            }
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message: MutableMapping[str, Any]) -> None:  # ruff: ignore[unused-async]
        if message["type"] == "http.response.start":
            statuses.append(message["status"])
            raise ConnectionError

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/query/stream",
        "raw_path": b"/v1/query/stream",
        "query_string": b"",
        "headers": [
            (b"authorization", b"Bearer secret"),
            (b"content-type", b"application/json"),
        ],
        "server": ("testserver", 80),
        "client": ("testclient", 1234),
    }
    with pytest.raises(ConnectionError if spec_version == "2.3" else ClientDisconnect):
        await asyncio.wait_for(test_app(scope, receive, send), 1)
    assert statuses == [200]
    assert runner.options is None
    assert not test_app.state.semaphore.locked()
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/query/stream", json={"prompt": "x"}, headers=AUTH
        )
    assert response.status_code == 200
    assert parse_sse(response.text)[-1]["type"] == "result"

    await test_app.state.semaphore.acquire()
    assert test_app.state.semaphore.locked()
    test_app.state.semaphore.release()


@dataclass
class SandboxEnv:
    """Isolated service home and workspace for sandbox tests."""

    home: Path
    workspace: Path
    cli: Path


@pytest.fixture
def sbx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SandboxEnv]:
    """Provide a service home, a separate workspace, and a fake bundled CLI.

    Yields:
        The isolated environment.
    """
    previous_umask = os.umask(0o022)  # deterministic modes for test directories
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    home.mkdir()
    workspace.mkdir()
    sdk = tmp_path / "sdk"
    cli = sdk / "_bundled" / "claude"
    cli.parent.mkdir(parents=True)
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("casapi.sandbox._SDK_DIR", sdk)
    yield SandboxEnv(home, workspace, cli)
    os.umask(previous_umask)


def sbx_client(
    sbx: SandboxEnv,
    *,
    root: Path | None = None,
    **kwargs: Any,  # ruff: ignore[any-type]
) -> tuple[TestClient, FakeRunner]:
    """Create an opt-in sandbox client.

    Args:
        sbx: Sandbox environment.
        root: Workspace root override.
        **kwargs: Settings overrides.

    Returns:
        Client and runner.
    """
    return client_for(
        make_settings(root or sbx.workspace, sandbox_mode="opt-in", **kwargs)
    )


SANDBOX_BODY = {"prompt": "x", "sandbox": True}


def test_check_sandbox_request() -> None:
    """Verify the sandbox request guard."""
    check_sandbox_request(Settings(), requested=False)
    with pytest.raises(HTTPException) as exc:
        check_sandbox_request(Settings(), requested=True)
    assert exc.value.status_code == 403
    opt_in = Settings(sandbox_mode="opt-in")
    check_sandbox_request(opt_in, requested=False)
    check_sandbox_request(opt_in, requested=True)
    check_sandbox_request(opt_in, requested=True, permission_mode="plan")
    check_sandbox_request(opt_in, requested=False, permission_mode="auto")


def test_sandbox_rejected_when_off(tmp_path: Path) -> None:
    """Verify sandbox requests are rejected when disabled."""
    client, runner = client_for(make_settings(tmp_path))
    response = client.post(
        "/v1/query", json={"prompt": "x", "sandbox": True}, headers=AUTH
    )
    assert response.status_code == 403
    assert response.json() == {
        "detail": "sandbox is disabled (CASAPI_SANDBOX_MODE=off)"
    }
    assert runner.stream_calls == 0
    assert runner.options is None


def test_sandbox_stream_rejected_when_off(tmp_path: Path) -> None:
    """Verify the stream endpoint rejects sandbox requests when disabled."""
    client, runner = client_for(make_settings(tmp_path))
    response = client.post(
        "/v1/query/stream", json={"prompt": "x", "sandbox": True}, headers=AUTH
    )
    assert response.status_code == 403
    assert runner.stream_calls == 0
    assert runner.options is None


def test_sandbox_false_when_off(tmp_path: Path) -> None:
    """Verify an explicit sandbox=false behaves as before."""
    client, runner = client_for(make_settings(tmp_path))
    response = client.post(
        "/v1/query", json={"prompt": "x", "sandbox": False}, headers=AUTH
    )
    assert response.status_code == 200
    assert runner.options is not None
    assert runner.options.sandbox is None
    assert runner.options.cli_path is None


def test_sandbox_opt_in_enabled(sbx: SandboxEnv) -> None:
    """Verify an opt-in sandbox request builds sandboxed options."""
    settings = make_settings(sbx.workspace, sandbox_mode="opt-in")
    client, runner = client_for(settings)
    response = client.post("/v1/query", json=SANDBOX_BODY, headers=AUTH)
    assert response.status_code == 200
    assert runner.options is not None
    assert runner.options.sandbox == build_sandbox_settings(settings, sbx.home)
    assert runner.options.cli_path == sbx.cli
    env = runner.options.env
    assert env["XDG_CACHE_HOME"] == str(sandbox_cache_dir(sbx.home))
    assert env["npm_config_cache"]
    assert env["npm_config_store_dir"]
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    cache = sandbox_cache_dir(sbx.home)
    assert cache.is_dir()
    assert cache.stat().st_mode & 0o777 == 0o700


def test_sandbox_stream_opt_in_enabled(sbx: SandboxEnv) -> None:
    """Verify the stream endpoint also runs sandboxed."""
    client, runner = sbx_client(sbx)
    response = client.post("/v1/query/stream", json=SANDBOX_BODY, headers=AUTH)
    assert response.status_code == 200
    assert runner.stream_calls == 1


def test_sandbox_opt_in_not_requested(tmp_path: Path) -> None:
    """Verify opt-in mode does not sandbox unless requested."""
    client, runner = client_for(make_settings(tmp_path, sandbox_mode="opt-in"))
    response = client.post("/v1/query", json={"prompt": "x"}, headers=AUTH)
    assert response.status_code == 200
    assert runner.options is not None
    assert runner.options.sandbox is None
    assert runner.options.cli_path is None
    assert "XDG_CACHE_HOME" not in runner.options.env


@pytest.mark.parametrize("path", ["/v1/query", "/v1/query/stream"])
@pytest.mark.parametrize("mode", ["auto", "bypassPermissions"])
def test_sandbox_rejects_egress_mode_in_request(
    sbx: SandboxEnv, path: str, mode: str
) -> None:
    """Verify requested egress-expanding modes are rejected with 403."""
    client, runner = sbx_client(sbx, allow_bypass_permissions=True)
    body = {**SANDBOX_BODY, "permission_mode": mode}
    response = client.post(path, json=body, headers=AUTH)
    assert response.status_code == 403
    detail = response.json()["detail"]
    assert mode in detail
    assert "incompatible with sandbox" in detail
    assert runner.stream_calls == 0
    assert not sandbox_cache_dir(sbx.home).exists()


@pytest.mark.parametrize("path", ["/v1/query", "/v1/query/stream"])
@pytest.mark.parametrize("mode", ["auto", "bypassPermissions"])
def test_sandbox_rejects_egress_mode_inherited(
    sbx: SandboxEnv, path: str, mode: str
) -> None:
    """Verify egress-expanding server default modes are rejected."""
    client, runner = sbx_client(
        sbx, permission_mode=mode, allow_bypass_permissions=True
    )
    response = client.post(path, json=SANDBOX_BODY, headers=AUTH)
    assert response.status_code == 403
    assert "incompatible with sandbox" in response.json()["detail"]
    assert runner.stream_calls == 0


def test_sandbox_request_mode_overrides_default(sbx: SandboxEnv) -> None:
    """Verify a safe request mode wins over an egress-expanding default."""
    client, runner = sbx_client(sbx, permission_mode="auto")
    body = {**SANDBOX_BODY, "permission_mode": "plan"}
    assert client.post("/v1/query", json=body, headers=AUTH).status_code == 200
    assert runner.stream_calls == 1


def test_unsandboxed_egress_mode_allowed(sbx: SandboxEnv) -> None:
    """Verify the egress guard applies to sandboxed runs only."""
    client, runner = sbx_client(sbx, permission_mode="auto")
    response = client.post("/v1/query", json={"prompt": "x"}, headers=AUTH)
    assert response.status_code == 200
    assert runner.stream_calls == 1


@pytest.mark.parametrize("path", ["/v1/query", "/v1/query/stream"])
def test_sandbox_rejects_cwd_at_home_root(sbx: SandboxEnv, path: str) -> None:
    """Verify a workspace rooted at the service home is rejected without cwd."""
    (sbx.home / ".cache").mkdir()
    client, runner = sbx_client(sbx, root=sbx.home)
    response = client.post(path, json=SANDBOX_BODY, headers=AUTH)
    assert response.status_code == 403
    assert "cache" in response.json()["detail"]
    assert runner.stream_calls == 0
    assert not sandbox_cache_dir(sbx.home).exists()


def test_sandbox_rejects_cwd_ancestor_and_shared_cache(sbx: SandboxEnv) -> None:
    """Verify ancestors of ~/.cache and non-dedicated cache subdirs are rejected."""
    cache = sbx.home / ".cache"
    (cache / "pip").mkdir(parents=True)
    (cache / "casapi-sandbox" / "work").mkdir(parents=True)
    client, runner = sbx_client(sbx, root=sbx.home)
    for cwd in (".cache", ".cache/pip", "."):
        body = {**SANDBOX_BODY, "cwd": cwd}
        response = client.post("/v1/query", json=body, headers=AUTH)
        assert response.status_code == 403, cwd
    assert runner.stream_calls == 0
    ok = {**SANDBOX_BODY, "cwd": ".cache/casapi-sandbox/work"}
    assert client.post("/v1/query", json=ok, headers=AUTH).status_code == 200
    assert runner.stream_calls == 1


def test_sandbox_allows_unrelated_cwd_under_home(sbx: SandboxEnv) -> None:
    """Verify a sibling directory of ~/.cache is allowed."""
    project = sbx.home / "project"
    project.mkdir()
    client, runner = sbx_client(sbx, root=sbx.home)
    body = {**SANDBOX_BODY, "cwd": "project"}
    assert client.post("/v1/query", json=body, headers=AUTH).status_code == 200
    assert runner.stream_calls == 1


def test_unsandboxed_cwd_at_home_allowed(sbx: SandboxEnv) -> None:
    """Verify the cache guard applies to sandboxed runs only."""
    client, runner = sbx_client(sbx, root=sbx.home)
    assert (
        client.post("/v1/query", json={"prompt": "x"}, headers=AUTH).status_code == 200
    )
    assert runner.stream_calls == 1


def test_sandbox_cache_hardening(sbx: SandboxEnv) -> None:
    """Verify an existing broad-mode cache directory is tightened."""
    cache = sandbox_cache_dir(sbx.home)
    cache.mkdir(parents=True)
    cache.chmod(0o777)
    assert prepare_sandbox_cache(sbx.home) == cache
    assert cache.stat().st_mode & 0o777 == 0o700


def test_sandbox_cache_chmod_ineffective(
    sbx: SandboxEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify failing to tighten the mode fails closed."""
    cache = sandbox_cache_dir(sbx.home)
    cache.mkdir(parents=True)
    cache.chmod(0o777)

    def no_chmod(self: Path, mode: int) -> None:
        assert self == cache
        assert mode == 0o700

    monkeypatch.setattr(Path, "chmod", no_chmod)
    with pytest.raises(SandboxSetupError, match="cannot be set"):
        prepare_sandbox_cache(sbx.home)


def test_sandbox_cache_leaf_0500_corrected(sbx: SandboxEnv) -> None:
    """Verify a leaf the owner cannot write is corrected to exactly 0700."""
    cache = sandbox_cache_dir(sbx.home)
    cache.mkdir(parents=True)
    cache.chmod(0o500)
    assert prepare_sandbox_cache(sbx.home) == cache
    assert cache.stat().st_mode & 0o777 == 0o700


def test_sandbox_cache_leaf_0500_uncorrectable(
    sbx: SandboxEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify an unusable leaf that cannot be corrected is a 503 error."""
    cache = sandbox_cache_dir(sbx.home)
    cache.mkdir(parents=True)
    cache.chmod(0o500)

    def no_chmod(self: Path, mode: int) -> None:
        """Ignore the chmod request."""

    monkeypatch.setattr(Path, "chmod", no_chmod)
    with pytest.raises(SandboxSetupError, match="cannot be set"):
        prepare_sandbox_cache(sbx.home)


@pytest.mark.parametrize("mode", [0o777, 0o775, 0o757, 0o770, 0o722])
def test_sandbox_cache_parent_writable_rejected(sbx: SandboxEnv, mode: int) -> None:
    """Verify a group/other-writable ~/.cache is rejected before the leaf."""
    parent = sbx.home / ".cache"
    parent.mkdir()
    parent.chmod(mode)
    with pytest.raises(SandboxSetupError, match="writable"):
        prepare_sandbox_cache(sbx.home)
    assert not (parent / "casapi-sandbox").exists()


def test_sandbox_cache_parent_0755_accepted(sbx: SandboxEnv) -> None:
    """Verify a conventional 0755 parent passes."""
    parent = sbx.home / ".cache"
    parent.mkdir()
    parent.chmod(0o755)
    assert prepare_sandbox_cache(sbx.home) == parent / "casapi-sandbox"


def test_sandbox_parent_writable_is_503(sbx: SandboxEnv) -> None:
    """Verify a writable cache parent yields a 503 on both routes."""
    parent = sbx.home / ".cache"
    parent.mkdir()
    parent.chmod(0o777)
    client, runner = sbx_client(sbx)
    for path in ("/v1/query", "/v1/query/stream"):
        response = client.post(path, json=SANDBOX_BODY, headers=AUTH)
        assert response.status_code == 503
    assert runner.stream_calls == 0


def test_sandbox_cache_symlink_leaf(sbx: SandboxEnv) -> None:
    """Verify a symlinked cache leaf is rejected, not followed."""
    cache = sandbox_cache_dir(sbx.home)
    cache.parent.mkdir()
    target = sbx.home / "elsewhere"
    target.mkdir()
    cache.symlink_to(target)
    with pytest.raises(SandboxSetupError, match="owned directory"):
        prepare_sandbox_cache(sbx.home)


def test_sandbox_cache_not_directory(sbx: SandboxEnv) -> None:
    """Verify a regular file in place of the cache is rejected."""
    cache = sandbox_cache_dir(sbx.home)
    cache.parent.mkdir()
    cache.write_text("x")
    with pytest.raises(SandboxSetupError, match="owned directory"):
        prepare_sandbox_cache(sbx.home)


def test_sandbox_cache_wrong_owner(
    sbx: SandboxEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify a directory owned by someone else is rejected."""
    monkeypatch.setattr("casapi.sandbox.os.geteuid", lambda: 4242424)
    with pytest.raises(SandboxSetupError, match="owned directory"):
        prepare_sandbox_cache(sbx.home)


def test_sandbox_cache_missing_home_dir(tmp_path: Path) -> None:
    """Verify OS errors become SandboxSetupError."""
    with pytest.raises(SandboxSetupError, match="setup failed"):
        prepare_sandbox_cache(tmp_path / "absent")


def test_sandbox_setup_failure_is_controlled(
    sbx: SandboxEnv,
) -> None:
    """Verify a cache setup failure is a controlled 503 and the slot is freed."""
    (sbx.home / ".cache").write_text("not a directory")
    client, runner = sbx_client(sbx, max_concurrency=1)
    for path in ("/v1/query", "/v1/query/stream", "/v1/query"):
        response = client.post(path, json=SANDBOX_BODY, headers=AUTH)
        assert response.status_code == 503
        assert "sandbox unavailable" in response.json()["detail"]
    assert runner.stream_calls == 0
    ok = client.post("/v1/query", json={"prompt": "x"}, headers=AUTH)
    assert ok.status_code == 200


def test_sandbox_capacity_checked_before_setup(sbx: SandboxEnv) -> None:
    """Verify 429 is returned before any filesystem setup happens."""
    runner = FakeRunner()
    test_app = create_app(make_settings(sbx.workspace, sandbox_mode="opt-in"), runner)
    test_app.state.semaphore = asyncio.Semaphore(0)
    client = TestClient(test_app)
    response = client.post("/v1/query", json=SANDBOX_BODY, headers=AUTH)
    assert response.status_code == 429
    assert not (sbx.home / ".cache").exists()
    assert runner.stream_calls == 0


@pytest.mark.parametrize("path", ["/v1/query", "/v1/query/stream"])
def test_sandbox_429_beats_unavailable_home(
    sbx: SandboxEnv, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """Verify a saturated request gets 429 even when the home is unavailable."""
    runner = FakeRunner()
    test_app = create_app(make_settings(sbx.workspace, sandbox_mode="opt-in"), runner)
    test_app.state.semaphore = asyncio.Semaphore(0)
    client = TestClient(test_app)

    def boom() -> Path:
        msg = "no home"
        raise RuntimeError(msg)

    monkeypatch.setattr(Path, "home", boom)
    response = client.post(path, json=SANDBOX_BODY, headers=AUTH)
    assert response.status_code == 429
    assert runner.stream_calls == 0


def test_sandbox_missing_home(sbx: SandboxEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify an unknown or relative home is a controlled 503."""
    client, runner = sbx_client(sbx)

    def boom() -> Path:
        msg = "no home"
        raise RuntimeError(msg)

    monkeypatch.setattr(Path, "home", boom)
    response = client.post("/v1/query", json=SANDBOX_BODY, headers=AUTH)
    assert response.status_code == 503
    with pytest.raises(SandboxSetupError, match="unknown"):
        service_home()
    monkeypatch.setattr(Path, "home", lambda: Path("relative"))
    with pytest.raises(SandboxSetupError, match="not absolute"):
        service_home()
    assert runner.stream_calls == 0


def test_sandbox_missing_bundled_cli(
    sbx: SandboxEnv,
) -> None:
    """Verify a missing or non-executable bundled CLI fails closed."""
    client, runner = sbx_client(sbx)
    sbx.cli.chmod(0o644)
    response = client.post("/v1/query", json=SANDBOX_BODY, headers=AUTH)
    assert response.status_code == 503
    assert "bundled Claude CLI" in response.json()["detail"]
    sbx.cli.unlink()
    response = client.post("/v1/query/stream", json=SANDBOX_BODY, headers=AUTH)
    assert response.status_code == 503
    assert runner.stream_calls == 0
    assert not sandbox_cache_dir(sbx.home).exists()


def test_bundled_cli_path_default_is_real_sdk() -> None:
    """Verify the real SDK bundles an executable CLI."""
    assert bundled_cli_path().parent.name == "_bundled"


def test_build_options_failure_releases_slot(
    sbx: SandboxEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify unexpected option-building errors release the slot and propagate."""

    def boom(*_a: object, **_k: object) -> ClaudeAgentOptions:
        msg = "unexpected"
        raise RuntimeError(msg)

    monkeypatch.setattr("casapi.main.build_options", boom)
    test_app = create_app(make_settings(sbx.workspace, max_concurrency=1), FakeRunner())
    client = TestClient(test_app, raise_server_exceptions=False)
    for _ in range(2):
        response = client.post("/v1/query", json={"prompt": "x"}, headers=AUTH)
        assert response.status_code == 500


def test_build_sandbox_settings(tmp_path: Path) -> None:
    """Verify the sandbox settings dict exactly."""
    home = tmp_path / "h"
    settings = Settings(sandbox_allowed_domains=("github.com",))
    result = build_sandbox_settings(settings, home)
    assert result == {
        "enabled": True,
        "failIfUnavailable": True,
        "autoAllowBashIfSandboxed": False,
        "allowUnsandboxedCommands": False,
        "enableWeakerNestedSandbox": False,
        "network": {"allowedDomains": ["github.com"], "allowAllUnixSockets": False},
        "filesystem": {
            "allowWrite": [str(home / ".cache" / "casapi-sandbox")],
            "denyRead": [
                str(home / ".ssh"),
                str(home / ".gnupg"),
                str(home / ".aws"),
                str(home / ".config"),
                str(home / ".claude.json"),
                str(home / ".claude" / ".credentials.json"),
                str(home / "Dropbox"),
                "/run/user",
                "/run/dbus",
            ],
            "allowRead": [str(home / ".config" / "gh"), str(home / ".config" / "uv")],
        },
        "credentials": {
            "envVars": [
                {"name": "ANTHROPIC_API_KEY", "mode": "deny"},
                {"name": "CASAPI_API_KEY", "mode": "deny"},
                {"name": "CLAUDE_CODE_MESSAGING_TOKEN", "mode": "deny"},
                {"name": "CLAUDE_CODE_OAUTH_TOKEN", "mode": "deny"},
            ]
        },
    }
    assert "excludedCommands" not in result
    assert result["failIfUnavailable"] is True
    assert result["allowUnsandboxedCommands"] is False


def test_sandbox_env(tmp_path: Path) -> None:
    """Verify cache redirection environment."""
    cache = tmp_path / ".cache" / "casapi-sandbox"
    assert sandbox_env(tmp_path) == {
        "XDG_CACHE_HOME": str(cache),
        "npm_config_cache": str(cache / "npm"),
        "npm_config_store_dir": str(cache / "pnpm-store"),
    }
