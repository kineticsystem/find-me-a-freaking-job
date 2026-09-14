#!/bin/bash
# The container's command: the model server and the app in one container.
# Either process dying ends the container, so the restart policy brings both
# back together rather than leaving the app running against a dead model.

cd "$(dirname "$(readlink -f "$0")")/.."

# A broken config file stops everything here, before the model server is
# even launched, so the reason is the only thing in the log.
if ! python -m jobfinder check; then
    exit 2
fi

if [ -x modules/llama.cpp/build/bin/llama-server ]; then
    llama-server.sh &
    LLAMA_PID=$!
    echo "start.sh: llama-server pid $LLAMA_PID"
else
    echo "start.sh: llama.cpp is not built; starting the app without a model." >&2
    echo "start.sh: run   ./docker/dock.sh <name> build-llama   on the host, then restart." >&2
    LLAMA_PID=""
fi

serve.sh &
APP_PID=$!
echo "start.sh: app pid $APP_PID"

# Forward a stop to both, then wait for the first to exit.
trap 'kill $APP_PID $LLAMA_PID 2>/dev/null' TERM INT
wait -n $APP_PID $LLAMA_PID
rc=$?
echo "start.sh: a process exited (rc=$rc); stopping the other" >&2
kill $APP_PID $LLAMA_PID 2>/dev/null
wait
exit $rc
