from collections.abc import Callable
from functools import lru_cache
from typing import Any, Literal, cast

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import Connection, Engine, create_engine
from sqlalchemy.exc import SQLAlchemyError

from control_plane.app import __version__
from control_plane.app.modules.agent.adapters import (
    DevActorResolver,
    DevDefinitionAvailabilityPolicy,
    DevEventCursorCodec,
    DevExecutionBindingPolicy,
    RequirementFacadeExecutionContext,
    SqlAlchemyAgentTransactionRunner,
)
from control_plane.app.modules.agent.adapters.dev_temporal import DevTemporalAdapter
from control_plane.app.modules.agent.api import AgentHttpRuntime, create_agent_router
from control_plane.app.modules.agent.api.runtime import UnboundRequirementExecutionContext
from control_plane.app.modules.agent.application.dependencies import AgentDependencies
from control_plane.app.modules.audit.adapters.sqlalchemy_repository import (
    SqlAlchemyAuditEventRepository,
)
from control_plane.app.modules.audit.adapters.transactional import (
    SqlAlchemyTransactionalAuditAppender,
)
from control_plane.app.modules.audit.api import AUDIT_READ_CAPABILITY, create_audit_router
from control_plane.app.modules.authorization import (
    ActorQualificationRuntime,
    AuthorizationDependencies,
    AuthorizationPrincipal,
    CurrentActorFactsAdapter,
    DecisionDependencies,
    Scope,
    SecurityChangeOrchestrator,
    principal_has_capability,
)
from control_plane.app.modules.authorization.adapters import (
    SqlAlchemyAuthorizationRepository,
    SqlAlchemyIdentitySessionValidator,
    SqlAlchemyOrganizationSummary,
    SqlAlchemyWorkspaceMembership,
    SqlAlchemyWorkspaceSummaries,
)
from control_plane.app.modules.authorization.api import (
    AuthorizationHttpRuntime,
    create_authorization_router,
)
from control_plane.app.modules.authorization.api.dependencies import (
    current_principal,
    require_capability,
)
from control_plane.app.modules.configuration import (
    ConfigurationDependencies,
    IdentityPolicyRuntime,
    PolicyRuntimeRegistry,
)
from control_plane.app.modules.configuration.adapters import IdentityEffectivePolicy
from control_plane.app.modules.configuration.api import (
    ConfigurationHttpRuntime,
    create_configuration_router,
)
from control_plane.app.modules.identity import (
    IdentityDependencies,
    IdentityPolicyCommandRuntime,
    IdentityPolicyReauthenticationRuntime,
    OwnedPolicySnapshotUnavailable,
    SessionPrincipal,
    current_identity_change_source,
    effective_identity_policy,
)
from control_plane.app.modules.identity.adapters.runtime import (
    SystemClock,
    SystemRandom,
)
from control_plane.app.modules.identity.adapters.sqlalchemy import SqlAlchemyIdentityRepository
from control_plane.app.modules.identity.api.admin_routes import create_admin_account_router
from control_plane.app.modules.identity.api.auth_routes import (
    IdentityHttpRuntime,
    create_auth_router,
)
from control_plane.app.modules.identity.api.super_admin_routes import (
    create_super_admin_router,
)
from control_plane.app.modules.model_gateway import CatalogDependencies
from control_plane.app.modules.model_gateway.adapters.connections import (
    FileConnectionDirectory,
    ModelConnectionSettings,
)
from control_plane.app.modules.model_gateway.api.check_routes import create_model_check_router
from control_plane.app.modules.model_gateway.api.dossier_routes import create_model_dossier_router
from control_plane.app.modules.model_gateway.api.routes import (
    ModelGatewayHttpRuntime,
    create_model_gateway_router,
)
from control_plane.app.modules.organization import OrganizationDependencies
from control_plane.app.modules.organization.adapters import (
    SqlAlchemyIdentityAccountLookup as OrganizationIdentityAccountLookup,
)
from control_plane.app.modules.organization.adapters import (
    SqlAlchemyOrganizationRepository,
)
from control_plane.app.modules.organization.api import (
    OrganizationHttpRuntime,
    create_organization_router,
)
from control_plane.app.modules.requirement import (
    RequirementDependencies,
    RequirementPolicyAuthorization,
    RequirementPolicyRuntime,
)
from control_plane.app.modules.requirement.adapters import (
    ComposedAutomaticAssignmentGuard,
    ComposedGateReviewerGuard,
    DeliveryGatePolicyAdapter,
    DeliveryReviewerGuardAdapter,
    SourceControlFacadeEvidenceAdapter,
    SqlAlchemyRequirementRepository,
    SqlAlchemySddArtifactReader,
    V04RouteSnapshotCatalog,
    WorkspaceOwnerGatePolicy,
)
from control_plane.app.modules.requirement.api import (
    FORMAL_MERGE_REQUEST_MERGE_CAPABILITY,
    FORMAL_MERGE_REQUEST_REQUEST_CAPABILITY,
    FORMAL_MERGE_REQUEST_REVIEW_CAPABILITY,
    REQUIREMENT_ACCEPTANCE_DECIDE_CAPABILITY,
    REQUIREMENT_ACCEPTANCE_SUBMIT_CAPABILITY,
    REQUIREMENT_BASELINE_ASSIGN_CAPABILITY,
    REQUIREMENT_BASELINE_DECIDE_CAPABILITY,
    REQUIREMENT_BASELINE_SUBMIT_CAPABILITY,
    REQUIREMENT_DELIVERY_GATE_ASSIGN_CAPABILITY,
    REQUIREMENT_EVIDENCE_REQUEST_CAPABILITY,
    REQUIREMENT_EVIDENCE_SELECT_CAPABILITY,
    WORK_ITEM_ASSIGN_CAPABILITY,
    WORK_ITEM_CREATE_CAPABILITY,
    WORK_ITEM_VALIDATION_SUBMIT_CAPABILITY,
    RequirementHttpRuntime,
    create_requirement_baseline_router,
    create_requirement_delivery_router,
    create_requirement_foundation_router,
    create_requirement_planning_router,
    create_requirement_v06_delivery_router,
)
from control_plane.app.modules.source_control import SourceControlDependencies
from control_plane.app.modules.source_control.adapters import (
    SqlAlchemyAgentDeliveryRepository,
    SqlAlchemySourceControlEvidenceRepository,
    SqlAlchemySourceControlRepository,
)
from control_plane.app.modules.source_control.api import (
    SourceControlQueryRuntime,
    create_agent_delivery_query_router,
    create_repository_query_router,
)
from control_plane.app.modules.workspace import (
    WorkspaceDependencies,
    on_membership_change,
)
from control_plane.app.modules.workspace.adapters import (
    SqlAlchemyIdentityAccountLookup as WorkspaceIdentityAccountLookup,
)
from control_plane.app.modules.workspace.adapters import (
    SqlAlchemyOrganizationReports,
    SqlAlchemyWorkspaceRepository,
)
from control_plane.app.modules.workspace.api import (
    WorkspaceHttpRuntime,
    create_workspace_router,
)
from control_plane.app.shared.api.camel import CamelModel
from control_plane.app.shared.api.problem import (
    PROBLEM_RESPONSES,
    SERVICE_UNAVAILABLE_RESPONSE,
    problem_response,
    register_problem_handlers,
)
from control_plane.app.shared.api.request_id import request_id_middleware
from control_plane.app.shared.db.engine import ping, runtime_engine
from control_plane.app.shared.db.settings import DbSettings, SecuritySettings
from control_plane.app.shared.security import FileSecretManager

