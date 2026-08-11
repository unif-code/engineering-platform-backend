from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class SessionKind(StrEnum):
    BOOTSTRAP = "BOOTSTRAP"
    FULL = "FULL"


class IssuedSession(BaseModel):
    model_config = ConfigDict(frozen=True)

    account_id: str
    raw_token: str
    kind: SessionKind


class LoginChallenge(BaseModel):
    model_config = ConfigDict(frozen=True)

    account_id: str
    challenge_token: str
    purpose: str = "LOGIN_TOTP"


class TotpEnrollment(BaseModel):
    model_config = ConfigDict(frozen=True)

    secret: str
    provisioning_uri: str


class SessionPrincipal(BaseModel):
    model_config = ConfigDict(frozen=True)

    account_id: str
    employee_no: str
    display_name: str
    session_kind: SessionKind
    is_super_admin: bool
