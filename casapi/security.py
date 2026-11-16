"""Authentication, workspace confinement, and permission guards."""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from fastapi import Header, HTTPException, Request, status

if TYPE_CHECKING:
    from casapi.config import Settings


def require_auth(settings: Settings, authorization: str | None) -> None:
    """Enforce bearer-token authentication.

    Args:
        settings: Server settings.
        authorization: Value of the ``Authorization`` header.

    Raises:
        HTTPException: 503 if no key is configured and unauthenticated access is
            not allowed; 401 if the supplied credentials are wrong.
    """
    key = settings.api_key.strip() if settings.api_key else None
    if not key:
        if settings.allow_unauthenticated:
            return
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "agent execution disabled: CASAPI_API_KEY is not configured",
        )
    expected = f"Bearer {key}"
    if authorization is None or not secrets.compare_digest(
        authorization.encode(), expected.encode()
    ):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "invalid or missing credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


def auth_dependency(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """FastAPI dependency applying :func:`require_auth` with app settings.

    Args:
        request: Incoming request.
        authorization: Value of the ``Authorization`` header.
    """
    require_auth(request.app.state.settings, authorization)


def resolve_cwd(settings: Settings, cwd: str | None) -> Path:
    """Resolve a requested working directory inside the workspace root.

    Args:
        settings: Server settings.
        cwd: Requested directory (relative to the root, or absolute).

    Returns:
        Resolved existing directory.

    Raises:
        HTTPException: 403 if outside the root; 400 if not an existing directory.
    """
    base = settings.workspace_root.resolve()
    if cwd is None:
        candidate = base
    else:
        path = Path(cwd)
        candidate = path if path.is_absolute() else base / path
    try:
        resolved = candidate.resolve()
        is_dir = resolved.is_dir()
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "cwd is not a valid path"
        ) from exc
    if not resolved.is_relative_to(base):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "cwd is outside the workspace root"
        )
    if not is_dir:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "cwd does not exist or is not a directory"
        )
    return resolved


def check_permission_mode(settings: Settings, requested: str | None) -> str:
    """Reject ``bypassPermissions`` unless explicitly enabled.

    Args:
        settings: Server settings.
        requested: Permission mode from the request, if any.

    Returns:
        The effective permission mode.

    Raises:
        HTTPException: 403 if bypassPermissions is not allowed.
    """
    mode = requested or settings.permission_mode
    if mode == "bypassPermissions" and not settings.allow_bypass_permissions:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "bypassPermissions is disabled (CASAPI_ALLOW_BYPASS_PERMISSIONS)",
        )
    return mode
