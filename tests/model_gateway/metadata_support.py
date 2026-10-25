from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import Engine

from control_plane.app.modules.model_gateway import CatalogDependencies
from control_plane.app.modules.model_gateway.application.metadata import ModelMetadataObservations
from control_plane.app.modules.model_gateway.application.metadata_worker import (
    ModelMetadataWorkerDependencies,
)
from control_plane.app.modules.model_gateway.domain import Deployment, DeploymentState
from control_plane.app.modules.model_gateway.domain.connections import ConnectionDefinition
from control_plane.app.modules.model_gateway.domain.metadata import (
    MetadataObservation,
    MetadataSourceDefinition,
    MetadataState,
)
from tests.configuration.test_draft_rebase_api import ScopedMemory
from tests.model_gateway.test_metadata_adapter import SOURCE
from tests.model_gateway.test_probe import CONNECTION

NOW = datetime(2026, 10, 26, tzinfo=UTC)


class MetadataStore(ScopedMemory):
    def __init__(self) -> None:
        super().__init__()
        self.db = object()
        self.jobs: dict[str, MetadataObservation] = {}
        self.audit: list[Any] = []
        self.fail_audit = False
        self.other_running = False
        self.transactions = 0
        self.candidate = Deployment.model_validate(
            {
                "id": str(uuid4()),
                "deployment_key": "synthetic-candidate",
                "display_name": "Synthetic",
                "provider_kind": "BAILIAN_COMPATIBLE_MODE",
                "provider_model_id": "synthetic-model",
                "declared_capabilities": ["chat"],
                "declared_context_window": 8192,
                "declared_max_output_tokens": 1024,
                "connection_ref": "model-connection:trial",
                "revision": 1,
                "state": "DRAFT",
                "created_by": "admin",
                "created_at": NOW,
                "updated_by": "admin",
                "updated_at": NOW,
            }
        )

    @contextmanager
    def connect(self) -> Iterator[object]:
        yield self.db

    @contextmanager
    def begin(self) -> Iterator[object]:
        before = deepcopy((self.jobs, self.rows, self.audit))
        self.transactions += 1
        try:
            yield self.db
        except Exception:
            self.jobs, self.rows, self.audit = before
            raise
        finally:
            self.transactions -= 1

    def append_in_transaction(self, db: object, event: Any) -> None:
        assert db is self.db and self.transactions
        if self.fail_audit:
            raise RuntimeError("synthetic audit unavailable")
        self.audit.append(event)

    def get(self, identifier: str, *, for_update: bool = False) -> Deployment | None:
        if for_update:
            assert self.transactions
        return self.candidate if identifier == self.candidate.id else None

    def observation(
        self, identifier: str, *, for_update: bool = False
    ) -> MetadataObservation | None:
        if for_update:
            assert self.transactions
        return self.jobs.get(identifier)

    def active_observation(self, identifier: str) -> bool:
        return any(
            v.deployment_id == identifier
            and v.state in {MetadataState.QUEUED, MetadataState.RUNNING}
            for v in self.jobs.values()
        )

    def insert_observation(self, value: MetadataObservation) -> bool:
        assert self.transactions
        if self.active_observation(value.deployment_id):
            return False
        self.jobs[value.id] = value
        return True

    def save_observation(self, value: MetadataObservation, *, expected_revision: int) -> bool:
        assert self.transactions
        old = self.jobs[value.id]
        if old.revision != expected_revision or old.state not in {
            MetadataState.QUEUED,
            MetadataState.RUNNING,
        }:
            return False
        if value.state is MetadataState.RUNNING and (
            self.candidate.state is not DeploymentState.DRAFT
            or self.candidate.revision != value.input.deployment_revision
        ):
            return False
        self.jobs[value.id] = value
        return True

    def connection_busy(self, reference: str) -> bool:
        assert self.transactions
        return self.other_running or any(
            x.input.connection_ref == reference and x.state is MetadataState.RUNNING
            for x in self.jobs.values()
        )

    def queued_ids(self, *, limit: int) -> list[str]:
        return [x.id for x in self.jobs.values() if x.state is MetadataState.QUEUED][:limit]

    def expired_ids(self, *, now: datetime, limit: int) -> list[str]:
        return [
            x.id
            for x in self.jobs.values()
            if x.state is MetadataState.RUNNING
            and x.deadline_at is not None
            and x.deadline_at <= now
        ][:limit]

    def list_observations(
        self, identifier: str, *, before_at: datetime | None, before_id: str | None, limit: int
    ) -> list[MetadataObservation]:
        rows = sorted(
            (x for x in self.jobs.values() if x.deployment_id == identifier),
            key=lambda x: (x.requested_at, x.id),
            reverse=True,
        )
        if before_at is not None and before_id is not None:
            rows = [x for x in rows if (x.requested_at, x.id) < (before_at, before_id)]
        return rows[:limit]


def metadata_state(adapter: Any) -> SimpleNamespace:
    h = SimpleNamespace(store=MetadataStore(), now=NOW)
    h.connection = ConnectionDefinition.model_validate(CONNECTION)
    h.source = MetadataSourceDefinition.model_validate(SOURCE)
    h.directory = SimpleNamespace(resolve=lambda _: ("TEST", h.connection))
    h.sources = SimpleNamespace(resolve=lambda _: ("TEST", h.source))
    h.common = CatalogDependencies(audit=h.store, now=lambda: h.now, new_id=uuid4)
    h.actors = SimpleNamespace(require_manager=lambda _: None)
    h.worker = ModelMetadataWorkerDependencies(
        engine=cast(Engine, h.store),
        repository_factory=lambda _: h.store,
        common=h.common,
        connections=h.directory,
        sources=h.sources,
        actors=h.actors,
        adapter=adapter,
    )
    h.service = ModelMetadataObservations(h.store, h.common, h.directory, h.sources)
    return h
