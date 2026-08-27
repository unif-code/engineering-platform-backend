import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from cryptography.exceptions import InvalidTag
from pydantic import ValidationError

from control_plane.app.modules.requirement.application.common import (
    actor_id,
    audit,
    requirement_dto,
)
from control_plane.app.modules.requirement.application.delivery import (
    WorkItemDeliveryConflict,
    WorkItemDeliveryResult,
    _delivery_dto,
)
from control_plane.app.modules.requirement.application.dependencies import (
    RequirementDependencies,
)
from control_plane.app.modules.requirement.domain import (
    IntegrationDeliveryBlockedReason,
    IntegrationDeliveryContext,
    IntegrationDeliveryRequestKind,
    IntegrationDeliveryRequestMessage,
    IntegrationDeliveryState,
    InvalidRequirementInput,
    RepositoryState,
    RequirementError,
    RequirementNotFound,
    RequirementState,
    StaleRequirementRevision,
    StaleWorkItemRevision,
    WorkItemNotFound,
    WorkItemState,
    transition_integration_mr_ready,
)
from control_plane.app.modules.requirement.ports import RequirementRepository
from control_plane.app.shared.idempotency import (
    IdempotentResponse,
    SealedIdempotentEnvelope,
    canonical_request_fingerprint,
    execute_idempotent,
)
from control_plane.app.shared.security import unseal

_CREATE_TOPIC = "requirement.integration-merge-request.requested"
_MERGE_TOPIC = "requirement.integration-merge.requested"
_TOPIC_KINDS = {
    _CREATE_TOPIC: IntegrationDeliveryRequestKind.CREATE_MR,
    _MERGE_TOPIC: IntegrationDeliveryRequestKind.MERGE_MR,
}
_DELIVERY_COMMANDS = {
    IntegrationDeliveryRequestKind.CREATE_MR: (
        "requirement_request_integration_merge_request",
        "requirement.request-integration-merge-request",
        _CREATE_TOPIC,
    ),
    IntegrationDeliveryRequestKind.MERGE_MR: (
        "requirement_request_integration_merge",
        "requirement.request-integration-merge",
        _MERGE_TOPIC,
    ),
}
_DELIVERY_RELEASE_ERROR_CODES = frozenset(
    {
        "DELIVERY_REQUEST_CONFLICT",
        "DELIVERY_REQUEST_INVALID",
        "SOURCE_CONTROL_UNAVAILABLE",
    }
)


class IntegrationDeliveryMessageInvalid(RequirementError):
    pass


class IntegrationDeliveryRequestMissing(RequirementError):
    pass


