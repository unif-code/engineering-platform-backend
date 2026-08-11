from dataclasses import dataclass

from control_plane.app.modules.audit import TransactionalAuditAppender
from control_plane.app.modules.configuration.ports.runtime import ClockPort, RandomPort


@dataclass(frozen=True, slots=True)
class ConfigurationDependencies:
    clock: ClockPort
    random: RandomPort
    audit: TransactionalAuditAppender
