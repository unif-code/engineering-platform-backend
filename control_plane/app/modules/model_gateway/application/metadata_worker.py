from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import Connection, Engine

from control_plane.app.modules.model_gateway.application import CatalogDependencies
from control_plane.app.modules.model_gateway.application.metadata import (
    metadata_audit,
    resolve_metadata_input,
)
from control_plane.app.modules.model_gateway.application.worker import ModelCheckBatchResult
from control_plane.app.modules.model_gateway.domain.checks import CheckBlocked
from control_plane.app.modules.model_gateway.domain.metadata import (
    METADATA_LEASE_SECONDS,
    MetadataApplicability,
    MetadataBlocked,
    MetadataObservation,
    MetadataOutcome,
    MetadataReason,
    MetadataSourceDefinition,
    MetadataState,
)
from control_plane.app.modules.model_gateway.ports.checks import (
    CheckActorPort,
    ConnectionDirectoryPort,
)
from control_plane.app.modules.model_gateway.ports.metadata import (
    MetadataRepository,
    MetadataSourceDirectoryPort,
    ModelMetadataPort,
)


@dataclass(frozen=True)
class ModelMetadataWorkerDependencies:
    engine: Engine
    repository_factory: Callable[[Connection], MetadataRepository]
    common: CatalogDependencies
    connections: ConnectionDirectoryPort
    sources: MetadataSourceDirectoryPort
    actors: CheckActorPort
    adapter: ModelMetadataPort


def _finish(
    repository: MetadataRepository,
    value: MetadataObservation,
    outcome: MetadataOutcome,
    material: MetadataApplicability,
    deps: ModelMetadataWorkerDependencies,
) -> None:
    final = MetadataObservation.model_validate(
        value.model_dump()
        | outcome.model_dump()
        | {
            "revision": value.revision + 1,
            "finished_at": deps.common.now(),
            "material_currentness": material,
        }
    )
    if not repository.save_observation(final, expected_revision=value.revision):
        raise RuntimeError("metadata observation execution fence lost")
    metadata_audit(repository, final, deps.common, terminal=True)


def _source(
    value: MetadataObservation,
    repository: MetadataRepository,
    deps: ModelMetadataWorkerDependencies,
) -> MetadataSourceDefinition:
    candidate = repository.get(value.deployment_id)
    if candidate is None:
        raise MetadataBlocked(MetadataReason.CANDIDATE_ARCHIVED)
    current, source = resolve_metadata_input(candidate, deps.connections, deps.sources)
    if current != value.input:
        raise MetadataBlocked(MetadataReason.INPUT_CHANGED)
    return source


def recover_expired_metadata_observations(
    *, dependencies: ModelMetadataWorkerDependencies, limit: int
) -> int:
    deps = dependencies
    with deps.engine.connect() as db:
        ids = deps.repository_factory(db).expired_ids(now=deps.common.now(), limit=limit)
    count = 0
    for observation_id in ids:
        with deps.engine.begin() as db:
            repository = deps.repository_factory(db)
            value = repository.observation(observation_id, for_update=True)
            if (
                value is None
                or value.state is not MetadataState.RUNNING
                or value.deadline_at is None
                or value.deadline_at > deps.common.now()
            ):
                continue
            _finish(
                repository,
                value,
                MetadataOutcome(
                    state=MetadataState.UNKNOWN, reason=MetadataReason.EXECUTION_EXPIRED
                ),
                MetadataApplicability.UNVERIFIABLE,
                deps,
            )
            count += 1
    return count