_DEFAULT_NAVIGATION_ACTION_CAPABILITIES = frozenset(
    {
        "platform.model.manage",
        WORK_ITEM_CREATE_CAPABILITY,
        WORK_ITEM_ASSIGN_CAPABILITY,
        REQUIREMENT_BASELINE_SUBMIT_CAPABILITY,
        REQUIREMENT_BASELINE_ASSIGN_CAPABILITY,
        REQUIREMENT_BASELINE_DECIDE_CAPABILITY,
        "work_item.execute",
        FORMAL_MERGE_REQUEST_MERGE_CAPABILITY,
        FORMAL_MERGE_REQUEST_REQUEST_CAPABILITY,
        FORMAL_MERGE_REQUEST_REVIEW_CAPABILITY,
        REQUIREMENT_ACCEPTANCE_DECIDE_CAPABILITY,
        REQUIREMENT_ACCEPTANCE_SUBMIT_CAPABILITY,
        REQUIREMENT_DELIVERY_GATE_ASSIGN_CAPABILITY,
        REQUIREMENT_EVIDENCE_REQUEST_CAPABILITY,
        REQUIREMENT_EVIDENCE_SELECT_CAPABILITY,
        WORK_ITEM_VALIDATION_SUBMIT_CAPABILITY,
    }
)

