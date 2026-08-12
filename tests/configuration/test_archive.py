import json
from dataclasses import replace
from datetime import timedelta
from io import StringIO
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine, text

from control_plane.app.modules.audit.adapters.transactional import (
    SqlAlchemyTransactionalAuditAppender,
)
from control_plane.app.modules.configuration import ConfigurationDependencies, create_draft
from control_plane.app.modules.configuration.adapters import IdentityEffectivePolicy
from tests.configuration.test_publish import _Clock, _Random
from tests.identity.task5_helpers import dependencies as identity_dependencies


@pytest.mark.integration
def test_archive_uses_active_policy_boundary_is_idempotent_and_only_transitions_drafts(
    configuration_rw_engine: Engine,
    configuration_owner_engine: Engine,
    configuration_seed: None,
) -> None:
    del configuration_seed
    from control_plane.app.modules.configuration import archive_stale_drafts

    identity_deps = replace(identity_dependencies(), policy=IdentityEffectivePolicy())
    dependencies = ConfigurationDependencies(
        clock=_Clock(),
        random=_Random(),
        audit=SqlAlchemyTransactionalAuditAppender(),
        identity=identity_deps,
    )
    owner_id = f"archive-owner-{uuid4()}"
    now = dependencies.clock.now()
    with configuration_rw_engine.begin() as db:
        exact = create_draft(
            db,
            namespace="identity",
            values={"identity.session_idle_timeout": 31},
            actor_id=owner_id,
            dependencies=dependencies,
        )
        older = create_draft(
            db,
            namespace="identity",
            values={"identity.session_idle_timeout": 32},
            actor_id=owner_id,
            dependencies=dependencies,
        )
        active = create_draft(
            db,
            namespace="identity",
            values={"identity.session_idle_timeout": 33},
            actor_id=owner_id,
            dependencies=dependencies,
        )
        already_archived = create_draft(
            db,
            namespace="identity",
            values={"identity.session_idle_timeout": 34},
            actor_id=owner_id,
            dependencies=dependencies,
        )
    draft_ids = [exact.id, older.id, active.id, already_archived.id]
    prior_archive_time = now - timedelta(days=1)
    with configuration_owner_engine.begin() as db:
        db.execute(
            text("UPDATE identity.draft SET last_meaningful_activity_at=:activity WHERE id=:id"),
            {"activity": now - timedelta(days=30), "id": exact.id},
        )
        db.execute(
            text("UPDATE identity.draft SET last_meaningful_activity_at=:activity WHERE id=:id"),
            {"activity": now - timedelta(days=31), "id": older.id},
        )
        db.execute(
            text("UPDATE identity.draft SET last_meaningful_activity_at=:activity WHERE id=:id"),
            {"activity": now - timedelta(days=30) + timedelta(seconds=1), "id": active.id},
        )
        db.execute(
            text(
                "UPDATE identity.draft SET status='ARCHIVED', "
                "last_meaningful_activity_at=:activity, archived_at=:archived_at "
                "WHERE id=:id"
            ),
            {
                "activity": now - timedelta(days=31),
                "archived_at": prior_archive_time,
                "id": already_archived.id,
            },
        )

    try:
        with configuration_rw_engine.begin() as db:
            first_count = archive_stale_drafts(db, now=now, dependencies=dependencies)
        with configuration_rw_engine.begin() as db:
            replay_count = archive_stale_drafts(db, now=now, dependencies=dependencies)

        with configuration_owner_engine.connect() as db:
            rows = db.execute(
                text(
                    "SELECT id::text, status, archived_at FROM identity.draft "
                    "WHERE id::text = ANY(:ids) ORDER BY id"
                ),
                {"ids": draft_ids},
            ).all()
            outbox = (
                db.execute(
                    text(
                        "SELECT aggregate_id, payload FROM identity.configuration_outbox "
                        "WHERE event_type='DRAFT_ARCHIVED' "
                        "AND aggregate_id = ANY(:aggregate_ids) ORDER BY aggregate_id"
                    ),
                    {
                        "aggregate_ids": [
                            f"identity:PLATFORM:draft:{exact.id}",
                            f"identity:PLATFORM:draft:{older.id}",
                        ]
                    },
                )
                .mappings()
                .all()
            )
            audits = (
                db.execute(
                    text(
                        "SELECT target_id, result, reason FROM audit.audit_event "
                        "WHERE action='configuration.draft.archived' "
                        "AND target_id = ANY(:ids) ORDER BY target_id"
                    ),
                    {"ids": draft_ids},
                )
                .mappings()
                .all()
            )

        by_id = {row[0]: row for row in rows}
        assert first_count == 2
        assert replay_count == 0
        assert by_id[exact.id][1:] == ("ARCHIVED", now)
        assert by_id[older.id][1:] == ("ARCHIVED", now)
        assert by_id[active.id][1:] == ("DRAFT", None)
        assert by_id[already_archived.id][1:] == ("ARCHIVED", prior_archive_time)
        assert [row["aggregate_id"] for row in outbox] == sorted(
            [f"identity:PLATFORM:draft:{exact.id}", f"identity:PLATFORM:draft:{older.id}"]
        )
        assert all(row["payload"]["policyVersion"] == 1 for row in outbox)
        assert {row["target_id"] for row in audits} == {exact.id, older.id}
        assert all(row["result"] == "SUCCESS" for row in audits)
        assert all("policyVersion=1" in row["reason"] for row in audits)
        assert all("cutoff=" in row["reason"] for row in audits)
    finally:
        with configuration_owner_engine.begin() as db:
            db.execute(
                text(
                    "DELETE FROM identity.configuration_outbox WHERE event_type='DRAFT_ARCHIVED' "
                    "AND aggregate_id = ANY(:aggregate_ids)"
                ),
                {
                    "aggregate_ids": [
                        f"identity:PLATFORM:draft:{exact.id}",
                        f"identity:PLATFORM:draft:{older.id}",
                    ]
                },
            )
            db.execute(
                text("DELETE FROM audit.audit_event WHERE target_id = ANY(:ids)"),
                {"ids": draft_ids},
            )
            db.execute(
                text("DELETE FROM identity.draft WHERE id::text = ANY(:ids)"),
                {"ids": draft_ids},
            )


