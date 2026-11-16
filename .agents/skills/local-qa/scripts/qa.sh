#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

export UV_EXCLUDE_NEWER="1 day"

uv sync --all-groups
uv run ruff format .
uv run ruff check --fix .
uv run pyright .
uv run pytest
pnpm dlx oxfmt --write './**/*.md'

case "${OSTYPE}" in
  darwin* | linux*)
    uvx zizmor --fix=safe .github/workflows
    git ls-files -z -- '.github/workflows/*.yml' | xargs -0 actionlint
    git ls-files -z -- '.github/workflows/*.yml' | xargs -0 uvx yamllint -d '{"extends": "relaxed", "rules": {"line-length": "disable"}}'
    uvx checkov --framework=all --output=github_failed_only --directory=.
    ;;
  *)
    echo "GitHub Actions linting requires macOS or Linux." >&2
    exit 1
    ;;
esac
