from dataclasses import dataclass
from datetime import datetime, timedelta

from control_plane.app.modules.source_control.adapters.settings import (
    SourceControlDevSettings,
)


@dataclass(frozen=True, slots=True)
class SourceControlDevPolicy:
    settings: SourceControlDevSettings

    @property
    def version(self) -> int:
        return self.settings.policy_version

    def next_reconcile_at(self, *, now: datetime, attempts: int) -> datetime:
        delay = self.settings.reconcile_base_delay_seconds
        maximum = self.settings.reconcile_max_delay_seconds
        remaining_doublings = max(attempts, 1) - 1
        while remaining_doublings and delay < maximum:
            delay = min(delay * 2, maximum)
            remaining_doublings -= 1
        return now + timedelta(seconds=delay)

    def webhook_replay_window(self) -> timedelta:
        return timedelta(seconds=self.settings.webhook_replay_window_seconds)
