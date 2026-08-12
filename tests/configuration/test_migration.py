from collections.abc import Iterator
from contextlib import contextmanager
from runpy import run_path
from uuid import uuid4

import pytest
from sqlalchemy import Connection, Engine, inspect, text
from sqlalchemy.exc import IntegrityError, ProgrammingError

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("configuration_seed")]


def _columns(engine: Engine, table: str) -> dict[str, tuple[str, bool]]:
    return {
        column["name"]: (type(column["type"]).__name__, bool(column["nullable"]))
        for column in inspect(engine).get_columns(table, schema="identity")
    }


@contextmanager
def _rollback(engine: Engine) -> Iterator[Connection]:
    with engine.connect() as db:
        transaction = db.begin()
        try:
            yield db
        finally:
            transaction.rollback()


def test_identity_owns_policy_publish_and_archive_lifecycle_tables(
    configuration_owner_engine: Engine,
) -> None:
    tables = set(inspect(configuration_owner_engine).get_table_names(schema="identity"))

    assert {
        "policy_key",
        "draft",
        "version",
        "active_pointer",
        "configuration_outbox",
    } <= tables
    assert _columns(configuration_owner_engine, "policy_key") == {
        "key": ("TEXT", False),
        "namespace": ("TEXT", False),
        "value_type": ("TEXT", False),
        "unit": ("TEXT", True),
        "default_value": ("JSONB", False),
        "min_value": ("JSONB", True),
        "max_value": ("JSONB", True),
        "enum_values": ("JSONB", True),
        "effect_semantics": ("TEXT", False),
        "schema_revision": ("INTEGER", False),
    }
    assert _columns(configuration_owner_engine, "draft") == {
        "id": ("UUID", False),
        "namespace": ("TEXT", False),
        "scope": ("TEXT", False),
        "content": ("JSONB", False),
        "base_version": ("BIGINT", False),
        "owner_id": ("TEXT", False),
        "revision": ("INTEGER", False),
        "status": ("TEXT", False),
        "stale": ("BOOLEAN", False),
        "last_meaningful_activity_at": ("TIMESTAMP", False),
        "archived_at": ("TIMESTAMP", True),
        "schema_revision": ("INTEGER", False),
        "content_hash": ("TEXT", False),
        "validation_evidence": ("JSONB", True),
        "validation_content_hash": ("TEXT", True),
        "validation_schema_revision": ("INTEGER", True),
        "validation_base_version": ("BIGINT", True),
        "validation_dependency_versions": ("JSONB", True),
        "rollback_from_version": ("BIGINT", True),
        "preview_evidence": ("JSONB", True),
        "preview_content_hash": ("TEXT", True),
        "preview_schema_revision": ("INTEGER", True),
        "preview_base_version": ("BIGINT", True),
        "preview_dependency_versions": ("JSONB", True),
    }
    assert _columns(configuration_owner_engine, "version") == {
        "namespace": ("TEXT", False),
        "scope": ("TEXT", False),
        "version": ("BIGINT", False),
        "snapshot": ("JSONB", False),
        "changeset": ("JSONB", False),
        "published_by": ("TEXT", False),
        "reason": ("TEXT", False),
        "published_at": ("TIMESTAMP", False),
        "schema_revision": ("INTEGER", False),
        "snapshot_hash": ("TEXT", False),
        "validation_evidence": ("JSONB", False),
        "dependency_versions": ("JSONB", False),
        "preview_evidence": ("JSONB", False),
        "activated_at": ("TIMESTAMP", False),
    }
    assert _columns(configuration_owner_engine, "active_pointer") == {
        "namespace": ("TEXT", False),
        "scope": ("TEXT", False),
        "version": ("BIGINT", False),
    }
    assert _columns(configuration_owner_engine, "configuration_outbox") == {
        "id": ("UUID", False),
        "namespace": ("TEXT", False),
        "scope": ("TEXT", False),
        "event_type": ("TEXT", False),
        "aggregate_id": ("TEXT", False),
        "payload": ("JSONB", False),
        "occurred_at": ("TIMESTAMP", False),
        "delivered_at": ("TIMESTAMP", True),
    }