API_DESCRIPTION = """内部研发平台 Control Plane API。

全局约定：JSON 一律 camelCase；ID 一律 string；错误统一 application/problem+json（RFC 9457）；
分页 cursor 型 {items, nextCursor}；写并发 If-Match/ETag；变更命令 Idempotency-Key——
后三者自 V0.2 首个真实接口起强制执行。"""


class ReadyDto(CamelModel):
    status: Literal["ready"]


@lru_cache(maxsize=1)
def identity_runtime_engine() -> Engine:
    """Identity runtime engine; constructing it does not open a database connection."""
    return create_engine(
        DbSettings().identity_database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 2},
    )


@lru_cache(maxsize=1)
def organization_runtime_engine() -> Engine:
    return create_engine(
        DbSettings().organization_database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 2},
    )


@lru_cache(maxsize=1)
def workspace_runtime_engine() -> Engine:
    return create_engine(
        DbSettings().workspace_database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 2},
    )


@lru_cache(maxsize=1)
def authorization_runtime_engine() -> Engine:
    return create_engine(
        DbSettings().authorization_database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 2},
    )


@lru_cache(maxsize=1)
def requirement_runtime_engine() -> Engine:
    return create_engine(
        DbSettings().requirement_database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 2},
    )


@lru_cache(maxsize=1)
def source_control_query_runtime_engine() -> Engine:
    return create_engine(
        DbSettings().source_control_database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 2},
    )


@lru_cache(maxsize=1)
def model_gateway_runtime_engine() -> Engine:
    return create_engine(
        DbSettings().model_gateway_database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 2},
    )


@lru_cache(maxsize=1)
def model_gateway_worker_runtime_engine() -> Engine:
    return create_engine(
        DbSettings().model_gateway_worker_database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 2},
    )


@lru_cache(maxsize=1)
def model_gateway_http_runtime() -> ModelGatewayHttpRuntime:
    return ModelGatewayHttpRuntime(
        engine=model_gateway_runtime_engine(),
        connections=FileConnectionDirectory(ModelConnectionSettings()),
        dependencies=CatalogDependencies(
            audit=SqlAlchemyTransactionalAuditAppender(),
            now=SystemClock().now,
            new_id=SystemRandom().uuid4,
        ),
        secret_manager=FileSecretManager(SecuritySettings()),
    )


@lru_cache(maxsize=1)
def agent_runtime_engine() -> Engine:
    return create_engine(
        DbSettings().agent_database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 2},
    )


def _identity_authorization_change(account_id: str) -> object:
    source = current_identity_change_source()
    if source is None or not source.source_transaction_id:
        raise RuntimeError("identity security change requires an actual source transaction")
    return security_change_orchestrator().identity_change(
        account_id,
        actor=source.actor,
        operation=source.operation,
        idempotency_key=source.idempotency_key,
        source_transaction_id=source.source_transaction_id,
        request_fingerprint=source.request_fingerprint,
        idempotency_claim_id=source.idempotency_claim_id,
    )


