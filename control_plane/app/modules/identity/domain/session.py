from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class SessionKind(StrEnum):
    BOOTSTRAP = "BOOTSTRAP"
    FULL = "FULL"


class BootstrapPurpose(StrEnum):
    INITIAL_SETUP = "INITIAL_SETUP"
    PASSWORD_RESET = "PASSWORD_RESET"
    PASSWORD_EXPIRED = "PASSWORD_EXPIRED"


class AuthDenialCode(StrEnum):
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    BACKOFF_ACTIVE = "BACKOFF_ACTIVE"
    INVALID_CHALLENGE = "INVALID_CHALLENGE"


class AuthChallengeState(StrEnum):
    RETRY_ALLOWED = "RETRY_ALLOWED"
    TERMINAL = "TERMINAL"


class AuthenticationDenial(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: AuthDenialCode
    retry_after_seconds: int | None = None
    challenge_state: AuthChallengeState | None = None


class IssuedSession(BaseModel):
    model_config = ConfigDict(frozen=True)

    account_id: str
    raw_token: str
    kind: SessionKind
    bootstrap_purpose: BootstrapPurpose | None = None


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
    bootstrap_purpose: BootstrapPurpose | None = None
    is_super_admin: bool
