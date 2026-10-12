"""Multi-signal image-forensics engine.

Independent signals computed per figure pair:
  1. perceptual hashes (pHash/dHash/aHash, Hamming)
  2. SSIM on luminance after transformation variants
  3. HSV histogram intersection (color layout)
  4. ORB feature matching + RANSAC homography (geometric verification of
     cropping, flipping, rotation, stretching, translation)
  5. multi-scale containment / template matching (partial overlap of a small
     figure inside a larger one)

The battery explicitly tests transformation variants: flips, rotations,
aspect stretch, center crops, rescales.
"""
from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from typing import Union

import cv2
import imagehash
import numpy as np
from PIL import Image
from skimage.metrics import structural_similarity as _ssim

OPS_SIDE = 1024          # max side for heavy ops
SSIM_SIDE = 256          # common canvas for SSIM
ORB_FEATURES = 1500

PathOrBytes = Union[str, Path, bytes]


def load_rgb(src: PathOrBytes) -> Image.Image:
    if isinstance(src, (bytes, bytearray)):
        img = Image.open(BytesIO(src))
    else:
        img = Image.open(str(src))
    return img.convert("RGB")


def load_gray(src: PathOrBytes, max_side: int = OPS_SIDE) -> np.ndarray:
    img = load_rgb(src)
    img.thumbnail((max_side, max_side))
    return np.asarray(img.convert("L"))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# --- perceptual hashes --------------------------------------------------------

def hashes(img: Image.Image) -> dict:
    return {
        "phash": str(imagehash.phash(img)),
        "dhash": str(imagehash.dhash(img)),
        "ahash": str(imagehash.average_hash(img)),
    }


