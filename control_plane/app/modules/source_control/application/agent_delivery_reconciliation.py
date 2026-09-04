from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal, cast

from control_plane.app.modules.audit import AuditEnvelope
from control_plane.app.modules.source_control.application.agent_delivery import (
    _agent_delivery_components,
    _append_audit,
    _delivery,
    _fact_payload,
    _is_locally_fenced,
    _record_fenced_observation,
)
from control_plane.app.modules.source_control.application.dependencies import (
    SourceControlDependencies,
)
from control_plane.app.modules.source_control.domain import (
    AgentDeliveryBatchResult,
    AgentDeliveryDto,
    AgentDeliveryFactTopic,
    AgentFenceResult,
    AgentPushBindingRejected,
    AgentPushState,
    AgentRevocationBatchResult,
)
from control_plane.app.modules.source_control.ports import (
    AgentDeliveryPolicyPort,
    AgentDeliveryRepository,
    AgentDeliveryRepositoryFactory,
    AgentPushBrokerPort,
    AgentPushDenied,
    AgentPushResultUnknown,
    BrokerGrantLocator,
    BrokerPushLocator,
)

RevocationState = Literal["PENDING", "UNKNOWN", "SUCCEEDED"]


def _revocation_state(value: object) -> RevocationState:
    normalized = str(value)
    if normalized not in {"PENDING", "UNKNOWN", "SUCCEEDED"}:
        raise AgentPushBindingRejected("Agent delivery revocation state is invalid")
    return cast(RevocationState, normalized)


def _push_locator(row: Mapping[str, Any]) -> BrokerPushLocator:
    return BrokerPushLocator(
        request_id=str(row["id"]),
        attempt_id=str(row["attempt_id"]),
        attempt_generation=int(row["attempt_generation"]),
        repository_id=str(row["repository_id"]),
        branch_name=str(row["branch_name"]),
        expected_remote_head_sha=str(row["expected_remote_head_sha"]),
        target_commit_sha=str(row["target_commit_sha"]),
        content_digest=str(row["content_digest"]),
    )


def _append_attempt_audit(
    repository: AgentDeliveryRepository,
    *,
    dependencies: SourceControlDependencies,
    action: str,
    attempt_id: str,
    correlation_id: str,
    result: str,
    reason: str | None,
) -> None:
    dependencies.audit.append_in_transaction(
        repository.db,
        AuditEnvelope(
            id=str(dependencies.random.uuid4()),
            occurred_at=dependencies.clock.now(),
            actor="SYSTEM:SOURCE_CONTROL",
            actor_type="SYSTEM",
            action=action,
            target_type="AGENT_ATTEMPT",
            target_id=attempt_id,
            result=result,
            reason=reason,
            correlation_id=correlation_id,
        ),
    )


def _finish_reconciliation(
    claimed: Mapping[str, Any],
    *,
    target_state: AgentPushState,
    error_code: str | None,
    remote_head_sha: str | None,
    observed_at: datetime | None,
    next_reconcile_at: datetime | None,
    dependencies: SourceControlDependencies,
    repository_factory: AgentDeliveryRepositoryFactory,
) -> AgentDeliveryDto:
    now = dependencies.clock.now()
    values: dict[str, object] = {
        "state": target_state.value,
        "last_error_code": error_code,
        "next_reconcile_at": next_reconcile_at,
        "updated_at": now,
    }
    if target_state in {AgentPushState.SUCCEEDED, AgentPushState.BLOCKED, AgentPushState.FENCED}:
        values["completed_at"] = now
    if remote_head_sha is not None and observed_at is not None:
        values["remote_head_sha"] = remote_head_sha
        values["observed_at"] = observed_at
    request_id = str(claimed["id"])
    with dependencies.engine.begin() as db:
        repository = repository_factory(db)
        row = repository.transition_agent_push(
            request_id,
            expected_state=AgentPushState.RECONCILIATION.value,
            expected_attempts=int(claimed["attempts"]),
            values=values,
        )
        if row is None:
            current = repository.agent_push_by_id(request_id, for_update=True)
            if current is None:
                raise AgentPushBindingRejected("Agent push reconciliation lost its request")
            return _delivery(current)
        if target_state is AgentPushState.SUCCEEDED:
            if observed_at is None:
                raise AgentPushBindingRejected("Agent push observation is invalid")
            repository.insert_fact(
                id=str(dependencies.random.uuid4()),
                push_request_id=request_id,
                topic=AgentDeliveryFactTopic.CONFIRMED.value,
                payload=_fact_payload(row, observed_at=observed_at, reason_code=None),
                correlation_id=str(row["correlation_id"]),
                occurred_at=observed_at,
            )
        _append_audit(
            repository,
            dependencies=dependencies,
            action={
                AgentPushState.SUCCEEDED: "source_control.agent_push_confirmed",
                AgentPushState.BLOCKED: "source_control.agent_push_blocked",
                AgentPushState.FENCED: "source_control.agent_push_fenced",
                AgentPushState.UNKNOWN: "source_control.agent_push_unknown",
            }[target_state],
            request_id=request_id,
            correlation_id=str(row["correlation_id"]),
            result=(
                "UNKNOWN"
                if target_state is AgentPushState.UNKNOWN
                else "BLOCKED"
                if target_state in {AgentPushState.BLOCKED, AgentPushState.FENCED}
                else "SUCCESS"
            ),
            reason=error_code,
        )
        return _delivery(row)


