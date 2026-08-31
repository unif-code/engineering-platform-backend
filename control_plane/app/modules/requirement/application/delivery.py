from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from control_plane.app.modules.audit import AuditEnvelope, record
from control_plane.app.modules.requirement.application.common import (
    actor_id,
    audit,
    requirement_dto,
)
from control_plane.app.modules.requirement.application.dependencies import (
    RequirementDependencies,
)
from control_plane.app.modules.requirement.domain import (
    AssignmentState,
    ExecutorType,
    IntegrationDeliveryBlockedReason,
    IntegrationDeliveryState,
    RepositoryState,
    RequirementDto,
    RequirementError,
    RequirementNotFound,
    RequirementState,
    StaleRequirementRevision,
    StaleWorkItemRevision,
    WorkItemNotFound,
    WorkItemState,
    transition_human_work_started,
)
from control_plane.app.modules.requirement.ports import RequirementRepository
from control_plane.app.shared.api.request_id import current_request_id
from control_plane.app.shared.idempotency import (
    IdempotencyConflict,
    IdempotentResponse,
    canonical_request_fingerprint,
    execute_idempotent,
)

_START_OPERATION = "requirement_start_work_item"
_REQUEST_MR_OPERATION = "requirement_request_integration_merge_request"
_REQUEST_MERGE_OPERATION = "requirement_request_integration_merge"
_REQUEST_MR_TOPIC = "requirement.integration-merge-request.requested"
_REQUEST_MERGE_TOPIC = "requirement.integration-merge.requested"


class WorkItemActorDenied(RequirementError):
    pass


class WorkItemDeliveryConflict(RequirementError):
    pass


class WorkItemDeliveryResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    requirement: RequirementDto
    work_item: "WorkItemDeliveryDto"
    outbox_topic: str | None = None


class WorkItemDeliveryDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    requirement_id: str
    human_owner_id: str | None
    assignment_state: AssignmentState
    repository_state: RepositoryState
    state: WorkItemState
    repository_id: str
    integration_delivery_state: IntegrationDeliveryState
    integration_merge_request_binding_id: str | None
    integration_blocked_reason_code: IntegrationDeliveryBlockedReason | None
    integration_updated_at: datetime | None
    revision: int


def _delivery_dto(row: Any) -> WorkItemDeliveryDto:
    return WorkItemDeliveryDto(
        id=str(row["id"]),
        requirement_id=str(row["requirement_id"]),
        human_owner_id=row["human_owner_id"],
        assignment_state=AssignmentState(row["assignment_state"]),
        repository_state=RepositoryState(row["repository_state"]),
        state=WorkItemState(row["state"]),
        repository_id=row["repository_id"],
        integration_delivery_state=IntegrationDeliveryState(row["integration_delivery_state"]),
        integration_merge_request_binding_id=(
            None
            if row["integration_merge_request_binding_id"] is None
            else str(row["integration_merge_request_binding_id"])
        ),
        integration_blocked_reason_code=(
            None
            if row["integration_blocked_reason_code"] is None
            else IntegrationDeliveryBlockedReason(row["integration_blocked_reason_code"])
        ),
        integration_updated_at=row["integration_updated_at"],
        revision=row["revision"],
    )


def _audit_denial(
    *,
    dependencies: RequirementDependencies,
    actor: str,
    action: str,
    work_item_id: str,
    error: Exception,
) -> None:
    record(
        AuditEnvelope(
            id=str(dependencies.random.uuid4()),
            occurred_at=dependencies.clock.now(),
            actor=actor,
            actor_type="HUMAN",
            action=f"{action}_denied",
            target_type="WORK_ITEM",
            target_id=work_item_id,
            result="DENIED",
            reason=f"reasonCode={type(error).__name__.upper()}",
            correlation_id=current_request_id() or str(dependencies.random.uuid4()),
        ),
        dependencies.denial_audit,
    )


def _locked_subject(
    repository: RequirementRepository,
    *,
    requirement_id: str,
    work_item_id: str,
) -> tuple[Any, Any]:
    requirement = repository.requirement_by_id(requirement_id, for_update=True)
    if requirement is None:
        raise RequirementNotFound(requirement_id)
    work_item = repository.work_item_by_id(work_item_id, for_update=True)
    if work_item is None or str(work_item["requirement_id"]) != requirement_id:
        raise WorkItemNotFound(work_item_id)
    return requirement, work_item


def _validate_human_subject(
    work_item: Any,
    *,
    stable_actor: str,
    owner_required: bool,
) -> None:
    if (
        AssignmentState(work_item["assignment_state"]) is not AssignmentState.ASSIGNED
        or ExecutorType(work_item["executor_type"]) is not ExecutorType.HUMAN
        or not work_item["human_owner_id"]
    ):
        raise WorkItemActorDenied("WorkItem has no eligible human owner")
    if owner_required and work_item["human_owner_id"] != stable_actor:
        raise WorkItemActorDenied("Actor is not the current WorkItem owner")
    if RepositoryState(work_item["repository_state"]) is not RepositoryState.BOUND:
        raise WorkItemDeliveryConflict("WorkItem repository is not bound")


