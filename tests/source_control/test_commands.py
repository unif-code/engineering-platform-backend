from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import Engine

from control_plane.app.modules.source_control import (
    BindingRequestEnvelope,
    EffectState,
    InvalidRepositorySecretReference,
    RepositoryAuthorizationState,
    RepositoryRemoved,
    RepositoryWorkspaceConflict,
    RequirementCallbackUnavailable,
    SourceControlDependencies,
    accept_binding_request,
    process_binding_request,
    register_workspace_repository,
    remove_workspace_repository,
)
from control_plane.app.modules.source_control.adapters import (
    SqlAlchemySourceControlRepository,
)
from control_plane.app.modules.source_control.ports import (
    BindingBlockedResult,
    BindingEligibility,
    BindingReadyResult,
    BranchSnapshot,
    GitLabAccessDenied,
    GitLabProviderUnavailable,
    GitLabRepositoryProfile,
    GitLabResultUnknown,
    RequirementBindingContext,
)

NOW = datetime(2026, 8, 26, 4, 0, tzinfo=UTC)
REPOSITORY_ID = "gitlab-project-501"
WORKSPACE_ID = "20000000-0000-0000-0000-000000000501"


class FixedClock:
    def now(self) -> datetime:
        return NOW


class FixedRandom:
    def __init__(self) -> None:
        self._next = 500

    def uuid4(self) -> UUID:
        self._next += 1
        return UUID(f"90000000-0000-0000-0000-{self._next:012d}")


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[object] = []

    def append_in_transaction(self, _db: object, envelope: object) -> None:
        self.events.append(envelope)


class FakeRequirement:
    def __init__(self, context: RequirementBindingContext) -> None:
        self.context = context
        self.ready: list[BindingReadyResult] = []
        self.blocked: list[BindingBlockedResult] = []
        self.fail_next_ready = False

    def claim_requests(
        self,
        *,
        limit: int,
        lease_until: datetime,
    ) -> tuple[BindingRequestEnvelope, ...]:
        raise AssertionError((limit, lease_until))

    def acknowledge_request(self, message_id: str) -> None:
        raise AssertionError(message_id)

    def release_request(
        self,
        message_id: str,
        *,
        error_code: str,
        retry_at: datetime,
    ) -> None:
        raise AssertionError((message_id, error_code, retry_at))

    def binding_context(self, work_item_id: str) -> RequirementBindingContext:
        assert work_item_id == self.context.work_item_id
        return self.context

    def record_ready(self, result: BindingReadyResult) -> None:
        if self.fail_next_ready:
            self.fail_next_ready = False
            raise RequirementCallbackUnavailable("ready unavailable")
        self.ready.append(result)

    def record_blocked(self, result: BindingBlockedResult) -> None:
        self.blocked.append(result)


class FakeEligibility:
    def __init__(self, eligible: bool = True) -> None:
        self.eligible = eligible

    def evaluate(self, _context: RequirementBindingContext) -> BindingEligibility:
        return BindingEligibility(
            eligible=self.eligible,
            reason_code=None if self.eligible else "OWNER_INELIGIBLE",
        )


class FakeGitLab:
    def __init__(self) -> None:
        self.created: list[tuple[str, str]] = []
        self.calls: list[tuple[str, str]] = []
        self.create_error: Exception | None = None
        self.task_read_error: Exception | None = None
        self.task_read_error_once: Exception | None = None
        self.branch_sha: str | None = None

    def get_branch(
        self,
        _repository: GitLabRepositoryProfile,
        name: str,
    ) -> BranchSnapshot:
        self.calls.append(("GET", name))
        if name != "main" and self.task_read_error_once is not None:
            error = self.task_read_error_once
            self.task_read_error_once = None
            raise error
        if name != "main" and self.task_read_error is not None:
            raise self.task_read_error
        return BranchSnapshot(name=name, commit_sha=self.branch_sha or "a" * 40)

    def create_branch(
        self,
        _repository: GitLabRepositoryProfile,
        *,
        name: str,
        ref_sha: str,
    ) -> BranchSnapshot:
        self.calls.append(("POST", name))
        self.created.append((name, ref_sha))
        if self.create_error is not None:
            raise self.create_error
        return BranchSnapshot(name=name, commit_sha=ref_sha)


class FixedPolicy:
    def next_reconcile_at(self, *, now: datetime, attempts: int) -> datetime:
        return now + timedelta(seconds=30 * max(attempts, 1))

    def webhook_replay_window(self) -> timedelta:
        return timedelta(minutes=5)


