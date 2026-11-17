# REST API

All `/v1/*` endpoints require `Authorization: Bearer <CASAPI_API_KEY>`.
FastAPI also serves `/docs` and `/openapi.json`.

## Statelessness contract

Every request is one ephemeral SDK `query()` run. The server keeps no sessions
and offers no resume, continue, or fork. The `session_id` in responses is
informational only and cannot be sent back: request bodies with unknown fields
(such as `resume` or `session_id`) are rejected with `422`. To continue work,
include the needed context in the next prompt.

Runs are hermetic. CLI session persistence is disabled, so the CLI writes no
resumable transcript, and host and project settings are not loaded
(`setting_sources` is empty): hooks, MCP servers, and permission rules from
the host's or the workspace's `.claude/settings.json` never enter a run. Auto
memory is disabled (`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`), and MCP configuration
is strict and isolated. Managed policy settings and `~/.claude.json` still load.
What persists after a run is only what the agent itself writes — files in the
workspace and ordinary CLI bookkeeping under `~/.claude`.

## `GET /health`

No authentication.

```json
{ "status": "ok" }
```

## `GET /v1/info`

```json
{
  "version": "0.1.0",
  "stateless": true,
  "default_model": null,
  "workspace_root": "/srv/workspaces",
  "default_permission_mode": "acceptEdits",
  "default_allowed_tools": ["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
  "max_concurrency": 4,
  "auth_required": true
}
```

## `POST /v1/query`

Request (only `prompt` is required):

```json
{
  "prompt": "List the Python files",
  "system_prompt": null,
  "model": null,
  "cwd": "myproject",
  "allowed_tools": ["Read", "Glob"],
  "disallowed_tools": null,
  "permission_mode": "default",
  "max_turns": 5,
  "max_budget_usd": 1.0,
  "effort": "low",
  "timeout_seconds": 300
}
```

- `cwd`: relative to `CASAPI_WORKSPACE_ROOT`, or absolute inside it.
- `system_prompt`: when omitted, the run uses the Claude Code preset system
  prompt. Send a string to replace it.
- `allowed_tools`: an auto-approve list, not a restriction. It names tools the
  run may use without a permission decision; it does not remove other tools,
  and file edits are still approved under `acceptEdits`. When omitted, the
  server default (`CASAPI_ALLOWED_TOOLS`) applies — a default, not a ceiling.
- `permission_mode`: `default`, `acceptEdits`, `plan`, `bypassPermissions`
  (requires server opt-in), `dontAsk`, `auto`.
- `max_turns`: 1–1000. `max_budget_usd` and `timeout_seconds` must be > 0;
  `timeout_seconds` may not exceed `CASAPI_MAX_TIMEOUT_SECONDS`.

If the client disconnects, the run is promptly cancelled and its concurrency
slot is freed. The server internally answers 499 without logging an error; the
response is not delivered to the disconnected client.

Response:

```json
{
  "result": "There are 3 Python files.",
  "session_id": "0b0e...",
  "subtype": "success",
  "is_error": false,
  "num_turns": 2,
  "duration_ms": 4210,
  "duration_api_ms": 3900,
  "total_cost_usd": 0.0123,
  "usage": { "input_tokens": 100, "output_tokens": 40 },
  "stop_reason": "end_turn",
  "messages": [
    { "type": "system", "subtype": "init", "data": {} },
    {
      "type": "assistant",
      "model": "...",
      "content": [
        { "type": "thinking", "thinking": "...", "signature": "..." },
        { "type": "tool_use", "id": "t1", "name": "Glob", "input": { "pattern": "*.py" } }
      ],
      "error": null,
      "usage": null,
      "stop_reason": null,
      "session_id": null,
      "uuid": null
    },
    {
      "type": "user",
      "content": [
        { "type": "tool_result", "tool_use_id": "t1", "content": "a.py", "is_error": false }
      ],
      "uuid": null,
      "parent_tool_use_id": null
    },
    {
      "type": "result",
      "subtype": "success",
      "is_error": false,
      "result": "...",
      "session_id": "0b0e...",
      "num_turns": 2,
      "duration_ms": 4210,
      "duration_api_ms": 3900,
      "total_cost_usd": 0.0123,
      "usage": null,
      "stop_reason": "end_turn",
      "errors": null
    }
  ]
}
```

An agent-level failure (`is_error: true`, e.g. `subtype: "error_max_turns"`) is
still HTTP `200`. Message types not listed above are emitted as
`{"type": "<snake_case_class>", "data": {...}}`.

## `POST /v1/query/stream`

Same request body. All validation (auth, `cwd`, permissions, concurrency)
happens before streaming starts, so those failures are ordinary HTTP errors.
The response is `text/event-stream`; each SDK message is one event:

```text
data: {"type": "system", "subtype": "init", "data": {}}

data: {"type": "assistant", "content": [...]}

data: {"type": "result", "subtype": "success", "result": "..."}
```

If the runner fails or times out mid-stream, one final event is sent and the
stream ends:

```text
data: {"type": "error", "detail": "agent run timed out"}
```

Treat a run as successful only when a `result` event arrives. A stream that
ends without one — including a run whose message stream stops early — closes
with an `error` event instead. During idle periods the stream sends an SSE
comment `: heartbeat` every 15 seconds; clients must ignore comment lines.
Disconnecting
from this endpoint cancels the run and releases its slot. The run deadline also
cancels the run and releases its slot if the client stops reading. For an
abandoned stream, the run timeout initiates cancellation, but awaited SDK
shutdown can extend beyond it.

## Errors

| Status | Meaning                                                                |
| ------ | ---------------------------------------------------------------------- |
| 400    | `cwd` missing or not a directory; `timeout_seconds` above server max   |
| 401    | Missing or incorrect bearer token                                      |
| 403    | `cwd` outside workspace root; `bypassPermissions` not enabled          |
| 422    | Invalid body (empty prompt, unknown/stateful fields, bad ranges)       |
| 429    | Concurrency limit reached                                              |
| 502    | SDK/CLI failure, or the run ended without a result message             |
| 503    | `CASAPI_API_KEY` not configured and unauthenticated access not allowed |
| 504    | Run exceeded its timeout                                               |

Errors use FastAPI's `{"detail": "..."}` body.
