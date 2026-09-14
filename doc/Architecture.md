# Architecture

How the system is built and why. For installing and using it, see the [README](../README.md).

## The one design decision

Fetching is deterministic Python; judging is the local language model. Nothing else about the design matters as much.

A local model driving a browser through LinkedIn is the slowest, most fragile and most blockable way to find jobs, and it spends the scarce resource — inference time — on parsing HTML. Meanwhile the job boards that matter expose clean JSON: every Greenhouse, Lever and Ashby company board, and the big remote aggregators. So:

- **Python fetches.** Adapters pull structured postings from APIs and feeds. Raw HTML is stripped to text before storage. The model never sees markup.
- **The model judges.** Relevance against the CV, eligibility from the candidate's location and citizenship, a summary, the concerns. Things that need reading, not things that need an HTTP client.
- **The model also explores**, but only where deterministic fetching cannot reach: sites with no API, and prose adverts that need structuring.

## Pipeline

```
                 ┌─ discovery ─────────────────────────────────────────┐
                 │  A harvest   scan fetched postings for ATS links     │
                 │  B keyword   CV vocabulary -> filterable board APIs  │
                 │  C websearch search engines -> ATS URLs (best effort)│
                 │  D explorer  LLM browses an API-less site            │
                 └──────────────────────┬──────────────────────────────┘
                                        v
  sources ──────> fetch (threaded) ──> extract ──> prefilter ──> SQLite
                                       (LLM)      (Python)         │
                                                                   v
                                             triage (LLM, batched, cheap)
                                                                   │
                                                       score >= 65 v
                                             deep dive (LLM, 1 job/session)
                                                                   │
                                                                   v
                                                   digest.md + HTTP API + web UI
```

One run, in order (`jobfinder/pipeline/run.py`):

| Stage | Module | LLM | Purpose |
|---|---|---|---|
| profile | `pipeline/profile.py` | once, cached | CV + notes → a compact `ProfileDigest` (~800 tokens) injected into every later prompt. Rebuilt only when the CV or notes change (content hash). The notes themselves are also injected verbatim into every judgement (`limits.notes_chars`): the digest compresses, and nuance such as "knows X but is not a Y" must not be. |
| fetch | `pipeline/fetch.py`, `sources/` | no | Every enabled source in a thread pool. One failing board never fails the run; failures are recorded per source and a source is disabled after five in a row. |
| harvest | `discovery.py` | no | See Discovery. |
| extract | `pipeline/extract.py` | batched | Prose adverts (HN "Who is hiring" comments) → structured postings. Output-token bound, so batches are small and capped per run; each comment is only ever structured once. |
| prefilter | `prefilter.py` | no | Keyword gate. Bulk aggregators return every job on earth; this drops the medical coders before any inference is spent. Deliberately permissive — it removes the obviously irrelevant and leaves judgement to the model. |
| store | `pipeline/fetch.py`, `db.py` | no | Normalise, fingerprint, upsert. |
| triage | `pipeline/triage.py` | batched | 12 postings per session, title + company + location + 700-char excerpt each. Score 0–100 and a one-line reason. Cheap and wide. |
| deep dive | `pipeline/deepdive.py` | one per job | Only postings above `deepdive_min_score`, at most `deepdive_top_n`. The full posting (truncated at 20K chars), an explicit eligibility judgement, summary, salary, stack, concerns. Expensive and narrow. |
| websearch | `discovery.py` | queries only | See Discovery. |
| digest | `pipeline/digest.py` | no | `runs/<ts>/digest.md` and `runs/latest-digest.md`. |

The two-stage triage/deep-dive split is the cost model: a batched pass over everything, a per-item pass over survivors. The expensive stage sees the fewest items.

Every run gets a directory under `runs/` holding each session's prompt, the opencode stdout/stderr, and the `result.json` the model wrote. That is the audit trail; when a score is wrong, the directory shows why.

## Discovery

The system is not limited to a hardcoded source list. Four channels, kept independent so that one being blocked does not blind the others:

**A. Harvest** — every fetched posting's apply link and body text is scanned for Greenhouse / Lever / Ashby / Workday URLs. Recognised boards are registered as permanent sources. This is the channel that compounds: one aggregator hit at a company becomes that company's entire board, fetched directly, on the next run. The first run typically adds ten boards; HN comment bodies alone yielded Discord, Wikimedia, DuckDuckGo, Runway and Loft Orbital.

**B. Keyword** — the CV's own vocabulary (from the profile digest) is pushed through the aggregator APIs that honour filters (Jobicy's `tag` and `geo`). Ephemeral: these are queries, not stored sources.

