import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from control_plane.app.modules.model_gateway.adapters.checks import _check, _parameters
from control_plane.app.modules.model_gateway.adapters.probe import HttpxModelProbe
from control_plane.app.modules.model_gateway.adapters.secrets import FileModelSecretPort
from control_plane.app.modules.model_gateway.api import check_routes
from control_plane.app.modules.model_gateway.api.routes import ModelGatewayHttpRuntime
from control_plane.app.modules.model_gateway.application.worker import process_connection_check
from control_plane.app.modules.model_gateway.domain.connections import CheckKind
from control_plane.app.shared.api.problem import register_problem_handlers
from tests.configuration.test_api import _Secrets
from tests.model_gateway.dispatch_support import check_dispatch
from tests.model_gateway.test_probe import public_dns, response
from tests.model_gateway.test_stream_probe import ControlledStream
from tests.model_gateway.tool_call_support import tool_response


@pytest.fixture
def tool_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    h = check_dispatch(tmp_path, CheckKind.TOOL_CALL)
    h.store.checks.clear()
    h.store.audit.clear()
    h.calls = []
    h.provider = SimpleNamespace(value=tool_response(), status=200, stream=None)

    def handler(request: httpx.Request) -> httpx.Response:
        h.calls.append(request)
        assert h.store.transactions == 0
        return (
            response(h.provider.value, status=h.provider.status)
            if h.provider.stream is None
            else httpx.Response(
                200, stream=h.provider.stream, headers={"content-type": "application/json"}
            )
        )

    h.probe = HttpxModelProbe(
        FileModelSecretPort(tmp_path), transport=httpx.MockTransport(handler), resolver=public_dns
    )
    h.worker = replace(h.worker, probe=h.probe)
    h.guard = Mock()
    h.principal = SimpleNamespace(account_id="admin", is_super_admin=True)
    runtime = cast(
        ModelGatewayHttpRuntime,
        SimpleNamespace(
            engine=h.store,
            dependencies=h.common,
            secret_manager=_Secrets(),
            connections=h.directory,
        ),
    )
    monkeypatch.setattr(check_routes, "SqlAlchemyCheckRepository", lambda _: h.store)
    app = FastAPI()
    register_problem_handlers(app)
    app.include_router(
        check_routes.create_model_check_router(lambda: runtime, lambda: h.principal, h.guard)
    )
    h.path = f"/api/v1/admin/model-deployments/{h.store.candidate.id}/connection-checks"
    h.headers = {
        "Origin": "https://testserver",
        "If-Match": '"v1"',
        "Idempotency-Key": "tool-original",
    }
    with TestClient(app, base_url="https://testserver", raise_server_exceptions=False) as client:
        h.client = client
        yield h


def accept(h: Any) -> dict[str, Any]:
    result = h.client.post(h.path, json={"checkKind": "TOOL_CALL"}, headers=h.headers)
    assert result.status_code == 202, result.text
    return cast(dict[str, Any], result.json())


def test_real_api_worker_result_and_read_only_history_do_not_promote_candidate(
    tool_http: Any,
) -> None:
    h = tool_http
    before = h.store.candidate.model_dump_json()
    receipt = accept(h)
    assert receipt["state"] == "QUEUED" and h.calls == []
    assert "observation" not in receipt
    assert process_connection_check(receipt["id"], dependencies=h.worker)
    assert not process_connection_check(receipt["id"], dependencies=h.worker)
    detail = h.client.get(h.path + "/" + receipt["id"])
    assert detail.status_code == 200, detail.text
    value = detail.json()
    assert value["state"] == "SUCCEEDED" and value["currentness"] == "CURRENT"
    assert value["observation"] == {
        "kind": "TOOL_CALL",
        "consumedBytes": len(json.dumps(h.provider.value).encode()),
        "matchingCallObserved": True,
        "toolCallCompletionObserved": True,
        "localResponseClosed": True,
    }
    assert h.client.get(h.path).json()["items"][0] == value
    assert h.client.get(h.path + "/" + receipt["id"]).json() == value
    assert len(h.calls) == 1 and h.store.candidate.model_dump_json() == before
    assert len(h.store.audit) == 2
    assert all(
        secret not in detail.text + str(h.store.audit)
        for secret in [
            "synthetic-only-secret",
            "arguments",
            "platform_probe",
            "call_synthetic",
            "textObserved",
        ]
    )
    assert json.loads(h.calls[0].content)["tools"][0]["function"]["name"] == "platform_probe"


