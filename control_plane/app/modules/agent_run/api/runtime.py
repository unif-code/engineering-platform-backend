from dataclasses import dataclass
from typing import Protocol

from pydantic import SecretStr

from control_plane.app.modules.agent_run.domain.models import FrozenModel, NonEmptyStr
from control_plane.app.modules.agent_run.ports import SandboxPort


class WorkloadPrincipal(FrozenModel):
    actor: NonEmptyStr


class ServiceIdentityUnavailable(RuntimeError):
    """The private workload identity verifier is not configured or reachable."""


class ServiceIdentityVerifier(Protocol):
    def verify(self, bearer_token: SecretStr) -> WorkloadPrincipal | None: ...


class UnavailableServiceIdentityVerifier:
    def verify(self, bearer_token: SecretStr) -> WorkloadPrincipal | None:
        del bearer_token
        raise ServiceIdentityUnavailable


@dataclass(frozen=True, slots=True)
class SandboxHttpRuntime:
    controller: SandboxPort | None
    identity_verifier: ServiceIdentityVerifier

    @property
    def ready(self) -> bool:
        return self.controller is not None
