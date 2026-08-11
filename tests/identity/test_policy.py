from datetime import timedelta
from typing import Any, cast

import pytest
from pydantic import ValidationError
from sqlalchemy import Connection

from control_plane.app.modules.identity import (
    EffectiveIdentityPolicy,
    IdentityDependencies,
    Principal,
    create_account,
)


def test_effective_identity_policy_exposes_architecture_defaults() -> None:
    policy = EffectiveIdentityPolicy()

    assert policy.temp_credential_ttl == timedelta(hours=24)
    assert policy.password_max_age is None
    assert policy.session_cap == 3
    assert policy.session_idle_timeout == timedelta(minutes=60)
    assert policy.backoff_threshold == 5
    assert policy.backoff_initial_delay == timedelta(seconds=30)
    assert policy.backoff_max_delay == timedelta(minutes=15)
    assert policy.backoff_reset_after == timedelta(hours=24)
    assert policy.totp_attempt_cap == 5
    assert policy.draft_archive_after == timedelta(days=30)


def test_identity_dependencies_fail_closed_without_audit_and_auth_change_wiring() -> None:
    dependency = cast(Any, object())
    with pytest.raises(TypeError):
        IdentityDependencies(  # type: ignore[call-arg]
            secret_manager=dependency,
            policy=dependency,
            clock=dependency,
            random=dependency,
        )


def test_public_facade_fails_closed_without_explicit_dependencies() -> None:
    with pytest.raises(TypeError, match="dependencies") as missing:
        create_account(  # type: ignore[call-arg]
            cast(Connection, object()),
            employee_no="00000001",
            display_name="Alice",
            actor=Principal(employee_id="SYSTEM", name="System"),
            reason="missing composition",
        )
    assert "create_account" in str(missing.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("temp_credential_ttl", timedelta(0)),
        ("password_max_age", timedelta(0)),
        ("session_cap", 0),
        ("session_cap", 11),
        ("session_idle_timeout", timedelta(minutes=14)),
        ("session_idle_timeout", timedelta(minutes=241)),
        ("backoff_threshold", 0),
        ("backoff_initial_delay", timedelta(0)),
        ("backoff_max_delay", timedelta(seconds=29)),
        ("backoff_reset_after", timedelta(0)),
        ("totp_attempt_cap", 0),
        ("draft_archive_after", timedelta(0)),
    ],
)
def test_effective_identity_policy_rejects_values_outside_security_contract(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        EffectiveIdentityPolicy.model_validate({field: value})
