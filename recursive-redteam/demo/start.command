#!/bin/zsh
set -e
cd "$(dirname "$0")/.."
exec python3 demo/server.py --port 8765
