from typing import Literal

from pydantic import Field

from control_plane.app.shared.api.camel import CamelModel


class LoginRequestDto(CamelModel):
    employee_no: str = Field(pattern=r"^[0-9]{8}$")
    password: str


class TotpRequestDto(CamelModel):
    challenge_token: str
    code: str


class BootstrapPasswordRequestDto(CamelModel):
    password: str


class BootstrapTotpConfirmRequestDto(CamelModel):
    code: str


class TotpRequiredDto(CamelModel):
    state: Literal["TOTP_REQUIRED"] = "TOTP_REQUIRED"
    challenge_token: str


class BootstrapRequiredDto(CamelModel):
    state: Literal["BOOTSTRAP_REQUIRED"] = "BOOTSTRAP_REQUIRED"


class AuthenticatedDto(CamelModel):
    state: Literal["AUTHENTICATED"] = "AUTHENTICATED"


class PasswordSetDto(CamelModel):
    state: Literal["PASSWORD_SET"] = "PASSWORD_SET"


class PasswordUpdatedDto(CamelModel):
    state: Literal["PASSWORD_UPDATED_LOGIN_REQUIRED"] = "PASSWORD_UPDATED_LOGIN_REQUIRED"


class TotpEnrollmentDto(CamelModel):
    provisioning_uri: str


class LoggedOutDto(CamelModel):
    state: Literal["LOGGED_OUT"] = "LOGGED_OUT"
