"""Within-image tamper screening: copy-move (clone stamp) detection and
western-blot anomaly checks.

These catch what pair-wise comparison cannot: duplicated patches, painted
regions, and spliced lanes *inside a single figure* — the manipulation
family most commonly exposed in paper-mill forensics.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Optional

import cv2
import numpy as np

_orb = cv2.ORB_create(nfeatures=2000)


def _cluster_translations(pairs: list[tuple], bin_px: int = 16,
                          min_cluster: int = 12, min_bidir: int = 5) -> list[dict]:
    """Group match pairs by translation vector; dense bins = cloned regions.

    A→B and B→A canonicalize to the same bin; neighboring bins merge (rounding
    splits true translations at .5 boundaries); bidirectional pairs (twins
    match each other back) separate real clones from coincidental similarity.
    """
    bins: dict[tuple[int, int], list] = defaultdict(list)
    for x1, y1, x2, y2, qi, ti in pairs:
        dx, dy = x2 - x1, y2 - y1
        if float(np.hypot(dx, dy)) < 30:      # ignore trivial self/near matches
            continue
        # canonicalize direction: A→B and B→A are the same clone evidence
        if dx < 0 or (dx == 0 and dy < 0):
            dx, dy = -dx, -dy
            x1, y1, x2, y2, qi, ti = x2, y2, x1, y1, ti, qi
        bins[(round(dx / bin_px), round(dy / bin_px))].append((x1, y1, x2, y2, qi, ti))

    def to_cluster(bx, by, pts) -> dict:
        a = np.array([(p[0], p[1]) for p in pts])
        b = np.array([(p[2], p[3]) for p in pts])
        idx = {(p[4], p[5]) for p in pts}
        dxs = np.array([p[2] - p[0] for p in pts])
        dys = np.array([p[3] - p[1] for p in pts])
        return {
            "matches": len(pts),
            "bidir": sum(1 for (q, t) in idx if (t, q) in idx) // 2,
            "dx": round(float(dxs.mean()), 1), "dy": round(float(dys.mean()), 1),
            "dx_std": round(float(dxs.std()), 2), "dy_std": round(float(dys.std()), 2),
            "bbox_a": [round(float(v), 1) for v in
                       (a[:, 0].min(), a[:, 1].min(), a[:, 0].max(), a[:, 1].max())],
            "bbox_b": [round(float(v), 1) for v in
                       (b[:, 0].min(), b[:, 1].min(), b[:, 0].max(), b[:, 1].max())],
            "_pts": pts,
        }

    clusters = [to_cluster(bx, by, pts) for (bx, by), pts in bins.items() if len(pts) >= 4]
    # merge neighboring bins (translation rounding splits real clones)
    merged: list[dict] = []
    for c in sorted(clusters, key=lambda c: -c["matches"]):
        host = None
        for m in merged:
            if abs(m["dx"] - c["dx"]) <= bin_px * 1.5 and abs(m["dy"] - c["dy"]) <= bin_px * 1.5:
                host = m
                break
        if host is None:
            merged.append(c)
        else:
            host["_pts"].extend(c["_pts"])
            pts = host["_pts"]
            a = np.array([(p[0], p[1]) for p in pts])
            b = np.array([(p[2], p[3]) for p in pts])
            idx = {(p[4], p[5]) for p in pts}
            dxs = np.array([p[2] - p[0] for p in pts])
            dys = np.array([p[3] - p[1] for p in pts])
            host.update({"matches": len(pts),
                         "bidir": sum(1 for (q, t) in idx if (t, q) in idx) // 2,
                         "dx": round(float(dxs.mean()), 1),
                         "dy": round(float(dys.mean()), 1),
                         "dx_std": round(float(dxs.std()), 2),
                         "dy_std": round(float(dys.std()), 2)})
    out = []
    for c in merged:
        c.pop("_pts", None)
        if c["matches"] >= min_cluster:
            out.append(c)
    out.sort(key=lambda c: -c["matches"])
    return out[:6]


def _tight(c: dict) -> bool:
    return c.get("dx_std", 99) <= 10 and c.get("dy_std", 99) <= 10


def _ncc(a: np.ndarray, b: np.ndarray) -> float:
    a = cv2.resize(a, (64, 64), interpolation=cv2.INTER_AREA).astype(np.float32).ravel()
    b = cv2.resize(b, (64, 64), interpolation=cv2.INTER_AREA).astype(np.float32).ravel()
    da, db = a - a.mean(), b - b.mean()
    return float((da * db).sum() / (np.sqrt((da ** 2).sum() * (db ** 2).sum()) + 1e-9))


def _mirror_verified(g: np.ndarray, c: dict, size: int = 64) -> bool:
    """Direct pixel check: the region at bbox_a, shifted by the cluster's own
    translation, must correlate with its mirror. Translation-derived boxes
    (not keypoint extents) so edge-concentrated matches still verify."""
    try:
        H, W = g.shape
        ba = c["bbox_a"]
        cx, cy = (ba[0] + ba[2]) / 2, (ba[1] + ba[3]) / 2
        dx, dy = c.get("dx", 0), c.get("dy", 0)

        def box(px, py):
            x0, y0 = int(round(px - size / 2)), int(round(py - size / 2))
            x0, y0 = max(0, x0), max(0, y0)
            x1, y1 = min(W, x0 + size), min(H, y0 + size)
            return x0, y0, x1, y1

        x0, y0, x1, y1 = box(cx, cy)
        u0, v0, u1, v1 = box(cx + dx, cy + dy)
        if min(x1 - x0, y1 - y0, u1 - u0, v1 - v0) < 24:
            return False
        return _ncc(g[y0:y1, x0:x1], cv2.flip(g[v0:v1, u0:u1], 1)) > 0.85
    except Exception:
        return False


def _compact(c: dict, g: np.ndarray, frac: float = 0.6) -> bool:
    """Clone matches must be localized to compact regions; scattered
    image-spanning matches are coincidental similarity, not copy-move."""
    H, W = g.shape
    for k in ("bbox_a", "bbox_b"):
        b = c[k]
        if (b[2] - b[0]) / W > frac or (b[3] - b[1]) / H > frac:
            return False
    return True


def _passes(c: dict, g: Optional[np.ndarray] = None) -> bool:
    # plain clones: many matches sharing one EXACT translation, localized
    if not c.get("mirrored"):
        return c["matches"] >= 12 and _tight(c) and (g is None or _compact(c, g))
    # mirrored clones: exact translation + localized + direct pixel verification
    if c["matches"] < 12 or not _tight(c) or g is None or not _compact(c, g):
        return False
    return _mirror_verified(g, c)


def _self_pairs(desc, kps, other_desc, other_kps, mirror_w: Optional[int] = None):
    """Self-matching that excludes coincident (self) hits BEFORE the ratio
    test — otherwise every query's best match is itself and twins are lost."""
    if desc is None or other_desc is None or len(kps) < 10 or len(other_kps) < 10:
        return []
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
    knn = matcher.knnMatch(desc, other_desc, k=3)
    pairs = []
    for item in knn:
        cands = []
        for m in item:
            x1, y1 = kps[m.queryIdx].pt
            x2, y2 = other_kps[m.trainIdx].pt
            if mirror_w is not None:
                x2 = mirror_w - x2
            if abs(x1 - x2) < 4 and abs(y1 - y2) < 4:
                continue  # coincident self-hit, not evidence
            cands.append((m, x1, y1, x2, y2))
        if len(cands) < 2:
            continue
        (m1, x1, y1, x2, y2), (m2, *_) = cands[0], cands[1]
        if m1.distance < 0.75 * m2.distance:
            pairs.append((x1, y1, x2, y2, m1.queryIdx, m1.trainIdx))
    return pairs