**C. Web search** — classic search-engine scraping for ATS URLs. Kept, but best-effort: DuckDuckGo, searx, Brave and Startpage all captcha-gate a headless client, so this channel is allowed to return nothing without failing the run. A `search_endpoint` under `discovery` in settings can point it at a private SearXNG instance.

**D. Careers pages without an API** — `POST /sources` with any URL runs `discovery.sniff_ats`: the raw HTML is searched for board links and embeds; if the listings are built by JavaScript the page is rendered in headless Chromium (`jobfinder/render.py`) and the requests it makes are inspected, since a page fetches its jobs from the board's API; finally the three board APIs are probed with the domain name as the slug (boards are nearly always named after the company). A hit registers a proper board source. Only when all of that fails is the page itself registered as a `webpage` source: rendered on each scan into text plus its links (each link labelled with its row's text, so "View & Apply" becomes "Senior Engineer: View & Apply"), and handed as one entry to the extraction stage, which the model turns into postings. The older `llm_explorer` type, where the model drives `webfetch` itself, remains for hand-configured cases but is the slowest path and no longer the default.

Discovery bookkeeping lives in the `discovery_log` table: which queries have been tried, which boards have been seen, which HN comments have been structured.

## Sources

`jobfinder/sources/` — one class per adapter, registered by type name:

| Type | Kind | Notes |
|---|---|---|
| `greenhouse`, `lever`, `ashby` | company ATS boards | Public JSON, no key. The best data in the system: complete, structured, per company. Given a `slug`. |
| `workday` | company ATS board | The site's own JSON API: a paged list, then one request per posting for its description (capped per scan). Given `tenant`, `wd` and `site`, parsed from the site's URL. |
| `webpage` | rendered page | Headless Chromium renders the page; text and labelled links go to the extraction stage as one entry. For careers pages with no board behind them. |
| `remoteok`, `remotive`, `himalayas`, `themuse`, `arbeitnow`, `weworkremotely` | bulk aggregators | Everything they have, filtered locally. Remotive and TheMuse ignore their own filter parameters. |
| `jobicy` | filterable aggregator | The one whose `tag` / `geo` / `industry` filters work; used by the keyword channel. |
| `hn_hiring` | prose | Latest "Ask HN: Who is hiring?" via Algolia; comments emitted with `needs_extraction`. |
| `llm_explorer` | LLM | See Discovery D. |

LinkedIn and Indeed are deliberately absent. Both wall or block automated access; an adapter would be a scraper that silently returns nothing.

`config/sources.yaml` is the seed. On every run it is upserted into the `sources` table, which is the live registry. The file stays authoritative for *what* a config source is (type, slug, options) but not for whether it is on: `enabled` in the YAML applies only when the row is first created, and a source switched off in the UI stays off across re-syncs. Rows have an `origin` of `config`, `discovered` (harvest) or `user` (pasted into the UI); only the last two can be deleted, since the YAML would recreate a config one.

## opencode integration

`jobfinder/opencode.py` is the only place the model is called.

**Every call is a fresh, single-shot session.** Never `--continue`. A growing session is how a small context window is blown; a fresh one costs nothing but opencode's fixed system prompt.

**The model writes a file; Python reads it.** Each prompt ends with the instruction to write `result.json` in the working directory. Python validates it against a pydantic schema and, on failure, retries with the validation error appended. Parsing a small local model's prose is a losing game; reading a file it wrote with its own tool is reliable, and it stays on disk for the audit trail. The file is the whole contract: nothing is ever salvaged from stdout, which under `--format json` is opencode's own event stream — an earlier version did, and an event object once validated as an empty batch.

**Sessions run against a generated config, not the user's.** On startup the wrapper writes `runs/opencode-config.json`: a `provider` block built from the `llm` settings, the two agents from `.opencode/agent/*.md` inlined, `mcp` empty, `lsp` off. Sessions then run `opencode run --pure` with `OPENCODE_CONFIG` pointing at it.

This was forced by measurement. The interactive config loads three MCP servers through `npx` (playwright, pdf-reader, context). Booted in a fresh session directory they took over twenty minutes without a single request reaching the model, and left 63 MB of `node_modules` behind per directory. With the generated config the same session completes in about ten seconds. None of those servers are needed: this workload reads text and writes one JSON file.

**Two agents**, defined in `.opencode/agent/`:

