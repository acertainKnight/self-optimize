#!/usr/bin/env bash
# Synthetic, offline demo of the self-optimize eval gym. No API key, no
# network call, no credentials -- see demo/README.md and demo/run_demo.py.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec python3 demo/run_demo.py "$@"
