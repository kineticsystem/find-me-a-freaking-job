#!/bin/bash -e
# One full search run, now. Pass --no-llm to fetch without spending inference.
cd "$(dirname "$(readlink -f "$0")")/.."
exec python -m jobfinder run-once "$@"