def test_identity_owns_a_separate_configuration_idempotency_record(
    configuration_owner_engine: Engine,
) -> None:
    assert _columns(configuration_owner_engine, "configuration_idempotency_record") == {
        "id": ("UUID", False),
        "actor": ("TEXT", False),
        "operation": ("TEXT", False),
        "idempotency_key": ("TEXT", False),
        "request_fingerprint": ("TEXT", False),
        "state": ("TEXT", False),
        "http_status": ("INTEGER", True),
        "result_metadata": ("JSONB", True),
        "sealed_response": ("BYTEA", True),
        "created_at": ("TIMESTAMP", False),
        "updated_at": ("TIMESTAMP", False),
        "completed_at": ("TIMESTAMP", True),
    }


def test_draft_rejects_status_outside_the_lifecycle(
    configuration_owner_engine: Engine,
) -> None:
    with _rollback(configuration_owner_engine) as db:
        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "INSERT INTO identity.draft ("
                    "id, namespace, scope, content, base_version, owner_id, revision, "
                    "status, stale, last_meaningful_activity_at, schema_revision, content_hash"
                    ") VALUES ("
                    "'00000000-0000-0000-0000-000000000011', 'identity', 'PLATFORM', "
                    "'{}'::jsonb, 1, 'owner-1', 1, 'PUBLISHED', false, now(), 1, 'hash'"
                    ")"
                )
            )


@pytest.mark.parametrize(
    "override",
    [
        {"namespace": ""},
        {"scope": "WORKSPACE"},
        {"content": "[]"},
        {"base_version": 0},
        {"owner_id": ""},
        {"revision": 0},
        {"schema_revision": 0},
        {"content_hash": ""},
        {"status": "ARCHIVED", "archived_at": None},
        {"status": "DRAFT", "archived_at": "2026-08-12T00:00:00+00:00"},
        {"validation_evidence": "{}"},
    ],
    ids=[
        "namespace",
        "scope",
        "content-object",
        "base-version",
        "owner",
        "revision",
        "schema-revision",
        "content-hash",
        "archived-timestamp",
        "draft-timestamp",
        "validation-binding",
    ],
)
def test_draft_rejects_invalid_structure(
    configuration_owner_engine: Engine,
    override: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "namespace": "identity",
        "scope": "PLATFORM",
        "content": "{}",
        "base_version": 1,
        "owner_id": "owner-1",
        "revision": 1,
        "status": "DRAFT",
        "stale": False,
        "archived_at": None,
        "schema_revision": 1,
        "content_hash": "hash",
        "validation_evidence": None,
        "validation_content_hash": None,
        "validation_schema_revision": None,
        "validation_base_version": None,
        "validation_dependency_versions": None,
    }
    values.update(override)

    with _rollback(configuration_owner_engine) as db:
        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "INSERT INTO identity.draft ("
                    "id, namespace, scope, content, base_version, owner_id, revision, "
                    "status, stale, last_meaningful_activity_at, archived_at, schema_revision, "
                    "content_hash, validation_evidence, validation_content_hash, "
                    "validation_schema_revision, validation_base_version, "
                    "validation_dependency_versions"
                    ") VALUES ("
                    "'00000000-0000-0000-0000-000000000012', :namespace, :scope, "
                    "CAST(:content AS JSONB), :base_version, :owner_id, :revision, :status, "
                    ":stale, now(), :archived_at, :schema_revision, :content_hash, "
                    "CAST(:validation_evidence AS JSONB), :validation_content_hash, "
                    ":validation_schema_revision, :validation_base_version, "
                    "CAST(:validation_dependency_versions AS JSONB))"
                ),
                values,
            )


