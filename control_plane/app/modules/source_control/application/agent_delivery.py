from collections.abc import Mapping
from datetime import datetime
from typing import Any, cast

from sqlalchemy import Connection
from sqlalchemy.exc import IntegrityError

from control_plane.app.modules.audit import AuditEnvelope
from control_plane.app.modules.source_control.application.dependencies import (
    SourceControlDependencies,
)
from control_plane.app.modules.source_control.domain import (
    AgentDeliveryDto,
    AgentDeliveryFactPayload,
    AgentDeliveryFactTopic,
    AgentExecutionBindingSnapshot,
    AgentPushBindingRejected,
    AgentPushGrantResult,
    AgentPushIdempotencyConflict,
    AgentPushNotFound,
    AgentPushRequestSpec,
    AgentPushState,
    InvalidAgentPushGrant,
    agent_push_request_fingerprint,
    digest_agent_push_grant,
    validate_agent_push_expiry,
)
from control_plane.app.modules.source_control.ports import (
    AgentDeliveryDependencyUnavailable,
    AgentDeliveryPolicyPort,
    AgentDeliveryRepository,
    AgentDeliveryRepositoryFactory,
    AgentExecutionBindingPort,
    AgentPushBrokerPort,
    AgentPushDenied,
    AgentPushGrantIssuerPort,
    AgentPushHeadConflict,
    AgentPushResultUnknown,
    BrokerPushLocator,
    BrokerPushRequest,
)


def _agent_delivery_components(
    dependencies: SourceControlDependencies,
) -> tuple[
    AgentDeliveryRepositoryFactory,
    AgentExecutionBindingPort,
    AgentPushBrokerPort,
    AgentPushGrantIssuerPort,
    AgentDeliveryPolicyPort,
]:
    values = (
        dependencies.agent_delivery_repository_factory,
        dependencies.agent_execution_bindings,
        dependencies.agent_push_broker,
        dependencies.agent_push_grants,
        dependencies.agent_delivery_policy,
    )
    if any(value is None for value in values):
        raise AgentDeliveryDependencyUnavailable("Agent delivery is unavailable")
    return cast(
        tuple[
            AgentDeliveryRepositoryFactory,
            AgentExecutionBindingPort,
            AgentPushBrokerPort,
            AgentPushGrantIssuerPort,
            AgentDeliveryPolicyPort,
        ],
        values,
    )


def _delivery(row: Mapping[str, Any]) -> AgentDeliveryDto:
    return AgentDeliveryDto(
        id=str(row["id"]),
        attempt_id=str(row["attempt_id"]),
        attempt_generation=int(row["attempt_generation"]),
        requirement_id=str(row["requirement_id"]),
        work_item_id=str(row["work_item_id"]),
        workspace_id=str(row["workspace_id"]),
        repository_id=str(row["repository_id"]),
        branch_binding_id=str(row["branch_binding_id"]),
        branch_name=str(row["branch_name"]),
        expected_remote_head_sha=str(row["expected_remote_head_sha"]),
        target_commit_sha=str(row["target_commit_sha"]),
        content_digest=str(row["content_digest"]),
        artifact_refs=tuple(str(value) for value in row["artifact_refs"]),
        state=AgentPushState(str(row["state"])),
        issued_at=row["issued_at"],
        expires_at=row["expires_at"],
        consumed_at=row["consumed_at"],
        observed_at=row["observed_at"],
        completed_at=row["completed_at"],
        remote_head_sha=row["remote_head_sha"],
        last_error_code=row["last_error_code"],
        correlation_id=str(row["correlation_id"]),
    )


def get_agent_delivery(
    db: Connection,
    *,
    workspace_id: str,
    request_id: str,
    dependencies: SourceControlDependencies,
) -> AgentDeliveryDto:
    repository_factory = dependencies.agent_delivery_repository_factory
    if repository_factory is None:
        raise AgentDeliveryDependencyUnavailable("Agent delivery is unavailable")
    row = repository_factory(db).delivery_for_workspace(request_id, workspace_id)
    if row is None:
        raise AgentPushNotFound("Agent push request was not found")
    return _delivery(row)


