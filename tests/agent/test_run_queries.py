import base64
import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock
from uuid import UUID

import pytest

from control_plane.app.modules.agent.adapters.dev_policy import DevEventCursorCodec
from control_plane.app.modules.agent.application import queries
from control_plane.app.modules.agent.application.dependencies import AgentDependencies
from control_plane.app.modules.agent.domain import RunState
from tests.agent.test_repository import ATTEMPT, BINDING, EVENT, NOW, RUN


def test_inconsistent_latest_association_is_unavailable_not_an_empty_page() -> None:
    from control_plane.app.modules.agent.adapters.sqlalchemy import SqlAlchemyAgentRepository
    from control_plane.app.modules.agent.domain import AgentQueryUnavailable

    db = Mock()
    db.execute.return_value.mappings.return_value = [
        {
            "run": RUN.model_dump(mode="json"),
            "latest_attempt": None,
            "binding": None,
            "checkpoint": None,
        }
    ]
    with pytest.raises(AgentQueryUnavailable):
        SqlAlchemyAgentRepository(db).runs_page(
            RUN.workspace_id, state=None, before_at=None, before_id=None, limit=2
        )
    assert db.execute.call_count == 1
    statement, parameters = db.execute.call_args.args
    assert "run.workspace_id=CAST(:workspace_id AS UUID)" in str(statement)
    assert "LIMIT :limit" in str(statement)
    assert parameters["workspace_id"] == RUN.workspace_id and parameters["limit"] == 2


def dependencies(repository: Any) -> AgentDependencies:
    return cast(
        AgentDependencies,
        SimpleNamespace(
            transaction_runner=lambda operation: operation(
                SimpleNamespace(repository=lambda: repository)
            ),
            cursor_codec=DevEventCursorCodec(),
        ),
    )


def item(number: int) -> Any:
    from control_plane.app.modules.agent import domain

    assert hasattr(domain, "AgentRunListItem"), "joined Run read model is missing"
    run_id, attempt_id = str(UUID(int=number)), str(UUID(int=number + 100))
    attempt = ATTEMPT.model_copy(update={"id": attempt_id, "run_id": run_id})
    run = RUN.model_copy(update={"id": run_id, "latest_attempt_id": attempt_id})
    return domain.AgentRunListItem(run=run, latest_attempt=attempt, binding=BINDING)


def test_run_page_forwards_workspace_filter_and_bounded_seek_without_loading_details() -> None:
    assert hasattr(queries, "list_runs"), "Workspace run directory is missing"
    repository = Mock(spec=["runs_page"])
    first, second = item(9), item(8)
    repository.runs_page.return_value = (first, second)
    result = queries.list_runs(
        RUN.workspace_id,
        state=RunState.ACTIVE,
        cursor=None,
        limit=1,
        dependencies=dependencies(repository),
    )
    assert result.items == (first,) and result.next_cursor is not None
    repository.runs_page.assert_called_once_with(
        RUN.workspace_id, state=RunState.ACTIVE, before_at=None, before_id=None, limit=2
    )
    repository.runs_page.return_value = (second,)
    final = queries.list_runs(
        RUN.workspace_id,
        state=RunState.ACTIVE,
        cursor=result.next_cursor,
        limit=1,
        dependencies=dependencies(repository),
    )
    assert final.items == (second,) and final.next_cursor is None
    repository.runs_page.assert_called_with(
        RUN.workspace_id, state=RunState.ACTIVE, before_at=NOW, before_id=first.run.id, limit=2
    )
    for workspace, state in ((str(UUID(int=777)), RunState.ACTIVE), (RUN.workspace_id, None)):
        with pytest.raises(queries.InvalidRunCursor):
            queries.list_runs(
                workspace,
                state=state,
                cursor=result.next_cursor,
                limit=1,
                dependencies=dependencies(repository),
            )
    assert repository.runs_page.call_count == 2


@pytest.mark.parametrize(
    "payload",
    [
        [],
        [1, RUN.workspace_id, None, NOW.isoformat(), True],
        [True, RUN.workspace_id, None, NOW.isoformat(), RUN.id],
        [1, RUN.workspace_id, None, "2026-10-02", RUN.id],
        [1, RUN.workspace_id, None, "0001-01-01T00:00:00+08:00", RUN.id],
        [1, RUN.workspace_id, None, "9999-12-31T23:59:59-08:00", RUN.id],
        [1, RUN.workspace_id, None, NOW.isoformat(), "invalid"],
        [1, RUN.workspace_id, "INVALID", NOW.isoformat(), RUN.id],
    ],
)
def test_invalid_run_cursor_is_rejected_before_repository_access(payload: list[Any]) -> None:
    assert hasattr(queries, "list_runs"), "Workspace run directory is missing"
    repository = Mock(spec=["runs_page"])
    cursor = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    with pytest.raises(queries.InvalidRunCursor):
        queries.list_runs(
            RUN.workspace_id,
            state=None,
            cursor=cursor,
            limit=10,
            dependencies=dependencies(repository),
        )
    repository.runs_page.assert_not_called()