@pytest.mark.parametrize(
    "override",
    [
        {"key": "other.setting"},
        {"namespace": "other"},
        {"value_type": "STRING"},
        {"default_value": "[]", "value_type": "OBJECT"},
        {"min_value": "10", "max_value": "1"},
        {"enum_values": "{}"},
        {"effect_semantics": "CLIENT_DEFINED"},
        {"schema_revision": 0},
    ],
    ids=[
        "key-prefix",
        "namespace",
        "value-type",
        "default-type",
        "range-order",
        "enum-array",
        "effect-semantics",
        "schema-revision",
    ],
)
def test_policy_key_rejects_invalid_typed_schema(
    configuration_owner_engine: Engine,
    override: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "key": "identity.test_limit",
        "namespace": "identity",
        "value_type": "INTEGER",
        "unit": "COUNT",
        "default_value": "3",
        "min_value": "1",
        "max_value": "10",
        "enum_values": None,
        "effect_semantics": "IMMEDIATE",
        "schema_revision": 1,
    }
    values.update(override)

    with _rollback(configuration_owner_engine) as db:
        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "INSERT INTO identity.policy_key ("
                    "key, namespace, value_type, unit, default_value, min_value, max_value, "
                    "enum_values, effect_semantics, schema_revision"
                    ") VALUES ("
                    ":key, :namespace, :value_type, :unit, "
                    "CAST(:default_value AS JSONB), CAST(:min_value AS JSONB), "
                    "CAST(:max_value AS JSONB), CAST(:enum_values AS JSONB), "
                    ":effect_semantics, :schema_revision)"
                ),
                values,
            )


@pytest.mark.parametrize(
    "override",
    [
        {"namespace": "other"},
        {"scope": "WORKSPACE"},
        {"version": 0},
        {"snapshot": "[]"},
        {"changeset": "[]"},
        {"published_by": ""},
        {"reason": ""},
        {"schema_revision": 0},
        {"snapshot_hash": "not-a-sha256"},
        {"validation_evidence": "[]"},
        {"dependency_versions": "[]"},
        {"preview_evidence": "[]"},
    ],
    ids=[
        "namespace",
        "scope",
        "version",
        "snapshot",
        "changeset",
        "publisher",
        "reason",
        "schema-revision",
        "snapshot-hash",
        "validation-evidence",
        "dependency-versions",
        "preview-evidence",
    ],
)
def test_published_version_rejects_invalid_immutable_evidence(
    configuration_owner_engine: Engine,
    override: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "namespace": "identity",
        "scope": "PLATFORM",
        "version": 2,
        "snapshot": "{}",
        "changeset": "{}",
        "published_by": "SYSTEM_TEST",
        "reason": "test-only direct version",
        "schema_revision": 1,
        "snapshot_hash": "a" * 64,
        "validation_evidence": "{}",
        "dependency_versions": "{}",
        "preview_evidence": "{}",
    }
    values.update(override)

    with _rollback(configuration_owner_engine) as db:
        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "INSERT INTO identity.version ("
                    "namespace, scope, version, snapshot, changeset, published_by, reason, "
                    "published_at, schema_revision, snapshot_hash, validation_evidence, "
                    "dependency_versions, preview_evidence"
                    ") VALUES ("
                    ":namespace, :scope, :version, CAST(:snapshot AS JSONB), "
                    "CAST(:changeset AS JSONB), :published_by, :reason, now(), "
                    ":schema_revision, :snapshot_hash, CAST(:validation_evidence AS JSONB), "
                    "CAST(:dependency_versions AS JSONB), CAST(:preview_evidence AS JSONB))"
                ),
                values,
            )


def test_active_pointer_must_reference_an_identity_platform_version(
    configuration_owner_engine: Engine,
) -> None:
    with _rollback(configuration_owner_engine) as db:
        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "INSERT INTO identity.active_pointer (namespace, scope, version) "
                    "VALUES ('identity', 'PLATFORM', 999999)"
                )
            )


