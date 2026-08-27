from datetime import timedelta
from typing import Any, Literal

from sqlalchemy.exc import IntegrityError

from control_plane.app.modules.source_control.application._integration_common import (
    CREATE_OPERATION as _CREATE_OPERATION,
)
from control_plane.app.modules.source_control.application._integration_common import (
    REQUIREMENT_TYPE_PREFIXES as _REQUIREMENT_TYPE_PREFIXES,
)
from control_plane.app.modules.source_control.application._integration_common import (
    AcquiredEffect as _AcquiredEffect,
)
from control_plane.app.modules.source_control.application._integration_common import (
    Admission as _Admission,
)
from control_plane.app.modules.source_control.application._integration_common import (
    CommittedFacts as _CommittedFacts,
)
from control_plane.app.modules.source_control.application._integration_common import (
    EffectCollision as _EffectCollision,
)
from control_plane.app.modules.source_control.application._integration_common import (
    append_audit as _append_audit,
)
from control_plane.app.modules.source_control.application._integration_common import (
    binding_dto as _binding_dto,
)
from control_plane.app.modules.source_control.application._integration_common import (
    effect_dto as _effect_dto,
)
from control_plane.app.modules.source_control.application._integration_common import (
    observation_digest as _observation_digest,
)
from control_plane.app.modules.source_control.application._integration_common import (
    observation_dto as _observation_dto,
)
from control_plane.app.modules.source_control.application._integration_common import (
    repository_profile as _repository_profile,
)
from control_plane.app.modules.source_control.application._integration_common import (
    snapshot_state as _snapshot_state,
)
from control_plane.app.modules.source_control.application.dependencies import (
    SourceControlDependencies,
)
from control_plane.app.modules.source_control.domain import (
    CreateIntegrationMergeRequestEffectPayload,
    EffectState,
    MergeRequestCreationOrigin,
    MergeRequestKind,
    RequirementCallbackState,
    RequirementCallbackUnavailable,
    SourceControlDependencyUnavailable,
    SourceControlEffectDto,
)
from control_plane.app.modules.source_control.domain.reasons import SourceControlReason
from control_plane.app.modules.source_control.ports import GitLabMergeRequestSnapshot


def _read_admission(
    inbox: Any,
    branch_row: Any,
    *,
    dependencies: SourceControlDependencies,
) -> _Admission | SourceControlReason:
    requirement_delivery = dependencies.requirement_delivery
    requirement_binding = dependencies.requirement
    eligibility = dependencies.eligibility
    if requirement_delivery is None or requirement_binding is None or eligibility is None:
        raise SourceControlDependencyUnavailable("Integration admission unavailable")
    context = requirement_delivery.delivery_context(str(inbox["work_item_id"]))
    binding_context = requirement_binding.binding_context(str(inbox["work_item_id"]))
    if (
        context.requirement_id != str(inbox["requirement_id"])
        or context.requirement_revision != inbox["requirement_revision"]
        or context.work_item_id != str(inbox["work_item_id"])
        or context.repository_id != str(inbox["repository_id"])
        or context.work_item_revision != inbox["work_item_revision"]
        or context.request_actor_id != inbox["actor_id"]
        or context.requirement_state != "IN_PROGRESS"
        or context.work_item_state != "IN_PROGRESS"
        or context.integration_delivery_state != "MR_PENDING"
        or context.integration_merge_request_binding_id is not None
        or binding_context.requirement_id != context.requirement_id
        or binding_context.workspace_id != context.workspace_id
        or binding_context.work_item_id != context.work_item_id
        or binding_context.work_item_revision != context.work_item_revision
        or binding_context.repository_id != context.repository_id
        or binding_context.required_capabilities != context.required_capabilities
        or binding_context.requirement_type not in _REQUIREMENT_TYPE_PREFIXES
    ):
        raise SourceControlDependencyUnavailable("Integration MR context is invalid")
    if (
        context.human_owner_id != inbox["actor_id"]
        or binding_context.assignment_state != "ASSIGNED"
        or binding_context.human_owner_id != context.human_owner_id
        or binding_context.human_owner_id != inbox["actor_id"]
    ):
        return SourceControlReason.OWNER_MISMATCH
    if context.repository_state != "BOUND":
        return SourceControlReason.REPOSITORY_NOT_AUTHORIZED
    if context.base_commit_sha is None or context.task_branch is None:
        return SourceControlReason.BRANCH_BINDING_MISSING
    owner = eligibility.evaluate(binding_context)
    if not owner.eligible:
        return SourceControlReason.OWNER_INELIGIBLE
    with dependencies.engine.connect() as db:
        repository_row = dependencies.repository_factory(db).workspace_repository(
            context.repository_id
        )
    if (
        repository_row is None
        or repository_row["status"] != "AUTHORIZED"
        or str(repository_row["workspace_id"]) != context.workspace_id
    ):
        return SourceControlReason.REPOSITORY_NOT_AUTHORIZED
    if (
        branch_row is None
        or str(branch_row["id"]) == ""
        or str(branch_row["requirement_id"]) != context.requirement_id
        or str(branch_row["workspace_id"]) != context.workspace_id
        or str(branch_row["repository_id"]) != context.repository_id
        or branch_row["branch_name"] != context.task_branch
        or branch_row["base_commit_sha"] != context.base_commit_sha
    ):
        return SourceControlReason.BRANCH_BINDING_MISSING
    return _Admission(
        context=context,
        binding_context=binding_context,
        repository_profile=_repository_profile(repository_row),
        branch_binding_id=str(branch_row["id"]),
        task_branch=context.task_branch,
        base_commit_sha=context.base_commit_sha,
    )


