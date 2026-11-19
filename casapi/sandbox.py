"""Bash sandbox settings for opt-in sandboxed agent runs.

CLI schema keys beyond the SDK TypedDict are forwarded verbatim; verified
against bundled CLI 2.1.325/2.1.327.
"""

from __future__ import annotations

import os
import stat
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

import claude_agent_sdk

if TYPE_CHECKING:
    from casapi.config import Settings

SANDBOX_DENIED_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "CASAPI_API_KEY",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
)


_CACHE_MODE = 0o700

_SDK_DIR = Path(claude_agent_sdk.__file__).parent


class SandboxSetupError(Exception):
    """Raised when the sandbox prerequisites cannot be established safely."""


def service_home() -> Path:
    """Return the absolute home directory of the server user.

    Returns:
        Absolute home directory.

    Raises:
        SandboxSetupError: If the home directory is unknown or not absolute.
    """
    try:
        home = Path.home()
    except (RuntimeError, KeyError, OSError) as exc:
        msg = "sandbox unavailable: service home directory is unknown"
        raise SandboxSetupError(msg) from exc
    if not home.is_absolute():
        msg = "sandbox unavailable: service home directory is not absolute"
        raise SandboxSetupError(msg)
    return home


def bundled_cli_path() -> Path:
    """Return the Claude CLI executable bundled with the installed SDK.

    Returns:
        Path of the bundled executable.

    Raises:
        SandboxSetupError: If it is missing or not executable.
    """
    cli = _SDK_DIR / "_bundled" / "claude"
    if not cli.is_file() or not os.access(cli, os.X_OK):
        msg = "sandbox unavailable: bundled Claude CLI is missing or not executable"
        raise SandboxSetupError(msg)
    return cli


def _trusted_dir(path: Path) -> os.stat_result:
    """Return lstat of ``path`` after checking it is our own real directory.

    Args:
        path: Directory to inspect without following a leaf symlink.

    Returns:
        The ``lstat`` result.

    Raises:
        SandboxSetupError: If it is not a non-symlink directory owned by us.
    """
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        msg = "sandbox unavailable: cache path is not an owned directory"
        raise SandboxSetupError(msg)
    return info


def _ensure_dir(path: Path) -> None:
    """Create ``path`` (0o700) if absent, then verify it is trusted."""
    with suppress(FileExistsError):
        path.mkdir(mode=0o700)
    _trusted_dir(path)


def _tighten(path: Path) -> None:
    """Make ``path`` exactly 0o700 or fail closed.

    Raises:
        SandboxSetupError: If the mode is not exactly 0o700 after tightening.
    """
    if stat.S_IMODE(path.lstat().st_mode) != _CACHE_MODE:
        path.chmod(_CACHE_MODE)
        if stat.S_IMODE(path.lstat().st_mode) != _CACHE_MODE:
            msg = (
                "sandbox unavailable: cache directory permissions cannot be set to 0700"
            )
            raise SandboxSetupError(msg)


def _check_parent_not_writable(parent: Path) -> None:
    """Reject a group/other-writable parent, where the leaf could be replaced.

    Raises:
        SandboxSetupError: If group or other write bits are set.
    """
    if stat.S_IMODE(parent.lstat().st_mode) & 0o022:
        msg = "sandbox unavailable: cache parent directory is group or other writable"
        raise SandboxSetupError(msg)


def prepare_sandbox_cache(home: Path) -> Path:
    """Create or verify the dedicated cache directory without trusting it.

    The parent ``~/.cache`` and the leaf must be real directories (symlinks are
    not followed) owned by the current user. A leaf with group/other
    permissions is set to exactly 0o700 and verified. A group/other-writable
    parent is rejected.

    Args:
        home: Absolute home directory of the server user.

    Returns:
        The verified cache directory.

    Raises:
        SandboxSetupError: On any failure; the caller must not run the agent.
    """
    cache = sandbox_cache_dir(home)
    try:
        _ensure_dir(cache.parent)
        _check_parent_not_writable(cache.parent)
        _ensure_dir(cache)
        _tighten(cache)
    except OSError as exc:
        msg = "sandbox unavailable: cache directory setup failed"
        raise SandboxSetupError(msg) from exc
    return cache


def sandbox_cache_dir(home: Path) -> Path:
    """Return the dedicated cache directory for sandboxed runs.

    Args:
        home: Home directory of the server user.

    Returns:
        Path of the only writable location outside the working directory.
    """
    return home / ".cache" / "casapi-sandbox"


def build_sandbox_settings(settings: Settings, home: Path) -> dict[str, Any]:
    """Build the fail-closed CLI ``sandbox`` settings.

    Args:
        settings: Server settings.
        home: Home directory of the server user.

    Returns:
        Sandbox settings for ``ClaudeAgentOptions.sandbox``.
    """
    return {
        "enabled": True,
        "failIfUnavailable": True,
        "autoAllowBashIfSandboxed": False,
        "allowUnsandboxedCommands": False,
        "enableWeakerNestedSandbox": False,
        "network": {
            "allowedDomains": list(settings.sandbox_allowed_domains),
            "allowAllUnixSockets": False,
        },
        "filesystem": {
            "allowWrite": [str(sandbox_cache_dir(home))],
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
            "allowRead": [
                str(home / ".config" / "gh"),
                str(home / ".config" / "uv"),
            ],
        },
        "credentials": {
            "envVars": [{"name": n, "mode": "deny"} for n in SANDBOX_DENIED_ENV_VARS]
        },
    }


def sandbox_env(home: Path) -> dict[str, str]:
    """Build environment variables that redirect caches into the sandbox dir.

    Args:
        home: Home directory of the server user.

    Returns:
        Environment variables for the CLI process.
    """
    cache = sandbox_cache_dir(home)
    return {
        "XDG_CACHE_HOME": str(cache),
        "npm_config_cache": str(cache / "npm"),
        "npm_config_store_dir": str(cache / "pnpm-store"),
    }
