from datetime import timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import Connection

from control_plane.app.modules.configuration.adapters.identity import IdentityPolicyOwner
from control_plane.app.modules.configuration.application import active_snapshot
from control_plane.app.modules.configuration.domain import PolicySnapshotUnavailable
from control_plane.app.modules.identity import EffectiveIdentityPolicy

_IDENTITY_KEYS = {
    "identity.temp_credential_ttl",
    "identity.password_max_age",
    "identity.session_cap",
    "identity.session_idle_timeout",
    "identity.login_backoff",
    "identity.totp_attempt_cap",
    "identity.draft_archive_after",
}
_BACKOFF_KEYS = {
    "failureThreshold",
    "initialDelaySeconds",
    "maximumDelaySeconds",
    "resetAfterHours",
}


def _registered_integer(value: Any) -> int:
    if type(value) is not int:
        raise ValueError("policy value is not a registered integer")
    return value


class IdentityEffectivePolicy:
    """Production adapter backed exclusively by the active identity snapshot."""

    def get_identity_policy(self, db: Connection) -> EffectiveIdentityPolicy:
        snapshot = active_snapshot(IdentityPolicyOwner(db), "identity")
        values = snapshot.values
        try:
            if (
                snapshot.namespace != "identity"
                or snapshot.scope != "PLATFORM"
                or snapshot.schema_revision != 1
                or set(values) != _IDENTITY_KEYS
            ):
                raise ValueError("unsupported identity snapshot")
            backoff_value: Any = values["identity.login_backoff"]
            if not isinstance(backoff_value, dict) or set(backoff_value) != _BACKOFF_KEYS:
                raise ValueError("invalid backoff object")
            password_value = values["identity.password_max_age"]
            password_max_age = (
                None
                if password_value == "NEVER"
                else timedelta(days=_registered_integer(password_value))
            )
            return EffectiveIdentityPolicy(
                temp_credential_ttl=timedelta(
                    hours=_registered_integer(values["identity.temp_credential_ttl"])
                ),
                password_max_age=password_max_age,
                session_cap=_registered_integer(values["identity.session_cap"]),
                session_idle_timeout=timedelta(
                    minutes=_registered_integer(values["identity.session_idle_timeout"])
                ),
                backoff_threshold=_registered_integer(backoff_value["failureThreshold"]),
                backoff_initial_delay=timedelta(
                    seconds=_registered_integer(backoff_value["initialDelaySeconds"])
                ),
                backoff_max_delay=timedelta(
                    seconds=_registered_integer(backoff_value["maximumDelaySeconds"])
                ),
                backoff_reset_after=timedelta(
                    hours=_registered_integer(backoff_value["resetAfterHours"])
                ),
                totp_attempt_cap=_registered_integer(values["identity.totp_attempt_cap"]),
                draft_archive_after=timedelta(
                    days=_registered_integer(values["identity.draft_archive_after"])
                ),
            )
        except (KeyError, TypeError, ValueError, ValidationError, OverflowError) as exc:
            raise PolicySnapshotUnavailable("identity") from exc
