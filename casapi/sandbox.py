"""Bash sandbox settings for opt-in sandboxed agent runs.

CLI schema keys beyond the SDK TypedDict are forwarded verbatim; verified
against bundled CLI 2.1.325/2.1.327.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from casapi.config import Settings

SANDBOX_DENIED_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "CASAPI_API_KEY",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
)


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
