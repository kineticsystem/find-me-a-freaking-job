# Multi-user plan

Working notes for the `multi-users` branch. A discussion document, kept current as decisions are made; nothing here is implemented until it says so.

**Implemented so far:** Decision 1 (preferences in the database, `users` table with the default user, one-time import of the YAML file); `user_id` on `evaluations` and `user_state` (key `(job_id, user_id)`), every query per user; old databases rebuilt in place with every row becoming user 1's. Login, phase one (open point 1): admin-created accounts, Argon2id password hashes, opaque bearer tokens stored hashed, `current_user` on every route, admin-only for whatever is still shared (settings, profile files, sources, scans, resets), a login / first-account screen and Users + Account sections in the web app, `jobfinder.sh create-user`. The first account claims user 1 and so owns the existing data (open point 6, for the rows). Open point 2 (the CV and the notes): in the database, one `user_profile` row per user holding the uploaded CV, its extracted text, the notes and the digest; the `profile/` folder is gone. The pipeline fetches once and then scores for every user with a complete profile, each under their own criteria (the first half of Decision 3: no queue yet, one run does everyone in turn; and of Decision 4: keyword queries per user, the prefilter passes a posting for anyone). Decision 2 (sources): `user_sources` and `job_sources` exist, following is per user with the defaults below, visibility and scoring follow it, keyword sources are registered per user. Still shared and admin-only: the schedule, scans, resets.

## Goal

One installation, several people, each logged in with their own CV, notes, preferences, scores and decisions. Postings and the model server are shared.

## What changes shape

Today several things are singletons — one file, one folder, one row. Per user, each becomes a row keyed by a user id.

| Today | Per user | Stays shared |
|---|---|---|
| `config/preferences.yaml` | `user_preferences` row | |
| `profile/notes.md` | `user_notes` row | |
| `profile/cv.*` + `.cache/profile.json` | `user_cv` (file or blob) + digest row | |
| `user_state` (shortlisted / applied / dismissed + reason) | keyed by `(job_id, user_id)` — done | |
| `evaluations` (scores) | `user_id` column: a score is a (job, user, criteria) fact — done | |
| criteria hash | computed per user from their preferences + CV digest | |
| `jobs` | | shared: a posting is the same for everyone |
| `sources` registry (what a source is, how to fetch it, its health) | `user_sources` overlay: who follows it | the registry row is shared |
| `config/settings.yaml` (interval, model) | | shared: the installation's, not a user's |
| `config/sources.yaml` (seed list) | | shared |

Model cost is per user, but over each user's own slice of the postings (see Decision 4), not over everything. Fetching is per source: once for a source with no search keys however many users follow it, per user for a source queried with that user's keys (Decision 4).

## Decision 1 — preferences go into the database as a JSON document

One row per user in `user_preferences`: `user_id` (PK, FK to users), `data` (JSON text: the whole `Preferences` object), `schema_version` (int), `updated_at`.

Why a document and not tables: the application never queries inside preferences. It loads the whole object for one user, gives it to the model, and hashes it for the criteria. Normalising into `preference_titles`, `location_rules` and the rest would add a join per field and a migration per new field for no query that anyone needs. The pydantic `Preferences` model already is the schema: validate on write (`PUT /preferences` does), `model_validate` on read, and only what the model accepted is ever stored. The YAML file was a document too; this is the same design in a different container. SQLite's JSON functions are available if a query inside the document is ever needed.

`schema_version` is there so a future change to the model can migrate stored documents lazily on read instead of with a bulk migration.

The same shape serves the other per-user documents: `user_notes` (`user_id`, `text`, `updated_at`) and, if ever wanted, per-user settings.

## Decision 2 — sources: the registry is shared, following is per user

A source is two things. What it is — type, slug, how to fetch it, last error, failure count — is shared in `sources`, so a company two users both follow is fetched once and each of its postings exists once. Who follows it is per user, in `user_sources` (`user_id`, `source_id`, `enabled`).

A source is fetched if at least one user follows it. Its postings appear in, and are scored for, only the users who follow it — which also keeps one user's interests from costing everyone else model time.

Defaults, by how a source arrives:

- Seed list (`config/sources.yaml`): followed by everyone by default; anyone can switch one off for themselves.
- Added by a user (a pasted careers URL): followed by that user only. Another user pasting the same URL starts following the existing registry row rather than creating a second one.
- Discovered by harvest: inherits the followers of the source whose posting it was found in. A board found in the shared HN thread is followed by everyone; a board found on a source only one user follows is followed by that user only. Discovery cannot leak one person's interests into another's list.

## Decision 3 — scans become a queue with one worker per model

What the current code does: `run_once` takes a lock with `blocking=False` and returns `skipped` if a run is going; `POST /runs` answers 409; the scheduler tick is `max_instances=1`, so a tick that fires during a run is dropped, not deferred. Nothing is queued. With two users, the second to press Scan now is refused and must come back later, and a scheduled scan can silently miss a whole interval.

A scan today is monolithic: fetch, then score for the one user. Those are different kinds of work with different sharing, so the queued unit changes:

