from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import Connection, Engine

from control_plane.app.modules.model_gateway.application import CatalogDependencies
from control_plane.app.modules.model_gateway.application.checks import (
    check_audit,
    currentness,
    validate_connection_admission,
)
from control_plane.app.modules.model_gateway.domain import DeploymentState
from control_plane.app.modules.model_gateway.domain.checks import (
    CheckBlocked,
    CheckInputSnapshot,
    CheckReason,
    CheckState,
    ConnectionCheck,
    InputCurrentness,
    ProbeOutcome,
)
from control_plane.app.modules.model_gateway.domain.connections import (
    EXECUTION_LEASE_SECONDS,
    ConnectionDefinition,
)
from control_plane.app.modules.model_gateway.ports.checks import (
    CheckActorPort,
    CheckRepository,
    ConnectionDirectoryPort,
    ProbePort,
)


@dataclass(frozen=True)
class ModelCheckWorkerDependencies:
    engine: Engine
    repository_factory: Callable[[Connection], CheckRepository]
    common: CatalogDependencies
    directory: ConnectionDirectoryPort
    actors: CheckActorPort
    probe: ProbePort


@dataclass(frozen=True)
class ModelCheckBatchResult:
    processed: int
    recovered: int
    attempted: int


def _finish(
    repository: CheckRepository,
    check: ConnectionCheck,
    outcome: ProbeOutcome,
    material: InputCurrentness,
    deps: ModelCheckWorkerDependencies,
) -> ConnectionCheck:
    now = deps.common.now()
    value = ConnectionCheck.model_validate(
        check.model_dump()
        | outcome.model_dump()
        | {
            "revision": check.revision + 1,
            "finished_at": now,
            "material_currentness": material,
        }
    )
    if not repository.save_check(value, expected_revision=check.revision):
        raise RuntimeError("check execution fence lost")
    deployment = repository.get(value.deployment_id)
    observed = (
        currentness(value, deployment, deps.directory)[0]
        if deployment is not None
        else InputCurrentness.UNVERIFIABLE
    )
    check_audit(repository, value, deps.common, terminal=True, input_currentness=observed)
    return value


def recover_expired_checks(*, dependencies: ModelCheckWorkerDependencies, limit: int) -> int:
    deps = dependencies
    with deps.engine.connect() as db:
        ids = deps.repository_factory(db).expired_ids(now=deps.common.now(), limit=limit)
    count = 0
    for check_id in ids:
        with deps.engine.begin() as db:
            repository = deps.repository_factory(db)
            check = repository.check(check_id, for_update=True)
            if (
                check is None
                or check.state is not CheckState.RUNNING
                or check.deadline_at is None
                or check.deadline_at > deps.common.now()
            ):
                continue
            _finish(
                repository,
                check,
                ProbeOutcome(state=CheckState.UNKNOWN, reason=CheckReason.EXECUTION_EXPIRED),
                InputCurrentness.UNVERIFIABLE,
                deps,
            )
            count += 1
    return count


def _validate_input(
    check: ConnectionCheck, repository: CheckRepository, deps: ModelCheckWorkerDependencies
) -> ConnectionDefinition:
    deployment = repository.get(check.deployment_id)
    if deployment is None or deployment.state is DeploymentState.ARCHIVED:
        raise CheckBlocked(CheckReason.CANDIDATE_ARCHIVED)
    environment, connection = deps.directory.resolve(deployment.connection_ref)
    validate_connection_admission(deployment, connection, check.check_kind)
    current_input = CheckInputSnapshot.capture(
        deployment, connection, environment, check.check_kind
    )
    # The kind-specific probe version binds kind into the digest while unchanged BASIC_TEXT
    # retains its original canonical input and immutable history.
    if current_input != check.input:
        raise CheckBlocked(CheckReason.INPUT_CHANGED)
    return connection


