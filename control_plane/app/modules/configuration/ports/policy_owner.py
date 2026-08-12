from datetime import datetime, timedelta
from typing import Any, Protocol

from control_plane.app.modules.configuration.domain import (
    Draft,
    PolicyKey,
    PolicySnapshot,
    PreviewItem,
    PublishedVersion,
    ValidationIssue,
)


class PolicyOwnerPort(Protocol):
    def catalog(self, namespace: str) -> list[PolicyKey]: ...

    def active_snapshot(self, namespace: str) -> PolicySnapshot: ...

    def locked_active_snapshot(self, namespace: str) -> PolicySnapshot: ...

    def version_snapshot(
        self,
        namespace: str,
        scope: str,
        version: int,
    ) -> PolicySnapshot | None: ...

    def list_versions(
        self,
        namespace: str,
        scope: str,
        *,
        before_version: int | None,
        limit: int,
    ) -> list[PublishedVersion]: ...

    def validate_candidate(
        self,
        namespace: str,
        *,
        schema_revision: int,
        values: dict[str, Any],
    ) -> list[ValidationIssue]: ...

    def create_draft(self, **values: Any) -> Draft: ...

    def draft(self, draft_id: str, *, for_update: bool = False) -> Draft | None: ...

    def update_draft(
        self,
        draft_id: str,
        *,
        expected_revision: int,
        content: dict[str, Any],
        content_hash: str,
        stale: bool,
        now: datetime,
    ) -> Draft | None: ...

    def save_validation(
        self,
        draft_id: str,
        *,
        expected_revision: int,
        evidence: dict[str, Any],
        dependency_versions: dict[str, Any],
        now: datetime,
    ) -> Draft | None: ...

    def preview_candidate(
        self,
        namespace: str,
        *,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> list[PreviewItem]: ...

    def save_preview(
        self,
        draft_id: str,
        *,
        expected_revision: int,
        evidence: dict[str, Any],
        dependency_versions: dict[str, Any],
    ) -> Draft | None: ...

    def publish_version(self, **values: Any) -> PublishedVersion | None: ...

    def draft_archive_after(self, namespace: str) -> timedelta: ...

    def archive_candidates(
        self,
        namespace: str,
        scope: str,
        *,
        cutoff: datetime,
        limit: int,
    ) -> list[Draft]: ...

    def archive_draft(self, **values: Any) -> bool: ...
