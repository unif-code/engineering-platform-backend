from control_plane.app.modules.authorization.application.common import (
    audit,
    grant_dto,
    principal_version_dto,
)
from control_plane.app.modules.authorization.application.dependencies import (
    AuthorizationDependencies,
    DecisionDependencies,
)
from control_plane.app.modules.authorization.domain import (
    AuthorizationDecision,
    AuthorizationPrincipal,
    DecisionCode,
    Scope,
    ScopedCapability,
    ScopeType,
)
from control_plane.app.modules.authorization.domain.errors import AuthorizationUnavailable
from control_plane.app.modules.authorization.ports import AuthorizationRepository


def _decision_audit(
    repository: AuthorizationRepository,
    *,
    dependencies: AuthorizationDependencies,
    actor: str,
    capability: str,
    scope: Scope,
    result: str,
    version: int | None,
    reason: str,
) -> None:
    audit(
        repository,
        dependencies=dependencies,
        actor=actor,
        action="authorization.decision",
        target_type=scope.scope_type.value,
        target_id=scope.scope_id or "PLATFORM",
        result=result,
        reason=(
            f"capability={capability}; scope={scope.scope_type.value}:"
            f"{scope.scope_id or '-'}; authorizationVersion={version or 'unknown'}; "
            f"reason={reason}"
        ),
    )


def _denial(
    repository: AuthorizationRepository,
    *,
    dependencies: AuthorizationDependencies,
    actor: str,
    capability: str,
    scope: Scope,
    code: DecisionCode,
    version: int | None,
    reason: str,
) -> AuthorizationDecision:
    _decision_audit(
        repository,
        dependencies=dependencies,
        actor=actor,
        capability=capability,
        scope=scope,
        result="UNAVAILABLE" if code is DecisionCode.UNAVAILABLE else "DENY",
        version=version,
        reason=reason,
    )
    return AuthorizationDecision(allowed=False, code=code)


def _unavailable(
    repository: AuthorizationRepository,
    *,
    dependencies: AuthorizationDependencies,
    actor: str,
    capability: str,
    scope: Scope,
    version: int | None,
    reason: str,
) -> AuthorizationDecision:
    try:
        return _denial(
            repository,
            dependencies=dependencies,
            actor=actor,
            capability=capability,
            scope=scope,
            code=DecisionCode.UNAVAILABLE,
            version=version,
            reason=reason,
        )
    except Exception:
        return AuthorizationDecision(allowed=False, code=DecisionCode.UNAVAILABLE)


def _principal_capabilities(
    repository: AuthorizationRepository,
    *,
    account_id: str,
    decision_dependencies: DecisionDependencies,
    dependencies: AuthorizationDependencies,
) -> tuple[ScopedCapability, ...]:
    values: list[ScopedCapability] = []
    rows = repository.effective_grants(
        principal_id=account_id,
        capability=None,
        scope_type=None,
        scope_id=None,
        now=dependencies.clock.now(),
    )
    for row in rows:
        item = grant_dto(row)
        if item.scope.scope_type is ScopeType.WORKSPACE:
            assert item.scope.scope_id is not None
            if not decision_dependencies.workspace.is_formal_member(
                item.scope.scope_id,
                account_id,
            ):
                continue
        values.append(ScopedCapability(capability=item.capability, scope=item.scope))
    return tuple(values)


