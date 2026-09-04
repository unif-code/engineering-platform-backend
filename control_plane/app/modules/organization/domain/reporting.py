from pydantic import BaseModel, ConfigDict


class ReportingParticipant(BaseModel):
    model_config = ConfigDict(frozen=True)

    account_id: str
    superior_id: str | None
    kind: str
    employee_no: str
    display_name: str
    status: str
    initialized: bool


class ReportingContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    account_id: str
    kind: str
    reviewer_id: str
    participants: tuple[ReportingParticipant, ...]
    facts_hash: str
