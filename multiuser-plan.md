# Multi-user plan

Working notes for the `multi-users` branch. A discussion document, kept current as decisions are made; nothing here is implemented until it says so.

**Implemented so far:** Decision 1 (preferences in the database, `users` table with the default user, one-time import of the YAML file).

## Goal

One installation, several people, each logged in with their own CV, notes, preferences, scores and decisions. Postings and the model server are shared.

## What changes shape

Today several things are singletons — one file, one folder, one row. Per user, each becomes a row keyed by a user id.

| Today | Per user | Stays shared |
|---|---|---|
| `config/preferences.yaml` | `user_preferences` row | |
| `profile/notes.md` | `user_notes` row | |
| `profile/cv.*` + `.cache/profile.json` | `user_cv` (file or blob) + digest row | |
| `user_state` (shortlisted / applied / dismissed + reason) | gains `user_id` | |
| `evaluations` (scores) | gains `user_id`: a score is a (job, user, criteria) fact | |
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

1. **Users and login.** A `users` table (id, email, display name, password hash or external identity, created_at) and a session mechanism. Decide: local passwords, or an identity provider (Cloudflare Access already fronts the app when exposed). Not decided.
2. **The CV.** File on disk under `profile/<user_id>/` or a blob in the database. Files are simpler for the PDF parser and for size; the digest cache becomes a row either way. Not decided.
3. **The scan.** Decided: see Decision 3.
4. **Sources.** Decided: see Decision 2. Open detail: whether a user can *remove* a registry row (only if nobody else follows it) or only unfollow.
5. **Rejections block and stale scores.** Already keyed by user once `user_state` and `evaluations` carry `user_id`; no new design needed.
6. **Migration of the current single-user data.** On first start of the multi-user version, create user 1 from the existing files and rows so nothing is lost. Needed.
7. **The web app.** A login screen; everything else stays as it is, scoped by the session's user.

## Not changing

The pipeline stages, the sources, the discovery channels, the opencode integration, the Docker packaging.
