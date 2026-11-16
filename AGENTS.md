# Development guidelines

- Use Python, FastAPI, Claude Agent SDK, and `uv`. Use `pnpm` for Node-based tooling.
- Run `.agents/skills/local-qa/scripts/qa.sh` after modifying files.
- Keep the HTTP API stateless. Start and stop a Claude execution process per request unless a documented feature requires a separate worker.
- Do not use in-memory process state as a durable session store. Make session persistence and authorization explicit before implementing resume.
- Bind to loopback by default. Do not add unauthenticated remote agent-execution endpoints.
- Treat prompts, workspace paths, tool permissions, and agent-generated output as untrusted. Enforce workspace confinement and deny privileged tools by default.
- Do not introduce a task queue, database, MCP server, or WebSocket transport without a concrete requirement.
- Keep implementations small and preserve strict type checking and full branch coverage.
