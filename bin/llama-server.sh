#!/bin/bash -e
# Start the model server. This is the qwen3.8-27B.sh from the llama.cpp fork,
# with paths that make sense inside the container: logs under ./runs/llama,
# the model from the mounted Hugging Face cache (no re-download).
#
#   LLAMA_HOST  default 0.0.0.0 (host networking: reachable as the host's :8084)
#   LLAMA_PORT  default 8084

cd "$(dirname "$(readlink -f "$0")")/.."
mkdir -p runs/llama/prompts

exec ./modules/llama.cpp/build/bin/llama-server \
  --hf-repo unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL \
  --jinja \
  --chat-template-kwargs '{"reasoning_effort":"medium"}' \
  --alias "Qwen3.8-27B" \
  --n-gpu-layers 999 \
  --ctx-size 128000 \
  --cache-type-k q8_0 \
  --cache-type-v turbo4 \
  --host "${LLAMA_HOST:-0.0.0.0}" \
  --port "${LLAMA_PORT:-8084}" \
  --reasoning-budget -1 \
  --temp 0.6 \
  --top-p 0.95 \
  --top-k 20 \
  --min-p 0.0 \
  --presence-penalty 0.0 \
  --repeat-penalty 1.0 \
  --flash-attn on \
  --parallel 1 \
  --spec-type draft-mtp \
  --spec-draft-n-max 2 \
  -lv 4 \
  --log-prompts-dir runs/llama/prompts \
  --log-file runs/llama/server.log \
  --no-mmproj-offload

# Context using turbo 8, 110080
# Context using turbo4, 128000
# Context using turbo4, and offload vision model, 155000