def _spec_from_row(row: Mapping[str, Any]) -> AgentPushRequestSpec:
    return AgentPushRequestSpec(
        idempotency_key=str(row["idempotency_key"]),
        correlation_id=str(row["correlation_id"]),
        attempt_id=str(row["attempt_id"]),
        attempt_generation=int(row["attempt_generation"]),
        requirement_id=str(row["requirement_id"]),
        work_item_id=str(row["work_item_id"]),
        workspace_id=str(row["workspace_id"]),
        repository_id=str(row["repository_id"]),
        branch_binding_id=str(row["branch_binding_id"]),
        branch_name=str(row["branch_name"]),
        expected_remote_head_sha=str(row["expected_remote_head_sha"]),
        target_commit_sha=str(row["target_commit_sha"]),
        content_digest=str(row["content_digest"]),
        artifact_refs=tuple(str(value) for value in row["artifact_refs"]),
        expires_at=row["expires_at"],
    )


def _binding_is_valid(
    binding: AgentExecutionBindingSnapshot,
    spec: AgentPushRequestSpec,
    *,
    expected_digest: str | None = None,
) -> bool:
    return binding.matches(spec) and (
        expected_digest is None or binding.execution_binding_digest == expected_digest
    )


def _validate_source_control_binding(
    repository: AgentDeliveryRepository,
    spec: AgentPushRequestSpec,
) -> None:
    workspace_repository = repository.workspace_repository(spec.repository_id, for_update=True)
    branch_binding = repository.branch_binding(spec.branch_binding_id)
    if (
        workspace_repository is None
        or workspace_repository["status"] != "AUTHORIZED"
        or str(workspace_repository["workspace_id"]) != spec.workspace_id
        or branch_binding is None
        or str(branch_binding["work_item_id"]) != spec.work_item_id
        or str(branch_binding["requirement_id"]) != spec.requirement_id
        or str(branch_binding["workspace_id"]) != spec.workspace_id
        or str(branch_binding["repository_id"]) != spec.repository_id
        or str(branch_binding["branch_name"]) != spec.branch_name
    ):
        raise AgentPushBindingRejected("Agent push binding was rejected")


def _is_locally_fenced(
    repository: AgentDeliveryRepository,
    *,
    attempt_id: str,
    attempt_generation: int,
) -> bool:
    fence = repository.attempt_fence(attempt_id, for_update=True)
    return fence is not None and int(fence["fenced_generation"]) >= attempt_generation


def _append_audit(
    repository: AgentDeliveryRepository,
    *,
    dependencies: SourceControlDependencies,
    action: str,
    request_id: str,
    correlation_id: str,
    result: str = "SUCCESS",
    reason: str | None = None,
) -> None:
    dependencies.audit.append_in_transaction(
        repository.db,
        AuditEnvelope(
            id=str(dependencies.random.uuid4()),
            occurred_at=dependencies.clock.now(),
            actor="SYSTEM:SOURCE_CONTROL",
            actor_type="SYSTEM",
            action=action,
            target_type="AGENT_PUSH_REQUEST",
            target_id=request_id,
            result=result,
            reason=reason,
            correlation_id=correlation_id,
        ),
    )


def _idempotent_authorization_result(
    row: Mapping[str, Any],
    *,
    fingerprint: str,
) -> AgentPushGrantResult:
    if str(row["request_fingerprint"]) != fingerprint:
        raise AgentPushIdempotencyConflict("Agent push idempotency conflict")
    return AgentPushGrantResult(
        delivery=_delivery(row),
        raw_grant=None,
        replayed=True,
    )


