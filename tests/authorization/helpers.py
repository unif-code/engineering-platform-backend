from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from control_plane.app.modules.audit.adapters.transactional import (
    SqlAlchemyTransactionalAuditAppender,
)
from control_plane.app.modules.authorization import AuthorizationDependencies
from control_plane.app.modules.authorization.adapters import (
    SqlAlchemyAuthorizationRepository,
)
from tests.organization.helpers import StaticSecrets


@dataclass
class MutableClock:
    value: datetime = datetime(2026, 8, 11, 2, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value


class RandomValues:
    def uuid4(self) -> UUID:
        return uuid4()


def authorization_dependencies(
    clock: MutableClock | None = None,
) -> AuthorizationDependencies:
    return AuthorizationDependencies(
        repository_factory=SqlAlchemyAuthorizationRepository,
        audit=SqlAlchemyTransactionalAuditAppender(),
        clock=clock or MutableClock(),
        random=RandomValues(),
        secret_manager=StaticSecrets(),
    )
