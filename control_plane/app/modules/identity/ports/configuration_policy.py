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

    def record_revision(
        self,
        *,
        draft: dict[str, Any],
        operation: str,
        actor_id: str,
        validation: dict[str, Any] | None = None,
        takeover: dict[str, Any] | None = None,
        governance_record_id: str | None = None,
    ) -> None: ...

    def draft_revision_target(
        self, namespace: str, scope: str, draft_id: str
    ) -> dict[str, Any] | None: ...

    def draft_revision_stats(
        self, namespace: str, scope: str, draft_id: str, *, through_revision: int
    ) -> dict[str, Any]: ...

    def draft_revision_rows(
        self,
        namespace: str,
        scope: str,
        draft_id: str,
        *,
        through_revision: int,
        before_revision: int | None,
        limit: int,
    ) -> list[dict[str, Any]]: ...

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

    def draft_summary(self, namespace: str, scope: str, draft_id: str) -> dict[str, Any] | None: ...

    def archive_records(
        self, namespace: str, scope: str, draft_id: str
    ) -> list[dict[str, Any]]: ...

    def list_draft_summaries(
        self,
        namespace: str,
        scope: str,
        *,
        view: str,
        owner_id: str | None,
        current_version: int,
        after_id: str | None,
        limit: int,
    ) -> list[dict[str, Any]]: ...

    def list_versions(
        self,
        namespace: str,
        scope: str,
        *,
        before_version: int | None,
        limit: int,
    ) -> list[OwnedPublishedPolicyVersion]: ...

    def insert_draft(
        self,
        *,
        id: str,
        namespace: str,
        scope: str,
        content: dict[str, Any],
        base_version: int,
        owner_id: str,
        now: datetime,
        schema_revision: int,
        stale: bool = False,
        rollback_from_version: int | None = None,
    ) -> OwnedPolicyDraft: ...

    def draft_by_id(
        self, draft_id: str, *, for_update: bool = False
    ) -> OwnedPolicyDraft | None: ...

    def takeover_draft(
        self, draft_id: str, *, expected_revision: int, owner_id: str, now: datetime
    ) -> OwnedPolicyDraft | None: ...

    def rebase_draft(
        self,
        draft_id: str,
        *,
        namespace: str,
        expected_revision: int,
        expected_owner_id: str,
        expected_base_version: int,
        schema_revision: int,
        base_version: int,
        content: dict[str, Any],
        now: datetime,
    ) -> OwnedPolicyDraft | None: ...

    def record_rebase(self, **values: Any) -> None: ...

    def record_clone(self, **values: Any) -> None: ...

    def clone_record(self, namespace: str, scope: str, draft_id: str) -> dict[str, Any] | None: ...

    def rebase_records(
        self,
        namespace: str,
        scope: str,
        draft_id: str,
        *,
        through_revision: int,
        before_revision: int | None,
        limit: int,
    ) -> list[dict[str, Any]]: ...

    def update_draft(
        self,
        draft_id: str,
        *,
        expected_revision: int,
        content: dict[str, Any],
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
