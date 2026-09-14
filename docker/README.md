# Running in Docker

One container runs everything: the model server (the llama.cpp fork), the app server, and the web UI. `bin/start.sh` is the container's command; it starts `llama-server`, then the app, and exits if either dies so the restart policy brings both back together.

## What is where

- **In the image:** the Python code, the agent prompts, the built web app, opencode, headless Chromium (for career pages that render their listings in JavaScript), the CUDA toolchain and build tools, and all dependencies. Rebuild the image to pick up code changes.
- **On the host, bind-mounted:** `config/`, `profile/`, `data/`, `runs/` — everything you edit and everything the app persists — and `modules/llama.cpp`, the llama.cpp fork with its build tree. Survives rebuilds, restarts and `clean`. Your own files in `config/` and `profile/` are created from the `.example` templates on first start and are git-ignored.
- **Model files:** `~/.cache/huggingface` and `~/.cache/llama.cpp` on the host are mounted into the container, so a model already downloaded is reused, and one downloaded from inside the container stays on the host.

The container runs as a user with your uid/gid, so files it writes into those directories are owned by you.

## Prerequisites

Docker with the compose plugin:

```bash
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
```

Docker must be able to hand the GPU to a container. Today the model server still runs on the host, but it is the next thing to move in, and a container without GPU access cannot run it. Install the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) for your distribution, register it with Docker, and restart the daemon:

```bash
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

Then confirm a container can see the card:

```bash
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
```

If that prints your GPU, Docker is ready. If it fails with `could not select device driver`, the toolkit is not registered; if `nvidia-smi` itself is missing on the host, the NVIDIA driver is not installed.

The llama.cpp fork is a git submodule; fetch it once after cloning:

```bash
git submodule update --init
```

(`dock.sh build` does this for you if you forget.)

## Commands

`dock.sh` wraps `docker compose`, supplying the container name and your uid/gid. The first argument names the container; it can be anything.

```bash
./docker/dock.sh jobfinder build     # build the image, then llama.cpp if it is not built yet
./docker/dock.sh jobfinder build-llama   # (re)build llama.cpp, e.g. after updating the submodule
./docker/dock.sh jobfinder start     # start the model server and the app in the background
./docker/dock.sh jobfinder logs      # follow the log
./docker/dock.sh jobfinder run       # one search run, now (add --no-llm to skip inference)
./docker/dock.sh jobfinder shell     # a shell inside the container
./docker/dock.sh jobfinder stop
./docker/dock.sh jobfinder clean     # remove container and image; your state stays
```

The first `build` compiles the llama.cpp fork with CUDA, which takes several minutes. The result goes into `modules/llama.cpp/build` on the host, so it is done once: later `build`s only rebuild the image, and `clean` does not touch it.

After `start`, the model takes a few seconds to load (the app waits for it), then the web app is at **http://127.0.0.1:8099/** and, because the container shares the host's network, at `http://<host ip>:8099/` from any device on your network. The model server is on `:8084`, exactly where it was when it ran on the host, so anything else on the machine that used it keeps working.

The container restarts itself after a reboot or a crash (`restart: unless-stopped`); `stop` is the only thing that keeps it down.

## Inside the container

`~/app/bin` is on the `PATH`, so these work from a shell inside the container and from `docker exec`:

```bash
start.sh                 # what the container runs: llama-server, then the app
llama-server.sh          # the model server alone (the fork's qwen3.8-27B.sh, container paths)
serve.sh                 # the app alone
build-llama.sh           # compile the fork into modules/llama.cpp/build
run-once.sh [--no-llm]   # one search run
jobfinder.sh doctor      # any other subcommand: jobs, runs, sources, profile
test.sh                  # the API contract tests
```

## The model

`bin/llama-server.sh` holds the exact `llama-server` command line — model, context size, KV cache types, sampling. Edit it to change the model or its settings, then `stop` and `start` — `bin/` is bind-mounted, so no rebuild is needed. The model name it serves (`--alias`) must match `llm.model` in `config/settings.yaml`.

The llama.cpp fork is pinned by the submodule to a specific commit. To move it: `cd modules/llama.cpp && git checkout <commit>`, then `./docker/dock.sh jobfinder build-llama`.

## Networking and the GPU

The compose file uses `network_mode: host`: the model server is on the host's `:8084` and the UI on `:8099`, no port mappings. It reserves the GPU through the NVIDIA runtime (`deploy.resources.reservations.devices`), which is why Docker has to be installed with GPU support.

To use a model server running somewhere else instead of the built-in one, point the app at it; the built-in `llama-server` still starts unless you remove it from `start.sh`:

```bash
JOBFINDER_LLM_BASE_URL=http://192.168.1.50:8084/v1 ./docker/dock.sh jobfinder start
```

## If the model is not there

If llama.cpp has not been built, the container starts the app alone and says so in the log; the UI works, runs fetch but do not score. Build it and restart.

A run that finds the model server answering 503 (still loading) waits for it, up to `llm.startup_wait_seconds` (300 as shipped). A run that finds it unreachable skips the scoring stages and is recorded as `partial` — in about twenty seconds, not the half hour it would take to time out batch by batch. Scoring catches up on the next run.
