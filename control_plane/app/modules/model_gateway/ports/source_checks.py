from datetime import datetime
from typing import Any, Protocol

from control_plane.app.modules.model_gateway.domain import Deployment
from control_plane.app.modules.model_gateway.domain.dossiers import ValidationDossier
from control_plane.app.modules.model_gateway.domain.source_checks import (
    MaterialSourceCheck,
    SourceInspection,
)
from control_plane.app.shared.idempotency import IdempotencyRepository


class ModelMaterialSourcePort(Protocol):
    def inspect(self, source_reference: str, external_version: str | None) -> SourceInspection: ...


class MaterialSourceCheckRepository(IdempotencyRepository, Protocol):
    db: Any

    def get(self, deployment_id: str, *, for_update: bool = False) -> Deployment | None: ...
    def dossier(self, dossier_id: str) -> ValidationDossier | None: ...
    def insert_source_check(self, value: MaterialSourceCheck) -> None: ...
    def source_check(self, source_check_id: str) -> MaterialSourceCheck | None: ...
    def list_source_checks(
        self,
        deployment_id: str,
        dossier_id: str,
        *,
        before_at: datetime | None,
        before_id: str | None,
        limit: int,
    ) -> list[MaterialSourceCheck]: ...