def test_missing_purpose_permission_cannot_inherit_general_or_search_approval(
    tool_http: Any,
) -> None:
    h = tool_http
    h.connection = h.connection.model_copy(update={"tool_call_model_ids": ()})
    h.probe.prepare = Mock(side_effect=AssertionError("unapproved purpose must not prepare HTTP"))
    receipt = accept(h)
    assert receipt["state"] == "BLOCKED" and receipt["reason"] == "TOOL_CALL_MODEL_NOT_ALLOWED"
    assert not process_connection_check(receipt["id"], dependencies=h.worker)
    assert h.calls == [] and len(h.store.audit) == 2
    assert h.client.get(h.path + "/" + receipt["id"]).json()["observation"] is None


def test_replay_keeps_original_receipt_before_current_purpose_or_candidate_admission(
    tool_http: Any,
) -> None:
    h = tool_http
    first = h.client.post(h.path, json={"checkKind": "TOOL_CALL"}, headers=h.headers)
    assert first.status_code == 202
    h.store.candidate = h.store.candidate.model_copy(update={"revision": 2})
    h.directory.resolve = Mock(side_effect=AssertionError("replay must not resolve current source"))
    repeated = h.client.post(h.path, json={"checkKind": "TOOL_CALL"}, headers=h.headers)
    assert repeated.status_code == 202 and repeated.content == first.content
    assert len(h.store.checks) == 1 and len(h.store.audit) == 1 and h.calls == []
    assert (
        h.client.post(h.path, json={"checkKind": "BASIC_TEXT"}, headers=h.headers).status_code
        == 409
    )
    h.guard.side_effect = HTTPException(403, "synthetic revoke")
    assert (
        h.client.post(h.path, json={"checkKind": "TOOL_CALL"}, headers=h.headers).status_code == 403
    )
    h.directory.resolve.assert_not_called()


@pytest.mark.parametrize("change", ["purpose", "version", "actor", "material"])
def test_dispatch_rechecks_new_purpose_and_existing_bindings(tool_http: Any, change: str) -> None:
    h = tool_http
    receipt = accept(h)
    if change == "purpose":
        h.connection = h.connection.model_copy(update={"tool_call_model_ids": ()})
    if change == "version":
        h.connection = h.connection.model_copy(update={"version": "changed"})
    if change == "actor":
        from control_plane.app.modules.model_gateway.domain.checks import CheckBlocked, CheckReason

        h.actors.require_manager = Mock(side_effect=CheckBlocked(CheckReason.ACTOR_INELIGIBLE))
    if change == "material":
        h.probe.material_version = Mock(return_value="rotated")
    assert not process_connection_check(receipt["id"], dependencies=h.worker)
    detail = h.client.get(h.path + "/" + receipt["id"]).json()
    assert detail["state"] == "BLOCKED" and h.calls == []


def test_cleanup_unknown_keeps_observed_match_but_not_success(tool_http: Any) -> None:
    h = tool_http

    class BrokenClose(ControlledStream):
        async def aclose(self) -> None:
            raise OSError("synthetic private close")

    h.provider.stream = BrokenClose([json.dumps(h.provider.value).encode()])
    receipt = accept(h)
    assert process_connection_check(receipt["id"], dependencies=h.worker)
    before = h.store.checks[receipt["id"]].model_dump_json()
    row = _parameters(h.store.checks[receipt["id"]])
    row["input"] = json.loads(row["input"])
    row["usage"] = json.loads(row["usage"]) if row["usage"] else None
    assert _check(row).model_dump_json() == before
    detail = h.client.get(h.path + "/" + receipt["id"]).json()
    assert detail["state"] == "UNKNOWN" and detail["reason"] == "RESPONSE_CLOSE_FAILED"
    assert detail["observation"]["matchingCallObserved"]
    assert detail["observation"]["toolCallCompletionObserved"]
    assert not detail["observation"]["localResponseClosed"]
    assert not process_connection_check(receipt["id"], dependencies=h.worker)
    h.connection = h.connection.model_copy(update={"tool_call_model_ids": ()})
    changed = h.client.get(h.path + "/" + receipt["id"]).json()
    assert changed["currentness"] == "STALE"
    assert changed["observation"] == detail["observation"]
    assert changed["finishedAt"] == detail["finishedAt"]
    assert h.store.checks[receipt["id"]].model_dump_json() == before and len(h.calls) == 1


@pytest.mark.parametrize("status", [401, 403])
def test_provider_rejection_remains_a_result_under_portal_read_authorization(
    tool_http: Any, status: int
) -> None:
    h = tool_http
    h.provider.status = status
    receipt = accept(h)
    assert process_connection_check(receipt["id"], dependencies=h.worker)
    detail = h.client.get(h.path + "/" + receipt["id"])
    assert detail.status_code == 200 and detail.json()["state"] == "FAILED"
    assert detail.json()["reason"] == "PROVIDER_REJECTED" and len(h.calls) == 1