def _validated_effect_payload(
    effect: SourceControlEffectDto,
    *,
    subject_key: str,
    requirement_id: str,
    repository_id: str,
    request_fingerprint: str,
    branch_binding_id: str,
) -> CreateIntegrationMergeRequestEffectPayload | None:
    if (
        effect.operation is not _CREATE_OPERATION
        or effect.subject_key != subject_key
        or effect.work_item_id != subject_key.removeprefix("work-item:")
        or effect.requirement_id != requirement_id
        or effect.repository_id != repository_id
        or effect.request_fingerprint != request_fingerprint
        or not isinstance(
            effect.payload,
            CreateIntegrationMergeRequestEffectPayload,
        )
        or effect.payload.branch_binding_id != branch_binding_id
    ):
        return None
    return effect.payload


def _acquire_in_flight_effect(
    admission: _Admission,
    *,
    request_fingerprint: str,
    payload: CreateIntegrationMergeRequestEffectPayload,
    dependencies: SourceControlDependencies,
) -> _AcquiredEffect:
    repository_factory = dependencies.delivery_repository_factory
    if repository_factory is None:
        raise SourceControlDependencyUnavailable("Integration repository unavailable")
    context = admission.context
    subject_key = f"work-item:{context.work_item_id}"
    try:
        with dependencies.engine.begin() as db:
            repository = repository_factory(db)
            effect_row = repository.effect_by_operation_subject(
                _CREATE_OPERATION.value,
                subject_key,
                for_update=True,
            )
            if effect_row is None:
                effect_row = repository.insert_effect(
                    id=str(dependencies.random.uuid4()),
                    effect_key=f"source-control:create-integration-mr:{context.work_item_id}",
                    operation=_CREATE_OPERATION.value,
                    subject_key=subject_key,
                    payload=payload,
                    work_item_id=context.work_item_id,
                    requirement_id=context.requirement_id,
                    repository_id=context.repository_id,
                    request_fingerprint=request_fingerprint,
                    attempts=0,
                    next_reconcile_at=None,
                    state=EffectState.PLANNED.value,
                    requirement_callback_state=RequirementCallbackState.PENDING.value,
                    now=dependencies.clock.now(),
                )
                _append_audit(
                    repository,
                    action="source_control.integration_mr.planned",
                    target_type="source_control_effect",
                    target_id=str(effect_row["id"]),
                    correlation_id=f"source-control:effect:{effect_row['id']}",
                    dependencies=dependencies,
                )
    except IntegrityError:
        with dependencies.engine.connect() as db:
            effect_row = repository_factory(db).effect_by_operation_subject(
                _CREATE_OPERATION.value,
                subject_key,
            )
        if effect_row is None:
            raise
    try:
        effect = _effect_dto(effect_row)
    except (TypeError, ValueError):
        raise _EffectCollision from None
    validated_payload = _validated_effect_payload(
        effect,
        subject_key=subject_key,
        requirement_id=context.requirement_id,
        repository_id=context.repository_id,
        request_fingerprint=request_fingerprint,
        branch_binding_id=admission.branch_binding_id,
    )
    if validated_payload != payload:
        raise _EffectCollision
    with dependencies.engine.begin() as db:
        repository = repository_factory(db)
        in_flight_row = repository.transition_effect(
            effect.id,
            expected_state=EffectState.PLANNED.value,
            expected_attempts=effect.attempts,
            values={
                "state": EffectState.IN_FLIGHT.value,
                "attempts": effect.attempts + 1,
                "next_reconcile_at": dependencies.clock.now() + timedelta(minutes=2),
                "updated_at": dependencies.clock.now(),
            },
        )
        if in_flight_row is None:
            raise RequirementCallbackUnavailable("Integration MR effect lease was lost")
        _append_audit(
            repository,
            action="source_control.integration_mr.in_flight",
            target_type="source_control_effect",
            target_id=effect.id,
            correlation_id=f"source-control:effect:{effect.id}",
            dependencies=dependencies,
        )
    return _AcquiredEffect(
        effect=_effect_dto(in_flight_row),
        payload=validated_payload,
    )