def authorize_agent_push(
    spec: AgentPushRequestSpec,
    *,
    raw_fencing_token: str,
    dependencies: SourceControlDependencies,
) -> AgentPushGrantResult:
    repository_factory, execution_bindings, _broker, grants, policy = _agent_delivery_components(
        dependencies
    )
    now = dependencies.clock.now()
    validate_agent_push_expiry(
        issued_at=now,
        expires_at=spec.expires_at,
        max_ttl=policy.max_push_grant_ttl(),
    )
    fingerprint = agent_push_request_fingerprint(spec)

    try:
        with dependencies.engine.begin() as db:
            repository = repository_factory(db)
            _validate_source_control_binding(repository, spec)
            execution_binding = execution_bindings.validate(
                spec,
                raw_fencing_token=raw_fencing_token,
            )
            if not _binding_is_valid(execution_binding, spec):
                raise AgentPushBindingRejected("Agent push binding was rejected")
            if _is_locally_fenced(
                repository,
                attempt_id=spec.attempt_id,
                attempt_generation=spec.attempt_generation,
            ):
                raise AgentPushBindingRejected("Agent push binding was rejected")
            existing = repository.agent_push_by_idempotency(
                spec.workspace_id,
                spec.idempotency_key,
                for_update=True,
            )
            if existing is not None:
                return _idempotent_authorization_result(
                    existing,
                    fingerprint=fingerprint,
                )
            issued = grants.issue()
            row = repository.insert_agent_push(
                id=str(dependencies.random.uuid4()),
                idempotency_key=spec.idempotency_key,
                request_fingerprint=fingerprint,
                attempt_id=spec.attempt_id,
                attempt_generation=spec.attempt_generation,
                execution_binding_digest=execution_binding.execution_binding_digest,
                requirement_id=spec.requirement_id,
                work_item_id=spec.work_item_id,
                workspace_id=spec.workspace_id,
                repository_id=spec.repository_id,
                branch_binding_id=spec.branch_binding_id,
                branch_name=spec.branch_name,
                expected_remote_head_sha=spec.expected_remote_head_sha,
                target_commit_sha=spec.target_commit_sha,
                content_digest=spec.content_digest,
                artifact_refs=spec.artifact_refs,
                grant_digest=issued.digest,
                correlation_id=spec.correlation_id,
                state=AgentPushState.AUTHORIZED.value,
                attempts=0,
                issued_at=now,
                expires_at=spec.expires_at,
                next_reconcile_at=None,
                consumed_at=None,
                observed_at=None,
                completed_at=None,
                remote_head_sha=None,
                last_error_code=None,
                created_at=now,
                updated_at=now,
            )
            request_id = str(row["id"])
            _append_audit(
                repository,
                dependencies=dependencies,
                action="source_control.agent_push_authorized",
                request_id=request_id,
                correlation_id=spec.correlation_id,
            )
            delivery = _delivery(row)
        return AgentPushGrantResult(
            delivery=delivery,
            raw_grant=issued.raw,
            replayed=False,
        )
    except IntegrityError:
        with dependencies.engine.connect() as db:
            existing = repository_factory(db).agent_push_by_idempotency(
                spec.workspace_id,
                spec.idempotency_key,
            )
        if existing is None:
            raise AgentPushIdempotencyConflict("Agent push coordinate conflict") from None
        return _idempotent_authorization_result(existing, fingerprint=fingerprint)


def _load_agent_push(
    request_id: str,
    *,
    repository_factory: AgentDeliveryRepositoryFactory,
    dependencies: SourceControlDependencies,
) -> Mapping[str, Any]:
    with dependencies.engine.connect() as db:
        row = repository_factory(db).agent_push_by_id(request_id)
    if row is None:
        raise AgentPushNotFound("Agent push request was not found")
    return cast(Mapping[str, Any], row)


def _finish_inflight(
    request_id: str,
    *,
    consumed: Mapping[str, Any],
    dependencies: SourceControlDependencies,
    repository_factory: AgentDeliveryRepositoryFactory,
    target_state: AgentPushState,
    error_code: str | None,
    action: str,
    remote_head_sha: str | None = None,
    observed_at: datetime | None = None,
    next_reconcile_at: datetime | None = None,
) -> AgentDeliveryDto:
    now = dependencies.clock.now()
    values: dict[str, object] = {
        "state": target_state.value,
        "next_reconcile_at": next_reconcile_at,
        "last_error_code": error_code,
        "updated_at": now,
    }
    if target_state in {AgentPushState.BLOCKED, AgentPushState.FENCED, AgentPushState.SUCCEEDED}:
        values["completed_at"] = now
    if remote_head_sha is not None:
        values["remote_head_sha"] = remote_head_sha
        values["observed_at"] = observed_at or now
    with dependencies.engine.begin() as db:
        repository = repository_factory(db)
        row = repository.transition_agent_push(
            request_id,
            expected_state=AgentPushState.IN_FLIGHT.value,
            expected_attempts=int(consumed["attempts"]),
            values=values,
        )
        if row is None:
            current = repository.agent_push_by_id(request_id, for_update=True)
            if current is None:
                raise AgentPushNotFound("Agent push request was not found")
            return _delivery(current)
        audit_result = (
            "UNKNOWN"
            if target_state is AgentPushState.UNKNOWN
            else "BLOCKED"
            if target_state in {AgentPushState.BLOCKED, AgentPushState.FENCED}
            else "SUCCESS"
        )
        _append_audit(
            repository,
            dependencies=dependencies,
            action=action,
            request_id=request_id,
            correlation_id=str(row["correlation_id"]),
            result=audit_result,
            reason=error_code,
        )
        return _delivery(row)