def authorize(
    repository: AuthorizationRepository,
    *,
    raw_token: str,
    capability: str,
    scope: Scope,
    dependencies: AuthorizationDependencies,
    decision_dependencies: DecisionDependencies,
) -> AuthorizationDecision:
    try:
        session = decision_dependencies.identity.validate(raw_token)
    except Exception:
        return _unavailable(
            repository,
            dependencies=dependencies,
            actor="SYSTEM",
            capability=capability,
            scope=scope,
            version=None,
            reason="identity projection unavailable",
        )
    if session is None or str(getattr(session, "session_kind", "")) != "FULL":
        return AuthorizationDecision(allowed=False, code=DecisionCode.UNAUTHENTICATED)
    account_id = str(session.account_id)
    try:
        state_row = repository.principal_version(account_id)
    except Exception:
        return _unavailable(
            repository,
            dependencies=dependencies,
            actor=account_id,
            capability=capability,
            scope=scope,
            version=None,
            reason="authorization version unavailable",
        )
    if state_row is None:
        return _unavailable(
            repository,
            dependencies=dependencies,
            actor=account_id,
            capability=capability,
            scope=scope,
            version=None,
            reason="authorization version unknown",
        )
    state = principal_version_dto(state_row)
    if state.dirty_generation is not None:
        reconciled = False
        if decision_dependencies.reconcile is not None:
            try:
                reconciled = decision_dependencies.reconcile(account_id)
            except Exception:
                reconciled = False
        if reconciled:
            try:
                state_row = repository.principal_version(account_id)
            except Exception:
                state_row = None
            if state_row is not None:
                state = principal_version_dto(state_row)
        if state.dirty_generation is not None:
            return _unavailable(
                repository,
                dependencies=dependencies,
                actor=account_id,
                capability=capability,
                scope=scope,
                version=state.version,
                reason="authorization convergence pending",
            )
    try:
        matching = repository.effective_grants(
            principal_id=account_id,
            capability=capability,
            scope_type=scope.scope_type.value,
            scope_id=scope.scope_id,
            now=dependencies.clock.now(),
        )
    except Exception:
        return _unavailable(
            repository,
            dependencies=dependencies,
            actor=account_id,
            capability=capability,
            scope=scope,
            version=state.version,
            reason="exact grant unavailable",
        )
    if not matching:
        return _denial(
            repository,
            dependencies=dependencies,
            actor=account_id,
            capability=capability,
            scope=scope,
            code=DecisionCode.DENIED,
            version=state.version,
            reason="grant missing or inactive",
        )
    if scope.scope_type is ScopeType.WORKSPACE:
        assert scope.scope_id is not None
        try:
            member = decision_dependencies.workspace.is_formal_member(
                scope.scope_id,
                account_id,
            )
        except Exception:
            return _unavailable(
                repository,
                dependencies=dependencies,
                actor=account_id,
                capability=capability,
                scope=scope,
                version=state.version,
                reason="workspace membership unavailable",
            )
        if not member:
            return _denial(
                repository,
                dependencies=dependencies,
                actor=account_id,
                capability=capability,
                scope=scope,
                code=DecisionCode.DENIED,
                version=state.version,
                reason="workspace membership missing",
            )
    try:
        capabilities = _principal_capabilities(
            repository,
            account_id=account_id,
            decision_dependencies=decision_dependencies,
            dependencies=dependencies,
        )
    except Exception:
        return _unavailable(
            repository,
            dependencies=dependencies,
            actor=account_id,
            capability=capability,
            scope=scope,
            version=state.version,
            reason="effective capability projection unavailable",
        )
    principal = AuthorizationPrincipal(
        account_id=account_id,
        employee_id=str(session.employee_no),
        name=str(session.display_name),
        is_super_admin=bool(session.is_super_admin),
        authorization_version=state.version,
        capabilities=capabilities,
    )
    return AuthorizationDecision(
        allowed=True,
        code=DecisionCode.ALLOW,
        principal=principal,
    )


