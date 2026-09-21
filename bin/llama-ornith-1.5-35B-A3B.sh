#!/bin/bash -e
# NVIDIA Ornith-1.5-35B-A3B (ornith-ai Q4_K_M): a 35B mixture of experts with
# 3B active, about 1.5x faster than Qwen3.8-27B per triage batch on a 4090 and
# the closest to it in ranking (doc/score_comparison.md). Architecture
# qwen35moe, known to the pinned llama.cpp fork. Same command line that was
# used for the comparison, with container paths.
# Started by llama-server.sh when config/settings.yaml says llm.model: Ornith-1.5-35B.
#
#   LLAMA_HOST  default 0.0.0.0 (host networking: reachable as the host's :8084)
#   LLAMA_PORT  default 8084

cd "$(dirname "$(readlink -f "$0")")/.."
mkdir -p runs/llama/prompts

exec ./modules/llama.cpp/build/bin/llama-server \
  --hf-repo ornith-ai/Ornith-1.5-35B-A3B-GGUF \
  --jinja \
  --alias "Ornith-1.5-35B" \
  --n-gpu-layers 999 \
  --ctx-size 80128 \
  --cache-type-k q8_0 \
  --cache-type-v q8_0 \
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
  -lv 4 \
  --log-prompts-dir runs/llama/prompts \
  --log-file runs/llama/server.log
