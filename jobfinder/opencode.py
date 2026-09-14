"""Thin, defensive wrapper around `opencode run`.

Design notes:
  * Every call is a FRESH single-shot session. We never use --continue: a
    growing session is how you blow a 64K context window.
  * The model is asked to WRITE a JSON file rather than print JSON. Parsing a
    small local model's prose is a losing game; reading a file it wrote with
    its own tool is reliable and inspectable afterwards.
  * Every session runs `--pure` against a GENERATED config (see _build_config).
    Your interactive opencode config loads three MCP servers over `npx`; booting
    those per session costs minutes and a 63MB node_modules per directory, and
    none of them are needed here. The generated config keeps only your llama.cpp
    provider and defines the agents inline, so a session starts in ~1s.
  * Sessions run in throwaway directories under runs/, so the model never sees
    this repo's source.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Type, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from .config import ROOT, settings

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

AGENT_DIR = ROOT / ".opencode" / "agent"
RESULT_NAME = "result.json"


class OpencodeError(RuntimeError):
    pass


def estimate_tokens(text: str) -> int:
    """Crude but adequate: ~4 chars/token for English + JSON."""
    return len(text) // 4 + 1


def workspace_root() -> Path:
    root = settings().paths.resolve("runs")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _parse_agent_file(path: Path) -> dict[str, Any]:
    """Split an agent markdown file into its frontmatter and its prompt body."""
    text = path.read_text()
    meta: dict[str, Any] = {}
    body = text
    if text.startswith("---"):
        _, _, rest = text.partition("---\n")
        front, _, body = rest.partition("\n---")
        meta = yaml.safe_load(front) or {}
    agent = {k: v for k, v in meta.items() if k in ("description", "mode", "temperature", "tools")}
    agent["prompt"] = body.strip()
    return agent


def _agent_definitions() -> dict[str, Any]:
    if not AGENT_DIR.exists():
        return {}
    return {p.stem: _parse_agent_file(p) for p in sorted(AGENT_DIR.glob("*.md"))}


def config_path() -> Path:
    return workspace_root() / "opencode-config.json"


def _build_config(force: bool = False) -> Path:
    """Write the hermetic config every session runs against.

    Deliberately excludes `mcp`, `plugin` and `lsp`: this workload only ever
    reads text and writes one JSON file, and each MCP server costs a multi-minute
    npx bootstrap per session.
    """
    path = config_path()
    cfg = settings()
    config: dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json",
        "provider": cfg.llm.provider_block,
        "model": cfg.opencode.model,
        "small_model": cfg.opencode.model,
        "agent": _agent_definitions(),
        "mcp": {},
        "lsp": False,
        "autoupdate": False,
    }
    payload = json.dumps(config, indent=2)
    if force or not path.exists() or path.read_text() != payload:
        path.write_text(payload)
        log.info("wrote opencode config %s", path)
    return path


def _prepare_workdir(workdir: Path, agent: str) -> Path:
    workdir.mkdir(parents=True, exist_ok=True)
    return _build_config()


def llm_reachable(timeout: float = 5.0, wait_seconds: float = 0.0) -> tuple[bool, str]:
    """Is the model server answering? Checked once per run so a downed server
    costs seconds, not a minute per session times every batch.

    A 503 means llama.cpp is up but still loading the model, which is normal
    when both start in the same container; with `wait_seconds` we keep
    polling for that case instead of declaring the model gone.
    """
    import httpx

    url = settings().llm.base_url.rstrip("/") + "/models"
    deadline = time.time() + wait_seconds
    last = ""
    while True:
        try:
            resp = httpx.get(url, timeout=timeout)
            if resp.status_code < 500:
                return True, ""
            last = f"{url} -> HTTP {resp.status_code}"
            loading = resp.status_code == 503
        except Exception as exc:
            last = f"{url}: {type(exc).__name__}: {exc}"
            loading = False
        if not loading or time.time() >= deadline:
            return False, last
        log.info("model server is still loading (%s); waiting", last)
        time.sleep(5)


def run_session(
    prompt: str,
    workdir: Path,
    schema: Type[T],
    *,
    agent: str | None = None,
    title: str = "jobfinder",
    files: list[Path] | None = None,
    timeout: int | None = None,
) -> T:
    """Run one opencode session and return its validated JSON result."""
    cfg = settings().opencode
    agent = agent or cfg.analyst_agent
    timeout = timeout or cfg.timeout_seconds

    budget = cfg.context_tokens
    used = estimate_tokens(prompt)
    if used > budget * 0.8:
        raise OpencodeError(
            f"prompt is ~{used} tokens, over 80% of the {budget}-token budget; "
            "reduce the batch size in config/settings.yaml"
        )

    config = _prepare_workdir(workdir, agent)
    env = dict(os.environ, OPENCODE_CONFIG=str(config))

    result_path = workdir / RESULT_NAME
    last_error = ""
    attempt_prompt = prompt

    for attempt in range(1, cfg.max_retries + 2):
        result_path.unlink(missing_ok=True)
        cmd = [
            cfg.binary, "run",
            "--pure",                    # no external plugins
            "--dir", str(workdir),
            "--agent", agent,
            "-m", cfg.model,
            "--auto",
            "--format", "json",
            "--title", f"{title} (try {attempt})",
            attempt_prompt,
        ]
        for f in files or []:
            cmd += ["-f", str(f)]

        started = time.time()
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
                cwd=str(workdir), env=env
            )
        except subprocess.TimeoutExpired:
            last_error = f"opencode timed out after {timeout}s"
            log.warning("%s: %s", title, last_error)
            continue
        elapsed = time.time() - started

        (workdir / f"attempt{attempt}.stdout.log").write_text(proc.stdout or "")
        (workdir / f"attempt{attempt}.stderr.log").write_text(proc.stderr or "")
        (workdir / f"attempt{attempt}.prompt.txt").write_text(attempt_prompt)
        log.info("%s: attempt %d finished in %.1fs (rc=%d)", title, attempt, elapsed, proc.returncode)

        # The contract is the file. stdout is opencode's own JSON event stream
        # under --format json, so nothing in it is ever a result: salvaging from
        # it once let an event object validate as an empty batch.
        payload = result_path.read_text().strip() if result_path.exists() else ""
        if not payload:
            last_error = f"no {RESULT_NAME} written (rc={proc.returncode})"
            if proc.returncode != 0 and proc.stderr:
                last_error += ": " + proc.stderr.strip().splitlines()[-1][:200]
        else:
            try:
                return schema.model_validate_json(payload)
            except ValidationError as exc:
                last_error = f"result did not match the required shape: {exc.errors()[:3]}"
            except Exception as exc:  # malformed JSON
                last_error = f"result was not valid JSON: {exc}"

        log.warning("%s attempt %d failed: %s", title, attempt, last_error)
        attempt_prompt = (
            f"{prompt}\n\n"
            f"## PREVIOUS ATTEMPT FAILED\n{last_error}\n"
            f"Write ONLY the file {RESULT_NAME} in the current directory, containing "
            f"a single valid JSON object matching the schema above. No prose, no "
            f"markdown fences, no extra keys."
        )

    raise OpencodeError(f"{title}: giving up after {cfg.max_retries + 1} attempts: {last_error}")


def health_check() -> dict[str, object]:
    """Is the binary present and is the model server answering?"""
    cfg = settings().opencode
    out: dict[str, object] = {"binary": shutil.which(cfg.binary) or cfg.binary}
    try:
        proc = subprocess.run(
            [cfg.binary, "--version"], capture_output=True, text=True, timeout=30
        )
        out["version"] = proc.stdout.strip() or proc.stderr.strip()
        out["ok"] = proc.returncode == 0
    except Exception as exc:
        out["ok"] = False
        out["error"] = str(exc)
    return out
