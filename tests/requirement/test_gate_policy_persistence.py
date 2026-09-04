from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from control_plane.app.modules import requirement
from control_plane.app.modules.configuration import (
    PolicySnapshotUnavailable,
)
from tests.configuration.test_publish import _dependencies

pytestmark = pytest.mark.integration


def test_seed_lifecycle_and_archive_use_requirement_transaction(
    isolated_requirement_database: Any,
) -> None:
    runtime_type = getattr(requirement, "RequirementPolicyRuntime", None)
    assert runtime_type is not None, "Requirement policy runtime is missing"
    runtime = runtime_type(isolated_requirement_database.runtime, _dependencies())
    snapshot = runtime.resolved_snapshot()
    assert snapshot.version == 1
    assert snapshot.policy.draft_archive_after_days == 30
    with runtime.transaction() as lifecycle:
        draft = lifecycle.create_draft(
            namespace="requirement.gate", values={}, actor_id=str(uuid4())
        )
        validation = lifecycle.validate_draft(
            namespace="requirement.gate",
            draft_id=draft.id,
            actor_id=draft.owner_id,
            expected_revision=1,
        )
        before = lifecycle.owner.draft(draft.id).last_meaningful_activity_at
        lifecycle.preview(
            namespace="requirement.gate",
            draft_id=draft.id,
            actor_id=draft.owner_id,
            expected_revision=validation.revision,
        )
        assert lifecycle.owner.draft(draft.id).last_meaningful_activity_at == before
    assert runtime.archive(now=before + timedelta(days=31)) == 1
    assert runtime.archive(now=before + timedelta(days=31)) == 0
    assert runtime.resolved_snapshot() == snapshot
    with isolated_requirement_database.owner.connect() as db:
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM requirement.gate_policy_outbox WHERE "
                    "event_type='DRAFT_ARCHIVED'"
                )
            ).scalar_one()
            == 1
        )


def test_missing_active_policy_never_falls_back(isolated_requirement_database: Any) -> None:
    runtime_type = getattr(requirement, "RequirementPolicyRuntime", None)
    assert runtime_type is not None, "Requirement policy runtime is missing"
    with isolated_requirement_database.owner.begin() as db:
        db.execute(text("DELETE FROM requirement.gate_policy_active_pointer"))
    with pytest.raises(PolicySnapshotUnavailable):
        runtime_type(isolated_requirement_database.runtime, _dependencies()).resolved_snapshot()


def test_runtime_cannot_rewrite_immutable_versions(isolated_requirement_database: Any) -> None:
    with isolated_requirement_database.runtime.begin() as db:
        assert db.execute(
            text("SELECT to_regclass('requirement.gate_policy_version') IS NOT NULL")
        ).scalar_one(), "policy migration missing"
        with pytest.raises(DBAPIError):
            db.execute(text("UPDATE requirement.gate_policy_version SET reason='tampered'"))


@pytest.mark.parametrize("mutation", ["hash", "schema", "content"])
def test_corrupt_active_policy_cannot_be_read_or_archived(
    isolated_requirement_database: Any, mutation: str
) -> None:
    statements = {
        "hash": "UPDATE requirement.gate_policy_version SET snapshot_hash=repeat('a',64)",
        "schema": "UPDATE requirement.gate_policy_version SET schema_revision=2",
        "content": "UPDATE requirement.gate_policy_version SET snapshot='{}'",
    }
    with isolated_requirement_database.owner.begin() as db:
        db.execute(text(statements[mutation]))
    runtime = requirement.RequirementPolicyRuntime(
        isolated_requirement_database.runtime, _dependencies()
    )
    with pytest.raises(PolicySnapshotUnavailable):
        runtime.resolved_snapshot()
    with pytest.raises(PolicySnapshotUnavailable):
        runtime.archive(now=_dependencies().clock.now())


def test_concurrent_edit_prevents_stale_archive_and_receipt_is_append_only(
    isolated_requirement_database: Any,
) -> None:
    runtime = requirement.RequirementPolicyRuntime(
        isolated_requirement_database.runtime, _dependencies()
    )
    with runtime.transaction() as lifecycle:
        original = lifecycle.create_draft(
            namespace="requirement.gate", values={}, actor_id=str(uuid4())
        )
        edited = lifecycle.update_draft(
            namespace="requirement.gate",
            draft_id=original.id,
            values={"draft_archive_after_days": 12},
            actor_id=original.owner_id,
            expected_revision=1,
        )
        assert not lifecycle.owner.archive_draft(
            draft_id=original.id,
            namespace=original.namespace,
            scope=original.scope,
            expected_revision=original.revision,
            expected_owner_id=original.owner_id,
            expected_activity=original.last_meaningful_activity_at,
            archived_at=original.last_meaningful_activity_at + timedelta(days=31),
            outbox_id=str(uuid4()),
            aggregate_id=original.id,
            outbox_payload={},
        )
        assert lifecycle.owner.draft(original.id) == edited
    with isolated_requirement_database.owner.connect() as db:
        assert db.execute(
            text(
                "SELECT has_table_privilege('requirement_rw','requirement.gate_policy_receipt',"
                "'INSERT')"
            )
        ).scalar_one()
        for role in ("identity_rw", "configuration_rw", "authorization_rw", "source_control_rw"):
            assert not db.execute(
                text("SELECT has_table_privilege(:role,'requirement.gate_policy_draft','INSERT')"),
                {"role": role},
            ).scalar_one()
        assert not db.execute(
            text(
                "SELECT has_table_privilege('requirement_rw','requirement.gate_policy_receipt',"
                "'UPDATE')"
            )
        ).scalar_one()


def test_unrepresentable_archive_cutoff_is_rejected_before_draft_write(
    isolated_requirement_database: Any,
) -> None:
    from control_plane.app.modules.configuration import InvalidPolicyValue

    runtime = requirement.RequirementPolicyRuntime(
        isolated_requirement_database.runtime, _dependencies()
    )
    with runtime.transaction() as lifecycle:
        with pytest.raises(InvalidPolicyValue):
            lifecycle.create_draft(
                namespace="requirement.gate",
                values={"draft_archive_after_days": 3_000_000},
                actor_id=str(uuid4()),
            )
