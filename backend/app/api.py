"""Read-only monitoring API + SPA host.

There are no mutating endpoints by design: during autonomous execution the
dashboard is an observation surface. Humans submit work via the CLI
(`python -m app.main investigate <seed>`); the loop never waits on them.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .config import settings
from .controller import Controller

_controller: Controller | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _controller
    db.init_db()
    _controller = Controller()
    _controller.start()
    yield
    if _controller:
        _controller.stop()


app = FastAPI(title="Scientific Image Forensics Pipeline — read-only dashboard",
              version="0.1", lifespan=lifespan)


def _budgets(inv_id: int) -> dict:
    c = db.all_counters(inv_id)
    inv = db.get_investigation(inv_id)
    started = inv["started_at"] if inv else None
    from datetime import datetime, timezone
    runtime = 0.0
    if started:
        runtime = round((datetime.now(timezone.utc) - datetime.fromisoformat(started)).total_seconds() / 60, 1)
    return {
        "papers": {"used": int(c.get("papers", 0)), "limit": settings.max_papers},
        "figures": {"used": int(c.get("figures", 0)), "limit": settings.max_figures},
        "comparisons": {"used": int(c.get("comparisons", 0)), "limit": settings.max_comparisons},
        "llm_calls": {"used": int(c.get("llm_calls", 0)), "limit": settings.max_llm_calls},
        "download_mb": {"used": round(c.get("bytes_downloaded", 0) / 1048576, 1), "limit": settings.max_download_mb},
        "runtime_min": {"used": runtime, "limit": settings.max_runtime_minutes},
    }


def _inv_or_404(inv_id: int) -> dict:
    inv = db.get_investigation(inv_id)
    if not inv:
        raise HTTPException(404, "investigation not found")
    return inv


@app.get("/api/health")
def health() -> dict:
    from . import llm
    return {"ok": True, "llm_available": llm.llm_available(), "model": settings.llm_model}


@app.get("/api/state")
def state() -> dict:
    invs = db.list_investigations()
    active = next((i for i in invs if i["status"] == "running"), None)
    out = {"investigations": invs, "active": None}
    if active:
        iid = active["id"]
        out["active"] = {
            "investigation": active,
            "budgets": _budgets(iid),
            "queue": db.queue_stats(iid),
            "verdicts": _verdict_counts(iid),
            "recent_events": db.query(
                "SELECT id, ts, level, actor, message FROM events "
                "WHERE investigation_id=? ORDER BY id DESC LIMIT 25", (iid,)),
        }
    return out


def _verdict_counts(inv_id: int) -> dict:
    rows = db.query("SELECT COALESCE(verdict,'unverified') AS v, COUNT(*) AS n FROM candidates "
                    "WHERE investigation_id=? GROUP BY v", (inv_id,))
    return {r["v"]: r["n"] for r in rows}


@app.get("/api/investigations")
def investigations() -> list[dict]:
    return db.list_investigations()


@app.get("/api/investigations/{inv_id}")
def investigation(inv_id: int) -> dict:
    inv = _inv_or_404(inv_id)
    return {"investigation": inv, "budgets": _budgets(inv_id),
            "queue": db.queue_stats(inv_id), "verdicts": _verdict_counts(inv_id),
            "counters": db.all_counters(inv_id)}


@app.get("/api/investigations/{inv_id}/papers")
def papers(inv_id: int) -> list[dict]:
    _inv_or_404(inv_id)
    return db.query("SELECT * FROM papers WHERE investigation_id=? ORDER BY id", (inv_id,))


@app.get("/api/investigations/{inv_id}/figures")
def figures(inv_id: int, limit: int = Query(500, le=2000)) -> list[dict]:
    _inv_or_404(inv_id)
    return db.query("SELECT * FROM figures WHERE investigation_id=? ORDER BY id LIMIT ?", (inv_id, limit))


@app.get("/api/investigations/{inv_id}/candidates")
def candidates(inv_id: int, verdict: str | None = None, min_score: float = 0.0,
               limit: int = Query(100, le=500), offset: int = 0) -> dict:
    _inv_or_404(inv_id)
    where = "c.investigation_id=? AND COALESCE(c.score,0)>=?"
    params: list = [inv_id, min_score]
    if verdict:
        where += " AND c.verdict=?"
        params.append(verdict)
    rows = db.query(
        f"SELECT c.*, fa.paper_id AS paper_a, fb.paper_id AS paper_b, "
        f"fa.image_path AS image_a, fb.image_path AS image_b, "
        f"fa.width AS wa, fa.height AS ha, fb.width AS wb, fb.height AS hb "
        f"FROM candidates c JOIN figures fa ON fa.id=c.figure_a JOIN figures fb ON fb.id=c.figure_b "
        f"WHERE {where} ORDER BY c.score DESC LIMIT ? OFFSET ?", (*params, limit, offset))
    total = db.query_one(f"SELECT COUNT(*) AS n FROM candidates c WHERE {where}", params)["n"]
    return {"total": total, "items": rows}


@app.get("/api/candidates/{cand_id}")
def candidate(cand_id: int) -> dict:
    c = db.query_one("SELECT * FROM candidates WHERE id=?", (cand_id,))
    if not c:
        raise HTTPException(404, "candidate not found")
    fa = db.query_one("SELECT * FROM figures WHERE id=?", (c["figure_a"],))
    fb = db.query_one("SELECT * FROM figures WHERE id=?", (c["figure_b"],))
    pa = db.query_one("SELECT * FROM papers WHERE id=?", (fa["paper_id"],)) if fa else None
    pb = db.query_one("SELECT * FROM papers WHERE id=?", (fb["paper_id"],)) if fb else None
    evidence = db.query("SELECT * FROM evidence WHERE subject_type='candidate' AND subject_id=? ORDER BY id",
                        (cand_id,))
    return {"candidate": c, "figure_a": fa, "figure_b": fb, "paper_a": pa, "paper_b": pb,
            "evidence": evidence,
            "interpretation": json.loads(c.get("interpretation_json") or "null")}


@app.get("/api/investigations/{inv_id}/events")
def events(inv_id: int, after: int = 0, limit: int = Query(200, le=1000)) -> list[dict]:
    _inv_or_404(inv_id)
    return db.query("SELECT * FROM events WHERE investigation_id=? AND id>? ORDER BY id LIMIT ?",
                    (inv_id, after, limit))


@app.get("/api/investigations/{inv_id}/graph")
def graph(inv_id: int) -> dict:
    _inv_or_404(inv_id)
    papers_r = db.query("SELECT id, title, year, doi FROM papers WHERE investigation_id=?", (inv_id,))
    figs = db.query("SELECT id, paper_id, page, width, height, image_path FROM figures "
                    "WHERE investigation_id=? ORDER BY id LIMIT 800", (inv_id,))
    cands = db.query("SELECT id, figure_a, figure_b, verdict, score, status FROM candidates "
                     "WHERE investigation_id=?", (inv_id,))
    nodes = ([{"id": f"p{p['id']}", "type": "paper", "label": (p["title"] or "")[:60], "year": p["year"]}
              for p in papers_r]
             + [{"id": f"f{f['id']}", "type": "figure", "paper": f"p{f['paper_id']}",
                 "label": f"#{f['id']}", "page": f["page"],
                 "thumb": _media_rel(f["image_path"])} for f in figs]
             + [{"id": f"c{c['id']}", "type": "candidate", "verdict": c["verdict"], "score": c["score"],
                 "label": f"#{c['id']}"} for c in cands])
    edges = [{"source": f"f{f['id']}", "target": f"p{f['paper_id']}", "type": "contains"} for f in figs]
    for c in cands:
        edges.append({"source": f"c{c['id']}", "target": f"f{c['figure_a']}", "type": "similarity",
                      "score": c["score"], "verdict": c["verdict"]})
        edges.append({"source": f"c{c['id']}", "target": f"f{c['figure_b']}", "type": "similarity",
                      "score": c["score"], "verdict": c["verdict"]})
    return {"nodes": nodes, "edges": edges}


def _media_rel(path: str | None) -> str | None:
    if not path:
        return None
    try:
        return "/media/" + str(Path(path).resolve().relative_to(settings.data_dir.resolve()))
    except Exception:
        return None


@app.get("/api/investigations/{inv_id}/reports")
def reports(inv_id: int) -> list[dict]:
    _inv_or_404(inv_id)
    return db.query("SELECT * FROM reports WHERE investigation_id=? ORDER BY id DESC", (inv_id,))


@app.get("/api/reports/{report_id}")
def report(report_id: int) -> dict:
    r = db.query_one("SELECT * FROM reports WHERE id=?", (report_id,))
    if not r:
        raise HTTPException(404, "report not found")
    try:
        md = Path(r["path"]).read_text(encoding="utf-8")
    except OSError as e:
        raise HTTPException(500, f"report file unreadable: {e}")
    return {"report": r, "markdown": md}


@app.get("/api/export/{inv_id}")
def export(inv_id: int) -> StreamingResponse:
    _inv_or_404(inv_id)
    payload = {
        "investigation": db.get_investigation(inv_id),
        "budgets": _budgets(inv_id),
        "papers": db.query("SELECT * FROM papers WHERE investigation_id=?", (inv_id,)),
        "figures": db.query("SELECT * FROM figures WHERE investigation_id=?", (inv_id,)),
        "candidates": db.query("SELECT * FROM candidates WHERE investigation_id=?", (inv_id,)),
        "evidence": db.query("SELECT * FROM evidence WHERE investigation_id=?", (inv_id,)),
        "events": db.query("SELECT * FROM events WHERE investigation_id=?", (inv_id,)),
        "reports": db.query("SELECT * FROM reports WHERE investigation_id=?", (inv_id,)),
    }

    def gen():
        yield json.dumps(payload, default=str, indent=1)

    return StreamingResponse(gen(), media_type="application/json",
                             headers={"Content-Disposition": f'attachment; filename="investigation_{inv_id}.json"'})


@app.get("/media/{path:path}")
def media(path: str) -> FileResponse:
    target = (settings.data_dir / path).resolve()
    if not str(target).startswith(str(settings.data_dir.resolve())) or not target.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(target)


# SPA (built React dashboard) — mounted last so /api and /media win.
if settings.static_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(settings.static_dir), html=True), name="spa")