class _BrokenEngine:
    def begin(self) -> Any:
        raise RuntimeError("postgresql://configuration_rw:credential-sentinel@db/platform")


def test_archive_cli_has_stable_safe_success_and_failure_output() -> None:
    from control_plane.tools.archive_drafts import main

    class _NoopContext:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *_args: object) -> None:
            return None

    class _NoopEngine:
        def begin(self) -> _NoopContext:
            return _NoopContext()

    success_stdout = StringIO()
    success_stderr = StringIO()
    dependencies = ConfigurationDependencies(
        clock=_Clock(),
        random=_Random(),
        audit=SqlAlchemyTransactionalAuditAppender(),
    )
    success = main(
        [],
        engine=_NoopEngine(),  # type: ignore[arg-type]
        dependencies=dependencies,
        archive=lambda _db, **_values: 0,
        stdout=success_stdout,
        stderr=success_stderr,
    )
    failed_stdout = StringIO()
    failed_stderr = StringIO()
    failed = main(
        [],
        engine=_BrokenEngine(),  # type: ignore[arg-type]
        dependencies=dependencies,
        stdout=failed_stdout,
        stderr=failed_stderr,
    )

    assert success == 0
    assert json.loads(success_stdout.getvalue()) == {"archivedDrafts": 0}
    assert success_stderr.getvalue() == ""
    assert failed == 1
    assert failed_stdout.getvalue() == ""
    assert json.loads(failed_stderr.getvalue()) == {"status": "FAILED"}
    combined = success_stdout.getvalue() + failed_stderr.getvalue()
    assert "credential-sentinel" not in combined
    assert "postgresql" not in combined.lower()
