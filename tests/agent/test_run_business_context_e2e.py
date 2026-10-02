from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import text

import control_plane.app.bootstrap.app as bootstrap
from control_plane.app.modules.agent import accept_workflow_event
from control_plane.app.modules.agent.adapters.requirement import RequirementFacadeExecutionContext
from control_plane.app.modules.agent.adapters.sqlalchemy import SqlAlchemyAgentRepository
from control_plane.app.modules.agent.api.dto import AgentRunResponseDto
from tests.agent.test_events import event
from tests.agent.test_repository import DEFINITION, RUN, insert_predecessor_run
from tests.agent.test_run_queries import seed_run
from tests.agent.test_run_query_e2e import agent_facts
from tests.source_control.test_v06_production_e2e import Journey, _grant, _revoke, _write
from tests.source_control.test_v06_production_e2e import journey as journey
from tests.source_control.test_v06_production_e2e import production_database as production_database


@pytest.mark.parametrize("historical_absence", [False, True])
def test_public_run_always_contains_business_context_or_explicit_null(
    historical_absence: bool,
) -> None:
    run = RUN.model_copy(update={"business_context": None}) if historical_absence else RUN
    value = AgentRunResponseDto.from_domain(run).model_dump(mode="json", by_alias=True)
    assert value["businessContext"] == (
        None
        if historical_absence
        else {
            "requirementId": "10000000-0000-0000-0000-000000000816",
            "workItemId": "10000000-0000-0000-0000-000000000817",
            "assignmentId": "10000000-0000-0000-0000-000000000818",
        }
    )


def test_openapi_run_source_is_required_nullable_and_shared_by_existing_operations() -> None:
    schema = bootstrap.create_app().openapi()
    run = schema["components"]["schemas"]["AgentRunResponseDto"]
    assert "businessContext" in run["required"]
    field = run["properties"]["businessContext"]
    assert {"type": "null"} in field["anyOf"]
    reference = next(item["$ref"] for item in field["anyOf"] if "$ref" in item)
    source = schema["components"]["schemas"][reference.rsplit("/", 1)[-1]]
    assert set(source["required"]) == {"requirementId", "workItemId", "assignmentId"}
    assert source["additionalProperties"] is False
    assert all(value["format"] == "uuid" for value in source["properties"].values())
    for name in (
        "StartAgentRunResponseDto",
        "AgentRunDetailsResponseDto",
        "AgentRunListItemResponseDto",
    ):
        assert (
            schema["components"]["schemas"][name]["properties"]["run"]["$ref"]
            == "#/components/schemas/AgentRunResponseDto"
        )
    for path, method, operation in (
        ("/api/v1/agent-runs", "post", "agent_runs_start"),
        ("/api/v1/agent-runs", "get", "agent_runs_list"),
        ("/api/v1/agent-runs/{runId}", "get", "agent_runs_get"),
    ):
        assert schema["paths"][path][method]["operationId"] == operation
    request = schema["components"]["schemas"]["StartAgentRunRequestDto"]
    assert not {"businessContext", "assignmentId"} & request["properties"].keys()
    assert request["additionalProperties"] is False


@pytest.mark.parametrize(
    "missing", ["businessContext", "requirementId", "workItemId", "assignmentId"]
)
def test_public_source_missing_fields_are_not_silently_treated_as_null(missing: str) -> None:
    value = AgentRunResponseDto.from_domain(RUN).model_dump(mode="json", by_alias=True)
    if missing == "businessContext":
        del value[missing]
    else:
        del value["businessContext"][missing]
    with pytest.raises(ValidationError):
        AgentRunResponseDto.model_validate(value)