def test_policy_tables_have_runtime_lookup_indexes(
    configuration_owner_engine: Engine,
) -> None:
    inspector = inspect(configuration_owner_engine)
    indexes = {
        table: {
            index["name"]: tuple(index["column_names"])
            for index in inspector.get_indexes(table, schema="identity")
        }
        for table in ("policy_key", "draft", "version")
    }

    assert indexes == {
        "policy_key": {
            "ix_identity_policy_key_namespace": ("namespace", "key"),
        },
        "draft": {
            "ix_identity_draft_lookup": ("namespace", "scope", "status", "stale"),
            "ix_identity_draft_owner_activity": (
                "owner_id",
                "status",
                "last_meaningful_activity_at",
            ),
        },
        "version": {
            "ix_identity_version_published_at": ("namespace", "scope", "published_at"),
        },
    }


def test_migration_seeds_the_exact_identity_defaults_and_active_version(
    configuration_owner_engine: Engine,
) -> None:
    with configuration_owner_engine.connect() as db:
        catalog = {
            row["key"]: {
                "value_type": row["value_type"],
                "unit": row["unit"],
                "default": row["default_value"],
                "minimum": row["min_value"],
                "maximum": row["max_value"],
                "enum": row["enum_values"],
                "effect": row["effect_semantics"],
                "schema_revision": row["schema_revision"],
            }
            for row in db.execute(
                text(
                    "SELECT key, value_type, unit, default_value, min_value, max_value, "
                    "enum_values, effect_semantics, schema_revision "
                    "FROM identity.policy_key WHERE namespace='identity'"
                )
            ).mappings()
        }
        active = (
            db.execute(
                text(
                    "SELECT p.version, v.snapshot, v.changeset, v.published_by, v.reason, "
                    "v.schema_revision, v.snapshot_hash, v.validation_evidence, "
                    "v.dependency_versions, v.preview_evidence, v.published_at "
                    "FROM identity.active_pointer p JOIN identity.version v "
                    "USING (namespace, scope, version) "
                    "WHERE p.namespace='identity' AND p.scope='PLATFORM'"
                )
            )
            .mappings()
            .one_or_none()
        )
        audit = (
            db.execute(
                text(
                    "SELECT actor, actor_type, action, target_type, target_id, result, reason, "
                    "correlation_id, occurred_at FROM audit.audit_event "
                    "WHERE id='configuration-system-seed-identity-v1'"
                )
            )
            .mappings()
            .one_or_none()
        )

    assert catalog == {
        "identity.temp_credential_ttl": {
            "value_type": "INTEGER",
            "unit": "HOURS",
            "default": 24,
            "minimum": 1,
            "maximum": None,
            "enum": None,
            "effect": "NEW_OBJECT",
            "schema_revision": 1,
        },
        "identity.password_max_age": {
            "value_type": "ENUM_OR_INTEGER",
            "unit": "DAYS",
            "default": "NEVER",
            "minimum": 1,
            "maximum": None,
            "enum": ["NEVER", 90, 180],
            "effect": "IMMEDIATE",
            "schema_revision": 1,
        },
        "identity.session_cap": {
            "value_type": "INTEGER",
            "unit": "SESSIONS",
            "default": 3,
            "minimum": 1,
            "maximum": 10,
            "enum": None,
            "effect": "IMMEDIATE",
            "schema_revision": 1,
        },
        "identity.session_idle_timeout": {
            "value_type": "INTEGER",
            "unit": "MINUTES",
            "default": 60,
            "minimum": 15,
            "maximum": 240,
            "enum": None,
            "effect": "IMMEDIATE",
            "schema_revision": 1,
        },
        "identity.login_backoff": {
            "value_type": "OBJECT",
            "unit": None,
            "default": {
                "failureThreshold": 5,
                "initialDelaySeconds": 30,
                "maximumDelaySeconds": 900,
                "resetAfterHours": 24,
            },
            "minimum": None,
            "maximum": None,
            "enum": None,
            "effect": "IMMEDIATE",
            "schema_revision": 1,
        },
        "identity.totp_attempt_cap": {
            "value_type": "INTEGER",
            "unit": "ATTEMPTS",
            "default": 5,
            "minimum": 1,
            "maximum": None,
            "enum": None,
            "effect": "IMMEDIATE",
            "schema_revision": 1,
        },
        "identity.draft_archive_after": {
            "value_type": "INTEGER",
            "unit": "DAYS",
            "default": 30,
            "minimum": 1,
            "maximum": None,
            "enum": None,
            "effect": "NEXT_SCHEDULE",
            "schema_revision": 1,
        },
    }
    assert active is not None
    assert audit is not None
    expected_snapshot = {
        "identity.temp_credential_ttl": 24,
        "identity.password_max_age": "NEVER",
        "identity.session_cap": 3,
        "identity.session_idle_timeout": 60,
        "identity.login_backoff": {
            "failureThreshold": 5,
            "initialDelaySeconds": 30,
            "maximumDelaySeconds": 900,
            "resetAfterHours": 24,
        },
        "identity.totp_attempt_cap": 5,
        "identity.draft_archive_after": 30,
    }
    assert active["version"] == 1
    assert active["snapshot"] == expected_snapshot
    assert active["changeset"] == {"source": "SYSTEM_SEED", "values": expected_snapshot}
    assert active["published_by"] == "SYSTEM_SEED"
    assert active["reason"] == "bootstrap identity policy defaults"
    assert active["schema_revision"] == 1
    assert active["snapshot_hash"] == (
        "0406fd566b249b81c2c260833d56264c728171c38cba10c104781f7142ed3cb8"
    )
    assert active["validation_evidence"] == {
        "issues": [],
        "source": "SYSTEM_SEED",
        "valid": True,
    }
    assert active["dependency_versions"] == {}
    assert active["preview_evidence"] == {
        "effects": "bootstrap defaults",
        "source": "SYSTEM_SEED",
    }
    assert audit == {
        "actor": "SYSTEM_SEED",
        "actor_type": "system",
        "action": "configuration.policy.seeded",
        "target_type": "policy_namespace",
        "target_id": "identity",
        "result": "SUCCESS",
        "reason": (
            "source=bootstrap; namespace=identity; version=1; "
            "snapshotHash=0406fd566b249b81c2c260833d56264c728171c38cba10c104781f7142ed3cb8"
        ),
        "correlation_id": "configuration-system-seed-identity-v1",
        "occurred_at": active["published_at"],
    }


