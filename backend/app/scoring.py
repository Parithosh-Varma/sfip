"""Multi-signal fusion: verdicts, uncertainty, false-positive heuristics,
and an expected-information-gain (EIG) heuristic used for autonomous
prioritization.

Language discipline: findings are *observed image similarities* with
characterized evidence — never labeled misconduct or intent.
"""
from __future__ import annotations

import math
from statistics import pstdev
from typing import Optional

from .config import settings


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def fuse_battery(battery: dict, fig_a: dict, fig_b: dict) -> dict:
    """Combine independent signals into score/verdict/uncertainty/flags."""
    base = battery["base"]
    best = battery["best"]
    contain = battery.get("containment") or {"score": 0.0}

    hash_sim = base["hash_sim"]
    hist_sim = base["hist_sim"]
    ssim_sim = best.get("ssim", 0.0)
    orb_inliers = int(best.get("orb_inliers", 0))
    coverage = float(best.get("orb_coverage", 0.0))
    contain_sim = float(contain.get("score", 0.0))

    # geometric signal: saturating inlier count + coverage + inlier ratio from orb block
    orb_block = battery.get("orb_by_variant", {}).get(best.get("variant", "identity"), {})
    ratio = float(orb_block.get("inlier_ratio", 0.0))
    orb_sim = _clamp(0.55 * min(1.0, orb_inliers / 60) + 0.25 * min(1.0, ratio / 0.6)
                     + 0.20 * min(1.0, coverage / 0.5))

    # base fusion (weights sum to 1)
    score = 0.25 * hash_sim + 0.15 * hist_sim + 0.30 * ssim_sim + 0.30 * orb_sim
    # containment (partial overlap) can dominate when it's decisive
    if contain_sim > 0.75:
        score = max(score, 0.75 * contain_sim + 0.15 * ssim_sim + 0.10 * hash_sim)
    score = round(_clamp(score), 4)

    flags: list[str] = []
    # exact duplicate?
    if fig_a.get("sha256") and fig_a.get("sha256") == fig_b.get("sha256"):
        flags.append("exact_duplicate_bytes")
        score = 1.0
    if hash_sim > 0.985 and ssim_sim > 0.98:
        flags.append("near_exact_duplicate")

    # generic-content risk: flat grayscale charts match each other easily
    generic_risk = False
    if (base["colorfulness_a"] < 12 and base["colorfulness_b"] < 12
            and orb_inliers < 15 and ssim_sim > 0.6 and hash_sim < 0.75):
        generic_risk = True
        flags.append("generic_content_risk")
        score = round(score * 0.85, 4)

    # tiny figures (icons/legends) — similarity less meaningful
    for f, side in ((fig_a, min(fig_a.get("width", 999), fig_a.get("height", 999))),
                    (fig_b, min(fig_b.get("width", 999), fig_b.get("height", 999)))):
        if side < 120:
            flags.append(f"small_figure_{f.get('id')}")
            score = round(score * 0.9, 4)

    # conflicting evidence tracking
    conflicting = (hash_sim > 0.85 and ssim_sim < 0.55) or (ssim_sim > 0.85 and orb_inliers < 8)
    if conflicting:
        flags.append("conflicting_signals")

    if contain_sim > 0.7 and orb_inliers < settings.gate_orb_inliers:
        flags.append("partial_overlap_candidate")

    # uncertainty from signal disagreement
    sigs = [hash_sim, hist_sim, ssim_sim, orb_sim]
    uncertainty = round(_clamp(pstdev(sigs) * 1.6 + (0.15 if conflicting else 0.0)), 4)

    verdict = _verdict(score, flags)
    eig = _eig(score, uncertainty, len(flags))

    return {
        "score": score,
        "verdict": verdict,
        "uncertainty": uncertainty,
        "flags": flags,
        "eig": eig,
        "signals": {
            "hash_similarity": hash_sim,
            "histogram_similarity": hist_sim,
            "ssim": ssim_sim,
            "orb_geometric": round(orb_sim, 4),
            "orb_inliers": orb_inliers,
            "orb_transform": best.get("orb_transform"),
            "coverage": coverage,
            "containment": contain_sim,
            "best_variant": best.get("variant"),
        },
    }


def _verdict(score: float, flags: list[str]) -> str:
    if "exact_duplicate_bytes" in flags or "near_exact_duplicate" in flags:
        return "exact_duplicate"
    if score >= settings.verdict_high:
        return "high"
    if score >= settings.verdict_moderate:
        return "moderate"
    if score >= settings.verdict_weak:
        return "weak"
    return "likely_false_positive"


def _eig(score: float, uncertainty: float, n_flags: int) -> float:
    """Expected information gain heuristic (0..1): candidates near a decision
    boundary, or with high uncertainty, or with only generic-content flags,
    gain most from the next autonomous step (cross-check or interpretation)."""
    cutoffs = [settings.verdict_weak, settings.verdict_moderate, settings.verdict_high]
    dist = min(abs(score - c) for c in cutoffs)
    boundary = _clamp(1 - dist / settings.eig_threshold_band)
    return round(_clamp(0.5 * boundary + 0.35 * uncertainty + 0.15 * min(n_flags, 3) / 3), 4)


def passes_gate(hashes_a: dict, hashes_b: dict) -> bool:
    """Cheap prefilter before the full battery (hash-only)."""
    from .vision import hamming_hex
    for k in ("phash", "dhash"):
        d = hamming_hex(hashes_a[k], hashes_b[k])
        if d <= settings.gate_hamming:
            return True
    return False
