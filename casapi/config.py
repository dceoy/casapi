"""Runtime settings loaded from environment variables."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_DEFAULT_TOOLS = "Read,Write,Edit,Bash,Glob,Grep"

SandboxMode = Literal["off", "opt-in"]
_SANDBOX_MODES: tuple[SandboxMode, ...] = ("off", "opt-in")
DEFAULT_SANDBOX_ALLOWED_DOMAINS: tuple[str, ...] = (
    "github.com",
    "api.github.com",
    "codeload.github.com",
    "uploads.github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
)
_DOMAIN_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_MAX_HOSTNAME_LENGTH = 253
_MIN_HOSTNAME_LABELS = 2


def _env_bool(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE_VALUES


def _env_api_key() -> str | None:
    raw = os.environ.get("CASAPI_API_KEY")
    if raw is None:
        return None
    return raw.strip() or None


def _valid_domain(domain: str) -> bool:
    """Check an allow-list entry.

    Args:
        domain: Lowercased entry: ASCII host with an optional leading ``*.``.

    Returns:
        Whether every DNS label is 1-63 chars and the host is at most 253.
    """
    host = domain.removeprefix("*.")
    labels = host.split(".")
    return (
        len(host) <= _MAX_HOSTNAME_LENGTH
        and len(labels) >= _MIN_HOSTNAME_LABELS
        and all(_DOMAIN_LABEL_RE.fullmatch(label) for label in labels)
    )


def _env_sandbox_mode() -> SandboxMode:
    raw = os.environ.get("CASAPI_SANDBOX_MODE", "off")
    for mode in _SANDBOX_MODES:
        if raw == mode:
            return mode
    msg = f"CASAPI_SANDBOX_MODE must be one of off, opt-in; got {raw!r}"
    raise ValueError(msg)


def _env_sandbox_domains() -> tuple[str, ...]:
    raw = os.environ.get("CASAPI_SANDBOX_ALLOWED_DOMAINS")
    if raw is None:
        return DEFAULT_SANDBOX_ALLOWED_DOMAINS
    domains = tuple(d.strip().lower() for d in raw.split(",") if d.strip())
    for d in domains:
        if not _valid_domain(d):
            msg = f"invalid domain in CASAPI_SANDBOX_ALLOWED_DOMAINS: {d!r}"
            raise ValueError(msg)
    return domains


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
        sandbox_mode: ``off`` rejects ``sandbox: true`` requests; ``opt-in`` lets
            requests enable the Bash sandbox.
        sandbox_allowed_domains: Network egress allow-list for sandboxed Bash.
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
    sandbox_mode: SandboxMode = "off"
    sandbox_allowed_domains: tuple[str, ...] = DEFAULT_SANDBOX_ALLOWED_DOMAINS

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from ``CASAPI_*`` environment variables.

        Returns:
            Parsed settings with defaults applied.
        """
        tools = os.environ.get("CASAPI_ALLOWED_TOOLS", _DEFAULT_TOOLS)
        return cls(
            api_key=_env_api_key(),
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
            sandbox_mode=_env_sandbox_mode(),
            sandbox_allowed_domains=_env_sandbox_domains(),
        )
