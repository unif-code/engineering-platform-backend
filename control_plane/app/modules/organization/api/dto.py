from pydantic import Field

from control_plane.app.modules.organization.domain import (
    AccountRef,
    LeaderNode,
    ManagerNode,
    OrgTreeDto,
)
from control_plane.app.shared.api.camel import CamelModel


class SetSuperiorRequestDto(CamelModel):
    superior_id: str | None = None
    reason: str = Field(min_length=1, max_length=500)


class AccountRefDto(CamelModel):
    id: str
    employee_no: str
    display_name: str

    @classmethod
    def from_domain(cls, account: AccountRef) -> "AccountRefDto":
        return cls.model_validate(account.model_dump())


class LeaderNodeDto(CamelModel):
    account: AccountRefDto
    members: list[AccountRefDto]

    @classmethod
    def from_domain(cls, leader: LeaderNode) -> "LeaderNodeDto":
        return cls(
            account=AccountRefDto.from_domain(leader.account),
            members=[AccountRefDto.from_domain(member) for member in leader.members],
        )


class ManagerNodeDto(CamelModel):
    account: AccountRefDto
    leaders: list[LeaderNodeDto]

    @classmethod
    def from_domain(cls, manager: ManagerNode) -> "ManagerNodeDto":
        return cls(
            account=AccountRefDto.from_domain(manager.account),
            leaders=[LeaderNodeDto.from_domain(leader) for leader in manager.leaders],
        )


class OrgTreeResponseDto(CamelModel):
    managers: list[ManagerNodeDto]

    @classmethod
    def from_domain(cls, tree: OrgTreeDto) -> "OrgTreeResponseDto":
        return cls(managers=[ManagerNodeDto.from_domain(manager) for manager in tree.managers])
