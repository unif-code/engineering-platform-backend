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
from control_plane.app.modules.model_gateway.adapters.metadata import HttpxModelMetadata
from control_plane.app.modules.model_gateway.adapters.metadata_observations import (
    SqlAlchemyMetadataRepository,
)
from control_plane.app.modules.model_gateway.adapters.probe import HttpxModelProbe
from control_plane.app.modules.model_gateway.adapters.secrets import FileModelSecretPort
from control_plane.app.modules.model_gateway.application.metadata_worker import (
    ModelMetadataWorkerDependencies,
)
from control_plane.app.modules.model_gateway.ports.checks import ModelSecretPort, ProbePort
from control_plane.app.modules.model_gateway.ports.metadata import ModelMetadataPort


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


def model_metadata_worker_dependencies(
    *,
    adapter_factory: Callable[[ModelSecretPort], ModelMetadataPort] = HttpxModelMetadata,
) -> ModelMetadataWorkerDependencies:
    settings = ModelWorkerSettings()
    runtime = control_plane.model_gateway_http_runtime()
    return ModelMetadataWorkerDependencies(
        engine=control_plane.model_gateway_worker_runtime_engine(),
        repository_factory=SqlAlchemyMetadataRepository,
        common=runtime.dependencies,
        connections=runtime.connections,
        sources=runtime.metadata_sources,
        actors=CurrentModelManager(
            control_plane.identity_runtime_engine(),
            control_plane.identity_dependencies(),
            control_plane.authorization_runtime_engine(),
            control_plane.authorization_dependencies(),
        ),
        adapter=adapter_factory(FileModelSecretPort(settings.secret_reference_root)),
    )