- `job-analyst` — read/write only, no network. Triage, deep dive, extraction, profile distillation, query generation. Told to be sceptical and concise, to score only from the text given, and to cap any dealbreaker at 20.
- `job-explorer` — `webfetch` enabled. Discovery channel D only.

Edit the markdown files to change behaviour; the config is regenerated when they differ from what was last written.

## Context budget

llama.cpp reports three different context sizes for the same server: `opencode.json` may claim one number, `/v1/models` reports `n_ctx`, and `/slots` reports the per-slot `n_ctx` that actually governs a request. On the development machine those were 155K, 64K and 128K respectively. Settings assume a conservative 60K and the wrapper refuses to send a prompt over 80% of that rather than let the server truncate it silently.

opencode's own system prompt costs about 15K tokens per session before any task content. Rough per-session budgets on top of that: profile ~800 tokens, triage ~3–4K for 12 postings, deep dive ~6K for one posting. Raising `triage_batch_size` is the main lever; `runs/*/attempt1.prompt.txt` shows what is actually being sent.

The model is whatever llama.cpp serves; the wrapper only assumes an OpenAI-compatible endpoint and tool calling. Development used Qwen 3.8 27B on an RTX 4090 (24 GB), where generation runs at ~95 tok/s. What makes a session slow there is not generation but the model's thinking, which Qwen 3 leaves unbounded by default (`--reasoning-budget -1`). Extraction sessions, which produce a lot of JSON, take two to four minutes; triage and deep dive about one. A different model or card shifts all of these numbers.

## Storage

SQLite, one file, WAL mode, a fresh connection per operation so the scheduler thread and the API threads never share one. Schema in `jobfinder/db.py`.

| Table | Holds |
|---|---|
| `jobs` | One row per posting, unique on `fingerprint` = normalised company + title + location, so the same job on three boards collapses to one row. `first_seen`, `last_seen`, `seen_count` track its lifetime. |
| `evaluations` | One row per (job, stage, criteria). History is kept: a re-score under new preferences adds a row rather than overwriting. |
| `user_state` | Your decisions: `new`, `shortlisted`, `applied`, `dismissed`, `archived`, plus notes. Separate from `evaluations` on purpose — a re-run never touches it. |
| `runs` | Start, end, status, stats JSON, error. |
| `sources` | The live source registry (see Sources). |
| `discovery_log` | What discovery has already tried or seen. |

**Criteria hash.** Every evaluation is stamped with `sha256(profile_digest_hash | preferences_hash)`. A job "needs evaluation" when it has no row for the current hash. So editing the CV, the notes or `preferences.yaml` automatically makes every job eligible for re-scoring on the next run, and the old scores remain queryable.

**Feedback loop.** Dismissing a job records a free-text `reason` on `user_state` (the UI composes it from chips plus optional text). The most recent `limits.rejections_in_prompt` reasons are rendered into every triage and deep-dive prompt as a "postings the candidate rejected, with their reasons" block, phrased as guidance about taste rather than rules, so the model generalises ("avoids consultancies") without keyword-banning. Restoring a job clears its reason. The block is deliberately not part of the criteria hash: a dismissal should shape the scoring of new postings, not trigger a re-score of everything. If the list ever grows long enough to matter, the same distillation trick as the CV digest applies — collapse the reasons into a handful of rules once, cache them, inject those.

**Score precedence.** In the job list a deep-dive score supersedes the triage score it was derived from; the triage score stands in until a deep dive exists. When the criteria have changed since a job was scored, its most recent score under the old criteria is still shown, flagged `score_stale` and drawn faded, and still used for sorting and filtering — a preferences change must not blank the list until the next scan replaces the scores. `/health` reports `stale_scores`, and the UI offers to re-score at once. `dismissed` and `archived` jobs are skipped by both LLM stages.

## The user's files

Four files are the user's own and never belong in the repository: `config/settings.yaml`, `config/preferences.yaml`, `profile/notes.md` and `profile/cv.*`. The first three are created from checked-in `.example` templates by `config.ensure_user_files()`, which every CLI command runs before reading anything, so a fresh clone starts. All four are git-ignored, as are `data/` and `runs/`. The example notes file is a single HTML comment; `notes_text()` strips comments, so an untouched file reads as empty.

`profile.readiness()` reports whether the CV, the notes and the essential preferences (based in, titles) exist. `/health` exposes it, the UI shows a checklist banner until all three are done, and until then `run_once` does nothing at all — no fetch, no run record, just a log line — and `POST /runs` answers 409 naming what is missing.

## Configuration failures

