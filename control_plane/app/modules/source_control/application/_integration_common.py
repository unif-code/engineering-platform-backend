import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from control_plane.app.modules.audit import AuditEnvelope
from control_plane.app.modules.source_control.application.dependencies import (
    SourceControlDependencies,
)
from control_plane.app.modules.source_control.domain import (
    CreateIntegrationMergeRequestEffectPayload,
    EffectOperation,
    MergeRequestBindingDto,
    MergeRequestObservationDto,
    MergeRequestState,
    RequirementCallbackUnavailable,
    SourceControlDependencyUnavailable,
    SourceControlEffectDto,
)
from control_plane.app.modules.source_control.ports import (
    BranchSnapshot,
    GitLabMergeRequestSnapshot,
    GitLabRepositoryProfile,
    RequirementBindingContext,
    RequirementDeliveryContext,
    SourceControlIntegrationRepository,
)

TARGET_BRANCH = "dev"
CREATE_OPERATION = EffectOperation.CREATE_INTEGRATION_MR
CREATE_TOPIC: Literal["requirement.integration-merge-request.requested"] = (
    "requirement.integration-merge-request.requested"
)
MERGE_TOPIC: Literal["requirement.integration-merge.requested"] = (
    "requirement.integration-merge.requested"
)
REQUIREMENT_TYPE_PREFIXES = frozenset({"feat", "fix", "refactor", "chore"})
PREFLIGHT_OUTCOME_REASONS = frozenset(
    {
        "BRANCH_BINDING_MISSING",
        "HEAD_SHA_CHANGED",
        "MR_CONFLICT",
        "NO_DELIVERY_COMMIT",
        "OWNER_INELIGIBLE",
        "OWNER_MISMATCH",
        "PROJECT_PROFILE_UNSUPPORTED",
        "REPOSITORY_NOT_AUTHORIZED",
        "TARGET_BRANCH_NOT_FOUND",
        "TARGET_BRANCH_NOT_PROTECTED",
    }
)

type PreflightReason = Literal[
    "BRANCH_BINDING_MISSING",
    "HEAD_SHA_CHANGED",
    "MR_CONFLICT",
    "NO_DELIVERY_COMMIT",
    "OWNER_INELIGIBLE",
    "OWNER_MISMATCH",
    "PROJECT_PROFILE_UNSUPPORTED",
    "REPOSITORY_NOT_AUTHORIZED",
    "TARGET_BRANCH_NOT_FOUND",
    "TARGET_BRANCH_NOT_PROTECTED",
]


class CallbackSubject(Protocol):
    work_item_id: str
    work_item_revision: int


class OriginatingCallbackSubject(BaseModel):
    model_config = ConfigDict(frozen=True)

    work_item_id: str
    work_item_revision: int


@dataclass(frozen=True, slots=True)
class Admission:
    context: RequirementDeliveryContext
    binding_context: RequirementBindingContext
    repository_profile: GitLabRepositoryProfile
    branch_binding_id: str
    task_branch: str
    base_commit_sha: str


@dataclass(frozen=True, slots=True)
class ProviderPreflight:
    source: BranchSnapshot


@dataclass(frozen=True, slots=True)
class ProviderPreflightBlocked(Exception):
    reason_code: PreflightReason


class ProviderPreflightTransient(Exception):
    pass


@dataclass(frozen=True, slots=True)
class AcquiredEffect:
    effect: SourceControlEffectDto
    payload: CreateIntegrationMergeRequestEffectPayload


@dataclass(frozen=True, slots=True)
class CommittedFacts:
    effect: SourceControlEffectDto
    binding: MergeRequestBindingDto
    observation: MergeRequestObservationDto


class EffectCollision(Exception):
    pass


class ProcessIntegrationRequestResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    effect: SourceControlEffectDto | None
    binding: MergeRequestBindingDto | None
    observation: MergeRequestObservationDto | None
    blocked_reason: str | None


def claim_exact_delivery_request(
    message_id: str,
    *,
    expected_topic: Literal[
        "requirement.integration-merge-request.requested",
        "requirement.integration-merge.requested",
    ],
    dependencies: SourceControlDependencies,
) -> tuple[Any | None, Any]:
    repository_factory = dependencies.delivery_repository_factory
    if repository_factory is None:
        raise SourceControlDependencyUnavailable("Integration repository unavailable")
    now = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        repository = repository_factory(db)
        claimed = repository.claim_delivery_request(
            message_id,
            expected_topic=expected_topic,
            now=now,
            lease_until=now + timedelta(minutes=2),
        )
        inbox = claimed or repository.delivery_request(message_id)
    if inbox is None:
        raise RequirementCallbackUnavailable("Delivery request is unavailable")
    if inbox["topic"] != expected_topic:
        raise SourceControlDependencyUnavailable("Delivery request operation is invalid")
    return claimed, inbox


