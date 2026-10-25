from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from control_plane.app.modules.model_gateway.adapters.metadata import HttpxModelMetadata
from control_plane.app.modules.model_gateway.adapters.secrets import FileModelSecretPort
from control_plane.app.modules.model_gateway.api import metadata_routes
from control_plane.app.modules.model_gateway.api.metadata_dto import MetadataObservationDto
from control_plane.app.modules.model_gateway.api.routes import ModelGatewayHttpRuntime
from control_plane.app.modules.model_gateway.application.metadata import (
    metadata_admission,
    metadata_applicability,
)
from control_plane.app.modules.model_gateway.application.metadata_worker import (
    process_metadata_observation,
    recover_expired_metadata_observations,
)
from control_plane.app.modules.model_gateway.domain.metadata import (
    MetadataApplicability,
    MetadataBlocked,
    MetadataReason,
    MetadataState,
)
from control_plane.app.shared.api.problem import register_problem_handlers
from tests.configuration.test_api import _Secrets
from tests.model_gateway.metadata_support import metadata_state
from tests.model_gateway.test_metadata_adapter import RESPONSE
from tests.model_gateway.test_probe import material, public_dns, response


@pytest.fixture
def metadata_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    material(tmp_path)
    calls: list[httpx.Request] = []
    state = SimpleNamespace(response=RESPONSE, status=200)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response(state.response, status=state.status)

    secrets = FileModelSecretPort(tmp_path)
    adapter = HttpxModelMetadata(
        secrets, transport=httpx.MockTransport(handler), resolver=public_dns
    )
    h = metadata_state(adapter)
    h.calls, h.provider, h.material_root = calls, state, tmp_path
    h.guard = Mock()
    h.runtime = cast(
        ModelGatewayHttpRuntime,
        SimpleNamespace(
            engine=h.store,
            dependencies=h.common,
            secret_manager=_Secrets(),
            connections=h.directory,
            metadata_sources=h.sources,
        ),
    )
    monkeypatch.setattr(metadata_routes, "SqlAlchemyMetadataRepository", lambda _db: h.store)
    app = FastAPI()
    register_problem_handlers(app)
    app.include_router(
        metadata_routes.create_model_metadata_router(
            lambda: h.runtime,
            lambda: SimpleNamespace(account_id="admin", is_super_admin=True),
            h.guard,
        )
    )
    h.path = f"/api/v1/admin/model-deployments/{h.store.candidate.id}/metadata-observations"
    with TestClient(app, base_url="https://testserver", raise_server_exceptions=False) as client:
        h.client = client
        yield h


def accepted(h: Any, *, key: str = "metadata-original-key") -> Any:
    listed = h.client.get(h.path)
    assert listed.status_code == 200, listed.text
    assert h.calls == []
    source = listed.json()["admission"]["source"]
    h.body = {"sourceFingerprint": source["fingerprint"]}
    h.headers = {"Origin": "https://testserver", "If-Match": '"v1"', "Idempotency-Key": key}
    result = h.client.post(h.path, json=h.body, headers=h.headers)
    assert result.status_code == 202, result.text
    return result


def test_real_http_admission_worker_and_detail_keep_metadata_separate_from_candidate(
    metadata_http: Any,
) -> None:
    h = metadata_http
    before = h.store.candidate.model_dump_json()
    receipt = accepted(h)
    identifier = receipt.json()["id"]
    assert receipt.json()["state"] == "QUEUED" and len(h.calls) == 0
    assert process_metadata_observation(identifier, dependencies=h.worker)
    assert not process_metadata_observation(identifier, dependencies=h.worker)
    assert len(h.calls) == 1
    detail = h.client.get(h.path + "/" + identifier)
    assert detail.status_code == 200, detail.text
    data = detail.json()
    assert data["state"] == "REPORTED"
    assert data["contextWindowComparison"]["comparison"] == "EQUAL"
    assert data["maxOutputTokensComparison"]["comparison"] == "DIFFERENT"
    assert data["report"]["maxInputTokens"] == {"state": "NULL", "value": None}
    assert data["report"]["specificationVersion"] is None and data["report"]["expiresAt"] is None
    assert h.store.candidate.model_dump_json() == before
    assert h.client.get(h.path + "/" + identifier).json() == data and len(h.calls) == 1
    assert not {"executionToken", "secretRef", "responseBody"} & data.keys()
    assert "synthetic-only-secret" not in detail.text + str(h.store.audit)
    h.source = h.source.model_copy(update={"version": "source-2"})
    changed = h.client.get(h.path + "/" + identifier)
    assert changed.json()["resultHash"] == data["resultHash"]
    assert changed.json()["applicability"] == "STALE"
    assert changed.headers["etag"] != detail.headers["etag"]
    assert len(h.calls) == 1


def test_replay_uses_original_identity_before_current_source_and_candidate_checks(
    metadata_http: Any,
) -> None:
    h = metadata_http
    receipt = accepted(h)
    h.source = h.source.model_copy(update={"version": "revoked"})
    h.store.candidate = h.store.candidate.model_copy(update={"revision": 2})
    h.sources.resolve = Mock(side_effect=AssertionError("replay must not resolve a new source"))
    replay = h.client.post(h.path, json=h.body, headers=h.headers)
    assert replay.status_code == 202 and replay.content == receipt.content
    assert len(h.store.jobs) == 1 and len(h.store.audit) == 1 and h.calls == []
    assert (
        h.client.post(h.path, json={"sourceFingerprint": "a" * 64}, headers=h.headers).status_code
        == 409
    )
    h.guard.side_effect = HTTPException(403, "synthetic revoke")
    assert h.client.post(h.path, json=h.body, headers=h.headers).status_code == 403
    h.sources.resolve.assert_not_called()