@lru_cache(maxsize=1)
def identity_dependencies() -> IdentityDependencies:
    return IdentityDependencies(
        repository_factory=SqlAlchemyIdentityRepository,
        secret_manager=FileSecretManager(SecuritySettings()),
        policy=IdentityEffectivePolicy(),
        clock=SystemClock(),
        random=SystemRandom(),
        audit=SqlAlchemyTransactionalAuditAppender(),
        on_auth_change=_identity_authorization_change,
    )


@lru_cache(maxsize=1)
def identity_http_runtime() -> IdentityHttpRuntime:
    return IdentityHttpRuntime(
        engine=identity_runtime_engine(),
        dependencies=identity_dependencies(),
        security_changes=security_change_orchestrator(),
    )


def _post_commit_membership_change(_account_ids: object) -> None:
    # Organization HTTP composition performs the real committed-state recompute.
    return None


@lru_cache(maxsize=1)
def organization_dependencies() -> OrganizationDependencies:
    return OrganizationDependencies(
        repository_factory=SqlAlchemyOrganizationRepository,
        identity=OrganizationIdentityAccountLookup(
            identity_runtime_engine(),
            identity_dependencies(),
        ),
        audit=SqlAlchemyTransactionalAuditAppender(),
        on_membership_change=_post_commit_membership_change,
        clock=SystemClock(),
        random=SystemRandom(),
        secret_manager=FileSecretManager(SecuritySettings()),
    )


@lru_cache(maxsize=1)
def workspace_dependencies() -> WorkspaceDependencies:
    return WorkspaceDependencies(
        repository_factory=SqlAlchemyWorkspaceRepository,
        identity=WorkspaceIdentityAccountLookup(
            identity_runtime_engine(),
            identity_dependencies(),
        ),
        organization=SqlAlchemyOrganizationReports(
            organization_runtime_engine(),
            organization_dependencies(),
        ),
        audit=SqlAlchemyTransactionalAuditAppender(),
        clock=SystemClock(),
        random=SystemRandom(),
        secret_manager=FileSecretManager(SecuritySettings()),
    )


@lru_cache(maxsize=1)
def authorization_dependencies() -> AuthorizationDependencies:
    return AuthorizationDependencies(
        repository_factory=SqlAlchemyAuthorizationRepository,
        audit=SqlAlchemyTransactionalAuditAppender(),
        clock=SystemClock(),
        random=SystemRandom(),
        secret_manager=FileSecretManager(SecuritySettings()),
    )


@lru_cache(maxsize=1)
def configuration_dependencies() -> ConfigurationDependencies:
    return ConfigurationDependencies(
        clock=SystemClock(),
        random=SystemRandom(),
        audit=SqlAlchemyTransactionalAuditAppender(),
    )


@lru_cache(maxsize=1)
def configuration_http_runtime() -> ConfigurationHttpRuntime:
    dependencies = configuration_dependencies()
    secrets = FileSecretManager(SecuritySettings())
    return ConfigurationHttpRuntime(
        owners=PolicyRuntimeRegistry(
            identity=IdentityPolicyRuntime(
                identity_runtime_engine(),
                dependencies,
                IdentityPolicyCommandRuntime(identity_runtime_engine(), identity_dependencies()),
            ),
            requirement_gate=requirement_policy_runtime(),
        ),
        dependencies=dependencies,
        secret_manager=secrets,
    )


@lru_cache(maxsize=1)
def requirement_policy_runtime() -> RequirementPolicyRuntime:
    auth = authorization_http_runtime()
    return RequirementPolicyRuntime(
        requirement_runtime_engine(),
        configuration_dependencies(),
        secret_manager=FileSecretManager(SecuritySettings()),
        reauthentication=IdentityPolicyReauthenticationRuntime(
            identity_runtime_engine(), identity_dependencies()
        ),
        authorization=RequirementPolicyAuthorization(
            auth.engine, auth.dependencies, auth.decision_dependencies
        ),
    )


@lru_cache(maxsize=1)
def actor_qualification_runtime() -> ActorQualificationRuntime:
    return ActorQualificationRuntime(
        authorization_runtime_engine(),
        authorization_dependencies(),
        CurrentActorFactsAdapter(
            identity_runtime_engine(),
            identity_dependencies(),
            workspace_runtime_engine(),
            workspace_dependencies(),
        ),
    )


