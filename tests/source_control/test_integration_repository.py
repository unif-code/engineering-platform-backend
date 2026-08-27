from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine

from control_plane.app.modules.source_control.adapters import (
    SqlAlchemySourceControlIntegrationRepository,
    SqlAlchemySourceControlRepository,
)
from control_plane.app.modules.source_control.domain import (
    CreateIntegrationMergeRequestEffectPayload,
)

NOW = datetime(2026, 8, 26, 5, 0, tzinfo=UTC)
REPOSITORY_ID = "10000000-0000-0000-0000-000000000501"
WORKSPACE_ID = "20000000-0000-0000-0000-000000000501"
MESSAGE_ID = "30000000-0000-0000-0000-000000000501"
REQUIREMENT_ID = "40000000-0000-0000-0000-000000000501"
WORK_ITEM_ID = "50000000-0000-0000-0000-000000000501"
BRANCH_EFFECT_ID = "60000000-0000-0000-0000-000000000501"
CREATE_MR_EFFECT_ID = "60000000-0000-0000-0000-000000000502"
BRANCH_BINDING_ID = "70000000-0000-0000-0000-000000000501"
MR_BINDING_ID = "71000000-0000-0000-0000-000000000501"
HEAD_SHA = "b" * 40


def _insert_repository(repository: SqlAlchemySourceControlRepository) -> None:
    repository.insert_workspace_repository(
        id=REPOSITORY_ID,
        workspace_id=WORKSPACE_ID,
        provider="GITLAB",
        project_id="101",
        project_path="platform/backend",
        default_branch="main",
        connection_ref="gitlab-dev",
        credential_secret_ref="secret-ref:credential",
        webhook_signing_secret_ref="secret-ref:webhook",
        status="AUTHORIZED",
        revision=1,
        now=NOW,
    )


def _insert_branch_graph(
    branch: SqlAlchemySourceControlRepository,
) -> None:
    branch.insert_effect(
        id=BRANCH_EFFECT_ID,
        effect_key=f"create:{WORK_ITEM_ID}",
        operation="CREATE_TASK_BRANCH",
        work_item_id=WORK_ITEM_ID,
        requirement_id=REQUIREMENT_ID,
        repository_id=REPOSITORY_ID,
        work_item_number=501,
        branch_name="feat/wi-501-integration",
        base_commit_sha="a" * 40,
        request_fingerprint="sha256:branch",
        attempts=0,
        state="IN_FLIGHT",
        requirement_callback_state="ACKED",
        next_reconcile_at=None,
        now=NOW,
    )
    branch.transition_effect(
        BRANCH_EFFECT_ID,
        expected_state="IN_FLIGHT",
        values={"state": "SUCCEEDED", "completed_at": NOW, "updated_at": NOW},
    )
    branch.insert_binding(
        id=BRANCH_BINDING_ID,
        work_item_id=WORK_ITEM_ID,
        requirement_id=REQUIREMENT_ID,
        workspace_id=WORKSPACE_ID,
        repository_id=REPOSITORY_ID,
        work_item_number=501,
        base_commit_sha="a" * 40,
        branch_name="feat/wi-501-integration",
        effect_id=BRANCH_EFFECT_ID,
        now=NOW,
    )


def _insert_create_mr_effect(
    integration: SqlAlchemySourceControlIntegrationRepository,
    *,
    state: str = "UNKNOWN",
    callback_state: str = "PENDING",
) -> None:
    integration.insert_effect(
        id=CREATE_MR_EFFECT_ID,
        effect_key=f"create-integration-mr:{WORK_ITEM_ID}",
        operation="CREATE_INTEGRATION_MR",
        subject_key=f"work-item:{WORK_ITEM_ID}",
        payload=CreateIntegrationMergeRequestEffectPayload(
            branchBindingId=BRANCH_BINDING_ID,
            headSha=HEAD_SHA,
        ),
        work_item_id=WORK_ITEM_ID,
        requirement_id=REQUIREMENT_ID,
        repository_id=REPOSITORY_ID,
        request_fingerprint="sha256:create-mr",
        attempts=0,
        next_reconcile_at=NOW if state == "UNKNOWN" else None,
        state=state,
        requirement_callback_state=callback_state,
        completed_at=NOW if state in {"SUCCEEDED", "BLOCKED"} else None,
        now=NOW,
    )