def hamming_hex(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def hash_similarity(h1: dict, h2: dict) -> float:
    """0..1 similarity; weighted blend of the three hash distances (64-bit)."""
    d_p = hamming_hex(h1["phash"], h2["phash"]) if h1.get("phash") and h2.get("phash") else 32
    d_d = hamming_hex(h1["dhash"], h2["dhash"]) if h1.get("dhash") and h2.get("dhash") else 32
    d_a = hamming_hex(h1["ahash"], h2["ahash"]) if h1.get("ahash") and h2.get("ahash") else 32
    return 0.5 * (1 - d_p / 64) + 0.3 * (1 - d_d / 64) + 0.2 * (1 - d_a / 64)


# --- SSIM ---------------------------------------------------------------------

def _to_canvas(g: np.ndarray, side: int = SSIM_SIDE) -> np.ndarray:
    im = Image.fromarray(g).resize((side, side), Image.BILINEAR)
    return np.asarray(im)


def ssim_score(ga: np.ndarray, gb: np.ndarray, side: int = SSIM_SIDE) -> float:
    a, b = _to_canvas(ga, side), _to_canvas(gb, side)
    return float(_ssim(a, b))


# --- color histogram ----------------------------------------------------------

def hist_similarity(rgb_a: np.ndarray, rgb_b: np.ndarray) -> float:
    ha = cv2.calcHist([cv2.cvtColor(rgb_a, cv2.COLOR_RGB2HSV)], [0, 1], None,
                      [24, 16], [0, 180, 0, 256])
    hb = cv2.calcHist([cv2.cvtColor(rgb_b, cv2.COLOR_RGB2HSV)], [0, 1], None,
                      [24, 16], [0, 180, 0, 256])
    cv2.normalize(ha, ha)
    cv2.normalize(hb, hb)
    return min(1.0, float(cv2.compareHist(ha, hb, cv2.HISTCMP_INTERSECT)))


def colorfulness(rgb: np.ndarray) -> float:
    """Hasler–Süsstrunk colorfulness metric — used for false-positive checks
    (flat grayscale plots are more prone to generic similarity)."""
    b, g, r = rgb[:, :, 0].astype(float), rgb[:, :, 1].astype(float), rgb[:, :, 2].astype(float)
    rg = np.abs(r - g)
    yb = np.abs(0.5 * (r + g) - b)
    return float(np.sqrt(rg.std() ** 2 + yb.std() ** 2) + 0.3 * np.sqrt(rg.mean() ** 2 + yb.mean() ** 2))


# --- geometric verification (ORB + homography) --------------------------------

_orb = cv2.ORB_create(nfeatures=ORB_FEATURES)


def orb_verify(ga: np.ndarray, gb: np.ndarray) -> dict:
    kp1, d1 = _orb.detectAndCompute(ga, None)
    kp2, d2 = _orb.detectAndCompute(gb, None)
    out = {"matches": 0, "inliers": 0, "inlier_ratio": 0.0, "transform": "none",
           "flip": None, "stretch_x": 1.0, "stretch_y": 1.0, "coverage": 0.0, "homography": None}
    if d1 is None or d2 is None or len(kp1) < 8 or len(kp2) < 8:
        return out
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
    knn = matcher.knnMatch(d1, d2, k=2)
    good = [m for m, n in (pair for pair in knn if len(pair) == 2) if m.distance < 0.75 * n.distance]
    out["matches"] = len(good)
    if len(good) < 8:
        return out
    src = np.float32([kp1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    dst = np.float32([kp2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    H, mask = cv2.findHomography(src, dst, cv2.RANSAC, 5.0)
    if H is None or mask is None:
        return out
    inl = int(mask.sum())
    out["inliers"] = inl
    out["inlier_ratio"] = inl / max(1, len(good))
    out["homography"] = [round(float(x), 5) for x in H.flatten()]
    if inl >= 8:
        out["transform"] = _classify_homography(H, ga, gb, kp1, good, mask)
    # coverage: what fraction of image A the matched points span
    pts = src[mask.ravel() == 1].reshape(-1, 2)
    if len(pts) >= 4:
        w = max(ga.shape[1], 1)
        h = max(ga.shape[0], 1)
        x0, y0 = pts.min(axis=0)
        x1, y1 = pts.max(axis=0)
        out["coverage"] = float(((x1 - x0) * (y1 - y0)) / (w * h))
    return out


def _classify_homography(H: np.ndarray, ga: np.ndarray, gb: np.ndarray,
                         kp1, good, mask) -> str:
    a, b = H[0, 0], H[0, 1]
    c, d = H[1, 0], H[1, 1]
    det = a * d - b * c
    sx = float(np.hypot(a, c))
    sy = float(np.hypot(b, d))
    angle = float(np.degrees(np.arctan2(c, a)))
    if det < 0:
        return "hflip"
    if abs(angle) > 20:
        label = "rotated"
    elif max(sx, sy) / max(1e-6, min(sx, sy)) > 1.2:
        label = "stretched"
    elif 0.9 < sx < 1.1 and 0.9 < sy < 1.1 and abs(angle) < 5:
        label = "identity_or_cropped"
    elif sx < 0.95 or sy < 0.95:
        label = "resized"
    else:
        label = "transformed"
    # partial: smaller image fully contained? detect via coverage of larger side
    return label


def containment_check(ga: np.ndarray, gb: np.ndarray) -> dict:
    """Multi-scale template match: is the smaller figure embedded in the larger?"""
    if ga.shape[0] < 8 or gb.shape[0] < 8:
        return {"score": 0.0, "scale": None, "partial_overlap": False}
    area_a, area_b = ga.size, gb.size
    small, big = (ga, gb) if area_a <= area_b else (gb, ga)
    if big.size / max(1, small.size) < 1.4:
        return {"score": 0.0, "scale": None, "partial_overlap": False}
    best = {"score": 0.0, "scale": None, "partial_overlap": True}
    sh, sw = small.shape
    for scale in np.arange(0.35, 1.02, 0.05):
        nw, nh = max(8, int(sw * scale)), max(8, int(sh * scale))
        if nh >= big.shape[0] or nw >= big.shape[1]:
            continue
        tmpl = cv2.resize(small, (nw, nh), interpolation=cv2.INTER_AREA)
        res = cv2.matchTemplate(big, tmpl, cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(res)
        if mx > best["score"]:
            best = {"score": float(mx), "scale": round(float(scale), 3),
                    "partial_overlap": True, "loc": [int(loc[0]), int(loc[1])],
                    "template_size": [nw, nh]}
    return best


# --- transformation variants --------------------------------------------------

def variant_set(gb: np.ndarray) -> list[tuple[str, np.ndarray]]:
    im = Image.fromarray(gb)
    h, w = gb.shape
    out: list[tuple[str, np.ndarray]] = [("identity", gb)]

    def arr(i: Image.Image) -> np.ndarray:
        return np.asarray(i.convert("L"))

    out.append(("hflip", arr(im.transpose(Image.FLIP_LEFT_RIGHT))))
    out.append(("vflip", arr(im.transpose(Image.FLIP_TOP_BOTTOM))))
    out.append(("rot90", arr(im.transpose(Image.ROTATE_90))))
    out.append(("rot180", arr(im.transpose(Image.ROTATE_180))))
    out.append(("rot270", arr(im.transpose(Image.ROTATE_270))))
    for f in (0.8, 1.25):
        out.append((f"stretch_w{f}", arr(im.resize((max(8, int(w * f)), h)))))
    for frac in (0.75, 0.5):
        cw, ch = max(8, int(w * frac)), max(8, int(h * frac))
        x0, y0 = (w - cw) // 2, (h - ch) // 2
        out.append((f"crop_c{int(frac * 100)}", gb[y0:y0 + ch, x0:x0 + cw]))
    return out


# --- full battery -------------------------------------------------------------

def run_battery(path_a: Path, path_b: Path) -> dict:
    """Run the complete signal battery on a figure pair."""
    img_a, img_b = load_rgb(path_a), load_rgb(path_b)
    ga, gb = load_gray(path_a), load_gray(path_b)
    h1, h2 = hashes(img_a), hashes(img_b)

    base = {
        "hash_sim": round(hash_similarity(h1, h2), 4),
        "hist_sim": round(hist_similarity(np.asarray(img_a), np.asarray(img_b)), 4),
        "colorfulness_a": round(colorfulness(np.asarray(img_a)), 2),
        "colorfulness_b": round(colorfulness(np.asarray(img_b)), 2),
    }

    # cheap SSIM over all variants, then heavy verification on the top few
    var_scores = []
    for name, gv in variant_set(gb):
        try:
            var_scores.append((name, round(ssim_score(ga, gv), 4)))
        except Exception:
            continue
    var_scores.sort(key=lambda t: -t[1])
    top = var_scores[:4]

    orb_results = {}
    for name, _ in top:
        gv = dict(variant_set(gb))[name]
        orb_results[name] = orb_verify(ga, gv)

    best_name, best_ssim = var_scores[0] if var_scores else ("identity", 0.0)
    best_orb = orb_results.get(best_name, {"inliers": 0})

    contain = containment_check(ga, gb)
    # containment may also be explained by a flip variant
    contain_flip = containment_check(ga, np.fliplr(gb))
    if contain_flip.get("score", 0) > contain.get("score", 0):
        contain = {**contain_flip, "variant": "hflip"}

    return {
        "base": base,
        "ssim_by_variant": dict(var_scores),
        "orb_by_variant": orb_results,
        "containment": contain,
        "best": {
            "variant": best_name,
            "ssim": best_ssim,
            "orb_inliers": int(best_orb.get("inliers", 0)),
            "orb_transform": best_orb.get("transform", "none"),
            "orb_coverage": round(float(best_orb.get("coverage", 0.0)), 4),
        },
    }
