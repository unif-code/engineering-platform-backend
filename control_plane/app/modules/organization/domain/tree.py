from pydantic import BaseModel, ConfigDict


class CorruptStructure(RuntimeError):
    """Stored organization facts cannot be represented safely."""


class AccountRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    employee_no: str
    display_name: str


class LeaderNode(BaseModel):
    model_config = ConfigDict(frozen=True)

    account: AccountRef
    members: list[AccountRef]


class ManagerNode(BaseModel):
    model_config = ConfigDict(frozen=True)

    account: AccountRef
    leaders: list[LeaderNode]


class OrgTreeDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    managers: list[ManagerNode]