@pytest.mark.parametrize("limit", [0, 101, True])
def test_run_query_limit_is_bounded(limit: int) -> None:
    assert hasattr(queries, "list_runs"), "Workspace run directory is missing"
    repository = Mock(spec=["runs_page"])
    with pytest.raises(queries.InvalidRunPageLimit):
        queries.list_runs(
            RUN.workspace_id,
            state=None,
            cursor=None,
            limit=limit,
            dependencies=dependencies(repository),
        )
    repository.runs_page.assert_not_called()


def test_metadata_lookup_does_not_load_attempts_or_bindings() -> None:
    assert hasattr(queries, "get_run_metadata"), "lightweight authorization lookup is missing"
    repository = Mock(spec=["run_by_id"])
    repository.run_by_id.return_value = RUN
    assert queries.get_run_metadata(RUN.id, dependencies=dependencies(repository)) == RUN
    repository.run_by_id.assert_called_once_with(RUN.id)


def test_event_page_uses_existing_cursor_identity_and_a_bounded_repository_call() -> None:
    repository = Mock(spec=["run_by_id", "events_page_by_run_id"])
    repository.run_by_id.return_value = RUN
    second = EVENT.model_copy(update={"id": str(UUID(int=42)), "sequence": 2})
    repository.events_page_by_run_id.return_value = (EVENT, second)
    page = queries.list_events(RUN.id, cursor=None, limit=1, dependencies=dependencies(repository))
    assert page.items == (EVENT,) and page.next_cursor is not None
    assert DevEventCursorCodec().decode(run_id=RUN.id, cursor=page.next_cursor) == EVENT.id
    repository.events_page_by_run_id.assert_called_once_with(RUN.id, after_event_id=None, limit=2)
    repository.events_page_by_run_id.return_value = (second,)
    next_page = queries.list_events(
        RUN.id, cursor=page.next_cursor, limit=1, dependencies=dependencies(repository)
    )
    assert next_page.items == (second,) and next_page.next_cursor is None
    repository.events_page_by_run_id.assert_called_with(RUN.id, after_event_id=EVENT.id, limit=2)


def test_signed_but_invalid_event_identifier_is_not_sent_to_sql() -> None:
    repository = Mock(spec=["run_by_id", "events_page_by_run_id"])
    repository.run_by_id.return_value = RUN
    cursor = DevEventCursorCodec().encode(run_id=RUN.id, event_id="not-a-uuid")
    with pytest.raises(queries.InvalidEventCursor):
        queries.list_events(RUN.id, cursor=cursor, limit=1, dependencies=dependencies(repository))
    repository.events_page_by_run_id.assert_not_called()


def seed_run(
    repository: Any,
    number: int,
    workspace_id: str,
    *,
    source: Any = None,
    state: RunState = RunState.ACTIVE,
) -> tuple[Any, Any, Any]:
    from control_plane.app.modules.agent.domain import AttemptState, ExecutionBindingSource

    binding = BINDING.model_copy(
        update={
            "id": str(UUID(int=number + 1000)),
            "source": source or ExecutionBindingSource.DEV_FAKE,
        }
    )
    attempt = ATTEMPT.model_copy(
        update={
            "id": str(UUID(int=number + 2000)),
            "run_id": str(UUID(int=number)),
            "binding_id": binding.id,
            "binding_digest": binding.digest,
            "state": AttemptState.QUEUED if state == RunState.ACTIVE else AttemptState(state.value),
        }
    )
    run = RUN.model_copy(
        update={
            "id": attempt.run_id,
            "workspace_id": workspace_id,
            "latest_attempt_id": attempt.id,
            "state": state,
        }
    )
    repository.insert_run(run)
    repository.insert_attempt(attempt)
    repository.insert_binding(attempt.id, binding)
    return run, attempt, binding