def _update_delivery(
    repository: RequirementRepository,
    *,
    work_item: Any,
    state: WorkItemState,
    delivery_state: IntegrationDeliveryState,
    binding_id: str | None,
    now: datetime,
) -> Any:
    updated = repository.update_work_item_delivery(
        str(work_item["id"]),
        expected_revision=work_item["revision"],
        state=state.value,
        delivery_state=delivery_state.value,
        binding_id=binding_id,
        blocked_reason=None,
        now=now,
    )
    if updated is None:
        raise StaleWorkItemRevision(str(work_item["id"]))
    return updated


def _update_requirement(
    repository: RequirementRepository,
    *,
    requirement: Any,
    state: RequirementState,
    now: datetime,
) -> Any:
    updated = repository.update_requirement_state(
        str(requirement["id"]),
        expected_revision=requirement["revision"],
        state=state.value,
        now=now,
    )
    if updated is None:
        raise StaleRequirementRevision(str(requirement["id"]))
    return updated


def _fingerprint(
    *,
    operation: str,
    path: str,
    requirement_id: str,
    work_item_id: str,
    expected_revision: int,
    sealing_key: bytes,
) -> str:
    return canonical_request_fingerprint(
        operation=operation,
        method="COMMAND",
        path=path,
        body={
            "requirementId": requirement_id,
            "workItemId": work_item_id,
            "expectedRevision": expected_revision,
        },
        idempotency_sealing_key=sealing_key,
    )


def _execute_locked(
    repository: RequirementRepository,
    *,
    operation: str,
    path: str,
    status_code: int,
    requirement_id: str,
    work_item_id: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
    owner_required: bool,
    command: Callable[[Any, Any, str], WorkItemDeliveryResult],
) -> WorkItemDeliveryResult:
    stable_actor = actor_id(actor)
    material = dependencies.secret_manager.load()
    requirement, work_item = _locked_subject(
        repository,
        requirement_id=requirement_id,
        work_item_id=work_item_id,
    )
    try:
        _validate_human_subject(
            work_item,
            stable_actor=stable_actor,
            owner_required=owner_required,
        )
        fingerprint = _fingerprint(
            operation=operation,
            path=path,
            requirement_id=requirement_id,
            work_item_id=work_item_id,
            expected_revision=expected_revision,
            sealing_key=material.idempotency_sealing_key,
        )
        existing = repository.idempotency_by_scope(
            stable_actor,
            operation,
            idempotency_key,
        )
        if existing is None and requirement["revision"] != expected_revision:
            raise StaleRequirementRevision(requirement_id)

        def run() -> IdempotentResponse:
            result = command(requirement, work_item, stable_actor)
            return IdempotentResponse(status_code=status_code, body=result.model_dump(mode="json"))

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
    except (RequirementError, IdempotencyConflict) as error:
        _audit_denial(
            dependencies=dependencies,
            actor=stable_actor,
            action=operation,
            work_item_id=work_item_id,
            error=error,
        )
        raise
    return WorkItemDeliveryResult.model_validate(execution.response.body)


