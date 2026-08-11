from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class WorkspaceAccountView:
    id: str
    status: str
    initialized: bool


@dataclass(frozen=True, slots=True)
class DirectReportView:
    id: str


class IdentityAccountLookupPort(Protocol):
    def get(self, account_id: str) -> WorkspaceAccountView | None: ...


class OrganizationReportsPort(Protocol):
    def is_effective_leader(self, account_id: str) -> bool: ...

    def direct_reports(self, leader_id: str) -> list[DirectReportView]: ...