def test_register_repository_stores_only_secret_references(
    isolated_source_control_rw_engine: Engine,
) -> None:
    audit = FakeAudit()
    dependencies = SourceControlDependencies(
        repository_factory=SqlAlchemySourceControlRepository,
        engine=isolated_source_control_rw_engine,
        requirement=None,
        eligibility=None,
        audit=audit,
        clock=FixedClock(),
        random=FixedRandom(),
    )
    with isolated_source_control_rw_engine.begin() as db:
        registered = register_workspace_repository(
            SqlAlchemySourceControlRepository(db),
            repository_id=REPOSITORY_ID,
            workspace_id=WORKSPACE_ID,
            project_id="101",
            project_path="platform/backend",
            connection_ref="gitlab-dev",
            credential_secret_ref="openbao:source-control/gitlab-dev/token",
            webhook_signing_secret_ref="openbao:source-control/gitlab-dev/webhook",
            actor="SYSTEM",
            dependencies=dependencies,
        )

    assert registered.status is RepositoryAuthorizationState.AUTHORIZED
    assert registered.credential_secret_ref == "openbao:source-control/gitlab-dev/token"
    assert "glpat-" not in registered.model_dump_json().lower()
    assert len(audit.events) == 1


@pytest.mark.parametrize(
    ("credential_ref", "webhook_ref"),
    [
        ("custom-gitlab-token-value", None),
        ("secret-ref:credential", "custom-webhook-token-value"),
        ("https://vault.example/secrets/gitlab", None),
    ],
)
def test_repository_registration_accepts_only_allowlisted_opaque_secret_references(
    isolated_source_control_rw_engine: Engine,
    credential_ref: str,
    webhook_ref: str | None,
) -> None:
    dependencies = SourceControlDependencies(
        repository_factory=SqlAlchemySourceControlRepository,
        engine=isolated_source_control_rw_engine,
        requirement=None,
        eligibility=None,
        audit=FakeAudit(),
        clock=FixedClock(),
        random=FixedRandom(),
    )

    with (
        isolated_source_control_rw_engine.begin() as db,
        pytest.raises(InvalidRepositorySecretReference),
    ):
        register_workspace_repository(
            SqlAlchemySourceControlRepository(db),
            repository_id=REPOSITORY_ID,
            workspace_id=WORKSPACE_ID,
            project_id="101",
            project_path="platform/backend",
            connection_ref="gitlab-dev",
            credential_secret_ref=credential_ref,
            webhook_signing_secret_ref=webhook_ref,
            actor="SYSTEM",
            dependencies=dependencies,
        )


def test_repository_cannot_move_workspace_and_removal_is_terminal(
    isolated_source_control_rw_engine: Engine,
) -> None:
    dependencies = SourceControlDependencies(
        repository_factory=SqlAlchemySourceControlRepository,
        engine=isolated_source_control_rw_engine,
        requirement=None,
        eligibility=None,
        audit=FakeAudit(),
        clock=FixedClock(),
        random=FixedRandom(),
    )
    with isolated_source_control_rw_engine.begin() as db:
        repository = SqlAlchemySourceControlRepository(db)
        register_workspace_repository(
            repository,
            repository_id=REPOSITORY_ID,
            workspace_id=WORKSPACE_ID,
            project_id="101",
            project_path="platform/backend",
            connection_ref="gitlab-dev",
            credential_secret_ref="secret-ref:credential",
            webhook_signing_secret_ref=None,
            actor="SYSTEM",
            dependencies=dependencies,
        )
        with pytest.raises(RepositoryWorkspaceConflict):
            register_workspace_repository(
                repository,
                repository_id=REPOSITORY_ID,
                workspace_id="20000000-0000-0000-0000-000000000599",
                project_id="101",
                project_path="platform/backend",
                connection_ref="gitlab-dev",
                credential_secret_ref="secret-ref:credential",
                webhook_signing_secret_ref=None,
                actor="SYSTEM",
                dependencies=dependencies,
            )
        removed = remove_workspace_repository(
            repository,
            repository_id=REPOSITORY_ID,
            expected_revision=1,
            actor="SYSTEM",
            dependencies=dependencies,
        )
        with pytest.raises(RepositoryRemoved):
            remove_workspace_repository(
                repository,
                repository_id=REPOSITORY_ID,
                expected_revision=2,
                actor="SYSTEM",
                dependencies=dependencies,
            )

    assert removed.status is RepositoryAuthorizationState.REMOVED
    assert removed.revision == 2


