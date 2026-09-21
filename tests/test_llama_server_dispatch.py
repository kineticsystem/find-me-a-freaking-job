"""bin/llama-server.sh starts the bin/llama-<model>.sh whose --alias is config/settings.yaml's llm.model."""

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _tree(tmp_path: Path, model: str) -> Path:
    (tmp_path / "bin").mkdir(exist_ok=True); (tmp_path / "config").mkdir(exist_ok=True)
    shutil.copy(ROOT / "bin/llama-server.sh", tmp_path / "bin/llama-server.sh")
    for name, alias in (("llama-a.sh", "Model-A"), ("llama-b.sh", "Model-B")):
        p = tmp_path / "bin" / name
        p.write_text(f'#!/bin/bash\n# fake\nexec echo started {alias} \\\n  --alias "{alias}" \\\n  --port 1\n')
        p.chmod(0o755)
    (tmp_path / "config/settings.yaml").write_text(f"llm:\n  model: {model}\n")
    return tmp_path


def _run(tree: Path, **env):
    import os
    return subprocess.run(["bash", str(tree / "bin/llama-server.sh")], capture_output=True, text=True,
                          env={**os.environ, **env}, cwd=tree)


def test_picks_the_script_whose_alias_matches(tmp_path):
    r = _run(_tree(tmp_path, "Model-B"))
    assert r.returncode == 0 and "started Model-B" in r.stdout and "starting bin/llama-b.sh" in r.stdout


def test_unknown_model_fails_and_lists_what_exists(tmp_path):
    r = _run(_tree(tmp_path, "Nope"))
    assert r.returncode == 2 and 'no bin/llama-*.sh declares --alias "Nope"' in r.stderr
    assert '"Model-A"' in r.stderr and '"Model-B"' in r.stderr


def test_env_override_wins(tmp_path):
    tree = _tree(tmp_path, "Model-A")
    r = _run(tree, LLAMA_SCRIPT=str(tree / "bin/llama-b.sh"))
    assert "started Model-B" in r.stdout


def test_real_scripts_declare_distinct_aliases_matching_the_example_setting():
    import re, yaml
    aliases = {}
    for s in (ROOT / "bin").glob("llama-*.sh"):
        if s.name == "llama-server.sh":
            continue
        m = re.search(r'--alias "([^"]+)"', s.read_text())
        assert m, s.name
        aliases[m.group(1)] = s.name
    assert len(aliases) == 2 and "Qwen3.8-27B" in aliases and "Ornith-1.5-35B" in aliases
    example = yaml.safe_load((ROOT / "config/settings.example.yaml").read_text())
    assert example["llm"]["model"] in aliases
