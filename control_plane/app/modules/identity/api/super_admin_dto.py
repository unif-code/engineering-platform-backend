from datetime import datetime

from pydantic import Field, field_validator

from control_plane.app.modules.identity import AccountDto, AccountStatus
from control_plane.app.shared.api.camel import CamelModel


class SuperAdminResponseDto(CamelModel):
    id: str
    employee_no: str
    display_name: str
    profession: str | None
    status: AccountStatus
    password_set_at: datetime | None
    totp_confirmed_at: datetime | None
    is_super_admin: bool
    version: int

    @classmethod
    def from_domain(cls, value: AccountDto) -> "SuperAdminResponseDto":
        return cls(**value.__dict__)


class SuperAdminListResponseDto(CamelModel):
    items: list[SuperAdminResponseDto]
    next_cursor: str | None = None


class _SuperAdminReasonRequestDto(CamelModel):
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason must not be blank")
        return normalized


class AddSuperAdminRequestDto(_SuperAdminReasonRequestDto):
    account_id: str = Field(min_length=1)
    totp_code: str = Field(min_length=6, max_length=6, pattern=r"^[0-9]{6}$")


class RemoveSuperAdminRequestDto(_SuperAdminReasonRequestDto):
    totp_code: str = Field(min_length=6, max_length=6, pattern=r"^[0-9]{6}$")
