#!/usr/bin/env bash
# Generic YouTube playlist → Apple Books audiobook downloader
set -euo pipefail
cd "$(dirname "$0")"

if [[ -x .venv/bin/python ]]; then
  PY=.venv/bin/python
else
  PY=python3
fi

exec "$PY" download_playlists.py "$@"
