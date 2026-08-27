from pydantic import BaseModel, ConfigDict, field_validator


class _CorrelatedCallbackResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    correlation_id: str

    @field_validator("correlation_id")
    @classmethod
    def _correlation_id_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("correlation ID must not be blank")
        return value