def test_identity_policy_seed_is_idempotent_and_keeps_original_time(
    configuration_owner_engine: Engine,
) -> None:
    seed_sql = str(run_path("migrations/identity/0005_configuration_policy.py")["_SEED_SQL"])
    with configuration_owner_engine.connect() as db:
        before = db.execute(
            text(
                "SELECT v.published_at, a.occurred_at "
                "FROM identity.version v JOIN audit.audit_event a "
                "ON a.id='configuration-system-seed-identity-v1' "
                "WHERE v.namespace='identity' AND v.scope='PLATFORM' AND v.version=1"
            )
        ).one()

    with configuration_owner_engine.begin() as db:
        db.exec_driver_sql(seed_sql)
        db.exec_driver_sql(seed_sql)

    with configuration_owner_engine.connect() as db:
        counts = db.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM identity.policy_key WHERE namespace='identity'), "
                "(SELECT count(*) FROM identity.version WHERE namespace='identity' "
                " AND scope='PLATFORM' AND version=1), "
                "(SELECT count(*) FROM identity.active_pointer WHERE namespace='identity' "
                " AND scope='PLATFORM'), "
                "(SELECT count(*) FROM audit.audit_event "
                " WHERE id='configuration-system-seed-identity-v1')"
            )
        ).one()
        after = db.execute(
            text(
                "SELECT v.published_at, a.occurred_at "
                "FROM identity.version v JOIN audit.audit_event a "
                "ON a.id='configuration-system-seed-identity-v1' "
                "WHERE v.namespace='identity' AND v.scope='PLATFORM' AND v.version=1"
            )
        ).one()

    assert counts == (7, 1, 1, 1)
    assert after == before