def resolve_principal(
    repository: AuthorizationRepository,
    *,
    raw_token: str,
    dependencies: AuthorizationDependencies,
    decision_dependencies: DecisionDependencies,
) -> AuthorizationDecision:
    """Resolve a fence-safe principal without granting any particular action."""
    try:
        session = decision_dependencies.identity.validate(raw_token)
    except Exception:
        return _unavailable(
            repository,
            dependencies=dependencies,
            actor="SYSTEM",
            capability="authorization.principal.resolve",
            scope=Scope.platform(),
            version=None,
            reason="identity projection unavailable",
        )
    if session is None or str(getattr(session, "session_kind", "")) != "FULL":
        return AuthorizationDecision(allowed=False, code=DecisionCode.UNAUTHENTICATED)

    account_id = str(session.account_id)
    try:
        state_row = repository.principal_version(account_id)
    except Exception:
        return _unavailable(
            repository,
            dependencies=dependencies,
            actor=account_id,
            capability="authorization.principal.resolve",
            scope=Scope.platform(),
            version=None,
            reason="authorization version unavailable",
        )
    if state_row is None:
        return _unavailable(
            repository,
            dependencies=dependencies,
            actor=account_id,
            capability="authorization.principal.resolve",
            scope=Scope.platform(),
            version=None,
            reason="authorization version unknown",
        )
    state = principal_version_dto(state_row)
    if state.dirty_generation is not None:
        reconciled = False
        if decision_dependencies.reconcile is not None:
            try:
                reconciled = decision_dependencies.reconcile(account_id)
            except Exception:
                reconciled = False
        if reconciled:
            try:
                state_row = repository.principal_version(account_id)
            except Exception:
                state_row = None
            if state_row is not None:
                state = principal_version_dto(state_row)
        if state.dirty_generation is not None:
            return _unavailable(
                repository,
                dependencies=dependencies,
                actor=account_id,
                capability="authorization.principal.resolve",
                scope=Scope.platform(),
                version=state.version,
                reason="authorization convergence pending",
            )
    try:
        capabilities = _principal_capabilities(
            repository,
            account_id=account_id,
            decision_dependencies=decision_dependencies,
            dependencies=dependencies,
        )
    except Exception:
        return _unavailable(
            repository,
            dependencies=dependencies,
            actor=account_id,
            capability="authorization.principal.resolve",
            scope=Scope.platform(),
            version=state.version,
            reason="effective capability projection unavailable",
        )
    return AuthorizationDecision(
        allowed=True,
        code=DecisionCode.ALLOW,
        principal=AuthorizationPrincipal(
            account_id=account_id,
            employee_id=str(session.employee_no),
            name=str(session.display_name),
            is_super_admin=bool(session.is_super_admin),
            authorization_version=state.version,
            capabilities=capabilities,
        ),
    )


def principal_has_capability(
    repository: AuthorizationRepository,
    *,
    principal: AuthorizationPrincipal,
    capability: str,
    scope: Scope,
    dependencies: AuthorizationDependencies,
    decision_dependencies: DecisionDependencies | None = None,
) -> bool:
    try:
        state_row = repository.principal_version(principal.account_id)
    except Exception as exc:
        raise AuthorizationUnavailable("authorization version unavailable") from exc
    if state_row is None:
        raise AuthorizationUnavailable("authorization version unknown")
    state = principal_version_dto(state_row)
    if state.dirty_generation is not None or state.version != principal.authorization_version:
        raise AuthorizationUnavailable("authorization principal changed")
    try:
        matching = repository.effective_grants(
            principal_id=principal.account_id,
            capability=capability,
            scope_type=scope.scope_type.value,
            scope_id=scope.scope_id,
            now=dependencies.clock.now(),
        )
    except Exception as exc:
        raise AuthorizationUnavailable("exact grant unavailable") from exc
    if matching and scope.scope_type is ScopeType.WORKSPACE:
        if decision_dependencies is None or scope.scope_id is None:
            raise AuthorizationUnavailable("workspace membership unavailable")
        try:
            member = decision_dependencies.workspace.is_formal_member(
                scope.scope_id,
                principal.account_id,
            )
        except Exception as exc:
            raise AuthorizationUnavailable("workspace membership unavailable") from exc
        if not member:
            matching = []
    if matching:
        return True
    _decision_audit(
        repository,
        dependencies=dependencies,
        actor=principal.account_id,
        capability=capability,
        scope=scope,
        result="DENY",
        version=principal.authorization_version,
        reason="grant missing, inactive, or outside current membership",
    )
    return False
