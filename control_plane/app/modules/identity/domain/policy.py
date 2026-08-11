from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EffectiveIdentityPolicy(BaseModel):
    """Typed effective identity policy consumed by authentication behavior."""

    model_config = ConfigDict(frozen=True)

    temp_credential_ttl: timedelta = Field(default=timedelta(hours=24), gt=timedelta(0))
    password_max_age: timedelta | None = Field(default=None, gt=timedelta(0))
    session_cap: int = Field(default=3, ge=1, le=10)
    session_idle_timeout: timedelta = Field(
        default=timedelta(minutes=60),
        ge=timedelta(minutes=15),
        le=timedelta(minutes=240),
    )
    backoff_threshold: int = Field(default=5, ge=1)
    backoff_initial_delay: timedelta = Field(default=timedelta(seconds=30), gt=timedelta(0))
    backoff_max_delay: timedelta = Field(default=timedelta(minutes=15), gt=timedelta(0))
    backoff_reset_after: timedelta = Field(default=timedelta(hours=24), gt=timedelta(0))
    totp_attempt_cap: int = Field(default=5, ge=1)
    draft_archive_after: timedelta = Field(default=timedelta(days=30), gt=timedelta(0))

    @model_validator(mode="after")
    def validate_backoff_window(self) -> "EffectiveIdentityPolicy":
        if self.backoff_max_delay < self.backoff_initial_delay:
            raise ValueError("backoff maximum must not be below its initial delay")
        return self