def _fact_payload(
    row: Mapping[str, Any],
    *,
    observed_at: datetime,
    reason_code: str | None,
) -> dict[str, object]:
    validated = AgentDeliveryFactPayload(
        attempt_id=str(row["attempt_id"]),
        attempt_generation=int(row["attempt_generation"]),
        requirement_id=str(row["requirement_id"]),
        work_item_id=str(row["work_item_id"]),
        workspace_id=str(row["workspace_id"]),
        repository_id=str(row["repository_id"]),
        branch_binding_id=str(row["branch_binding_id"]),
        branch_name=str(row["branch_name"]),
        target_commit_sha=str(row["target_commit_sha"]),
        content_digest=str(row["content_digest"]),
        artifact_refs=tuple(str(value) for value in row["artifact_refs"]),
        executor_type="AGENT",
        correlation_id=str(row["correlation_id"]),
        observed_at=observed_at,
        reason_code=reason_code,
    )
    return {
        "attemptId": validated.attempt_id,
        "attemptGeneration": validated.attempt_generation,
        "requirementId": validated.requirement_id,
        "workItemId": validated.work_item_id,
        "workspaceId": validated.workspace_id,
        "repositoryId": validated.repository_id,
        "branchBindingId": validated.branch_binding_id,
        "branchName": validated.branch_name,
        "targetCommitSha": validated.target_commit_sha,
        "contentDigest": validated.content_digest,
        "artifactRefs": list(validated.artifact_refs),
        "executorType": validated.executor_type,
        "correlationId": validated.correlation_id,
        "observedAt": validated.observed_at.isoformat(),
        **({"reasonCode": validated.reason_code} if validated.reason_code is not None else {}),
    }


def _confirm_inflight(
    request_id: str,
    *,
    consumed: Mapping[str, Any],
    remote_head_sha: str,
    observed_at: datetime,
    dependencies: SourceControlDependencies,
    repository_factory: AgentDeliveryRepositoryFactory,
) -> AgentDeliveryDto:
    now = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        repository = repository_factory(db)
        row = repository.transition_agent_push(
            request_id,
            expected_state=AgentPushState.IN_FLIGHT.value,
            expected_attempts=int(consumed["attempts"]),
            values={
                "state": AgentPushState.SUCCEEDED.value,
                "next_reconcile_at": None,
                "last_error_code": None,
                "observed_at": observed_at,
                "remote_head_sha": remote_head_sha,
                "completed_at": now,
                "updated_at": now,
            },
        )
        if row is None:
            current = repository.agent_push_by_id(request_id, for_update=True)
            if current is None:
                raise AgentPushNotFound("Agent push request was not found")
            return _delivery(current)
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
            action="source_control.agent_push_confirmed",
            request_id=request_id,
            correlation_id=str(row["correlation_id"]),
        )
        return _delivery(row)


