"""Immutable, locally measured material copies; no effective Model capabilities."""

from alembic import op

revision = "0007_model_material_sources"
down_revision = "0006_model_validation_dossiers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION model_gateway.valid_source_check(p JSONB) RETURNS BOOLEAN
        LANGUAGE plpgsql IMMUTABLE AS $$
        DECLARE key TEXT; metadata_count INTEGER;
        BEGIN
            IF (jsonb_typeof(p)='object' AND p ?& ARRAY[
                'id','deployment_id','candidate_revision','dossier_id','dossier_snapshot_hash',
                'material_index','source_reference','external_version','declared_content_sha256',
                'source_id','source_version','entry_fingerprint','approved_copy_sha256',
                'observed_sha256','observed_bytes','result','reason','created_by','created_at']
                AND p - ARRAY[
                'id','deployment_id','candidate_revision','dossier_id','dossier_snapshot_hash',
                'material_index','source_reference','external_version','declared_content_sha256',
                'source_id','source_version','entry_fingerprint','approved_copy_sha256',
                'observed_sha256','observed_bytes','result','reason','created_by','created_at']
                ='{}'::jsonb) IS NOT TRUE THEN RETURN FALSE; END IF;
            FOREACH key IN ARRAY ARRAY['dossier_snapshot_hash','declared_content_sha256',
                'entry_fingerprint','approved_copy_sha256','observed_sha256'] LOOP
                IF p->key<>'null'::jsonb AND (jsonb_typeof(p->key)='string' AND
                    p->>key ~ '^[0-9a-f]{64}$') IS NOT TRUE THEN RETURN FALSE; END IF;
            END LOOP;
            SELECT count(*) INTO metadata_count FROM unnest(ARRAY[
                'source_id','source_version','entry_fingerprint','approved_copy_sha256']) k
                WHERE p->k<>'null'::jsonb;
            IF metadata_count NOT IN (0,4) THEN RETURN FALSE; END IF;
            FOREACH key IN ARRAY ARRAY['source_id','source_version'] LOOP
                IF p->key<>'null'::jsonb AND (jsonb_typeof(p->key)='string' AND
                    length(p->>key) BETWEEN 1 AND 64 AND
                    p->>key ~ '^[A-Za-z0-9][A-Za-z0-9._-]*$') IS NOT TRUE
                THEN RETURN FALSE; END IF;
            END LOOP;
            IF (p->'observed_sha256'='null'::jsonb) <>
               (p->'observed_bytes'='null'::jsonb) THEN RETURN FALSE; END IF;
            IF p->'observed_bytes'<>'null'::jsonb THEN
                IF (metadata_count=4 AND jsonb_typeof(p->'observed_bytes')='number' AND
                    p->>'observed_bytes' ~ '^[1-9][0-9]*$' AND
                    (p->>'observed_bytes')::integer BETWEEN 1 AND 65536) IS NOT TRUE
                THEN RETURN FALSE; END IF;
            END IF;
            IF p->>'result' IN ('MATCHED','MISMATCH') THEN
                IF (metadata_count=4 AND p->'declared_content_sha256'<>'null'::jsonb AND
                    p->'observed_sha256'=p->'approved_copy_sha256' AND
                    p->'observed_bytes'<>'null'::jsonb) IS NOT TRUE THEN RETURN FALSE; END IF;
                IF p->>'result'='MATCHED' THEN
                    RETURN p->'reason'='null'::jsonb AND
                        p->'observed_sha256'=p->'declared_content_sha256';
                END IF;
                RETURN p->>'reason'='DECLARED_HASH_MISMATCH' AND
                    p->'observed_sha256'<>p->'declared_content_sha256';
            END IF;
            IF (p->>'result'='BLOCKED') IS NOT TRUE THEN RETURN FALSE; END IF;
            IF p->>'reason'='DECLARED_HASH_MISSING' THEN
                RETURN p->'declared_content_sha256'='null'::jsonb AND
                    metadata_count=0 AND p->'observed_sha256'='null'::jsonb;
            END IF;
            IF p->'declared_content_sha256'='null'::jsonb THEN RETURN FALSE; END IF;
            IF p->>'reason'='APPROVED_COPY_HASH_MISMATCH' THEN
                RETURN metadata_count=4 AND p->'observed_sha256'<>'null'::jsonb AND
                    p->'observed_sha256'<>p->'approved_copy_sha256';
            END IF;
            RETURN p->'observed_sha256'='null'::jsonb AND p->>'reason' IN (
                'SOURCE_DIRECTORY_UNCONFIGURED','SOURCE_DIRECTORY_INVALID',
                'SOURCE_ENVIRONMENT_MISMATCH','SOURCE_NOT_APPROVED','SOURCE_COPY_UNAVAILABLE',
                'SOURCE_COPY_NOT_REGULAR','SOURCE_COPY_TOO_LARGE','SOURCE_COPY_CHANGED',
                'SOURCE_COPY_EMPTY');
        EXCEPTION WHEN OTHERS THEN RETURN FALSE;
        END $$
    """)
    op.execute("""
        CREATE TABLE model_gateway.material_source_check (
            id UUID PRIMARY KEY,
            deployment_id UUID NOT NULL REFERENCES model_gateway.deployment(id),
            candidate_revision INTEGER NOT NULL CHECK (candidate_revision>=1),
            dossier_id UUID NOT NULL REFERENCES model_gateway.validation_dossier(id),
            material_index INTEGER NOT NULL CHECK (material_index BETWEEN 0 AND 31),
            created_by TEXT NOT NULL CHECK (length(created_by)>0),
            created_at TIMESTAMPTZ NOT NULL,
            snapshot_text TEXT NOT NULL CHECK (octet_length(snapshot_text)<=32768),
            snapshot_hash TEXT NOT NULL CHECK (snapshot_hash ~ '^[0-9a-f]{64}$' AND
                snapshot_hash=encode(sha256(convert_to(snapshot_text,'UTF8')),'hex')),
            CHECK (model_gateway.valid_source_check(snapshot_text::jsonb) IS TRUE)
        )
    """)
    op.execute("""
        CREATE FUNCTION model_gateway.guard_material_source_check() RETURNS TRIGGER
        LANGUAGE plpgsql AS $$
        DECLARE p JSONB; material JSONB; candidate model_gateway.deployment;
            dossier model_gateway.validation_dossier;
        BEGIN
            IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'source checks are immutable'; END IF;
            p := NEW.snapshot_text::jsonb;
            IF model_gateway.valid_source_check(p) IS NOT TRUE
            THEN RAISE EXCEPTION 'invalid source check snapshot'; END IF;
            IF ((p->>'id')::uuid,(p->>'deployment_id')::uuid,
                (p->>'candidate_revision')::integer,(p->>'dossier_id')::uuid,
                (p->>'material_index')::integer,p->>'created_by',
                (p->>'created_at')::timestamptz) IS DISTINCT FROM
                (NEW.id,NEW.deployment_id,NEW.candidate_revision,NEW.dossier_id,
                 NEW.material_index,NEW.created_by,NEW.created_at)
            THEN RAISE EXCEPTION 'source check identity mismatch'; END IF;
            SELECT * INTO candidate FROM model_gateway.deployment
                WHERE id=NEW.deployment_id FOR UPDATE;
            IF NOT FOUND OR candidate.state<>'DRAFT' OR candidate.revision<>NEW.candidate_revision
            THEN RAISE EXCEPTION 'source check candidate changed'; END IF;
            SELECT * INTO dossier FROM model_gateway.validation_dossier WHERE id=NEW.dossier_id;
            IF NOT FOUND OR dossier.deployment_id<>NEW.deployment_id OR
                dossier.candidate_revision<>NEW.candidate_revision OR
                dossier.snapshot_hash IS DISTINCT FROM p->>'dossier_snapshot_hash'
            THEN RAISE EXCEPTION 'source check dossier binding mismatch'; END IF;
            material := dossier.snapshot_text::jsonb->'materials'->NEW.material_index;
            IF material IS NULL OR (material->'source_reference',material->'external_version',
                material->'declared_content_sha256') IS DISTINCT FROM
                (p->'source_reference',p->'external_version',p->'declared_content_sha256')
            THEN RAISE EXCEPTION 'source check material binding mismatch'; END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER material_source_check_guard BEFORE INSERT OR UPDATE OR DELETE
        ON model_gateway.material_source_check
        FOR EACH ROW EXECUTE FUNCTION model_gateway.guard_material_source_check()
    """)
    op.execute(
        "CREATE INDEX ix_model_material_source_history ON model_gateway.material_source_check"
        "(deployment_id,dossier_id,created_at DESC,id DESC)"
    )
    op.execute("GRANT SELECT,INSERT ON model_gateway.material_source_check TO model_gateway_rw")
    op.execute(
        "REVOKE ALL ON FUNCTION model_gateway.valid_source_check(JSONB), "
        "model_gateway.guard_material_source_check() FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION model_gateway.valid_source_check(JSONB) TO model_gateway_rw"
    )


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM model_gateway.material_source_check) THEN
            RAISE EXCEPTION 'material source facts prevent downgrade';
        END IF;
    END $$""")
    op.execute("DROP TABLE model_gateway.material_source_check")
    op.execute("DROP FUNCTION model_gateway.guard_material_source_check()")
    op.execute("DROP FUNCTION model_gateway.valid_source_check(JSONB)")
