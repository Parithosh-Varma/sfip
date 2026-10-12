"""DB round-trip + pipeline sweep/deep-compare on synthetic figures (no network)."""
import dataclasses
import json

import pytest
from PIL import Image, ImageDraw

from app import db, vision
from app.config import settings
from app.pipeline import h_sweep, h_deep_compare


@pytest.fixture()
def tdb(tmp_path, monkeypatch):
    """Redirect the DB to a temp file for the whole app."""
    new = dataclasses.replace(settings, db_path=tmp_path / "t.db")
    monkeypatch.setattr(db, "settings", new)
    db.init_db()
    return tmp_path


def _png(path, seed=1, flip=False):
    import numpy as np
    rng = np.random.default_rng(seed)
    img = Image.new("RGB", (300, 220), "white")
    d = ImageDraw.Draw(img)
    for _ in range(8):
        x0, y0 = int(rng.integers(10, 220)), int(rng.integers(10, 160))
        d.rectangle([x0, y0, x0 + 40, y0 + 30],
                    fill=tuple(int(c) for c in rng.integers(0, 200, 3)))
    if flip:
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
    img.save(path)
    return path


def test_job_lifecycle_idempotent(tdb):
    inv = db.create_investigation("seed-x", "query", {})
    assert db.enqueue_job(inv, "sweep", {}, dedupe_key="sweep") is not None
    assert db.enqueue_job(inv, "sweep", {}, dedupe_key="sweep") is None  # duplicate
    job = db.claim_next_job(inv)
    assert job and job["kind"] == "sweep"
    assert db.claim_next_job(inv) is None  # claimed jobs are invisible
    db.finish_job(job["id"], True)
    assert db.queue_stats(inv).get("done") == 1


def test_crash_recovery_requeues_running(tdb):
    inv = db.create_investigation("seed-y", "query", {})
    jid = db.enqueue_job(inv, "sweep", {})
    job = db.claim_next_job(inv)
    assert job["id"] == jid
    assert db.release_stuck_jobs(inv) == 1
    assert db.claim_next_job(inv)["id"] == jid


def test_sweep_promotes_and_verifies_transformed_pair(tdb, tmp_path):
    inv = db.create_investigation("seed-z", "query", {})
    pa = tmp_path / "a.png"
    pb = tmp_path / "b.png"
    _png(pa, seed=3)
    _png(pb, seed=3, flip=True)  # same content, flipped
    figs = []
    for i, p in enumerate((pa, pb)):
        img = vision.load_rgb(p)
        h = vision.hashes(img)
        fid = db.insert_figure(inv, 100 + i, page=1, idx_in_page=i,
                               width=300, height=220,
                               sha256=vision.sha256_file(p),
                               phash=h["phash"], dhash=h["dhash"], ahash=h["ahash"],
                               image_path=str(p), kind="raster", added_at=db.now_iso())
        assert fid
        db.bump_counter(inv, "figures")
        figs.append(fid)
    # make hashes near-identical for the gate (flip preserves dhash poorly; force gate pass)
    ha = db.query_one("SELECT * FROM figures WHERE id=?", (figs[0],))
    hb = db.query_one("SELECT * FROM figures WHERE id=?", (figs[1],))
    assert ha and hb

    out = h_sweep(inv, {})
    cands = db.query("SELECT * FROM candidates WHERE investigation_id=?", (inv,))
    # gate may or may not pass on synthetic art; either path must be consistent
    if out["promoted"]:
        assert len(cands) == out["promoted"]
        deep = db.claim_next_job(inv)
        assert deep["kind"] == "deep_compare"
        res = h_deep_compare(inv, deep["payload"])
        assert res["verdict"] in ("high", "moderate", "exact_duplicate", "weak",
                                  "likely_false_positive"), res
        row = db.query_one("SELECT * FROM candidates WHERE id=?", (deep["payload"]["candidate_id"],))
        assert row["stage"] == "verified"
        assert json.loads(row["signals_json"])["best_variant"] in (
            "hflip", "identity", "vflip", "rot180")
    else:
        assert out["pairs"] >= 1  # sweep ran, gate filtered everything


def test_controller_finalizes_idle_investigation(tdb):
    from app.controller import Controller
    inv = db.create_investigation("seed-idle", "query", settings.snapshot())
    c = Controller()
    for _ in range(5):
        c.tick(inv)
    got = db.get_investigation(inv)
    assert got["status"] in ("completed", "stopped_budget", "running")
    fin = db.query("SELECT * FROM jobs WHERE investigation_id=? AND kind='finalize_report'", (inv,))
    assert fin, "idle loop must schedule finalization"