def test_missing_configuration_privilege_role_is_nologin(
    configuration_owner_engine: Engine,
) -> None:
    with configuration_owner_engine.connect() as db:
        can_login = db.execute(
            text("SELECT rolcanlogin FROM pg_roles WHERE rolname='configuration_rw'")
        ).scalar_one_or_none()

    assert can_login is False


def test_configuration_rw_has_only_publish_and_archive_table_privileges(
    configuration_owner_engine: Engine,
    configuration_rw_engine: Engine,
) -> None:
    del configuration_rw_engine
    with configuration_owner_engine.connect() as db:
        schema_privileges = {
            privilege
            for privilege in ("USAGE", "CREATE")
            if db.execute(
                text("SELECT has_schema_privilege('configuration_rw', 'identity', :value)"),
                {"value": privilege},
            ).scalar_one()
        }
        table_privileges = {
            table_name: {
                privilege
                for privilege in (
                    "SELECT",
                    "INSERT",
                    "UPDATE",
                    "DELETE",
                    "TRUNCATE",
                    "REFERENCES",
                    "TRIGGER",
                )
                if db.execute(
                    text(
                        "SELECT has_table_privilege("
                        "'configuration_rw', 'identity.' || :table_name, :value)"
                    ),
                    {"table_name": table_name, "value": privilege},
                ).scalar_one()
            }
            for table_name in (
                "policy_key",
                "draft",
                "version",
                "active_pointer",
                "configuration_idempotency_record",
                "configuration_outbox",
                "auth_challenge",
            )
        }
        audit_dml = {
            privilege
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
            if db.execute(
                text("SELECT has_table_privilege('configuration_rw', 'audit.audit_event', :p)"),
                {"p": privilege},
            ).scalar_one()
        }
        account_privileges = {
            privilege
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
            if db.execute(
                text(
                    "SELECT has_table_privilege('configuration_rw', 'identity.account', :privilege)"
                ),
                {"privilege": privilege},
            ).scalar_one()
        }
        account_update_columns = {
            column
            for column in ("totp_last_step", "updated_at", "password_hash", "status")
            if db.execute(
                text(
                    "SELECT has_column_privilege("
                    "'configuration_rw', 'identity.account', :column, 'UPDATE')"
                ),
                {"column": column},
            ).scalar_one()
        }
        challenge_insert_columns = {
            column
            for column in (
                "id",
                "token_hash",
                "purpose",
                "account_id",
                "actor_id",
                "issued_at",
                "expires_at",
                "attempt_limit",
                "attempt_count",
                "consumed_at",
                "revoked_at",
            )
            if db.execute(
                text(
                    "SELECT has_column_privilege("
                    "'configuration_rw', 'identity.auth_challenge', :column, 'INSERT')"
                ),
                {"column": column},
            ).scalar_one()
        }
        challenge_update_columns = {
            column
            for column in (
                "id",
                "token_hash",
                "purpose",
                "account_id",
                "actor_id",
                "issued_at",
                "expires_at",
                "attempt_limit",
                "attempt_count",
                "consumed_at",
                "revoked_at",
            )
            if db.execute(
                text(
                    "SELECT has_column_privilege("
                    "'configuration_rw', 'identity.auth_challenge', :column, 'UPDATE')"
                ),
                {"column": column},
            ).scalar_one()
        }

    assert schema_privileges == {"USAGE"}
    assert table_privileges == {
        "policy_key": {"SELECT"},
        "draft": {"SELECT", "INSERT", "UPDATE"},
        "version": {"SELECT", "INSERT"},
        "active_pointer": {"SELECT", "UPDATE"},
        "configuration_idempotency_record": {"SELECT", "INSERT", "UPDATE"},
        "configuration_outbox": {"SELECT", "INSERT"},
        "auth_challenge": set(),
    }
    assert audit_dml == set()
    assert account_privileges == set()
    assert account_update_columns == set()
    assert challenge_insert_columns == set()
    assert challenge_update_columns == set()


