"""Offline tests for the forensic core: synthetic images only, no network."""
import numpy as np
import pytest
from PIL import Image, ImageDraw

from app import vision


@pytest.fixture(scope="module")
def imgs(tmp_path_factory):
    rng = np.random.default_rng(7)
    base = Image.new("RGB", (320, 240), "white")
    d = ImageDraw.Draw(base)
    for _ in range(10):
        x0, y0 = int(rng.integers(10, 240)), int(rng.integers(10, 180))
        d.ellipse([x0, y0, x0 + int(rng.integers(20, 70)), y0 + int(rng.integers(15, 50))],
                  fill=tuple(int(c) for c in rng.integers(0, 200, 3)))
    d.text((15, 5), "Figure 1. Synthetic panel", fill="black")
    td = tmp_path_factory.mktemp("vision")
    p0 = td / "orig.png"
    p1 = td / "flipped.png"
    p2 = td / "blank.png"
    base.save(p0)
    base.transpose(Image.FLIP_LEFT_RIGHT).crop((30, 20, 300, 220)).save(p1)
    Image.new("RGB", (320, 240), (240, 240, 255)).save(p2)
    return p0, p1, p2


def test_hash_similarity_orders_pairs(imgs):
    p0, p1, p2 = imgs
    h0, h1, h2 = (vision.hashes(vision.load_rgb(p)) for p in (p0, p1, p2))
    assert vision.hash_similarity(h0, h1) > vision.hash_similarity(h0, h2)


def test_variant_search_finds_flip(imgs):
    p0, p1, _ = imgs
    b = vision.run_battery(p0, p1)
    assert b["best"]["variant"] == "hflip", b["best"]
    assert b["best"]["orb_inliers"] >= 12
    assert b["containment"]["score"] > 0.7


def test_unrelated_pair_is_weak(imgs):
    p0, _, p2 = imgs
    b = vision.run_battery(p0, p2)
    assert b["best"]["orb_inliers"] < 12
    assert b["containment"]["score"] < 0.7


def test_ssim_identity_is_one(imgs):
    p0, _, _ = imgs
    g = vision.load_gray(p0)
    assert vision.ssim_score(g, g) == pytest.approx(1.0, abs=1e-6)
