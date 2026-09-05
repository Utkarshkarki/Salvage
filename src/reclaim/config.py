"""Environment-driven settings for Reclaim.

All values are read from environment variables and/or a local ``.env`` file.
No hardcoded secrets are allowed in source; the webhook secret and Razorpay
keys must come from configuration.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Zero-halo configuration: required secrets fail loudly at load time."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- LLM backend (OpenAI-compatible client -> local Ollama) ---
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:32b-instruct"
    ollama_timeout_seconds: float = 30.0
    llm_mode: Literal["offline", "online"] = "offline"

    # --- Razorpay ---
    razorpay_webhook_secret: str = Field(default="")
    razorpay_key_id: str | None = None
    razorpay_key_secret: str | None = None
    razorpay_base_url: str = "https://api.razorpay.com/v1"
    # Empty by default: the exact routes must be confirmed against current
    # Razorpay docs, so we never guess a wire format (see razorpay_client.py).
    # Substitutions: {subscription_id}, {settlement_id}.
    razorpay_retry_path: str = ""
    razorpay_subscription_path: str = ""
    razorpay_settlement_path: str = ""
    # Live-failure generator target (Part 6.1): the Payments API Payment Link
    # route, used by `python -m reclaim.live create` to produce a real test-mode
    # checkout that a human completes with an error-simulation card. Empty by
    # default (ZERO-HALO — refuse rather than guess); set to the documented path.
    razorpay_payment_link_path: str = ""
    # Where RAW webhook payloads that pass signature verification are written
    # verbatim, as captured fixtures for traceable replay:
    #   {dir}/{event_type}/{event_id}.json
    # Empty (the default) = capture DISABLED — the hermetic/demo default so tests
    # and demos never write to disk. When set (e.g. RAZORPAY_WEBHOOK_CAPTURE_DIR=
    # fixtures/captured), every signature-passing external delivery is recorded.
    razorpay_webhook_capture_dir: str = ""
    # Best-effort (verification-only, non-blocking) external lookups. When
    # False the verify helpers record nothing and never touch the network —
    # the hermetic/demo default keeps the stub path side-effect silent.
    verification_enabled: bool = True
    act_mode: Literal["stub", "live"] = "stub"

    # --- Celery / Redis broker ---
    # NOTE: never a real credential here — set REDIS_URL in .env. The default
    # is a local-dev placeholder; Upstash servers are configured per-env.
    redis_url: str = "redis://localhost:6379"
    reclaim_celery_eager: bool = True

    # --- Database ---
    database_url: str = "sqlite:///reclaim.db"

    # --- Stopping-rule thresholds (code-enforced, override the LLM) ---
    escalation_amount_threshold: float = 5000.0
    escalation_days_threshold: int = 7
    max_retries_per_cycle: int = 3
    cooldown_hours: float = 24.0
    email_cap_per_7d: int = 1
    # Economic floor: cases below this amount are never auto-retried — the
    # retry cost/risk outweighs the recovery value for trivially small amounts.
    # (Stopping rule R7; env-configurable as MIN_RECOVERY_AMOUNT.)
    min_recovery_amount: float = 100.0

    # --- Stale-lock reconciliation ---
    # A case stuck in ACTING longer than this (seconds) is considered aborted
    # mid-pipeline and is swept to ESCALATED by the reconciliation task.
    stale_lock_timeout_seconds: float = 300.0

    # --- Frontend CORS ---
    # Comma-separated origins allowed by the API for the React SPA. The default
    # is the Vite dev server ONLY. This is a LOCAL-DEMO default: for any real
    # deployment narrow it to the deployed frontend origin(s), ideally serving
    # the SPA and API from the same origin so CORS is unnecessary entirely.
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # --- Concurrency throttle ---
    max_concurrency: int = 5
    llm_backoff_base_seconds: float = 1.0
    llm_backoff_max_seconds: float = 15.0

    @field_validator("razorpay_webhook_secret")
    @classmethod
    def _webhook_secret_not_empty(cls, v: str) -> str:
        # ZERO-HALO: never fall through with an empty signing secret.
        if not v:
            raise ValueError(
                "RAZORPAY_WEBHOOK_SECRET is required. Generate one with "
                "`python -c \"import secrets; print(secrets.token_hex(32))\"` and set it in .env"
            )
        return v

    @field_validator("razorpay_key_id", "razorpay_key_secret")
    @classmethod
    def _strip_optional(cls, v: str | None) -> str | None:
        if v in (None, ""):
            return None
        return v

    @property
    def redis_tls(self) -> bool:
        """True when the broker URL uses the TLS scheme (Upstash requires this)."""
        return self.redis_url.startswith("rediss://")

    def require_live_credentials(self) -> None:
        """Loudly refuse to run the live Act path without Razorpay test keys."""
        if not self.razorpay_key_id or not self.razorpay_key_secret:
            raise RuntimeError(
                "ACT_MODE=live requires RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET "
                "(test-mode credentials). Refusing to make unsigned Razorpay calls."
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Module-level settings singleton (secrets loaded once)."""
    return Settings()


def clear_settings_cache() -> None:
    """Drop the cached singleton so tests can reload with different env values."""
    get_settings.cache_clear()