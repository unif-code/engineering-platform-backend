from pathlib import Path

from pydantic import Field, HttpUrl, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SourceControlDevSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="SOURCE_CONTROL_",
        extra="ignore",
    )

    gitlab_api_url: HttpUrl
    connection_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    request_timeout_seconds: float = Field(gt=0, le=30)
    policy_version: int = Field(ge=1)
    reconcile_base_delay_seconds: int = Field(ge=1, le=300)
    reconcile_max_delay_seconds: int = Field(ge=1, le=3600)
    webhook_replay_window_seconds: int = Field(ge=30, le=900)
    secret_reference_root: Path

    @classmethod
    def from_environment(cls) -> "SourceControlDevSettings":
        # BaseSettings supplies required fields from SOURCE_CONTROL_* at runtime;
        # mypy only sees the generated model constructor and cannot model that source.
        return cls()  # type: ignore[call-arg]

    @field_validator("gitlab_api_url")
    @classmethod
    def reject_endpoint_credentials(cls, value: HttpUrl) -> HttpUrl:
        if value.username is not None or value.password is not None:
            raise ValueError("GitLab endpoint must not contain credentials")
        if value.query is not None or value.fragment is not None:
            raise ValueError("GitLab endpoint must not contain query or fragment")
        return value

    @model_validator(mode="after")
    def validate_reconcile_schedule(self) -> "SourceControlDevSettings":
        if self.reconcile_base_delay_seconds > self.reconcile_max_delay_seconds:
            raise ValueError("Reconcile base delay must not exceed its maximum")
        return self
