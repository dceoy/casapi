# Deployment

## Local development

```sh
uv sync
CASAPI_ALLOW_UNAUTHENTICATED=true uv run uvicorn casapi.main:app --host 127.0.0.1 --port 8000
```

## Remote server

1. Install dependencies on the server:

   ```sh
   git clone https://github.com/dceoy/casapi.git /opt/casapi
   cd /opt/casapi && uv sync --no-dev
   ```

2. Authenticate Claude on the server. The SDK spawns the Claude Code CLI, so
   either set `ANTHROPIC_API_KEY` in the environment file below, or log in once
   with `claude` as the user that runs the service.

3. Create `/etc/casapi.env` (mode `0600`):

   ```sh
   CASAPI_API_KEY=<long random secret, e.g. openssl rand -hex 32>
   CASAPI_WORKSPACE_ROOT=/srv/workspaces
   ANTHROPIC_API_KEY=sk-ant-...
   ```

4. Run uvicorn on loopback or a private interface, and expose it only through
   TLS (a reverse proxy) or a private network such as Tailscale or an SSH
   tunnel (`ssh -L 8000:127.0.0.1:8000 server`). Disable proxy response
   buffering for `/v1/query/stream` (casapi sends `X-Accel-Buffering: no`).

5. Run it as an unprivileged user that owns only the workspace directory.
   Workspace confinement limits the starting `cwd`, not what `Bash` can reach.
   The agent inherits this environment file, so a run can read
   `CASAPI_API_KEY` and `ANTHROPIC_API_KEY` from its own environment — the
   API key is effectively a shell as this user (see the README security
   model). The concurrency limit is per process: running uvicorn with
   `--workers N` multiplies `CASAPI_MAX_CONCURRENCY` by N.

### systemd unit

```ini
[Unit]
Description=casapi
After=network-online.target

[Service]
User=casapi
WorkingDirectory=/opt/casapi
EnvironmentFile=/etc/casapi.env
ExecStart=/opt/casapi/.venv/bin/uvicorn casapi.main:app --host 127.0.0.1 --port 8000
Restart=on-failure
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

### Bash sandbox (optional)

Runs that send `sandbox: true` execute Bash inside the Claude CLI sandbox.

- Prerequisites on the host: `bwrap` (bubblewrap), `socat`, and `rg` (ripgrep).
- The CLI used is the one bundled with the installed `claude-agent-sdk`, not
  any `claude` on `PATH`. Its version decides how sandbox settings are
  interpreted, so redo the probe below after every SDK update.
- The sandbox is fail-closed. On Ubuntu releases that ship the
  `bwrap-userns-restrict` AppArmor profile, the sandbox's seccomp helper cannot
  start, so `sandbox: true` requests fail until the host policy is adjusted
  (for example with a dedicated, AppArmor-profiled copy of `bwrap` placed first
  on the service `PATH`). Do not weaken this by disabling the sandbox checks.

To enable it:

1. Set `CASAPI_SANDBOX_MODE=opt-in` (optionally
   `CASAPI_SANDBOX_ALLOWED_DOMAINS`) and restart the service.
2. Run an acceptance probe with `sandbox: true`: `echo ok` succeeds; writes
   outside the working directory and the sandbox cache are denied; secret
   directories such as `~/.ssh` are unreadable; `env` shows no
   `CASAPI_API_KEY`; `curl` to a non-allow-listed host is denied; and
   `systemctl --user status` fails.

Clients must never retry a failed `sandbox: true` request with
`sandbox: false`; that would reintroduce fail-open behaviour.

### Client smoke test

From the client machine (here via an SSH tunnel on `localhost:8000`):

```sh
curl -sS http://localhost:8000/health
curl -sS http://localhost:8000/v1/info -H "Authorization: Bearer $CASAPI_API_KEY"
curl -sS http://localhost:8000/v1/query \
  -H "Authorization: Bearer $CASAPI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Reply with the single word: pong", "max_turns": 1}'
```

A `200` response with `"result": "pong"` confirms authentication, Claude
credentials, and the SDK/CLI path all work.
