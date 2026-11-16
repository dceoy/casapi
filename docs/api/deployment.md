# Deployment

For local development, run the API on loopback:

```sh
uv sync
uv run uvicorn casapi.main:app --host 127.0.0.1 --port 8000
```

For remote deployments, configure TLS, authentication, rate limits,
workspace isolation, and least-privilege execution before exposing any agent
execution API. These controls are not implemented in the scaffold.

Running Claude Agent SDK workloads also requires an appropriate Claude
authentication mechanism and an isolated execution workspace.
