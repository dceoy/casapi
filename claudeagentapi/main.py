"""FastAPI application for the Claude Agent API scaffold."""

from fastapi import FastAPI

app = FastAPI(
    title="Claude Agent API",
    description="Stateless HTTP API for remote Claude Code execution.",
    version="0.1.0",
)


@app.get("/health")
async def health() -> dict[str, str]:
    """Report API process readiness.

    Returns:
        Status for the running HTTP server.
    """
    return {"status": "ok"}
