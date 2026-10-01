from typing import Any, Protocol

from control_plane.app.modules.model_gateway.domain import Deployment, DeploymentState
from control_plane.app.shared.idempotency import IdempotencyRepository


class DeploymentRepository(IdempotencyRepository, Protocol):
    db: Any

    def get(self, deployment_id: str, *, for_update: bool = False) -> Deployment | None: ...
    def insert(self, deployment: Deployment) -> Deployment | None: ...
    def save(self, deployment: Deployment, *, expected_revision: int) -> bool: ...
    def list(
        self,
        *,
        state: DeploymentState | None,
        query: str,
        after_key: str | None,
        limit: int,
    ) -> list[Deployment]: ...
