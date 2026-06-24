"""Application configuration loaded from environment variables."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CALLS_DIR = PROJECT_ROOT / "calls"
RECORDINGS_DIR = CALLS_DIR / "recordings"
TRANSCRIPTS_DIR = CALLS_DIR / "transcripts"
METADATA_DIR = CALLS_DIR / "metadata"

ALLOWED_TARGET = "+18054398008"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    twilio_account_sid: str = Field(alias="TWILIO_ACCOUNT_SID")
    twilio_auth_token: str = Field(alias="TWILIO_AUTH_TOKEN")
    phone_number_from: str = Field(alias="PHONE_NUMBER_FROM")
    openai_api_key: str = Field(alias="OPENAI_API_KEY")
    domain: str = Field(alias="DOMAIN")
    port: int = Field(default=6060, alias="PORT")

    openai_realtime_model: str = Field(
        default="gpt-realtime",
        alias="OPENAI_REALTIME_MODEL",
    )
    openai_voice: str = Field(default="shimmer", alias="OPENAI_VOICE")
    openai_temperature: float = Field(default=0.7, alias="OPENAI_TEMPERATURE")
    openai_ws_open_timeout: float = Field(default=60.0, alias="OPENAI_WS_OPEN_TIMEOUT")
    openai_ws_max_retries: int = Field(default=3, alias="OPENAI_WS_MAX_RETRIES")

    target_phone_number: str = Field(
        default=ALLOWED_TARGET,
        alias="TARGET_PHONE_NUMBER",
    )
    max_call_duration_seconds: int = Field(
        default=180,
        alias="MAX_CALL_DURATION_SECONDS",
    )
    call_cooldown_seconds: int = Field(default=30, alias="CALL_COOLDOWN_SECONDS")

    @field_validator("domain")
    @classmethod
    def strip_domain(cls, value: str) -> str:
        return re.sub(r"(^\w+:|^)\/\/|\/+$", "", value.strip())

    @field_validator("target_phone_number")
    @classmethod
    def enforce_target_number(cls, value: str) -> str:
        normalized = re.sub(r"[^\d+]", "", value)
        if not normalized.startswith("+"):
            normalized = f"+{normalized}"
        if normalized != ALLOWED_TARGET:
            raise ValueError(
                f"Calls are restricted to the assessment line {ALLOWED_TARGET} only."
            )
        return normalized

    @property
    def media_stream_url(self) -> str:
        return f"wss://{self.domain}/media-stream"

    @property
    def recording_callback_url(self) -> str:
        return f"https://{self.domain}/recording-callback"

    @property
    def status_callback_url(self) -> str:
        return f"https://{self.domain}/call-status"


def get_settings() -> Settings:
    return Settings()
