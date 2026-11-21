#!/usr/bin/env bash
# Download a single YouTube video as best-quality audio
set -euo pipefail
cd "$(dirname "$0")"

if [[ -x .venv/bin/python ]]; then
  PY=.venv/bin/python
else
  PY=python3
fi

exec "$PY" download_video.py "$@"