`config/settings.yaml` and `config/preferences.yaml` are loaded through `config._load_model`, which turns any parse or validation failure into a `ConfigError` naming the file and the problem (line and column for YAML errors, field paths for validation errors). Nothing catches it to substitute defaults: running on values the user never wrote would be worse than not running. Every CLI command, and therefore the container's `start.sh` before it launches the model server, calls `check_config()` first and exits with code 2 and the message. `dock.sh start` notices the container not staying up and prints the last log lines, so the reason is visible without asking for the logs. At runtime, `reload()` validates before dropping the caches, so a hand edit that breaks a file while the server runs is reported by `POST /reload` (or by a settings change, which reloads) and the last good values stay in force.

## Server and API

FastAPI + APScheduler in one process (`jobfinder/api.py`, `jobfinder/scheduler.py`). The scheduler fires `run_once` on an interval with `max_instances=1` and `coalesce=True`: a run that outlasts the interval is never stacked, and missed ticks collapse into one. A process-wide lock makes a manual `POST /runs` and a scheduled tick mutually exclusive.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | scheduler state, next run, set-up checklist, live scan progress (`pipeline/progress.py`: stage, units done of total, an ETA from the mean unit time so far), counts |
| GET | `/jobs` | paged, ranked list; `q`, `status`, `remote`, `source`, `min_score`, `sort`, `include_archived`, `limit`, `offset`; returns `total` |
| GET | `/jobs/facets` | filter options with counts |
| GET | `/jobs/{id}` | one job with its full evaluation history |
| PATCH | `/jobs/{id}/state` | `{"status": …, "notes": …, "reason": …}`; reason kept while dismissed |
| GET | `/rejections` | the dismissals with reasons the model is currently shown |
| POST | `/jobs/archive` | `{"older_than_days": N}` and/or `{"ids": […]}`; the age rule only touches `new` jobs |
| DELETE | `/jobs/{id}` | permanent, cascades |
| GET / POST | `/runs` | history / trigger now |
| POST | `/runs/stop` | cooperative stop: the stages check a flag between units, and the in-flight opencode session's process group is killed so the wait is seconds; the run is recorded as `stopped` with what it scored |
| GET | `/digest` | latest Markdown digest |
| GET | `/sources` | the registry with per-source counts and health |
| POST | `/sources` | `{"url": …}` — register a Greenhouse / Lever / Ashby board from its careers URL; fetched once to check it answers and has openings |
| POST | `/sources/{id}/enabled?enabled=` · DELETE `/sources/{id}` | toggle · remove (not config-origin) |
| GET / PATCH | `/settings` | the interval and run-on-start; a PATCH rewrites the key in `config/settings.yaml` in place (comments kept) and reschedules the running scheduler, so no restart |
| GET | `/profile` | the CV on disk, the notes, the cached digest |
| POST | `/profile/cv` | multipart upload; saved as `profile/cv.<ext>`, previous CV removed, digest cache dropped |
| PUT | `/profile/notes` | replaces `profile/notes.md` |
| GET / PUT | `/preferences` | the parsed preferences plus the YAML text; PUT saves from the form, validated by the same model the pipeline reads, with `location_rules` order becoming the priority and `market_priority` derived from it |
| POST | `/reset/jobs` · `/reset/all` | `{"confirm": "DELETE"}` — delete every job, score, decision and run (sources and files stay) · that plus the sources and discovery memory, reseeded from config; both refused while a scan runs |
| POST | `/reload` | re-read YAML without a restart |

When `web/dist/index.html` exists the same app mounts `/assets` and serves `index.html` for any unmatched path, so the UI and the API share one port. API routes are declared first and take precedence.

## Web UI

`web/` — React 19, TypeScript, Vite, pnpm. No component library; the whole stylesheet is ~200 lines with light and dark from `prefers-color-scheme`.

Three files carry the logic: `App.tsx` (query state, paging, actions, toasts), `components/JobCard.tsx`, `components/Filters.tsx`. `api.ts` is the typed client; `types.ts` mirrors the API contract.

Responsive breakpoints: two card columns at ≥900px, one below; at ≤640px the filter panel collapses behind a button, the header wraps so search keeps full width, and every control is at least 40px tall.

State changes are optimistic in the sense that the list is updated from the server's acknowledgement rather than re-fetched: a job that leaves the current filter (archived while viewing active jobs) is removed from the list and the total decremented, without a round trip.

In development `pnpm dev` proxies API paths to `:8099`; in production the built files are served by FastAPI as above.

## Deployment

