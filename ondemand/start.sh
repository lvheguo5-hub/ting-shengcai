#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
# Prefer local .venv; allow override via VENV_ONDEMAND
if [[ -n "${VENV_ONDEMAND:-}" ]]; then
  VENV="$VENV_ONDEMAND"
elif [[ -x "$ROOT/.venv/bin/uvicorn" ]]; then
  VENV="$ROOT/.venv"
elif [[ -x "/workspace/.venv-ondemand/bin/uvicorn" ]]; then
  VENV="/workspace/.venv-ondemand"
else
  echo "No venv found. Create one:" >&2
  echo "  cd $ROOT && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt edge-tts" >&2
  exit 1
fi
export PATH="$VENV/bin:$PATH"
cd "$ROOT"
mkdir -p data/posts cache/audio queue web secrets
[[ -f data/authors.json ]] || echo '[]' > data/authors.json
[[ -f data/topics_index.json ]] || echo '{"topics":[]}' > data/topics_index.json
echo "Using venv: $VENV"
echo "edge-tts: $(command -v edge-tts || true)"
exec "$VENV/bin/uvicorn" server:app --host 0.0.0.0 --port 8766 --app-dir "$ROOT"
