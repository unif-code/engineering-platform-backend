from collections.abc import Callable, Iterable
from dataclasses import dataclass

from sqlalchemy import Engine

from control_plane.app.modules.authorization.application.dependencies import (
    AuthorizationDependencies,
)


@dataclass(frozen=True, slots=True)
class SecurityChangeTicket:
    generations: dict[str, int]
    reason: str


class SecurityChangeOrchestrator:
    """Durable pre-fence/source-commit/post-commit convergence coordinator."""

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
        account_ids: Iterable[str] | None = None,
    ) -> SecurityChangeTicket:
        with self.engine.begin() as db:
            repository = self.dependencies.repository_factory(db)
            principals = (
                tuple(sorted(set(account_ids)))
                if account_ids is not None
                else tuple(repository.principal_ids())
            )
            generations = {
                account_id: repository.mark_fence(
                    account_id,
                    reason,
                    self.dependencies.clock.now(),
                )
                for account_id in principals
            }
        return SecurityChangeTicket(generations=generations, reason=reason)

    def complete(
        self,
        ticket: SecurityChangeTicket,
        *,
        affected_account_ids: Iterable[str] = (),
        recompute_membership: bool = False,
    ) -> set[str]:
        affected = tuple(sorted(set(affected_account_ids)))
        if recompute_membership and self.recompute_membership is not None:
            self.recompute_membership(affected)
        converged: set[str] = set()
        with self.engine.begin() as db:
            repository = self.dependencies.repository_factory(db)
            now = self.dependencies.clock.now()
            for account_id, generation in ticket.generations.items():
                row = repository.converge_fence(account_id, generation, now)
                if row is not None:
                    converged.add(account_id)
        return converged

    def cancel(self, ticket: SecurityChangeTicket) -> set[str]:
        cleared: set[str] = set()
        with self.engine.begin() as db:
            repository = self.dependencies.repository_factory(db)
            now = self.dependencies.clock.now()
            for account_id, generation in ticket.generations.items():
                if repository.clear_fence(account_id, generation, now):
                    cleared.add(account_id)
        return cleared

    def identity_change(self, account_id: str) -> None:
        """Identity facts are checked live; this hook atomically advances their version."""
        ticket = self.begin(reason="identity security fact changed", account_ids=(account_id,))
        self.complete(ticket)
