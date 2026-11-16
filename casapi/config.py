"""Runtime settings loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_DEFAULT_TOOLS = "Read,Write,Edit,Bash,Glob,Grep"


def _env_bool(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE_VALUES


@dataclass(frozen=True)
class Settings:
    """Server configuration.

    Attributes:
        api_key: Bearer token required from clients, if any.
        allow_unauthenticated: Permit requests when no API key is configured.
        workspace_root: Directory that all agent working directories must stay in.
        default_model: Model used when a request does not specify one.
        permission_mode: Default permission mode for agent runs.
        allowed_tools: Default tool allow-list for agent runs.
        allow_bypass_permissions: Permit requests using ``bypassPermissions``.
        max_concurrency: Maximum number of simultaneous agent runs.
        max_timeout_seconds: Upper bound (and default) for run duration.
    """

    api_key: str | None = None
    allow_unauthenticated: bool = False
    workspace_root: Path = field(default_factory=lambda: Path.cwd().resolve())
    default_model: str | None = None
    permission_mode: str = "acceptEdits"
    allowed_tools: tuple[str, ...] = tuple(_DEFAULT_TOOLS.split(","))
    allow_bypass_permissions: bool = False
    max_concurrency: int = 4
    max_timeout_seconds: float = 3600.0

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from ``CASAPI_*`` environment variables.

        Returns:
            Parsed settings with defaults applied.
        """
        tools = os.environ.get("CASAPI_ALLOWED_TOOLS", _DEFAULT_TOOLS)
        return cls(
            api_key=os.environ.get("CASAPI_API_KEY"),
            allow_unauthenticated=_env_bool("CASAPI_ALLOW_UNAUTHENTICATED"),
            workspace_root=Path(
                os.environ.get("CASAPI_WORKSPACE_ROOT", str(Path.cwd()))
            ).resolve(),
            default_model=os.environ.get("CASAPI_DEFAULT_MODEL"),
            permission_mode=os.environ.get("CASAPI_PERMISSION_MODE", "acceptEdits"),
            allowed_tools=tuple(t.strip() for t in tools.split(",") if t.strip()),
            allow_bypass_permissions=_env_bool("CASAPI_ALLOW_BYPASS_PERMISSIONS"),
            max_concurrency=int(os.environ.get("CASAPI_MAX_CONCURRENCY", "4")),
            max_timeout_seconds=float(
                os.environ.get("CASAPI_MAX_TIMEOUT_SECONDS", "3600")
            ),
        )
