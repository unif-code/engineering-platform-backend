from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import Connection


class IdentityRepository(Protocol):
    """Persistence operations required by identity application services."""

    db: Connection

    def lock_super_admin_invariant(self) -> None: ...

    def lock_backoff_scope(self, employee_no: str, source: str) -> None: ...

    def lock_account_lifecycle(self, account_id: str) -> None: ...

    def claim_idempotency(self, **values: Any) -> bool: ...

    def idempotency_by_scope(
        self,
        actor: str,
        operation: str,
        idempotency_key: str,
        *,
        for_update: bool = False,
    ) -> Any: ...

    def complete_idempotency(
        self,
        record_id: str,
        *,
        http_status: int,
        result_metadata: dict[str, object],
        sealed_response: bytes,
        now: datetime,
    ) -> bool: ...

    def insert_account(self, **values: Any) -> None: ...

    def account_by_id(self, account_id: str, *, for_update: bool = False) -> Any: ...

    def account_by_employee_no(self, employee_no: str, *, for_update: bool = False) -> Any: ...

    def invalidate_temp_credentials(self, account_id: str, now: datetime) -> None: ...

    def insert_temp_credential(self, **values: Any) -> None: ...

    def lock_active_temp_credential(self, employee_no: str, now: datetime) -> Any: ...

    def consume_temp_credential(self, credential_id: str, now: datetime) -> bool: ...

    def insert_session(self, **values: Any) -> None: ...

    def session_with_account(self, token_hash: str, *, for_update: bool = False) -> Any: ...

    def update_password(self, account_id: str, password_hash: str, now: datetime) -> None: ...

    def reset_password_state(self, account_id: str, now: datetime) -> None: ...

    def update_totp_enrollment(self, account_id: str, sealed: bytes, now: datetime) -> None: ...

    def confirm_totp(self, account_id: str, step: int, now: datetime) -> None: ...

    def restore_password_reset(self, account_id: str, step: int, now: datetime) -> None: ...

    def revoke_sessions(
        self,
        account_id: str,
        now: datetime,
        reason: str,
        *,
        except_session_id: str | None = None,
        kind: str | None = None,
    ) -> list[str]: ...

    def insert_challenge(self, **values: Any) -> None: ...

    def challenge_by_hash(self, token_hash: str, *, for_update: bool = False) -> Any: ...

    def fail_challenge(self, challenge_id: str, now: datetime) -> int: ...

    def consume_challenge(self, challenge_id: str, now: datetime) -> bool: ...

    def update_totp_step(self, account_id: str, step: int, now: datetime) -> bool: ...

    def backoff_by_scope(self, employee_no: str, source: str, *, for_update: bool) -> Any: ...

    def save_backoff(
        self,
        employee_no: str,
        source: str,
        failure_count: int,
        last_failure_at: datetime | None,
        locked_until: datetime | None,
    ) -> None: ...

    def evict_old_full_sessions(self, account_id: str, cap: int, now: datetime) -> list[str]: ...

    def revoke_session(self, session_id: str, now: datetime, reason: str) -> str | None: ...

    def touch_session(self, session_id: str, now: datetime, expires_hint: datetime) -> None: ...

    def effective_super_admins_except(self, account_id: str) -> int: ...

    def update_account_status(
        self,
        account_id: str,
        status: str,
        expected_version: int,
        now: datetime,
    ) -> Any: ...


class IdentityRepositoryFactory(Protocol):
    """Composition-owned factory for an identity persistence adapter."""

    def __call__(self, db: Connection) -> IdentityRepository: ...
