from datetime import datetime
from typing import Any, Protocol

from control_plane.app.modules.configuration.domain import (
    Draft,
    PolicyKey,
    PolicySnapshot,
    ValidationIssue,
)


class PolicyOwnerPort(Protocol):
    def catalog(self, namespace: str) -> list[PolicyKey]: ...

    def active_snapshot(self, namespace: str) -> PolicySnapshot: ...

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
