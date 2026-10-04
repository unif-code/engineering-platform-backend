from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import Connection

from control_plane.app.modules.configuration.domain import (
    Draft,
    DraftBaseComparison,
    DraftValidation,
    Preview,
)
from control_plane.app.modules.configuration.ports.policy_owner import PolicyOwnerPort
from control_plane.app.shared.idempotency import IdempotentResponse


class PolicyLifecyclePort(Protocol):
    @property
    def db(self) -> Connection: ...
    @property
    def owner(self) -> PolicyOwnerPort: ...
    def create_draft(self, **values: Any) -> Draft: ...
    def update_draft(self, **values: Any) -> Draft: ...
    def takeover_draft(self, **values: Any) -> Draft: ...
    def validate_draft(self, **values: Any) -> DraftValidation: ...
    def preview(self, **values: Any) -> Preview: ...
    def base_comparison(self, **values: Any) -> DraftBaseComparison: ...
    def apply_rebase(self, **values: Any) -> Draft: ...


class PolicyRuntimePort(Protocol):
    def transaction(self) -> AbstractContextManager[PolicyLifecyclePort]: ...
    def archive(self, *, now: datetime) -> int: ...
    def publish(
        self,
        *,
        actor_id: str,
        namespace: str,
        draft_id: str,
        expected_revision: int,
        reason: str,
        totp_code: str,
        idempotency_key: str,
        raw_session: str,
    ) -> IdempotentResponse: ...
    def rollback(
        self,
        *,
        actor_id: str,
        namespace: str,
        scope: str,
        to_version: int,
        expected_version: int,
        reason: str,
        totp_code: str,
        idempotency_key: str,
        raw_session: str,
    ) -> IdempotentResponse: ...
