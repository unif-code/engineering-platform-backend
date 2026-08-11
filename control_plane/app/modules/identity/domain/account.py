from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from control_plane.app.modules.identity.domain.errors import (
    InvalidAccountTransition,
    LastEffectiveSuperAdmin,
)


class AccountStatus(StrEnum):
    PENDING_INIT = "PENDING_INIT"
    ENABLED = "ENABLED"
    DISABLED = "DISABLED"
    RESTRICTED = "RESTRICTED"


class AccountDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    employee_no: str
    display_name: str
    profession: str | None = None
    status: AccountStatus
    password_set_at: datetime | None = None
    totp_confirmed_at: datetime | None = None
    is_super_admin: bool = False
    version: int


def ensure_account_transition_allowed(
    *,
    current: AccountStatus,
    target: AccountStatus,
    password_initialized: bool,
    totp_initialized: bool,
) -> None:
    if target is current or target is AccountStatus.PENDING_INIT:
        raise InvalidAccountTransition(f"cannot transition {current} to {target}")
    if target is AccountStatus.ENABLED and (
        current is AccountStatus.PENDING_INIT or not password_initialized or not totp_initialized
    ):
        raise InvalidAccountTransition("bootstrap completion is required before enabling")


def ensure_effective_super_admin_remains(
    *,
    is_super_admin: bool,
    status: AccountStatus,
    password_initialized: bool,
    totp_initialized: bool,
    other_effective_super_admins: int,
) -> None:
    """Central invariant reused by every path that can remove effective availability."""
    currently_effective = (
        is_super_admin
        and status is AccountStatus.ENABLED
        and password_initialized
        and totp_initialized
    )
    if currently_effective and other_effective_super_admins == 0:
        raise LastEffectiveSuperAdmin("last effective Super Admin must remain available")