@pytest.mark.integration
def test_default_session_real_requirement_source_survives_reassignment_and_replay(
    journey: Journey,
) -> None:
    created = _write(
        journey.member,
        "/api/v1/requirements",
        {
            "workspaceId": journey.workspace_id,
            "type": "feat",
            "title": "Startup source snapshot",
            "description": "Synthetic context, no repository execution",
            "acceptanceCriteria": ["Preserve owner identities"],
            "initialRepositoryId": journey.repository_id,
        },
        status=201,
    ).json()
    source = {
        "requirementId": created["requirement"]["id"],
        "workItemId": created["workItem"]["id"],
        "assignmentId": created["assignment"]["id"],
    }
    for capability in ("agent.run.execute", "agent.run.read"):
        _grant(journey.admin, journey.member_id, capability, journey.workspace_id)
    body = {
        "workspaceId": journey.workspace_id,
        "requirementId": source["requirementId"],
        "workItemId": source["workItemId"],
        "definitionId": "00000000-0000-0000-0000-000000000800",
        "definitionVersion": 1,
        "goal": "Business identities come from the owner Port",
    }
    key = "start-business-source"
    runtime = bootstrap.agent_http_runtime()
    external = Mock(side_effect=AssertionError("source snapshot cannot execute a workflow"))
    guarded = replace(
        runtime,
        dependencies=replace(
            runtime.dependencies,
            workflow_orchestrator=SimpleNamespace(
                start=external,
                cancel=external,
                resume=external,
                lookup=external,
            ),
        ),
    )
    with patch.object(bootstrap, "agent_http_runtime", return_value=guarded):
        started = _write(journey.member, "/api/v1/agent-runs", body, status=202, key=key)
        run_id = started.json()["run"]["id"]
        assert started.json()["run"]["businessContext"] == source
        assert started.json()["run"]["workspaceId"] == journey.workspace_id
        details = journey.member.get(f"/api/v1/requirements/{source['requirementId']}").json()
        assert details["workItemAssignments"][0]["id"] == source["assignmentId"]
        work = next(item for item in details["workItems"] if item["id"] == source["workItemId"])
        reassigned = _write(
            journey.member,
            f"/api/v1/requirements/{source['requirementId']}/work-items/{source['workItemId']}:assign",
            {"humanOwnerId": journey.leader_id, "reason": "New responsible owner after startup"},
            etag=f'"v{work["revision"]}"',
        ).json()
        assert reassigned["assignment"]["id"] != source["assignmentId"]
        before = agent_facts(journey)
        with journey.database.owner.connect() as db:
            accepted_before = (
                db.execute(text("SELECT to_jsonb(t) FROM agent.idempotency_key t ORDER BY 1"))
                .scalars()
                .all()
            )
            audit_before = (
                db.execute(
                    text(
                        "SELECT to_jsonb(t) FROM audit.audit_event t "
                        "WHERE action LIKE 'agent.%' ORDER BY 1"
                    )
                )
                .scalars()
                .all()
            )
        with patch.object(
            RequirementFacadeExecutionContext,
            "resolve",
            side_effect=AssertionError("historical source must not be re-resolved"),
        ):
            replay = _write(journey.member, "/api/v1/agent-runs", body, status=202, key=key)
            assert (
                replay.content == started.content
                and replay.headers["etag"] == started.headers["etag"]
            )
            listed = journey.member.get(
                "/api/v1/agent-runs", params={"workspaceId": journey.workspace_id}
            ).json()
            detailed = journey.member.get(f"/api/v1/agent-runs/{run_id}").json()
        assert (
            listed["items"][0]["run"]["businessContext"]
            == detailed["run"]["businessContext"]
            == source
        )
        assert agent_facts(journey) == before
        with journey.database.owner.connect() as db:
            assert (
                db.execute(text("SELECT to_jsonb(t) FROM agent.idempotency_key t ORDER BY 1"))
                .scalars()
                .all()
                == accepted_before
            )
            assert (
                db.execute(
                    text(
                        "SELECT to_jsonb(t) FROM audit.audit_event t "
                        "WHERE action LIKE 'agent.%' ORDER BY 1"
                    )
                )
                .scalars()
                .all()
                == audit_before
            )
        assert journey.leader.get(f"/api/v1/agent-runs/{run_id}").status_code == 403
        assert (
            journey.member.get(
                "/api/v1/agent-runs", params={"workspaceId": str(UUID(int=999))}
            ).status_code
            == 403
        )
        _revoke(journey, journey.member_id, "agent.run.read")
        assert journey.member.get(f"/api/v1/agent-runs/{run_id}").status_code == 403
        assert (
            journey.member.get(
                "/api/v1/agent-runs", params={"workspaceId": journey.workspace_id}
            ).status_code
            == 403
        )
    external.assert_not_called()