def clone_scan(gray: np.ndarray, max_side: int = 800) -> dict:
    """Detect copy-move cloning inside one image, including mirrored clones."""
    try:
        g = np.asarray(gray, dtype=np.uint8)
        h, w = g.shape
        s = min(1.0, max_side / max(h, w))
        if s < 1.0:
            g = cv2.resize(g, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
        H, W = g.shape
        kp, desc = _orb.detectAndCompute(g, None)
        pairs = _self_pairs(desc, kp, desc, kp)
        clusters = _cluster_translations(pairs)

        # mirrored clones: match against horizontally flipped self
        gf = cv2.flip(g, 1)
        kpf, descf = _orb.detectAndCompute(gf, None)
        mpairs = _self_pairs(desc, kp, descf, kpf, mirror_w=W)
        mclusters = _cluster_translations(mpairs)
        for c in mclusters:
            c["mirrored"] = True
        clusters = sorted(clusters + mclusters, key=lambda c: -c["matches"])[:6]

        flag = bool(clusters and _passes(clusters[0], g))
        return {"clone_clusters": len(clusters), "flag": flag,
                "largest_cluster_matches": clusters[0]["matches"] if clusters else 0,
                "clusters": clusters,
                "note": "clusters of near-identical regions at a fixed offset suggest copy-move"}
    except Exception as e:
        return {"clone_clusters": 0, "flag": False, "error": str(e)[:200]}


def blot_screen(gray: np.ndarray) -> dict:
    """Western-blot anomalies: repeated band rows (duplicated lanes/bands) and
    sharp vertical splice edges spanning the image height."""
    try:
        g = np.asarray(gray, dtype=np.float32)
        H, W = g.shape
        # --- repeated bands: find anomalous rows, group into segments, compare
        # segment strips pairwise (global autocorrelation dilutes small bands)
        row = g.mean(axis=1)
        med = float(np.median(row))
        std = float(row.std()) + 1e-9
        dev = np.abs(row - med) > 2.5 * std
        segs = []
        y0 = None
        for i, v in enumerate(dev):
            if v and y0 is None:
                y0 = i
            elif not v and y0 is not None:
                if i - y0 >= 6:
                    segs.append((y0, i))
                y0 = None
        if y0 is not None and H - y0 >= 6:
            segs.append((y0, H))
        repeated, best_lag, best_corr = False, None, 0.0
        for i in range(len(segs)):
            for j in range(i + 1, len(segs)):
                a0, a1 = segs[i]
                b0, b1 = segs[j]
                if abs((a1 - a0) - (b1 - b0)) > 2:
                    continue
                hgt = min(a1 - a0, b1 - b0)
                a = g[a0:a0 + hgt, :].ravel()
                b = g[b0:b0 + hgt, :].ravel()
                da, db = a - a.mean(), b - b.mean()
                corr = float((da * db).sum() / (np.sqrt((da ** 2).sum() * (db ** 2).sum()) + 1e-9))
                if corr > best_corr:
                    best_corr, best_lag = corr, abs(a0 - b0)
        repeated = bool(best_corr > 0.85 and best_lag and best_lag >= 20)
        # --- vertical splice edges: column discontinuity energy outliers
        col_diff = np.abs(np.diff(g, axis=1)).mean(axis=0)
        med = float(np.median(col_diff)) + 1e-9
        mad = float(np.median(np.abs(col_diff - med))) + 1e-9
        splice = [int(i) for i in range(len(col_diff))
                  if (col_diff[i] - med) / mad > 8.0][:5]
        flag = bool(repeated or splice)
        return {"repeated_bands": repeated,
                "band_lag": best_lag if repeated else None,
                "band_corr": round(best_corr, 3),
                "splice_columns": splice, "flag": flag,
                "note": "repeated rows suggest duplicated bands; full-height edges suggest splicing"}
    except Exception as e:
        return {"repeated_bands": False, "splice_columns": [], "flag": False,
                "error": str(e)[:200]}
