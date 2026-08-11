from datetime import datetime
from typing import Any

from pydantic import Field

from control_plane.app.modules.authorization import (
    AuthorizationPrincipal,
    GrantDto,
    GrantStatus,
    ScopeType,
)
from control_plane.app.shared.api.camel import CamelModel


class ScopedCapabilityDto(CamelModel):
    capability: str
    scope_type: ScopeType
    scope_id: str | None = None


class PrincipalDto(CamelModel):
    employee_id: str
    name: str
    account_id: str | None = None
    organization: dict[str, Any] | None = None
    workspaces: list[dict[str, Any]] = Field(default_factory=list)
    authorization_version: int | None = None
    capabilities: list[ScopedCapabilityDto] = Field(default_factory=list)
    is_super_admin: bool | None = None

    @classmethod
    def from_domain(
        cls,
        principal: AuthorizationPrincipal,
        *,
        organization: dict[str, Any] | None,
        workspaces: list[dict[str, Any]],
    ) -> "PrincipalDto":
        return cls(
            employee_id=principal.employee_id,
            name=principal.name,
            account_id=principal.account_id,
            organization=organization,
            workspaces=workspaces,
            authorization_version=principal.authorization_version,
            capabilities=[
                ScopedCapabilityDto(
                    capability=item.capability,
                    scope_type=item.scope.scope_type,
                    scope_id=item.scope.scope_id,
                )
                for item in principal.capabilities
            ],
            is_super_admin=principal.is_super_admin,
        )


class NavigationItemDto(CamelModel):
    route_key: str
    name: str
    order: int
    capability: str | None = None
    scope_type: ScopeType | None = None
    meta: dict[str, Any] | None = None


class GrantCreateRequestDto(CamelModel):
    principal_id: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    scope_type: ScopeType
    scope_id: str | None = None
    source: str = Field(default="MANUAL", min_length=1)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    reason: str = Field(min_length=1, max_length=500)


class GrantRevokeRequestDto(CamelModel):
    reason: str = Field(min_length=1, max_length=500)


class GrantResponseDto(CamelModel):
    id: str
    principal_id: str
    capability: str
    scope_type: ScopeType
    scope_id: str | None
    source: str
    valid_from: datetime | None
    valid_to: datetime | None
    status: GrantStatus
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, value: GrantDto) -> "GrantResponseDto":
        return cls(
            id=value.id,
            principal_id=value.principal_id,
            capability=value.capability,
            scope_type=value.scope.scope_type,
            scope_id=value.scope.scope_id,
            source=value.source,
            valid_from=value.valid_from,
            valid_to=value.valid_to,
            status=value.status,
            version=value.version,
            created_at=value.created_at,
            updated_at=value.updated_at,
        )


class GrantListResponseDto(CamelModel):
    items: list[GrantResponseDto]
    next_cursor: str | None = None