def _commit_final_facts(
    admission: _Admission,
    effect: SourceControlEffectDto,
    readback: GitLabMergeRequestSnapshot,
    *,
    creation_origin: MergeRequestCreationOrigin,
    final_state: Literal[EffectState.SUCCEEDED, EffectState.BLOCKED],
    error_code: SourceControlReason | None,
    message_id: str,
    inbox_attempts: int,
    dependencies: SourceControlDependencies,
) -> _CommittedFacts:
    repository_factory = dependencies.delivery_repository_factory
    if repository_factory is None:
        raise SourceControlDependencyUnavailable("Integration repository unavailable")
    context = admission.context
    completed_at = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        repository = repository_factory(db)
        binding_row = repository.insert_merge_request_binding(
            id=str(dependencies.random.uuid4()),
            kind=MergeRequestKind.INTEGRATION.value,
            work_item_id=context.work_item_id,
            requirement_id=context.requirement_id,
            workspace_id=context.workspace_id,
            repository_id=context.repository_id,
            branch_binding_id=admission.branch_binding_id,
            external_project_id=readback.project_id,
            merge_request_iid=readback.iid,
            source_branch=readback.source_branch,
            target_branch=readback.target_branch,
            create_effect_id=effect.id,
            head_sha=readback.head_sha,
            creation_origin=creation_origin.value,
            now=completed_at,
        )
        observation_row = repository.append_merge_request_observation(
            id=str(dependencies.random.uuid4()),
            binding_id=str(binding_row["id"]),
            head_sha=readback.head_sha,
            state=_snapshot_state(readback).value,
            merge_commit_sha=readback.merge_commit_sha,
            external_merge_user_id=readback.merge_user_id,
            merged_at=readback.merged_at,
            observation_digest=_observation_digest(readback),
            observed_at=completed_at,
        )
        final_row = repository.transition_effect(
            effect.id,
            expected_state=EffectState.IN_FLIGHT.value,
            expected_attempts=effect.attempts,
            values={
                "state": final_state.value,
                "last_error_code": None if error_code is None else error_code.value,
                "next_reconcile_at": None,
                "completed_at": completed_at,
                "updated_at": completed_at,
            },
        )
        if final_row is None or observation_row is None:
            raise RequirementCallbackUnavailable("Integration MR effect lease was lost")
        completed_inbox = repository.complete_delivery_request(
            message_id,
            expected_attempts=inbox_attempts,
            now=completed_at,
        )
        if completed_inbox is None:
            raise RequirementCallbackUnavailable("Integration MR inbox lease was lost")
        _append_audit(
            repository,
            action=f"source_control.integration_mr.{final_state.value.lower()}",
            target_type="source_control_effect",
            target_id=effect.id,
            correlation_id=f"source-control:effect:{effect.id}",
            dependencies=dependencies,
        )
    return _CommittedFacts(
        effect=_effect_dto(final_row),
        binding=_binding_dto(binding_row),
        observation=_observation_dto(observation_row),
    )
