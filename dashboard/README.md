# SFIP Dashboard (frontend-only)

Read-only React observation UI for the SFIP autonomous image-forensics
pipeline. It never mutates anything — all investigation work happens in the
Python backend; this app only visualizes `/api/*` and `/media/*`.

## Run

```bash
npm install
npm run dev      # http://localhost:5173, proxies /api + /media to the backend at 127.0.0.1:8000
npm run build    # production bundle into dist/
```

The backend (separate repository) must be running:
`python backend/app/main.py serve` → http://127.0.0.1:8000

## Smoke test

Renders the real evidence graph against the live API in Node/jsdom:

```bash
SFIP_BASE=http://127.0.0.1:8000 npm run smoke
```

## Tabs

Overview (controller phase, budgets, verdicts, activity) · Candidates
(ranked pairs, signals, evidence trail, provenance chips) · Papers (corpus) ·
Graph (pan/zoom evidence graph with click-to-inspect + deep links) ·
Reports (rendered markdown) · Activity (live SSE event feed).
