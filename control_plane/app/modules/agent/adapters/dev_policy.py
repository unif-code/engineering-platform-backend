import base64
import hashlib
import hmac
import json

from control_plane.app.modules.agent.domain import (
    AgentDefinition,
    ExecutionBinding,
    ExecutionBindingSource,
)
from control_plane.app.modules.agent.ports.runtime import (
    ExecutionBindingRequest,
    ResolvedActorReference,
)


class DevExecutionBindingPolicy:
    """The only V0.8 binding policy: explicit, local, and side-effect-free."""

    _PERMISSIONS = ("checkpoint.write", "context.read", "event.emit")

    def resolve(self, request: ExecutionBindingRequest) -> ExecutionBinding:
        return ExecutionBinding(
            id=request.binding_id,
            source=ExecutionBindingSource.DEV_FAKE,
            runtime_ref="DEV_FAKE:runtime:control-plane-v1",
            model_route_ref="DEV_FAKE:model-route:none",
            capability_bundle_ref="DEV_FAKE:capability-bundle:control-plane-v1",
            skill_refs=request.definition.skill_declarations,
            runtime_permissions=self._PERMISSIONS,
            context_policy_ref="DEV_FAKE:context-policy:requirement-read-v1",
            network_policy_ref="DEV_FAKE:network-policy:none",
        )


class DevDefinitionAvailabilityPolicy:
    """V0.8 DEV projection; future Configuration remains outside this module."""

    def is_active(self, _definition: AgentDefinition) -> bool:
        return True


class DevActorResolver:
    """Restricted V0.8 identity allowlist; Task 6 will bind a current Principal."""

    _ACTORS = {
        "employee-901": ResolvedActorReference(reference="employee-901", actor_type="EMPLOYEE"),
        "service-901": ResolvedActorReference(reference="service-901", actor_type="SERVICE"),
        "system-901": ResolvedActorReference(reference="system-901", actor_type="SYSTEM"),
    }

    def resolve(self, untrusted_actor: str) -> ResolvedActorReference:
        resolved = self._ACTORS.get(untrusted_actor)
        if resolved is None:
            raise ValueError("actor is not a trusted platform actor")
        return resolved


class DevEventCursorCodec:
    """Restricted DEV-only integrity codec; its material is not a deployed secret."""

    _VERSION = 1
    _TEST_SIGNING_MATERIAL = b"agent-control-plane-dev-cursor-v1"

    @classmethod
    def _encode(cls, value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode().rstrip("=")

    @classmethod
    def _decode(cls, value: str) -> bytes:
        if not value or any(
            character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
            for character in value
        ):
            raise ValueError("invalid canonical event cursor")
        try:
            decoded = base64.b64decode(
                value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
            )
        except ValueError as error:
            raise ValueError("invalid canonical event cursor") from error
        if cls._encode(decoded) != value:
            raise ValueError("invalid canonical event cursor")
        return decoded

    def encode(self, *, run_id: str, event_id: str) -> str:
        payload = json.dumps(
            {"event": event_id, "run": run_id, "v": self._VERSION},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        signature = hmac.new(self._TEST_SIGNING_MATERIAL, payload, hashlib.sha256).digest()
        return f"v{self._VERSION}.{self._encode(payload)}.{self._encode(signature)}"

    def decode(self, *, run_id: str, cursor: str) -> str:
        parts = cursor.split(".")
        if len(parts) != 3 or parts[0] != f"v{self._VERSION}":
            raise ValueError("invalid canonical event cursor")
        payload = self._decode(parts[1])
        signature = self._decode(parts[2])
        expected = hmac.new(self._TEST_SIGNING_MATERIAL, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("invalid canonical event cursor")
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as error:
            raise ValueError("invalid canonical event cursor") from error
        if (
            not isinstance(decoded, dict)
            or set(decoded) != {"event", "run", "v"}
            or decoded["v"] != self._VERSION
            or decoded["run"] != run_id
            or not isinstance(decoded["event"], str)
        ):
            raise ValueError("invalid canonical event cursor")
        return decoded["event"]