def test_delivery_inbox_completion_uses_claim_attempt_as_fence(
    isolated_source_control_rw_engine: Engine,
) -> None:
    with isolated_source_control_rw_engine.begin() as db:
        branch = SqlAlchemySourceControlRepository(db)
        integration = SqlAlchemySourceControlIntegrationRepository(db)
        _insert_repository(branch)
        accepted = integration.accept_delivery_request(
            message_id=MESSAGE_ID,
            topic="requirement.integration-merge-request.requested",
            payload_hash="sha256:delivery",
            requirement_id=REQUIREMENT_ID,
            requirement_revision=3,
            work_item_id=WORK_ITEM_ID,
            work_item_revision=5,
            repository_id=REPOSITORY_ID,
            actor_id="employee-1",
            integration_merge_request_binding_id=None,
            now=NOW,
        )
        duplicate = integration.accept_delivery_request(
            message_id=MESSAGE_ID,
            topic="requirement.integration-merge-request.requested",
            payload_hash="sha256:delivery",
            requirement_id=REQUIREMENT_ID,
            requirement_revision=3,
            work_item_id=WORK_ITEM_ID,
            work_item_revision=5,
            repository_id=REPOSITORY_ID,
            actor_id="employee-1",
            integration_merge_request_binding_id=None,
            now=NOW,
        )
        first = integration.claim_delivery_requests(
            limit=10,
            now=NOW,
            lease_until=NOW,
        )[0]
        second = integration.claim_delivery_requests(
            limit=10,
            now=NOW,
            lease_until=NOW + timedelta(minutes=2),
        )[0]
        stale = integration.complete_delivery_request(
            MESSAGE_ID,
            expected_attempts=first["attempts"],
            now=NOW + timedelta(minutes=1),
        )
        completed = integration.complete_delivery_request(
            MESSAGE_ID,
            expected_attempts=second["attempts"],
            now=NOW + timedelta(minutes=1),
        )

    assert accepted is not None
    assert duplicate is None
    assert first["attempts"] == 1
    assert second["attempts"] == 2
    assert stale is None
    assert completed["state"] == "PROCESSED"


def test_exact_claim_preserves_fenced_allowlisted_preflight_outcome(
    isolated_source_control_rw_engine: Engine,
) -> None:
    with isolated_source_control_rw_engine.begin() as db:
        _insert_repository(SqlAlchemySourceControlRepository(db))
        integration = SqlAlchemySourceControlIntegrationRepository(db)
        integration.accept_delivery_request(
            message_id=MESSAGE_ID,
            topic="requirement.integration-merge-request.requested",
            payload_hash="sha256:delivery",
            requirement_id=REQUIREMENT_ID,
            requirement_revision=3,
            work_item_id=WORK_ITEM_ID,
            work_item_revision=5,
            repository_id=REPOSITORY_ID,
            actor_id="employee-1",
            integration_merge_request_binding_id=None,
            now=NOW,
        )
        first = integration.claim_delivery_request(
            MESSAGE_ID,
            now=NOW,
            lease_until=NOW + timedelta(minutes=2),
        )
        marked = integration.record_preflight_outcome(
            MESSAGE_ID,
            expected_attempts=first["attempts"],
            reason_code="OWNER_MISMATCH",
            now=NOW,
        )
        stale = integration.record_preflight_outcome(
            MESSAGE_ID,
            expected_attempts=0,
            reason_code="OWNER_INELIGIBLE",
            now=NOW,
        )
        replay = integration.claim_delivery_request(
            MESSAGE_ID,
            now=NOW + timedelta(minutes=3),
            lease_until=NOW + timedelta(minutes=5),
        )

    assert marked["last_error_code"] == "OWNER_MISMATCH"
    assert stale is None
    assert replay["attempts"] == 2
    assert replay["last_error_code"] == "OWNER_MISMATCH"


def test_preflight_outcome_and_transient_release_reject_unsafe_or_stale_updates(
    isolated_source_control_rw_engine: Engine,
) -> None:
    with isolated_source_control_rw_engine.begin() as db:
        _insert_repository(SqlAlchemySourceControlRepository(db))
        integration = SqlAlchemySourceControlIntegrationRepository(db)
        integration.accept_delivery_request(
            message_id=MESSAGE_ID,
            topic="requirement.integration-merge-request.requested",
            payload_hash="sha256:delivery",
            requirement_id=REQUIREMENT_ID,
            requirement_revision=3,
            work_item_id=WORK_ITEM_ID,
            work_item_revision=5,
            repository_id=REPOSITORY_ID,
            actor_id="employee-1",
            integration_merge_request_binding_id=None,
            now=NOW,
        )
        claimed = integration.claim_delivery_request(
            MESSAGE_ID,
            now=NOW,
            lease_until=NOW + timedelta(minutes=2),
        )
        with pytest.raises(ValueError):
            integration.record_preflight_outcome(
                MESSAGE_ID,
                expected_attempts=claimed["attempts"],
                reason_code="provider body included secret",
                now=NOW,
            )
        stale = integration.release_delivery_request(
            MESSAGE_ID,
            expected_attempts=0,
            error_code="PROVIDER_UNAVAILABLE",
            retry_at=NOW + timedelta(minutes=1),
            now=NOW,
        )
        released = integration.release_delivery_request(
            MESSAGE_ID,
            expected_attempts=claimed["attempts"],
            error_code="PROVIDER_UNAVAILABLE",
            retry_at=NOW + timedelta(minutes=1),
            now=NOW,
        )

    assert stale is None
    assert released["state"] == "FAILED"
    assert released["last_error_code"] == "PROVIDER_UNAVAILABLE"


