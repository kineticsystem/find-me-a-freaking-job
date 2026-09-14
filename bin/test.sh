#!/bin/bash -e
# API contract tests. pytest is in the image (pyproject's dev extras).
cd "$(dirname "$(readlink -f "$0")")/.."
exec python -m pytest tests "$@"
