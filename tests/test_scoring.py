"""Tests for fusion verdicts, flags, gates, and EIG prioritization."""
from app import scoring


def _battery(score_hash=0.9, ssim=0.9, hist=0.9, inl=40, ratio=0.7, cov=0.4,
             contain=0.0, variant="identity", transform="identity_or_cropped"):
    return {
        "base": {"hash_sim": score_hash, "hist_sim": hist,
                 "colorfulness_a": 60.0, "colorfulness_b": 55.0},
        "ssim_by_variant": {variant: ssim},
        "orb_by_variant": {variant: {"inliers": inl, "inlier_ratio": ratio,
                                     "transform": transform, "coverage": cov}},
        "containment": {"score": contain},
        "best": {"variant": variant, "ssim": ssim, "orb_inliers": inl,
                 "orb_transform": transform, "orb_coverage": cov},
    }


def _fig(fid, sha, side=400):
    return {"id": fid, "sha256": sha, "width": side, "height": side}


def test_strong_pair_verdict_high():
    f = scoring.fuse_battery(_battery(), _fig(1, "a"), _fig(2, "b"))
    assert f["verdict"] == "high", f
    assert f["score"] >= 0.82


def test_exact_bytes_detected():
    f = scoring.fuse_battery(_battery(0.5, 0.5, 0.5, 0, 0.0, 0.0),
                             _fig(1, "same"), _fig(2, "same"))
    assert f["verdict"] == "exact_duplicate"
    assert f["score"] == 1.0
    assert "exact_duplicate_bytes" in f["flags"]


def test_generic_content_flagged_and_downgraded():
    b = _battery(score_hash=0.6, ssim=0.65, hist=0.5, inl=5, ratio=0.2, cov=0.1)
    b["base"]["colorfulness_a"] = 5.0
    b["base"]["colorfulness_b"] = 4.0
    f = scoring.fuse_battery(b, _fig(1, "a"), _fig(2, "b"))
    assert "generic_content_risk" in f["flags"]
    assert f["verdict"] in ("weak", "likely_false_positive")


def test_conflicting_signals_tracked():
    f = scoring.fuse_battery(_battery(score_hash=0.9, ssim=0.4, hist=0.5, inl=30),
                             _fig(1, "a"), _fig(2, "b"))
    assert "conflicting_signals" in f["flags"]
    assert f["uncertainty"] > 0.15


def test_containment_dominates_for_partial_overlap():
    f = scoring.fuse_battery(_battery(score_hash=0.55, ssim=0.6, hist=0.5, inl=3,
                                      ratio=0.1, cov=0.05, contain=0.95),
                             _fig(1, "a"), _fig(2, "b"))
    assert f["verdict"] in ("high", "moderate", "exact_duplicate"), f
    assert "partial_overlap_candidate" in f["flags"]


def test_gate_prefilter():
    near = {"phash": "0" * 16, "dhash": "0" * 16, "ahash": "0" * 16}
    far = {"phash": "f" * 16, "dhash": "f" * 16, "ahash": "f" * 16}
    assert scoring.passes_gate(near, dict(near))
    assert not scoring.passes_gate(near, far)