@pytest.mark.parametrize("status", [401, 403])
def test_provider_authentication_failure_does_not_become_portal_auth_failure(
    metadata_http: Any, status: int
) -> None:
    h = metadata_http
    identifier = accepted(h).json()["id"]
    h.provider.status = status
    assert process_metadata_observation(identifier, dependencies=h.worker)
    result = h.client.get(h.path + "/" + identifier)
    assert result.status_code == 200
    assert result.json()["state"] == "FAILED" and result.json()["reason"] == "PROVIDER_REJECTED"
    assert len(h.calls) == 1


def test_missing_metadata_permission_never_inherits_inference_or_reads_secret(
    metadata_http: Any,
) -> None:
    h = metadata_http
    h.sources.resolve = Mock(side_effect=MetadataBlocked(MetadataReason.SOURCE_UNCONFIGURED))
    result = h.client.get(h.path)
    assert result.status_code == 200 and result.json()["admission"] == {
        "available": False,
        "reason": "SOURCE_UNCONFIGURED",
        "source": None,
    }
    h.store.fail_audit = False
    write = h.client.post(
        h.path,
        json={"sourceFingerprint": "a" * 64},
        headers={
            "Origin": "https://testserver",
            "If-Match": '"v1"',
            "Idempotency-Key": "metadata-unconfigured-key",
        },
    )
    assert write.status_code == 409 and len(h.store.jobs) == 0 and h.calls == []


@pytest.mark.parametrize("change", ["source", "candidate", "secret", "actor", "connection-busy"])
def test_dispatch_rechecks_actual_inputs_and_stops_before_http(
    metadata_http: Any, change: str
) -> None:
    h = metadata_http
    identifier = accepted(h).json()["id"]
    if change == "source":
        h.source = h.source.model_copy(update={"version": "source-2"})
    if change == "candidate":
        h.store.candidate = h.store.candidate.model_copy(update={"revision": 2})
    if change == "secret":
        material(h.material_root, "rotated")
    if change == "actor":
        from control_plane.app.modules.model_gateway.domain.checks import CheckBlocked, CheckReason

        h.actors.require_manager = Mock(side_effect=CheckBlocked(CheckReason.ACTOR_INELIGIBLE))
    if change == "connection-busy":
        h.store.other_running = True
    assert not process_metadata_observation(identifier, dependencies=h.worker)
    assert h.store.jobs[identifier].state is MetadataState.BLOCKED and h.calls == []


def test_expired_lease_is_unknown_and_late_completion_cannot_overwrite_or_resend(
    metadata_http: Any,
) -> None:
    h = metadata_http
    identifier = accepted(h).json()["id"]
    original = h.worker.adapter.observe

    def observe(prepared: Any, model_id: str) -> Any:
        result = original(prepared, model_id)
        h.now += timedelta(seconds=26)
        assert recover_expired_metadata_observations(dependencies=h.worker, limit=1) == 1
        return result

    adapter = SimpleNamespace(
        prepare=h.worker.adapter.prepare,
        material_version=h.worker.adapter.material_version,
        observe=observe,
    )
    assert process_metadata_observation(identifier, dependencies=replace(h.worker, adapter=adapter))
    assert h.store.jobs[identifier].state is MetadataState.UNKNOWN
    assert h.store.jobs[identifier].reason is MetadataReason.EXECUTION_EXPIRED
    assert not process_metadata_observation(identifier, dependencies=h.worker)
    assert len(h.calls) == 1


def test_audit_failure_rolls_back_admission_and_idempotency_in_same_transaction(
    metadata_http: Any,
) -> None:
    h = metadata_http
    admission = h.client.get(h.path).json()["admission"]
    h.store.fail_audit = True
    result = h.client.post(
        h.path,
        json={"sourceFingerprint": admission["source"]["fingerprint"]},
        headers={
            "Origin": "https://testserver",
            "If-Match": '"v1"',
            "Idempotency-Key": "metadata-audit-fail-key",
        },
    )
    assert result.status_code == 500
    assert h.store.jobs == {} and h.store.rows == {} and h.store.audit == []


def test_detail_unknown_input_is_separate_from_immutable_result(metadata_http: Any) -> None:
    h = metadata_http
    identifier = accepted(h).json()["id"]
    assert process_metadata_observation(identifier, dependencies=h.worker)
    observation = h.store.jobs[identifier]
    original = observation.model_dump_json()
    h.sources.resolve = Mock(side_effect=MetadataBlocked(MetadataReason.SOURCE_UNAVAILABLE))
    state, reasons = metadata_applicability(observation, h.store.candidate, h.directory, h.sources)
    assert state is MetadataApplicability.UNVERIFIABLE
    dto = MetadataObservationDto.from_observation(observation, state, reasons)
    assert dto.report is not None and observation.model_dump_json() == original
    assert not metadata_admission(h.store.candidate, h.directory, h.sources).available
