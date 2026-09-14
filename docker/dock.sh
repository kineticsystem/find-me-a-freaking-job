#! /bin/bash -e

# Build, start, stop and remove the container that runs the server and the
# web UI. The container itself is defined in docker-compose.yml; this script
# only adds what compose cannot express: the host uid/gid and the container
# name passed on the command line.

function container_exists() {
    [[ $(docker ps -aq --filter name=^/${1}$) ]]
}

function image_exists() {
    [[ $(docker images -q "$1") ]]
}

# Compile the llama.cpp submodule inside a one-off container. The build tree
# is in the bind-mounted submodule, so it persists on the host.
function build_llama() {
    if [ ! -f ../modules/llama.cpp/CMakeLists.txt ]; then
        echo "modules/llama.cpp is empty; fetching the submodule"
        git -C .. submodule update --init modules/llama.cpp
    fi
    docker compose run --rm --no-deps app build-llama.sh
}

function display_usage() {
    echo -e "\nUsage: ./dock.sh <container-name> <command>\n
    Commands:
    build        Build the image, then llama.cpp if it is not built yet
    build-llama  (Re)build the llama.cpp fork inside the container
    start        Start the model server and the app in the background
    stop    Stop it
    logs    Follow the server log
    shell   Open a shell inside the running container (extra args go to bash)
    run     Run one search now:   ./dock.sh <name> run [--no-llm]
    clean   Stop, and remove the container and the image\n
    State (config/, profile/, data/, runs/) lives in the repo on the host and
    survives all of these.\n"
}

# Compose resolves the paths in docker-compose.yml against the directory that
# holds it, so every command has to run from there.
cd "$(dirname "$0")"

if [ "$#" -lt 2 ]; then
    echo "Missing required arguments."
    display_usage
    exit 1
fi

name="$1"
command="$2"
shift 2

# The container name doubles as the compose project name, so several
# differently named containers can coexist from this same compose file.
export CONTAINER_NAME="$name"
export COMPOSE_PROJECT_NAME="$name"

# Build args for the Dockerfile: a container user matching the host user, so
# the bind-mounted state directories stay writable on the host.
export USER_UID=$(id -u)
export USER_GID=$(id -g)

case "$command" in
    build)
        docker compose build
        if [ ! -x ../modules/llama.cpp/build/bin/llama-server ]; then
            echo "llama.cpp is not built yet; building it now (this takes a while)"
            build_llama
        fi
        ;;
    build-llama)
        build_llama
        ;;
    start)
        docker compose up --detach
        # A broken config file makes the app refuse to start; the container
        # then restarts in a loop. Catch that here and show the reason.
        for _ in 1 2 3 4 5 6; do
            sleep 2
            state=$(docker inspect "$name" --format '{{.State.Status}} {{.State.ExitCode}} {{.RestartCount}}' 2>/dev/null)
            if [[ "$state" == restarting* ]] || [[ "${state#* }" != "0 0"* && "${state%% *}" != "running" ]]; then
                echo "The container is not staying up. Last lines of its log:" >&2
                docker logs --tail 15 "$name" 2>&1 | sed 's/^/    /' >&2
                exit 1
            fi
        done
        echo "Starting: model server on :8084, web app on http://127.0.0.1:8099/   (./dock.sh $name logs to follow)"
        ;;
    stop)
        docker compose stop
        ;;
    logs)
        docker compose logs --follow --tail 100
        ;;
    shell)
        # -t only when there is a terminal, so `shell -c '...'` also works
        # from scripts and pipes.
        if [ -t 0 ]; then tty=-it; else tty=-i; fi
        docker exec $tty "$name" bash "$@"
        ;;
    run)
        docker exec "$name" run-once.sh "$@"
        ;;
    clean)
        docker compose down --rmi local --remove-orphans
        if container_exists "$name"; then
            docker rm --force "$name" > /dev/null
        fi
        if image_exists "$name:latest"; then
            docker rmi "$name:latest" > /dev/null
        fi
        ;;
    *)
        echo "Unknown command: $command"
        display_usage
        exit 1
        ;;
esac
