from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine

from control_plane.app.modules.authorization.application.common import audit
from control_plane.app.modules.authorization.application.dependencies import (
    AuthorizationDependencies,
)
from control_plane.app.shared.api.request_id import current_request_id


@dataclass(frozen=True, slots=True)
class SecurityChangeSource:
    module: str
    actor: str
    operation: str
    idempotency_key: str
    source_transaction_id: str | None = None
    request_fingerprint: str | None = None
    idempotency_claim_id: str | None = None

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (self.module, self.actor, self.operation, self.idempotency_key)
        ):
            raise ValueError("security change source fields must not be blank")
        if (self.request_fingerprint is None) != (self.idempotency_claim_id is None):
            raise ValueError("security change claim identity must be complete")
        if self.request_fingerprint is not None and not self.request_fingerprint.strip():
            raise ValueError("security change request fingerprint must not be blank")


@dataclass(frozen=True, slots=True)
class SecurityChangeTicket:
    id: str
    source: SecurityChangeSource
    generations: dict[str, int]
    reason: str
    status: str
    created: bool

    @property
    def completed(self) -> bool:
        return self.status == "COMPLETED"

    @property
    def cancelled(self) -> bool:
        return self.status == "CANCELLED"


def _strings(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({value.strip() for value in values if value.strip()}))


def _ticket(row: Mapping[str, Any], *, created: bool = False) -> SecurityChangeTicket:
    generations = {
        str(account_id): int(generation)
        for account_id, generation in dict(row["generation_map"]).items()
    }
    return SecurityChangeTicket(
        id=str(row["id"]),
        source=SecurityChangeSource(
            module=str(row["source_module"]),
            actor=str(row["actor"]),
            operation=str(row["operation"]),
            idempotency_key=str(row["idempotency_key"]),
            source_transaction_id=(
                str(row["source_transaction_id"])
                if row["source_transaction_id"] is not None
                else None
            ),
            request_fingerprint=(
                str(row["request_fingerprint"]) if row["request_fingerprint"] is not None else None
            ),
            idempotency_claim_id=(
                str(row["idempotency_claim_id"])
                if row["idempotency_claim_id"] is not None
                else None
            ),
        ),
        generations=generations,
        reason=str(row["reason"]),
        status=str(row["status"]),
        created=created,
    )


