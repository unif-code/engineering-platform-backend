from collections.abc import Iterable, Mapping

from control_plane.app.modules.authorization.application.common import (
    principal_version_dto,
)
from control_plane.app.modules.authorization.application.dependencies import (
    AuthorizationDependencies,
)
from control_plane.app.modules.authorization.domain import PrincipalVersionDto
from control_plane.app.modules.authorization.ports import AuthorizationRepository


def mark_fence(
    repository: AuthorizationRepository,
    *,
    account_ids: Iterable[str],
    reason: str,
    dependencies: AuthorizationDependencies,
) -> dict[str, int]:
    now = dependencies.clock.now()
    return {
        account_id: repository.mark_fence(account_id, reason, now)
        for account_id in sorted(set(account_ids))
    }


def clear_fence(
    repository: AuthorizationRepository,
    *,
    generations: Mapping[str, int],
    dependencies: AuthorizationDependencies,
) -> set[str]:
    now = dependencies.clock.now()
    return {
        account_id
        for account_id, generation in generations.items()
        if repository.clear_fence(account_id, generation, now)
    }


def bump_version(
    repository: AuthorizationRepository,
    *,
    account_id: str,
    dependencies: AuthorizationDependencies,
) -> PrincipalVersionDto:
    return principal_version_dto(
        repository.bump_principal_version(account_id, dependencies.clock.now())
    )


def principal_version(
    repository: AuthorizationRepository,
    *,
    account_id: str,
) -> PrincipalVersionDto | None:
    row = repository.principal_version(account_id)
    return principal_version_dto(row) if row is not None else None
