from datetime import datetime
from typing import Protocol

from control_plane.app.modules.configuration import (
    Draft,
    PolicyOwnerPort,
    PolicySnapshot,
    PublishedVersion,
)
from control_plane.app.modules.identity import ConsumedReauthReceipt, PolicyReauthBinding


class GatePolicyRepository(PolicyOwnerPort, Protocol):
    def active_snapshot(self, namespace: str, *, for_update: bool = False) -> PolicySnapshot: ...
    def reference_receipt(self, receipt: ConsumedReauthReceipt, *, now: datetime) -> None: ...
    def publish(
        self,
        draft: Draft,
        receipt: ConsumedReauthReceipt,
        *,
        actor_id: str,
        reason: str,
        now: datetime,
        outbox_id: str,
    ) -> PublishedVersion: ...


class PolicyReauthenticationPort(Protocol):
    def verify_and_consume_policy_reauth(
        self, *, raw_session: str, totp_code: str, binding: PolicyReauthBinding, attempt_id: str
    ) -> ConsumedReauthReceipt: ...
    def validate_consumed_policy_reauth(
        self, *, raw_session: str, binding: PolicyReauthBinding, receipt: ConsumedReauthReceipt
    ) -> None: ...


class PolicyAuthorizationPort(Protocol):
    def check(self, *, raw_session: str, actor_id: str) -> tuple[int, int]: ...