class SecurityChangeOrchestrator:
    """Authorization-owned durable source-to-projection convergence coordinator."""

    def __init__(
        self,
        engine: Engine,
        dependencies: AuthorizationDependencies,
        *,
        recompute_membership: Callable[[tuple[str, ...]], None] | None = None,
    ) -> None:
        self.engine = engine
        self.dependencies = dependencies
        self.recompute_membership = recompute_membership

    def begin(
        self,
        *,
        reason: str,
        source: SecurityChangeSource | None = None,
        source_module: str | None = None,
        actor: str | None = None,
        operation: str | None = None,
        idempotency_key: str | None = None,
        request_fingerprint: str | None = None,
        idempotency_claim_id: str | None = None,
        source_transaction_id: str | None = None,
        account_ids: Iterable[str] | None = None,
        affected_account_ids: Iterable[str] = (),
        affected_workspace_ids: Iterable[str] = (),
        recompute_membership: bool = False,
    ) -> SecurityChangeTicket:
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("security change reason must not be blank")
        if source is None and all(
            value is not None for value in (source_module, actor, operation, idempotency_key)
        ):
            source = SecurityChangeSource(
                module=str(source_module),
                actor=str(actor),
                operation=str(operation),
                idempotency_key=str(idempotency_key),
                source_transaction_id=source_transaction_id,
                request_fingerprint=request_fingerprint,
                idempotency_claim_id=idempotency_claim_id,
            )
        if source is None:
            source = SecurityChangeSource(
                module="legacy",
                actor="SYSTEM",
                operation="security_change",
                idempotency_key=str(self.dependencies.random.uuid4()),
            )
        affected_accounts = _strings(affected_account_ids)
        affected_workspaces = _strings(affected_workspace_ids)
        with self.engine.begin() as db:
            repository = self.dependencies.repository_factory(db)
            repository.lock_convergence_source(
                source.module,
                source.actor,
                source.operation,
                source.idempotency_key,
                source.idempotency_claim_id,
            )
            existing = repository.convergence_work_by_source(
                source.module,
                source.actor,
                source.operation,
                source.idempotency_key,
                idempotency_claim_id=source.idempotency_claim_id,
                for_update=True,
            )
            if existing is not None:
                return _ticket(existing)
            principals = (
                _strings(account_ids)
                if account_ids is not None
                else tuple(repository.principal_ids())
            )
            generations = {
                account_id: repository.mark_fence(
                    account_id,
                    normalized_reason,
                    self.dependencies.clock.now(),
                )
                for account_id in principals
            }
            now = self.dependencies.clock.now()
            row = repository.insert_convergence_work(
                id=str(self.dependencies.random.uuid4()),
                source_module=source.module,
                actor=source.actor,
                operation=source.operation,
                idempotency_key=source.idempotency_key,
                reason=normalized_reason,
                source_transaction_id=source.source_transaction_id,
                idempotency_claim_id=source.idempotency_claim_id,
                request_fingerprint=source.request_fingerprint,
                generation_map=generations,
                affected_account_ids=list(affected_accounts),
                affected_workspace_ids=list(affected_workspaces),
                recompute_membership=recompute_membership,
                now=now,
            )
            for account_id, generation in generations.items():
                repository.insert_pending_principal(
                    work_id=str(row["id"]),
                    account_id=account_id,
                    generation=generation,
                    reason=normalized_reason,
                    created_at=now,
                )
        return _ticket(row, created=True)

    def _reconcile_work(
        self,
        work_id: str,
        *,
        affected_account_ids: Iterable[str] | None = None,
        affected_workspace_ids: Iterable[str] | None = None,
        recompute_membership: bool | None = None,
    ) -> set[str]:
        converged: set[str] = set()
        with self.engine.begin() as db:
            repository = self.dependencies.repository_factory(db)
            row = repository.convergence_work_by_id(work_id, for_update=True)
            if row is None:
                return converged
            if str(row["status"]) != "PENDING":
                return converged
            source_transaction_id = row["source_transaction_id"]
            if source_transaction_id is None and str(row["source_module"]) != "legacy":
                return converged
            if source_transaction_id is not None:
                source_status = repository.source_transaction_status(str(source_transaction_id))
                if source_status == "in progress":
                    return converged
                if source_status == "aborted":
                    now = self.dependencies.clock.now()
                    for account_id in sorted(dict(row["generation_map"])):
                        repository.settle_pending_principal(
                            work_id,
                            str(account_id),
                            bump_version=False,
                            now=now,
                        )
                    repository.cancel_convergence_work(work_id, now)
                    return converged
                if source_status != "committed":
                    raise RuntimeError("source transaction status is unknown")
            if (
                affected_account_ids is not None
                or affected_workspace_ids is not None
                or recompute_membership is not None
            ):
                accounts = (
                    list(_strings(affected_account_ids))
                    if affected_account_ids is not None
                    else [str(value) for value in row["affected_account_ids"]]
                )
                workspaces = (
                    list(_strings(affected_workspace_ids))
                    if affected_workspace_ids is not None
                    else [str(value) for value in row["affected_workspace_ids"]]
                )
                row = repository.update_convergence_effects(
                    work_id,
                    affected_account_ids=accounts,
                    affected_workspace_ids=workspaces,
                    recompute_membership=(
                        bool(recompute_membership)
                        if recompute_membership is not None
                        else bool(row["recompute_membership"])
                    ),
                    now=self.dependencies.clock.now(),
                )
                if row is None:
                    return converged
            recompute_accounts = tuple(str(value) for value in row["affected_account_ids"])
            if bool(row["recompute_membership"]):
                repository.update_convergence_phase(
                    work_id,
                    "RECOMPUTE_PENDING",
                    self.dependencies.clock.now(),
                )
                if self.recompute_membership is None:
                    raise RuntimeError("membership recompute unavailable")
                self.recompute_membership(recompute_accounts)
            repository.update_convergence_phase(
                work_id,
                "VERSION_PENDING",
                self.dependencies.clock.now(),
            )
            now = self.dependencies.clock.now()
            for account_id in sorted(dict(row["generation_map"])):
                updated = repository.settle_pending_principal(
                    work_id,
                    str(account_id),
                    bump_version=True,
                    now=now,
                )
                if updated is None:
                    continue
                converged.add(str(account_id))
                audit(
                    repository,
                    dependencies=self.dependencies,
                    actor=str(row["actor"]),
                    action=f"authorization.{row['source_module']}.converged",
                    target_type="authorization_principal",
                    target_id=str(account_id),
                    result="SUCCESS",
                    reason=(
                        f"sourceOperation={row['operation']}; "
                        f"beforeAuthorizationVersion={updated.before_version}; "
                        f"afterAuthorizationVersion={updated.after_version}; "
                        f"authorizationVersion={updated.after_version}"
                    ),
                    correlation_id=(
                        f"cli-{str(row['idempotency_key'])[:32]}"
                        if str(row["source_module"]) == "identity"
                        and str(row["actor"]).startswith("SYSTEM_")
                        and str(row["operation"]).endswith("_cli")
                        else current_request_id()
                    ),
                )
            repository.complete_convergence_work(work_id, now)
        return converged

    def complete(
        self,
        ticket: SecurityChangeTicket,
        *,
        affected_account_ids: Iterable[str] = (),
        affected_workspace_ids: Iterable[str] = (),
        recompute_membership: bool = False,
    ) -> set[str]:
        if not ticket.created or ticket.completed or ticket.cancelled:
            return set()
        return self._reconcile_work(
            ticket.id,
            affected_account_ids=affected_account_ids or None,
            affected_workspace_ids=affected_workspace_ids or None,
            recompute_membership=recompute_membership or None,
        )

    def cancel(self, ticket: SecurityChangeTicket) -> set[str]:
        cleared: set[str] = set()
        if not ticket.created:
            return cleared
        with self.engine.begin() as db:
            repository = self.dependencies.repository_factory(db)
            row = repository.convergence_work_by_id(ticket.id, for_update=True)
            if row is None or str(row["status"]) != "PENDING":
                return cleared
            now = self.dependencies.clock.now()
            for account_id in sorted(dict(row["generation_map"])):
                if (
                    repository.settle_pending_principal(
                        ticket.id,
                        str(account_id),
                        bump_version=False,
                        now=now,
                    )
                    is not None
                ):
                    cleared.add(str(account_id))
            repository.cancel_convergence_work(ticket.id, now)
        return cleared

    def reconcile_for_account(self, account_id: str) -> bool:
        try:
            with self.engine.connect() as db:
                repository = self.dependencies.repository_factory(db)
                work_ids = repository.pending_convergence_for_account(account_id)
            for work_id in work_ids:
                self._reconcile_work(work_id)
            with self.engine.connect() as db:
                repository = self.dependencies.repository_factory(db)
                state = repository.principal_version(account_id)
            return state is not None and state["dirty_generation"] is None
        except Exception:
            return False

    def reconcile_pending(self) -> bool:
        """Reconcile persisted work without requiring an authenticated principal."""
        try:
            with self.engine.connect() as db:
                repository = self.dependencies.repository_factory(db)
                work_ids = repository.pending_convergence_work_ids()
        except Exception:
            return False
        all_reconciled = True
        for work_id in work_ids:
            try:
                self._reconcile_work(work_id)
            except Exception:
                all_reconciled = False
        try:
            with self.engine.connect() as db:
                repository = self.dependencies.repository_factory(db)
                if repository.pending_convergence_work_ids():
                    all_reconciled = False
        except Exception:
            return False
        return all_reconciled

    def claim_converged(self, source_module: str, idempotency_claim_id: str) -> bool:
        return self.claim_convergence_status(source_module, idempotency_claim_id) == "COMPLETED"

    def claim_convergence_status(
        self,
        source_module: str,
        idempotency_claim_id: str,
    ) -> str | None:
        try:
            with self.engine.connect() as db:
                repository = self.dependencies.repository_factory(db)
                return repository.convergence_status_for_claim(
                    source_module,
                    idempotency_claim_id,
                )
        except Exception:
            return "UNKNOWN"

    def identity_change(
        self,
        account_id: str,
        *,
        actor: str | None = None,
        operation: str | None = None,
        idempotency_key: str | None = None,
        source_transaction_id: str | None = None,
        request_fingerprint: str | None = None,
        idempotency_claim_id: str | None = None,
    ) -> SecurityChangeTicket:
        """Register a pre-fence that stays pending until committed truth is reconciled."""
        return self.begin(
            source=SecurityChangeSource(
                module="identity",
                actor=actor or account_id,
                operation=operation or "identity_security_change",
                idempotency_key=idempotency_key or str(self.dependencies.random.uuid4()),
                source_transaction_id=source_transaction_id,
                request_fingerprint=request_fingerprint,
                idempotency_claim_id=idempotency_claim_id,
            ),
            reason="identity security fact changed",
            account_ids=(account_id,),
            affected_account_ids=(account_id,),
            recompute_membership=True,
        )
