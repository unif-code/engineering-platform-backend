from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

PLATFORM_CONFIGURATION_MANAGE = "platform.configuration.manage"
PLATFORM_SUPER_ADMIN_MANAGE = "platform.super_admin.manage"
RESERVED_PLATFORM_CAPABILITIES = frozenset(
    {PLATFORM_CONFIGURATION_MANAGE, PLATFORM_SUPER_ADMIN_MANAGE}
)
V02_SUPER_ADMIN_PLATFORM_CAPABILITIES = frozenset(
    {
        "platform.home.read",
        "platform.admin.access",
        "audit.read",
        "identity.account.manage",
        "platform.organization.read",
        "platform.organization.manage",
        "platform.workspace.read",
        "platform.workspace.manage",
        "platform.authorization.manage",
        PLATFORM_CONFIGURATION_MANAGE,
        PLATFORM_SUPER_ADMIN_MANAGE,
    }
)


class ScopeType(StrEnum):
    PLATFORM = "PLATFORM"
    WORKSPACE = "WORKSPACE"


class Scope(BaseModel):
    model_config = ConfigDict(frozen=True)

    scope_type: ScopeType
    scope_id: str | None = None

    @field_validator("scope_id")
    @classmethod
    def _normalize_scope_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("scope id must not be blank")
        return normalized

    @model_validator(mode="after")
    def _validate_shape(self) -> "Scope":
        if self.scope_type is ScopeType.PLATFORM and self.scope_id is not None:
            raise ValueError("platform scope has no resource id")
        if self.scope_type is ScopeType.WORKSPACE and self.scope_id is None:
            raise ValueError("workspace scope requires an id")
        return self

    @classmethod
    def platform(cls) -> "Scope":
        return cls(scope_type=ScopeType.PLATFORM)

    @classmethod
    def workspace(cls, workspace_id: str) -> "Scope":
        return cls(scope_type=ScopeType.WORKSPACE, scope_id=workspace_id)


def is_v02_super_admin_platform_capability(
    capability: str,
    scope: Scope,
    *,
    is_super_admin: bool,
) -> bool:
    return (
        is_super_admin
        and scope.scope_type is ScopeType.PLATFORM
        and capability in V02_SUPER_ADMIN_PLATFORM_CAPABILITIES
    )


class GrantStatus(StrEnum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class GrantDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    principal_id: str
    capability: str
    scope: Scope
    source: str
    valid_from: datetime | None
    valid_to: datetime | None
    status: GrantStatus
    version: int
    created_at: datetime
    updated_at: datetime


class PrincipalVersionDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    account_id: str
    version: int
    fence_generation: int
    dirty_generation: int | None
    dirty_reason: str | None
    updated_at: datetime


class ScopedCapability(BaseModel):
    model_config = ConfigDict(frozen=True)

    capability: str
    scope: Scope


class AuthorizationPrincipal(BaseModel):
    model_config = ConfigDict(frozen=True)

    account_id: str
    employee_id: str
    name: str
    is_super_admin: bool
    authorization_version: int
    capabilities: tuple[ScopedCapability, ...]


class DecisionCode(StrEnum):
    ALLOW = "ALLOW"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    DENIED = "DENIED"
    UNAVAILABLE = "UNAVAILABLE"


class AuthorizationDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    allowed: bool
    code: DecisionCode
    principal: AuthorizationPrincipal | None = None
