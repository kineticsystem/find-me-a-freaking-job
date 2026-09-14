#!/bin/bash -e
# Start the scheduler, the API and the web UI. This is the container's command.
cd "$(dirname "$(readlink -f "$0")")/.."
exec python -m jobfinder serve "$@"
