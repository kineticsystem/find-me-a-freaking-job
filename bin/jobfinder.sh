#!/bin/bash -e
# Any other subcommand: jobfinder.sh doctor | jobs | runs | sources | profile
cd "$(dirname "$(readlink -f "$0")")/.."
exec python -m jobfinder "$@"
