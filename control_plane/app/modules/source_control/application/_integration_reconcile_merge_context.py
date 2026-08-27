from dataclasses import dataclass

from control_plane.app.modules.source_control.application._integration_common import (
    TARGET_BRANCH,
    binding_dto,
    observation_dto,
    repository_profile,
)
from control_plane.app.modules.source_control.application._integration_reconcile_context import (
    LocalReconciliationBlocked,
)
from control_plane.app.modules.source_control.application.dependencies import (
    SourceControlDependencies,
)
from control_plane.app.modules.source_control.domain import (
    MergeIntegrationMergeRequestEffectPayload,
    MergeRequestBindingDto,
    MergeRequestObservationDto,
    SourceControlDependencyUnavailable,
    SourceControlEffectDto,
    merge_effect_subject,
)
from control_plane.app.modules.source_control.domain.reasons import SourceControlReason
from control_plane.app.modules.source_control.ports import GitLabRepositoryProfile


@dataclass(frozen=True, slots=True)
class MergeReconciliationContext:
    effect: SourceControlEffectDto
    payload: MergeIntegrationMergeRequestEffectPayload
    repository_profile: GitLabRepositoryProfile
    binding: MergeRequestBindingDto
    latest_observation: MergeRequestObservationDto


def read_merge_reconciliation_context(
    effect: SourceControlEffectDto,
    *,
    dependencies: SourceControlDependencies,
) -> MergeReconciliationContext | LocalReconciliationBlocked:
    repository_factory = dependencies.delivery_repository_factory
    if repository_factory is None:
        raise SourceControlDependencyUnavailable("Integration repository unavailable")
    payload = effect.payload
    if not isinstance(
        payload, MergeIntegrationMergeRequestEffectPayload
    ) or effect.subject_key != merge_effect_subject(payload.binding_id, payload.requested_head_sha):
        return LocalReconciliationBlocked(SourceControlReason.MR_CONFLICT)
    with dependencies.engine.connect() as db:
        repository = repository_factory(db)
        binding_row = repository.merge_request_binding_by_id(payload.binding_id)
        work_item_binding_row = repository.merge_request_binding_by_work_item(effect.work_item_id)
        branch_row = repository.branch_binding_by_work_item(effect.work_item_id)
        observation_row = (
            None
            if binding_row is None
            else repository.latest_merge_request_observation(payload.binding_id)
        )
        workspace_repository = dependencies.repository_factory(db).workspace_repository(
            effect.repository_id
        )
    if (
        binding_row is None
        or work_item_binding_row is None
        or branch_row is None
        or observation_row is None
    ):
        return LocalReconciliationBlocked(SourceControlReason.BRANCH_BINDING_MISSING)
    try:
        binding = binding_dto(binding_row)
        work_item_binding = binding_dto(work_item_binding_row)
        observation = observation_dto(observation_row)
    except (TypeError, ValueError):
        return LocalReconciliationBlocked(SourceControlReason.MR_CONFLICT)
    if (
        binding != work_item_binding
        or binding.id != payload.binding_id
        or binding.work_item_id != effect.work_item_id
        or binding.requirement_id != effect.requirement_id
        or binding.repository_id != effect.repository_id
        or binding.branch_binding_id != str(branch_row["id"])
        or binding.source_branch != branch_row["branch_name"]
        or binding.target_branch != TARGET_BRANCH
        or observation.binding_id != binding.id
    ):
        return LocalReconciliationBlocked(SourceControlReason.MR_CONFLICT)
    if (
        workspace_repository is None
        or workspace_repository["status"] != "AUTHORIZED"
        or str(workspace_repository["workspace_id"]) != binding.workspace_id
        or str(workspace_repository["project_id"]) != binding.external_project_id
    ):
        return LocalReconciliationBlocked(SourceControlReason.REPOSITORY_NOT_AUTHORIZED)
    profile = repository_profile(workspace_repository)
    if profile.default_branch != "main":
        return LocalReconciliationBlocked(SourceControlReason.PROJECT_PROFILE_UNSUPPORTED)
    return MergeReconciliationContext(
        effect=effect,
        payload=payload,
        repository_profile=profile,
        binding=binding,
        latest_observation=observation,
    )


__all__: list[str] = []
