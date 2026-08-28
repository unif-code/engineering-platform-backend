from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine

from control_plane.app.modules.source_control.adapters import (
    SqlAlchemySourceControlRepository,
)

NOW = datetime(2026, 8, 26, 3, 0, tzinfo=UTC)
REPOSITORY_ID = "10000000-0000-0000-0000-000000000401"
WORKSPACE_ID = "20000000-0000-0000-0000-000000000401"
MESSAGE_ID = "30000000-0000-0000-0000-000000000401"
REQUIREMENT_ID = "40000000-0000-0000-0000-000000000401"
WORK_ITEM_ID = "50000000-0000-0000-0000-000000000401"
EFFECT_ID = "60000000-0000-0000-0000-000000000401"


def _insert_repository(
    repository: SqlAlchemySourceControlRepository,
    *,
    repository_id: str = REPOSITORY_ID,
    workspace_id: str = WORKSPACE_ID,
    project_id: str = "101",
    project_path: str = "platform/backend",
    status: str = "AUTHORIZED",
) -> None:
    repository.insert_workspace_repository(
        id=repository_id,
        workspace_id=workspace_id,
        provider="GITLAB",
        project_id=project_id,
        project_path=project_path,
        default_branch="main",
        connection_ref="gitlab-dev",
        credential_secret_ref="secret-ref:credential",
        webhook_signing_secret_ref="secret-ref:webhook",
        status=status,
        revision=1,
        now=NOW,
    )


def test_authorized_repository_query_is_scoped_sanitized_and_deterministic(
    isolated_source_control_rw_engine: Engine,
) -> None:
    second_repository_id = "10000000-0000-0000-0000-000000000402"
    removed_repository_id = "10000000-0000-0000-0000-000000000403"
    other_workspace_repository_id = "10000000-0000-0000-0000-000000000404"
    other_workspace_id = "20000000-0000-0000-0000-000000000402"
    with isolated_source_control_rw_engine.begin() as db:
        repository = SqlAlchemySourceControlRepository(db)
        _insert_repository(repository, project_path="platform/zeta")
        _insert_repository(
            repository,
            repository_id=second_repository_id,
            project_id="102",
            project_path="platform/alpha",
        )
        _insert_repository(
            repository,
            repository_id=removed_repository_id,
            project_id="103",
            project_path="platform/removed",
            status="REMOVED",
        )
        _insert_repository(
            repository,
            repository_id=other_workspace_repository_id,
            workspace_id=other_workspace_id,
            project_id="104",
            project_path="platform/other-workspace",
        )

        rows = repository.authorized_repositories(WORKSPACE_ID)

    assert [str(row["id"]) for row in rows] == [second_repository_id, REPOSITORY_ID]
    assert [row["project_path"] for row in rows] == ["platform/alpha", "platform/zeta"]
    assert all(set(row) == {"id", "provider", "project_path", "default_branch"} for row in rows)


def test_repository_registration_and_removal_use_revision_cas(
    isolated_source_control_rw_engine: Engine,
) -> None:
    with isolated_source_control_rw_engine.begin() as db:
        repository = SqlAlchemySourceControlRepository(db)
        _insert_repository(repository)

        registered = repository.workspace_repository(REPOSITORY_ID)
        removed = repository.remove_workspace_repository(
            REPOSITORY_ID,
            expected_revision=1,
            now=NOW + timedelta(minutes=1),
        )
        stale = repository.remove_workspace_repository(
            REPOSITORY_ID,
            expected_revision=1,
            now=NOW + timedelta(minutes=2),
        )

    assert str(registered["workspace_id"]) == WORKSPACE_ID
    assert removed["status"] == "REMOVED"
    assert removed["revision"] == 2
    assert stale is None


def test_repository_claims_inbox_and_unknown_effects_with_cas(
    isolated_source_control_rw_engine: Engine,
) -> None:
    with isolated_source_control_rw_engine.begin() as db:
        repository = SqlAlchemySourceControlRepository(db)
        _insert_repository(repository)
        repository.accept_binding_request(
            message_id=MESSAGE_ID,
            payload_hash="sha256:request",
            requirement_id=REQUIREMENT_ID,
            requirement_version=1,
            work_item_id=WORK_ITEM_ID,
            repository_id=REPOSITORY_ID,
            now=NOW,
        )
        claimed = repository.claim_binding_requests(
            limit=10,
            now=NOW,
            lease_until=NOW + timedelta(minutes=5),
        )
        number = repository.next_work_item_number()
        repository.insert_effect(
            id=EFFECT_ID,
            effect_key=f"create:{WORK_ITEM_ID}",
            operation="CREATE_TASK_BRANCH",
            work_item_id=WORK_ITEM_ID,
            requirement_id=REQUIREMENT_ID,
            repository_id=REPOSITORY_ID,
            work_item_number=number,
            branch_name=f"feat/wi-{number}-source-control",
            base_commit_sha="a" * 40,
            request_fingerprint="sha256:fingerprint",
            attempts=0,
            state="UNKNOWN",
            requirement_callback_state="PENDING",
            next_reconcile_at=NOW,
            now=NOW,
        )
        reconciled = repository.claim_unknown_effects(
            limit=10,
            now=NOW,
            lease_until=NOW + timedelta(minutes=5),
        )

    assert [str(row["message_id"]) for row in claimed] == [MESSAGE_ID]
    assert claimed[0]["state"] == "PROCESSING"
    assert claimed[0]["attempts"] == 1
    assert [str(row["id"]) for row in reconciled] == [EFFECT_ID]
    assert reconciled[0]["state"] == "RECONCILIATION"


