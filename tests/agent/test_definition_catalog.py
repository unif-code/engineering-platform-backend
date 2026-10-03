from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from control_plane.app.modules.agent.api import routes
from control_plane.app.modules.agent.application.definitions import list_definitions
from control_plane.app.modules.agent.application.dependencies import AgentDependencies
from control_plane.app.modules.authorization import Scope, is_super_admin_platform_capability
from tests.agent.test_repository import DEFINITION


@pytest.fixture
def catalog() -> Any:
    repository = Mock()
    second = DEFINITION.model_copy(update={"version": 2})
    repository.list_definitions.return_value = (DEFINITION, second)
    availability = Mock()
    availability.is_active.return_value = True
    audit = Mock(side_effect=AssertionError("catalog reading cannot append Audit"))
    dependencies = cast(
        AgentDependencies,
        SimpleNamespace(
            transaction_runner=lambda operation: operation(
                SimpleNamespace(repository=lambda: repository, append_audit_event=audit)
            ),
            definition_availability=availability,
        ),
    )
    guard = Mock()
    runtime = Mock(return_value=SimpleNamespace(dependencies=dependencies))
    app = FastAPI()
    app.include_router(routes.create_agent_router(cast(Any, runtime), lambda: object(), guard))
    with TestClient(app, raise_server_exceptions=False) as client:
        yield SimpleNamespace(
            repository=repository,
            availability=availability,
            dependencies=dependencies,
            guard=guard,
            runtime=runtime,
            client=client,
            second=second,
        )


def test_catalog_preserves_multiple_versions_no_store_and_only_boolean_false_filters(
    catalog: Any,
) -> None:
    response = catalog.client.get("/api/v1/agent-definitions")
    assert response.status_code == 200, response.text
    assert [(item["id"], item["version"]) for item in response.json()["items"]] == [
        (DEFINITION.id, 1),
        (DEFINITION.id, 2),
    ]
    assert response.headers["cache-control"] == "no-store"
    assert "etag" not in response.headers
    catalog.availability.is_active.side_effect = [True, False]
    assert list_definitions(dependencies=catalog.dependencies) == (DEFINITION,)
    catalog.repository.list_definitions.return_value = ()
    assert catalog.client.get("/api/v1/agent-definitions").json() == {"items": []}


@pytest.mark.parametrize("status", [401, 403])
def test_catalog_authorization_precedes_runtime_storage_and_availability(
    catalog: Any, status: int
) -> None:
    catalog.guard.side_effect = HTTPException(status)
    assert catalog.client.get("/api/v1/agent-definitions").status_code == status
    assert catalog.guard.call_args.args[1:] == ("agent.definition.read", None)
    catalog.runtime.assert_not_called()
    catalog.repository.list_definitions.assert_not_called()
    catalog.availability.is_active.assert_not_called()


@pytest.mark.parametrize("value", [None, 0, 1, "available", []])
def test_unknown_availability_is_not_true_false_or_empty_success(catalog: Any, value: Any) -> None:
    catalog.availability.is_active.return_value = value
    response = catalog.client.get("/api/v1/agent-definitions")
    assert response.status_code == 503, response.text
    assert "items" not in response.json()


@pytest.mark.parametrize("where", ["storage", "availability", "runtime"])
def test_catalog_dependency_errors_are_sanitized_service_unavailable(
    catalog: Any, where: str
) -> None:
    target = {
        "storage": catalog.repository.list_definitions,
        "availability": catalog.availability.is_active,
        "runtime": catalog.runtime,
    }[where]
    target.side_effect = RuntimeError("private-definition-dependency-sentinel")
    response = catalog.client.get("/api/v1/agent-definitions")
    assert response.status_code == 503, response.text
    assert "private-definition" not in response.text and "items" not in response.json()


@pytest.mark.parametrize("status", [401, 403])
def test_catalog_does_not_fold_current_authorization_failure_into_503(
    catalog: Any, status: int
) -> None:
    catalog.availability.is_active.side_effect = HTTPException(status)
    assert catalog.client.get("/api/v1/agent-definitions").status_code == status


@pytest.mark.parametrize(
    "field,value",
    [("id", "invalid"), ("name", ""), ("version", 0), ("input_schema", {"sdk": object()})],
)
def test_bad_definition_cannot_be_filtered_out_as_unavailable(
    catalog: Any, field: str, value: Any
) -> None:
    broken = DEFINITION.model_copy()
    object.__setattr__(broken, field, value)
    catalog.repository.list_definitions.return_value = (broken,)
    catalog.availability.is_active.return_value = False
    response = catalog.client.get("/api/v1/agent-definitions")
    assert response.status_code == 503, response.text
    catalog.availability.is_active.assert_not_called()
    assert "items" not in response.json()


def test_definition_read_is_a_finite_platform_super_admin_default() -> None:
    assert is_super_admin_platform_capability(
        "agent.definition.read", Scope.platform(), is_super_admin=True
    )
    assert not is_super_admin_platform_capability(
        "agent.definition.read", Scope.platform(), is_super_admin=False
    )
    assert not is_super_admin_platform_capability(
        "agent.definition.read", Scope.workspace("workspace-1"), is_super_admin=True
    )
    assert not is_super_admin_platform_capability(
        "agent.run.execute", Scope.platform(), is_super_admin=True
    )