def _reconcile_one(
    claimed: Mapping[str, Any],
    *,
    dependencies: SourceControlDependencies,
    repository_factory: AgentDeliveryRepositoryFactory,
    broker: AgentPushBrokerPort,
    policy: AgentDeliveryPolicyPort,
) -> AgentDeliveryDto:
    locator = _push_locator(claimed)
    observed_at = dependencies.clock.now()
    try:
        observation = broker.observe(locator)
    except AgentPushResultUnknown:
        return _finish_reconciliation(
            claimed,
            target_state=AgentPushState.UNKNOWN,
            error_code="RESULT_UNKNOWN",
            remote_head_sha=None,
            observed_at=None,
            next_reconcile_at=policy.next_reconcile_at(
                now=observed_at,
                attempts=int(claimed["attempts"]),
            ),
            dependencies=dependencies,
            repository_factory=repository_factory,
        )
    except AgentPushDenied:
        return _finish_reconciliation(
            claimed,
            target_state=AgentPushState.BLOCKED,
            error_code="BROKER_DENIED",
            remote_head_sha=None,
            observed_at=None,
            next_reconcile_at=None,
            dependencies=dependencies,
            repository_factory=repository_factory,
        )
    remote_head = observation.remote_head_sha
    observed_at = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        repository = repository_factory(db)
        fenced = _is_locally_fenced(
            repository,
            attempt_id=locator.attempt_id,
            attempt_generation=locator.attempt_generation,
        )
    if remote_head == locator.target_commit_sha and fenced:
        delivery = _finish_reconciliation(
            claimed,
            target_state=AgentPushState.FENCED,
            error_code="PUSH_OBSERVED_AFTER_FENCE",
            remote_head_sha=remote_head,
            observed_at=observed_at,
            next_reconcile_at=None,
            dependencies=dependencies,
            repository_factory=repository_factory,
        )
        delivery = _record_fenced_observation(
            locator.request_id,
            remote_head_sha=remote_head,
            observed_at=observed_at,
            dependencies=dependencies,
            repository_factory=repository_factory,
        )
        broker.freeze_branch(locator)
        return delivery
    if remote_head == locator.target_commit_sha:
        delivery = _finish_reconciliation(
            claimed,
            target_state=AgentPushState.SUCCEEDED,
            error_code=None,
            remote_head_sha=remote_head,
            observed_at=observed_at,
            next_reconcile_at=None,
            dependencies=dependencies,
            repository_factory=repository_factory,
        )
        if delivery.state is AgentPushState.FENCED:
            delivery = _record_fenced_observation(
                locator.request_id,
                remote_head_sha=remote_head,
                observed_at=observed_at,
                dependencies=dependencies,
                repository_factory=repository_factory,
            )
            broker.freeze_branch(locator)
        return delivery
    if remote_head == locator.expected_remote_head_sha:
        return _finish_reconciliation(
            claimed,
            target_state=AgentPushState.UNKNOWN,
            error_code="RESULT_UNKNOWN",
            remote_head_sha=remote_head,
            observed_at=observed_at,
            next_reconcile_at=policy.next_reconcile_at(
                now=observed_at,
                attempts=int(claimed["attempts"]),
            ),
            dependencies=dependencies,
            repository_factory=repository_factory,
        )
    return _finish_reconciliation(
        claimed,
        target_state=AgentPushState.BLOCKED,
        error_code="REMOTE_HEAD_CONFLICT",
        remote_head_sha=remote_head,
        observed_at=observed_at,
        next_reconcile_at=None,
        dependencies=dependencies,
        repository_factory=repository_factory,
    )


