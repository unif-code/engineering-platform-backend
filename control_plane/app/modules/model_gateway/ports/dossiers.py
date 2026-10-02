from datetime import datetime
from typing import Any, Protocol

from control_plane.app.modules.model_gateway.domain import Deployment
from control_plane.app.modules.model_gateway.domain.checks import ConnectionCheck
from control_plane.app.modules.model_gateway.domain.dossiers import ValidationDossier
from control_plane.app.shared.idempotency import IdempotencyRepository


class ValidationDossierRepository(IdempotencyRepository, Protocol):
    db: Any

    def get(self, deployment_id: str, *, for_update: bool = False) -> Deployment | None: ...
    def check(self, check_id: str, *, for_update: bool = False) -> ConnectionCheck | None: ...
    def insert_dossier(self, value: ValidationDossier) -> None: ...
    def dossier(self, dossier_id: str) -> ValidationDossier | None: ...
    def list_dossiers(
        self, deployment_id: str, *, before_at: datetime | None, before_id: str | None, limit: int
    ) -> list[ValidationDossier]: ...
