from collections.abc import Callable
from pathlib import Path

from control_plane.app.bootstrap import app as control_plane
from control_plane.app.modules.model_gateway import ModelCheckWorkerDependencies
from control_plane.app.modules.model_gateway.adapters.authorization import CurrentModelManager
from control_plane.app.modules.model_gateway.adapters.checks import SqlAlchemyCheckRepository
from control_plane.app.modules.model_gateway.adapters.connections import (
    FileConnectionDirectory,
    ModelConnectionSettings,
)
from control_plane.app.modules.model_gateway.adapters.probe import HttpxModelProbe
from control_plane.app.modules.model_gateway.adapters.secrets import FileModelSecretPort
from control_plane.app.modules.model_gateway.ports.checks import ModelSecretPort, ProbePort


class ModelWorkerSettings(ModelConnectionSettings):
    secret_reference_root: Path | None = None


def model_check_worker_dependencies(
    *, probe_factory: Callable[[ModelSecretPort], ProbePort] = HttpxModelProbe
) -> ModelCheckWorkerDependencies:
    settings = ModelWorkerSettings()
    return ModelCheckWorkerDependencies(
        engine=control_plane.model_gateway_worker_runtime_engine(),
        repository_factory=SqlAlchemyCheckRepository,
        common=control_plane.model_gateway_http_runtime().dependencies,
        directory=FileConnectionDirectory(settings),
        actors=CurrentModelManager(
            control_plane.identity_runtime_engine(),
            control_plane.identity_dependencies(),
            control_plane.authorization_runtime_engine(),
            control_plane.authorization_dependencies(),
        ),
        probe=probe_factory(FileModelSecretPort(settings.secret_reference_root)),
    )
