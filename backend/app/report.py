"""Reproducible, evidence-backed report generation (markdown)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy
import cv2
import PIL
import pymupdf

from . import db, llm
from .config import settings

DISCLAIMER = (
    "> **Interpretation boundary.** This report describes *observed image "
    "similarities* and characterizes their evidence. Similarity is not "
    "equated with fraud or misconduct; shared controls, common source "
    "images, production errors, and generic chart structure are all viable "
    "alternative explanations. No conclusion about intent is drawn, and no "
    "allegation is made."
)


def generate(inv_id: int) -> tuple[Path, str]:
    inv = db.get_investigation(inv_id)
    papers = db.query("SELECT * FROM papers WHERE investigation_id=? ORDER BY id", (inv_id,))
    n_figs = db.query_one("SELECT COUNT(*) AS n FROM figures WHERE investigation_id=?", (inv_id,))["n"]
    cands = db.query(
        "SELECT c.*, fa.paper_id AS pa, fb.paper_id AS pb, fa.page AS page_a, fb.page AS page_b "
        "FROM candidates c JOIN figures fa ON fa.id=c.figure_a JOIN figures fb ON fb.id=c.figure_b "
        "WHERE c.investigation_id=? ORDER BY c.score DESC", (inv_id,))
    by_verdict: dict[str, int] = {}
    for c in cands:
        by_verdict[c["verdict"] or "unverified"] = by_verdict.get(c["verdict"] or "unverified", 0) + 1

    ts = datetime.now(timezone.utc).isoformat()
    L: list[str] = []
    L.append(f"# Image-Similarity Investigation Report — `{inv['seed']}`\n")
    L.append(f"Generated {ts} · investigation #{inv_id} · status **{inv['status']}**\n")
    L.append(DISCLAIMER + "\n")

    # --- summary
    L.append("## 1. Executive summary\n")
    L.append(f"- Papers discovered: **{len(papers)}**")
    L.append(f"- Figures extracted: **{n_figs}**")
    L.append(f"- Candidate pairs verified: **{len(cands)}** "
             f"(exact_duplicate={by_verdict.get('exact_duplicate', 0)}, "
             f"high={by_verdict.get('high', 0)}, moderate={by_verdict.get('moderate', 0)}, "
             f"weak={by_verdict.get('weak', 0)}, "
             f"likely_false_positive={by_verdict.get('likely_false_positive', 0)})")
    flagged = [c for c in cands if c["verdict"] in ("exact_duplicate", "high", "moderate")]
    L.append(f"- Pairs with substantive observed similarity: **{len(flagged)}**")
    L.append("")

    # --- corpus
    L.append("## 2. Corpus\n")
    L.append("| id | year | title | source | figures | OA | doi |")
    L.append("|---|---|---|---|---|---|---|")
    for p in papers:
        meta = json.loads(p.get("metadata_json") or "{}")
        L.append(f"| {p['id']} | {p.get('year') or ''} | {(p.get('title') or '')[:90]} | "
                 f"{p.get('source')} | {p.get('n_figures') or 0} | "
                 f"{'yes' if meta.get('is_oa') else 'no'} | {p.get('doi') or ''} |")
    L.append("")

    # --- method
    L.append("## 3. Method\n")
    L.append("- Discovery: OpenAlex / Crossref / Europe PMC (open-access only, source rate limits respected).")
    L.append("- Figure extraction: PyMuPDF (raster + caption-anchored vector rendering), sha256 dedupe.")
    L.append("- Signals per pair: perceptual hashes (p/d/aHash), SSIM over transformation variants, "
             "HSV histogram intersection, ORB+RANSAC geometric verification, multi-scale containment.")
    L.append("- Variants tested: identity, hflip, vflip, rot90/180/270, stretch ×0.8/×1.25, "
             "center crops 75%/50%, rescale 35–100% containment.")
    L.append("- Fusion: weighted multi-signal score; false-positive heuristics (generic-content risk, "
             "small figures, conflicting signals) reduce confidence; uncertainty from signal dispersion.")
    if llm.llm_available():
        L.append(f"- Interpretation: grounded LLM analysis (`{settings.llm_model}`) with image input; "
                 "deterministic fallback when unavailable.")
    L.append("")

    # --- findings
    L.append("## 4. Findings (ranked by fused score)\n")
    if not flagged:
        L.append("No pairs with substantive observed similarity were found in this corpus "
                 "within the configured budgets.\n")
    for c in flagged[:40]:
        sig = json.loads(c.get("signals_json") or "{}")
        flags = json.loads(c.get("flags_json") or "[]")
        interp = json.loads(c.get("interpretation_json") or "null")
        L.append(f"### Candidate {c['id']} — verdict **{c['verdict']}** (score {c['score']})\n")
        L.append(f"- Figures: `{c['figure_a']}` (paper {c['pa']}, p.{c['page_a']}) ↔ "
                 f"`{c['figure_b']}` (paper {c['pb']}, p.{c['page_b']})")
        L.append(f"- Best-matching transformation: `{c.get('best_transform')}` · "
                 f"uncertainty {c.get('uncertainty')} · flags: {', '.join(flags) or 'none'}")
        L.append(f"- Signals: hash={sig.get('hash_similarity')}, ssim={sig.get('ssim')}, "
                 f"hist={sig.get('histogram_similarity')}, orb_inliers={sig.get('orb_inliers')}, "
                 f"orb_geom={sig.get('orb_geometric')}, containment={sig.get('containment')}")
        L.append(f"- Images: `/media/{_rel(cand_fig_path(inv_id, c['figure_a']))}` · "
                 f"`/media/{_rel(cand_fig_path(inv_id, c['figure_b']))}`")
        xcheck = db.query("SELECT * FROM evidence WHERE subject_type='candidate' AND subject_id=? "
                          "AND kind='cross_check' ORDER BY id DESC LIMIT 2", (c["id"],))
        for e in xcheck:
            L.append(f"- Cross-check: {e['interpretation']}")
        if interp:
            L.append(f"- Interpretation ({'heuristic fallback' if interp.get('fallback') else 'LLM-grounded'}): "
                     f"{interp.get('summary', '')}")
            for alt in (interp.get("alternative_explanations") or [])[:4]:
                L.append(f"  - alternative: {alt}")
            for w in (interp.get("weaknesses") or [])[:4]:
                L.append(f"  - weakness: {w}")
        L.append("")

    # --- AI-provenance & tamper screening
    L.append("## 4b. AI-provenance & tamper screening\n")
    prov = db.query("SELECT e.*, f.paper_id, f.page FROM evidence e "
                    "JOIN figures f ON f.id=e.subject_id "
                    "WHERE e.investigation_id=? AND e.subject_type='figure' "
                    "AND e.kind IN ('provenance','forensics') ORDER BY e.id", (inv_id,))
    flagged_prov = [e for e in prov if "ai_tool_marked" in e["value"]
                    or '"suspicion": "high"' in e["value"] or '"suspicion":"high"' in e["value"]
                    or '"flag": true' in e["value"] or '"flag":true' in e["value"]]
    L.append(f"- Figures screened: {len({e['subject_id'] for e in prov})}; "
             f"flags raised: **{len(flagged_prov)}**")
    L.append("- Layers: file-metadata forensics on every figure; copy-move (clone stamp), "
             "blot-anomaly, diffusion spectral, and SynthID-provider checks on figures from "
             "suspicious pairs. Absence of signals proves nothing (publishers strip metadata).")
    for e in flagged_prov[:30]:
        L.append(f"- figure {e['subject_id']} (paper {e['paper_id']}, p.{e['page']}) "
                 f"[{e['method']}]: {e['interpretation']}")
    L.append(f"- Figures screened: {len({e['subject_id'] for e in prov})}; "
             f"high-suspicion flags: **{len(flagged_prov)}**")
    L.append("- Layers: file-metadata forensics (generator tags, EXIF/XMP, C2PA manifest) on "
             "every figure; diffusion spectral screen + SynthID provider check on figures from "
             "suspicious pairs. Absence of signals proves nothing (publishers strip metadata).")
    for e in flagged_prov[:30]:
        L.append(f"- figure {e['subject_id']} (paper {e['paper_id']}, p.{e['page']}) "
                 f"[{e['method']}]: {e['interpretation']}")
    if not flagged_prov:
        L.append("- No AI-generator markers or watermark detections recorded.")
    L.append("")

    # --- uncertainty
    L.append("## 5. Uncertainty & conflicting evidence\n")
    conflicting = [c for c in cands if "conflicting_signals" in (c.get("flags_json") or "")]
    L.append(f"- Candidates with conflicting signals: {len(conflicting)} "
             f"(ids: {', '.join(str(c['id']) for c in conflicting[:20]) or 'none'})")
    weak = [c for c in cands if c["verdict"] == "weak"]
    L.append(f"- Weak-similarity candidates retained for audit (not dismissed silently): {len(weak)}")
    L.append("")

    # --- budgets / stopping
    L.append("## 6. Budgets & stopping\n")
    counters = db.all_counters(inv_id)
    for k, v in sorted(counters.items()):
        L.append(f"- {k}: {v}")
    L.append(f"- Final status: {inv['status']} "
             f"({'all queues drained' if inv['status'] == 'completed' else 'stopped by explicit budget'})")
    L.append("")

    # --- provenance
    L.append("## 7. Provenance & reproducibility\n")
    L.append(f"- Library versions: pymupdf {pymupdf.__version__}, opencv {cv2.__version__}, "
             f"pillow {PIL.__version__}, numpy {numpy.__version__}")
    L.append(f"- LLM: {settings.llm_model if llm.llm_available() else 'unavailable (heuristic fallback)'}")
    cfg = inv.get("config") or {}
    L.append(f"- Thresholds: gate_hamming={cfg.get('gate_hamming')}, "
             f"gate_orb_inliers={cfg.get('gate_orb_inliers')}, "
             f"verdict cutoffs=({cfg.get('verdict_weak')}/"
             f"{cfg.get('verdict_moderate')}/{cfg.get('verdict_high')})")
    L.append(f"- Full configuration snapshot and event audit log are available via `/api/export/{inv_id}`.")
    L.append("")

    md = "\n".join(L)
    out = settings.reports_dir / f"investigation_{inv_id}_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.md"
    out.write_text(md, encoding="utf-8")
    first = (f"Investigation of {inv['seed']}: {len(papers)} papers, {n_figs} figures, "
             f"{len(cands)} verified pairs, {len(flagged)} with substantive similarity.")
    return out, first


def cand_fig_path(inv_id: int, fig_id: int) -> str:
    row = db.query_one("SELECT image_path FROM figures WHERE id=?", (fig_id,))
    return row["image_path"] if row else ""


def _rel(path: str) -> str:
    try:
        return str(Path(path).resolve().relative_to(settings.data_dir.resolve()))
    except Exception:
        return path
