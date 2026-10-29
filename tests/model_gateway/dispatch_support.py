"""Unit storage/clock only; admission, recovery, worker and HTTP adapter remain real."""

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
from sqlalchemy import Engine

from control_plane.app.modules.model_gateway.adapters.probe import HttpxModelProbe
from control_plane.app.modules.model_gateway.adapters.secrets import FileModelSecretPort
from control_plane.app.modules.model_gateway.application.checks import ModelConnectionChecks
from control_plane.app.modules.model_gateway.application.worker import ModelCheckWorkerDependencies
from control_plane.app.modules.model_gateway.domain import DeploymentState
from control_plane.app.modules.model_gateway.domain.checks import CheckState, ConnectionCheck
from control_plane.app.modules.model_gateway.domain.connections import (
    CheckKind,
    ConnectionDefinition,
)
from tests.model_gateway.metadata_support import MetadataStore, metadata_state
from tests.model_gateway.test_probe import CONNECTION, material, public_dns, response
from tests.model_gateway.test_search_sources_probe import search_response
from tests.model_gateway.test_stream_probe import ControlledStream, chunk, event
from tests.model_gateway.test_thinking_probe import thinking_chunk
from tests.model_gateway.tool_call_support import tool_response


class CheckStore(MetadataStore):
    def __init__(self) -> None:
        super().__init__()
        self.checks: dict[str, ConnectionCheck] = {}

    @contextmanager
    def begin(self) -> Iterator[object]:
        before = deepcopy(self.checks)
        try:
            with super().begin() as db:
                yield db
        except Exception:
            self.checks = before
            raise

    def check(self, identifier: str, *, for_update: bool = False) -> ConnectionCheck | None:
        if for_update:
            assert self.transactions
        return self.checks.get(identifier)

    def active_check(self, identifier: str) -> ConnectionCheck | None:
        return next(
            (
                value
                for value in self.checks.values()
                if value.deployment_id == identifier
                and value.state in {CheckState.QUEUED, CheckState.RUNNING}
            ),
            None,
        )

    def insert_check(self, value: ConnectionCheck) -> bool:
        assert self.transactions
        if value.id in self.checks or self.active_check(value.deployment_id) is not None:
            return False
        self.checks[value.id] = value
        return True

    def save_check(self, value: ConnectionCheck, *, expected_revision: int) -> bool:
        assert self.transactions
        old = self.checks.get(value.id)
        if (
            old is None
            or old.revision != expected_revision
            or old.state
            not in {
                CheckState.QUEUED,
                CheckState.RUNNING,
            }
        ):
            return False
        if value.state is CheckState.RUNNING and (
            self.candidate.state is not DeploymentState.DRAFT
            or self.candidate.revision != value.input.deployment_revision
        ):
            return False
        self.checks[value.id] = value
        return True

    def connection_busy(self, reference: str) -> bool:
        return super().connection_busy(reference) or any(
            value.state is CheckState.RUNNING and value.input.connection_ref == reference
            for value in self.checks.values()
        )

    def queued_ids(self, *, limit: int) -> list[str]:
        return [value.id for value in self.checks.values() if value.state is CheckState.QUEUED][
            :limit
        ]

    def expired_ids(self, *, now: datetime, limit: int) -> list[str]:
        return [
            value.id
            for value in self.checks.values()
            if value.state is CheckState.RUNNING
            and value.deadline_at is not None
            and value.deadline_at <= now
        ][:limit]

    def list_checks(
        self, identifier: str, *, before_at: datetime | None, before_id: str | None, limit: int
    ) -> list[ConnectionCheck]:
        rows = sorted(
            (value for value in self.checks.values() if value.deployment_id == identifier),
            key=lambda value: (value.requested_at, value.id),
            reverse=True,
        )
        if before_at is not None and before_id is not None:
            rows = [
                value for value in rows if (value.requested_at, value.id) < (before_at, before_id)
            ]
        return rows[:limit]


def successful_response(kind: CheckKind) -> httpx.Response:
    if kind is CheckKind.BASIC_TEXT:
        return response()
    if kind is CheckKind.SEARCH_SOURCES:
        return response(search_response())
    if kind is CheckKind.TOOL_CALL:
        return response(tool_response())
    if kind is CheckKind.THINKING:
        parts = [
            event(thinking_chunk("synthetic reasoning")),
            event(chunk("synthetic answer", "stop")),
            event("[DONE]"),
        ]
    else:
        parts = [
            event(chunk()),
            event(chunk("synthetic answer")),
            event(chunk("", "stop")),
            event("[DONE]"),
        ]
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=ControlledStream(parts)
    )


def check_dispatch(
    tmp_path: Path, kind: CheckKind = CheckKind.BASIC_TEXT, *, handler: Any = None
) -> SimpleNamespace:
    material(tmp_path)
    h = metadata_state(None)
    h.store = CheckStore()
    approved = {"toolCallModelIds": ["synthetic-model"]} if kind is CheckKind.TOOL_CALL else {}
    h.connection = ConnectionDefinition.model_validate(
        CONNECTION | {"responsesSearchModelIds": ["synthetic-model"]} | approved
    )
    h.directory = SimpleNamespace(resolve=lambda _: ("TEST", h.connection))
    h.common = replace(h.common, audit=h.store)
    h.events = []
    h.clock = lambda: h.now

    def measured(request: httpx.Request) -> httpx.Response:
        row = h.store.checks[h.identifier]
        h.events.append(
            {
                "method": request.method,
                "state": row.state,
                "revision": row.revision,
                "deadline_expired": row.deadline_at is not None and row.deadline_at <= h.clock(),
                "transactions": h.store.transactions,
            }
        )
        return successful_response(kind)

    h.probe = HttpxModelProbe(
        FileModelSecretPort(tmp_path),
        transport=httpx.MockTransport(handler or measured),
        resolver=public_dns,
    )
    h.worker = ModelCheckWorkerDependencies(
        engine=cast(Engine, h.store),
        repository_factory=lambda _: h.store,
        common=h.common,
        directory=h.directory,
        actors=h.actors,
        probe=h.probe,
    )
    h.service = ModelConnectionChecks(h.store, h.common, h.directory)
    with h.store.begin():
        accepted = h.service.accept(
            h.store.candidate.id, expected_revision=1, actor="synthetic-admin", check_kind=kind
        )
    assert accepted.state is CheckState.QUEUED
    h.identifier = accepted.id
    return h