@pytest.mark.integration
def test_postgres_run_pages_keep_same_time_rows_filters_and_actual_latest_binding(
    isolated_agent_database: Any,
) -> None:
    from sqlalchemy import event

    from control_plane.app.modules.agent.adapters.sqlalchemy import SqlAlchemyAgentRepository
    from control_plane.app.modules.agent.domain import ExecutionBindingSource, RunMutation
    from tests.agent.test_repository import DEFINITION

    with isolated_agent_database.runtime.begin() as db:
        repository = SqlAlchemyAgentRepository(db)
        repository.insert_definition(DEFINITION)
        first, old_attempt, _ = seed_run(repository, 30, RUN.workspace_id)
        second, _, _ = seed_run(repository, 20, RUN.workspace_id)
        seed_run(repository, 10, RUN.workspace_id, state=RunState.CANCELED)
        seed_run(repository, 40, str(UUID(int=444)))
        binding = BINDING.model_copy(
            update={"id": str(UUID(int=9001)), "source": ExecutionBindingSource.CONFIGURATION}
        )
        latest = old_attempt.model_copy(
            update={
                "id": str(UUID(int=9002)),
                "number": 2,
                "binding_id": binding.id,
                "binding_digest": binding.digest,
            }
        )
        repository.insert_attempt(latest)
        repository.insert_binding(latest.id, binding)
        repository.compare_and_set_run(
            first.id,
            expected_revision=1,
            mutation=RunMutation(state=RunState.ACTIVE, latest_attempt_id=latest.id, now=NOW),
        )
    statements: list[str] = []

    def captured(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: Any
    ) -> None:
        statements.append(statement)

    event.listen(isolated_agent_database.runtime, "before_cursor_execute", captured)
    try:
        with isolated_agent_database.runtime.connect() as db:
            repository = SqlAlchemyAgentRepository(db)
            page = queries.list_runs(
                RUN.workspace_id,
                state=RunState.ACTIVE,
                cursor=None,
                limit=1,
                dependencies=dependencies(repository),
            )
            assert [entry.run.id for entry in page.items] == [first.id]
            assert page.items[0].latest_attempt.id == latest.id
            assert (
                page.items[0].binding.id == binding.id
                and page.items[0].binding.source == "CONFIGURATION"
            )
            assert len(statements) == 1
            last = queries.list_runs(
                RUN.workspace_id,
                state=RunState.ACTIVE,
                cursor=page.next_cursor,
                limit=1,
                dependencies=dependencies(repository),
            )
            assert [entry.run.id for entry in last.items] == [
                second.id
            ] and last.next_cursor is None
            assert len(statements) == 2
    finally:
        event.remove(isolated_agent_database.runtime, "before_cursor_execute", captured)


@pytest.mark.integration
def test_postgres_events_keep_cursor_order_and_see_append_without_full_materialization(
    isolated_agent_database: Any,
) -> None:
    from control_plane.app.modules.agent.adapters.sqlalchemy import SqlAlchemyAgentRepository
    from tests.agent.test_repository import DEFINITION

    with isolated_agent_database.runtime.begin() as db:
        repository = SqlAlchemyAgentRepository(db)
        repository.insert_definition(DEFINITION)
        run, first, _ = seed_run(repository, 31, RUN.workspace_id)
        other, other_attempt, _ = seed_run(repository, 32, RUN.workspace_id)
        second = first.model_copy(update={"id": str(UUID(int=9902)), "number": 2})
        repository.insert_attempt(second)
        events = [
            EVENT.model_copy(
                update={
                    "id": str(UUID(int=10000 + index)),
                    "attempt_id": attempt_id,
                    "generation": generation,
                    "sequence": sequence,
                }
            )
            for index, (attempt_id, generation, sequence) in enumerate(
                [
                    (first.id, 1, 1),
                    (first.id, 1, 2),
                    (first.id, 2, 1),
                    (second.id, 1, 1),
                ]
            )
        ]
        for value in reversed(events):
            repository.append_event(value)
        foreign = EVENT.model_copy(
            update={"id": str(UUID(int=11000)), "attempt_id": other_attempt.id}
        )
        repository.append_event(foreign)
    with isolated_agent_database.runtime.connect() as db:
        repository = SqlAlchemyAgentRepository(db)
        first_page = queries.list_events(
            run.id, cursor=None, limit=2, dependencies=dependencies(repository)
        )
        assert [value.id for value in first_page.items] == [value.id for value in events[:2]]
        assert (
            queries.list_events(run.id, cursor=None, limit=2, dependencies=dependencies(repository))
            == first_page
        )
        with isolated_agent_database.runtime.begin() as writer:
            appended = EVENT.model_copy(
                update={"id": str(UUID(int=12000)), "attempt_id": first.id, "sequence": 3}
            )
            SqlAlchemyAgentRepository(writer).append_event(appended)
        second_page = queries.list_events(
            run.id, cursor=first_page.next_cursor, limit=2, dependencies=dependencies(repository)
        )
        assert [value.id for value in second_page.items] == [appended.id, events[2].id]
        final = queries.list_events(
            run.id, cursor=second_page.next_cursor, limit=2, dependencies=dependencies(repository)
        )
        assert [value.id for value in final.items] == [events[3].id] and final.next_cursor is None
        for event_id in (foreign.id, str(UUID(int=999999))):
            cursor = DevEventCursorCodec().encode(run_id=run.id, event_id=event_id)
            with pytest.raises(queries.InvalidEventCursor):
                queries.list_events(
                    run.id, cursor=cursor, limit=2, dependencies=dependencies(repository)
                )