def reconcile_agent_pushes(
    *,
    limit: int,
    dependencies: SourceControlDependencies,
) -> AgentDeliveryBatchResult:
    if limit < 1:
        raise ValueError("Agent push reconciliation limit must be positive")
    repository_factory, _execution, broker, _grants, policy = _agent_delivery_components(
        dependencies
    )
    now = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        claimed = repository_factory(db).claim_reconcilable(
            limit=limit,
            now=now,
            lease_until=policy.next_reconcile_at(now=now, attempts=1),
        )
    deliveries = tuple(
        _reconcile_one(
            row,
            dependencies=dependencies,
            repository_factory=repository_factory,
            broker=broker,
            policy=policy,
        )
        for row in claimed
    )
    return AgentDeliveryBatchResult(
        claimed=len(claimed),
        processed=len(deliveries),
        deliveries=deliveries,
    )


def _grant_locators(rows: list[Any], *, fenced_generation: int) -> tuple[BrokerGrantLocator, ...]:
    coordinates = {(str(row["repository_id"]), str(row["branch_name"])) for row in rows}
    attempt_id = str(rows[0]["attempt_id"]) if rows else ""
    return tuple(
        BrokerGrantLocator(
            attempt_id=attempt_id,
            fenced_generation=fenced_generation,
            repository_id=repository_id,
            branch_name=branch_name,
        )
        for repository_id, branch_name in sorted(coordinates)
    )


def _complete_revocation(
    *,
    attempt_id: str,
    fenced_generation: int,
    expected_state: RevocationState,
    target_state: RevocationState,
    correlation_id: str,
    attempts: int,
    dependencies: SourceControlDependencies,
    repository_factory: AgentDeliveryRepositoryFactory,
    policy: AgentDeliveryPolicyPort,
) -> RevocationState:
    now = dependencies.clock.now()
    next_revoke_at = (
        None
        if target_state == "SUCCEEDED"
        else policy.next_reconcile_at(now=now, attempts=max(attempts, 1))
    )
    with dependencies.engine.begin() as db:
        repository = repository_factory(db)
        row = repository.transition_revocation(
            attempt_id,
            expected_generation=fenced_generation,
            expected_state=expected_state,
            values={
                "revocation_state": target_state,
                "revoke_attempts": attempts,
                "next_revoke_at": next_revoke_at,
                "updated_at": now,
            },
        )
        if row is None:
            current = repository.attempt_fence(attempt_id, for_update=True)
            if current is None:
                raise AgentPushBindingRejected("Agent delivery fence was not found")
            return _revocation_state(current["revocation_state"])
        _append_attempt_audit(
            repository,
            dependencies=dependencies,
            action=(
                "source_control.agent_push_revocation_succeeded"
                if target_state == "SUCCEEDED"
                else "source_control.agent_push_revocation_unknown"
            ),
            attempt_id=attempt_id,
            correlation_id=correlation_id,
            result="SUCCESS" if target_state == "SUCCEEDED" else "UNKNOWN",
            reason=None if target_state == "SUCCEEDED" else "REVOCATION_UNKNOWN",
        )
        return _revocation_state(row["revocation_state"])


