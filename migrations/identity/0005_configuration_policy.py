"""identity-owned policy registry, drafts, versions, and active pointer.

Controller resolution #11 places identity namespace policy facts in the identity
schema.  The configuration module orchestrates these facts through the identity
package-root Facade.
"""

from alembic import op

revision = "0005_identity_configuration"
down_revision = "0004_identity_bootstrap_totp_cap"
branch_labels = None
depends_on = "0002_audit_transactional_append"

_SNAPSHOT_HASH = "0406fd566b249b81c2c260833d56264c728171c38cba10c104781f7142ed3cb8"
_SEED_AUDIT_ID = "configuration-system-seed-identity-v1"
_SEED_SQL = f"""
INSERT INTO identity.policy_key (
    key, namespace, value_type, unit, default_value, min_value, max_value,
    enum_values, effect_semantics, schema_revision
) VALUES
    (
        'identity.temp_credential_ttl', 'identity', 'INTEGER', 'HOURS',
        '24'::jsonb, '1'::jsonb, NULL, NULL, 'NEW_OBJECT', 1
    ),
    (
        'identity.password_max_age', 'identity', 'ENUM_OR_INTEGER', 'DAYS',
        '"NEVER"'::jsonb, '1'::jsonb, NULL, '["NEVER", 90, 180]'::jsonb,
        'IMMEDIATE', 1
    ),
    (
        'identity.session_cap', 'identity', 'INTEGER', 'SESSIONS',
        '3'::jsonb, '1'::jsonb, '10'::jsonb, NULL, 'IMMEDIATE', 1
    ),
    (
        'identity.session_idle_timeout', 'identity', 'INTEGER', 'MINUTES',
        '60'::jsonb, '15'::jsonb, '240'::jsonb, NULL, 'IMMEDIATE', 1
    ),
    (
        'identity.login_backoff', 'identity', 'OBJECT', NULL,
        '{{"failureThreshold":5,"initialDelaySeconds":30,"maximumDelaySeconds":900,'
        '"resetAfterHours":24}}'::jsonb,
        NULL, NULL, NULL, 'IMMEDIATE', 1
    ),
    (
        'identity.totp_attempt_cap', 'identity', 'INTEGER', 'ATTEMPTS',
        '5'::jsonb, '1'::jsonb, NULL, NULL, 'IMMEDIATE', 1
    ),
    (
        'identity.draft_archive_after', 'identity', 'INTEGER', 'DAYS',
        '30'::jsonb, '1'::jsonb, NULL, NULL, 'NEXT_SCHEDULE', 1
    )
ON CONFLICT (key) DO NOTHING;

INSERT INTO identity.version (
    namespace, scope, version, snapshot, changeset, published_by, reason,
    published_at, schema_revision, snapshot_hash, validation_evidence,
    dependency_versions, preview_evidence
) VALUES (
    'identity',
    'PLATFORM',
    1,
    '{{
        "identity.temp_credential_ttl":24,
        "identity.password_max_age":"NEVER",
        "identity.session_cap":3,
        "identity.session_idle_timeout":60,
        "identity.login_backoff":{{
            "failureThreshold":5,
            "initialDelaySeconds":30,
            "maximumDelaySeconds":900,
            "resetAfterHours":24
        }},
        "identity.totp_attempt_cap":5,
        "identity.draft_archive_after":30
    }}'::jsonb,
    '{{
        "source":"SYSTEM_SEED",
        "values":{{
            "identity.temp_credential_ttl":24,
            "identity.password_max_age":"NEVER",
            "identity.session_cap":3,
            "identity.session_idle_timeout":60,
            "identity.login_backoff":{{
                "failureThreshold":5,
                "initialDelaySeconds":30,
                "maximumDelaySeconds":900,
                "resetAfterHours":24
            }},
            "identity.totp_attempt_cap":5,
            "identity.draft_archive_after":30
        }}
    }}'::jsonb,
    'SYSTEM_SEED',
    'bootstrap identity policy defaults',
    COALESCE(
        (
            SELECT occurred_at
            FROM audit.audit_event
            WHERE id = '{_SEED_AUDIT_ID}'
        ),
        transaction_timestamp()
    ),
    1,
    '{_SNAPSHOT_HASH}',
    '{{"issues":[],"source":"SYSTEM_SEED","valid":true}}'::jsonb,
    '{{}}'::jsonb,
    '{{"effects":"bootstrap defaults","source":"SYSTEM_SEED"}}'::jsonb
)
ON CONFLICT (namespace, scope, version) DO NOTHING;

INSERT INTO identity.active_pointer (namespace, scope, version)
VALUES ('identity', 'PLATFORM', 1)
ON CONFLICT (namespace, scope) DO NOTHING;

SELECT audit.append_event(
    '{_SEED_AUDIT_ID}',
    (SELECT published_at FROM identity.version
     WHERE namespace='identity' AND scope='PLATFORM' AND version=1),
    'SYSTEM_SEED',
    'system',
    'configuration.policy.seeded',
    'policy_namespace',
    'identity',
    'SUCCESS',
    'source=bootstrap; namespace=identity; version=1; snapshotHash={_SNAPSHOT_HASH}',
    '{_SEED_AUDIT_ID}',
    1
)
WHERE NOT EXISTS (
    SELECT 1 FROM audit.audit_event WHERE id = '{_SEED_AUDIT_ID}'
);
"""


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE identity.policy_key (
            key TEXT PRIMARY KEY,
            namespace TEXT NOT NULL,
            value_type TEXT NOT NULL,
            unit TEXT,
            default_value JSONB NOT NULL,
            min_value JSONB,
            max_value JSONB,
            enum_values JSONB,
            effect_semantics TEXT NOT NULL,
            schema_revision INTEGER NOT NULL,
            CONSTRAINT ck_identity_policy_key_namespace
                CHECK (namespace = 'identity' AND key LIKE namespace || '.%'),
            CONSTRAINT ck_identity_policy_key_value_type
                CHECK (value_type IN ('INTEGER', 'ENUM_OR_INTEGER', 'OBJECT')),
            CONSTRAINT ck_identity_policy_key_unit
                CHECK (unit IS NULL OR length(btrim(unit)) > 0),
            CONSTRAINT ck_identity_policy_key_default_type
                CHECK (
                    (value_type = 'INTEGER' AND default_value::text ~ '^-?[0-9]+$')
                    OR (
                        value_type = 'ENUM_OR_INTEGER'
                        AND jsonb_typeof(default_value) IN ('number', 'string')
                    )
                    OR (value_type = 'OBJECT' AND jsonb_typeof(default_value) = 'object')
                ),
            CONSTRAINT ck_identity_policy_key_range
                CHECK (
                    (min_value IS NULL OR min_value::text ~ '^-?[0-9]+$')
                    AND (max_value IS NULL OR max_value::text ~ '^-?[0-9]+$')
                    AND (
                        min_value IS NULL
                        OR max_value IS NULL
                        OR (min_value #>> '{}')::numeric <= (max_value #>> '{}')::numeric
                    )
                ),
            CONSTRAINT ck_identity_policy_key_enum
                CHECK (enum_values IS NULL OR jsonb_typeof(enum_values) = 'array'),
            CONSTRAINT ck_identity_policy_key_effect_semantics
                CHECK (
                    effect_semantics IN (
                        'IMMEDIATE', 'NEW_OBJECT', 'NEXT_SCHEDULE', 'NEW_ATTEMPT',
                        'RESTART', 'ROLLOUT', 'RECREATE'
                    )
                ),
            CONSTRAINT ck_identity_policy_key_schema_revision
                CHECK (schema_revision > 0)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE identity.draft (
            id UUID PRIMARY KEY,
            namespace TEXT NOT NULL,
            scope TEXT NOT NULL,
            content JSONB NOT NULL,
            base_version BIGINT NOT NULL,
            owner_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            status TEXT NOT NULL,
            stale BOOLEAN NOT NULL,
            last_meaningful_activity_at TIMESTAMPTZ NOT NULL,
            archived_at TIMESTAMPTZ,
            schema_revision INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            validation_evidence JSONB,
            validation_content_hash TEXT,
            validation_schema_revision INTEGER,
            validation_base_version BIGINT,
            validation_dependency_versions JSONB,
            CONSTRAINT ck_identity_draft_namespace
                CHECK (namespace = 'identity'),
            CONSTRAINT ck_identity_draft_scope
                CHECK (scope = 'PLATFORM'),
            CONSTRAINT ck_identity_draft_content
                CHECK (jsonb_typeof(content) = 'object'),
            CONSTRAINT ck_identity_draft_base_version
                CHECK (base_version > 0),
            CONSTRAINT ck_identity_draft_owner
                CHECK (length(btrim(owner_id)) > 0),
            CONSTRAINT ck_identity_draft_revision
                CHECK (revision > 0),
            CONSTRAINT ck_identity_draft_status
                CHECK (status IN ('DRAFT', 'ARCHIVED')),
            CONSTRAINT ck_identity_draft_archive_lifecycle
                CHECK (
                    (status = 'DRAFT' AND archived_at IS NULL)
                    OR (
                        status = 'ARCHIVED'
                        AND archived_at IS NOT NULL
                        AND archived_at >= last_meaningful_activity_at
                    )
                ),
            CONSTRAINT ck_identity_draft_schema_revision
                CHECK (schema_revision > 0),
            CONSTRAINT ck_identity_draft_content_hash
                CHECK (content_hash ~ '^[0-9a-f]{64}$'),
            CONSTRAINT ck_identity_draft_validation_binding
                CHECK (
                    (
                        validation_evidence IS NULL
                        AND validation_content_hash IS NULL
                        AND validation_schema_revision IS NULL
                        AND validation_base_version IS NULL
                        AND validation_dependency_versions IS NULL
                    )
                    OR (
                        jsonb_typeof(validation_evidence) = 'object'
                        AND validation_content_hash = content_hash
                        AND validation_schema_revision = schema_revision
                        AND validation_base_version = base_version
                        AND jsonb_typeof(validation_dependency_versions) = 'object'
                    )
                )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE identity.version (
            namespace TEXT NOT NULL,
            scope TEXT NOT NULL,
            version BIGINT NOT NULL,
            snapshot JSONB NOT NULL,
            changeset JSONB NOT NULL,
            published_by TEXT NOT NULL,
            reason TEXT NOT NULL,
            published_at TIMESTAMPTZ NOT NULL,
            schema_revision INTEGER NOT NULL,
            snapshot_hash TEXT NOT NULL,
            validation_evidence JSONB NOT NULL,
            dependency_versions JSONB NOT NULL,
            preview_evidence JSONB NOT NULL,
            PRIMARY KEY (namespace, scope, version),
            CONSTRAINT ck_identity_version_namespace
                CHECK (namespace = 'identity'),
            CONSTRAINT ck_identity_version_scope
                CHECK (scope = 'PLATFORM'),
            CONSTRAINT ck_identity_version_number
                CHECK (version > 0),
            CONSTRAINT ck_identity_version_snapshot
                CHECK (jsonb_typeof(snapshot) = 'object'),
            CONSTRAINT ck_identity_version_changeset
                CHECK (jsonb_typeof(changeset) = 'object'),
            CONSTRAINT ck_identity_version_publisher
                CHECK (length(btrim(published_by)) > 0),
            CONSTRAINT ck_identity_version_reason
                CHECK (length(btrim(reason)) > 0),
            CONSTRAINT ck_identity_version_schema_revision
                CHECK (schema_revision > 0),
            CONSTRAINT ck_identity_version_snapshot_hash
                CHECK (snapshot_hash ~ '^[0-9a-f]{64}$'),
            CONSTRAINT ck_identity_version_validation_evidence
                CHECK (jsonb_typeof(validation_evidence) = 'object'),
            CONSTRAINT ck_identity_version_dependency_versions
                CHECK (jsonb_typeof(dependency_versions) = 'object'),
            CONSTRAINT ck_identity_version_preview_evidence
                CHECK (jsonb_typeof(preview_evidence) = 'object')
        )
        """
    )
    op.execute(
        """
        CREATE TABLE identity.active_pointer (
            namespace TEXT NOT NULL,
            scope TEXT NOT NULL,
            version BIGINT NOT NULL,
            PRIMARY KEY (namespace, scope),
            CONSTRAINT fk_identity_active_pointer_version
                FOREIGN KEY (namespace, scope, version)
                REFERENCES identity.version(namespace, scope, version),
            CONSTRAINT ck_identity_active_pointer_namespace
                CHECK (namespace = 'identity'),
            CONSTRAINT ck_identity_active_pointer_scope
                CHECK (scope = 'PLATFORM'),
            CONSTRAINT ck_identity_active_pointer_version
                CHECK (version > 0)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_identity_policy_key_namespace ON identity.policy_key(namespace, key)"
    )
    op.execute(
        "CREATE INDEX ix_identity_draft_lookup ON identity.draft(namespace, scope, status, stale)"
    )
    op.execute(
        "CREATE INDEX ix_identity_draft_owner_activity "
        "ON identity.draft(owner_id, status, last_meaningful_activity_at)"
    )
    op.execute(
        "CREATE INDEX ix_identity_version_published_at "
        "ON identity.version(namespace, scope, published_at)"
    )
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'configuration_rw') THEN
                CREATE ROLE configuration_rw NOLOGIN;
            END IF;
        END $$
        """
    )
    op.execute("GRANT USAGE ON SCHEMA identity TO configuration_rw")
    op.execute("GRANT SELECT ON identity.policy_key TO configuration_rw")
    op.execute("GRANT SELECT, INSERT, UPDATE ON identity.draft TO configuration_rw")
    op.execute("GRANT SELECT ON identity.version TO configuration_rw")
    op.execute("GRANT SELECT ON identity.active_pointer TO configuration_rw")
    op.get_bind().exec_driver_sql(_SEED_SQL)


def downgrade() -> None:
    op.execute("REVOKE ALL ON identity.active_pointer FROM configuration_rw")
    op.execute("REVOKE ALL ON identity.version FROM configuration_rw")
    op.execute("REVOKE ALL ON identity.draft FROM configuration_rw")
    op.execute("REVOKE ALL ON identity.policy_key FROM configuration_rw")
    op.execute("REVOKE USAGE ON SCHEMA identity FROM configuration_rw")
    op.execute("DROP TABLE identity.active_pointer")
    op.execute("DROP TABLE identity.version")
    op.execute("DROP TABLE identity.draft")
    op.execute("DROP TABLE identity.policy_key")