def _payload_hash(payload: dict[str, object]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _expected_payload(
    result: WorkItemDeliveryResult,
    *,
    kind: IntegrationDeliveryRequestKind,
    actor: str,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": kind.value,
        "requirementId": result.requirement.id,
        "requirementRevision": result.requirement.revision,
        "workItemId": result.work_item.id,
        "workItemRevision": result.work_item.revision,
    }
    if kind is IntegrationDeliveryRequestKind.MERGE_MR:
        payload["integrationMergeRequestBindingId"] = (
            result.work_item.integration_merge_request_binding_id
        )
    payload.update(
        {
            "repositoryId": result.work_item.repository_id,
            "actorId": actor,
        }
    )
    return payload


def _bound_payload_hash(
    repository: RequirementRepository,
    *,
    row: Any,
    payload: dict[str, object],
    kind: IntegrationDeliveryRequestKind,
    dependencies: RequirementDependencies,
) -> str:
    operation, path, topic = _DELIVERY_COMMANDS[kind]
    requirement_revision = payload["requirementRevision"]
    request_actor = payload["actorId"]
    if type(requirement_revision) is not int or not isinstance(request_actor, str):
        raise IntegrationDeliveryMessageInvalid(str(row["id"]))
    prior_requirement_revision = requirement_revision - 1
    if prior_requirement_revision < 1:
        raise IntegrationDeliveryMessageInvalid(str(row["id"]))
    material = dependencies.secret_manager.load()
    request_fingerprint = canonical_request_fingerprint(
        operation=operation,
        method="COMMAND",
        path=path,
        body={
            "requirementId": payload["requirementId"],
            "workItemId": payload["workItemId"],
            "expectedRevision": prior_requirement_revision,
        },
        idempotency_sealing_key=material.idempotency_sealing_key,
    )
    records = repository.completed_idempotency_by_fingerprint(
        request_actor,
        operation,
        request_fingerprint,
    )
    current_hash = _payload_hash(payload)
    for record in records:
        try:
            envelope = SealedIdempotentEnvelope.model_validate_json(
                unseal(record["sealed_response"], material.idempotency_sealing_key)
            )
            result = WorkItemDeliveryResult.model_validate(envelope.response.body)
        except (InvalidTag, TypeError, UnicodeError, ValueError, ValidationError):
            continue
        if (
            record["result_metadata"] != {"kind": "http-response", "schemaVersion": 1}
            or record["http_status"] != 202
            or envelope.actor != record["actor"]
            or envelope.operation != record["operation"]
            or envelope.idempotency_key != record["idempotency_key"]
            or envelope.request_fingerprint != record["request_fingerprint"]
            or envelope.response.status_code != record["http_status"]
            or result.outbox_topic != topic
        ):
            continue
        original_payload = _expected_payload(result, kind=kind, actor=envelope.actor)
        original_hash = _payload_hash(original_payload)
        if original_hash == current_hash and original_payload == payload:
            return original_hash
    raise IntegrationDeliveryMessageInvalid(str(row["id"]))


def _message(
    repository: RequirementRepository,
    row: Any,
    *,
    dependencies: RequirementDependencies,
) -> IntegrationDeliveryRequestMessage:
    kind = _TOPIC_KINDS.get(row["topic"])
    payload = row["payload"]
    if kind is None or row["aggregate_type"] != "REQUIREMENT" or not isinstance(payload, dict):
        raise IntegrationDeliveryMessageInvalid(str(row["id"]))
    common_fields = {
        "kind",
        "requirementId",
        "requirementRevision",
        "workItemId",
        "workItemRevision",
        "repositoryId",
        "actorId",
    }
    expected_fields = (
        common_fields
        if kind is IntegrationDeliveryRequestKind.CREATE_MR
        else common_fields | {"integrationMergeRequestBindingId"}
    )
    string_fields = {"requirementId", "workItemId", "repositoryId", "actorId"}
    if kind is IntegrationDeliveryRequestKind.MERGE_MR:
        string_fields.add("integrationMergeRequestBindingId")
    if (
        set(payload) != expected_fields
        or payload.get("kind") != kind.value
        or any(
            not isinstance(payload.get(field), str) or not payload[field].strip()
            for field in string_fields
        )
        or type(payload.get("requirementRevision")) is not int
        or payload["requirementRevision"] < 1
        or type(payload.get("workItemRevision")) is not int
        or payload["workItemRevision"] < 1
        or payload["requirementRevision"] != row["aggregate_version"]
        or payload["requirementId"] != str(row["aggregate_id"])
    ):
        raise IntegrationDeliveryMessageInvalid(str(row["id"]))
    bound_payload_hash = _bound_payload_hash(
        repository,
        row=row,
        payload=payload,
        kind=kind,
        dependencies=dependencies,
    )
    try:
        return IntegrationDeliveryRequestMessage(
            message_id=str(row["id"]),
            payload_hash=bound_payload_hash,
            requirement_id=payload["requirementId"],
            requirement_revision=payload["requirementRevision"],
            work_item_id=payload["workItemId"],
            work_item_revision=payload["workItemRevision"],
            repository_id=payload["repositoryId"],
            actor_id=payload["actorId"],
            kind=kind,
            integration_merge_request_binding_id=payload.get("integrationMergeRequestBindingId"),
            attempts=row["attempts"],
        )
    except ValidationError:
        raise IntegrationDeliveryMessageInvalid(str(row["id"])) from None


def claim_integration_delivery_requests(
    repository: RequirementRepository,
    *,
    limit: int,
    available_before: datetime,
    lease_until: datetime,
    dependencies: RequirementDependencies,
) -> tuple[IntegrationDeliveryRequestMessage, ...]:
    if not 1 <= limit <= 100 or lease_until <= available_before:
        raise InvalidRequirementInput("integration delivery lease is invalid")
    return tuple(
        _message(repository, row, dependencies=dependencies)
        for row in repository.claim_delivery_requests(
            limit=limit,
            available_before=available_before,
            lease_until=lease_until,
        )
    )


def acknowledge_integration_delivery_request(
    repository: RequirementRepository,
    *,
    message_id: str,
    consumer: str,
    dependencies: RequirementDependencies,
) -> None:
    if consumer != "SOURCE_CONTROL":
        raise InvalidRequirementInput("integration delivery consumer is invalid")
    message = repository.outbox_by_id(message_id, for_update=True)
    if message is None or message["topic"] not in _TOPIC_KINDS:
        raise IntegrationDeliveryRequestMissing(message_id)
    _message(repository, message, dependencies=dependencies)
    if message["state"] == "PUBLISHED":
        return
    if repository.publish_outbox(message_id, now=dependencies.clock.now()) is None:
        raise IntegrationDeliveryRequestMissing(message_id)


def release_integration_delivery_request(
    repository: RequirementRepository,
    *,
    message_id: str,
    error_code: str,
    available_at: datetime,
    dependencies: RequirementDependencies,
) -> None:
    if error_code not in _DELIVERY_RELEASE_ERROR_CODES:
        raise InvalidRequirementInput("integration delivery release error code is invalid")
    message = repository.outbox_by_id(message_id, for_update=True)
    if message is None or message["topic"] not in _TOPIC_KINDS:
        raise IntegrationDeliveryRequestMissing(message_id)
    _message(repository, message, dependencies=dependencies)
    if message["state"] == "PUBLISHED":
        return
    released = repository.release_outbox(
        message_id,
        error_code=error_code,
        available_at=available_at,
    )
    if released is None:
        raise IntegrationDeliveryRequestMissing(message_id)


def get_integration_delivery_context(
    repository: RequirementRepository,
    *,
    work_item_id: str,
) -> IntegrationDeliveryContext:
    row = repository.integration_delivery_context(work_item_id)
    if row is None:
        raise WorkItemNotFound(work_item_id)
    if not row["request_actor_id"]:
        raise IntegrationDeliveryMessageInvalid(work_item_id)
    return IntegrationDeliveryContext(
        requirement_id=str(row["requirement_id"]),
        requirement_revision=row["requirement_revision"],
        requirement_state=RequirementState(row["requirement_state"]),
        workspace_id=str(row["workspace_id"]),
        work_item_id=str(row["work_item_id"]),
        work_item_revision=row["work_item_revision"],
        work_item_state=WorkItemState(row["work_item_state"]),
        repository_id=row["repository_id"],
        repository_state=RepositoryState(row["repository_state"]),
        human_owner_id=row["human_owner_id"],
        required_capabilities=tuple(row["required_capabilities"]),
        base_commit_sha=row["base_commit_sha"],
        task_branch=row["task_branch"],
        integration_delivery_state=IntegrationDeliveryState(row["integration_delivery_state"]),
        integration_merge_request_binding_id=(
            None
            if row["integration_merge_request_binding_id"] is None
            else str(row["integration_merge_request_binding_id"])
        ),
        request_actor_id=row["request_actor_id"],
    )


def _normalized_binding_id(binding_id: str | None) -> str | None:
    if binding_id is None:
        return None
    try:
        return str(UUID(binding_id))
    except (AttributeError, ValueError):
        raise InvalidRequirementInput("integration merge request binding id is invalid") from None


def _locked_subject(
    repository: RequirementRepository,
    *,
    work_item_id: str,
) -> tuple[Any, Any]:
    subject = repository.work_item_by_id(work_item_id)
    if subject is None:
        raise WorkItemNotFound(work_item_id)
    requirement_id = str(subject["requirement_id"])
    requirement = repository.requirement_by_id(requirement_id, for_update=True)
    if requirement is None:
        raise RequirementNotFound(requirement_id)
    work_item = repository.work_item_by_id(work_item_id, for_update=True)
    if work_item is None or str(work_item["requirement_id"]) != requirement_id:
        raise WorkItemNotFound(work_item_id)
    return requirement, work_item


def _callback_result(requirement: Any, work_item: Any) -> WorkItemDeliveryResult:
    return WorkItemDeliveryResult(
        requirement=requirement_dto(requirement),
        work_item=_delivery_dto(work_item),
    )


def _record_callback(
    repository: RequirementRepository,
    *,
    operation: str,
    work_item_id: str,
    binding_id: str | None,
    reason_code: IntegrationDeliveryBlockedReason | None,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
    command: Callable[[Any, Any, str | None, str, datetime], WorkItemDeliveryResult],
) -> WorkItemDeliveryResult:
    stable_actor = actor_id(actor)
    stable_binding_id = _normalized_binding_id(binding_id)
    requirement, work_item = _locked_subject(repository, work_item_id=work_item_id)
    material = dependencies.secret_manager.load()
    body: dict[str, object] = {
        "workItemId": work_item_id,
        "bindingId": stable_binding_id,
        "expectedRevision": expected_revision,
    }
    if reason_code is not None:
        body["reasonCode"] = reason_code.value
    fingerprint = canonical_request_fingerprint(
        operation=operation,
        method="COMMAND",
        path=f"requirement.{operation.replace('_', '-')}",
        body=body,
        idempotency_sealing_key=material.idempotency_sealing_key,
    )

    def run() -> IdempotentResponse:
        if work_item["revision"] != expected_revision:
            raise StaleWorkItemRevision(work_item_id)
        result = command(
            requirement,
            work_item,
            stable_binding_id,
            stable_actor,
            dependencies.clock.now(),
        )
        return IdempotentResponse(status_code=200, body=result.model_dump(mode="json"))

    execution = execute_idempotent(
        repository,
        actor=stable_actor,
        operation=operation,
        key=idempotency_key,
        fingerprint=fingerprint,
        command=run,
        now=dependencies.clock.now,
        new_id=dependencies.random.uuid4,
        idempotency_sealing_key=material.idempotency_sealing_key,
    )
    return WorkItemDeliveryResult.model_validate(execution.response.body)


def _update_projection(
    repository: RequirementRepository,
    *,
    requirement: Any,
    work_item: Any,
    state: WorkItemState,
    delivery_state: IntegrationDeliveryState,
    binding_id: str | None,
    blocked_reason: IntegrationDeliveryBlockedReason | None,
    actor: str,
    operation: str,
    now: datetime,
    dependencies: RequirementDependencies,
    advance_requirement: bool = False,
) -> WorkItemDeliveryResult:
    current_delivery = IntegrationDeliveryState(work_item["integration_delivery_state"])
    if (
        current_delivery is IntegrationDeliveryState.INTEGRATED
        and delivery_state is not current_delivery
    ):
        raise WorkItemDeliveryConflict("Integrated delivery cannot regress")
    updated_work_item = repository.update_work_item_delivery(
        str(work_item["id"]),
        expected_revision=work_item["revision"],
        state=state.value,
        delivery_state=delivery_state.value,
        binding_id=binding_id,
        blocked_reason=None if blocked_reason is None else blocked_reason.value,
        now=now,
    )
    if updated_work_item is None:
        raise StaleWorkItemRevision(str(work_item["id"]))
    updated_requirement = requirement
    if advance_requirement:
        required_states = tuple(
            WorkItemState(value)
            for value in repository.required_work_item_states(str(requirement["id"]))
        )
        target = transition_integration_mr_ready(required_states)
        if RequirementState(requirement["state"]) is not target:
            updated_requirement = repository.update_requirement_state(
                str(requirement["id"]),
                expected_revision=requirement["revision"],
                state=target.value,
                now=now,
            )
            if updated_requirement is None:
                raise StaleRequirementRevision(str(requirement["id"]))
    audit(
        repository,
        dependencies=dependencies,
        actor=actor,
        action=f"requirement.integration_delivery.{operation}",
        target_type="WORK_ITEM",
        target_id=str(work_item["id"]),
        reason=(
            f"bindingId={binding_id or 'none'}; "
            f"reasonCode={blocked_reason.value if blocked_reason else 'none'}; "
            f"revision={updated_work_item['revision']}"
        ),
    )
    return _callback_result(updated_requirement, updated_work_item)


def record_integration_mr_ready(
    repository: RequirementRepository,
    *,
    work_item_id: str,
    binding_id: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> WorkItemDeliveryResult:
    def command(
        requirement: Any,
        work_item: Any,
        stable_binding: str | None,
        stable_actor: str,
        now: datetime,
    ) -> WorkItemDeliveryResult:
        current = IntegrationDeliveryState(work_item["integration_delivery_state"])
        current_binding = work_item["integration_merge_request_binding_id"]
        if (
            stable_binding is None
            or current
            not in {
                IntegrationDeliveryState.MR_PENDING,
                IntegrationDeliveryState.BLOCKED,
                IntegrationDeliveryState.RECONCILIATION_PENDING,
            }
            or (current_binding is not None and str(current_binding) != stable_binding)
        ):
            raise WorkItemDeliveryConflict("WorkItem cannot accept this MR-ready callback")
        return _update_projection(
            repository,
            requirement=requirement,
            work_item=work_item,
            state=WorkItemState.VERIFYING,
            delivery_state=IntegrationDeliveryState.MR_OPEN,
            binding_id=stable_binding,
            blocked_reason=None,
            actor=stable_actor,
            operation="mr_ready",
            now=now,
            dependencies=dependencies,
            advance_requirement=True,
        )

    return _record_callback(
        repository,
        operation="requirement_record_integration_mr_ready",
        work_item_id=work_item_id,
        binding_id=binding_id,
        reason_code=None,
        expected_revision=expected_revision,
        actor=actor,
        idempotency_key=idempotency_key,
        dependencies=dependencies,
        command=command,
    )


def _record_delivery_problem(
    repository: RequirementRepository,
    *,
    operation: str,
    delivery_state: IntegrationDeliveryState,
    reason_code: IntegrationDeliveryBlockedReason,
    work_item_id: str,
    binding_id: str | None,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> WorkItemDeliveryResult:
    def command(
        requirement: Any,
        work_item: Any,
        stable_binding: str | None,
        stable_actor: str,
        now: datetime,
    ) -> WorkItemDeliveryResult:
        current = IntegrationDeliveryState(work_item["integration_delivery_state"])
        current_binding = (
            None
            if work_item["integration_merge_request_binding_id"] is None
            else str(work_item["integration_merge_request_binding_id"])
        )
        if (
            current
            not in {
                IntegrationDeliveryState.MR_PENDING,
                IntegrationDeliveryState.MR_OPEN,
                IntegrationDeliveryState.MERGE_PENDING,
                IntegrationDeliveryState.BLOCKED,
                IntegrationDeliveryState.RECONCILIATION_PENDING,
            }
            or (current_binding is not None and stable_binding != current_binding)
            or (
                current_binding is None
                and stable_binding is not None
                and current is IntegrationDeliveryState.MR_PENDING
            )
        ):
            raise WorkItemDeliveryConflict("WorkItem cannot accept this delivery callback")
        return _update_projection(
            repository,
            requirement=requirement,
            work_item=work_item,
            state=WorkItemState(work_item["state"]),
            delivery_state=delivery_state,
            binding_id=current_binding or stable_binding,
            blocked_reason=(
                reason_code if delivery_state is IntegrationDeliveryState.BLOCKED else None
            ),
            actor=stable_actor,
            operation=operation,
            now=now,
            dependencies=dependencies,
        )

    return _record_callback(
        repository,
        operation=f"requirement_record_integration_{operation}",
        work_item_id=work_item_id,
        binding_id=binding_id,
        reason_code=reason_code,
        expected_revision=expected_revision,
        actor=actor,
        idempotency_key=idempotency_key,
        dependencies=dependencies,
        command=command,
    )


def record_integration_delivery_blocked(
    repository: RequirementRepository,
    *,
    work_item_id: str,
    binding_id: str | None,
    reason_code: IntegrationDeliveryBlockedReason,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> WorkItemDeliveryResult:
    if not isinstance(reason_code, IntegrationDeliveryBlockedReason):
        raise InvalidRequirementInput("integration delivery blocked reason is invalid")
    return _record_delivery_problem(
        repository,
        operation="blocked",
        delivery_state=IntegrationDeliveryState.BLOCKED,
        reason_code=reason_code,
        work_item_id=work_item_id,
        binding_id=binding_id,
        expected_revision=expected_revision,
        actor=actor,
        idempotency_key=idempotency_key,
        dependencies=dependencies,
    )


def record_integration_reconciliation_pending(
    repository: RequirementRepository,
    *,
    work_item_id: str,
    binding_id: str | None,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> WorkItemDeliveryResult:
    return _record_delivery_problem(
        repository,
        operation="reconciliation_pending",
        delivery_state=IntegrationDeliveryState.RECONCILIATION_PENDING,
        reason_code=IntegrationDeliveryBlockedReason.RECONCILIATION_PENDING,
        work_item_id=work_item_id,
        binding_id=binding_id,
        expected_revision=expected_revision,
        actor=actor,
        idempotency_key=idempotency_key,
        dependencies=dependencies,
    )


def _record_bound_terminal(
    repository: RequirementRepository,
    *,
    operation: Literal["merged", "external_merge_drift"],
    work_item_id: str,
    binding_id: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> WorkItemDeliveryResult:
    def command(
        requirement: Any,
        work_item: Any,
        stable_binding: str | None,
        stable_actor: str,
        now: datetime,
    ) -> WorkItemDeliveryResult:
        current_binding = (
            None
            if work_item["integration_merge_request_binding_id"] is None
            else str(work_item["integration_merge_request_binding_id"])
        )
        if (
            stable_binding is None
            or stable_binding != current_binding
            or WorkItemState(work_item["state"]) is not WorkItemState.VERIFYING
            or IntegrationDeliveryState(work_item["integration_delivery_state"])
            not in {
                IntegrationDeliveryState.MERGE_PENDING,
                IntegrationDeliveryState.BLOCKED,
                IntegrationDeliveryState.RECONCILIATION_PENDING,
            }
        ):
            raise WorkItemDeliveryConflict("WorkItem cannot accept this terminal callback")
        drift = operation == "external_merge_drift"
        return _update_projection(
            repository,
            requirement=requirement,
            work_item=work_item,
            state=WorkItemState.VERIFYING,
            delivery_state=(
                IntegrationDeliveryState.BLOCKED if drift else IntegrationDeliveryState.INTEGRATED
            ),
            binding_id=stable_binding,
            blocked_reason=(
                IntegrationDeliveryBlockedReason.EXTERNAL_MERGE_DRIFT if drift else None
            ),
            actor=stable_actor,
            operation=operation,
            now=now,
            dependencies=dependencies,
        )

    return _record_callback(
        repository,
        operation=f"requirement_record_integration_{operation}",
        work_item_id=work_item_id,
        binding_id=binding_id,
        reason_code=(
            IntegrationDeliveryBlockedReason.EXTERNAL_MERGE_DRIFT
            if operation == "external_merge_drift"
            else None
        ),
        expected_revision=expected_revision,
        actor=actor,
        idempotency_key=idempotency_key,
        dependencies=dependencies,
        command=command,
    )


def record_integration_merged(
    repository: RequirementRepository,
    *,
    work_item_id: str,
    binding_id: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> WorkItemDeliveryResult:
    return _record_bound_terminal(
        repository,
        operation="merged",
        work_item_id=work_item_id,
        binding_id=binding_id,
        expected_revision=expected_revision,
        actor=actor,
        idempotency_key=idempotency_key,
        dependencies=dependencies,
    )


def record_external_merge_drift(
    repository: RequirementRepository,
    *,
    work_item_id: str,
    binding_id: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> WorkItemDeliveryResult:
    return _record_bound_terminal(
        repository,
        operation="external_merge_drift",
        work_item_id=work_item_id,
        binding_id=binding_id,
        expected_revision=expected_revision,
        actor=actor,
        idempotency_key=idempotency_key,
        dependencies=dependencies,
    )
