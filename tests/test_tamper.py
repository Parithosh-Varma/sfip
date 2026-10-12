"""Tests for within-image tamper screening + Sholto-David-inspired channels."""
import numpy as np
import pytest
from PIL import Image

from app import tamper, vision, scoring


def _shapes(seed=11, size=(400, 300)):
    rng = np.random.default_rng(seed)
    img = Image.new("RGB", size, "white")
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    for _ in range(14):
        x0, y0 = int(rng.integers(10, size[0] - 90)), int(rng.integers(10, size[1] - 70))
        d.ellipse([x0, y0, x0 + int(rng.integers(25, 80)), y0 + int(rng.integers(20, 60))],
                  fill=tuple(int(c) for c in rng.integers(0, 200, 3)))
    return np.asarray(img.convert("L"))


def test_clone_scan_finds_copied_patch():
    g = _shapes()
    g2 = g.copy()
    patch = g[40:110, 50:140].copy()          # copy-move within the image
    g2[180:250, 250:340] = patch
    r = tamper.clone_scan(g2)
    assert r["flag"] is True, r
    assert r["clone_clusters"] >= 1


def test_clone_scan_finds_mirrored_patch():
    g = _shapes()
    g2 = g.copy()
    patch = g[40:110, 50:140]
    g2[180:250, 250:340] = np.fliplr(patch)
    r = tamper.clone_scan(g2)
    assert r["flag"] is True, r


def test_clone_scan_clean_image_quiet():
    g = _shapes()
    r = tamper.clone_scan(g)
    assert r["flag"] is False, r


def test_blot_screen_finds_repeated_bands():
    rng = np.random.default_rng(3)
    g = rng.normal(200, 6, (300, 200))
    g[60:75, :] -= 120                        # dark band
    g[180:195, :] = g[60:75, :]               # duplicated band
    r = tamper.blot_screen(g.astype(np.float32))
    assert r["repeated_bands"] is True, r
    assert r["flag"] is True


def test_blot_screen_finds_splice_edge():
    g = np.full((200, 200), 180.0)
    g[:, 100:] -= 60                          # hard vertical splice
    rng = np.random.default_rng(4)
    g += rng.normal(0, 2, g.shape)
    r = tamper.blot_screen(g)
    assert r["splice_columns"], r
    assert any(95 <= c <= 105 for c in r["splice_columns"]), r["splice_columns"]


def test_fine_rotation_variant_detected(tmp_path):
    g = _shapes()
    p0, p1 = tmp_path / "a.png", tmp_path / "b.png"
    Image.fromarray(g).save(p0)
    import cv2
    h, w = g.shape
    M = cv2.getRotationMatrix2D((w / 2, h / 2), 20, 1.0)
    cv2.imwrite(str(p1), cv2.warpAffine(g, M, (w, h), borderMode=cv2.BORDER_REPLICATE))
    b = vision.run_battery(p0, p1)
    assert "rot-15" in b["ssim_by_variant"] and "rot20" not in b["ssim_by_variant"]
    assert b["best"]["orb_inliers"] >= 12, b["best"]  # geometric verification holds


def test_equalized_channel_defeats_brightness_shift(tmp_path):
    g = _shapes()
    p0, p1 = tmp_path / "a.png", tmp_path / "b.png"
    Image.fromarray(g).save(p0)
    Image.fromarray(np.clip(g.astype(int) - 90, 0, 255).astype(np.uint8)).save(p1)
    b = vision.run_battery(p0, p1)
    assert b["equalized"]["ssim"] > b["ssim_by_variant"]["identity"], b["equalized"]
    f = scoring.fuse_battery(b, {"sha256": "a", "width": 400, "height": 300, "id": 1},
                             {"sha256": "b", "width": 400, "height": 300, "id": 2})
    assert "ssim_equalized" in f["signals"]
