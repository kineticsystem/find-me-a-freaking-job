#!/bin/bash -e
# Start the model server for the model named in config/settings.yaml.
#
# One script per model lives beside this one, bin/llama-<model>.sh, each with
# its own --alias. This picks the script whose alias equals llm.model and
# execs it, so the setting is the only place the choice is made: change
# llm.model, restart, and the matching server comes up. LLAMA_SCRIPT=<path>
# overrides the lookup.

cd "$(dirname "$(readlink -f "$0")")/.."

if [ -n "$LLAMA_SCRIPT" ]; then
    exec "$LLAMA_SCRIPT"
fi

model=$(python - <<'PY'
import yaml, pathlib
cfg = yaml.safe_load(pathlib.Path("config/settings.yaml").read_text()) or {}
print((cfg.get("llm") or {}).get("model") or "")
PY
)
if [ -z "$model" ]; then
    echo "llama-server.sh: llm.model is not set in config/settings.yaml" >&2
    exit 2
fi

for script in bin/llama-*.sh; do
    [ "$script" = "bin/llama-server.sh" ] && continue
    alias=$(grep -oE -- '--alias "[^"]+"' "$script" | head -1 | sed 's/--alias "//; s/"$//')
    if [ "$alias" = "$model" ]; then
        echo "llama-server.sh: llm.model is $model; starting $script"
        exec "$script"
    fi
done

echo "llama-server.sh: no bin/llama-*.sh declares --alias \"$model\" (config/settings.yaml llm.model)." >&2
echo "  available: $(grep -ohE -- '--alias "[^"]+"' bin/llama-*.sh | sed 's/--alias //' | tr '\n' ' ')" >&2
exit 2
