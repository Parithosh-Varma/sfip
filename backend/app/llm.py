"""Grounded LLM layer (NVIDIA NIM, multimodal) + deterministic fallback.

Every LLM call is grounded in stored tool outputs (candidate records,
signal values, evidence rows) and figure images. Responses must be JSON and
cite record ids; on any failure the system falls back to heuristic templates
so the autonomous loop never stalls waiting on a model.
"""
from __future__ import annotations

import base64
import io
import itertools
import json
import re
from typing import Optional

import httpx
from PIL import Image

from .config import settings

_key_cycle = itertools.count()


class LLMUnavailable(Exception):
    pass


class _KeyExhausted(Exception):
    """Rate-limited / rejected key — try the next one."""
    pass


def _vision_data_url(path: str) -> Optional[str]:
    try:
        img = Image.open(path).convert("RGB")
        img.thumbnail((settings.llm_vision_max_side, settings.llm_vision_max_side))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70)
        raw = buf.getvalue()
        if len(raw) > settings.llm_vision_max_bytes:
            img.thumbnail((420, 420))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=55)
            raw = buf.getvalue()
        return "data:image/jpeg;base64," + base64.b64encode(raw).decode()
    except Exception:
        return None


def _post(messages: list, key: str, require_json: bool) -> str:
    payload = {
        "model": settings.llm_model,
        "messages": messages,
        "temperature": settings.llm_temperature,
        "max_tokens": settings.llm_max_tokens,
    }
    if require_json:
        payload["response_format"] = {"type": "json_object"}
    try:
        with httpx.Client(base_url=settings.llm_base_url, timeout=settings.llm_timeout) as c:
            r = c.post("/chat/completions", json=payload,
                       headers={"Authorization": f"Bearer {key}"})
            if r.status_code in (401, 403, 429):
                raise _KeyExhausted(f"HTTP {r.status_code}")
            if r.status_code == 400 and "response_format" in r.text:
                payload.pop("response_format", None)   # model may not support it
                r = c.post("/chat/completions", json=payload,
                           headers={"Authorization": f"Bearer {key}"})
                if r.status_code in (401, 403, 429):
                    raise _KeyExhausted(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        raise LLMUnavailable(str(e)[:300])


def _chat(messages: list, require_json: bool = True) -> str:
    keys = list(settings.llm_api_keys)
    if not keys:
        raise LLMUnavailable("no API key configured")
    last: Optional[Exception] = LLMUnavailable("all keys exhausted")
    # round-robin start spreads load; fail over across keys (bounded)
    start = next(_key_cycle) % len(keys)
    for i in range(min(len(keys), 8)):
        key = keys[(start + i) % len(keys)]
        try:
            return _post(messages, key, require_json)
        except _KeyExhausted as e:
            last = e
            continue
        except LLMUnavailable as e:
            last = e
            break  # network/transport error — retrying another key won't help
    raise last if isinstance(last, Exception) else LLMUnavailable(str(last))


def _parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError("no JSON object in response")
    return json.loads(m.group(0))


def llm_available() -> bool:
    return bool(settings.llm_api_keys)


# --- grounded interpretation --------------------------------------------------

SYSTEM_INTERPRET = (
    "You are a research-integrity image analyst. You describe OBSERVED image "
    "similarities and their evidence. You never assert fraud, misconduct, or "
    "intent; you characterize evidence, weigh alternative explanations "
    "(legitimate reuse of shared controls, common reagent/vendor images, "
    "standard curves, stock patterns, generic plot layouts), and flag weak "
    "evidence. Respond ONLY with a JSON object."
)


def interpret_candidate(cand: dict, fig_a: dict, fig_b: dict,
                        evidence: list[dict], allow_images: bool = True) -> dict:
    """Grounded interpretation of a verified candidate. Returns a dict; the
    'fallback' key is True when produced heuristically."""
    signals = json.loads(cand.get("signals_json") or "{}")
    flags = json.loads(cand.get("flags_json") or "[]")
    grounding = {
        "candidate_id": cand["id"],
        "figure_a": {k: fig_a.get(k) for k in ("id", "paper_id", "page", "width", "height", "caption")},
        "figure_b": {k: fig_b.get(k) for k in ("id", "paper_id", "page", "width", "height", "caption")},
        "fusion": {"score": cand.get("score"), "verdict": cand.get("verdict"),
                   "best_transform": cand.get("best_transform"),
                   "uncertainty": cand.get("uncertainty"), "flags": flags},
        "signals": signals,
        "evidence_rows": [{"id": e["id"], "method": e["method"], "value": e["value"],
                           "interpretation": e["interpretation"]} for e in evidence[:20]],
    }
    prompt = (
        "Analyze this verified image-similarity candidate. Ground every claim in "
        "the provided record ids (cite them like [evidence:12]) and the signals. "
        "Return JSON with keys: "
        "context_hypothesis (string), what kind of figure pair this likely is and what "
        "the similarity most plausibly reflects; "
        "alternative_explanations (array of strings, >=2, benign and non-benign); "
        "weaknesses (array of strings: what is weak or could make this a false positive); "
        "recommended_followup (array of strings: autonomous checks, not human actions); "
        "confidence (0..1); "
        "summary (2-3 sentences, neutral scientific register).\n\n"
        f"GROUNDING:\n{json.dumps(grounding, indent=1)[:12000]}"
    )
    content: list = [{"type": "text", "text": prompt}]
    used_images = False
    if allow_images:
        for f in (fig_a, fig_b):
            du = _vision_data_url(f.get("image_path") or "")
            if du:
                content.append({"type": "image_url", "image_url": {"url": du}})
                used_images = True
    messages = [
        {"role": "system", "content": SYSTEM_INTERPRET},
        {"role": "user", "content": content if used_images else [{"type": "text", "text": prompt}]},
    ]
    try:
        raw = _chat(messages)
        out = _parse_json(raw)
        out["llm_model"] = settings.llm_model
        out["used_images"] = used_images
        out["fallback"] = False
        return out
    except (LLMUnavailable, ValueError, json.JSONDecodeError) as e:
        return _heuristic_interpretation(cand, fig_a, fig_b, e)


def _heuristic_interpretation(cand: dict, fig_a: dict, fig_b: dict, err: Exception) -> dict:
    signals = json.loads(cand.get("signals_json") or "{}")
    v = cand.get("verdict")
    tf = cand.get("best_transform") or "identity"
    if v in ("high", "exact_duplicate"):
        hyp = (f"Strong observed image similarity under transformation '{tf}' "
               f"(score {cand.get('score')}). Consistent with a shared source image.")
    elif v == "moderate":
        hyp = (f"Moderate similarity (score {cand.get('score')}, best variant '{tf}'); "
               "could reflect shared content or partial reuse.")
    else:
        hyp = f"Weak similarity (score {cand.get('score')}); more consistent with generic structure."
    return {
        "context_hypothesis": hyp,
        "alternative_explanations": [
            "Legitimate reuse of a shared control/standard image across panels or papers by the same group.",
            "Common vendor/catalog or textbook source image used independently.",
            "Generic scientific chart layout producing coincidental structure.",
            "Genuine duplication through production/copy error (without implying intent).",
        ],
        "weaknesses": [
            f"Signals: hash={signals.get('hash_similarity')}, ssim={signals.get('ssim')}, "
            f"orb_inliers={signals.get('orb_inliers')}, containment={signals.get('containment')}.",
            "Automated heuristic interpretation (no LLM/model analysis available).",
        ],
        "recommended_followup": [
            "Cross-check both figures against all other figures in the corpus.",
            "Inspect panels visually for partial overlap and splicing boundaries.",
        ],
        "confidence": cand.get("score", 0.0),
        "summary": hyp + " Interpretation generated by deterministic fallback; no conclusion about intent is drawn.",
        "fallback": True,
        "error": str(err)[:300],
    }


# --- advisory planning --------------------------------------------------------

def plan_focus(state_summary: dict) -> dict:
    """Advisory next-focus suggestion. Deterministic priorities remain authoritative."""
    if not llm_available():
        return {"fallback": True}
    prompt = (
        "You are the planning advisor of an autonomous image-forensics controller. "
        "Given the current state, suggest where to focus next. You may ONLY suggest "
        "these action kinds: expand_discovery, deep_verify_candidates, cross_check, "
        "interpret_high_value. Return JSON: {focus: string, rationale: string, "
        "candidate_ids: [int]} (cite at most 5 candidate ids worth prioritizing).\n"
        f"STATE:\n{json.dumps(state_summary, indent=1)[:6000]}"
    )
    try:
        out = _parse_json(_chat([{"role": "system", "content": SYSTEM_INTERPRET},
                                 {"role": "user", "content": prompt}]))
        out["fallback"] = False
        return out
    except Exception as e:
        return {"fallback": True, "error": str(e)[:200]}