`docker/` holds a two-stage Dockerfile (Node builds `web/dist`; an `nvidia/cuda` *devel* image runs everything and can compile llama.cpp), a compose file, and `dock.sh`, a thin wrapper that supplies the container name and the host uid/gid.

One container runs three things: `llama-server` from the fork, the app server, and the web UI. `bin/start.sh` is the entrypoint; it starts the model server and the app and exits when either dies, so `restart: unless-stopped` brings both back together rather than leaving the app running against a dead model.

The split is code-in-image, state-on-host. Bind-mounted from the host: `config/` (the examples are tracked, the real files are not), `profile/`, `data/`, `runs/`, `bin/` (so the model command line can be tuned with a restart), `modules/llama.cpp` (the fork and its build tree), and the model caches `~/.cache/huggingface` and `~/.cache/llama.cpp`. Everything else is baked. The container runs as a user with the host's uid/gid so those directories stay owned by the host user.

The llama.cpp fork (`TheTom/llama-cpp-turboquant`, for its TurboQuant KV cache) is a git submodule at `modules/llama.cpp`, pinned to a commit. It is compiled inside the container by `bin/build-llama.sh` — CUDA on, `CMAKE_CUDA_ARCHITECTURES=native` so it targets the GPU present — into the bind-mounted submodule, which is what makes the build persistent: it survives container restarts, image rebuilds and `dock.sh clean`. `dock.sh build` compiles it if the binary is missing; `build-llama` forces it.

The model endpoint comes from `llm.base_url` in settings, overridable with `JOBFINDER_LLM_BASE_URL`; `JOBFINDER_API_HOST` likewise overrides where the server listens. The compose file uses host networking, so the model server is on the host's `:8084` — where it was before it moved into the container, so other tools on the machine that used it keep working — and reserves the GPU through the NVIDIA runtime.

Before a run spends any inference the pipeline checks that the model server answers (`opencode.llm_reachable`). A 503 means llama.cpp is still loading the model, normal when both start together, and the run waits for it (`llm.startup_wait_seconds`). If the server is unreachable, the run still fetches and stores, skips the LLM stages, and is recorded as `partial`: a downed model costs seconds per run rather than a minute per session times every batch.

## Working on the code

The container is the only supported way to run it; nothing is installed on the host. The image bakes the code, so the loop is edit, `./docker/dock.sh <name> build`, `start`. The build reuses cached layers: a Python-only change does not rebuild the web app, and llama.cpp is never rebuilt by `build` once its binary exists.

The API tests run inside the container: `./docker/dock.sh <name> shell -c test.sh`. The browser suite (`web/e2e/smoke.mjs`) needs Node and a Chromium download, so it is a development tool run from a machine that has them, against a running container: `cd web && pnpm install && pnpm e2e`.

The toolchain the image contains, for reference: Python 3.12 with the dependencies from `pyproject.toml` in a venv at `~/venv`; opencode pinned at 1.18.26 in `~/.opencode/bin`; the web app built by a Node 24 stage with pnpm and copied in as static files. opencode's own configuration is not used: the wrapper generates `runs/opencode-config.json` from the `llm` settings on every start.

## Tests

- `tests/` — pytest against a temporary SQLite file, exercising the API contract the UI depends on: ordering, score precedence, every filter, pagination totals, state round-trips, archive visibility and reversibility, bulk archive by id and by age (and that it spares shortlisted jobs), delete cascade, facets, and that the pipeline skips archived jobs.
- `web/e2e/smoke.mjs` — Playwright, headless Chromium, against a running server with real data. Drives every user action at desktop and phone viewports and reverses each one, so the database is unchanged afterwards. Also asserts the responsive invariants: column count, no horizontal overflow, collapsed filters, tap-target height.

Both suites have caught real bugs: NULL list fields crashing the card renderer, an age comparison that compared ISO timestamps lexically against SQLite's `datetime('now')` and so never matched, and 28px buttons on a phone.

## Layout

```
config/            settings.yaml, preferences.yaml, sources.yaml
profile/           your CV and notes (git-ignored); .cache/profile.json
jobfinder/         the Python package
  sources/         one adapter per source type
  pipeline/        one module per stage; run.py orchestrates
  opencode.py      the only place the model is called
  discovery.py     the four discovery channels
  db.py            schema and every query
  api.py           FastAPI app, static UI mount
  scheduler.py     APScheduler wiring
  cli.py           python -m jobfinder …
.opencode/agent/   the two agent prompts
web/               the React app
tests/             API contract tests
runs/              per-run audit trail and the generated opencode config (git-ignored)
data/jobs.db       the database (git-ignored)
```
