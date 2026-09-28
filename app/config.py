"""Runtime configuration, read once from environment variables (and a local .env file if present)."""
import os
from dataclasses import dataclass

try:  # python-dotenv is optional; on Render the env vars are set in the dashboard
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    database_url: str
    port: int
    ai_process_url: str
    ai_narrate_url: str
    ai_auth_header: str | None
    ai_timeout_seconds: float
    admin_key: str | None
    recompute_debounce_seconds: float
    recommendation_max_age_hours: float
    ai_sample_size: int
    max_audio_base64_chars: int
    cors_origins: list[str]
    db_pool_min: int
    db_pool_max: int


def load_settings() -> Settings:
    port = _int("PORT", 8000)
    # By default the backend talks to its OWN mock AI endpoints, so switching to
    # Person 1's real n8n webhooks later is an env-var change only.
    self_base = f"http://127.0.0.1:{port}"
    return Settings(
        database_url=os.environ.get("DATABASE_URL", ""),
        port=port,
        ai_process_url=os.environ.get("AI_PROCESS_URL") or f"{self_base}/mock/ai/process-complaint",
        ai_narrate_url=os.environ.get("AI_NARRATE_URL") or f"{self_base}/mock/ai/narrate",
        ai_auth_header=os.environ.get("AI_AUTH_HEADER") or None,
        ai_timeout_seconds=_float("AI_TIMEOUT_SECONDS", 10.0),
        admin_key=os.environ.get("ADMIN_KEY") or None,
        recompute_debounce_seconds=_float("RECOMPUTE_DEBOUNCE_SECONDS", 5.0),
        recommendation_max_age_hours=_float("RECOMMENDATION_MAX_AGE_HOURS", 24.0),
        ai_sample_size=_int("AI_SAMPLE_SIZE", 30),
        # ~10 MB of audio once decoded (base64 is 4 chars per 3 bytes)
        max_audio_base64_chars=_int("MAX_AUDIO_BASE64_CHARS", 14_000_000),
        cors_origins=[o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()],
        db_pool_min=_int("DB_POOL_MIN", 1),
        db_pool_max=_int("DB_POOL_MAX", 5),
    )


settings = load_settings()
