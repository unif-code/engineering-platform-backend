from dataclasses import dataclass

from control_plane.app.modules.source_control.application._integration_common import (
    TARGET_BRANCH,
    repository_profile,
)
from control_plane.app.modules.source_control.application.dependencies import (
    SourceControlDependencies,
)
from control_plane.app.modules.source_control.domain import (
    CreateIntegrationMergeRequestEffectPayload,
    SourceControlDependencyUnavailable,
    SourceControlEffectDto,
)
from control_plane.app.modules.source_control.domain.reasons import SourceControlReason
from control_plane.app.modules.source_control.ports import GitLabRepositoryProfile


@dataclass(frozen=True, slots=True)
class CreateReconciliationContext:
    effect: SourceControlEffectDto
    payload: CreateIntegrationMergeRequestEffectPayload
    workspace_id: str
    branch_binding_id: str
    source_branch: str
    profile: GitLabRepositoryProfile


@dataclass(frozen=True, slots=True)
class LocalReconciliationBlocked:
    reason: SourceControlReason


def read_create_reconciliation_context(
    effect: SourceControlEffectDto,
    *,
    dependencies: SourceControlDependencies,
) -> CreateReconciliationContext | LocalReconciliationBlocked:
    repository_factory = dependencies.delivery_repository_factory
    if repository_factory is None:
        raise SourceControlDependencyUnavailable("Integration repository unavailable")
    payload = effect.payload
    if not isinstance(payload, CreateIntegrationMergeRequestEffectPayload):
        return LocalReconciliationBlocked(SourceControlReason.MR_CONFLICT)
    with dependencies.engine.connect() as db:
        branch = repository_factory(db).branch_binding_by_work_item(effect.work_item_id)
        workspace_repository = dependencies.repository_factory(db).workspace_repository(
            effect.repository_id
        )
    if (
        branch is None
        or str(branch["id"]) != payload.branch_binding_id
        or str(branch["work_item_id"]) != effect.work_item_id
        or str(branch["requirement_id"]) != effect.requirement_id
        or str(branch["repository_id"]) != effect.repository_id
    ):
        return LocalReconciliationBlocked(SourceControlReason.BRANCH_BINDING_MISSING)
    if (
        workspace_repository is None
        or workspace_repository["status"] != "AUTHORIZED"
        or str(workspace_repository["id"]) != effect.repository_id
        or str(workspace_repository["workspace_id"]) != str(branch["workspace_id"])
    ):
        return LocalReconciliationBlocked(SourceControlReason.REPOSITORY_NOT_AUTHORIZED)
    profile = repository_profile(workspace_repository)
    if profile.default_branch != "main":
        return LocalReconciliationBlocked(SourceControlReason.PROJECT_PROFILE_UNSUPPORTED)
    if not branch["branch_name"] or branch["branch_name"] == TARGET_BRANCH:
        return LocalReconciliationBlocked(SourceControlReason.BRANCH_BINDING_MISSING)
    return CreateReconciliationContext(
        effect=effect,
        payload=payload,
        workspace_id=str(branch["workspace_id"]),
        branch_binding_id=str(branch["id"]),
        source_branch=branch["branch_name"],
        profile=profile,
    )


__all__: list[str] = []