def process_metadata_observation(
    observation_id: str, *, dependencies: ModelMetadataWorkerDependencies
) -> bool:
    deps = dependencies
    blocked = None
    prepared = None
    with deps.engine.connect() as db:
        repository = deps.repository_factory(db)
        value = repository.observation(observation_id)
        if value is None or value.state is not MetadataState.QUEUED:
            return False
        try:
            source = _source(value, repository, deps)
        except MetadataBlocked as error:
            source = None
            blocked = error.reason
    if source is not None:
        try:
            deps.actors.require_manager(value.requested_by)
            # Secrets are resolved outside the transaction; DNS starts only after the lease.
            prepared = deps.adapter.prepare(source)
        except MetadataBlocked as error:
            blocked = error.reason
        except CheckBlocked as error:
            blocked = MetadataReason(error.reason.value)
    with deps.engine.begin() as db:
        repository = deps.repository_factory(db)
        locked = repository.observation(observation_id, for_update=True)
        if locked is None or locked.state is not MetadataState.QUEUED:
            return False
        if blocked is None:
            try:
                current_source = _source(locked, repository, deps)
                deps.actors.require_manager(locked.requested_by)
                if deps.adapter.material_version(current_source) != current_source.material_version:
                    raise MetadataBlocked(MetadataReason.MATERIAL_VERSION_CHANGED)
                if repository.connection_busy(locked.input.connection_ref):
                    raise MetadataBlocked(MetadataReason.CONNECTION_BUSY)
            except MetadataBlocked as error:
                blocked = error.reason
            except CheckBlocked as error:
                blocked = MetadataReason(error.reason.value)
        if blocked is not None:
            _finish(
                repository,
                locked,
                MetadataOutcome(state=MetadataState.BLOCKED, reason=blocked),
                MetadataApplicability.UNVERIFIABLE,
                deps,
            )
            return False
        now = deps.common.now()
        running = MetadataObservation.model_validate(
            locked.model_dump()
            | {
                "state": MetadataState.RUNNING,
                "revision": locked.revision + 1,
                "attempt": 1,
                "execution_token": str(deps.common.new_id()),
                "started_at": now,
                "deadline_at": now + timedelta(seconds=METADATA_LEASE_SECONDS),
                "material_currentness": MetadataApplicability.MATCHING,
            }
        )
        if not repository.save_observation(running, expected_revision=locked.revision):
            _finish(
                repository,
                locked,
                MetadataOutcome(state=MetadataState.BLOCKED, reason=MetadataReason.INPUT_CHANGED),
                MetadataApplicability.UNVERIFIABLE,
                deps,
            )
            return False
    assert prepared is not None and source is not None

    def dispatch_guard() -> float:
        # Release the read transaction before DNS/HTTP; a recovered fence is never reacquired.
        with deps.engine.connect() as db:
            current = deps.repository_factory(db).observation(running.id)
            if (
                current is not None
                and current.state is MetadataState.RUNNING
                and current.revision == running.revision
                and current.execution_token == running.execution_token
                and current.deadline_at == running.deadline_at
                and current.deadline_at is not None
                and current.deadline_at > deps.common.now()
            ):
                return max(0.0, (current.deadline_at - deps.common.now()).total_seconds())
            return 0.0

    attempted = False
    try:
        if dispatch_guard() > 0:
            attempted = True
            outcome = deps.adapter.observe(
                prepared, running.input.provider_model_id, dispatch_guard=dispatch_guard
            )
        else:
            outcome = MetadataOutcome(
                state=MetadataState.UNKNOWN, reason=MetadataReason.EXECUTION_EXPIRED
            )
    except Exception:
        outcome = MetadataOutcome(
            state=MetadataState.UNKNOWN, reason=MetadataReason.REQUEST_OUTCOME_UNKNOWN
        )
    try:
        material = (
            MetadataApplicability.MATCHING
            if deps.adapter.material_version(source) == source.material_version
            else MetadataApplicability.STALE
        )
    except MetadataBlocked:
        material = MetadataApplicability.UNVERIFIABLE
    with deps.engine.begin() as db:
        repository = deps.repository_factory(db)
        current = repository.observation(observation_id, for_update=True)
        if (
            current is None
            or current.state is not MetadataState.RUNNING
            or current.execution_token != running.execution_token
            or current.revision != running.revision
        ):
            return attempted
        if current.deadline_at is None or current.deadline_at <= deps.common.now():
            outcome = MetadataOutcome(
                state=MetadataState.UNKNOWN, reason=MetadataReason.EXECUTION_EXPIRED
            )
            material = MetadataApplicability.UNVERIFIABLE
        _finish(repository, current, outcome, material, deps)
    return attempted


def run_model_metadata_batch(
    *, dependencies: ModelMetadataWorkerDependencies, limit: int = 20
) -> ModelCheckBatchResult:
    if not 1 <= limit <= 100:
        raise ValueError("batch limit must be between 1 and 100")
    recovered = recover_expired_metadata_observations(dependencies=dependencies, limit=limit)
    with dependencies.engine.connect() as db:
        ids = dependencies.repository_factory(db).queued_ids(limit=limit)
    attempted = sum(process_metadata_observation(item, dependencies=dependencies) for item in ids)
    return ModelCheckBatchResult(processed=len(ids), recovered=recovered, attempted=attempted)
