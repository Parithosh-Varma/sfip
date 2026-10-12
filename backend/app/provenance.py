"""AI-provenance screening: is this figure AI-generated or AI-modified?

Layers (strongest first):
  1. metadata forensics — generator tags left by AI tools (Stable Diffusion
     parameter chunks, ComfyUI workflows, DALL-E/Firefly/Midjourney markers),
     EXIF/XMP software tags, C2PA Content Credentials manifest presence.
  2. diffusion fingerprint screen — FFT spectral-peak anomaly typical of
     neural upsamplers. Weak, directional signal only.
  3. SynthID provider hook — calls a real watermark-detection endpoint when
     configured (env); otherwise records `not_configured` with enablement
     instructions. Detection is NEVER faked.

Language discipline: this reports *observed provenance signals* and a
suspicion level with caveats — never a verdict that an image "is AI".
Absence of signals proves nothing (metadata is routinely stripped).
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
from pathlib import Path
from typing import Optional, Union

import httpx
import numpy as np
from PIL import Image
from PIL.ExifTags import TAGS

PathLike = Union[str, Path]

# --- known markers ------------------------------------------------------------

AI_GENERATORS = (
    "stable diffusion", "automatic1111", "a1111", "comfyui", "novelai", "nai diffusion",
    "dall-e", "dalle", "dall·e", "midjourney", "firefly", "imagen", "leonardo",
    "ideogram", "civitai", "invokeai", "fooocus", "playground ai", "tensor.art",
    "yodayo", "gpt-image", "gpt-4o", "chatgpt image", "nano banana", "gemini image",
    "ai generated", "generated with ai", "ai-generated", "synthetic image",
)

EDITORS = (
    "photoshop", "illustrator", "gimp", "lightroom", "inkscape", "inkscape",
    "imagej", "fiji", " affinity", "canva", "biorender", "graphpad", "originlab",
    "matplotlib", "python", "r studio",
)

EXIF_TAGS_OF_INTEREST = {271: "Make", 272: "Model", 270: "ImageDescription",
                         305: "Software", 306: "DateTime", 315: "Artist",
                         33432: "Copyright", 700: "XMLPacket"}

MANUAL_PORTAL = ("Manual follow-up options for high-suspicion figures: Google's public "
                 "SynthID Detector site and OpenAI's image provenance check tool "
                 "(both cover their ecosystems' watermarks; login-gated with daily quotas).")


def _hit(text: str, markers: tuple) -> Optional[str]:
    low = text.lower()
    for m in markers:
        if m in low:
            return m
    return None


# --- layer 1: metadata ----------------------------------------------------------

def metadata_screen(path: PathLike) -> dict:
    out: dict = {"format": None, "ai_markers": [], "editor_markers": [],
                 "exif": {}, "png_text_keys": [], "xmp_creator_tool": None,
                 "c2pa_manifest_present": False, "c2pa_lib_available": False,
                 "metadata_stripped": True, "verdict": "clean"}
    try:
        raw = Path(path).read_bytes()
    except OSError as e:
        return {**out, "verdict": "unreadable", "error": str(e)[:200]}

    out["c2pa_manifest_present"] = b"jumb" in raw and b"c2pa" in raw
    out["c2pa_lib_available"] = importlib.util.find_spec("c2pa") is not None

    try:
        img = Image.open(path)
        out["format"] = img.format
    except Exception as e:
        return {**out, "verdict": "unreadable", "error": str(e)[:200]}

    blob_texts: list[tuple[str, str]] = []  # (field, value)

    # EXIF
    try:
        exif = img.getexif()
        for tag_id, name in EXIF_TAGS_OF_INTEREST.items():
            if tag_id in exif:
                val = str(exif[tag_id])[:400]
                out["exif"][name] = val
                blob_texts.append((f"exif:{name}", val))
    except Exception:
        pass

    # PNG text chunks (tEXt/zTXt/iTXt): A1111 'parameters', ComfyUI 'prompt'/'workflow'
    try:
        info = dict(getattr(img, "info", {}) or {})
        text_dict = dict(getattr(img, "text", {}) or {})
        info.update(text_dict)
        for k, v in info.items():
            if isinstance(v, str) and len(v) < 20000:
                out["png_text_keys"].append(str(k)[:60])
                blob_texts.append((f"png:{k}", v[:2000]))
            elif k == "parameters":
                blob_texts.append(("png:parameters", str(v)[:2000]))
    except Exception:
        pass

    # XMP creator tool from raw bytes
    m = re.search(rb"<xmp:CreatorTool>(.*?)</xmp:CreatorTool>", raw, re.DOTALL)
    if not m:
        m = re.search(rb"dc:creatorTool[^>]*>([^<]{1,200})<", raw)
    if not m:
        m = re.search(rb"xmp:CreatorTool=\"([^\"]{1,200})\"", raw)
    if m:
        try:
            out["xmp_creator_tool"] = m.group(1).decode("utf-8", "ignore")[:200]
            blob_texts.append(("xmp:creator_tool", out["xmp_creator_tool"]))
        except Exception:
            pass

    for field, val in blob_texts:
        hit = _hit(val, AI_GENERATORS)
        if hit and (hit, field) not in out["ai_markers"]:
            out["ai_markers"].append([hit, field])
        ed = _hit(val, EDITORS)
        if ed and (ed, field) not in out["editor_markers"]:
            out["editor_markers"].append([ed, field])

    if blob_texts or out["xmp_creator_tool"] or out["c2pa_manifest_present"]:
        out["metadata_stripped"] = False

    if out["ai_markers"]:
        out["verdict"] = "ai_tool_marked"
    elif out["c2pa_manifest_present"]:
        out["verdict"] = "c2pa_present"
    elif out["editor_markers"]:
        out["verdict"] = "edited_with_software"
    elif out["metadata_stripped"]:
        out["verdict"] = "stripped_or_clean"
    return out


# --- layer 2: diffusion fingerprint screen ---------------------------------------

def diffusion_screen(gray: np.ndarray) -> dict:
    """FFT radial-spectrum peakiness. Neural upsamplers leave spectral spikes;
    natural images decay ~smoothly. WEAK directional screen, not a detector."""
    try:
        g = np.asarray(gray, dtype=np.float32)
        g -= g.mean()
        F = np.fft.rfft2(g)
        mag = np.log1p(np.abs(F))
        yy, xx = np.mgrid[0:mag.shape[0], 0:mag.shape[1]]
        r = np.sqrt(yy ** 2 + xx ** 2)
        nbins, rmax = 64, float(r.max())
        prof = np.array([mag[(r >= i * rmax / nbins) & (r < (i + 1) * rmax / nbins)].mean()
                         for i in range(nbins)])
        # local spikiness vs the smooth 1/f decay: each bin against its neighbors.
        # Periodic grids / upsampler artifacts stick out; smooth spectra (ramps,
        # gradients, natural scenes) do not.
        band = prof[8:]
        spikes = []
        for i in range(2, len(band) - 2):
            neigh = float(np.mean(list(band[i - 2:i]) + list(band[i + 1:i + 3]))) + 1e-9
            spikes.append(float(band[i]) / neigh)
        peakiness = max(spikes) - 1.0 if spikes else 0.0
        anomaly = round(max(0.0, min(1.0, peakiness / 3.0)), 4)
        return {"anomaly": anomaly, "peakiness": round(float(peakiness), 3),
                "note": "weak screen: high values merit review, low values prove nothing"}
    except Exception as e:
        return {"anomaly": 0.0, "peakiness": 0.0, "error": str(e)[:200]}


# --- layer 3: SynthID provider hook ----------------------------------------------

def _provider_endpoints() -> list[str]:
    eps = [e.strip() for e in os.getenv("SFIP_AI_PROVIDERS", "").split(",") if e.strip()]
    legacy = os.getenv("SFIP_SYNTHID_ENDPOINT", "").strip()
    if legacy and legacy not in eps:
        eps.append(legacy)
    return eps


def synthid_check(path: PathLike) -> dict:
    """Call real watermark/AI-detection endpoints when configured (tried in
    order, first decisive result wins); otherwise record not_configured.
    NEVER synthesizes a detection result."""
    endpoints = _provider_endpoints()
    token = os.getenv("SFIP_SYNTHID_TOKEN", "").strip()
    if not endpoints:
        return {"status": "not_configured", "provider": None, "watermarked": None,
                "confidence": None,
                "how_to_enable": ("Set SFIP_AI_PROVIDERS (comma-separated) to watermark/AI-"
                                  "verification REST endpoints accepting multipart image "
                                  "upload (e.g. Google Cloud AI Content Detection API, "
                                  "Sightengine/Hive AI-generated-image endpoints); "
                                  "SFIP_SYNTHID_TOKEN supplies the bearer token."),
                "manual_followup": MANUAL_PORTAL}
    tried = []
    for endpoint in endpoints[:4]:  # bounded provider chain
        res = _try_provider(endpoint, token, path)
        tried.append({"provider": endpoint, "status": res["status"]})
        if res.get("watermarked") is True or res.get("status") == "checked":
            return {**res, "tried": tried}
    return {"status": "provider_undecided", "provider": None, "watermarked": None,
            "confidence": None, "tried": tried, "manual_followup": MANUAL_PORTAL}


def _try_provider(endpoint: str, token: str, path: PathLike) -> dict:
    try:
        with open(path, "rb") as f:
            files = {"file": (Path(path).name, f, "application/octet-stream")}
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            with httpx.Client(timeout=90.0) as c:
                r = c.post(endpoint, files=files, headers=headers)
        if r.status_code in (401, 403, 429):
            return {"status": "provider_rejected", "provider": endpoint,
                    "http": r.status_code, "watermarked": None, "confidence": None,
                    "manual_followup": MANUAL_PORTAL}
        r.raise_for_status()
        try:
            body = r.json()
        except ValueError:
            return {"status": "provider_bad_response", "provider": endpoint,
                    "watermarked": None, "confidence": None}
        text = json.dumps(body).lower()
        if isinstance(body, dict):
            wm = body.get("watermarked", body.get("detected", body.get("is_ai_generated")))
            conf = body.get("confidence", body.get("score"))
        else:
            wm, conf = None, None
        if wm is None:
            positive = any(w in text for w in ("watermark detected", "watermarked\":true",
                                               "detected\":true", "\"verdict\":\"detected"))
            wm = True if positive else None
        return {"status": "checked", "provider": endpoint,
                "watermarked": bool(wm) if wm is not None else None,
                "confidence": conf, "raw_keys": list(body.keys())[:10] if isinstance(body, dict) else []}
    except Exception as e:
        return {"status": "provider_error", "provider": endpoint,
                "watermarked": None, "confidence": None, "error": str(e)[:300],
                "manual_followup": MANUAL_PORTAL}


# --- combined ---------------------------------------------------------------------

def run_provenance(path: PathLike, gray: Optional[np.ndarray] = None) -> dict:
    meta = metadata_screen(path)
    if gray is None:
        try:
            from .vision import load_gray
            gray = load_gray(path)
        except Exception:
            gray = None
    diff = diffusion_screen(gray) if gray is not None else {"anomaly": 0.0, "note": "no pixels"}
    syn = synthid_check(path)

    if syn.get("watermarked") is True:
        suspicion, why = "high", "watermark detector reported a watermark"
    elif meta["verdict"] == "ai_tool_marked":
        suspicion, why = "high", f"generator marker(s): {meta['ai_markers']}"
    elif diff.get("anomaly", 0) >= 0.6:
        suspicion, why = "medium", f"spectral anomaly {diff['anomaly']} (weak signal)"
    elif meta["verdict"] == "c2pa_present":
        suspicion, why = "low", "signed C2PA manifest present (provenance attached)"
    else:
        suspicion, why = "low", "no AI-provenance signals observed"

    caveats = [
        "Absence of signals proves nothing: metadata is routinely stripped by publishers.",
        "The spectral screen is directional, not diagnostic; natural textures can score high.",
    ]
    if syn.get("status") == "not_configured":
        caveats.append("No watermark-detection provider configured; SynthID status unknown.")
    return {"suspicion": suspicion, "reason": why, "metadata": meta,
            "diffusion": diff, "synthid": syn, "caveats": caveats}
