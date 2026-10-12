"""LLM layer never crashes the loop: null/garbled model output -> heuristic fallback."""
from app import llm


def _cand():
    return {"id": 1, "score": 0.7, "verdict": "moderate", "best_transform": "hflip",
            "uncertainty": 0.2, "signals_json": "{}", "flags_json": "[]"}


def _fig(i):
    return {"id": i, "paper_id": 1, "page": 1, "width": 400, "height": 300,
            "caption": "test", "image_path": "/nonexistent.png"}


def test_null_content_falls_back(monkeypatch):
    monkeypatch.setattr(llm, "_chat", lambda *a, **k: None)
    out = llm.interpret_candidate(_cand(), _fig(1), _fig(2), [], allow_images=False)
    assert out["fallback"] is True


def test_garbled_content_falls_back(monkeypatch):
    monkeypatch.setattr(llm, "_chat", lambda *a, **k: "sorry, no json here")
    out = llm.interpret_candidate(_cand(), _fig(1), _fig(2), [], allow_images=False)
    assert out["fallback"] is True


def test_valid_json_passes_through(monkeypatch):
    monkeypatch.setattr(llm, "_chat", lambda *a, **k:
                        '{"summary":"s","context_hypothesis":"h","confidence":0.5}')
    out = llm.interpret_candidate(_cand(), _fig(1), _fig(2), [], allow_images=False)
    assert out["fallback"] is False
    assert out["summary"] == "s"