@pytest.mark.parametrize(
    "query",
    [
        "SELECT password_hash FROM identity.account LIMIT 1",
        "SELECT totp_sealed FROM identity.account LIMIT 1",
        "SELECT token_hash FROM identity.auth_challenge LIMIT 1",
        (
            "SELECT id FROM identity.auth_challenge "
            "WHERE purpose NOT IN ('POLICY_PUBLISH', 'POLICY_ROLLBACK') LIMIT 1"
        ),
    ],
    ids=["password-hash", "sealed-totp", "challenge-token", "unrelated-challenge"],
)
def test_configuration_runtime_cannot_directly_read_identity_credentials_or_challenges(
    configuration_rw_engine: Engine,
    query: str,
) -> None:
    with configuration_rw_engine.connect() as db:
        with pytest.raises(ProgrammingError, match="permission denied"):
            db.execute(text(query)).all()


def test_configuration_rw_cannot_delete_or_create_identity_objects(
    configuration_rw_engine: Engine,
) -> None:
    with configuration_rw_engine.connect() as db:
        with pytest.raises(ProgrammingError, match="permission denied"):
            db.execute(text("DELETE FROM identity.draft WHERE false"))
    with configuration_rw_engine.connect() as db:
        with pytest.raises(ProgrammingError, match="permission denied"):
            db.execute(text("CREATE TABLE identity.configuration_runtime_ddl_forbidden (id int)"))


def test_identity_runtime_has_exact_policy_command_owner_privileges(
    configuration_owner_engine: Engine,
) -> None:
    with configuration_owner_engine.connect() as db:
        privileges = {
            table_name: {
                privilege
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
                if db.execute(
                    text(
                        "SELECT has_table_privilege("
                        "'identity_rw', 'identity.' || :table_name, :privilege)"
                    ),
                    {"table_name": table_name, "privilege": privilege},
                ).scalar_one()
            }
            for table_name in (
                "policy_key",
                "draft",
                "version",
                "active_pointer",
                "configuration_idempotency_record",
                "configuration_outbox",
            )
        }

    assert privileges == {
        "policy_key": {"SELECT"},
        "draft": {"SELECT", "INSERT", "UPDATE"},
        "version": {"SELECT", "INSERT"},
        "active_pointer": {"SELECT", "UPDATE"},
        "configuration_idempotency_record": {"SELECT", "INSERT", "UPDATE"},
        "configuration_outbox": {"INSERT"},
    }


def test_configuration_rw_appends_audit_only_through_the_hardened_function(
    configuration_owner_engine: Engine,
    configuration_rw_engine: Engine,
) -> None:
    committed_id = str(uuid4())
    rolled_back_id = str(uuid4())
    statement = (
        "SELECT audit.append_event("
        ":id, now(), 'actor-1', 'human', 'configuration.draft.created', "
        "'configuration_draft', 'draft-1', 'SUCCESS', 'test reason', 'req-task11', 1)"
    )
    with configuration_rw_engine.begin() as db:
        db.execute(text(statement), {"id": committed_id})
    with configuration_rw_engine.connect() as db:
        transaction = db.begin()
        db.execute(text(statement), {"id": rolled_back_id})
        transaction.rollback()

    with configuration_owner_engine.connect() as db:
        counts = db.execute(
            text(
                "SELECT "
                "count(*) FILTER (WHERE id=:committed_id), "
                "count(*) FILTER (WHERE id=:rolled_back_id) "
                "FROM audit.audit_event WHERE id IN (:committed_id, :rolled_back_id)"
            ),
            {"committed_id": committed_id, "rolled_back_id": rolled_back_id},
        ).one()

    assert counts == (1, 0)
