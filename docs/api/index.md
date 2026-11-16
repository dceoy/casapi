# API Reference

casapi exposes a stateless REST API around Claude Agent SDK `query()`:

- `GET /health`: readiness probe (no auth).
- `GET /v1/info`: server defaults and limits.
- `POST /v1/query`: one agent run, JSON result with transcript.
- `POST /v1/query/stream`: one agent run, streamed as SSE.

- [REST API](rest-api.md)
- [Deployment](deployment.md)