@lru_cache(maxsize=1)
def requirement_dependencies() -> RequirementDependencies:
    return RequirementDependencies(
        repository_factory=SqlAlchemyRequirementRepository,
        audit=SqlAlchemyTransactionalAuditAppender(),
        denial_audit=SqlAlchemyAuditEventRepository(runtime_engine()),
        clock=SystemClock(),
        random=SystemRandom(),
        route_snapshots=V04RouteSnapshotCatalog(),
        assignment_guard=ComposedAutomaticAssignmentGuard(
            identity_engine=identity_runtime_engine(),
            identity_dependencies=identity_dependencies(),
            workspace_engine=workspace_runtime_engine(),
            workspace_dependencies=workspace_dependencies(),
            authorization_engine=authorization_runtime_engine(),
            authorization_dependencies=authorization_dependencies(),
            source_control_engine=source_control_query_runtime_engine(),
            source_control_dependencies=source_control_dependencies(),
        ),
        secret_manager=FileSecretManager(SecuritySettings()),
        artifacts=SqlAlchemySddArtifactReader(requirement_runtime_engine()),
        integration_evidence=SourceControlFacadeEvidenceAdapter(
            source_control_query_runtime_engine(),
            source_control_dependencies(),
        ),
        delivery_gate_policies=DeliveryGatePolicyAdapter(requirement_policy_runtime()),
        delivery_reviewer_guard=DeliveryReviewerGuardAdapter(actor_qualification_runtime()),
        gate_policies=WorkspaceOwnerGatePolicy(
            workspace_engine=workspace_runtime_engine(),
            workspace_dependencies=workspace_dependencies(),
        ),
        reviewer_guard=ComposedGateReviewerGuard(
            identity_engine=identity_runtime_engine(),
            identity_dependencies=identity_dependencies(),
            workspace_engine=workspace_runtime_engine(),
            workspace_dependencies=workspace_dependencies(),
            authorization_engine=authorization_runtime_engine(),
            authorization_dependencies=authorization_dependencies(),
        ),
    )


@lru_cache(maxsize=1)
def requirement_http_runtime() -> RequirementHttpRuntime:
    return RequirementHttpRuntime(
        engine=requirement_runtime_engine(),
        dependencies=requirement_dependencies(),
    )


@lru_cache(maxsize=1)
def agent_dependencies() -> AgentDependencies:
    clock = SystemClock()
    random = SystemRandom()
    return AgentDependencies(
        transaction_runner=SqlAlchemyAgentTransactionRunner(agent_runtime_engine()),
        requirement_context=UnboundRequirementExecutionContext(),
        binding_policy=DevExecutionBindingPolicy(),
        definition_availability=DevDefinitionAvailabilityPolicy(),
        actor_resolver=DevActorResolver(),
        clock=clock.now,
        new_id=lambda: str(random.uuid4()),
        cursor_codec=DevEventCursorCodec(),
        secret_manager=FileSecretManager(SecuritySettings()),
        workflow_orchestrator=DevTemporalAdapter(),
    )


def _agent_requirement_context(db: Connection) -> RequirementFacadeExecutionContext:
    return RequirementFacadeExecutionContext(db, requirement_dependencies())


@lru_cache(maxsize=1)
def agent_http_runtime() -> AgentHttpRuntime:
    return AgentHttpRuntime(
        engine=agent_runtime_engine(),
        dependencies=agent_dependencies(),
        requirement_engine=requirement_runtime_engine(),
        requirement_context_factory=_agent_requirement_context,
    )


@lru_cache(maxsize=1)
def source_control_dependencies() -> SourceControlDependencies:
    return SourceControlDependencies(
        repository_factory=SqlAlchemySourceControlRepository,
        engine=source_control_query_runtime_engine(),
        requirement=None,
        eligibility=None,
        audit=SqlAlchemyTransactionalAuditAppender(),
        clock=SystemClock(),
        random=SystemRandom(),
        agent_delivery_repository_factory=SqlAlchemyAgentDeliveryRepository,
        evidence_repository_factory=SqlAlchemySourceControlEvidenceRepository,
    )


