# REST API

## `GET /health`

Reports HTTP server readiness.

```sh
curl http://127.0.0.1:8000/health
```

Expected response:

```json
{"status":"ok"}
```

FastAPI automatically generates documentation at `/docs` and `/openapi.json`.

**Security:** this health endpoint is not an agent-execution capability.
Do not expose a future execution endpoint without authentication, authorization,
workspace confinement, and tool-permission restrictions.
