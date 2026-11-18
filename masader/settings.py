"""Runtime settings read from environment variables (12-factor style).

Every value has a development default so `pytest` and `make up` work from a clean checkout; production
values come from the .env file written at deploy time (architecture F.4). Secrets are never defaulted
to anything that would work in production: the development pepper is refused when ENV=prod.

The timer order of architecture H.2 is asserted at import time:
    call timeout < stage deadline < soft limit < hard limit < lease < visibility timeout
    soft limit + cancel wait < hard limit
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _i(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _s(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    env: str
    database_url: str
    redis_url: str
    key_pepper: bytes
    model_mode: str                 # "simulated" (default, no API key needed) or "live" (Anthropic API)
    anthropic_api_key: str | None
    embedder: str                   # "hashing" (default, offline) or "fastembed" (needs the model download)
    object_store: str               # "local" or "s3"
    object_dir: Path
    s3_bucket: str
    config_dir: Path
    prompts_dir: Path
    budget_period: str
    est_report_cost: float
    admission_max_active: int
    admission_retry_after_s: int
    db_pool_max: int
    db_pool_timeout_s: float
    statement_timeout_ms: int
    # gateway and pipeline timers (production values; tests scale them down through the environment)
    llm_timeout_s: float
    llm_min_call_s: float
    llm_backoff_s: float
    planner_timeout_s: float
    cb_threshold: int
    cb_open_s: float
    provider_retry_s: float
    stage_deadline_margin_s: float
    stage_soft_limit_s: float
    stage_hard_limit_s: float
    stage_lease_s: float
    visibility_timeout_s: float
    cancel_wait_s: float
    stage_retry_s: float
    reaper_s: float
    sim_latency_scale: float        # simulated model latency = stage budget (minutes) x this many seconds
    live_sources: bool              # connectors call the real source APIs (needs outbound network)


def load() -> Settings:
    s = Settings(
        env=_s("ENV", "dev"),
        database_url=_s("DATABASE_URL", "postgresql://masader:masader@localhost:5432/masader"),
        redis_url=_s("REDIS_URL", "redis://localhost:6379/0"),
        key_pepper=_s("KEY_PEPPER", "dev-pepper-not-for-production").encode(),
        model_mode=_s("MODEL_MODE", "simulated"),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        embedder=_s("EMBEDDER", "hashing"),
        object_store=_s("OBJECT_STORE", "local"),
        object_dir=Path(_s("OBJECT_DIR", str(ROOT / "var" / "objects"))),
        s3_bucket=_s("S3_BUCKET", "masader-dev"),
        config_dir=Path(_s("CONFIG_DIR", str(ROOT / "config"))),
        prompts_dir=Path(_s("PROMPTS_DIR", str(ROOT / "prompts"))),
        budget_period=_s("BUDGET_PERIOD_START", "2026-09-01"),
        est_report_cost=_f("EST_REPORT_COST", 0.45),
        admission_max_active=_i("ADMISSION_MAX_ACTIVE", 6),
        admission_retry_after_s=_i("ADMISSION_RETRY_AFTER_S", 60),
        db_pool_max=_i("DB_POOL_MAX", 5),
        db_pool_timeout_s=_f("DB_POOL_TIMEOUT_S", 10),
        statement_timeout_ms=_i("STATEMENT_TIMEOUT_MS", 60000),
        llm_timeout_s=_f("LLM_TIMEOUT_S", 110),
        llm_min_call_s=_f("LLM_MIN_CALL_S", 15),
        llm_backoff_s=_f("LLM_BACKOFF_S", 3),
        planner_timeout_s=_f("PLANNER_TIMEOUT_S", 8),
        cb_threshold=_i("CB_THRESHOLD", 5),
        cb_open_s=_f("CB_OPEN_S", 60),
        provider_retry_s=_f("PROVIDER_RETRY_S", 60),
        stage_deadline_margin_s=_f("STAGE_DEADLINE_MARGIN_S", 6),
        stage_soft_limit_s=_f("STAGE_SOFT_LIMIT_S", 240),
        stage_hard_limit_s=_f("STAGE_HARD_LIMIT_S", 270),
        stage_lease_s=_f("STAGE_LEASE_S", 300),
        visibility_timeout_s=_f("VISIBILITY_TIMEOUT_S", 360),
        cancel_wait_s=_f("CANCEL_WAIT_S", 15),
        stage_retry_s=_f("STAGE_RETRY_S", 3),
        reaper_s=_f("REAPER_S", 60),
        sim_latency_scale=_f("SIM_LATENCY_SCALE", 0.0),
        live_sources=_s("LIVE_SOURCES", "0") == "1",
    )
    check(s)
    return s


def check(s: Settings) -> None:
    """Refuse to start with an unsafe or inconsistent configuration."""
    deadline = s.stage_soft_limit_s - s.stage_deadline_margin_s
    if not (s.llm_timeout_s < deadline < s.stage_soft_limit_s < s.stage_hard_limit_s
            < s.stage_lease_s < s.visibility_timeout_s):
        raise ValueError("timer order violated: call timeout < stage deadline < soft < hard < lease < visibility")
    if not s.stage_soft_limit_s + s.cancel_wait_s < s.stage_hard_limit_s:
        raise ValueError("soft limit + cancel wait must end before the hard limit")
    if s.model_mode not in ("simulated", "live"):
        raise ValueError("MODEL_MODE must be 'simulated' or 'live'")
    if s.model_mode == "live" and not s.anthropic_api_key:
        raise ValueError("MODEL_MODE=live needs ANTHROPIC_API_KEY")
    if s.env == "prod" and s.key_pepper == b"dev-pepper-not-for-production":
        raise ValueError("set KEY_PEPPER in production")


settings = load()