@lru_cache(maxsize=1)
def source_control_query_runtime() -> SourceControlQueryRuntime:
    return SourceControlQueryRuntime(
        engine=source_control_query_runtime_engine(),
        dependencies=source_control_dependencies(),
    )


@lru_cache(maxsize=1)
def security_change_orchestrator() -> SecurityChangeOrchestrator:
    return SecurityChangeOrchestrator(
        authorization_runtime_engine(),
        authorization_dependencies(),
        recompute_membership=on_membership_change(
            workspace_runtime_engine(),
            dependencies=workspace_dependencies(),
        ),
    )


@lru_cache(maxsize=1)
def authorization_http_runtime() -> AuthorizationHttpRuntime:
    workspace_membership = SqlAlchemyWorkspaceMembership(
        workspace_runtime_engine(),
        workspace_dependencies(),
    )
    return AuthorizationHttpRuntime(
        engine=authorization_runtime_engine(),
        dependencies=authorization_dependencies(),
        decision_dependencies=DecisionDependencies(
            identity=SqlAlchemyIdentitySessionValidator(
                identity_runtime_engine(),
                identity_dependencies(),
                security_changes=security_change_orchestrator(),
            ),
            workspace=workspace_membership,
            reconcile=security_change_orchestrator().reconcile_for_account,
        ),
        organization_summary=SqlAlchemyOrganizationSummary(
            organization_runtime_engine(),
            organization_dependencies(),
        ),
        workspace_summaries=SqlAlchemyWorkspaceSummaries(
            workspace_runtime_engine(),
            workspace_dependencies(),
        ),
    )


@lru_cache(maxsize=1)
def organization_http_runtime() -> OrganizationHttpRuntime:
    return OrganizationHttpRuntime(
        engine=organization_runtime_engine(),
        dependencies=organization_dependencies(),
        security_changes=security_change_orchestrator(),
    )


@lru_cache(maxsize=1)
def workspace_http_runtime() -> WorkspaceHttpRuntime:
    return WorkspaceHttpRuntime(
        engine=workspace_runtime_engine(),
        dependencies=workspace_dependencies(),
        security_changes=security_change_orchestrator(),
    )


def authorization_capability_guard(
    principal: Any,
    capability: str,
    workspace_id: str | None,
) -> None:
    resolved = cast(AuthorizationPrincipal, principal)
    scope = Scope.workspace(workspace_id) if workspace_id is not None else Scope.platform()
    runtime = authorization_http_runtime()
    try:
        with runtime.engine.begin() as db:
            allowed = principal_has_capability(
                db,
                principal=resolved,
                capability=capability,
                scope=scope,
                dependencies=runtime.dependencies,
                decision_dependencies=runtime.decision_dependencies,
            )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Authorization unavailable",
        ) from exc
    if not allowed:
        raise HTTPException(status_code=403, detail="Forbidden")


