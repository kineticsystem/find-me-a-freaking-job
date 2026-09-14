# Web UI

React + TypeScript + Vite, managed with pnpm. Talks to the FastAPI backend in `../jobfinder`. Responsive: two-column cards on desktop, single column with a collapsible filter panel on a phone.

## Development

```bash
cd web
pnpm install
pnpm dev            # http://localhost:5173, proxies API calls to :8099
```

Run the backend alongside it: `python -m jobfinder serve` from the repo root. `pnpm dev` binds to all interfaces, so a phone on the same Wi-Fi can open the dev server too.

## Production

```bash
pnpm build          # writes web/dist
```

When `web/dist/index.html` exists, the FastAPI server serves it at `/` on the same port as the API. One process, one port, nothing else to run — that is the thing you expose to the internet.

## Tests

```bash
pnpm e2e            # needs the backend up on :8099
```

Headless Chromium drives the real UI against the real API: search, every filter, sorting, pagination, archive/unarchive, shortlist → applied → reset, the delete confirmation, the archive-older-than action, and the phone layout (single column, no horizontal overflow, ≥40px tap targets). Every mutation is reversed, so the database is unchanged afterwards. Screenshots land in `e2e/screenshots/`. The backend's own contract tests are in `../tests`.