def _record_fenced_observation(
    request_id: str,
    *,
    remote_head_sha: str,
    observed_at: datetime,
    dependencies: SourceControlDependencies,
    repository_factory: AgentDeliveryRepositoryFactory,
) -> AgentDeliveryDto:
    now = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        repository = repository_factory(db)
        row = repository.record_fenced_observation(
            request_id,
            remote_head_sha=remote_head_sha,
            observed_at=observed_at,
            reason_code="PUSH_OBSERVED_AFTER_FENCE",
            now=now,
        )
        if row is None:
            current = repository.agent_push_by_id(request_id, for_update=True)
            if current is None:
                raise AgentPushNotFound("Agent push request was not found")
            return _delivery(current)
        if repository.fact_by_request(request_id) is None:
            repository.insert_fact(
                id=str(dependencies.random.uuid4()),
                push_request_id=request_id,
                topic=AgentDeliveryFactTopic.FENCED.value,
                payload=_fact_payload(
                    row,
                    observed_at=observed_at,
                    reason_code="PUSH_OBSERVED_AFTER_FENCE",
                ),
                correlation_id=str(row["correlation_id"]),
                occurred_at=observed_at,
            )
        _append_audit(
            repository,
            dependencies=dependencies,
            action="source_control.agent_push_fenced",
            request_id=request_id,
            correlation_id=str(row["correlation_id"]),
            result="BLOCKED",
            reason="PUSH_OBSERVED_AFTER_FENCE",
        )
        return _delivery(row)


