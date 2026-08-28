import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

import control_plane.app.bootstrap.source_control_runtime as runtime_module
from control_plane.app.bootstrap.source_control_connector import (
    create_source_control_connector_app,
)
from control_plane.app.bootstrap.source_control_runtime import (
    SourceControlRuntime,
    SourceControlRuntimeCollaborators,
    build_source_control_runtime,
    build_source_control_runtime_from_environment,
)
from control_plane.app.modules.source_control import SourceControlDependencyUnavailable
from control_plane.app.modules.source_control.adapters import (
    CurrentOwnerEligibilityAdapter,
    DevSecretReferenceResolver,
    HttpxGitLabAdapter,
    HttpxGitLabMergeRequestAdapter,
    RequirementFacadeBindingAdapter,
    RequirementFacadeDeliveryAdapter,
    SourceControlDevPolicy,
    SourceControlDevSettings,
    SqlAlchemySourceControlIntegrationRepository,
    SqlAlchemySourceControlRepository,
)
from control_plane.tools.source_control_worker import main as worker_main
from tests.source_control.conftest import IsolatedSourceControlDatabase


def _settings(secret_root: Path) -> SourceControlDevSettings:
    return SourceControlDevSettings.model_validate(
        {
            "gitlab_api_url": "https://gitlab.dev.example/api/v4",
            "connection_id": "gitlab-dev",
            "request_timeout_seconds": 5,
            "policy_version": 1,
            "reconcile_base_delay_seconds": 15,
            "reconcile_max_delay_seconds": 120,
            "webhook_replay_window_seconds": 300,
            "secret_reference_root": secret_root,
        }
    )


def _collaborators(
    source_control_engine: Engine | None = None,
) -> SourceControlRuntimeCollaborators:
    engines = {
        name: cast(Engine, object())
        for name in (
            "source_control",
            "requirement",
            "identity",
            "workspace",
            "authorization",
        )
    }
    return SourceControlRuntimeCollaborators(
        source_control_engine=source_control_engine or engines["source_control"],
        requirement_engine=engines["requirement"],
        requirement_dependencies=object(),
        identity_engine=engines["identity"],
        identity_dependencies=object(),
        workspace_engine=engines["workspace"],
        workspace_dependencies=object(),
        authorization_engine=engines["authorization"],
        authorization_dependencies=object(),
    )


def test_complete_non_secret_settings_build_one_shared_runtime(tmp_path: Path) -> None:
    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    requests: list[httpx.Request] = []
    client_options: dict[str, Any] = {}

    def client_factory(**kwargs: Any) -> httpx.Client:
        client_options.update(kwargs)

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(500)

        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    runtime = build_source_control_runtime(
        _settings(secret_root),
        collaborators=_collaborators(),
        client_factory=client_factory,
    )
    dependencies = runtime.dependencies

    assert dependencies.repository_factory is SqlAlchemySourceControlRepository
    assert dependencies.delivery_repository_factory is SqlAlchemySourceControlIntegrationRepository
    assert isinstance(dependencies.requirement, RequirementFacadeBindingAdapter)
    assert isinstance(dependencies.requirement_delivery, RequirementFacadeDeliveryAdapter)
    assert isinstance(dependencies.eligibility, CurrentOwnerEligibilityAdapter)
    assert isinstance(dependencies.gitlab, HttpxGitLabAdapter)
    assert isinstance(dependencies.gitlab_merge_requests, HttpxGitLabMergeRequestAdapter)
    assert isinstance(dependencies.webhook_secrets, DevSecretReferenceResolver)
    assert isinstance(dependencies.policy, SourceControlDevPolicy)
    assert dependencies.gitlab.client is runtime.client
    assert dependencies.gitlab_merge_requests.client is runtime.client
    assert dependencies.gitlab.secrets is dependencies.webhook_secrets
    assert dependencies.gitlab.connection_ref == "gitlab-dev"
    assert client_options["trust_env"] is False
    assert requests == []

    runtime.close()
    assert runtime.client.is_closed


