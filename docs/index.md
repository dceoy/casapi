# casapi

Stateless HTTP API scaffold for remote Claude Code execution.

## Current capabilities

The application exposes FastAPI OpenAPI documentation and `GET /health`.
No agent execution, credentials, or session-resume endpoints are implemented.

## Development

```sh
uv sync
uv run uvicorn casapi.main:app --host 127.0.0.1 --port 8000
```

- [API reference](api/index.md)
- [Deployment](api/deployment.md)

The original repository is licensed under AGPL-3.0-only.