def _binding_context(*, assignment_state: str = "ASSIGNED") -> RequirementBindingContext:
    return RequirementBindingContext(
        requirement_id="40000000-0000-0000-0000-000000000501",
        requirement_type="feat",
        requirement_title="Deterministic GitLab branch",
        workspace_id=WORKSPACE_ID,
        work_item_id="50000000-0000-0000-0000-000000000501",
        work_item_revision=1,
        repository_id=REPOSITORY_ID,
        assignment_state=assignment_state,
        human_owner_id="account-501" if assignment_state == "ASSIGNED" else None,
        required_capabilities=("code.change",),
    )


def _saga_dependencies(
    engine: Engine,
    *,
    context: RequirementBindingContext | None = None,
    eligible: bool = True,
) -> tuple[SourceControlDependencies, FakeRequirement, FakeGitLab]:
    requirement = FakeRequirement(context or _binding_context())
    gitlab = FakeGitLab()
    dependencies = SourceControlDependencies(
        repository_factory=SqlAlchemySourceControlRepository,
        engine=engine,
        requirement=requirement,
        eligibility=FakeEligibility(eligible),
        audit=FakeAudit(),
        clock=FixedClock(),
        random=FixedRandom(),
        gitlab=gitlab,
        policy=FixedPolicy(),
    )
    return dependencies, requirement, gitlab


def _seed_binding_request(engine: Engine, dependencies: SourceControlDependencies) -> None:
    with engine.begin() as db:
        repository = SqlAlchemySourceControlRepository(db)
        register_workspace_repository(
            repository,
            repository_id=REPOSITORY_ID,
            workspace_id=WORKSPACE_ID,
            project_id="platform/backend",
            project_path="platform/backend",
            connection_ref="gitlab-dev",
            credential_secret_ref="secret-ref:credential",
            webhook_signing_secret_ref=None,
            actor="SYSTEM",
            dependencies=dependencies,
        )
        accept_binding_request(
            repository,
            BindingRequestEnvelope(
                message_id="30000000-0000-0000-0000-000000000501",
                topic="requirement.repository-binding.requested",
                requirement_id="40000000-0000-0000-0000-000000000501",
                requirement_version=1,
                work_item_id="50000000-0000-0000-0000-000000000501",
                repository_id=REPOSITORY_ID,
                attempts=1,
            ),
            now=NOW,
        )


def test_duplicate_processing_reuses_effect_number_name_and_binding(
    isolated_source_control_rw_engine: Engine,
) -> None:
    dependencies, requirement, gitlab = _saga_dependencies(isolated_source_control_rw_engine)
    _seed_binding_request(isolated_source_control_rw_engine, dependencies)

    first = process_binding_request(
        message_id="30000000-0000-0000-0000-000000000501",
        dependencies=dependencies,
    )
    second = process_binding_request(
        message_id="30000000-0000-0000-0000-000000000501",
        dependencies=dependencies,
    )
    with isolated_source_control_rw_engine.connect() as db:
        effect_count = db.exec_driver_sql(
            "SELECT count(*) FROM source_control.source_control_effect"
        ).scalar_one()
        binding_count = db.exec_driver_sql(
            "SELECT count(*) FROM source_control.repository_branch_binding"
        ).scalar_one()

    assert second == first
    assert first.binding is not None
    assert effect_count == 1
    assert binding_count == 1
    assert gitlab.created == [(first.binding.branch_name, "a" * 40)]
    assert len(requirement.ready) == 2


def test_timeout_never_creates_binding(
    isolated_source_control_rw_engine: Engine,
) -> None:
    dependencies, requirement, gitlab = _saga_dependencies(isolated_source_control_rw_engine)
    _seed_binding_request(isolated_source_control_rw_engine, dependencies)
    gitlab.create_error = GitLabResultUnknown("timeout")
    gitlab.task_read_error = GitLabProviderUnavailable("unreadable")

    result = process_binding_request(
        message_id="30000000-0000-0000-0000-000000000501",
        dependencies=dependencies,
    )
    with isolated_source_control_rw_engine.connect() as db:
        binding_count = db.exec_driver_sql(
            "SELECT count(*) FROM source_control.repository_branch_binding"
        ).scalar_one()

    assert result.effect is not None
    assert result.effect.state is EffectState.UNKNOWN
    assert result.binding is None
    assert binding_count == 0
    assert requirement.blocked[0].reason_code == "RECONCILIATION_PENDING"