def effect_dto(row: Any) -> SourceControlEffectDto:
    return SourceControlEffectDto.model_validate(
        {
            "id": str(row["id"]),
            "effect_key": row["effect_key"],
            "operation": row["operation"],
            "subject_key": row["subject_key"],
            "payload": dict(row["payload"]),
            "work_item_id": str(row["work_item_id"]),
            "requirement_id": str(row["requirement_id"]),
            "repository_id": str(row["repository_id"]),
            "work_item_number": row["work_item_number"],
            "branch_name": row["branch_name"],
            "base_commit_sha": row["base_commit_sha"],
            "request_fingerprint": row["request_fingerprint"],
            "attempts": row["attempts"],
            "next_reconcile_at": row["next_reconcile_at"],
            "state": row["state"],
            "last_error_code": row["last_error_code"],
            "callback_state": row["requirement_callback_state"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "completed_at": row["completed_at"],
        }
    )


def binding_dto(row: Any) -> MergeRequestBindingDto:
    return MergeRequestBindingDto(
        id=str(row["id"]),
        kind=row["kind"],
        work_item_id=str(row["work_item_id"]),
        requirement_id=str(row["requirement_id"]),
        workspace_id=str(row["workspace_id"]),
        repository_id=str(row["repository_id"]),
        branch_binding_id=str(row["branch_binding_id"]),
        external_project_id=row["external_project_id"],
        merge_request_iid=row["merge_request_iid"],
        source_branch=row["source_branch"],
        target_branch=row["target_branch"],
        create_effect_id=str(row["create_effect_id"]),
        head_sha=row["head_sha"],
        creation_origin=row["creation_origin"],
        created_at=row["created_at"],
    )


def observation_dto(row: Any) -> MergeRequestObservationDto:
    return MergeRequestObservationDto(
        id=str(row["id"]),
        binding_id=str(row["binding_id"]),
        head_sha=row["head_sha"],
        state=row["state"],
        merge_commit_sha=row["merge_commit_sha"],
        external_merge_user_id=row["external_merge_user_id"],
        merged_at=row["merged_at"],
        observation_digest=row["observation_digest"],
        observed_at=row["observed_at"],
    )


def repository_profile(row: Any) -> GitLabRepositoryProfile:
    return GitLabRepositoryProfile(
        repository_id=str(row["id"]),
        project_id=row["project_id"],
        project_path=row["project_path"],
        connection_ref=row["connection_ref"],
        default_branch=row["default_branch"],
        credential_secret_ref=row["credential_secret_ref"],
    )


def append_audit(
    repository: SourceControlIntegrationRepository,
    *,
    action: str,
    target_type: str,
    target_id: str,
    dependencies: SourceControlDependencies,
) -> None:
    dependencies.audit.append_in_transaction(
        repository.db,
        AuditEnvelope(
            id=str(dependencies.random.uuid4()),
            occurred_at=dependencies.clock.now(),
            actor="SYSTEM:SOURCE_CONTROL",
            actor_type="SYSTEM",
            action=action,
            target_type=target_type,
            target_id=target_id,
            result="SUCCESS",
            correlation_id=f"source-control:effect:{target_id}",
        ),
    )


def observation_digest(snapshot: GitLabMergeRequestSnapshot) -> str:
    payload = {
        "projectId": snapshot.project_id,
        "iid": snapshot.iid,
        "sourceBranch": snapshot.source_branch,
        "targetBranch": snapshot.target_branch,
        "headSha": snapshot.head_sha,
        "state": snapshot.state,
        "mergeCommitSha": snapshot.merge_commit_sha,
        "mergeUserId": snapshot.merge_user_id,
        "mergedAt": None if snapshot.merged_at is None else snapshot.merged_at.isoformat(),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def snapshot_state(snapshot: GitLabMergeRequestSnapshot) -> MergeRequestState:
    return {
        "opened": MergeRequestState.OPEN,
        "merged": MergeRequestState.MERGED,
        "closed": MergeRequestState.CLOSED,
        "locked": MergeRequestState.LOCKED,
    }[snapshot.state]
