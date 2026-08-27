from dataclasses import dataclass
from typing import Any

from control_plane.app.modules.source_control.application._integration_common import (
    TARGET_BRANCH as _TARGET_BRANCH,
)
from control_plane.app.modules.source_control.application._integration_common import (
    EffectCollision as _EffectCollision,
)
from control_plane.app.modules.source_control.application._integration_common import (
    binding_dto as _binding_dto,
)
from control_plane.app.modules.source_control.application._integration_common import (
    effect_dto as _effect_dto,
)
from control_plane.app.modules.source_control.application._integration_common import (
    observation_dto as _observation_dto,
)
from control_plane.app.modules.source_control.application._integration_common import (
    repository_profile as _repository_profile,
)
from control_plane.app.modules.source_control.application.dependencies import (
    SourceControlDependencies,
)
from control_plane.app.modules.source_control.domain import (
    EffectOperation,
    MergeIntegrationMergeRequestEffectPayload,
    MergeRequestBindingDto,
    MergeRequestObservationDto,
    MergeRequestState,
    SourceControlDependencyUnavailable,
    SourceControlEffectDto,
    merge_effect_subject,
)
from control_plane.app.modules.source_control.ports import (
    GitLabRepositoryProfile,
    RequirementBindingContext,
    RequirementDeliveryContext,
)

_MERGE_OPERATION = EffectOperation.MERGE_INTEGRATION_MR
_MERGE_CAPABILITY = "merge_request.merge"


@dataclass(frozen=True, slots=True)
class _MergeAdmission:
    context: RequirementDeliveryContext
    binding_context: RequirementBindingContext
    repository_profile: GitLabRepositoryProfile
    branch_binding_id: str
    binding: MergeRequestBindingDto
    latest_observation: MergeRequestObservationDto
    blocked_reason: str | None


@dataclass(frozen=True, slots=True)
class _StoredMergeFacts:
    binding: MergeRequestBindingDto
    observation: MergeRequestObservationDto
    effect: SourceControlEffectDto | None


def _read_stored_merge_facts(
    inbox: Any,
    *,
    include_effect: bool = True,
    dependencies: SourceControlDependencies,
) -> _StoredMergeFacts:
    repository_factory = dependencies.delivery_repository_factory
    if repository_factory is None:
        raise SourceControlDependencyUnavailable("Integration repository unavailable")
    binding_id = inbox["integration_merge_request_binding_id"]
    if binding_id is None:
        raise SourceControlDependencyUnavailable("Integration merge binding is unavailable")
    with dependencies.engine.connect() as db:
        repository = repository_factory(db)
        binding_row = repository.merge_request_binding_by_id(str(binding_id))
        work_item_binding_row = repository.merge_request_binding_by_work_item(
            str(inbox["work_item_id"])
        )
        observation_row = (
            None
            if binding_row is None
            else repository.latest_merge_request_observation(str(binding_id))
        )
        effect_rows = (
            repository.effects_by_operation_work_item_fingerprint(
                _MERGE_OPERATION.value,
                str(inbox["work_item_id"]),
                inbox["payload_hash"],
            )
            if include_effect
            else []
        )
    if binding_row is None or work_item_binding_row is None or observation_row is None:
        raise SourceControlDependencyUnavailable("Integration merge facts are unavailable")
    try:
        binding = _binding_dto(binding_row)
        work_item_binding = _binding_dto(work_item_binding_row)
        observation = _observation_dto(observation_row)
        if len(effect_rows) > 1:
            raise _EffectCollision
        effect = None if not effect_rows else _effect_dto(effect_rows[0])
    except (TypeError, ValueError):
        raise _EffectCollision from None
    if (
        binding != work_item_binding
        or binding.id != str(binding_id)
        or binding.work_item_id != str(inbox["work_item_id"])
        or binding.requirement_id != str(inbox["requirement_id"])
        or binding.repository_id != str(inbox["repository_id"])
        or observation.binding_id != binding.id
    ):
        raise SourceControlDependencyUnavailable("Integration merge facts are invalid")
    if effect is not None:
        payload = effect.payload
        if (
            effect.operation is not _MERGE_OPERATION
            or effect.work_item_id != str(inbox["work_item_id"])
            or effect.requirement_id != str(inbox["requirement_id"])
            or effect.repository_id != str(inbox["repository_id"])
            or effect.request_fingerprint != inbox["payload_hash"]
            or not isinstance(payload, MergeIntegrationMergeRequestEffectPayload)
            or payload.binding_id != binding.id
            or effect.subject_key != merge_effect_subject(binding.id, payload.requested_head_sha)
        ):
            raise _EffectCollision
    return _StoredMergeFacts(
        binding=binding,
        observation=observation,
        effect=effect,
    )


