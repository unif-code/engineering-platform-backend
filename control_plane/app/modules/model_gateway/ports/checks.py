from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, SecretStr

from control_plane.app.modules.model_gateway.domain import Deployment
from control_plane.app.modules.model_gateway.domain.checks import ConnectionCheck, ProbeOutcome
from control_plane.app.modules.model_gateway.domain.connections import (
    CheckKind,
    ConnectionDefinition,
    VersionLabel,
)
from control_plane.app.shared.idempotency import IdempotencyRepository


class ConnectionDirectoryPort(Protocol):
    def resolve(self, reference: str | None) -> tuple[str, ConnectionDefinition]: ...


class CheckRepository(IdempotencyRepository, Protocol):
    db: Any

    def get(self, deployment_id: str, *, for_update: bool = False) -> Deployment | None: ...
    def check(self, check_id: str, *, for_update: bool = False) -> ConnectionCheck | None: ...
    def insert_check(self, value: ConnectionCheck) -> bool: ...
    def active_check(self, deployment_id: str) -> ConnectionCheck | None: ...
    def list_checks(
        self, deployment_id: str, *, before_at: datetime | None, before_id: str | None, limit: int
    ) -> list[ConnectionCheck]: ...
    def queued_ids(self, *, limit: int) -> list[str]: ...
    def expired_ids(self, *, now: datetime, limit: int) -> list[str]: ...
    def connection_busy(self, reference: str) -> bool: ...
    def save_check(self, value: ConnectionCheck, *, expected_revision: int) -> bool: ...


class CheckActorPort(Protocol):
    def require_manager(self, account_id: str) -> None: ...


class ProbePort(Protocol):
    def prepare(self, connection: ConnectionDefinition) -> Any: ...
    def send(self, prepared: Any, model_id: str, check_kind: CheckKind) -> ProbeOutcome: ...
    def material_version(self, connection: ConnectionDefinition) -> str: ...


class ProviderSecretMaterial(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: VersionLabel
    value: SecretStr


class ModelSecretPort(Protocol):
    def resolve(self, reference: str) -> ProviderSecretMaterial: ...
