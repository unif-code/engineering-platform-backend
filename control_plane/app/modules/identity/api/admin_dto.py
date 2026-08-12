from pydantic import Field, field_validator

from control_plane.app.modules.identity import AccountDto, AccountStatus
from control_plane.app.shared.api.camel import CamelModel
from control_plane.app.shared.api.concurrency import entity_tag


class AccountSummaryResponseDto(CamelModel):
    id: str
    employee_no: str
    display_name: str
    profession: str | None
    status: AccountStatus
    etag: str

    @classmethod
    def from_domain(cls, value: AccountDto) -> "AccountSummaryResponseDto":
        return cls(
            id=value.id,
            employee_no=value.employee_no,
            display_name=value.display_name,
            profession=value.profession,
            status=value.status,
            etag=entity_tag(value.version),
        )


class AccountListResponseDto(CamelModel):
    items: list[AccountSummaryResponseDto]
    next_cursor: str | None = None


class _ReasonRequestDto(CamelModel):
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason must not be blank")
        return normalized


class CreateAccountRequestDto(_ReasonRequestDto):
    employee_no: str = Field(pattern=r"^[0-9]{8}$")
    display_name: str = Field(min_length=1, max_length=200)
    profession: str | None = Field(default=None, max_length=200)


class AccountReasonRequestDto(_ReasonRequestDto):
    pass


class AccountCredentialReceiptDto(CamelModel):
    account: AccountSummaryResponseDto
    temporary_password: str
