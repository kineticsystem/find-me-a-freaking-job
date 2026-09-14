# The server and the web UI in one image. The model (llama.cpp) stays outside
# for now and is reached over the network; see docker-compose.yml.

# ---- stage 1: build the web app -------------------------------------------
FROM node:24-slim AS web
RUN corepack enable
WORKDIR /web
COPY web/package.json web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm build

# ---- stage 2: the runtime --------------------------------------------------
# A CUDA *devel* image: it carries nvcc, so the llama.cpp fork can be compiled
# inside the container (bin/build-llama.sh) into the bind-mounted submodule at
# ./modules/llama.cpp, where the result persists on the host.
FROM nvidia/cuda:12.8.1-devel-ubuntu24.04

LABEL name="find-me-a-freaking-job"

# git, curl: opencode and its installer. The rest: building llama.cpp with
# CUDA and with libcurl for --hf-repo model downloads.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git curl ca-certificates \
      python3 python3-venv python3-pip \
      build-essential cmake ninja-build pkg-config libcurl4-openssl-dev \
 && rm -rf /var/lib/apt/lists/*

# Ubuntu 24.04 images ship a default 'ubuntu' user at uid/gid 1000. Remove it
# so 'developer' can take the host user's uid/gid, which keeps the bind-mounted
# directories writable and correctly owned on the host.
RUN if id -u ubuntu >/dev/null 2>&1; then userdel -r ubuntu; fi \
 && if getent group ubuntu >/dev/null 2>&1; then groupdel ubuntu; fi

ARG USER_UID=1000
ARG USER_GID=1000
RUN groupadd -g ${USER_GID} developer \
 && useradd --create-home --shell /bin/bash --uid ${USER_UID} --gid ${USER_GID} developer

USER developer
ENV HOME=/home/developer
WORKDIR /home/developer/app

# opencode drives the model. Pinned: it is the one moving part this image
# depends on that is not in pyproject.toml.
ARG OPENCODE_VERSION=1.18.26
RUN curl -fsSL https://opencode.ai/install | bash -s -- --version ${OPENCODE_VERSION}

# Python dependencies, read from pyproject.toml (the one place they are
# declared), into a venv outside the app directory so nothing mounted over
# ./app can hide them. Only the dependencies are installed, not the package:
# the code runs from ./app, where config/ data/ runs/ resolve relative to it.
COPY --chown=developer:developer pyproject.toml ./
RUN python3 -m venv /home/developer/venv \
 && python3 -c 'import tomllib; p = tomllib.load(open("pyproject.toml", "rb"))["project"]; print("\n".join(p["dependencies"] + p["optional-dependencies"]["dev"]))' \
    | /home/developer/venv/bin/pip install --no-cache-dir -r /dev/stdin

# The application, the agent prompts, and the built web app.
COPY --chown=developer:developer jobfinder/ ./jobfinder/
COPY --chown=developer:developer .opencode/ ./.opencode/
COPY --chown=developer:developer bin/ ./bin/
COPY --chown=developer:developer tests/ ./tests/
COPY --chown=developer:developer --from=web /web/dist ./web/dist/

# State and the llama.cpp source+build live in these; compose bind-mounts
# them from the host. The model cache is mounted under ~/.cache.
RUN mkdir -p config profile data runs modules/llama.cpp /home/developer/.cache

# ./bin on the PATH so `docker exec <name> run-once.sh` works without a shell,
# and the venv and opencode likewise.
ENV PATH="/home/developer/app/bin:/home/developer/venv/bin:/home/developer/.opencode/bin:${PATH}"

# With host networking 127.0.0.1 is the host, where llama-server (started by
# start.sh in this same container) listens on :8084. Listen on all interfaces
# so a phone on the LAN can reach the UI.
ENV JOBFINDER_API_HOST=0.0.0.0

EXPOSE 8099 8084
CMD ["start.sh"]
