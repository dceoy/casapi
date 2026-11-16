# casapi

Stateless HTTP API for remote Claude Code execution powered by Claude Agent SDK.

## Capabilities

- `POST /v1/query`: run one ephemeral agent query and receive the result and
  full message transcript as JSON.
- `POST /v1/query/stream`: the same run streamed as Server-Sent Events.
- `GET /v1/info` and `GET /health`.
- Bearer-token authentication, workspace confinement, opt-in
  `bypassPermissions`, concurrency and timeout limits.
- No sessions, resume, or server-side conversation state.

## Development

```sh
uv sync
uv run uvicorn casapi.main:app --host 127.0.0.1 --port 8000
```

- [API reference](api/index.md)
- [Deployment](api/deployment.md)

The original repository is licensed under AGPL-3.0-only.