def create_app(
    *,
    identity_runtime_provider: Callable[[], IdentityHttpRuntime] = identity_http_runtime,
    requirement_runtime_provider: Callable[[], RequirementHttpRuntime] = requirement_http_runtime,
    agent_runtime_provider: Callable[[], AgentHttpRuntime] = agent_http_runtime,
    source_control_query_runtime_provider: Callable[
        [], SourceControlQueryRuntime
    ] = source_control_query_runtime,
) -> FastAPI:
    app = FastAPI(
        title="engineering-platform-control-plane",
        version=__version__,
        description=API_DESCRIPTION,
    )
    register_problem_handlers(app)
    app.middleware("http")(request_id_middleware)

    @app.get(
        "/healthz",
        operation_id="system_healthz",
        responses={500: PROBLEM_RESPONSES[500]},
    )
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # 就绪返回固定 DTO；数据库不可达时返回 Problem Details。
    # 同步定义：ping 是阻塞 IO，由 FastAPI 放入线程池执行，不阻塞事件循环。
    @app.get(
        "/readyz",
        operation_id="system_readyz",
        response_model=ReadyDto,
        responses={503: SERVICE_UNAVAILABLE_RESPONSE, 500: PROBLEM_RESPONSES[500]},
    )
    def readyz() -> JSONResponse | dict[str, str]:
        try:
            if not ping(runtime_engine()):
                return problem_response(503, "Not ready")
            with identity_runtime_provider().engine.connect() as db:
                effective_identity_policy(db)
        except (OwnedPolicySnapshotUnavailable, SQLAlchemyError):
            return problem_response(503, "Not ready")
        return {"status": "ready"}

    app.include_router(create_auth_router(identity_runtime_provider))
    app.include_router(
        create_authorization_router(
            authorization_http_runtime,
            published_action_capabilities=_DEFAULT_NAVIGATION_ACTION_CAPABILITIES,
        )
    )
    protected_principal = current_principal(authorization_http_runtime)
    app.include_router(
        create_admin_account_router(
            identity_runtime_provider,
            cast(Callable[[], Any], protected_principal),
            authorization_capability_guard,
        )
    )
    app.include_router(
        create_super_admin_router(
            identity_runtime_provider,
            cast(Callable[[], Any], protected_principal),
            authorization_capability_guard,
        )
    )
    app.include_router(
        create_organization_router(
            organization_http_runtime,
            cast(Callable[[], Any], protected_principal),
            authorization_capability_guard,
        )
    )
    app.include_router(
        create_workspace_router(
            workspace_http_runtime,
            cast(Callable[[], SessionPrincipal], protected_principal),
            authorization_capability_guard,
        )
    )
    app.include_router(
        create_model_dossier_router(
            model_gateway_http_runtime,
            protected_principal,
            authorization_capability_guard,
        )
    )
    app.include_router(
        create_model_check_router(
            model_gateway_http_runtime,
            protected_principal,
            authorization_capability_guard,
        )
    )
    app.include_router(
        create_model_gateway_router(
            model_gateway_http_runtime,
            protected_principal,
            authorization_capability_guard,
        )
    )
    app.include_router(
        create_configuration_router(
            configuration_http_runtime,
            cast(Callable[[], Any], protected_principal),
            authorization_capability_guard,
        )
    )
    app.include_router(
        create_requirement_foundation_router(
            requirement_runtime_provider,
            cast(Callable[[], Any], protected_principal),
            authorization_capability_guard,
        )
    )
    app.include_router(
        create_requirement_planning_router(
            requirement_runtime_provider,
            cast(Callable[[], Any], protected_principal),
            authorization_capability_guard,
        )
    )
    app.include_router(
        create_requirement_baseline_router(
            requirement_runtime_provider,
            cast(Callable[[], Any], protected_principal),
            authorization_capability_guard,
        )
    )
    app.include_router(
        create_requirement_delivery_router(
            requirement_runtime_provider,
            cast(Callable[[], Any], protected_principal),
            authorization_capability_guard,
        )
    )
    app.include_router(
        create_agent_router(
            agent_runtime_provider,
            cast(Callable[[], Any], protected_principal),
            authorization_capability_guard,
        )
    )
    app.include_router(
        create_requirement_v06_delivery_router(
            requirement_runtime_provider,
            cast(Callable[[], Any], protected_principal),
            authorization_capability_guard,
        )
    )
    app.include_router(
        create_repository_query_router(
            source_control_query_runtime_provider,
            cast(Callable[[], Any], protected_principal),
            authorization_capability_guard,
        )
    )
    app.include_router(
        create_agent_delivery_query_router(
            source_control_query_runtime_provider,
            cast(Callable[[], Any], protected_principal),
            authorization_capability_guard,
        )
    )
    app.include_router(
        create_audit_router(
            runtime_engine,
            require_capability(
                AUDIT_READ_CAPABILITY,
                runtime_provider=authorization_http_runtime,
            ),
        )
    )

    return app