- **fetch** — keyless sources once each (the union of everyone's followed sources); keyed sources once per user with that user's keys. One fetch at a time; a request while one is pending joins it.
- **score(user)** — per user; at most one pending entry per user; scores that user's candidate set (Decision 4), i.e. only what has no score under their current criteria.

A `queue` table (id, kind, user_id, requested_at, requested_by [manual | scheduled], started_at, finished_at, status, progress) so it survives a restart and the UI can show position and estimate. A single worker thread drains it, because there is one GPU; when there are several model endpoints the worker count becomes the number of endpoints. Scan now enqueues instead of running or refusing; the scheduled tick enqueues a fetch plus a score for every user.

Fairness: interleave users at batch granularity (A1, B1, A2, B2, …) rather than FIFO by user, so everyone sees scores within minutes rather than one user waiting for another's whole run. Manual requests go ahead of scheduled ones. Stop cancels the current user's entry only.

Progress and the Stop button, already per run, become per queue entry.

## Decision 4 — the model only ever sees a user's own slice

Different users want different things — a gardener and a developer share nothing but, perhaps, a bulk aggregator. The model must not score everything for everyone. Per user, a funnel in which only the last step costs GPU:

1. Fetch. Two kinds of source. **Keyless** — a company board returns every opening at the company, a feed returns everything it has, the HN thread is the whole thread; there is nothing to search, so fetching it twice downloads the same list twice: once, then each follower takes their slice. **Keyed** — the keyword channel already queries Jobicy with `tag` and `geo` built from each user's own CV vocabulary; the gardener's `tag=horticulture` and the developer's `tag=c++` are two different fetches and run per user, as would any future source with a search API. Both kinds land in the shared postings table; a posting found by one user's query belongs to a query source only that user follows.
2. Candidate set: postings from the sources this user follows (Decision 2). Nothing from anyone else's boards.
3. Prefilter: the existing cheap keyword gate, in Python, built from this user's titles, skills and CV keywords. Drops the obviously irrelevant before any model call — the developers from the gardener's aggregator slice, and vice versa.
4. Score: the model sees only what survived, and only what has no score under this user's current criteria.

Storage stays shared: a posting is stored once and each user's slice is a query over it, not a copy.

**Required change:** the prefilter (`jobfinder/prefilter.py`) is software-centric today — a hardcoded list of technology words that count as a signal, and a hardcoded exclusion list (nurse, welder, barista, ...). That would discard every gardening job for a gardener. It must be built only from the user's own vocabulary, with nothing hardcoded about any field.

## Open points, in the order they need answering

1. **Users and login.** Decided in principle: bearer tokens in the `Authorization` header, never in the URL (a query-string token leaks into access logs, browser history and the `Referer` of every Apply link). Opaque tokens rather than JWT: a random 256-bit value stored hashed in `api_tokens` (id, user_id, token_hash, name, created_at, expires_at, last_used_at), looked up per request — microseconds in SQLite — and revocable per token, so "log out this device" and "log out everywhere" are one DELETE each; JWT's statelessness buys nothing for one process on one file and costs revocation. The same table serves personal access tokens for scripts and curl. Flow: `POST /auth/login` (email + password, Argon2 via `pwdlib`) returns `{token, expires_at}`; `POST /auth/logout` deletes the row; `GET /auth/me`; a `current_user` dependency on every route supplies the `user_id` the queries already accept. The web app keeps the token in localStorage and sends the header — readable by page scripts, unlike an HttpOnly cookie, an accepted trade since the app loads no third-party scripts. Optional: a request carrying a valid Cloudflare Access `Cf-Access-Jwt-Assertion` header counts as authenticated for the tunnel path. Decided and done: admin-created accounts, no self-registration, no second factor; the first account (UI or `create-user`) claims user 1 and with it the existing data. Not done: the Cloudflare Access header (the app's own login is enough for the tunnel path too). Parked, deliberately, while experimenting: a devices list under Account with per-token revoke (the table already holds name, created_at, last_used_at); a README warning that plain HTTP on a shared network exposes the token; a shorter token lifetime with sliding renewal.
2. **The CV.** Decided and done: a blob in the database (`user_profile`), with the extracted text and the digest beside it. pypdf reads from memory as happily as from a file, CVs are small, and the alternative left `jobs.db` an incomplete backup.
3. **The scan.** Decided: see Decision 3.
4. **Sources.** Done: see Decision 2. Removing a registry row is allowed only for its sole follower, and never for a seed row; everyone else unfollows. Known edge: a posting is attributed to every source it was seen from within a run, but a posting first seen from board A and later listed on board B is attributed to both only when B's fetch actually returns it, which it does on every scan, so this settles itself.
5. **Rejections block and stale scores.** Done: keyed by user through `user_state` and `evaluations`.
6. **Migration of the current single-user data.** Done: rows became user 1's when `user_id` arrived; the first account claims user 1; the author's own `profile/` files were imported into user 1's `user_profile` row and the folder removed; other pre-login installs re-upload in the web app.
7. **The web app.** Done: a login screen; everything else scoped by the token's user; the Profile section is everyone's, the installation's sections the admin's.

## Considered and deferred

**PostgreSQL instead of SQLite.** Not worth it at this shape: one process, one scan worker (one GPU), a few users who mostly read, a few hundred writes per scan; WAL mode already lets the scan write while the UI reads, blobs the size of a CV are faster in SQLite than on disk, and "back up `jobs.db` and you have everything" is a feature. Postgres starts to earn its keep only with several writers at once (multiple scan workers) or the app split into several processes; neither is planned. Switching would cost a driver change plus a review of ~60 SQLite-flavoured queries and a second container forever. Cheap moves to make first if ever needed: FTS5 for search past ~20K postings; keep the queue single-writer so the door stays open.

## Not changing

The pipeline stages, the sources, the discovery channels, the opencode integration, the Docker packaging.