def execute_agent_push(
    request_id: str,
    *,
    raw_grant: str,
    raw_fencing_token: str,
    dependencies: SourceControlDependencies,
) -> AgentDeliveryDto:
    repository_factory, execution_bindings, broker, _grants, policy = _agent_delivery_components(
        dependencies
    )
    initial = _load_agent_push(
        request_id,
        repository_factory=repository_factory,
        dependencies=dependencies,
    )
    if AgentPushState(str(initial["state"])) is not AgentPushState.AUTHORIZED:
        return _delivery(initial)
    spec = _spec_from_row(initial)
    execution_binding = execution_bindings.validate(
        spec,
        raw_fencing_token=raw_fencing_token,
    )
    if not _binding_is_valid(
        execution_binding,
        spec,
        expected_digest=str(initial["execution_binding_digest"]),
    ):
        raise AgentPushBindingRejected("Agent push binding was rejected")
    grant_digest = digest_agent_push_grant(raw_grant)
    now = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        repository = repository_factory(db)
        consumed = repository.consume_agent_push(
            request_id=request_id,
            grant_digest=grant_digest,
            now=now,
            next_reconcile_at=policy.next_reconcile_at(now=now, attempts=1),
        )
        if consumed is None:
            current = repository.agent_push_by_id(request_id, for_update=True)
            if current is None:
                raise AgentPushNotFound("Agent push request was not found")
            if AgentPushState(str(current["state"])) is not AgentPushState.AUTHORIZED:
                return _delivery(current)
            if current["expires_at"] <= now:
                blocked = repository.transition_agent_push(
                    request_id,
                    expected_state=AgentPushState.AUTHORIZED.value,
                    values={
                        "state": AgentPushState.BLOCKED.value,
                        "completed_at": now,
                        "last_error_code": "GRANT_EXPIRED",
                        "next_reconcile_at": None,
                        "updated_at": now,
                    },
                )
                if blocked is None:
                    raise AgentPushBindingRejected("Agent push binding was rejected")
                _append_audit(
                    repository,
                    dependencies=dependencies,
                    action="source_control.agent_push_blocked",
                    request_id=request_id,
                    correlation_id=str(blocked["correlation_id"]),
                    reason="GRANT_EXPIRED",
                )
                return _delivery(blocked)
            if str(current["grant_digest"]) != grant_digest:
                raise InvalidAgentPushGrant("Agent push grant is invalid")
            raise AgentPushBindingRejected("Agent push binding was rejected")
        _append_audit(
            repository,
            dependencies=dependencies,
            action="source_control.agent_push_consumed",
            request_id=request_id,
            correlation_id=str(consumed["correlation_id"]),
        )

    broker_request = BrokerPushRequest(
        request_id=request_id,
        attempt_id=str(consumed["attempt_id"]),
        attempt_generation=int(consumed["attempt_generation"]),
        repository_id=str(consumed["repository_id"]),
        branch_name=str(consumed["branch_name"]),
        expected_remote_head_sha=str(consumed["expected_remote_head_sha"]),
        target_commit_sha=str(consumed["target_commit_sha"]),
        content_digest=str(consumed["content_digest"]),
        execution_binding_digest=str(consumed["execution_binding_digest"]),
    )
    try:
        observation = broker.push_and_verify(broker_request)
    except AgentPushResultUnknown:
        unknown_at = dependencies.clock.now()
        return _finish_inflight(
            request_id,
            consumed=consumed,
            dependencies=dependencies,
            repository_factory=repository_factory,
            target_state=AgentPushState.UNKNOWN,
            error_code="RESULT_UNKNOWN",
            action="source_control.agent_push_unknown",
            next_reconcile_at=policy.next_reconcile_at(
                now=unknown_at,
                attempts=int(consumed["attempts"]),
            ),
        )
    except AgentPushDenied:
        return _finish_inflight(
            request_id,
            consumed=consumed,
            dependencies=dependencies,
            repository_factory=repository_factory,
            target_state=AgentPushState.BLOCKED,
            error_code="BROKER_DENIED",
            action="source_control.agent_push_blocked",
        )
    except AgentPushHeadConflict:
        return _finish_inflight(
            request_id,
            consumed=consumed,
            dependencies=dependencies,
            repository_factory=repository_factory,
            target_state=AgentPushState.BLOCKED,
            error_code="REMOTE_HEAD_CONFLICT",
            action="source_control.agent_push_blocked",
        )
    if observation.remote_head_sha != str(consumed["target_commit_sha"]):
        return _finish_inflight(
            request_id,
            consumed=consumed,
            dependencies=dependencies,
            repository_factory=repository_factory,
            target_state=AgentPushState.BLOCKED,
            error_code="REMOTE_HEAD_CONFLICT",
            action="source_control.agent_push_blocked",
            remote_head_sha=observation.remote_head_sha,
            observed_at=dependencies.clock.now(),
        )

    try:
        post_binding = execution_bindings.validate(
            spec,
            raw_fencing_token=raw_fencing_token,
        )
    except (AgentPushBindingRejected, AgentDeliveryDependencyUnavailable):
        post_binding = None
    with dependencies.engine.begin() as db:
        repository = repository_factory(db)
        fenced = _is_locally_fenced(
            repository,
            attempt_id=spec.attempt_id,
            attempt_generation=spec.attempt_generation,
        )
    observed_at = dependencies.clock.now()
    freeze_locator = BrokerPushLocator(
        request_id=request_id,
        attempt_id=spec.attempt_id,
        attempt_generation=spec.attempt_generation,
        repository_id=spec.repository_id,
        branch_name=spec.branch_name,
        expected_remote_head_sha=spec.expected_remote_head_sha,
        target_commit_sha=spec.target_commit_sha,
        content_digest=spec.content_digest,
    )
    if (
        fenced
        or post_binding is None
        or not _binding_is_valid(
            post_binding,
            spec,
            expected_digest=str(consumed["execution_binding_digest"]),
        )
    ):
        _finish_inflight(
            request_id,
            consumed=consumed,
            dependencies=dependencies,
            repository_factory=repository_factory,
            target_state=AgentPushState.FENCED,
            error_code="PUSH_OBSERVED_AFTER_FENCE",
            action="source_control.agent_push_fenced",
            remote_head_sha=observation.remote_head_sha,
            observed_at=observed_at,
        )
        delivery = _record_fenced_observation(
            request_id,
            remote_head_sha=observation.remote_head_sha,
            observed_at=observed_at,
            dependencies=dependencies,
            repository_factory=repository_factory,
        )
        broker.freeze_branch(freeze_locator)
        return delivery
    delivery = _confirm_inflight(
        request_id,
        consumed=consumed,
        remote_head_sha=observation.remote_head_sha,
        observed_at=observed_at,
        dependencies=dependencies,
        repository_factory=repository_factory,
    )
    if delivery.state is AgentPushState.FENCED:
        delivery = _record_fenced_observation(
            request_id,
            remote_head_sha=observation.remote_head_sha,
            observed_at=observed_at,
            dependencies=dependencies,
            repository_factory=repository_factory,
        )
        broker.freeze_branch(freeze_locator)
    return delivery