def start_work_item(
    repository: RequirementRepository,
    *,
    requirement_id: str,
    work_item_id: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> WorkItemDeliveryResult:
    def command(requirement: Any, work_item: Any, stable_actor: str) -> WorkItemDeliveryResult:
        requirement_state, work_item_state = transition_human_work_started(
            RequirementState(requirement["state"]),
            WorkItemState(work_item["state"]),
        )
        if (
            IntegrationDeliveryState(work_item["integration_delivery_state"])
            is not IntegrationDeliveryState.NOT_STARTED
        ):
            raise WorkItemDeliveryConflict("WorkItem integration delivery already started")
        now = dependencies.clock.now()
        updated_work_item = _update_delivery(
            repository,
            work_item=work_item,
            state=work_item_state,
            delivery_state=IntegrationDeliveryState.IMPLEMENTING,
            binding_id=None,
            now=now,
        )
        updated_requirement = _update_requirement(
            repository,
            requirement=requirement,
            state=requirement_state,
            now=now,
        )
        audit(
            repository,
            dependencies=dependencies,
            actor=stable_actor,
            action="requirement.work_item.started",
            target_type="WORK_ITEM",
            target_id=work_item_id,
            reason=(
                f"requirement={requirement_id}; requirementRevision="
                f"{updated_requirement['revision']}; workItemRevision="
                f"{updated_work_item['revision']}"
            ),
        )
        return WorkItemDeliveryResult(
            requirement=requirement_dto(updated_requirement),
            work_item=_delivery_dto(updated_work_item),
        )

    return _execute_locked(
        repository,
        operation=_START_OPERATION,
        path="requirement.start-work-item",
        status_code=200,
        requirement_id=requirement_id,
        work_item_id=work_item_id,
        expected_revision=expected_revision,
        actor=actor,
        idempotency_key=idempotency_key,
        dependencies=dependencies,
        owner_required=True,
        command=command,
    )


def _request_delivery(
    repository: RequirementRepository,
    *,
    kind: Literal["CREATE_MR", "MERGE_MR"],
    requirement_id: str,
    work_item_id: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> WorkItemDeliveryResult:
    request_merge = kind == "MERGE_MR"
    operation = _REQUEST_MERGE_OPERATION if request_merge else _REQUEST_MR_OPERATION
    topic = _REQUEST_MERGE_TOPIC if request_merge else _REQUEST_MR_TOPIC

    def command(requirement: Any, work_item: Any, stable_actor: str) -> WorkItemDeliveryResult:
        requirement_state = (
            RequirementState.VERIFYING if request_merge else RequirementState.IN_PROGRESS
        )
        work_item_state = WorkItemState.VERIFYING if request_merge else WorkItemState.IN_PROGRESS
        delivery_state = (
            IntegrationDeliveryState.MR_OPEN
            if request_merge
            else IntegrationDeliveryState.IMPLEMENTING
        )
        if (
            RequirementState(requirement["state"]) is not requirement_state
            or WorkItemState(work_item["state"]) is not work_item_state
            or IntegrationDeliveryState(work_item["integration_delivery_state"])
            is not delivery_state
        ):
            raise WorkItemDeliveryConflict("WorkItem is not ready for the delivery request")
        binding_id = (
            None
            if work_item["integration_merge_request_binding_id"] is None
            else str(work_item["integration_merge_request_binding_id"])
        )
        if request_merge and binding_id is None:
            raise WorkItemDeliveryConflict("Integration merge request binding is missing")
        now = dependencies.clock.now()
        updated_work_item = _update_delivery(
            repository,
            work_item=work_item,
            state=work_item_state,
            delivery_state=(
                IntegrationDeliveryState.MERGE_PENDING
                if request_merge
                else IntegrationDeliveryState.MR_PENDING
            ),
            binding_id=binding_id,
            now=now,
        )
        updated_requirement = _update_requirement(
            repository,
            requirement=requirement,
            state=requirement_state,
            now=now,
        )
        payload: dict[str, object] = {
            "kind": kind,
            "requirementId": str(updated_requirement["id"]),
            "requirementRevision": updated_requirement["revision"],
            "workItemId": str(updated_work_item["id"]),
            "workItemRevision": updated_work_item["revision"],
        }
        if request_merge:
            payload["integrationMergeRequestBindingId"] = binding_id
        payload.update(
            {
                "repositoryId": updated_work_item["repository_id"],
                "actorId": stable_actor,
            }
        )
        repository.insert_outbox(
            id=str(dependencies.random.uuid4()),
            topic=topic,
            aggregate_type="REQUIREMENT",
            aggregate_id=requirement_id,
            aggregate_version=updated_requirement["revision"],
            payload=payload,
            now=now,
        )
        audit(
            repository,
            dependencies=dependencies,
            actor=stable_actor,
            action=(
                "requirement.integration_merge.requested"
                if request_merge
                else "requirement.integration_merge_request.requested"
            ),
            target_type="WORK_ITEM",
            target_id=work_item_id,
            reason=(
                f"requirement={requirement_id}; repository={updated_work_item['repository_id']}; "
                f"requirementRevision={updated_requirement['revision']}; "
                f"workItemRevision={updated_work_item['revision']}"
            ),
        )
        return WorkItemDeliveryResult(
            requirement=requirement_dto(updated_requirement),
            work_item=_delivery_dto(updated_work_item),
            outbox_topic=topic,
        )

    return _execute_locked(
        repository,
        operation=operation,
        path=(
            "requirement.request-integration-merge"
            if request_merge
            else "requirement.request-integration-merge-request"
        ),
        status_code=202,
        requirement_id=requirement_id,
        work_item_id=work_item_id,
        expected_revision=expected_revision,
        actor=actor,
        idempotency_key=idempotency_key,
        dependencies=dependencies,
        owner_required=not request_merge,
        command=command,
    )


def request_integration_merge_request(
    repository: RequirementRepository,
    *,
    requirement_id: str,
    work_item_id: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> WorkItemDeliveryResult:
    return _request_delivery(
        repository,
        kind="CREATE_MR",
        requirement_id=requirement_id,
        work_item_id=work_item_id,
        expected_revision=expected_revision,
        actor=actor,
        idempotency_key=idempotency_key,
        dependencies=dependencies,
    )


def request_integration_merge(
    repository: RequirementRepository,
    *,
    requirement_id: str,
    work_item_id: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> WorkItemDeliveryResult:
    return _request_delivery(
        repository,
        kind="MERGE_MR",
        requirement_id=requirement_id,
        work_item_id=work_item_id,
        expected_revision=expected_revision,
        actor=actor,
        idempotency_key=idempotency_key,
        dependencies=dependencies,
    )
