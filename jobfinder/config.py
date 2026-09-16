"""Typed configuration loaded from the YAML files in config/."""

from __future__ import annotations

import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


class LlmSettings(BaseModel):
    """The local model server. This is what the generated opencode config uses;
    it no longer depends on the user's own ~/.config/opencode/opencode.json."""

    base_url: str = "http://127.0.0.1:8084/v1"
    model: str = "Qwen3.8-27B"        # the name llama.cpp serves it under
    context_tokens: int = 120000       # what the server's slot actually holds
    output_tokens: int = 8192
    api_key: str = "local"
    # How long a run waits for a server that answers 503 (model loading)
    # before giving up on the LLM stages for that run.
    startup_wait_seconds: int = 300

    @property
    def provider_block(self) -> dict[str, Any]:
        return {
            "llamacpp": {
                "name": "llama.cpp",
                "npm": "@ai-sdk/openai-compatible",
                "options": {"baseURL": self.base_url, "apiKey": self.api_key, "includeUsage": True},
                "models": {
                    self.model: {
                        "name": self.model,
                        "tool_call": True,
                        "limit": {"context": self.context_tokens, "output": self.output_tokens},
                    }
                },
            }
        }

    @property
    def opencode_model(self) -> str:
        return f"llamacpp/{self.model}"


class OpencodeSettings(BaseModel):
    binary: str = "opencode"
    model: str | None = None            # defaults to llm.opencode_model
    analyst_agent: str = "job-analyst"
    explorer_agent: str = "job-explorer"
    timeout_seconds: int = 900
    max_retries: int = 2
    context_tokens: int = 60000


class Limits(BaseModel):
    triage_batch_size: int = 12
    max_jobs_per_run: int = 300
    deepdive_top_n: int = 10
    deepdive_min_score: int = 65
    posting_chars: int = 20000
    digest_min_score: int = 60
    max_extract_batches: int = 3
    rejections_in_prompt: int = 20    # recent dismissals fed back into every judgement
    notes_chars: int = 6000           # the candidate's notes go into every judgement verbatim, up to this


class DiscoverySettings(BaseModel):
    enabled: bool = True
    queries_per_run: int = 8
    results_per_query: int = 15
    max_new_boards_per_run: int = 10
    explorer_enabled: bool = True
    # Optional private search endpoint with a {q} placeholder, e.g. a SearXNG
    # instance: the public engines captcha-gate headless clients.
    search_endpoint: str | None = None


class HttpSettings(BaseModel):
    user_agent: str = "jobfinder/0.1"
    timeout_seconds: int = 30
    max_concurrency: int = 5
    politeness_delay_seconds: float = 1.0


class ApiSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8099
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])


class Paths(BaseModel):
    db: str = "data/jobs.db"
    runs: str = "runs"

    def resolve(self, attr: str) -> Path:
        p = Path(getattr(self, attr))
        return p if p.is_absolute() else ROOT / p