@pytest.mark.integration
def test_default_session_legacy_source_null_query_and_cancel_remain_available(
    journey: Journey,
) -> None:
    with journey.database.engines["agent"].begin() as db:
        repository = SqlAlchemyAgentRepository(db)
        repository.insert_definition(DEFINITION)
        run, attempt, binding = seed_run(repository, 300, journey.workspace_id)
        legacy_binding = binding.model_copy(update={"id": str(UUID(int=1900))})
        legacy_attempt = attempt.model_copy(
            update={
                "id": str(UUID(int=2900)),
                "run_id": str(UUID(int=900)),
                "binding_id": legacy_binding.id,
                "binding_digest": legacy_binding.digest,
            }
        )
        legacy = run.model_copy(
            update={
                "id": legacy_attempt.run_id,
                "latest_attempt_id": legacy_attempt.id,
                "business_context": None,
                "goal_ref": (
                    "requirement:10000000-0000-0000-0000-000000000816:"
                    "work-item:10000000-0000-0000-0000-000000000817"
                ),
            }
        )
        insert_predecessor_run(db, legacy)
        repository.insert_attempt(legacy_attempt)
        repository.insert_binding(legacy_attempt.id, legacy_binding)
    for capability in ("agent.run.read", "agent.run.control"):
        _grant(journey.admin, journey.member_id, capability, journey.workspace_id)
    before = agent_facts(journey)
    listed = journey.member.get(
        "/api/v1/agent-runs", params={"workspaceId": journey.workspace_id}
    ).json()
    assert {item["run"]["id"] for item in listed["items"]} == {run.id, legacy.id}
    assert (
        next(
            item["run"]["businessContext"]
            for item in listed["items"]
            if item["run"]["id"] == legacy.id
        )
        is None
    )
    detail = journey.member.get(f"/api/v1/agent-runs/{legacy.id}").json()
    assert detail["run"]["businessContext"] is None
    assert agent_facts(journey) == before
    accepted = _write(
        journey.member,
        f"/api/v1/agent-runs/{legacy.id}/attempts/{legacy_attempt.id}/cancel",
        {},
        status=202,
        etag=f'"v{legacy_attempt.revision}"',
    )
    assert accepted.json()["attempt"]["state"] == "CANCELING"
    accept_workflow_event(
        None,
        event=event(
            legacy_attempt.id, event_id=str(uuid4()), event_type="ATTEMPT_CANCELED", sequence=1
        ),
        dependencies=bootstrap.agent_dependencies(),
    )
    final = journey.member.get(f"/api/v1/agent-runs/{legacy.id}").json()
    assert final["run"]["state"] == "CANCELED" and final["run"]["businessContext"] is None


@pytest.mark.integration
def test_corrupted_partial_source_is_unavailable_and_never_dropped_from_a_page(
    journey: Journey,
) -> None:
    with journey.database.engines["agent"].begin() as db:
        repository = SqlAlchemyAgentRepository(db)
        repository.insert_definition(DEFINITION)
        broken, _, _ = seed_run(repository, 300, journey.workspace_id)
        seed_run(repository, 400, journey.workspace_id)
    _grant(journey.admin, journey.member_id, "agent.run.read", journey.workspace_id)
    # Owner-only isolated corruption fixture; runtime INSERT/UPDATE cannot create this row.
    with journey.database.owner.begin() as db:
        db.execute(
            text("ALTER TABLE agent.agent_run DROP CONSTRAINT ck_agent_run_business_context")
        )
        db.execute(
            text("UPDATE agent.agent_run SET assignment_id=NULL WHERE id=CAST(:id AS UUID)"),
            {"id": broken.id},
        )
    before = agent_facts(journey)
    for path, params in (
        (f"/api/v1/agent-runs/{broken.id}", {}),
        ("/api/v1/agent-runs", {"workspaceId": journey.workspace_id}),
    ):
        response = journey.member.get(path, params=params)
        assert response.status_code == 503
        assert response.json()["title"] == "Agent service unavailable"
        assert "items" not in response.json() and "businessContext" not in response.json()
    assert agent_facts(journey) == before
