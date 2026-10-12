"""Central configuration for the autonomous image-investigation pipeline.

All budgets, rate limits, and thresholds are explicit, bounded, and snapshotted
into each investigation at creation time for reproducibility.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _llm_keys() -> tuple:
    raw = (os.getenv("NVAPI_KEYS", "") + "," + os.getenv("NVAPI_KEY", "")).split(",")
    seen: list[str] = []
    for k in raw:
        k = k.strip().strip('"').strip("'")
        if k and k not in seen:
            seen.append(k)
    return tuple(seen)


@dataclass(frozen=True)
class Settings:
    # --- paths -----------------------------------------------------------------
    root: Path = ROOT
    data_dir: Path = ROOT / "data"
    db_path: Path = ROOT / "data" / "investigations.db"
    pdfs_dir: Path = ROOT / "data" / "pdfs"
    figures_dir: Path = ROOT / "data" / "figures"
    reports_dir: Path = ROOT / "data" / "reports"
    static_dir: Path = ROOT / "static"

    # --- identity / politeness -------------------------------------------------
    contact_email: str = os.getenv("CONTACT_EMAIL", "")
    user_agent: str = "sfip/0.1 (autonomous scientific-image forensics research tool; polite crawler)"

    # --- global budgets (per investigation) ------------------------------------
    max_papers: int = _env_int("SFIP_MAX_PAPERS", 150)
    max_figures: int = _env_int("SFIP_MAX_FIGURES", 3000)
    max_comparisons: int = _env_int("SFIP_MAX_COMPARISONS", 200000)
    max_llm_calls: int = _env_int("SFIP_MAX_LLM_CALLS", 500)
    max_runtime_minutes: int = _env_int("SFIP_MAX_RUNTIME_MINUTES", 720)
    max_download_mb: int = _env_int("SFIP_MAX_DOWNLOAD_MB", 5000)

    # --- job execution ---------------------------------------------------------
    max_job_attempts: int = 3
    job_backoff_base: float = 2.0          # seconds^attempts backoff
    tick_seconds: float = 1.0
    stability_ticks_to_finish: int = 3     # empty queue for N ticks -> finalize
    plan_llm_every_n_jobs: int = 25        # advisory LLM plan cadence (bounded)

    # --- rate limits (requests / second, per source) ---------------------------
    rps_openalex: float = _env_float("SFIP_RPS_OPENALEX", 8.0)
    rps_crossref: float = _env_float("SFIP_RPS_CROSSREF", 8.0)
    rps_europepmc: float = _env_float("SFIP_RPS_EUROPEPMC", 4.0)
    rps_pdf: float = _env_float("SFIP_RPS_PDF", 4.0)
    http_retries: int = 3
    http_timeout: float = 45.0

    # --- vision thresholds -----------------------------------------------------
    min_figure_side: int = 48              # ignore icons/legends smaller than this
    min_figure_area: int = 4000
    gate_hamming: int = 22                 # 64-bit perceptual hash gate
    gate_ssim: float = 0.70                # SSIM gate (after variant search)
    gate_orb_inliers: int = 12             # geometric-verification gate
    verdict_high: float = 0.82
    verdict_moderate: float = 0.62
    verdict_weak: float = 0.42
    eig_threshold_band: float = 0.10       # scores within +/- band of a cutoff -> high info gain

    # --- LLM (NVIDIA NIM, multimodal) ------------------------------------------
    llm_base_url: str = os.getenv("SFIP_LLM_BASE_URL", "https://integrate.api.nvidia.com/v1")
    llm_model: str = os.getenv("SFIP_LLM_MODEL", "meta/muse-glimmer-30b")
    llm_api_keys: tuple = _llm_keys()

    @property
    def llm_api_key(self) -> str:
        return self.llm_api_keys[0] if self.llm_api_keys else ""
    llm_max_tokens: int = 1400
    llm_temperature: float = 0.15
    llm_timeout: float = 120.0
    llm_vision_max_side: int = 768         # downscale before sending images to the LLM
    llm_vision_max_bytes: int = 900_000    # base64 payload cap per image

    # --- dashboard -------------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = _env_int("SFIP_PORT", 8000)
    poll_seconds: float = 2.5

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.pdfs_dir, self.figures_dir, self.reports_dir):
            p.mkdir(parents=True, exist_ok=True)

    def snapshot(self) -> dict:
        d = asdict(self)
        for k in ("root", "data_dir", "db_path", "pdfs_dir", "figures_dir", "reports_dir", "static_dir"):
            d[k] = str(d[k])
        d.pop("llm_api_key", None)          # never persist secrets
        d.pop("llm_api_keys", None)
        d["llm_api_key_set"] = bool(self.llm_api_keys)
        d["llm_api_key_count"] = len(self.llm_api_keys)
        return d


settings = Settings()
settings.ensure_dirs()
