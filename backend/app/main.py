"""CLI entry point.

  python backend/app/main.py investigate "10.1234/example"   # start a run + dashboard
  python backend/app/main.py serve                           # resume all runs + dashboard
  python backend/app.main.py status                          # print current state (see below)
  python backend/app/main.py selftest                        # synthetic end-to-end vision check

Note: run as `python backend/app/main.py ...` from anywhere.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db, llm, sources  # noqa: E402
from app.config import settings  # noqa: E402


def cmd_investigate(args: argparse.ArgumentParser) -> None:
    seed = args.seed
    stype = sources.classify_seed(seed)
    print(f"[sfip] seed {seed!r} classified as: {stype}")
    db.init_db()
    db.release_stuck_jobs()
    inv_id = db.create_investigation(seed, stype, settings.snapshot())
    db.enqueue_job(inv_id, "discover_seed", {}, priority=9, dedupe_key="discover")
    print(f"[sfip] investigation #{inv_id} created; autonomous loop starting.")
    _serve(args)


def cmd_serve(args: argparse.ArgumentParser) -> None:
    db.init_db()
    n = db.release_stuck_jobs()
    running = [i for i in db.list_investigations() if i["status"] == "running"]
    print(f"[sfip] resuming: {len(running)} running investigation(s), {n} stuck job(s) requeued.")
    _serve(args)


def _serve(args: argparse.ArgumentParser) -> None:
    import uvicorn
    host, port = settings.host, args.port or settings.port
    llm_state = "available" if llm.llm_available() else "UNAVAILABLE (heuristic fallback)"
    print(f"[sfip] dashboard: http://{host}:{port}   LLM: {llm_state} ({settings.llm_model})")
    print("[sfip] dashboard is read-only; the loop runs autonomously. Ctrl-C to stop.")
    uvicorn.run("app.api:app", host=host, port=port, log_level="warning")


def cmd_status(_args: argparse.ArgumentParser) -> None:
    db.init_db()
    for inv in db.list_investigations():
        c = db.all_counters(inv["id"])
        print(f"#{inv['id']} [{inv['status']}] seed={inv['seed']!r} "
              f"papers={int(c.get('papers', 0))} figures={int(c.get('figures', 0))} "
              f"comparisons={int(c.get('comparisons', 0))} "
              f"candidates={db.query_one('SELECT COUNT(*) AS n FROM candidates WHERE investigation_id=?', (inv['id'],))['n']}")


def cmd_selftest(_args: argparse.ArgumentParser) -> None:
    """Synthetic end-to-end check of the forensic core (no network)."""
    import numpy as np
    from PIL import Image, ImageDraw
    from app import vision, scoring

    rng = np.random.default_rng(7)
    base = Image.new("RGB", (640, 480), "white")
    d = ImageDraw.Draw(base)
    for i in range(12):
        x0, y0 = int(rng.integers(20, 500)), int(rng.integers(20, 380))
        d.ellipse([x0, y0, x0 + int(rng.integers(30, 120)), y0 + int(rng.integers(20, 80))],
                  fill=tuple(int(c) for c in rng.integers(0, 200, 3)))
    d.text((30, 10), "Figure 1. Synthetic test panel", fill="black")

    td = settings.data_dir / "selftest"
    td.mkdir(parents=True, exist_ok=True)
    p0, p1, p2 = td / "orig.png", td / "flipped_cropped.png", td / "unrelated.png"
    base.save(p0)
    base.transpose(Image.FLIP_LEFT_RIGHT).crop((60, 40, 600, 440)).save(p1)
    Image.new("RGB", (640, 480), (240, 240, 255)).save(p2)

    b = vision.run_battery(p0, p1)
    f = scoring.fuse_battery(b, {"sha256": "x", "width": 640, "height": 480, "id": 1},
                             {"sha256": "y", "width": 540, "height": 400, "id": 2})
    print("[selftest] transformed pair:", f["verdict"], f["score"], f["signals"])

    b2 = vision.run_battery(p0, p2)
    f2 = scoring.fuse_battery(b2, {"sha256": "x", "width": 640, "height": 480, "id": 1},
                              {"sha256": "z", "width": 640, "height": 480, "id": 3})
    print("[selftest] unrelated pair:", f2["verdict"], f2["score"], f2["signals"])

    ok = f["verdict"] in ("high", "exact_duplicate", "moderate") and f2["verdict"] in (
        "likely_false_positive", "weak")
    print("[selftest]", "PASS ✓" if ok else "FAIL ✗")
    sys.exit(0 if ok else 1)


def main() -> None:
    ap = argparse.ArgumentParser(prog="sfip", description="Autonomous scientific-image investigation pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_inv = sub.add_parser("investigate", help="start a new autonomous investigation from a seed")
    p_inv.add_argument("seed", help="DOI, paper URL, researcher name, or query")
    p_inv.add_argument("--port", type=int, default=None)
    p_inv.set_defaults(func=cmd_investigate)

    p_serve = sub.add_parser("serve", help="resume pending work and serve the dashboard")
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.set_defaults(func=cmd_serve)

    p_status = sub.add_parser("status", help="print investigation status")
    p_status.set_defaults(func=cmd_status)

    p_self = sub.add_parser("selftest", help="offline synthetic verification of the forensic core")
    p_self.set_defaults(func=cmd_selftest)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
