from typing import Protocol

from control_plane.app.modules.identity.domain.policy_reauthentication import ConsumedReauthReceipt


class PolicyReauthenticationRepository(Protocol):
    def attempt_consumed(self, actor_id: str, attempt_id: str) -> bool: ...

    def insert_consumption(self, receipt: ConsumedReauthReceipt) -> None: ...

    def matches_consumption(self, receipt: ConsumedReauthReceipt) -> bool: ...