class Settings(BaseModel):
    interval_minutes: int = 720
    run_on_start: bool = True
    llm: LlmSettings = Field(default_factory=LlmSettings)
    opencode: OpencodeSettings = Field(default_factory=OpencodeSettings)

    def model_post_init(self, __context: Any) -> None:
        # Environment wins over YAML for the two things that differ between a
        # laptop and a container: where the model is, and where to listen.
        if url := os.environ.get("JOBFINDER_LLM_BASE_URL"):
            self.llm.base_url = url
        if host := os.environ.get("JOBFINDER_API_HOST"):
            self.api.host = host
        if port := os.environ.get("JOBFINDER_API_PORT"):
            self.api.port = int(port)
        if self.opencode.model is None:
            self.opencode.model = self.llm.opencode_model
    limits: Limits = Field(default_factory=Limits)
    discovery: DiscoverySettings = Field(default_factory=DiscoverySettings)
    http: HttpSettings = Field(default_factory=HttpSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    paths: Paths = Field(default_factory=Paths)


class LocationRule(BaseModel):
    country: str | None = None
    region: str | None = None
    remote: Literal["required", "any", "no"] = "any"
    # 1 is the market to search first. Equal-quality roles in a lower-numbered
    # market outrank those in a higher one; see prompts._context_block.
    priority: int = 99
    note: str | None = None


class Salary(BaseModel):
    amount: float
    currency: str = "USD"
    period: Literal["year", "month", "day", "hour"] = "year"


class Preferences(BaseModel):
    based_in: str = ""
    citizenship: list[str] = Field(default_factory=list)
    work_authorization_notes: str = ""
    titles: list[str] = Field(default_factory=list)
    seniority: list[str] = Field(default_factory=list)
    location_rules: list[LocationRule] = Field(default_factory=list)
    market_priority: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    must_have: list[str] = Field(default_factory=list)
    nice_to_have: list[str] = Field(default_factory=list)
    dealbreakers: list[str] = Field(default_factory=list)
    min_salary: Salary | None = None

    def normalised(self) -> "Preferences":
        """location_rules in list order define the market priority: number
        them 1..n and derive market_priority from them, so the two never
        disagree. Used when the form saves."""
        rules = [r.model_copy(update={"priority": i}) for i, r in enumerate(self.location_rules, start=1)]
        labels = [r.country or r.region or "" for r in rules]
        return self.model_copy(update={"location_rules": rules, "market_priority": [l for l in labels if l]})

    def as_prompt_block(self) -> str:
        """Compact, model-readable rendering. Kept small on purpose."""
        return json.dumps(
            self.model_dump(exclude_none=True, exclude_defaults=False),
            indent=2,
            ensure_ascii=False,
        )

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.as_prompt_block().encode()).hexdigest()[:16]


class SourceConfig(BaseModel):
    id: str
    type: str
    enabled: bool = True
    slug: str | None = None
    url: str | None = None
    feed: str | None = None
    hint: str | None = None
    pages: int = 1
    max_comments: int = 120

    model_config = {"extra": "allow"}


class ConfigError(Exception):
    """A config file that cannot be used. Raised, never worked around: the
    process stops at startup with the message, and a reload keeps the last
    good values and reports it."""

    def __init__(self, file: str, detail: str) -> None:
        self.file = file
        self.detail = detail
        super().__init__(f"config/{file}: {detail}")

    @property
    def message(self) -> str:
        return f"config/{self.file} cannot be used: {self.detail}"


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open() as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError("the file must be a mapping of settings, not a list or a scalar")
    return data


def _describe(exc: Exception) -> str:
    if isinstance(exc, yaml.YAMLError):
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        return f"not valid YAML{where}: {getattr(exc, 'problem', None) or exc}"
    if isinstance(exc, ValidationError):
        parts = [f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()[:3]]
        return "invalid values: " + "; ".join(parts)
    return f"{type(exc).__name__}: {exc}"


def _load_model(name: str, model: type) -> Any:
    try:
        return model.model_validate(_load_yaml(CONFIG_DIR / name))
    except Exception as exc:  # noqa: BLE001 - re-raised as one clear error
        raise ConfigError(name, _describe(exc)) from exc


@lru_cache(maxsize=1)
def settings() -> Settings:
    return _load_model("settings.yaml", Settings)


DEFAULT_USER_ID = 1
_prefs_cache: dict[int, Preferences] = {}


def preferences(user_id: int = DEFAULT_USER_ID) -> Preferences:
    """The user's preferences, from the database. A user who has never
    saved any gets the empty defaults (and is "not set up" until they do)."""
    if user_id not in _prefs_cache:
        _prefs_cache[user_id] = _load_preferences(user_id)
    return _prefs_cache[user_id]


preferences.cache_clear = _prefs_cache.clear  # type: ignore[attr-defined]


def _load_preferences(user_id: int) -> Preferences:
    from . import db

    doc = db.get_preferences_doc(user_id)
    if doc is None and user_id == DEFAULT_USER_ID:
        doc = import_preferences_file()
    if doc is None:
        return Preferences()
    try:
        return Preferences.model_validate(doc)
    except ValidationError as exc:
        raise ConfigError(f"user {user_id} preferences (database)", _describe(exc)) from exc