def test_effect_claims_and_callbacks_are_operation_scoped_and_attempt_fenced(
    isolated_source_control_rw_engine: Engine,
) -> None:
    with isolated_source_control_rw_engine.begin() as db:
        branch = SqlAlchemySourceControlRepository(db)
        integration = SqlAlchemySourceControlIntegrationRepository(db)
        _insert_repository(branch)
        _insert_branch_graph(branch)
        _insert_create_mr_effect(integration)

        branch_lookup = branch.effect_by_work_item(WORK_ITEM_ID)
        create_lookup = integration.effect_by_operation_subject(
            "CREATE_INTEGRATION_MR",
            f"work-item:{WORK_ITEM_ID}",
        )
        first = integration.claim_effects(
            limit=10,
            now=NOW,
            lease_until=NOW,
        )[0]
        second = integration.claim_effects(
            limit=10,
            now=NOW,
            lease_until=NOW + timedelta(minutes=2),
        )[0]
        stale = integration.transition_effect(
            CREATE_MR_EFFECT_ID,
            expected_state="RECONCILIATION",
            expected_attempts=first["attempts"],
            values={"state": "SUCCEEDED", "completed_at": NOW, "updated_at": NOW},
        )
        succeeded = integration.transition_effect(
            CREATE_MR_EFFECT_ID,
            expected_state="RECONCILIATION",
            expected_attempts=second["attempts"],
            values={"state": "SUCCEEDED", "completed_at": NOW, "updated_at": NOW},
        )
        callbacks = integration.pending_callback_effects(limit=10)

    assert str(branch_lookup["id"]) == BRANCH_EFFECT_ID
    assert str(create_lookup["id"]) == CREATE_MR_EFFECT_ID
    assert first["attempts"] == 1
    assert second["attempts"] == 2
    assert stale is None
    assert succeeded["state"] == "SUCCEEDED"
    assert [str(row["id"]) for row in callbacks] == [CREATE_MR_EFFECT_ID]


def test_branch_repository_does_not_return_integration_effect_by_id(
    isolated_source_control_rw_engine: Engine,
) -> None:
    with isolated_source_control_rw_engine.begin() as db:
        branch = SqlAlchemySourceControlRepository(db)
        integration = SqlAlchemySourceControlIntegrationRepository(db)
        _insert_repository(branch)
        _insert_create_mr_effect(integration)

        found = branch.effect_by_id(CREATE_MR_EFFECT_ID)

    assert found is None


def test_branch_repository_does_not_transition_integration_effect(
    isolated_source_control_rw_engine: Engine,
) -> None:
    with isolated_source_control_rw_engine.begin() as db:
        branch = SqlAlchemySourceControlRepository(db)
        integration = SqlAlchemySourceControlIntegrationRepository(db)
        _insert_repository(branch)
        _insert_create_mr_effect(integration)

        transitioned = branch.transition_effect(
            CREATE_MR_EFFECT_ID,
            expected_state="UNKNOWN",
            values={"state": "RECONCILIATION", "updated_at": NOW},
        )
        persisted = integration.effect_by_operation_subject(
            "CREATE_INTEGRATION_MR",
            f"work-item:{WORK_ITEM_ID}",
        )

    assert transitioned is None
    assert persisted["state"] == "UNKNOWN"


def test_repository_appends_observations_and_preserves_immutable_mr_binding(
    isolated_source_control_rw_engine: Engine,
) -> None:
    with isolated_source_control_rw_engine.begin() as db:
        branch = SqlAlchemySourceControlRepository(db)
        integration = SqlAlchemySourceControlIntegrationRepository(db)
        _insert_repository(branch)
        _insert_branch_graph(branch)
        _insert_create_mr_effect(integration, state="SUCCEEDED")
        binding = integration.insert_merge_request_binding(
            id=MR_BINDING_ID,
            kind="INTEGRATION",
            work_item_id=WORK_ITEM_ID,
            requirement_id=REQUIREMENT_ID,
            workspace_id=WORKSPACE_ID,
            repository_id=REPOSITORY_ID,
            branch_binding_id=BRANCH_BINDING_ID,
            external_project_id="101",
            merge_request_iid=42,
            source_branch="feat/wi-501-integration",
            target_branch="dev",
            create_effect_id=CREATE_MR_EFFECT_ID,
            head_sha="b" * 40,
            creation_origin="PLATFORM_CREATED",
            now=NOW,
        )
        first = integration.append_merge_request_observation(
            id="80000000-0000-0000-0000-000000000501",
            binding_id=MR_BINDING_ID,
            head_sha="b" * 40,
            state="OPEN",
            merge_commit_sha=None,
            external_merge_user_id=None,
            merged_at=None,
            observation_digest="sha256:open",
            observed_at=NOW,
        )
        duplicate = integration.append_merge_request_observation(
            id="80000000-0000-0000-0000-000000000502",
            binding_id=MR_BINDING_ID,
            head_sha="b" * 40,
            state="OPEN",
            merge_commit_sha=None,
            external_merge_user_id=None,
            merged_at=None,
            observation_digest="sha256:open",
            observed_at=NOW + timedelta(minutes=1),
        )
        latest = integration.latest_merge_request_observation(MR_BINDING_ID)

    assert str(binding["id"]) == MR_BINDING_ID
    assert first is not None
    assert duplicate is None
    assert str(latest["id"]) == "80000000-0000-0000-0000-000000000501"
