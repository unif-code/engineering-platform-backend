from control_plane.app.modules.configuration.domain import PolicyKey, PolicySnapshot
from control_plane.app.modules.configuration.ports import PolicyOwnerPort


def catalog(owner: PolicyOwnerPort, namespace: str) -> list[PolicyKey]:
    return owner.catalog(namespace)


def active_snapshot(owner: PolicyOwnerPort, namespace: str) -> PolicySnapshot:
    return owner.active_snapshot(namespace)
