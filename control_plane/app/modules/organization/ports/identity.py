from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class OrganizationAccountView:
    id: str
    employee_no: str
    display_name: str
    status: str
    initialized: bool


class IdentityAccountLookupPort(Protocol):
    def get(self, account_id: str) -> OrganizationAccountView | None: ...