def process_connection_check(check_id: str, *, dependencies: ModelCheckWorkerDependencies) -> bool:
    """True means this invocation reached the single-send boundary, never a retry."""
    deps = dependencies
    blocked: CheckReason | None = None
    with deps.engine.connect() as db:
        repository = deps.repository_factory(db)
        check = repository.check(check_id)
        if check is None or check.state is not CheckState.QUEUED:
            return False
        try:
            connection = _validate_input(check, repository, deps)
        except CheckBlocked as error:
            connection = None
            blocked = error.reason
        else:
            blocked = None
    prepared = None
    if blocked is None and connection is not None:
        try:
            deps.actors.require_manager(check.requested_by)
            prepared = deps.probe.prepare(connection)
        except CheckBlocked as error:
            blocked = error.reason
    with deps.engine.begin() as db:
        repository = deps.repository_factory(db)
        locked = repository.check(check_id, for_update=True)
        if locked is None or locked.state is not CheckState.QUEUED:
            return False
        if blocked is None:
            try:
                current_connection = _validate_input(locked, repository, deps)
                deps.actors.require_manager(locked.requested_by)
                if (
                    deps.probe.material_version(current_connection)
                    != current_connection.material_version
                ):
                    raise CheckBlocked(CheckReason.MATERIAL_VERSION_CHANGED)
                if repository.connection_busy(current_connection.reference):
                    raise CheckBlocked(CheckReason.CONNECTION_BUSY)
            except CheckBlocked as error:
                blocked = error.reason
        if blocked is not None:
            _finish(
                repository,
                locked,
                ProbeOutcome(state=CheckState.BLOCKED, reason=blocked),
                InputCurrentness.UNVERIFIABLE,
                deps,
            )
            return False
        now = deps.common.now()
        running = ConnectionCheck.model_validate(
            locked.model_dump()
            | {
                "state": CheckState.RUNNING,
                "revision": locked.revision + 1,
                "attempt": 1,
                "execution_token": str(deps.common.new_id()),
                "started_at": now,
                "deadline_at": now + timedelta(seconds=EXECUTION_LEASE_SECONDS),
                "material_currentness": InputCurrentness.CURRENT,
            }
        )
        if not repository.save_check(running, expected_revision=locked.revision):
            _finish(
                repository,
                locked,
                ProbeOutcome(state=CheckState.BLOCKED, reason=CheckReason.INPUT_CHANGED),
                InputCurrentness.UNVERIFIABLE,
                deps,
            )
            return False
    assert prepared is not None and connection is not None
    # No transaction or row lock spans this boundary. RUNNING is never sent again.
    try:
        outcome = deps.probe.send(prepared, running.input.provider_model_id, running.check_kind)
    except Exception:
        outcome = ProbeOutcome(state=CheckState.UNKNOWN, reason=CheckReason.REQUEST_OUTCOME_UNKNOWN)
    try:
        material = (
            InputCurrentness.CURRENT
            if deps.probe.material_version(connection) == connection.material_version
            else InputCurrentness.STALE
        )
    except CheckBlocked:
        material = InputCurrentness.UNVERIFIABLE
    with deps.engine.begin() as db:
        repository = deps.repository_factory(db)
        current = repository.check(check_id, for_update=True)
        if (
            current is None
            or current.state is not CheckState.RUNNING
            or current.execution_token != running.execution_token
            or current.revision != running.revision
        ):
            return True
        if current.deadline_at is None or current.deadline_at <= deps.common.now():
            outcome = ProbeOutcome(state=CheckState.UNKNOWN, reason=CheckReason.EXECUTION_EXPIRED)
            material = InputCurrentness.UNVERIFIABLE
        _finish(repository, current, outcome, material, deps)
    return True


def run_model_check_batch(
    *, dependencies: ModelCheckWorkerDependencies, limit: int = 20
) -> ModelCheckBatchResult:
    if not 1 <= limit <= 100:
        raise ValueError("batch limit must be between 1 and 100")
    recovered = recover_expired_checks(dependencies=dependencies, limit=limit)
    with dependencies.engine.connect() as db:
        ids = dependencies.repository_factory(db).queued_ids(limit=limit)
    attempted = sum(
        process_connection_check(check_id, dependencies=dependencies) for check_id in ids
    )
    return ModelCheckBatchResult(processed=len(ids), recovered=recovered, attempted=attempted)
