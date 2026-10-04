"""Reusable owner-injected lifecycle; the caller owns the transaction."""

from datetime import datetime
from typing import Any

from sqlalchemy import Connection

from control_plane.app.modules.configuration.application import drafts
from control_plane.app.modules.configuration.application.archive import archive_stale_drafts
from control_plane.app.modules.configuration.application.base_comparison import compare_draft_base
from control_plane.app.modules.configuration.application.dependencies import (
    ConfigurationDependencies,
)
from control_plane.app.modules.configuration.application.preview import preview
from control_plane.app.modules.configuration.domain import (
    Draft,
    DraftBaseComparison,
    DraftValidation,
    Preview,
)
from control_plane.app.modules.configuration.ports.policy_owner import PolicyOwnerPort


class PolicyLifecycle:
    def __init__(
        self, db: Connection, owner: PolicyOwnerPort, dependencies: ConfigurationDependencies
    ) -> None:
        self.db, self.owner, self.dependencies = db, owner, dependencies

    def create_draft(self, **values: Any) -> Draft:
        return drafts.create_draft(self.db, self.owner, dependencies=self.dependencies, **values)

    def update_draft(self, **values: Any) -> Draft:
        return drafts.update_draft(self.db, self.owner, dependencies=self.dependencies, **values)

    def takeover_draft(self, **values: Any) -> Draft:
        return drafts.takeover_draft(self.db, self.owner, dependencies=self.dependencies, **values)

    def validate_draft(self, **values: Any) -> DraftValidation:
        return drafts.validate_draft(self.db, self.owner, dependencies=self.dependencies, **values)

    def preview(self, **values: Any) -> Preview:
        return preview(self.db, self.owner, dependencies=self.dependencies, **values)

    def base_comparison(self, **values: Any) -> DraftBaseComparison:
        return compare_draft_base(self.owner, **values)

    def archive(self, *, now: datetime, namespace: str) -> int:
        return archive_stale_drafts(
            self.db, self.owner, now=now, namespace=namespace, dependencies=self.dependencies
        )
