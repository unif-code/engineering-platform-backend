from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine

from control_plane.app.modules.authorization.application.dependencies import (
    AuthorizationDependencies,
)


@dataclass(frozen=True, slots=True)
class SecurityChangeSource:
    module: str
    actor: str
    operation: str
    idempotency_key: str
    source_transaction_id: str | None = None

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (self.module, self.actor, self.operation, self.idempotency_key)
        ):
            raise ValueError("security change source fields must not be blank")


@dataclass(frozen=True, slots=True)
class SecurityChangeTicket:
    id: str
    source: SecurityChangeSource
    generations: dict[str, int]
    reason: str
    status: str

    @property
    def completed(self) -> bool:
        return self.status == "COMPLETED"

    @property
    def cancelled(self) -> bool:
        return self.status == "CANCELLED"


def _strings(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({value.strip() for value in values if value.strip()}))


def _ticket(row: Mapping[str, Any]) -> SecurityChangeTicket:
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
        ),
        generations=generations,
        reason=str(row["reason"]),
        status=str(row["status"]),
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
            )
            existing = repository.convergence_work_by_source(
                source.module,
                source.actor,
                source.operation,
                source.idempotency_key,
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
                generation_map=generations,
                affected_account_ids=list(affected_accounts),
                affected_workspace_ids=list(affected_workspaces),
                recompute_membership=recompute_membership,
                now=now,
            )
        return _ticket(row)

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
            if (
                source_transaction_id is not None
                and repository.source_transaction_status(str(source_transaction_id))
                == "in progress"
            ):
                return converged
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
            for account_id, generation in dict(row["generation_map"]).items():
                if repository.converge_fence(str(account_id), int(generation), now) is not None:
                    converged.add(str(account_id))
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
        if ticket.completed or ticket.cancelled:
            return set()
        return self._reconcile_work(
            ticket.id,
            affected_account_ids=affected_account_ids or None,
            affected_workspace_ids=affected_workspace_ids or None,
            recompute_membership=recompute_membership or None,
        )

    def cancel(self, ticket: SecurityChangeTicket) -> set[str]:
        cleared: set[str] = set()
        with self.engine.begin() as db:
            repository = self.dependencies.repository_factory(db)
            row = repository.convergence_work_by_id(ticket.id, for_update=True)
            if row is None or str(row["status"]) != "PENDING":
                return cleared
            now = self.dependencies.clock.now()
            for account_id, generation in dict(row["generation_map"]).items():
                if repository.clear_fence(str(account_id), int(generation), now):
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

    def identity_change(
        self,
        account_id: str,
        *,
        actor: str | None = None,
        operation: str | None = None,
        idempotency_key: str | None = None,
        source_transaction_id: str | None = None,
    ) -> None:
        """Register a pre-fence that stays pending until committed truth is reconciled."""
        self.begin(
            source=SecurityChangeSource(
                module="identity",
                actor=actor or account_id,
                operation=operation or "identity_security_change",
                idempotency_key=idempotency_key or str(self.dependencies.random.uuid4()),
                source_transaction_id=source_transaction_id,
            ),
            reason="identity security fact changed",
            account_ids=(account_id,),
            affected_account_ids=(account_id,),
            recompute_membership=True,
        )
