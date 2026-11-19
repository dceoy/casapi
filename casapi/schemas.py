"""Request and response models."""

from __future__ import annotations

from typing import Any

# Pydantic resolves these Literal aliases at runtime, so the import cannot
# move into a TYPE_CHECKING block.
from claude_agent_sdk import EffortLevel, PermissionMode  # ruff: ignore[typing-only-third-party-import]
from pydantic import BaseModel, ConfigDict, Field


class QueryRequest(BaseModel):
    """One ephemeral agent run. Unknown (e.g. session) fields are rejected."""

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1)
    system_prompt: str | None = None
    model: str | None = None
    cwd: str | None = None
    allowed_tools: list[str] | None = None
    disallowed_tools: list[str] | None = None
    permission_mode: PermissionMode | None = None
    max_turns: int | None = Field(default=None, ge=1, le=1000)
    max_budget_usd: float | None = Field(default=None, gt=0)
    effort: EffortLevel | None = None
    timeout_seconds: float | None = Field(default=None, gt=0)
    include_messages: bool = True
    sandbox: bool = False


class QueryResponse(BaseModel):
    """Final agent result with an optional message transcript."""

    result: str | None
    session_id: str
    model: str | None
    subtype: str
    is_error: bool
    num_turns: int
    duration_ms: int
    duration_api_ms: int
    total_cost_usd: float | None
    usage: dict[str, Any] | None
    stop_reason: str | None
    messages: list[dict[str, Any]]


class InfoResponse(BaseModel):
    """Server capabilities and defaults."""

    version: str
    stateless: bool
    default_model: str | None
    workspace_root: str
    default_permission_mode: str
    default_allowed_tools: list[str]
    max_concurrency: int
    auth_required: bool
    sandbox_mode: str
    sandbox_allowed_domains: list[str]