def test_unexpected_process_crash_leaves_in_flight_effect_with_recovery_due_time(
    isolated_source_control_rw_engine: Engine,
) -> None:
    dependencies, _requirement, gitlab = _saga_dependencies(isolated_source_control_rw_engine)
    _seed_binding_request(isolated_source_control_rw_engine, dependencies)
    gitlab.create_error = RuntimeError("simulated process crash")

    with pytest.raises(RuntimeError, match="simulated process crash"):
        process_binding_request(
            message_id="30000000-0000-0000-0000-000000000501",
            dependencies=dependencies,
        )
    with isolated_source_control_rw_engine.connect() as db:
        row = db.exec_driver_sql(
            "SELECT state, next_reconcile_at FROM source_control.source_control_effect"
        ).one()

    assert row.state == EffectState.IN_FLIGHT.value
    assert row.next_reconcile_at is not None
    assert row.next_reconcile_at > NOW


def test_create_access_denial_blocks_without_creating_binding(
    isolated_source_control_rw_engine: Engine,
) -> None:
    dependencies, requirement, gitlab = _saga_dependencies(isolated_source_control_rw_engine)
    _seed_binding_request(isolated_source_control_rw_engine, dependencies)
    gitlab.create_error = GitLabAccessDenied("denied")

    result = process_binding_request(
        message_id="30000000-0000-0000-0000-000000000501",
        dependencies=dependencies,
    )

    assert result.effect is not None
    assert result.effect.state is EffectState.BLOCKED
    assert result.binding is None
    assert result.blocked_reason == "ACCESS_DENIED"
    assert requirement.blocked[0].reason_code == "ACCESS_DENIED"


@pytest.mark.parametrize(
    ("assignment_state", "eligible", "reason"),
    [
        ("UNASSIGNED", True, "OWNER_UNASSIGNED"),
        ("ASSIGNED", False, "OWNER_INELIGIBLE"),
    ],
)
def test_owner_guard_blocks_without_gitlab_calls(
    isolated_source_control_rw_engine: Engine,
    assignment_state: str,
    eligible: bool,
    reason: str,
) -> None:
    dependencies, requirement, gitlab = _saga_dependencies(
        isolated_source_control_rw_engine,
        context=_binding_context(assignment_state=assignment_state),
        eligible=eligible,
    )
    _seed_binding_request(isolated_source_control_rw_engine, dependencies)

    result = process_binding_request(
        message_id="30000000-0000-0000-0000-000000000501",
        dependencies=dependencies,
    )

    assert result.effect is None
    assert result.blocked_reason == reason
    assert gitlab.calls == []
    assert requirement.blocked[0].reason_code == reason


@pytest.mark.parametrize("failure", ["removed", "context-mismatch"])
def test_repository_guard_blocks_without_gitlab_calls(
    isolated_source_control_rw_engine: Engine,
    failure: str,
) -> None:
    context = _binding_context()
    if failure == "context-mismatch":
        context = context.model_copy(update={"repository_id": "gitlab-project-other"})
    dependencies, requirement, gitlab = _saga_dependencies(
        isolated_source_control_rw_engine,
        context=context,
    )
    _seed_binding_request(isolated_source_control_rw_engine, dependencies)
    if failure == "removed":
        with isolated_source_control_rw_engine.begin() as db:
            remove_workspace_repository(
                SqlAlchemySourceControlRepository(db),
                repository_id=REPOSITORY_ID,
                expected_revision=1,
                actor="SYSTEM",
                dependencies=dependencies,
            )

    result = process_binding_request(
        message_id="30000000-0000-0000-0000-000000000501",
        dependencies=dependencies,
    )

    assert result.blocked_reason == "REPOSITORY_NOT_AUTHORIZED"
    assert gitlab.calls == []
    assert requirement.blocked[0].reason_code == "REPOSITORY_NOT_AUTHORIZED"


def test_concurrent_processing_leaves_one_effect_and_binding(
    isolated_source_control_rw_engine: Engine,
) -> None:
    dependencies, _requirement, gitlab = _saga_dependencies(isolated_source_control_rw_engine)
    _seed_binding_request(isolated_source_control_rw_engine, dependencies)

    def process() -> object:
        try:
            return process_binding_request(
                message_id="30000000-0000-0000-0000-000000000501",
                dependencies=dependencies,
            )
        except RequirementCallbackUnavailable as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: process(), range(2)))
    with isolated_source_control_rw_engine.connect() as db:
        effect_count = db.exec_driver_sql(
            "SELECT count(*) FROM source_control.source_control_effect"
        ).scalar_one()
        binding_count = db.exec_driver_sql(
            "SELECT count(*) FROM source_control.repository_branch_binding"
        ).scalar_one()

    assert effect_count == 1
    assert binding_count == 1
    assert len(gitlab.created) == 1
    assert any(not isinstance(outcome, Exception) for outcome in outcomes)
