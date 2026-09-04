from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from control_plane.app.modules.source_control import (
    EvidenceStale,
    ExternalValidationRequestEnvelope,
    IntegrationBaselineRequestEnvelope,
    RequirementCallbackUnavailable,
    accept_external_validation,
    relay_requirement_evidence_requests,
)
from control_plane.app.modules.source_control.ports import RequirementEvidencePort
from tests.requirement.conftest import (
    IsolatedRequirementDatabase,
    isolated_requirement_database,
    requirement_owner_engine,
)
from tests.source_control.conftest import IsolatedSourceControlDatabase
from tests.source_control.test_v06_e2e import (
    _scenario,
    _submit_external_validation,
)
from tests.source_control.test_v06_evidence_application import (
    BINDING_ID,
    NOW,
    _dependencies,
    _seed_merged_integration,
    _validation,
)

assert isolated_requirement_database and requirement_owner_engine


class FailFirstAcknowledgement:
    def __init__(self, delegate: RequirementEvidencePort) -> None:
        self.delegate = delegate
        self.fail_next_ack = True

    def claim_requests(
        self,
        *,
        limit: int,
        lease_until: datetime,
    ) -> tuple[
        ExternalValidationRequestEnvelope | IntegrationBaselineRequestEnvelope,
        ...,
    ]:
        return self.delegate.claim_requests(limit=limit, lease_until=lease_until)

    def acknowledge_request(self, message_id: str) -> None:
        if self.fail_next_ack:
            self.fail_next_ack = False
            raise RuntimeError("test-only acknowledgement outage")
        self.delegate.acknowledge_request(message_id)

    def release_request(
        self,
        message_id: str,
        *,
        error_code: str,
        retry_at: datetime,
    ) -> None:
        self.delegate.release_request(
            message_id,
            error_code=error_code,
            retry_at=retry_at,
        )


def _append_changed_integration_observation(
    source: IsolatedSourceControlDatabase,
    *,
    binding_id: str,
) -> None:
    with source.owner.begin() as db:
        latest_observed_at = db.execute(
            text(
                "SELECT max(observed_at) FROM source_control.merge_request_observation "
                "WHERE binding_id=:binding_id"
            ),
            {"binding_id": binding_id},
        ).scalar_one()
        db.execute(
            text(
                "INSERT INTO source_control.merge_request_observation "
                "(id, binding_id, head_sha, state, merge_commit_sha, "
                "external_merge_user_id, merged_at, observation_digest, observed_at) "
                "VALUES ('80000000-0000-0000-0000-000000000698', :binding_id, "
                ":head_sha, 'MERGED', :merge_sha, '42', :observed_at, "
                "'sha256:evidence-relay-context-drift', :observed_at)"
            ),
            {
                "binding_id": binding_id,
                "head_sha": "d" * 40,
                "merge_sha": "e" * 40,
                "observed_at": latest_observed_at + timedelta(seconds=1),
            },
        )


def _make_validation_context_current(
    source: IsolatedSourceControlDatabase,
    *,
    binding_id: str,
    target_commit_sha: str,
    merge_commit_sha: str,
) -> None:
    with source.owner.begin() as db:
        latest_observed_at = db.execute(
            text(
                "SELECT max(observed_at) FROM source_control.merge_request_observation "
                "WHERE binding_id=:binding_id"
            ),
            {"binding_id": binding_id},
        ).scalar_one()
        db.execute(
            text(
                "UPDATE source_control.merge_request_binding SET head_sha=:head_sha "
                "WHERE id=:binding_id"
            ),
            {"binding_id": binding_id, "head_sha": target_commit_sha},
        )
        db.execute(
            text(
                "INSERT INTO source_control.merge_request_observation "
                "(id, binding_id, head_sha, state, merge_commit_sha, "
                "external_merge_user_id, merged_at, observation_digest, observed_at) "
                "VALUES ('80000000-0000-0000-0000-000000000697', :binding_id, "
                ":head_sha, 'MERGED', :merge_sha, '42', :observed_at, "
                "'sha256:evidence-relay-context-became-current', :observed_at)"
            ),
            {
                "binding_id": binding_id,
                "head_sha": target_commit_sha,
                "merge_sha": merge_commit_sha,
                "observed_at": latest_observed_at + timedelta(seconds=1),
            },
        )


