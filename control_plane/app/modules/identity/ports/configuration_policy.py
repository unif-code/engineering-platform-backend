from datetime import datetime
from typing import Any, Protocol

from control_plane.app.modules.identity.domain.configuration_policy import (
    OwnedPolicyDraft,
    OwnedPolicyKey,
    OwnedPolicySnapshot,
    OwnedPublishedPolicyVersion,
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

    def active_snapshot(
        self,
        namespace: str,
        *,
        for_update: bool = False,
    ) -> OwnedPolicySnapshot | None: ...

    def version_snapshot(
        self,
        namespace: str,
        scope: str,
        version: int,
    ) -> OwnedPolicySnapshot | None: ...

    def list_versions(
        self,
        namespace: str,
        scope: str,
        *,
        before_version: int | None,
        limit: int,
    ) -> list[OwnedPublishedPolicyVersion]: ...

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

    def save_preview(
        self,
        draft_id: str,
        *,
        expected_revision: int,
        evidence: dict[str, Any],
        dependency_versions: dict[str, Any],
    ) -> OwnedPolicyDraft | None: ...

    def publish_version(self, **values: Any) -> OwnedPublishedPolicyVersion | None: ...

    def archive_candidates(
        self,
        namespace: str,
        scope: str,
        *,
        cutoff: datetime,
        limit: int,
    ) -> list[OwnedPolicyDraft]: ...

    def archive_draft(self, **values: Any) -> bool: ...
