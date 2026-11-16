# casapi

Stateless HTTP API for remote Claude Code execution powered by Claude Agent SDK.

[![CI](https://github.com/dceoy/casapi/actions/workflows/ci.yml/badge.svg)](https://github.com/dceoy/casapi/actions/workflows/ci.yml)

## Status

This repository is an initial **scaffold**, not a functioning remote agent-execution service.
The HTTP application currently exposes only `GET /health` and FastAPI's OpenAPI
documentation. Claude Agent SDK is declared as a dependency, but no agent
execution endpoint, session management, or credential handling is implemented yet.

## Development

Requirements: Python 3.11–3.14 and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/dceoy/casapi.git
cd casapi
uv sync
uv run uvicorn casapi.main:app --host 127.0.0.1 --port 8000
```

Check the service at `http://127.0.0.1:8000/health` and Swagger UI at
`http://127.0.0.1:8000/docs`.

```sh
uv run ruff format --check .
uv run ruff check .
uv run pyright .
uv run pytest
uv run mkdocs build --strict
```

Dependency versions are tracked in the committed `uv.lock`.

## Design constraints

- Use a stateless FastAPI frontend and ephemeral Claude Agent SDK executions.
- Keep the API bound to loopback until authentication and workspace isolation are implemented.
- Require explicit authorization before exposing agent execution or filesystem access.
- Add SSE, session persistence, and an external worker only when needed.
- Do not assume that a stateless API makes an active Claude subprocess stateless.

## License

AGPL-3.0-only. See [LICENSE](LICENSE).