def import_preferences_file() -> dict[str, Any] | None:
    """One-time migration: the pre-database config/preferences.yaml becomes the
    default user's document, and the file is renamed so it is plainly no
    longer read. Returns the document, or None if there was no file."""
    import logging

    from . import db

    path = preferences_path()
    if not path.exists():
        return None
    prefs = _load_model("preferences.yaml", Preferences)     # a broken file stops the start, as before
    doc = prefs.model_dump(exclude_none=True)
    db.save_preferences_doc(DEFAULT_USER_ID, doc)
    renamed = path.with_name(path.name + ".imported")
    path.rename(renamed)
    logging.getLogger(__name__).warning(
        "imported %s into the database for user %d and renamed it to %s; the file is no longer read",
        path.name, DEFAULT_USER_ID, renamed.name)
    return doc


def all_preferences() -> list[Preferences]:
    """Every user's preferences (for the shared fetch stage)."""
    from . import db

    out = []
    for user_id, _ in db.all_preferences_docs():
        try:
            out.append(preferences(user_id))
        except ConfigError:
            continue
    return out


def save_preferences(prefs: "Preferences", user_id: int = DEFAULT_USER_ID) -> None:
    """Validated model in, document out. Invalidates the cache."""
    from . import db

    db.save_preferences_doc(user_id, prefs.model_dump(exclude_none=True))
    _prefs_cache.pop(user_id, None)


# The user's own files, and the checked-in template each is created from.
USER_FILES: tuple[tuple[str, str], ...] = (
    ("config/settings.yaml", "config/settings.example.yaml"),
)


def ensure_user_files() -> list[str]:
    """Create any missing user file from its example. Returns what was
    created. Called before anything reads them, so a fresh clone starts."""
    import shutil

    created: list[str] = []
    for rel, example in USER_FILES:
        target, source = ROOT / rel, ROOT / example
        if not target.exists() and source.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(source, target)
            created.append(rel)
    return created


def check_config() -> list[ConfigError]:
    """Validate what is read at start: settings.yaml, and a preferences.yaml
    that is still waiting to be imported. Empty list means all usable."""
    errors: list[ConfigError] = []
    try:
        _load_model("settings.yaml", Settings)
    except ConfigError as exc:
        errors.append(exc)
    if preferences_path().exists():
        try:
            _load_model("preferences.yaml", Preferences)
        except ConfigError as exc:
            errors.append(exc)
    return errors


def seed_sources() -> list[SourceConfig]:
    data = _load_yaml(CONFIG_DIR / "sources.yaml")
    return [SourceConfig.model_validate(s) for s in data.get("sources", [])]


def reload() -> None:
    """Pick up edited files. Both are validated first; if either is broken
    the caches are left alone -- the last good values stay in force -- and
    the error is raised for the caller to report."""
    errors = check_config()
    if errors:
        raise errors[0]
    settings.cache_clear()
    _prefs_cache.clear()


def settings_path() -> Path:
    return CONFIG_DIR / "settings.yaml"


def preferences_path() -> Path:
    return CONFIG_DIR / "preferences.yaml"


def write_top_level_setting(key: str, value: int | float | str | bool) -> None:
    """Persist one top-level scalar into settings.yaml in place, preserving the
    file's comments and layout; a missing key is appended. Used by the API for
    the few settings that are editable from the UI."""
    import re

    import logging

    path = settings_path()
    text = path.read_text() if path.exists() else ""
    rendered = str(value).lower() if isinstance(value, bool) else str(value)
    logging.getLogger(__name__).info("writing %s: %s = %s", path.name, key, rendered)
    # value, then an optional trailing comment that must stay separated from
    # the new value by whitespace (YAML needs " #" to start a comment).
    pattern = re.compile(rf"^{re.escape(key)}:[ \t]*[^#\n]*?[ \t]*(#[^\n]*)?$", re.M)
    m = pattern.search(text)
    if m:
        comment = f"    {m.group(1)}" if m.group(1) else ""
        text = text[: m.start()] + f"{key}: {rendered}{comment}" + text[m.end():]
    else:
        text = text.rstrip("\n") + f"\n{key}: {rendered}\n"
    path.write_text(text)
    reload()

