"""PDF figure/panel extraction.

Raster figures are extracted from embedded images; vector figures (common in
publisher PDFs) are detected via caption anchors and rendered from the region
above the caption. Captions are matched by proximity and panel labels like
(A), (B) are recorded.
"""
from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Optional

import pymupdf
from PIL import Image

from .config import settings
from . import vision

CAPTION_RE = re.compile(r"^\s*(Figure|Fig\.?|FIGURE)\s*\d+[A-Za-z]?\s*[.:]")
PANEL_RE = re.compile(r"\(([A-Z]{1,2})\)")


def _caption_blocks(page) -> list[dict]:
    out = []
    d = page.get_text("dict")
    for b in d.get("blocks", []):
        if b.get("type") != 0:
            continue
        text = "\n".join(s["text"] for line in b.get("lines", []) for s in line.get("spans", [])).strip()
        if CAPTION_RE.match(text):
            out.append({"bbox": pymupdf.Rect(b["bbox"]), "text": text})
    return out


def _nearest_caption(captions: list[dict], rect) -> Optional[dict]:
    best, best_d = None, 1e9
    for c in captions:
        cb = c["bbox"]
        dy = cb.y0 - rect.y1 if cb.y0 >= rect.y1 else rect.y0 - cb.y1
        overlap = not (cb.x1 < rect.x0 or cb.x0 > rect.x1)
        d = dy - (50 if overlap else 0)
        if 0 <= d < best_d < 300:
            best, best_d = c, d
    return best


def _save_png(img_bytes: bytes, dest: Path) -> Optional[tuple[int, int]]:
    try:
        img = Image.open(io.BytesIO(img_bytes))
        img = img.convert("RGB")
        if min(img.size) < settings.min_figure_side or img.size[0] * img.size[1] < settings.min_figure_area:
            return None
        img.save(dest, format="PNG")
        return img.size
    except Exception:
        return None


def extract_figures(pdf_path: str, out_dir: Path) -> list[dict]:
    """Extract figures from a PDF. Returns figure dicts (paths + metadata)."""
    out: list[dict] = []
    doc = pymupdf.open(pdf_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    seen_xrefs: set[int] = set()
    idx = 0
    for pno in range(len(doc)):
        page = doc[pno]
        captions = _caption_blocks(page)

        # --- raster figures
        for img_info in page.get_images(full=True):
            xref = img_info[0]
            if xref in seen_xrefs:
                continue
            seen_xrefs.add(xref)
            try:
                rects = page.get_image_rects(xref)
            except Exception:
                rects = []
            if not rects:
                continue
            rect = max(rects, key=lambda r: r.width * r.height)
            if rect.width * rect.height < (settings.min_figure_side * 1.4) ** 2:
                continue
            try:
                data = doc.extract_image(xref)
                img_bytes = data["image"]
            except Exception:
                continue
            idx += 1
            dest = out_dir / f"fig_{idx:04d}.png"
            size = _save_png(img_bytes, dest)
            if not size:
                idx -= 1
                continue
            cap = _nearest_caption(captions, rect)
            out.append(_fig_dict(pno + 1, idx, size, dest, cap, "raster"))

        # --- vector figures (caption present, nothing raster captured nearby)
        if captions:
            captured = [f for f in out if f["page"] == pno + 1]
            for cap in captions:
                if any(f.get("_cap_text") == cap["text"][:80] for f in captured):
                    continue
                if len(cap["text"]) > 900:
                    continue
                region = _region_above(page, cap["bbox"])
                if region is None:
                    continue
                idx += 1
                dest = out_dir / f"fig_{idx:04d}.png"
                try:
                    pix = page.get_pixmap(clip=region, matrix=pymupdf.Matrix(2, 2))
                    size = _save_png(pix.tobytes("png"), dest)
                except Exception:
                    size = None
                if not size:
                    idx -= 1
                    continue
                out.append(_fig_dict(pno + 1, idx, size, dest, cap, "rendered"))
    doc.close()

    # finalize: hashes + panel labels
    final = []
    for f in out:
        try:
            img = vision.load_rgb(f.pop("_path"))
            f.update(vision.hashes(img))
            f["sha256"] = vision.sha256_file(Path(f["image_path"]))
            f["panel_labels"] = ",".join(sorted(set(PANEL_RE.findall(f.get("caption") or ""))))[:80]
        except Exception:
            continue
        final.append(f)
    return final


def _fig_dict(page: int, idx: int, size: tuple[int, int], dest: Path,
              cap: Optional[dict], kind: str) -> dict:
    return {
        "page": page, "idx_in_page": idx,
        "width": size[0], "height": size[1],
        "image_path": str(dest), "kind": kind,
        "caption": (cap["text"][:1500] if cap else None),
        "_cap_text": (cap["text"][:80] if cap else None),
        "_path": str(dest),
    }


def _region_above(page, cap_bbox) -> Optional[pymupdf.Rect]:
    """Region between the content above and the caption: candidate figure area."""
    top_limit = 36.0
    bottom = cap_bbox.y0 - 6
    # walk text blocks: find lowest text bottom above the caption
    y = bottom
    d = page.get_text("dict")
    for b in d.get("blocks", []):
        bb = pymupdf.Rect(b["bbox"])
        if bb.y1 <= bottom and bb.y1 > top_limit + 20:
            if CAPTION_RE.match("\n".join(s["text"] for line in b.get("lines", [])
                                          for s in line.get("spans", [])).strip()):
                continue
            y = max(bb.y1, top_limit + 40) if (bottom - bb.y1) < 260 else y
    region = pymupdf.Rect(page.rect.x0 + 30, max(top_limit, y - 40), page.rect.x1 - 30, bottom)
    if region.height < 60 or region.width < 120:
        return None
    return region
