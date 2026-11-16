# casapi

Stateless HTTP API for remote Claude Code execution powered by Claude Agent SDK.

[![CI](https://github.com/dceoy/casapi/actions/workflows/ci.yml/badge.svg)](https://github.com/dceoy/casapi/actions/workflows/ci.yml)

Each request is one ephemeral `claude_agent_sdk.query()` run. There are no
sessions, no resume, and no server-side conversation state. A client on another
machine POSTs a prompt and receives the agent result plus the full message
transcript, either as one JSON document or as a Server-Sent Events stream.

## Endpoints

| Method | Path               | Auth | Description                                |
| ------ | ------------------ | ---- | ------------------------------------------ |
| GET    | `/health`          | no   | Readiness probe: `{"status": "ok"}`        |
| GET    | `/v1/info`         | yes  | Server defaults and limits                 |
| POST   | `/v1/query`        | yes  | Run one agent query, return the transcript |
| POST   | `/v1/query/stream` | yes  | Same, streamed as SSE                      |

See [docs/api/rest-api.md](docs/api/rest-api.md) for the full reference.

## Quickstart

Server:

```sh
uv sync
export CASAPI_API_KEY="$(openssl rand -hex 32)"
export CASAPI_WORKSPACE_ROOT=/srv/workspaces
export ANTHROPIC_API_KEY=...   # or log in with `claude` as the server user
uv run uvicorn casapi.main:app --host 127.0.0.1 --port 8000
```

Client (another machine, via TLS or a private network):

```sh
curl -sS https://casapi.example.net/v1/query \
  -H "Authorization: Bearer $CASAPI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "List the files in this directory", "cwd": "myproject", "max_turns": 5}'

curl -N https://casapi.example.net/v1/query/stream \
  -H "Authorization: Bearer $CASAPI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Summarize README.md"}'
```

## Configuration

| Variable                          | Default                          | Description                                              |
| --------------------------------- | -------------------------------- | -------------------------------------------------------- |
| `CASAPI_API_KEY`                  | unset                            | Bearer token clients must send                           |
| `CASAPI_ALLOW_UNAUTHENTICATED`    | `false`                          | Allow requests with no key configured (local dev only)   |
| `CASAPI_WORKSPACE_ROOT`           | current directory                | All request `cwd` values must resolve inside this path   |
| `CASAPI_DEFAULT_MODEL`            | unset (SDK default)              | Model used when a request omits `model`                  |
| `CASAPI_PERMISSION_MODE`          | `acceptEdits`                    | Default permission mode                                  |
| `CASAPI_ALLOWED_TOOLS`            | `Read,Write,Edit,Bash,Glob,Grep` | Default auto-approve list (not a restriction; see below) |
| `CASAPI_ALLOW_BYPASS_PERMISSIONS` | `false`                          | Permit requests with `permission_mode=bypassPermissions` |
| `CASAPI_MAX_CONCURRENCY`          | `4`                              | Simultaneous runs; excess requests get HTTP 429          |
| `CASAPI_MAX_TIMEOUT_SECONDS`      | `3600`                           | Default and maximum run duration                         |

## Security model

- **The API key is a shell.** Holding the key is equivalent to running a shell
  as the service user (with the workspace as the starting directory). Protect
  it accordingly; everything below is defence in depth, not a sandbox.
- **API key required by default.** Without `CASAPI_API_KEY` (or with an empty
  one), execution endpoints return 503 unless
  `CASAPI_ALLOW_UNAUTHENTICATED=true` is set explicitly. Keys are compared in
  constant time.
- **`allowed_tools` is an auto-approve list, not a restriction.** It names the
  tools a run may use without a permission decision; it does not remove other
  tools, and under the default `acceptEdits` mode file edits are approved
  regardless. The server-side `CASAPI_ALLOWED_TOOLS` is only a default — any
  key holder can send a different list per request.
- **The agent can read the server's secrets.** A run inherits the service
  environment, including `ANTHROPIC_API_KEY`, the Claude login credentials,
  and `CASAPI_API_KEY` itself. Prompt-injected workspace content can therefore
  exfiltrate them; do not point the service at untrusted workspaces.
- **Workspace confinement.** Request `cwd` is resolved (symlinks included) and
  must stay inside `CASAPI_WORKSPACE_ROOT`. Note this confines the agent's
  starting directory, not what its tools (e.g. `Bash`) can reach; run the server
  as an unprivileged user or in a container for real isolation.
- **`bypassPermissions` is opt-in** via `CASAPI_ALLOW_BYPASS_PERMISSIONS`.
  It is not a meaningful boundary on its own: with `Bash` auto-approved, the
  default posture is already shell-equivalent.
- **Client disconnects.** `POST /v1/query` does not stop when the client
  disconnects; the run continues until it finishes or times out. The
  streaming endpoint stops when its consumer goes away.
- **Bind and TLS.** Bind to loopback or a private interface and put TLS in front
  (reverse proxy, Tailscale, or an SSH tunnel). The API key travels in a header.
- **Statelessness.** Request bodies containing `resume`, `session_id`, or any
  other unknown field are rejected with 422. Runs are hermetic: CLI session
  persistence is disabled, so no resumable transcript is written, and host
  and project settings are not loaded (`setting_sources` is empty), so hooks
  or MCP servers from the host or the workspace cannot leak into a run. The
  default system prompt is the Claude Code preset. What still persists after
  a run: files the agent creates or edits in the workspace, and ordinary CLI
  bookkeeping under `~/.claude`.

## Development

Requirements: Python 3.11–3.14 and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/dceoy/casapi.git
cd casapi
uv sync
uv run uvicorn casapi.main:app --host 127.0.0.1 --port 8000
```

Swagger UI is at `http://127.0.0.1:8000/docs`.

```sh
uv run ruff format --check .
uv run ruff check .
uv run pyright .
uv run pytest
uv run mkdocs build --strict
```

Dependency versions are tracked in the committed `uv.lock`.

## License

AGPL-3.0-only. See [LICENSE](LICENSE).
