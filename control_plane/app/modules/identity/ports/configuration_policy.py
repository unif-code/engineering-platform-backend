from datetime import datetime
from typing import Any, Protocol

from control_plane.app.modules.identity.domain.configuration_policy import (
    OwnedPolicyDraft,
    OwnedPolicyKey,
    OwnedPolicySnapshot,
)


class IdentityPolicyOwnerRepository(Protocol):
    def claim_configuration_idempotency(self, **values: Any) -> bool: ...

    def configuration_idempotency_by_scope(
        self,
        actor: str,
        operation: str,
        idempotency_key: str,
        *,
        for_update: bool = False,
    ) -> Any: ...

    def complete_configuration_idempotency(
        self,
        record_id: str,
        *,
        http_status: int,
        result_metadata: dict[str, object],
        sealed_response: bytes,
        now: datetime,
    ) -> bool: ...

    def catalog(self, namespace: str) -> list[OwnedPolicyKey]: ...

    def active_snapshot(self, namespace: str) -> OwnedPolicySnapshot | None: ...

    def insert_draft(self, **values: Any) -> OwnedPolicyDraft: ...

    def draft_by_id(
        self, draft_id: str, *, for_update: bool = False
    ) -> OwnedPolicyDraft | None: ...

    def update_draft(
        self,
        draft_id: str,
        *,
        expected_revision: int,
        content: dict[str, Any],
        content_hash: str,
        stale: bool,
        now: datetime,
    ) -> OwnedPolicyDraft | None: ...

    def save_validation(
        self,
        draft_id: str,
        *,
        expected_revision: int,
        evidence: dict[str, Any],
        dependency_versions: dict[str, Any],
        now: datetime,
    ) -> OwnedPolicyDraft | None: ...