def test_connector_readiness_fails_closed_for_missing_authorized_secret_reference(
    isolated_source_control_database: IsolatedSourceControlDatabase,
    tmp_path: Path,
) -> None:
    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    with isolated_source_control_database.runtime.begin() as db:
        db.execute(
            text(
                "INSERT INTO source_control.workspace_repository "
                "(id, workspace_id, provider, project_id, project_path, default_branch, "
                "connection_ref, credential_secret_ref, webhook_signing_secret_ref, "
                "status, revision) VALUES "
                "('10000000-0000-0000-0000-000000000801', "
                "'20000000-0000-0000-0000-000000000801', 'GITLAB', '801', "
                "'platform/backend', 'main', 'gitlab-dev', "
                "'secret-ref:missing-pat', 'secret-ref:missing-webhook', "
                "'AUTHORIZED', 1)"
            )
        )
    runtime = build_source_control_runtime(
        _settings(secret_root),
        collaborators=_collaborators(isolated_source_control_database.runtime),
        client_factory=lambda **kwargs: httpx.Client(
            transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
            **kwargs,
        ),
    )

    @contextmanager
    def runtime_context() -> Iterator[SourceControlRuntime]:
        try:
            yield runtime
        finally:
            runtime.close()

    app = create_source_control_connector_app(runtime_context_provider=runtime_context)
    with TestClient(app) as client:
        ready = client.get("/readyz")

    assert ready.status_code == 503
    assert "missing-pat" not in ready.text
    assert "missing-webhook" not in ready.text
    assert runtime.client.is_closed


def test_worker_exits_nonzero_before_an_empty_batch_when_authorized_secret_is_missing(
    isolated_source_control_database: IsolatedSourceControlDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    with isolated_source_control_database.runtime.begin() as db:
        db.execute(
            text(
                "INSERT INTO source_control.workspace_repository "
                "(id, workspace_id, provider, project_id, project_path, default_branch, "
                "connection_ref, credential_secret_ref, webhook_signing_secret_ref, "
                "status, revision) VALUES "
                "('10000000-0000-0000-0000-000000000802', "
                "'20000000-0000-0000-0000-000000000802', 'GITLAB', '802', "
                "'platform/worker', 'main', 'gitlab-dev', "
                "'secret-ref:missing-worker-pat', NULL, 'AUTHORIZED', 1)"
            )
        )
    runtime = build_source_control_runtime(
        _settings(secret_root),
        collaborators=_collaborators(isolated_source_control_database.runtime),
        client_factory=lambda **kwargs: httpx.Client(
            transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
            **kwargs,
        ),
    )
    monkeypatch.setattr(
        runtime_module,
        "build_source_control_runtime_from_environment",
        lambda: runtime,
    )

    exit_code = worker_main(
        ["relay", "--limit", "2"],
        runtime_context_provider=runtime_module.source_control_runtime_context,
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert json.loads(output) == {
        "command": "relay",
        "errorCodes": ["DEPENDENCY_UNAVAILABLE"],
    }
    assert "missing-worker-pat" not in output
    assert runtime.client.is_closed


def test_incomplete_environment_fails_before_http_client_creation(tmp_path: Path) -> None:
    client_created = False

    def incomplete_settings() -> SourceControlDevSettings:
        return SourceControlDevSettings.model_validate({})

    def forbidden_client(**_kwargs: Any) -> httpx.Client:
        nonlocal client_created
        client_created = True
        raise AssertionError("HTTP client must not be created")

    with pytest.raises(SourceControlDependencyUnavailable):
        build_source_control_runtime_from_environment(
            settings_factory=incomplete_settings,
            collaborators_provider=_collaborators,
            client_factory=forbidden_client,
        )

    assert not client_created


def test_connector_owns_and_closes_the_shared_runtime(tmp_path: Path) -> None:
    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    runtime = build_source_control_runtime(
        _settings(secret_root),
        collaborators=_collaborators(),
        client_factory=lambda **kwargs: httpx.Client(
            transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
            **kwargs,
        ),
    )

    @contextmanager
    def runtime_context() -> Iterator[SourceControlRuntime]:
        try:
            yield runtime
        finally:
            runtime.close()

    app = create_source_control_connector_app(runtime_context_provider=runtime_context)
    with TestClient(app):
        assert not runtime.client.is_closed
    assert runtime.client.is_closed


def test_connector_with_incomplete_settings_stays_503_and_sanitized() -> None:
    @contextmanager
    def unavailable_context() -> Iterator[SourceControlRuntime]:
        raise SourceControlDependencyUnavailable("private-config-detail")
        yield  # pragma: no cover

    app = create_source_control_connector_app(runtime_context_provider=unavailable_context)
    with TestClient(app) as client:
        ready = client.get("/readyz")
        webhook = client.post("/webhooks/gitlab/repository-1", content=b"private-body")

    assert ready.status_code == webhook.status_code == 503
    assert "private-config-detail" not in ready.text + webhook.text
    assert "private-body" not in webhook.text
