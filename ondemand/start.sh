#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV="${VENV_ONDEMAND:-/workspace/.venv-ondemand}"
export PATH="$VENV/bin:$PATH"
cd "$ROOT"
mkdir -p data/posts cache/audio queue web
# seed empties only if missing
[[ -f data/authors.json ]] || echo '[]' > data/authors.json
[[ -f data/topics_index.json ]] || echo '{"topics":[]}' > data/topics_index.json
exec "$VENV/bin/uvicorn" server:app --host 0.0.0.0 --port 8766 --app-dir "$ROOT"