def test_expired_inbox_and_effect_leases_are_recoverable(
    isolated_source_control_rw_engine: Engine,
) -> None:
    with isolated_source_control_rw_engine.begin() as db:
        repository = SqlAlchemySourceControlRepository(db)
        _insert_repository(repository)
        repository.accept_binding_request(
            message_id=MESSAGE_ID,
            payload_hash="sha256:request",
            requirement_id=REQUIREMENT_ID,
            requirement_version=1,
            work_item_id=WORK_ITEM_ID,
            repository_id=REPOSITORY_ID,
            now=NOW,
        )
        repository.claim_binding_request(
            MESSAGE_ID,
            now=NOW,
            lease_until=NOW,
        )
        repository.insert_effect(
            id=EFFECT_ID,
            effect_key=f"create:{WORK_ITEM_ID}",
            operation="CREATE_TASK_BRANCH",
            work_item_id=WORK_ITEM_ID,
            requirement_id=REQUIREMENT_ID,
            repository_id=REPOSITORY_ID,
            work_item_number=401,
            branch_name="feat/wi-401-source-control",
            base_commit_sha="a" * 40,
            request_fingerprint="sha256:fingerprint",
            attempts=1,
            state="IN_FLIGHT",
            requirement_callback_state="PENDING",
            next_reconcile_at=NOW,
            now=NOW,
        )

        pending = repository.pending_binding_request_ids(limit=10, now=NOW)
        first_recovery = repository.claim_unknown_effects(
            limit=10,
            now=NOW,
            lease_until=NOW,
        )
        second_recovery = repository.claim_unknown_effects(
            limit=10,
            now=NOW,
            lease_until=NOW + timedelta(minutes=2),
        )

    assert pending == [MESSAGE_ID]
    assert [str(row["id"]) for row in first_recovery] == [EFFECT_ID]
    assert [str(row["id"]) for row in second_recovery] == [EFFECT_ID]
    assert second_recovery[0]["state"] == "RECONCILIATION"


def test_repository_persists_immutable_binding_and_deduplicated_webhook(
    isolated_source_control_rw_engine: Engine,
) -> None:
    with isolated_source_control_rw_engine.begin() as db:
        repository = SqlAlchemySourceControlRepository(db)
        _insert_repository(repository)
        repository.insert_effect(
            id=EFFECT_ID,
            effect_key=f"create:{WORK_ITEM_ID}",
            operation="CREATE_TASK_BRANCH",
            work_item_id=WORK_ITEM_ID,
            requirement_id=REQUIREMENT_ID,
            repository_id=REPOSITORY_ID,
            work_item_number=401,
            branch_name="feat/wi-401-source-control",
            base_commit_sha="a" * 40,
            request_fingerprint="sha256:fingerprint",
            attempts=0,
            state="IN_FLIGHT",
            requirement_callback_state="PENDING",
            next_reconcile_at=None,
            now=NOW,
        )
        succeeded = repository.transition_effect(
            EFFECT_ID,
            expected_state="IN_FLIGHT",
            values={
                "state": "SUCCEEDED",
                "completed_at": NOW,
                "updated_at": NOW,
            },
        )
        repository.insert_binding(
            id="70000000-0000-0000-0000-000000000401",
            work_item_id=WORK_ITEM_ID,
            requirement_id=REQUIREMENT_ID,
            workspace_id=WORKSPACE_ID,
            repository_id=REPOSITORY_ID,
            work_item_number=401,
            base_commit_sha="a" * 40,
            branch_name="feat/wi-401-source-control",
            effect_id=EFFECT_ID,
            now=NOW,
        )
        first_webhook = repository.accept_webhook(
            id="80000000-0000-0000-0000-000000000401",
            repository_id=REPOSITORY_ID,
            webhook_id="webhook-401",
            webhook_timestamp=NOW,
            payload_digest="sha256:webhook",
            provider_event_uuid=None,
            event_type="Push Hook",
            object_kind="push",
            project_id="101",
            ref="refs/heads/feat/wi-401-source-control",
            before_sha="b" * 40,
            after_sha="a" * 40,
            checkout_sha="a" * 40,
            now=NOW,
        )
        duplicate_webhook = repository.accept_webhook(
            id="80000000-0000-0000-0000-000000000402",
            repository_id=REPOSITORY_ID,
            webhook_id="webhook-401",
            webhook_timestamp=NOW,
            payload_digest="sha256:webhook",
            provider_event_uuid=None,
            event_type="Push Hook",
            object_kind="push",
            project_id="101",
            ref="refs/heads/feat/wi-401-source-control",
            before_sha="b" * 40,
            after_sha="a" * 40,
            checkout_sha="a" * 40,
            now=NOW,
        )
        binding = repository.binding_by_work_item(WORK_ITEM_ID)

    assert succeeded["state"] == "SUCCEEDED"
    assert str(binding["effect_id"]) == EFFECT_ID
    assert first_webhook is not None
    assert duplicate_webhook is None
