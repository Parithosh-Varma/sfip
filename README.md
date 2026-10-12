# SFIP — Scientific Forensic Image Pipeline

Autonomous investigation of figure-image similarities across scientific papers.
A user supplies a paper, DOI, or researcher name; the system discovers
accessible related papers, extracts figures, finds and verifies candidate
image matches, expands the investigation, builds an evidence graph, and
produces a reproducible report. **The human observes; the loop runs itself.**

## Autonomous loop

```
Observe → Plan → Execute → Verify → Score → Update state → Select next action → Repeat
```

- Persistent controller (`backend/app/controller.py`) with bounded retries,
  explicit budgets (`max_papers / max_figures / max_comparisons / max_llm_calls /
  max_download_mb / max_runtime_minutes`) and explicit stopping conditions
  (queues drained → `completed`; any budget exhausted → `stopped_budget`;
  wall-clock overrun → `stopped_runtime`).
- Crash-safe: all state in SQLite; interrupted jobs are requeued on startup.
- LLM (`meta/muse-glimmer-30b` via NVIDIA NIM, vision-capable) advises and
  interprets grounded in stored tool outputs — but deterministic heuristics
  are authoritative, so the loop never blocks on a model or a human.

## Signals (per figure pair)

Perceptual hashes (pHash/dHash/aHash) → gate sweep → deep battery: SSIM over
transformation variants (flips, exact + fine rotations ±15/30/45, stretch,
center crops), including a contrast-enhanced (CLAHE) channel that defeats
brightness/contrast concealment; HSV histogram intersection, ORB+RANSAC
geometric verification, multi-scale containment for partial overlap →
weighted fusion with false-positive heuristics (generic-content risk,
small figures, conflicting signals).

Within-image tamper screening (every figure): copy-move/clone-stamp detection
(ORB self-match with translation clustering, incl. mirrored clones with pixel
verification), western-blot anomaly checks (duplicated bands, splice edges).

AI-provenance screening: file-metadata forensics (Stable Diffusion chunks,
AI-tool tags, EXIF/XMP, C2PA manifests) on every figure; diffusion spectral
screen + real watermark-provider hook (never faked) on suspicious figures.

Methods inspired by the paper-mill forensics community (notably Sholto David's
video investigations): flip/rotate/overlay comparison, brightness-adjusted
inspection, clone-stamp and blot forensics, and SynthID/watermark checks.

Language discipline: the system reports *observed image similarities* and
characterizes evidence — it never equates similarity with fraud or intent.

## Quickstart

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
cd dashboard && npm install && npm run build && cd ..

cp .env.example .env        # add NVAPI_KEY (NVIDIA NIM); LLM optional — heuristics fallback otherwise
.venv/bin/python backend/app/main.py selftest     # offline check, no network
.venv/bin/python backend/app/main.py investigate "10.1038/s41586-020-03148-4"
# dashboard: http://127.0.0.1:8000  (read-only; the loop needs no clicks)
```

`serve` resumes all runs; `status` prints counters; `/api/export/{id}` dumps
the full evidence bundle (config snapshot, papers, figures, candidates,
evidence, audit log).

## Safeguards

Respects source rate limits, open-access only (no paywall/access bypass),
download/compute budgets, `.env` secrets never committed, and takes no
external consequential actions (no allegations published, no researchers contacted).

## Layout

`backend/app/` — `config.py` `db.py` `httpc.py` `sources.py` (OpenAlex/Crossref/Europe PMC)
`pdfs.py` `vision.py` `scoring.py` `tamper.py` (clone-stamp, blot forensics)
`provenance.py` (metadata/C2PA/diffusion-screen/SynthID hook) `llm.py`
`pipeline.py` `controller.py` `report.py` `api.py` (read-only + SSE) `main.py` ·
`dashboard/src/` — React observation UI · `tests/` — offline pytest suite