def fence_agent_attempt(
    *,
    attempt_id: str,
    fenced_generation: int,
    reason_code: str,
    correlation_id: str,
    dependencies: SourceControlDependencies,
) -> AgentFenceResult:
    if fenced_generation < 1 or not attempt_id.strip() or not reason_code.strip():
        raise AgentPushBindingRejected("Agent delivery fence was rejected")
    repository_factory, _execution, broker, _grants, policy = _agent_delivery_components(
        dependencies
    )
    now = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        repository = repository_factory(db)
        fence = repository.upsert_attempt_fence(
            attempt_id=attempt_id,
            fenced_generation=fenced_generation,
            reason_code=reason_code,
            correlation_id=correlation_id,
            now=now,
            next_revoke_at=now,
        )
        actual_generation = int(fence["fenced_generation"])
        canonical_reason = str(fence["reason_code"])
        canonical_correlation_id = str(fence["correlation_id"])
        rows = repository.fence_open_agent_pushes(
            attempt_id=attempt_id,
            fenced_generation=actual_generation,
            reason_code=canonical_reason,
            now=now,
        )
        all_rows = repository.agent_pushes_for_attempt(
            attempt_id,
            fenced_generation=actual_generation,
        )
        for row in rows:
            _append_audit(
                repository,
                dependencies=dependencies,
                action="source_control.agent_push_fenced",
                request_id=str(row["id"]),
                correlation_id=str(row["correlation_id"]),
                result="BLOCKED",
                reason=canonical_reason,
            )
        _append_attempt_audit(
            repository,
            dependencies=dependencies,
            action="source_control.agent_attempt_fenced",
            attempt_id=attempt_id,
            correlation_id=canonical_correlation_id,
            result="SUCCESS",
            reason=canonical_reason,
        )
        initial_revocation_state = _revocation_state(fence["revocation_state"])
    final_state: RevocationState
    if initial_revocation_state == "SUCCEEDED":
        final_state = initial_revocation_state
    else:
        try:
            for locator in _grant_locators(all_rows, fenced_generation=actual_generation):
                broker.revoke(locator)
        except AgentPushResultUnknown:
            final_state = _complete_revocation(
                attempt_id=attempt_id,
                fenced_generation=actual_generation,
                expected_state=initial_revocation_state,
                target_state="UNKNOWN",
                correlation_id=canonical_correlation_id,
                attempts=1,
                dependencies=dependencies,
                repository_factory=repository_factory,
                policy=policy,
            )
        else:
            final_state = _complete_revocation(
                attempt_id=attempt_id,
                fenced_generation=actual_generation,
                expected_state=initial_revocation_state,
                target_state="SUCCEEDED",
                correlation_id=canonical_correlation_id,
                attempts=1,
                dependencies=dependencies,
                repository_factory=repository_factory,
                policy=policy,
            )
    return AgentFenceResult(
        attempt_id=attempt_id,
        fenced_generation=actual_generation,
        revocation_state=final_state,
        deliveries=tuple(_delivery(row) for row in rows),
    )


def reconcile_agent_revocations(
    *,
    limit: int,
    dependencies: SourceControlDependencies,
) -> AgentRevocationBatchResult:
    if limit < 1:
        raise ValueError("Agent revocation reconciliation limit must be positive")
    repository_factory, _execution, broker, _grants, policy = _agent_delivery_components(
        dependencies
    )
    now = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        claimed = repository_factory(db).claim_due_revocations(
            limit=limit,
            now=now,
            lease_until=policy.next_reconcile_at(now=now, attempts=1),
        )
    succeeded = 0
    unknown = 0
    for fence in claimed:
        attempt_id = str(fence["attempt_id"])
        with dependencies.engine.connect() as db:
            rows = repository_factory(db).agent_pushes_for_attempt(
                attempt_id,
                fenced_generation=int(fence["fenced_generation"]),
            )
        target: RevocationState
        try:
            for locator in _grant_locators(
                rows,
                fenced_generation=int(fence["fenced_generation"]),
            ):
                broker.revoke(locator)
        except AgentPushResultUnknown:
            target = "UNKNOWN"
        else:
            target = "SUCCEEDED"
        completed = _complete_revocation(
            attempt_id=attempt_id,
            fenced_generation=int(fence["fenced_generation"]),
            expected_state="UNKNOWN",
            target_state=target,
            correlation_id=str(fence["correlation_id"]),
            attempts=int(fence["revoke_attempts"]),
            dependencies=dependencies,
            repository_factory=repository_factory,
            policy=policy,
        )
        if completed == "SUCCEEDED":
            succeeded += 1
        elif completed == "UNKNOWN":
            unknown += 1
    return AgentRevocationBatchResult(
        claimed=len(claimed),
        succeeded=succeeded,
        unknown=unknown,
    )
