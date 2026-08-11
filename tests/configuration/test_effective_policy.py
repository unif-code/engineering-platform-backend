import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from tests.configuration.policy_helpers import (
    rollback_connection,
    snapshot_with_idle_minutes,
    temporary_active_snapshot,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("configuration_seed")]


def test_effective_policy_adapter_maps_all_seven_active_identity_values(
    identity_rw_engine: Engine,
) -> None:
    from control_plane.app.modules.configuration.adapters import IdentityEffectivePolicy

    with identity_rw_engine.connect() as db:
        policy = IdentityEffectivePolicy().get_identity_policy(db)

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


def test_production_bootstrap_uses_active_snapshot_policy_not_default_fallback() -> None:
    from control_plane.app.bootstrap.app import identity_dependencies
    from control_plane.app.modules.configuration.adapters import IdentityEffectivePolicy

    identity_dependencies.cache_clear()
    assert isinstance(identity_dependencies().policy, IdentityEffectivePolicy)


def test_effective_policy_fails_closed_for_incomplete_or_hash_invalid_snapshot(
    configuration_owner_engine: Engine,
) -> None:
    from control_plane.app.modules.configuration import PolicySnapshotUnavailable
    from control_plane.app.modules.configuration.adapters import IdentityEffectivePolicy

    with rollback_connection(configuration_owner_engine) as db:
        db.execute(
            text(
                "UPDATE identity.version SET snapshot=snapshot - "
                "'identity.session_idle_timeout' "
                "WHERE namespace='identity' AND scope='PLATFORM' AND version=1"
            )
        )
        with pytest.raises(PolicySnapshotUnavailable):
            IdentityEffectivePolicy().get_identity_policy(db)


def test_effective_policy_fails_closed_for_well_hashed_wrong_value_type(
    configuration_owner_engine: Engine,
) -> None:
    from control_plane.app.modules.configuration import PolicySnapshotUnavailable
    from control_plane.app.modules.configuration.adapters import IdentityEffectivePolicy

    snapshot, _snapshot_hash = snapshot_with_idle_minutes(60)
    snapshot["identity.session_idle_timeout"] = "60"
    with temporary_active_snapshot(configuration_owner_engine, snapshot):
        with configuration_owner_engine.connect() as db:
            with pytest.raises(PolicySnapshotUnavailable):
                IdentityEffectivePolicy().get_identity_policy(db)


def test_readyz_accepts_a_valid_typed_identity_policy(
    configuration_seed: None,
) -> None:
    del configuration_seed
    from control_plane.app.bootstrap.app import create_app

    response = TestClient(create_app()).get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


@pytest.mark.parametrize("invalid_case", ["wrong_type", "missing_key"])
def test_readyz_fails_closed_for_well_hashed_invalid_identity_policy(
    configuration_owner_engine: Engine,
    invalid_case: str,
) -> None:
    from control_plane.app.bootstrap.app import create_app

    snapshot, _snapshot_hash = snapshot_with_idle_minutes(60)
    if invalid_case == "wrong_type":
        snapshot["identity.session_idle_timeout"] = "60"
    else:
        snapshot.pop("identity.session_idle_timeout")
    request_id = f"req-ready{invalid_case.replace('_', '')}"
    with temporary_active_snapshot(configuration_owner_engine, snapshot):
        response = TestClient(create_app()).get(
            "/readyz",
            headers={"X-Request-ID": request_id},
        )

        assert response.status_code == 503
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.json() == {
            "title": "Not ready",
            "status": 503,
            "requestId": request_id,
        }


def test_test_only_direct_version_two_changes_identity_idle_expiry(
    identity_rw_engine: Engine,
    configuration_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from control_plane.app.modules.configuration.adapters import IdentityEffectivePolicy
    from control_plane.app.modules.identity import IdentityDependencies, validate_session
    from control_plane.app.modules.identity.adapters.runtime import SystemRandom
    from tests.identity.task5_helpers import MutableClock, StaticSecrets, dependencies
    from tests.identity.test_auth_flow import _initialize_account

    clock = MutableClock()
    base = dependencies(clock=clock)
    deps = IdentityDependencies(
        repository_factory=base.repository_factory,
        secret_manager=StaticSecrets(),
        policy=IdentityEffectivePolicy(),
        clock=clock,
        random=SystemRandom(),
        audit=base.audit,
        on_auth_change=base.on_auth_change,
    )
    with configuration_owner_engine.begin() as db:
        db.execute(
            text(
                "TRUNCATE identity.idempotency_record, identity.auth_challenge, "
                "identity.session, identity.temp_credential, identity.login_backoff, "
                "identity.account"
            )
        )
    snapshot, snapshot_hash = snapshot_with_idle_minutes(15)
    try:
        _secret, token = _initialize_account(identity_rw_engine, deps, monkeypatch)
        with configuration_owner_engine.begin() as db:
            db.execute(
                text(
                    "INSERT INTO identity.version ("
                    "namespace, scope, version, snapshot, changeset, published_by, reason, "
                    "published_at, schema_revision, snapshot_hash, validation_evidence, "
                    "dependency_versions, preview_evidence) VALUES ("
                    "'identity', 'PLATFORM', 2, CAST(:snapshot AS JSONB), '{}'::jsonb, "
                    "'SYSTEM_TEST', 'test-only direct version helper', now(), 1, :hash, "
                    "CAST(:evidence AS JSONB), '{}'::jsonb, '{}'::jsonb)"
                ),
                {
                    "snapshot": json.dumps(snapshot, separators=(",", ":")),
                    "hash": snapshot_hash,
                    "evidence": json.dumps({"valid": True, "issues": []}),
                },
            )
            db.execute(
                text(
                    "UPDATE identity.active_pointer SET version=2 "
                    "WHERE namespace='identity' AND scope='PLATFORM'"
                )
            )
        clock.value += timedelta(minutes=16)
        with identity_rw_engine.begin() as db:
            assert validate_session(db, raw_token=token, dependencies=deps) is None
    finally:
        with configuration_owner_engine.begin() as db:
            db.execute(
                text(
                    "UPDATE identity.active_pointer SET version=1 "
                    "WHERE namespace='identity' AND scope='PLATFORM'"
                )
            )
            db.execute(
                text(
                    "DELETE FROM identity.version WHERE namespace='identity' "
                    "AND scope='PLATFORM' AND version=2"
                )
            )
            db.execute(
                text(
                    "TRUNCATE identity.idempotency_record, identity.auth_challenge, "
                    "identity.session, identity.temp_credential, identity.login_backoff, "
                    "identity.account"
                )
            )
