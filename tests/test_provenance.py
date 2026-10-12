"""Tests for AI-provenance screening: metadata, diffusion screen, SynthID hook."""
import numpy as np
import pytest
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from app import provenance


def _clean_png(path):
    Image.new("RGB", (200, 150), (120, 140, 160)).save(path)
    return path


def test_clean_image_has_no_ai_markers(tmp_path):
    p = _clean_png(tmp_path / "clean.png")
    m = provenance.metadata_screen(p)
    assert m["verdict"] in ("stripped_or_clean", "clean", "c2pa_present")
    assert m["ai_markers"] == []
    assert m["c2pa_manifest_present"] is False


def test_stable_diffusion_chunk_flagged(tmp_path):
    p = tmp_path / "sd.png"
    info = PngInfo()
    info.add_text("parameters", "a microscopy image\nSteps: 30, Sampler: Euler, Stable Diffusion v1.5")
    Image.new("RGB", (200, 150), "white").save(p, pnginfo=info)
    m = provenance.metadata_screen(p)
    assert m["verdict"] == "ai_tool_marked", m
    assert any("stable diffusion" in hit[0] for hit in m["ai_markers"])


def test_photoshop_exif_is_editor_not_ai(tmp_path):
    p = tmp_path / "ed.jpg"
    ex = Image.Exif()
    ex[305] = "Adobe Photoshop 25.0"
    Image.new("RGB", (200, 150), "white").save(p, exif=ex)
    m = provenance.metadata_screen(p)
    assert m["verdict"] == "edited_with_software", m
    assert m["ai_markers"] == []
    assert any("photoshop" in hit[0] for hit in m["editor_markers"])


def test_diffusion_screen_direction(tmp_path):
    # periodic grid -> strong spectral peaks; smooth gradient -> weak
    x = np.tile((np.arange(200) // 10 % 2) * 255, (150, 1)).astype(np.uint8)
    checker = np.zeros((150, 200), np.uint8)
    checker[::8, :] = 255
    checker[:, ::8] = 255
    grad = np.tile(np.linspace(0, 255, 200), (150, 1)).astype(np.uint8)
    a_grid = provenance.diffusion_screen(checker)["anomaly"]
    a_grad = provenance.diffusion_screen(grad)["anomaly"]
    assert 0.0 <= a_grad <= 1.0 and 0.0 <= a_grid <= 1.0
    assert a_grid > a_grad, (a_grid, a_grad)


def test_synthid_hook_honest_when_unconfigured(tmp_path, monkeypatch):
    monkeypatch.delenv("SFIP_SYNTHID_ENDPOINT", raising=False)
    p = _clean_png(tmp_path / "clean.png")
    s = provenance.synthid_check(p)
    assert s["status"] == "not_configured"
    assert s["watermarked"] is None  # never invented


def test_run_provenance_combines_layers(tmp_path):
    p = tmp_path / "sd.png"
    info = PngInfo()
    info.add_text("parameters", "Steps: 20, Model: Stable Diffusion XL")
    Image.new("RGB", (200, 150), "white").save(p, pnginfo=info)
    r = provenance.run_provenance(p)
    assert r["suspicion"] == "high"
    assert "caveats" in r and len(r["caveats"]) >= 2
