from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from control_plane.app.modules.audit.adapters.transactional import (
    SqlAlchemyTransactionalAuditAppender,
)
from control_plane.app.modules.identity.adapters.runtime import SystemClock, SystemRandom
from control_plane.app.modules.source_control import (
    SourceControlDependencies,
    SourceControlDependencyUnavailable,
    validate_authorized_repository_runtime,
)
from control_plane.app.modules.source_control.adapters import (
    CurrentActorEligibilityAdapter,
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


class HttpClientFactory(Protocol):
    def __call__(self, **kwargs: Any) -> httpx.Client: ...


def default_http_client_factory(**kwargs: Any) -> httpx.Client:
    return httpx.Client(**kwargs)


@dataclass(frozen=True, slots=True)
class SourceControlRuntimeCollaborators:
    source_control_engine: Engine
    requirement_engine: Engine
    requirement_dependencies: Any
    identity_engine: Engine
    identity_dependencies: Any
    workspace_engine: Engine
    workspace_dependencies: Any
    authorization_engine: Engine
    authorization_dependencies: Any


@dataclass(frozen=True, slots=True)
class SourceControlRuntime:
    dependencies: SourceControlDependencies
    client: httpx.Client
    secrets: DevSecretReferenceResolver
    connection_ref: str

    def ensure_ready(self) -> None:
        try:
            with self.dependencies.engine.connect() as db:
                validate_authorized_repository_runtime(
                    db,
                    dependencies=self.dependencies,
                    secrets=self.secrets,
                    connection_ref=self.connection_ref,
                )
        except SourceControlDependencyUnavailable:
            raise
        except (OSError, SQLAlchemyError, ValueError):
            raise SourceControlDependencyUnavailable(
                "Source Control runtime configuration is unavailable"
            ) from None

    def close(self) -> None:
        if not self.client.is_closed:
            self.client.close()


def default_source_control_collaborators() -> SourceControlRuntimeCollaborators:
    from control_plane.app.bootstrap import app as control_plane_bootstrap

    return SourceControlRuntimeCollaborators(
        source_control_engine=control_plane_bootstrap.source_control_query_runtime_engine(),
        requirement_engine=control_plane_bootstrap.requirement_runtime_engine(),
        requirement_dependencies=control_plane_bootstrap.requirement_dependencies(),
        identity_engine=control_plane_bootstrap.identity_runtime_engine(),
        identity_dependencies=control_plane_bootstrap.identity_dependencies(),
        workspace_engine=control_plane_bootstrap.workspace_runtime_engine(),
        workspace_dependencies=control_plane_bootstrap.workspace_dependencies(),
        authorization_engine=control_plane_bootstrap.authorization_runtime_engine(),
        authorization_dependencies=control_plane_bootstrap.authorization_dependencies(),
    )


def build_source_control_runtime(
    settings: SourceControlDevSettings,
    *,
    collaborators: SourceControlRuntimeCollaborators,
    client_factory: HttpClientFactory = default_http_client_factory,
) -> SourceControlRuntime:
    try:
        secret_root = settings.secret_reference_root.resolve(strict=True)
        if not secret_root.is_dir():
            raise OSError
    except OSError:
        raise SourceControlDependencyUnavailable(
            "Source Control runtime configuration is unavailable"
        ) from None

    secrets = DevSecretReferenceResolver(root=secret_root)
    policy = SourceControlDevPolicy(settings)
    client = client_factory(
        base_url=str(settings.gitlab_api_url),
        timeout=settings.request_timeout_seconds,
        trust_env=False,
    )
    try:
        clock = SystemClock()
        dependencies = SourceControlDependencies(
            repository_factory=SqlAlchemySourceControlRepository,
            engine=collaborators.source_control_engine,
            requirement=RequirementFacadeBindingAdapter(
                collaborators.requirement_engine,
                collaborators.requirement_dependencies,
                clock,
            ),
            eligibility=CurrentActorEligibilityAdapter(
                identity_engine=collaborators.identity_engine,
                identity_dependencies=collaborators.identity_dependencies,
                workspace_engine=collaborators.workspace_engine,
                workspace_dependencies=collaborators.workspace_dependencies,
                authorization_engine=collaborators.authorization_engine,
                authorization_dependencies=collaborators.authorization_dependencies,
            ),
            audit=SqlAlchemyTransactionalAuditAppender(),
            clock=clock,
            random=SystemRandom(),
            gitlab=HttpxGitLabAdapter(
                client=client,
                secrets=secrets,
                connection_ref=settings.connection_id,
            ),
            policy=policy,
            webhook_secrets=secrets,
            delivery_repository_factory=SqlAlchemySourceControlIntegrationRepository,
            requirement_delivery=RequirementFacadeDeliveryAdapter(
                collaborators.requirement_engine,
                collaborators.requirement_dependencies,
            ),
            gitlab_merge_requests=HttpxGitLabMergeRequestAdapter(
                client=client,
                secrets=secrets,
                connection_ref=settings.connection_id,
            ),
        )
    except Exception:
        client.close()
        raise
    return SourceControlRuntime(
        dependencies=dependencies,
        client=client,
        secrets=secrets,
        connection_ref=settings.connection_id,
    )


def build_source_control_runtime_from_environment(
    *,
    settings_factory: Callable[
        [], SourceControlDevSettings
    ] = SourceControlDevSettings.from_environment,
    collaborators_provider: Callable[
        [], SourceControlRuntimeCollaborators
    ] = default_source_control_collaborators,
    client_factory: HttpClientFactory = default_http_client_factory,
) -> SourceControlRuntime:
    try:
        settings = settings_factory()
        collaborators = collaborators_provider()
        return build_source_control_runtime(
            settings,
            collaborators=collaborators,
            client_factory=client_factory,
        )
    except SourceControlDependencyUnavailable:
        raise
    except (OSError, ValueError, ValidationError):
        raise SourceControlDependencyUnavailable(
            "Source Control runtime configuration is unavailable"
        ) from None


@contextmanager
def source_control_runtime_context() -> Iterator[SourceControlRuntime]:
    runtime = build_source_control_runtime_from_environment()
    try:
        runtime.ensure_ready()
        yield runtime
    finally:
        runtime.close()


__all__ = [
    "SourceControlRuntime",
    "SourceControlRuntimeCollaborators",
    "build_source_control_runtime",
    "build_source_control_runtime_from_environment",
    "default_source_control_collaborators",
    "source_control_runtime_context",
]