def test_stale_external_validation_is_terminally_acked_and_safely_audited(
    isolated_requirement_database: IsolatedRequirementDatabase,
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    scenario = _scenario(
        isolated_requirement_database,
        isolated_source_control_database,
        key_suffix="v06-stale-validation-terminal-relay",
    )
    response = _submit_external_validation(
        scenario,
        idempotency_key="v06-stale-validation-terminal",
        if_match=f'"v{scenario.initial_revision}"',
        target_commit_sha="9" * 40,
    )
    assert response.status_code == 200, response.text
    message_id = response.json()["submission"]["messageId"]

    result = relay_requirement_evidence_requests(
        limit=1,
        dependencies=scenario.source_dependencies,
    )
    replay = relay_requirement_evidence_requests(
        limit=1,
        dependencies=scenario.source_dependencies,
    )

    assert result.model_dump() == {"claimed": 1, "accepted": 1, "released": 0}
    assert replay.model_dump() == {"claimed": 0, "accepted": 0, "released": 0}
    with scenario.requirement_database.owner.connect() as db:
        requirement_state, work_item_state, outbox_state, error_code = db.execute(
            text(
                "SELECT requirement.state, work_item.state, message.state, "
                "message.last_error_code FROM requirement.requirement "
                "JOIN requirement.work_item "
                "ON work_item.requirement_id=requirement.id "
                "JOIN requirement.outbox_message AS message "
                "ON message.aggregate_id=requirement.id "
                "WHERE requirement.id=:requirement_id AND work_item.id=:work_item_id "
                "AND message.id=:message_id"
            ),
            {
                "requirement_id": scenario.requirement_id,
                "work_item_id": scenario.work_item_id,
                "message_id": message_id,
            },
        ).one()
    assert (requirement_state, work_item_state) == ("VERIFYING", "VERIFYING")
    assert (outbox_state, error_code) == ("PUBLISHED", None)

    with scenario.source_database.owner.connect() as db:
        fact_count = db.execute(
            text("SELECT count(*) FROM source_control.external_validation_reference")
        ).scalar_one()
        receipt = db.execute(
            text(
                "SELECT outcome, canonical_external_validation_id, rejection_reason_code "
                "FROM source_control.external_validation_receipt "
                "WHERE message_id=:message_id"
            ),
            {"message_id": message_id},
        ).one()
        audit = db.execute(
            text(
                "SELECT result, reason FROM audit.audit_event "
                "WHERE action='source_control.evidence.external_validation_rejected' "
                "AND target_id=:message_id"
            ),
            {"message_id": message_id},
        ).one()
    assert fact_count == 0
    assert tuple(receipt) == ("REJECTED", None, "EVIDENCE_STALE")
    assert tuple(audit) == (
        "DENIED",
        (
            f"reasonCode=EVIDENCE_STALE; workItemId={scenario.work_item_id}; "
            f"bindingId={scenario.integration_binding_id}"
        ),
    )


def test_rejected_receipt_survives_ack_failure_and_prevents_later_acceptance(
    isolated_requirement_database: IsolatedRequirementDatabase,
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    scenario = _scenario(
        isolated_requirement_database,
        isolated_source_control_database,
        key_suffix="v06-stale-receipt-ack-loss",
    )
    stale_target = "9" * 40
    response = _submit_external_validation(
        scenario,
        idempotency_key="v06-stale-receipt-ack-loss",
        if_match=f'"v{scenario.initial_revision}"',
        target_commit_sha=stale_target,
    )
    assert response.status_code == 200, response.text
    message_id = response.json()["submission"]["messageId"]
    requirement = scenario.source_dependencies.requirement_evidence
    assert requirement is not None
    flaky = FailFirstAcknowledgement(requirement)
    replay_dependencies = replace(
        scenario.source_dependencies,
        requirement_evidence=flaky,
    )

    with pytest.raises(RequirementCallbackUnavailable, match="acknowledgement unavailable"):
        relay_requirement_evidence_requests(limit=1, dependencies=replay_dependencies)

    _make_validation_context_current(
        scenario.source_database,
        binding_id=scenario.integration_binding_id,
        target_commit_sha=stale_target,
        merge_commit_sha="c" * 40,
    )
    with scenario.requirement_database.owner.begin() as db:
        db.execute(
            text("UPDATE requirement.outbox_message SET available_at=:now WHERE id=:message_id"),
            {"now": NOW, "message_id": message_id},
        )

    replayed = relay_requirement_evidence_requests(
        limit=1,
        dependencies=replay_dependencies,
    )

    assert replayed.model_dump() == {"claimed": 1, "accepted": 1, "released": 0}
    with scenario.source_database.owner.connect() as db:
        fact_count = db.execute(
            text("SELECT count(*) FROM source_control.external_validation_reference")
        ).scalar_one()
        receipt = db.execute(
            text(
                "SELECT outcome, canonical_external_validation_id, rejection_reason_code "
                "FROM source_control.external_validation_receipt "
                "WHERE message_id=:message_id"
            ),
            {"message_id": message_id},
        ).one()
        audit_counts = db.execute(
            text(
                "SELECT count(*) FILTER (WHERE action="
                "'source_control.evidence.external_validation_rejected'), "
                "count(*) FILTER (WHERE action="
                "'source_control.evidence.external_validation_accepted') "
                "FROM audit.audit_event WHERE target_id=:message_id"
            ),
            {"message_id": message_id},
        ).one()
    assert fact_count == 0
    assert tuple(receipt) == ("REJECTED", None, "EVIDENCE_STALE")
    assert tuple(audit_counts) == (1, 0)


def test_receipt_survives_ack_failure_and_replays_after_context_drift(
    isolated_requirement_database: IsolatedRequirementDatabase,
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    scenario = _scenario(
        isolated_requirement_database,
        isolated_source_control_database,
        key_suffix="v06-validation-receipt-replay",
    )
    first = _submit_external_validation(
        scenario,
        idempotency_key="v06-validation-receipt-first",
        if_match=f'"v{scenario.initial_revision}"',
    )
    assert first.status_code == 200, first.text
    first_message_id = first.json()["submission"]["messageId"]
    assert (
        relay_requirement_evidence_requests(
            limit=1,
            dependencies=scenario.source_dependencies,
        ).accepted
        == 1
    )
    second = _submit_external_validation(
        scenario,
        idempotency_key="v06-validation-receipt-second",
        if_match=first.headers["etag"],
    )
    assert second.status_code == 200, second.text
    second_message_id = second.json()["submission"]["messageId"]
    requirement = scenario.source_dependencies.requirement_evidence
    assert requirement is not None
    flaky = FailFirstAcknowledgement(requirement)
    replay_dependencies = replace(
        scenario.source_dependencies,
        requirement_evidence=flaky,
    )

    with pytest.raises(RequirementCallbackUnavailable, match="acknowledgement unavailable"):
        relay_requirement_evidence_requests(limit=1, dependencies=replay_dependencies)

    with scenario.requirement_database.owner.begin() as db:
        db.execute(
            text("UPDATE requirement.outbox_message SET available_at=:now WHERE id=:message_id"),
            {"now": NOW, "message_id": second_message_id},
        )
    _append_changed_integration_observation(
        scenario.source_database,
        binding_id=scenario.integration_binding_id,
    )

    replayed = relay_requirement_evidence_requests(
        limit=1,
        dependencies=replay_dependencies,
    )

    assert replayed.model_dump() == {"claimed": 1, "accepted": 1, "released": 0}
    with scenario.requirement_database.owner.connect() as db:
        outbox_state = db.execute(
            text("SELECT state FROM requirement.outbox_message WHERE id=:message_id"),
            {"message_id": second_message_id},
        ).scalar_one()
    assert outbox_state == "PUBLISHED"
    with scenario.source_database.owner.connect() as db:
        facts = (
            db.execute(
                text(
                    "SELECT id::text FROM source_control.external_validation_reference ORDER BY id"
                )
            )
            .scalars()
            .all()
        )
        receipts = db.execute(
            text(
                "SELECT message_id::text, canonical_external_validation_id::text "
                "FROM source_control.external_validation_receipt ORDER BY message_id"
            )
        ).all()
        alias_audit_count = db.execute(
            text(
                "SELECT count(*) FROM audit.audit_event "
                "WHERE action='source_control.evidence.external_validation_replayed' "
                "AND target_id=:message_id"
            ),
            {"message_id": second_message_id},
        ).scalar_one()
    assert facts == [first_message_id]
    assert {tuple(row) for row in receipts} == {
        (first_message_id, first_message_id),
        (second_message_id, first_message_id),
    }
    assert alias_audit_count == 1


def test_first_time_stale_message_cannot_bypass_currentness_by_canonical_hash(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    _seed_merged_integration(isolated_source_control_database)
    dependencies = _dependencies(isolated_source_control_database)
    original = _validation(message_id="93000000-0000-0000-0000-000000000691")
    with isolated_source_control_database.runtime.begin() as db:
        accept_external_validation(db, original, dependencies=dependencies)
    _append_changed_integration_observation(
        isolated_source_control_database,
        binding_id=BINDING_ID,
    )
    first_time_stale = original.model_copy(
        update={
            "message_id": "93000000-0000-0000-0000-000000000692",
            "payload_hash": "sha256:" + "9" * 64,
        }
    )

    with pytest.raises(EvidenceStale, match="target commit"):
        with isolated_source_control_database.runtime.begin() as db:
            accept_external_validation(
                db,
                first_time_stale,
                dependencies=dependencies,
            )

    with isolated_source_control_database.owner.connect() as db:
        facts = (
            db.execute(text("SELECT id::text FROM source_control.external_validation_reference"))
            .scalars()
            .all()
        )
        receipts = (
            db.execute(
                text(
                    "SELECT message_id::text FROM source_control.external_validation_receipt "
                    "ORDER BY message_id"
                )
            )
            .scalars()
            .all()
        )
    assert facts == [original.message_id]
    assert receipts == [original.message_id]
