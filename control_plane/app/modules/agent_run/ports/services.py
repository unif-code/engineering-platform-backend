from datetime import datetime
from typing import Protocol
from uuid import UUID

from control_plane.app.modules.agent_run.ports.repository import AdmissionPolicySnapshot


class ClockPort(Protocol):
    def now(self) -> datetime: ...


class RandomPort(Protocol):
    def uuid4(self) -> UUID: ...

    def token_urlsafe(self, nbytes: int) -> str: ...


class AdmissionPolicyPort(Protocol):
    def snapshot(self, environment_id: str) -> AdmissionPolicySnapshot: ...


class WorkloadAuthorizationPort(Protocol):
    def authorize(self, *, actor: str, operation: str, environment_id: str) -> bool: ...


class DefaultDenyWorkloadAuthorization:
    def authorize(self, *, actor: str, operation: str, environment_id: str) -> bool:
        del actor, operation, environment_id
        return False