def _read_merge_admission(
    inbox: Any,
    *,
    dependencies: SourceControlDependencies,
) -> _MergeAdmission:
    requirement_delivery = dependencies.requirement_delivery
    requirement_binding = dependencies.requirement
    eligibility = dependencies.eligibility
    repository_factory = dependencies.delivery_repository_factory
    if (
        requirement_delivery is None
        or requirement_binding is None
        or eligibility is None
        or repository_factory is None
    ):
        raise SourceControlDependencyUnavailable("Integration merge admission unavailable")
    context = requirement_delivery.delivery_context(str(inbox["work_item_id"]))
    binding_context = requirement_binding.binding_context(str(inbox["work_item_id"]))
    if (
        context.requirement_id != str(inbox["requirement_id"])
        or context.requirement_revision != inbox["requirement_revision"]
        or context.work_item_id != str(inbox["work_item_id"])
        or context.work_item_revision != inbox["work_item_revision"]
        or context.repository_id != str(inbox["repository_id"])
        or context.request_actor_id != inbox["actor_id"]
        or context.requirement_state != "VERIFYING"
        or context.work_item_state != "VERIFYING"
        or context.integration_delivery_state != "MERGE_PENDING"
        or context.integration_merge_request_binding_id
        != str(inbox["integration_merge_request_binding_id"])
        or binding_context.requirement_id != context.requirement_id
        or binding_context.workspace_id != context.workspace_id
        or binding_context.work_item_id != context.work_item_id
        or binding_context.work_item_revision != context.work_item_revision
        or binding_context.repository_id != context.repository_id
        or binding_context.required_capabilities != context.required_capabilities
    ):
        raise SourceControlDependencyUnavailable("Integration merge context is invalid")
    blocked_reason: str | None = None
    if (
        context.human_owner_id != inbox["actor_id"]
        or binding_context.assignment_state != "ASSIGNED"
        or binding_context.human_owner_id != context.human_owner_id
        or binding_context.human_owner_id != inbox["actor_id"]
    ):
        blocked_reason = "OWNER_MISMATCH"
    elif context.repository_state != "BOUND":
        blocked_reason = "REPOSITORY_NOT_AUTHORIZED"
    required_capabilities = tuple(
        dict.fromkeys((*binding_context.required_capabilities, _MERGE_CAPABILITY))
    )
    merge_context = binding_context.model_copy(
        update={"required_capabilities": required_capabilities}
    )
    if blocked_reason is None and not eligibility.evaluate(merge_context).eligible:
        blocked_reason = "MERGE_ACTOR_INELIGIBLE"
    binding_id = context.integration_merge_request_binding_id
    if binding_id is None:
        raise SourceControlDependencyUnavailable("Integration merge binding is unavailable")
    with dependencies.engine.connect() as db:
        repository_row = dependencies.repository_factory(db).workspace_repository(
            context.repository_id
        )
        repository = repository_factory(db)
        branch_row = repository.branch_binding_by_work_item(context.work_item_id)
        binding_row = repository.merge_request_binding_by_id(binding_id)
        work_item_binding_row = repository.merge_request_binding_by_work_item(context.work_item_id)
        observation_row = (
            None if binding_row is None else repository.latest_merge_request_observation(binding_id)
        )
    if (
        repository_row is None
        or repository_row["status"] != "AUTHORIZED"
        or str(repository_row["workspace_id"]) != context.workspace_id
        or branch_row is None
        or binding_row is None
        or work_item_binding_row is None
        or observation_row is None
    ):
        raise SourceControlDependencyUnavailable("Integration merge facts are unavailable")
    binding = _binding_dto(binding_row)
    work_item_binding = _binding_dto(work_item_binding_row)
    latest_observation = _observation_dto(observation_row)
    if (
        str(branch_row["id"]) != binding.branch_binding_id
        or str(branch_row["requirement_id"]) != context.requirement_id
        or str(branch_row["workspace_id"]) != context.workspace_id
        or str(branch_row["repository_id"]) != context.repository_id
        or branch_row["branch_name"] != context.task_branch
        or branch_row["base_commit_sha"] != context.base_commit_sha
        or binding.id != binding_id
        or work_item_binding != binding
        or binding.requirement_id != context.requirement_id
        or binding.workspace_id != context.workspace_id
        or binding.work_item_id != context.work_item_id
        or binding.repository_id != context.repository_id
        or binding.external_project_id != str(repository_row["project_id"])
        or binding.source_branch != context.task_branch
        or binding.target_branch != _TARGET_BRANCH
        or latest_observation.binding_id != binding.id
        or latest_observation.state is not MergeRequestState.OPEN
    ):
        raise SourceControlDependencyUnavailable("Integration merge facts are invalid")
    return _MergeAdmission(
        context=context,
        binding_context=merge_context,
        repository_profile=_repository_profile(repository_row),
        branch_binding_id=str(branch_row["id"]),
        binding=binding,
        latest_observation=latest_observation,
        blocked_reason=blocked_reason,
    )


__all__: list[str] = []
