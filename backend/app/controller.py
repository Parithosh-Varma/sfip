"""Persistent autonomous controller.

Loop: Observe → Plan → Execute → Verify → Score → Update state →
Select next action → Repeat.

- Measurable success criteria, bounded retries, explicit budgets, and
  explicit stopping conditions live here.
- Crash-safe: all state is in SQLite; interrupted jobs are requeued on start.
- The LLM may advise on focus, but deterministic EIG/budget logic is
  authoritative — the loop never blocks on a model or a human.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import db, llm
from .config import settings
from .pipeline import HANDLERS

GROWTH_JOBS = ("expand_paper", "fetch_pdf", "sweep")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _trim(x, limit: int = 1500):
    s = json.dumps(x, default=str)
    if len(s) > limit:
        return {"_truncated": s[:limit]}
    return x


class Controller(threading.Thread):
    daemon = True

    def __init__(self) -> None:
        super().__init__(name="sfip-controller")
        self._stop_evt = threading.Event()

    def stop(self) -> None:
        self._stop_evt.set()

    # ------------------------------------------------------------------ #
    def run(self) -> None:
        recovered = db.release_stuck_jobs()
        if recovered:
            db.add_event(None, "warn", "controller",
                         f"resume: requeued {recovered} interrupted job(s)", {})
        while not self._stop_evt.is_set():
            try:
                running = [i for i in db.list_investigations() if i["status"] == "running"]
                for inv in running:
                    if self._stop_evt.is_set():
                        break
                    self.tick(inv["id"])
            except Exception as e:                     # controller must never die
                db.add_event(None, "error", "controller", f"loop error: {e}", {})
            self._stop_evt.wait(settings.tick_seconds)

    # ------------------------------------------------------------------ #
    def tick(self, inv_id: int) -> None:
        inv = db.get_investigation(inv_id)
        if not inv or inv["status"] != "running":
            return

        # stopping condition: wall-clock runtime
        elapsed_min = (_now() - _parse(inv["started_at"])).total_seconds() / 60
        if elapsed_min > settings.max_runtime_minutes:
            db.add_event(inv_id, "warn", "controller",
                         f"stopping: runtime budget exceeded ({elapsed_min:.0f} min)", {})
            self._finalize(inv_id, "stopped_runtime")
            self._observe(inv_id, phase="stopped_runtime")
            return

        # observe → plan → execute
        job = db.claim_next_job(inv_id)
        if job:
            self._execute(inv_id, job)
            self._observe(inv_id, phase=f"job:{job['kind']}")
            self._maybe_llm_plan(inv_id)
            return

        # idle: check stability + budget-aware completion
        st = inv.get("state") or {}
        idle = int(st.get("idle_ticks", 0)) + 1
        st["idle_ticks"] = idle
        if idle >= settings.stability_ticks_to_finish:
            self._finalize(inv_id, self._final_status(inv_id))
            self._observe(inv_id, phase="finalized", state=st)
            return
        db.set_inv_state(inv_id, {**st, "phase": "idle"})
        self._observe(inv_id, phase="idle", state=st)

    # ------------------------------------------------------------------ #
    def _execute(self, inv_id: int, job: dict) -> None:
        handler = HANDLERS.get(job["kind"])
        t0 = time.time()
        try:
            if handler is None:
                raise KeyError(f"unknown job kind {job['kind']!r}")
            result = handler(inv_id, job["payload"])
            if not isinstance(result, dict):
                raise ValueError(f"handler returned non-dict: {type(result)}")
            db.finish_job(job["id"], True)
            db.bump_counter(inv_id, "jobs_done")
            db.add_event(inv_id, "info", f"job:{job['kind']}",
                         f"done in {time.time() - t0:.1f}s", {"result": _trim(result)})
        except Exception as e:
            attempts = int(job.get("attempts") or 1)
            if attempts < int(job.get("max_attempts") or settings.max_job_attempts):
                delay = settings.job_backoff_base ** attempts
                run_after = (_now() + timedelta(seconds=delay)).isoformat()
                db.requeue_job(job["id"], run_after)
                db.add_event(inv_id, "warn", f"job:{job['kind']}",
                             f"attempt {attempts} failed ({str(e)[:200]}); retry in {delay:.0f}s", {})
            else:
                db.finish_job(job["id"], False, str(e)[:500])
                db.add_event(inv_id, "error", f"job:{job['kind']}",
                             f"failed after {attempts} attempts: {str(e)[:300]}", {})

    def _maybe_llm_plan(self, inv_id: int) -> None:
        """Advisory LLM planning at a bounded cadence; deterministic flow continues regardless."""
        done = int(db.get_counter(inv_id, "jobs_done"))
        if done == 0 or done % settings.plan_llm_every_n_jobs != 0:
            return
        if not llm.llm_available() or db.get_counter(inv_id, "llm_calls") >= settings.max_llm_calls:
            return
        db.bump_counter(inv_id, "llm_calls")
        summary = self._state_summary(inv_id)
        plan = llm.plan_focus(summary)
        db.add_event(inv_id, "decision", "llm:plan",
                     f"advisory focus: {plan.get('focus', 'n/a')}", {"plan": _trim(plan)})
        inv = db.get_investigation(inv_id)
        st = inv.get("state") or {}
        st["llm_plan"] = plan
        db.set_inv_state(inv_id, st)

    # ------------------------------------------------------------------ #
    def _state_summary(self, inv_id: int) -> dict:
        inv = db.get_investigation(inv_id)
        top = db.query(
            "SELECT id, figure_a, figure_b, verdict, score, uncertainty, flags_json "
            "FROM candidates WHERE investigation_id=? ORDER BY score DESC LIMIT 12", (inv_id,))
        return {
            "seed": inv["seed"], "status": inv["status"],
            "counters": db.all_counters(inv_id),
            "queue": db.queue_stats(inv_id),
            "budgets": self._budgets(inv_id),
            "top_candidates": top,
        }

    def _budgets(self, inv_id: int) -> dict:
        c = db.all_counters(inv_id)
        def used(name): return c.get(name, 0)
        return {
            "papers": [int(used("papers")), settings.max_papers],
            "figures": [int(used("figures")), settings.max_figures],
            "comparisons": [int(used("comparisons")), settings.max_comparisons],
            "llm_calls": [int(used("llm_calls")), settings.max_llm_calls],
            "download_mb": [round(used("bytes_downloaded") / 1048576, 1), settings.max_download_mb],
            "runtime_min": [round((_now() - _parse(db.get_investigation(inv_id)["started_at"])).total_seconds() / 60, 1),
                            settings.max_runtime_minutes],
        }

    def _final_status(self, inv_id: int) -> str:
        b = self._budgets(inv_id)
        exhausted = any(u >= m for u, m in b.values())
        return "stopped_budget" if exhausted else "completed"

    def _finalize(self, inv_id: int, status: str) -> None:
        db.enqueue_job(inv_id, "finalize_report", {"final_status": status},
                       priority=10, dedupe_key=f"finalize:{status}")

    def _observe(self, inv_id: int, phase: str, state: Optional[dict] = None) -> None:
        st = state if state is not None else (db.get_investigation(inv_id).get("state") or {})
        last = db.query_one("SELECT id, message, ts FROM events WHERE investigation_id=? ORDER BY id DESC LIMIT 1",
                            (inv_id,))
        st.update({
            "phase": phase,
            "queue": db.queue_stats(inv_id),
            "counters": db.all_counters(inv_id),
            "budgets": self._budgets(inv_id),
            "last_event_id": last["id"] if last else 0,
            "updated_at": db.now_iso(),
        })
        db.set_inv_state(inv_id, st)
