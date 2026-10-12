"""Job handlers for the autonomous pipeline.

Each handler is a pure unit of work on (investigation_id, payload) -> result
dict. Failures raise; the controller owns retries/backoff. Handlers never
ask a human anything — every decision uses stored state, budgets, and scores.
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import db, httpc, llm, pdfs, sources, scoring, vision
from .config import settings
from .sources import DOI_RE


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _paper(inv_id: int, paper_id: int) -> Optional[dict]:
    return db.query_one("SELECT * FROM papers WHERE id=? AND investigation_id=?", (paper_id, inv_id))


def _fig(fig_id: int) -> Optional[dict]:
    return db.query_one("SELECT * FROM figures WHERE id=?", (fig_id,))


def _cand(cand_id: int) -> Optional[dict]:
    return db.query_one("SELECT * FROM candidates WHERE id=?", (cand_id,))


def _budget_left(inv_id: int, counter: str, limit: int) -> int:
    return int(limit - db.get_counter(inv_id, counter))


def _insert_paper(inv_id: int, p: dict, seed: bool = False) -> Optional[int]:
    if db.get_counter(inv_id, "papers") >= settings.max_papers:
        return None
    meta = dict(p.get("metadata") or {})
    meta["seed"] = seed
    pid = db.execute(
        "INSERT OR IGNORE INTO papers(investigation_id, doi, external_id, title, authors, year, venue, "
        "source, oa_url, pdf_url, landing_url, status, relevance, metadata_json, added_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (inv_id, p.get("doi"), p.get("external_id"), p.get("title"), p.get("authors"), p.get("year"),
         p.get("venue"), p.get("source"), p.get("oa_url"), p.get("pdf_url"), p.get("landing_url"),
         "discovered", float(p.get("relevance") or 0.0), json.dumps(meta), db.now_iso()),
    )
    if not pid or pid == -1:
        return None
    if db.query_one("SELECT id FROM papers WHERE id=?", (pid,)) is None:
        return None
    db.bump_counter(inv_id, "papers")
    return pid


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #

def h_discover_seed(inv_id: int, payload: dict) -> dict:
    inv = db.get_investigation(inv_id)
    seed, stype = inv["seed"], inv["seed_type"]
    papers: list[dict] = []

    if stype == "doi":
        p = sources.lookup_doi(seed)
        papers = [p] if p else []
    elif stype in ("url", "arxiv"):
        m = DOI_RE.search(seed)
        if m:
            p = sources.lookup_doi(m.group(1))
            papers = [p] if p else []
        if not papers:
            papers = sources.search_query(seed)
    elif stype == "author":
        papers = sources.search_author(seed)
    else:
        papers = sources.search_query(seed)

    added, seed_pid = [], None
    for i, p in enumerate(papers):
        pid = _insert_paper(inv_id, p, seed=(stype in ("doi", "url", "arxiv") and i == 0))
        if pid:
            added.append(pid)
            if i == 0:
                seed_pid = pid
    db.add_event(inv_id, "info", "job:discover_seed",
                 f"discovered {len(added)} paper(s) from {stype} seed", {"paper_ids": added[:20]})

    # autonomous expansion + fetching begin immediately
    if seed_pid:
        db.enqueue_job(inv_id, "expand_paper", {"paper_id": seed_pid, "depth": 1}, priority=6)
    for pid in added[:3]:
        db.enqueue_job(inv_id, "fetch_pdf", {"paper_id": pid}, priority=5)
    for pid in added[3:]:
        db.enqueue_job(inv_id, "fetch_pdf", {"paper_id": pid}, priority=2)
    return {"papers_added": len(added), "seed_paper": seed_pid}


def h_expand_paper(inv_id: int, payload: dict) -> dict:
    paper = _paper(inv_id, payload["paper_id"])
    depth = int(payload.get("depth", 1))
    if not paper or not paper.get("external_id") or depth > 2:
        return {"expanded": 0}
    if db.get_counter(inv_id, "papers") >= settings.max_papers:
        return {"expanded": 0, "budget": "papers"}

    related = sources.expand_paper(paper["external_id"], max_items=12)
    added = []
    for i, p in enumerate(related):
        pid = _insert_paper(inv_id, p)
        if not pid:
            continue
        added.append(pid)
        if p.get("pdf_url"):
            db.enqueue_job(inv_id, "fetch_pdf", {"paper_id": pid}, priority=3)
        if depth < 2 and i < 3:
            db.enqueue_job(inv_id, "expand_paper", {"paper_id": pid, "depth": depth + 1},
                           priority=1, dedupe_key=f"expand:{pid}")
    db.add_event(inv_id, "info", "job:expand_paper",
                 f"depth {depth} expansion from paper {paper['id']}: +{len(added)}",
                 {"from_paper": paper["id"], "added": len(added)})
    return {"expanded": len(added)}


# --------------------------------------------------------------------------- #
# acquisition + extraction
# --------------------------------------------------------------------------- #

def _arxiv_pdf(url: str) -> str:
    m = re.match(r"https?://arxiv.org/abs/(.+)", url)
    return f"https://arxiv.org/pdf/{m.group(1)}" if m else url


def h_fetch_pdf(inv_id: int, payload: dict) -> dict:
    paper = _paper(inv_id, payload["paper_id"])
    if not paper or paper["status"] in ("fetched", "extracted", "no_pdf"):
        return {"skipped": True}

    candidates_urls = []
    for u in (paper.get("pdf_url"), paper.get("oa_url")):
        if not u:
            continue
        u2 = _arxiv_pdf(u)
        if u2 not in candidates_urls:
            candidates_urls.append(u2)

    if not candidates_urls:
        db.execute("UPDATE papers SET status='no_pdf' WHERE id=?", (paper["id"],))
        db.add_event(inv_id, "info", "job:fetch_pdf",
                     f"no openly accessible PDF for paper {paper['id']} — skipping (no access bypass)",
                     {"paper": paper["id"]})
        return {"no_pdf": True}

    budget = _budget_left(inv_id, "bytes_downloaded", settings.max_download_mb * 1024 * 1024)
    if budget <= 0:
        return {"budget": "download"}

    dest = settings.pdfs_dir / f"inv{inv_id}" / f"paper_{paper['id']}.pdf"
    last_err = None
    for url in candidates_urls[:2]:
        try:
            nbytes = httpc.download(url, dest, inv_id)
            with open(dest, "rb") as f:
                if f.read(5) != b"%PDF-":
                    raise httpc.FetchError("not a PDF")
            db.execute("UPDATE papers SET status='fetched', pdf_path=?, pdf_bytes=? WHERE id=?",
                       (str(dest), nbytes, paper["id"]))
            db.enqueue_job(inv_id, "extract_figures", {"paper_id": paper["id"]}, priority=4)
            db.add_event(inv_id, "info", "job:fetch_pdf",
                         f"fetched PDF for paper {paper['id']} ({nbytes // 1024} KB)", {})
            return {"bytes": nbytes, "url": url}
        except httpc.FetchError as e:
            last_err = str(e)
            dest.unlink(missing_ok=True)
    db.execute("UPDATE papers SET status='failed' WHERE id=?", (paper["id"],))
    db.add_event(inv_id, "warn", "job:fetch_pdf", f"PDF fetch failed for paper {paper['id']}: {last_err}", {})
    return {"failed": True, "error": last_err}


def h_extract_figures(inv_id: int, payload: dict) -> dict:
    paper = _paper(inv_id, payload["paper_id"])
    if not paper or not paper.get("pdf_path"):
        return {"skipped": True}
    out_dir = settings.figures_dir / f"inv{inv_id}" / f"paper_{paper['id']}"
    figs = pdfs.extract_figures(paper["pdf_path"], out_dir)

    inserted = 0
    for f in figs:
        if db.get_counter(inv_id, "figures") >= settings.max_figures:
            db.add_event(inv_id, "warn", "job:extract_figures", "figure budget reached — stopping extraction", {})
            break
        keys = {k: v for k, v in f.items() if k in
                ("page", "idx_in_page", "width", "height", "sha256", "phash", "dhash", "ahash",
                 "caption", "panel_labels", "image_path", "kind")}
        if db.insert_figure(inv_id, paper["id"], added_at=db.now_iso(), **keys):
            inserted += 1
            db.bump_counter(inv_id, "figures")

    db.execute("UPDATE papers SET status='extracted', n_figures=? WHERE id=?", (inserted, paper["id"]))
    db.enqueue_job(inv_id, "sweep", {}, priority=3, dedupe_key="sweep")
    db.add_event(inv_id, "info", "job:extract_figures",
                 f"extracted {inserted} figure(s) from paper {paper['id']}", {})
    return {"figures": inserted}


# --------------------------------------------------------------------------- #
# comparison scheduling (cheap gate sweep)
# --------------------------------------------------------------------------- #

def h_sweep(inv_id: int, payload: dict) -> dict:
    figs = db.query("SELECT * FROM figures WHERE investigation_id=? ORDER BY id", (inv_id,))
    if len(figs) < 2:
        return {"pairs": 0, "promoted": 0}

    last = int(db.get_counter(inv_id, "sweep_last_fig_id"))
    new = [f for f in figs if f["id"] > last]
    older = [f for f in figs if f["id"] <= last]
    if not new:
        return {"pairs": 0, "promoted": 0, "noop": True}

    pairs = [(a, b) for a in new for b in older]
    pairs += [(new[i], new[j]) for i in range(len(new)) for j in range(i + 1, len(new))]

    promoted, processed, budget_hit = 0, 0, False
    for a, b in pairs:
        if db.get_counter(inv_id, "comparisons") >= settings.max_comparisons:
            budget_hit = True
            break
        db.bump_counter(inv_id, "comparisons")
        processed += 1
        if not scoring.passes_gate(a, b):
            continue
        fa, fb = (a, b) if a["id"] < b["id"] else (b, a)
        pair_key = f"{fa['id']}:{fb['id']}"
        try:
            cid = db.execute(
                "INSERT INTO candidates(investigation_id, figure_a, figure_b, pair_key, stage, "
                "status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (inv_id, fa["id"], fb["id"], pair_key, "gate", "open", db.now_iso(), db.now_iso()),
            )
        except Exception:
            continue  # already exists
        db.enqueue_job(inv_id, "deep_compare", {"candidate_id": cid},
                       priority=7, dedupe_key=f"deep:{pair_key}")
        promoted += 1

    if not budget_hit:
        db.set_counter(inv_id, "sweep_last_fig_id", max(f["id"] for f in new))
    db.add_event(inv_id, "info", "job:sweep",
                 f"gate sweep: {processed} pairs checked, {promoted} promoted to deep verify"
                 + (" (budget cut short)" if budget_hit else ""),
                 {"promoted": promoted})
    return {"pairs": processed, "promoted": promoted, "budget_hit": budget_hit}


# --------------------------------------------------------------------------- #
# deep verification
# --------------------------------------------------------------------------- #

def h_deep_compare(inv_id: int, payload: dict) -> dict:
    cand = _cand(payload["candidate_id"])
    if not cand or cand["stage"] in ("verified", "interpreted", "cross_checked"):
        return {"skipped": True}
    fa, fb = _fig(cand["figure_a"]), _fig(cand["figure_b"])
    if not fa or not fb:
        return {"skipped": True}

    battery = vision.run_battery(Path(fa["image_path"]), Path(fb["image_path"]))
    fused = scoring.fuse_battery(battery, fa, fb)

    db.execute(
        "UPDATE candidates SET stage='verified', verdict=?, score=?, best_transform=?, "
        "signals_json=?, uncertainty=?, flags_json=?, updated_at=? WHERE id=?",
        (fused["verdict"], fused["score"], fused["signals"].get("best_variant"),
         json.dumps(fused["signals"]), fused["uncertainty"], json.dumps(fused["flags"]),
         db.now_iso(), cand["id"]),
    )
    for name, val in fused["signals"].items():
        db.insert_evidence(inv_id, "candidate", cand["id"], "signal", f"signal:{name}", val,
                           interpretation=f"{name} = {val}")
    db.insert_evidence(inv_id, "candidate", cand["id"], "transformation", "variant_search",
                       battery["ssim_by_variant"],
                       interpretation=f"best variant: {fused['signals'].get('best_variant')}",
                       params={"orb_by_variant": {k: v.get("inliers") for k, v in
                                                  battery.get("orb_by_variant", {}).items()},
                               "containment": battery.get("containment")})
    db.add_event(inv_id, "info", "job:deep_compare",
                 f"candidate {cand['id']}: verdict={fused['verdict']} score={fused['score']} "
                 f"variant={fused['signals'].get('best_variant')}", {"candidate": cand["id"]})

    if fused["verdict"] in ("high", "moderate", "exact_duplicate"):
        db.enqueue_job(inv_id, "interpret", {"candidate_id": cand["id"]}, priority=8)
        db.enqueue_job(inv_id, "cross_check", {"candidate_id": cand["id"]}, priority=6)
    elif fused["verdict"] == "weak" and fused["eig"] >= 0.5:
        db.enqueue_job(inv_id, "cross_check", {"candidate_id": cand["id"]}, priority=4)
    return {"verdict": fused["verdict"], "score": fused["score"], "eig": fused["eig"]}


def h_cross_check(inv_id: int, payload: dict) -> dict:
    """Cross-check both figures of a candidate against the rest of the corpus."""
    cand = _cand(payload["candidate_id"])
    if not cand:
        return {"skipped": True}
    figs = db.query("SELECT * FROM figures WHERE investigation_id=? AND id NOT IN (?,?) "
                    "ORDER BY id DESC LIMIT 400", (inv_id, cand["figure_a"], cand["figure_b"]))
    out = {}
    for role, fid in (("a", cand["figure_a"]), ("b", cand["figure_b"])):
        me = _fig(fid)
        if not me:
            continue
        others = sorted(figs, key=lambda f: scoring.passes_gate(me, f), reverse=True)[:6]
        hits, checked = [], 0
        for o in others:
            if not scoring.passes_gate(me, o):
                continue
            checked += 1
            try:
                battery = vision.run_battery(Path(me["image_path"]), Path(o["image_path"]))
            except Exception:
                continue
            fused = scoring.fuse_battery(battery, me, o)
            if fused["verdict"] in ("high", "moderate", "exact_duplicate"):
                hits.append({"figure_id": o["id"], "paper_id": o["paper_id"],
                             "score": fused["score"], "verdict": fused["verdict"]})
        out[role] = {"checked": checked, "hits": hits}
        db.insert_evidence(
            inv_id, "candidate", cand["id"], "cross_check", f"corpus_sweep:{role}",
            hits,
            interpretation=("this image also matches other corpus figures — suggests broader reuse"
                            if hits else "no other corpus figure matches this image beyond the pair"),
            params={"checked": checked},
        )
    db.execute("UPDATE candidates SET stage='cross_checked', updated_at=? WHERE id=?",
               (db.now_iso(), cand["id"]))
    db.add_event(inv_id, "info", "job:cross_check",
                 f"candidate {cand['id']} cross-checked against corpus", {"candidate": cand["id"]})
    return out


def h_interpret(inv_id: int, payload: dict) -> dict:
    cand = _cand(payload["candidate_id"])
    if not cand:
        return {"skipped": True}
    fa, fb = _fig(cand["figure_a"]), _fig(cand["figure_b"])
    evidence = db.query("SELECT * FROM evidence WHERE subject_type='candidate' AND subject_id=? "
                        "ORDER BY id", (cand["id"],))
    use_llm = llm.llm_available() and db.get_counter(inv_id, "llm_calls") < settings.max_llm_calls
    if use_llm:
        db.bump_counter(inv_id, "llm_calls")
    out = llm.interpret_candidate(cand, fa, fb, evidence, allow_images=use_llm)
    db.execute("UPDATE candidates SET interpretation_json=?, llm_used=?, stage='interpreted', "
               "updated_at=? WHERE id=?",
               (json.dumps(out), 0 if out.get("fallback") else 1, db.now_iso(), cand["id"]))
    db.insert_evidence(inv_id, "candidate", cand["id"], "llm" if not out.get("fallback") else "note",
                       f"interpretation:{settings.llm_model if not out.get('fallback') else 'heuristic'}",
                       out.get("summary", ""), interpretation=out.get("context_hypothesis", ""))
    db.add_event(inv_id, "info", "job:interpret",
                 f"candidate {cand['id']} interpreted ({'heuristic fallback' if out.get('fallback') else 'LLM'})",
                 {"candidate": cand["id"]})
    return {"fallback": bool(out.get("fallback"))}


def h_finalize_report(inv_id: int, payload: dict) -> dict:
    from . import report as report_mod
    path, summary = report_mod.generate(inv_id)
    final_status = payload.get("final_status", "completed")
    db.execute("INSERT INTO reports(investigation_id, path, format, summary, created_at) "
               "VALUES (?,?,?,?,?)", (inv_id, str(path), "markdown", summary, db.now_iso()))
    db.set_investigation_status(inv_id, final_status)
    db.add_event(inv_id, "info", "job:finalize_report",
                 f"investigation finalized: {final_status}", {"report": str(path)})
    return {"report": str(path), "summary": summary}


HANDLERS = {
    "discover_seed": h_discover_seed,
    "expand_paper": h_expand_paper,
    "fetch_pdf": h_fetch_pdf,
    "extract_figures": h_extract_figures,
    "sweep": h_sweep,
    "deep_compare": h_deep_compare,
    "cross_check": h_cross_check,
    "interpret": h_interpret,
    "finalize_report": h_finalize_report,
}
